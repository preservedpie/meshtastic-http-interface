import base64
import collections
import logging
import os
import threading
import time
from datetime import datetime

logger = logging.getLogger(__name__)

MESHTASTIC_SERIAL_PORT = os.environ.get('MESHTASTIC_SERIAL_PORT', '/dev/ttyACM1')

_iface = None
_lock = threading.Lock()
_packet_handler = None  # set via set_packet_handler() — called with normalized packet dict

# Set by the meshtastic.connection.lost pubsub event. This is the reliable
# "radio died" signal: iface.nodes is a plain dict attribute that never raises,
# so attribute probes alone cannot detect a dead serial link.
_conn_lost = threading.Event()

# Monotonic timestamp of the last time we actually received something from the
# radio (a packet, or a successful (re)connect). connection.lost is a library
# pubsub event and — per the outage this watchdog was added for — it does not
# reliably fire when the underlying serial/USB link silently hangs (e.g. a
# blocked read with no timeout). This gives us an independent, time-based
# signal: if no data has arrived in too long, assume the link is dead
# regardless of what _conn_lost says.
_last_rx_at = time.monotonic()

# How long we tolerate total silence from the radio before assuming the link
# has frozen and forcing a reconnect. Env var is the floor/fallback; the
# GUI-editable SERIAL_WATCHDOG_TIMEOUT_SECONDS bot setting overrides it live.
WATCHDOG_TIMEOUT_SECONDS = float(os.environ.get('MESHTASTIC_WATCHDOG_TIMEOUT_SECONDS', '3600'))

# How often to proactively ask the local radio for a telemetry reading when
# nothing else has come in. This turns "the mesh has been quiet" (expected,
# not a failure) into "the local radio didn't even answer a direct request"
# (a real liveness signal), so genuine mesh silence can't masquerade as, or
# hide, a frozen serial link for up to the full watchdog window.
HEARTBEAT_INTERVAL_SECONDS = float(os.environ.get('MESHTASTIC_HEARTBEAT_INTERVAL_SECONDS', '300'))


def _watchdog_timeout() -> float:
    """Resolve the no-data watchdog timeout, preferring the live bot setting
    over the env/default so it can be tuned from the GUI without a restart."""
    try:
        from bot import settings as bot_settings
        val = bot_settings.get_int('SERIAL_WATCHDOG_TIMEOUT_SECONDS', 0)
        if val > 0:
            return float(val)
    except Exception:
        pass
    return WATCHDOG_TIMEOUT_SECONDS


def _heartbeat_interval() -> float:
    try:
        from bot import settings as bot_settings
        val = bot_settings.get_int('SERIAL_HEARTBEAT_INTERVAL_SECONDS', 0)
        if val > 0:
            return float(val)
    except Exception:
        pass
    return HEARTBEAT_INTERVAL_SECONDS


def get_watchdog_status() -> dict:
    """Snapshot of watchdog state for diagnostics (/api/status, health checks)."""
    idle = time.monotonic() - _last_rx_at
    timeout = _watchdog_timeout()
    return {
        'seconds_since_last_data':  round(idle, 1),
        'watchdog_timeout_seconds': timeout,
        'stale':                    idle > timeout,
    }

_serial_log: collections.deque = collections.deque(maxlen=500)
_serial_log_lock = threading.Lock()


def _append_log(level: str, msg: str) -> None:
    with _serial_log_lock:
        _serial_log.append({
            'ts':    datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'),
            'level': level,
            'msg':   msg,
        })


def set_packet_handler(handler):
    """Register the callback used to process incoming packets (called from receive thread)."""
    global _packet_handler
    _packet_handler = handler


def _on_receive(packet, interface):
    global _last_rx_at
    _last_rx_at = time.monotonic()
    if _packet_handler is None:
        return
    try:
        from parser import parse_serial_packet
        normalized = parse_serial_packet(packet)
        _append_log('rx', f"RX {normalized.get('type','?')} from {normalized.get('from','?')} ch={normalized.get('channel','?')}")
        _packet_handler(normalized)
    except Exception as e:
        _append_log('error', f"Packet handler error: {e}")
        logger.error(f"Error handling serial packet: {e}", exc_info=True)


