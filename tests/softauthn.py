"""A software passkey, enough to drive WebAuthn registration and sign-in in tests."""

import base64
import hashlib
import json
import os
import struct

import cbor2
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64u(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class SoftAuthenticator:
    def __init__(self):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.cred_id = os.urandom(16)
        self.count = 0

    def _cose(self) -> bytes:
        nums = self.key.public_key().public_numbers()
        return cbor2.dumps({1: 2, 3: -7, -1: 1, -2: nums.x.to_bytes(32, "big"), -3: nums.y.to_bytes(32, "big")})

    def create(self, options: dict, origin: str) -> dict:
        rp_hash = hashlib.sha256(options["rp"]["id"].encode()).digest()
        auth_data = (rp_hash + bytes([0x45]) + struct.pack(">I", self.count) + b"\0" * 16
                     + struct.pack(">H", len(self.cred_id)) + self.cred_id + self._cose())
        client_data = json.dumps({"type": "webauthn.create", "challenge": options["challenge"],
                                  "origin": origin, "crossOrigin": False}).encode()
        att = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth_data})
        return {"id": b64u(self.cred_id), "rawId": b64u(self.cred_id), "type": "public-key",
                "response": {"clientDataJSON": b64u(client_data), "attestationObject": b64u(att)}}

    def get(self, options: dict, origin: str) -> dict:
        self.count += 1
        rp_hash = hashlib.sha256(options["rpId"].encode()).digest()
        auth_data = rp_hash + bytes([0x05]) + struct.pack(">I", self.count)
        client_data = json.dumps({"type": "webauthn.get", "challenge": options["challenge"],
                                  "origin": origin, "crossOrigin": False}).encode()
        sig = self.key.sign(auth_data + hashlib.sha256(client_data).digest(), ec.ECDSA(hashes.SHA256()))
        return {"id": b64u(self.cred_id), "rawId": b64u(self.cred_id), "type": "public-key",
                "response": {"clientDataJSON": b64u(client_data), "authenticatorData": b64u(auth_data),
                             "signature": b64u(sig), "userHandle": None}}
