#!/usr/bin/env python3
"""
orientation_fix.py

1. Parcourt la bibliothèque Immich (montée en volume, modification in-place).
2. Pour chaque photo pas encore traitée :
   a. Si un tag EXIF Orientation valide existe -> on pivote les PIXELS selon ce
      tag, puis on remet Orientation=1 (l'affichage était déjà correct via
      EXIF, mais la reconnaissance faciale d'Immich a besoin de pixels
      physiquement dans le bon sens, cf. discussion préalable).
   b. Sinon (pas d'EXIF ou Orientation=1 par défaut) -> fallback détection de
      visage : on teste les 4 rotations et on garde celle qui donne le plus
      de visages détectés avec la plus forte confiance.
   c. Si aucun visage n'est trouvé dans aucune rotation, on laisse la photo
      telle quelle (rien de fiable pour décider) et on log le cas.
3. Sauvegarde un backup horodaté AVANT toute modification.
4. Retrouve l'assetId Immich (via checksum) et déclenche un job ciblé
   regenerate-thumbnail + refresh-faces (PAS de "Facial Recognition globale",
   qui efface les assignations de personnes existantes).
"""
import hashlib
import os
import shutil
import signal
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import cv2
import piexif
import requests
from PIL import Image
import pillow_heif

pillow_heif.register_heif_opener()

sys.path.insert(0, "/app")
from common_config import load_config, load_state, save_state, log, BACKUP_DIR, STATE_DIR

SCRIPT_NAME = "orientation_fix"

# Cascade Haar pour la détection de visage (fournie avec opencv-python-headless)
FACE_CASCADE = cv2.CascadeClassifier(
    cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
)


def file_checksum(path: Path) -> str:
    """SHA1 du contenu brut du fichier, format attendu par l'API Immich
    (bulk-upload-check). À vérifier/adapter si la version d'Immich change
    d'algorithme de checksum."""
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def get_exif_orientation(path: Path) -> int | None:
    """Retourne la valeur du tag EXIF Orientation (1-8), ou None si absent."""
    try:
        exif_dict = piexif.load(str(path))
        orientation = exif_dict.get("0th", {}).get(piexif.ImageIFD.Orientation)
        return orientation
    except Exception:
        return None


def rotate_by_exif_value(img: Image.Image, orientation: int) -> Image.Image:
    """Applique la rotation physique correspondant à une valeur EXIF standard."""
    rotations = {
        1: lambda i: i,
        2: lambda i: i.transpose(Image.FLIP_LEFT_RIGHT),
        3: lambda i: i.rotate(180, expand=True),
        4: lambda i: i.transpose(Image.FLIP_TOP_BOTTOM),
        5: lambda i: i.transpose(Image.FLIP_LEFT_RIGHT).rotate(90, expand=True),
        6: lambda i: i.rotate(-90, expand=True),
        7: lambda i: i.transpose(Image.FLIP_LEFT_RIGHT).rotate(-90, expand=True),
        8: lambda i: i.rotate(90, expand=True),
    }
    return rotations.get(orientation, lambda i: i)(img)


def best_rotation_by_face_detection(path: Path) -> int:
    """Teste 0/90/180/270°, retourne l'angle donnant le plus de visages
    détectés avec le plus haut score de confiance (nb de voisins Haar).

    Exige une marge de confiance nette entre le meilleur et le 2e meilleur
    candidat avant de trancher — sinon le résultat est jugé ambigu et
    AUCUNE rotation n'est appliquée (mieux vaut ne rien faire qu'une
    mauvaise proposition, la revue manuelle se fait ensuite via le dashboard)."""
    cv_img = cv2.imread(str(path))
    if cv_img is None:
        return -1

    scores: dict[int, float] = {}
    for angle in (0, 90, 180, 270):
        if angle == 0:
            rotated = cv_img
        elif angle == 90:
            rotated = cv2.rotate(cv_img, cv2.ROTATE_90_CLOCKWISE)
        elif angle == 180:
            rotated = cv2.rotate(cv_img, cv2.ROTATE_180)
        else:
            rotated = cv2.rotate(cv_img, cv2.ROTATE_90_COUNTERCLOCKWISE)

        gray = cv2.cvtColor(rotated, cv2.COLOR_BGR2GRAY)
        # minNeighbors relevé (8 au lieu de 5) : réduit nettement les faux positifs
        try:
            faces, reject_levels, level_weights = FACE_CASCADE.detectMultiScale3(
                gray, scaleFactor=1.1, minNeighbors=8, outputRejectLevels=True
            )
            score = float(sum(level_weights)) if len(level_weights) else 0.0
        except Exception:
            faces = FACE_CASCADE.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=8)
            score = float(len(faces))

        # Ignore les visages trop petits (probables faux positifs sur texture/bruit)
        min_face_area = 0.01 * rotated.shape[0] * rotated.shape[1]
        valid_faces = [f for f in faces if f[2] * f[3] >= min_face_area]
        if not valid_faces:
            score = 0.0

        scores[angle] = score

    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    best_angle, best_score = ranked[0]
    second_score = ranked[1][1] if len(ranked) > 1 else 0.0

    if best_score <= 0:
        return -1  # aucun visage fiable trouvé

    # Marge de confiance : le meilleur candidat doit surclasser nettement
    # le 2e (au moins 40% de score en plus), sinon on juge le choix ambigu.
    if second_score > 0 and best_score < second_score * 1.4:
        return -1

    return best_angle


