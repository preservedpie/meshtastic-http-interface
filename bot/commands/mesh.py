"""Mesh command — network health snapshot."""

from datetime import datetime, timezone, timedelta

RECENT_WINDOW = timedelta(hours=1)


def mesh_command(args: str, context: dict) -> str:
    node_log = context.get('node_log', {})
    if not node_log:
        return "No nodes heard yet."

    now = datetime.now(timezone.utc)
    recent = {
        nid: info for nid, info in node_log.items()
        if info.get('last_seen') and now - info['last_seen'] < RECENT_WINDOW
    }

    total = len(node_log)
    active = len(recent)
    rssi_vals = [i['rssi'] for i in recent.values() if i.get('rssi') is not None]
    direct = sum(1 for i in recent.values() if i.get('hops_away') == 0)
    relayed = active - direct

    ch_utils = [i['channel_utilization'] for i in node_log.values()
                if i.get('channel_utilization') is not None]
    air_utils = [i['air_util_tx'] for i in node_log.values()
                 if i.get('air_util_tx') is not None]

    parts = [f"Net: {active}/{total} nodes active ({direct}d {relayed}r)"]
    if rssi_vals:
        avg = sum(rssi_vals) // len(rssi_vals)
        parts.append(f"RSSI avg:{avg} hi:{max(rssi_vals)} lo:{min(rssi_vals)}")
    if ch_utils:
        parts.append(f"ch_util:{sum(ch_utils)/len(ch_utils):.1f}%")
    if air_utils:
        parts.append(f"air_tx:{sum(air_utils)/len(air_utils):.1f}%")
    return " | ".join(parts)
