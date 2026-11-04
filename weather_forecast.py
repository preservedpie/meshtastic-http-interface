"""Off-grid weather forecasting from locally-stored Meshtastic sensor telemetry.

No internet, no numerical weather model — everything here is derived purely
from temperature/humidity/pressure readings already sitting in the packets
table. Two different techniques are used depending on how far out the
prediction reaches, because they have very different amounts of real skill:

  - 24-48h "nowcast": classic pressure-tendency (barometer trend) forecasting
    — the same technique sailors and farmers used for centuries before
    numerical weather prediction existed. A single station's pressure trend
    genuinely does carry short-range signal.
  - 3-7 day rain "outlook": a single local barometer cannot resolve
    synoptic-scale systems days in advance, so this is deliberately NOT
    presented as a forecast. It's a climatological frequency — how often a
    rain-signature pressure drop occurred in the recent historical window —
    labeled Low/Moderate/Elevated rather than a fake precise percentage.
  - Hourly temperature curve: each node's own hour-of-day climatology,
    blended with the current trend offset that decays back to pure
    climatology by day 7.
"""

import json
from datetime import datetime, timedelta

# Metrics this module knows how to use for the weather station config UI.
METRICS = ('temperature', 'relative_humidity', 'barometric_pressure', 'iaq', 'gas_resistance')
# Subset actually used by the forecasting algorithm below.
CORE_METRICS = ('temperature', 'relative_humidity', 'barometric_pressure')

_TS_FMT = '%Y-%m-%d %H:%M:%S'


def _parse_ts(t: str):
    try:
        return datetime.strptime(t.split('.')[0].replace('T', ' '), _TS_FMT)
    except Exception:
        return None


def get_config(conn) -> dict:
    """metric -> [node_id, ...] currently assigned to feed that metric."""
    cfg = {m: [] for m in METRICS}
    for r in conn.execute("SELECT metric, node_id FROM weather_station_config"):
        cfg.setdefault(r['metric'], []).append(r['node_id'])
    return cfg


def set_config(conn, metric: str, node_ids: list) -> None:
    if metric not in METRICS:
        raise ValueError(f"unknown metric {metric!r}")
    conn.execute("DELETE FROM weather_station_config WHERE metric = ?", (metric,))
    for nid in node_ids:
        conn.execute(
            "INSERT OR IGNORE INTO weather_station_config (metric, node_id) VALUES (?, ?)",
            (metric, nid),
        )
    conn.commit()


def _series(conn, node_ids: list, metric: str, since_dt=None) -> list:
    """[{'t': datetime, 'v': float}, ...] oldest-first, averaged across nodes
    at read time (i.e. just pooled — not bucketed by timestamp)."""
    if not node_ids:
        return []
    placeholders = ','.join('?' * len(node_ids))
    conditions = [f"from_node IN ({placeholders})", "packet_type = 'telemetry'"]
    params = list(node_ids)
    if since_dt:
        conditions.append("received_at >= ?")
        params.append(since_dt.strftime(_TS_FMT))
    where = " AND ".join(conditions)
    rows = conn.execute(
        f"SELECT received_at, payload_json FROM packets WHERE {where} ORDER BY received_at ASC",
        params,
    ).fetchall()
    out = []
    for row in rows:
        try:
            raw = json.loads(row['payload_json']) if isinstance(row['payload_json'], str) else row['payload_json']
            payload = raw.get('payload', {}) if isinstance(raw, dict) else {}
            v = payload.get(metric)
            t = _parse_ts(row['received_at'])
            if v is not None and t is not None:
                out.append({'t': t, 'v': float(v)})
        except Exception:
            pass
    return out


def _avg(vals):
    vals = [v for v in vals if v is not None]
    return sum(vals) / len(vals) if vals else None


def _value_near(series, target_dt, tolerance_hours=1.0):
    """Average of points within `tolerance_hours` of target_dt — used to read
    a pressure/temp value "as of" a point a few hours in the past."""
    window = [p['v'] for p in series if abs((p['t'] - target_dt).total_seconds()) <= tolerance_hours * 3600]
    return _avg(window)


def latest_readings(conn, cfg: dict) -> dict:
    """Current conditions: most recent value per configured metric, averaged
    across whichever assigned nodes have reported in the last 3 hours."""
    now = datetime.utcnow()
    out = {}
    for metric in METRICS:
        nodes = cfg.get(metric) or []
        if not nodes:
            out[metric] = None
            continue
        series = _series(conn, nodes, metric, since_dt=now - timedelta(hours=3))
        if not series:
            out[metric] = None
            continue
        out[metric] = {
            'value': round(_avg([p['v'] for p in series]), 2),
            'at': series[-1]['t'].strftime(_TS_FMT),
        }
    return out


# ---------------------------------------------------------------------------
# 24-48h nowcast — pressure tendency
# ---------------------------------------------------------------------------

