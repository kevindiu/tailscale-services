#!/usr/bin/env bash
# ==============================================================================
# Initialize data directories for tailscale-services with least privilege permissions
# ==============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
DATA_DIR="${ROOT_DIR}/data"

echo "[+] Initializing persistent data directories in ${DATA_DIR}..."

mkdir -p "${DATA_DIR}/tailscale"
mkdir -p "${DATA_DIR}/adguard/work"
mkdir -p "${DATA_DIR}/adguard/conf"
mkdir -p "${DATA_DIR}/rustdesk"
mkdir -p "${DATA_DIR}/hermes"

# Set secure permissions: private to current user.
# Skip directories already chowned by a container (e.g. data/hermes -> UID 10000 on Linux).
for dir in tailscale adguard rustdesk hermes; do
  if [ -O "${DATA_DIR}/${dir}" ]; then
    chmod 700 "${DATA_DIR}/${dir}"
  fi
done

echo "[✓] Directory initialization complete."
