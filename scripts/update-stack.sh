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

UP_ARGS=(--remove-orphans --wait --wait-timeout 300)

echo "===== $(date -Is) update start ====="

old_container=""
old_image=""
if docker inspect tailscale >/dev/null 2>&1; then
  old_container=$(docker inspect -f '{{.Id}}' tailscale)
  old_image=$(docker inspect -f '{{.Image}}' tailscale)
fi

docker compose pull --quiet

new_image=$(docker image inspect -f '{{.Id}}' \
  "$(docker compose config --images | grep -m1 'tailscale/tailscale')")

if [[ -n "$old_image" && "$old_image" != "$new_image" ]]; then
  echo "tailscale image changed -> recreating the whole stack"
  docker compose up -d --force-recreate "${UP_ARGS[@]}"
else
  docker compose up -d "${UP_ARGS[@]}"
  # A config change (not an image change) can still force tailscale to be
  # recreated. Dependents then hold a dead netns, yet their loopback
  # healthchecks keep passing, so detect it explicitly and rebuild them.
  new_container=$(docker inspect -f '{{.Id}}' tailscale 2>/dev/null || true)
  if [[ -n "$old_container" && "$old_container" != "$new_container" ]]; then
    echo "tailscale was recreated -> rebuilding dependents"
    docker compose up -d --force-recreate "${UP_ARGS[@]}"
  fi
fi

docker image prune -f

echo "===== $(date -Is) update done ====="
