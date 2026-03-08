"""
Apartment management routes — group devices into units/rooms.
Each apartment can have an optional dome-light relay and belongs to an Area.
"""

from __future__ import annotations
from typing import Optional
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from database import db

router = APIRouter()


class ApartmentCreate(BaseModel):
    name: str
    relay_config_id: Optional[int] = None
    area_id: Optional[int] = None


class ApartmentUpdate(ApartmentCreate):
    pass


@router.get("/")
def list_apartments():
    with db() as conn:
        apts = conn.execute("SELECT * FROM apartments ORDER BY name").fetchall()
    return [dict(a) for a in apts]


@router.post("/")
def create_apartment(body: ApartmentCreate):
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO apartments (name, relay_config_id, area_id) VALUES (?,?,?)",
            (body.name, body.relay_config_id, body.area_id),
        )
        row_id = cur.lastrowid
    with db() as conn:
        row = conn.execute("SELECT * FROM apartments WHERE id=?", (row_id,)).fetchone()
    return dict(row)


@router.put("/{apt_id}")
def update_apartment(apt_id: int, body: ApartmentUpdate):
    with db() as conn:
        cur = conn.execute(
            "UPDATE apartments SET name=?, relay_config_id=?, area_id=? WHERE id=?",
            (body.name, body.relay_config_id, body.area_id, apt_id),
        )
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="Apartment not found")
    with db() as conn:
        row = conn.execute("SELECT * FROM apartments WHERE id=?", (apt_id,)).fetchone()
    return dict(row)


@router.delete("/{apt_id}")
def delete_apartment(apt_id: int):
    with db() as conn:
        conn.execute("UPDATE devices SET apartment_id=NULL WHERE apartment_id=?", (apt_id,))
        cur = conn.execute("DELETE FROM apartments WHERE id=?", (apt_id,))
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="Apartment not found")
    return {"ok": True}


@router.get("/{apt_id}/devices")
def apartment_devices(apt_id: int):
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM devices WHERE apartment_id=? ORDER BY name", (apt_id,)
        ).fetchall()
    return [dict(r) for r in rows]
