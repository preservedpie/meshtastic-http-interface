"""Storage usage and data-retention housekeeping for the two SQLite files
this app keeps (db.py's meshtastic.db and bot/db.py's mesh-bot.db). Neither
has ever had a pruning story besides the bot's message_log row cap — this
module adds visibility (row counts, file sizes, oldest/newest rows) plus
manual and scheduled cleanup of the two high-volume tables (packets,
text_messages). Config tables (nodes, alert_rules, settings, ...) are small
and never auto-pruned.
"""

import logging
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta

import db
from bot import db as bot_db

logger = logging.getLogger('maintenance')

_MAIN_TABLES = ['packets', 'nodes', 'text_messages', 'alert_rules', 'app_settings', 'weather_station_config']
_BOT_TABLES = ['settings', 'modules', 'message_log', 'channel_policy', 'node_weather_alerts']

DEFAULT_RETENTION_DAYS = 90
DEFAULT_RUN_HOUR = 3
_TS_FMT = '%Y-%m-%d %H:%M:%S'


def _file_size(path: str) -> int:
    """Main file plus WAL/SHM sidecars, since WAL mode can leave a fair
    amount of not-yet-checkpointed data sitting outside the main file."""
    total = 0
    for suffix in ('', '-wal', '-shm'):
        p = path + suffix
        if os.path.exists(p):
            total += os.path.getsize(p)
    return total


def get_stats() -> dict:
    conn = db.get_connection()
    tables = []
    for t in _MAIN_TABLES:
        n = conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()['n']
        tables.append({'table': t, 'db': 'main', 'rows': n})
    oldest = conn.execute("SELECT MIN(received_at) AS t FROM packets").fetchone()['t']
    newest = conn.execute("SELECT MAX(received_at) AS t FROM packets").fetchone()['t']
    conn.close()

    try:
        bconn = sqlite3.connect(bot_db.get_db_path())
        bconn.row_factory = sqlite3.Row
        for t in _BOT_TABLES:
            n = bconn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()['n']
            tables.append({'table': t, 'db': 'bot', 'rows': n})
        bconn.close()
    except Exception as e:
        logger.warning(f"Could not read bot DB stats: {e}")

    return {
        'tables': tables,
        'main_db_bytes': _file_size(db.DB_PATH),
        'bot_db_bytes': _file_size(bot_db.get_db_path()),
        'oldest_packet_at': oldest,
        'newest_packet_at': newest,
    }


def cleanup(conn, retention_days: int, vacuum: bool = False) -> dict:
    """Delete packets (and any text_messages tied to them) older than
    retention_days. text_messages is cleared first — by its own timestamp AND
    by referencing an old packet — so the later packets delete never trips
    the packets<-text_messages foreign key (PRAGMA foreign_keys=ON)."""
    cutoff = (datetime.utcnow() - timedelta(days=retention_days)).strftime(_TS_FMT)
    cur = conn.cursor()
    cur.execute(
        "DELETE FROM text_messages WHERE received_at < ? OR packet_id IN "
        "(SELECT id FROM packets WHERE received_at < ?)",
        (cutoff, cutoff),
    )
    messages_deleted = cur.rowcount
    cur.execute("DELETE FROM packets WHERE received_at < ?", (cutoff,))
    packets_deleted = cur.rowcount
    conn.commit()
    if vacuum:
        conn.execute("VACUUM")
    logger.info(f"Cleanup: removed {packets_deleted} packets, {messages_deleted} messages older than {cutoff}")
    return {'packets_deleted': packets_deleted, 'text_messages_deleted': messages_deleted, 'cutoff': cutoff}


def get_settings(conn) -> dict:
    return {
        'enabled': (db.get_app_setting(conn, 'AUTO_CLEANUP_ENABLED', 'false') or 'false') == 'true',
        'retention_days': int(db.get_app_setting(conn, 'AUTO_CLEANUP_RETENTION_DAYS', str(DEFAULT_RETENTION_DAYS))),
        'run_hour': int(db.get_app_setting(conn, 'AUTO_CLEANUP_HOUR', str(DEFAULT_RUN_HOUR))),
    }


def set_settings(conn, enabled=None, retention_days=None, run_hour=None) -> None:
    if enabled is not None:
        db.set_app_setting(conn, 'AUTO_CLEANUP_ENABLED', 'true' if enabled else 'false')
    if retention_days is not None:
        db.set_app_setting(conn, 'AUTO_CLEANUP_RETENTION_DAYS', str(int(retention_days)))
    if run_hour is not None:
        db.set_app_setting(conn, 'AUTO_CLEANUP_HOUR', str(int(run_hour)))


# ---- background scheduler (mirrors bot/engine.py's _scheduler_loop) -------

_scheduler_started = False
_last_run_date = None


def start_scheduler() -> None:
    global _scheduler_started
    if _scheduler_started:
        return
    _scheduler_started = True
    threading.Thread(target=_scheduler_loop, name='maintenance-sched', daemon=True).start()
    logger.info("Maintenance auto-cleanup scheduler started")


def _scheduler_loop() -> None:
    global _last_run_date
    while True:
        try:
            conn = db.get_connection()
            cfg = get_settings(conn)
            now = datetime.utcnow()
            today = now.date()
            due = cfg['enabled'] and now.hour == cfg['run_hour'] and _last_run_date != today
            if due:
                _last_run_date = today
                logger.info(f"Running scheduled auto-cleanup (retention={cfg['retention_days']}d)...")
                cleanup(conn, cfg['retention_days'], vacuum=True)
            conn.close()
        except Exception as e:
            logger.error(f"Maintenance scheduler error: {e}")
        time.sleep(60)
