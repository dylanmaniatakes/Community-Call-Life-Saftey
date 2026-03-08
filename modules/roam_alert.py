"""
Roam Alert Wander System — asyncio TCP listener
================================================
Connects to Roam Alert network controllers (Lantronix/MOXA RS-485 bridges)
and polls attached door controllers for wander-tag alarms, door open/close
events, and bypass state.

Protocol (binary, RS-485 over TCP):
  Commands are hex strings converted to bytes.
  Frame: [0x01][length][command_byte][data...][checksum]

Counter cycles through high-nibble values: 0x40 → 0x80 → 0xC0 → 0x00 → repeat.

Event codes (matching the reference engine):
  DAL  — Door alarm: known tag in alarm
  DBR  — Door break: unknown tag / alarm input
  DOP  — Door opened
  DOC  — Door closed
  DBY  — Door bypassed (alarm cleared by staff)
  DAR  — Tag alarm resolved (during bypass)
  DRB  — Door reset from bypass
  BCP  — Bypass cleared

Wander alarms are also injected into the calls table so they appear on the
main dashboard alongside nurse-call alarms.

Keypad code programming:
  Based on protocol pattern analysis.  The command uses byte 0x4X where X
  is the controller key.  Slot/code bytes follow.  Exact byte layout should
  be verified against live hardware — this is a best-effort implementation.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

from database import db
from modules.ws_manager import manager as ws_manager

log = logging.getLogger("roam_alert")

# ── Globals ────────────────────────────────────────────────────
_network_tasks: dict[int, asyncio.Task] = {}   # network_id → Task
_stop_event: Optional[asyncio.Event] = None


# ── Protocol helpers ───────────────────────────────────────────

class _Counter:
    """Rolling high-nibble counter: 4 → 8 → c → 0 → repeat (matching Tools.increment)."""
    _seq = ('4', '8', 'c', '0')

    def __init__(self):
        self._idx = 0

    def next(self) -> str:
        v = self._seq[self._idx]
        self._idx = (self._idx + 1) % 4
        return v


def _checksum(hex_str: str) -> str:
    """Two's-complement (Intel HEX) checksum of a hex string."""
    total = sum(int(hex_str[i * 2:i * 2 + 2], 16) for i in range(len(hex_str) // 2))
    return f'{((total ^ 0xFF) + 1) & 0xFF:02x}'


def _build(hex_str: str) -> bytes:
    """Append checksum and return bytes."""
    return bytes.fromhex(hex_str + _checksum(hex_str))


def _cmd_set_bus_addr(ctr: _Counter, serial: str) -> bytes:
    return _build(f'0106{ctr.next()}002{serial.zfill(6)}')


def _cmd_init(ctr: _Counter, serial: str, key: int) -> bytes:
    """Three-part init sequence for one door controller."""
    k = f'{key:02d}'
    s = serial.zfill(6)
    h1 = f'0106{ctr.next()}2{k}{s}'
    h2 = f'0106{ctr.next()}2{k}{s}'
    h3 = f'0106{ctr.next()}a02809800'
    return _build(h1) + _build(h2) + _build(h3)


def _cmd_poll(ctr: _Counter, key: int) -> bytes:
    return _build(f'0106{ctr.next()}a{key:02d}818000')


def _cmd_ack(ctr: _Counter, key: int) -> bytes:
    return _build(f'0103{ctr.next()}7{key:02d}')


def _cmd_global_poll() -> bytes:
    return bytes.fromhex('01034702b3')


def _cmd_set_code(ctr: _Counter, key: int, slot: int, code: str) -> bytes:
    """
    Program a keypad access code into a door controller.

    NOTE: The exact byte layout for keypad code programming is not in the
    public Roam Alert documentation.  This implementation follows the observed
    protocol pattern (command nibble 0x5X, slot byte, followed by BCD-encoded
    digits).  Verify against live hardware and adjust if needed.

    code  — up to 6-digit string ('0'-'9' only)
    slot  — code slot index (1–based, typically 1–100)
    """
    # Pad code to 6 digits, encode each digit as a nibble pair
    padded = code.zfill(6)[:6]
    code_hex = ''.join(f'{int(d):01x}' for d in padded)  # 6 nibbles = 3 bytes
    slot_hex = f'{slot:02x}'
    return _build(f'0106{ctr.next()}5{key:02d}{slot_hex}{code_hex}')


# ── DB helpers ─────────────────────────────────────────────────

def _load_tags() -> dict[str, dict]:
    """Return {tag_serial_lower: {name, apartment_id}} for all enabled tags."""
    with db() as conn:
        rows = conn.execute(
            "SELECT tag_serial, resident_name, apartment_id FROM ra_tags WHERE enabled=1"
        ).fetchall()
    return {r['tag_serial'].lower(): dict(r) for r in rows}


def _set_door_online(door_id: int, online: bool) -> None:
    ts = datetime.now(timezone.utc).isoformat()
    with db() as conn:
        conn.execute(
            "UPDATE ra_doors SET online=?, last_seen=? WHERE id=?",
            (1 if online else 0, ts if online else None, door_id),
        )


def _set_network_online(net_id: int, online: bool) -> None:
    ts = datetime.now(timezone.utc).isoformat()
    with db() as conn:
        conn.execute(
            "UPDATE ra_networks SET online=?, last_seen=? WHERE id=?",
            (1 if online else 0, ts if online else None, net_id),
        )


def _log_event(event_code: str, tag_serial: Optional[str], resident_name: Optional[str],
               door_id: Optional[int], door_name: Optional[str], details: Optional[str]) -> None:
    with db() as conn:
        conn.execute(
            """INSERT INTO ra_events (event_code, tag_serial, resident_name,
               door_id, door_name, details) VALUES (?,?,?,?,?,?)""",
            (event_code, tag_serial, resident_name, door_id, door_name, details),
        )
        # Trim to last 500 rows
        conn.execute(
            "DELETE FROM ra_events WHERE id NOT IN "
            "(SELECT id FROM ra_events ORDER BY id DESC LIMIT 500)"
        )


def _make_call(door_id: int, door_name: str, tag_serial: str, resident_name: str) -> int:
    """Create a wander-alarm call entry; return the new call id."""
    ts = datetime.now(timezone.utc).isoformat()
    device_id = f'ra-tag-{tag_serial}'
    with db() as conn:
        # Ensure a device placeholder exists
        conn.execute(
            """INSERT OR IGNORE INTO devices (device_id, name, location, device_type, priority, active)
               VALUES (?, ?, ?, 'wander', 'urgent', 1)""",
            (device_id, resident_name or f'Tag {tag_serial}', door_name),
        )
        cur = conn.execute(
            """INSERT INTO calls (device_id, device_name, location, status, priority,
               raw_data, timestamp)
               VALUES (?, ?, ?, 'active', 'urgent', ?, ?)""",
            (device_id,
             resident_name or f'Tag {tag_serial}',
             door_name,
             f'wander:{tag_serial}',
             ts),
        )
        return cur.lastrowid


def _clear_wander_call(tag_serial: str) -> Optional[dict]:
    """Clear any active wander call for this tag. Returns the call dict if found."""
    device_id = f'ra-tag-{tag_serial}'
    ts = datetime.now(timezone.utc).isoformat()
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM calls WHERE device_id=? AND status IN ('active','acknowledged')",
            (device_id,),
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE calls SET status='cleared', cleared_at=? WHERE id=?",
                (ts, row['id']),
            )
            return dict(row)
    return None


# ── Event broadcasting ─────────────────────────────────────────

async def _broadcast(event_code: str, door: dict, tag_serial: Optional[str] = None,
                     resident_name: Optional[str] = None, extra: Optional[dict] = None) -> None:
    payload = {
        'event_code': event_code,
        'door_id':    door['id'],
        'door_name':  door['name'],
        'tag_serial': tag_serial,
        'resident':   resident_name,
        'timestamp':  datetime.now(timezone.utc).isoformat(),
        **(extra or {}),
    }
    await ws_manager.broadcast('roam_alert.event', payload)
    _log_event(event_code, tag_serial, resident_name, door['id'], door['name'], None)

    # Tag alarms → call cards on the dashboard
    if event_code == 'DAL':
        call_id = await asyncio.to_thread(
            _make_call, door['id'], door['name'], tag_serial, resident_name
        )
        with db() as conn:
            call = dict(conn.execute("SELECT * FROM calls WHERE id=?", (call_id,)).fetchone())
        await ws_manager.broadcast('call.new', call)

    elif event_code in ('DAR', 'DRB'):
        cleared = await asyncio.to_thread(_clear_wander_call, tag_serial or '')
        if cleared:
            cleared['status'] = 'cleared'
            await ws_manager.broadcast('call.cleared', cleared)


# ── Message parsing ────────────────────────────────────────────

HEADER_LENGTH = 11


async def _process_response(data: bytes, key: int, door: dict, state: dict,
                             tags: dict[str, dict]) -> None:
    """Parse one raw response frame from a door controller."""
    if len(data) < 3:
        return
    msg_length = data[1]
    if msg_length <= 12 or len(data) < msg_length + 2:
        return

    payload = data[2:msg_length + 1]
    body_len = len(payload) - HEADER_LENGTH

    if body_len < 4 or body_len % 4 != 0:
        return

    num_messages = body_len // 4

    for i in range(num_messages):
        off = HEADER_LENGTH + i * 4
        b1, b2, b3, b4 = payload[off], payload[off + 1], payload[off + 2], payload[off + 3]
        msg_type = (b1 >> 4) & 0x0F   # high nibble

        if msg_type == 0x00:
            # Tag message
            tag_int = (b2 << 16) | (b3 << 8) | b4
            tag_sn = hex(tag_int)[2:]
            alarm = bool(b1 & 0x01)

            if alarm:
                tag_info = tags.get(tag_sn)
                if tag_info:
                    if tag_sn not in state['tags_in_alarm']:
                        state['tags_in_alarm'].append(tag_sn)
                        await _broadcast('DAL', door, tag_sn, tag_info.get('resident_name'))
                else:
                    # Unknown tag
                    if not state['unknown_alarm']:
                        state['unknown_alarm'] = True
                        await _broadcast('DBR', door, tag_sn, None,
                                         {'detail': 'Unknown tag detected'})

        elif msg_type == 0x03:
            # Status / door state message
            door_open = bool(b3 & 0x01)
            bypassed  = bool(b4 & 0x08)

            if door_open != state['door_open']:
                state['door_open'] = door_open
                await _broadcast('DOP' if door_open else 'DOC', door)

            if bypassed and not state['bypassed']:
                state['bypassed'] = True
                # Resolve all active tag alarms
                for tag_sn in list(state['tags_in_alarm']):
                    tag_info = tags.get(tag_sn, {})
                    await _broadcast('DAR', door, tag_sn, tag_info.get('resident_name'))
                    state['tags_in_alarm'].remove(tag_sn)
                if state['unknown_alarm']:
                    state['unknown_alarm'] = False
                    await _broadcast('DRB', door)
                await _broadcast('DBY', door)

            elif not bypassed and state['bypassed']:
                state['bypassed'] = False
                await _broadcast('BCP', door)


# ── Bus loop ───────────────────────────────────────────────────

async def _bus_loop(network: dict, stop: asyncio.Event) -> None:
    """
    Manage one TCP connection to a Roam Alert network controller.
    Runs until stop is set.  Reconnects automatically on failure.
    """
    net_id = network['id']
    host   = network['host']
    port   = network['port']

    while not stop.is_set():
        reader: Optional[asyncio.StreamReader]  = None
        writer: Optional[asyncio.StreamWriter]  = None

        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=5.0
            )
        except Exception as exc:
            log.warning("Roam Alert [%s:%s] connect failed: %s", host, port, exc)
            await _set_network_online_async(net_id, False)
            await asyncio.sleep(5)
            continue

        log.info("Roam Alert connected to %s:%s", host, port)
        await _set_network_online_async(net_id, True)
        await ws_manager.broadcast('roam_alert.network', {'id': net_id, 'online': True})

        try:
            # Load door controllers for this network
            with db() as conn:
                doors = conn.execute(
                    "SELECT * FROM ra_doors WHERE network_id=? AND enabled=1", (net_id,)
                ).fetchall()
            doors = [dict(d) for d in doors]

            if not doors:
                log.info("Roam Alert network %d has no enabled doors; reconnect in 30s", net_id)
                await asyncio.sleep(30)
                continue

            # Assign bus keys starting at 2 (matching reference engine)
            keyed = {i + 2: d for i, d in enumerate(doors)}
            ctr   = _Counter()
            tags  = _load_tags()

            # Per-door runtime state
            states = {
                key: {
                    'door_open':    None,
                    'bypassed':     False,
                    'unknown_alarm': False,
                    'tags_in_alarm': [],
                    'online':       False,
                    'last_ping':    0.0,
                }
                for key in keyed
            }

            # Initialise all door controllers
            for key, door in keyed.items():
                serial = str(door['serial_number'])
                writer.write(_cmd_set_bus_addr(ctr, serial))
                writer.write(_cmd_init(ctr, serial, key))
                await writer.drain()
                try:
                    await asyncio.wait_for(reader.read(1024), timeout=1.0)
                    states[key]['online'] = True
                    await asyncio.to_thread(_set_door_online, door['id'], True)
                    log.info("RA door %s online (key=%d)", serial, key)
                except asyncio.TimeoutError:
                    log.warning("No init response from RA door %s", serial)

            writer.write(_cmd_global_poll())
            await writer.drain()

            last_heard = asyncio.get_event_loop().time()

            # Main poll loop
            while not stop.is_set():
                # Reload tags periodically (every ~30s on the first door's cycle)
                for key, door in list(keyed.items()):
                    if not states[key]['online']:
                        # Ping attempt every 10 s
                        now = asyncio.get_event_loop().time()
                        if now - states[key]['last_ping'] > 10:
                            states[key]['last_ping'] = now
                            serial = str(door['serial_number'])
                            ping = _build(f'0106{ctr.next()}300{serial.zfill(6)}')
                            writer.write(ping)
                            await writer.drain()
                            try:
                                data = await asyncio.wait_for(reader.read(1024), timeout=0.5)
                                if data and data[0] == 0x01:
                                    writer.write(_cmd_ack(ctr, key))
                                    writer.write(_cmd_init(ctr, serial, key))
                                    await writer.drain()
                                    states[key]['online'] = True
                                    await asyncio.to_thread(_set_door_online, door['id'], True)
                                    log.info("RA door %s back online", serial)
                            except asyncio.TimeoutError:
                                pass
                        continue

                    writer.write(_cmd_poll(ctr, key))
                    await writer.drain()
                    await asyncio.sleep(0.2)

                    try:
                        data = await asyncio.wait_for(reader.read(1024), timeout=0.4)
                        if data:
                            last_heard = asyncio.get_event_loop().time()
                            await _process_response(data, key, door, states[key], tags)
                            writer.write(_cmd_ack(ctr, key))
                            await writer.drain()
                    except asyncio.TimeoutError:
                        log.debug("No poll response from door key=%d", key)
                        states[key]['online'] = False
                        await asyncio.to_thread(_set_door_online, door['id'], False)

                    await asyncio.sleep(0.05)

                # If nothing heard from any door in 5 s, reconnect
                if asyncio.get_event_loop().time() - last_heard > 5:
                    log.warning("Roam Alert [%s] — no response in 5 s, reconnecting", host)
                    break

        except Exception as exc:
            log.error("Roam Alert bus error [%s]: %s", host, exc)

        finally:
            await _set_network_online_async(net_id, False)
            await ws_manager.broadcast('roam_alert.network', {'id': net_id, 'online': False})
            if writer:
                try:
                    writer.close()
                    await writer.wait_closed()
                except Exception:
                    pass

        if not stop.is_set():
            await asyncio.sleep(3)


