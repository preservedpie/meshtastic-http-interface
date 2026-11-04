# Map Meshtastic portnum strings (as emitted by the Python serial interface)
# to the short type strings used by the rest of the app and the existing DB.
_PORTNUM_TO_TYPE = {
    'TEXT_MESSAGE_APP':    'text',
    'POSITION_APP':        'position',
    'NODEINFO_APP':        'nodeinfo',
    'TELEMETRY_APP':       'telemetry',
    'TRACEROUTE_APP':      'traceroute',
    'MAP_REPORT_APP':      'map_report',
    'ROUTING_APP':         'routing',
    'ADMIN_APP':           'admin',
    'REMOTE_HARDWARE_APP': 'remotehardware',
    'WAYPOINT_APP':        'waypoint',
    'NEIGHBORINFO_APP':    'neighborinfo',
    'RANGE_TEST_APP':      'rangetest',
    'STORE_FORWARD_APP':   'storeforward',
    'DETECTION_SENSOR_APP':'detection',
    'REPLY_APP':           'reply',
    'ATAK_PLUGIN':         'atak',
}


def node_id_to_hex(node_int):
    """Convert integer node ID to !hexstring format. Passes through existing !hex strings."""
    if node_int is None:
        return None
    if isinstance(node_int, str) and node_int.startswith('!'):
        return node_int
    try:
        return f"!{int(node_int):08x}"
    except (ValueError, TypeError):
        return str(node_int)


def _channel_from_topic(topic):
    """Extract channel name from topic like .../2/json/Sub/!abcdef12"""
    parts = topic.split('/')
    try:
        json_idx = parts.index('json')
        return parts[json_idx + 1]
    except (ValueError, IndexError):
        return None


def parse_packet(topic, payload):
    """
    Parse a Meshtastic MQTT JSON payload into a normalized dict.

    Meshtastic JSON mode gateway publishes full packet JSON to topics like:
      home/meshtastic2/2/json/LongFast/!abcdef12
    """
    channel = _channel_from_topic(topic) or str(payload.get('channel', ''))
    ptype = payload.get('type', 'unknown')

    # map_report arrives with type "map_report" (firmware 2.3+)
    # Normalize it so _upsert_node can handle it like nodeinfo
    if ptype == 'map_report':
        p = payload.get('payload', {})
        if isinstance(p, dict) and 'longname' not in p:
            # map_report uses long_name / short_name (underscored), remap to match nodeinfo shape
            payload = dict(payload)
            payload['payload'] = {
                'longname':  p.get('long_name'),
                'shortname': p.get('short_name'),
                'hardware':  p.get('hw_model'),
                'role':      p.get('role'),
                # keep position fields for _upsert_map_report
                'latitude_i':  p.get('latitude_i'),
                'longitude_i': p.get('longitude_i'),
                'altitude':    p.get('altitude'),
                # extra map_report fields
                'firmware_version':  p.get('firmware_version'),
                'region':            p.get('region'),
                'modem_preset':      p.get('modem_preset'),
                'num_online_local_nodes': p.get('num_online_local_nodes'),
            }

    packet = {
        'topic': topic,
        'type': ptype,
        'from': node_id_to_hex(payload.get('from')),
        'to': node_id_to_hex(payload.get('to')),
        'sender': payload.get('sender'),
        'channel': channel,
        'rssi': payload.get('rssi'),
        'snr': payload.get('snr'),
        'hop_start': payload.get('hop_start'),
        'hops_away': payload.get('hops_away'),
        'raw': payload,
    }

    # Convenience field for rule matching
    if packet['type'] == 'text':
        packet['text'] = payload.get('payload', {}).get('text', '') if isinstance(payload.get('payload'), dict) else ''
    else:
        packet['text'] = ''

    return packet


# ---------------------------------------------------------------------------
# Serial-interface parsing
# ---------------------------------------------------------------------------

def _first(d, *keys):
    """First non-None value among d[key] for the given keys. The library emits
    camelCase or snake_case depending on version; a plain `a or b` fallback
    drops legitimate zero values (0% battery, 0 humidity), so check is None."""
    for k in keys:
        v = d.get(k)
        if v is not None:
            return v
    return None


