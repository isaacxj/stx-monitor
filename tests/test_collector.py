from monitor.collector import HostEdges, docker_event_to_event


def sample(t=0.0, ac=True, temp=50.0, pct=50.0, cap=80):
    return {"time": t, "cpu_temp": temp,
            "power_supply": {"ac_online": ac, "battery": {"capacity": cap}},
            "mounts": [{"mountpoint": "/", "percent": pct, "free": 1e9}]}


def test_power_edges():
    e = HostEdges()
    assert e.check(sample()) == []  # first sample only sets the baseline
    lost = e.check(sample(t=60, ac=False, cap=79))
    assert [x["title"] for x in lost] == ["Running on battery"]
    assert "79%" in lost[0]["detail"]
    assert e.check(sample(t=120, ac=False)) == []
    back = e.check(sample(t=300, ac=True))
    assert back[0]["title"] == "Wall power is back" and "4 min" in back[0]["detail"]


def test_heat_and_storage_edges_with_hysteresis():
    e = HostEdges()
    e.check(sample())
    hot = e.check(sample(temp=92, pct=91))
    assert {x["kind"] for x in hot} == {"heat", "storage"}
    assert e.check(sample(temp=88, pct=89)) == []  # still above the "clear" line
    cleared = e.check(sample(temp=80, pct=80))
    assert all(x["level"] == "ok" for x in cleared) and len(cleared) == 2


def test_docker_events_become_sentences():
    die = docker_event_to_event({"action": "die", "name": "radarr", "id": "abc", "exit_code": "137"})
    assert die["title"] == "radarr stopped" and die["level"] == "error" and "137" in die["detail"]
    assert docker_event_to_event({"action": "exec_start: sh", "name": "x"}) is None
    assert docker_event_to_event({"action": "health_status: unhealthy", "name": "x"})["level"] == "error"
