#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA_DIR="${ROOT_DIR}/data"

mkdir -p "${DATA_DIR}"/{tailscale,adguard/work,adguard/conf,rustdesk,hermes,immich/library,immich/postgres,immich/model-cache,rclone-manager/restore}

# Also create custom IMMICH_UPLOAD_LOCATION if configured in .env
if [ -f "${ROOT_DIR}/.env" ]; then
  UPLOAD_DIR="$(sed -n 's/^IMMICH_UPLOAD_LOCATION=//p' "${ROOT_DIR}/.env" | tail -n 1 | tr -d '"' | tr -d "'")"
  if [ -n "${UPLOAD_DIR}" ]; then
    case "${UPLOAD_DIR}" in
      /*) mkdir -p "${UPLOAD_DIR}" ;;
      *)  mkdir -p "${ROOT_DIR}/${UPLOAD_DIR}" ;;
    esac
  fi
fi

if [ -O "${DATA_DIR}" ]; then
  chmod 700 "${DATA_DIR}"
else
  echo "WARN: ${DATA_DIR} is not owned by $(id -un); run: sudo chmod 700 '${DATA_DIR}'" >&2
fi

# Skip directories already chowned by a non-root container (e.g. data/hermes -> UID 10000),
# and warn if root-owned directories (e.g. created by Docker) remain world-readable.
for dir in tailscale adguard rustdesk hermes immich rclone-manager; do
  target="${DATA_DIR}/${dir}"
  if [ -O "${target}" ]; then
    chmod 700 "${target}"
  else
    echo "WARN: ${target} is not owned by $(id -un) — verify permissions are 700" >&2
  fi
done

echo "Initialized ${DATA_DIR}"
