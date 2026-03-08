"""
Web Push notification manager.

Handles VAPID key generation/storage and sending push messages to all
registered browser subscriptions via the Web Push Protocol (RFC 8030).

VAPID keys are auto-generated on first use and stored in system_config.
Stale subscriptions (HTTP 404/410 from push service) are pruned automatically.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from typing import Optional

log = logging.getLogger("push_manager")


# ---------------------------------------------------------------------------
# VAPID key management
# ---------------------------------------------------------------------------

def _ensure_vapid_keys() -> tuple[str, str]:
    """Return (private_pem, public_b64), generating and persisting if needed."""
    from database import db

    with db() as conn:
        row = conn.execute(
            "SELECT vapid_private_key, vapid_public_key FROM system_config WHERE id=1"
        ).fetchone()
    if row and row["vapid_private_key"] and row["vapid_public_key"]:
        return row["vapid_private_key"], row["vapid_public_key"]

    # Generate a fresh P-256 VAPID key pair
    from cryptography.hazmat.primitives.asymmetric.ec import generate_private_key, SECP256R1
    from cryptography.hazmat.backends import default_backend
    from cryptography.hazmat.primitives.serialization import (
        Encoding, NoEncryption, PrivateFormat, PublicFormat,
    )

    key = generate_private_key(SECP256R1(), default_backend())
    private_pem = key.private_bytes(
        Encoding.PEM, PrivateFormat.TraditionalOpenSSL, NoEncryption()
    ).decode()
    pub_bytes = key.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    public_b64 = base64.urlsafe_b64encode(pub_bytes).rstrip(b"=").decode()

    with db() as conn:
        conn.execute(
            "UPDATE system_config SET vapid_private_key=?, vapid_public_key=? WHERE id=1",
            (private_pem, public_b64),
        )

    log.info("VAPID key pair generated and stored.")
    return private_pem, public_b64


def get_public_key() -> str:
    """Return the VAPID public key as URL-safe base64 (no padding)."""
    _, pub = _ensure_vapid_keys()
    return pub


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------

async def send_push(endpoint: str, p256dh: str, auth: str, payload: dict) -> bool:
    """
    Send a Web Push message to a single subscription.
    Returns False if the subscription is stale (should be deleted).
    """
    from pywebpush import webpush, WebPushException

    private_pem, _ = _ensure_vapid_keys()
    sub_info = {
        "endpoint": endpoint,
        "keys": {"p256dh": p256dh, "auth": auth},
    }
    try:
        await asyncio.to_thread(
            webpush,
            subscription_info=sub_info,
            data=json.dumps(payload),
            vapid_private_key=private_pem,
            vapid_claims={"sub": "mailto:admin@community-call.local"},
            content_encoding="aes128gcm",
        )
        return True
    except WebPushException as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status in (404, 410):
            # Push service says subscription is gone — caller will prune it
            return False
        log.warning("Push delivery failed (HTTP %s): %s", status, exc)
        return True  # transient error — keep subscription
    except Exception as exc:
        log.warning("Push error: %s", exc)
        return True


async def broadcast_push(payload: dict) -> None:
    """
    Send payload to every registered push subscription.
    Stale subscriptions are removed automatically.
    """
    from database import db

    with db() as conn:
        subs = [dict(r) for r in conn.execute("SELECT * FROM push_subscriptions").fetchall()]

    if not subs:
        return

    results = await asyncio.gather(
        *[send_push(s["endpoint"], s["p256dh"], s["auth"], payload) for s in subs],
        return_exceptions=True,
    )

    stale = [s["id"] for s, ok in zip(subs, results) if ok is False]
    if stale:
        placeholders = ",".join("?" * len(stale))
        with db() as conn:
            conn.execute(
                f"DELETE FROM push_subscriptions WHERE id IN ({placeholders})", stale
            )
        log.info("Pruned %d stale push subscription(s).", len(stale))
