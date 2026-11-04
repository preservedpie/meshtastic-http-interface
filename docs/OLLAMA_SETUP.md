# Ollama Setup (Raspberry Pi 4, 64-bit)

This guide stands up **Ollama in its own Docker container**, completely separate
from the `meshtastic-http-interface` stack. The bot in this project reaches it
over HTTP (`LLM_HOST` / `LLM_PORT` in `.env`); the two are otherwise unrelated
and can be started, stopped, and upgraded independently.

> **Prerequisite: 64-bit OS.** Ollama publishes only `amd64` and `arm64` images
> — there is **no 32-bit (arm/v7) build**. On 32-bit Raspberry Pi OS you'll get
> `no matching manifest for linux/arm/v7`. The Pi must be running **64-bit**
> Raspberry Pi OS. Verify with:
> ```bash
> uname -m      # must print: aarch64
> ```
> If it prints `armv7l`, reflash the SD card with the 64-bit image first.

---

## 1. Install Docker

If Docker isn't already on the Pi, use the official convenience script:

```bash
curl -fsSL https://get.docker.com -o get-docker.sh
sudo sh get-docker.sh
```

Let your user run docker without `sudo` (log out/in afterwards for it to apply):

```bash
sudo usermod -aG docker "$USER"
```

Confirm it works:

```bash
docker version
docker run --rm hello-world
```

---

## 2. Run the Ollama container

The Pi has **no CUDA/NVIDIA GPU**, so do **not** pass `--gpus all` — Ollama runs
CPU-only here. (`--gpus all` on a Pi fails or is silently ignored.)

```bash
docker run -d \
  --name ollama \
  -v ollama:/root/.ollama \
  -p 11434:11434 \
  --restart unless-stopped \
  ollama/ollama
```

What each flag does:

| Flag | Why |
|---|---|
| `-d` | Run detached (in the background). |
| `--name ollama` | Stable name for `docker exec` / `docker logs`. |
| `-v ollama:/root/.ollama` | **Persist pulled models** in a named volume. Without this, every recreate re-downloads gigabytes. |
| `-p 11434:11434` | Publish the API on the host so the meshtastic container (and other LAN hosts) can reach it. |
| `--restart unless-stopped` | Comes back automatically after a reboot. |

Check it started:

```bash
docker ps
docker logs ollama
curl http://localhost:11434/api/tags     # should return JSON (empty list at first)
```

---

## 3. Pull a small model

On a **2GB Pi 4 (CPU-only)** the model must stay tiny — it shares RAM with the
OS, Docker, and the meshtastic app. Replies also go out over LoRa capped at ~200
characters, so a big model buys little.

**Recommended starter model — `llama3.2:1b`** (~1.3GB):

```bash
docker exec -it ollama ollama pull llama3.2:1b
```

This matches `LLM_MODEL=llama3.2:1b` in the project's `.env`.

### Alternatives by RAM

| Model | Size | Notes |
|---|---|---|
| `llama3.2:1b` | ~1.3GB | **Default.** Good instruction-following for its size. |
| `qwen2.5:1.5b` | ~1.0GB | Strong terse factual answers; good alternative. |
| `qwen2.5:0.5b` | ~0.4GB | Tiniest; fastest; weaker reasoning. Use if 1B is too slow. |
| `gemma2:2b` / `llama3.2:3b` | ~1.6–2GB | **Too tight for 2GB** — will swap to SD card (unusably slow) or fail to load. Only on a 4GB+ Pi. |

Test the model directly before wiring up the bot:

```bash
docker exec -it ollama ollama run llama3.2:1b "Say hi in five words."
```

List what's installed at any time:

```bash
docker exec -it ollama ollama list
```

---

## 4. Connect the meshtastic bot to Ollama

The bot reads these from `.env` (see `.env.example`):

```env
LLM_FALLBACK_ENABLED=true
LLM_HOST=host.docker.internal
LLM_PORT=11434
LLM_MODEL=llama3.2:1b
```

**Networking note — this is the common gotcha.** The meshtastic app runs in its
*own* container. Inside it, `127.0.0.1` means *that* container, not the Pi host,
so it can't reach Ollama that way. Two clean options:

- **Use `host.docker.internal`** (what `.env` is set to). For this name to
  resolve, add an `extra_hosts` entry to the meshtastic service in its compose
  file:

  ```yaml
  services:
    flask-app:
      extra_hosts:
        - "host.docker.internal:host-gateway"
  ```

- **Or use the Pi's LAN IP** (e.g. `LLM_HOST=192.168.1.50`). Simpler, but breaks
  if the Pi's IP changes — give it a DHCP reservation if you go this route.

Verify reachability from inside the meshtastic container:

```bash
docker exec -it meshtastic-http-interface \
  curl -s http://host.docker.internal:11434/api/tags
```

A JSON response listing your model means the bot can talk to Ollama.

---

## Expectations & tuning (2GB Pi 4, CPU-only)

- **It will be slow.** Even a 1B model is several seconds per reply on a Pi CPU,
  and the bot can make up to 3 calls per answer (generate → shorten → summarize).
- If you see frequent `LLM timeout` replies, raise `LLM_TIMEOUT` in `.env`
  (default 60s) or drop to a smaller model (`qwen2.5:0.5b`).
- Keep only **one** model pulled to save RAM/disk; remove others with
  `docker exec -it ollama ollama rm <model>`.
- Watch memory: `docker stats ollama`. If it's swapping hard, the model is too
  big for 2GB — go smaller.

---

## Common operations

```bash
docker logs -f ollama                      # follow logs
docker restart ollama                      # restart
docker stop ollama && docker rm ollama     # remove container (models persist in the volume)
docker exec -it ollama ollama list         # list installed models
docker exec -it ollama ollama pull <model> # add a model
docker exec -it ollama ollama rm <model>   # remove a model
```

Upgrading Ollama itself:

```bash
docker pull ollama/ollama
docker stop ollama && docker rm ollama
# re-run the `docker run` from step 2 — the `ollama` volume keeps your models
```