def _on_connection_established(interface=None):
    _conn_lost.clear()
    logger.info("Meshtastic connection established")
    _append_log('info', "Connection established")


def _on_connection_lost(interface=None):
    _conn_lost.set()
    logger.warning("Meshtastic connection lost")
    _append_log('warn', "Connection lost")


def _on_node_updated(node, interface):
    pass


def _connect():
    """(Re)connect the SerialInterface. Caller must hold _lock."""
    global _iface, _local_node_number, _last_rx_at
    import meshtastic.serial_interface
    from pubsub import pub

    _local_node_number = 0  # clear cache; a reconnect may be a different radio
    last_err = None
    for attempt in range(1, 4):
        try:
            logger.info(f"Connecting to Meshtastic node at {MESHTASTIC_SERIAL_PORT} (attempt {attempt}/3)")
            _append_log('info', f"Connecting to {MESHTASTIC_SERIAL_PORT} (attempt {attempt}/3)")
            _conn_lost.clear()
            _iface = meshtastic.serial_interface.SerialInterface(
                devPath=MESHTASTIC_SERIAL_PORT,
                debugOut=None,
                noProto=False,
            )
            time.sleep(2)  # allow handshake / node list population

            for topic, cb in (
                ("meshtastic.receive",                _on_receive),
                ("meshtastic.connection.established", _on_connection_established),
                ("meshtastic.connection.lost",        _on_connection_lost),
                ("meshtastic.node.updated",           _on_node_updated),
            ):
                try:
                    pub.subscribe(cb, topic)
                except pub.TopicNameError:
                    pass

            logger.info("Meshtastic serial connected")
            _append_log('info', f"Connected to {MESHTASTIC_SERIAL_PORT}")
            # Set directly (not just via the connection.established pubsub
            # callback) so the watchdog's clock always restarts on a
            # successful connect, even if that library event misfires.
            _last_rx_at = time.monotonic()
            # Resolve & cache the local node number now (while myInfo is fresh)
            # so DM detection works for the very first incoming packet.
            num = get_local_node_number()
            if not num:
                _append_log('warn', "Could not resolve local node number at connect; "
                                    "DM detection may misfire until BOT_NODE_NUMBER is set")
            return _iface
        except Exception as e:
            last_err = e
            _iface = None
            logger.warning(f"Serial connect attempt {attempt} failed: {e}")
            _append_log('warn', f"Connect attempt {attempt} failed: {e}")
            if attempt < 3:
                time.sleep(3)
    _append_log('error', f"Connect failed after 3 attempts: {last_err}")
    raise RuntimeError(f"Could not connect to Meshtastic serial after 3 attempts: {last_err}")


def _iface_alive() -> bool:
    """True if the interface exists and the connection-lost event hasn't fired.

    iface.nodes is a plain dict attribute in the meshtastic library — reading
    it never raises, even after the serial port disappears — so the old
    attribute-probe 'liveness check' could never detect a dead radio. The
    pubsub meshtastic.connection.lost event is the real signal."""
    if _iface is None or _conn_lost.is_set():
        return False
    try:
        _ = _iface.nodes  # belt-and-braces; cheap even though it rarely raises
        return True
    except Exception:
        return False


def _force_close():
    """Drop the current interface so the next _get_iface() call reconnects."""
    global _iface
    with _lock:
        try:
            if _iface is not None:
                _iface.close()
        except Exception:
            pass
        _iface = None


def _get_iface():
    """Return a connected MeshInterface, (re)connecting as needed."""
    global _iface
    with _lock:
        if _iface is not None:
            if _iface_alive():
                return _iface
            try:
                _iface.close()
            except Exception:
                pass
            _iface = None
        return _connect()