def _pressure_tendency(pressure_series, reference_time):
    """3-hour pressure tendency in hPa: reading at reference_time vs ~3h
    before it. None if we don't have enough history to compare."""
    p_now = _value_near(pressure_series, reference_time, tolerance_hours=0.75)
    p_3h = _value_near(pressure_series, reference_time - timedelta(hours=3), tolerance_hours=1.0)
    if p_now is None or p_3h is None:
        return None
    return p_now - p_3h


_STALE_AFTER_HOURS = 6


def nowcast(conn, cfg: dict) -> dict:
    """24h/48h rain-chance nowcast from pressure trend + level + humidity.
    Returns None fields with a `reason` if pressure isn't configured."""
    pressure_nodes = cfg.get('barometric_pressure') or []
    if not pressure_nodes:
        return {'available': False, 'reason': 'No node assigned to barometric pressure yet.'}

    now = datetime.utcnow()
    pressure_series = _series(conn, pressure_nodes, 'barometric_pressure', since_dt=now - timedelta(hours=8))
    if len(pressure_series) < 2:
        return {'available': False, 'reason': 'Not enough pressure history yet (need a few hours of readings).'}

    # Anchor "now" on the latest actual reading rather than wall-clock time —
    # a node reporting every 15-60 min will routinely be outside a strict
    # ±1h wall-clock window, and stale-but-present data shouldn't crash.
    latest = pressure_series[-1]
    if (now - latest['t']).total_seconds() > _STALE_AFTER_HOURS * 3600:
        return {'available': False, 'reason': f"Pressure data is more than {_STALE_AFTER_HOURS}h old — check the assigned node is still reporting."}

    p_now = latest['v']
    trend_3h = _pressure_tendency(pressure_series, latest['t'])

    humidity_nodes = cfg.get('relative_humidity') or []
    humidity_series = _series(conn, humidity_nodes, 'relative_humidity', since_dt=now - timedelta(hours=3)) if humidity_nodes else []
    humidity_now = _avg([p['v'] for p in humidity_series]) if humidity_series else None

    # Base chance from absolute pressure level (hPa at sea level would be
    # more precise, but stations here report raw local pressure — the
    # thresholds are wide bands, not tuned to altitude).
    if p_now < 1000:
        base = 60
    elif p_now < 1013:
        base = 35
    elif p_now < 1020:
        base = 15
    else:
        base = 5

    trend_label = 'steady'
    trend_adjust = 0
    if trend_3h is not None:
        if trend_3h <= -3:
            trend_label, trend_adjust = 'falling fast', 25
        elif trend_3h <= -1:
            trend_label, trend_adjust = 'falling', 10
        elif trend_3h >= 3:
            trend_label, trend_adjust = 'rising fast', -20
        elif trend_3h >= 1:
            trend_label, trend_adjust = 'rising', -10

    humidity_adjust = 0
    if humidity_now is not None:
        if humidity_now >= 80:
            humidity_adjust = 10
        elif humidity_now <= 40:
            humidity_adjust = -10

    chance_24h = max(0, min(95, round(base + trend_adjust + humidity_adjust)))
    # 48h uncertainty grows — pull halfway back toward the neutral base rate.
    chance_48h = max(0, min(95, round((chance_24h + base) / 2)))

    if trend_label in ('falling fast',):
        summary = 'Pressure falling fast — unsettled weather likely within 24h.'
    elif trend_label == 'falling':
        summary = 'Pressure falling — conditions trending toward showers.'
    elif trend_label in ('rising', 'rising fast'):
        summary = 'Pressure rising — conditions trending toward clear/fair.'
    else:
        summary = 'Pressure steady — little short-term change expected.'

    return {
        'available': True,
        'pressure_hpa': round(p_now, 1),
        'trend_3h_hpa': round(trend_3h, 1) if trend_3h is not None else None,
        'trend_label': trend_label,
        'humidity_pct': round(humidity_now, 1) if humidity_now is not None else None,
        'rain_chance_24h': chance_24h,
        'rain_chance_48h': chance_48h,
        'summary': summary,
    }


# ---------------------------------------------------------------------------
# 3-7 day rain outlook — recent-history climatology, not a forecast
# ---------------------------------------------------------------------------

_OUTLOOK_LOOKBACK_DAYS = 30
_RAIN_SIGNATURE_DROP_HPA = 3.0  # drop within a 12h window that historically preceded rain


def _rain_signature_days(pressure_series, lookback_days):
    """Count of distinct calendar days (UTC) in the lookback window where
    pressure dropped by _RAIN_SIGNATURE_DROP_HPA or more within any 12h span."""
    if len(pressure_series) < 2:
        return 0, 0
    by_day = {}
    for p in pressure_series:
        by_day.setdefault(p['t'].date(), []).append(p)
    hit_days = 0
    for day, points in by_day.items():
        points = sorted(points, key=lambda p: p['t'])
        hit = False
        for i, a in enumerate(points):
            for b in points[i + 1:]:
                if (b['t'] - a['t']).total_seconds() > 12 * 3600:
                    break
                if a['v'] - b['v'] >= _RAIN_SIGNATURE_DROP_HPA:
                    hit = True
                    break
            if hit:
                break
        if hit:
            hit_days += 1
    return hit_days, len(by_day)


