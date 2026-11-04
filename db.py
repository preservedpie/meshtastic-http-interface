import os
import sqlite3

DB_PATH = os.environ.get('DB_PATH', '/app/data/meshtastic.db')

# On flash-storage deployments (the Raspberry Pi) every fsync is an SD-card
# write that wears the card. With LOW_DISK_WRITES set, WAL+synchronous=NORMAL
# stops fsync-per-commit (fsyncs only at checkpoint) and temp tables stay in
# RAM. The DB stays crash-safe; only the last few seconds of packets are at
# risk on a hard power loss. Default off so existing deployments are unchanged.
LOW_DISK_WRITES = os.environ.get('LOW_DISK_WRITES', '0').lower() in ('1', 'true', 'yes')

_SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS packets (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        received_at  TEXT DEFAULT (datetime('now')),
        topic        TEXT,
        packet_type  TEXT,
        from_node    TEXT,
        sender       TEXT,
        channel      TEXT,
        rssi         INTEGER,
        snr          REAL,
        hop_start    INTEGER,
        hops_away    INTEGER,
        payload_json TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_packets_received_at ON packets(received_at)",
    "CREATE INDEX IF NOT EXISTS idx_packets_from_node   ON packets(from_node)",
    "CREATE INDEX IF NOT EXISTS idx_packets_packet_type ON packets(packet_type)",
    """
    CREATE TABLE IF NOT EXISTS nodes (
        node_id    TEXT PRIMARY KEY,
        long_name  TEXT,
        short_name TEXT,
        hw_model   TEXT,
        role       TEXT,
        last_seen  TEXT,
        last_rssi  INTEGER,
        last_snr   REAL,
        last_hops  INTEGER
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS text_messages (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        received_at TEXT DEFAULT (datetime('now')),
        from_node   TEXT,
        to_node     TEXT,
        channel     TEXT,
        message     TEXT,
        rssi        INTEGER,
        snr         REAL,
        hops_away   INTEGER,
        direction   TEXT DEFAULT 'in',
        ack         INTEGER,
        packet_id   INTEGER REFERENCES packets(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS alert_rules (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        name        TEXT,
        enabled     INTEGER DEFAULT 1,
        packet_type TEXT,
        from_node   TEXT,
        channel     TEXT,
        keyword     TEXT,
        priority    INTEGER DEFAULT 0,
        sound       TEXT DEFAULT 'pushover',
        created_at  TEXT DEFAULT (datetime('now'))
    )
    """,
    # Generic key/value settings for main-app features (weather station config,
    # auto-cleanup config, ...) — mirrors bot/db.py's settings table, kept
    # separate since the bot has its own SQLite file.
    """
    CREATE TABLE IF NOT EXISTS app_settings (
        key   TEXT PRIMARY KEY,
        value TEXT
    )
    """,
    # Which node(s) feed each weather metric (temperature, relative_humidity,
    # barometric_pressure, ...) into the off-grid forecast. A metric can have
    # multiple nodes; readings are averaged across them.
    """
    CREATE TABLE IF NOT EXISTS weather_station_config (
        metric  TEXT NOT NULL,
        node_id TEXT NOT NULL,
        PRIMARY KEY (metric, node_id)
    )
    """,
]

# Example rules, inserted only when the alert_rules table is first created.
# (Seeding with INSERT OR IGNORE on every startup resurrected rules the user
# had deleted.) Serial-path channels are integer indexes, not names.
_SEED_RULES = [
    "INSERT INTO alert_rules (name, enabled, packet_type, channel, priority, sound) VALUES ('Any text on channel 2 (Sub)', 1, 'text', '2', 0, 'pushover')",
    "INSERT INTO alert_rules (name, enabled, packet_type, keyword, priority, sound) VALUES ('Emergency keyword', 1, 'text', 'emergency', 1, 'siren')",
]


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = get_connection()
    rules_existed = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='alert_rules'"
    ).fetchone() is not None
    for stmt in _SCHEMA:
        conn.execute(stmt)
    if not rules_existed:
        for stmt in _SEED_RULES:
            conn.execute(stmt)
    _migrate(conn)
    conn.commit()
    conn.close()


def _migrate(conn):
    """Additive, idempotent migrations for existing databases."""
    cols = {r['name'] for r in conn.execute("PRAGMA table_info(nodes)")}
    if 'alias' not in cols:
        # User-assigned friendly name; takes display priority over long_name.
        conn.execute("ALTER TABLE nodes ADD COLUMN alias TEXT")

    # Node-database enrichment: synced from the connected radio's own node DB
    # (meshtastic_iface.get_known_nodes()), which is richer than any single
    # packet — it's how we learn a node's PKI public key (needed for DMs) and
    # other fields that don't arrive on every announcement.
    node_db_cols = {
        'node_num':                 'INTEGER',
        'macaddr':                  'TEXT',
        'public_key':               'TEXT',
        'is_licensed':              'INTEGER',
        'is_unmessagable':          'INTEGER',
        'via_mqtt':                 'INTEGER',
        'is_favorite':              'INTEGER',
        'is_key_manually_verified': 'INTEGER',
    }
    for col, coltype in node_db_cols.items():
        if col not in cols:
            conn.execute(f"ALTER TABLE nodes ADD COLUMN {col} {coltype}")

    msg_cols = {r['name'] for r in conn.execute("PRAGMA table_info(text_messages)")}
    if 'direction' not in msg_cols:
        # Existing rows all came from received packets; default them to 'in'
        # so historical data still filters/sorts correctly.
        conn.execute("ALTER TABLE text_messages ADD COLUMN direction TEXT DEFAULT 'in'")
    if 'to_node' not in msg_cols:
        # NULL = broadcast; set = DM destination (outgoing) or observed
        # unicast destination (incoming, e.g. a DM addressed to us).
        conn.execute("ALTER TABLE text_messages ADD COLUMN to_node TEXT")
    if 'ack' not in msg_cols:
        # DM delivery outcome: 1 = ACKed, 0 = failed/NAKed, NULL = n/a
        # (broadcasts aren't individually ACKed) or unknown.
        conn.execute("ALTER TABLE text_messages ADD COLUMN ack INTEGER")
    if 'bot_responded' not in msg_cols:
        # Incoming text only: 1 = the bot replied (or queued an async reply),
        # 0 = it looked at the message but didn't, NULL = n/a (outgoing, or a
        # pre-migration row from before this was tracked).
        conn.execute("ALTER TABLE text_messages ADD COLUMN bot_responded INTEGER")
    if 'rule_matched' not in msg_cols:
        # Comma-separated names of any forwarding (alert_rules) rules that
        # fired for this message, e.g. sent a Pushover notification. NULL if
        # none matched.
        conn.execute("ALTER TABLE text_messages ADD COLUMN rule_matched TEXT")


def get_connection():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    if LOW_DISK_WRITES:
        # No fsync per commit (only at checkpoint), temp tables in RAM, and let
        # the WAL grow a bit longer between checkpoints to batch SD-card writes.
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA temp_store=MEMORY")
        conn.execute("PRAGMA wal_autocheckpoint=2000")
    return conn


# ---------------------------------------------------------------------------
# app_settings — generic key/value store shared by weather + maintenance
# ---------------------------------------------------------------------------

def get_app_setting(conn, key: str, default=None):
    row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    return row['value'] if row else default


def get_all_app_settings(conn) -> dict:
    rows = conn.execute("SELECT key, value FROM app_settings").fetchall()
    return {r['key']: r['value'] for r in rows}


def set_app_setting(conn, key: str, value) -> None:
    conn.execute(
        "INSERT INTO app_settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )
    conn.commit()