def _send_heartbeat_probe():
    """Best-effort local telemetry request, run off the liveness-loop thread.

    sendTelemetry() ultimately does a blocking serial write; on a genuinely
    wedged USB/serial link that write can hang indefinitely. Firing it from a
    throwaway thread means a hung write can never block the liveness loop
    itself, so the no-data watchdog below can still trip and force a
    reconnect even if this probe never returns."""
    try:
        request_local_telemetry()
        _append_log('info', "Watchdog heartbeat probe sent")
    except Exception as e:
        _append_log('warn', f"Watchdog heartbeat probe failed: {e}")


def start(packet_handler):
    """Wire the packet handler and open the serial connection in a background thread.
    Returns immediately; the listener runs on the meshtastic library's reader thread."""
    set_packet_handler(packet_handler)

    def _runner():
        while True:
            try:
                _get_iface()
                last_probe_at = time.monotonic()
                # Once connected, the meshtastic library owns the reader thread.
                # Sit in a slow liveness loop; reconnect on failure.
                while True:
                    time.sleep(10)

                    if not _iface_alive():
                        logger.warning("Serial iface lost — reconnecting")
                        _append_log('warn', "Serial interface lost — reconnecting")
                        _force_close()
                        break

                    # Independent, time-based backstop: connection.lost isn't
                    # guaranteed to fire on a silent serial hang, so also
                    # force a reconnect if we simply haven't heard anything
                    # in too long (default 1 hour, see WATCHDOG_TIMEOUT_SECONDS).
                    idle_for = time.monotonic() - _last_rx_at
                    timeout = _watchdog_timeout()
                    if idle_for > timeout:
                        logger.error(
                            f"No data from radio in {int(idle_for)}s "
                            f"(limit {int(timeout)}s) — forcing reconnect")
                        _append_log(
                            'error',
                            f"Watchdog: no data in {int(idle_for)}s "
                            f"(limit {int(timeout)}s) — forcing reconnect")
                        _force_close()
                        break

                    # Proactively probe the local radio once things have been
                    # quiet for a while, so genuine mesh silence doesn't
                    # consume the whole watchdog window before we notice a
                    # real freeze.
                    since_probe = time.monotonic() - last_probe_at
                    heartbeat = _heartbeat_interval()
                    if idle_for > heartbeat and since_probe > heartbeat:
                        last_probe_at = time.monotonic()
                        threading.Thread(target=_send_heartbeat_probe,
                                          daemon=True, name="meshtastic-heartbeat").start()
            except Exception as e:
                logger.error(f"Serial loop error: {e} — retrying in 10s")
                _append_log('error', f"Serial loop error: {e} — retrying in 10s")
                time.sleep(10)

    t = threading.Thread(target=_runner, daemon=True, name="meshtastic-serial")
    t.start()
    logger.info("Meshtastic serial listener thread started")


# How long to wait for a delivery ACK before reporting the send unconfirmed.
# LoRa round-trips (esp. multi-hop) are slow, so give it real time. The env var
# is the floor/fallback; the GUI-editable ACK_TIMEOUT_SECONDS bot setting (when
# present) overrides it so the timeout can be tuned live without a restart.
ACK_TIMEOUT_SECONDS = float(os.environ.get('MESHTASTIC_ACK_TIMEOUT', '15'))


def _ack_timeout() -> float:
    """Resolve the ACK wait, preferring the live bot setting over the env/default.

    Read at send time (not import) so changes in the GUI take effect on the next
    DM. The bot package is an optional dependency of this transport layer, so a
    missing/failed lookup falls back to the module-level constant rather than
    breaking sends."""
    try:
        from bot import settings as bot_settings
        val = bot_settings.get_int('ACK_TIMEOUT_SECONDS', 0)
        if val > 0:
            return float(val)
    except Exception:
        pass
    return ACK_TIMEOUT_SECONDS


