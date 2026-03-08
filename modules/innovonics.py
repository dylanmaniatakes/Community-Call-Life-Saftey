"""
Innovonics Coordinator — Binary Frame Listener
===============================================
Connects to the EN-series IP gateway via TCP (typically a MOXA or
similar serial-to-Ethernet adapter).  The coordinator output is binary,
NOT ASCII.

Frame format (discovered from working sniffer / ino_nc.py):
  Gateway wraps each Innovonics packet in a length prefix:
    [len_byte] [payload of exactly len_byte bytes]
  OR emits CRLF-delimited lines when in transparent-mode.
  Telnet / RFC-2217 negotiation bytes (IAC) are stripped automatically.

Payload layout (length byte excluded):
  byte 0:    aggregate header / flags
  bytes 1-3: 24-bit serial number (device ID), big-endian
  byte 4:    MCB — Message Control Byte
               0x3E = security  (pendant, pull-cord, alert)
               0x18 = serial_pal (text / PAL data)
               0x3C = temperature
  tail bytes (indexed from end):
    [-4] STAT1  bit0=IN1_ALARM  bit1=IN2_ALARM/RESET  bit2=IN3  bit3=IN4
    [-3] STAT0  bit5=RESET_BTN  bit6=TAMPER  bit7=LOW_BATTERY
    [-2] signal level
    [-1] checksum (not used for logic)

A security frame triggers a nurse-call event when any of IN1-IN4 are
set in STAT1, or RESET_BUTTON is set in STAT0.

PAL frames are also monitored; the text payload is searched for the
token "ALT" (alert) which some firmware revisions use for call signals.

Learn mode:
  start_learn_mode() arms a one-shot capture of the next frame that has
  a valid serial number.  The resolved serial is broadcast as
  coordinator.device_seen and returned to the HTTP caller.
"""

from __future__ import annotations

import asyncio
import json
import logging
import struct
from datetime import datetime, timezone
from typing import Callable, Optional

log = logging.getLogger("innovonics")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MCB_SECURITY    = 0x3E
MCB_SERIAL_PAL  = 0x18
MCB_TEMPERATURE = 0x3C

# Security STAT1 alarm bits — byte at frame[-4], bit numbering from LSB
# Verified against ino_nc.py: STAT1[7] in binary string == bit 0 == IN1_ALARM
STAT1_BITS = {
    0: "IN1_ALARM",
    1: "IN2_ALARM",
    2: "IN3_ALARM",
    3: "IN4_ALARM",
}
# STAT0 bits — byte at frame[-3], verified against ino_nc.py:
#   STAT0[4] in binary string == bit 3 == RESET_BUTTON
#   STAT0[2] in binary string == bit 5 == TAMPER
#   STAT0[1] in binary string == bit 6 == LOW_BATTERY
STAT0_BITS = {
    3: "RESET_BUTTON",
    5: "TAMPER",
    6: "LOW_BATTERY",
}

# Events that constitute a call activation
# RESET_BUTTON is a CLEAR signal — it goes through auto_clear_call, not process_new_call
CALL_EVENTS = {"IN1_ALARM", "IN2_ALARM", "IN3_ALARM", "IN4_ALARM"}

# ---------------------------------------------------------------------------
# Module state
# ---------------------------------------------------------------------------

_stop_event:   Optional[asyncio.Event] = None
_learn_mode:   bool = False
_learn_future: Optional[asyncio.Future] = None
_status:       str  = "disabled"
_nc_writer:    Optional[asyncio.StreamWriter] = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Public control API (called from main.py lifespan)
# ---------------------------------------------------------------------------

async def start_listener() -> None:
    from database import db

    with db() as conn:
        cfg = conn.execute("SELECT * FROM innovonics_config WHERE id=1").fetchone()

    if not cfg or not cfg["enabled"]:
        log.info("Innovonics listener disabled.")
        await _set_status("disabled")
        return

    cfg = dict(cfg)
    global _stop_event
    _stop_event = asyncio.Event()

    # Only TCP is supported for the binary protocol (serial would need RFC-2217)
    await _tcp_listener(cfg)


async def stop_listener() -> None:
    if _stop_event:
        _stop_event.set()


def start_learn_mode(loop: asyncio.AbstractEventLoop) -> asyncio.Future:
    global _learn_mode, _learn_future
    _learn_future = loop.create_future()
    _learn_mode   = True
    log.info("Learn mode armed — waiting for next frame with a valid serial number…")
    return _learn_future


