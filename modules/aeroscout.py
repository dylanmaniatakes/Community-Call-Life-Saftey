"""
AeroScout Location Engine (ALE) TCP connector.

Protocol confirmed from packet captures (ALE.pcapng, ALE-Arial.pcapng,
ALE-Arial-long.pcapng — 13 690 packets, ~36 hr session):

  Wire format : [4-byte big-endian length][UTF-8 XML body]
                No XML declaration. No newline. No null padding.
  TLS         : NOT supported (server ignores TLS ClientHello, drops after 130 s)
  On connect  : send <Authorize> immediately — NO null bytes prefix.
  Keepalive   : server sends 6 raw null bytes in response to <HeartBeat>
                (NOT a framed message). Client echoes 6 null bytes back.

Session flow (confirmed order):
  1. TCP connect
  2. Client → 6 null bytes
  3. Client → <Authorize>          (OpCode 0)
  4. Server → <AuthorizeResponse>  (StatusResponse/StatusCode == "0")
  5. Client → <RegisterForDevicesStatusNotification>  (OpCode 916)
  6. Server → <…Response>
  7. Client → <StartTags>          (OpCode 27) — triggers LocationReport stream
  8. Server → <StartTagsResponse>
  -- session live --
  Server pushes <LocationReport>          (OpCode 80) on tag movement
  Server pushes <DevicesStatusNotification> (OpCode 917) on device changes
  Client sends  <HeartBeat>   (OpCode 63) every ~30 min
  Client sends  <WgHeartBeat> (OpCode 62) every ~3 hr  (server responds with XML)

NOTE: <Subscribe> does NOT exist in this server version. Use <StartTags>.

WS events broadcast:
  aeroscout.status  — { status: str }
  aeroscout.raw     — { hex: str, text: str, length: int, ts: str, label: str }
  aeroscout.tag     — parsed LocationReport dict
  aeroscout.device  — parsed DevicesStatusNotification dict
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import struct
import xml.etree.ElementTree as ET

from database import db as _db
from modules.ws_manager import manager as ws_manager

log = logging.getLogger("aeroscout")

# ── Constants ────────────────────────────────────────────────────────────────

KEEPALIVE_BYTES = b"\x00" * 6   # sent raw, NOT framed with length prefix
CLIENT_VERSION  = "5.7.30"      # Arial's version — server 5.8.20.91 accepts it

AUTH_TIMEOUT      = 15   # s — wait for AuthorizeResponse
RECONNECT_DELAY   = 15   # s — between reconnect attempts
HB_INTERVAL       = 1800 # s — HeartBeat (OpCode 63) every 30 min
WG_HB_INTERVAL    = 9000 # s — WgHeartBeat (OpCode 62) every ~2.5 hr
READ_TIMEOUT      = 60   # s — recv timeout before sending keepalive

# ── Shared state ─────────────────────────────────────────────────────────────

_status: str = "disconnected"
_stop_evt: asyncio.Event | None = None
_connector_task: asyncio.Task | None = None
_conn_info: dict = {}
_msg_id: int = 0


def get_status() -> str:
    return _status

def get_conn_info() -> dict:
    return _conn_info


# ── Helpers ──────────────────────────────────────────────────────────────────

async def _set_status(s: str) -> None:
    global _status
    _status = s
    await ws_manager.broadcast("aeroscout.status", {"status": s})


def _load_config() -> dict | None:
    with _db() as conn:
        row = conn.execute("SELECT * FROM aeroscout_config WHERE id=1").fetchone()
    if not row:
        return None
    cfg = dict(row)
    if not cfg.get("enabled") or not cfg.get("host"):
        return None
    return cfg


def _next_id() -> int:
    global _msg_id
    _msg_id += 1
    return _msg_id


# ── Wire I/O ─────────────────────────────────────────────────────────────────

async def _send_raw(writer: asyncio.StreamWriter, data: bytes) -> None:
    writer.write(data)
    await writer.drain()


async def _send_msg(writer: asyncio.StreamWriter, xml_body: str) -> None:
    """Frame and send one ALE XML message (4-byte BE length prefix + UTF-8 body)."""
    payload = xml_body.encode("utf-8")
    frame   = struct.pack(">I", len(payload)) + payload
    log.info("ALE → [%d bytes] %s…", len(payload), xml_body[:100])
    await _send_raw(writer, frame)


async def _recv_msg(reader: asyncio.StreamReader, timeout: float) -> str | None:
    """
    Read one framed ALE message.
    Returns XML string, or None if the server's 6-null HeartBeat response was received.
    Raises asyncio.TimeoutError or asyncio.IncompleteReadError on close.

    Wire format: [4-byte BE uint32 length][UTF-8 body]

    IMPORTANT: the server sends 6 raw null bytes (not a framed message) in
    response to <HeartBeat>. We read 1 byte at a time to distinguish the
    6-null run from a real 4-byte length header — both start with 0x00.
    """
    first = await asyncio.wait_for(reader.readexactly(1), timeout=timeout)
    if first == b"\x00":
        # Could be start of 6-null server keepalive OR a normal message with
        # a length < 16 MB (first byte of length header is always 0x00).
        rest = await asyncio.wait_for(reader.readexactly(5), timeout=5.0)
        if rest == b"\x00" * 5:
            return None  # 6-null server keepalive — consumed cleanly
        # Normal message: length header is first + rest[0:3], rest[3:] is body prefix
        raw_len = first + rest[0:3]
        length  = struct.unpack(">I", raw_len)[0]
        if length == 0:
            return None
        prefix   = rest[3:]   # 2 body bytes already read
        raw_body = prefix + await asyncio.wait_for(
            reader.readexactly(length - len(prefix)), timeout=30.0)
    else:
        # First byte is non-zero (message length >= 16 MB — effectively impossible,
        # but handle cleanly anyway)
        raw_len  = first + await asyncio.wait_for(reader.readexactly(3), timeout=5.0)
        length   = struct.unpack(">I", raw_len)[0]
        if length == 0:
            return None
        raw_body = await asyncio.wait_for(reader.readexactly(length), timeout=30.0)
    return raw_body.decode("utf-8", errors="replace")


# ── Broadcast ─────────────────────────────────────────────────────────────────

async def _broadcast_xml(xml_str: str, label: str = "") -> None:
    raw     = xml_str.encode("utf-8")
    ts      = datetime.datetime.utcnow().strftime("%H:%M:%S.%f")[:-3]
    hex_str = raw.hex(" ")
    log.info("ALE ← %s[%d bytes] %s…",
             f"{label} " if label else "", len(raw), xml_str[:120])
    await ws_manager.broadcast("aeroscout.raw", {
        "hex":    hex_str,
        "text":   f"[{label}] {xml_str}" if label else xml_str,
        "length": len(raw),
        "ts":     ts,
        "label":  label,
    })


# ── Session commands ──────────────────────────────────────────────────────────

async def _authorize(reader: asyncio.StreamReader,
                     writer: asyncio.StreamWriter,
                     cfg: dict) -> bool:
    """Send <Authorize> and wait for <AuthorizeResponse>. Returns True on success."""
    user    = cfg.get("username") or "Admin"
    pwd     = cfg.get("password") or ""
    version = cfg.get("client_version") or CLIENT_VERSION
    mid     = _next_id()
    log.info("ALE: authorizing as user=%r clientVersion=%s", user, version)

    await _send_msg(writer,
        f"<Authorize>"
        f"<OpCode>0</OpCode>"
        f"<MsgID>{mid}</MsgID>"
        f"<UserData>"
        f"<UserName>{user}</UserName>"
        f"<Password>{pwd}</Password>"
        f"</UserData>"
        f"<ClientVersion>{version}</ClientVersion>"
        f"</Authorize>"
    )

    deadline = asyncio.get_event_loop().time() + AUTH_TIMEOUT
    while True:
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            log.warning("ALE: no AuthorizeResponse within %d s", AUTH_TIMEOUT)
            return False
        try:
            frame = await _recv_msg(reader, timeout=remaining)
        except asyncio.TimeoutError:
            log.warning("ALE: no AuthorizeResponse within %d s", AUTH_TIMEOUT)
            return False
        except asyncio.IncompleteReadError as e:
            log.warning(
                "ALE: server closed connection without AuthorizeResponse — "
                "check credentials or IP whitelist on ALE server. Detail: %s", e)
            return False
        except (ConnectionError, OSError) as e:
            log.warning("ALE: connection lost waiting for AuthorizeResponse: %s", e)
            return False

        if frame is None:
            # Zero-length keepalive — echo and keep waiting
            await _send_raw(writer, KEEPALIVE_BYTES)
            continue

        await _broadcast_xml(frame, "auth-response")

        # ── Parse AuthorizeResponse ────────────────────────────────────────
        # Confirmed format (from wire):
        #   <AuthorizeResponse>
        #     <StatusResponse>
        #       <StatusCode>0</StatusCode>   ← 0 = success
        #       <Description>OK</Description>
        #     </StatusResponse>
        #     <AuthorizationLevel>3</AuthorizationLevel>
        #   </AuthorizeResponse>
        # NOTE: there is NO <Result> field and NO <SessionID> field.
        try:
            root = ET.fromstring(frame)
            sr   = root.find("StatusResponse")
            if sr is None:
                log.warning("ALE: AuthorizeResponse has no StatusResponse: %s", frame[:200])
                return False
            code = sr.findtext("StatusCode")
            if code == "0":
                level = root.findtext("AuthorizationLevel") or "?"
                log.info("ALE: authenticated — AuthorizationLevel=%s", level)
                return True
            else:
                desc = sr.findtext("Description") or ""
                log.warning("ALE: auth rejected — StatusCode=%s (%s)", code, desc)
                return False
        except ET.ParseError as e:
            log.warning("ALE: could not parse AuthorizeResponse: %s | raw: %s", e, frame[:200])
            return False


async def _register_devices(writer: asyncio.StreamWriter) -> None:
    """Subscribe to device status push notifications (OpCode 916)."""
    mid = _next_id()
    await _send_msg(writer,
        f"<RegisterForDevicesStatusNotification>"
        f"<OpCode>916</OpCode><MsgID>{mid}</MsgID>"
        f"<DeviceModel>EX5500</DeviceModel>"
        f"<DeviceModel>EX5700</DeviceModel>"
        f"<DeviceModel>DC1000</DeviceModel>"
        f"<DeviceModel>GW3000</DeviceModel>"
        f"<DeviceModel>GW3100</DeviceModel>"
        f"</RegisterForDevicesStatusNotification>"
    )
    log.info("ALE: sent RegisterForDevicesStatusNotification")


async def _start_tags(writer: asyncio.StreamWriter) -> None:
    """Send StartTags (OpCode 27) — this triggers LocationReport push events."""
    mid = _next_id()
    await _send_msg(writer,
        f"<StartTags><OpCode>27</OpCode><MsgID>{mid}</MsgID></StartTags>"
    )
    log.info("ALE: sent StartTags — location events should begin")


async def _heartbeat(writer: asyncio.StreamWriter) -> None:
    """HeartBeat (OpCode 63) — server sends zero-length ACK only."""
    mid = _next_id()
    await _send_msg(writer,
        f"<HeartBeat><OpCode>63</OpCode><MsgID>{mid}</MsgID></HeartBeat>"
    )
    log.info("ALE: sent HeartBeat")


async def _wg_heartbeat(writer: asyncio.StreamWriter) -> None:
    """WgHeartBeat (OpCode 62) — server responds with WgHeartBeatResponse XML."""
    mid = _next_id()
    await _send_msg(writer,
        f"<WgHeartBeat><OpCode>62</OpCode><MsgID>{mid}</MsgID></WgHeartBeat>"
    )
    log.info("ALE: sent WgHeartBeat")


# ── Frame parsers ─────────────────────────────────────────────────────────────

# Device models we care about for WanderGuard Blue
_WANDER_MODELS = {"DC1000", "EX5500", "EX5700"}


def _parse_location_report(xml_str: str) -> dict | None:
    """
    Parse a <LocationReport> (OpCode 80).
    Field names confirmed from wire — differ from AeroScout documentation.
    """
    try:
        root = ET.fromstring(xml_str)
        if root.tag != "LocationReport":
            return None
        sr = root.find("StatusResponse")
        if sr is not None and sr.findtext("StatusCode") != "0":
            return None

        ts_ms = int(root.findtext("Location/Time") or 0)
        return {
            "mac":      root.findtext("UnitDescriptor/MacAdd"),        # e.g. "000CCC1A0F75"
            "type":     root.findtext("UnitDescriptor/Type"),           # "TAG"
            "category": root.findtext("UnitDescriptor/TagCategory"),    # "CATEGORY_ONE" etc.
            "duress":   root.findtext("UnitDescriptor/Duress") == "true",
            "protected":root.findtext("UnitDescriptor/Protected") == "true",
            "x":        float(root.findtext("Location/Coordinates/XCor") or 0),
            "y":        float(root.findtext("Location/Coordinates/YCor") or 0),
            "z":        float(root.findtext("Location/Coordinates/ZCor") or 0),
            "map_id":   root.findtext("Location/MapID"),                # e.g. "17_1_0"
            "zone_id":  root.findtext("Location/ZoneID"),               # numeric zone ID
            "ts_ms":    ts_ms,                                          # Unix milliseconds
            "battery":  root.findtext("UnitData/BatteryStatus"),        # "0" = OK
            "quality":  float(root.findtext("LocationQuality") or 0),  # 0.0–1.0
            "ap_id":    root.findtext("LocationSources/APIDs/ID"),      # e.g. "3_4"
        }
    except (ET.ParseError, ValueError, TypeError):
        return None


def _parse_device_status(xml_str: str) -> dict | None:
    """Parse a <DevicesStatusNotification> (OpCode 917)."""
    try:
        root = ET.fromstring(xml_str)
        if root.tag != "DevicesStatusNotification":
            return None
        dcd = root.find("DeviceCommStatusData")
        if dcd is None:
            return None

        # Collect all notifications from the NotificationList
        alerts = []
        for notif in root.findall("DeviceCommStatusData/NotificationList/Notification"):
            alerts.append({
                "type": notif.findtext("Type"),
                "code": notif.findtext("Code"),
                "desc": notif.findtext("Description"),
            })

        sec = root.find("DeviceCommStatusData/DeviceSecurityStatus")

        return {
            "device_id":        dcd.findtext("ID"),
            "general_status":   dcd.findtext("GeneralStatus"),    # OK / Unreachable
            "comm_status":      dcd.findtext("CommStatus"),
            "name":             root.findtext("Name"),
            "mac":              root.findtext("MacAdd"),
            "model":            root.findtext("DeviceModel"),
            "firmware":         root.findtext("FirmwareData"),
            "security_enabled": (sec.findtext("DeviceSecurityEnabled") == "true") if sec is not None else False,
            "alerts":           alerts,
        }
    except (ET.ParseError, TypeError):
        return None


# ── DB persistence ─────────────────────────────────────────────────────────────

async def _handle_device_status(parsed: dict) -> None:
    """
    Persist a DevicesStatusNotification to ale_devices.
    Creates a nurse-call alarm for non-OK alerts from WanderGuard Blue devices.
    """
    now = datetime.datetime.utcnow().isoformat(timespec="seconds")
    device_id      = parsed["device_id"]
    general_status = parsed.get("general_status") or "unknown"
    model          = parsed.get("model") or ""
    alerts         = parsed.get("alerts") or []

    # Upsert device record
    with _db() as conn:
        conn.execute("""
            INSERT INTO ale_devices
                (device_id, name, mac, model, firmware, general_status,
                 comm_status, security_enabled, last_seen,
                 last_alert_type, last_alert_desc)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(device_id) DO UPDATE SET
                name             = excluded.name,
                mac              = excluded.mac,
                model            = excluded.model,
                firmware         = excluded.firmware,
                general_status   = excluded.general_status,
                comm_status      = excluded.comm_status,
                security_enabled = excluded.security_enabled,
                last_seen        = excluded.last_seen,
                last_alert_type  = excluded.last_alert_type,
                last_alert_desc  = excluded.last_alert_desc
        """, (
            device_id,
            parsed.get("name"),
            parsed.get("mac"),
            model,
            parsed.get("firmware"),
            general_status,
            parsed.get("comm_status"),
            1 if parsed.get("security_enabled") else 0,
            now,
            alerts[0]["type"] if alerts else None,
            alerts[0]["desc"] if alerts else None,
        ))

    await ws_manager.broadcast("aeroscout.device", parsed)

    # Only create calls for WanderGuard Blue door controllers
    if model not in _WANDER_MODELS:
        return

    for alert in alerts:
        alert_type = alert.get("type") or ""
        alert_desc = alert.get("desc") or ""

        # Skip routine "device unreachable" if that's the only alert and
        # it has been ongoing (we'll let the device status badge reflect it
        # rather than spamming the call board).
        # TODO: create a call if the device goes unreachable for > N minutes.
        if alert_type == "DeviceAlert":
            log.info("ALE: device %s (%s) alert: %s", device_id, model, alert_desc)
            continue

        # Any non-DeviceAlert notification from a WanderGuard Blue device
        # (e.g. TagAlert, DoorAlert, ExciterAlarm) → create a nurse call
        priority = "high" if "alarm" in alert_type.lower() else "normal"
        device_name = parsed.get("name") or device_id

        with _db() as conn:
            # Avoid duplicate active calls for the same device+type
            existing = conn.execute("""
                SELECT id FROM calls
                WHERE device_id=? AND status IN ('active','acknowledged')
                  AND raw_data LIKE ?
                LIMIT 1
            """, (f"ale:{device_id}", f"%{alert_type}%")).fetchone()
            if existing:
                continue

            cur = conn.execute("""
                INSERT INTO calls
                    (device_id, device_name, location, priority, status, timestamp, raw_data)
                VALUES (?,?,?,?,?,?,?)
            """, (
                f"ale:{device_id}",
                device_name,
                parsed.get("mac"),
                priority,
                "active",
                now,
                f"{alert_type}: {alert_desc}",
            ))
            call_id = cur.lastrowid
            row = conn.execute("SELECT * FROM calls WHERE id=?", (call_id,)).fetchone()

        log.info("ALE: created call %d for %s %s — %s", call_id, model, device_id, alert_type)
        await ws_manager.broadcast("call.new", dict(row))


async def _handle_location_report(parsed: dict) -> None:
    """
    Persist tag location to ale_tags.
    Creates a duress alarm if the Duress flag is set.
    """
    mac = parsed.get("mac")
    if not mac:
        return

    now = datetime.datetime.utcnow().isoformat(timespec="seconds")

    with _db() as conn:
        conn.execute("""
            INSERT INTO ale_tags
                (mac, last_x, last_y, last_z, last_map_id, last_zone_id,
                 last_seen, battery_status, location_quality)
            VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT(mac) DO UPDATE SET
                last_x           = excluded.last_x,
                last_y           = excluded.last_y,
                last_z           = excluded.last_z,
                last_map_id      = excluded.last_map_id,
                last_zone_id     = excluded.last_zone_id,
                last_seen        = excluded.last_seen,
                battery_status   = excluded.battery_status,
                location_quality = excluded.location_quality
        """, (
            mac,
            parsed.get("x"), parsed.get("y"), parsed.get("z"),
            parsed.get("map_id"), parsed.get("zone_id"),
            now,
            parsed.get("battery"),
            parsed.get("quality"),
        ))
        tag_row = conn.execute("SELECT * FROM ale_tags WHERE mac=?", (mac,)).fetchone()

    tag_info = dict(tag_row)
    full_event = {**parsed, **tag_info}
    await ws_manager.broadcast("aeroscout.tag", full_event)

    # Duress alarm
    if parsed.get("duress"):
        resident = tag_info.get("resident_name") or f"Tag {mac}"
        log.warning("ALE: DURESS from %s (%s)", mac, resident)

        with _db() as conn:
            # Only one active duress call per tag at a time
            existing = conn.execute("""
                SELECT id FROM calls
                WHERE device_id=? AND status IN ('active','acknowledged')
                LIMIT 1
            """, (f"ale:tag:{mac}",)).fetchone()
            if not existing:
                cur = conn.execute("""
                    INSERT INTO calls
                        (device_id, device_name, location, priority, status, timestamp, raw_data)
                    VALUES (?,?,?,?,?,?,?)
                """, (
                    f"ale:tag:{mac}",
                    resident,
                    f"Map:{parsed.get('map_id')} Zone:{parsed.get('zone_id')}",
                    "high",
                    "active",
                    now,
                    f"Duress alarm — X:{parsed.get('x')} Y:{parsed.get('y')}",
                ))
                call_id = cur.lastrowid
                row = conn.execute("SELECT * FROM calls WHERE id=?", (call_id,)).fetchone()
                await ws_manager.broadcast("call.new", dict(row))


# ── Device commands ────────────────────────────────────────────────────────────
#
# WanderGuard Blue commands are sent via ALE to the door controllers.
# Security-enabled devices (DeviceSecurityEnabled=true) reject control
# commands with StatusCode 107 "Non secure client not allowed" unless the
# security key exchange is completed first.
#
# Commands implemented:
#   relay_on / relay_off  — direct relay control (OpCode 549)
#   night_mode_on/off     — toggle night-mode on an exciter (OpCode 549, relay 2)
#   override_on/off       — bypass door alarm (OpCode 549, relay 3)
#   restart               — restart the controller (OpCode 503)

_COMMAND_RELAY: dict[str, tuple[int, int]] = {
    # command_name: (relay_number, relay_state)
    "relay_on":       (1, 1),
    "relay_off":      (1, 0),
    "night_mode_on":  (2, 1),
    "night_mode_off": (2, 0),
    "override_on":    (3, 1),
    "override_off":   (3, 0),
}

# Shared writer reference so routes can call send_command without passing writer
_writer: asyncio.StreamWriter | None = None


async def send_command(device_id: str, command: str, **kwargs) -> dict:
    """
    Send a control command to a WanderGuard Blue door controller.

    Returns {"ok": bool, "error": str|None, "detail": str|None}
    """
    global _writer
    if _writer is None or _writer.is_closing():
        return {"ok": False, "error": "not_connected",
                "detail": "ALE is not connected — try again after reconnect"}

    mid = _next_id()

    if command == "restart":
        xml = (
            f"<ResetDevice>"
            f"<OpCode>503</OpCode><MsgID>{mid}</MsgID>"
            f"<DeviceID>{device_id}</DeviceID>"
            f"</ResetDevice>"
        )
    elif command in _COMMAND_RELAY:
        relay_num, relay_state = _COMMAND_RELAY[command]
        duration = int(kwargs.get("duration", 0))  # 0 = indefinite
        xml = (
            f"<RelayControl>"
            f"<OpCode>549</OpCode><MsgID>{mid}</MsgID>"
            f"<DeviceID>{device_id}</DeviceID>"
            f"<RelayNumber>{relay_num}</RelayNumber>"
            f"<RelayState>{relay_state}</RelayState>"
            f"<Duration>{duration}</Duration>"
            f"</RelayControl>"
        )
    elif command == "raw":
        # Escape hatch for testing unknown OpCodes
        xml = kwargs.get("xml", "")
        if not xml:
            return {"ok": False, "error": "missing_xml", "detail": "Provide xml= parameter"}
    else:
        return {"ok": False, "error": "unknown_command",
                "detail": f"Unknown command '{command}'. Valid: {list(_COMMAND_RELAY)} + restart + raw"}

    log.info("ALE: sending command %r to device %s", command, device_id)
    try:
        await _send_msg(_writer, xml)
        return {"ok": True, "error": None, "detail": f"Sent {command} to {device_id}"}
    except Exception as e:
        log.warning("ALE: command failed: %s", e)
        return {"ok": False, "error": "send_failed", "detail": str(e)}


# ── Main connection loop ──────────────────────────────────────────────────────

async def _connector_loop() -> None:
    global _conn_info, _writer

    while _stop_evt and not _stop_evt.is_set():

        cfg = _load_config()
        if not cfg:
            await _set_status("disabled")
            _conn_info = {}
            try:
                await asyncio.wait_for(_stop_evt.wait(), timeout=15)
            except asyncio.TimeoutError:
                pass
            continue

        host = cfg["host"]
        port = cfg.get("port", 1411)
        _conn_info = {"host": host, "port": port}

        await _set_status("connecting")
        log.info("ALE connecting to %s:%s", host, port)

        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=10)
        except (asyncio.TimeoutError, OSError) as e:
            log.warning("ALE connect failed: %s", e)
            await _set_status("error")
            await _sleep_or_stop(20)
            continue

        log.info("ALE TCP connected to %s:%s", host, port)

        try:
            # Step 1 — authenticate (no preamble bytes; connect then XML immediately)
            await _set_status("authenticating")
            if not await _authorize(reader, writer, cfg):
                await _set_status("error")
                await _sleep_or_stop(20)
                continue

            # Step 2 — register for device status notifications
            await _register_devices(writer)

            # Step 3 — start tag location stream
            await _set_status("subscribing")
            await _start_tags(writer)
            await _set_status("connected")

            # Expose writer so send_command() can reach it
            _writer = writer

            # Step 4 — read loop
            now = asyncio.get_event_loop().time
            last_hb    = now()
            last_wg_hb = now()

            while _stop_evt and not _stop_evt.is_set():
                # Send heartbeats on schedule
                t = now()
                if t - last_hb > HB_INTERVAL:
                    await _heartbeat(writer)
                    last_hb = t
                if t - last_wg_hb > WG_HB_INTERVAL:
                    await _wg_heartbeat(writer)
                    last_wg_hb = t

                try:
                    xml_str = await _recv_msg(reader, timeout=READ_TIMEOUT)
                except asyncio.TimeoutError:
                    # No data in READ_TIMEOUT seconds — send keepalive
                    await _send_raw(writer, KEEPALIVE_BYTES)
                    log.info("ALE: sent idle keepalive")
                    continue
                except asyncio.IncompleteReadError:
                    log.info("ALE: server closed connection (EOF)")
                    break
                except (ConnectionError, OSError) as e:
                    log.info("ALE: connection lost: %s", e)
                    break
                except Exception as e:
                    log.warning("ALE: read error: %s", e)
                    break

                if xml_str is None:
                    # Zero-length keepalive frame from server — echo back
                    await _send_raw(writer, KEEPALIVE_BYTES)
                    continue

                await _broadcast_xml(xml_str)

                # Dispatch known frame types
                try:
                    tag = ET.fromstring(xml_str).tag
                except ET.ParseError:
                    continue

                if tag == "LocationReport":
                    loc = _parse_location_report(xml_str)
                    if loc:
                        log.info("ALE: tag %s at X=%.1f Y=%.1f zone=%s map=%s",
                                 loc["mac"], loc["x"], loc["y"],
                                 loc["zone_id"], loc["map_id"])
                        await _handle_location_report(loc)

                elif tag == "DevicesStatusNotification":
                    dev = _parse_device_status(xml_str)
                    if dev:
                        log.info("ALE: device %s (%s) status=%s",
                                 dev["device_id"], dev.get("name"), dev["general_status"])
                        await _handle_device_status(dev)

                elif tag in ("HeartBeatResponse", "WgHeartBeatResponse",
                             "StartTagsResponse",
                             "RegisterForDevicesStatusNotificationResponse",
                             "RelayControlResponse", "ResetDeviceResponse"):
                    # Log command responses at info level so we can see OpCode results
                    try:
                        root = ET.fromstring(xml_str)
                        sr   = root.find("StatusResponse")
                        code = sr.findtext("StatusCode") if sr is not None else "?"
                        desc = sr.findtext("Description") if sr is not None else ""
                        log.info("ALE: <%s> StatusCode=%s %s", tag, code, desc)
                    except Exception:
                        pass

                else:
                    log.debug("ALE: unhandled frame <%s>", tag)

        except Exception as e:
            log.warning("ALE session error: %s", e)
        finally:
            _writer = None
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

        if _stop_evt and not _stop_evt.is_set():
            await _set_status("disconnected")
            log.info("ALE disconnected — reconnecting in %d s", RECONNECT_DELAY)
            await _sleep_or_stop(RECONNECT_DELAY)


async def _sleep_or_stop(secs: float) -> None:
    if _stop_evt:
        try:
            await asyncio.wait_for(_stop_evt.wait(), timeout=secs)
        except asyncio.TimeoutError:
            pass


# ── Public API ────────────────────────────────────────────────────────────────

async def start_listener() -> None:
    global _stop_evt, _connector_task
    _stop_evt = asyncio.Event()
    _connector_task = asyncio.current_task()
    try:
        await _connector_loop()
    except asyncio.CancelledError:
        pass
    finally:
        await _set_status("disconnected")


async def stop_listener() -> None:
    global _connector_task
    if _stop_evt:
        _stop_evt.set()
    if _connector_task and not _connector_task.done():
        _connector_task.cancel()
        try:
            await _connector_task
        except (asyncio.CancelledError, Exception):
            pass
    await _set_status("disconnected")


async def reload() -> None:
    """Re-read config and reconnect. Cancels any running connector first."""
    global _stop_evt, _connector_task

    # Signal the current loop to stop
    if _stop_evt:
        _stop_evt.set()

    # Cancel and await the current task so it fully exits before we start a new one
    if _connector_task and not _connector_task.done():
        _connector_task.cancel()
        try:
            await _connector_task
        except (asyncio.CancelledError, Exception):
            pass

    _stop_evt = asyncio.Event()
    _connector_task = asyncio.create_task(_connector_loop())
