import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta

from flask import Flask, abort, jsonify, render_template, request
from flask_cors import CORS

from db import get_connection, init_db
import meshtastic_iface
from meshtastic_iface import (is_available as mesh_available,
                               is_connected as mesh_connected, request_position,
                               request_telemetry, request_traceroute, send_text,
                               get_serial_log, get_local_node_info,
                               get_local_device_info, request_local_telemetry,
                               get_known_nodes, get_device_channels, set_channel_name,
                               set_channel_flags, add_device_channel, delete_device_channel,
                               set_channel_psk)
from rules import evaluate_rules
import weather_forecast
import maintenance
import node_health

from bot_routes import bot_bp
from bot import engine as bot_engine

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

# On flash-storage deployments (the Raspberry Pi), write logs to stdout only —
# Docker/journald captures them, so nothing hits the SD card. Otherwise also
# tee to logs/app.log. Toggle with LOW_DISK_WRITES (see db.py).
_low_disk_writes = os.environ.get('LOW_DISK_WRITES', '0').lower() in ('1', 'true', 'yes')
_log_handlers = [logging.StreamHandler()]
if not _low_disk_writes:
    os.makedirs('logs', exist_ok=True)
    _log_handlers.append(logging.FileHandler('logs/app.log'))
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s %(message)s',
    handlers=_log_handlers,
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# App + DB init
# ---------------------------------------------------------------------------

app = Flask(__name__)
CORS(app)

init_db()

# Initialise the bot engine: opens its own SQLite DB, starts the LLM worker,
# builds the command router. The send_fn closure routes bot replies through
# the same SerialInterface the http-interface uses for outbound packets.
def _bot_send(text, channel, to_node):
    try:
        return meshtastic_iface.send_text(text, channel_index=channel, to_node=to_node)
    except Exception as e:
        logger.error(f"Bot send failed: {e}")
        return None


bot_engine.init(send_fn=_bot_send)
bot_engine.start_workers()
app.register_blueprint(bot_bp)

maintenance.start_scheduler()

# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _json_safe(obj):
    """json.dumps fallback for non-serializable values (e.g. raw protobuf
    payload bytes on routing/unknown packets), so saving never throws
    'Object of type bytes is not JSON serializable'."""
    if isinstance(obj, (bytes, bytearray)):
        try:
            return obj.decode('utf-8', errors='replace')
        except Exception:
            return obj.hex()
    return str(obj)


def _save_packet(packet):
    """Persist a packet and return its packets.id (or None on failure), so
    the caller can later attach post-processing results (rule matches, bot
    replies — see _update_message_flags) to the text_messages row it created."""
    conn = None
    packet_id = None
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO packets
                (topic, packet_type, from_node, sender, channel, rssi, snr, hop_start, hops_away, payload_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                packet.get('topic'),
                packet.get('type'),
                packet.get('from'),
                packet.get('sender'),
                packet.get('channel'),
                packet.get('rssi'),
                packet.get('snr'),
                packet.get('hop_start'),
                packet.get('hops_away'),
                json.dumps(packet.get('raw', {}), default=_json_safe),
            ),
        )
        packet_id = cur.lastrowid

        if packet['type'] in ('nodeinfo', 'map_report'):
            _upsert_node(cur, packet)
        else:
            _touch_node(cur, packet)
        if packet['type'] == 'text':
            _save_message(cur, packet, packet_id)

        conn.commit()
    except Exception as e:
        logger.error(f"DB error saving packet: {e}")
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
    finally:
        if conn:
            conn.close()
    return packet_id


def _update_message_flags(packet_id, rule_names, bot_responded):
    """Record whether a forwarding rule matched and/or the bot replied to an
    incoming text message, keyed by the packets.id set on its text_messages
    row at insert time. Best-effort: called after the message is already
    visible in the UI, so a failure here just means those two columns stay
    NULL for this row rather than losing the message."""
    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute(
            "UPDATE text_messages SET bot_responded = ?, rule_matched = ? WHERE packet_id = ?",
            (1 if bot_responded else 0, ','.join(rule_names) if rule_names else None, packet_id),
        )
        conn.commit()
    except Exception as e:
        logger.error(f"DB error updating message flags: {e}")
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
    finally:
        if conn:
            conn.close()


def _upsert_node(cur, packet):
    payload = packet.get('raw', {}).get('payload', {})
    if not isinstance(payload, dict):
        return
    cur.execute(
        """
        INSERT INTO nodes (node_id, long_name, short_name, hw_model, role, last_seen, last_rssi, last_snr, last_hops,
                            macaddr, public_key, is_licensed, is_unmessagable)
        VALUES (?, ?, ?, ?, ?, datetime('now'), ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(node_id) DO UPDATE SET
            long_name        = COALESCE(excluded.long_name,  long_name),
            short_name       = COALESCE(excluded.short_name, short_name),
            hw_model         = COALESCE(excluded.hw_model,   hw_model),
            role             = COALESCE(excluded.role,       role),
            last_seen        = datetime('now'),
            last_rssi        = COALESCE(excluded.last_rssi, last_rssi),
            last_snr         = COALESCE(excluded.last_snr,  last_snr),
            last_hops        = COALESCE(excluded.last_hops, last_hops),
            macaddr          = COALESCE(excluded.macaddr, macaddr),
            public_key       = COALESCE(excluded.public_key, public_key),
            is_licensed      = COALESCE(excluded.is_licensed, is_licensed),
            is_unmessagable  = COALESCE(excluded.is_unmessagable, is_unmessagable)
        """,
        (
            packet.get('from'),
            payload.get('longname'),
            payload.get('shortname'),
            payload.get('hardware'),
            payload.get('role'),
            packet.get('rssi'),
            packet.get('snr'),
            packet.get('hops_away'),
            payload.get('macaddr'),
            payload.get('public_key'),
            payload.get('is_licensed'),
            payload.get('is_unmessagable'),
        ),
    )


def _touch_node(cur, packet):
    node_id = packet.get('from')
    if not node_id:
        return
    cur.execute(
        """
        INSERT INTO nodes (node_id, last_seen, last_rssi, last_snr, last_hops)
        VALUES (?, datetime('now'), ?, ?, ?)
        ON CONFLICT(node_id) DO UPDATE SET
            last_seen = datetime('now'),
            last_rssi = COALESCE(excluded.last_rssi, last_rssi),
            last_snr  = COALESCE(excluded.last_snr,  last_snr),
            last_hops = COALESCE(excluded.last_hops, last_hops)
        """,
        (node_id, packet.get('rssi'), packet.get('snr'), packet.get('hops_away')),
    )


