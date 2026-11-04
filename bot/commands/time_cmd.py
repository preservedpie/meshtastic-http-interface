"""Time command — current server time."""

from datetime import datetime, timezone


def time_command(args: str, context: dict) -> str:
    now = datetime.now(timezone.utc)
    return "\n".join([
        f"Time: {now.strftime('%Y-%m-%d')}",
        f"UTC: {now.strftime('%H:%M:%S')}",
        f"Unix: {int(now.timestamp())}",
    ])
