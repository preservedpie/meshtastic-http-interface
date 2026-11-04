"""Network diagnostic commands — ping a host, the default gateway, the
internet, and run an nmap scan. These shell out to the system `ping`/`nmap`
binaries (installed in the container image) with no shell interpolation, so a
user argument can't inject extra commands."""

import re
import shutil
import socket
import struct
import subprocess

PING_COUNT   = 3
PING_TIMEOUT = 8      # whole-command wall-clock budget for ping
NMAP_TIMEOUT = 45     # nmap can be slow; cap it
MAX_OUTPUT   = 800    # mesh replies get chunked, but don't flood


def _default_gateway():
    """Default-route gateway IP, read from /proc/net/route (no `ip` binary
    needed). Returns None if there's no default route."""
    try:
        with open('/proc/net/route') as f:
            for line in f.readlines()[1:]:
                fields = line.strip().split()
                if len(fields) < 4:
                    continue
                dest, flags = fields[1], int(fields[3], 16)
                # Destination 0.0.0.0 with the RTF_GATEWAY flag (0x2) = default route.
                if dest == '00000000' and flags & 0x2:
                    return socket.inet_ntoa(struct.pack('<L', int(fields[2], 16)))
    except Exception:
        pass
    return None


def _run_ping(host):
    """Ping `host` and return a one-line summary string."""
    if not shutil.which('ping'):
        return "ping not available on host"
    try:
        proc = subprocess.run(
            ['ping', '-c', str(PING_COUNT), '-w', str(PING_TIMEOUT), host],
            capture_output=True, text=True, timeout=PING_TIMEOUT + 4, shell=False,
        )
    except subprocess.TimeoutExpired:
        return f"{host}: timed out"
    except Exception as e:
        return f"{host}: error ({e})"

    if proc.returncode == 0:
        # avg rtt and loss% from the summary lines
        rtt = re.search(r'=\s*[\d.]+/([\d.]+)/', proc.stdout)
        loss = re.search(r'(\d+)% packet loss', proc.stdout)
        bits = [f"{host}: up"]
        if rtt:  bits.append(f"~{float(rtt.group(1)):.0f}ms")
        if loss: bits.append(f"{loss.group(1)}% loss")
        return ' '.join(bits)
    loss = re.search(r'(\d+)% packet loss', proc.stdout)
    if loss and loss.group(1) != '100':
        return f"{host}: degraded ({loss.group(1)}% loss)"
    return f"{host}: unreachable"


def pingip_command(args: str, context: dict) -> str:
    host = args.strip().split()[0] if args.strip() else ''
    if not host:
        return "Usage: pingip <ip or hostname>"
    return _run_ping(host)


def gw_command(args: str, context: dict) -> str:
    gw = _default_gateway()
    if not gw:
        return "No default gateway found"
    return f"GW {_run_ping(gw)}"


def wan_command(args: str, context: dict) -> str:
    # A specific host may be given; otherwise check internet reachability via a
    # well-known anycast resolver.
    host = args.strip().split()[0] if args.strip() else '1.1.1.1'
    return f"WAN {_run_ping(host)}"


def nmap_command(args: str, context: dict) -> str:
    if not shutil.which('nmap'):
        return "nmap not installed on host"
    target = args.strip()
    if not target:
        gw = _default_gateway()
        if not gw:
            return "Usage: nmap <target> (e.g. 192.168.1.0/24)"
        # Default to scanning the local /24 around the gateway.
        target = re.sub(r'\.\d+$', '.0/24', gw)

    # Fast top-ports scan; split target on whitespace so each token is its own
    # argv entry (no shell, so no injection).
    argv = ['nmap', '-T4', '--top-ports', '20', '-Pn'] + target.split()
    try:
        proc = subprocess.run(argv, capture_output=True, text=True,
                              timeout=NMAP_TIMEOUT, shell=False)
    except subprocess.TimeoutExpired:
        return f"nmap {target}: timed out after {NMAP_TIMEOUT}s"
    except Exception as e:
        return f"nmap error: {e}"

    # Summarise: hosts up + their open ports, dropping nmap's verbose preamble.
    lines = []
    host = None
    for ln in proc.stdout.splitlines():
        ln = ln.strip()
        m = re.match(r'Nmap scan report for (.+)', ln)
        if m:
            host = m.group(1)
            lines.append(host)
        elif re.match(r'\d+/\w+\s+open', ln):
            lines.append(f"  {ln}")
    out = '\n'.join(lines) if lines else (proc.stdout.strip() or 'no results')
    return f"nmap {target}:\n{out}"[:MAX_OUTPUT]
