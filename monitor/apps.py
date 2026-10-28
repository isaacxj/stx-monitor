"""The app map: what is planned (apps.yaml) joined with what is running (Docker)."""

from __future__ import annotations

import logging
from pathlib import Path

import yaml

log = logging.getLogger(__name__)

UNLISTED = {"id": "unlisted", "name": "Running, not on the plan"}


def app_status(container: dict | None) -> str:
    if container is None:
        return "planned"
    state = container.get("state")
    health = container.get("health")
    if state == "running":
        if health == "unhealthy":
            return "unhealthy"
        if health == "starting":
            return "starting"
        return "running"
    if state == "restarting":
        return "restarting"
    if state == "paused":
        return "paused"
    return "down"  # exited, dead, created


class AppsFile:
    """apps.yaml, reloaded automatically when the file changes."""

    def __init__(self, path: Path):
        self.path = path
        self._mtime: float | None = None
        self.groups: list[dict] = []
        self.apps: list[dict] = []
        self.error: str | None = None
        self.reload_if_changed()

    def reload_if_changed(self) -> None:
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            if self._mtime is not None or not self.error:
                self.error = f"{self.path} not found"
                self.groups, self.apps = [], []
            return
        if mtime == self._mtime:
            return
        self._mtime = mtime
        try:
            self.groups, self.apps = parse_apps(yaml.safe_load(self.path.read_text()) or {})
            self.error = None
        except (yaml.YAMLError, ValueError, TypeError) as exc:
            log.error("could not read %s: %s", self.path, exc)
            self.error = str(exc)


def parse_apps(data: dict) -> tuple[list[dict], list[dict]]:
    groups = []
    seen_groups = set()
    for g in data.get("groups") or []:
        gid = str(g["id"])
        groups.append({"id": gid, "name": g.get("name") or gid})
        seen_groups.add(gid)
    apps = []
    for a in data.get("apps") or []:
        aid = str(a["id"])
        group = str(a.get("group") or "other")
        if group not in seen_groups:
            groups.append({"id": group, "name": group.title()})
            seen_groups.add(group)
        apps.append({
            "id": aid,
            "name": a.get("name") or aid,
            "group": group,
            "description": a.get("description") or "",
            "port": a.get("port"),
            "url": a.get("url"),
            "container": a.get("container") or aid,
            "links": [str(x) for x in a.get("links") or []],
        })
    return groups, apps


def match_container(app: dict, containers: list[dict]) -> dict | None:
    for c in containers:
        if (c.get("labels") or {}).get("stx.app") == app["id"]:
            return c
    for c in containers:
        if c.get("name") == app["container"]:
            return c
    for c in containers:
        if c.get("compose_service") in (app["container"], app["id"]):
            return c
    return None


def _container_view(c: dict, stats: dict | None) -> dict:
    s = stats or {}
    return {
        "id": c["id"],
        "name": c["name"],
        "image": c.get("image"),
        "state": c.get("state"),
        "status": c.get("status"),
        "health": c.get("health"),
        "ports": c.get("ports") or [],
        "cpu": s.get("cpu"),
        "mem": s.get("mem"),
        "mem_limit": s.get("mem_limit"),
    }


def build_map(groups: list[dict], apps: list[dict], containers: list[dict], stats: dict[str, dict]) -> dict:
    """Join the plan with reality. Containers that match no app get their own group."""
    used: set[str] = set()
    out_groups = [{**g, "apps": []} for g in groups]
    by_group = {g["id"]: g for g in out_groups}
    links = []
    for app in apps:
        c = match_container(app, [x for x in containers if x["id"] not in used])
        if c:
            used.add(c["id"])
        entry = {
            **{k: app[k] for k in ("id", "name", "group", "description", "port", "url", "links")},
            "status": app_status(c),
            "container": _container_view(c, stats.get(c["id"])) if c else None,
        }
        if entry["port"] is None and c and c.get("ports"):
            entry["port"] = c["ports"][0]
        by_group[app["group"]]["apps"].append(entry)
        links.extend([app["id"], target] for target in app["links"])

    extra = []
    for c in containers:
        if c["id"] in used:
            continue
        labels = c.get("labels") or {}
        group = labels.get("stx.group")
        entry = {
            "id": f"container:{c['name']}",
            "name": labels.get("stx.name") or c["name"],
            "group": group if group in by_group else UNLISTED["id"],
            "description": labels.get("stx.description") or c.get("image") or "",
            "port": c["ports"][0] if c.get("ports") else None,
            "url": labels.get("stx.url"),
            "links": [],
            "status": app_status(c),
            "container": _container_view(c, stats.get(c["id"])),
            "unlisted": True,
        }
        if entry["group"] in by_group:
            by_group[entry["group"]]["apps"].append(entry)
        else:
            extra.append(entry)
    if extra:
        out_groups.append({**UNLISTED, "apps": extra})

    all_apps = [a for g in out_groups for a in g["apps"]]
    counts = {
        "total": len(all_apps),
        "running": sum(1 for a in all_apps if a["status"] == "running"),
        "down": sum(1 for a in all_apps if a["status"] in ("down", "unhealthy", "restarting")),
        "planned": sum(1 for a in all_apps if a["status"] == "planned"),
    }
    counts["deployed"] = counts["total"] - counts["planned"]
    return {"groups": out_groups, "links": links, "counts": counts}
