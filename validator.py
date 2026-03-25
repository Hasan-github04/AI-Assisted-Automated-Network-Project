"""
validator.py
============
Post-deployment validation with closed-loop retry.

Validation checks performed per device:
  Router:
    - show ip interface brief  → verify interfaces are up with correct IPs
    - show ip route            → verify expected routes exist
    - show access-lists        → verify ACL is configured
    - ping <host_ip>           → connectivity test per host IP in intent

  Switch:
    - show vlan brief          → verify VLANs exist and ports are assigned

Closed-loop retry:
  If any check fails, the failure evidence is sent to Claude for a delta fix.
  The corrective commands are deployed and validation re-runs.
  Maximum 2 retry attempts.
"""

import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Any

from netmiko import ConnectHandler, NetmikoTimeoutException

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# File paths
# ---------------------------------------------------------------------------
VALIDATION_FILE = "validation.json"
TOPOLOGY_FILE = "topology.json"
INTENT_FILE = "intent.json"

# Connection settings
DEFAULT_CONSOLE_HOST = "127.0.0.1"  # overridden per-node by console_host from topology
CONNECT_TIMEOUT = 60
COMMAND_TIMEOUT = 30

MAX_RETRY_ATTEMPTS = 2


# ---------------------------------------------------------------------------
# Netmiko helpers
# ---------------------------------------------------------------------------

def _build_netmiko_device(console_port: int, console_host: str = DEFAULT_CONSOLE_HOST) -> dict:
    return {
        "device_type": "cisco_ios_telnet",
        "host": console_host,
        "port": console_port,
        "username": "",
        "password": "",
        "secret": "",
        "timeout": CONNECT_TIMEOUT,
        "global_delay_factor": 4,
        "fast_cli": False,
    }


def _resolve_node(device_name: str, topology: dict) -> dict | None:
    for node in topology.get("nodes", []):
        if node["name"].strip().lower() == device_name.strip().lower():
            return node
    return None


def _get_console_info(device_name: str, topology: dict) -> tuple[int, str]:
    """Return (console_port, console_host) for device_name from topology."""
    node = _resolve_node(device_name, topology)
    if not node:
        raise ValueError(f"Device '{device_name}' not found in topology.")
    port = node.get("console")
    if not port:
        raise ValueError(f"Device '{device_name}' has no console port.")
    host = node.get("console_host") or DEFAULT_CONSOLE_HOST
    return int(port), host


def run_show_command(device_name: str, console_port: int, command: str, console_host: str = DEFAULT_CONSOLE_HOST) -> str:
    """
    Open a Telnet connection and run a single show/ping command.
    console_host comes from topology.json (may be GNS3 VM IP, not 127.0.0.1).
    """
    logger.info("  [%s] %s:%d → %s", device_name, console_host, console_port, command)
    device_dict = _build_netmiko_device(console_port, console_host)
    try:
        with ConnectHandler(**device_dict) as conn:
            # Use a generous read_timeout for ping commands (network RTT)
            read_timeout = 60 if command.startswith("ping") else COMMAND_TIMEOUT
            output = conn.send_command(
                command,
                read_timeout=read_timeout,
                expect_string=r"#",
            )
            logger.debug("  [%s] Output:\n%s", device_name, output)
            return output
    except NetmikoTimeoutException as exc:
        msg = f"ERROR: Telnet timeout connecting to {device_name} on port {console_port}."
        logger.error(msg)
        return msg
    except Exception as exc:
        msg = f"ERROR: {type(exc).__name__}: {exc}"
        logger.error("  [%s] Command failed: %s", device_name, msg)
        return msg


# ---------------------------------------------------------------------------
# Parse helpers
# ---------------------------------------------------------------------------

