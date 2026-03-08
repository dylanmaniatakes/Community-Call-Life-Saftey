"""
Device management routes — register, list, update, delete Innovonics devices.
Includes learn-mode endpoint for auto-detecting device IDs from the coordinator.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException

from database import db
from models import DeviceCreate, DeviceUpdate

router = APIRouter()


@router.get("/")
def list_devices(active_only: bool = False):
    with db() as conn:
        if active_only:
            rows = conn.execute(
                "SELECT * FROM devices WHERE active=1 ORDER BY name"
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM devices ORDER BY name").fetchall()
    return [dict(r) for r in rows]


@router.post("/")
def create_device(body: DeviceCreate):
    with db() as conn:
        try:
            cur = conn.execute(
                """INSERT INTO devices
                   (device_id, name, location, device_type, priority, vendor_type,
                    apartment_id, relay_config_id, relay_number, aux_label)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (body.device_id, body.name, body.location,
                 body.device_type, body.priority, body.vendor_type,
                 body.apartment_id, body.relay_config_id, body.relay_number,
                 body.aux_label),
            )
            row_id = cur.lastrowid
        except Exception as exc:
            if "UNIQUE" in str(exc):
                raise HTTPException(status_code=409,
                                    detail=f"Device ID '{body.device_id}' already exists")
            raise
    with db() as conn:
        row = conn.execute("SELECT * FROM devices WHERE id=?", (row_id,)).fetchone()
    return dict(row)


@router.get("/{device_id}")
def get_device(device_id: str):
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM devices WHERE device_id=?", (device_id,)
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Device not found")
    return dict(row)


@router.patch("/{device_id}")
def update_device(device_id: str, body: DeviceUpdate):
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    if not fields:
        raise HTTPException(status_code=400, detail="No fields to update")
    set_clause = ", ".join(f"{k}=?" for k in fields)
    with db() as conn:
        cur = conn.execute(
            f"UPDATE devices SET {set_clause} WHERE device_id=?",
            (*fields.values(), device_id),
        )
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="Device not found")
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM devices WHERE device_id=?", (device_id,)
        ).fetchone()
    return dict(row)


@router.delete("/{device_id}")
def delete_device(device_id: str):
    with db() as conn:
        cur = conn.execute("DELETE FROM devices WHERE device_id=?", (device_id,))
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="Device not found")
    return {"ok": True}


# ---------------------------------------------------------------------------
# Learn mode — auto-detect the next device that fires on the coordinator
# ---------------------------------------------------------------------------

@router.post("/learn/start")
async def learn_start(timeout: int = 30):
    """
    Arms the coordinator listener to capture the next activation and
    return its device ID.  The coordinator must be enabled and connected.
    A coordinator.device_seen WebSocket event is also broadcast so the
    UI can fill in the field without polling this endpoint.
    """
    from modules.innovonics import get_status, start_learn_mode, stop_learn_mode

    if get_status() != "connected":
        raise HTTPException(
            status_code=409,
            detail=f"Coordinator not connected (status: {get_status()}). "
                   "Enable and configure it in Settings → Coordinator first.",
        )

    loop = asyncio.get_event_loop()
    fut  = start_learn_mode(loop)
    try:
        device_id = await asyncio.wait_for(asyncio.shield(fut), timeout=timeout)
        return {"ok": True, "device_id": device_id}
    except asyncio.TimeoutError:
        stop_learn_mode()
        raise HTTPException(status_code=408, detail="No device activated within the timeout window.")
    except asyncio.CancelledError:
        # learn/stop was called — return a clean 200 rather than crashing
        return {"ok": False, "detail": "Learn mode cancelled."}


@router.post("/learn/stop")
def learn_stop():
    from modules.innovonics import stop_learn_mode
    stop_learn_mode()
    return {"ok": True}
