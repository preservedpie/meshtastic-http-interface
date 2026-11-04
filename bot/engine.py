"""Bot engine — couples the command router to the http-interface's
serial receive path and send path.

Lifecycle:
  bot.engine.init()           — open bot DB, start LLM worker, build router
  bot.engine.handle_packet(p) — called by app._handle_packet for every
                                normalized incoming packet
  bot.engine.start_workers()  — kick off the send flush + weather scheduler
                                threads (foreground keeps using the
                                meshtastic library's reader thread)
"""

import collections
import logging
import queue
import threading
import time
from datetime import datetime, timezone, date as _date_t
from typing import Optional

from . import db
from . import settings
from . import llm_worker
from .command_router import CommandRouter
from .commands import check_weather_alert, check_weather_alert_at

logger = logging.getLogger('mesh-bot.engine')

# ---- module state --------------------------------------------------------

_router: Optional[CommandRouter] = None
_node_log: dict = {}                  # node_id -> heard-node summary
_node_log_lock = threading.Lock()
_last_response: dict = {}             # node_id -> epoch seconds
_send_queue: "queue.Queue[dict]" = queue.Queue()
_start_time: Optional[datetime] = None
_last_alert_date: Optional[_date_t] = None
_send_fn = None                       # set by init() — callable(text, channel, to_node)
_node_positions_fn = None             # set by set_node_positions_fn() — callable(max_age_hours) -> list[dict]
_workers_started = False

# Mesh flood-routing delivers the same packet multiple times (once per relaying
# neighbor). Remember recently-seen packet ids so we only act on each once.
_seen_packet_ids: "collections.OrderedDict[int, float]" = collections.OrderedDict()
_seen_lock = threading.Lock()
_SEEN_MAX = 512
_SEEN_TTL_SECONDS = 600


def _is_duplicate_packet(packet) -> bool:
    """True if this packet id was already processed recently."""
    raw = packet.get('raw') if isinstance(packet.get('raw'), dict) else {}
    pkt_id = packet.get('id') or raw.get('id')
    if not pkt_id:
        return False  # no id to dedup on — let it through
    now = time.time()
    with _seen_lock:
        # Drop entries past their TTL.
        cutoff = now - _SEEN_TTL_SECONDS
        while _seen_packet_ids:
            oldest_id, ts = next(iter(_seen_packet_ids.items()))
            if ts < cutoff:
                _seen_packet_ids.popitem(last=False)
            else:
                break
        if pkt_id in _seen_packet_ids:
            return True
        _seen_packet_ids[pkt_id] = now
        while len(_seen_packet_ids) > _SEEN_MAX:
            _seen_packet_ids.popitem(last=False)
    return False


# ---- init / shutdown -----------------------------------------------------

def init(send_fn) -> None:
    """One-time setup. send_fn(text, channel, to_node) is provided by the host."""
    global _router, _start_time, _send_fn
    _send_fn = send_fn
    db.init()
    settings.seed_defaults_from_env()
    llm_worker.start()
    _router = CommandRouter()
    _start_time = datetime.now(timezone.utc)
    logger.info("Bot engine initialised")


def set_node_positions_fn(fn) -> None:
    """Wire in the host's node-position lookup: fn(max_age_hours) -> list of
    {'node_id', 'lat', 'lon', 'is_unmessagable', 'hops_away'}. Used for per-node weather
    alerts; the bot has no direct access to the http-interface's packets/nodes
    tables, so this stays a callback like send_fn instead of a cross-module
    DB import."""
    global _node_positions_fn
    _node_positions_fn = fn


def stop() -> None:
    llm_worker.stop()


# ---- public accessors (used by the HTTP API) ----------------------------

def is_enabled() -> bool:
    return settings.get_bool('BOT_ENABLED', True)


def bot_node_number() -> int:
    """Numeric node id of the bot's own radio.

    Prefer the explicit BOT_NODE_NUMBER setting; if it's unset (0), fall back
    to the live local node number reported by the serial interface. Without
    this, DMs to the bot were never recognised as DMs (is_dm stayed False),
    so they were dropped by the prefix requirement and any reply went to the
    channel instead of back to the sender.
    """
    configured = settings.get_int('BOT_NODE_NUMBER', 0)
    if configured:
        return configured
    try:
        import meshtastic_iface
        return meshtastic_iface.get_local_node_number()
    except Exception:
        return 0


