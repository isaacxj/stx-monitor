import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from monitor import alerts as al
from monitor.alerts import Alerts, classify, parse_destination
from monitor.collector import docker_event_to_event, event
from monitor.store import Store


def fake_collector(stopped_by_me=()):
    store = Store(":memory:")
    return SimpleNamespace(
        store=store,
        settings=SimpleNamespace(public_url=""),
        tailnet={"devices": [{"self": True, "dns_name": "stx-server.tail1234.ts.net"}]},
        host=SimpleNamespace(identity={"hostname": "stx-server"}),
        actions=SimpleNamespace(expected_stop=lambda cid: cid in stopped_by_me),
        broadcast=lambda kind, data: None,
    )


def recorder(status=200):
    sent = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append({"url": str(request.url), "auth": request.headers.get("authorization"),
                     "body": json.loads(request.content)})
        return httpx.Response(status, json={"id": "x"})

    return sent, httpx.MockTransport(handler)


def test_parse_destination():
    assert parse_destination("https://ntfy.sh/stx-abc") == ("https://ntfy.sh", "stx-abc")
    assert parse_destination("http://127.0.0.1:2586/stx/") == ("http://127.0.0.1:2586", "stx")
    assert parse_destination("https://example.com/ntfy/alerts") == ("https://example.com/ntfy", "alerts")
    for bad in ("", "ntfy.sh/stx", "https://ntfy.sh", "https://ntfy.sh/", "ftp://x/y"):
        assert parse_destination(bad) is None


def test_classify_picks_what_is_worth_a_buzz():
    crash = docker_event_to_event({"action": "die", "name": "radarr", "id": "abc", "exit_code": "1"})
    assert classify(crash)["category"] == "apps"
    asked_to_stop = docker_event_to_event({"action": "die", "name": "radarr", "id": "abc", "exit_code": "143"})
    assert classify(asked_to_stop) is None
    assert classify(event("app", "ok", "radarr started")) is None
    assert classify(event("system", "info", "stx-monitor started")) is None
    assert classify(event("power", "warn", "Running on battery"))["priority"] == 4
    assert classify(event("power", "warn", "Battery at 19%: shutting down cleanly"))["priority"] == 5
    assert classify(event("heat", "warn", "CPU is hot: 93 °C"))["category"] == "heat"
    assert classify(event("storage", "warn", "/srv/data is 91% full"))["key"] == "storage:/srv/data"
    assert classify(event("security", "error", "Sign-in paused for 15 minutes"))["priority"] == 5


async def test_sends_json_to_the_topic_with_a_link_back():
    sent, transport = recorder()
    a = Alerts(fake_collector(), "https://ntfy.sh/stx-secret", token="tk_1", transport=transport)
    assert a.notify(event("heat", "warn", "CPU is hot: 93 °C", "Check the fans."))
    await asyncio.gather(*a.tasks)
    assert len(sent) == 1
    msg = sent[0]
    assert msg["url"] == "https://ntfy.sh"
    assert msg["auth"] == "Bearer tk_1"
    assert msg["body"]["topic"] == "stx-secret"
    assert msg["body"]["title"] == "CPU is hot: 93 °C"  # unicode is fine in a JSON body
    assert msg["body"]["message"] == "Check the fans."
    assert msg["body"]["click"] == "https://stx-server.tail1234.ts.net"
    assert a.view()["last_sent"]["title"] == "CPU is hot: 93 °C"
    await a.close()


async def test_cooldown_and_hourly_limit(monkeypatch):
    sent, transport = recorder()
    a = Alerts(fake_collector(), "https://ntfy.sh/stx-secret", transport=transport)
    crash = docker_event_to_event({"action": "die", "name": "sonarr", "id": "c1", "exit_code": "1"})
    assert a.notify(crash)
    assert not a.notify(crash)  # a crash loop buzzes once per ALERT_COOLDOWN
    monkeypatch.setattr(al, "HOURLY_LIMIT", 3)
    assert a.notify(event("heat", "warn", "CPU is hot"))
    assert a.notify(event("storage", "warn", "/ is 95% full"))
    assert not a.notify(event("storage", "warn", "/srv/data is 95% full"))
    await asyncio.gather(*a.tasks)
    assert len(sent) == 3
    await a.close()


async def test_respects_toggles_and_my_own_stops():
    sent, transport = recorder()
    col = fake_collector(stopped_by_me={"c9"})
    a = Alerts(col, "https://ntfy.sh/stx-secret", transport=transport)
    a.set_categories({"security": False}, "isaac", "iPhone")
    assert not a.notify(event("security", "info", "isaac signed in from iPhone"))
    assert not a.notify(docker_event_to_event({"action": "die", "name": "x", "id": "c9", "exit_code": "137"}))
    assert col.store.list_actions(5)[0]["action"] == "alerts"
    with pytest.raises(ValueError):
        a.set_categories({"bogus": True}, "isaac", "iPhone")
    assert sent == []
    await a.close()


async def test_off_without_a_url_and_reports_failures():
    a = Alerts(fake_collector(), "")
    assert not a.configured and not a.notify(event("heat", "warn", "CPU is hot"))
    with pytest.raises(RuntimeError, match="STX_NTFY_URL"):
        await a.send_test()
    await a.close()

    sent, transport = recorder(status=403)
    b = Alerts(fake_collector(), "https://ntfy.sh/stx-secret", transport=transport)
    with pytest.raises(RuntimeError, match="403"):
        await b.send_test()
    assert b.view()["last_error"]["detail"] == "HTTP 403"
    assert "secret" not in json.dumps(b.view())  # the page never sees the whole topic
    await b.close()