def stop_learn_mode() -> None:
    global _learn_mode, _learn_future
    _learn_mode = False
    if _learn_future and not _learn_future.done():
        _learn_future.cancel()
    _learn_future = None


def get_status() -> str:
    return _status


async def force_repeater_nid(serial_number: str) -> dict:
    """
    Send a set_repeater_nid command to a registered repeater.

    Command format (from JNL ino_nc.py):
      [0x20, 0x07, 0x00, 0x01] + 3-byte big-endian serial + checksum

    The coordinator receives this and pushes the current NID to that repeater.
    The repeater confirms with MCB 0x21.

    Returns {"ok": bool, "detail": str}
    """
    if _nc_writer is None or _nc_writer.is_closing():
        return {"ok": False, "detail": "Coordinator not connected"}
    try:
        sn_int = int(serial_number)
    except ValueError:
        return {"ok": False, "detail": f"Invalid serial number: {serial_number!r}"}

    sn_bytes = sn_int.to_bytes(3, "big")
    cmd = b'\x20\x07\x00\x01' + sn_bytes
    try:
        _nc_writer.write(_make_command(cmd))
        await _nc_writer.drain()
        log.info("force_repeater_nid: sent NID command to repeater %s", serial_number)
        return {"ok": True, "detail": f"NID command sent to repeater {serial_number}"}
    except Exception as e:
        log.warning("force_repeater_nid failed: %s", e)
        return {"ok": False, "detail": str(e)}


# ---------------------------------------------------------------------------
# Status helpers
# ---------------------------------------------------------------------------

async def _set_status(s: str) -> None:
    global _status
    _status = s
    from modules.ws_manager import manager as ws
    await ws.broadcast("coordinator.status", {"status": s})
    log.info("Coordinator status: %s", s)


# ---------------------------------------------------------------------------
# Telnet / RFC-2217 IAC stripping
# Identical to the working sniffer's io_gateway._strip_telnet_iac
# ---------------------------------------------------------------------------

def _strip_iac(data: bytes) -> bytes:
    out = bytearray()
    i, n = 0, len(data)
    while i < n:
        b = data[i]
        if b == 0xFF:                      # IAC
            if i + 1 >= n:
                i += 1; continue
            cmd = data[i + 1]
            if cmd in (251, 252, 253, 254):  # WILL/WONT/DO/DONT — 3 bytes
                i += 3; continue
            elif cmd == 250:               # SB … IAC SE
                i += 2
                while i < n:
                    if data[i] == 0xFF and i + 1 < n and data[i + 1] == 240:
                        i += 2; break
                    i += 1
                continue
            elif cmd == 255:               # escaped 0xFF
                out.append(0xFF); i += 2; continue
            else:
                i += 2; continue
        else:
            out.append(b); i += 1
    return bytes(out)


def _make_command(cmd: bytes) -> bytes:
    """Append Innovonics single-byte checksum (sum of all bytes mod 256)."""
    return cmd + bytes([sum(cmd) % 256])


# ---------------------------------------------------------------------------
# TCP listener — reads from the gateway, strips IAC, segments frames
# ---------------------------------------------------------------------------

# Innovonics wire format (transparent serial-to-TCP pass-through):
#   [start_byte: 1B] [length: 1B] [payload: length-1 bytes]
# The length byte counts itself, so payload = length - 1 bytes.
_NC_START_BYTES = frozenset((0x72, 0x35, 0x1C, 0x06, 0x15))


