#!/bin/bash
set -e

mkdir -p /data/config /data/state /data/logs /data/backups "${BLOOMIN8_DEST_PATH:-/data/bloomin8-share}"

echo "[entrypoint] Génération du crontab initial..."
python3 /app/regen_crontab.py

echo "[entrypoint] Démarrage de cron..."
cron

echo "[entrypoint] Démarrage du dashboard sur le port 8080..."
cd /app/dashboard
exec uvicorn app:app --host 0.0.0.0 --port 8080