def bot_node_number_configured() -> int:
    """The explicitly-set BOT_NODE_NUMBER (0 if relying on auto-detection)."""
    return settings.get_int('BOT_NODE_NUMBER', 0)


def start_time() -> Optional[datetime]:
    return _start_time


def uptime_str() -> str:
    if not _start_time:
        return '-'
    secs = int((datetime.now(timezone.utc) - _start_time).total_seconds())
    d, rem = divmod(secs, 86400)
    h, rem = divmod(rem, 3600)
    m, _ = divmod(rem, 60)
    if d:
        return f"{d}d {h}h {m}m"
    if h:
        return f"{h}h {m}m"
    return f"{m}m"


def snapshot_nodes() -> list:
    """Heard-node table for the bot dashboard."""
    out = []
    with _node_log_lock:
        for node_id, info in _node_log.items():
            node = dict(info)
            node['id'] = node_id
            node['hex'] = f"!{node_id:08x}"
            last = node.get('last_seen')
            if isinstance(last, datetime):
                node['last_seen'] = last.isoformat()
            out.append(node)
    out.sort(key=lambda n: n.get('last_seen') or '', reverse=True)
    return out


def _chunk_text(text: str, chunk_size: int, max_chunks: int = 4) -> list:
    """Split text into chunks of at most chunk_size chars, breaking at word boundaries.
    Returns at most max_chunks chunks; text that won't fit is discarded."""
    text = text.strip()
    if not text:
        return []
    if len(text) <= chunk_size:
        return [text]
    chunks = []
    remaining = text
    while remaining and len(chunks) < max_chunks:
        if len(remaining) <= chunk_size:
            chunks.append(remaining)
            break
        split_at = remaining.rfind(' ', 0, chunk_size)
        if split_at <= 0:
            split_at = chunk_size
        chunks.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip()
    return chunks


def queue_send(text: str, channel: int = 0, to_node: Optional[int] = None) -> None:
    """Queue a single outbound message. Callers should use queue_send_chunked for
    bot responses so long text is split rather than truncated."""
    chunk_size = settings.get_int('MAX_RESPONSE_LENGTH', 200)
    if len(text) > chunk_size:
        text = text[:chunk_size - 3] + '...'
    db.log_message('out', text, channel=channel)
    _send_queue.put({'text': text, 'channel': channel, 'to_node': to_node})


def queue_send_chunked(text: str, channel: int = 0, to_node: Optional[int] = None) -> None:
    """Split text across up to MAX_REPLY_MESSAGES messages and queue each chunk."""
    chunk_size = settings.get_int('MAX_RESPONSE_LENGTH', 200)
    max_msgs   = max(1, min(settings.get_int('MAX_REPLY_MESSAGES', 4), 4))
    for chunk in _chunk_text(text, chunk_size, max_chunks=max_msgs):
        queue_send(chunk, channel=channel, to_node=to_node)


# ---- packet handling ----------------------------------------------------

