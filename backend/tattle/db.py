from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    id          INTEGER PRIMARY KEY,
    key         TEXT NOT NULL UNIQUE,     -- 'id:<dhcp client-id>', 'mac:<mac>', 'ip:<ip>' or 'server'
    mac         TEXT,
    client_id   TEXT,
    hostname    TEXT,
    custom_name TEXT,
    vendor      TEXT,
    last_ip     TEXT,
    first_seen  INTEGER NOT NULL,
    last_seen   INTEGER NOT NULL,         -- last time it held a lease or had traffic
    last_active INTEGER,                  -- last time it used the internet
    merged_into INTEGER REFERENCES devices(id)
);

-- Internet bytes per device per hour (hour = unix time of the hour start, UTC).
-- rx = downloaded by the device, tx = uploaded.
CREATE TABLE IF NOT EXISTS usage_hourly (
    device_id INTEGER NOT NULL REFERENCES devices(id),
    hour      INTEGER NOT NULL,
    rx        INTEGER NOT NULL DEFAULT 0,
    tx        INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (device_id, hour)
) WITHOUT ROWID;

-- What the router itself counted on the internet side, per hour, next to
-- what Tattle attributed to devices over exactly the same polling intervals.
CREATE TABLE IF NOT EXISTS wan_hourly (
    hour   INTEGER PRIMARY KEY,
    rx     INTEGER NOT NULL DEFAULT 0,
    tx     INTEGER NOT NULL DEFAULT 0,
    dev_rx INTEGER NOT NULL DEFAULT 0,
    dev_tx INTEGER NOT NULL DEFAULT 0
);