async def _tcp_listener(cfg: dict) -> None:
    host, port = cfg["host"], cfg["port"]
    configured_nid = int(cfg.get("nid") or 16)
    reconnect = 3

    while not (_stop_event and _stop_event.is_set()):
        await _set_status("connecting")
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=10
            )
            await _set_status("connected")
            log.info("Connected to coordinator at %s:%s", host, port)

            global _nc_writer
            _nc_writer = writer

            # Query NID from coordinator immediately on connect
            writer.write(_make_command(b'\x34\x03\x82'))
            await writer.drain()

            buf = bytearray()
            try:
                while not (_stop_event and _stop_event.is_set()):
                    try:
                        chunk = await asyncio.wait_for(reader.read(4096), timeout=0.12)
                    except asyncio.TimeoutError:
                        chunk = b""

                    if chunk:
                        buf.extend(_strip_iac(chunk))

                    if reader.at_eof() and not buf:
                        log.warning("Coordinator closed the connection.")
                        break

                    # Parse frames: [start_byte][length][payload = length-1 bytes]
                    while len(buf) >= 2:
                        start = buf[0]
                        if start not in _NC_START_BYTES:
                            del buf[0]
                            continue
                        frame_len = buf[1]
                        payload_len = frame_len - 1  # length byte counts itself
                        if payload_len < 0:
                            del buf[:2]
                            continue
                        total = 2 + payload_len
                        if len(buf) < total:
                            break  # wait for more data
                        payload = bytes(buf[2:total])
                        del buf[:total]
                        await _dispatch_nc_msg(start, payload, configured_nid, writer)

            finally:
                _nc_writer = None
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    pass

        except asyncio.TimeoutError:
            log.warning("Connection to coordinator timed out — retry in %ds", reconnect)
        except Exception as exc:
            log.error("Coordinator error: %s — retry in %ds", exc, reconnect)

        await _set_status("disconnected")
        await asyncio.sleep(reconnect)


# ---------------------------------------------------------------------------
# NC message dispatcher and handlers
# ---------------------------------------------------------------------------

async def _dispatch_nc_msg(start: int, payload: bytes, configured_nid: int,
                           writer=None) -> None:
    if start == 0x1C:
        log.debug("NC heartbeat received")
    elif start == 0x06:
        log.debug("NC CTS")
    elif start == 0x35:
        await _handle_nc_status(payload, configured_nid, writer)
    elif start == 0x72:
        await _handle_device_msg(payload, configured_nid, writer)


async def _handle_nc_status(payload: bytes, configured_nid: int, writer=None) -> None:
    """Parse 0x35 NC status responses — NID reply, check-in time, etc."""
    if not payload:
        return
    response_type = payload[0]
    if response_type == 0x82 and len(payload) >= 2:
        nc_nid = payload[1]
        match = (nc_nid == configured_nid)
        log.info(
            "Coordinator NID: %d | Configured NID: %d | %s",
            nc_nid, configured_nid, "MATCH" if match else "MISMATCH",
        )
        from modules.ws_manager import manager as ws
        await ws.broadcast("coordinator.nid", {
            "nc_nid":         nc_nid,
            "configured_nid": configured_nid,
            "match":          match,
        })
        # Force-set the coordinator NID if it doesn't match
        if not match and writer:
            log.warning(
                "NID mismatch — forcing coordinator NID to %d", configured_nid
            )
            set_cmd = bytes([0x34, 0x04, 0x02, configured_nid & 0xFF])
            writer.write(_make_command(set_cmd))
            await writer.drain()
            await asyncio.sleep(0.15)
            # Re-query to confirm
            writer.write(_make_command(b'\x34\x03\x82'))
            await writer.drain()


async def _handle_repeater_checkin(serial_number: str) -> None:
    """Update repeater last_seen/status in DB and broadcast WS events."""
    now = _now_iso()
    from database import db
    with db() as conn:
        cur = conn.execute(
            "UPDATE repeaters SET last_seen=?, status='online' WHERE serial_number=?",
            (now, serial_number),
        )
        known = cur.rowcount > 0

    from modules.ws_manager import manager as ws

    # Always broadcast so the repeater auto-detect UI can capture any repeater
    await ws.broadcast("coordinator.repeater_seen", {
        "serial_number": serial_number,
        "registered":    known,
    })

    if known:
        log.debug("Repeater %s checked in", serial_number)
        await ws.broadcast("coordinator.repeater_checkin", {
            "serial_number": serial_number,
            "last_seen":     now,
            "status":        "online",
        })
    else:
        log.info("Unknown repeater checked in (not registered): %s", serial_number)


async def _handle_aggregate(data: bytes, num_messages: int) -> None:
    """Parse aggregated sub-messages and dispatch each as a device frame."""
    pos = 0
    for _ in range(num_messages):
        if pos >= len(data):
            break
        sub_len = data[pos]
        end = pos + sub_len
        if end > len(data):
            break
        # Sub-message: strip the leading length byte; MCB is at index 4
        sub = data[pos + 1:end]
        if len(sub) >= 5:
            await _handle_frame(sub)
        pos = end