def handle_packet(packet: dict) -> bool:
    """Called by the host app for every normalized incoming packet.

    `packet` is the dict produced by parser.parse_serial_packet(): top-level
    fields from/to/type/rssi/snr/hop_start/hops_away/channel/id, plus
    'raw' carrying the original. We pull the inner payload from raw.

    Returns True if the bot replied (or queued an async reply) to this
    message — every early-exit path below implicitly returns the falsy
    None, so the host (app.py) can use the return value to record whether
    the bot responded, for display in the Messages tab.
    """
    try:
        if not is_enabled():
            return

        if _is_duplicate_packet(packet):
            return

        from_node = packet.get('from')
        msg_type = packet.get('type')
        if not from_node or not msg_type:
            return

        # Normalize from_node to int (parser stores it as a hex string for
        # the http-interface's DB; commands want an int)
        from_int = _coerce_int_node(from_node)
        if from_int is None:
            return

        if from_int == bot_node_number():
            return
        if from_int in settings.get_ignored_nodes():
            return

        raw = packet.get('raw', {}) or {}
        inner_payload = raw.get('payload', {}) if isinstance(raw.get('payload'), dict) else {}

        _update_node_log(from_int, msg_type, packet, inner_payload)

        if msg_type != 'text':
            return

        text = ''
        rp = raw.get('payload', '')
        if isinstance(rp, dict):
            text = (rp.get('text') or '').strip()
        elif isinstance(rp, str):
            text = rp.strip()
        if not text:
            return

        channel = packet.get('channel')
        ch_int = _coerce_channel_int(channel)

        # Detect direct message addressed to this bot
        to_node = packet.get('to')
        bot_num = bot_node_number()
        to_int = _coerce_int_node(to_node)
        is_dm = bool(to_int and bot_num and to_int == bot_num)
        logger.info(
            f"DM check: to={to_node!r} to_int={to_int} bot_num={bot_num} "
            f"is_dm={is_dm} ch={ch_int}"
        )

        # Channel policy only applies to non-DM messages
        policy = None
        if not is_dm and ch_int is not None:
            policy = db.get_channel_policy(ch_int)
            if policy and not policy['listen']:
                return

        db.log_message('in', text,
                       node_id=from_int,
                       node_name=_node_name(from_int),
                       channel=ch_int)

        # Command prefix — DMs skip the prefix requirement since the message
        # is already addressed directly to the bot
        prefix = settings.get('COMMAND_PREFIX', '') or ''
        require_prefix = settings.get_bool('REQUIRE_PREFIX', True)
        stripped = text
        if prefix:
            if stripped.lower().startswith(prefix.lower()):
                stripped = stripped[len(prefix):].strip()
            elif require_prefix and not is_dm:
                return

        # Rate limit
        rate_limit = settings.get_int('RATE_LIMIT_SECONDS', 30)
        now = time.time()
        if from_int in _last_response:
            elapsed = now - _last_response[from_int]
            if elapsed < rate_limit:
                logger.info(f"Rate limited node {from_int} "
                            f"(wait {rate_limit - elapsed:.0f}s)")
                return

        # Resolve reply destination
        if is_dm:
            response_channel = 0
            reply_to_node = from_int
        else:
            response_channel = _resolve_response_channel(ch_int, policy)
            reply_to_node = None
            if policy and not policy['respond']:
                return

        # Pass to_node into the send callback so async LLM replies also go as DM
        send_cb = _make_send_callback(to_node=reply_to_node)
        context = {
            'from': from_int,
            'sender': packet.get('sender'),
            'rssi': packet.get('rssi'),
            'snr': packet.get('snr'),
            'hop_start': packet.get('hop_start'),
            'hops_away': packet.get('hops_away'),
            'timestamp': raw.get('timestamp') if isinstance(raw, dict) else None,
            'channel': ch_int,
            'id': packet.get('id') or (raw.get('id') if isinstance(raw, dict) else None),
            'received_at': datetime.now(timezone.utc),
            'bot_uptime': datetime.now(timezone.utc) - _start_time if _start_time else None,
            'node_log': _node_log_snapshot(),
            'send': send_cb,
            'response_channel': response_channel,
            'channel_allowed_cmds': policy['allowed_cmds'] if policy else [],
            'is_dm': is_dm,
        }

        response = _router.handle(stripped, context)
        if response:
            queue_send_chunked(response, channel=response_channel, to_node=reply_to_node)
        # Only start the rate-limit clock when the bot actually replied (or
        # queued an async LLM reply); otherwise plain chatter on a listened
        # channel would block that node's next real command.
        if response or context.get('handled'):
            _last_response[from_int] = now

        return bool(response or context.get('handled'))

    except Exception as e:
        logger.error(f"Bot engine error processing packet: {e}", exc_info=True)
        return False


# ---- internal helpers ---------------------------------------------------

def _coerce_int_node(val):
    if val is None:
        return None
    if isinstance(val, int):
        return val
    s = str(val).strip()
    if not s:
        return None
    if s.startswith('!'):
        try:
            return int(s[1:], 16)
        except ValueError:
            return None
    try:
        return int(s)
    except ValueError:
        try:
            return int(s, 16)
        except ValueError:
            return None


