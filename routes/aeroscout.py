"""
AeroScout Location Engine REST API

  GET  /api/aeroscout/config                    — connection config (password masked)
  PUT  /api/aeroscout/config                    — save connection config
  GET  /api/aeroscout/status                    — live connection status
  POST /api/aeroscout/reload                    — restart TCP connection

  GET  /api/aeroscout/devices                   — all known ALE devices (DC1000/EX5700/EX5500)
  POST /api/aeroscout/devices/{id}/command      — send command to a door controller

  GET  /api/aeroscout/tags                      — all known wander tags
  POST /api/aeroscout/tags                      — register / pre-seed a tag
  PUT  /api/aeroscout/tags/{mac}                — update strap address / resident mapping
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from database import db

router = APIRouter()


# ── Config ────────────────────────────────────────────────────────────────────

class AeroscoutConfigIn(BaseModel):
    host:           Optional[str] = None
    port:           int = 1411
    username:       Optional[str] = "Admin"
    password:       Optional[str] = None   # None = don't update stored password
    client_version: Optional[str] = "5.7.30"
    enabled:        int = 0


@router.get("/config")
def get_config():
    with db() as conn:
        row = conn.execute("SELECT * FROM aeroscout_config WHERE id=1").fetchone()
    if not row:
        return {"id": 1, "host": None, "port": 1411, "username": "Admin",
                "password": None, "enabled": 0}
    cfg = dict(row)
    cfg["password"] = "••••••••" if cfg.get("password") else ""
    return cfg


@router.put("/config")
def save_config(body: AeroscoutConfigIn):
    with db() as conn:
        if body.password and body.password != "••••••••":
            conn.execute(
                """UPDATE aeroscout_config
                   SET host=?, port=?, username=?, password=?, client_version=?, enabled=?
                   WHERE id=1""",
                (body.host, body.port, body.username, body.password,
                 body.client_version, body.enabled),
            )
        else:
            conn.execute(
                """UPDATE aeroscout_config
                   SET host=?, port=?, username=?, client_version=?, enabled=?
                   WHERE id=1""",
                (body.host, body.port, body.username, body.client_version, body.enabled),
            )
        row = conn.execute("SELECT * FROM aeroscout_config WHERE id=1").fetchone()
    cfg = dict(row)
    cfg["password"] = "••••••••" if cfg.get("password") else ""
    return cfg


@router.get("/status")
def get_status():
    from modules.aeroscout import get_status, get_conn_info
    return {"status": get_status(), **get_conn_info()}


@router.post("/reload")
async def reload_connection():
    from modules.aeroscout import reload
    await reload()
    return {"ok": True}


# ── Devices ───────────────────────────────────────────────────────────────────

@router.get("/devices")
def list_devices():
    """Return all door controllers / exciters discovered from ALE."""
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM ale_devices ORDER BY model, name"
        ).fetchall()
    return [dict(r) for r in rows]


class DeviceCommandIn(BaseModel):
    command:  str                        # relay_on/off  night_mode_on/off  override_on/off  restart  raw
    duration: Optional[int] = 0         # seconds (0 = indefinite) for relay commands
    xml:      Optional[str] = None      # raw XML body when command="raw"


@router.post("/devices/{device_id}/command")
async def send_device_command(device_id: str, body: DeviceCommandIn):
    """
    Send a control command to a WanderGuard Blue door controller via ALE.

    Commands:
      relay_on / relay_off            — relay 1 (main door relay)
      night_mode_on / night_mode_off  — relay 2 (night-mode relay)
      override_on / override_off      — relay 3 (bypass / override relay)
      restart                         — reset the controller (OpCode 503)
      raw                             — send arbitrary XML (supply xml= field)

    Security-enabled devices return StatusCode 107 "Non secure client not
    allowed" until the security-key handshake is implemented.  The command
    is still sent and the server response appears in /aeroscoutraw and logs.
    """
    from modules.aeroscout import send_command
    result = await send_command(
        device_id, body.command,
        duration=body.duration or 0,
        xml=body.xml or "",
    )
    if not result["ok"]:
        raise HTTPException(status_code=400, detail=result.get("detail") or result.get("error") or "Command failed")
    return result


# ── Tags ──────────────────────────────────────────────────────────────────────

@router.get("/tags")
def list_tags():
    """Return all known wander tags with resident mapping and last location."""
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM ale_tags ORDER BY last_seen DESC"
        ).fetchall()
    return [dict(r) for r in rows]


class TagIn(BaseModel):
    mac:           str
    strap_address: Optional[str] = None
    resident_name: Optional[str] = None
    apartment_id:  Optional[int] = None


@router.post("/tags", status_code=201)
def create_tag(body: TagIn):
    """Pre-register a tag so the resident mapping is ready before the first LocationReport."""
    mac = body.mac.upper().replace(":", "")
    with db() as conn:
        conn.execute("""
            INSERT INTO ale_tags (mac, strap_address, resident_name, apartment_id)
            VALUES (?,?,?,?)
            ON CONFLICT(mac) DO UPDATE SET
                strap_address = COALESCE(excluded.strap_address, ale_tags.strap_address),
                resident_name = COALESCE(excluded.resident_name, ale_tags.resident_name),
                apartment_id  = COALESCE(excluded.apartment_id,  ale_tags.apartment_id)
        """, (mac, body.strap_address, body.resident_name, body.apartment_id))
        row = conn.execute("SELECT * FROM ale_tags WHERE mac=?", (mac,)).fetchone()
    return dict(row)


class TagUpdateIn(BaseModel):
    strap_address: Optional[str] = None
    resident_name: Optional[str] = None
    apartment_id:  Optional[int] = None


@router.put("/tags/{mac}")
def update_tag(mac: str, body: TagUpdateIn):
    """Update the resident / strap-address mapping for a tag."""
    mac = mac.upper().replace(":", "")
    with db() as conn:
        if not conn.execute("SELECT id FROM ale_tags WHERE mac=?", (mac,)).fetchone():
            raise HTTPException(status_code=404, detail="Tag not found — use POST /tags to create it first")
        if body.strap_address is not None:
            conn.execute("UPDATE ale_tags SET strap_address=? WHERE mac=?", (body.strap_address, mac))
        if body.resident_name is not None:
            conn.execute("UPDATE ale_tags SET resident_name=? WHERE mac=?", (body.resident_name, mac))
        if body.apartment_id is not None:
            conn.execute("UPDATE ale_tags SET apartment_id=? WHERE mac=?", (body.apartment_id, mac))
        row = conn.execute("SELECT * FROM ale_tags WHERE mac=?", (mac,)).fetchone()
    return dict(row)