def _send_with_ack(iface, text, *, destination_id=None, channel_index=None) -> dict:
    """Send a text packet and block until the firmware reports the routing
    result (ACK / NAK) or ACK_TIMEOUT_SECONDS elapses.

    sendText() only *enqueues* a packet and returns immediately; the previous
    code treated that as success, so a dropped frame looked identical to a
    delivered one ("bot says it sent it" but nothing arrives). We attach an
    onResponse callback and wait on it so the returned dict reflects reality.
    """
    done = threading.Event()
    outcome = {'ack': None, 'error': None}

    def _on_response(response):
        try:
            decoded = response.get('decoded', {}) if isinstance(response, dict) else {}
            routing = decoded.get('routing', {}) or {}
            err = routing.get('errorReason', 'NONE')
            if err in (None, 'NONE'):
                outcome['ack'] = True
            else:
                outcome['ack'] = False
                outcome['error'] = err
        except Exception as e:
            outcome['error'] = str(e)
        finally:
            done.set()

    # Only pass kwargs the installed library actually accepts. Older meshtastic
    # versions' sendText() lack onResponse / onResponseAckPermitted, and passing
    # an unknown kwarg raises TypeError ("unexpected keyword argument"), which
    # would make every DM reply fail. Introspect the signature and degrade
    # gracefully: with full support we get real ACK confirmation; otherwise we
    # fall back to a best-effort wantAck send.
    try:
        import inspect
        accepted = set(inspect.signature(iface.sendText).parameters)
    except (TypeError, ValueError):
        accepted = set()

    kwargs = {}
    if 'wantAck' in accepted:
        kwargs['wantAck'] = True
    if destination_id is not None:
        kwargs['destinationId'] = destination_id
    if channel_index is not None:
        kwargs['channelIndex'] = channel_index

    can_confirm = 'onResponse' in accepted
    if can_confirm:
        kwargs['onResponse'] = _on_response
        if 'onResponseAckPermitted' in accepted:
            kwargs['onResponseAckPermitted'] = True

    try:
        iface.sendText(text, **kwargs)
    except TypeError:
        # Last-resort fallback: a plain send with no extras. The message still
        # goes out; we just can't confirm delivery.
        iface.sendText(text, destinationId=destination_id) if destination_id \
            else iface.sendText(text)
        return {'ok': True, 'acked': False, 'reason': 'sent_no_confirm'}

    if not can_confirm:
        # Library can't report delivery; treat a clean enqueue as sent.
        return {'ok': True, 'acked': False, 'reason': 'no_confirm_support'}

    acked = done.wait(timeout=_ack_timeout())
    if not acked:
        # No ACK/NAK within the timeout is not a confirmed failure — on a
        # multi-hop mesh the ack itself has to travel back through the same
        # lossy/slow path, so this fires constantly for DMs that still
        # arrive. Treat it like the other "sent, can't confirm" cases below
        # rather than reporting a false failure.
        return {'ok': True, 'acked': False, 'reason': 'ack_timeout'}
    if outcome['ack']:
        return {'ok': True, 'acked': True}
    return {'ok': False, 'acked': False, 'reason': outcome['error'] or 'nak'}


def send_text(text: str, channel_index: int = 0, to_node=None) -> dict:
    """Broadcast a text message on the given channel index, or DM a node.

    'acked' is only True when the firmware confirmed delivery; 'ok' is False
    only on a *confirmed* failure (a NAK / routing error came back). A DM
    that times out waiting for that confirmation still comes back ok=True,
    acked=False — on a multi-hop mesh the ACK has to survive the same lossy
    trip back, so "no ACK yet" does not mean "not delivered". Broadcasts on
    the primary channel are never individually ACK'd by the mesh, so they
    always come back acked=False with reason 'no_ack_for_broadcast' — also
    expected, not a failure.
    """
    iface = _get_iface()
    if to_node:
        # DM — firmware handles PKC encryption when paired
        if isinstance(to_node, int):
            dest = f"!{to_node:08x}"
        else:
            dest = str(to_node)
        result = _send_with_ack(iface, text, destination_id=dest)
        status = 'ACK' if result.get('acked') else f"NO-ACK ({result.get('reason')})"
        _append_log('tx', f"TX DM to {dest} [{status}]: {text[:60]}")
        return {**result, 'text': text, 'to': dest}

    # Broadcast: the mesh does not return a per-recipient ACK, so fire it and
    # report queued. Delivery to any given node is inherently best-effort.
    iface.sendText(text, channelIndex=channel_index, wantAck=False)
    _append_log('tx', f"TX ch={channel_index} [queued]: {text[:60]}")
    return {'ok': True, 'acked': False, 'reason': 'no_ack_for_broadcast',
            'text': text, 'channel': channel_index}


