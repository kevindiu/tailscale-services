# tailscale-services

Hardened Docker Compose stack running **Tailscale**, **AdGuard Home**, **RustDesk (`hbbs` + `hbbr`)**, and **Hermes AI Agent** inside a private Tailscale WireGuard mesh.

## Services

All app containers share `network_mode: "service:tailscale"` with zero host-exposed app ports (only `41641/udp` is published to pin WireGuard for mobile clients). Web UIs bind `127.0.0.1` only and are served over tailnet-only HTTPS via [`config/serve.json`](config/serve.json).

| Service | Purpose | Tailnet Ports | Limits |
| :--- | :--- | :--- | :--- |
| **`tailscale`** | WireGuard mesh, Exit Node, HTTPS Serve | `41641/udp` *(host)*, `3443`/`9443` (HTTPS) | 256M / 256 PIDs |
| **`adguardhome`** | Ad-blocking DNS (DoH/DoT/H3 + DNSSEC) | `53` (DNS), `3443` (HTTPS UI) | 512M / 256 PIDs |
| **`rustdesk-hbbs` / `hbbr`** | Private RustDesk ID & relay (`-k _`) | `21115–21119` | 256M / 256 PIDs each |
| **`hermes`** | Containerized Hermes AI Agent + UI | `9443` (HTTPS, Basic Auth) | 2G / 1024 PIDs |

## Quick Start

1. **Create a Tailscale auth key:** In [Tailscale Admin → Keys](https://login.tailscale.com/admin/settings/keys), generate a **Reusable**, **Pre-approved** key (no tags needed).
2. **Configure `.env` and start:**
   ```bash
   cp env.example .env && chmod 600 .env
   # Set TS_AUTHKEY and HERMES_DASHBOARD_PASSWORD in .env
   ./scripts/init-volumes.sh
   docker compose up -d
   ```
3. **Configure Tailscale Admin:**
   - **[Machines](https://login.tailscale.com/admin/machines):** Open `...` on `tailscale-services` → **Disable key expiry** and **Edit route settings** → check **Use as exit node**.
     *(With `TS_AUTH_ONCE=true`, if the node is deleted or expires, stop the stack, remove `data/tailscale/tailscaled.state`, and start again.)*
   - **[DNS](https://login.tailscale.com/admin/dns):** Add `tailscale-services`'s Tailscale IPv4 (and IPv6) as a custom nameserver, enable **Override local DNS**, **MagicDNS**, and **HTTPS Certificates**.
     *(Don't add a secondary public DNS like `1.1.1.1` — clients race multiple nameservers and bypass AdGuard.)*
   - Verify HTTPS routes: `docker compose exec tailscale tailscale serve status`

## Service Setup

### AdGuard Home (`https://tailscale-services.<tailnet>.ts.net:3443`)
Complete the wizard, or restore [`AdGuardHome.template.yaml`](AdGuardHome.template.yaml) (pre-tuned with Quad9/Cloudflare DoH+DoT+H3, DNSSEC, 16 MB optimistic cache, HaGeZi Multi PRO, HaGeZi TIF Mini, and OISD Big; default login `admin`/`admin`):
```bash
docker compose stop adguardhome
cp AdGuardHome.template.yaml data/adguard/conf/AdGuardHome.yaml
htpasswd -nBC 10 "" | sed 's/^://'   # replace default password hash in AdGuardHome.yaml
docker compose start adguardhome
```
> Always `docker compose stop adguardhome` before editing `data/adguard/conf/AdGuardHome.yaml` or AdGuard will overwrite it on exit.

### RustDesk
Get the server key with `cat data/rustdesk/id_ed25519.pub`, then set in your client (**Settings → Network → ID/Relay Server**):
- **ID Server:** `tailscale-services` (or `<TAILSCALE_IP>`)
- **Relay Server:** `<TAILSCALE_IP>:21117`
- **Key:** contents of `id_ed25519.pub`

### Hermes (`https://tailscale-services.<tailnet>.ts.net:9443`)
Sign in with `admin` and `HERMES_DASHBOARD_PASSWORD`.
```bash
docker compose exec -it hermes hermes setup   # LLM keys & channels
docker compose exec -it hermes hermes chat    # CLI chat
docker compose exec -it hermes hermes model   # List models
```
- `./data/hermes` is `700` owned by UID `10000` (`sudo` required from host).
- OpenAI-compatible API (`:8642`) is off by default; enable with `API_SERVER_ENABLED=true` and `API_SERVER_KEY` in `data/hermes/.env`.

## Maintenance & Troubleshooting

**Update images** (always via Compose so dependents re-attach to `tailscale`'s namespace):
```bash
docker compose pull
docker compose up -d --remove-orphans --wait --wait-timeout 300
```
If a config edit recreates `tailscale`, verify with `docker compose ps` that dependents also recreated.

**Apple devices not blocking ads:**
1. Turn off **iCloud Private Relay** (Settings → Apple Account → iCloud).
2. Turn off **Limit IP Address Tracking** on the active Wi-Fi network (iOS/iPadOS).
3. Confirm **Override local DNS** is ON (Tailscale Admin → DNS) and **Use Tailscale DNS Settings** is checked (macOS menu bar).
