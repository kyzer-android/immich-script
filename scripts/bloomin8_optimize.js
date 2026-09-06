#!/usr/bin/env node
/**
 * bloomin8_optimize.js
 *
 * 1. Lit la config partagée /data/config/config.json (écrite par le
 *    dashboard Python, format agnostique au langage).
 * 2. Détecte un changement d'orientation ou d'album depuis le dernier run :
 *      - orientation changée  -> vidage automatique + reconstruction complète
 *      - album changé seul    -> respecte "pending_action" ("clear" ou "keep")
 *        positionné par le dashboard après confirmation de l'utilisateur
 * 3. Récupère les assets de l'album Immich source, filtre STRICTEMENT selon
 *    l'orientation configurée (portrait/landscape, via width/height —
 *    fiable car orientation_fix.py a déjà normalisé les pixels/EXIF en amont).
 * 4. Télécharge, optimise (resize/crop + correction colorimétrique) et
 *    dépose dans le dossier partagé en NFS avec HAOS.
 *
 * IMPORTANT — pipeline de couleur (revu) :
 * BLOOMIN8 fait SON PROPRE dithering en interne à partir d'un JPEG en tons
 * continus. Pré-ditherer côté script (palette figée à 6 couleurs) est
 * CONTRE-PRODUCTIF pour ce cadre : ça produit un double dithering + des
 * artefacts de recompression JPEG sur un motif déjà quantifié. C'est
 * documenté par l'auteur de bloomin8_pull, qui déconseille explicitement
 * epdoptimize (fait pour SES cadres paperlesspaper) sur du BLOOMIN8 :
 * https://github.com/fwmone/eink-optimize
 * On applique donc à la place : resize/crop -> gamma -> saturation ->
 * lift des ombres -> export JPEG qualité élevée, SANS quantification de
 * palette. Les valeurs par défaut (gamma 0.85, saturation 1.15, lift 13,
 * liftThreshold 90) sont celles documentées par l'auteur pour BLOOMIN8.
 */
const fs = require("fs");
const path = require("path");
const fetch = require("node-fetch");
const { createCanvas, loadImage } = require("canvas");

const DATA_DIR = process.env.DATA_DIR || "/data";
const CONFIG_PATH = path.join(DATA_DIR, "config", "config.json");
const STATE_PATH = path.join(DATA_DIR, "state", "bloomin8_optimize.json");
const LOCK_PATH = path.join(DATA_DIR, "state", "bloomin8_optimize.lock");

function acquireLock() {
  fs.mkdirSync(path.dirname(LOCK_PATH), { recursive: true });
  if (fs.existsSync(LOCK_PATH)) {
    const oldPid = parseInt(fs.readFileSync(LOCK_PATH, "utf-8").trim(), 10);
    try {
      process.kill(oldPid, 0); // ne tue rien, vérifie juste que le PID existe encore
      return false; // une instance tourne réellement déjà
    } catch (e) {
      // verrou périmé (process mort sans nettoyer), on continue
    }
  }
  fs.writeFileSync(LOCK_PATH, String(process.pid));
  return true;
}

function releaseLock() {
  try {
    fs.unlinkSync(LOCK_PATH);
  } catch (e) {
    // déjà absent, rien à faire
  }
}

let stopRequested = false;
process.on("SIGTERM", () => {
  stopRequested = true;
  log("Arrêt demandé — sauvegarde de la progression en cours avant de quitter...", "WARN");
});

function log(message, level = "INFO") {
  const ts = new Date().toISOString();
  const line = `${ts} [${level}] ${message}\n`;
  fs.mkdirSync(path.join(DATA_DIR, "logs"), { recursive: true });
  fs.appendFileSync(path.join(DATA_DIR, "logs", "bloomin8_optimize.log"), line);
  process.stdout.write(line);
}

function loadConfig() {
  const raw = fs.readFileSync(CONFIG_PATH, "utf-8");
  return JSON.parse(raw);
}

function saveConfig(cfg) {
  fs.writeFileSync(CONFIG_PATH, JSON.stringify(cfg, null, 2), "utf-8");
}

function loadState() {
  if (!fs.existsSync(STATE_PATH)) return { processed_asset_ids: [] };
  return JSON.parse(fs.readFileSync(STATE_PATH, "utf-8"));
}

function saveState(state) {
  fs.mkdirSync(path.dirname(STATE_PATH), { recursive: true });
  fs.writeFileSync(STATE_PATH, JSON.stringify(state, null, 2), "utf-8");
}

function clearDestination(destDir) {
  if (fs.existsSync(destDir)) {
    for (const f of fs.readdirSync(destDir)) {
      fs.rmSync(path.join(destDir, f), { force: true });
    }
  }
  log(`Dossier de destination vidé : ${destDir}`);
}

