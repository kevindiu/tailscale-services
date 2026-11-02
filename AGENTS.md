# AGENTS.md — tailscale-services

This file documents the architecture, operational workflows, and security invariants of the `tailscale-services` repository for AI coding agents (Hermes Agent, Pi, Claude Code, Cursor, Codex).

---

## 1. Project Overview

`tailscale-services` is a hardened, production-grade Docker Compose stack that runs multiple private infrastructure services inside a private **Tailscale** WireGuard mesh:

1. **`tailscale`**: The network namespace owner. Establishes the encrypted WireGuard tunnel, registers the MagicDNS hostname (`tailscale-services`), and acts as a network exit node.
2. **`adguardhome`**: High-performance, ad-blocking recursive DNS server. Resolves upstream DNS via encrypted DoH/DoT (Quad9, Cloudflare) with DNSSEC validation.
3. **`rustdesk-hbbs` & `rustdesk-hbbr`**: Self-hosted RustDesk rendezvous server and relay server with public key authentication (`-k _`).
4. **`hermes`**: Autonomous AI agent by Nous Research (`nousresearch/hermes-agent`). Supports messaging platforms (Telegram, Discord, Slack) and an OpenAI-compatible API server on port 8642.
5. **`antigravity-proxy`**: Ultra-lightweight containerized Python bridge (`python:3.11-alpine`) connecting Hermes directly to Google Cloud Code Assist / Antigravity models (Gemini 3.8 Flash, Claude Sonnet 5.5, Claude Opus 5.5).
6. **`watchtower`**: Daily automated updater (checks at 04:00 AM UTC) to pull newer Docker images without downtime.

---

## 2. Architecture & Networking

```
+---------------------------------------------------------------------------------------------------+
|                                            Docker Host                                            |
|                                                                                                   |
|  +---------------------------------------------------------------------------------------------+  |
|  |                             Tailscale Container (Network Owner)                             |  |
|  |    Hostname: tailscale-services | Tagless (Key Expiry disabled via admin console)           |  |
|  |    Capabilities: ALL dropped, only NET_ADMIN + NET_RAW added                                |  |
|  |    Exit Node: Enabled by default (--advertise-exit-node)                                    |  |
|  +----------------------------------------------+----------------------------------------------+  |
|                                                 | network_mode: "service:tailscale"               |
|            +------------------+-----------------+------------------+-----------------+            |
|            |                  |                                    |                 |            |
|  +---------v---------+  +-----v--------------+  +------------------v--+  +-----------v---------+  |
|  |   AdGuard Home    |  |  RustDesk Server   |  |   Hermes Agent      |  |  Antigravity Bridge |  |
|  |  Port 53 (DNS)    |  |  hbbs: 21115-21116 |  |  Port 9119 (Web UI) |  |  Port 8877 (Loopback)|  |
|  |  Port 3000 / 80   |  |  hbbr: 21117       |  |  Telegram Gateway   |  |  Google OAuth Token |  |
|  |  (Web Dashboard)  |  |  Auth: Public Key  |  |  Plugin: antigravity|  |  No Mac processes   |  |
|  +-------------------+  +--------------------+  +---------------------+  +---------------------+  |
+---------------------------------------------------------------------------------------------------+
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
   - All inbound traffic must travel exclusively through the encrypted Tailscale network namespace.
2. **Network Sharing:**
   - Dependent services (`adguardhome`, `rustdesk-hbbs`, `rustdesk-hbbr`, `hermes`, `antigravity-proxy`) must retain `network_mode: "service:tailscale"`.
3. **Docker Socket Isolation:**
   - `watchtower` is the **only** container permitted to mount `/var/run/docker.sock`.
   - Never mount `/var/run/docker.sock` to `hermes` or `antigravity-proxy`.
4. **Zero Host Mac Processes:**
   - The Antigravity bridge runs 100% inside Docker via the `antigravity-proxy` container.
   - Hermes communicates with the bridge over local container loopback (`http://127.0.0.1:8877/v1`).
   - Do not instruct the user to run host-level Python scripts or use `host.docker.internal`.
5. **Least Privilege & Resource Limits:**
   - Tailscale runs with `cap_drop: ALL` and only `NET_ADMIN` + `NET_RAW`.
   - All containers have explicit memory caps in `docker-compose.yml` to prevent host exhaustion.

---

## 4. Key Paths & Configurations

```text
tailscale-services/
├── docker-compose.yml              # Complete stack definition (fixed project name: tailscale-services)
├── env.example                     # Environment template (Tailscale auth, exit node, model keys)
├── .env                            # Active environment file (ignored by git)
├── AdGuardHome.template.yaml       # Pre-configured upstream resolvers, DNSSEC & blocklists
├── antigravity_proxy.py            # Containerized OpenAI-to-CloudCode SSE bridge script
├── AGENTS.md                       # This file (AI developer instructions)
├── README.md                       # Human documentation
└── data/                           # Persistent state directory (ignored by git)
    ├── adguard/                    # AdGuard configuration and query logs
    │   ├── conf/AdGuardHome.yaml
    │   └── work/
    ├── rustdesk/                   # Server private/public keys and sqlite DB
    │   ├── id_ed25519
    │   └── id_ed25519.pub
    ├── tailscale/                  # Tailscaled state file
    │   └── tailscaled.state
    ├── antigravity/                # Google OAuth token for Cloud Code Assist API
    │   └── antigravity-oauth-token
    └── hermes/                     # Hermes persistent state, skills, memories, configs
        ├── config.yaml
        ├── .env
        ├── auth.json
        ├── SOUL.md
        ├── state.db
        └── plugins/model-providers/antigravity/
            ├── plugin.yaml
            └── __init__.py
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

### Checking Service Health & Logs
```bash
# Check status of all containers
docker compose ps

# Tail logs for all or specific services
docker compose logs -f
docker compose logs -f tailscale
docker compose logs -f adguardhome
docker compose logs -f hermes
docker compose logs -f antigravity-proxy
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

# Switch default model (gemini-3.8-flash, claude-sonnet-5.5, claude-opus-5.5)
docker compose exec -it hermes hermes config set model.default claude-sonnet-5.5
```

---

## 6. Guidelines for AI Assistants

- **Editing `docker-compose.yml`:** Always keep `name: tailscale-services` at the root. Ensure memory limits, restart policies, and Watchtower labels (`com.centurylinklabs.watchtower.depends-on=tailscale`) are preserved when adding services.
- **Modifying Antigravity Models:** When adding new models, update both `MODEL_MAP` in `antigravity_proxy.py` and `fallback_models` in `data/hermes/plugins/model-providers/antigravity/__init__.py`.
- **Handling Secrets:** Never commit `.env` or files under `data/` to git.
