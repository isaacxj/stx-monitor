import httpx

from monitor import tailscale

STATUS = {
    "BackendState": "Running",
    "MagicDNSSuffix": "tail1234.ts.net",
    "CurrentTailnet": {"Name": "isaacxj.github"},
    "Self": {"HostName": "stx-server", "DNSName": "stx-server.tail1234.ts.net.", "OS": "linux",
             "TailscaleIPs": ["100.64.0.1", "fd7a::1"], "Online": True, "LastSeen": "0001-01-01T00:00:00Z"},
    "Peer": {
        "k1": {"HostName": "iPhone", "DNSName": "iphone.tail1234.ts.net.", "OS": "iOS",
               "TailscaleIPs": ["100.64.0.3"], "Online": False, "LastSeen": "2026-10-28T08:40:00Z"},
        "k2": {"HostName": "laptop", "DNSName": "isaacs-laptop.tail1234.ts.net.", "OS": "windows",
               "TailscaleIPs": ["100.64.0.2"], "Online": True},
    },
}


async def test_status_via_localapi(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "local-tailscaled.sock"
        assert request.url.path == "/localapi/v0/status"
        return httpx.Response(200, json=STATUS)

    s = await tailscale.read_status(tmp_path, transport=httpx.MockTransport(handler))
    assert s["tailnet"] == "isaacxj.github"
    assert [d["name"] for d in s["devices"]] == ["stx-server", "isaacs-laptop", "iphone"]
    me = s["devices"][0]
    assert me["self"] and me["online"] and me["ip"] == "100.64.0.1" and me["last_seen"] is None
    assert s["devices"][2]["last_seen"] == "2026-10-28T08:40:00Z"
    assert s["online"] == 2


async def test_missing_socket(tmp_path):
    assert await tailscale.read_status(tmp_path) is None
