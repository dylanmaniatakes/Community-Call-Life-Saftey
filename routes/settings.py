"""
Settings routes — SMTP, pagers, relays, Innovonics coordinator,
notification rules, plus test endpoints for each module.
"""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, HTTPException, Request

from database import db
from models import (
    InnoConfig,
    InputBatch,
    InputCreate,
    InputEvent,
    InputTest,
    InputUpdate,
    PagerCreate,
    PagerTest,
    PagerUpdate,
    RelayBatch,
    RelayCreate,
    RelayTest,
    RelayUpdate,
    RepeaterCreate,
    RepeaterUpdate,
    RuleCreate,
    RuleUpdate,
    SmtpConfig,
    SmtpTest,
    TelegramConfig,
    TelegramTest,
    TwilioConfig,
    TwilioTest,
)

router = APIRouter()


def _normalize_state(value) -> bool:
    """Best-effort conversion of external input state to boolean."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value > 0
    if isinstance(value, str):
        v = value.strip().lower()
        if v in {"1", "true", "on", "active", "alarm", "pressed", "open", "high", "triggered"}:
            return True
        if v in {"0", "false", "off", "inactive", "reset", "released", "closed", "low", "normal"}:
            return False
    raise HTTPException(status_code=400, detail="Invalid state value")


def _normalize_host(host: str | None) -> str:
    if not host:
        return ""
    h = host.strip().lower()
    if h.startswith("http://"):
        h = h[7:]
    elif h.startswith("https://"):
        h = h[8:]
    h = h.split("/", 1)[0]
    h = h.split(":", 1)[0]
    return h


def _coerce_int(value):
    if value is None or value == "":
        return None
    try:
        return int(value)
    except Exception:
        return None


def _call_key_for_input(cfg: dict) -> str:
    return f"input:{cfg['id']}"


# ===========================================================================
# SMTP
# ===========================================================================

@router.get("/smtp")
def get_smtp():
    with db() as conn:
        row = conn.execute("SELECT * FROM smtp_config WHERE id=1").fetchone()
    if not row:
        return {}
    d = dict(row)
    d.pop("password", None)  # never expose password via GET
    return d


@router.put("/smtp")
def save_smtp(body: SmtpConfig):
    with db() as conn:
        conn.execute(
            """UPDATE smtp_config SET server=?, port=?, email=?, password=?,
               encryption=?, enabled=? WHERE id=1""",
            (body.server, body.port, body.email, body.password,
             body.encryption, body.enabled),
        )
    return {"ok": True}


@router.post("/smtp/test")
async def test_smtp(body: SmtpTest):
    from modules.email_notifier import send_email

    with db() as conn:
        cfg = conn.execute("SELECT * FROM smtp_config WHERE id=1").fetchone()
    if not cfg:
        raise HTTPException(status_code=400, detail="SMTP not configured")
    try:
        await asyncio.to_thread(
            send_email,
            dict(cfg),
            body.to,
            "Nurse Call System — SMTP Test",
            "This is a test message from the Nurse Call System.",
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return {"ok": True}


# ===========================================================================
# Pagers
# ===========================================================================

@router.get("/pagers")
def list_pagers():
    with db() as conn:
        rows = conn.execute("SELECT * FROM pager_configs ORDER BY id").fetchall()
    return [dict(r) for r in rows]


@router.post("/pagers")
def create_pager(body: PagerCreate):
    with db() as conn:
        cur = conn.execute(
            """INSERT INTO pager_configs
               (name, host, port, protocol, default_capcode, enabled)
               VALUES (?,?,?,?,?,?)""",
            (body.name, body.host, body.port, body.protocol,
             body.default_capcode, body.enabled),
        )
        row_id = cur.lastrowid
    with db() as conn:
        row = conn.execute("SELECT * FROM pager_configs WHERE id=?", (row_id,)).fetchone()
    return dict(row)


@router.put("/pagers/{pager_id}")
def update_pager(pager_id: int, body: PagerUpdate):
    with db() as conn:
        cur = conn.execute(
            """UPDATE pager_configs SET name=?, host=?, port=?, protocol=?,
               default_capcode=?, enabled=? WHERE id=?""",
            (body.name, body.host, body.port, body.protocol,
             body.default_capcode, body.enabled, pager_id),
        )
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="Pager not found")
    with db() as conn:
        row = conn.execute("SELECT * FROM pager_configs WHERE id=?", (pager_id,)).fetchone()
    return dict(row)


@router.delete("/pagers/{pager_id}")
def delete_pager(pager_id: int):
    with db() as conn:
        cur = conn.execute("DELETE FROM pager_configs WHERE id=?", (pager_id,))
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="Pager not found")
    return {"ok": True}


@router.post("/pagers/{pager_id}/test")
async def test_pager(pager_id: int, body: PagerTest):
    from modules.paging import send_page

    with db() as conn:
        pager = conn.execute(
            "SELECT * FROM pager_configs WHERE id=?", (pager_id,)
        ).fetchone()
    if not pager:
        raise HTTPException(status_code=404, detail="Pager not found")
    pager = dict(pager)
    try:
        await asyncio.to_thread(
            send_page,
            pager["host"], pager["port"], pager["protocol"],
            body.capcode, body.message,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return {"ok": True}


# ===========================================================================
# Relays / Dome Lights
# ===========================================================================

@router.get("/relays")
def list_relays():
    with db() as conn:
        rows = conn.execute("SELECT * FROM relay_configs ORDER BY id").fetchall()
    return [dict(r) for r in rows]


@router.post("/relays")
def create_relay(body: RelayCreate):
    with db() as conn:
        cur = conn.execute(
            """INSERT INTO relay_configs
               (name, relay_type, host, port, relay_number, enabled)
               VALUES (?,?,?,?,?,?)""",
            (body.name, body.relay_type, body.host, body.port,
             body.relay_number, body.enabled),
        )
        row_id = cur.lastrowid
    with db() as conn:
        row = conn.execute("SELECT * FROM relay_configs WHERE id=?", (row_id,)).fetchone()
    return dict(row)


@router.put("/relays/{relay_id}")
def update_relay(relay_id: int, body: RelayUpdate):
    with db() as conn:
        cur = conn.execute(
            """UPDATE relay_configs SET name=?, relay_type=?, host=?, port=?,
               relay_number=?, enabled=? WHERE id=?""",
            (body.name, body.relay_type, body.host, body.port,
             body.relay_number, body.enabled, relay_id),
        )
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="Relay not found")
    with db() as conn:
        row = conn.execute("SELECT * FROM relay_configs WHERE id=?", (relay_id,)).fetchone()
    return dict(row)


@router.delete("/relays/{relay_id}")
def delete_relay(relay_id: int):
    with db() as conn:
        cur = conn.execute("DELETE FROM relay_configs WHERE id=?", (relay_id,))
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="Relay not found")
    return {"ok": True}


@router.post("/relays/batch")
def batch_create_relays(body: RelayBatch):
    """Create one relay_config entry per channel (relay_number = 1..count)."""
    created = []
    with db() as conn:
        for n in range(1, body.count + 1):
            cur = conn.execute(
                """INSERT INTO relay_configs
                   (name, relay_type, host, port, relay_number, enabled)
                   VALUES (?,?,?,?,?,?)""",
                (f"{body.name_prefix} {n}", body.relay_type, body.host,
                 body.port, n, body.enabled),
            )
            row = conn.execute(
                "SELECT * FROM relay_configs WHERE id=?", (cur.lastrowid,)
            ).fetchone()
            created.append(dict(row))
    return created


@router.post("/relays/{relay_id}/clone")
def clone_relay(relay_id: int):
    """Duplicate a relay config entry (name prefixed with 'Copy of ')."""
    with db() as conn:
        src = conn.execute(
            "SELECT * FROM relay_configs WHERE id=?", (relay_id,)
        ).fetchone()
    if not src:
        raise HTTPException(status_code=404, detail="Relay not found")
    src = dict(src)
    with db() as conn:
        cur = conn.execute(
            """INSERT INTO relay_configs
               (name, relay_type, host, port, relay_number, enabled)
               VALUES (?,?,?,?,?,?)""",
            (f"Copy of {src['name']}", src["relay_type"], src["host"],
             src["port"], src["relay_number"], src["enabled"]),
        )
        row = conn.execute(
            "SELECT * FROM relay_configs WHERE id=?", (cur.lastrowid,)
        ).fetchone()
    return dict(row)


@router.post("/relays/{relay_id}/test")
async def test_relay(relay_id: int, body: RelayTest):
    from modules.relay import activate_relay, deactivate_relay

    with db() as conn:
        relay = conn.execute(
            "SELECT * FROM relay_configs WHERE id=?", (relay_id,)
        ).fetchone()
    if not relay:
        raise HTTPException(status_code=404, detail="Relay not found")
    relay = dict(relay)
    try:
        await asyncio.to_thread(
            activate_relay,
            relay["host"], relay["port"], relay["relay_type"], relay["relay_number"],
        )
        await asyncio.sleep(body.duration_seconds)
        await asyncio.to_thread(
            deactivate_relay,
            relay["host"], relay["port"], relay["relay_type"], relay["relay_number"],
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return {"ok": True}


# ===========================================================================
# Inputs (ESP / external alarm triggers)
# ===========================================================================

@router.get("/inputs")
def list_inputs():
    with db() as conn:
        rows = conn.execute("SELECT * FROM input_configs ORDER BY id").fetchall()
    return [dict(r) for r in rows]


@router.post("/inputs")
def create_input(body: InputCreate):
    with db() as conn:
        if body.device_id:
            dev = conn.execute(
                "SELECT device_id FROM devices WHERE device_id=?",
                (body.device_id,),
            ).fetchone()
            if not dev:
                raise HTTPException(status_code=400, detail="device_id not found")
        cur = conn.execute(
            """INSERT INTO input_configs
               (name, input_type, host, port, input_number, input_name, device_id, active_high, enabled)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                body.name, body.input_type, body.host, body.port, body.input_number,
                body.input_name, body.device_id or "", body.active_high, body.enabled,
            ),
        )
        row_id = cur.lastrowid
        row = conn.execute("SELECT * FROM input_configs WHERE id=?", (row_id,)).fetchone()
    return dict(row)