def _bucket(frequency):
    if frequency is None:
        return 'unknown'
    if frequency >= 0.35:
        return 'elevated'
    if frequency >= 0.15:
        return 'moderate'
    return 'low'


def rain_outlook(conn, cfg: dict, trend_label: str = 'steady') -> dict:
    pressure_nodes = cfg.get('barometric_pressure') or []
    if not pressure_nodes:
        return {'available': False, 'reason': 'No node assigned to barometric pressure yet.', 'days': []}

    now = datetime.utcnow()
    series = _series(conn, pressure_nodes, 'barometric_pressure', since_dt=now - timedelta(days=_OUTLOOK_LOOKBACK_DAYS))
    hit_days, total_days = _rain_signature_days(series, _OUTLOOK_LOOKBACK_DAYS)
    if total_days < 5:
        return {'available': False, 'reason': 'Not enough pressure history yet (need at least ~5 days).', 'days': []}

    frequency = hit_days / total_days
    base_bucket = _bucket(frequency)

    # A currently-unsettled trend nudges the near days (3-4 out) up a level;
    # it decays back to pure climatology by day 6-7.
    levels = ['low', 'moderate', 'elevated']
    bump = 1 if trend_label in ('falling', 'falling fast') else 0

    days = []
    for day_offset in range(3, 8):
        idx = levels.index(base_bucket)
        local_bump = bump if day_offset <= 4 else 0
        level = levels[min(idx + local_bump, len(levels) - 1)]
        days.append({'day_offset': day_offset, 'level': level})

    return {
        'available': True,
        'lookback_days': total_days,
        'historical_rain_signature_frequency': round(frequency, 2),
        'days': days,
    }


# ---------------------------------------------------------------------------
# Hourly temperature — diurnal climatology + decaying trend offset
# ---------------------------------------------------------------------------

_TEMP_HISTORY_DAYS = 30
_TEMP_FORECAST_HOURS = 7 * 24


def hourly_temperature_forecast(conn, cfg: dict) -> dict:
    temp_nodes = cfg.get('temperature') or []
    if not temp_nodes:
        return {'available': False, 'reason': 'No node assigned to temperature yet.', 'series': []}

    now = datetime.utcnow()
    series = _series(conn, temp_nodes, 'temperature', since_dt=now - timedelta(days=_TEMP_HISTORY_DAYS))
    if len(series) < 24:
        return {'available': False, 'reason': 'Not enough temperature history yet (need at least ~24h).', 'series': []}

    # Hour-of-day climatology: average temp seen at each hour, across all
    # historical days we have.
    by_hour = {h: [] for h in range(24)}
    for p in series:
        by_hour[p['t'].hour].append(p['v'])
    clim = {h: (_avg(vals) if vals else None) for h, vals in by_hour.items()}
    overall_avg = _avg([v for vals in by_hour.values() for v in vals])
    for h in range(24):
        if clim[h] is None:
            clim[h] = overall_avg  # sparse-data fallback

    # Recent trend offset: how much warmer/cooler the last 24h have run
    # compared to climatology for those same hours — decays to 0 by day 7.
    recent = [p for p in series if p['t'] >= now - timedelta(hours=24)]
    if recent:
        recent_avg = _avg([p['v'] for p in recent])
        recent_clim_avg = _avg([clim[p['t'].hour] for p in recent])
        offset_now = recent_avg - recent_clim_avg if recent_clim_avg is not None else 0
    else:
        offset_now = 0

    out = []
    current_hour = now.replace(minute=0, second=0, microsecond=0)
    for i in range(1, _TEMP_FORECAST_HOURS + 1):
        future = current_hour + timedelta(hours=i)
        decay = max(0.0, 1 - (i / _TEMP_FORECAST_HOURS))
        predicted = clim[future.hour] + offset_now * decay
        out.append({'t': future.strftime(_TS_FMT), 'v': round(predicted, 1)})

    return {'available': True, 'series': out}


# ---------------------------------------------------------------------------
# Aggregate
# ---------------------------------------------------------------------------

def build_forecast(conn) -> dict:
    cfg = get_config(conn)
    current = latest_readings(conn, cfg)
    nc = nowcast(conn, cfg)
    outlook = rain_outlook(conn, cfg, trend_label=nc.get('trend_label', 'steady'))
    temp = hourly_temperature_forecast(conn, cfg)
    return {
        'generated_at': datetime.utcnow().strftime(_TS_FMT) + 'Z',
        'config': cfg,
        'current': current,
        'nowcast': nc,
        'outlook': outlook,
        'hourly_temperature': temp,
    }
