from fastapi.testclient import TestClient

from monitor.main import app


def test_health_overview_and_guards():
    with TestClient(app) as client:
        assert client.get("/api/health").json()["ok"] is True
        ov = client.get("/api/overview").json()
        assert set(ov) >= {"identity", "host", "containers", "apps", "history", "events", "tailnet", "docker", "build"}
        assert ov["apps"]["counts"]["planned"] == ov["apps"]["counts"]["total"]  # docker is off in tests
        assert ov["events"][0]["title"] == "stx-monitor started"
        assert client.get("/api/containers/abc").status_code == 503
        # Nothing on the API changes state
        assert client.post("/api/containers/abc").status_code in (404, 405)
