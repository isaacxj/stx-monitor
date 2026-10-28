from monitor.history import FIELDS, History


def test_minute_rollup():
    h = History()
    t0 = 1_800_000_000 - (1_800_000_000 % 60)
    h.add(t0, {"cpu": 10, "mem": 50, "temp": None})
    h.add(t0 + 3, {"cpu": 20, "mem": 50, "temp": 60})
    assert len(h.day) == 0  # minute not finished yet
    h.add(t0 + 61, {"cpu": 99})
    assert len(h.day) == 1
    assert h.day[0][0] == t0
    row = dict(zip(["t", *FIELDS], h.day[0]))
    assert row["cpu"] == 15 and row["mem"] == 50 and row["temp"] == 60 and row["watts"] is None
    assert len(h.live) == 3


def test_live_ring_is_bounded():
    h = History()
    for i in range(1300):
        h.add(1_800_000_000 + i * 3, {"cpu": i})
    assert len(h.live) == 1200
    assert len(h.day) <= 1440


def test_save_and_reload_drops_old(tmp_path):
    h = History()
    now = 1_800_000_000
    h.day.extend([[now - 25 * 3600, 1, 1, 1, 1, 1, 1], [now - 3600, 2, 2, 2, 2, 2, 2]])
    path = tmp_path / "data" / "history.json"
    h.save(path)
    fresh = History()
    assert fresh.load(path, now=now) == 1
    assert fresh.day[0][1] == 2


def test_load_missing_or_corrupt(tmp_path):
    assert History().load(tmp_path / "nope.json") == 0
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert History().load(bad) == 0
