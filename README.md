# Hardened Tailscale + AdGuard Home + RustDesk Server + Hermes AI Agent in Docker

A production-grade, hardened Docker Compose stack that runs **Tailscale**, **AdGuard Home**, a **RustDesk Relay Server (`hbbs` + `hbbr`)**, and an isolated **Hermes AI Agent** in a private, encrypted Tailscale network mesh.

---

## Architecture & Security Model

```
+-----------------------------------------------------------------------------------------+
|                                       Docker Host                                       |
|                                                                                         |
|  +-----------------------------------------------------------------------------------+  |
|  |                        Tailscale Container (Network Owner)                        |  |
|  |       IP: 100.x.y.z | Hostname: tailscale-services                                 |  |
|  |       Tags: tag:dns-server (Key expiry disabled)                                  |  |
|  |       Capabilities: ALL dropped, only NET_ADMIN + NET_RAW                         |  |
|  |       Memory Cap: 256MB                                                           |  |
|  +-----------------------------------------+-----------------------------------------+  |
|                                            | (network_mode: service:tailscale)          |
|                    +-----------------------+-----------------------+                    |
|                    |                       |                       |                    |
|  +-----------------v-----------------+  +--v--------------------+  +--v--------------+  |
|  |           AdGuard Home            |  |    RustDesk Server    |  | Hermes AI Agent |  |
|  |  Port 53 (DNS - open to Tailnet)  |  |  hbbs: 21115, 21116   |  | Port 8642 (API) |  |
|  |  Port 3000 / 80 (Admin ACL)       |  |  hbbr: 21117 (Relay)  |  | Strict Sandbox  |  |
|  |  Upstream: Encrypted DoH / DoT    |  |  Auth: Public Key     |  | Named Volume    |  |
|  |  DNSSEC: Enabled                  |  |  Memory Cap: 256MB    |  | Memory Cap: 2GB |  |
|  |  Memory Cap: 512MB                |  |                       |  |                 |  |
|  +-----------------------------------+  +-----------------------+  +-----------------+  |
+-----------------------------------------------------------------------------------------+
                                             ^
                                             | End-to-end WireGuard tunnel (Zero open WAN ports)
                                             v
                               Your Private Tailscale Network
                      - All devices query DNS on port 53
                      - Only admin devices can access web dashboard (80/3000),
                        RustDesk (21115-21119), and Hermes Agent (8642)
```

### Security Invariants
- **No WAN / Open Resolver risk:** No ports are mapped to the host (`ports:` omitted). AdGuard Home is physically unreachable by the public internet or external port scanners.
- **Tailscale ACL isolation:** Devices on your Tailnet can send DNS queries to port `53`, but only designated admin devices can reach the AdGuard dashboard, RustDesk ports, or Hermes Agent API.
- **Strict Host Filesystem Isolation (Hermes AI Agent):**
  - **Zero Host Bind Mounts:** Hermes stores state (memories, sessions, skills, SQLite DBs) in an isolated Docker named volume (`hermes_data`). On macOS, this volume lives exclusively inside the Docker VM virtual disk, preventing the agent from seeing, reading, or modifying `/Users/...` or any files on your Mac.
  - **Zero Docker Socket Access:** `/var/run/docker.sock` is NOT mounted into Hermes, strictly preventing container escapes or host filesystem mounts.
  - **Least Privilege:** Runs with `no-new-privileges:true`, unprivileged UID/GID (10000), and memory limits enforced.
- **No key expiration outages:** Tagged nodes (`tag:dns-server`) do not expire after 180 days, preventing sudden DNS blackout.
- **DNS privacy against ISP:** AdGuard resolves upstream queries using DNS-over-HTTPS (DoH) or DNS-over-TLS (DoT) with DNSSEC validation enabled.
- **Principle of Least Privilege:** `cap_drop: ALL` with only `NET_ADMIN` and `NET_RAW` granted to Tailscale for tunnel operation. Memory limits enforced on all containers.

---

## Step-by-Step Setup

### Step 1: Configure Tailscale ACLs & Tags

