# immich-scripts

Conteneur unique regroupant 3 automatisations Immich + un dashboard web de pilotage.

## Composants

| Script | Langage | Rôle |
|---|---|---|
| `orientation_fix.py` | Python + OpenCV | Corrige l'orientation des photos (EXIF prioritaire, fallback détection de visage), backup avant modif, régénération ciblée vignettes/visages |
| `person_to_album.py` | Python | Ajoute automatiquement les photos d'une personne à un album (remplace `alangrainger/immich-person-to-album`) |
| `bloomin8_optimize.js` | Node.js + epdoptimize | Filtre un album par orientation, optimise pour l'écran Spectra 6 du cadre BLOOMIN8, dépose dans un dossier partagé en NFS avec HAOS |
| `dashboard/` | Python (FastAPI) + HTML/JS | Configuration à chaud, gestion des backups, logs, galerie |

## Déploiement (Portainer via dépôt Git)

Portainer ne build pas d'image à partir d'un `docker-compose.yml` collé directement dans l'éditeur web — `build: .` a besoin d'un contexte de fichiers sur disque. Il faut donc passer par un déploiement **Stacks → Repository** :

1. Créer un dépôt Git (GitHub privé, Gitea auto-hébergé...) et y pousser l'intégralité de ce dossier (`.gitignore` déjà prêt pour exclure `__pycache__`, `node_modules`, `.env`, `data/`).
2. Copier `.env.example` en `.env` **en local uniquement** (jamais poussé sur le repo), renseigner `IMMICH_API_KEY`, `IMMICH_SCRIPTS_DATA` et `IMMICH_LIBRARY_PATH`.
3. Créer les dossiers hôte sur la VM Docker (`192.168.1.205`) :
   ```bash
   mkdir -p /home/mathieu/immich-scripts-data
   ```
4. Dans Portainer → **Stacks → Add stack → Repository** : renseigner l'URL du dépôt (+ credentials si privé), le chemin vers `docker-compose.yml` (racine du repo), puis coller les variables d'environnement du `.env`.
5. Déployer — Portainer clone le repo et **build l'image lui-même** via le `Dockerfile`, sans étape manuelle de ta part. `build: .` reste tel quel dans le compose, pas besoin de basculer vers `image: immich-scripts:latest`.

**Mises à jour futures** : push tes changements sur le repo, puis dans Portainer clique **"Pull and redeploy"** sur la stack — elle re-clone et rebuild automatiquement.

## Premier démarrage

Au premier lancement, `/data/config/config.json` est créé à partir des variables d'environnement. **Toute modification ultérieure passe par le dashboard** — les variables d'environnement ne servent plus après ce premier démarrage (sauf recréation du volume `/data`).

## Partage NFS vers HAOS (pour BLOOMIN8)

Le dossier `${IMMICH_SCRIPTS_DATA}/bloomin8-share` (mappé sur `/data/bloomin8-share` dans le conteneur) doit être exposé en NFS à la VM HAOS :

1. Sur le host Proxmox, étendre `/etc/exports` pour autoriser l'IP de HAOS sur ce chemin.
2. Dans HAOS : `Paramètres → Système → Stockage → Ajouter un stockage réseau` (type NFS), pointer vers ce chemin.
3. Configurer l'intégration `bloomin8_pull` avec `image_dir` pointant vers ce point de montage.

## Comportement de reconstruction BLOOMIN8

- **Changement d'orientation** (portrait ↔ paysage) : vidage automatique du dossier + reconstruction complète, sans confirmation.
- **Changement d'album seul** (orientation inchangée) : le dashboard demande de choisir entre "Conserver" et "Vider" le contenu existant avant le prochain run.

## Notes techniques

- L'API `POST /assets/jobs` (avec `refresh-faces` et `regenerate-thumbnail`) est utilisée pour cibler précisément les photos corrigées — jamais de "Facial Recognition" globale, qui efface les assignations de personnes existantes.
- Le checksum utilisé pour retrouver l'`assetId` Immich via `/api/assets/bulk-upload-check` est un SHA1 du fichier. À vérifier/adapter si une future version d'Immich change cet algorithme.
- `pillow-heif` est inclus pour gérer les photos HEIC (iPhone), même si l'essentiel de la bibliothèque est en JPEG.