async def _handle_device_msg(payload: bytes, configured_nid: int = 16,
                              writer=None) -> None:
    """Route 0x72 device messages by msg_type and MCB."""
    if len(payload) < 4:
        return
    msg_type = payload[0]

    if msg_type == 0x01:
        # Routed message via repeater: originator SN[1:4], first_hop[5:8], MCB[10]
        if len(payload) < 11:
            return
        mcb = payload[10]
        originator_sn = str(int.from_bytes(payload[1:4], "big"))
        if mcb == 0x41:
            # Repeater status check-in
            await _handle_repeater_checkin(originator_sn)
        elif mcb == 0x00:
            # Repeater reset / enrollment — send NID assignment
            log.info("Repeater enrollment (SN=%s) — sending set_repeater_nid", originator_sn)
            from modules.ws_manager import manager as ws
            await ws.broadcast("coordinator.repeater_seen", {
                "serial_number": originator_sn,
                "registered":    False,   # unknown at this point; UI will check
            })
            if writer:
                sn_bytes = payload[1:4]          # 3-byte big-endian serial
                cmd = b'\x20\x07\x00\x01' + sn_bytes
                writer.write(_make_command(cmd))
                await writer.drain()
        elif mcb == 0x21:
            # Repeater confirmed NID configuration
            log.info("Repeater NID config confirmed (SN=%s)", originator_sn)
            from database import db
            with db() as conn:
                conn.execute(
                    "UPDATE repeaters SET status='online' WHERE serial_number=?",
                    (originator_sn,),
                )
        elif mcb == 0x02:
            # Aggregate message: num_messages[11], sub-messages at [12:]
            if len(payload) < 12:
                return
            await _handle_aggregate(payload[12:], payload[11])
        elif mcb in (MCB_SECURITY, MCB_SERIAL_PAL, MCB_TEMPERATURE):
            # Strip outer wrapper; leaves serial at [1:4], MCB at [4]
            frame = payload[:4] + payload[10:-1]
            await _handle_frame(frame)

    elif msg_type == 0xB2:
        # Device detected directly by NC (no repeater hop)
        if len(payload) < 11:
            return
        frame = payload[:4] + payload[10:-1]
        await _handle_frame(frame)

    elif msg_type == 0xC0:
        # Analog / temperature detected directly by NC
        if len(payload) < 11:
            return
        if payload[10] == MCB_TEMPERATURE:
            frame = payload[:4] + payload[10:-1]
            await _handle_frame(frame)


# ---------------------------------------------------------------------------
# Frame decoder (mirrors parser.py from the working sniffer)
# ---------------------------------------------------------------------------

def _b2hex(b: bytes) -> str:
    return b.hex(" ").upper()


def _to_bits(val: int) -> str:
    return format(val & 0xFF, "08b")


def _parse_serial(frame: bytes) -> int:
    """24-bit serial number from bytes 1-3 (big-endian)."""
    if len(frame) >= 4:
        return int.from_bytes(frame[1:4], "big")
    return -1


def _classify(frame: bytes) -> str:
    if len(frame) >= 5:
        mcb = frame[4]
        if mcb == MCB_SECURITY:    return "security"
        if mcb == MCB_SERIAL_PAL:  return "serial_pal"
        if mcb == MCB_TEMPERATURE: return "temperature"
    return "unknown"


def _decode_security(frame: bytes) -> dict:
    serial = _parse_serial(frame)
    stat1  = frame[-4] if len(frame) >= 4 else 0
    stat0  = frame[-3] if len(frame) >= 3 else 0
    level  = frame[-2] if len(frame) >= 2 else 0

    events = []
    for bit, name in STAT1_BITS.items():
        if stat1 & (1 << bit):
            events.append(name)
    for bit, name in STAT0_BITS.items():
        if stat0 & (1 << bit):
            events.append(name)

    return {
        "class":  "security",
        "serial": serial,
        "STAT1":  _to_bits(stat1),
        "STAT0":  _to_bits(stat0),
        "level":  level,
        "events": events,
    }