Open your **[Tailscale Admin Console → Access Controls (ACLs)](https://login.tailscale.com/admin/acls)** and add:

```json
{
  // 1. Declare the tag owner (only admins can assign this tag)
  "tagOwners": {
    "tag:dns-server": ["autogroup:admin"]
  },

  "acls": [
    // 2. Allow ALL devices on your Tailnet to query DNS
    {
      "action": "accept",
      "src": ["*"],
      "proto": ["tcp", "udp"],
      "dst": ["tag:dns-server:53"]
    },

    // 3. Allow ONLY admin devices to access AdGuard dashboard, RustDesk & Hermes
    {
      "action": "accept",
      "src": ["autogroup:admin"],
      "dst": [
        "tag:dns-server:80",
        "tag:dns-server:3000",
        "tag:dns-server:8642",
        "tag:dns-server:21115-21119"
      ]
    }
  ]
}
```

---

### Step 2: Generate Tagged Auth Key

1. Go to **[Tailscale Admin → Settings → Keys](https://login.tailscale.com/admin/settings/keys)**.
2. Click **Generate auth key...**:
   - Check **Reusable**.
   - Check **Pre-approved**.
   - Select Tag: **`tag:dns-server`** (ensures key never expires).
3. Copy the generated key (`tskey-auth-...`).

---

### Step 3: Configure Environment & Launch Stack

1. Copy `env.example` to `.env`:
   ```bash
   cp env.example .env
   ```
2. Edit `.env` and paste your key into `TS_AUTHKEY`:
   ```env
   TS_AUTHKEY=tskey-auth-kXXXXXX-XXXXXXXXXXXXXXXX
   TAILSCALE_HOSTNAME=tailscale-services
   TS_EXTRA_ARGS=--advertise-tags=tag:dns-server
   RUSTDESK_RELAY_HOST=tailscale-services
   ```
3. Start the containers:
   ```bash
   docker compose up -d
   ```
4. Verify all services are running:
   ```bash
   docker compose ps
   docker compose logs -f
   ```

---

### Step 4: AdGuard Home Hardening

1. Find the assigned Tailscale IP in **[Tailscale Machines](https://login.tailscale.com/admin/machines)** (or `http://tailscale-services:3000`).
2. From an **admin device**, open `http://<TAILSCALE_IP>:3000`.
3. Complete the setup wizard (set admin username and a strong 16+ character password).
4. **Configure Encrypted Upstream DNS:**
   - In AdGuard, navigate to **Settings → DNS Settings**.
   - Under **Upstream DNS servers**, replace the defaults with encrypted resolvers:
     ```text
     # Quad9 (Malware blocking + DNSSEC)
     tls://dns.quad9.net
     https://dns.quad9.net/dns-query

     # Cloudflare (Fast privacy resolver)
     tls://1.1.1.1
     https://cloudflare-dns.com/dns-query
     ```
5. **Enable DNSSEC:**
   - Under **Settings → DNS Settings**, check **Enable DNSSEC**.
6. **Set Rate Limiting:**
   - Set **Rate limit** to `20` or `30` rps to mitigate client query flooding.
7. **Performance Tuning (Sub-Millisecond DNS & Fast Browsing):**
   - **Parallel Requests:** Under *Upstream DNS servers*, select **"Parallel requests"** (queries upstream servers concurrently, returning the fastest response).
   - **Optimistic Caching:** Under *DNS cache configuration*, check **"Optimistic caching"** (instantly serves cached records from RAM while refreshing in the background).
8. **Recommended Filter Lists:**
   Go to **Filters → DNS blocklists** → **Add blocklist** → **Add a custom list**:
   - **HaGeZi Multi PRO (Gold Standard):** `https://raw.githubusercontent.com/hagezi/dns-blocklists/main/adblock/pro.txt`
   - **OISD Big:** `https://big.oisd.nl`
9. **Disk Storage & Log Retention:**
   - Under **Settings → General Settings**, change **Query log retention** from `90 days` to **`7 days`** or **`14 days`** to keep storage lightweight.
10. **Friendly Client Names:**
    - Under **Settings → Client settings**, map Tailscale IPs to human names (e.g. `100.83.241.44` -> `MacBook`, `100.120.203.50` -> `iPhone`) so query logs show exact device names.

---

### Step 5: Route Tailnet DNS to AdGuard

1. Open **[Tailscale Admin → DNS](https://login.tailscale.com/admin/dns)**.
2. Under **Nameservers**, click **Add nameserver** → **Custom**.
3. Enter the Tailscale IPv4 (and IPv6) of your `tailscale-services` node.
4. Toggle **Override local DNS** to **ON**.
5. Keep **MagicDNS** **ON**.

*(Optional)* You can add a secondary public DoH fallback (e.g. `1.1.1.1`) underneath AdGuard so internet stays alive if the container is restarting for updates.

---

### Step 6: Configure RustDesk Remote Access

Because RustDesk is started with key enforcement (`-k _`), get the public key generated by your server:

```bash
cat data/rustdesk/id_ed25519.pub
```

On any RustDesk client connected to your Tailnet:
1. Open RustDesk → Click **Settings** (3 dots next to your ID) → **Network** → **ID/Relay Server**.
2. Set:
   - **ID Server:** `<TAILSCALE_IP>` (or `tailscale-services`)
   - **Relay Server:** `<TAILSCALE_IP>:21117`
   - **Key:** Paste the public key string from `id_ed25519.pub`.
3. Click **Apply**. The bottom status indicator will turn green (**Ready**).

---

### Step 7: Hermes AI Agent Usage & Host Isolation

**Hermes** is an autonomous AI agent by Nous Research capable of task execution, memory retention, and tool use.

#### Why Hermes cannot touch files on your Mac
1. **Docker Named Volume (`hermes_data`):** The persistent data mount `/opt/data` is backed by a named Docker volume created inside Docker's virtualized storage rather than a host path bind-mount (`./data` or `~/.hermes`). The agent has no path traversal, mount point, or visibility into `/Users/...` on macOS.
2. **No Docker Socket Mount:** The container does NOT mount `/var/run/docker.sock`. Hermes cannot command the Docker daemon to spawn privileged containers or mount host directories.
3. **Privilege Hardening:** Runs with `no-new-privileges:true` and non-root user mapping (`HERMES_UID=10000`), bounded by a 2GB memory ceiling.
4. **Tailscale Network Isolation:** Bound to `network_mode: "service:tailscale"`, meaning Hermes does not bind to your Mac's physical network adapters or open ports to your local LAN.

#### Initial Configuration (Setup Wizard)
To configure your LLM providers (OpenAI, Anthropic, OpenRouter, etc.) and model preferences:
```bash
docker compose exec -it hermes hermes setup
```
Credentials and configuration are saved securely into the isolated `hermes_data` volume.

Alternatively, provide API keys directly in `.env`:
```env
HERMES_OPENAI_API_KEY=your-openai-api-key
HERMES_ANTHROPIC_API_KEY=your-anthropic-api-key
HERMES_OPENROUTER_API_KEY=your-openrouter-api-key
```

#### Interactive Terminal Chat
To chat directly with Hermes inside its isolated container:
```bash
docker compose exec -it hermes hermes chat
```

#### Background Gateway & Integrations
Hermes continuously runs `gateway run` in the background. If you configure messaging connectors (Discord, Telegram, Slack) or the OpenAI-compatible API server via `hermes setup` or config files, the gateway handles them autonomously.

---

### Step 8: (Optional) Enable Exit Node (Full VPN for Public Wi-Fi / Abroad)

You can turn this same container into an exit node with zero extra containers:

1. In `.env`, append `--advertise-exit-node`:
   ```env
   TS_EXTRA_ARGS=--advertise-tags=tag:dns-server --advertise-exit-node
   ```
2. Recreate the stack:
   ```bash
   docker compose up -d
   ```
3. In **[Tailscale Admin → Machines](https://login.tailscale.com/admin/machines)**:
   - Click the three dots `...` next to `tailscale-services` → **Edit route settings...**
   - Check **"Use as exit node"** and click **Save**.
4. In your client Tailscale app (iPhone / iPad / Mac):
   - Tap **Exit Node** → select **`tailscale-services`** whenever connecting to public Wi-Fi or traveling abroad.

---

## Troubleshooting & Apple Device Tips

### Why is an Apple Device (iPhone / iPad / Mac) not blocking ads?
1. **iCloud Private Relay is Active:**
   - Apple proxies Safari DNS through its own servers, bypassing Tailscale and AdGuard.
   - Go to **Settings → [Your Name] → iCloud → Private Relay** and turn it **OFF**.
2. **Limit IP Address Tracking on Wi-Fi:**
   - In iOS/iPadOS: **Settings → Wi-Fi → tap `(i)` next to connected network**.
   - Toggle **"Limit IP Address Tracking"** to **OFF**.
3. **Tailscale "Override local DNS" is OFF:**
   - In [Tailscale Admin → DNS](https://login.tailscale.com/admin/dns), ensure **Override local DNS** is **ON**. If OFF, devices race local Wi-Fi router DNS in parallel with AdGuard.
4. **Mac Tailscale DNS Setting:**
   - Click the Tailscale icon in the macOS menu bar and verify **"Use Tailscale DNS Settings"** is checked.

---

---

## Maintenance & Automatic Updates

### Automatic Updates (Watchtower)
**Watchtower** is included in the stack and configured to check Docker Hub daily at **04:00 AM UTC**:
- **Zero Downtime if No Update:** Watchtower queries Docker Hub's API. If no new image hash exists, nothing is restarted.
- **Dependency Aware:** `adguardhome`, `rustdesk-hbbs`, `rustdesk-hbbr`, and `hermes` are tagged with `com.centurylinklabs.watchtower.depends-on=tailscale`. If the Tailscale container updates, Watchtower automatically recreates the dependent containers so they re-attach to the new network namespace seamlessly.
- **Auto Clean:** Automatically prunes old dangling image layers (`WATCHTOWER_CLEANUP=true`).

To view Watchtower status and next scheduled run:
```bash
docker compose logs -f watchtower
```

### Manual Updates (On-Demand)
If you prefer to trigger updates manually at any time:
```bash
docker compose pull
docker compose up -d
docker image prune -f
```

### Inspecting Logs
```bash
docker compose logs -f tailscale
docker compose logs -f adguardhome
docker compose logs -f rustdesk-hbbs
docker compose logs -f rustdesk-hbbr
docker compose logs -f hermes
docker compose logs -f watchtower
```

### Restoring Pre-Configured Settings
The repo includes **[`AdGuardHome.template.yaml`](AdGuardHome.template.yaml)** with all performance settings, encrypted DoH/DoT upstreams, and HaGeZi/OISD blocklists pre-configured (credentials removed).

To restore it to a fresh deployment:
```bash
mkdir -p data/adguard/conf
cp AdGuardHome.template.yaml data/adguard/conf/AdGuardHome.yaml
docker compose restart adguardhome
```
Open the web UI to set your admin username and password.
