"""Nodes command — recently heard nodes with signal and battery."""

from datetime import datetime, timezone


def nodes_command(args: str, context: dict) -> str:
    node_log = context.get('node_log', {})
    if not node_log:
        return "No nodes heard yet."

    now = datetime.now(timezone.utc)
    lines = [f"Nodes ({len(node_log)}):"]

    for node_id, info in sorted(node_log.items(),
                                key=lambda x: x[1].get('last_seen') or now,
                                reverse=True):
        last_seen = info.get('last_seen')
        if last_seen:
            ago_secs = int((now - last_seen).total_seconds())
            if ago_secs < 60:
                ago = f"{ago_secs}s"
            elif ago_secs < 3600:
                ago = f"{ago_secs // 60}m"
            else:
                ago = f"{ago_secs // 3600}h"
        else:
            ago = "?"

        name = info.get('short_name') or f"!{node_id:08x}"
        rssi = info.get('rssi')
        hops = info.get('hops_away')
        batt = info.get('battery_level')

        parts = [f"{name} {ago}ago"]
        if rssi is not None:
            parts.append(f"{rssi}dBm")
        if hops is not None:
            parts.append("direct" if hops == 0 else f"{hops}hops")
        if batt is not None:
            flag = "!" if batt < 20 else ""
            parts.append(f"{batt}%{flag}")
        lines.append(" ".join(parts))

    return "\n".join(lines)
