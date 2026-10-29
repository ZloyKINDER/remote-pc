"""Password proof used by the browser client.

The password is never sent over the wire.  The server keeps only a PBKDF2 key;
the client proves knowledge of the password with an HMAC over both nonces.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

ITERATIONS = 240_000
PROOF_PREFIX = b"RemotePC auth v1|"


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def unb64(value: str) -> bytes:
    if not isinstance(value, str):
        raise ValueError("expected base64 string")
    if len(value) > 256:
        raise ValueError("base64 value too long")
    return base64.b64decode(value.encode("ascii"), validate=True)


def new_salt() -> bytes:
    return secrets.token_bytes(16)


def new_nonce() -> bytes:
    return secrets.token_bytes(24)


def derive_key(password: str, salt: bytes, iterations: int = ITERATIONS) -> bytes:
    if not password:
        raise ValueError("empty password")
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations, dklen=32)


def proof(key: bytes, server_nonce: bytes, client_nonce: bytes) -> bytes:
    message = PROOF_PREFIX + server_nonce + b"|" + client_nonce
    return hmac.new(key, message, hashlib.sha256).digest()


def verify(key: bytes, server_nonce: bytes, client_nonce: bytes, received: bytes) -> bool:
    expected = proof(key, server_nonce, client_nonce)
    return hmac.compare_digest(expected, received)


def generate_password(length: int = 10) -> str:
    # Avoid characters that are easy to mix up when read from a console.
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"
    return "".join(secrets.choice(alphabet) for _ in range(length))
