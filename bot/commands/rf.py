"""RF command — serial port / response channel summary.

The original MQTT-oriented version is replaced because this app is
serial-only. Falls back to live device info via the meshtastic interface
when available.
"""

import os
from .. import settings


def rf_command(args: str, context: dict) -> str:
    port = os.environ.get('MESHTASTIC_SERIAL_PORT', '?')
    response_ch = context.get('response_channel', '?')
    bot_node = settings.get_int('BOT_NODE_NUMBER', 0)

    lines = [
        "Radio Config:",
        f"Port: {port}",
        f"Response ch: {response_ch}",
    ]
    if bot_node:
        lines.append(f"Bot node: {bot_node} (!{bot_node:08x})")
    return "\n".join(lines)
