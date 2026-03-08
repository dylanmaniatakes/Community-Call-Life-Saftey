"""
SQLite database layer for the Nurse Call System.
Handles schema initialisation and provides a simple context-manager
connection helper. All timestamps stored as UTC ISO-8601.
"""

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

# Allow the DB path to be overridden via environment variable so the
# Docker deployment can mount a persistent volume at /data.
DB_PATH = Path(os.environ.get("DB_PATH", str(Path(__file__).parent / "nurse_call.db")))


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def db():
    """Yield an auto-commit/rollback connection."""
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    """Create all tables if they do not already exist."""
    with db() as conn:
        conn.executescript("""
        -- ----------------------------------------------------------------
        -- Innovonics devices registered in the system
        -- ----------------------------------------------------------------
        CREATE TABLE IF NOT EXISTS devices (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id   TEXT    UNIQUE NOT NULL,
            name        TEXT    NOT NULL,
            location    TEXT,
            device_type TEXT    DEFAULT 'pendant',
            priority    TEXT    DEFAULT 'normal',
            active      INTEGER DEFAULT 1,
            last_seen   TEXT,
            created_at  TEXT    DEFAULT (datetime('now','utc'))
        );

        -- ----------------------------------------------------------------
        -- Call events (active + history)
        -- ----------------------------------------------------------------
        CREATE TABLE IF NOT EXISTS calls (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id        TEXT    NOT NULL,
            device_name      TEXT,
            location         TEXT,
            priority         TEXT    DEFAULT 'normal',
            status           TEXT    DEFAULT 'active',
            timestamp        TEXT    NOT NULL,
            acknowledged_at  TEXT,
            acknowledged_by  TEXT,
            cleared_at       TEXT,
            raw_data         TEXT
        );

        -- ----------------------------------------------------------------
        -- Call audit trail
        -- ----------------------------------------------------------------
        CREATE TABLE IF NOT EXISTS call_events (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            call_id    INTEGER NOT NULL,
            event      TEXT    NOT NULL,
            actor      TEXT,
            timestamp  TEXT    DEFAULT (datetime('now','utc')),
            notes      TEXT
        );

        -- ----------------------------------------------------------------
        -- SMTP notification config (single row, id=1)
        -- ----------------------------------------------------------------
        CREATE TABLE IF NOT EXISTS smtp_config (
            id         INTEGER PRIMARY KEY,
            server     TEXT,
            port       INTEGER DEFAULT 587,
            email      TEXT,
            password   TEXT,
            encryption TEXT    DEFAULT 'STARTTLS',
            enabled    INTEGER DEFAULT 0
        );

        -- ----------------------------------------------------------------
        -- Pager endpoints (multiple)
        -- ----------------------------------------------------------------
        CREATE TABLE IF NOT EXISTS pager_configs (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            name            TEXT    NOT NULL,
            host            TEXT    NOT NULL,
            port            INTEGER NOT NULL,
            protocol        TEXT    DEFAULT 'TAP',
            default_capcode TEXT,
            enabled         INTEGER DEFAULT 1
        );

        -- ----------------------------------------------------------------
        -- Relay / dome-light controllers (multiple; type: rcm | esp)
        -- ----------------------------------------------------------------
        CREATE TABLE IF NOT EXISTS relay_configs (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            name         TEXT    NOT NULL,
            relay_type   TEXT    NOT NULL,
            host         TEXT    NOT NULL,
            port         INTEGER DEFAULT 23,
            relay_number INTEGER DEFAULT 1,
            enabled      INTEGER DEFAULT 1
        );

        -- ----------------------------------------------------------------
        -- External input interfaces (ESP32/ESPHome inputs)
        -- ----------------------------------------------------------------
        CREATE TABLE IF NOT EXISTS input_configs (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            name         TEXT    NOT NULL,
            input_type   TEXT    NOT NULL DEFAULT 'esp',
            host         TEXT    NOT NULL,
            port         INTEGER DEFAULT 80,
            input_number INTEGER DEFAULT 1,
            input_name   TEXT,
            device_id    TEXT    NOT NULL,
            active_high  INTEGER DEFAULT 1,
            last_state   TEXT,
            last_seen    TEXT,
            enabled      INTEGER DEFAULT 1
        );

        -- ----------------------------------------------------------------
        -- Innovonics coordinator connection (single row, id=1)
        -- ----------------------------------------------------------------
        CREATE TABLE IF NOT EXISTS innovonics_config (
            id          INTEGER PRIMARY KEY,
            mode        TEXT    DEFAULT 'tcp',
            host        TEXT,
            port        INTEGER DEFAULT 3000,
            serial_port TEXT,
            baud_rate   INTEGER DEFAULT 9600,
            enabled     INTEGER DEFAULT 0
        );

        -- ----------------------------------------------------------------
        -- Notification rules: device + priority filters -> action
        -- action_type: email | page | relay
        -- action_config: JSON blob
        -- ----------------------------------------------------------------
        CREATE TABLE IF NOT EXISTS notification_rules (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            name            TEXT    NOT NULL,
            device_filter   TEXT    DEFAULT 'all',
            priority_filter TEXT    DEFAULT 'all',
            action_type     TEXT    NOT NULL,
            action_config   TEXT    NOT NULL,
            enabled         INTEGER DEFAULT 1,
            created_at      TEXT    DEFAULT (datetime('now','utc'))
        );

        -- ----------------------------------------------------------------
        -- Staff users (basic, for ack attribution)
        -- ----------------------------------------------------------------
        CREATE TABLE IF NOT EXISTS users (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            username        TEXT    UNIQUE NOT NULL,
            hashed_password TEXT,
            role            TEXT    DEFAULT 'staff',
            active          INTEGER DEFAULT 1,
            created_at      TEXT    DEFAULT (datetime('now','utc'))
        );

        -- Seed a default admin if table is empty
        -- (do NOT include hashed_password here — old DBs may not have that column yet;
        --  the migration below will add it)
        INSERT OR IGNORE INTO users (id, username, role)
        VALUES (1, 'admin', 'admin');

        -- ----------------------------------------------------------------
        -- Innovonics repeaters (separate from call devices)
        -- ----------------------------------------------------------------
        CREATE TABLE IF NOT EXISTS repeaters (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            serial_number TEXT    UNIQUE NOT NULL,
            name          TEXT,
            last_seen     TEXT,
            status        TEXT    DEFAULT 'unknown',
            created_at    TEXT    DEFAULT (datetime('now','utc'))
        );

        -- ----------------------------------------------------------------
        -- Areas — group apartments/devices by floor, wing, building, etc.
        -- ----------------------------------------------------------------
        CREATE TABLE IF NOT EXISTS areas (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            name            TEXT    NOT NULL,
            relay_config_id INTEGER,
            created_at      TEXT    DEFAULT (datetime('now','utc'))
        );

        -- ----------------------------------------------------------------
        -- Apartments / units — group devices; optional dome-light relay
        -- ----------------------------------------------------------------
        CREATE TABLE IF NOT EXISTS apartments (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            name            TEXT    NOT NULL,
            relay_config_id INTEGER,
            relay_number    INTEGER DEFAULT 1,
            area_id         INTEGER,
            created_at      TEXT    DEFAULT (datetime('now','utc'))
        );

        -- ----------------------------------------------------------------
        -- Telegram bot notification (single row, id=1)
        -- ----------------------------------------------------------------
        CREATE TABLE IF NOT EXISTS telegram_config (
            id         INTEGER PRIMARY KEY,
            bot_token  TEXT,
            chat_id    TEXT,
            enabled    INTEGER DEFAULT 0
        );

        -- ----------------------------------------------------------------
        -- Twilio SMS notification (single row, id=1)
        -- ----------------------------------------------------------------
        CREATE TABLE IF NOT EXISTS twilio_config (
            id           INTEGER PRIMARY KEY,
            account_sid  TEXT,
            auth_token   TEXT,
            from_number  TEXT,
            to_number    TEXT,
            enabled      INTEGER DEFAULT 0
        );

        -- ----------------------------------------------------------------
        -- Roam Alert Wander System
        -- ----------------------------------------------------------------
        CREATE TABLE IF NOT EXISTS ra_networks (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            name         TEXT    NOT NULL,
            host         TEXT    NOT NULL,
            port         INTEGER DEFAULT 10001,
            enabled      INTEGER DEFAULT 1,
            online       INTEGER DEFAULT 0,
            last_seen    TEXT
        );
        CREATE TABLE IF NOT EXISTS ra_doors (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            network_id      INTEGER NOT NULL,
            name            TEXT    NOT NULL,
            serial_number   TEXT    NOT NULL,
            location        TEXT,
            monitor_sanity  INTEGER DEFAULT 1,
            enabled         INTEGER DEFAULT 1,
            online          INTEGER DEFAULT 0,
            last_seen       TEXT
        );
        CREATE TABLE IF NOT EXISTS ra_tags (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            tag_serial    TEXT    UNIQUE NOT NULL,
            resident_name TEXT,
            apartment_id  INTEGER,
            enabled       INTEGER DEFAULT 1,
            created_at    TEXT    DEFAULT (datetime('now','utc'))
        );
        CREATE TABLE IF NOT EXISTS ra_events (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            event_code      TEXT    NOT NULL,
            tag_serial      TEXT,
            resident_name   TEXT,
            door_id         INTEGER,
            door_name       TEXT,
            details         TEXT,
            timestamp       TEXT    DEFAULT (datetime('now','utc'))
        );
        CREATE TABLE IF NOT EXISTS ra_codes (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            door_id       INTEGER NOT NULL,
            slot          INTEGER NOT NULL DEFAULT 1,
            code          TEXT    NOT NULL,
            label         TEXT,
            code_type     TEXT    DEFAULT 'access',
            created_at    TEXT    DEFAULT (datetime('now','utc'))
        );

        -- ----------------------------------------------------------------
        -- System-wide configuration (single row, id=1)
        -- ----------------------------------------------------------------
        CREATE TABLE IF NOT EXISTS system_config (
            id            INTEGER PRIMARY KEY,
            auth_required INTEGER DEFAULT 0
        );

        -- ----------------------------------------------------------------
        -- AeroScout Location Engine (ALE) connection (single row, id=1)
        -- ----------------------------------------------------------------
        CREATE TABLE IF NOT EXISTS aeroscout_config (
            id             INTEGER PRIMARY KEY,
            host           TEXT,
            port           INTEGER DEFAULT 1411,
            username       TEXT    DEFAULT 'Admin',
            password       TEXT,
            client_version TEXT    DEFAULT '5.7.30',
            enabled        INTEGER DEFAULT 0
        );

        -- ----------------------------------------------------------------
        -- ALE door controllers / exciters (DC1000, EX5700, EX5500, GW3100)
        -- Populated automatically from DevicesStatusNotification stream.
        -- ----------------------------------------------------------------
        CREATE TABLE IF NOT EXISTS ale_devices (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id        TEXT    UNIQUE NOT NULL,  -- e.g. "3_6"
            name             TEXT,
            mac              TEXT,
            model            TEXT,                     -- DC1000 EX5700 EX5500 GW3100 …
            firmware         TEXT,
            general_status   TEXT    DEFAULT 'unknown',-- OK Unreachable …
            comm_status      INTEGER,
            security_enabled INTEGER DEFAULT 0,
            last_seen        TEXT,
            last_alert_type  TEXT,                     -- most recent NotificationList type
            last_alert_desc  TEXT,
            created_at       TEXT    DEFAULT (datetime('now','utc'))
        );

        -- ----------------------------------------------------------------
        -- ALE wander tags — strap address / resident mapping
        -- Populated automatically from LocationReport stream.
        -- ----------------------------------------------------------------
        CREATE TABLE IF NOT EXISTS ale_tags (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            mac              TEXT    UNIQUE NOT NULL,  -- e.g. "000CCC1A0F75"
            strap_address    TEXT,                     -- ID printed on the strap
            resident_name    TEXT,
            apartment_id     INTEGER,
            last_x           REAL,
            last_y           REAL,
            last_z           REAL,
            last_map_id      TEXT,
            last_zone_id     TEXT,
            last_seen        TEXT,
            battery_status   TEXT,
            location_quality REAL,
            created_at       TEXT    DEFAULT (datetime('now','utc'))
        );

        -- Seed empty rows so UPDATE always works
        INSERT OR IGNORE INTO smtp_config (id) VALUES (1);
        INSERT OR IGNORE INTO innovonics_config (id) VALUES (1);
        INSERT OR IGNORE INTO telegram_config (id) VALUES (1);
        INSERT OR IGNORE INTO twilio_config (id) VALUES (1);
        INSERT OR IGNORE INTO system_config (id) VALUES (1);
        INSERT OR IGNORE INTO aeroscout_config (id) VALUES (1);
        """)
    # Migrate: add client_version to aeroscout_config if it doesn't exist
    with db() as conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(aeroscout_config)").fetchall()]
        if "client_version" not in cols:
            conn.execute(
                "ALTER TABLE aeroscout_config ADD COLUMN client_version TEXT DEFAULT '5.7.30'"
            )

    # Migrate: add nid column if it doesn't exist (safe for existing DBs)
    with db() as conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(innovonics_config)").fetchall()]
        if "nid" not in cols:
            conn.execute("ALTER TABLE innovonics_config ADD COLUMN nid INTEGER DEFAULT 16")

    # Migrate: add vendor_type to devices if it doesn't exist
    with db() as conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(devices)").fetchall()]
        if "vendor_type" not in cols:
            conn.execute(
                "ALTER TABLE devices ADD COLUMN vendor_type TEXT DEFAULT 'innovonics'"
            )

    # Migrate: add apartment_id, relay_config_id, relay_number to devices
    with db() as conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(devices)").fetchall()]
        if "apartment_id" not in cols:
            conn.execute("ALTER TABLE devices ADD COLUMN apartment_id INTEGER")
        if "relay_config_id" not in cols:
            conn.execute("ALTER TABLE devices ADD COLUMN relay_config_id INTEGER")
        if "relay_number" not in cols:
            conn.execute("ALTER TABLE devices ADD COLUMN relay_number INTEGER")
        if "aux_label" not in cols:
            conn.execute("ALTER TABLE devices ADD COLUMN aux_label TEXT")
        if "area_id" not in cols:
            conn.execute("ALTER TABLE devices ADD COLUMN area_id INTEGER")

    # Migrate: add area_id to apartments
    with db() as conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(apartments)").fetchall()]
        if "area_id" not in cols:
            conn.execute("ALTER TABLE apartments ADD COLUMN area_id INTEGER")

    # Migrate: add area_filter to notification_rules
    with db() as conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(notification_rules)").fetchall()]
        if "area_filter" not in cols:
            conn.execute(
                "ALTER TABLE notification_rules ADD COLUMN area_filter TEXT DEFAULT 'all'"
            )

    # Migrate: add notify_on to notification_rules (call | clear | both)
    with db() as conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(notification_rules)").fetchall()]
        if "notify_on" not in cols:
            conn.execute(
                "ALTER TABLE notification_rules ADD COLUMN notify_on TEXT DEFAULT 'call'"
            )

    # Migrate: ensure input_configs has all expected columns (for older DB snapshots)
    with db() as conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(input_configs)").fetchall()]
        if cols:
            if "input_type" not in cols:
                conn.execute("ALTER TABLE input_configs ADD COLUMN input_type TEXT DEFAULT 'esp'")
            if "input_name" not in cols:
                conn.execute("ALTER TABLE input_configs ADD COLUMN input_name TEXT")
            if "active_high" not in cols:
                conn.execute("ALTER TABLE input_configs ADD COLUMN active_high INTEGER DEFAULT 1")
            if "last_state" not in cols:
                conn.execute("ALTER TABLE input_configs ADD COLUMN last_state TEXT")
            if "last_seen" not in cols:
                conn.execute("ALTER TABLE input_configs ADD COLUMN last_seen TEXT")

    # Migrate: add hashed_password to users if it doesn't exist (added with auth system)
    with db() as conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()]
        if "hashed_password" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN hashed_password TEXT DEFAULT ''")

    # Ensure system_config seed row exists (executescript may have stopped before seeding it
    # if an earlier statement failed on an old schema)
    with db() as conn:
        conn.execute("INSERT OR IGNORE INTO system_config (id) VALUES (1)")

    # Migrate: add VAPID keys to system_config (for Web Push notifications)
    with db() as conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(system_config)").fetchall()]
        if "vapid_private_key" not in cols:
            conn.execute("ALTER TABLE system_config ADD COLUMN vapid_private_key TEXT")
        if "vapid_public_key" not in cols:
            conn.execute("ALTER TABLE system_config ADD COLUMN vapid_public_key TEXT")

    # Push subscriptions — one row per browser/device that opted in
    with db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS push_subscriptions (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                endpoint   TEXT    UNIQUE NOT NULL,
                p256dh     TEXT    NOT NULL,
                auth       TEXT    NOT NULL,
                user_agent TEXT,
                created_at TEXT    DEFAULT (datetime('now','utc'))
            )
        """)