async function fetchAlbumAssets(server, apiKey, albumId) {
  const url = `${server}/api/search/metadata`;

  const body = {
    albumIds: [albumId],
    page: 1,
    size: 1000,
    type: 'IMAGE'
  };

  const resp = await fetch(url, {
    method: "POST",
    headers: {
      "x-api-key": apiKey,
      "Content-Type": "application/json",
    },
    body: JSON.stringify(body),
  });

  if (!resp.ok) {
    throw new Error(
      `Erreur lecture album ${albumId} : HTTP ${resp.status}`
    );
  }

  const data = await resp.json();
  const assets = data.assets?.items || [];

  log(`Album ${albumId} lu : ${assets.length} asset(s)`);
  return assets;
}

function matchesOrientation(asset, wantedOrientation) {
  // width/height reflètent les dimensions réelles du fichier après la
  // normalisation faite par orientation_fix.py (EXIF Orientation=1).
  const w = asset?.width;
  const h = asset?.height;
  if (!w || !h) return false; // pas de métadonnées fiables -> exclu (traitement strict)
  const isPortrait = h > w;
  return wantedOrientation === "portrait" ? isPortrait : !isPortrait;
}

async function downloadOriginal(server, apiKey, assetId) {
  const resp = await fetch(`${server}/api/assets/${assetId}/original`, {
    headers: { "x-api-key": apiKey },
  });
  if (!resp.ok) {
    throw new Error(`Téléchargement asset ${assetId} : HTTP ${resp.status}`);
  }
  return Buffer.from(await resp.arrayBuffer());
}

/**
 * Corrections colorimétriques appliquées en place sur les données de pixels
 * (RGBA), dans cet ordre : gamma -> saturation -> lift des ombres.
 * Reproduit le comportement documenté (paramètres gamma/saturation/lift/
 * liftThreshold) de https://github.com/fwmone/eink-optimize — reconstruit
 * à partir de sa doc publique, pas une copie du fichier source (non
 * accessible depuis cet environnement).
 */
function applyColorGrading(imageData, { gamma, saturation, lift, liftThreshold }) {
  const data = imageData.data;

  // Gamma : LUT 0-255 précalculée, moins cher que Math.pow par pixel.
  const gammaLUT = new Uint8ClampedArray(256);
  for (let i = 0; i < 256; i++) {
    gammaLUT[i] = Math.round(255 * Math.pow(i / 255, gamma));
  }

  for (let i = 0; i < data.length; i += 4) {
    let r = gammaLUT[data[i]];
    let g = gammaLUT[data[i + 1]];
    let b = gammaLUT[data[i + 2]];

    // Saturation : écart à la luminance perçue (Rec. 601), mis à l'échelle.
    if (saturation !== 1) {
      const luma = 0.299 * r + 0.587 * g + 0.114 * b;
      r = luma + (r - luma) * saturation;
      g = luma + (g - luma) * saturation;
      b = luma + (b - luma) * saturation;
    }

    // Lift des ombres : les pixels sous liftThreshold sont éclaircis,
    // proportionnellement à leur distance au seuil (effet dégressif —
    // rien ne bouge déjà au-dessus du seuil).
    if (lift > 0) {
      r = liftChannel(r, lift, liftThreshold);
      g = liftChannel(g, lift, liftThreshold);
      b = liftChannel(b, lift, liftThreshold);
    }

    data[i] = clamp255(r);
    data[i + 1] = clamp255(g);
    data[i + 2] = clamp255(b);
  }

  return imageData;
}

function liftChannel(value, lift, threshold) {
  if (value >= threshold) return value;
  const depth = 1 - value / threshold; // 0 = au seuil, 1 = noir pur
  return value + lift * depth;
}

function clamp255(v) {
  return v < 0 ? 0 : v > 255 ? 255 : Math.round(v);
}

async function optimizeForBloomin8(buffer, targetWidth, targetHeight, colorOptions) {
  const img = await loadImage(buffer);

  // Resize + crop centré ("cover") pour remplir exactement la résolution cible
  const srcRatio = img.width / img.height;
  const dstRatio = targetWidth / targetHeight;
  let sx = 0, sy = 0, sw = img.width, sh = img.height;
  if (srcRatio > dstRatio) {
    sw = img.height * dstRatio;
    sx = (img.width - sw) / 2;
  } else {
    sh = img.width / dstRatio;
    sy = (img.height - sh) / 2;
  }

  const canvas = createCanvas(targetWidth, targetHeight);
  const ctx = canvas.getContext("2d");
  ctx.drawImage(img, sx, sy, sw, sh, 0, 0, targetWidth, targetHeight);

  // Correction colorimétrique en place sur les pixels bruts. Le résultat
  // reste en tons CONTINUS (pas de palette figée) : c'est le firmware de
  // BLOOMIN8 qui fait son propre dithering final à la réception du JPEG.
  const imageData = ctx.getImageData(0, 0, targetWidth, targetHeight);
  applyColorGrading(imageData, colorOptions);
  ctx.putImageData(imageData, 0, 0);

  // Qualité élevée : l'image reste en tons continus, donc pas de motif de
  // dithering à préserver ici (contrairement à l'ancienne approche
  // epdoptimize) — un JPEG classique à quality 0.95 convient très bien.
  return canvas.toBuffer("image/jpeg", { quality: 0.95 });
}

