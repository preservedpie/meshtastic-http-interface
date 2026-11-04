# Meshtastic HTTP Interface

A self-hosted web dashboard and mesh bot for a [Meshtastic](https://meshtastic.org)
node connected over USB serial. It records every packet the radio hears to
SQLite and gives you a browser UI for watching and managing the mesh. A
built-in bot can answer commands sent over the mesh, with optional AI replies
from a local [Ollama](https://ollama.com) model.

It's built for function over looks, and it runs well on a Raspberry Pi 4
in Docker.

## Features

**Web UI tabs**

| Tab | What it does |
|---|---|
| Live Feed | Every packet as it arrives (text, position, telemetry, nodeinfo, traceroute, detection sensor, …) |
| Nodes | Every node heard: last seen, battery, SNR/RSSI, hops. Lets you set aliases and request telemetry, position or a traceroute |
| Local Node | The connected radio's info and channels (add, edit or delete channels and set PSKs) |
| Messages | Text message history. Send to a channel or DM a node |
| Map | Node positions (Leaflet), with movement trails |
| Graphs | Telemetry charts per node: temp, humidity, pressure, IAQ, battery, voltage, channel utilization |
| Report | Summary report for the mesh |
| Weather | Offline forecast from your nodes' own sensors. Pressure-trend forecast for 24–48 h plus a rain outlook based on past patterns, with no internet needed |
| Forwarding Rules | Match packets (type / channel / node / text) and send [Pushover](https://pushover.net) alerts |
| Bot | Bot settings, which channels it listens on, ignored nodes, custom modules, an LLM test, and manual sending |
| Stats / Maintenance | DB size, row counts, and scheduled data-retention cleanup |
| Diagnostics | Serial link health and recent serial log |
| API | Documents the JSON endpoints |

**Mesh bot commands** (prefix optional, default `/`): `status`, `ping`,
`time`, `uptime`, `weather [tomorrow|3d|7d] [city]`, `sun`, `nodes`, `rf`,
`mesh`, `telem`, `news`, `date`, `ntp`, `dice`, `echo`, `pingip`, `gw`,
`wan`, `nmap` (network commands are opt-in, see Security), `ask <question>` (LLM), and `help`. If a message doesn't
match a command, it can go to the LLM instead. You can also add custom
modules from the UI that call an HTTP endpoint, run an allowlisted shell
command, ping a host, return fixed text, or (if explicitly enabled) run
Python.

**Monitoring integrations**
- `/api/health/serial` returns an error status when the serial link stops
  receiving, which happens even when the port still looks "connected". The
  Docker healthcheck uses this endpoint.
- `/api/health/nodes` and `/api/health/nodes/<id>` report per-node
  online/offline status for an uptime monitor such as Uptime Kuma.
- `/api/latest-weather/<node_id>` returns the latest sensor reading for one
  node. [`examples/reterminal-dashboard.yaml`](examples/reterminal-dashboard.yaml)
  is an ESPHome config for a Seeed reTerminal E1002 e-paper display that
  shows readings from up to six nodes.

## ⚠️ Security: read this before deploying

- **The web UI has no authentication.** Anyone who can reach port 5001 can
  send messages as your node, **change or delete channels and set PSKs**
  on your radio, and edit bot modules. Keep it on a trusted LAN, or put it
  behind a reverse proxy with authentication. Don't expose it directly to
  the internet.
- **Bot commands are available to anyone on the channels the bot listens
  to.** The network commands (`pingip`, `gw`, `wan`, `nmap`) run from the
  host's network, so they're **off by default**. Turn them on with
  `ALLOW_NETWORK_COMMANDS=true` (or in the Bot tab) only if the bot
  listens solely on private channels. Otherwise strangers could scan
  your LAN.
- `ALLOW_PYTHON_MODULES=true` lets anyone with access to the UI run
  arbitrary Python inside the container. Leave it off unless you need it.
  Shell modules are limited to `SHELL_ALLOWLIST`.

## Quick start (Docker)

You need a Meshtastic node plugged in over USB and Docker with Compose.

```bash
git clone https://github.com/preservedpie/meshtastic-http-interface.git
cd meshtastic-http-interface
cp .env.example .env               # set MESHTASTIC_SERIAL_PORT at minimum
docker network create mesh-net     # shared network for an optional Ollama container
docker compose up -d --build
```

Open `http://<host>:5001/`.

- Prefer a stable device path such as `/dev/serial/by-id/usb-…` over
  `/dev/ttyACM0`, because numbering can change after a reboot or replug.
- Once the first packets arrive, find your radio's decimal node number
  under **Nodes** and set `BOT_NODE_NUMBER` in `.env`. This stops the bot
  from replying to itself.
- On a Pi or other SD-card host, set `LOW_DISK_WRITES=1`. Logs then go to
  stdout only, and SQLite uses WAL, which cuts card wear.

### Without Docker

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
set -a; . ./.env; set +a
gunicorn --bind 0.0.0.0:5001 --workers 1 --threads 4 --timeout 120 app:app
```

Keep it to **one worker**, because the serial connection lives inside the
process. The user running it needs to be in the `dialout` group.

### AI replies (optional)

Run Ollama as a separate container on the `mesh-net` network and set
`LLM_HOST=ollama`. See [docs/OLLAMA_SETUP.md](docs/OLLAMA_SETUP.md) for a
Raspberry Pi 4 walkthrough. On a 2 GB Pi 4, `llama3.2:1b` works, slowly.
Set `LLM_FALLBACK_ENABLED=false` to turn AI replies off.

## Configuration

All settings live in `.env`. See [`.env.example`](.env.example), which
documents each one. Most bot settings are only a starting point and can be
changed live in the **Bot** tab; values saved there override `.env`.

## Troubleshooting

- **Radio won't connect / healthcheck unhealthy:** check that the device
  path exists on the host, is passed through in `docker-compose.yml`, and
  isn't held open by another program (e.g. the `meshtastic` CLI).
- **Two radios disagree / DMs never get ACKed:** run
  `python3 diag_radio.py` on both hosts and compare the output. It prints
  the library version, LoRa settings, and a fingerprint of the primary
  channel, and with `--ack <node>` it can test a real DM ACK.
- **Bot "sometimes doesn't reply":** `RATE_LIMIT_SECONDS` silently drops
  replies to the same node that arrive inside the window.

## License

[MIT](LICENSE). Bundled third-party files in `static/vendor/` (React,
ReactDOM, Babel standalone, Leaflet) keep their own licenses (MIT /
BSD-2-Clause).
