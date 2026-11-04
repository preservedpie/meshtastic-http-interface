#!/usr/bin/env python3
"""Radio diagnostic — compare a healthy host (RAK-on-PC) against a flaky one
(Pi) to find why DMs at zero hops never get ACK'd and multi-chunk replies drop.

Run it on BOTH hosts and diff the output:

    python3 diag_radio.py                       # inspect local radio
    python3 diag_radio.py --ack !b505c07e       # also probe a live DM ACK
    python3 diag_radio.py --port /dev/ttyACM0   # override serial port

It reports, in order:
  1. host + python-meshtastic library version (prime suspect for the no-ACK
     difference between the two hosts)
  2. local node identity
  3. LoRa config: modem preset, region, hop limit, channel count — a mismatch
     here explains both no-ACK and chunk loss
  4. primary channel name/PSK fingerprint so you can confirm both radios share
     the same channel
  5. (optional) a real wantAck DM and whether the firmware returns ACK/NAK/timeout

It does NOT touch the running app or its DB; it opens its own short-lived
SerialInterface and closes it. Don't run it while the app holds the same port
on the same host — stop the app first, or point --port at a different radio.
"""

import argparse
import os
import sys
import threading
import time


def _hr(title):
    print(f"\n=== {title} ===")


def _lib_version():
    try:
        import meshtastic
        return getattr(meshtastic, '__version__', '(no __version__ attr)')
    except Exception as e:
        return f"<import failed: {e}>"


def _enum_name(val):
    """Protobuf enum fields come back as ints or names depending on version;
    show whatever we got without crashing."""
    return val if val is not None else '(unset)'


def main():
    ap = argparse.ArgumentParser(description="Meshtastic radio diagnostic")
    ap.add_argument('--port', default=os.environ.get('MESHTASTIC_SERIAL_PORT',
                                                      '/dev/ttyACM0'),
                    help="serial device (default: $MESHTASTIC_SERIAL_PORT or /dev/ttyACM0)")
    ap.add_argument('--ack', metavar='!HEX_OR_NUM', default=None,
                    help="send a wantAck DM to this node and report ACK/NAK/timeout")
    ap.add_argument('--ack-timeout', type=float, default=15.0,
                    help="seconds to wait for the ACK probe (default: 15)")
    args = ap.parse_args()

    _hr("Host / library")
    print(f"host         : {os.uname().nodename if hasattr(os, 'uname') else sys.platform}")
    print(f"python       : {sys.version.split()[0]}")
    print(f"meshtastic   : {_lib_version()}")
    print(f"serial port  : {args.port}")

    try:
        import meshtastic.serial_interface
    except Exception as e:
        print(f"\nCannot import meshtastic.serial_interface: {e}")
        sys.exit(1)

    _hr("Connecting")
    try:
        iface = meshtastic.serial_interface.SerialInterface(devPath=args.port)
    except Exception as e:
        print(f"FAILED to open {args.port}: {e}")
        print("Is the app still running and holding this port? Stop it first.")
        sys.exit(1)
    time.sleep(2)  # let myInfo / node list populate

    try:
        _hr("Local node")
        my = getattr(iface, 'myInfo', None)
        my_num = getattr(my, 'my_node_num', None) if my else None
        if my_num:
            print(f"node number  : {my_num} (!{my_num:08x})")
        else:
            print("node number  : <unresolved> (myInfo not populated)")

        # localNode holds the live config we care about.
        node = getattr(iface, 'localNode', None)
        lc = getattr(node, 'localConfig', None) if node else None

        _hr("LoRa config (compare these between the two hosts)")
        if lc and lc.HasField('lora'):
            lora = lc.lora
            print(f"modem_preset      : {_enum_name(lora.modem_preset)}")
            print(f"region            : {_enum_name(lora.region)}")
            print(f"use_preset        : {lora.use_preset}")
            print(f"hop_limit         : {lora.hop_limit}")
            print(f"tx_enabled        : {lora.tx_enabled}")
            print(f"tx_power          : {lora.tx_power}")
            # When use_preset is False these define a custom modem config that
            # MUST match on both radios or they can't hear each other's ACKs.
            print(f"bandwidth         : {lora.bandwidth}")
            print(f"spread_factor     : {lora.spread_factor}")
            print(f"coding_rate       : {lora.coding_rate}")
            print(f"frequency_offset  : {lora.frequency_offset}")
        else:
            print("lora config not available on this library version "
                  "(run `meshtastic --port %s --info` instead)" % args.port)

        _hr("Channels (primary must match on both radios)")
        try:
            ch_count = 0
            for i in range(8):
                ch = node.getChannelByChannelIndex(i) if node else None
                if ch is None:
                    continue
                # role 0 = DISABLED, 1 = PRIMARY, 2 = SECONDARY
                role = getattr(ch, 'role', None)
                if role in (0, None):
                    continue
                ch_count += 1
                name = getattr(ch.settings, 'name', '') or '(default)'
                psk = bytes(getattr(ch.settings, 'psk', b''))
                # fingerprint the PSK without printing the key itself
                fp = (psk[:1].hex() + '..' + psk[-1:].hex() + f"[{len(psk)}B]") \
                    if psk else '<none>'
                print(f"ch[{i}] role={role} name={name!r} psk={fp}")
            if ch_count == 0:
                print("no channels reported (library may not expose channels here; "
                      "use `meshtastic --info`)")
        except Exception as e:
            print(f"channel read failed: {e} — use `meshtastic --info`")

        _hr("Node table")
        nodes = getattr(iface, 'nodes', None) or {}
        print(f"heard nodes  : {len(nodes)}")

        if args.ack:
            _hr(f"ACK probe -> {args.ack}")
            dest = args.ack if str(args.ack).startswith('!') else \
                f"!{int(args.ack):08x}"
            done = threading.Event()
            outcome = {'ack': None, 'err': None}

            def _on_response(resp):
                try:
                    dec = resp.get('decoded', {}) if isinstance(resp, dict) else {}
                    routing = dec.get('routing', {}) or {}
                    err = routing.get('errorReason', 'NONE')
                    outcome['ack'] = (err in (None, 'NONE'))
                    outcome['err'] = err
                except Exception as e:
                    outcome['err'] = str(e)
                finally:
                    done.set()

            t0 = time.monotonic()
            try:
                iface.sendText(f"diag ack probe {int(t0)}",
                               destinationId=dest, wantAck=True,
                               onResponse=_on_response)
            except TypeError:
                # older lib without onResponse — best-effort, no confirmation
                iface.sendText(f"diag ack probe {int(t0)}", destinationId=dest)
                print("library has no onResponse support — sent, cannot confirm ACK")
                done.set()
                outcome['err'] = 'no_onResponse_support'

            got = done.wait(timeout=args.ack_timeout)
            dt = time.monotonic() - t0
            if not got:
                print(f"RESULT: NO RESPONSE after {args.ack_timeout:.0f}s "
                      f"-> firmware never reported routing for this DM")
            elif outcome['ack']:
                print(f"RESULT: ACK in {dt:.1f}s -> delivery confirmed")
            else:
                print(f"RESULT: NAK/err={outcome['err']!r} after {dt:.1f}s")
            print("\nInterpretation:")
            print("  ACK on PC but NO RESPONSE on Pi at 0 hops, same room")
            print("  => Pi radio isn't receiving the return ACK frame.")
            print("     Suspect: different modem_preset/channel, USB power (RX")
            print("     brownout), or older meshtastic lib not surfacing routing.")
    finally:
        try:
            iface.close()
        except Exception:
            pass


if __name__ == '__main__':
    main()
