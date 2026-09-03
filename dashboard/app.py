#!/usr/bin/env python3
"""
Dashboard FastAPI — configuration à chaud, gestion des backups, logs,
galerie avant/après. Sert aussi la page statique index.html.

Empreinte volontairement légère : pas de base de données, tout repose sur
les fichiers JSON déjà utilisés par les 3 scripts (config.json, state/*.json).
"""
import json
import shutil
import subprocess
import sys
import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

sys.path.insert(0, "/app")
from common_config import load_config, save_config, LOG_DIR, BACKUP_DIR, DATA_DIR, STATE_DIR

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


RUNNABLE_SCRIPTS = {
    "orientation_fix": ["python3", "/app/scripts/orientation_fix.py"],
    "person_to_album": ["python3", "/app/scripts/person_to_album.py"],
    "bloomin8_optimize": ["node", "/app/scripts/bloomin8_optimize.js"],
}


@app.post("/api/run/{script_name}")
def run_script(script_name: str):
    if script_name not in RUNNABLE_SCRIPTS:
        raise HTTPException(404, "Script inconnu")

    lock_path = DATA_DIR / "state" / f"{script_name}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    if lock_path.exists():
        raise HTTPException(409, "Ce script est déjà en cours d'exécution")

    lock_path.touch()

    def _run():
        try:
            subprocess.run(RUNNABLE_SCRIPTS[script_name], check=False)
        finally:
            lock_path.unlink(missing_ok=True)

    threading.Thread(target=_run, daemon=True).start()
    return {"status": "started"}


@app.get("/api/run/{script_name}/status")
def run_script_status(script_name: str):
    if script_name not in RUNNABLE_SCRIPTS:
        raise HTTPException(404, "Script inconnu")
    lock_path = DATA_DIR / "state" / f"{script_name}.lock"
    return {"running": lock_path.exists()}


@app.get("/api/backups")
def list_backups():
    if not BACKUP_DIR.exists():
        return {"folder_count": 0, "file_count": 0, "size_bytes": 0}
    folders = [e for e in BACKUP_DIR.iterdir() if e.is_dir()]
    all_files = [f for e in folders for f in e.rglob("*") if f.is_file()]
    return {
        "folder_count": len(folders),
        "file_count": len(all_files),
        "size_bytes": sum(f.stat().st_size for f in all_files),
    }


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


@app.get("/api/gallery/before/{backup_id}/{relative_path:path}")
def gallery_before_image(backup_id: str, relative_path: str):
    """Sert l'image AVANT correction (version sauvegardée dans le backup)."""
    if ".." in backup_id or ".." in relative_path:
        raise HTTPException(400, "Chemin invalide")
    path = BACKUP_DIR / backup_id / relative_path
    if not path.exists() or not path.is_file():
        raise HTTPException(404, "Image introuvable")
    return FileResponse(path)


@app.get("/api/gallery/after/{relative_path:path}")
def gallery_after_image(relative_path: str):
    """Sert l'image APRÈS correction (fichier actuel dans la bibliothèque Immich)."""
    if ".." in relative_path:
        raise HTTPException(400, "Chemin invalide")
    cfg = load_config()
    library_root = Path(cfg["orientation"]["library_path"])
    path = library_root / relative_path
    if not path.exists() or not path.is_file():
        raise HTTPException(404, "Image introuvable (peut-être déplacée/supprimée depuis)")
    return FileResponse(path)


class GalleryResolveItem(BaseModel):
    backup_id: str
    relative_path: str
    keep: str  # "before" (restaure l'original) ou "after" (garde la correction)


class GalleryResolveBatch(BaseModel):
    items: list[GalleryResolveItem]


@app.post("/api/gallery/resolve")
def resolve_gallery_batch(payload: GalleryResolveBatch):
    """Applique en masse les choix garder/supprimer de la galerie avant/après.

    - keep="after"  : la version corrigée reste en place, le backup est supprimé.
    - keep="before" : l'original est restauré dans la bibliothèque, le backup est supprimé.

    Dans les deux cas, le fichier est marqué en "manual_review" dans l'état
    d'orientation_fix pour qu'il ne soit plus jamais retraité automatiquement —
    la décision de l'utilisateur est définitive."""
    cfg = load_config()
    library_root = Path(cfg["orientation"]["library_path"])

    state_path = STATE_DIR / "orientation_fix.json"
    state = {}
    if state_path.exists():
        with open(state_path, "r", encoding="utf-8") as f:
            state = json.load(f)
    manual_review = set(state.get("manual_review", []))

    done, errors = 0, []

    for item in payload.items:
        if ".." in item.backup_id or ".." in item.relative_path:
            errors.append(item.relative_path)
            continue

        backup_path = BACKUP_DIR / item.backup_id / item.relative_path
        library_path = library_root / item.relative_path

        try:
            if item.keep == "before":
                if not backup_path.exists():
                    errors.append(item.relative_path)
                    continue
                library_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(backup_path, library_path)
            elif item.keep != "after":
                errors.append(item.relative_path)
                continue

            if backup_path.exists():
                backup_path.unlink()
            manual_review.add(item.relative_path)
            done += 1
        except Exception:
            errors.append(item.relative_path)

    state["manual_review"] = sorted(manual_review)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(state_path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)

    return {"resolved": done, "errors": errors}


STATE_FILES = {
    "orientation_fix": ("processed_files", "orientation_fix.json"),
    "bloomin8_optimize": ("processed_asset_ids", "bloomin8_optimize.json"),
}


@app.get("/api/state/{script_name}")
def get_state(script_name: str, limit: int = 200):
    if script_name not in STATE_FILES:
        raise HTTPException(404, "Pas d'état suivi pour ce script")
    key, filename = STATE_FILES[script_name]
    path = STATE_DIR / filename
    if not path.exists():
        return {"count": 0, "sample": []}

    with open(path, "r", encoding="utf-8") as f:
        state = json.load(f)

    items = state.get(key, {})
    if isinstance(items, dict):
        all_keys = sorted(items.keys())
    else:  # liste (ex: bloomin8_optimize)
        all_keys = sorted(items)

    return {"count": len(all_keys), "sample": all_keys[:limit], "truncated": len(all_keys) > limit}


app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