def _upsert_known_node(cur, n):
    """Enrich a node row from the connected radio's own node database (see
    meshtastic_iface.get_known_nodes()). This is a slow-changing background
    sync, not a live packet, so it never touches last_seen — that stays
    driven by _upsert_node/_touch_node on actual RX."""
    cur.execute(
        """
        INSERT INTO nodes (node_id, node_num, long_name, short_name, hw_model, role,
                            macaddr, public_key, is_licensed, is_unmessagable,
                            last_snr, last_hops, via_mqtt, is_favorite, is_key_manually_verified)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(node_id) DO UPDATE SET
            node_num                 = COALESCE(excluded.node_num, node_num),
            long_name                = COALESCE(excluded.long_name, long_name),
            short_name               = COALESCE(excluded.short_name, short_name),
            hw_model                 = COALESCE(excluded.hw_model, hw_model),
            role                     = COALESCE(excluded.role, role),
            macaddr                  = COALESCE(excluded.macaddr, macaddr),
            public_key               = COALESCE(excluded.public_key, public_key),
            is_licensed              = COALESCE(excluded.is_licensed, is_licensed),
            is_unmessagable          = COALESCE(excluded.is_unmessagable, is_unmessagable),
            last_snr                 = COALESCE(excluded.last_snr, last_snr),
            last_hops                = COALESCE(excluded.last_hops, last_hops),
            via_mqtt                 = COALESCE(excluded.via_mqtt, via_mqtt),
            is_favorite              = COALESCE(excluded.is_favorite, is_favorite),
            is_key_manually_verified = COALESCE(excluded.is_key_manually_verified, is_key_manually_verified)
        """,
        (
            n.get('node_id'),
            n.get('node_num'),
            n.get('long_name'),
            n.get('short_name'),
            n.get('hw_model'),
            n.get('role'),
            n.get('macaddr'),
            n.get('public_key'),
            n.get('is_licensed'),
            n.get('is_unmessagable'),
            n.get('snr'),
            n.get('hops_away'),
            n.get('via_mqtt'),
            n.get('is_favorite'),
            n.get('is_key_manually_verified'),
        ),
    )


def _sync_known_nodes():
    """Pull the connected radio's full node database and enrich our nodes
    table with it — fields like the PKI public key often sit in the radio's
    cache well before/without a fresh NODEINFO packet reaching our handler."""
    try:
        known = get_known_nodes()
    except Exception as e:
        logger.error(f"Node sync failed: {e}")
        return
    if not known:
        return
    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor()
        for n in known:
            if n.get('node_id'):
                _upsert_known_node(cur, n)
        conn.commit()
    except Exception as e:
        logger.error(f"Node sync DB error: {e}")
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
    finally:
        if conn:
            conn.close()


def _node_sync_loop():
    time.sleep(15)  # give the serial connection a moment to establish first
    while True:
        _sync_known_nodes()
        time.sleep(60)


def _get_nodes_for_weather_alerts(max_age_hours: int) -> list:
    """Every known node, plus its latest reported position if one has arrived
    within max_age_hours. Feeds the bot's per-node weather alert scheduler
    (see bot.engine.set_node_positions_fn) — the bot has no direct access to
    this module's DB, so it calls back through this instead.

    Every node is included (lat/lon are None when there's no recent fix) so
    the caller can fall back to a general, fixed-location alert for nodes
    that never report GPS — role/watch-list filtering also happens on the
    caller's side, since that's driven by bot settings this module doesn't
    know about."""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT n.node_id, n.role, n.is_unmessagable, n.last_hops, p.payload_json
            FROM nodes n
            LEFT JOIN (
                SELECT p.from_node, p.payload_json
                FROM packets p
                INNER JOIN (
                    SELECT from_node, MAX(received_at) AS latest
                    FROM packets
                    WHERE packet_type IN ('position', 'map_report')
                      AND received_at >= datetime('now', ?)
                    GROUP BY from_node
                ) latest ON p.from_node = latest.from_node AND p.received_at = latest.latest
                WHERE p.packet_type IN ('position', 'map_report')
            ) p ON p.from_node = n.node_id
            """,
            (f'-{int(max_age_hours)} hours',),
        )
        rows = cur.fetchall()
    finally:
        conn.close()

    out = []
    for row in rows:
        lat = lon = None
        if row['payload_json']:
            try:
                raw = json.loads(row['payload_json']) if isinstance(row['payload_json'], str) else row['payload_json']
                payload = raw.get('payload', {})
                lat_i = payload.get('latitude_i')
                lon_i = payload.get('longitude_i')
                if lat_i is not None and lon_i is not None and not (lat_i == 0 and lon_i == 0):
                    lat, lon = lat_i / 1e7, lon_i / 1e7
            except Exception:
                pass
        out.append({
            'node_id':         row['node_id'],
            'role':            row['role'],
            'is_unmessagable': bool(row['is_unmessagable']),
            'hops_away':       row['last_hops'],
            'lat':             lat,
            'lon':             lon,
        })
    return out


bot_engine.set_node_positions_fn(_get_nodes_for_weather_alerts)


BROADCAST_NODE_ID = '!ffffffff'


def _save_message(cur, packet, packet_id):
    payload = packet.get('raw', {}).get('payload', {})
    text = payload.get('text', '') if isinstance(payload, dict) else ''
    to_raw = packet.get('to')
    to_node = to_raw if to_raw and to_raw.lower() != BROADCAST_NODE_ID else None
    cur.execute(
        """
        INSERT INTO text_messages (from_node, to_node, channel, message, rssi, snr, hops_away, packet_id, direction)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'in')
        """,
        (
            packet.get('from'),
            to_node,
            packet.get('channel'),
            text,
            packet.get('rssi'),
            packet.get('snr'),
            packet.get('hops_away'),
            packet_id,
        ),
    )


def _save_outgoing_message(text, channel, to_node, result):
    """Log a message we sent (broadcast or DM) so it shows up in the Messages
    list alongside received traffic — previously only RX ever got persisted,
    so a sent message vanished into the log with no trace in the UI."""
    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor()
        local = get_local_node_info() or {}
        ack = None
        if to_node:
            if result.get('acked'):
                ack = 1
            elif result.get('ok') is False:
                ack = 0
        cur.execute(
            """
            INSERT INTO text_messages (from_node, to_node, channel, message, direction, ack)
            VALUES (?, ?, ?, ?, 'out', ?)
            """,
            (local.get('node_id'), to_node, None if to_node else channel, text, ack),
        )
        conn.commit()
    except Exception as e:
        logger.error(f"Failed to save outgoing message: {e}")
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
    finally:
        if conn:
            conn.close()


def _load_rules():
    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("SELECT * FROM alert_rules WHERE enabled = 1")
        return [dict(r) for r in cur.fetchall()]
    except Exception as e:
        logger.error(f"DB error loading rules: {e}")
        return []
    finally:
        if conn:
            conn.close()

# ---------------------------------------------------------------------------
# Meshtastic serial listener
# ---------------------------------------------------------------------------

def _handle_packet(packet):
    """Called from the meshtastic serial reader thread for every incoming packet.

    Each stage is isolated: a failure in persistence or alert-rule evaluation
    (e.g. Pushover being unreachable) must never prevent the bot from seeing
    the packet, and vice-versa. Previously a single try/except wrapped all
    three calls in sequence, so any exception in _save_packet or
    evaluate_rules silently swallowed the message before bot_engine ran —
    the message showed up in the web UI but the bot never replied.
    """
    packet_id = None
    try:
        packet_id = _save_packet(packet)
    except Exception as e:
        logger.error(f"Error saving serial packet: {e}", exc_info=True)

    matched_rules = []
    try:
        matched_rules = evaluate_rules(packet, _load_rules()) or []
    except Exception as e:
        logger.error(f"Error evaluating rules for serial packet: {e}", exc_info=True)

    bot_responded = False
    try:
        bot_responded = bool(bot_engine.handle_packet(packet))
    except Exception as e:
        logger.error(f"Error in bot engine for serial packet: {e}", exc_info=True)

    # Only text messages get a text_messages row to attach these to (see
    # _save_message, called from _save_packet for packet['type'] == 'text').
    if packet.get('type') == 'text' and packet_id is not None:
        try:
            _update_message_flags(packet_id, matched_rules, bot_responded)
        except Exception as e:
            logger.error(f"Error updating message flags: {e}", exc_info=True)


_serial_started = False


def _start_serial():
    global _serial_started
    if not _serial_started:
        _serial_started = True
        meshtastic_iface.start(_handle_packet)
        threading.Thread(target=_node_sync_loop, daemon=True, name="node-sync").start()


_start_serial()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_since(since_str):
    """Return an ISO datetime string suitable for SQLite comparison, or None."""
    if not since_str:
        return None
    unit = since_str[-1]
    try:
        amount = int(since_str[:-1])
    except (ValueError, IndexError):
        return None
    if unit == 'h':
        dt = datetime.utcnow() - timedelta(hours=amount)
    elif unit == 'd':
        dt = datetime.utcnow() - timedelta(days=amount)
    else:
        return None
    return dt.strftime('%Y-%m-%d %H:%M:%S')


def _serialize(row):
    if not row:
        return None
    return dict(row) if not isinstance(row, dict) else row


def _safe_int(val, default=0):
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


def _int_arg(name, default, max_value=None):
    """Parse an integer query parameter, falling back to default on garbage
    instead of letting int() raise a 500."""
    try:
        v = int(request.args.get(name, default))
    except (TypeError, ValueError):
        v = default
    if max_value is not None:
        v = min(v, max_value)
    return v

# ---------------------------------------------------------------------------
# Routes — frontend
# ---------------------------------------------------------------------------

@app.route('/')
def index():
    return render_template('index.html')

# ---------------------------------------------------------------------------
# Routes — packets
# ---------------------------------------------------------------------------

@app.route('/api/packets')
def get_packets():
    ptype   = request.args.get('type')
    node    = request.args.get('node')
    channel = request.args.get('channel')
    since   = _parse_since(request.args.get('since'))
    limit   = _int_arg('limit', 100, max_value=500)

    conditions, params = [], []
    if ptype:
        conditions.append("packet_type = ?"); params.append(ptype)
    if node:
        conditions.append("from_node = ?"); params.append(node)
    if channel:
        conditions.append("channel = ?"); params.append(channel)
    if since:
        conditions.append("received_at >= ?"); params.append(since)

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    params.append(limit)

    conn = get_connection()
    cur = conn.cursor()
    cur.execute(f"SELECT * FROM packets {where} ORDER BY received_at DESC LIMIT ?", params)
    rows = [_serialize(r) for r in cur.fetchall()]
    conn.close()
    return jsonify(rows)

# ---------------------------------------------------------------------------
# Routes — nodes
# ---------------------------------------------------------------------------

@app.route('/api/nodes')
def get_nodes():
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM nodes ORDER BY last_seen DESC")
    rows = [_serialize(r) for r in cur.fetchall()]
    conn.close()
    return jsonify(rows)


@app.route('/api/nodes/telemetry')
def get_nodes_telemetry():
    """Latest telemetry payload per node — used by the Nodes tab for battery/temp inline display."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT p.from_node, p.payload_json
        FROM packets p
        INNER JOIN (
            SELECT from_node, MAX(received_at) AS latest
            FROM packets
            WHERE packet_type = 'telemetry'
            GROUP BY from_node
        ) latest ON p.from_node = latest.from_node AND p.received_at = latest.latest
        WHERE p.packet_type = 'telemetry'
    """)
    rows = cur.fetchall()
    conn.close()
    result = {}
    for row in rows:
        try:
            raw = json.loads(row['payload_json']) if isinstance(row['payload_json'], str) else row['payload_json']
            result[row['from_node']] = raw.get('payload', {})
        except Exception:
            pass
    return jsonify(result)


def _normalize_node_id(raw):
    """Accept a node id as decimal ('490325664'), hex ('1d3a14a0'), or already
    '!hex', and return the canonical '!hex' form used for storage. Returns None
    if it can't be parsed."""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    if s.startswith('!'):
        return s.lower()
    try:
        n = int(s)               # decimal (e.g. Homepage node IDs)
    except ValueError:
        try:
            n = int(s, 16)       # bare hex
        except ValueError:
            return None
    return f"!{n:08x}"


