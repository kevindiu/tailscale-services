#!/usr/bin/env bash
# Pull the newest images and recreate the stack in dependency order.
#
# Why compose and not Watchtower: every dependent service joins the tailscale
# container's network namespace by ID. Recreating tailscale alone leaves them
# pointing at a dead namespace, so the recreating step has to happen through
# compose, which restarts tailscale first and then rebuilds the dependents.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

echo "===== $(date -Is) update start ====="

old_tailscale=""
if docker inspect tailscale >/dev/null 2>&1; then
  old_tailscale=$(docker inspect -f '{{.Image}}' tailscale)
fi

docker compose pull --quiet

new_tailscale=$(docker image inspect -f '{{.Id}}' \
  "$(docker compose config --images | grep -m1 'tailscale/tailscale')")

if [[ -n "$old_tailscale" && "$old_tailscale" != "$new_tailscale" ]]; then
  echo "tailscale image changed -> recreating the whole stack"
  docker compose up -d --force-recreate --wait --wait-timeout 300
elif ! docker compose up -d --wait --wait-timeout 300; then
  # Safety net: a config change can also force tailscale to be recreated.
  echo "health check failed -> forcing a full recreate"
  docker compose up -d --force-recreate --wait --wait-timeout 300
fi

docker image prune -f

echo "===== $(date -Is) update done ====="
