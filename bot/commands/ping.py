"""Ping command — simple acknowledgement with latency."""

from datetime import datetime, timezone


def ping_command(args: str, context: dict) -> str:
    msg_timestamp = context.get('timestamp')
    received_at = context.get('received_at')
    if msg_timestamp and received_at:
        sent = datetime.fromtimestamp(msg_timestamp, tz=timezone.utc)
        latency = (received_at - sent).total_seconds()
        return f"Pong! (latency: {latency:.1f}s)"
    return "Pong!"
