"""
intent_wizard.py
================
Intent collection logic.

Responsible for:
  - Merging topology facts with user-supplied intent data (from the browser form).
  - Basic validation of user input (IP format, VLAN range, required fields).
  - Persisting the merged result to intent.json.
"""

import ipaddress
import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

INTENT_FILE = "intent.json"
TOPOLOGY_FILE = "topology.json"


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
        # Convert to prefix length to validate
        ipaddress.IPv4Network(f"0.0.0.0/{mask_str}", strict=False)
        return True
    except ValueError:
        return False


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

    # ---- ACL -----------------------------------------------------------------
    acl = intent.get("acl", {})
    if acl:
        for j, rule in enumerate(acl.get("rules", [])):
            src = rule.get("src", "")
            dst = rule.get("dst", "")
            if src and not _validate_network(src):
                errors.append(f"acl.rules[{j}]: src '{src}' is not a valid CIDR network.")
            if dst and not _validate_network(dst):
                errors.append(f"acl.rules[{j}]: dst '{dst}' is not a valid CIDR network.")
            if rule.get("action") not in ("permit", "deny"):
                errors.append(
                    f"acl.rules[{j}]: action must be 'permit' or 'deny', got '{rule.get('action')}'."
                )

    # ---- Routing -------------------------------------------------------------
    routing = intent.get("routing", {})
    if routing.get("type") == "static":
        for k, route in enumerate(routing.get("routes", [])):
            network = route.get("network", "")
            next_hop = route.get("next_hop", "")
            if network and not _validate_network(network):
                errors.append(f"routing.routes[{k}]: network '{network}' is not valid CIDR.")
            if next_hop and not _validate_ip(next_hop):
                errors.append(f"routing.routes[{k}]: next_hop '{next_hop}' is not a valid IP.")

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

    form_data is the parsed JSON body from the browser wizard POST request.
    It is expected to have the following top-level keys:
      - vlans          : list of {id, name, ports (list of "NodeName:IfaceName")}
      - ip_plan        : list of {device, interface, ip, mask, description}
      - acl            : {name, interface_applied, direction, rules (list of {action, src, dst, protocol})}
      - routing        : {type ("static"|"ospf"), routes (list of {network, next_hop})}
      - constraints    : free-text string
    """
    logger.info("Building intent from wizard data ...")

    intent = {
        "topology": topology,
        "vlans": form_data.get("vlans", []),
        "ip_plan": form_data.get("ip_plan", []),
        "acl": form_data.get("acl", {}),
        "routing": form_data.get("routing", {"type": "static", "routes": []}),
        "constraints": form_data.get("constraints", ""),
    }

    # Coerce VLAN IDs to integers
    for vlan in intent["vlans"]:
        if "id" in vlan:
            try:
                vlan["id"] = int(vlan["id"])
            except (TypeError, ValueError):
                pass

    logger.info(
        "Intent built: %d VLANs, %d IP entries, %d ACL rules, routing=%s",
        len(intent["vlans"]),
        len(intent["ip_plan"]),
        len(intent.get("acl", {}).get("rules", [])),
        intent["routing"].get("type", "none"),
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