def request_telemetry(node_id: str) -> dict:
    """Ask node_id to send its device + environment telemetry."""
    iface = _get_iface()
    iface.sendTelemetry(destinationId=node_id, wantResponse=True)
    return {'requested': 'telemetry', 'node': node_id}


def request_local_telemetry(telemetry_type: str = 'environment_metrics') -> dict:
    """Ask the locally connected radio for a fresh telemetry reading.

    The firmware answers a telemetry request addressed to its own node number,
    and the response arrives through the normal receive callback — so it lands
    in the packets DB like any other node's telemetry. telemetryType is only
    passed when the installed library supports it (older versions always send
    a device-metrics request)."""
    iface = _get_iface()
    num = get_local_node_number()
    if not num:
        raise RuntimeError("local node number not resolved yet")
    dest = f"!{num:08x}"

    try:
        import inspect
        accepted = set(inspect.signature(iface.sendTelemetry).parameters)
    except (TypeError, ValueError):
        accepted = set()

    kwargs = {'destinationId': dest, 'wantResponse': True}
    if 'telemetryType' in accepted:
        kwargs['telemetryType'] = telemetry_type
    iface.sendTelemetry(**kwargs)
    _append_log('tx', f"TX local telemetry request ({telemetry_type})")
    return {'requested': telemetry_type, 'node': dest}


def request_position(node_id: str) -> dict:
    """Ask node_id to send its current position."""
    iface = _get_iface()
    iface.sendPosition(destinationId=node_id, wantResponse=True)
    return {'requested': 'position', 'node': node_id}


def request_traceroute(node_id: str) -> dict:
    """Send a traceroute to node_id and return immediately (result arrives via receive callback)."""
    iface = _get_iface()
    iface.sendTraceRoute(dest=node_id, hopLimit=7)
    return {'requested': 'traceroute', 'node': node_id}


def get_node_snapshot() -> dict:
    """Return current node table from the live interface, or empty dict."""
    try:
        iface = _get_iface()
        nodes = iface.nodes or {}
        return {k: dict(v) if hasattr(v, '__iter__') and not isinstance(v, str) else v
                for k, v in nodes.items()}
    except Exception as e:
        return {'error': str(e)}


def is_connected() -> bool:
    """True only if the interface exists AND the connection-lost event hasn't
    fired, so callers get the truth immediately rather than after the next
    liveness-loop pass."""
    return _iface_alive()


_local_node_number = 0  # cached once resolved; survives transient myInfo gaps


def get_local_node_number() -> int:
    """Return the locally connected node's numeric ID, or 0 if unknown.

    Lets the bot auto-detect DMs addressed to itself even when BOT_NODE_NUMBER
    isn't configured. Different meshtastic library versions expose this in
    different places, and myInfo can be momentarily None right after connect,
    so we try several accessors and cache the first good value."""
    global _local_node_number
    if _local_node_number:
        return _local_node_number
    if _iface is None:
        return 0

    # myInfo.my_node_num is the authoritative source for the *local* node.
    # (Other accessors like localNode/getMyNodeInfo can return a different node
    # on some library versions — we saw one resolve a remote node, breaking DM
    # detection — so we trust myInfo and only log the rest for diagnostics.)
    def _try(label, fn):
        try:
            v = fn()
            n = int(v) if v is not None else 0
        except Exception as e:
            n = 0
            v = f"<err {e}>"
        _append_log('info', f"local-node candidate {label}: {v!r} -> {n}"
                            + (f" (!{n:08x})" if n else ""))
        return n

    primary = _try('myInfo.my_node_num',
                   lambda: getattr(getattr(_iface, 'myInfo', None), 'my_node_num', None))
    # Diagnostics only — do not use these to set the value.
    _try('localNode.nodeNum',
         lambda: getattr(getattr(_iface, 'localNode', None), 'nodeNum', None))
    _try('getMyNodeInfo().num',
         lambda: (_iface.getMyNodeInfo() or {}).get('num')
                 if callable(getattr(_iface, 'getMyNodeInfo', None)) else None)

    if primary:
        _local_node_number = primary
        _append_log('info', f"Local node number resolved: {primary} (!{primary:08x})")
        return primary
    return 0


