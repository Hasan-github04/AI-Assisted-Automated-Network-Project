"""
deployer.py
===========
Automated configuration deployment via Netmiko (Cisco IOS) and raw Telnet (VPCS).

Supported node types:
  - dynamips (c7200, c2691, etc.) → Netmiko cisco_ios_telnet
  - iou                            → Netmiko cisco_ios_telnet
  - vpcs                           → Raw socket Telnet (VPCS CLI)
  - ethernet_switch, cloud, nat    → Skipped (no configurable CLI)

VPCS note:
  VPCS uses its own simple CLI, NOT Cisco IOS. Commands are like:
    ip 192.168.10.2/24 192.168.10.1   (IP/prefix gateway)
    save
  The AI is instructed to generate these commands for VPCS host nodes.
"""

import json
import logging
import os
import socket
import time
from datetime import datetime, timezone
from typing import Any

from netmiko import ConnectHandler, NetmikoAuthenticationException, NetmikoTimeoutException

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DEPLOY_LOGS_FILE = "deploy_logs.json"
DEFAULT_CONSOLE_HOST = "127.0.0.1"  # overridden per-node by console_host from topology

# GNS3 node types that run Cisco IOS → use Netmiko
IOS_NODE_TYPES = {"dynamips", "iou", "docker"}

# GNS3 node types with no configurable console → skip
SKIP_NODE_TYPES = {"ethernet_switch", "cloud", "nat", "frame_relay_switch", "atm_switch"}

# ---------------------------------------------------------------------------
# Netmiko helper (IOS devices)
# ---------------------------------------------------------------------------

def _build_netmiko_device(console_port: int, console_host: str = DEFAULT_CONSOLE_HOST) -> dict:
    """
    Netmiko device dict for a GNS3 simulated Cisco IOS device.
    global_delay_factor=4 is critical — emulated IOS is much slower than real HW.
    console_host comes from topology.json (may be GNS3 VM IP, not 127.0.0.1).
    """
    return {
        "device_type": "cisco_ios_telnet",
        "host": console_host,
        "port": console_port,
        "username": "",
        "password": "",
        "secret": "",
        "timeout": 120,
        "session_timeout": 120,
        "conn_timeout": 60,
        "global_delay_factor": 4,
        "fast_cli": False,
        "read_timeout_override": 30,
    }


def deploy_ios_device(device_name: str, commands: list[str], console_port: int, console_host: str = DEFAULT_CONSOLE_HOST) -> dict[str, Any]:
    """Deploy IOS CLI commands to a Cisco IOS device via Netmiko Telnet."""
    timestamp = datetime.now(timezone.utc).isoformat()
    logger.info("IOS deploy → %s (%s:%d, %d cmds)", device_name, console_host, console_port, len(commands))

    meta_enter = {"conf t", "configure terminal"}
    meta_exit  = {"end", "exit"}
    meta_save  = {"write memory", "wr", "copy running-config startup-config"}

    config_cmds, save_cmds = [], []
    for cmd in commands:
        cl = cmd.strip().lower()
        if cl in meta_enter or cl in meta_exit:
            continue
        elif cl in meta_save:
            save_cmds.append(cmd.strip())
        else:
            config_cmds.append(cmd.strip())

    full_output = ""
    try:
        with ConnectHandler(**_build_netmiko_device(console_port, console_host)) as conn:
            logger.info("  ✔ Connected (IOS): %s", device_name)
            if config_cmds:
                out = conn.send_config_set(
                    config_cmds, cmd_verify=False, delay_factor=4,
                    enter_config_mode=True, exit_config_mode=True,
                )
                full_output += out
            for sc in (save_cmds or ["write memory"]):
                out = conn.send_command(sc, expect_string=r"\[OK\]|Copy complete|#", read_timeout=30)
                full_output += f"\n{out}"
        logger.info("  ✅ SUCCESS (IOS): %s", device_name)
        return {"device": device_name, "status": "success", "output": full_output,
                "error": None, "timestamp": timestamp, "commands_sent": len(config_cmds)}

    except NetmikoTimeoutException as exc:
        msg = (
            f"Telnet timeout on {device_name} ({console_host}:{console_port}). "
            "Device may still be booting — wait ~90 s for c7200/c2691, then retry."
        )
        logger.error("  ❌ TIMEOUT (IOS): %s", device_name)
        return {"device": device_name, "status": "failed", "output": full_output,
                "error": msg, "timestamp": timestamp, "commands_sent": 0}

    except NetmikoAuthenticationException as exc:
        msg = (
            f"Auth error on {device_name} ({console_host}:{console_port}). "
            "IOS may still be initializing. Open the GNS3 console for this device, "
            "press Enter, and confirm you see 'Router>' before retrying."
        )
        logger.error("  ❌ AUTH ERROR (IOS): %s", device_name)
        return {"device": device_name, "status": "failed", "output": full_output,
                "error": msg, "timestamp": timestamp, "commands_sent": 0}

    except Exception as exc:
        msg = f"Error deploying to {device_name}: {type(exc).__name__}: {exc}"
        logger.exception("  ❌ ERROR (IOS): %s", device_name)
        return {"device": device_name, "status": "failed", "output": full_output,
                "error": msg, "timestamp": timestamp, "commands_sent": 0}


