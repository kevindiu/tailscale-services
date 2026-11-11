# AGENTS.md — Antigravity Universal Proxy

This document specifies the architecture, operational contracts, upstream protocols, and strict engineering invariants for AI coding agents (Pi, Claude Code, Cursor, Codex) working on `antigravity-proxy/antigravity_proxy.py`.

---

## 1. Architecture & Role

`antigravity_proxy.py` is an ultra-lightweight, zero-dependency Python bridge that runs as a dedicated container (`antigravity-proxy`) in the `tailscale-services` stack. It exposes OpenAI, Anthropic, Gemini, and Image generation endpoints to local agents (such as Hermes) and translates them into Google Cloud Code Assist internal RPCs.

```
+-----------------------------------------------------------------------------------------+
|                                    Docker Namespace                                     |
|                                                                                         |
|   +-----------------------+              HTTP Loopback               +---------------+  |
|   |  Hermes / AI Clients  |  ------------------------------------->  |  Antigravity  |  |
|   |  (or external agent)  |  <-------------------------------------  |  Proxy (8877) |  |
|   +-----------------------+      OpenAI / Anthropic / Gemini API     +-------+-------+  |
+------------------------------------------------------------------------------|----------+
                                                                               |
                                                       Google OAuth (Bearer)   | HTTPS / TLS
                                                       Internal CloudCode API  |
                                                                               v
                                                          Google Cloud Code Assist Backend
                                                          daily-cloudcode-pa.googleapis.com
```

---

## 2. Strict Engineering Invariants (DO NOT VIOLATE)

### Invariant 1: Zero External Dependencies
- **Strict Rule:** `antigravity_proxy.py` must **never** import third-party packages (e.g. `urllib3`, `requests`, `aiohttp`, `fastapi`, `pydantic`).
- **Reason:** The Docker container runs `python:3.11-alpine` directly with `user: "10000:10000"`, `cap_drop: ALL`, and no `pip install` step. Introducing external packages will immediately cause `ModuleNotFoundError` on container startup.
- **Allowed Libraries:** Standard library only (`http.server`, `urllib.request`, `urllib.parse`, `urllib.error`, `json`, `os`, `re`, `time`, `datetime`, `threading`, `uuid`, `base64`, `argparse`).

### Invariant 2: Zero Hardcoded Model Catalogs
- **Strict Rule:** Never hardcode static model candidate lists (e.g. `["gemini-3.8-flash", "claude-sonnet-5.5", ...]`) in model listing or fallback logic.
- **Implementation:**
  - Catalog must always be dynamically discovered from `fetchAvailableModels` via `get_available_models()`.
  - Dot-formatted model aliases (e.g. `claude-sonnet-5.5` for `claude-sonnet-5-5`) must be generated dynamically via regex (`re.sub(r"-(\d+)-(\d+)", r"-\1.\2", m)`).
  - Only clean shorthand aliases (`sonnet` -> `claude-sonnet-5-5`, `opus` -> `claude-opus-5-5`, `flash` -> `gemini-3.8-flash`, `pro` -> `gemini-2.5-pro`, `image` -> image-gen) are permitted in `resolve_backend_model`.

### Invariant 3: Strict Sequential Multi-Hop Fallback
- **Strict Rule:** Fallback routing must strictly follow the caller's provided chain (`X-Fallback-Chain` or `fallback_chain`).
- **Behavior:**
  - If the requested model is in cooldown, the proxy selects the first non-cooldown candidate from `fallback_chain`.
  - If upstream returns HTTP 429 (`RESOURCE_EXHAUSTED`), the proxy records the exhausted model into cooldown and loops through remaining healthy candidates sequentially (`while True`) until success or chain exhaustion.
  - If **no** fallback chain is supplied by the caller, the proxy must **never** guess or substitute arbitrary fallback models. It must propagate the upstream 429 error directly to the caller.

### Invariant 4: Cooldown State Machine & Upstream Parsing
- **Strict Rule:** Upstream 429 responses must parse exact reset timestamps to avoid guessing.
- **Parsing Priority:**
  1. ISO timestamp: `quotaResetTimeStamp` (case-insensitive regex).
  2. Float seconds: `retryDelay: 12345.6s` (case-insensitive regex).
  3. Natural language: `resets in Xh Ym Zs` (case-insensitive regex).
  4. Fallback: 3600 seconds (1 hour) only if no reset info is parseable.
- Cooldown keys are mapped to their canonical backend model representation and automatically expire when `time.time() >= reset_epoch`.

### Invariant 5: In-Memory Token Caching (Zero Disk I/O Hot Path)
- **Strict Rule:** Do not open, read, or parse `TOKEN_FILE` from disk on every HTTP request.
- **Implementation:**
  - `get_access_token()` checks `_cached_access_token` and `_cached_token_expiry_ts` in memory.
  - While `now < _cached_token_expiry_ts - TOKEN_REFRESH_SKEW` (skew = 120s), return the cached token immediately without acquiring locks or reading disk.
  - Only read disk / refresh via OAuth if the cached token is absent or within 2 minutes of expiration.

### Invariant 6: Served Model Attribution Headers
- **Strict Rule:** The proxy must always emit response headers indicating the actual model served:
  - `X-Served-Model: <model_name>`
  - `x-litellm-model-id: <model_name>`
- This ensures upstream clients (e.g. Hermes Agent turn footers) know precisely which model answered if a fallback occurred.

---

## 3. Upstream Google Protocol Reference

Antigravity proxies to Google Cloud Code Assist internal API:

- **Host Base:** `https://daily-cloudcode-pa.googleapis.com`
- **Required Upstream Headers:**
  - `Authorization: Bearer <access_token>`
  - `User-Agent: antigravity/cli/1.4.0 (aidev_client; os_type=linux; arch=amd64; cl=982146307; auth_method=consumer)`
  - `Client-Metadata: {"ideType":"ANTIGRAVITY","platform":"MACOS","pluginType":"GEMINI"}`
  - `X-Goog-Api-Client: google-cloud-sdk vscode_cloudshelleditor/0.1`
  - `Content-Type: application/json`
- **Key Internal RPC Endpoints:**
  - `POST /v1internal:loadCodeAssist` — Project ID discovery (`cloudaicompanionProject`)
  - `POST /v1internal:fetchAvailableModels` — Live model catalog discovery
  - `POST /v1internal:generateContent` — Non-streaming completion
  - `POST /v1internal:streamGenerateContent?alt=sse` — Streaming SSE completion
  - `POST /v1internal:retrieveUserQuotaSummary` — Account quota buckets and remaining fractions

---

## 4. Verification Workflow for Changes

After making changes to `antigravity-proxy/antigravity_proxy.py`:

```bash
# 1. Syntax check
python3 -m py_compile antigravity-proxy/antigravity_proxy.py

# 2. Recreate proxy container
docker compose up -d --force-recreate antigravity-proxy

# 3. Verify healthcheck and active cooldowns
docker compose exec hermes curl -s http://127.0.0.1:8877/health

# 4. Verify model listing
docker compose exec hermes curl -s http://127.0.0.1:8877/v1/models

# 5. Verify OpenAI Chat Completions
docker compose exec hermes curl -s -X POST http://127.0.0.1:8877/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"messages": [{"role": "user", "content": "ping"}]}'

# 6. Verify Anthropic Messages
docker compose exec hermes curl -s -X POST http://127.0.0.1:8877/v1/messages \
  -H "Content-Type: application/json" \
  -d '{"messages": [{"role": "user", "content": "ping"}]}'

# 7. Check container logs for errors
docker compose logs antigravity-proxy --tail 50
```
