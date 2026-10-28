from fastapi.testclient import TestClient

from monitor.main import app


def test_health():
    with TestClient(app) as client:
        r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True
