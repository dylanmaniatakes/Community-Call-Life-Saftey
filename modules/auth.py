"""
Simple token-based authentication — no external dependencies.
Uses stdlib hashlib (PBKDF2-HMAC-SHA256) for password hashing.
Tokens are stored in-memory; they reset on server restart (acceptable for
a single-server nurse-call appliance).
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from typing import Optional

log = logging.getLogger("auth")

# token -> {user_id, username, role}
_sessions: dict[str, dict] = {}


# ---------------------------------------------------------------------------
# Password helpers
# ---------------------------------------------------------------------------

def _pbkdf2(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), 260_000
    ).hex()


def encode_password(password: str) -> str:
    """Hash a password and return 'salt_hex:hash_hex' for DB storage."""
    salt = secrets.token_hex(16)
    return f"{salt}:{_pbkdf2(password, salt)}"


def verify_password(password: str, stored: str) -> bool:
    """Verify a password against a stored 'salt_hex:hash_hex' string."""
    try:
        salt, expected = stored.split(":", 1)
        return secrets.compare_digest(_pbkdf2(password, salt), expected)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Session / token helpers
# ---------------------------------------------------------------------------

def create_token(user_id: int, username: str, role: str) -> str:
    token = secrets.token_urlsafe(32)
    _sessions[token] = {"user_id": user_id, "username": username, "role": role}
    log.info("Session created for user '%s'", username)
    return token


def get_session(token: str) -> Optional[dict]:
    return _sessions.get(token)


def revoke_token(token: str) -> None:
    session = _sessions.pop(token, None)
    if session:
        log.info("Session revoked for user '%s'", session.get("username"))
