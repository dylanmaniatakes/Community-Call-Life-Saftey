"""
Area management routes — group apartments/devices by floor, wing, or building.
Each area can have an optional dome-light relay that fires on any call from
a device in any apartment belonging to the area.
"""

from __future__ import annotations
from typing import Optional
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from database import db

router = APIRouter()


class AreaCreate(BaseModel):
    name: str
    relay_config_id: Optional[int] = None


class AreaUpdate(AreaCreate):
    pass


@router.get("/")
def list_areas():
    with db() as conn:
        rows = conn.execute("SELECT * FROM areas ORDER BY name").fetchall()
    return [dict(r) for r in rows]


@router.post("/")
def create_area(body: AreaCreate):
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO areas (name, relay_config_id) VALUES (?,?)",
            (body.name, body.relay_config_id),
        )
        row_id = cur.lastrowid
    with db() as conn:
        row = conn.execute("SELECT * FROM areas WHERE id=?", (row_id,)).fetchone()
    return dict(row)


@router.put("/{area_id}")
def update_area(area_id: int, body: AreaUpdate):
    with db() as conn:
        cur = conn.execute(
            "UPDATE areas SET name=?, relay_config_id=? WHERE id=?",
            (body.name, body.relay_config_id, area_id),
        )
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="Area not found")
    with db() as conn:
        row = conn.execute("SELECT * FROM areas WHERE id=?", (area_id,)).fetchone()
    return dict(row)


@router.delete("/{area_id}")
def delete_area(area_id: int):
    with db() as conn:
        conn.execute("UPDATE apartments SET area_id=NULL WHERE area_id=?", (area_id,))
        conn.execute("UPDATE devices SET area_id=NULL WHERE area_id=?", (area_id,))
        cur = conn.execute("DELETE FROM areas WHERE id=?", (area_id,))
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="Area not found")
    return {"ok": True}
