"""
Input monitor for ESP/ESPHome-based binary inputs.

The server actively polls configured inputs and turns changes into alarms,
so ESP devices do not need hardcoded webhook callbacks.
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.parse
import urllib.request
from typing import Optional

from database import db
from modules.call_manager import auto_clear_call, process_named_call, process_new_call

log = logging.getLogger("input_monitor")

_stop_event: Optional[asyncio.Event] = None

POLL_INTERVAL_SECONDS = 1.0
REQUEST_TIMEOUT_SECONDS = 3


def _call_key(cfg: dict) -> str:
    return cfg.get("device_id") or f"input:{cfg['id']}"


def _normalize_state(value) -> bool:
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
    raise ValueError(f"Invalid state value: {value!r}")


def _sensor_name(cfg: dict) -> str:
    explicit = (cfg.get("input_name") or "").strip()
    if explicit:
        return explicit
    return f"input-{int(cfg.get('input_number') or 1)}"


def _host_only(host: str) -> str:
    h = (host or "").strip()
    if h.startswith("http://"):
        h = h[7:]
    elif h.startswith("https://"):
        h = h[8:]
    h = h.split("/", 1)[0]
    return h


def _candidate_sensor_names(cfg: dict) -> list[str]:
    names: list[str] = []
    explicit = (cfg.get("input_name") or "").strip()
    if explicit:
        names.append(explicit)
    n = int(cfg.get("input_number") or 1)
    names.extend([f"input-{n}", f"input_{n}"])
    # Preserve order while removing duplicates.
    out = []
    seen = set()
    for name in names:
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(name)
    return out


def _read_body(url: str) -> str:
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
        return resp.read(2048).decode("utf-8", errors="replace").strip()


def _fetch_esp_state(cfg: dict) -> tuple[bool, str]:
    host = _host_only(str(cfg.get("host") or ""))
    port = int(cfg.get("port") or 80)
    last_err: Exception | None = None
    raw: str | None = None

    for sensor in _candidate_sensor_names(cfg):
        name = urllib.parse.quote(sensor, safe="-_.")
        # Try a few common web_server endpoint variants.
        urls = [
            f"http://{host}:{port}/binary_sensor/{name}",
            f"http://{host}:{port}/binary_sensor/{name}/state",
            f"http://{host}:{port}/sensor/{name}",
        ]
        for url in urls:
            try:
                raw = _read_body(url)
                break
            except Exception as exc:
                last_err = exc
        if raw is not None:
            break

    if raw is None:
        raise last_err or RuntimeError("No readable endpoint returned data")

    # ESPHome web_server commonly returns JSON, but tolerate plain text.
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            for key in ("value", "state", "status", "active", "on"):
                if key in data:
                    return _normalize_state(data[key]), raw
    except Exception:
        pass

    return _normalize_state(raw), raw


async def _apply_input_state(cfg: dict, is_active: bool, raw_data: str) -> None:
    alarm_state = is_active if int(cfg.get("active_high") or 0) else (not is_active)
    state_token = "1" if is_active else "0"
    prior_token = None if cfg.get("last_state") is None else str(cfg.get("last_state"))

    with db() as conn:
        conn.execute(
            "UPDATE input_configs SET last_state=?, last_seen=datetime('now','utc') WHERE id=?",
            (state_token, cfg["id"]),
        )

    if prior_token == state_token:
        return

    if alarm_state:
        if cfg.get("device_id"):
            call_id = await process_new_call(cfg["device_id"], raw_data, source="aux")
            if call_id is None:
                await process_named_call(
                    f"input:{cfg['id']}",
                    cfg["name"],
                    raw_data,
                    priority="normal",
                    location=f"{cfg.get('host')}:{cfg.get('input_number')}",
                )
        else:
            await process_named_call(
                f"input:{cfg['id']}",
                cfg["name"],
                raw_data,
                priority="normal",
                location=f"{cfg.get('host')}:{cfg.get('input_number')}",
            )
        return

    await auto_clear_call(_call_key(cfg))


async def _poll_once() -> None:
    with db() as conn:
        rows = conn.execute(
            """SELECT * FROM input_configs
               WHERE enabled=1 AND lower(coalesce(input_type, 'esp'))='esp'
               ORDER BY id"""
        ).fetchall()
    inputs = [dict(r) for r in rows]
    if not inputs:
        return

    for cfg in inputs:
        try:
            is_active, raw = await asyncio.to_thread(_fetch_esp_state, cfg)
            await _apply_input_state(cfg, is_active, raw_data=raw)
        except Exception as exc:
            log.warning("Input poll failed for id=%s name=%s host=%s:%s input_name=%s input_number=%s: %s",
                        cfg.get("id"), cfg.get("name"), cfg.get("host"), cfg.get("port"),
                        cfg.get("input_name"), cfg.get("input_number"), exc)


async def start_monitor() -> None:
    global _stop_event
    _stop_event = asyncio.Event()
    log.info("Input monitor started.")

    while not _stop_event.is_set():
        await _poll_once()
        try:
            await asyncio.wait_for(_stop_event.wait(), timeout=POLL_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            pass

    log.info("Input monitor stopped.")


async def stop_monitor() -> None:
    if _stop_event:
        _stop_event.set()
