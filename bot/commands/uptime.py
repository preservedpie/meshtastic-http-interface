"""Uptime command — how long the bot has been running."""


def uptime_command(args: str, context: dict) -> str:
    uptime = context.get('bot_uptime')
    if not uptime:
        return "Uptime unknown"
    total = int(uptime.total_seconds())
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if seconds or not parts:
        parts.append(f"{seconds}s")
    return f"Online: {' '.join(parts)}"
