"""
intent_wizard.py
================
Intent collection logic.

Responsible for:
  - Merging topology facts with user-supplied intent data (from the browser form).
  - Basic validation of user input (IP format, VLAN range, required fields).
  - Persisting the merged result to intent.json.

Supported intent schema (all sections optional except ip_plan):
  vlans         - VLAN definitions (id, name, ports per switch)
  ip_plan       - Interface IP assignments per device
  routing       - Any protocol: static, rip, eigrp, ospf, bgp, or mixed
  security      - Multiple ACLs, NAT, port security
  services      - DHCP pools, NTP, syslog
  spanning_tree - STP mode and root bridge
  device_specific - Free-text instructions per device name
  additional_requirements - Global free-text for anything not covered above
"""

import ipaddress
import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

INTENT_FILE = "intent.json"
TOPOLOGY_FILE = "topology.json"

VALID_ROUTING_PROTOCOLS = {"static", "rip", "eigrp", "ospf", "bgp", "mixed", "connected"}
VALID_STP_MODES = {"pvst", "rapid-pvst", "mst", ""}
VALID_NAT_TYPES = {"static", "dynamic", "pat", "overload", ""}


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------

def load_topology(path: str = TOPOLOGY_FILE) -> dict:
    """Load topology.json from disk."""
    if not os.path.exists(path):
        raise FileNotFoundError(f"'{path}' not found. Run topology discovery first.")
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def load_intent(path: str = INTENT_FILE) -> dict:
    """Load intent.json from disk (raises if not yet generated)."""
    if not os.path.exists(path):
        raise FileNotFoundError(f"'{path}' not found. Submit the intent wizard first.")
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def _validate_ip(ip_str: str) -> bool:
    """Return True if ip_str is a valid IPv4 address."""
    try:
        ipaddress.IPv4Address(ip_str)
        return True
    except ValueError:
        return False


def _validate_network(cidr_str: str) -> bool:
    """Return True if cidr_str is a valid IPv4 network in CIDR notation."""
    try:
        ipaddress.IPv4Network(cidr_str, strict=False)
        return True
    except ValueError:
        return False


def _validate_mask(mask_str: str) -> bool:
    """Return True if mask_str is a valid dotted-decimal subnet mask."""
    try:
        ipaddress.IPv4Network(f"0.0.0.0/{mask_str}", strict=False)
        return True
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Main validation
# ---------------------------------------------------------------------------