async def _set_network_online_async(net_id: int, online: bool) -> None:
    await asyncio.to_thread(_set_network_online, net_id, online)


# ── Public API ─────────────────────────────────────────────────

async def start_listener() -> None:
    """Start a bus task for every enabled Roam Alert network. Idempotent."""
    global _stop_event
    if _stop_event is None:
        _stop_event = asyncio.Event()

    with db() as conn:
        networks = conn.execute(
            "SELECT * FROM ra_networks WHERE enabled=1"
        ).fetchall()

    for net in networks:
        net_id = net['id']
        if net_id not in _network_tasks or _network_tasks[net_id].done():
            task = asyncio.create_task(
                _bus_loop(dict(net), _stop_event),
                name=f'roam_alert_net_{net_id}',
            )
            _network_tasks[net_id] = task
            log.info("Roam Alert bus task started for network %d (%s)", net_id, net['host'])


async def stop_listener() -> None:
    """Signal all bus tasks to stop and await them."""
    if _stop_event:
        _stop_event.set()
    for task in list(_network_tasks.values()):
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
    _network_tasks.clear()
    log.info("Roam Alert listener stopped")


async def reload_networks() -> None:
    """Restart listener (called after config changes)."""
    await stop_listener()
    global _stop_event
    _stop_event = asyncio.Event()
    await start_listener()


async def send_keypad_code(door_id: int, slot: int, code: str) -> bool:
    """
    Send a keypad code to a door controller.
    Returns True if the command was sent (no ACK verification yet).
    """
    with db() as conn:
        door = conn.execute("SELECT * FROM ra_doors WHERE id=?", (door_id,)).fetchone()
        if not door:
            return False
        net = conn.execute("SELECT * FROM ra_networks WHERE id=?", (door['network_id'],)).fetchone()
        if not net:
            return False
    door = dict(door)
    net  = dict(net)

    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(net['host'], net['port']), timeout=5.0
        )
        ctr    = _Counter()
        serial = str(door['serial_number'])
        key    = 2  # Only works if there's one door on the bus; multi-door requires key lookup
        writer.write(_cmd_set_bus_addr(ctr, serial))
        writer.write(_cmd_init(ctr, serial, key))
        await writer.drain()
        await asyncio.sleep(0.5)
        writer.write(_cmd_set_code(ctr, key, slot, code))
        await writer.drain()
        await asyncio.sleep(0.2)
        writer.close()
        await writer.wait_closed()
        log.info("Keypad code sent to door %d slot %d", door_id, slot)
        return True
    except Exception as exc:
        log.error("Failed to send keypad code: %s", exc)
        return False
