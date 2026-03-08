"""
Call management routes.
Handles the lifecycle: active → acknowledged → cleared.
Also exposes a manual injection endpoint for testing.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from database import db
from models import CallAck, CallClear, CallInject
from modules.call_manager import acknowledge_call, clear_call, process_new_call

router = APIRouter()


# ---------------------------------------------------------------------------
# List calls
# ---------------------------------------------------------------------------

@router.get("/")
def list_calls(status: str = "active", limit: int = 100):
    """
    status: 'active' | 'acknowledged' | 'cleared' | 'all'
    """
    with db() as conn:
        if status == "all":
            rows = conn.execute(
                "SELECT * FROM calls ORDER BY timestamp DESC LIMIT ?", (limit,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM calls WHERE status=? ORDER BY timestamp DESC LIMIT ?",
                (status, limit),
            ).fetchall()
    return [dict(r) for r in rows]


@router.get("/active")
def list_active_calls():
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM calls WHERE status IN ('active','acknowledged') ORDER BY timestamp DESC"
        ).fetchall()
    return [dict(r) for r in rows]


@router.get("/{call_id}")
def get_call(call_id: int):
    with db() as conn:
        row = conn.execute("SELECT * FROM calls WHERE id=?", (call_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Call not found")
    return dict(row)


@router.get("/{call_id}/events")
def get_call_events(call_id: int):
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM call_events WHERE call_id=? ORDER BY timestamp", (call_id,)
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Lifecycle actions
# ---------------------------------------------------------------------------

@router.post("/{call_id}/acknowledge")
async def ack_call(call_id: int, body: CallAck):
    ok = await acknowledge_call(call_id, body.actor, body.notes or "")
    if not ok:
        raise HTTPException(status_code=409, detail="Call not active or already acknowledged")
    return {"ok": True}


@router.post("/{call_id}/clear")
async def clear(call_id: int, body: CallClear):
    ok = await clear_call(call_id, body.actor, body.notes or "")
    if not ok:
        raise HTTPException(status_code=409, detail="Call already cleared")
    return {"ok": True}


# ---------------------------------------------------------------------------
# Manual / test injection
# ---------------------------------------------------------------------------

@router.post("/inject")
async def inject_call(body: CallInject):
    """
    Manually trigger a call for a device — useful for testing notification
    rules and relay/pager wiring without needing the physical coordinator.
    """
    call_id = await process_new_call(body.device_id, body.raw_data or "manual-inject")
    return {"ok": True, "call_id": call_id}
