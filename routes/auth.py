"""
Authentication routes.
  GET  /api/auth/config        — public; returns {auth_required}
  PUT  /api/auth/config        — toggle auth (admin only when auth is on)
  POST /api/auth/login         — returns session token
  POST /api/auth/logout        — revokes token
  GET  /api/auth/me            — returns current user or 401
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from database import db
from modules.auth import create_token, get_session, revoke_token, verify_password

router = APIRouter()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def auth_required() -> bool:
    with db() as conn:
        row = conn.execute(
            "SELECT auth_required FROM system_config WHERE id=1"
        ).fetchone()
    return bool(row and row["auth_required"])


def current_user(request: Request) -> dict | None:
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return get_session(auth[7:])
    return None


def require_admin(request: Request) -> dict:
    """Raise 401/403 if the request is not from an admin when auth is required."""
    if auth_required():
        user = current_user(request)
        if not user:
            raise HTTPException(status_code=401, detail="Authentication required")
        if user["role"] != "admin":
            raise HTTPException(status_code=403, detail="Admin role required")
        return user
    return {"user_id": 0, "username": "system", "role": "admin"}


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/config")
def get_config():
    return {"auth_required": auth_required()}


@router.put("/config")
async def set_config(request: Request):
    body = await request.json()
    require_admin(request)
    with db() as conn:
        conn.execute(
            "UPDATE system_config SET auth_required=? WHERE id=1",
            (1 if body.get("auth_required") else 0,),
        )
    return {"auth_required": auth_required()}


class LoginRequest(BaseModel):
    username: str
    password: str


@router.post("/login")
def login(body: LoginRequest):
    with db() as conn:
        row = conn.execute(
            "SELECT id, username, hashed_password, role, active FROM users WHERE username=?",
            (body.username,),
        ).fetchone()
    if not row or not row["active"]:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    row = dict(row)
    stored = row.get("hashed_password") or ""
    if stored:
        if not verify_password(body.password, stored):
            raise HTTPException(status_code=401, detail="Invalid credentials")
    elif body.password:
        # No password set on account — only blank password allowed
        raise HTTPException(status_code=401, detail="Invalid credentials")
    token = create_token(row["id"], row["username"], row["role"])
    return {"token": token, "username": row["username"], "role": row["role"]}


@router.post("/logout")
def logout(request: Request):
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        revoke_token(auth[7:])
    return {"ok": True}


@router.get("/me")
def me(request: Request):
    if not auth_required():
        return {"authenticated": False, "auth_required": False}
    user = current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return {**user, "authenticated": True, "auth_required": True}
