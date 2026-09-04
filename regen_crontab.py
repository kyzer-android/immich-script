#!/usr/bin/env python3
"""Régénère /etc/cron.d/immich-scripts à partir des 'schedule' de config.json.
Appelé au démarrage du conteneur (entrypoint.sh) et à chaque sauvegarde de
config depuis le dashboard."""
import sys

sys.path.insert(0, "/app")
from common_config import load_config

CRON_FILE = "/etc/cron.d/immich-scripts"

JOBS = [
    ("orientation", "orientation_fix", "python3 /app/scripts/orientation_fix.py"),
    ("person_to_album", "person_to_album", "python3 /app/scripts/person_to_album.py"),
    ("bloomin8", "bloomin8_optimize", "node /app/scripts/bloomin8_optimize.js"),
]


def main() -> None:
    cfg = load_config()
    lines = ["SHELL=/bin/bash", "PATH=/usr/local/bin:/usr/bin:/bin", ""]

    for config_key, log_name, command in JOBS:
        section = cfg.get(config_key, {})
        if not section.get("enabled", True):
            continue
        schedule = section.get("schedule", "").strip()
        if not schedule:
            continue  # planification vide -> job désactivé, aucune ligne cron générée
        full_cmd = f"{command} >> /data/logs/{log_name}.cron.log 2>&1"
        lines.append(f"{schedule} root {full_cmd}")

    lines.append("")  # ligne vide finale requise par cron

    with open(CRON_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    import os
    os.chmod(CRON_FILE, 0o644)


if __name__ == "__main__":
    main()
