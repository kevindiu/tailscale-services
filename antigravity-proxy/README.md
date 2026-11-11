# Antigravity Universal Proxy (Cloud Code Assist API Bridge)

A production-grade, zero-dependency proxy server connecting AI clients and agents (Hermes, Claude Code, Continue, Zed, OpenAI/Anthropic SDKs) directly to Google Antigravity's internal Cloud Code Assist API.

---

## 1. Key Features

- **Multi-API Protocol Translation:**
  - **OpenAI Chat & Text Completions:** `/v1/chat/completions` & `/v1/completions` (streaming SSE & non-streaming, function/tool calling).
  - **Anthropic Messages API:** `/v1/messages` (native Claude format for tools like Claude Code, Zed, Continue).
  - **Native Gemini Passthrough:** `/v1beta/models/{model}:generateContent` & stream.
  - **Image Generations (DALL-E format):** `/v1/images/generations` powered by Gemini image models.
- **Zero External Dependencies:**
  - 100% written with Python standard library (`http.server`, `urllib.request`, `json`, `threading`).
  - Runs out-of-the-box in minimalist containers like `python:3.11-alpine` without running `pip install`.
- **Dynamic Model Discovery:**
  - Fetches the live catalog from Google Cloud Code Assist (`fetchAvailableModels`) on startup with cached live updates.
  - Zero hardcoded model catalogs — automatically adapts when Google introduces or deprecates models.
- **Intelligent Cooldown & Multi-hop Fallback:**
  - Automatically captures upstream `429 RESOURCE_EXHAUSTED` responses and parses exact quota reset timestamps.
  - Enters temporary model cooldown and seamlessly tries the next healthy model in the caller's fallback chain sequentially until success.
- **High-Performance In-Memory Token Caching:**
  - Tokens are parsed and validated in memory; zero disk I/O on 99% of requests while automatically refreshing 2 minutes prior to expiry.
- **Quota & Cooldown Observability:**
  - Direct live insight into Google Cloud Code Assist quota buckets (weekly limit, 5-hour limit, remaining fractions) via `GET /v1/usage`.
  - Live active cooldown monitoring and manual reset via `GET /health` and `POST /v1/cooldown/clear`.

---

## 2. API Endpoints

| Method | Endpoint | Description |
| :--- | :--- | :--- |
| `POST` | `/v1/chat/completions` | OpenAI-compatible Chat Completions (streaming & non-streaming, tool calling) |
| `POST` | `/v1/completions` | OpenAI-compatible Text Completions |
| `POST` | `/v1/messages` | Anthropic Messages API (Claude Code, Continue, Zed) |
| `POST` | `/v1/images/generations` | OpenAI DALL-E format Image Generation |
| `POST` | `/v1beta/models/{model}:generateContent` | Native Gemini API passthrough |
| `POST` | `/v1beta/models/{model}:streamGenerateContent` | Native Gemini API streaming passthrough |
| `GET` | `/v1/models` | OpenAI-compatible model catalog (dynamic live discovery) |
| `GET` | `/v1/models/{model}` | Model details and backend target mapping |
| `GET` | `/v1beta/models` | Gemini-compatible model catalog |
| `GET` | `/v1/usage` | Google Cloud Code Assist live quota buckets & active cooldowns |
| `GET` | `/health` | Service health status, project ID, default model, active cooldowns |
| `POST` | `/v1/cooldown/clear` | Clear all active model cooldowns immediately |
| `OPTIONS` | `/*` | CORS preflight handling for browser clients |

---

## 3. Quickstart & Deployment

### Running Inside the Tailscale-Services Stack (Default)
In this repository, the proxy runs as an isolated container connected to the `tailscale` network namespace:

```bash
# Launch stack
docker compose up -d

# Check proxy logs
docker compose logs -f antigravity-proxy

# Test healthcheck from within the network namespace (e.g. from Hermes)
docker compose exec hermes curl -s http://127.0.0.1:8877/health
```

### Running Standalone (Local Machine or Custom Container)
The proxy requires Python 3.10+ and a valid Antigravity OAuth token file:

```bash
# Set path to your OAuth token (defaults to ~/.gemini/antigravity-cli/antigravity-oauth-token)
export ANTIGRAVITY_TOKEN_FILE="$HOME/.gemini/antigravity-cli/antigravity-oauth-token"

# Start proxy on port 8877
python3 antigravity-proxy/antigravity_proxy.py --host 127.0.0.1 --port 8877
```

---

## 4. Usage & Examples

### 1. OpenAI Chat Completions (Non-Streaming)
```bash
curl -s -X POST http://127.0.0.1:8877/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini-3.8-flash",
    "messages": [
      {"role": "user", "content": "What is the capital of Japan?"}
    ]
  }'
```

### 2. OpenAI Chat Completions (Streaming)
```bash
curl -s -N -X POST http://127.0.0.1:8877/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini-3.8-flash",
    "messages": [
      {"role": "user", "content": "Write a 3-line haiku."}
    ],
    "stream": true
  }'
```

### 3. Automatic Multi-Hop Fallback Chain
Pass `X-Fallback-Chain` in headers (or `fallback_chain` in JSON body). If your primary model is in cooldown or hits upstream quota exhaustion, the proxy automatically attempts candidates sequentially:

```bash
curl -s -X POST http://127.0.0.1:8877/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "X-Fallback-Chain: claude-opus-5-5-low, gemini-4-argon-low, gemini-3.8-flash-medium" \
  -d '{
    "model": "claude-opus-5-5-low",
    "messages": [{"role": "user", "content": "Hello"}]
  }'
```
*The proxy inspects the response headers (`X-Served-Model` and `x-litellm-model-id`) to transparently report which model fulfilled the request.*

### 4. Anthropic Messages API (`/v1/messages`)
```bash
curl -s -X POST http://127.0.0.1:8877/v1/messages \
  -H "Content-Type: application/json" \
  -d '{
    "model": "claude-sonnet-5.5",
    "max_tokens": 1024,
    "messages": [
      {"role": "user", "content": "Explain quantum superposition in one sentence."}
    ]
  }'
```

### 5. Inspect Quota & Cooldowns
```bash
# View live quota summary and active cooldowns
curl -s http://127.0.0.1:8877/v1/usage

# Clear cooldowns manually
curl -s -X POST http://127.0.0.1:8877/v1/cooldown/clear
```

---

## 5. Hermes AI Agent Integration

Hermes connects to the Antigravity proxy via the custom model provider plugin located at `data/hermes/plugins/model-providers/antigravity/__init__.py`:

1. **Loopback Routing:** Communicates over local container loopback `http://127.0.0.1:8877/v1` via shared Tailscale container networking (`network_mode: "service:tailscale"`).
2. **Transparent Fallback Injection:** The Hermes Antigravity plugin dynamically reads Hermes' configured `fallback_providers` from `config.yaml` and injects the `X-Fallback-Chain` header into every outbound request.
3. **Served Model Attribution:** The plugin captures the proxy's `X-Served-Model` response header so that turn footers in Telegram, Discord, or CLI accurately report the actual model used.
