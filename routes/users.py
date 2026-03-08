"""
Full user management — create, read, update, delete with password support.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from database import db
from modules.auth import encode_password

router = APIRouter()


class UserCreate(BaseModel):
    username: str
    password: str = ""
    role: str = "staff"
    active: int = 1


class UserUpdate(BaseModel):
    username: Optional[str] = None
    password: Optional[str] = None   # empty string = clear password; None = no change
    role: Optional[str] = None
    active: Optional[int] = None


@router.get("/")
def list_users():
    with db() as conn:
        rows = conn.execute(
            "SELECT id, username, role, active, created_at FROM users ORDER BY username"
        ).fetchall()
    return [dict(r) for r in rows]


@router.post("/")
def create_user(body: UserCreate):
    hashed = encode_password(body.password) if body.password else ""
    with db() as conn:
        try:
            cur = conn.execute(
                "INSERT INTO users (username, hashed_password, role, active) VALUES (?,?,?,?)",
                (body.username, hashed, body.role, body.active),
            )
            row_id = cur.lastrowid
        except Exception as exc:
            if "UNIQUE" in str(exc):
                raise HTTPException(status_code=409, detail="Username already exists")
            raise
    with db() as conn:
        row = conn.execute(
            "SELECT id, username, role, active, created_at FROM users WHERE id=?", (row_id,)
        ).fetchone()
    return dict(row)


@router.patch("/{user_id}")
def update_user(user_id: int, body: UserUpdate):
    with db() as conn:
        existing = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if not existing:
        raise HTTPException(status_code=404, detail="User not found")

    updates: dict = {}
    if body.username is not None:
        updates["username"] = body.username
    if body.password is not None:
        updates["hashed_password"] = encode_password(body.password) if body.password else ""
    if body.role is not None:
        updates["role"] = body.role
    if body.active is not None:
        if user_id == 1 and body.active == 0:
            raise HTTPException(status_code=403, detail="Cannot deactivate the default admin")
        updates["active"] = body.active

    if updates:
        set_clause = ", ".join(f"{k}=?" for k in updates)
        try:
            with db() as conn:
                conn.execute(
                    f"UPDATE users SET {set_clause} WHERE id=?",
                    (*updates.values(), user_id),
                )
        except Exception as exc:
            if "UNIQUE" in str(exc):
                raise HTTPException(status_code=409, detail="Username already exists")
            raise

    with db() as conn:
        row = conn.execute(
            "SELECT id, username, role, active, created_at FROM users WHERE id=?", (user_id,)
        ).fetchone()
    return dict(row)


@router.delete("/{user_id}")
def delete_user(user_id: int):
    if user_id == 1:
        raise HTTPException(status_code=403, detail="Cannot delete the default admin")
    with db() as conn:
        cur = conn.execute("DELETE FROM users WHERE id=?", (user_id,))
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="User not found")
    return {"ok": True}