@app.route('/api/latest-weather/<node_id>')
def latest_weather(node_id):
    """Latest environment telemetry for a single node, flattened for dashboard
    widgets (e.g. Homepage customapi). Accepts decimal or hex node ids.

    Optional ?unit=f converts temperature from Celsius (as reported by
    Meshtastic) to Fahrenheit so a "°F" suffix is accurate.
    """
    canonical = _normalize_node_id(node_id)
    if canonical is None:
        return jsonify({'error': 'invalid node id'}), 400

    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT payload_json, received_at
        FROM packets
        WHERE packet_type = 'telemetry' AND from_node = ?
        ORDER BY received_at DESC
        LIMIT 1
        """,
        (canonical,),
    )
    row = cur.fetchone()
    conn.close()

    if not row:
        return jsonify({'error': 'no telemetry for node', 'node_id': canonical}), 404

    try:
        raw = json.loads(row['payload_json']) if isinstance(row['payload_json'], str) else row['payload_json']
        payload = raw.get('payload', {}) if isinstance(raw, dict) else {}
    except Exception:
        payload = {}

    result = {
        'node_id':             canonical,
        'temperature':         payload.get('temperature'),
        'relative_humidity':   payload.get('relative_humidity'),
        'barometric_pressure': payload.get('barometric_pressure'),
        'gas_resistance':      payload.get('gas_resistance'),
        'iaq':                 payload.get('iaq'),
        'received_at':         row['received_at'],
    }

    if request.args.get('unit', '').lower() in ('f', 'fahrenheit') and result['temperature'] is not None:
        result['temperature'] = round(result['temperature'] * 9 / 5 + 32, 1)

    return jsonify(result)


@app.route('/api/nodes/<node_id>')
def get_node(node_id):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM nodes WHERE node_id = ?", (node_id,))
    node = cur.fetchone()
    if not node:
        conn.close()
        abort(404)
    cur.execute(
        "SELECT * FROM packets WHERE from_node = ? ORDER BY received_at DESC LIMIT 50",
        (node_id,),
    )
    packets = [_serialize(r) for r in cur.fetchall()]
    conn.close()
    return jsonify({'node': _serialize(node), 'recent_packets': packets})

# ---------------------------------------------------------------------------
# Routes — messages
# ---------------------------------------------------------------------------

MESSAGE_SORT_COLUMNS = {
    'received_at', 'from_node', 'to_node', 'channel',
    'rssi', 'snr', 'hops_away', 'direction', 'message',
    'bot_responded', 'rule_matched',
}


@app.route('/api/messages')
def get_messages():
    node      = request.args.get('node')
    channel   = request.args.get('channel')
    since     = _parse_since(request.args.get('since'))
    direction = request.args.get('direction')  # 'in' | 'out'
    q         = request.args.get('q')
    sort      = request.args.get('sort') or 'received_at'
    order     = request.args.get('order') or 'desc'
    limit     = _int_arg('limit', 100, max_value=500)

    conditions, params = [], []
    if node:
        # Either side of the conversation — a DM's from_node/to_node depend
        # on which end sent it, so "filter by node" should match both.
        conditions.append("(from_node = ? OR to_node = ?)"); params.extend([node, node])
    if channel:
        conditions.append("channel = ?"); params.append(channel)
    if since:
        conditions.append("received_at >= ?"); params.append(since)
    if direction in ('in', 'out'):
        conditions.append("direction = ?"); params.append(direction)
    if q:
        conditions.append("message LIKE ?"); params.append(f"%{q}%")

    if sort not in MESSAGE_SORT_COLUMNS:
        sort = 'received_at'
    order_sql = 'ASC' if str(order).lower() == 'asc' else 'DESC'

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    params.append(limit)

    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        f"SELECT * FROM text_messages {where} ORDER BY {sort} {order_sql}, id {order_sql} LIMIT ?",
        params,
    )
    rows = [_serialize(r) for r in cur.fetchall()]
    conn.close()
    return jsonify(rows)

# ---------------------------------------------------------------------------
# Routes — stats
# ---------------------------------------------------------------------------

@app.route('/api/stats')
def get_stats():
    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        "SELECT packet_type, COUNT(*) as count FROM packets "
        "WHERE received_at >= datetime('now', '-24 hours') "
        "GROUP BY packet_type ORDER BY count DESC"
    )
    by_type = [_serialize(r) for r in cur.fetchall()]

    cur.execute(
        "SELECT COUNT(*) as count FROM nodes WHERE last_seen >= datetime('now', '-24 hours')"
    )
    active_nodes = cur.fetchone()['count']

    cur.execute(
        "SELECT COUNT(*) as count FROM packets WHERE received_at >= datetime('now', '-1 hours')"
    )
    packets_1h = cur.fetchone()['count']

    cur.execute(
        "SELECT COUNT(*) as count FROM packets WHERE received_at >= datetime('now', '-24 hours')"
    )
    packets_24h = cur.fetchone()['count']

    cur.execute("SELECT received_at FROM packets ORDER BY received_at DESC LIMIT 1")
    last_pkt = cur.fetchone()

    conn.close()
    return jsonify({
        'by_type_24h':    by_type,
        'active_nodes_24h': active_nodes,
        'packets_1h':     packets_1h,
        'packets_24h':    packets_24h,
        'last_packet_at': last_pkt['received_at'] if last_pkt else None,
    })

# ---------------------------------------------------------------------------
# Routes — alert rules (CRUD)
# ---------------------------------------------------------------------------

@app.route('/api/rules', methods=['GET'])
def get_rules():
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM alert_rules ORDER BY id")
    rows = [_serialize(r) for r in cur.fetchall()]
    conn.close()
    return jsonify(rows)


@app.route('/api/rules', methods=['POST'])
def create_rule():
    data = request.get_json(force=True)
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO alert_rules (name, enabled, packet_type, from_node, channel, keyword, priority, sound)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            data.get('name', 'Unnamed rule'),
            1 if data.get('enabled', True) else 0,
            data.get('packet_type') or None,
            data.get('from_node') or None,
            data.get('channel') or None,
            data.get('keyword') or None,
            _safe_int(data.get('priority', 0)),
            data.get('sound', 'pushover'),
        ),
    )
    rule_id = cur.lastrowid
    conn.commit()
    cur.execute("SELECT * FROM alert_rules WHERE id = ?", (rule_id,))
    rule = _serialize(cur.fetchone())
    conn.close()
    return jsonify(rule), 201


@app.route('/api/rules/<int:rule_id>', methods=['PUT'])
def update_rule(rule_id):
    data = request.get_json(force=True)
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE alert_rules
        SET name=?, enabled=?, packet_type=?, from_node=?,
            channel=?, keyword=?, priority=?, sound=?
        WHERE id=?
        """,
        (
            data.get('name', 'Unnamed rule'),
            1 if data.get('enabled', True) else 0,
            data.get('packet_type') or None,
            data.get('from_node') or None,
            data.get('channel') or None,
            data.get('keyword') or None,
            _safe_int(data.get('priority', 0)),
            data.get('sound', 'pushover'),
            rule_id,
        ),
    )
    conn.commit()
    cur.execute("SELECT * FROM alert_rules WHERE id = ?", (rule_id,))
    rule = cur.fetchone()
    conn.close()
    if not rule:
        abort(404)
    return jsonify(_serialize(rule))


