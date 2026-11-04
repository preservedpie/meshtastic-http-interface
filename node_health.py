"""Per-node online/offline status, for external uptime monitors (e.g. Uptime
Kuma). A node counts as "online" if it's been heard from (nodes.last_seen,
bumped on every real RX packet — see app.py's _touch_node/_upsert_node) within
the configured offline threshold; otherwise it's "offline". The threshold is
persisted in app_settings so it survives restarts and can be tuned without a
redeploy.
"""

from datetime import datetime, timedelta

import db

DEFAULT_OFFLINE_THRESHOLD_HOURS = 4
_TS_FMT = '%Y-%m-%d %H:%M:%S'


def _parse_ts(ts):
    if not ts:
        return None
    try:
        return datetime.strptime(ts, _TS_FMT)
    except (ValueError, TypeError):
        return None


def get_settings(conn) -> dict:
    return {
        'offline_threshold_hours': float(db.get_app_setting(
            conn, 'HEALTH_OFFLINE_THRESHOLD_HOURS', str(DEFAULT_OFFLINE_THRESHOLD_HOURS)
        )),
    }


def set_settings(conn, offline_threshold_hours=None) -> None:
    if offline_threshold_hours is not None:
        hours = float(offline_threshold_hours)
        if hours <= 0:
            raise ValueError('offline_threshold_hours must be positive')
        db.set_app_setting(conn, 'HEALTH_OFFLINE_THRESHOLD_HOURS', str(hours))


def _node_status(row, now, cutoff):
    last_seen = row['last_seen']
    last_seen_dt = _parse_ts(last_seen)
    online = bool(last_seen_dt and last_seen_dt >= cutoff)
    seconds_since_seen = int((now - last_seen_dt).total_seconds()) if last_seen_dt else None
    return {
        'node_id':            row['node_id'],
        'name':               row['alias'] or row['long_name'] or row['short_name'] or row['node_id'],
        'last_seen':          last_seen,
        'seconds_since_seen': seconds_since_seen,
        'online':             online,
    }


def list_status(conn, threshold_hours=None) -> list:
    if threshold_hours is None:
        threshold_hours = get_settings(conn)['offline_threshold_hours']
    now = datetime.utcnow()
    cutoff = now - timedelta(hours=threshold_hours)
    rows = conn.execute(
        "SELECT node_id, alias, long_name, short_name, last_seen FROM nodes ORDER BY last_seen DESC"
    ).fetchall()
    return [_node_status(r, now, cutoff) for r in rows]


def get_status(conn, node_id, threshold_hours=None):
    """Status for a single node, or None if the node isn't known."""
    if threshold_hours is None:
        threshold_hours = get_settings(conn)['offline_threshold_hours']
    row = conn.execute(
        "SELECT node_id, alias, long_name, short_name, last_seen FROM nodes WHERE node_id = ?",
        (node_id,),
    ).fetchone()
    if not row:
        return None
    now = datetime.utcnow()
    cutoff = now - timedelta(hours=threshold_hours)
    return _node_status(row, now, cutoff)