def is_available() -> bool:
    return bool(MESHTASTIC_SERIAL_PORT)


def get_serial_log(limit: int = 200) -> list:
    """Return recent serial log entries (oldest first), up to *limit*."""
    with _serial_log_lock:
        entries = list(_serial_log)
    return entries[-limit:]


def get_local_device_info() -> dict:
    """Live snapshot of the locally connected radio: identity, firmware, and
    whatever metrics its node DB holds. Device metrics (battery/voltage/util)
    are always tracked by the firmware; environment metrics appear here only
    on library versions that store them in the node DB — the /api/local/info
    endpoint backfills those from the packets table otherwise."""
    try:
        if _iface is None:
            return {}
        my = getattr(_iface, 'myInfo', None)
        num = getattr(my, 'my_node_num', None) if my else None
        if not num:
            return {}
        hexid = f"!{num:08x}"
        nodes = _iface.nodes or {}
        nd = nodes.get(hexid) or {}
        if not isinstance(nd, dict):
            nd = {}
        user = nd.get('user', {}) or {}
        dm = nd.get('deviceMetrics', {}) or {}
        em = nd.get('environmentMetrics', {}) or {}
        meta = getattr(_iface, 'metadata', None)
        firmware = getattr(meta, 'firmware_version', None) if meta else None
        return {
            'node_id':    hexid,
            'node_num':   num,
            'long_name':  user.get('longName'),
            'short_name': user.get('shortName'),
            'hw_model':   user.get('hwModel'),
            'firmware_version': firmware,
            'last_heard': nd.get('lastHeard'),
            'device_metrics': {
                'battery_level':       dm.get('batteryLevel'),
                'voltage':             dm.get('voltage'),
                'channel_utilization': dm.get('channelUtilization'),
                'air_util_tx':         dm.get('airUtilTx'),
                'uptime_seconds':      dm.get('uptimeSeconds'),
            },
            'environment_metrics': {
                'temperature':         em.get('temperature'),
                'relative_humidity':   em.get('relativeHumidity'),
                'barometric_pressure': em.get('barometricPressure'),
                'gas_resistance':      em.get('gasResistance'),
                'iaq':                 em.get('iaq'),
            } if em else {},
        }
    except Exception as e:
        return {'error': str(e)}


def get_local_node_info() -> dict:
    """Return basic info about the locally connected Meshtastic node."""
    try:
        if _iface is None:
            return {}
        my_info = _iface.myInfo
        if my_info is None:
            return {}
        my_num = my_info.my_node_num
        my_hex = f"!{my_num:08x}"
        nodes = _iface.nodes or {}
        node_data = nodes.get(my_hex) or {}
        user = node_data.get('user', {}) if isinstance(node_data, dict) else {}
        return {
            'node_id':    my_hex,
            'long_name':  user.get('longName'),
            'short_name': user.get('shortName'),
            'hw_model':   user.get('hwModel'),
        }
    except Exception as e:
        return {'error': str(e)}


