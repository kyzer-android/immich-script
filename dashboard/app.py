#!/usr/bin/env python3
"""
Dashboard FastAPI — configuration à chaud, gestion des backups, logs,
galerie avant/après. Sert aussi la page statique index.html.

Empreinte volontairement légère : pas de base de données, tout repose sur
les fichiers JSON déjà utilisés par les 3 scripts (config.json, state/*.json).
"""
import shutil
import subprocess
import sys
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

sys.path.insert(0, "/app")
from common_config import load_config, save_config, LOG_DIR, BACKUP_DIR, DATA_DIR

app = FastAPI(title="Immich Scripts Dashboard")

STATIC_DIR = Path(__file__).parent / "static"


class ConfigUpdate(BaseModel):
    config: dict


class Bloomin8Decision(BaseModel):
    action: str  # "clear" ou "keep"


@app.get("/api/config")
def get_config():
    return load_config()


@app.post("/api/config")
def update_config(payload: ConfigUpdate):
    """Sauvegarde la config. Si l'album BLOOMIN8 change alors que l'orientation
    reste identique, on NE sauvegarde PAS tout de suite le nouvel albumId :
    on renvoie un flag au frontend pour lui demander de confirmer garder/vider
    (cf. /api/bloomin8/confirm-album-change)."""
    current = load_config()
    new_cfg = payload.config

    current_album = current["bloomin8"]["_last_run"].get("album_id") or current["bloomin8"]["album_id"]
    current_orientation = current["bloomin8"]["orientation"]
    new_album = new_cfg["bloomin8"]["album_id"]
    new_orientation = new_cfg["bloomin8"]["orientation"]

    album_changed = new_album != current_album
    orientation_changed = new_orientation != current_orientation

    if album_changed and not orientation_changed:
        # On sauvegarde tout SAUF le nouvel albumId, en attendant la décision.
        # Le frontend doit rappeler /api/bloomin8/confirm-album-change.
        pending_cfg = dict(new_cfg)
        pending_cfg["bloomin8"] = dict(new_cfg["bloomin8"])
        pending_cfg["bloomin8"]["_pending_album_id"] = new_album
        pending_cfg["bloomin8"]["album_id"] = current_album  # inchangé pour l'instant
        save_config(pending_cfg)
        regen_crontab()
        return JSONResponse({
            "status": "confirmation_required",
            "message": "L'album BLOOMIN8 a changé. Garder ou vider le contenu actuel du dossier ?",
        })

    save_config(new_cfg)
    regen_crontab()
    return {"status": "ok"}


@app.post("/api/bloomin8/confirm-album-change")
def confirm_album_change(decision: Bloomin8Decision):
    if decision.action not in ("clear", "keep"):
        raise HTTPException(400, "action doit être 'clear' ou 'keep'")

    cfg = load_config()
    pending_album_id = cfg["bloomin8"].pop("_pending_album_id", None)
    if not pending_album_id:
        raise HTTPException(400, "Aucun changement d'album en attente de confirmation")

    cfg["bloomin8"]["album_id"] = pending_album_id
    cfg["bloomin8"]["pending_action"] = decision.action
    save_config(cfg)
    return {"status": "ok", "album_id": pending_album_id, "action": decision.action}


def regen_crontab() -> None:
    """Régénère /etc/cron.d/immich-scripts à partir des schedules en config."""
    try:
        subprocess.run(["python3", "/app/regen_crontab.py"], check=True)
    except subprocess.CalledProcessError as e:
        print(f"Erreur régénération crontab : {e}", file=sys.stderr)


@app.get("/api/backups")
def list_backups():
    if not BACKUP_DIR.exists():
        return []
    result = []
    for entry in sorted(BACKUP_DIR.iterdir(), reverse=True):
        if not entry.is_dir():
            continue
        size = sum(f.stat().st_size for f in entry.rglob("*") if f.is_file())
        file_count = sum(1 for f in entry.rglob("*") if f.is_file())
        result.append({
            "id": entry.name,
            "size_bytes": size,
            "file_count": file_count,
        })
    return result


@app.delete("/api/backups/{backup_id}")
def delete_backup(backup_id: str):
    target = BACKUP_DIR / backup_id
    if not target.exists() or not target.is_dir() or ".." in backup_id:
        raise HTTPException(404, "Backup introuvable")
    shutil.rmtree(target)
    return {"status": "deleted"}


@app.delete("/api/backups")
def purge_all_backups():
    if BACKUP_DIR.exists():
        for entry in BACKUP_DIR.iterdir():
            if entry.is_dir():
                shutil.rmtree(entry, ignore_errors=True)
    return {"status": "purged"}


@app.get("/api/logs/{script_name}")
def get_logs(script_name: str, lines: int = 200):
    safe_names = {"orientation_fix", "person_to_album", "bloomin8_optimize"}
    if script_name not in safe_names:
        raise HTTPException(404, "Script inconnu")
    log_path = LOG_DIR / f"{script_name}.log"
    if not log_path.exists():
        return {"lines": []}
    with open(log_path, "r", encoding="utf-8") as f:
        all_lines = f.readlines()
    return {"lines": [l.rstrip("\n") for l in all_lines[-lines:]]}


@app.get("/api/gallery")
def gallery():
    """Liste les backups disponibles comme paires avant/après potentielles,
    en s'appuyant sur les chemins relatifs conservés dans chaque snapshot."""
    if not BACKUP_DIR.exists():
        return []
    result = []
    for entry in sorted(BACKUP_DIR.iterdir(), reverse=True):
        if not entry.is_dir():
            continue
        for f in entry.rglob("*"):
            if f.is_file():
                result.append({
                    "backup_id": entry.name,
                    "relative_path": str(f.relative_to(entry)),
                })
    return result[:200]  # borne raisonnable pour l'UI


app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