def _payload_for_portnum(decoded, ptype):
    """
    Pull the type-specific payload off `decoded` and reshape it into the
    same dict layout the MQTT JSON gateway emits, so _upsert_node / rules /
    map / telemetry code paths stay identical.
    """
    if not isinstance(decoded, dict):
        return {}

    if ptype == 'text':
        text = decoded.get('text')
        if text is None:
            payload_bytes = decoded.get('payload')
            if isinstance(payload_bytes, (bytes, bytearray)):
                try:
                    text = payload_bytes.decode('utf-8', errors='replace')
                except Exception:
                    text = ''
            else:
                text = ''
        return {'text': text}

    if ptype == 'position':
        pos = decoded.get('position', {}) or {}
        return {
            'latitude_i':   _first(pos, 'latitudeI', 'latitude_i'),
            'longitude_i':  _first(pos, 'longitudeI', 'longitude_i'),
            'altitude':     pos.get('altitude'),
            'time':         pos.get('time'),
            'PDOP':         pos.get('PDOP'),
            'ground_speed': _first(pos, 'groundSpeed', 'ground_speed'),
            'sats_in_view': _first(pos, 'satsInView', 'sats_in_view'),
        }

    if ptype == 'nodeinfo':
        u = decoded.get('user', {}) or {}
        return {
            'id':               u.get('id'),
            'longname':         _first(u, 'longName', 'long_name'),
            'shortname':        _first(u, 'shortName', 'short_name'),
            'hardware':         _first(u, 'hwModel', 'hw_model'),
            'role':             u.get('role'),
            'macaddr':          u.get('macaddr'),
            'public_key':       _first(u, 'publicKey', 'public_key'),
            'is_licensed':      _first(u, 'isLicensed', 'is_licensed'),
            'is_unmessagable':  _first(u, 'isUnmessagable', 'is_unmessagable'),
        }

    if ptype == 'telemetry':
        t = decoded.get('telemetry', {}) or {}
        device  = t.get('deviceMetrics')      or t.get('device_metrics')      or {}
        env     = t.get('environmentMetrics') or t.get('environment_metrics') or {}
        # Match the MQTT JSON gateway: flat payload with battery + env mixed in.
        out = {}
        if device:
            out.update({
                'battery_level':       _first(device, 'batteryLevel', 'battery_level'),
                'voltage':             device.get('voltage'),
                'channel_utilization': _first(device, 'channelUtilization', 'channel_utilization'),
                'air_util_tx':         _first(device, 'airUtilTx', 'air_util_tx'),
                'uptime_seconds':      _first(device, 'uptimeSeconds', 'uptime_seconds'),
            })
        if env:
            out.update({
                'temperature':         env.get('temperature'),
                'relative_humidity':   _first(env, 'relativeHumidity', 'relative_humidity'),
                'barometric_pressure': _first(env, 'barometricPressure', 'barometric_pressure'),
                'gas_resistance':      _first(env, 'gasResistance', 'gas_resistance'),
                'iaq':                 env.get('iaq'),
            })
        out['time'] = t.get('time')
        return out

    if ptype == 'map_report':
        mr = decoded.get('mapReport') or decoded.get('map_report') or {}
        # Re-shape into the same fields the upsert path expects (matches the MQTT remap)
        return {
            'longname':              _first(mr, 'longName', 'long_name'),
            'shortname':             _first(mr, 'shortName', 'short_name'),
            'hardware':              _first(mr, 'hwModel', 'hw_model'),
            'role':                  mr.get('role'),
            'latitude_i':            _first(mr, 'latitudeI', 'latitude_i'),
            'longitude_i':           _first(mr, 'longitudeI', 'longitude_i'),
            'altitude':              mr.get('altitude'),
            'firmware_version':      _first(mr, 'firmwareVersion', 'firmware_version'),
            'region':                mr.get('region'),
            'modem_preset':          _first(mr, 'modemPreset', 'modem_preset'),
            'num_online_local_nodes': _first(mr, 'numOnlineLocalNodes', 'num_online_local_nodes'),
        }

    # Unknown / opaque app: keep whatever decoded gave us so the raw JSON survives in the DB.
    return decoded


def parse_serial_packet(raw):
    """
    Normalize a packet from the Meshtastic SerialInterface pub/sub callback into
    the same dict shape parse_packet() produces for MQTT, so _save_packet,
    rules, the map endpoints, and the UI all keep working unchanged.
    """
    if not isinstance(raw, dict):
        return {
            'topic': 'serial',
            'type': 'unknown',
            'from': None, 'to': None, 'sender': None,
            'channel': None, 'rssi': None, 'snr': None,
            'hop_start': None, 'hops_away': None,
            'raw': {}, 'text': '',
        }

    decoded = raw.get('decoded') or {}
    portnum = decoded.get('portnum') if isinstance(decoded, dict) else None
    ptype = _PORTNUM_TO_TYPE.get(portnum, 'unknown') if portnum else 'unknown'

    hop_start = raw.get('hopStart') if 'hopStart' in raw else raw.get('hop_start')
    hop_limit = raw.get('hopLimit') if 'hopLimit' in raw else raw.get('hop_limit')
    # hops_away on the MQTT side == hop_start - hop_limit (how many hops the packet actually took)
    hops_away = None
    if hop_start is not None and hop_limit is not None:
        try:
            hops_away = int(hop_start) - int(hop_limit)
        except (TypeError, ValueError):
            hops_away = None

    payload = _payload_for_portnum(decoded, ptype)

    # Build a payload dict shaped like the MQTT JSON gateway emits, so
    # downstream (history extraction, /api/map, telemetry endpoints) is unchanged.
    mqtt_shaped = {
        'from':      raw.get('from'),
        'to':        raw.get('to'),
        'id':        raw.get('id'),
        'channel':   raw.get('channel'),
        'rssi':      raw.get('rxRssi') if 'rxRssi' in raw else raw.get('rssi'),
        'snr':       raw.get('rxSnr')  if 'rxSnr'  in raw else raw.get('snr'),
        'hop_start': hop_start,
        'hop_limit': hop_limit,
        'hops_away': hops_away,
        'type':      ptype,
        'payload':   payload,
    }

    packet = {
        'topic':     'serial',
        'type':      ptype,
        'from':      node_id_to_hex(raw.get('from')),
        'to':        node_id_to_hex(raw.get('to')),
        'sender':    None,
        'channel':   raw.get('channel'),
        'rssi':      mqtt_shaped['rssi'],
        'snr':       mqtt_shaped['snr'],
        'hop_start': hop_start,
        'hops_away': hops_away,
        'raw':       mqtt_shaped,
        'text':      payload.get('text', '') if ptype == 'text' and isinstance(payload, dict) else '',
    }
    return packet