@app.route('/api/rules/<int:rule_id>', methods=['DELETE'])
def delete_rule(rule_id):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM alert_rules WHERE id = ?", (rule_id,))
    conn.commit()
    conn.close()
    return '', 204


@app.route('/api/rules/<int:rule_id>/toggle', methods=['PATCH'])
def toggle_rule(rule_id):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("UPDATE alert_rules SET enabled = NOT enabled WHERE id = ?", (rule_id,))
    conn.commit()
    cur.execute("SELECT * FROM alert_rules WHERE id = ?", (rule_id,))
    rule = cur.fetchone()
    conn.close()
    if not rule:
        abort(404)
    return jsonify(_serialize(rule))

# ---------------------------------------------------------------------------
# Routes — e-ink display summary
# ---------------------------------------------------------------------------

@app.route('/api/display/summary')
def display_summary():
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("SELECT * FROM text_messages ORDER BY received_at DESC LIMIT 1")
    last_msg = cur.fetchone()

    cur.execute(
        "SELECT COUNT(*) as count FROM nodes WHERE last_seen >= datetime('now', '-24 hours')"
    )
    active_nodes = cur.fetchone()['count']

    cur.execute("SELECT received_at FROM packets ORDER BY received_at DESC LIMIT 1")
    last_pkt = cur.fetchone()

    cur.execute(
        "SELECT COUNT(*) as count FROM packets WHERE received_at >= datetime('now', '-1 hours')"
    )
    packets_1h = cur.fetchone()['count']

    conn.close()

    def _ago(ts):
        if not ts:
            return 'never'
        try:
            dt = datetime.strptime(ts, '%Y-%m-%d %H:%M:%S')
        except (ValueError, TypeError):
            return '?'
        s = int((datetime.utcnow() - dt).total_seconds())
        if s < 60:   return f"{s}s"
        if s < 3600: return f"{s // 60}m"
        return f"{s // 3600}h"

    return jsonify({
        'last_message': {
            'from': last_msg['from_node'],
            'text': last_msg['message'],
            'ago':  _ago(last_msg['received_at']),
        } if last_msg else None,
        'active_nodes':    active_nodes,
        'last_packet_ago': _ago(last_pkt['received_at']) if last_pkt else 'never',
        'packet_count_1h': packets_1h,
    })

# ---------------------------------------------------------------------------
# Routes — map
# ---------------------------------------------------------------------------