def validate_intent(intent: dict) -> list[str]:
    """
    Validate an intent dict.

    Returns a list of error message strings.  An empty list means the intent
    is valid and ready for AI generation.
    """
    errors: list[str] = []

    # ---- VLANs ---------------------------------------------------------------
    vlans = intent.get("vlans", [])
    vlan_ids_seen: set[int] = set()
    for i, vlan in enumerate(vlans):
        vid = vlan.get("id")
        if vid is None:
            errors.append(f"VLAN[{i}]: missing 'id' field.")
            continue
        try:
            vid_int = int(vid)
        except (TypeError, ValueError):
            errors.append(f"VLAN[{i}]: id '{vid}' is not an integer.")
            continue
        if not (1 <= vid_int <= 4094):
            errors.append(f"VLAN[{i}]: id {vid_int} is out of range (1-4094).")
        if vid_int in vlan_ids_seen:
            errors.append(f"VLAN[{i}]: duplicate VLAN id {vid_int}.")
        vlan_ids_seen.add(vid_int)
        if not vlan.get("name"):
            errors.append(f"VLAN[{i}]: missing 'name' field.")

    # ---- IP Plan -------------------------------------------------------------
    ip_plan = intent.get("ip_plan", [])
    if not ip_plan:
        errors.append("ip_plan is empty — provide at least one interface IP assignment.")

    for i, entry in enumerate(ip_plan):
        ip = entry.get("ip", "")
        mask = entry.get("mask", "")
        if not _validate_ip(ip):
            errors.append(f"ip_plan[{i}]: '{ip}' is not a valid IPv4 address.")
        if not _validate_mask(mask):
            errors.append(f"ip_plan[{i}]: '{mask}' is not a valid subnet mask.")
        if not entry.get("device"):
            errors.append(f"ip_plan[{i}]: missing 'device' field.")
        if not entry.get("interface"):
            errors.append(f"ip_plan[{i}]: missing 'interface' field.")

    # ---- Routing -------------------------------------------------------------
    routing = intent.get("routing", {})
    protocol = routing.get("protocol", "static")
    if protocol not in VALID_ROUTING_PROTOCOLS:
        errors.append(f"routing.protocol '{protocol}' is not valid. Choose from: {', '.join(sorted(VALID_ROUTING_PROTOCOLS))}")

    # Static routes
    for k, route in enumerate(routing.get("static_routes", [])):
        network = route.get("network", "")
        next_hop = route.get("next_hop", "")
        if network and not _validate_network(network):
            errors.append(f"routing.static_routes[{k}]: network '{network}' is not valid CIDR.")
        if next_hop and not _validate_ip(next_hop):
            errors.append(f"routing.static_routes[{k}]: next_hop '{next_hop}' is not a valid IP.")

    # OSPF
    ospf = routing.get("ospf", {})
    for j, net in enumerate(ospf.get("networks", [])):
        network = net.get("network", "")
        if network and not _validate_network(network):
            errors.append(f"routing.ospf.networks[{j}]: '{network}' is not valid CIDR.")

    # EIGRP
    eigrp = routing.get("eigrp", {})
    for j, net in enumerate(eigrp.get("networks", [])):
        network = net.get("network", "")
        if network and not _validate_network(network):
            errors.append(f"routing.eigrp.networks[{j}]: '{network}' is not valid CIDR.")

    # BGP
    bgp = routing.get("bgp", {})
    for j, nb in enumerate(bgp.get("neighbors", [])):
        ip = nb.get("ip", "")
        if ip and not _validate_ip(ip):
            errors.append(f"routing.bgp.neighbors[{j}]: ip '{ip}' is not valid.")

    # ---- Security: Multiple ACLs ---------------------------------------------
    security = intent.get("security", {})
    for i, acl in enumerate(security.get("acls", [])):
        if not acl.get("name"):
            errors.append(f"security.acls[{i}]: missing 'name' field.")
        for j, rule in enumerate(acl.get("rules", [])):
            src = rule.get("src", "")
            dst = rule.get("dst", "")
            if src and src.lower() != "any" and not _validate_network(src):
                errors.append(f"security.acls[{i}].rules[{j}]: src '{src}' is not valid CIDR.")
            if dst and dst.lower() != "any" and not _validate_network(dst):
                errors.append(f"security.acls[{i}].rules[{j}]: dst '{dst}' is not valid CIDR.")
            if rule.get("action") not in ("permit", "deny"):
                errors.append(f"security.acls[{i}].rules[{j}]: action must be 'permit' or 'deny'.")

    # NAT
    nat = security.get("nat", {})
    if nat.get("enabled"):
        nat_type = nat.get("type", "")
        if nat_type not in VALID_NAT_TYPES:
            errors.append(f"security.nat.type '{nat_type}' is not valid. Choose: static, dynamic, pat.")
        if not nat.get("inside_interface"):
            errors.append("security.nat: 'inside_interface' is required when NAT is enabled.")
        if not nat.get("outside_interface"):
            errors.append("security.nat: 'outside_interface' is required when NAT is enabled.")

    # DHCP pools
    services = intent.get("services", {})
    for i, pool in enumerate(services.get("dhcp_pools", [])):
        network = pool.get("network", "")
        gateway = pool.get("gateway", "")
        if network and not _validate_network(network):
            errors.append(f"services.dhcp_pools[{i}]: network '{network}' is not valid CIDR.")
        if gateway and not _validate_ip(gateway):
            errors.append(f"services.dhcp_pools[{i}]: gateway '{gateway}' is not a valid IP.")

    # STP
    stp = intent.get("spanning_tree", {})
    mode = stp.get("mode", "")
    if mode and mode not in VALID_STP_MODES:
        errors.append(f"spanning_tree.mode '{mode}' is not valid. Choose from: pvst, rapid-pvst, mst.")

    if errors:
        logger.warning("Intent validation found %d error(s).", len(errors))
        for err in errors:
            logger.warning("  - %s", err)
    else:
        logger.info("Intent validation passed — no errors found.")

    return errors


