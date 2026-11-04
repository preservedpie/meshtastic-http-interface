"""Command router — dispatch text to commands, custom modules, or LLM."""

import logging
from typing import Dict, Callable, Optional

from .commands import COMMANDS
from . import settings
from . import db
from . import modules
from . import llm_worker
from .llm_worker import LLMJob

logger = logging.getLogger('mesh-bot.router')


class CommandRouter:
    def __init__(self):
        self.commands: Dict[str, Callable] = {}
        self.aliases: Dict[str, str] = {}
        self._register_commands()

    def _register_commands(self):
        for cmd_info in COMMANDS:
            name = cmd_info['name'].lower()
            self.commands[name] = cmd_info['handler']
            for alias in cmd_info.get('aliases', []):
                self.aliases[alias.lower()] = name
            logger.info(f"Registered command: {name} (aliases: {cmd_info.get('aliases', [])})")

    def _find_module(self, cmd: str):
        if cmd in self.commands or cmd in self.aliases:
            return None
        mod = db.get_module_by_name(cmd)
        if mod and mod['enabled']:
            return mod
        for m in db.list_modules(only_enabled=True):
            if cmd in m['aliases']:
                return m
        return None

    def get_command(self, text: str):
        parts = text.strip().split(maxsplit=1)
        if not parts:
            return None, ''
        cmd = parts[0].lower()
        args = parts[1] if len(parts) > 1 else ''
        if cmd in self.commands:
            return cmd, args
        if cmd in self.aliases:
            return self.aliases[cmd], args
        return None, text

    def handle(self, text: str, context: dict) -> Optional[str]:
        cmd_name, args = self.get_command(text)

        # Per-channel allowlist enforcement
        allowed = context.get('channel_allowed_cmds') or []
        if cmd_name and allowed and cmd_name not in allowed:
            logger.info(f"Command '{cmd_name}' blocked by channel allowlist")
            return None

        # context['handled'] tells the caller a reply was produced or queued
        # (async LLM jobs return None here), so rate limiting only charges a
        # node when the bot actually responded.
        if cmd_name:
            context['handled'] = True
            try:
                return self.commands[cmd_name](args, context)
            except Exception as e:
                logger.error(f"Error in command {cmd_name}: {e}")
                return f"Error: {e}"

        first = text.strip().split(maxsplit=1)
        if first:
            mod_args = first[1] if len(first) > 1 else ''
            mod = self._find_module(first[0].lower())
            if mod:
                if allowed and mod['name'] not in allowed:
                    return None
                logger.info(f"Routing to module '{mod['name']}'")
                context['handled'] = True
                return modules.run_module(mod, mod_args, context)

        text_lower = text.lower()
        for cmd_info in COMMANDS:
            for phrase in cmd_info.get('phrases', []):
                if phrase.lower() in text_lower:
                    name = cmd_info['name'].lower()
                    if allowed and name not in allowed:
                        continue
                    context['handled'] = True
                    try:
                        return cmd_info['handler'](text, context)
                    except Exception as e:
                        logger.error(f"Error in command {cmd_info['name']}: {e}")
                        return f"Error: {e}"

        if settings.get_bool('LLM_FALLBACK_ENABLED', True):
            if allowed and 'ask' not in allowed:
                return None
            send = context.get('send')
            if send is not None:
                logger.info(f"No command match, queuing LLM fallback: {text}")
                context['handled'] = True
                llm_worker.submit(LLMJob(
                    prompt=text,
                    node_id=context.get('from'),
                    channel=context.get('response_channel', 0),
                    reply=send,
                ))
                return None
            context['handled'] = True
            return llm_worker.query_sync(text, node_id=context.get('from'))
        return None

    def get_help(self) -> str:
        from .commands import network_commands_enabled
        show_network = network_commands_enabled()
        lines = ["Available commands:"]
        for c in COMMANDS:
            if c.get('network') and not show_network:
                continue
            aliases = c.get('aliases', [])
            alias_str = f" ({', '.join(aliases)})" if aliases else ''
            lines.append(f"  {c['name']}{alias_str}: {c.get('description','')}")
        return "\n".join(lines)