def rotate_by_angle(img: Image.Image, angle: int) -> Image.Image:
    if angle == 0:
        return img
    return img.rotate(-angle, expand=True)


def backup_file(path: Path, library_root: Path) -> None:
    rel = path.relative_to(library_root)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    dest = BACKUP_DIR / ts / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, dest)


def cleanup_old_backups(retention_days: int) -> None:
    if not BACKUP_DIR.exists():
        return
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    for entry in BACKUP_DIR.iterdir():
        if not entry.is_dir():
            continue
        try:
            entry_date = datetime.strptime(entry.name, "%Y%m%d_%H%M%S").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            continue
        if entry_date < cutoff:
            shutil.rmtree(entry, ignore_errors=True)
            log(SCRIPT_NAME, f"Backup expiré supprimé : {entry.name}")


def get_immich_asset_id(server: str, api_key: str, checksum: str) -> str | None:
    try:
        resp = requests.post(
            f"{server}/api/assets/bulk-upload-check",
            headers={"x-api-key": api_key, "Content-Type": "application/json"},
            json={"assets": [{"id": "orientation-fix-check", "checksum": checksum}]},
            timeout=30,
        )
        resp.raise_for_status()
        results = resp.json().get("results", [])
        for r in results:
            if r.get("action") == "reject" and r.get("assetId"):
                return r["assetId"]
    except requests.RequestException as e:
        log(SCRIPT_NAME, f"Erreur lookup assetId (checksum {checksum[:8]}...) : {e}", "WARN")
    return None


def trigger_immich_jobs(server: str, api_key: str, asset_ids: list[str]) -> None:
    """Régénération CIBLÉE (pas de Facial Recognition globale, qui efface les
    assignations de personnes existantes — cf. discussion préalable)."""
    if not asset_ids:
        return
    for job_name in ("regenerate-thumbnail", "refresh-faces"):
        try:
            resp = requests.post(
                f"{server}/api/assets/jobs",
                headers={"x-api-key": api_key, "Content-Type": "application/json"},
                json={"assetIds": asset_ids, "name": job_name},
                timeout=30,
            )
            resp.raise_for_status()
        except requests.RequestException as e:
            log(SCRIPT_NAME, f"Erreur job {job_name} : {e}", "WARN")


def process_file(path: Path, library_root: Path, cfg: dict) -> tuple[bool, str | None]:
    """Retourne (a_été_modifié, assetId_immich_si_trouvé)."""
    checksum_before = file_checksum(path)

    exif_orientation = get_exif_orientation(path)
    method = None

    if exif_orientation and exif_orientation != 1:
        method = f"exif({exif_orientation})"
        img = Image.open(path)
        icc_profile = img.info.get("icc_profile")
        img = rotate_by_exif_value(img, exif_orientation)
    elif cfg["orientation"]["face_detection_fallback"]:
        angle = best_rotation_by_face_detection(path)
        if angle == -1:
            return False, None  # aucun visage fiable / ambigu, silencieux (pas de bruit dans les logs)
        if angle == 0:
            return False, None  # déjà dans le bon sens, rien à faire
        method = f"face_detection({angle}°)"
        img = Image.open(path)
        icc_profile = img.info.get("icc_profile")
        img = rotate_by_angle(img, angle)
    else:
        return False, None

    backup_file(path, library_root)

    # Sauvegarde en écrasant l'original, EXIF Orientation remis à 1 (normal)
    exif_bytes = b""
    try:
        exif_dict = piexif.load(str(path))
        exif_dict["0th"][piexif.ImageIFD.Orientation] = 1
        exif_bytes = piexif.dump(exif_dict)
    except Exception:
        pass

    save_kwargs = {"quality": 95}
    if exif_bytes:
        save_kwargs["exif"] = exif_bytes
    if icc_profile:
        save_kwargs["icc_profile"] = icc_profile
    img.save(path, **save_kwargs)

    log(SCRIPT_NAME, f"Corrigé ({method}) : {path}")

    asset_id = get_immich_asset_id(
        cfg["immich"]["server"], cfg["immich"]["api_key"], checksum_before
    )
    if not asset_id:
        log(SCRIPT_NAME, f"AssetId Immich introuvable pour {path}, régénération manuelle nécessaire", "WARN")
    return True, asset_id


