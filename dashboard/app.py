#!/usr/bin/env python3
"""
Dashboard FastAPI — configuration à chaud, gestion des backups, logs,
galerie avant/après. Sert aussi la page statique index.html.

Empreinte volontairement légère : pas de base de données, tout repose sur
les fichiers JSON déjà utilisés par les 3 scripts (config.json, state/*.json).
"""
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
from pathlib import Path

import piexif
from PIL import Image as PILImage

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, Response
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

    lock_path = STATE_DIR / f"{script_name}.lock"
    if lock_path.exists():
        raise HTTPException(409, "Ce script est déjà en cours d'exécution")

    def _run():
        subprocess.run(RUNNABLE_SCRIPTS[script_name])

    threading.Thread(target=_run, daemon=True).start()
    return {"status": "started"}


@app.post("/api/run/{script_name}/stop")
def stop_script(script_name: str):
    if script_name not in RUNNABLE_SCRIPTS:
        raise HTTPException(404, "Script inconnu")

    lock_path = STATE_DIR / f"{script_name}.lock"
    if not lock_path.exists():
        raise HTTPException(409, "Ce script n'est pas en cours d'exécution")

    try:
        pid = int(lock_path.read_text().strip())
        os.kill(pid, signal.SIGTERM)  # arrêt propre : le script sauvegarde son état avant de quitter
    except (ValueError, ProcessLookupError):
        lock_path.unlink(missing_ok=True)  # verrou périmé, on nettoie
        raise HTTPException(409, "Le process n'existait déjà plus, verrou nettoyé.")

    return {"status": "stopping"}


@app.get("/api/run/{script_name}/status")
def run_script_status(script_name: str):
    if script_name not in RUNNABLE_SCRIPTS:
        raise HTTPException(404, "Script inconnu")
    lock_path = STATE_DIR / f"{script_name}.lock"
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
def gallery(limit: int = 50, offset: int = 0):
    """Liste les backups disponibles comme paires avant/après potentielles,
    en s'appuyant sur les chemins relatifs conservés dans chaque snapshot."""
    if not BACKUP_DIR.exists():
        return {"total": 0, "items": []}
    all_items = []
    for entry in sorted(BACKUP_DIR.iterdir(), reverse=True):
        if not entry.is_dir():
            continue
        for f in entry.rglob("*"):
            if f.is_file():
                all_items.append({
                    "backup_id": entry.name,
                    "relative_path": str(f.relative_to(entry)),
                })
    return {"total": len(all_items), "items": all_items[offset:offset + limit]}


@app.get("/api/gallery/before-raw/{backup_id}/{relative_path:path}")
def gallery_before_raw(backup_id: str, relative_path: str):
    """Sert l'image AVANT correction en IGNORANT son tag EXIF Orientation,
    pour que le navigateur ne compense pas automatiquement l'affichage —
    montre les pixels tels que réellement stockés sur le disque."""
    if ".." in backup_id or ".." in relative_path:
        raise HTTPException(400, "Chemin invalide")
    path = BACKUP_DIR / backup_id / relative_path
    if not path.exists() or not path.is_file():
        raise HTTPException(404, "Image introuvable")

    import io
    from PIL import Image as PILImage

    img = PILImage.open(path)
    img.load()
    buf = io.BytesIO()
    # Pas de exif= passé ici : le tag Orientation d'origine n'est PAS
    # transmis, donc le navigateur affiche les pixels bruts sans rotation.
    img.save(buf, format="JPEG", quality=90)
    buf.seek(0)
    return Response(content=buf.read(), media_type="image/jpeg")


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
    user_id = cfg["orientation"].get("user_id", "").strip()
    if user_id:
        library_root = library_root / user_id
    path = library_root / relative_path
    if not path.exists() or not path.is_file():
        raise HTTPException(404, "Image introuvable (peut-être déplacée/supprimée depuis)")
    return FileResponse(path)


def _force_exif_orientation_normal(path: Path) -> None:
    """Force le tag EXIF Orientation à 1 (normal) SANS toucher aux pixels —
    utilisé quand l'utilisateur juge manuellement que les pixels bruts sont
    déjà dans le bon sens, pour empêcher les visionneuses de continuer à
    appliquer une ancienne instruction de rotation EXIF."""
    try:
        exif_dict = piexif.load(str(path))
    except Exception:
        exif_dict = {"0th": {}, "Exif": {}, "GPS": {}, "Interop": {}, "1st": {}, "thumbnail": None}
    try:
        exif_dict["0th"][piexif.ImageIFD.Orientation] = 1
        exif_bytes = piexif.dump(exif_dict)
        piexif.insert(exif_bytes, str(path))
    except Exception:
        pass  # formats sans support EXIF (ex: PNG) : rien à faire, pas bloquant