def get_known_nodes() -> list:
    """Snapshot of every node in the connected radio's own node database.

    This is richer than any single received packet: the firmware (and the
    meshtastic library's local cache) accumulate it from NODEINFO packets,
    mesh gossip, and key-exchange traffic over time. In particular it's the
    only place to find a node's PKI public key — the key needed to send that
    node a DM — since not every packet type carries it. Returns [] when the
    radio isn't connected yet.
    """
    if _iface is None:
        return []
    try:
        nodes = _iface.nodes or {}
    except Exception:
        return []

    out = []
    for node_id, nd in nodes.items():
        if not isinstance(nd, dict):
            continue
        user = nd.get('user', {}) or {}
        out.append({
            'node_id':                  node_id,
            'node_num':                 nd.get('num'),
            'long_name':                user.get('longName'),
            'short_name':               user.get('shortName'),
            'hw_model':                 user.get('hwModel'),
            'role':                     user.get('role'),
            'macaddr':                  user.get('macaddr'),
            'public_key':               user.get('publicKey'),
            'is_licensed':              user.get('isLicensed'),
            'is_unmessagable':          user.get('isUnmessagable'),
            'snr':                      nd.get('snr'),
            'last_heard':               nd.get('lastHeard'),
            'hops_away':                nd.get('hopsAway'),
            'via_mqtt':                 nd.get('viaMqtt'),
            'is_favorite':              nd.get('isFavorite'),
            'is_key_manually_verified': nd.get('isKeyManuallyVerified'),
        })
    return out


# ---- LoRa channel management ---------------------------------------------
#
# Reads/writes the connected radio's own channel table (index 0-7; index 0 is
# always PRIMARY). This is the actual mesh configuration, not just local
# metadata — renaming/adding/deleting a channel here changes what this radio
# can talk to, and (for anything but a pure name change) every other node
# sharing that channel needs matching settings to keep talking to it.

# The meshtastic CLI's own --ch-add rejects names longer than this.
MAX_CHANNEL_NAME_LEN = 10

# Valid decoded PSK lengths: AES128 (16 bytes) or AES256 (32 bytes) — the two
# lengths genPSK256() / the official app actually produce. The 0/1-byte
# "none"/"default" sentinel keys are a CLI shorthand, not a real pasted key,
# so a paste that decodes to one of those is far more likely a mistake than
# intentional — reject it with a clear error instead of silently accepting it.
_VALID_PSK_LENGTHS = (16, 32)


def _parse_psk_input(raw: str) -> bytes:
    """Decode a user-pasted encryption key — as copied from the official
    Meshtastic app's channel share/QR export — into raw PSK bytes. Accepts
    a base64 string or a 0x-prefixed hex string."""
    raw = (raw or '').strip()
    if not raw:
        raise ValueError("encryption key is required")
    try:
        if raw.lower().startswith('0x'):
            key = bytes.fromhex(raw[2:])
        else:
            key = base64.b64decode(raw, validate=True)
    except Exception:
        raise ValueError("encryption key must be a base64 string or a 0x-prefixed hex string")
    if len(key) not in _VALID_PSK_LENGTHS:
        raise ValueError("encryption key must decode to 16 bytes (AES128) or 32 bytes (AES256)")
    return key


def get_device_channels() -> list:
    """Snapshot of the connected radio's LoRa channels. PSKs are never
    returned to the caller — only whether one is set — since that's the
    channel's actual encryption key. Returns [] when not connected."""
    if _iface is None:
        return []
    node = getattr(_iface, 'localNode', None)
    channels = getattr(node, 'channels', None) if node is not None else None
    if not channels:
        return []
    from meshtastic import channel_pb2
    out = []
    for ch in channels:
        out.append({
            'index':            ch.index,
            'role':             channel_pb2.Channel.Role.Name(ch.role),
            'name':             ch.settings.name,
            'has_psk':          bool(ch.settings.psk),
            'uplink_enabled':   ch.settings.uplink_enabled,
            'downlink_enabled': ch.settings.downlink_enabled,
        })
    return out


def _get_local_channel(index: int):
    """Look up a channel by index on the connected radio, raising the same
    user-facing errors regardless of which write operation called it."""
    if _iface is None:
        raise RuntimeError("Meshtastic radio is not connected")
    node = getattr(_iface, 'localNode', None)
    ch = node.getChannelByChannelIndex(index) if node is not None else None
    if ch is None:
        raise ValueError(f"no channel at index {index}")
    return node, ch


