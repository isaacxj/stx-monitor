"""Sign-in for the one user of this console.

- Password: hashed with argon2, a deliberately slow hash that makes guessing
  expensive. It is set from the server with `python -m monitor set-password`,
  never from the web page.
- Passkeys (WebAuthn): the phone or laptop keeps a private key, unlocked with
  Face ID, Touch ID or Windows Hello, and signs a fresh random challenge. The
  server only ever stores the public half. Browsers allow passkeys only over
  HTTPS, which `tailscale serve` provides.
- Sessions: a random token in an HttpOnly cookie (JavaScript can't read it),
  Secure (HTTPS only) and SameSite=Strict (never sent by other sites). The
  server keeps only a SHA-256 of the token.
- Every state-changing request also carries a CSRF token that only our own
  page knows, so another site can't make your browser act on your behalf.
- After 5 wrong attempts in a row, sign-in pauses for 15 minutes.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from .store import Store

COOKIE = "stx_session"
REMEMBER_SECONDS = 30 * 86400
SHORT_SECONDS = 12 * 3600
MAX_FAILURES = 5
LOCKOUT_SECONDS = 15 * 60
CHALLENGE_SECONDS = 180
MIN_PASSWORD = 10


class AuthError(Exception):
    def __init__(self, message: str, status: int = 401):
        super().__init__(message)
        self.status = status


@dataclass
class Client:
    """Where a request came from, for the sign-in log."""
    ip: str
    device: str


def device_name(user_agent: str) -> str:
    ua = user_agent or ""
    if "iPhone" in ua:
        kind = "iPhone"
    elif "iPad" in ua:
        kind = "iPad"
    elif "Android" in ua:
        kind = "Android"
    elif "Macintosh" in ua or "Mac OS X" in ua:
        kind = "Mac"
    elif "Windows" in ua:
        kind = "Windows PC"
    elif "Linux" in ua:
        kind = "Linux"
    else:
        kind = "Unknown device"
    if "Edg/" in ua:
        browser = "Edge"
    elif "Firefox/" in ua or "FxiOS" in ua:
        browser = "Firefox"
    elif "Chrome/" in ua or "CriOS" in ua:
        browser = "Chrome"
    elif "Safari/" in ua:
        browser = "Safari"
    else:
        browser = ""
    return f"{kind}, {browser}" if browser else kind


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Auth:
    def __init__(self, store: Store, disabled: bool = False):
        self.store = store
        self.disabled = disabled
        self.hasher = PasswordHasher()
        self.challenges: dict[str, dict] = {}
        # Verifying against a throwaway hash keeps "no such user" as slow as "wrong password".
        self._dummy_hash = self.hasher.hash(secrets.token_urlsafe(16))

    # ------------------------------------------------------------------ user
    def username(self) -> str | None:
        return self.store.get("auth.username")

    def has_password(self) -> bool:
        return bool(self.store.get("auth.password_hash"))

    def set_password(self, username: str, password: str) -> None:
        username = username.strip()
        if not username or len(username) > 64:
            raise ValueError("Pick a username of 1 to 64 characters")
        if len(password) < MIN_PASSWORD:
            raise ValueError(f"Use at least {MIN_PASSWORD} characters")
        self.store.set("auth.username", username)
        self.store.set("auth.password_hash", self.hasher.hash(password))
        if not self.store.get("auth.user_handle"):
            self.store.set("auth.user_handle", bytes_to_base64url(secrets.token_bytes(32)))

    def _user_handle(self) -> bytes:
        handle = self.store.get("auth.user_handle")
        if not handle:
            handle = bytes_to_base64url(secrets.token_bytes(32))
            self.store.set("auth.user_handle", handle)
        return base64url_to_bytes(handle)

    # --------------------------------------------------------------- lockout
    def locked_for(self, now: float | None = None) -> int:
        """Seconds left in a lockout, or 0. Five failures in a row lock sign-in for 15 minutes."""
        now = now or time.time()
        recent = self.store.recent_sign_ins(MAX_FAILURES)
        if len(recent) < MAX_FAILURES or any(r["ok"] for r in recent):
            return 0
        left = LOCKOUT_SECONDS - (now - recent[0]["time"])
        return max(0, int(left + 0.999))

    def failures_left(self) -> int:
        streak = 0
        for r in self.store.recent_sign_ins(MAX_FAILURES):
            if r["ok"]:
                break
            streak += 1
        return max(0, MAX_FAILURES - streak)

    def record(self, ok: bool, method: str, client: Client, detail: str = "") -> None:
        self.store.add_sign_in(ok, method, client.ip, client.device, detail)

    def _check_lockout(self) -> None:
        left = self.locked_for()
        if left:
            raise AuthError(f"Too many wrong attempts. Try again in {max(1, round(left / 60))} min.", 429)

    # -------------------------------------------------------------- password
    def password_sign_in(self, username: str, password: str, client: Client) -> None:
        self._check_lockout()
        stored_user = self.username() or ""
        stored_hash = self.store.get("auth.password_hash")
        ok = False
        try:
            self.hasher.verify(stored_hash or self._dummy_hash, password)
            ok = bool(stored_hash)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            ok = False
        ok = ok and hmac.compare_digest(username.strip().lower(), stored_user.lower())
        if not ok:
            self.record(False, "password", client, "Wrong username or password")
            left = self.failures_left()
            if left == 0:
                raise AuthError("Too many wrong attempts. Sign-in is paused for 15 minutes.", 429)
            raise AuthError(f"Wrong username or password. {left} {'try' if left == 1 else 'tries'} left.")
        if self.hasher.check_needs_rehash(stored_hash):
            self.store.set("auth.password_hash", self.hasher.hash(password))
        self.record(True, "password", client)

    # -------------------------------------------------------------- sessions
    def create_session(self, remember: bool, method: str, client: Client) -> tuple[str, dict]:
        token = secrets.token_urlsafe(32)
        now = time.time()
        row = {
            "id": _hash_token(token),
            "csrf": secrets.token_urlsafe(24),
            "created": now,
            "expires": now + (REMEMBER_SECONDS if remember else SHORT_SECONDS),
            "last_seen": now,
            "remember": remember,
            "method": method,
            "ip": client.ip,
            "device": client.device,
        }
        self.store.add_session(row)
        return token, row

    def session(self, token: str | None) -> dict | None:
        if not token or len(token) > 128:
            return None
        row = self.store.get_session(_hash_token(token))
        if not row:
            return None
        now = time.time()
        if row["expires"] < now:
            self.store.delete_session(row["id"])
            return None
        if now - row["last_seen"] > 60:
            self.store.touch_session(row["id"], now)
        return row

    def end_session(self, token: str | None) -> None:
        if token:
            self.store.delete_session(_hash_token(token))

    @staticmethod
    def csrf_ok(session: dict, header: str | None) -> bool:
        return bool(header) and hmac.compare_digest(session["csrf"], header)

    # -------------------------------------------------------------- passkeys
    def _new_flow(self, kind: str, challenge: bytes, rp_id: str) -> str:
        now = time.time()
        for key in [k for k, v in self.challenges.items() if v["expires"] < now]:
            del self.challenges[key]
        flow = secrets.token_urlsafe(16)
        self.challenges[flow] = {"kind": kind, "challenge": challenge, "rp_id": rp_id, "expires": now + CHALLENGE_SECONDS}
        return flow

    def _take_flow(self, flow: str, kind: str) -> dict:
        entry = self.challenges.pop(flow or "", None)
        if not entry or entry["kind"] != kind or entry["expires"] < time.time():
            raise AuthError("That passkey request expired. Try again.", 400)
        return entry

    def _descriptors(self, rp_id: str) -> list[PublicKeyCredentialDescriptor]:
        return [PublicKeyCredentialDescriptor(id=base64url_to_bytes(p["id"]))
                for p in self.store.list_passkeys() if p["rp_id"] == rp_id]

    def begin_registration(self, rp_id: str) -> dict:
        username = self.username() or "owner"
        opts = generate_registration_options(
            rp_id=rp_id,
            rp_name="stx-monitor",
            user_name=username,
            user_id=self._user_handle(),
            user_display_name=username,
            authenticator_selection=AuthenticatorSelectionCriteria(
                resident_key=ResidentKeyRequirement.REQUIRED,
                user_verification=UserVerificationRequirement.REQUIRED,
            ),
            exclude_credentials=self._descriptors(rp_id),
        )
        return {"flow": self._new_flow("register", opts.challenge, rp_id), "options": json.loads(options_to_json(opts))}

    def finish_registration(self, flow: str, credential: dict, origin: str, name: str) -> dict:
        entry = self._take_flow(flow, "register")
        try:
            verified = verify_registration_response(
                credential=credential,
                expected_challenge=entry["challenge"],
                expected_rp_id=entry["rp_id"],
                expected_origin=origin,
                require_user_verification=True,
            )
        except Exception as exc:  # noqa: BLE001 - the library raises several types
            raise AuthError(f"The passkey could not be verified: {exc}", 400) from exc
        cred_id = bytes_to_base64url(verified.credential_id)
        label = (name or "Passkey").strip()[:60] or "Passkey"
        self.store.add_passkey(cred_id, verified.credential_public_key, verified.sign_count, entry["rp_id"], label)
        return {"id": cred_id, "name": label}

    def begin_authentication(self, rp_id: str) -> dict:
        self._check_lockout()
        opts = generate_authentication_options(
            rp_id=rp_id,
            allow_credentials=self._descriptors(rp_id),
            user_verification=UserVerificationRequirement.REQUIRED,
        )
        return {"flow": self._new_flow("authenticate", opts.challenge, rp_id), "options": json.loads(options_to_json(opts))}

    def finish_authentication(self, flow: str, credential: dict, origin: str, client: Client) -> str:
        self._check_lockout()
        entry = self._take_flow(flow, "authenticate")
        cred_id = str(credential.get("id") or "")
        passkey = self.store.get_passkey(cred_id)
        if not passkey or passkey["rp_id"] != entry["rp_id"]:
            self.record(False, "passkey", client, "Unknown passkey")
            raise AuthError("This passkey isn't registered here. Sign in with your password, then add it.")
        try:
            verified = verify_authentication_response(
                credential=credential,
                expected_challenge=entry["challenge"],
                expected_rp_id=entry["rp_id"],
                expected_origin=origin,
                credential_public_key=passkey["public_key"],
                credential_current_sign_count=passkey["sign_count"],
                require_user_verification=True,
            )
        except Exception as exc:  # noqa: BLE001
            self.record(False, "passkey", client, "Passkey check failed")
            raise AuthError("The passkey could not be verified. Try again.") from exc
        self.store.use_passkey(cred_id, verified.new_sign_count)
        self.record(True, "passkey", client, passkey["name"])
        return passkey["name"]