class GalleryResolveItem(BaseModel):
    backup_id: str
    relative_path: str
    keep: str  # "before" (restaure l'original, EXIF forcé à 1) | "after" (garde la correction) | "unresolved" (ni l'un ni l'autre, laissé pour plus tard, restaure l'original sans rien déclarer)


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
    user_id = cfg["orientation"].get("user_id", "").strip()
    if user_id:
        library_root = library_root / user_id

    state_path = STATE_DIR / "orientation_fix.json"
    state = {}
    if state_path.exists():
        with open(state_path, "r", encoding="utf-8") as f:
            state = json.load(f)
    manual_review = set(state.get("manual_review", []))
    processed_files = state.setdefault("processed_files", {})
    corrected_files = list(state.get("corrected_files", []))  # ordre chronologique préservé
    needs_manual_rotation = set(state.get("needs_manual_rotation", []))

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
                _force_exif_orientation_normal(library_path)
                if backup_path.exists():
                    backup_path.unlink()
                manual_review.add(item.relative_path)
                done += 1

            elif item.keep == "after":
                if backup_path.exists():
                    backup_path.unlink()
                manual_review.add(item.relative_path)
                done += 1

            elif item.keep == "unresolved":
                # Ni l'original ni la correction ne sont bons : on restaure
                # l'original SANS rien déclarer de correct, on retire l'entrée
                # de processed_files, et on l'ajoute à needs_manual_rotation
                # pour traitement dans l'onglet dédié.
                if not backup_path.exists():
                    errors.append(item.relative_path)
                    continue
                library_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(backup_path, library_path)
                backup_path.unlink()
                processed_files.pop(item.relative_path, None)
                if item.relative_path in corrected_files:
                    corrected_files.remove(item.relative_path)
                needs_manual_rotation.add(item.relative_path)
                done += 1

            else:
                errors.append(item.relative_path)
        except Exception:
            errors.append(item.relative_path)

    state["manual_review"] = sorted(manual_review)
    state["corrected_files"] = corrected_files
    state["needs_manual_rotation"] = sorted(needs_manual_rotation)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(state_path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)

    return {"resolved": done, "errors": errors}


STATE_FILES = {
    "orientation_fix": ("processed_files", "orientation_fix.json"),
    "orientation_fix_corrected": ("corrected_files", "orientation_fix.json"),
    "bloomin8_optimize": ("processed_asset_ids", "bloomin8_optimize.json"),
}


@app.get("/api/state/{script_name}")
def get_state(script_name: str, limit: int = 10):
    if script_name not in STATE_FILES:
        raise HTTPException(404, "Pas d'état suivi pour ce script")
    key, filename = STATE_FILES[script_name]
    path = STATE_DIR / filename

    if not path.exists():
        return {"count": 0, "total": None, "remaining": None, "session_processed": None, "sample": []}

    with open(path, "r", encoding="utf-8") as f:
        state = json.load(f)

    # Total mis en cache par orientation_fix.py lui-même (une fois par run),
    # PAS recalculé ici : un rglob() sur ~32000 fichiers via NFS à chaque
    # poll de 5s de l'onglet État a bien failli refaire planter la VM.
    total = state.get("total_files") if script_name in ("orientation_fix", "orientation_fix_corrected") else None

    items = state.get(key, {})
    all_keys = list(items.keys()) if isinstance(items, dict) else list(items)
    count = len(all_keys)

    # Pas d'intérêt à afficher/trier des milliers de chemins : seuls les
    # derniers traités (ordre d'insertion == ordre chronologique, préservé
    # par le JSON) sont utiles pour un coup d'œil rapide.
    last_n = all_keys[-limit:][::-1] if limit else []

    remaining = (total - count) if total is not None else None

    session_processed = None
    session = state.get("session")
    if session and script_name == "orientation_fix":
        # Uniquement pour la vue "traités" (pas "corrected"), le compte
        # global de fichiers examinés est le bon référentiel pour ce calcul.
        full_processed_count = len(state.get("processed_files", {}))
        session_processed = full_processed_count - session.get("count_at_start", full_processed_count)

    return {
        "count": count,
        "total": total,
        "remaining": remaining,
        "session_processed": session_processed,
        "sample": last_n,
    }


@app.get("/api/manual-rotation")
def list_manual_rotation():
    state_path = STATE_DIR / "orientation_fix.json"
    if not state_path.exists():
        return []
    with open(state_path, "r", encoding="utf-8") as f:
        state = json.load(f)
    return sorted(state.get("needs_manual_rotation", []))


class ManualRotationSave(BaseModel):
    relative_path: str
    angle: int  # 0, 90, 180, 270 — sens horaire, cumulé côté frontend


@app.post("/api/manual-rotation/save")
def save_manual_rotation(payload: ManualRotationSave):
    if ".." in payload.relative_path:
        raise HTTPException(400, "Chemin invalide")
    if payload.angle not in (0, 90, 180, 270):
        raise HTTPException(400, "Angle invalide")

    cfg = load_config()
    library_root = Path(cfg["orientation"]["library_path"])
    user_id = cfg["orientation"].get("user_id", "").strip()
    if user_id:
        library_root = library_root / user_id

    library_path = library_root / payload.relative_path
    if not library_path.exists():
        raise HTTPException(404, "Fichier introuvable")

    if payload.angle != 0:
        img = PILImage.open(library_path)
        icc_profile = img.info.get("icc_profile")
        img = img.rotate(-payload.angle, expand=True)  # -angle = sens horaire, cohérent avec l'aperçu

        exif_bytes = b""
        try:
            exif_dict = piexif.load(str(library_path))
            exif_dict["0th"][piexif.ImageIFD.Orientation] = 1
            exif_bytes = piexif.dump(exif_dict)
        except Exception:
            pass

        save_kwargs = {"quality": 95}
        if exif_bytes:
            save_kwargs["exif"] = exif_bytes
        if icc_profile:
            save_kwargs["icc_profile"] = icc_profile
        img.save(library_path, **save_kwargs)

    state_path = STATE_DIR / "orientation_fix.json"
    state = {}
    if state_path.exists():
        with open(state_path, "r", encoding="utf-8") as f:
            state = json.load(f)

    needs_rotation = set(state.get("needs_manual_rotation", []))
    needs_rotation.discard(payload.relative_path)
    manual_review = set(state.get("manual_review", []))
    manual_review.add(payload.relative_path)  # décision définitive, jamais retraité automatiquement

    state["needs_manual_rotation"] = sorted(needs_rotation)
    state["manual_review"] = sorted(manual_review)

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(state_path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)

    return {"status": "ok"}


app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
