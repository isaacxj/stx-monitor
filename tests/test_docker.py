import json
import struct

import httpx
import pytest

from monitor import docker_client as dc


def frame(stream: int, payload: bytes) -> bytes:
    return bytes([stream, 0, 0, 0]) + struct.pack(">I", len(payload)) + payload


def test_parse_docker_host():
    assert dc.parse_docker_host("tcp://127.0.0.1:2375") == ("http://127.0.0.1:2375", None)
    assert dc.parse_docker_host("unix:///var/run/docker.sock") == ("http://docker", "/var/run/docker.sock")
    with pytest.raises(ValueError):
        dc.parse_docker_host("ssh://box")


def test_demux_multiplexed_with_split_frames():
    data = (
        frame(1, b"2026-10-28T10:08:41.1Z Starting RSS sync\n2026-10-28T10:08:44.2Z Checked ")
        + frame(2, b"2026-10-28T10:08:45.0Z \x1b[31mwarn\x1b[0m disk slow\n")
        + frame(1, b"112 releases\n")
        + frame(1, b"no newline at end")
    )
    pairs = dc.demux(data)
    assert pairs == [
        ("stdout", "2026-10-28T10:08:41.1Z Starting RSS sync"),
        ("stderr", "2026-10-28T10:08:45.0Z \x1b[31mwarn\x1b[0m disk slow"),
        ("stdout", "2026-10-28T10:08:44.2Z Checked 112 releases"),
        ("stdout", "no newline at end"),
    ]
    lines = dc.parse_log_lines(pairs)
    assert lines[0] == {"ts": "2026-10-28T10:08:41.1Z", "stream": "stdout", "text": "Starting RSS sync"}
    assert lines[1]["text"] == "warn disk slow"  # ANSI colour codes stripped
    assert lines[3] == {"ts": None, "stream": "stdout", "text": "no newline at end"}


def test_demux_tty_is_raw():
    data = b"2026-10-28T10:00:00Z hello\r\n2026-10-28T10:00:01Z world\n"
    lines = dc.parse_log_lines(dc.demux(data, tty=True))
    assert [line["text"] for line in lines] == ["hello", "world"]
    assert all(line["stream"] == "stdout" for line in lines)


def test_demux_falls_back_when_not_multiplexed():
    assert dc.demux(b"plain text log line\nsecond\n") == [("stdout", "plain text log line"), ("stdout", "second")]