def _parse_ip_brief(output: str) -> dict[str, dict]:
    """
    Parse 'show ip interface brief' output.
    Returns dict of {interface: {ip, status, protocol}}
    """
    result = {}
    # Match lines like: GigabitEthernet0/0.10  192.168.10.1  YES manual up  up
    pattern = re.compile(
        r"^(\S+)\s+(\S+)\s+\w+\s+\S+\s+(\S+)\s+(\S+)",
        re.MULTILINE,
    )
    for m in pattern.finditer(output):
        iface, ip, status, protocol = m.group(1), m.group(2), m.group(3), m.group(4)
        result[iface] = {"ip": ip, "status": status, "protocol": protocol}
    return result


def _parse_vlan_brief(output: str) -> dict[int, str]:
    """
    Parse 'show vlan brief' output.
    Returns dict of {vlan_id: vlan_name}
    """
    result = {}
    pattern = re.compile(r"^(\d+)\s+(\S+)\s+active", re.MULTILINE)
    for m in pattern.finditer(output):
        vlan_id = int(m.group(1))
        vlan_name = m.group(2)
        result[vlan_id] = vlan_name
    return result


def _parse_routes(output: str) -> list[str]:
    """
    Parse 'show ip route' output.
    Returns list of network prefixes found.
    """
    routes = []
    # Lines starting with S (static), C (connected), O (OSPF), etc.
    pattern = re.compile(r"^[SCOBDIR*]\S*\s+(\d+\.\d+\.\d+\.\d+(?:/\d+)?)", re.MULTILINE)
    for m in pattern.finditer(output):
        routes.append(m.group(1))
    return routes


def _parse_ping(output: str) -> tuple[bool, str]:
    """
    Parse IOS extended ping output.
    Returns (success: bool, summary: str).
    """
    # Look for "Success rate is X percent"
    m = re.search(r"Success rate is (\d+) percent", output)
    if m:
        rate = int(m.group(1))
        return rate > 0, f"Success rate: {rate}%"
    # Fallback: look for !!!!!
    if "!!!!!" in output or "Success" in output:
        return True, "Ping successful"
    return False, "Ping failed or no response"


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

def _check_router(device_name: str, console_port: int, intent: dict, console_host: str = DEFAULT_CONSOLE_HOST) -> list[dict]:
    """Run all router validation checks. Returns list of check result dicts."""
    checks = []

    # --- show ip interface brief ---
    brief_output = run_show_command(device_name, console_port, "show ip interface brief", console_host)
    parsed_ifaces = _parse_ip_brief(brief_output)

    for entry in intent.get("ip_plan", []):
        if entry.get("device", "").lower() != device_name.lower():
            continue
        iface_name = entry["interface"]
        expected_ip = entry["ip"]

        # Find the interface in the parsed output (try exact match first, then partial)
        matched_iface = None
        for parsed_iface in parsed_ifaces:
            if parsed_iface.lower() == iface_name.lower() or iface_name.lower() in parsed_iface.lower():
                matched_iface = parsed_iface
                break

        if matched_iface:
            actual_ip = parsed_ifaces[matched_iface]["ip"]
            status = parsed_ifaces[matched_iface]["status"]
            proto = parsed_ifaces[matched_iface]["protocol"]
            passed = (actual_ip == expected_ip) and (status == "up") and (proto == "up")
            checks.append({
                "check": f"interface {iface_name} IP and state",
                "device": device_name,
                "expected": f"ip={expected_ip} status=up protocol=up",
                "actual": f"ip={actual_ip} status={status} protocol={proto}",
                "passed": passed,
                "output": brief_output,
            })
        else:
            checks.append({
                "check": f"interface {iface_name} exists",
                "device": device_name,
                "expected": f"ip={expected_ip}",
                "actual": "Interface not found in 'show ip interface brief'",
                "passed": False,
                "output": brief_output,
            })

    # --- show ip route ---
    route_output = run_show_command(device_name, console_port, "show ip route", console_host)
    actual_routes = _parse_routes(route_output)

    for route in intent.get("routing", {}).get("routes", []):
        network = route.get("network", "")
        if not network:
            continue
        # Normalize: 0.0.0.0/0 → look for 0.0.0.0
        expected_prefix = network.split("/")[0]
        found = any(expected_prefix in r for r in actual_routes)
        checks.append({
            "check": f"route to {network}",
            "device": device_name,
            "expected": f"route {network} in routing table",
            "actual": f"routes found: {actual_routes}" if actual_routes else "routing table empty",
            "passed": found,
            "output": route_output,
        })

    # --- show access-lists ---
    acl = intent.get("acl", {})
    if acl and acl.get("name"):
        acl_output = run_show_command(device_name, console_port, "show access-lists", console_host)
        acl_name = acl["name"]
        found_acl = acl_name in acl_output or "Extended" in acl_output or "Standard" in acl_output
        checks.append({
            "check": f"ACL '{acl_name}' configured",
            "device": device_name,
            "expected": f"ACL '{acl_name}' present in 'show access-lists'",
            "actual": "Present" if found_acl else "Not found",
            "passed": found_acl,
            "output": acl_output,
        })

    # --- ping tests ---
    host_ips = [
        entry["ip"]
        for entry in intent.get("ip_plan", [])
        if entry.get("device", "").lower() not in [device_name.lower(), ""]
        and not entry.get("interface", "").startswith("loopback")
    ]

    for host_ip in host_ips:
        ping_output = run_show_command(
            device_name, console_port,
            f"ping {host_ip} repeat 3",
            console_host,
        )
        success, summary = _parse_ping(ping_output)
        checks.append({
            "check": f"ping to {host_ip}",
            "device": device_name,
            "expected": "Ping success > 0%",
            "actual": summary,
            "passed": success,
            "output": ping_output,
        })

    return checks


