"""Time commands — system clock and NTP time from NIST.

`date` reports the host's own clock (local + UTC). `ntp` queries an external
NTP server (NIST by default) and reports the authoritative time plus how far
the host clock has drifted from it."""

import socket
import struct
import time
from datetime import datetime, timezone

# NIST first, then the NTP pool as a fallback if NIST is unreachable.
NTP_SERVERS = ('time.nist.gov', 'pool.ntp.org')
_NTP_DELTA = 2208988800   # seconds between 1900 (NTP epoch) and 1970 (Unix epoch)


def date_command(args: str, context: dict) -> str:
    local = datetime.now().astimezone()
    utc   = datetime.now(timezone.utc)
    tzname = local.tzname() or 'local'
    return "\n".join([
        "System time:",
        f"Local: {local.strftime('%Y-%m-%d %H:%M:%S')} {tzname}",
        f"UTC:   {utc.strftime('%Y-%m-%d %H:%M:%S')}",
    ])


def _ntp_query(host, timeout=5):
    """Return the server's transmit time as a Unix timestamp (float)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.settimeout(timeout)
        # LI=0, VN=3, Mode=3 (client) → first byte 0x1b, rest zero.
        sock.sendto(b'\x1b' + 47 * b'\0', (host, 123))
        data, _ = sock.recvfrom(48)
    finally:
        sock.close()
    if len(data) < 48:
        raise ValueError("short NTP response")
    # Transmit timestamp is the 11th 32-bit word (seconds), big-endian.
    secs = struct.unpack('!12I', data[:48])[10]
    return secs - _NTP_DELTA


def ntp_command(args: str, context: dict) -> str:
    servers = [args.strip()] if args.strip() else list(NTP_SERVERS)
    last_err = None
    for host in servers:
        try:
            t0 = time.time()
            server_ts = _ntp_query(host)
            drift = server_ts - t0   # +ve means host clock is behind the server
            dt = datetime.fromtimestamp(server_ts, tz=timezone.utc)
            return "\n".join([
                f"NTP ({host}):",
                f"{dt.strftime('%Y-%m-%d %H:%M:%S')} UTC",
                f"Host drift: {drift:+.1f}s",
            ])
        except Exception as e:
            last_err = e
            continue
    return f"NTP unreachable ({last_err})"