LOCK_PATH = STATE_DIR / f"{SCRIPT_NAME}.lock"
_stop_requested = False


def _handle_sigterm(signum, frame):
    global _stop_requested
    _stop_requested = True
    log(SCRIPT_NAME, "Arrêt demandé — sauvegarde de la progression en cours avant de quitter...", "WARN")


def _acquire_lock() -> bool:
    """Retourne True si le verrou est acquis, False si une autre instance tourne déjà."""
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    if LOCK_PATH.exists():
        try:
            old_pid = int(LOCK_PATH.read_text().strip())
            os.kill(old_pid, 0)  # ne tue rien, vérifie juste que le PID existe encore
            return False  # une instance tourne réellement déjà
        except (ValueError, ProcessLookupError, PermissionError):
            pass  # verrou périmé (process mort sans nettoyer), on continue
    LOCK_PATH.write_text(str(os.getpid()))
    return True


def _release_lock() -> None:
    LOCK_PATH.unlink(missing_ok=True)


def main() -> None:
    if not _acquire_lock():
        log(SCRIPT_NAME, "Une autre instance tourne déjà, run ignoré.", "WARN")
        return

    signal.signal(signal.SIGTERM, _handle_sigterm)

    try:
        _main_body()
    finally:
        _release_lock()


def _main_body() -> None:
    cfg = load_config()
    if not cfg["orientation"]["enabled"]:
        log(SCRIPT_NAME, "Script désactivé dans la config, arrêt.")
        return

    library_root = Path(cfg["orientation"]["library_path"])
    user_id = cfg["orientation"].get("user_id", "").strip()
    if user_id:
        library_root = library_root / user_id
        log(SCRIPT_NAME, f"Scan restreint à l'utilisateur {user_id} : {library_root}")

    if not library_root.exists():
        log(SCRIPT_NAME, f"Chemin bibliothèque introuvable : {library_root}", "ERROR")
        return

    formats = tuple(cfg["orientation"]["formats"])
    state = load_state(SCRIPT_NAME)
    processed = state.setdefault("processed_files", {})  # path -> mtime traité (tous, pour le resume)
    corrected = set(state.setdefault("corrected_files", []))  # uniquement les vraies corrections
    manual_review = set(state.get("manual_review", []))  # tranchés manuellement, jamais retraités

    log(SCRIPT_NAME, f"Comptage des fichiers à scanner dans {library_root}...")
    all_files = [p for p in library_root.rglob("*") if p.is_file() and p.suffix.lower() in formats]
    total_files = len(all_files)
    log(SCRIPT_NAME, f"{total_files} fichier(s) au total à examiner.")

    modified_asset_ids: list[str] = []
    corrected_count = 0
    scanned = 0
    PROGRESS_EVERY = 50

    for path in all_files:
        if _stop_requested:
            log(SCRIPT_NAME, f"Arrêt propre après {scanned}/{total_files} fichier(s) — état sauvegardé.")
            save_state(SCRIPT_NAME, state)
            return

        scanned += 1
        rel = str(path.relative_to(library_root))
        mtime = path.stat().st_mtime

        if rel in manual_review:
            continue  # décision tranchée manuellement via le dashboard, jamais retraité

        if processed.get(rel) == mtime:
            continue  # déjà traité et inchangé depuis

        try:
            was_modified, asset_id = process_file(path, library_root, cfg)
            if was_modified:
                corrected_count += 1
                corrected.add(rel)
            if asset_id:
                modified_asset_ids.append(asset_id)
        except Exception as e:
            log(SCRIPT_NAME, f"Erreur sur {path} : {e}", "ERROR")
            continue

        processed[rel] = path.stat().st_mtime  # mtime post-modification
        state["corrected_files"] = sorted(corrected)

        if scanned % PROGRESS_EVERY == 0:
            save_state(SCRIPT_NAME, state)  # persiste régulièrement, pas seulement en fin de run
            log(SCRIPT_NAME, f"Progression : {scanned}/{total_files} scanné(s), {corrected_count} corrigé(s) jusqu'ici.")

    save_state(SCRIPT_NAME, state)
    cleanup_old_backups(cfg["orientation"]["backup_retention_days"])

    if modified_asset_ids:
        trigger_immich_jobs(cfg["immich"]["server"], cfg["immich"]["api_key"], modified_asset_ids)
        log(SCRIPT_NAME, f"Job Immich déclenché pour {len(modified_asset_ids)} asset(s).")

    log(SCRIPT_NAME, f"Run terminé. {scanned} fichier(s) scanné(s), {corrected_count} corrigé(s), {len(modified_asset_ids)} régénération(s) Immich déclenchée(s).")


if __name__ == "__main__":
    main()