def _coerce_channel_int(val):
    if val is None:
        return None
    if isinstance(val, int):
        return val
    try:
        return int(val)
    except (ValueError, TypeError):
        return None


def _resolve_response_channel(incoming_ch, policy):
    """Reply on the same channel by default; allow policy/setting override."""
    if policy and policy.get('is_response_ch'):
        return policy['channel']
    # Fall back to setting (DEFAULT_RESPONSE_CHANNEL) only when incoming is None.
    if incoming_ch is None:
        return settings.get_int('DEFAULT_RESPONSE_CHANNEL', 0)
    return incoming_ch


def _make_send_callback(to_node=None):
    def _cb(text: str, channel: int):
        queue_send_chunked(text, channel=channel, to_node=to_node)
    return _cb


def _update_node_log(from_int, msg_type, packet, inner_payload):
    with _node_log_lock:
        if from_int not in _node_log:
            _node_log[from_int] = {}
        node = _node_log[from_int]
        node['last_seen'] = datetime.now(timezone.utc)
        for field in ('rssi', 'snr', 'hops_away'):
            v = packet.get(field)
            if v is not None:
                node[field] = v
        if not isinstance(inner_payload, dict):
            return
        if msg_type == 'nodeinfo':
            for field in ('long_name', 'short_name', 'hw_model', 'role',
                          'longname', 'shortname', 'hardware'):
                v = inner_payload.get(field)
                if v is not None:
                    # parser uses longname/shortname/hardware; mesh-bot used
                    # long_name/short_name/hw_model. Normalize both.
                    if field == 'longname':
                        node['long_name'] = v
                    elif field == 'shortname':
                        node['short_name'] = v
                    elif field == 'hardware':
                        node['hw_model'] = v
                    else:
                        node[field] = v
        elif msg_type == 'telemetry':
            for field in ('battery_level', 'voltage', 'channel_utilization',
                          'air_util_tx', 'uptime_seconds'):
                v = inner_payload.get(field)
                if v is not None:
                    node[field] = v
        elif msg_type == 'position':
            lat_i = inner_payload.get('latitude_i')
            lon_i = inner_payload.get('longitude_i')
            if lat_i is not None:
                node['latitude'] = lat_i / 1e7
            if lon_i is not None:
                node['longitude'] = lon_i / 1e7
            alt = inner_payload.get('altitude')
            if alt is not None:
                node['altitude'] = alt


def _node_log_snapshot():
    with _node_log_lock:
        return {k: dict(v) for k, v in _node_log.items()}


def _node_name(from_int):
    with _node_log_lock:
        info = _node_log.get(from_int, {})
        return info.get('long_name') or info.get('short_name') or ''


# ---- background workers (send flush + weather alert) --------------------

def start_workers():
    global _workers_started
    if _workers_started:
        return
    _workers_started = True
    threading.Thread(target=_send_loop, name='bot-send', daemon=True).start()
    threading.Thread(target=_scheduler_loop, name='bot-sched', daemon=True).start()
    logger.info("Bot background workers started")


def _inter_send_delay() -> float:
    """Seconds to wait between consecutive outbound LoRa packets."""
    ms = settings.get_int('INTER_SEND_DELAY_MS', 4000)
    return max(0.0, ms / 1000.0)


def _send_loop():
    while True:
        try:
            msg = _send_queue.get(timeout=1.0)
        except queue.Empty:
            continue
        started = time.monotonic()
        acked = False
        try:
            if _send_fn is None:
                logger.warning("Bot send callback not set; dropping message")
                continue
            result = _send_fn(msg['text'], msg.get('channel', 0), msg.get('to_node'))
            acked = bool(result.get('acked')) if isinstance(result, dict) else False
        except Exception as e:
            logger.error(f"Send failure: {e}")
        finally:
            # Space out consecutive LoRa transmissions. Blasting the up-to-4
            # chunks of a single reply back-to-back makes the radio drop later
            # chunks (channel busy / duty cycle), so the recipient sees only
            # the first chunk while the bot logs all of them as sent.
            #
            # Only credit the time the send already consumed against the gap
            # when the send was *ACK-confirmed* — a real ACK means chunk N is
            # delivered and off the air, so chunk N+1 can follow sooner. On a
            # NO-ACK / ack_timeout (the common case when the library can't match
            # the routing response to the request), the chunk may still be
            # queued or transmitting, so we must wait the FULL inter-send delay
            # or the next chunk steps on it and gets dropped.
            if not _send_queue.empty():
                if acked:
                    remaining = _inter_send_delay() - (time.monotonic() - started)
                else:
                    remaining = _inter_send_delay()
                if remaining > 0:
                    time.sleep(remaining)


