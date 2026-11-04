"""Status command — message reception diagnostics."""

from datetime import datetime, timezone


def status_command(args: str, context: dict) -> str:
    lines = ["Status Report"]

    from_node = context.get('from')
    if from_node:
        lines.append(f"From: {from_node} (!{from_node:08x})")

    msg_timestamp = context.get('timestamp')
    received_at = context.get('received_at')

    sent_time = None
    if msg_timestamp:
        sent_time = datetime.fromtimestamp(msg_timestamp, tz=timezone.utc)
        lines.append(f"Sent: {sent_time.strftime('%H:%M:%S UTC')}")

    if received_at:
        lines.append(f"Rcvd: {received_at.strftime('%H:%M:%S UTC')}")

    if sent_time and received_at:
        latency = (received_at - sent_time).total_seconds()
        lines.append(f"Latency: {latency:.1f}s")

    rssi = context.get('rssi')
    snr = context.get('snr')
    if rssi is not None:
        if rssi > -90:
            quality = "Excellent"
        elif rssi > -100:
            quality = "Good"
        elif rssi > -110:
            quality = "Fair"
        else:
            quality = "Weak"
        lines.append(f"RSSI: {rssi} dBm ({quality})")
    if snr is not None:
        lines.append(f"SNR: {snr} dB")

    hop_start = context.get('hop_start')
    hops_away = context.get('hops_away')
    if hop_start is not None and hops_away is not None:
        # hops_away (from the parser) is hop_start - hop_limit: hops actually
        # traveled. What's left of the hop budget is hop_start - hops_away.
        hop_str = "Direct (no hops)" if hops_away == 0 else f"{hops_away} hop(s)"
        lines.append(f"Path: {hop_str}")
        lines.append(f"TTL: {hop_start - hops_away} remaining")

    sender = context.get('sender')
    if sender:
        lines.append(f"Gateway: {sender}")

    lines.append("You're being heard!")
    return "\n".join(lines)
