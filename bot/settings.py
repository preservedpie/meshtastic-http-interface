"""Runtime settings for the bot — ported from meshtastic-mesh-bot.

Resolution order:
    1. value in the bot SQLite ``settings`` table (live, GUI-editable)
    2. environment variable / .env
    3. hard-coded default
"""

import os
import logging
from typing import Optional, Set

from . import db

logger = logging.getLogger('mesh-bot.settings')

SCHEMA: list = [
    # --- LLM ---
    {'key': 'LLM_FALLBACK_ENABLED', 'label': 'LLM fallback enabled',
     'kind': 'bool', 'group': 'LLM', 'default': 'true'},
    {'key': 'LLM_HOST', 'label': 'LLM host', 'kind': 'text',
     'group': 'LLM', 'default': 'localhost'},
    {'key': 'LLM_PORT', 'label': 'LLM port', 'kind': 'int',
     'group': 'LLM', 'default': '11434'},
    {'key': 'LLM_MODEL', 'label': 'LLM model', 'kind': 'text',
     'group': 'LLM', 'default': 'llama3'},
    {'key': 'LLM_TIMEOUT', 'label': 'LLM timeout (s)', 'kind': 'int',
     'group': 'LLM', 'default': '60'},
    {'key': 'LLM_SYSTEM_PROMPT', 'label': 'LLM persona / system prompt',
     'kind': 'textarea', 'group': 'LLM',
     'default': ('You are a helpful assistant responding over a low-bandwidth '
                 'mesh radio network. Be extremely concise and direct. No '
                 'introductions, no filler words, just the essential answer.')},
    {'key': 'MAX_HISTORY_TURNS', 'label': 'LLM history turns', 'kind': 'int',
     'group': 'LLM', 'default': '10'},
    {'key': 'HISTORY_TIMEOUT_MINUTES', 'label': 'LLM history timeout (min)',
     'kind': 'int', 'group': 'LLM', 'default': '5'},

    # --- Bot behaviour ---
    {'key': 'BOT_ENABLED', 'label': 'Bot enabled (master switch)',
     'kind': 'bool', 'group': 'Bot', 'default': 'true'},
    {'key': 'BOT_NAME', 'label': 'Bot name', 'kind': 'text',
     'group': 'Bot', 'default': 'MeshBot'},
    {'key': 'BOT_NODE_NUMBER', 'label': 'Bot node number (decimal)',
     'kind': 'int', 'group': 'Bot', 'default': '0'},
    {'key': 'COMMAND_PREFIX', 'label': 'Command prefix', 'kind': 'text',
     'group': 'Bot', 'default': ''},
    {'key': 'REQUIRE_PREFIX', 'label': 'Require prefix', 'kind': 'bool',
     'group': 'Bot', 'default': 'true'},
    {'key': 'RATE_LIMIT_SECONDS', 'label': 'Rate limit (s/node)',
     'kind': 'int', 'group': 'Bot', 'default': '30'},
    {'key': 'MAX_RESPONSE_LENGTH', 'label': 'Max chars per message',
     'kind': 'int', 'group': 'Bot', 'default': '200'},
    {'key': 'MAX_REPLY_MESSAGES', 'label': 'Max messages per reply (1-4)',
     'kind': 'int', 'group': 'Bot', 'default': '4'},
    {'key': 'INTER_SEND_DELAY_MS',
     'label': 'Delay between consecutive sent messages (ms)',
     'kind': 'int', 'group': 'Bot', 'default': '4000'},
    {'key': 'ACK_TIMEOUT_SECONDS',
     'label': 'DM delivery ACK timeout (s)',
     'kind': 'int', 'group': 'Bot', 'default': '15'},
    {'key': 'IGNORED_NODES', 'label': 'Ignored nodes (comma sep, int or !hex)',
     'kind': 'text', 'group': 'Bot', 'default': ''},
    {'key': 'STARTUP_ANNOUNCE', 'label': 'Send "online" message on startup',
     'kind': 'bool', 'group': 'Bot', 'default': 'false'},

    # --- Connection / watchdog ---
    {'key': 'SERIAL_WATCHDOG_TIMEOUT_SECONDS',
     'label': 'Reconnect if no radio data for (s)',
     'kind': 'int', 'group': 'Connection', 'default': '3600'},
    {'key': 'SERIAL_HEARTBEAT_INTERVAL_SECONDS',
     'label': 'Local radio heartbeat probe interval (s)',
     'kind': 'int', 'group': 'Connection', 'default': '300'},

    # --- Channels ---
    {'key': 'DEFAULT_RESPONSE_CHANNEL',
     'label': 'Default response channel (when no channel policy is response)',
     'kind': 'int', 'group': 'Channels', 'default': '0'},

    # --- Weather alert ---
    {'key': 'WEATHER_ALERT_ENABLED', 'label': 'Daily weather alert',
     'kind': 'bool', 'group': 'Weather', 'default': 'false'},
    {'key': 'WEATHER_ALERT_HOUR', 'label': 'Weather alert hour (0-23)',
     'kind': 'int', 'group': 'Weather', 'default': '8'},
    {'key': 'WEATHER_ALERT_TZ', 'label': 'Weather alert timezone',
     'kind': 'text', 'group': 'Weather', 'default': 'America/Chicago'},
    {'key': 'DEFAULT_LOCATION', 'label': 'Default location for weather/sun',
     'kind': 'text', 'group': 'Weather', 'default': 'New York'},
    {'key': 'WEATHER_ALERT_PER_NODE_ENABLED',
     'label': "DM each node its own local weather alert",
     'kind': 'bool', 'group': 'Weather', 'default': 'false'},
    {'key': 'WEATHER_ALERT_MAX_POSITION_AGE_HOURS',
     'label': 'Max node position age for per-node alerts (hours)',
     'kind': 'int', 'group': 'Weather', 'default': '168'},
    {'key': 'WEATHER_ALERT_ZERO_HOP_ONLY',
     'label': 'Only DM nodes at zero hops (direct radio range)',
     'kind': 'bool', 'group': 'Weather', 'default': 'false'},
    {'key': 'WEATHER_ALERT_WATCHED_NODES',
     'label': 'Watched nodes for weather alerts (comma sep, int or !hex; '
              'empty = all CLIENT-role nodes)',
     'kind': 'text', 'group': 'Weather', 'default': ''},

    # --- Modules ---
    {'key': 'ALLOW_NETWORK_COMMANDS',
     'label': 'Allow network commands (pingip/gw/wan/nmap) from mesh users',
     'kind': 'bool', 'group': 'Modules', 'default': 'false'},
    {'key': 'ALLOW_PYTHON_MODULES', 'label': 'Allow raw-Python modules',
     'kind': 'bool', 'group': 'Modules', 'default': 'false'},
    {'key': 'SHELL_ALLOWLIST', 'label': 'Shell command allowlist (comma sep)',
     'kind': 'text', 'group': 'Modules', 'default': 'echo,uptime,date,df,free'},
]

