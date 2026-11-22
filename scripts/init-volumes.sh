#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA_DIR="${ROOT_DIR}/data"

mkdir -p "${DATA_DIR}"/{tailscale,adguard/work,adguard/conf,rustdesk,hermes,immich/library,immich/postgres,immich/model-cache}

# Skip directories already chowned by a container (e.g. data/hermes -> UID 10000)
for dir in tailscale adguard rustdesk hermes immich; do
  if [ -O "${DATA_DIR}/${dir}" ]; then
    chmod 700 "${DATA_DIR}/${dir}"
  fi
done

echo "Initialized ${DATA_DIR}"
