"""
Config partagée entre orientation_fix.py, person_to_album.py, le dashboard,
et (lue en JSON brut) par bloomin8_optimize.js.

Principe : les variables d'environnement du docker-compose ne servent que de
valeurs PAR DÉFAUT au tout premier démarrage. Une fois /data/config/config.json
créé, c'est ce fichier qui fait autorité — modifiable à chaud depuis le dashboard,
sans avoir besoin de recréer le conteneur.
"""
import json
import os
from pathlib import Path
from datetime import datetime, timezone

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
CONFIG_PATH = DATA_DIR / "config" / "config.json"
STATE_DIR = DATA_DIR / "state"
LOG_DIR = DATA_DIR / "logs"
BACKUP_DIR = DATA_DIR / "backups"

DEFAULT_CONFIG = {
    "immich": {
        "server": os.environ.get("IMMICH_SERVER", "http://192.168.1.205:2283"),
        "api_key": os.environ.get("IMMICH_API_KEY", ""),
    },
    "orientation": {
        "enabled": True,
        "schedule": os.environ.get("ORIENTATION_SCHEDULE", "0 3 * * *"),
        "library_path": os.environ.get("ORIENTATION_LIBRARY_PATH", "/immich-library"),
        "user_id": os.environ.get("ORIENTATION_USER_ID", ""),  # optionnel : limite au sous-dossier de cet utilisateur Immich
        "backup_retention_days": int(os.environ.get("BACKUP_RETENTION_DAYS", "30")),
        "face_detection_fallback": True,
        "formats": [".jpg", ".jpeg", ".heic", ".heif", ".png"],
        # Timeout dur par fichier (sous-process tuable de force). Protège
        # contre un blocage kernel-level non interruptible par signal —
        # typiquement un hoquet sur un montage NFS en mode "hard" (cf.
        # incident du 2026-09-06 : SIGTERM resté sans effet pendant 3h+).
        "file_timeout_seconds": int(os.environ.get("ORIENTATION_FILE_TIMEOUT", "60")),
        # Après ce nombre de timeouts consécutifs sur le MÊME fichier, on
        # abandonne et on le marque traité (avec un WARN) pour ne pas
        # bloquer indéfiniment tout le run sur un fichier structurellement
        # inaccessible (au lieu de le retenter à chaque passage, à vie).
        "max_timeout_retries": int(os.environ.get("ORIENTATION_MAX_TIMEOUT_RETRIES", "3")),
    },
    "person_to_album": {
        "enabled": True,
        "schedule": os.environ.get("PERSON_TO_ALBUM_SCHEDULE", "*/30 * * * *"),
        # Reprend exactement la structure du CONFIG JSON de alangrainger/immich-person-to-album
        "links": [
            {
                "description": "Photos of Sohan",
                "personId": "78234a8d-b9f3-407c-9683-d24c2108f113",
                "albumId": "04bca25d-8c6e-40e3-8ba0-e9e759bf7bbb",
            }
        ],
    },
    "bloomin8": {
        "enabled": True,
        "schedule": os.environ.get("BLOOMIN8_SCHEDULE", "0 4 * * *"),
        "album_id": os.environ.get("BLOOMIN8_ALBUM_ID", "04bca25d-8c6e-40e3-8ba0-e9e759bf7bbb"),
        "orientation": os.environ.get("BLOOMIN8_ORIENTATION", "portrait"),  # portrait | landscape
        "resolution": {"width": 1600, "height": 1200},
        "destination_path": os.environ.get("BLOOMIN8_DEST_PATH", "/data/bloomin8-share"),
        # Rempli par le dashboard quand il détecte un changement d'album (orientation inchangée) :
        # None tant qu'aucune action n'est en attente, "clear" ou "keep" sinon.
        "pending_action": None,
        # Snapshot du dernier run, utilisé pour détecter les changements d'orientation/album
        "_last_run": {"album_id": None, "orientation": None},
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    """Fusionne override dans base sans écraser les clés absentes de override."""
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def load_config() -> dict:
    """Charge /data/config/config.json, le crée avec les valeurs par défaut sinon."""
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not CONFIG_PATH.exists():
        save_config(DEFAULT_CONFIG)
        return json.loads(json.dumps(DEFAULT_CONFIG))  # copie profonde

    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        stored = json.load(f)

    # Fusion avec les défauts : garantit que toute nouvelle clé ajoutée par une
    # mise à jour du conteneur est bien présente sans écraser la config existante.
    merged = json.loads(json.dumps(DEFAULT_CONFIG))
    _deep_merge(merged, stored)
    return merged


def save_config(config: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = CONFIG_PATH.with_suffix(".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
    tmp_path.replace(CONFIG_PATH)  # écriture atomique


def load_state(name: str) -> dict:
    """État incrémental d'un script (fichiers déjà traités, etc.)."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = STATE_DIR / f"{name}.json"
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_state(name: str, state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = STATE_DIR / f"{name}.json"
    tmp_path = path.with_suffix(".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    tmp_path.replace(path)


LOG_LEVELS = {"INFO": 0, "WARN": 1, "ERROR": 2}
PERSISTED_LOG_MIN_LEVEL = "WARN"  # seuls WARN/ERROR vont dans le fichier lu par le dashboard


def log(script: str, message: str, level: str = "INFO") -> None:
    """Log simple, fichier par script, lu ensuite par le dashboard.

    Tous les niveaux sont affichés en console (capturés intégralement dans
    les logs cron bruts, utiles pour du debug approfondi en SSH), mais seuls
    WARN/ERROR sont persistés dans le fichier lu par le dashboard, pour
    éviter de le noyer sous des milliers de lignes INFO sans intérêt."""
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    line = f"{ts} [{level}] {message}"
    print(line)

    if LOG_LEVELS.get(level, 0) >= LOG_LEVELS[PERSISTED_LOG_MIN_LEVEL]:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_DIR / f"{script}.log", "a", encoding="utf-8") as f:
            f.write(line + "\n")
