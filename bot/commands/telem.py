"""Telem command — battery / device metrics for heard nodes."""


def telem_command(args: str, context: dict) -> str:
    node_log = context.get('node_log', {})
    telem_nodes = {
        nid: info for nid, info in node_log.items()
        if info.get('battery_level') is not None or info.get('voltage') is not None
    }
    if not telem_nodes:
        return "No telemetry data yet. Waiting for device packets."

    sorted_nodes = sorted(telem_nodes.items(),
                          key=lambda x: x[1].get('battery_level', 999))
    parts = []
    for nid, info in sorted_nodes:
        name = info.get('short_name') or f"!{nid:08x}"[-6:]
        batt = info.get('battery_level')
        volt = info.get('voltage')
        entry = name
        if batt is not None:
            flag = "!" if batt < 20 else ""
            entry += f" {batt}%{flag}"
        if volt is not None:
            entry += f" {volt:.1f}V"
        uptime = info.get('uptime_seconds')
        if uptime is not None:
            if uptime < 3600:
                entry += f" up:{uptime // 60}m"
            else:
                entry += f" up:{uptime // 3600}h"
        parts.append(entry)
    return ("Telem: " + " | ".join(parts))[:200]
