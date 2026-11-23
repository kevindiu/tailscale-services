# AGENTS.md

AI agent instructions and invariants for `tailscale-services`. See [README.md](README.md) for setup and operational commands.

## Stack

All application containers share `tailscale`'s network namespace (`network_mode: "service:tailscale"`):
- **`tailscale`**: WireGuard mesh, MagicDNS (`tailscale-services`), exit node, and tailnet-only HTTPS proxy (`config/serve.json`).
- **`adguardhome`**: DNS ad-blocker (`0.0.0.0:53`), loopback Web UI (`127.0.0.1:3000` → HTTPS `:3443`).
- **`rustdesk-hbbs` / `rustdesk-hbbr`**: Private RustDesk ID & relay (`-k _`, ports `21115–21119`).
- **`hermes`**: Containerized Hermes AI agent (`nousresearch/hermes-agent`), loopback Web UI (`127.0.0.1:9119` → HTTPS `:9443`). Opt-in `/v1` API (`:8642`) requires `API_SERVER_ENABLED=true` and `API_SERVER_KEY`.
- **`immich-server`**: Private photo/video backup, loopback Web UI (`127.0.0.1:2283` → HTTPS `:2443`). Internal backends (`immich-postgres`, `immich-redis`, `immich-machine-learning`) run on the private Compose bridge only (unreachable from `tailscale0`).
- **`backrest`**: Encrypted Restic/rclone backup UI (`127.0.0.1:9898` → HTTPS `:5443`), mounts Immich library `:ro` at `/userdata/immich` and writable `./data/backrest/rclone` for OAuth refresh.

## Security Invariants

1. **No host ports:** Never add `ports:` mappings. Sole exception: `41641:41641/udp` on `tailscale` (pins WireGuard port for mobile stability).
2. **Shared namespace:** User-facing services must keep `network_mode: "service:tailscale"` and `depends_on: tailscale: { condition: service_healthy, restart: true }`. Internal backends (DB, cache, ML) stay on the private Compose bridge with zero host ports.
3. **No Docker socket:** Never mount `/var/run/docker.sock` or add an updater container. Updates run manually via Compose.
4. **No host processes:** Hermes runs strictly inside Docker; never use host scripts or `host.docker.internal`.
5. **Tailnet-only HTTPS:** Keep `"AllowFunnel": false` for every port in `config/serve.json`. Mount `./config:/config:ro` as a directory (not a single file) so `containerboot` watches changes. If binding port `443`, add `NET_BIND_SERVICE` to `tailscale.cap_add`.
6. **Least privilege:** Every container must keep `no-new-privileges:true`, `cap_drop: ALL`, minimal `cap_add`, memory + `deploy.resources.limits.pids` caps, `<<: *default-logging`, and `TZ=${TZ:-Asia/Tokyo}`.
7. **Secrets:** Never commit `.env` or `data/`, and never put real secrets in `env.example` or compose defaults.

## Editing & Ops Gotchas

- Keep `name: tailscale-services` at the root of `docker-compose.yml`.
- Keep `TS_ACCEPT_DNS=false` on `tailscale` (prevents DNS loop deadlocks with AdGuard).
- **Real healthchecks only:** Probe the actual service (`nc -z 127.0.0.1 53`, `GET /api/health`); never add catch-all fallbacks (e.g. `|| test -e /proc/net/tcp`). Do not add shell healthchecks to `rustdesk` (minimal image).
- **Stateful shutdowns:** Keep `stop_grace_period` on services writing DB state (Hermes uses `30s` for SQLite).
- **Namespace re-attachment:** Dependents bind to `tailscale` by container ID. Always update through `docker compose pull && docker compose up -d --remove-orphans`, and verify dependents recreated if `tailscale` was recreated by a config change.
- **AdGuard config edits:** Always `docker compose stop adguardhome` before editing `data/adguard/conf/AdGuardHome.yaml` (AdGuard overwrites it on exit).
- **Concise docs & comments:** Keep docs and inline comments brief and high-signal — record only key points, invariants, and non-obvious "why"s; avoid verbose prose, obvious comments, or duplicating `README.md`.
