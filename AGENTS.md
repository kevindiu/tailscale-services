# AGENTS.md — tailscale-services

This file documents the architecture, operational workflows, and security invariants of the `tailscale-services` repository for AI coding agents (Hermes Agent, Pi, Claude Code, Cursor, Codex).

---

## 1. Project Overview

`tailscale-services` is a hardened, production-grade Docker Compose stack that runs multiple private infrastructure services inside a private **Tailscale** WireGuard mesh:

1. **`tailscale`**: The network namespace owner. Establishes the encrypted WireGuard tunnel, registers the MagicDNS hostname (`tailscale-services`), and acts as a network exit node.
2. **`adguardhome`**: High-performance, ad-blocking recursive DNS server. Resolves upstream DNS via encrypted DoH/DoT (Quad9, Cloudflare) with DNSSEC validation.
3. **`rustdesk-hbbs` & `rustdesk-hbbr`**: Self-hosted RustDesk rendezvous server and relay server with public key authentication (`-k _`).
4. **`hermes`**: Autonomous AI agent by Nous Research (`nousresearch/hermes-agent`). Supports messaging platforms (Telegram, Discord, Slack) and an OpenAI-compatible API server on port 8642.

There is no updater container. Images are refreshed by `scripts/update-stack.sh` from the host (see §5).

---

## 2. Architecture & Networking

```
+-------------------------------------------------------------------------------------------+
|                                         Docker Host                                       |
|                                                                                           |
|  +-------------------------------------------------------------------------------------+  |
|  |                          Tailscale Container (Network Owner)                         |  |
|  |    Hostname: tailscale-services | Tagless (Key Expiry disabled via admin console)    |  |
|  |    Capabilities: ALL dropped, only NET_ADMIN + NET_RAW added                        |  |
|  |    Exit Node: Enabled by default (--advertise-exit-node)                            |  |
|  +-------------------------------------------+-----------------------------------------+  |
|                                              | network_mode: "service:tailscale"          |
|             +------------------+-------------+------------------+                        |
|             |                  |                                |                        |
|  +----------v--------+  +------v-------------+  +---------------v---------+             |
|  |   AdGuard Home    |  |  RustDesk Server   |  |      Hermes Agent       |             |
|  |  Port 53 (DNS)    |  |  hbbs: 21115-21116 |  |  Port 9119 (Web UI)     |             |
|  |  Port 3000 / 80   |  |  hbbr: 21117       |  |  Telegram Gateway       |             |
|  |  (Web Dashboard)  |  |  Auth: Public Key  |  |  API Server: 8642       |             |
|  +-------------------+  +--------------------+  +-------------------------+             |
+-------------------------------------------------------------------------------------------+
                                                  ^
                                                  | Private WireGuard Mesh
                                                  v
                                     Your Private Tailnet Devices
                                - All devices resolve DNS on port 53
                                - Admin devices reach web UIs and remote access
```

---

## 3. Strict Security Invariants (DO NOT VIOLATE)

1. **Zero Open WAN / Host Ports:**
   - **Never** add host port mappings (`ports:`) to `docker-compose.yml`.
   - **Single exception:** `41641:41641/udp` on `tailscale`, which pins the WireGuard endpoint and prevents iOS/mobile reconnection drops. Never remove or widen it.
   - All other inbound traffic must travel exclusively through the encrypted Tailscale network namespace.
2. **Network Sharing:**
   - Dependent services (`adguardhome`, `rustdesk-hbbs`, `rustdesk-hbbr`, `hermes`) must retain `network_mode: "service:tailscale"`.
3. **Docker Socket Isolation:**
   - No container may mount `/var/run/docker.sock`.
   - Updates run from the host via `scripts/update-stack.sh`; never add a container that needs daemon access.
4. **Zero Host Mac Processes:**
   - Hermes runs 100% inside Docker via the `hermes` container.
   - Do not instruct the user to run host-level Python scripts or use `host.docker.internal`.
5. **Least Privilege & Resource Limits:**
   - Tailscale runs with `cap_drop: ALL` and only `NET_ADMIN` + `NET_RAW`.
   - All containers have explicit memory caps in `docker-compose.yml` to prevent host exhaustion.
6. **Updates Must Go Through Compose:**
   - Every dependent service joins `tailscale`'s network namespace by container ID. Recreating `tailscale` outside Compose (Watchtower, `docker rm`, a bare `docker run`) leaves the dependents on a dead namespace.
   - Update with `scripts/update-stack.sh` only: it pulls, then recreates through `docker compose up`, which rebuilds `tailscale` first and the dependents after it.

