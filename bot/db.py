"""SQLite persistence layer for the bot half of the app.

Ported from meshtastic-mesh-bot. Uses a separate SQLite file from the
http-interface's packet/node/rules database so the two halves can be backed
up, pruned, or wiped independently.
"""

import json
import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger('mesh-bot.db')

_lock = threading.RLock()
_conn: Optional[sqlite3.Connection] = None
_db_path = os.environ.get('BOT_DB_PATH', '/app/data/mesh-bot.db')

# See db.py: on the Raspberry Pi, minimize SD-card fsyncs. Default off.
LOW_DISK_WRITES = os.environ.get('LOW_DISK_WRITES', '0').lower() in ('1', 'true', 'yes')

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS modules (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT UNIQUE NOT NULL,
    kind        TEXT NOT NULL,
    enabled     INTEGER NOT NULL DEFAULT 1,
    description TEXT NOT NULL DEFAULT '',
    aliases     TEXT NOT NULL DEFAULT '[]',
    config      TEXT NOT NULL DEFAULT '{}',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS message_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT NOT NULL,
    direction  TEXT NOT NULL,
    node_id    INTEGER,
    node_name  TEXT,
    channel    INTEGER,
    text       TEXT NOT NULL DEFAULT '',
    meta       TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_msglog_ts ON message_log(ts);

CREATE TABLE IF NOT EXISTS channel_policy (
    channel        INTEGER PRIMARY KEY,
    name           TEXT NOT NULL DEFAULT '',
    listen         INTEGER NOT NULL DEFAULT 1,
    respond        INTEGER NOT NULL DEFAULT 1,
    is_response_ch INTEGER NOT NULL DEFAULT 0,
    allowed_cmds   TEXT NOT NULL DEFAULT '',
    updated_at     TEXT NOT NULL
);

-- One row per node, tracking the last calendar date it was sent a
-- location-based weather alert DM (see engine._run_per_node_weather_alerts).
-- Persisted (not in-memory) so a restart during the alert hour can't
-- re-spam every node with a duplicate DM the same day.
CREATE TABLE IF NOT EXISTS node_weather_alerts (
    node_id         TEXT PRIMARY KEY,
    last_alert_date TEXT
);
"""


def init(path: Optional[str] = None) -> None:
    global _conn, _db_path
    if path:
        _db_path = path
    os.makedirs(os.path.dirname(_db_path), exist_ok=True)
    with _lock:
        _conn = sqlite3.connect(_db_path, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        if LOW_DISK_WRITES:
            _conn.execute("PRAGMA synchronous=NORMAL")
            _conn.execute("PRAGMA temp_store=MEMORY")
            _conn.execute("PRAGMA wal_autocheckpoint=2000")
        _conn.executescript(SCHEMA)
        _conn.commit()
    logger.info(f"Bot DB ready at {_db_path}")


def get_db_path() -> str:
    return _db_path


def _c() -> sqlite3.Connection:
    if _conn is None:
        raise RuntimeError("bot.db.init() must be called before use")
    return _conn


# --- Settings -------------------------------------------------------------

def get_setting(key: str, default: Optional[str] = None) -> Optional[str]:
    with _lock:
        row = _c().execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
    return row['value'] if row else default


def get_all_settings() -> dict:
    with _lock:
        rows = _c().execute("SELECT key, value FROM settings").fetchall()
    return {r['key']: r['value'] for r in rows}


def set_setting(key: str, value: str) -> None:
    with _lock:
        _c().execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )
        _c().commit()


def delete_setting(key: str) -> None:
    with _lock:
        _c().execute("DELETE FROM settings WHERE key = ?", (key,))
        _c().commit()


# --- Modules --------------------------------------------------------------

def list_modules(only_enabled: bool = False) -> list:
    sql = "SELECT * FROM modules"
    if only_enabled:
        sql += " WHERE enabled = 1"
    sql += " ORDER BY name"
    with _lock:
        rows = _c().execute(sql).fetchall()
    return [_module_row_to_dict(r) for r in rows]


def get_module(module_id: int) -> Optional[dict]:
    with _lock:
        row = _c().execute(
            "SELECT * FROM modules WHERE id = ?", (module_id,)
        ).fetchone()
    return _module_row_to_dict(row) if row else None


def get_module_by_name(name: str) -> Optional[dict]:
    with _lock:
        row = _c().execute(
            "SELECT * FROM modules WHERE name = ?", (name.lower(),)
        ).fetchone()
    return _module_row_to_dict(row) if row else None


def upsert_module(name, kind, description, aliases, config,
                  enabled=True, module_id=None) -> int:
    now = _now()
    aliases_json = json.dumps([a.lower() for a in aliases])
    config_json = json.dumps(config)
    with _lock:
        if module_id is not None:
            _c().execute(
                "UPDATE modules SET name=?, kind=?, enabled=?, description=?, "
                "aliases=?, config=?, updated_at=? WHERE id=?",
                (name.lower(), kind, int(enabled), description,
                 aliases_json, config_json, now, module_id),
            )
            _c().commit()
            return module_id
        cur = _c().execute(
            "INSERT INTO modules(name, kind, enabled, description, aliases, "
            "config, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (name.lower(), kind, int(enabled), description,
             aliases_json, config_json, now, now),
        )
        _c().commit()
        return int(cur.lastrowid)


def set_module_enabled(module_id: int, enabled: bool) -> None:
    with _lock:
        _c().execute(
            "UPDATE modules SET enabled=?, updated_at=? WHERE id=?",
            (int(enabled), _now(), module_id),
        )
        _c().commit()


def delete_module(module_id: int) -> None:
    with _lock:
        _c().execute("DELETE FROM modules WHERE id = ?", (module_id,))
        _c().commit()


def _module_row_to_dict(row) -> dict:
    return {
        'id': row['id'],
        'name': row['name'],
        'kind': row['kind'],
        'enabled': bool(row['enabled']),
        'description': row['description'],
        'aliases': json.loads(row['aliases']),
        'config': json.loads(row['config']),
        'created_at': row['created_at'],
        'updated_at': row['updated_at'],
    }


# --- Message log ----------------------------------------------------------

def log_message(direction, text, node_id=None, node_name=None,
                channel=None, meta=None) -> None:
    with _lock:
        _c().execute(
            "INSERT INTO message_log(ts, direction, node_id, node_name, "
            "channel, text, meta) VALUES(?,?,?,?,?,?,?)",
            (_now(), direction, node_id, node_name, channel, text,
             json.dumps(meta or {})),
        )
        _c().commit()


def recent_messages(limit: int = 100, search: str = '') -> list:
    sql = "SELECT * FROM message_log"
    params: list = []
    if search:
        sql += " WHERE text LIKE ?"
        params.append(f"%{search}%")
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    with _lock:
        rows = _c().execute(sql, params).fetchall()
    return [
        {
            'id': r['id'], 'ts': r['ts'], 'direction': r['direction'],
            'node_id': r['node_id'], 'node_name': r['node_name'],
            'channel': r['channel'], 'text': r['text'],
            'meta': json.loads(r['meta']),
        }
        for r in rows
    ]


def prune_message_log(keep: int = 5000) -> None:
    with _lock:
        _c().execute(
            "DELETE FROM message_log WHERE id NOT IN "
            "(SELECT id FROM message_log ORDER BY id DESC LIMIT ?)",
            (keep,),
        )
        _c().commit()


# --- Channel policy -------------------------------------------------------
# Per-channel control: should the bot listen on this channel, should it
# respond, is this the default response channel, and which commands (if any)
# are restricted to a per-channel allowlist.

def list_channel_policies() -> list:
    with _lock:
        rows = _c().execute(
            "SELECT * FROM channel_policy ORDER BY channel"
        ).fetchall()
    out = []
    for r in rows:
        out.append({
            'channel': r['channel'],
            'name': r['name'],
            'listen': bool(r['listen']),
            'respond': bool(r['respond']),
            'is_response_ch': bool(r['is_response_ch']),
            'allowed_cmds': [c for c in (r['allowed_cmds'] or '').split(',') if c],
            'updated_at': r['updated_at'],
        })
    return out


def get_channel_policy(channel: int) -> Optional[dict]:
    with _lock:
        row = _c().execute(
            "SELECT * FROM channel_policy WHERE channel = ?", (channel,)
        ).fetchone()
    if not row:
        return None
    return {
        'channel': row['channel'],
        'name': row['name'],
        'listen': bool(row['listen']),
        'respond': bool(row['respond']),
        'is_response_ch': bool(row['is_response_ch']),
        'allowed_cmds': [c for c in (row['allowed_cmds'] or '').split(',') if c],
    }


def upsert_channel_policy(channel: int, name: str = '', listen: bool = True,
                          respond: bool = True, is_response_ch: bool = False,
                          allowed_cmds: Optional[list] = None) -> None:
    cmds_csv = ','.join(c.strip().lower() for c in (allowed_cmds or []) if c.strip())
    now = _now()
    with _lock:
        # If marking this channel as response channel, unset others first.
        if is_response_ch:
            _c().execute("UPDATE channel_policy SET is_response_ch = 0")
        _c().execute(
            "INSERT INTO channel_policy(channel, name, listen, respond, "
            "is_response_ch, allowed_cmds, updated_at) VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(channel) DO UPDATE SET "
            "name=excluded.name, listen=excluded.listen, "
            "respond=excluded.respond, is_response_ch=excluded.is_response_ch, "
            "allowed_cmds=excluded.allowed_cmds, updated_at=excluded.updated_at",
            (channel, name, int(listen), int(respond), int(is_response_ch),
             cmds_csv, now),
        )
        _c().commit()


def delete_channel_policy(channel: int) -> None:
    with _lock:
        _c().execute("DELETE FROM channel_policy WHERE channel = ?", (channel,))
        _c().commit()


def get_response_channel() -> Optional[int]:
    with _lock:
        row = _c().execute(
            "SELECT channel FROM channel_policy WHERE is_response_ch = 1 LIMIT 1"
        ).fetchone()
    return row['channel'] if row else None


# --- Per-node weather alert dedup ------------------------------------------

def get_node_alert_date(node_id: str) -> Optional[str]:
    with _lock:
        row = _c().execute(
            "SELECT last_alert_date FROM node_weather_alerts WHERE node_id = ?",
            (node_id,),
        ).fetchone()
    return row['last_alert_date'] if row else None


def set_node_alert_date(node_id: str, date_str: str) -> None:
    with _lock:
        _c().execute(
            "INSERT INTO node_weather_alerts(node_id, last_alert_date) VALUES(?, ?) "
            "ON CONFLICT(node_id) DO UPDATE SET last_alert_date = excluded.last_alert_date",
            (node_id, date_str),
        )
        _c().commit()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
