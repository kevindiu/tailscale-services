# tailscale-services

A hardened Docker Compose stack running **Tailscale**, **AdGuard Home**, **RustDesk Server (`hbbs` + `hbbr`)**, and **Hermes AI Agent** inside a private Tailscale WireGuard mesh.

## Services

All application containers share Tailscale's network namespace (`network_mode: "service:tailscale"`). No application ports are exposed to the host or WAN.

| Service | Purpose | Ports (Tailnet) | Limits |
| :--- | :--- | :--- | :--- |
| **`tailscale`** | WireGuard mesh, MagicDNS (`tailscale-services`), Exit Node, HTTPS Serve | `41641/udp` *(host-mapped)*, `3443`/`9443` (HTTPS) | 256M / 256 PIDs |
| **`adguardhome`** | Ad-blocking DNS (DoH/DoT + DNSSEC) | `53` (DNS), `3000` (HTTP) / `3443` (HTTPS) | 512M / 256 PIDs |
| **`rustdesk-hbbs` / `hbbr`** | Self-hosted RustDesk ID & relay (`-k _`) | `21115–21119` | 256M / 256 PIDs each |
| **`hermes`** | Containerized Hermes AI Agent + Web UI | `9119` (HTTP) / `9443` (HTTPS, Basic Auth) | 2G / 1024 PIDs |

### Security Highlights
- **Zero open application ports:** Only `41641/udp` is published on the host to pin the WireGuard port and prevent iOS/mobile reconnection drops.
- **Least privilege:** Every container runs with `no-new-privileges:true`, `cap_drop: ALL`, minimal `cap_add`, memory/PID limits, and rotating logs.
- **Isolated runtime:** No container mounts `/var/run/docker.sock`. Hermes runs 100% in Docker under UID/GID `10000` with `./data/hermes` (`chmod 700`).

---

## Quick Start

### 1. Generate a Tailscale Auth Key
In **[Tailscale Admin → Keys](https://login.tailscale.com/admin/settings/keys)**, click **Generate auth key**, check **Reusable** and **Pre-approved** (no tags needed), and copy the key (`tskey-auth-...`).

### 2. Configure `.env` and Launch
```bash
cp env.example .env
chmod 600 .env
```
Edit `.env` and set at least `TS_AUTHKEY` and a strong `HERMES_DASHBOARD_PASSWORD` (the stack refuses to start without both), then launch:
```bash
./scripts/init-volumes.sh
docker compose up -d
docker compose ps
```

### 3. Configure Tailscale Admin
1. **Disable key expiry:** In **[Machines](https://login.tailscale.com/admin/machines)**, open `...` next to `tailscale-services` → **Disable key expiry**.
   > `TS_AUTH_ONCE=true` consumes `TS_AUTHKEY` only on first boot. If you remove the node or let the key expire, stop the stack, delete `data/tailscale/tailscaled.state`, and start again.
2. **Approve exit node:** In **[Machines](https://login.tailscale.com/admin/machines)**, open `...` → **Edit route settings** → check **Use as exit node**.
3. **Route Tailnet DNS:** In **[DNS](https://login.tailscale.com/admin/dns)**, add the Tailscale IPv4 (and IPv6) of `tailscale-services` as a custom nameserver, turn **Override local DNS** **ON**, and keep **MagicDNS** **ON**.
   > **Avoid adding a secondary public resolver (e.g. `1.1.1.1`):** OS stub resolvers race or round-robin multiple global nameservers, leaking queries past AdGuard.
4. **Enable HTTPS certificates:** In **[DNS](https://login.tailscale.com/admin/dns)**, enable **HTTPS Certificates**. Tailscale Serve ([`config/serve.json`](config/serve.json), `AllowFunnel: false`) automatically provisions Let's Encrypt certs for your `.ts.net` domain. Verify with:
   ```bash
   docker compose exec tailscale tailscale serve status
   ```

---

## Service Setup

### AdGuard Home (`https://tailscale-services.<tailnet>.ts.net:3443`)
- **Fresh setup:** Open `https://tailscale-services.<tailnet>.ts.net:3443` (or `http://tailscale-services:3000`) and finish the wizard. Recommended settings: Quad9/Cloudflare DoH & DoT upstreams, **Parallel requests**, **DNSSEC**, **Optimistic caching**, `20–30` rps rate limit, `7d` query log retention, and **HaGeZi Multi PRO** + **OISD Big** blocklists.
- **Or restore the pre-configured template** ([`AdGuardHome.template.yaml`](AdGuardHome.template.yaml), defaults to `admin` / `admin`):
  ```bash
  docker compose stop adguardhome
  mkdir -p data/adguard/conf
  cp AdGuardHome.template.yaml data/adguard/conf/AdGuardHome.yaml
  # Generate a bcrypt hash interactively and replace the default password in data/adguard/conf/AdGuardHome.yaml:
  htpasswd -nBC 10 "" | sed 's/^://'
  docker compose start adguardhome
  ```
  > Always `docker compose stop adguardhome` before editing `data/adguard/conf/AdGuardHome.yaml` so AdGuard does not overwrite your changes on exit.

### RustDesk
Print the generated server public key:
```bash
cat data/rustdesk/id_ed25519.pub
```
In your RustDesk client (**Settings → Network → ID/Relay Server**), set:
- **ID Server:** `tailscale-services` (or `<TAILSCALE_IP>`)
- **Relay Server:** `<TAILSCALE_IP>:21117`
- **Key:** contents of `data/rustdesk/id_ed25519.pub`

### Hermes AI Agent (`https://tailscale-services.<tailnet>.ts.net:9443`)
Sign in with `admin` and `HERMES_DASHBOARD_PASSWORD` (also reachable via `http://tailscale-services:9119`).

```bash
docker compose exec -it hermes hermes setup   # Configure LLM provider keys & channels
docker compose exec -it hermes hermes chat    # Interactive CLI chat
docker compose exec -it hermes hermes model   # List available models
docker compose exec -it hermes hermes config set model.default <provider>/<model>
```
- `./data/hermes` is `700` owned by UID `10000` (requires `sudo` to inspect from the host).
- The OpenAI-compatible API (`/v1` on port `8642`) is off by default; enable it by setting `API_SERVER_ENABLED=true` and a strong `API_SERVER_KEY` in `data/hermes/.env` (or under `environment:` in `docker-compose.yml`) and restarting `hermes`.

---

## Maintenance & Troubleshooting

### Updating Images
Updates are manual and must run through Compose (dependent containers bind to `tailscale`'s network namespace by container ID):
```bash
docker compose pull
docker compose up -d --remove-orphans --wait --wait-timeout 300
```
- If a config edit recreates `tailscale`, verify with `docker compose ps` that dependents were also recreated.
- Optionally run `docker image prune -f` afterwards. Images use `:latest`; pin `@sha256:...` digests if you need deterministic deploys.
- Tail logs anytime with `docker compose logs -f [service]`.

### Apple Devices Not Blocking Ads
1. Turn **OFF** **iCloud Private Relay** (Settings → Apple Account → iCloud).
2. Turn **OFF** **Limit IP Address Tracking** on the active Wi-Fi network (iOS/iPadOS).
3. Verify **Override local DNS** is **ON** in Tailscale Admin → DNS.
4. On macOS, check **Use Tailscale DNS Settings** in the Tailscale menu bar icon.
