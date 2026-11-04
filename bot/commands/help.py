"""Help command — list built-in commands and enabled custom modules."""

from .. import db


def help_command(args: str, context: dict) -> str:
    from . import COMMANDS

    lines = ["Commands:"]
    for cmd in COMMANDS:
        desc = cmd.get('description', '')
        if len(desc) > 30:
            desc = desc[:27] + "..."
        lines.append(f"• {cmd['name']}: {desc}")

    try:
        custom = db.list_modules(only_enabled=True)
    except Exception:
        custom = []
    for mod in custom:
        desc = mod.get('description', '')
        if len(desc) > 30:
            desc = desc[:27] + "..."
        lines.append(f"• {mod['name']}: {desc}")
    return "\n".join(lines)