def _scheduler_loop():
    """Every minute, check whether the daily weather alert(s) should fire.

    Two independent alert types share the same once-a-day/hour trigger:
    the fixed DEFAULT_LOCATION broadcast (WEATHER_ALERT_ENABLED) and the
    per-node DM alert based on each node's own reported GPS position
    (WEATHER_ALERT_PER_NODE_ENABLED). The due-check has to stay outside
    either feature's enabled flag, or enabling only the per-node one would
    never fire since it used to be nested under the broadcast's flag.
    """
    global _last_alert_date
    while True:
        try:
            tz_name = settings.get('WEATHER_ALERT_TZ', 'America/Chicago')
            try:
                from zoneinfo import ZoneInfo
                tz = ZoneInfo(tz_name)
            except Exception:
                tz = timezone.utc
            now_local = datetime.now(tz)
            alert_hour = settings.get_int('WEATHER_ALERT_HOUR', 8)
            today = now_local.date()
            due = now_local.hour == alert_hour and _last_alert_date != today

            if due:
                _last_alert_date = today

                if settings.get_bool('WEATHER_ALERT_ENABLED', False):
                    logger.info("Running daily weather alert check...")
                    _send_broadcast_weather_alert()

                if settings.get_bool('WEATHER_ALERT_PER_NODE_ENABLED', False):
                    _run_per_node_weather_alerts(today)
        except Exception as e:
            logger.error(f"Scheduler error: {e}")
        time.sleep(60)


def _today_local() -> _date_t:
    tz_name = settings.get('WEATHER_ALERT_TZ', 'America/Chicago')
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = timezone.utc
    return datetime.now(tz).date()


def _send_broadcast_weather_alert() -> dict:
    """Check for alert-worthy weather at DEFAULT_LOCATION and, if found,
    broadcast it on the configured response channel. Shared by the scheduled
    daily run and the dashboard's manual "run now" test action."""
    alert = check_weather_alert()
    if alert:
        ch = settings.get_int('DEFAULT_RESPONSE_CHANNEL', 0)
        resp_ch = db.get_response_channel()
        if resp_ch is not None:
            ch = resp_ch
        queue_send(alert, channel=ch)
    return {'alert': alert, 'sent': bool(alert)}


def run_weather_check_now() -> dict:
    """Manually trigger the broadcast daily weather alert check immediately —
    used by the bot dashboard's Actions tab to test the feature without
    waiting for WEATHER_ALERT_HOUR. Bypasses the scheduler's once-a-day gate
    but doesn't disturb it: _last_alert_date is scheduler-local state, so a
    manual run here has no effect on whether the real scheduled run fires
    later today."""
    return _send_broadcast_weather_alert()


def run_per_node_weather_check_now() -> dict:
    """Manually trigger the per-node weather DM check immediately — used by
    the bot dashboard's Actions tab to test the feature. Runs in force mode:
    nodes already alerted today are re-checked and re-messaged rather than
    skipped, so a tester isn't blocked by their own earlier test run, but a
    node's alert-date marker is only set if it wasn't already (see
    _run_per_node_weather_alerts), so this can't cause a real scheduled DM
    later today to be skipped that otherwise would have fired."""
    return _run_per_node_weather_alerts(_today_local(), force=True)


