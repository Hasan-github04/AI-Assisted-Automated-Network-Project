"""
gns3_client.py
==============
GNS3 REST API client for topology discovery.
Connects to the GNS3 server at http://localhost:3080/v2 and extracts:
  - All projects (to let the user pick one)
  - All nodes (name, type, node_id, console port, status)
  - All links (endpoints with adapter/port numbers)
  - Interface names per node
The result is saved to topology.json.
"""
import json
import logging
import os
from typing import Any
import requests
# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logger = logging.getLogger(__name__)
# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
GNS3_BASE_URL = os.environ.get("GNS3_URL", "http://localhost:3080/v2")
TOPOLOGY_FILE = "topology.json"
# GNS3 HTTP Basic Auth credentials.
# GNS3 2.x enables auth by default; check GNS3 → Edit → Preferences → Server
# for the username and password you set during installation.
GNS3_USER = os.environ.get("GNS3_USER", "admin")
GNS3_PASSWORD = os.environ.get("GNS3_PASSWORD", "")
_AUTH = (GNS3_USER, GNS3_PASSWORD) if GNS3_USER else None
# ---------------------------------------------------------------------------
# Helper: HTTP wrappers
# ---------------------------------------------------------------------------
def _get(path: str, timeout: int = 10) -> Any:
    """Perform a GET request against the GNS3 REST API (with Basic Auth if set)."""
    url = f"{GNS3_BASE_URL}{path}"
    logger.debug("GET %s (auth user=%s)", url, GNS3_USER or "<none>")
    try:
        response = requests.get(url, auth=_AUTH, timeout=timeout)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.ConnectionError as exc:
        raise ConnectionError(
            f"Cannot reach GNS3 at {GNS3_BASE_URL}. "
            "Is GNS3 running? Is the GNS3 VM started?"
        ) from exc
    except requests.exceptions.HTTPError as exc:
        if response.status_code == 401:
            raise RuntimeError(
                "GNS3 API returned 401 Unauthorized. "
                "Set GNS3_USER and GNS3_PASSWORD environment variables to match "
                "the credentials in GNS3 → Edit → Preferences → Server → Authentication. "
                f"Current user: '{GNS3_USER}'"
            ) from exc
        raise RuntimeError(
            f"GNS3 API error [{response.status_code}] for {url}: {response.text}"
        ) from exc
# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def get_projects() -> list[dict]:
    """
    Return a list of all GNS3 projects.
    Each entry contains: project_id, name, status.
    """
    raw = _get("/projects")
    projects = [
        {
            "project_id": p["project_id"],
            "name": p.get("name", "unnamed"),
            "status": p.get("status", "unknown"),
        }
        for p in raw
    ]
    logger.info("Found %d GNS3 project(s).", len(projects))
    return projects
def get_active_project() -> dict | None:
    """
    Return the first project whose status is 'opened', or None if none found.
    Useful for auto-selection when only one project is active.
    """
    for p in get_projects():
        if p["status"] == "opened":
            logger.info("Auto-selected active project: %s (%s)", p["name"], p["project_id"])
            return p
    logger.warning("No currently opened GNS3 project found.")
    return None
def _get_node_ports(project_id: str, node_id: str) -> list[dict]:
    """
    Fetch the port list for a node.
    Returns a list of dicts with adapter_number, port_number, name (interface name).
    """
    try:
        ports = _get(f"/projects/{project_id}/nodes/{node_id}/links")
        # GNS3 sometimes returns ports directly; fall back to empty list
        return ports if isinstance(ports, list) else []
    except Exception:
        return []
def _resolve_interface_name(node_ports: list[dict], adapter: int, port: int) -> str:
    """
    Given a list of port descriptors and an adapter+port number,
    return the human-readable interface name (e.g. 'GigabitEthernet0/0').
    Falls back to 'AdapterX/PortY' format if not found.
    """
    for p in node_ports:
        if p.get("adapter_number") == adapter and p.get("port_number") == port:
            return p.get("name", f"Adapter{adapter}/{port}")
    return f"Adapter{adapter}/Port{port}"