def _decode_pal(frame: bytes) -> dict:
    serial  = _parse_serial(frame)
    payload = frame[5:] if len(frame) > 5 else b""
    try:
        text = payload.decode(errors="ignore")
    except Exception:
        text = ""
    tokens = {}
    for key in ("ALT", "ASS", "ACK", "CHK"):
        if key in text:
            tokens[key] = True

    # Binary 900MHz pull cord format — no ASCII tokens.
    # Identified by frame[26] == 0x02 (sub-type marker) and frame length >= 29.
    # Alarm/clear state is at frame[27]: 0x01 = alarm, 0x00 = clear.
    # frame[28] is the complement (0x00 alarm / 0x01 clear).
    # Reverse-engineered from observed frames; frame[11] LSB agrees (0xAB=alarm, 0xAA=clear).
    if not tokens and len(frame) >= 29 and frame[5:8] != b'AIO' and frame[26] == 0x02:
        log.debug(
            "Arial-900 binary path sn=%d len=%d f[26]=0x%02X f[27]=0x%02X f[11]=0x%02X",
            serial, len(frame), frame[26], frame[27], frame[11],
        )
        if frame[27] == 0x01:
            tokens["ALT"] = True
        elif frame[27] == 0x02:
            tokens["AUX"] = True
        elif frame[27] == 0x00:
            tokens["ACK"] = True

    return {
        "class":       "serial_pal",
        "serial":      serial,
        "text":        text.strip(),
        "payload_hex": payload.hex().upper(),
        "tokens":      tokens,
    }


def _decode_temperature(frame: bytes) -> dict:
    serial = _parse_serial(frame)
    val1 = val2 = None
    if len(frame) >= 10:
        block = frame[9:5:-1]
        if len(block) == 4:
            try:
                val1 = round(struct.unpack("<f", block)[0], 2)
            except Exception:
                pass
    if len(frame) >= 14:
        block2 = frame[13:9:-1]
        if len(block2) == 4:
            try:
                val2 = round(struct.unpack("<f", block2)[0], 2)
            except Exception:
                pass
    return {"class": "temperature", "serial": serial, "primary": val1, "secondary": val2}


def _decode_unknown(frame: bytes) -> dict:
    """Heuristic scan for any frame that didn't match normal MCB positions."""
    raw_hex = _b2hex(frame)
    for i, b in enumerate(frame):
        if b == MCB_SECURITY and i >= 3:
            sn    = int.from_bytes(frame[i - 3:i], "big")
            stat1 = frame[-4] if len(frame) >= 4 else 0
            stat0 = frame[-3] if len(frame) >= 3 else 0
            events = []
            for bit, name in STAT1_BITS.items():
                if stat1 & (1 << bit): events.append(name)
            for bit, name in STAT0_BITS.items():
                if stat0 & (1 << bit): events.append(name)
            return {
                "class": "security", "serial": sn,
                "STAT1": _to_bits(stat1), "STAT0": _to_bits(stat0),
                "events": events, "raw_hex": raw_hex, "heuristic": True,
            }
    return {"class": "unknown", "serial": -1, "raw_hex": raw_hex}


def decode_frame(frame: bytes) -> dict:
    kind = _classify(frame)
    try:
        if kind == "security":    decoded = _decode_security(frame)
        elif kind == "serial_pal":decoded = _decode_pal(frame)
        elif kind == "temperature":decoded = _decode_temperature(frame)
        else:                     decoded = _decode_unknown(frame)
    except Exception as exc:
        decoded = {"class": "error", "serial": -1, "error": str(exc)}
    decoded["raw_hex"] = _b2hex(frame)
    return decoded


# ---------------------------------------------------------------------------
# Handle one decoded frame — broadcast to monitor + trigger calls
# ---------------------------------------------------------------------------