def _node_is_watched(node_id: str, role, watched: set, hops_away, zero_hop_only: bool) -> bool:
    """WEATHER_ALERT_WATCHED_NODES, when set, is an explicit allow-list that
    overrides the role filter (the user deliberately picked it). Otherwise
    fall back to CLIENT-role nodes only — routers/repeaters/sensors aren't
    someone's personal device to DM a storm warning to.

    WEATHER_ALERT_ZERO_HOP_ONLY layers on top of that (rather than replacing
    it) as an "only stuff close by" filter, so users don't have to hand-list
    every nearby node: it requires a last-known hop count of exactly 0,
    excluding nodes with an unknown hop count (None) since we can't tell if
    those are actually in direct range."""
    if zero_hop_only and hops_away != 0:
        return False
    if watched:
        return _coerce_int_node(node_id) in watched
    return role == 'CLIENT'


def _run_per_node_weather_alerts(today: _date_t, force: bool = False) -> dict:
    """DM each watched node its own local weather alert: nodes with a recent
    reported position get a check at their own coordinates; nodes that have
    never reported one (not every device has GPS) fall back to a general
    alert for the fixed DEFAULT_LOCATION setting, so they still get warned
    about storms instead of being silently skipped. Skips the bot's own
    node, unmessagable nodes, and (unless force=True) any node already
    alerted today (persisted, so a mid-hour restart can't re-spam everyone).

    force=True is for the dashboard's manual "run now" test action: it
    re-checks/re-messages nodes already alerted today instead of skipping
    them, but only touches a node's persisted alert-date marker if it wasn't
    already set — so it can't suppress a real scheduled DM later today that
    otherwise would have fired.

    Returns a summary dict: {'checked': int, 'messaged': [node_id, ...]},
    or {'error': str} if there's no wired-up position source."""
    if _node_positions_fn is None:
        logger.warning("WEATHER_ALERT_PER_NODE_ENABLED is on but no position source is wired up")
        return {'error': 'no node-position source is wired up'}

    max_age = settings.get_int('WEATHER_ALERT_MAX_POSITION_AGE_HOURS', 168)
    try:
        nodes = _node_positions_fn(max_age)
    except Exception as e:
        logger.error(f"Failed to fetch node positions for weather alerts: {e}")
        return {'error': f'failed to fetch node positions: {e}'}

    watched = settings.get_watched_nodes()
    zero_hop_only = settings.get_bool('WEATHER_ALERT_ZERO_HOP_ONLY', False)
    default_location = settings.get('DEFAULT_LOCATION', 'New York')
    local_num = bot_node_number()
    local_hex = f"!{local_num:08x}" if local_num else None
    today_str = today.isoformat()

    checked = 0
    messaged = []
    logger.info(f"Running per-node weather alert check for {len(nodes)} known node(s)...")
    for n in nodes:
        node_id = n.get('node_id')
        if not node_id or node_id == local_hex or n.get('is_unmessagable'):
            continue
        if not _node_is_watched(node_id, n.get('role'), watched, n.get('hops_away'), zero_hop_only):
            continue
        already_alerted = db.get_node_alert_date(node_id) == today_str
        if already_alerted and not force:
            continue
        checked += 1

        has_position = n.get('lat') is not None and n.get('lon') is not None
        try:
            alert = (check_weather_alert_at(n['lat'], n['lon']) if has_position
                     else check_weather_alert(default_location))
        except Exception as e:
            logger.error(f"Weather alert check failed for {node_id}: {e}")
            continue

        # Mark attempted regardless of outcome, so a bad/unreachable
        # location doesn't get retried on every scheduler tick this hour.
        if not already_alerted:
            db.set_node_alert_date(node_id, today_str)
        if alert:
            logger.info(f"Sending {'local' if has_position else 'general'} weather alert DM to {node_id}")
            queue_send(alert, to_node=_coerce_int_node(node_id))
            messaged.append(node_id)
        # Be polite to the free wttr.in API when checking many nodes.
        time.sleep(1)

    return {'checked': checked, 'messaged': messaged}


# ---- LLM passthroughs for the web API -----------------------------------

def llm_health():
    return llm_worker.health()


def llm_sessions():
    return llm_worker.active_sessions()


def llm_test(prompt: str):
    return llm_worker.query_sync(prompt, node_id=None, use_history=False)