---

## 4. Key Paths & Configurations

```text
tailscale-services/
├── docker-compose.yml              # Complete stack definition (fixed project name: tailscale-services)
├── env.example                     # Environment template (Tailscale auth, exit node, model keys)
├── .env                            # Active environment file (ignored by git)
├── AdGuardHome.template.yaml       # Pre-configured upstream resolvers, DNSSEC & blocklists
├── AGENTS.md                       # This file (AI developer instructions & invariants)
├── README.md                       # Human documentation
├── scripts/
│   ├── init-volumes.sh             # Sets up ./data directories with strict 700 permissions
│   └── update-stack.sh             # Pulls new images and recreates the stack via Compose
└── data/                           # Persistent state directory (ignored by git)
    ├── adguard/                    # AdGuard configuration and query logs
    │   ├── conf/AdGuardHome.yaml
    │   └── work/
    ├── rustdesk/                   # Server private/public keys and sqlite DB
    │   ├── id_ed25519
    │   └── id_ed25519.pub
    ├── tailscale/                  # Tailscaled state file
    │   └── tailscaled.state
    └── hermes/                     # Hermes persistent state, skills, memories, configs
        ├── config.yaml
        ├── .env
        ├── auth.json
        ├── SOUL.md
        └── state.db
```

---

## 5. Common Operational Workflows

### Starting and Stopping the Stack
```bash
# Launch entire stack in detached mode
docker compose up -d

# Stop the entire stack cleanly
docker compose down

# Recreate a specific container after configuration changes
docker compose up -d --force-recreate hermes
```

### Updating Images (Manual)
`scripts/update-stack.sh` is the only supported updater. It pulls, recreates through Compose (dependency order), waits for health, and prunes dangling images. There is no scheduler and no updater container; run it by hand.

### Checking Service Health & Logs
```bash
# Check status of all containers
docker compose ps

# Tail logs for all or specific services
docker compose logs -f
docker compose logs -f tailscale
docker compose logs -f adguardhome
docker compose logs -f hermes
```

### Restoring Pre-Configured AdGuard Home
If `data/adguard` is wiped or reset:
```bash
mkdir -p data/adguard/conf
cp AdGuardHome.template.yaml data/adguard/conf/AdGuardHome.yaml
docker compose restart adguardhome
```

### Checking RustDesk Server Public Key
Clients need the public encryption key to connect:
```bash
cat data/rustdesk/id_ed25519.pub
```

### Hermes Agent Operations
```bash
# Interactive CLI chat session
docker compose exec -it hermes hermes chat

# Run configuration wizard
docker compose exec -it hermes hermes setup

# Run diagnostic health check
docker compose exec hermes hermes doctor

# Switch default model (list available models first with: hermes model)
docker compose exec -it hermes hermes config set model.default <provider>/<model>
```

---

## 6. Guidelines for AI Assistants

- **Editing `docker-compose.yml`:**
  - Always keep `name: tailscale-services` at the root.
  - Every service must attach the `x-logging: &default-logging` anchor (`<<: *default-logging`) to prevent unbounded disk usage.
  - Sub-services must keep `depends_on: tailscale: { condition: service_healthy }`. Do not add shell-based healthchecks to `rustdesk` as its minimalist container image lacks utilities like `pgrep` or `curl`.
  - Ensure memory limits and restart policies are preserved when adding services.
  - Never add host-bound `ports:` mappings. All services must be routed strictly through `network_mode: "service:tailscale"`.
  - The only permitted mapping is `41641:41641/udp` on `tailscale` (fixed WireGuard port; removing it reintroduces iOS reconnection drops).
- **Healthchecks Must Be Real:** Never add catch-all fallbacks such as `|| test -e /proc/net/tcp` — they report healthy while the service is broken. Probe the actual service (`nc -z 127.0.0.1 53` for AdGuard, `GET /api/health` for Hermes).
- **Tailscale DNS Isolation:** Always keep `TS_ACCEPT_DNS=false` on the `tailscale` container to prevent DNS loop deadlocks with AdGuard Home.
- **Handling Secrets:** Never commit `.env` or files under `data/` to git. Never insert real API keys or OAuth secrets into `env.example` or compose fallbacks.
