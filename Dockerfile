FROM python:3.12-slim

# --- Dépendances système ---
# - cron : planification des 3 scripts
# - libheif1/libheif-dev : support HEIC (photos iPhone) via pillow-heif
# - libgl1/libglib2.0-0 : requis par opencv-python-headless au runtime
# - build-essential + libcairo2-dev + libpango1.0-dev + libjpeg-dev + libgif-dev + librsvg2-dev :
#     prérequis de compilation pour node-canvas (utilisé par epdoptimize pour le dithering Spectra 6)
# - nodejs/npm : runtime pour bloomin8_optimize.js
RUN apt-get update && apt-get install -y --no-install-recommends \
    cron \
    curl \
    gnupg \
    git \
    tzdata \
    libheif1 \
    libheif-dev \
    libgl1 \
    libglib2.0-0 \
    build-essential \
    libcairo2-dev \
    libpango1.0-dev \
    libjpeg-dev \
    libgif-dev \
    librsvg2-dev \
    libpixman-1-dev \
    pkg-config \
    python3 \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# --- Dépendances Python ---
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# --- Dépendances Node.js ---
COPY package.json .
RUN npm install --omit=dev

# --- Code applicatif ---
COPY scripts/ ./scripts/
COPY dashboard/ ./dashboard/
COPY common_config.py ./
COPY regen_crontab.py ./
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# --- Répertoires de données (montés en volume) ---
RUN mkdir -p /data/config /data/state /data/logs /data/backups /data/bloomin8-share

EXPOSE 8080

ENTRYPOINT ["/entrypoint.sh"]