def set_channel_name(index: int, name: str) -> None:
    """Rename an existing (non-disabled) channel. Leaves role/PSK/flags
    untouched — only the name is sent back to the device."""
    name = (name or '').strip()
    if not name:
        raise ValueError("channel name is required")
    if len(name) > MAX_CHANNEL_NAME_LEN:
        raise ValueError(f"channel name must be {MAX_CHANNEL_NAME_LEN} characters or fewer")
    from meshtastic import channel_pb2
    node, ch = _get_local_channel(index)
    if ch.role == channel_pb2.Channel.Role.DISABLED:
        raise ValueError("channel is disabled — add it first")
    ch.settings.name = name
    node.writeChannel(index)


def set_channel_flags(index: int, uplink_enabled=None, downlink_enabled=None) -> None:
    """Toggle MQTT uplink/downlink for an existing (non-disabled) channel."""
    from meshtastic import channel_pb2
    node, ch = _get_local_channel(index)
    if ch.role == channel_pb2.Channel.Role.DISABLED:
        raise ValueError("channel is disabled — add it first")
    if uplink_enabled is not None:
        ch.settings.uplink_enabled = bool(uplink_enabled)
    if downlink_enabled is not None:
        ch.settings.downlink_enabled = bool(downlink_enabled)
    node.writeChannel(index)


def add_device_channel(name: str, psk: str = None) -> dict:
    """Add a new SECONDARY channel into the first free (DISABLED) slot —
    mirrors what the meshtastic CLI's --ch-add does. Gets a freshly
    generated PSK unless the caller supplies one (as pasted from the
    official app). Raises if the name is invalid, already in use, the key
    is malformed, or there's no free slot (radios support up to 8 channels
    total)."""
    name = (name or '').strip()
    if not name:
        raise ValueError("channel name is required")
    if len(name) > MAX_CHANNEL_NAME_LEN:
        raise ValueError(f"channel name must be {MAX_CHANNEL_NAME_LEN} characters or fewer")
    key = _parse_psk_input(psk) if psk else None
    if _iface is None:
        raise RuntimeError("Meshtastic radio is not connected")
    node = getattr(_iface, 'localNode', None)
    if node is None:
        raise RuntimeError("Meshtastic radio is not connected")
    if node.getChannelByName(name):
        raise ValueError(f"a channel named {name!r} already exists")
    ch = node.getDisabledChannel()
    if ch is None:
        raise ValueError("no free channel slots (device supports up to 8 channels)")

    from meshtastic import channel_pb2
    import meshtastic.util
    chs = channel_pb2.ChannelSettings()
    chs.psk = key if key is not None else meshtastic.util.genPSK256()
    chs.name = name
    ch.settings.CopyFrom(chs)
    ch.role = channel_pb2.Channel.Role.SECONDARY
    node.writeChannel(ch.index)
    return {'index': ch.index, 'name': name, 'role': 'SECONDARY'}


def set_channel_psk(index: int, psk: str = None) -> None:
    """Replace an existing (non-disabled) channel's encryption key — either
    a caller-supplied key or, if none given, a freshly generated random one.
    The current key is never read back out anywhere; this only ever writes
    a new one, matching "don't show me the old key, just let me replace it"."""
    key = _parse_psk_input(psk) if psk else None
    from meshtastic import channel_pb2
    import meshtastic.util
    node, ch = _get_local_channel(index)
    if ch.role == channel_pb2.Channel.Role.DISABLED:
        raise ValueError("channel is disabled — add it first")
    ch.settings.psk = key if key is not None else meshtastic.util.genPSK256()
    node.writeChannel(index)


def delete_device_channel(index: int) -> None:
    """Disable a SECONDARY channel, shifting later channels up an index.
    PRIMARY can't be deleted this way — the library itself rejects it."""
    from meshtastic import channel_pb2
    node, ch = _get_local_channel(index)
    if ch.role != channel_pb2.Channel.Role.SECONDARY:
        raise ValueError("only secondary channels can be deleted")
    node.deleteChannel(index)