async function mainBody() {
  const cfg = loadConfig();
  const bloomin8 = cfg.bloomin8;

  if (!bloomin8.enabled) {
    log("Script désactivé dans la config, arrêt.");
    return;
  }

  const destDir = bloomin8.destination_path;
  fs.mkdirSync(destDir, { recursive: true });

  const lastRun = bloomin8._last_run || { album_id: null, orientation: null };
  const orientationChanged = lastRun.orientation !== null && lastRun.orientation !== bloomin8.orientation;
  const albumChanged = lastRun.album_id !== null && lastRun.album_id !== bloomin8.album_id;

  let state = loadState();
  let fullRebuild = false;

  if (orientationChanged) {
    // Priorité absolue : vidage automatique, aucune confirmation nécessaire
    log(`Orientation changée (${lastRun.orientation} -> ${bloomin8.orientation}) : reconstruction complète.`);
    clearDestination(destDir);
    state = { processed_asset_ids: [] };
    fullRebuild = true;
  } else if (albumChanged) {
    if (bloomin8.pending_action === "clear") {
      log(`Album changé (${lastRun.album_id} -> ${bloomin8.album_id}), action confirmée : vider.`);
      clearDestination(destDir);
      state = { processed_asset_ids: [] };
      fullRebuild = true;
    } else if (bloomin8.pending_action === "keep") {
      log(`Album changé (${lastRun.album_id} -> ${bloomin8.album_id}), action confirmée : conserver le contenu existant.`);
    } else {
      // Aucune décision prise via le dashboard -> on n'agit pas ce run-ci,
      // on attend que l'utilisateur tranche pour éviter une perte de données.
      log("Album changé mais aucune décision (garder/vider) confirmée via le dashboard. Run ignoré.", "WARN");
      return;
    }
    // La décision a été consommée, on la remet à zéro
    bloomin8.pending_action = null;
  }

  const assets = await fetchAlbumAssets(cfg.immich.server, cfg.immich.api_key, bloomin8.album_id);
  const filtered = assets.filter((a) => matchesOrientation(a, bloomin8.orientation));

  log(`${assets.length} asset(s) dans l'album, ${filtered.length} correspondent à l'orientation "${bloomin8.orientation}".`);

  // Valeurs par défaut = celles documentées par fwmone pour BLOOMIN8
  // portrait (https://github.com/fwmone/eink-optimize), surchageables
  // depuis la config si besoin d'ajuster au rendu réel du cadre.
  const colorOptions = {
    gamma: bloomin8.color?.gamma ?? 0.85,
    saturation: bloomin8.color?.saturation ?? 1.15,
    lift: bloomin8.color?.lift ?? 13,
    liftThreshold: bloomin8.color?.liftThreshold ?? 90,
  };

  const processedSet = new Set(state.processed_asset_ids || []);
  let done = 0;

  for (const asset of filtered) {
    if (stopRequested) {
      saveState({ processed_asset_ids: Array.from(processedSet) });
      log(`Arrêt propre après ${done} image(s) optimisée(s) — état sauvegardé.`);
      return;
    }

    if (!fullRebuild && processedSet.has(asset.id)) continue; // déjà traité, run incrémental

    try {
      const original = await downloadOriginal(cfg.immich.server, cfg.immich.api_key, asset.id);
      const optimized = await optimizeForBloomin8(
        original,
        bloomin8.resolution.width,
        bloomin8.resolution.height,
        colorOptions
      );
      const outPath = path.join(destDir, `${asset.id}.jpg`);
      fs.writeFileSync(outPath, optimized);
      processedSet.add(asset.id);
      done += 1;
    } catch (e) {
      log(`Erreur sur asset ${asset.id} : ${e.message}`, "ERROR");
    }
  }

  saveState({ processed_asset_ids: Array.from(processedSet) });

  // Met à jour le snapshot pour la détection de changement au prochain run
  cfg.bloomin8._last_run = { album_id: bloomin8.album_id, orientation: bloomin8.orientation };
  cfg.bloomin8.pending_action = null;
  saveConfig(cfg);

  log(`Run terminé. ${done} image(s) optimisée(s) et déposée(s) dans ${destDir}.`);
}

async function main() {
  if (!acquireLock()) {
    log("Une autre instance tourne déjà, run ignoré.", "WARN");
    return;
  }
  try {
    await mainBody();
  } finally {
    releaseLock();
  }
}

main().catch((e) => {
  log(`Erreur fatale : ${e.stack || e.message}`, "ERROR");
  process.exit(1);
});