def _check_switch(device_name: str, console_port: int, intent: dict, console_host: str = DEFAULT_CONSOLE_HOST) -> list[dict]:
    """Run all switch validation checks. Returns list of check result dicts."""
    checks = []

    vlan_output = run_show_command(device_name, console_port, "show vlan brief", console_host)
    actual_vlans = _parse_vlan_brief(vlan_output)

    for vlan in intent.get("vlans", []):
        vid = int(vlan.get("id", 0))
        vlan_name = vlan.get("name", "")
        found = vid in actual_vlans
        checks.append({
            "check": f"VLAN {vid} ({vlan_name}) exists",
            "device": device_name,
            "expected": f"VLAN {vid} active",
            "actual": f"VLAN {vid} {'found' if found else 'not found'} in 'show vlan brief'",
            "passed": found,
            "output": vlan_output,
        })

    return checks


# ---------------------------------------------------------------------------
# Orchestrate validation
# ---------------------------------------------------------------------------

def validate_all(intent: dict, topology: dict) -> dict[str, Any]:
    """
    Run all validation checks against all devices.

    Returns a result dict with:
      - checks: flat list of individual check results
      - summary: {total, passed, failed}
      - passed: bool (True only if ALL checks passed)
      - timestamp: ISO timestamp
    """
    logger.info("Starting validation ...")
    all_checks: list[dict] = []

    nodes_by_name = {n["name"].lower(): n for n in topology.get("nodes", [])}

    # Determine which devices to validate based on intent
    router_names = []
    switch_names = []

    for node in topology.get("nodes", []):
        name = node["name"]
        ntype = node.get("node_type", "").lower()
        console = node.get("console")
        if not console:
            logger.warning("Skipping %s — no console port.", name)
            continue

        # Routers: qemu or dynamips nodes (c7200 etc.)
        if any(kw in ntype for kw in ["dynamips", "qemu", "router", "iou"]):
            router_names.append(name)
        # Switches: ethernet switch or cloud nodes
        elif any(kw in ntype for kw in ["ethernet_switch", "switch", "l2"]):
            switch_names.append(name)
        else:
            # Heuristic: if device has ip_plan entries, treat as router; otherwise switch
            has_ip = any(
                e.get("device", "").lower() == name.lower()
                for e in intent.get("ip_plan", [])
            )
            if has_ip:
                router_names.append(name)
            else:
                switch_names.append(name)

    logger.info("Routers to validate: %s", router_names)
    logger.info("Switches to validate: %s", switch_names)

    for r_name in router_names:
        node = nodes_by_name.get(r_name.lower())
        if not node:
            continue
        port = node.get("console")
        console_host = node.get("console_host") or DEFAULT_CONSOLE_HOST
        if not port:
            continue
        try:
            checks = _check_router(r_name, int(port), intent, console_host)
            all_checks.extend(checks)
        except Exception as exc:
            logger.exception("Error validating router %s.", r_name)
            all_checks.append({
                "check": f"router {r_name} validation",
                "device": r_name,
                "expected": "validation run",
                "actual": f"ERROR: {exc}",
                "passed": False,
                "output": "",
            })

    for s_name in switch_names:
        node = nodes_by_name.get(s_name.lower())
        if not node:
            continue
        port = node.get("console")
        console_host = node.get("console_host") or DEFAULT_CONSOLE_HOST
        if not port:
            continue
        try:
            checks = _check_switch(s_name, int(port), intent, console_host)
            all_checks.extend(checks)
        except Exception as exc:
            logger.exception("Error validating switch %s.", s_name)
            all_checks.append({
                "check": f"switch {s_name} validation",
                "device": s_name,
                "expected": "validation run",
                "actual": f"ERROR: {exc}",
                "passed": False,
                "output": "",
            })

    # Summarize
    total = len(all_checks)
    passed_count = sum(1 for c in all_checks if c["passed"])
    failed_count = total - passed_count
    all_passed = failed_count == 0

    result = {
        "checks": all_checks,
        "summary": {"total": total, "passed": passed_count, "failed": failed_count},
        "passed": all_passed,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "retry_attempt": 0,
    }

    logger.info(
        "Validation complete: %d/%d checks passed%s.",
        passed_count,
        total,
        " ✅" if all_passed else " ❌",
    )
    return result