@app.route('/api/map')
def get_map_data():
    conn = get_connection()
    cur  = conn.cursor()

    # Latest position per node (position packets and map_report both carry coordinates)
    cur.execute("""
        SELECT p.from_node, p.received_at, p.rssi, p.snr, p.hops_away, p.payload_json
        FROM packets p
        INNER JOIN (
            SELECT from_node, MAX(received_at) AS latest
            FROM packets
            WHERE packet_type IN ('position', 'map_report')
            GROUP BY from_node
        ) latest ON p.from_node = latest.from_node AND p.received_at = latest.latest
        WHERE p.packet_type IN ('position', 'map_report')
    """)
    position_rows = cur.fetchall()

    # Latest temperature per node (from telemetry packets)
    cur.execute("""
        SELECT p.from_node, p.payload_json
        FROM packets p
        INNER JOIN (
            SELECT from_node, MAX(received_at) AS latest
            FROM packets
            WHERE packet_type = 'telemetry'
            GROUP BY from_node
        ) latest ON p.from_node = latest.from_node AND p.received_at = latest.latest
        WHERE p.packet_type = 'telemetry'
    """)
    telemetry_rows = cur.fetchall()

    # Node names
    cur.execute("SELECT node_id, long_name, short_name FROM nodes")
    node_names = {r['node_id']: dict(r) for r in cur.fetchall()}

    conn.close()

    # Build temperature lookup
    temps = {}
    for row in telemetry_rows:
        try:
            raw = json.loads(row['payload_json']) if isinstance(row['payload_json'], str) else row['payload_json']
            temp = raw.get('payload', {}).get('temperature')
            if temp is not None:
                temps[row['from_node']] = round(temp, 1)
        except Exception:
            pass

    results = []
    for row in position_rows:
        try:
            raw = json.loads(row['payload_json']) if isinstance(row['payload_json'], str) else row['payload_json']
            payload = raw.get('payload', {})
            lat_i = payload.get('latitude_i')
            lon_i = payload.get('longitude_i')
            if lat_i is None or lon_i is None or (lat_i == 0 and lon_i == 0):
                continue
            node_id  = row['from_node']
            node_info = node_names.get(node_id, {})
            results.append({
                'node_id':    node_id,
                'long_name':  node_info.get('long_name') if node_info else None,
                'short_name': node_info.get('short_name') if node_info else None,
                'lat':        lat_i / 1e7,
                'lon':        lon_i / 1e7,
                'altitude':   payload.get('altitude'),
                'rssi':       row['rssi'],
                'snr':        row['snr'],
                'hops_away':  row['hops_away'],
                'received_at': row['received_at'],
                'temperature': temps.get(node_id),
            })
        except Exception:
            pass

    return jsonify(results)


# ---------------------------------------------------------------------------
# Routes — telemetry time-series (for the Graphs tab)
# ---------------------------------------------------------------------------

# Metrics carried inside a telemetry packet's payload JSON.
_TELEMETRY_METRICS = {
    'temperature', 'relative_humidity', 'barometric_pressure',
    'battery_level', 'voltage', 'channel_utilization', 'air_util_tx',
    'iaq', 'gas_resistance',
}
# Metrics that live on the packet row itself (present on most packet types).
_SIGNAL_METRICS = {'rssi', 'snr', 'hops_away'}


@app.route('/api/telemetry/metrics')
def telemetry_metrics():
    """Return which metrics actually have data for a node, so the UI only
    offers populated charts. Requires ?node=<id>."""
    node = request.args.get('node')
    if not node:
        return jsonify({'error': 'node is required'}), 400

    conn = get_connection()
    cur = conn.cursor()

    available = set()
    # Signal metrics: any non-null column across this node's packets.
    cur.execute(
        "SELECT MAX(rssi IS NOT NULL), MAX(snr IS NOT NULL), "
        "MAX(hops_away IS NOT NULL) FROM packets WHERE from_node = ?",
        (node,),
    )
    has_rssi, has_snr, has_hops = cur.fetchone()
    if has_rssi:  available.add('rssi')
    if has_snr:   available.add('snr')
    if has_hops:  available.add('hops_away')

    # Telemetry metrics: scan recent telemetry payloads for present keys.
    cur.execute(
        "SELECT payload_json FROM packets "
        "WHERE from_node = ? AND packet_type = 'telemetry' "
        "ORDER BY received_at DESC LIMIT 200",
        (node,),
    )
    for (payload_json,) in cur.fetchall():
        try:
            raw = json.loads(payload_json) if isinstance(payload_json, str) else payload_json
            payload = raw.get('payload', {}) if isinstance(raw, dict) else {}
            for k in _TELEMETRY_METRICS:
                if payload.get(k) is not None:
                    available.add(k)
        except Exception:
            pass
    conn.close()
    return jsonify(sorted(available))


@app.route('/api/telemetry/series')
def telemetry_series():
    """Time-series for one node + one metric: [{t, v}, ...] oldest-first.
    Query: ?node=<id>&metric=<m>&since=<24h|7d|all>"""
    node   = request.args.get('node')
    metric = request.args.get('metric')
    if not node or not metric:
        return jsonify({'error': 'node and metric are required'}), 400
    if metric not in _TELEMETRY_METRICS and metric not in _SIGNAL_METRICS:
        return jsonify({'error': f'unknown metric {metric!r}'}), 400

    since_arg = request.args.get('since', '24h')
    since = None if since_arg.lower() == 'all' else _parse_since(since_arg)

    conn = get_connection()
    cur = conn.cursor()

    if metric in _SIGNAL_METRICS:
        # Read straight off the packet column across all packet types.
        conditions = [f"{metric} IS NOT NULL", "from_node = ?"]
        params = [node]
        if since:
            conditions.append("received_at >= ?"); params.append(since)
        where = " AND ".join(conditions)
        cur.execute(
            f"SELECT received_at, {metric} FROM packets "
            f"WHERE {where} ORDER BY received_at ASC",
            params,
        )
        series = [{'t': r['received_at'], 'v': r[metric]} for r in cur.fetchall()]
        conn.close()
        return jsonify(series)

    # Telemetry metric: extract from payload JSON.
    conditions = ["packet_type = 'telemetry'", "from_node = ?"]
    params = [node]
    if since:
        conditions.append("received_at >= ?"); params.append(since)
    where = " AND ".join(conditions)
    cur.execute(
        f"SELECT received_at, payload_json FROM packets "
        f"WHERE {where} ORDER BY received_at ASC",
        params,
    )
    rows = cur.fetchall()
    conn.close()

    series = []
    for row in rows:
        try:
            raw = json.loads(row['payload_json']) if isinstance(row['payload_json'], str) else row['payload_json']
            payload = raw.get('payload', {}) if isinstance(raw, dict) else {}
            v = payload.get(metric)
            if v is not None:
                series.append({'t': row['received_at'], 'v': v})
        except Exception:
            pass
    return jsonify(series)


# ---------------------------------------------------------------------------
# Routes — node alias (user-assigned friendly name)
# ---------------------------------------------------------------------------

@app.route('/api/nodes/<node_id>/alias', methods=['PATCH'])
def set_node_alias(node_id):
    data = request.get_json(force=True) or {}
    alias = (data.get('alias') or '').strip() or None
    conn = get_connection()
    cur = conn.cursor()
    # The node may not exist yet if we've only heard it indirectly; upsert it.
    cur.execute(
        "INSERT INTO nodes (node_id, alias) VALUES (?, ?) "
        "ON CONFLICT(node_id) DO UPDATE SET alias = excluded.alias",
        (node_id, alias),
    )
    conn.commit()
    cur.execute("SELECT * FROM nodes WHERE node_id = ?", (node_id,))
    row = cur.fetchone()
    conn.close()
    return jsonify(_serialize(row))


# ---------------------------------------------------------------------------
# Routes — report builder (nodes × metrics × range → summary stats)
# ---------------------------------------------------------------------------

