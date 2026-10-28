"""Tailscale devices, read from tailscaled's LocalAPI.

Tailscale is a VPN that links your own devices into a private network (a
"tailnet"). Its background service, tailscaled, answers questions over a Unix
socket on the host. We only ever ask for status, a read-only call.
"""

from __future__ import annotations

import logging
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

SOCKET = "run/tailscale/tailscaled.sock"
ZERO_TIME = "0001-01-01T00:00:00Z"


def _device(node: dict, is_self: bool, running: bool) -> dict:
    dns = (node.get("DNSName") or "").rstrip(".")
    ips = node.get("TailscaleIPs") or []
    last_seen = node.get("LastSeen")
    return {
        "name": dns.split(".")[0] if dns else node.get("HostName"),
        "hostname": node.get("HostName"),
        "dns_name": dns or None,
        "os": node.get("OS"),
        "ip": next((ip for ip in ips if "." in ip), ips[0] if ips else None),
        "online": running if is_self else bool(node.get("Online")),
        "last_seen": None if not last_seen or last_seen == ZERO_TIME else last_seen,
        "self": is_self,
        "exit_node": bool(node.get("ExitNode")),
    }


def parse_status(data: dict) -> dict:
    running = data.get("BackendState") == "Running"
    devices = []
    if data.get("Self"):
        devices.append(_device(data["Self"], True, running))
    peers = [_device(p, False, running) for p in (data.get("Peer") or {}).values()]
    peers.sort(key=lambda d: (not d["online"], (d["name"] or "").lower()))
    devices.extend(peers)
    tailnet = (data.get("CurrentTailnet") or {}).get("Name") or data.get("MagicDNSSuffix")
    return {
        "state": data.get("BackendState"),
        "tailnet": tailnet,
        "magic_dns_suffix": data.get("MagicDNSSuffix"),
        "devices": devices,
        "online": sum(1 for d in devices if d["online"]),
    }


async def read_status(root: Path, transport: httpx.AsyncBaseTransport | None = None) -> dict | None:
    sock = root / SOCKET
    if transport is None:
        if not sock.exists():
            return None
        transport = httpx.AsyncHTTPTransport(uds=str(sock))
    # tailscaled checks the Host header, so it must be exactly this name.
    async with httpx.AsyncClient(transport=transport, base_url="http://local-tailscaled.sock", timeout=5.0) as client:
        r = await client.get("/localapi/v0/status")
        r.raise_for_status()
        return parse_status(r.json())
