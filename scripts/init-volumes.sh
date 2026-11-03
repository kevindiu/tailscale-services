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
mkdir -p "${DATA_DIR}/antigravity"
mkdir -p "${DATA_DIR}/hermes"

# Set secure permissions: private to current user
chmod 700 "${DATA_DIR}/tailscale"
chmod 700 "${DATA_DIR}/adguard"
chmod 700 "${DATA_DIR}/rustdesk"
chmod 700 "${DATA_DIR}/antigravity"
chmod 755 "${DATA_DIR}/hermes"

echo "[✓] Directory initialization complete."