def _summarize(values):
    """min / max / avg / last / count for a list of numbers."""
    if not values:
        return None
    return {
        'count': len(values),
        'min':   round(min(values), 2),
        'max':   round(max(values), 2),
        'avg':   round(sum(values) / len(values), 2),
        'last':  round(values[-1], 2),
    }


@app.route('/api/report')
def build_report():
    """Summary report for a set of nodes × metrics over a time window.
    Query: ?nodes=a,b,c&metrics=temperature,rssi&since=24h
    Returns per-node, per-metric stats plus a downsampled series for charting."""
    node_ids = [n for n in (request.args.get('nodes', '').split(',')) if n.strip()]
    metrics  = [m for m in (request.args.get('metrics', '').split(',')) if m.strip()]
    if not node_ids or not metrics:
        return jsonify({'error': 'nodes and metrics are required'}), 400

    since_arg = request.args.get('since', '24h')
    since = None if since_arg.lower() == 'all' else _parse_since(since_arg)

    conn = get_connection()
    cur = conn.cursor()

    # Resolve display names (alias > long_name > short_name > id).
    cur.execute("SELECT node_id, alias, long_name, short_name FROM nodes")
    names = {r['node_id']: dict(r) for r in cur.fetchall()}

    def display_name(nid):
        n = names.get(nid, {})
        return n.get('alias') or n.get('long_name') or n.get('short_name') or nid

    report = {
        'generated_at': datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S') + 'Z',
        'since': since_arg,
        'nodes': [],
    }

    for nid in node_ids:
        node_entry = {'node_id': nid, 'name': display_name(nid), 'metrics': {}}
        for metric in metrics:
            series = _report_series(cur, nid, metric, since)
            node_entry['metrics'][metric] = {
                'summary': _summarize([p['v'] for p in series]),
                'series': _downsample(series, 120),
            }
        report['nodes'].append(node_entry)

    conn.close()
    return jsonify(report)


def _report_series(cur, node_id, metric, since):
    """Reuse the same extraction the /api/telemetry/series endpoint uses."""
    if metric in _SIGNAL_METRICS:
        conditions = [f"{metric} IS NOT NULL", "from_node = ?"]
        params = [node_id]
        if since:
            conditions.append("received_at >= ?"); params.append(since)
        cur.execute(
            f"SELECT received_at, {metric} AS v FROM packets "
            f"WHERE {' AND '.join(conditions)} ORDER BY received_at ASC",
            params,
        )
        return [{'t': r['received_at'], 'v': r['v']} for r in cur.fetchall()]

    if metric not in _TELEMETRY_METRICS:
        return []

    conditions = ["packet_type = 'telemetry'", "from_node = ?"]
    params = [node_id]
    if since:
        conditions.append("received_at >= ?"); params.append(since)
    cur.execute(
        f"SELECT received_at, payload_json FROM packets "
        f"WHERE {' AND '.join(conditions)} ORDER BY received_at ASC",
        params,
    )
    out = []
    for row in cur.fetchall():
        try:
            raw = json.loads(row['payload_json']) if isinstance(row['payload_json'], str) else row['payload_json']
            payload = raw.get('payload', {}) if isinstance(raw, dict) else {}
            v = payload.get(metric)
            if v is not None:
                out.append({'t': row['received_at'], 'v': v})
        except Exception:
            pass
    return out


def _downsample(series, max_points):
    """Evenly thin a series to at most max_points, keeping first and last."""
    n = len(series)
    if n <= max_points:
        return series
    step = n / max_points
    out = [series[int(i * step)] for i in range(max_points)]
    out[-1] = series[-1]
    return out


# ---------------------------------------------------------------------------
# Routes — node requests (telemetry, position, traceroute)
# ---------------------------------------------------------------------------

