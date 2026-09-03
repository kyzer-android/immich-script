#!/usr/bin/env python3
"""
person_to_album.py

Reproduit le comportement de alangrainger/immich-person-to-album :
pour chaque lien {personId, albumId} défini dans la config, recherche tous
les assets où cette personne apparaît, et les ajoute à l'album s'ils n'y
sont pas déjà.
"""
import sys

import requests

sys.path.insert(0, "/app")
from common_config import load_config, log

SCRIPT_NAME = "person_to_album"


def search_assets_by_person(server: str, api_key: str, person_id: str) -> list[str]:
    """Retourne la liste des assetIds où cette personne est reconnue."""
    asset_ids: list[str] = []
    page = 1
    while True:
        try:
            resp = requests.post(
                f"{server}/api/search/metadata",
                headers={"x-api-key": api_key, "Content-Type": "application/json"},
                json={"personIds": [person_id], "page": page, "size": 500},
                timeout=30,
            )
            resp.raise_for_status()
        except requests.RequestException as e:
            log(SCRIPT_NAME, f"Erreur recherche personId={person_id} : {e}", "ERROR")
            return asset_ids

        data = resp.json()
        items = data.get("assets", {}).get("items", [])
        asset_ids.extend(item["id"] for item in items)

        next_page = data.get("assets", {}).get("nextPage")
        if not next_page:
            break
        page = next_page

    return asset_ids


def get_album_asset_ids(server: str, api_key: str, album_id: str) -> set[str]:
    try:
        resp = requests.get(
            f"{server}/api/albums/{album_id}",
            headers={"x-api-key": api_key},
            timeout=30,
        )
        resp.raise_for_status()
        return {a["id"] for a in resp.json().get("assets", [])}
    except requests.RequestException as e:
        log(SCRIPT_NAME, f"Erreur lecture album {album_id} : {e}", "ERROR")
        return set()


def add_assets_to_album(server: str, api_key: str, album_id: str, asset_ids: list[str]) -> None:
    if not asset_ids:
        return
    try:
        resp = requests.put(
            f"{server}/api/albums/{album_id}/assets",
            headers={"x-api-key": api_key, "Content-Type": "application/json"},
            json={"ids": asset_ids},
            timeout=60,
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        log(SCRIPT_NAME, f"Erreur ajout à l'album {album_id} : {e}", "ERROR")


def main() -> None:
    cfg = load_config()
    if not cfg["person_to_album"]["enabled"]:
        log(SCRIPT_NAME, "Script désactivé dans la config, arrêt.")
        return

    server = cfg["immich"]["server"]
    api_key = cfg["immich"]["api_key"]

    for link in cfg["person_to_album"]["links"]:
        person_id = link["personId"]
        album_id = link["albumId"]
        description = link.get("description", "")

        person_assets = set(search_assets_by_person(server, api_key, person_id))
        if not person_assets:
            log(SCRIPT_NAME, f"[{description}] Aucun asset trouvé pour personId={person_id}")
            continue

        already_in_album = get_album_asset_ids(server, api_key, album_id)
        missing = list(person_assets - already_in_album)

        if missing:
            add_assets_to_album(server, api_key, album_id, missing)
            log(SCRIPT_NAME, f"[{description}] {len(missing)} asset(s) ajouté(s) à l'album {album_id}")
        else:
            log(SCRIPT_NAME, f"[{description}] Album déjà à jour ({len(already_in_album)} asset(s))")


if __name__ == "__main__":
    main()
