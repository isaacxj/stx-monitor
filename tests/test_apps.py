from pathlib import Path

from monitor.apps import AppsFile, app_status, build_map, parse_apps

PLAN = {
    "groups": [{"id": "media", "name": "Media"}, {"id": "monitoring", "name": "Monitoring"}],
    "apps": [
        {"id": "jellyfin", "group": "media", "port": 8096, "links": ["radarr"]},
        {"id": "radarr", "group": "media", "container": "radarr"},
        {"id": "sonarr", "group": "media"},
        {"id": "prowlarr", "group": "media"},
        {"id": "stx-monitor", "group": "monitoring", "container": "stx-monitor"},
    ],
}


def c(cid, name, state="running", labels=None, service=None, health=None, ports=None):
    return {"id": cid, "full_id": cid * 4, "name": name, "state": state, "health": health,
            "labels": labels or {}, "compose_service": service, "ports": ports or [], "image": f"img/{name}"}


def test_matching_order_and_planned():
    groups, apps = parse_apps(PLAN)
    containers = [
        c("aaa", "media-jf-1", labels={"stx.app": "jellyfin"}),  # by label
        c("bbb", "radarr", state="exited"),  # by container name
        c("ccc", "media-sonarr-1", service="sonarr", health="unhealthy"),  # by compose service
        c("ddd", "stx-monitor"),
        c("eee", "portainer", labels={"stx.url": "https://p.example", "stx.name": "Portainer"}, ports=[9443]),
        c("fff", "grafana", labels={"stx.group": "monitoring"}),
    ]
    m = build_map(groups, apps, containers, {"aaa": {"cpu": 4.1, "mem": 612_000_000}})
    apps_by_id = {a["id"]: a for g in m["groups"] for a in g["apps"]}
    assert apps_by_id["jellyfin"]["status"] == "running"
    assert apps_by_id["jellyfin"]["container"]["cpu"] == 4.1
    assert apps_by_id["radarr"]["status"] == "down"
    assert apps_by_id["sonarr"]["status"] == "unhealthy"
    assert apps_by_id["prowlarr"]["status"] == "planned"
    assert apps_by_id["prowlarr"]["container"] is None
    # Containers nobody planned for
    unlisted = m["groups"][-1]
    assert unlisted["id"] == "unlisted"
    assert [a["name"] for a in unlisted["apps"]] == ["Portainer"]
    assert unlisted["apps"][0]["url"] == "https://p.example"
    assert unlisted["apps"][0]["port"] == 9443
    assert "container:grafana" in [a["id"] for a in m["groups"][1]["apps"]]  # stx.group honoured
    assert m["links"] == [["jellyfin", "radarr"]]
    assert m["counts"] == {"total": 7, "running": 4, "down": 2, "planned": 1, "deployed": 6}


def test_a_container_matches_only_one_app():
    groups, apps = parse_apps({"apps": [{"id": "a", "container": "x"}, {"id": "b", "container": "x"}]})
    m = build_map(groups, apps, [c("111", "x")], {})
    statuses = [a["status"] for g in m["groups"] for a in g["apps"]]
    assert statuses == ["running", "planned"]


def test_status_mapping():
    assert app_status(None) == "planned"
    assert app_status({"state": "running", "health": "starting"}) == "starting"
    assert app_status({"state": "restarting"}) == "restarting"
    assert app_status({"state": "created"}) == "down"


def test_shipped_apps_yaml_loads():
    f = AppsFile(Path(__file__).resolve().parent.parent / "config" / "apps.yaml")
    assert f.error is None
    ids = {a["id"] for a in f.apps}
    assert {"stx-monitor", "jellyfin", "gluetun", "opengym", "ntfy", "caddy"} <= ids
    assert [g["name"] for g in f.groups] == ["Infrastructure", "Media", "Personal apps", "Monitoring"]
    for a in f.apps:  # every link points at a real app
        assert set(a["links"]) <= ids


def test_apps_file_reload_and_errors(tmp_path):
    p = tmp_path / "apps.yaml"
    f = AppsFile(p)
    assert f.error and f.apps == []
    p.write_text("apps:\n  - id: one\n")
    f.reload_if_changed()
    assert [a["id"] for a in f.apps] == ["one"] and f.error is None
