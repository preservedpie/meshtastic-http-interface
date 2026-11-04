"""Command registry — ported from meshtastic-mesh-bot."""

from .status import status_command
from .ping import ping_command
from .time_cmd import time_command
from .uptime import uptime_command
from .weather import weather_command, check_weather_alert, check_weather_alert_at  # noqa: F401
from .help import help_command
from .echo import echo_command
from .dice import dice_command
from .nodes import nodes_command
from .rf import rf_command
from .mesh import mesh_command
from .telem import telem_command
from .sun import sun_command
from .news import news_command
from .netinfo import (pingip_command, gw_command, wan_command, nmap_command)
from .timeinfo import date_command, ntp_command
from .ask import ask_command, query_llm, clear_history  # noqa: F401
from .. import settings


def network_commands_enabled() -> bool:
    """pingip/gw/wan/nmap run from the host's own network, and any mesh user
    on a channel the bot listens to can trigger them - so they're opt-in."""
    return settings.get_bool('ALLOW_NETWORK_COMMANDS', False)


def _network_only(handler):
    def gated(args: str, context: dict) -> str:
        if not network_commands_enabled():
            return "Network commands are disabled on this bot."
        return handler(args, context)
    return gated


def new_session_command(args: str, context: dict) -> str:
    clear_history(context.get('from'))
    return "New session started."


COMMANDS = [
    {'name': 'status', 'handler': status_command,
     'aliases': ['stat', 'report'],
     'phrases': ['status report', 'am i being heard', 'can you hear me'],
     'description': 'Show message reception details (RSSI, SNR, hops)'},
    {'name': 'ping', 'handler': ping_command, 'aliases': ['p'],
     'phrases': ['ping'], 'description': 'Simple ping response'},
    {'name': 'time', 'handler': time_command, 'aliases': ['t', 'clock'],
     'phrases': ['what time is it', 'current time'],
     'description': 'Current server time'},
    {'name': 'uptime', 'handler': uptime_command, 'aliases': ['up'],
     'phrases': ['how long have you been running'],
     'description': 'Bot uptime'},
    {'name': 'weather', 'handler': weather_command, 'aliases': ['wx', 'wttr'],
     'phrases': ['weather in', 'forecast'],
     'description': 'Weather: weather [tomorrow|3d|7d] [city]'},
    {'name': 'help', 'handler': help_command,
     'aliases': ['h', '?', 'commands'],
     'phrases': ['help me', 'what can you do'],
     'description': 'Show available commands'},
    {'name': 'echo', 'handler': echo_command, 'aliases': ['say'],
     'phrases': [], 'description': 'Echo back your message'},
    {'name': 'dice', 'handler': dice_command, 'aliases': ['roll', 'd'],
     'phrases': ['roll a die', 'roll dice'],
     'description': 'Roll dice (usage: dice 2d6)'},
    {'name': 'nodes', 'handler': nodes_command, 'aliases': ['n', 'heard'],
     'phrases': ['who is on the mesh', 'list nodes'],
     'description': 'List recently heard nodes with signal and battery'},
    {'name': 'mesh', 'handler': mesh_command,
     'aliases': ['net', 'network'],
     'phrases': ['network health', 'mesh health', 'how is the network'],
     'description': 'Network health snapshot'},
    {'name': 'telem', 'handler': telem_command,
     'aliases': ['battery', 'batt'],
     'phrases': ['battery status', 'device health', 'battery levels'],
     'description': 'Battery and device metrics for heard nodes'},
    {'name': 'rf', 'handler': rf_command, 'aliases': ['radio', 'config'],
     'phrases': ['radio config', 'show config'],
     'description': 'Show radio/gateway configuration'},
    {'name': 'sun', 'handler': sun_command,
     'aliases': ['sunrise', 'sunset'],
     'phrases': ['sunrise', 'sunset', 'when does the sun rise'],
     'description': 'Sunrise/sunset times (usage: sun <city>)'},
    {'name': 'news', 'handler': news_command, 'aliases': ['headlines'],
     'phrases': ['latest news', 'top news'],
     'description': 'Top headlines (usage: news [us|world|tech|sci])'},
    {'name': 'pingip', 'handler': _network_only(pingip_command), 'network': True, 'aliases': ['png'],
     'phrases': [], 'description': 'Ping an IP/host (usage: pingip <host>)'},
    {'name': 'gw', 'handler': _network_only(gw_command), 'network': True, 'aliases': ['gateway'],
     'phrases': ['ping gateway'],
     'description': 'Ping the default gateway'},
    {'name': 'wan', 'handler': _network_only(wan_command), 'network': True, 'aliases': ['inet', 'internet'],
     'phrases': ['ping internet', 'is the internet up'],
     'description': 'Ping the internet (usage: wan [host])'},
    {'name': 'nmap', 'handler': _network_only(nmap_command), 'network': True, 'aliases': ['scan'],
     'phrases': [], 'description': 'nmap top-ports scan (usage: nmap <target>)'},
    {'name': 'date', 'handler': date_command, 'aliases': ['sysdate', 'now'],
     'phrases': ["what's the date", 'system time'],
     'description': 'System date/time (local + UTC)'},
    {'name': 'ntp', 'handler': ntp_command, 'aliases': ['nist'],
     'phrases': ['ntp time', 'atomic time'],
     'description': 'NTP time from NIST + host clock drift'},
    {'name': 'ask', 'handler': ask_command, 'aliases': ['ai', 'llm', 'q'],
     'phrases': [], 'description': 'Ask the AI (usage: ask <question>)'},
    {'name': 'new', 'handler': new_session_command,
     'aliases': ['session', 'reset'],
     'phrases': ['new session', 'start over', 'forget everything'],
     'description': 'Start a new AI conversation session'},
]
