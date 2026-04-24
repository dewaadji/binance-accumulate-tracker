#!/bin/sh
set -eu

mkdir -p /data/logs

echo "[startup] running one-time pool scan..."
if python3 /app/accumulation_radar.py pool >> /data/logs/accumulation.log 2>&1; then
  echo "[startup] pool scan completed"
else
  echo "[startup] pool scan failed, continuing to scheduler"
fi

exec /usr/local/bin/supercronic /etc/supercronic/crontab