async def _handle_frame(frame: bytes) -> None:
    if not frame:
        return

    log.debug("FRAME [%d bytes]: %s", len(frame), frame.hex())

    decoded  = decode_frame(frame)
    frame_class = decoded.get("class", "unknown")
    serial   = decoded.get("serial", -1)
    events   = decoded.get("events", [])
    dev_id   = str(serial) if serial >= 0 else ""

    # RESET_BUTTON in events = device was reset → clear, not alarm
    reset_pressed = "RESET_BUTTON" in events

    # Is this a call-generating event?
    is_call  = bool(events and any(e in CALL_EVENTS for e in events))

    # PAL "ALT" token is also a call; PAL "ACK" token is a clear
    pal_tokens = decoded.get("tokens", {}) if frame_class == "serial_pal" else {}
    pal_aux = bool(pal_tokens.get("AUX"))
    if pal_tokens.get("ALT") or pal_aux:
        is_call = True
    pal_ack = bool(pal_tokens.get("ACK"))

    # Broadcast to monitor (always)
    from modules.ws_manager import manager as ws
    await ws.broadcast("coordinator.raw", {
        "ts":        _now_iso(),
        "raw":       decoded.get("raw_hex", ""),
        "hex":       decoded.get("raw_hex", ""),
        "parsed":    {
            "device_id": dev_id,
            "event":     ", ".join(events) if events else frame_class,
            "rssi":      str(decoded.get("level", "")),
            "battery":   "LOW" if "LOW_BATTERY" in events else ("OK" if dev_id else ""),
            "format":    f"innovonics/{frame_class}",
            **{k: v for k, v in decoded.items()
               if k not in ("raw_hex", "class")},
        },
        "is_call":   is_call,
    })

    if not dev_id:
        return

    # Learn mode — capture first serial regardless of call status
    global _learn_mode, _learn_future
    if _learn_mode and _learn_future and not _learn_future.done():
        _learn_future.set_result(dev_id)
        _learn_mode = False
        await ws.broadcast("coordinator.device_seen", {
            "device_id": dev_id,
            "raw":       decoded.get("raw_hex", ""),
        })
        log.info("Learn mode captured device_id=%s", dev_id)
        return  # don't generate a call from a learn capture

    # Reset signals: RESET_BUTTON bit, PAL ACK token, or TAMPER → auto-clear.
    # NOTE: We do NOT clear on "no alarm bits" — Innovonics devices send periodic
    # supervision/check-in frames with no alarm bits while an alarm is still active.
    # Only an explicit RESET_BUTTON, ACK, or TAMPER constitutes a clear signal.
    #
    # Arial Legacy pull cords use TAMPER as the "cord returned to holder" signal.
    # They send TAMPER+alarm-bits together on the FIRST reset hit, so we must treat
    # TAMPER as a clear for arial_legacy regardless of simultaneous alarm bits.
    # For standard Innovonics devices TAMPER+alarm-bits is NOT a clear (alarm stays).
    # Look up vendor_type + device_type for all security frames.
    # vendor_type drives protocol-variant behaviour (e.g. arial_legacy tamper-as-clear).
    # device_type drives physical-device behaviour (e.g. universal_tx zero-event-as-clear).
    vendor_type = None
    device_type = None
    if dev_id and frame_class == "security":
        from database import db as _db
        with _db() as conn:
            _vt_row = conn.execute(
                "SELECT vendor_type, device_type FROM devices WHERE device_id=?", (dev_id,)
            ).fetchone()
        if _vt_row:
            vendor_type = _vt_row["vendor_type"]
            device_type = _vt_row["device_type"]

    # Arial Legacy pull cords (vendor_type) send TAMPER+alarm-bits together as their
    # clear signal.  Universal TX / Reed Switch (device_type) also clear via tamper
    # when the housing is opened while in alarm.
    tamper_clear = "TAMPER" in events and (
        not any(e in CALL_EVENTS for e in events)
        or vendor_type == "arial_legacy"
        or device_type in {"universal_tx", "universal_tx_reed"}
    )

    # Universal TX and Reed Switch (device_type) go idle (STAT1=0x00, STAT0=0x00)
    # when the input returns to normal — that zero-event frame is their explicit clear.
    # Arial Legacy pull cords (vendor_type) do the same when the cord is returned.
    # Standard Innovonics pendants/pull-cords also send zero-event supervision frames
    # while an alarm is still active, so we must not clear those generically.
    arial_legacy_clear = (
        frame_class == "security"
        and not events
        and (
            vendor_type == "arial_legacy"
            or device_type in {"universal_tx", "universal_tx_reed"}
        )
    )

    if reset_pressed or pal_ack or tamper_clear or arial_legacy_clear:
        signal = ("PAL ACK" if pal_ack
                  else "TAMPER/cord-reset" if tamper_clear
                  else "arial_legacy idle (zero-event)" if arial_legacy_clear
                  else "RESET_BUTTON")
        log.info("Clear signal (%s) for device %s — calling auto_clear_call", signal, dev_id)
        from modules.call_manager import auto_clear_call
        cleared = await auto_clear_call(dev_id)
        if cleared:
            log.info("Auto-cleared call for device %s (%s)", dev_id, signal)
        else:
            log.info("Clear signal (%s) for device %s — no active call found to clear", signal, dev_id)
        return

    if is_call:
        source = "aux" if pal_aux else None
        log.info("Call from device %s | events=%s | source=%s", dev_id, events, source or "main")
        from modules.call_manager import process_new_call
        await process_new_call(dev_id, json.dumps(decoded), source=source)
