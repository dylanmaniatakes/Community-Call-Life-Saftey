"""
Core call-routing logic.

Responsibilities:
  - Create / acknowledge / clear calls in the database
  - Broadcast WebSocket events to all connected UI clients
  - Dispatch notification rules (email, page, relay) on new calls
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Optional

from database import db
from modules.ws_manager import manager as ws

log = logging.getLogger("call_manager")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _render_template(template: str, ctx: dict) -> str:
    """Replace {variable} tokens in a template string with values from ctx.
    Unknown variables are left unchanged (safe format_map via defaultdict)."""
    import string

    class _SafeDict(dict):
        def __missing__(self, key):
            return "{" + key + "}"

    try:
        return template.format_map(_SafeDict(ctx))
    except Exception:
        return template


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_device(device_id: str) -> Optional[dict]:
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM devices WHERE device_id = ?", (device_id,)
        ).fetchone()
    return dict(row) if row else None


def _log_event(call_id: int, event: str, actor: str = "system", notes: str = "") -> None:
    with db() as conn:
        conn.execute(
            "INSERT INTO call_events (call_id, event, actor, notes) VALUES (?,?,?,?)",
            (call_id, event, actor, notes),
        )


async def _send_push(payload: dict) -> None:
    """Fire-and-forget Web Push broadcast; errors are logged, never raised."""
    try:
        from modules.push_manager import broadcast_push
        await broadcast_push(payload)
    except Exception as exc:
        log.warning("Push broadcast error: %s", exc)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def process_new_call(device_id: str, raw_data: str = "",
                          source: Optional[str] = None) -> Optional[int]:
    """
    Called by the Innovonics listener (or manual injection) when a device fires.
    source="aux" uses the device's aux_label to annotate the call name.
    Returns the new call_id, or None if the device is not registered / not a call device.
    """
    device = _get_device(device_id)

    if device is None:
        log.warning("Call from unregistered device %s — ignored. Register it in the Devices page.", device_id)
        return None

    # Repeaters marry to the coordinator but never generate calls
    if device.get("device_type") == "repeater":
        log.debug("Frame from repeater %s — ignored.", device_id)
        return None

    # Deduplicate — only one active call per device at a time
    with db() as conn:
        existing = conn.execute(
            "SELECT id FROM calls WHERE device_id=? AND status IN ('active','acknowledged')",
            (device_id,),
        ).fetchone()
    if existing:
        log.debug("Device %s already has active call #%d — skipping duplicate.", device_id, existing["id"])
        return existing["id"]

    # Update last_seen
    with db() as conn:
        conn.execute(
            "UPDATE devices SET last_seen = ? WHERE device_id = ?",
            (_now(), device_id),
        )

    # Build call name — annotate with AUX label when source is auxiliary input
    call_name = device["name"]
    if source == "aux":
        aux_label = (device.get("aux_label") or "AUX").strip() or "AUX"
        call_name = f"{call_name} ({aux_label})"

    # Create call record
    call_id: int
    with db() as conn:
        cur = conn.execute(
            """INSERT INTO calls
               (device_id, device_name, location, priority, status, timestamp, raw_data)
               VALUES (?, ?, ?, ?, 'active', ?, ?)""",
            (
                device_id,
                call_name,
                device.get("location") or "",
                device.get("priority", "normal"),
                _now(),
                raw_data,
            ),
        )
        call_id = cur.lastrowid  # type: ignore[assignment]

    _log_event(call_id, "created")

    call_data = _call_dict(call_id)
    await ws.broadcast("call.new", call_data)
    log.info("New call #%d from device %s (%s)", call_id, device_id, call_name)

    # Web Push — notify all subscribed browsers (even when the PWA is in background)
    priority = device.get("priority", "normal")
    _priority_label = {"emergency": "EMERGENCY", "urgent": "URGENT"}.get(priority, "Call")
    asyncio.create_task(_send_push({
        "title": f"{_priority_label} — {call_name}",
        "body":  device.get("location") or "",
        "priority": priority,
        "tag":  f"call-{call_id}",
    }))

    # Fire notification rules + relays asynchronously (don't block the response)
    asyncio.create_task(_dispatch_rules(call_data, device))
    asyncio.create_task(_fire_device_relay(device))
    asyncio.create_task(_fire_apartment_relay(device))
    asyncio.create_task(_fire_area_relay(device))

    return call_id


async def process_named_call(
    call_key: str,
    call_name: str,
    raw_data: str = "",
    priority: str = "normal",
    location: str = "",
) -> Optional[int]:
    """
    Create an alarm that is not tied to a registered device row.
    call_key is used as the calls.device_id identity for de-duplication/clear.
    """
    with db() as conn:
        existing = conn.execute(
            "SELECT id FROM calls WHERE device_id=? AND status IN ('active','acknowledged')",
            (call_key,),
        ).fetchone()
    if existing:
        log.debug("Named alarm %s already active as call #%d — skipping duplicate.", call_key, existing["id"])
        return existing["id"]

    call_id: int
    with db() as conn:
        cur = conn.execute(
            """INSERT INTO calls
               (device_id, device_name, location, priority, status, timestamp, raw_data)
               VALUES (?, ?, ?, ?, 'active', ?, ?)""",
            (call_key, call_name, location or "", priority, _now(), raw_data),
        )
        call_id = cur.lastrowid  # type: ignore[assignment]

    _log_event(call_id, "created")

    call_data = _call_dict(call_id)
    await ws.broadcast("call.new", call_data)
    log.info("New named call #%d key=%s name=%s", call_id, call_key, call_name)

    _priority_label = {"emergency": "EMERGENCY", "urgent": "URGENT"}.get(priority, "Call")
    asyncio.create_task(_send_push({
        "title": f"{_priority_label} — {call_name}",
        "body":  location or "",
        "priority": priority,
        "tag":  f"call-{call_id}",
    }))

    asyncio.create_task(_dispatch_rules(call_data, None))
    return call_id


async def acknowledge_call(call_id: int, actor: str, notes: str = "") -> bool:
    with db() as conn:
        cur = conn.execute(
            """UPDATE calls SET status='acknowledged', acknowledged_at=?, acknowledged_by=?
               WHERE id=? AND status='active'""",
            (_now(), actor, call_id),
        )
    if cur.rowcount == 0:
        return False
    _log_event(call_id, "acknowledged", actor, notes)
    await ws.broadcast("call.updated", _call_dict(call_id))
    return True


async def clear_call(call_id: int, actor: str, notes: str = "") -> bool:
    with db() as conn:
        cur = conn.execute(
            """UPDATE calls SET status='cleared', cleared_at=?
               WHERE id=? AND status IN ('active','acknowledged')""",
            (_now(), call_id),
        )
    if cur.rowcount == 0:
        return False
    _log_event(call_id, "cleared", actor, notes)
    call_data = _call_dict(call_id)
    await ws.broadcast("call.cleared", call_data)
    # Deactivate relays and fire clear notifications asynchronously
    device = _get_device(call_data.get("device_id", ""))
    if device:
        asyncio.create_task(_deactivate_device_relay(device))
        asyncio.create_task(_deactivate_apartment_relay(device))
        asyncio.create_task(_deactivate_area_relay(device))
    asyncio.create_task(_dispatch_rules(call_data, device, trigger="clear"))
    return True


async def auto_clear_call(device_id: str) -> bool:
    """Auto-clear the active/acknowledged call for a device (e.g. pull-cord reset)."""
    with db() as conn:
        row = conn.execute(
            """SELECT id FROM calls WHERE device_id=? AND status IN ('active','acknowledged')
               ORDER BY id DESC LIMIT 1""",
            (device_id,),
        ).fetchone()
    if not row:
        return False
    return await clear_call(row["id"], "system", "Auto-cleared: device reset")


def _call_dict(call_id: int) -> dict:
    with db() as conn:
        row = conn.execute("SELECT * FROM calls WHERE id=?", (call_id,)).fetchone()
    return dict(row) if row else {}


# ---------------------------------------------------------------------------
# Notification dispatch
# ---------------------------------------------------------------------------

async def _dispatch_rules(call: dict, device: Optional[dict] = None,
                          trigger: str = "call") -> None:
    """Match enabled rules against the call and fire each action.

    trigger: 'call' (new alarm) or 'clear' (alarm cleared).
    Rules with notify_on='both' fire on either; notify_on='call'/'clear' fire on the matching event only.
    """
    with db() as conn:
        rules = conn.execute(
            "SELECT * FROM notification_rules WHERE enabled=1"
        ).fetchall()

    # Resolve the area for this device (direct area_id or via apartment)
    device_area_id: Optional[int] = None
    apt_name = ""
    area_name = ""
    if device:
        device_area_id = device.get("area_id")
        if device.get("apartment_id"):
            with db() as conn:
                apt_row = conn.execute(
                    "SELECT * FROM apartments WHERE id=?",
                    (device["apartment_id"],),
                ).fetchone()
            if apt_row:
                apt_name = apt_row["name"] or ""
                if not device_area_id:
                    device_area_id = apt_row["area_id"]
        if device_area_id:
            with db() as conn:
                area_row = conn.execute(
                    "SELECT name FROM areas WHERE id=?", (device_area_id,)
                ).fetchone()
            if area_row:
                area_name = area_row["name"] or ""

    # Template context — available as {variable} tokens in action_config messages
    tpl_ctx = {
        "device_name":      call.get("device_name", ""),
        "device_id":        call.get("device_id", ""),
        "location":         call.get("location", ""),
        "priority":         call.get("priority", "normal"),
        "PRIORITY":         call.get("priority", "normal").upper(),
        "timestamp":        call.get("timestamp", ""),
        "status":           call.get("status", ""),
        "call_id":          str(call.get("id", "")),
        "apartment":        apt_name,
        "area":             area_name,
        "device_type":      device.get("device_type", "") if device else "",
        "vendor_type":      device.get("vendor_type", "") if device else "",
        "acknowledged_by":  call.get("acknowledged_by") or "",
        "acknowledged_at":  call.get("acknowledged_at") or "",
        "cleared_at":       call.get("cleared_at") or "",
        "event":            "CLEARED" if trigger == "clear" else "NEW CALL",
    }

    for rule in rules:
        rule = dict(rule)
        # Trigger filter (notify_on: call | clear | both)
        notify_on = rule.get("notify_on", "call") or "call"
        if notify_on != "both" and notify_on != trigger:
            continue
        # Device filter
        if rule["device_filter"] != "all":
            allowed = json.loads(rule["device_filter"])
            if call["device_id"] not in allowed:
                continue
        # Priority filter
        if rule["priority_filter"] != "all":
            if call["priority"] != rule["priority_filter"]:
                continue
        # Area filter
        area_filter = rule.get("area_filter", "all")
        if area_filter != "all":
            if device_area_id is None or str(device_area_id) != str(area_filter):
                continue

        config = json.loads(rule["action_config"])
        # Apply template variable substitution to all string fields in action_config
        config = {
            k: _render_template(v, tpl_ctx) if isinstance(v, str) else v
            for k, v in config.items()
        }
        action = rule["action_type"]

        try:
            if action == "email":
                await _fire_email(call, config)
            elif action == "page":
                await _fire_page(call, config)
            elif action == "relay":
                await _fire_relay(call, config)
            elif action == "telegram":
                await _fire_telegram(call, config)
            elif action == "twilio":
                await _fire_twilio(call, config)
        except Exception as exc:
            log.error("Rule '%s' (%s) failed: %s", rule["name"], action, exc)


async def _fire_email(call: dict, config: dict) -> None:
    from modules.email_notifier import send_email
    from database import db as _db

    with _db() as conn:
        smtp = conn.execute("SELECT * FROM smtp_config WHERE id=1").fetchone()
    if not smtp or not smtp["enabled"]:
        return
    smtp = dict(smtp)

    subject = config.get(
        "subject", f"[NURSE CALL] {call['device_name']} — {call['location']}"
    )
    body = config.get(
        "body",
        f"Call from: {call['device_name']}\n"
        f"Location:  {call['location']}\n"
        f"Priority:  {call['priority']}\n"
        f"Time:      {call['timestamp']}\n",
    )
    recipients = config.get("recipients", smtp.get("email", ""))
    await asyncio.to_thread(send_email, smtp, recipients, subject, body)


async def _fire_page(call: dict, config: dict) -> None:
    from modules.paging import send_page
    from database import db as _db

    pager_id = config.get("pager_id")
    if not pager_id:
        return
    with _db() as conn:
        pager = conn.execute(
            "SELECT * FROM pager_configs WHERE id=? AND enabled=1", (pager_id,)
        ).fetchone()
    if not pager:
        return
    pager = dict(pager)
    capcode = config.get("capcode") or pager["default_capcode"] or "0"
    message = config.get(
        "message",
        f"CALL: {call['device_name']} {call['location']} [{call['priority'].upper()}]",
    )
    await asyncio.to_thread(
        send_page, pager["host"], pager["port"], pager["protocol"], capcode, message
    )


async def _fire_relay(call: dict, config: dict) -> None:
    from modules.relay import activate_relay
    from database import db as _db

    relay_id = config.get("relay_id")
    if not relay_id:
        return
    with _db() as conn:
        relay = conn.execute(
            "SELECT * FROM relay_configs WHERE id=? AND enabled=1", (relay_id,)
        ).fetchone()
    if not relay:
        return
    relay = dict(relay)
    await asyncio.to_thread(
        activate_relay,
        relay["host"],
        relay["port"],
        relay["relay_type"],
        relay["relay_number"],
    )


def _load_relay(relay_config_id: int) -> Optional[dict]:
    """Return relay_config row as dict if enabled, else None."""
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM relay_configs WHERE id=? AND enabled=1", (relay_config_id,)
        ).fetchone()
    return dict(row) if row else None


async def _operate_relay(relay: dict, activate: bool, label: str) -> None:
    from modules.relay import activate_relay, deactivate_relay
    fn = activate_relay if activate else deactivate_relay
    try:
        await asyncio.to_thread(
            fn, relay["host"], relay["port"], relay["relay_type"], relay["relay_number"]
        )
        log.info("%s relay: %s (relay %s)", "Activated" if activate else "Deactivated",
                 relay["name"], relay["relay_number"])
    except Exception as exc:
        log.error("%s relay '%s' failed: %s",
                  "Activating" if activate else "Deactivating", label, exc)


def _resolve_area_id(device: dict) -> Optional[int]:
    """Return area_id for device: device's own first, else apartment's."""
    if device.get("area_id"):
        return device["area_id"]
    if device.get("apartment_id"):
        with db() as conn:
            apt = conn.execute(
                "SELECT area_id FROM apartments WHERE id=?", (device["apartment_id"],)
            ).fetchone()
        if apt and apt["area_id"]:
            return apt["area_id"]
    return None


async def _fire_device_relay(device: dict) -> None:
    rc_id = device.get("relay_config_id")
    if not rc_id:
        return
    relay = _load_relay(rc_id)
    if relay:
        await _operate_relay(relay, activate=True, label=f"device {device.get('device_id')}")


async def _deactivate_device_relay(device: dict) -> None:
    rc_id = device.get("relay_config_id")
    if not rc_id:
        return
    relay = _load_relay(rc_id)
    if relay:
        await _operate_relay(relay, activate=False, label=f"device {device.get('device_id')}")


async def _fire_apartment_relay(device: dict) -> None:
    apt_id = device.get("apartment_id")
    if not apt_id:
        return
    with db() as conn:
        apt = conn.execute("SELECT * FROM apartments WHERE id=?", (apt_id,)).fetchone()
    if not apt or not apt["relay_config_id"]:
        return
    relay = _load_relay(apt["relay_config_id"])
    if relay:
        await _operate_relay(relay, activate=True, label=f"apartment {apt_id}")


async def _deactivate_apartment_relay(device: dict) -> None:
    apt_id = device.get("apartment_id")
    if not apt_id:
        return
    with db() as conn:
        apt = conn.execute("SELECT * FROM apartments WHERE id=?", (apt_id,)).fetchone()
    if not apt or not apt["relay_config_id"]:
        return
    relay = _load_relay(apt["relay_config_id"])
    if relay:
        await _operate_relay(relay, activate=False, label=f"apartment {apt_id}")


async def _fire_area_relay(device: dict) -> None:
    area_id = _resolve_area_id(device)
    if not area_id:
        return
    with db() as conn:
        area = conn.execute("SELECT * FROM areas WHERE id=?", (area_id,)).fetchone()
    if not area or not area["relay_config_id"]:
        return
    relay = _load_relay(area["relay_config_id"])
    if relay:
        await _operate_relay(relay, activate=True, label=f"area {area_id}")


async def _deactivate_area_relay(device: dict) -> None:
    area_id = _resolve_area_id(device)
    if not area_id:
        return
    with db() as conn:
        area = conn.execute("SELECT * FROM areas WHERE id=?", (area_id,)).fetchone()
    if not area or not area["relay_config_id"]:
        return
    relay = _load_relay(area["relay_config_id"])
    if relay:
        await _operate_relay(relay, activate=False, label=f"area {area_id}")


async def _fire_telegram(call: dict, config: dict) -> None:
    from modules.telegram_notifier import send_telegram
    from database import db as _db

    with _db() as conn:
        cfg = conn.execute("SELECT * FROM telegram_config WHERE id=1").fetchone()
    if not cfg or not cfg["enabled"]:
        return
    cfg = dict(cfg)
    chat_id = config.get("chat_id") or cfg.get("chat_id")
    if not chat_id:
        return
    message = config.get(
        "message",
        f"*CALL*: {call['device_name']}\n"
        f"Location: {call['location']}\n"
        f"Priority: {call['priority'].upper()}\n"
        f"Time: {call['timestamp']}",
    )
    await asyncio.to_thread(send_telegram, cfg, chat_id, message)


async def _fire_twilio(call: dict, config: dict) -> None:
    from modules.twilio_notifier import send_sms
    from database import db as _db

    with _db() as conn:
        cfg = conn.execute("SELECT * FROM twilio_config WHERE id=1").fetchone()
    if not cfg or not cfg["enabled"]:
        return
    cfg = dict(cfg)
    to_number = config.get("to_number") or cfg.get("to_number")
    if not to_number:
        return
    message = config.get(
        "message",
        f"CALL: {call['device_name']} {call['location']} [{call['priority'].upper()}]",
    )
    await asyncio.to_thread(send_sms, cfg, to_number, message)