@router.post("/inputs/batch")
def batch_create_inputs(body: InputBatch):
    """Create one input_config entry per channel (input_number = start..start+count-1)."""
    if body.count < 1:
        raise HTTPException(status_code=400, detail="count must be at least 1")
    if body.start_number < 1:
        raise HTTPException(status_code=400, detail="start_number must be at least 1")

    created = []
    name_prefix = body.name_prefix.strip()
    in_prefix = (body.input_name_prefix or "").strip()

    with db() as conn:
        for offset in range(body.count):
            n = body.start_number + offset
            input_name = f"{in_prefix}{n}" if in_prefix else None
            cur = conn.execute(
                """INSERT INTO input_configs
                   (name, input_type, host, port, input_number, input_name, device_id, active_high, enabled)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    f"{name_prefix} {n}",
                    "esp",
                    body.host,
                    body.port,
                    n,
                    input_name,
                    "",
                    body.active_high,
                    body.enabled,
                ),
            )
            row = conn.execute(
                "SELECT * FROM input_configs WHERE id=?",
                (cur.lastrowid,),
            ).fetchone()
            created.append(dict(row))
    return created


@router.put("/inputs/{input_id}")
def update_input(input_id: int, body: InputUpdate):
    with db() as conn:
        if body.device_id:
            dev = conn.execute(
                "SELECT device_id FROM devices WHERE device_id=?",
                (body.device_id,),
            ).fetchone()
            if not dev:
                raise HTTPException(status_code=400, detail="device_id not found")
        cur = conn.execute(
            """UPDATE input_configs
               SET name=?, input_type=?, host=?, port=?, input_number=?, input_name=?,
                   device_id=?, active_high=?, enabled=?
               WHERE id=?""",
            (
                body.name, body.input_type, body.host, body.port, body.input_number,
                body.input_name, body.device_id or "", body.active_high, body.enabled, input_id,
            ),
        )
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="Input not found")
    with db() as conn:
        row = conn.execute("SELECT * FROM input_configs WHERE id=?", (input_id,)).fetchone()
    return dict(row)


@router.delete("/inputs/{input_id}")
def delete_input(input_id: int):
    with db() as conn:
        cur = conn.execute("DELETE FROM input_configs WHERE id=?", (input_id,))
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="Input not found")
    return {"ok": True}


@router.post("/inputs/{input_id}/test")
async def test_input(input_id: int, body: InputTest):
    from modules.call_manager import auto_clear_call, process_named_call, process_new_call

    with db() as conn:
        row = conn.execute(
            "SELECT * FROM input_configs WHERE id=? AND enabled=1",
            (input_id,),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Input not found or disabled")
    cfg = dict(row)

    if cfg.get("device_id"):
        call_id = await process_new_call(cfg["device_id"], f"input-test:{cfg['name']}", source="aux")
        if call_id is None:
            await process_named_call(
                _call_key_for_input(cfg),
                cfg["name"],
                f"input-test:{cfg['name']}",
                priority="normal",
                location=f"{cfg.get('host')}:{cfg.get('input_number')}",
            )
    else:
        await process_named_call(
            _call_key_for_input(cfg),
            cfg["name"],
            f"input-test:{cfg['name']}",
            priority="normal",
            location=f"{cfg.get('host')}:{cfg.get('input_number')}",
        )
    await asyncio.sleep(body.duration_seconds)
    await auto_clear_call(cfg.get("device_id") or _call_key_for_input(cfg))
    return {"ok": True}


@router.api_route("/inputs/event", methods=["GET", "POST"])
async def process_input_event(request: Request):
    """
    External event ingress for ESP inputs.
    Match order:
      1) input_id
      2) host + input_name
      3) host + input_number
    """
    from modules.call_manager import auto_clear_call, process_named_call, process_new_call

    payload: dict = {}
    if request.method == "GET":
        payload = dict(request.query_params)
    else:
        content_type = (request.headers.get("content-type") or "").lower()
        if "application/json" in content_type:
            payload = await request.json()
        elif "application/x-www-form-urlencoded" in content_type or "multipart/form-data" in content_type:
            form = await request.form()
            payload = dict(form)
        else:
            try:
                payload = await request.json()
            except Exception:
                payload = dict(request.query_params)

    if "state" not in payload:
        raise HTTPException(status_code=400, detail="Missing state field")

    event = InputEvent(
        input_id=_coerce_int(payload.get("input_id") or payload.get("id")),
        host=(payload.get("host") or payload.get("ip") or payload.get("device") or
              payload.get("source_host") or (request.client.host if request.client else None)),
        port=_coerce_int(payload.get("port")),
        input_number=_coerce_int(payload.get("input_number") or payload.get("input") or payload.get("pin") or payload.get("channel")),
        input_name=(payload.get("input_name") or payload.get("name") or payload.get("entity_id")),
        state=payload.get("state"),
        raw_data=payload.get("raw_data") or json.dumps(payload),
    )

    event_host = _normalize_host(event.host)

    with db() as conn:
        row = None
        if event.input_id:
            row = conn.execute(
                "SELECT * FROM input_configs WHERE id=? AND enabled=1",
                (event.input_id,),
            ).fetchone()
        elif event_host and event.input_name:
            candidates = conn.execute(
                """SELECT * FROM input_configs
                   WHERE input_name=? AND enabled=1
                   ORDER BY id""",
                (event.input_name,),
            ).fetchall()
            row = next((r for r in candidates if _normalize_host(r["host"]) == event_host), None)
        elif event_host and event.input_number is not None:
            candidates = conn.execute(
                """SELECT * FROM input_configs
                   WHERE input_number=? AND enabled=1
                   ORDER BY id""",
                (event.input_number,),
            ).fetchall()
            row = next((r for r in candidates if _normalize_host(r["host"]) == event_host), None)

    if not row:
        raise HTTPException(status_code=404, detail="No enabled input mapping matched this event")

    cfg = dict(row)
    is_active = _normalize_state(event.state)
    alarm_state = is_active if cfg.get("active_high", 1) else (not is_active)
    state_token = "1" if is_active else "0"

    with db() as conn:
        conn.execute(
            "UPDATE input_configs SET last_state=?, last_seen=datetime('now','utc') WHERE id=?",
            (state_token, cfg["id"]),
        )

    call_key = cfg.get("device_id") or _call_key_for_input(cfg)

    if alarm_state:
        raw_data = event.raw_data or f"input:{cfg.get('input_name') or cfg.get('input_number')}"
        if cfg.get("device_id"):
            call_id = await process_new_call(cfg["device_id"], raw_data, source="aux")
            if call_id is None:
                call_id = await process_named_call(
                    _call_key_for_input(cfg),
                    cfg["name"],
                    raw_data,
                    priority="normal",
                    location=f"{cfg.get('host')}:{cfg.get('input_number')}",
                )
        else:
            call_id = await process_named_call(
                _call_key_for_input(cfg),
                cfg["name"],
                raw_data,
                priority="normal",
                location=f"{cfg.get('host')}:{cfg.get('input_number')}",
            )
        return {"ok": True, "action": "alarm", "call_id": call_id, "input_id": cfg["id"]}

    cleared = await auto_clear_call(call_key)
    return {"ok": True, "action": "clear", "cleared": bool(cleared), "input_id": cfg["id"]}


# ===========================================================================
# Innovonics coordinator
# ===========================================================================

@router.get("/innovonics")
def get_innovonics():
    with db() as conn:
        row = conn.execute("SELECT * FROM innovonics_config WHERE id=1").fetchone()
    return dict(row) if row else {}


@router.get("/innovonics/status")
def get_innovonics_status():
    from modules.innovonics import get_status
    return {"status": get_status()}


@router.put("/innovonics")
def save_innovonics(body: InnoConfig):
    with db() as conn:
        conn.execute(
            """UPDATE innovonics_config SET mode=?, host=?, port=?,
               serial_port=?, baud_rate=?, nid=?, enabled=? WHERE id=1""",
            (body.mode, body.host, body.port,
             body.serial_port, body.baud_rate, body.nid, body.enabled),
        )
    return {"ok": True, "note": "Restart the server to apply coordinator changes."}


# ===========================================================================
# Repeaters
# ===========================================================================

@router.get("/repeaters")
def list_repeaters():
    with db() as conn:
        rows = conn.execute("SELECT * FROM repeaters ORDER BY name, serial_number").fetchall()
    return [dict(r) for r in rows]


@router.post("/repeaters")
def create_repeater(body: RepeaterCreate):
    from datetime import datetime, timezone
    with db() as conn:
        try:
            cur = conn.execute(
                "INSERT INTO repeaters (serial_number, name) VALUES (?, ?)",
                (body.serial_number, body.name or f"Repeater {body.serial_number}"),
            )
            row_id = cur.lastrowid
        except Exception as exc:
            if "UNIQUE" in str(exc):
                raise HTTPException(status_code=409,
                                    detail=f"Repeater '{body.serial_number}' already registered")
            raise
    with db() as conn:
        row = conn.execute("SELECT * FROM repeaters WHERE id=?", (row_id,)).fetchone()
    return dict(row)


@router.patch("/repeaters/{repeater_id}")
def update_repeater(repeater_id: int, body: RepeaterUpdate):
    if body.name is None:
        raise HTTPException(status_code=400, detail="No fields to update")
    with db() as conn:
        cur = conn.execute(
            "UPDATE repeaters SET name=? WHERE id=?",
            (body.name, repeater_id),
        )
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="Repeater not found")
    with db() as conn:
        row = conn.execute("SELECT * FROM repeaters WHERE id=?", (repeater_id,)).fetchone()
    return dict(row)


@router.delete("/repeaters/{repeater_id}")
def delete_repeater(repeater_id: int):
    with db() as conn:
        cur = conn.execute("DELETE FROM repeaters WHERE id=?", (repeater_id,))
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="Repeater not found")
    return {"ok": True}


@router.post("/repeaters/{repeater_id}/force-nid")
async def force_repeater_nid(repeater_id: int):
    """
    Send a set_repeater_nid command for this repeater over the live coordinator
    connection.  The coordinator pushes the current NID to the repeater; the
    repeater confirms with MCB 0x21 and the DB status is set to 'online'.
    """
    with db() as conn:
        row = conn.execute(
            "SELECT serial_number FROM repeaters WHERE id=?", (repeater_id,)
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Repeater not found")

    from modules.innovonics import force_repeater_nid as _force
    result = await _force(row["serial_number"])
    if not result["ok"]:
        raise HTTPException(status_code=400, detail=result["detail"])
    return result


# ===========================================================================
# Notification rules
# ===========================================================================

@router.get("/rules")
def list_rules():
    with db() as conn:
        rows = conn.execute("SELECT * FROM notification_rules ORDER BY id").fetchall()
    return [dict(r) for r in rows]


@router.post("/rules")
def create_rule(body: RuleCreate):
    # Validate action_config is valid JSON
    try:
        json.loads(body.action_config)
    except ValueError:
        raise HTTPException(status_code=400, detail="action_config must be valid JSON")
    with db() as conn:
        cur = conn.execute(
            """INSERT INTO notification_rules
               (name, device_filter, priority_filter, area_filter, notify_on, action_type, action_config, enabled)
               VALUES (?,?,?,?,?,?,?,?)""",
            (body.name, body.device_filter, body.priority_filter, body.area_filter,
             body.notify_on, body.action_type, body.action_config, body.enabled),
        )
        row_id = cur.lastrowid
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM notification_rules WHERE id=?", (row_id,)
        ).fetchone()
    return dict(row)


@router.put("/rules/{rule_id}")
def update_rule(rule_id: int, body: RuleUpdate):
    try:
        json.loads(body.action_config)
    except ValueError:
        raise HTTPException(status_code=400, detail="action_config must be valid JSON")
    with db() as conn:
        cur = conn.execute(
            """UPDATE notification_rules SET name=?, device_filter=?, priority_filter=?,
               area_filter=?, notify_on=?, action_type=?, action_config=?, enabled=? WHERE id=?""",
            (body.name, body.device_filter, body.priority_filter, body.area_filter,
             body.notify_on, body.action_type, body.action_config, body.enabled, rule_id),
        )
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="Rule not found")
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM notification_rules WHERE id=?", (rule_id,)
        ).fetchone()
    return dict(row)


@router.delete("/rules/{rule_id}")
def delete_rule(rule_id: int):
    with db() as conn:
        cur = conn.execute("DELETE FROM notification_rules WHERE id=?", (rule_id,))
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="Rule not found")
    return {"ok": True}


# ===========================================================================
# Telegram
# ===========================================================================

@router.get("/telegram")
def get_telegram():
    with db() as conn:
        row = conn.execute("SELECT * FROM telegram_config WHERE id=1").fetchone()
    if not row:
        return {}
    d = dict(row)
    d.pop("bot_token", None)   # never expose token via GET
    return d


@router.put("/telegram")
def save_telegram(body: TelegramConfig):
    with db() as conn:
        if body.bot_token:
            conn.execute(
                "UPDATE telegram_config SET bot_token=?, chat_id=?, enabled=? WHERE id=1",
                (body.bot_token, body.chat_id, body.enabled),
            )
        else:
            # Preserve existing token if not supplied
            conn.execute(
                "UPDATE telegram_config SET chat_id=?, enabled=? WHERE id=1",
                (body.chat_id, body.enabled),
            )
    return {"ok": True}


@router.post("/telegram/test")
async def test_telegram(body: TelegramTest):
    from modules.telegram_notifier import send_telegram

    with db() as conn:
        cfg = conn.execute("SELECT * FROM telegram_config WHERE id=1").fetchone()
    if not cfg:
        raise HTTPException(status_code=400, detail="Telegram not configured")
    cfg = dict(cfg)
    chat_id = body.chat_id or cfg.get("chat_id")
    if not chat_id:
        raise HTTPException(status_code=400, detail="No chat_id provided")
    try:
        await asyncio.to_thread(
            send_telegram, cfg, chat_id, "Community Call System — Telegram test message."
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return {"ok": True}


# ===========================================================================
# Twilio
# ===========================================================================

@router.get("/twilio")
def get_twilio():
    with db() as conn:
        row = conn.execute("SELECT * FROM twilio_config WHERE id=1").fetchone()
    if not row:
        return {}
    d = dict(row)
    d.pop("auth_token", None)  # never expose token via GET
    return d


@router.put("/twilio")
def save_twilio(body: TwilioConfig):
    with db() as conn:
        if body.auth_token:
            conn.execute(
                """UPDATE twilio_config SET account_sid=?, auth_token=?,
                   from_number=?, to_number=?, enabled=? WHERE id=1""",
                (body.account_sid, body.auth_token,
                 body.from_number, body.to_number, body.enabled),
            )
        else:
            # Preserve existing auth_token if not supplied
            conn.execute(
                """UPDATE twilio_config SET account_sid=?,
                   from_number=?, to_number=?, enabled=? WHERE id=1""",
                (body.account_sid, body.from_number, body.to_number, body.enabled),
            )
    return {"ok": True}


@router.post("/twilio/test")
async def test_twilio(body: TwilioTest):
    from modules.twilio_notifier import send_sms

    with db() as conn:
        cfg = conn.execute("SELECT * FROM twilio_config WHERE id=1").fetchone()
    if not cfg:
        raise HTTPException(status_code=400, detail="Twilio not configured")
    cfg = dict(cfg)
    to_number = body.to_number or cfg.get("to_number")
    if not to_number:
        raise HTTPException(status_code=400, detail="No to_number provided")
    try:
        await asyncio.to_thread(
            send_sms, cfg, to_number, "Community Call System — Twilio SMS test message."
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return {"ok": True}
