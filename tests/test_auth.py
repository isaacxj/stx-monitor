import time

import pytest
from fastapi.testclient import TestClient

from monitor.auth import Auth, AuthError, Client, device_name
from monitor.main import app
from monitor.store import Store
from softauthn import SoftAuthenticator

ORIGIN = "https://testserver"
CLIENT = Client(ip="100.64.0.3", device="iPhone, Safari")


def test_device_names():
    iphone = "Mozilla/5.0 (iPhone; CPU iPhone OS 19_0 like Mac OS X) AppleWebKit/605.1.15 Version/19.0 Mobile/15E148 Safari/604.1"
    assert device_name(iphone) == "iPhone, Safari"
    assert device_name("Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/140.0 Safari/537.36 Edg/140.0") == "Windows PC, Edge"
    assert device_name("") == "Unknown device"


def test_password_and_lockout(tmp_path):
    auth = Auth(Store(tmp_path / "m.db"))
    with pytest.raises(ValueError):
        auth.set_password("isaac", "short")
    auth.set_password("isaac", "correct horse battery")
    assert auth.store.get("auth.password_hash").startswith("$argon2id$")
    auth.password_sign_in("Isaac", "correct horse battery", CLIENT)  # usernames ignore case
    for left in (4, 3, 2, 1):
        with pytest.raises(AuthError, match=f"{left} tr"):
            auth.password_sign_in("isaac", "wrong", CLIENT)
    with pytest.raises(AuthError) as exc:
        auth.password_sign_in("isaac", "wrong", CLIENT)
    assert exc.value.status == 429
    assert 890 < auth.locked_for() <= 900
    with pytest.raises(AuthError, match="Try again in 15 min"):
        auth.password_sign_in("isaac", "correct horse battery", CLIENT)  # even the right password waits
    assert auth.locked_for(now=time.time() + 901) == 0
    auth.store.clear_failed_sign_ins()
    auth.password_sign_in("isaac", "correct horse battery", CLIENT)


def test_sessions(tmp_path):
    auth = Auth(Store(tmp_path / "m.db"))
    token, row = auth.create_session(False, "password", CLIENT)
    assert auth.session(token)["id"] == row["id"]
    assert auth.store.get_session(token) is None  # only a hash of the token is stored
    assert auth.csrf_ok(row, row["csrf"]) and not auth.csrf_ok(row, "nope") and not auth.csrf_ok(row, None)
    auth.store._exec("UPDATE sessions SET expires = 0")
    assert auth.session(token) is None
    long_token, long_row = auth.create_session(True, "passkey", CLIENT)
    assert long_row["expires"] - long_row["created"] == 30 * 86400


@pytest.fixture
def client():
    with TestClient(app, base_url=ORIGIN) as c:
        auth = app.state.auth
        auth.store.clear_failed_sign_ins()
        auth.store.delete_all_sessions()
        auth.store.delete_all_passkeys()
        auth.set_password("isaac", "correct horse battery")
        yield c


def login(client, password="correct horse battery", remember=False):
    return client.post("/api/auth/login", json={"username": "isaac", "password": password, "remember": remember},
                       headers={"Origin": ORIGIN})


def test_api_needs_a_session(client):
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/overview").status_code == 401
    assert client.get("/api/logs").status_code == 401
    assert client.get("/api/session").json()["authenticated"] is False


