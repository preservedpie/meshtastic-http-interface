"""Custom-module engine (http/shell/network/text/python). Ported verbatim."""

import json
import re
import shlex
import socket
import subprocess
import logging
import platform
from typing import Optional

import requests

from . import db
from . import settings

logger = logging.getLogger('mesh-bot.modules')

VALID_KINDS = ('http', 'shell', 'network', 'text', 'python')

HTTP_TIMEOUT = 10
SHELL_TIMEOUT = 15
MAX_OUTPUT = 1000


def run_module(module: dict, args: str, context: dict) -> str:
    kind = module.get('kind')
    config = module.get('config', {})
    try:
        if kind == 'http':
            out = _run_http(config, args, context)
        elif kind == 'shell':
            out = _run_shell(config, args, context)
        elif kind == 'network':
            out = _run_network(config, args, context)
        elif kind == 'text':
            out = _run_text(config, args, context)
        elif kind == 'python':
            out = _run_python(module, config, args, context)
        else:
            return f"Module '{module.get('name')}' has unknown kind {kind!r}"
    except Exception as e:
        logger.error(f"Module '{module.get('name')}' failed: {e}")
        return f"Module error: {e}"
    out = (out or '').strip()
    return out[:MAX_OUTPUT] if out else "(no output)"


def validate_config(kind: str, config: dict) -> Optional[str]:
    if kind not in VALID_KINDS:
        return f"Unknown kind: {kind}"
    if kind == 'http' and not config.get('url'):
        return "HTTP module needs a 'url'"
    if kind == 'shell' and not config.get('command'):
        return "Shell module needs a 'command'"
    if kind == 'network':
        op = config.get('op')
        if op not in ('ping', 'port'):
            return "Network module 'op' must be 'ping' or 'port'"
        if not config.get('host'):
            return "Network module needs a 'host'"
        if op == 'port' and not config.get('port'):
            return "Port check needs a 'port'"
    if kind == 'text' and 'template' not in config:
        return "Text module needs a 'template'"
    if kind == 'python' and not config.get('code'):
        return "Python module needs 'code'"
    return None


def _run_http(config: dict, args: str, context: dict) -> str:
    url = _template(config['url'], args, context)
    method = (config.get('method') or 'GET').upper()
    headers = config.get('headers') or {'User-Agent': 'MeshBot/1.0'}
    body = config.get('body')
    if method == 'POST':
        resp = requests.post(url, headers=headers, data=body, timeout=HTTP_TIMEOUT)
    else:
        resp = requests.get(url, headers=headers, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    text = resp.text

    json_path = config.get('json_path')
    regex = config.get('regex')
    value = text

    if json_path:
        try:
            data = resp.json()
        except ValueError:
            return "HTTP module: response was not JSON"
        value = _json_path(data, json_path)
        if value is None:
            return f"HTTP module: no value at '{json_path}'"
        value = str(value)
    elif regex:
        m = re.search(regex, text, re.DOTALL)
        if not m:
            return "HTTP module: regex matched nothing"
        value = m.group(1) if m.groups() else m.group(0)

    template = config.get('output_template')
    if template:
        return _template(template, args, context, result=value)
    return value


def _json_path(data, path: str):
    cur = data
    for part in re.split(r'\.', path):
        if not part:
            continue
        m = re.match(r'^([^\[\]]*)((?:\[\d+\])*)$', part)
        if not m:
            return None
        key, idxs = m.group(1), m.group(2)
        if key:
            if not isinstance(cur, dict) or key not in cur:
                return None
            cur = cur[key]
        for idx in re.findall(r'\[(\d+)\]', idxs):
            i = int(idx)
            if not isinstance(cur, list) or i >= len(cur):
                return None
            cur = cur[i]
    return cur


def _run_shell(config: dict, args: str, context: dict) -> str:
    base = config['command']
    base_argv = shlex.split(base)
    if not base_argv:
        return "Shell module: empty command"
    allowlist = _shell_allowlist()
    if base_argv[0] not in allowlist:
        return (f"Shell module: '{base_argv[0]}' not in allowlist "
                f"(add it in Settings)")
    argv = list(base_argv)
    if config.get('pass_args') and args.strip():
        argv.extend(shlex.split(args))
    proc = subprocess.run(argv, capture_output=True, text=True,
                          timeout=SHELL_TIMEOUT, shell=False)
    out = (proc.stdout or '').strip()
    err = (proc.stderr or '').strip()
    if proc.returncode != 0:
        return f"exit {proc.returncode}: {err or out or 'no output'}"
    return out or err or "(no output)"


def _shell_allowlist():
    raw = settings.get('SHELL_ALLOWLIST', '') or ''
    return {c.strip() for c in raw.split(',') if c.strip()}


def _run_network(config: dict, args: str, context: dict) -> str:
    op = config['op']
    host = _template(config['host'], args, context)
    if op == 'ping':
        return _ping(host)
    if op == 'port':
        return _port_check(host, int(config['port']))
    return "Network module: unknown op"


def _ping(host: str) -> str:
    count_flag = '-n' if platform.system().lower() == 'windows' else '-c'
    try:
        proc = subprocess.run(['ping', count_flag, '3', host],
                              capture_output=True, text=True,
                              timeout=SHELL_TIMEOUT, shell=False)
    except subprocess.TimeoutExpired:
        return f"{host}: ping timed out"
    if proc.returncode == 0:
        m = re.search(r'(?:time[=<]|Average\s*=\s*)([\d.]+)\s*ms', proc.stdout)
        rtt = f" ~{m.group(1)}ms" if m else ""
        return f"{host}: up{rtt}"
    return f"{host}: unreachable"


def _port_check(host: str, port: int) -> str:
    try:
        with socket.create_connection((host, port), timeout=5):
            return f"{host}:{port} open"
    except (socket.timeout, ConnectionRefusedError, OSError):
        return f"{host}:{port} closed/filtered"


def _run_text(config: dict, args: str, context: dict) -> str:
    return _template(config['template'], args, context)


def _run_python(module: dict, config: dict, args: str, context: dict) -> str:
    if not settings.get_bool('ALLOW_PYTHON_MODULES', False):
        return ("Python modules are disabled. Enable ALLOW_PYTHON_MODULES "
                "in Settings to use this module.")
    code = config['code']
    sandbox_globals = {
        '__builtins__': __builtins__,
        'requests': requests, 're': re, 'json': json, 'socket': socket,
    }
    sandbox_locals = {
        'args': args,
        'node': context.get('from'),
        'context': context,
        'result': '',
    }
    exec(compile(code, f"<module:{module.get('name')}>", 'exec'),
         sandbox_globals, sandbox_locals)
    return str(sandbox_locals.get('result', ''))


def _template(text: str, args: str, context: dict, result: str = '') -> str:
    if not text:
        return ''
    return (text
            .replace('{args}', args or '')
            .replace('{node}', str(context.get('from', '')))
            .replace('{result}', result or ''))