@app.route('/api/nodes/<node_id>/request/telemetry', methods=['POST'])
def req_telemetry(node_id):
    if not mesh_available():
        return jsonify({'error': 'MESHTASTIC_SERIAL_PORT not configured'}), 503
    try:
        return jsonify(request_telemetry(node_id))
    except Exception as e:
        logger.error(f"Telemetry request failed: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/nodes/<node_id>/request/position', methods=['POST'])
def req_position(node_id):
    if not mesh_available():
        return jsonify({'error': 'MESHTASTIC_SERIAL_PORT not configured'}), 503
    try:
        return jsonify(request_position(node_id))
    except Exception as e:
        logger.error(f"Position request failed: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/nodes/<node_id>/request/traceroute', methods=['POST'])
def req_traceroute(node_id):
    if not mesh_available():
        return jsonify({'error': 'MESHTASTIC_SERIAL_PORT not configured'}), 503
    try:
        return jsonify(request_traceroute(node_id))
    except Exception as e:
        logger.error(f"Traceroute request failed: {e}")
        return jsonify({'error': str(e)}), 500


# ---------------------------------------------------------------------------
# Routes — local node info (the radio plugged into this host)
# ---------------------------------------------------------------------------

_ENV_KEYS    = ('temperature', 'relative_humidity', 'barometric_pressure',
                'gas_resistance', 'iaq')
_DEVICE_KEYS = ('battery_level', 'voltage', 'channel_utilization',
                'air_util_tx', 'uptime_seconds')


@app.route('/api/local/info')
def local_info():
    """Identity + device + environment data for the locally connected radio.

    Live radio data (node DB) is preferred; environment readings usually only
    exist in the packets table (stored when the radio answers a telemetry
    request — see POST /api/local/request/telemetry), so backfill from there.
    """
    info = get_local_device_info()
    if not isinstance(info, dict):
        info = {}
    node_id = info.get('node_id')

    device = {k: v for k, v in (info.get('device_metrics') or {}).items() if v is not None}
    env    = {k: v for k, v in (info.get('environment_metrics') or {}).items() if v is not None}
    env_source, env_at = ('radio', None) if env.get('temperature') is not None else (None, None)

    if node_id:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT payload_json, received_at FROM packets "
            "WHERE packet_type = 'telemetry' AND from_node = ? "
            "ORDER BY received_at DESC LIMIT 10",
            (node_id,),
        )
        rows = cur.fetchall()
        conn.close()
        # Telemetry packets carry either device or environment metrics, so scan
        # recent rows and take the newest value for each missing key.
        for row in rows:
            try:
                raw = json.loads(row['payload_json']) if isinstance(row['payload_json'], str) else row['payload_json']
                payload = raw.get('payload', {}) if isinstance(raw, dict) else {}
            except Exception:
                continue
            if env_source is None and payload.get('temperature') is not None:
                for k in _ENV_KEYS:
                    if payload.get(k) is not None:
                        env[k] = payload[k]
                env_source, env_at = 'history', row['received_at']
            for k in _DEVICE_KEYS:
                if k not in device and payload.get(k) is not None:
                    device[k] = payload[k]

    return jsonify({
        'connected':   mesh_connected(),
        'serial_port': os.environ.get('MESHTASTIC_SERIAL_PORT', '/dev/ttyACM1'),
        'node': {
            'node_id':          node_id,
            'node_num':         info.get('node_num'),
            'long_name':        info.get('long_name'),
            'short_name':       info.get('short_name'),
            'hw_model':         info.get('hw_model'),
            'firmware_version': info.get('firmware_version'),
            'last_heard':       info.get('last_heard'),
        } if node_id else None,
        'device_metrics': device,
        'environment': env or None,
        'environment_source': env_source,           # 'radio' | 'history' | None
        'environment_received_at': env_at,          # set when source == 'history'
        'error': info.get('error'),
    })


_TELEMETRY_REQUEST_TYPES = ('environment_metrics', 'device_metrics',
                            'air_quality_metrics', 'power_metrics')


@app.route('/api/local/request/telemetry', methods=['POST'])
def req_local_telemetry():
    """Ask the local radio for a fresh reading. The response arrives via the
    serial receive path within a few seconds and is stored like any telemetry
    packet, so a follow-up GET /api/local/info picks it up."""
    if not mesh_available():
        return jsonify({'error': 'MESHTASTIC_SERIAL_PORT not configured'}), 503
    data = request.get_json(silent=True) or {}
    ttype = data.get('type', 'environment_metrics')
    if ttype not in _TELEMETRY_REQUEST_TYPES:
        return jsonify({'error': f'type must be one of {_TELEMETRY_REQUEST_TYPES}'}), 400
    try:
        return jsonify(request_local_telemetry(ttype))
    except Exception as e:
        logger.error(f"Local telemetry request failed: {e}")
        return jsonify({'error': str(e)}), 500


# ---------------------------------------------------------------------------
# Routes — device (LoRa) channels
#
# These read/write the connected radio's own channel table over the admin
# protocol — not app-local metadata. A rename is low-risk, but adding,
# deleting, or (implicitly) anything touching a channel's PSK changes what
# this radio can actually talk to, so every write here is deliberately a
# separate, explicit call rather than a bulk "save all channels" endpoint.
# ---------------------------------------------------------------------------

@app.route('/api/device/channels')
def get_channels():
    if not mesh_available():
        return jsonify({'error': 'MESHTASTIC_SERIAL_PORT not configured'}), 503
    try:
        return jsonify(get_device_channels())
    except Exception as e:
        logger.error(f"Failed to read device channels: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/device/channels', methods=['POST'])
def add_channel():
    if not mesh_available():
        return jsonify({'error': 'MESHTASTIC_SERIAL_PORT not configured'}), 503
    data = request.get_json(force=True) or {}
    try:
        result = add_device_channel(data.get('name', ''), data.get('psk') or None)
        return jsonify(result), 201
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        logger.error(f"Failed to add device channel: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/device/channels/<int:index>', methods=['PATCH'])
def update_channel(index):
    if not mesh_available():
        return jsonify({'error': 'MESHTASTIC_SERIAL_PORT not configured'}), 503
    data = request.get_json(force=True) or {}
    try:
        if 'name' in data:
            set_channel_name(index, data.get('name', ''))
        if 'uplink_enabled' in data or 'downlink_enabled' in data:
            set_channel_flags(
                index,
                uplink_enabled=data.get('uplink_enabled'),
                downlink_enabled=data.get('downlink_enabled'),
            )
        return jsonify({'ok': True})
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        logger.error(f"Failed to update device channel {index}: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/device/channels/<int:index>', methods=['DELETE'])
def delete_channel(index):
    if not mesh_available():
        return jsonify({'error': 'MESHTASTIC_SERIAL_PORT not configured'}), 503
    try:
        delete_device_channel(index)
        return jsonify({'ok': True})
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        logger.error(f"Failed to delete device channel {index}: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/device/channels/<int:index>/psk', methods=['POST'])
def replace_channel_psk(index):
    """Replace a channel's encryption key — a dedicated endpoint (rather than
    folding this into PATCH) because it's the riskiest of these writes: get
    it wrong and every other node sharing the channel loses it. The current
    key is never read or returned; supply 'psk' to set a specific key
    (base64 or 0x-hex, as pasted from the official app) or omit it to
    generate a fresh random one."""
    if not mesh_available():
        return jsonify({'error': 'MESHTASTIC_SERIAL_PORT not configured'}), 503
    data = request.get_json(silent=True) or {}
    try:
        set_channel_psk(index, data.get('psk') or None)
        return jsonify({'ok': True})
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        logger.error(f"Failed to replace key for device channel {index}: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/status')
def get_status():
    local_node = get_local_node_info()

    # Bot identity diagnostics: the bot only treats a message as a DM when the
    # packet's "to" matches its own node number. Surface the configured number,
    # the radio's actual local node, and whether they agree — a mismatch is the
    # usual cause of "DM replied on a channel instead of back to me".
    bot_num = bot_engine.bot_node_number()
    bot_hex = f"!{bot_num:08x}" if bot_num else None
    local_hex = local_node.get('node_id') if isinstance(local_node, dict) else None
    bot_node = {
        'configured': bot_engine.bot_node_number_configured(),
        'effective_number': bot_num,
        'effective_hex': bot_hex,
        'matches_local': bool(bot_hex and local_hex and bot_hex == local_hex),
    }

    return jsonify({
        'serial_connected': mesh_connected(),
        'serial_port':      os.environ.get('MESHTASTIC_SERIAL_PORT', '/dev/ttyACM1'),
        'mesh_available':   mesh_available(),
        'local_node':       local_node,
        'bot_node':         bot_node,
        'watchdog':         meshtastic_iface.get_watchdog_status(),
    })


@app.route('/api/health/serial')
def health_serial():
    """Serial-link health for external monitors (Uptime Kuma, Docker healthcheck).

    mesh_connected() alone isn't enough — it only reflects the meshtastic
    library's own connection.lost event, which doesn't reliably fire on a
    silent serial/USB hang. This also checks the independent no-data watchdog
    (meshtastic_iface.get_watchdog_status) so a frozen-but-'connected' link
    still reports unhealthy. Returns 200 while healthy, 503 once stale."""
    wd = meshtastic_iface.get_watchdog_status()
    connected = mesh_connected()
    ok = connected and not wd['stale']
    return jsonify({'connected': connected, **wd}), 200 if ok else 503


@app.route('/api/serial/log')
def serial_log():
    limit = _int_arg('limit', 200, max_value=500)
    return jsonify(get_serial_log(limit))


@app.route('/api/map/trail')
def get_map_trail():
    """All position points per node within a time window, oldest-first, for drawing trails."""
    since_arg = request.args.get('since')
    # 'all' (or any unparseable value) means no time filter — return every point.
    since = None if (since_arg is None or since_arg.lower() == 'all') else _parse_since(since_arg)

    conn = get_connection()
    cur  = conn.cursor()

    conditions = ["packet_type IN ('position', 'map_report')"]
    params = []
    if since:
        conditions.append("received_at >= ?")
        params.append(since)

    where = "WHERE " + " AND ".join(conditions)
    cur.execute(
        f"SELECT from_node, received_at, rssi, snr, hops_away, payload_json "
        f"FROM packets {where} ORDER BY from_node, received_at ASC",
        params,
    )
    rows = cur.fetchall()

    # Node names
    cur.execute("SELECT node_id, long_name, short_name FROM nodes")
    node_names = {r['node_id']: dict(r) for r in cur.fetchall()}
    conn.close()

    # Group points by node
    by_node = {}
    for row in rows:
        try:
            raw = json.loads(row['payload_json']) if isinstance(row['payload_json'], str) else row['payload_json']
            payload = raw.get('payload', {})
            lat_i = payload.get('latitude_i')
            lon_i = payload.get('longitude_i')
            if lat_i is None or lon_i is None or (lat_i == 0 and lon_i == 0):
                continue
            node_id = row['from_node']
            if node_id not in by_node:
                info = node_names.get(node_id, {})
                by_node[node_id] = {
                    'node_id':    node_id,
                    'long_name':  info.get('long_name') if info else None,
                    'short_name': info.get('short_name') if info else None,
                    'points': [],
                }
            by_node[node_id]['points'].append({
                'lat':         lat_i / 1e7,
                'lon':         lon_i / 1e7,
                'altitude':    payload.get('altitude'),
                'rssi':        row['rssi'],
                'snr':         row['snr'],
                'hops_away':   row['hops_away'],
                'received_at': row['received_at'],
            })
        except Exception:
            pass

    return jsonify(list(by_node.values()))


# ---------------------------------------------------------------------------
# Routes — send message
# ---------------------------------------------------------------------------

@app.route('/api/send', methods=['POST'])
def send_message():
    data    = request.get_json(force=True)
    text    = (data.get('text') or '').strip()
    try:
        channel = int(data.get('channel') or 0)
    except (TypeError, ValueError):
        return jsonify({'error': 'channel must be an integer'}), 400

    if not text:
        return jsonify({'error': 'text is required'}), 400
    if len(text) > 200:
        return jsonify({'error': 'text exceeds 200 character limit'}), 400

    to_node = None
    to_raw  = data.get('to')
    if to_raw:
        to_node = _normalize_node_id(to_raw)
        if not to_node:
            return jsonify({'error': f'invalid destination node id: {to_raw!r}'}), 400

    try:
        result = send_text(text, channel, to_node=to_node)
        if to_node:
            logger.info(f"Sent DM to {to_node}: {text!r}")
        else:
            logger.info(f"Sent message ch={channel}: {text!r}")
        _save_outgoing_message(text, channel, to_node, result)
        return jsonify(result)
    except Exception as e:
        logger.error(f"Serial send failed: {e}")
        return jsonify({'error': str(e)}), 503


# ---------------------------------------------------------------------------
# Routes — weather forecast (off-grid, from locally stored sensor telemetry)
# ---------------------------------------------------------------------------

@app.route('/api/weather/config')
def weather_config_get():
    conn = get_connection()
    cfg = weather_forecast.get_config(conn)
    conn.close()
    return jsonify({'metrics': list(weather_forecast.METRICS), 'config': cfg})


@app.route('/api/weather/config', methods=['PUT'])
def weather_config_set():
    data = request.get_json(force=True) or {}
    metric = data.get('metric')
    node_ids = data.get('node_ids')
    if metric not in weather_forecast.METRICS or not isinstance(node_ids, list):
        return jsonify({'error': 'metric (one of the known weather metrics) and node_ids (list) are required'}), 400
    conn = get_connection()
    try:
        weather_forecast.set_config(conn, metric, node_ids)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    finally:
        conn.close()
    return jsonify({'ok': True})


@app.route('/api/weather/forecast')
def weather_forecast_get():
    conn = get_connection()
    forecast = weather_forecast.build_forecast(conn)
    conn.close()
    return jsonify(forecast)


# ---------------------------------------------------------------------------
# Routes — maintenance (storage stats + manual/auto cleanup)
# ---------------------------------------------------------------------------

@app.route('/api/maintenance/stats')
def maintenance_stats():
    return jsonify(maintenance.get_stats())


@app.route('/api/maintenance/config')
def maintenance_config_get():
    conn = get_connection()
    cfg = maintenance.get_settings(conn)
    conn.close()
    return jsonify(cfg)


@app.route('/api/maintenance/config', methods=['PUT'])
def maintenance_config_set():
    data = request.get_json(force=True) or {}
    conn = get_connection()
    try:
        maintenance.set_settings(
            conn,
            enabled=data.get('enabled'),
            retention_days=data.get('retention_days'),
            run_hour=data.get('run_hour'),
        )
    except (TypeError, ValueError) as e:
        return jsonify({'error': str(e)}), 400
    finally:
        conn.close()
    conn = get_connection()
    cfg = maintenance.get_settings(conn)
    conn.close()
    return jsonify(cfg)


@app.route('/api/maintenance/cleanup', methods=['POST'])
def maintenance_cleanup():
    data = request.get_json(force=True) or {}
    try:
        retention_days = int(data.get('retention_days'))
        if retention_days < 1:
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({'error': 'retention_days must be a positive integer'}), 400
    vacuum = bool(data.get('vacuum'))

    conn = get_connection()
    result = maintenance.cleanup(conn, retention_days, vacuum=vacuum)
    conn.close()
    logger.info(f"Manual cleanup via API: {result}")
    return jsonify(result)


# ---------------------------------------------------------------------------
# Routes — node health (Uptime Kuma / external monitor integration)
# ---------------------------------------------------------------------------

@app.route('/api/health/config', methods=['GET'])
def health_config_get():
    conn = get_connection()
    cfg = node_health.get_settings(conn)
    conn.close()
    return jsonify(cfg)


@app.route('/api/health/config', methods=['PUT'])
def health_config_set():
    data = request.get_json(force=True) or {}
    conn = get_connection()
    try:
        node_health.set_settings(conn, offline_threshold_hours=data.get('offline_threshold_hours'))
    except (TypeError, ValueError) as e:
        conn.close()
        return jsonify({'error': str(e)}), 400
    cfg = node_health.get_settings(conn)
    conn.close()
    return jsonify(cfg)


@app.route('/api/health/nodes')
def health_nodes():
    """All nodes with online/offline status, for a dashboard tile or a single
    Uptime Kuma 'HTTP(s) - Json Query' monitor. Optional ?hours= overrides
    the configured offline threshold for a one-off check."""
    hours_arg = request.args.get('hours')
    conn = get_connection()
    try:
        threshold = float(hours_arg) if hours_arg else node_health.get_settings(conn)['offline_threshold_hours']
        nodes = node_health.list_status(conn, threshold)
    finally:
        conn.close()
    online = sum(1 for n in nodes if n['online'])
    return jsonify({
        'threshold_hours': threshold,
        'total':   len(nodes),
        'online':  online,
        'offline': len(nodes) - online,
        'nodes':   nodes,
    })


@app.route('/api/health/nodes/<node_id>')
def health_node_one(node_id):
    """Single-node health check for Uptime Kuma's plain HTTP(s) monitor type
    — paste this URL in, nothing else to configure. Returns 200 while the
    node has been heard from within the offline threshold (default 4h,
    tunable via /api/health/config), 503 once it's gone quiet longer than
    that; Kuma treats non-2xx as down, so a node silent past the threshold
    shows offline on Kuma's next poll. Accepts decimal, bare-hex, or '!hex'
    node ids."""
    canonical = _normalize_node_id(node_id) or node_id
    hours_arg = request.args.get('hours')
    conn = get_connection()
    try:
        threshold = float(hours_arg) if hours_arg else node_health.get_settings(conn)['offline_threshold_hours']
        result = node_health.get_status(conn, canonical, threshold)
    finally:
        conn.close()
    if result is None:
        return jsonify({'error': 'unknown node', 'node_id': canonical}), 404
    return jsonify(result), 200 if result['online'] else 503


# ---------------------------------------------------------------------------
# Entry point (dev only — production uses gunicorn)
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('FLASK_PORT', 5000)), debug=False)
