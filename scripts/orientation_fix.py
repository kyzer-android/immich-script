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
4. Retrouve l'assetId Immich (via recherche par chemin) et déclenche un job ciblé
   regenerate-thumbnail + refresh-faces (PAS de "Facial Recognition globale",
   qui efface les assignations de personnes existantes).
"""
import os
import shutil
import signal
import sys
import time
from datetime import datetime, timedelta, timezone
from multiprocessing import Process, Queue
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


def get_immich_asset_id_by_path(server: str, api_key: str, rel_path: str) -> str | None:
    """Retrouve l'assetId via une recherche par nom de fichier + chemin.

    Plus fiable qu'un lookup par checksum (bulk-upload-check) : pour les
    assets importés via scan de bibliothèque (Library/External Library),
    Immich calcule son checksum comme SHA1("path:" + originalPath) — PAS un
    hash du contenu réel du fichier — ce qui rend le lookup par checksum de
    contenu systématiquement infructueux pour ce type d'assets."""
    filename = Path(rel_path).name
    try:
        resp = requests.post(
            f"{server}/api/search/metadata",
            headers={"x-api-key": api_key, "Content-Type": "application/json"},
            json={"originalFileName": filename, "page": 1, "size": 50},
            timeout=30,
        )
        resp.raise_for_status()
        items = resp.json().get("assets", {}).get("items", [])
        for item in items:
            if item.get("originalPath", "").endswith(rel_path):
                return item["id"]
        if len(items) == 1:
            return items[0]["id"]  # un seul résultat, on le prend même si le chemin ne matche pas exactement
    except requests.RequestException as e:
        log(SCRIPT_NAME, f"Erreur recherche assetId ({filename}) : {e}", "WARN")
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
            return False, None  # aucun visage fiable / ambigu
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

    rel_path = str(path.relative_to(library_root))
    asset_id = get_immich_asset_id_by_path(
        cfg["immich"]["server"], cfg["immich"]["api_key"], rel_path
    )
    if not asset_id:
        log(SCRIPT_NAME, f"AssetId Immich introuvable pour {path}, régénération manuelle nécessaire", "WARN")

    return True, asset_id


LOCK_PATH = STATE_DIR / f"{SCRIPT_NAME}.lock"
_stop_requested = False


def _process_file_worker(path: Path, library_root: Path, cfg: dict, queue: Queue) -> None:
    """Exécuté dans un sous-process dédié : process_file() peut bloquer
    indéfiniment sur un appel kernel-level non interruptible (lecture NFS
    en mode "hard" qui hoquette) — un SIGTERM ne suffit pas dans ce cas,
    seul un sous-process séparé peut être tué de force (SIGKILL) sans
    affecter le run principal."""
    try:
        result = process_file(path, library_root, cfg)
        queue.put(("ok", result))
    except Exception as e:
        queue.put(("error", str(e)))


def _process_file_with_timeout(
    path: Path, library_root: Path, cfg: dict, timeout_seconds: int
) -> tuple[bool, str | None, bool]:
    """Retourne (a_été_modifié, assetId, a_timeout). Isole process_file()
    dans un sous-process pour pouvoir le tuer de force au-delà du délai."""
    queue: Queue = Queue()
    proc = Process(target=_process_file_worker, args=(path, library_root, cfg, queue), daemon=True)
    proc.start()
    proc.join(timeout_seconds)

    if proc.is_alive():
        proc.terminate()
        proc.join(5)
        if proc.is_alive():
            proc.kill()  # SIGKILL — dernier recours si terminate() (SIGTERM) n'a pas suffi
            proc.join(5)
        return False, None, True

    if not queue.empty():
        status, payload = queue.get()
        if status == "ok":
            was_modified, asset_id = payload
            return was_modified, asset_id, False
        else:
            raise RuntimeError(payload)

    # Process terminé sans rien mettre dans la queue (crash silencieux,
    # ex: SIGKILL externe type OOM) — traité comme une erreur classique.
    raise RuntimeError(f"Sous-process terminé sans résultat (code {proc.exitcode})")


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