# ---------------------------------------------------------------------------
# Closed-loop retry
# ---------------------------------------------------------------------------

def closed_loop_retry(
    intent: dict,
    topology: dict,
    attempt: int = 1,
) -> dict[str, Any]:
    """
    Identify failures from a previous validation, request a delta fix from Claude,
    re-deploy, and re-validate.

    Args:
        intent:   Original intent dict.
        topology: Topology dict.
        attempt:  Current retry attempt number (1-based, max = MAX_RETRY_ATTEMPTS).

    Returns:
        New validation result dict (with retry_attempt set).
    """
    # Import here to avoid circular deps; these modules only needed at call time
    from ai_generator import generate_delta_fix, ConfigGenerationError
    from deployer import deploy_all, save_deploy_logs

    logger.info("--- Closed-loop retry attempt %d / %d ---", attempt, MAX_RETRY_ATTEMPTS)

    # Load current validation to get failures
    current_results = load_validation()
    failures = [c for c in current_results.get("checks", []) if not c["passed"]]

    if not failures:
        logger.info("No failures to retry — validation already passed.")
        return current_results

    logger.info("%d failing check(s) will be sent to Claude for a delta fix.", len(failures))

    try:
        delta_configs = generate_delta_fix(intent, failures)
    except ConfigGenerationError as exc:
        logger.error("Delta fix generation failed: %s", exc)
        return {**current_results, "retry_error": str(exc)}

    # Deploy delta fix
    deploy_results = deploy_all(delta_configs, topology)
    save_deploy_logs(deploy_results)

    # Wait for IOS to converge before re-validating
    logger.info("Waiting 5s for IOS to converge before re-validation ...")
    time.sleep(5)

    # Re-validate
    new_results = validate_all(intent, topology)
    new_results["retry_attempt"] = attempt
    save_validation(new_results)

    return new_results


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def save_validation(results: dict, path: str = VALIDATION_FILE) -> None:
    """Write validation results to validation.json (pretty-printed)."""
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(results, fh, indent=2)
    logger.info("Validation results saved to %s", path)


def load_validation(path: str = VALIDATION_FILE) -> dict:
    """Load validation results from disk."""
    if not os.path.exists(path):
        raise FileNotFoundError(f"'{path}' not found. Run validation first.")
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)