# ---------------------------------------------------------------------------
# Build intent
# ---------------------------------------------------------------------------

def build_intent(form_data: dict[str, Any], topology: dict) -> dict:
    """
    Merge topology facts with user-supplied form data into a single intent dict.

    Supports the full expanded schema including all routing protocols,
    multiple ACLs, NAT, DHCP, port-security, STP, and per-device instructions.
    """
    logger.info("Building intent from wizard data ...")

    # --- Routing: normalise into unified structure ---
    routing_raw = form_data.get("routing", {})
    # Support old schema (type/routes) and new schema (protocol/static_routes)
    if "type" in routing_raw and "protocol" not in routing_raw:
        routing_raw["protocol"] = routing_raw.pop("type")
    if "routes" in routing_raw and "static_routes" not in routing_raw:
        routing_raw["static_routes"] = routing_raw.pop("routes")

    routing = {
        "protocol": routing_raw.get("protocol", "static"),
        "static_routes": routing_raw.get("static_routes", []),
        "ospf": routing_raw.get("ospf", {}),
        "rip": routing_raw.get("rip", {}),
        "eigrp": routing_raw.get("eigrp", {}),
        "bgp": routing_raw.get("bgp", {}),
    }

    # --- Security: normalise ACLs ---
    security_raw = form_data.get("security", {})

    # Support old single-acl schema (acl key at top level)
    acls = security_raw.get("acls", [])
    if not acls:
        # Check for legacy top-level acl
        legacy_acl = form_data.get("acl", {})
        if legacy_acl and legacy_acl.get("name"):
            acls = [legacy_acl]

    security = {
        "acls": acls,
        "nat": security_raw.get("nat", {"enabled": False}),
        "port_security": security_raw.get("port_security", []),
    }

    # --- Services ---
    services_raw = form_data.get("services", {})
    services = {
        "dhcp_pools": services_raw.get("dhcp_pools", []),
        "ntp_server": services_raw.get("ntp_server", ""),
        "syslog_server": services_raw.get("syslog_server", ""),
    }

    # --- Spanning Tree ---
    spanning_tree = form_data.get("spanning_tree", {})

    # --- Per-device free-text instructions ---
    device_specific = form_data.get("device_specific", {})

    intent = {
        "topology": topology,
        "vlans": form_data.get("vlans", []),
        "ip_plan": form_data.get("ip_plan", []),
        "routing": routing,
        "security": security,
        "services": services,
        "spanning_tree": spanning_tree,
        "device_specific": device_specific,
        "additional_requirements": form_data.get("additional_requirements", ""),
        # Legacy compat
        "constraints": form_data.get("constraints", form_data.get("additional_requirements", "")),
    }

    # Coerce VLAN IDs to integers
    for vlan in intent["vlans"]:
        if "id" in vlan:
            try:
                vlan["id"] = int(vlan["id"])
            except (TypeError, ValueError):
                pass

    logger.info(
        "Intent built: %d VLANs, %d IP entries, protocol=%s, %d ACL(s), NAT=%s, "
        "%d DHCP pool(s), %d device-specific instructions",
        len(intent["vlans"]),
        len(intent["ip_plan"]),
        routing["protocol"],
        len(acls),
        security["nat"].get("enabled", False),
        len(services["dhcp_pools"]),
        len(device_specific),
    )
    return intent


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def save_intent(intent: dict, path: str = INTENT_FILE) -> None:
    """Write intent dict to intent.json (pretty-printed)."""
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(intent, fh, indent=2)
    logger.info("Intent saved to %s", path)
