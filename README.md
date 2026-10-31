# Hardened Tailscale + AdGuard Home + RustDesk Server in Docker

A production-grade, hardened Docker Compose stack that runs **Tailscale**, **AdGuard Home**, and a **RustDesk Relay Server (`hbbs` + `hbbr`)** in a private, encrypted Tailscale network mesh.

---

## Architecture & Security Model

```
+--------------------------------------------------------------------------+
|                              Docker Host                                 |
|                                                                          |
|  +--------------------------------------------------------------------+  |
|  |                 Tailscale Container (Network Owner)                |  |
|  |       IP: 100.x.y.z | Hostname: adguard-tailscale                  |  |
|  |       Tags: tag:dns-server (Key expiry disabled)                   |  |
|  |       Capabilities: ALL dropped, only NET_ADMIN + NET_RAW          |  |
|  |       Memory Cap: 256MB                                            |  |
|  +----------------------------------+---------------------------------+  |
|                                     | (network_mode: service:tailscale)  |
|                    +----------------+----------------+                   |
|                    |                                 |                   |
|  +-----------------v-----------------+    +----------v----------------+  |
|  |           AdGuard Home            |    |      RustDesk Server      |  |
|  |  Port 53 (DNS - open to Tailnet)  |    |  hbbs: 21115, 21116       |  |
|  |  Port 3000 / 80 (Admin - ACL locked)   |  hbbr: 21117 (Relay)      |  |
|  |  Upstream: Encrypted DoH / DoT    |    |  Auth: Public Key (-k _)  |  |
|  |  DNSSEC: Enabled                  |    |  Memory Cap: 256MB each   |  |
|  |  Memory Cap: 512MB                |    |                           |  |
|  +-----------------------------------+    +---------------------------+  |
+--------------------------------------------------------------------------+
                                      ^
                                      | End-to-end WireGuard tunnel (Zero open WAN ports)
                                      v
                        Your Private Tailscale Network
               - All devices query DNS on port 53
               - Only admin devices can access web dashboard (port 80/3000)
```

### Security Invariants
- **No WAN / Open Resolver risk:** No ports are mapped to the host (`ports:` omitted). AdGuard Home is physically unreachable by the public internet or external port scanners.
- **Tailscale ACL isolation:** Devices on your Tailnet can send DNS queries to port `53`, but only designated admin devices can reach the AdGuard dashboard or RustDesk ports.
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

    // 3. Allow ONLY admin devices to access AdGuard dashboard & RustDesk
    {
      "action": "accept",
      "src": ["autogroup:admin"],
      "dst": [
        "tag:dns-server:80",
        "tag:dns-server:3000",
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
   TAILSCALE_HOSTNAME=adguard-tailscale
   TS_EXTRA_ARGS=--advertise-tags=tag:dns-server
   RUSTDESK_RELAY_HOST=adguard-tailscale
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

1. Find the assigned Tailscale IP in **[Tailscale Machines](https://login.tailscale.com/admin/machines)** (or `http://adguard-tailscale:3000`).
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

---

### Step 5: Route Tailnet DNS to AdGuard

1. Open **[Tailscale Admin → DNS](https://login.tailscale.com/admin/dns)**.
2. Under **Nameservers**, click **Add nameserver** → **Custom**.
3. Enter the Tailscale IPv4 (and IPv6) of your `adguard-tailscale` node.
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
   - **ID Server:** `<TAILSCALE_IP>` (or `adguard-tailscale`)
   - **Relay Server:** `<TAILSCALE_IP>:21117`
   - **Key:** Paste the public key string from `id_ed25519.pub`.
3. Click **Apply**. The bottom status indicator will turn green (**Ready**).

---

---

## Maintenance & Automatic Updates

### Automatic Updates (Watchtower)
**Watchtower** is included in the stack and configured to check Docker Hub daily at **04:00 AM UTC**:
- **Zero Downtime if No Update:** Watchtower queries Docker Hub's API. If no new image hash exists, nothing is restarted.
- **Dependency Aware:** `adguardhome`, `rustdesk-hbbs`, and `rustdesk-hbbr` are tagged with `com.centurylinklabs.watchtower.depends-on=tailscale`. If the Tailscale container updates, Watchtower automatically recreates the dependent containers so they re-attach to the new network namespace seamlessly.
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
docker compose logs -f watchtower
```