_SCHEMA_BY_KEY = {s['key']: s for s in SCHEMA}


def get(key: str, default: Optional[str] = None) -> Optional[str]:
    val = db.get_setting(key)
    if val is not None:
        return val
    env = os.getenv(key)
    if env is not None:
        return env
    schema = _SCHEMA_BY_KEY.get(key)
    if schema is not None:
        return schema['default']
    return default


def get_int(key: str, default: int = 0) -> int:
    val = get(key)
    try:
        return int(val) if val is not None and val != '' else default
    except (ValueError, TypeError):
        return default


def get_bool(key: str, default: bool = False) -> bool:
    val = get(key)
    if val is None:
        return default
    return str(val).strip().lower() in ('1', 'true', 'yes', 'on')


def set(key: str, value: str) -> None:
    db.set_setting(key, value)
    logger.info(f"Setting updated: {key} = {value!r}")


def reset(key: str) -> None:
    db.delete_setting(key)


def all_with_values() -> list:
    overrides = db.get_all_settings()
    out = []
    for s in SCHEMA:
        item = dict(s)
        item['value'] = get(s['key'])
        item['overridden'] = s['key'] in overrides
        out.append(item)
    return out


def _parse_node_id(s: str) -> int:
    s = s.strip()
    if s.startswith('!'):
        return int(s[1:], 16)
    return int(s)


def _is_node_id(s: str) -> bool:
    try:
        _parse_node_id(s)
        return True
    except ValueError:
        return False


def get_ignored_nodes() -> Set[int]:
    raw = get('IGNORED_NODES', '') or ''
    return {
        _parse_node_id(n)
        for n in raw.split(',')
        if n.strip() and _is_node_id(n)
    }


def add_ignored_node(node_id) -> None:
    nodes = get_ignored_nodes()
    nodes.add(_parse_node_id(str(node_id)))
    set('IGNORED_NODES', ','.join(str(n) for n in sorted(nodes)))


def remove_ignored_node(node_id) -> None:
    nodes = get_ignored_nodes()
    nodes.discard(_parse_node_id(str(node_id)))
    set('IGNORED_NODES', ','.join(str(n) for n in sorted(nodes)))


def get_watched_nodes() -> Set[int]:
    """Explicit allow-list for per-node weather alerts. Empty means no
    explicit list — callers should fall back to a role-based default
    (CLIENT nodes only) instead of treating this as "watch nothing"."""
    raw = get('WEATHER_ALERT_WATCHED_NODES', '') or ''
    return {
        _parse_node_id(n)
        for n in raw.split(',')
        if n.strip() and _is_node_id(n)
    }


def add_watched_node(node_id) -> None:
    nodes = get_watched_nodes()
    nodes.add(_parse_node_id(str(node_id)))
    set('WEATHER_ALERT_WATCHED_NODES', ','.join(str(n) for n in sorted(nodes)))


def remove_watched_node(node_id) -> None:
    nodes = get_watched_nodes()
    nodes.discard(_parse_node_id(str(node_id)))
    set('WEATHER_ALERT_WATCHED_NODES', ','.join(str(n) for n in sorted(nodes)))


def seed_defaults_from_env() -> None:
    existing = db.get_all_settings()
    for s in SCHEMA:
        if s['key'] in existing:
            continue
        env = os.getenv(s['key'])
        if env is not None:
            db.set_setting(s['key'], env)