# ---------------------------------------------------------------------------
# Raw socket Telnet helper (VPCS devices)
# ---------------------------------------------------------------------------

def _vpcs_send(sock: socket.socket, cmd: str, delay: float = 0.5) -> str:
    """Send a command to a VPCS console and return the response."""
    sock.sendall((cmd + "\n").encode("utf-8"))
    time.sleep(delay)
    chunks = []
    sock.settimeout(2.0)
    try:
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            chunks.append(chunk.decode("utf-8", errors="ignore"))
    except socket.timeout:
        pass
    return "".join(chunks)


def deploy_vpcs_device(device_name: str, commands: list[str], console_port: int, console_host: str = DEFAULT_CONSOLE_HOST) -> dict[str, Any]:
    """
    Deploy VPCS commands to a VPCS host via raw socket Telnet.

    VPCS uses its own CLI (not IOS). Expected commands look like:
      ip 192.168.10.2/24 192.168.10.1
      save
    """
    timestamp = datetime.now(timezone.utc).isoformat()
    logger.info("VPCS deploy → %s (%s:%d, %d cmds)", device_name, console_host, console_port, len(commands))

    full_output = ""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(10)
        sock.connect((console_host, console_port))
        logger.info("  ✔ Connected (VPCS socket): %s", device_name)

        # Drain the initial banner
        time.sleep(1.0)
        sock.settimeout(2.0)
        try:
            banner = sock.recv(4096).decode("utf-8", errors="ignore")
            full_output += banner
            logger.debug("  VPCS banner: %s", banner.strip())
        except socket.timeout:
            pass

        # Send each command
        for cmd in commands:
            cmd = cmd.strip()
            if not cmd:
                continue
            logger.info("  VPCS cmd: %s", cmd)
            out = _vpcs_send(sock, cmd, delay=0.5)
            full_output += f"\n{cmd}\n{out}"
            logger.debug("  VPCS out: %s", out.strip())

        sock.close()
        logger.info("  ✅ SUCCESS (VPCS): %s", device_name)
        return {"device": device_name, "status": "success", "output": full_output,
                "error": None, "timestamp": timestamp, "commands_sent": len(commands)}

    except ConnectionRefusedError:
        msg = (
            f"VPCS socket refused on {device_name} ({console_host}:{console_port}). "
            "Verify the VPCS node is started in GNS3 and the console_host in topology.json is reachable."
        )
        logger.error("  ❌ REFUSED (VPCS): %s", device_name)
        return {"device": device_name, "status": "failed", "output": full_output,
                "error": msg, "timestamp": timestamp, "commands_sent": 0}

    except Exception as exc:
        msg = f"Error deploying to VPCS {device_name}: {type(exc).__name__}: {exc}"
        logger.exception("  ❌ ERROR (VPCS): %s", device_name)
        return {"device": device_name, "status": "failed", "output": full_output,
                "error": msg, "timestamp": timestamp, "commands_sent": 0}


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def _resolve_node(device_name: str, topology: dict) -> dict | None:
    for node in topology.get("nodes", []):
        if node["name"].strip().lower() == device_name.strip().lower():
            return node
    return None


