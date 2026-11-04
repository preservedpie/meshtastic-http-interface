FROM python:3.13-slim

WORKDIR /app

# iputils-ping + nmap back the bot's network diagnostic commands (pingip, gw,
# wan, nmap). The healthcheck uses python/urllib, not curl, so curl is not
# installed.
RUN apt-get update \
    && apt-get install -y --no-install-recommends iputils-ping nmap \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p logs

# On flash-storage hosts (e.g. Raspberry Pi), run with LOW_DISK_WRITES=1 to cut
# SD-card wear: logs go to stdout only (no logs/app.log) and SQLite uses
# WAL + synchronous=NORMAL (no fsync per commit). Default off elsewhere.
ENV LOW_DISK_WRITES=0

EXPOSE 5001

CMD ["sh", "-c", "gunicorn --bind 0.0.0.0:${FLASK_PORT:-5001} --workers 1 --threads 4 --timeout 120 app:app"]