def stats(total, system, usage, cache_key, cache, rx=0, blk_read=0):
    return {
        "cpu_stats": {"cpu_usage": {"total_usage": total}, "system_cpu_usage": system, "online_cpus": 12},
        "memory_stats": {"usage": usage, "limit": 2_000_000_000, "stats": {cache_key: cache}},
        "networks": {"eth0": {"rx_bytes": rx, "tx_bytes": rx // 2}},
        "blkio_stats": {"io_service_bytes_recursive": [
            {"major": 8, "minor": 0, "op": "read", "value": blk_read},
            {"major": 8, "minor": 0, "op": "write", "value": 0},
        ]},
        "pids_stats": {"current": 14},
    }


def test_stats_cgroup_v2():
    t = dc.StatsTracker()
    first = t.update("abc", stats(1_000_000_000, 100_000_000_000, 700_000_000, "inactive_file", 100_000_000), now=0.0)
    assert first["cpu"] is None  # nothing to compare with yet
    assert first["mem"] == 600_000_000
    assert first["mem_percent"] == 30.0
    # Container used 0.6 s of CPU while the whole machine (12 threads) used 12 s.
    second = t.update(
        "abc",
        stats(1_600_000_000, 112_000_000_000, 700_000_000, "inactive_file", 100_000_000, rx=6000, blk_read=3000),
        now=3.0,
    )
    assert second["cpu"] == 5.0
    assert second["net_rx"] == 2000
    assert second["net_tx"] == 1000
    assert second["blk_read"] == 1000
    assert second["pids"] == 14


def test_stats_cgroup_v1_memory():
    t = dc.StatsTracker()
    s = t.update("v1", stats(0, 0, 500, "total_inactive_file", 200))
    assert s["mem"] == 300


def test_stats_without_system_usage_uses_wall_clock():
    t = dc.StatsTracker()
    raw = {"cpu_stats": {"cpu_usage": {"total_usage": 0}, "online_cpus": 4}, "memory_stats": {}}
    t.update("x", raw, now=0.0)
    raw2 = {"cpu_stats": {"cpu_usage": {"total_usage": 2_000_000_000}, "online_cpus": 4}, "memory_stats": {}}
    assert t.update("x", raw2, now=2.0)["cpu"] == 25.0  # 1 of 4 cores busy


def test_stats_tracker_forgets_removed_containers():
    t = dc.StatsTracker()
    t.update("a", {}, now=0.0)
    t.update("b", {}, now=0.0)
    t.forget({"a"})
    assert set(t.prev) == {"a"}


def test_summaries():
    c = dc.summarize_container({
        "Id": "0123456789abcdef" * 4,
        "Names": ["/radarr"],
        "Image": "lscr.io/linuxserver/radarr",
        "State": "running",
        "Status": "Up 2 hours (healthy)",
        "Ports": [{"PrivatePort": 7878, "PublicPort": 7878}, {"PrivatePort": 7878, "PublicPort": 7878}],
        "Labels": {"com.docker.compose.service": "radarr", "stx.app": "radarr", "maintainer": "x"},
    })
    assert c["id"] == "0123456789ab"
    assert c["name"] == "radarr"
    assert c["health"] == "healthy"
    assert c["ports"] == [7878]
    assert c["labels"] == {"com.docker.compose.service": "radarr", "stx.app": "radarr"}
    ev = dc.summarize_event({"Type": "container", "Action": "die", "time": 5,
                             "Actor": {"ID": "f" * 64, "Attributes": {"name": "radarr", "exitCode": "137"}}})
    assert ev == {"time": 5, "action": "die", "id": "f" * 12, "name": "radarr", "image": None, "exit_code": "137"}
    assert dc.summarize_event({"Type": "network"}) is None


def fake_docker(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/containers/json":
        assert request.url.params["all"] == "1"
        return httpx.Response(200, json=[{"Id": "a" * 64, "Names": ["/web"], "State": "running", "Status": "Up"}])
    if path.endswith("/json"):
        return httpx.Response(200, json={"State": {"ExitCode": 0}, "Config": {"Tty": False}, "RestartCount": 2})
    if path.endswith("/logs"):
        assert request.url.params["tail"] == "2000"
        return httpx.Response(200, content=frame(1, b"2026-10-28T10:00:00Z up\n"))
    if path.endswith("/stats"):
        assert request.url.params["one-shot"] == "true"
        return httpx.Response(200, json=stats(1, 1, 10, "inactive_file", 0))
    if path == "/events":
        body = "\n".join(json.dumps(e) for e in [
            {"Type": "container", "Action": "start", "Actor": {"ID": "a" * 64, "Attributes": {"name": "web"}}},
            {"Type": "image", "Action": "pull"},
        ]) + "\n"
        return httpx.Response(200, content=body.encode())
    if path == "/info":
        return httpx.Response(200, json={"ServerVersion": "29.0.0", "ContainersRunning": 1, "CgroupVersion": "2"})
    return httpx.Response(404)


async def test_client_against_fake_daemon():
    client = dc.DockerClient("tcp://127.0.0.1:2375", transport=httpx.MockTransport(fake_docker))
    try:
        containers = await client.containers()
        assert containers[0]["name"] == "web"
        assert (await client.inspect("aaaa"))["restart_count"] == 2
        logs = await client.logs("aaaa", tail=99999)  # clamped to 2000
        assert logs == [{"ts": "2026-10-28T10:00:00Z", "stream": "stdout", "text": "up"}]
        assert (await client.all_stats(["aaaa"]))["aaaa"]["mem"] == 10
        events = [e async for e in client.events()]
        assert [e["action"] for e in events] == ["start"]
        assert (await client.info())["version"] == "29.0.0"
    finally:
        await client.close()
