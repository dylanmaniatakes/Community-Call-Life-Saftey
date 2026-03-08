"""
Web Push subscription endpoints.

  GET  /api/push/vapid-public-key  — returns the VAPID public key for the frontend
  POST /api/push/subscribe         — register a push subscription
  POST /api/push/unsubscribe       — remove a push subscription
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from modules.push_manager import get_public_key

router = APIRouter()


class SubscriptionBody(BaseModel):
    endpoint:   str
    p256dh:     str
    auth:       str
    user_agent: str = ""


@router.get("/vapid-public-key")
async def vapid_public_key():
    """Return the VAPID application server public key."""
    return {"public_key": get_public_key()}


@router.post("/subscribe")
async def subscribe(body: SubscriptionBody):
    """Register (or refresh) a browser push subscription."""
    from database import db
    with db() as conn:
        conn.execute(
            """INSERT INTO push_subscriptions (endpoint, p256dh, auth, user_agent)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(endpoint) DO UPDATE
               SET p256dh=excluded.p256dh,
                   auth=excluded.auth,
                   user_agent=excluded.user_agent""",
            (body.endpoint, body.p256dh, body.auth, body.user_agent),
        )
    return {"ok": True}


@router.post("/unsubscribe")
async def unsubscribe(body: SubscriptionBody):
    """Remove a browser push subscription."""
    from database import db
    with db() as conn:
        conn.execute(
            "DELETE FROM push_subscriptions WHERE endpoint=?", (body.endpoint,)
        )
    return {"ok": True}
