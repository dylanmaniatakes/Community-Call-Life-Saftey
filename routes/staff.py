"""
Staff communication routes.
  POST /api/staff/message    — broadcast a chat message to all connected clients
  POST /api/staff/emergency  — broadcast a staff emergency alert to all clients
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from routes.auth import auth_required, current_user

router = APIRouter()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _require_user(request: Request) -> dict:
    """Return the current user dict; raise 401 if auth is on and no valid token."""
    user = current_user(request)
    if auth_required() and not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    return user or {"user_id": 0, "username": "anonymous", "role": "staff"}


class MessageBody(BaseModel):
    message: str


@router.post("/message")
async def send_message(body: MessageBody, request: Request):
    user = _require_user(request)
    from modules.ws_manager import manager as ws
    await ws.broadcast("staff.message", {
        "username": user["username"],
        "message":  body.message.strip(),
        "ts":       _now_iso(),
    })
    return {"ok": True}


@router.post("/emergency")
async def send_emergency(body: MessageBody, request: Request):
    user     = _require_user(request)
    username = user["username"]
    message  = body.message.strip()

    # Create a dashboard call so staff can acknowledge and clear it like any alarm.
    # call_key is unique per user so they can only have one active emergency at a time.
    from modules.call_manager import process_named_call
    import json as _json
    await process_named_call(
        call_key  = f"staff_emergency:{username}",
        call_name = f"Staff Emergency — {username}",
        location  = message,
        priority  = "emergency",
        raw_data  = _json.dumps({"type": "staff_emergency", "username": username, "message": message}),
    )

    # Also broadcast the banner/beep event to all connected clients
    from modules.ws_manager import manager as ws
    await ws.broadcast("staff.emergency", {
        "username": username,
        "message":  message,
        "ts":       _now_iso(),
    })
    return {"ok": True}
