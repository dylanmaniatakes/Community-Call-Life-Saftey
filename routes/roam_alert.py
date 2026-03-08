"""
Roam Alert REST API
  GET/POST/DELETE  /api/ra/networks      — network controllers (IP/port/name)
  GET/POST/DELETE  /api/ra/doors         — door controllers
  GET/POST/DELETE  /api/ra/tags          — wander tags / resident mapping
  GET/DELETE       /api/ra/events        — recent event log
  GET/POST/DELETE  /api/ra/codes         — keypad access codes
  POST             /api/ra/codes/{id}/send — push code to door controller
  POST             /api/ra/reload        — restart bus listeners after config change
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from database import db

router = APIRouter()


# ── Models ─────────────────────────────────────────────────────

class NetworkIn(BaseModel):
    name: str
    host: str
    port: int = 10001
    enabled: int = 1


class DoorIn(BaseModel):
    network_id: int
    name: str
    serial_number: str
    location: Optional[str] = None
    monitor_sanity: int = 1
    enabled: int = 1


class TagIn(BaseModel):
    tag_serial: str
    resident_name: Optional[str] = None
    apartment_id: Optional[int] = None
    enabled: int = 1


class CodeIn(BaseModel):
    door_id: int
    slot: int = 1
    code: str
    label: Optional[str] = None
    code_type: str = 'access'


# ── Networks ───────────────────────────────────────────────────

@router.get('/networks')
def list_networks():
    with db() as conn:
        rows = conn.execute("SELECT * FROM ra_networks ORDER BY name").fetchall()
    return [dict(r) for r in rows]


@router.post('/networks')
def create_network(body: NetworkIn):
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO ra_networks (name, host, port, enabled) VALUES (?,?,?,?)",
            (body.name, body.host, body.port, body.enabled),
        )
        return dict(conn.execute("SELECT * FROM ra_networks WHERE id=?",
                                 (cur.lastrowid,)).fetchone())


@router.patch('/networks/{nid}')
def update_network(nid: int, body: NetworkIn):
    with db() as conn:
        conn.execute(
            "UPDATE ra_networks SET name=?, host=?, port=?, enabled=? WHERE id=?",
            (body.name, body.host, body.port, body.enabled, nid),
        )
        row = conn.execute("SELECT * FROM ra_networks WHERE id=?", (nid,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Network not found")
    return dict(row)


@router.delete('/networks/{nid}')
def delete_network(nid: int):
    with db() as conn:
        cur = conn.execute("DELETE FROM ra_networks WHERE id=?", (nid,))
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="Network not found")
    return {'ok': True}


# ── Doors ──────────────────────────────────────────────────────

@router.get('/doors')
def list_doors():
    with db() as conn:
        rows = conn.execute(
            """SELECT d.*, n.name as network_name
               FROM ra_doors d
               LEFT JOIN ra_networks n ON n.id = d.network_id
               ORDER BY d.name"""
        ).fetchall()
    return [dict(r) for r in rows]


@router.post('/doors')
def create_door(body: DoorIn):
    with db() as conn:
        cur = conn.execute(
            """INSERT INTO ra_doors (network_id, name, serial_number, location,
               monitor_sanity, enabled) VALUES (?,?,?,?,?,?)""",
            (body.network_id, body.name, body.serial_number, body.location,
             body.monitor_sanity, body.enabled),
        )
        return dict(conn.execute("SELECT * FROM ra_doors WHERE id=?",
                                 (cur.lastrowid,)).fetchone())


@router.patch('/doors/{did}')
def update_door(did: int, body: DoorIn):
    with db() as conn:
        conn.execute(
            """UPDATE ra_doors SET network_id=?, name=?, serial_number=?, location=?,
               monitor_sanity=?, enabled=? WHERE id=?""",
            (body.network_id, body.name, body.serial_number, body.location,
             body.monitor_sanity, body.enabled, did),
        )
        row = conn.execute("SELECT * FROM ra_doors WHERE id=?", (did,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Door not found")
    return dict(row)


@router.delete('/doors/{did}')
def delete_door(did: int):
    with db() as conn:
        cur = conn.execute("DELETE FROM ra_doors WHERE id=?", (did,))
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="Door not found")
    return {'ok': True}


# ── Tags ───────────────────────────────────────────────────────

@router.get('/tags')
def list_tags():
    with db() as conn:
        rows = conn.execute(
            """SELECT t.*, a.name as apartment_name
               FROM ra_tags t
               LEFT JOIN apartments a ON a.id = t.apartment_id
               ORDER BY t.resident_name, t.tag_serial"""
        ).fetchall()
    return [dict(r) for r in rows]


@router.post('/tags')
def create_tag(body: TagIn):
    with db() as conn:
        try:
            cur = conn.execute(
                """INSERT INTO ra_tags (tag_serial, resident_name, apartment_id, enabled)
                   VALUES (?,?,?,?)""",
                (body.tag_serial.lower(), body.resident_name, body.apartment_id, body.enabled),
            )
        except Exception as exc:
            if 'UNIQUE' in str(exc):
                raise HTTPException(status_code=409, detail='Tag serial already exists')
            raise
        return dict(conn.execute("SELECT * FROM ra_tags WHERE id=?",
                                 (cur.lastrowid,)).fetchone())


@router.patch('/tags/{tid}')
def update_tag(tid: int, body: TagIn):
    with db() as conn:
        conn.execute(
            """UPDATE ra_tags SET tag_serial=?, resident_name=?, apartment_id=?,
               enabled=? WHERE id=?""",
            (body.tag_serial.lower(), body.resident_name, body.apartment_id,
             body.enabled, tid),
        )
        row = conn.execute("SELECT * FROM ra_tags WHERE id=?", (tid,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Tag not found")
    return dict(row)


@router.delete('/tags/{tid}')
def delete_tag(tid: int):
    with db() as conn:
        cur = conn.execute("DELETE FROM ra_tags WHERE id=?", (tid,))
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="Tag not found")
    return {'ok': True}


# ── Events ─────────────────────────────────────────────────────

@router.get('/events')
def list_events(limit: int = 100):
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM ra_events ORDER BY id DESC LIMIT ?", (min(limit, 500),)
        ).fetchall()
    return [dict(r) for r in rows]


@router.delete('/events')
def clear_events():
    with db() as conn:
        conn.execute("DELETE FROM ra_events")
    return {'ok': True}


# ── Keypad codes ───────────────────────────────────────────────

@router.get('/codes')
def list_codes():
    with db() as conn:
        rows = conn.execute(
            """SELECT c.*, d.name as door_name
               FROM ra_codes c
               LEFT JOIN ra_doors d ON d.id = c.door_id
               ORDER BY d.name, c.slot"""
        ).fetchall()
    return [dict(r) for r in rows]


@router.post('/codes')
def create_code(body: CodeIn):
    with db() as conn:
        cur = conn.execute(
            """INSERT INTO ra_codes (door_id, slot, code, label, code_type)
               VALUES (?,?,?,?,?)""",
            (body.door_id, body.slot, body.code, body.label, body.code_type),
        )
        return dict(conn.execute("SELECT * FROM ra_codes WHERE id=?",
                                 (cur.lastrowid,)).fetchone())


@router.delete('/codes/{cid}')
def delete_code(cid: int):
    with db() as conn:
        cur = conn.execute("DELETE FROM ra_codes WHERE id=?", (cid,))
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="Code not found")
    return {'ok': True}


@router.post('/codes/{cid}/send')
async def send_code(cid: int):
    """Push a saved keypad code to the door controller hardware."""
    with db() as conn:
        row = conn.execute("SELECT * FROM ra_codes WHERE id=?", (cid,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Code not found")
    row = dict(row)
    from modules.roam_alert import send_keypad_code
    ok = await send_keypad_code(row['door_id'], row['slot'], row['code'])
    if not ok:
        raise HTTPException(status_code=503, detail="Failed to send code to controller")
    return {'ok': True}


# ── Reload ─────────────────────────────────────────────────────

@router.post('/reload')
async def reload():
    """Restart bus listener tasks after config changes."""
    from modules.roam_alert import reload_networks
    await reload_networks()
    return {'ok': True}