def discover_topology(project_id: str) -> dict:
    """
    Discover the full topology for the given GNS3 project.
    Returns a dict with:
      - project_id, project_name
      - nodes: list of node dicts
      - links: list of link dicts with resolved interface names
    Raises RuntimeError if the project cannot be found or has no nodes.
    """
    logger.info("Starting topology discovery for project %s ...", project_id)
    # ---- Fetch project info -----------------------------------------------
    project_info = _get(f"/projects/{project_id}")
    project_name = project_info.get("name", "unknown")
    logger.info("Project name: %s", project_name)
    # ---- Fetch nodes -------------------------------------------------------
    raw_nodes = _get(f"/projects/{project_id}/nodes")
    if not raw_nodes:
        raise RuntimeError(
            f"Project '{project_name}' has no nodes. "
            "Please add devices to the topology in GNS3 first."
        )
    nodes = []
    node_port_map: dict[str, list[dict]] = {}  # node_id -> list of port dicts
    for n in raw_nodes:
        node_id = n["node_id"]
        # Fetch per-node ports for interface name resolution
        ports_raw = _get(f"/projects/{project_id}/nodes/{node_id}")
        port_list = ports_raw.get("ports", [])
        node_port_map[node_id] = port_list
        node_entry = {
            "node_id": node_id,
            "name": n.get("name", "unknown"),
            "node_type": n.get("node_type", "unknown"),
            "console": n.get("console"),          # Telnet console port (int or None)
            "console_host": n.get("console_host", "127.0.0.1"),
            "status": n.get("status", "unknown"),
            "interfaces": [
                {
                    "adapter_number": p.get("adapter_number"),
                    "port_number": p.get("port_number"),
                    "name": p.get("name", f"port{p.get('port_number', '?')}"),
                    "link_type": p.get("link_type", "ethernet"),
                }
                for p in port_list
            ],
        }
        nodes.append(node_entry)
        logger.info(
            "  Node: %-20s type=%-20s console=%s status=%s",
            node_entry["name"],
            node_entry["node_type"],
            node_entry["console"],
            node_entry["status"],
        )
    # ---- Fetch links -------------------------------------------------------
    raw_links = _get(f"/projects/{project_id}/links")
    links = []
    for lnk in raw_links:
        nodes_in_link = lnk.get("nodes", [])
        if len(nodes_in_link) != 2:
            continue  # Skip incomplete links (e.g., unconnected tails)
        ep_a = nodes_in_link[0]
        ep_b = nodes_in_link[1]
        def _ep(ep: dict) -> dict:
            nid = ep["node_id"]
            adapter = ep.get("adapter_number", 0)
            port = ep.get("port_number", 0)
            port_info = node_port_map.get(nid, [])
            iface = _resolve_interface_name(port_info, adapter, port)
            name = next((nd["name"] for nd in nodes if nd["node_id"] == nid), nid)
            return {
                "node_id": nid,
                "node_name": name,
                "adapter_number": adapter,
                "port_number": port,
                "interface": iface,
            }
        link_entry = {
            "link_id": lnk["link_id"],
            "link_type": lnk.get("link_type", "ethernet"),
            "endpoint_a": _ep(ep_a),
            "endpoint_b": _ep(ep_b),
        }
        links.append(link_entry)
        logger.info(
            "  Link: %s:%s <--> %s:%s",
            link_entry["endpoint_a"]["node_name"],
            link_entry["endpoint_a"]["interface"],
            link_entry["endpoint_b"]["node_name"],
            link_entry["endpoint_b"]["interface"],
        )
    topology = {
        "project_id": project_id,
        "project_name": project_name,
        "nodes": nodes,
        "links": links,
    }
    logger.info(
        "Topology discovery complete: %d nodes, %d links.",
        len(nodes),
        len(links),
    )
    return topology
def save_topology(topology: dict, path: str = TOPOLOGY_FILE) -> None:
    """Write topology dict to a JSON file (pretty-printed)."""
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(topology, fh, indent=2)
    logger.info("Topology saved to %s", path)
def load_topology(path: str = TOPOLOGY_FILE) -> dict:
    """Load and return the topology from topology.json."""
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"'{path}' not found. Run topology discovery first."
        )
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)