def _refresh_manual_review(state: dict, manual_review: set) -> set:
    """Relit manual_review depuis le disque et fusionne avec la version en
    mémoire — évite d'écraser une validation faite via le dashboard pendant
    que ce run (potentiellement long) est encore en cours."""
    fresh_state = load_state(SCRIPT_NAME)
    fresh_manual_review = set(fresh_state.get("manual_review", []))
    merged = manual_review | fresh_manual_review
    state["manual_review"] = sorted(merged)
    return merged


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
    timeout_seconds = cfg["orientation"].get("file_timeout_seconds", 60)
    max_timeout_retries = cfg["orientation"].get("max_timeout_retries", 3)
    state = load_state(SCRIPT_NAME)
    processed = state.setdefault("processed_files", {})  # path -> True (juste la présence compte, pour le resume)
    corrected_list = state.setdefault("corrected_files", [])  # ordre chronologique préservé (pas trié)
    corrected_seen = set(corrected_list)  # pour les lookups O(1) sans dupliquer la liste
    manual_review = set(state.get("manual_review", []))  # tranchés manuellement, jamais retraités
    timeout_counts = state.setdefault("timeout_counts", {})  # path -> nb de timeouts consécutifs

    # Instantané de début de session : permet au dashboard de calculer
    # "traités depuis le début de CETTE session", distinct du total cumulé.
    state["session"] = {
        "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "count_at_start": len(processed),
    }
    save_state(SCRIPT_NAME, state)

    log(SCRIPT_NAME, f"Comptage des fichiers à scanner dans {library_root}...")
    all_files = [p for p in library_root.rglob("*") if p.is_file() and p.suffix.lower() in formats]
    total_files = len(all_files)
    log(SCRIPT_NAME, f"{total_files} fichier(s) au total à examiner.")

    # Mis en cache pour le dashboard (onglet État) : évite de refaire un scan
    # récursif complet du NFS à chaque poll (c'était la cause du run haute
    # charge CPU/IO du run précédent, cf. discussion).
    state["total_files"] = total_files
    save_state(SCRIPT_NAME, state)

    modified_asset_ids: list[str] = []
    corrected_count = 0
    timeout_count_this_run = 0
    scanned = 0
    PROGRESS_EVERY = 10

    for path in all_files:
        if _stop_requested:
            manual_review = _refresh_manual_review(state, manual_review)
            log(SCRIPT_NAME, f"Arrêt propre après {scanned}/{total_files} fichier(s) — état sauvegardé.")
            save_state(SCRIPT_NAME, state)
            return

        scanned += 1
        rel = str(path.relative_to(library_root))

        if rel in manual_review or rel in processed:
            pass  # décision déjà tranchée / déjà traité — rien à faire pour ce fichier
        else:
            try:
                was_modified, asset_id, timed_out = _process_file_with_timeout(
                    path, library_root, cfg, timeout_seconds
                )
                if timed_out:
                    timeout_count_this_run += 1
                    retries = timeout_counts.get(rel, 0) + 1
                    timeout_counts[rel] = retries
                    if retries >= max_timeout_retries:
                        log(
                            SCRIPT_NAME,
                            f"Timeout ({timeout_seconds}s) x{retries} sur {path} — abandon, "
                            f"marqué traité pour ne pas bloquer le run indéfiniment.",
                            "ERROR",
                        )
                        processed[rel] = True  # abandon définitif : on avance plutôt que de rester planté
                    else:
                        log(
                            SCRIPT_NAME,
                            f"Timeout ({timeout_seconds}s) sur {path} — probable hoquet NFS, "
                            f"nouvelle tentative au prochain passage ({retries}/{max_timeout_retries}).",
                            "WARN",
                        )
                        # PAS marqué processed : sera retenté au prochain run
                    state["timeout_counts"] = timeout_counts
                else:
                    timeout_counts.pop(rel, None)  # succès après un éventuel timeout précédent : on efface le compteur
                    state["timeout_counts"] = timeout_counts
                    if was_modified:
                        corrected_count += 1
                        if rel not in corrected_seen:
                            corrected_list.append(rel)
                            corrected_seen.add(rel)
                    if asset_id:
                        modified_asset_ids.append(asset_id)
                    processed[rel] = True
                state["corrected_files"] = corrected_list
            except Exception as e:
                log(SCRIPT_NAME, f"Erreur sur {path} : {e}", "ERROR")

        if scanned % PROGRESS_EVERY == 0:
            manual_review = _refresh_manual_review(state, manual_review)
            save_state(SCRIPT_NAME, state)  # persiste régulièrement, peu importe skip ou traitement réel
            log(SCRIPT_NAME, f"Progression : {scanned}/{total_files} scanné(s), {corrected_count} corrigé(s) jusqu'ici.")

    manual_review = _refresh_manual_review(state, manual_review)
    save_state(SCRIPT_NAME, state)
    cleanup_old_backups(cfg["orientation"]["backup_retention_days"])

    if modified_asset_ids:
        trigger_immich_jobs(cfg["immich"]["server"], cfg["immich"]["api_key"], modified_asset_ids)
        log(SCRIPT_NAME, f"Job Immich déclenché pour {len(modified_asset_ids)} asset(s).")

    log(SCRIPT_NAME, f"Run terminé. {scanned} fichier(s) scanné(s), {corrected_count} corrigé(s), {timeout_count_this_run} timeout(s), {len(modified_asset_ids)} régénération(s) Immich déclenchée(s).")


if __name__ == "__main__":
    main()