def test_login_flow_cookie_and_csrf(client):
    # No Origin header: refused before the password is even checked
    assert client.post("/api/auth/login", json={"username": "isaac", "password": "x"}).status_code == 403
    assert client.post("/api/auth/login", json={"username": "isaac", "password": "x"},
                       headers={"Origin": "https://evil.example"}).status_code == 403
    r = login(client, remember=True)
    assert r.status_code == 200
    cookie = r.headers["set-cookie"]
    for part in ("stx_session=", "HttpOnly", "Secure", "SameSite=strict", "Max-Age=2592000"):
        assert part.lower() in cookie.lower()
    csrf = r.json()["csrf"]
    assert client.get("/api/overview").status_code == 200
    s = client.get("/api/session").json()
    assert s["authenticated"] and s["user"] == "isaac" and s["csrf"] == csrf
    # A change without the CSRF token is refused, with it the request goes through
    body = {"action": "reboot", "confirm": "nope"}
    assert client.post("/api/actions", json=body, headers={"Origin": ORIGIN}).status_code == 403
    r = client.post("/api/actions", json=body, headers={"Origin": ORIGIN, "X-CSRF-Token": csrf})
    assert r.status_code == 400  # validated, and refused because the helper isn't installed here
    events = client.get("/api/overview").json()["events"]
    assert any(e["kind"] == "security" and "signed in" in e["title"] for e in events)
    assert client.post("/api/auth/logout", headers={"Origin": ORIGIN, "X-CSRF-Token": csrf}).status_code == 200
    assert client.get("/api/overview").status_code == 401


def test_lockout_over_http(client):
    for _ in range(4):
        assert login(client, "wrong password").status_code == 401
    r = login(client, "wrong password")
    assert r.status_code == 429 and r.json()["locked_for"] > 0
    assert login(client).status_code == 429
    events = client.app.state.collector.store.recent_events(10)
    assert any(e["title"] == "Sign-in paused for 15 minutes" for e in events)


def test_passkey_register_and_sign_in(client):
    csrf = login(client).json()["csrf"]
    h = {"Origin": ORIGIN, "X-CSRF-Token": csrf}
    soft = SoftAuthenticator()
    begin = client.post("/api/auth/passkeys/register/begin", headers=h).json()
    assert begin["options"]["rp"]["id"] == "testserver"
    assert begin["options"]["authenticatorSelection"]["userVerification"] == "required"
    added = client.post("/api/auth/passkeys/register/finish", headers=h,
                        json={"flow": begin["flow"], "credential": soft.create(begin["options"], ORIGIN), "name": "iPhone"})
    assert added.status_code == 200, added.text
    # Replaying the same registration fails: the challenge was used up
    assert client.post("/api/auth/passkeys/register/finish", headers=h,
                       json={"flow": begin["flow"], "credential": soft.create(begin["options"], ORIGIN)}).status_code == 400
    client.post("/api/auth/logout", headers=h)
    client.cookies.clear()

    begin = client.post("/api/auth/passkey/begin", headers={"Origin": ORIGIN}).json()
    assert begin["options"]["allowCredentials"][0]["id"] == added.json()["id"]
    r = client.post("/api/auth/passkey/finish", headers={"Origin": ORIGIN},
                    json={"flow": begin["flow"], "credential": soft.get(begin["options"], ORIGIN), "remember": False})
    assert r.status_code == 200, r.text
    assert "Max-Age" not in r.headers["set-cookie"]  # not remembered: ends with the browser session
    assert client.get("/api/overview").status_code == 200
    account = client.get("/api/auth/account").json()
    assert account["passkeys"][0]["name"] == "iPhone" and account["passkeys"][0]["last_used"]

    # A signature for another site's origin is rejected
    client.cookies.clear()
    begin = client.post("/api/auth/passkey/begin", headers={"Origin": ORIGIN}).json()
    forged = soft.get(begin["options"], "https://evil.example")
    r = client.post("/api/auth/passkey/finish", headers={"Origin": ORIGIN}, json={"flow": begin["flow"], "credential": forged})
    assert r.status_code == 401


def test_overview_and_guards_when_signed_in(client):
    login(client)
    ov = client.get("/api/overview").json()
    assert set(ov) >= {"identity", "host", "containers", "apps", "history", "events", "tailnet", "docker", "build", "reports"}
    assert ov["apps"]["counts"]["planned"] == ov["apps"]["counts"]["total"]  # docker is off in tests
    assert client.get("/api/containers/abc").status_code == 503
    assert client.get("/api/logs?source=../../etc").status_code == 400
    assert client.get("/api/logs").json()["lines"] is not None
    assert client.get("/api/control").json()["capabilities"] == {"control": False, "helper": False}
