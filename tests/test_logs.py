import json
from pathlib import Path

from monitor import journal
from monitor.docker_client import StreamDemux
from monitor.logs import LogHub, guess_level, matches, parse_ts

ENTRIES = [
    {"__REALTIME_TIMESTAMP": "1793200000000000", "PRIORITY": "6", "SYSLOG_IDENTIFIER": "systemd", "MESSAGE": "Started Daily apt upgrade.", "_BOOT_ID": "b1"},
    {"__REALTIME_TIMESTAMP": "1793200001000000", "PRIORITY": "3", "_TRANSPORT": "kernel", "MESSAGE": "Memory cgroup out of memory: Killed process 48211 (Radarr)", "_BOOT_ID": "b1"},
    {"__REALTIME_TIMESTAMP": "1793200002000000", "PRIORITY": "4", "SYSLOG_IDENTIFIER": "dockerd", "_SYSTEMD_UNIT": "docker.service", "MESSAGE": [104, 105, 255], "_BOOT_ID": "b1"},
]


def test_parse_journal_json():
    lines = journal.parse_lines("\n".join(json.dumps(e) for e in ENTRIES) + "\nnot json\n")
    assert [x["source"] for x in lines] == ["systemd", "kernel", "dockerd"]
    assert [x["level"] for x in lines] == ["info", "error", "warn"]
    assert lines[0]["ts"] == 1793200000.0
    assert lines[2]["text"] == "hi�"  # byte-array messages are decoded


def test_clean_and_unclean_shutdown():
    clean = journal.parse_lines(json.dumps({"MESSAGE": "Reached target System Power Off.", "PRIORITY": "6", "SYSLOG_IDENTIFIER": "systemd"}))
    assert journal.ended_cleanly(clean)
    stopped = journal.parse_lines(json.dumps({"MESSAGE": "Journal stopped", "SYSLOG_IDENTIFIER": "systemd-journald"}))
    assert journal.ended_cleanly(stopped)
    assert not journal.ended_cleanly(journal.parse_lines("\n".join(json.dumps(e) for e in ENTRIES)))


def test_journal_command(tmp_path):
    assert journal.journal_command(tmp_path) is None
    (tmp_path / "var/log/journal/abc").mkdir(parents=True)
    (tmp_path / "usr/bin").mkdir(parents=True)
    (tmp_path / "usr/bin/journalctl").write_text("")
    cmd = journal.journal_command(tmp_path)
    assert cmd == ["chroot", str(tmp_path), "journalctl", "--directory=/var/log/journal"]


def test_stream_demux_handles_split_headers():
    frame = bytes([2, 0, 0, 0, 0, 0, 0, 12]) + b"oops\nsecond "
    frame2 = bytes([1, 0, 0, 0, 0, 0, 0, 5]) + b"out\nx"
    d = StreamDemux()
    data = frame + frame2
    got = []
    for i in range(0, len(data), 3):  # feed in awkward 3-byte pieces
        got += d.feed(data[i:i + 3])
    got += d.flush()
    assert got == [("stderr", "oops"), ("stdout", "out"), ("stderr", "second "), ("stdout", "x")]


def test_levels_and_filters():
    assert guess_level("[ERR] Database is locked") == "error"
    assert guess_level("2026 Warn  Library scan is using 1.9 GB") == "warn"
    assert guess_level("[INF] Request served") == "info"
    hub = LogHub(maxlen=3)
    hub.add({"ts": 1, "origin": "host", "source": "dockerd", "unit": "docker.service", "level": "warn", "text": "radarr exited"})
    hub.add({"ts": 2, "origin": "container", "source": "radarr", "level": "error", "text": "Out of memory"})
    hub.add({"ts": 0.5, "origin": "host", "source": "kernel", "level": "info", "text": "link up"})
    hub.add({"ts": 3, "origin": "container", "source": "sonarr", "level": "info", "text": "RSS sync"})
    assert hub.query()["total"] == 3  # ring buffer dropped the oldest
    assert [x["source"] for x in hub.query()["lines"]] == ["kernel", "radarr", "sonarr"]  # sorted by time
    assert [x["source"] for x in hub.query(source="radarr")["lines"]] == ["radarr"]
    assert [x["source"] for x in hub.query(source="server")["lines"]] == ["kernel"]
    assert hub.query(level="warn")["matched"] == 1
    assert hub.query(query="RSS")["lines"][0]["source"] == "sonarr"
    assert not matches({"origin": "host", "source": "kernel", "level": "info", "text": "x"}, source="docker")


async def test_subscribers_get_matching_lines():
    hub = LogHub()
    q = hub.subscribe("radarr", "all", "")
    hub.add({"ts": 1, "origin": "container", "source": "sonarr", "level": "info", "text": "a"})
    hub.add({"ts": 2, "origin": "container", "source": "radarr", "level": "info", "text": "b"})
    assert q.qsize() == 1 and q.get_nowait()["text"] == "b"
    hub.unsubscribe(q)
    assert not hub.subscribers


def test_parse_docker_timestamp():
    assert parse_ts("2026-10-28T10:12:08.123456789Z") == 1793182328.123456
    assert parse_ts("2026-10-28T10:12:08Z") == 1793182328.0