-- Last raw counter readings, so a restart continues without losing bytes.
CREATE TABLE IF NOT EXISTS counter_state (
    name  TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Names a device announced about itself: its DHCP hostname, Bonjour (mDNS)
-- host or service names. `norm` is the comparable form used for matching.
CREATE TABLE IF NOT EXISTS device_names (
    device_id  INTEGER NOT NULL REFERENCES devices(id),
    source     TEXT NOT NULL,              -- 'dhcp' | 'mdns'
    value      TEXT NOT NULL,
    norm       TEXT NOT NULL,
    first_seen INTEGER NOT NULL,
    last_seen  INTEGER NOT NULL,
    PRIMARY KEY (device_id, source, value)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS device_names_norm ON device_names(norm);

-- Stretches of time a device was using the internet (gaps of a few minutes
-- are bridged). Used to tell "never online together" from "online together".
CREATE TABLE IF NOT EXISTS device_sessions (
    id        INTEGER PRIMARY KEY,
    device_id INTEGER NOT NULL REFERENCES devices(id),
    start     INTEGER NOT NULL,
    end       INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS device_sessions_device ON device_sessions(device_id, end);

-- Pairs of identities that are probably one physical device.
CREATE TABLE IF NOT EXISTS merge_suggestions (
    id      INTEGER PRIMARY KEY,
    a_id    INTEGER NOT NULL REFERENCES devices(id),
    b_id    INTEGER NOT NULL REFERENCES devices(id),
    reason  TEXT NOT NULL,
    score   REAL NOT NULL,
    status  TEXT NOT NULL DEFAULT 'open',  -- open | dismissed | merged
    created INTEGER NOT NULL,
    updated INTEGER NOT NULL,
    UNIQUE (a_id, b_id)
);

-- What Tattle decided on its own about which identities are one device.
CREATE TABLE IF NOT EXISTS identity_events (
    id        INTEGER PRIMARY KEY,
    ts        INTEGER NOT NULL,
    kind      TEXT NOT NULL,                 -- 'merged' | 'split'
    device_id INTEGER NOT NULL,              -- the identity that was merged / split off
    other_id  INTEGER NOT NULL,              -- the device it joined / left
    reason    TEXT NOT NULL
);

-- Sign-ins to internet providers. Credentials (tokens, cookies, and for some
-- providers the password they need to sign in again) never leave this server.
CREATE TABLE IF NOT EXISTS provider_accounts (
    id          INTEGER PRIMARY KEY,
    provider    TEXT NOT NULL,              -- e.g. 'eand_eg'
    identifier  TEXT NOT NULL,              -- the number or user name signed in with
    credentials TEXT NOT NULL,              -- JSON, the provider's own
    status      TEXT NOT NULL,              -- 'ok' | 'error' (retried) | 'expired' (sign in again)
    error       TEXT,
    created     INTEGER NOT NULL,
    updated     INTEGER NOT NULL,
    UNIQUE (provider, identifier)
);

-- Home internet lines an account can see.
CREATE TABLE IF NOT EXISTS isp_lines (
    number         TEXT PRIMARY KEY,        -- the provider's id for the line (landline, service id)
    login_id       INTEGER NOT NULL REFERENCES provider_accounts(id) ON DELETE CASCADE,
    provider       TEXT NOT NULL,
    plan           TEXT,
    account_number TEXT,                    -- billing account; can cover several lines
    meta           TEXT,                    -- JSON the provider keeps for its own calls
    error          TEXT,
    read_at        INTEGER,
    billing        TEXT,                    -- JSON: what is owed and recent bills
    billing_at     INTEGER
);

-- What the provider said about a line each time it was read.
CREATE TABLE IF NOT EXISTS isp_readings (
    id        INTEGER PRIMARY KEY,
    number    TEXT NOT NULL,
    ts        INTEGER NOT NULL,
    total     INTEGER,            -- bytes over the data bundles whose size is known
    remaining INTEGER,
    renews    TEXT,               -- 2026-10-25 (older rows may read 25/10/2026)
    live      INTEGER NOT NULL,   -- the line the house was using at that moment
    counted   INTEGER NOT NULL,   -- Tattle's running total of booked bytes at that moment
    items     TEXT NOT NULL       -- the bundles as JSON
);
CREATE INDEX IF NOT EXISTS isp_readings_number ON isp_readings(number, ts);

-- The provider's own history: internet used per line per day, where it has one.
CREATE TABLE IF NOT EXISTS isp_daily (
    number TEXT NOT NULL,
    day    TEXT NOT NULL,              -- 2026-10-11
    bytes  INTEGER NOT NULL,
    PRIMARY KEY (number, day)
) WITHOUT ROWID;

-- When the line in use changed, so cycles and comparisons start at the switch.
CREATE TABLE IF NOT EXISTS line_switches (
    ts     INTEGER NOT NULL,
    number TEXT,                       -- NULL: no line known to be in use
    source TEXT NOT NULL               -- 'router' | 'usage' | 'manual'
);
"""

# Columns added after the first release: (table, column, definition).
ADDED_COLUMNS = [
    ("wan_hourly", "dev_rx", "INTEGER NOT NULL DEFAULT 0"),
    ("wan_hourly", "dev_tx", "INTEGER NOT NULL DEFAULT 0"),
    ("devices", "os_hint", "TEXT"),  # iPhone or iPad / Android / Windows / ...
    ("devices", "model", "TEXT"),  # e.g. iPhone14,2, from Bonjour device-info
    ("devices", "fingerprint", "TEXT"),  # DHCP parameter request list | vendor class
    ("devices", "merge_reason", "TEXT"),  # why merged_into was set
    ("devices", "merged_at", "INTEGER"),
    ("devices", "custom_name_set_at", "INTEGER"),  # the newest name you typed wins after a merge
    ("isp_readings", "cycle_start", "TEXT"),  # when the provider says the current cycle began
]


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=10, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def init(path: str) -> None:
    conn = connect(path)
    try:
        _migrate_isp_logins(conn)
        conn.executescript(SCHEMA)
        for table, column, definition in ADDED_COLUMNS:
            existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    finally:
        conn.close()


def _migrate_isp_logins(conn: sqlite3.Connection) -> None:
    """Databases from before providers were pluggable kept e& sign-ins in
    isp_logins (one token pair per number). Move them to provider_accounts with
    the same ids, and rebuild isp_lines around them, keeping every line."""
    tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if "isp_logins" not in tables:
        return
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(isp_lines)")}

    def col(name: str) -> str:
        return name if name in columns else "NULL"

    conn.execute("PRAGMA foreign_keys=OFF")  # dropping isp_logins must not cascade to the lines
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS provider_accounts (id INTEGER PRIMARY KEY, provider TEXT NOT NULL, "
            "identifier TEXT NOT NULL, credentials TEXT NOT NULL, status TEXT NOT NULL, error TEXT, "
            "created INTEGER NOT NULL, updated INTEGER NOT NULL, UNIQUE (provider, identifier))"
        )
        conn.execute(
            "INSERT INTO provider_accounts(id, provider, identifier, credentials, status, error, created, updated) "
            "SELECT id, 'eand_eg', number, json_object('number', number, 'access', access_token, 'refresh', refresh_token), "
            "status, error, created, updated FROM isp_logins"
        )
        if "isp_lines" not in tables:
            conn.execute("DROP TABLE isp_logins")
            conn.execute("COMMIT")
            return
        conn.execute(
            "CREATE TABLE isp_lines_new (number TEXT PRIMARY KEY, login_id INTEGER NOT NULL REFERENCES "
            "provider_accounts(id) ON DELETE CASCADE, provider TEXT NOT NULL, plan TEXT, account_number TEXT, "
            "meta TEXT, error TEXT, read_at INTEGER, billing TEXT, billing_at INTEGER)"
        )
        conn.execute(
            "INSERT INTO isp_lines_new(number, login_id, provider, plan, account_number, meta, error, read_at, billing, billing_at) "
            f"SELECT number, login_id, 'eand_eg', plan, {col('account_number')}, "
            f"json_object('short_code', {col('short_code')}, 'bill_cycle', {col('bill_cycle')}), "
            f"error, read_at, {col('billing')}, {col('billing_at')} FROM isp_lines"
        )
        conn.execute("DROP TABLE isp_lines")
        conn.execute("ALTER TABLE isp_lines_new RENAME TO isp_lines")
        conn.execute("DROP TABLE isp_logins")
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.execute("PRAGMA foreign_keys=ON")


def device_roots(conn: sqlite3.Connection) -> dict[int, int]:
    """Every device id -> the id it is shown as (itself, or what it was merged into)."""
    parent = {row["id"]: row["merged_into"] for row in conn.execute("SELECT id, merged_into FROM devices")}
    roots: dict[int, int] = {}
    for device_id in parent:
        seen = {device_id}
        root = device_id
        while parent.get(root) is not None and parent[root] not in seen:
            root = parent[root]
            seen.add(root)
        roots[device_id] = root
    return roots


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def get_setting(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