def deploy_all(
    configs: dict[str, list[str]],
    topology: dict,
    progress_callback=None,
) -> list[dict[str, Any]]:
    """
    Deploy configurations to all devices in configs.

    Routing logic per node_type:
      dynamips / iou → Netmiko IOS Telnet
      vpcs           → Raw socket (VPCS CLI)
      ethernet_switch, cloud, nat → skipped (no configurable interface)
      unknown        → attempt Netmiko IOS Telnet
    """
    results = []
    logger.info("Starting deployment: %d device(s): %s", len(configs), list(configs.keys()))

    for device_name, commands in configs.items():
        if progress_callback:
            progress_callback(device_name, "connecting")

        ts = datetime.now(timezone.utc).isoformat()

        node = _resolve_node(device_name, topology)
        if node is None:
            msg = (f"'{device_name}' not found in topology.json. "
                   "Check that the device name in GNS3 matches exactly (case-sensitive).")
            logger.warning("  ⚠ NOT FOUND: %s", device_name)
            results.append({"device": device_name, "status": "failed", "output": "",
                            "error": msg, "timestamp": ts, "commands_sent": 0})
            if progress_callback:
                progress_callback(device_name, "failed")
            continue

        node_type = node.get("node_type", "").lower()
        console_port = node.get("console")
        # Use the node's console_host (may be GNS3 VM IP, not 127.0.0.1)
        console_host = node.get("console_host") or DEFAULT_CONSOLE_HOST

        # Skip non-configurable built-in nodes
        if node_type in SKIP_NODE_TYPES:
            msg = (f"'{device_name}' is a built-in GNS3 '{node_type}' node — "
                   "it has no configurable CLI and is skipped.")
            logger.info("  ⏭ SKIP (%s): %s", node_type, device_name)
            results.append({"device": device_name, "status": "skipped", "output": "",
                            "error": msg, "timestamp": ts, "commands_sent": 0})
            if progress_callback:
                progress_callback(device_name, "skipped")
            continue

        if not console_port:
            msg = (f"'{device_name}' has no console port — start the device in GNS3 first.")
            logger.warning("  ⚠ NO CONSOLE: %s", device_name)
            results.append({"device": device_name, "status": "failed", "output": "",
                            "error": msg, "timestamp": ts, "commands_sent": 0})
            if progress_callback:
                progress_callback(device_name, "failed")
            continue

        # Route to the correct deployer
        if node_type == "vpcs":
            result = deploy_vpcs_device(device_name, commands, int(console_port), console_host)
        else:
            # dynamips, iou, docker, or unknown → try IOS Netmiko
            result = deploy_ios_device(device_name, commands, int(console_port), console_host)

        results.append(result)
        if progress_callback:
            progress_callback(device_name, result["status"])

        time.sleep(2)

    ok      = sum(1 for r in results if r["status"] == "success")
    skipped = sum(1 for r in results if r["status"] == "skipped")
    failed  = sum(1 for r in results if r["status"] == "failed")
    logger.info("Deployment done: %d success / %d skipped / %d failed.", ok, skipped, failed)
    return results


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def save_deploy_logs(logs: list[dict], path: str = DEPLOY_LOGS_FILE) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(logs, fh, indent=2)
    logger.info("Deploy logs saved to %s", path)


def load_deploy_logs(path: str = DEPLOY_LOGS_FILE) -> list[dict]:
    if not os.path.exists(path):
        raise FileNotFoundError(f"'{path}' not found. Run deployment first.")
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)
