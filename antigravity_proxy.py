#!/usr/bin/env python3
from __future__ import annotations
"""
Antigravity Universal Proxy (Cloud Code Assist API)

A production-grade, zero-dependency proxy server connecting AI clients
to Google Antigravity's internal Cloud Code Assist API.

Supported APIs & Endpoints:
  1. OpenAI Chat Completions:
     - POST /v1/chat/completions, POST /chat/completions (streaming & non-streaming, tool-calling)
  2. OpenAI Text Completions:
     - POST /v1/completions, POST /completions (streaming & non-streaming)
  3. Anthropic Messages API:
     - POST /v1/messages, POST /messages (streaming & non-streaming, Claude Code / Continue / Zed)
  4. OpenAI Image Generations:
     - POST /v1/images/generations, POST /images/generations (DALL-E format via gemini-3.1-flash-image)
  5. Native Gemini Passthrough:
     - POST /v1beta/models/{model}:generateContent & POST /v1/models/{model}:generateContent
     - POST /v1beta/models/{model}:streamGenerateContent & POST /v1/models/{model}:streamGenerateContent
  6. Model Discovery & Listing (Dynamic from Google — ZERO hardcoding):
     - GET /v1/models, GET /models (OpenAI format)
     - GET /v1/models/{model} (OpenAI format)
     - GET /v1beta/models (Gemini format)
  7. Quota & Usage:
     - GET /v1/usage, GET /usage (Live weekly limit & quota summary)
  8. Health & System:
     - GET /health, GET /
  9. CORS Preflight:
     - OPTIONS /* (full browser support)
"""

import argparse
import base64
import json
import os
import re
import signal
import sys
import threading
import time
import traceback
import uuid
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

# ──────────────────────────────────────────────────────────────────────────────
# Configuration & Constants
# ──────────────────────────────────────────────────────────────────────────────

CLIENT_ID = os.environ.get("ANTIGRAVITY_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("ANTIGRAVITY_CLIENT_SECRET", "")

TOKEN_FILE = os.environ.get("ANTIGRAVITY_TOKEN_FILE") or os.path.expanduser("~/.gemini/antigravity-cli/antigravity-oauth-token")
OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"
CLOUDCODE_BASE = "https://daily-cloudcode-pa.googleapis.com"
LOAD_CODEASSIST_URL = f"{CLOUDCODE_BASE}/v1internal:loadCodeAssist"
GENERATE_CONTENT_URL = f"{CLOUDCODE_BASE}/v1internal:generateContent"
STREAM_GENERATE_CONTENT_URL = f"{CLOUDCODE_BASE}/v1internal:streamGenerateContent?alt=sse"
USER_QUOTA_SUMMARY_URL = f"{CLOUDCODE_BASE}/v1internal:retrieveUserQuotaSummary"
FETCH_AVAILABLE_MODELS_URL = f"{CLOUDCODE_BASE}/v1internal:fetchAvailableModels"

ANTIGRAVITY_USER_AGENT = (
    "antigravity/cli/1.4.0 (aidev_client; os_type=linux; arch=amd64; cl=982146307; auth_method=consumer)"
)
CLIENT_METADATA = json.dumps(
    {"ideType": "ANTIGRAVITY", "platform": "MACOS", "pluginType": "GEMINI"},
    separators=(",", ":"),
)

DEFAULT_SYSTEM_INSTRUCTION = "You are a helpful AI assistant."
DEFAULT_MODEL = os.environ.get("ANTIGRAVITY_DEFAULT_MODEL", "")


def get_default_model() -> str:
    if DEFAULT_MODEL:
        return DEFAULT_MODEL
    live = get_available_models()
    if not live:
        return ""
    for pref in ("flash", "sonnet", "chat"):
        cand = next((m for m in live if pref in m.lower() and not m.startswith("tab_") and not m.startswith("chat_")), None)
        if cand:
            return cand
    return live[0]
UPSTREAM_TIMEOUT = 300  # 5 minutes
TOKEN_REFRESH_SKEW = 120  # refresh 2 mins before expiry
MAX_PAYLOAD_SIZE = 20 * 1024 * 1024  # 20MB

# Persistent Connection Pool for Upstream APIs
# (Using standard library urllib.request for zero-dependency portability)

# ──────────────────────────────────────────────────────────────────────────────
# Token & Project State
# ──────────────────────────────────────────────────────────────────────────────

_token_lock = threading.Lock()
_cached_access_token: str | None = None
_cached_token_expiry_ts: float = 0.0

_project_lock = threading.Lock()
_cached_project_id: str | None = None

_models_lock = threading.Lock()
_cached_models: list[str] = []
_cached_models_time: float = 0.0
MODELS_CACHE_TTL = 900.0  # 15 minutes


def _log(msg: str):
    print(f"[antigravity-proxy] {msg}", file=sys.stderr, flush=True)


def _parse_expiry(expiry_raw) -> float:
    if not expiry_raw:
        return 0.0
    if isinstance(expiry_raw, (int, float)):
        return float(expiry_raw)
    s = str(expiry_raw).strip()
    try:
        if "." in s:
            head, tail = s.split(".", 1)
            tz_part = ""
            for i, ch in enumerate(tail):
                if ch in "Z+-":
                    tz_part = tail[i:]
                    tail = tail[:i]
                    break
            tail = tail[:6]
            s = f"{head}.{tail}{tz_part}"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return 0.0


def _read_token_from_disk() -> dict | None:
    try:
        with open(TOKEN_FILE, "r") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        _log(f"WARNING: could not read token file {TOKEN_FILE}: {e}")
        return None


def _write_token_to_disk(data: dict):
    tmp = None
    try:
        token_dir = os.path.dirname(TOKEN_FILE)
        if token_dir:
            os.makedirs(token_dir, exist_ok=True)
        tmp = f"{TOKEN_FILE}.tmp.{os.getpid()}_{uuid.uuid4().hex[:8]}"
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        mode = 0o600
        fd = os.open(tmp, flags, mode)
        with open(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, TOKEN_FILE)
        try:
            os.chmod(TOKEN_FILE, 0o600)
        except OSError:
            pass
    except OSError as e:
        _log(f"WARNING: could not write token file: {e}")
        if tmp and os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def _refresh_access_token(refresh_token: str) -> dict:
    body = json.dumps({
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }).encode()
    req = Request(OAUTH_TOKEN_URL, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode())
    except HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode()
        except Exception:
            pass
        raise RuntimeError(f"OAuth refresh failed (HTTP {e.code}): {detail}")
    except URLError as e:
        raise RuntimeError(f"OAuth refresh network error: {e}")

    expires_in = int(payload.get("expires_in", 3600))
    exp_dt = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
    return {
        "access_token": payload["access_token"],
        "refresh_token": payload.get("refresh_token", refresh_token),
        "token_type": payload.get("token_type", "Bearer"),
        "expiry": exp_dt.strftime("%Y-%m-%dT%H:%M:%S.000000Z"),
    }


def get_access_token() -> str:
    global _cached_access_token, _cached_token_expiry_ts
    now = time.time()
    # Fast path: use valid in-memory cached token without lock or disk I/O
    if _cached_access_token and (now < _cached_token_expiry_ts - TOKEN_REFRESH_SKEW):
        return _cached_access_token

    with _token_lock:
        now = time.time()
        # Double check inside lock
        if _cached_access_token and (now < _cached_token_expiry_ts - TOKEN_REFRESH_SKEW):
            return _cached_access_token

        data = _read_token_from_disk()
        if not data:
            raise RuntimeError(f"No OAuth token found at {TOKEN_FILE}.")
        token_obj = data.get("token") or {}
        access_token = token_obj.get("access_token")
        expiry_ts = _parse_expiry(token_obj.get("expiry"))
        now = time.time()

        # If disk token is still valid (e.g. refreshed externally), use it
        if access_token and expiry_ts > 0.0 and (expiry_ts - now) >= TOKEN_REFRESH_SKEW:
            _cached_access_token = access_token
            _cached_token_expiry_ts = expiry_ts
            return access_token

        refresh_token = token_obj.get("refresh_token")
        if not refresh_token:
            raise RuntimeError("No refresh_token available; cannot refresh OAuth token.")
        _log("Access token expired (or missing) — refreshing via Google OAuth...")
        new_tok = _refresh_access_token(refresh_token)
        data["token"] = new_tok
        _write_token_to_disk(data)
        access_token = new_tok["access_token"]

        _cached_access_token = access_token
        _cached_token_expiry_ts = _parse_expiry(new_tok.get("expiry"))
        return access_token


def get_project_id() -> str:
    global _cached_project_id
    with _project_lock:
        if _cached_project_id:
            return _cached_project_id
        token = get_access_token()
        req = Request(LOAD_CODEASSIST_URL, data=b"{}", method="POST")
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("Content-Type", "application/json")
        req.add_header("User-Agent", ANTIGRAVITY_USER_AGENT)
        req.add_header("X-Goog-Api-Client", "google-cloud-sdk vscode_cloudshelleditor/0.1")
        req.add_header("Client-Metadata", CLIENT_METADATA)
        try:
            with urlopen(req, timeout=30) as resp:
                payload = json.loads(resp.read().decode())
                pid = payload.get("cloudaicompanionProject") or payload.get("cloudaicompanion_project")
                if not pid:
                    pid = "aicode-consumers"
                _cached_project_id = pid
                _log(f"Discovered Antigravity project ID: {pid}")
                return pid
        except Exception as e:
            _log(f"Project ID discovery fallback to aicode-consumers: {e}")
            _cached_project_id = "aicode-consumers"
            return "aicode-consumers"


# ──────────────────────────────────────────────────────────────────────────────
# Dynamic Model Discovery (ZERO HARDCODING)
# ──────────────────────────────────────────────────────────────────────────────

def get_available_models() -> list[str]:
    """Fetch live available models from Google Cloud Code Assist (cached)."""
    global _cached_models, _cached_models_time
    with _models_lock:
        now = time.time()
        if _cached_models and (now - _cached_models_time) < MODELS_CACHE_TTL:
            return _cached_models

        try:
            token = get_access_token()
            pid = get_project_id()
            req = Request(
                FETCH_AVAILABLE_MODELS_URL,
                data=json.dumps({"project": pid}).encode(),
                method="POST"
            )
            req.add_header("Authorization", f"Bearer {token}")
            req.add_header("Content-Type", "application/json")
            req.add_header("User-Agent", ANTIGRAVITY_USER_AGENT)
            req.add_header("X-Goog-Api-Client", "google-cloud-sdk vscode_cloudshelleditor/0.1")
            req.add_header("Client-Metadata", CLIENT_METADATA)
            with urlopen(req, timeout=15) as resp:
                payload = json.loads(resp.read().decode())
                raw = payload.get("models", [])
                if raw:
                    valid_models = [m for m in raw if isinstance(m, str) and m]
                    _cached_models = valid_models
                    _cached_models_time = now
                    _log(f"Discovered {len(valid_models)} live models from Google")
                    return valid_models
        except Exception as e:
            _log(f"Live model fetch error: {e}")
            if _cached_models:
                return _cached_models

        return _cached_models or []


# Model Cooldown & Fallback Chain Cache
# model_name -> expiration timestamp (monotonic or unix)
_MODEL_COOLDOWNS: dict[str, float] = {}
_MODEL_COOLDOWNS_LOCK = threading.Lock()


def record_model_cooldown(model: str, detail_or_message: str):
    """Record an upstream 429 quota exhaustion for a model and calculate its reset timestamp."""
    if not model:
        return

    # Resolve to canonical backend model so cooldown keys match
    try:
        canonical_model = resolve_backend_model(model)[0]
    except Exception:
        canonical_model = model

    reset_epoch = None
    # 1. Parse quotaResetTimeStamp: 2026-11-14T01:13:20Z (case-insensitive)
    m_ts = re.search(r"['\"]?quotaResetTimeStamp['\"]?\s*:\s*['\"]?([0-9T:\-Z\.]+)['\"]?", detail_or_message, re.IGNORECASE)
    if m_ts:
        try:
            ts_str = m_ts.group(1).rstrip("Z") + "+00:00"
            target_dt = datetime.fromisoformat(ts_str)
            reset_epoch = target_dt.timestamp()
        except Exception:
            pass

    # 2. Parse retryDelay: 310370.5s (case-insensitive)
    if reset_epoch is None:
        m_delay = re.search(r"['\"]?retryDelay['\"]?\s*:\s*['\"]?(\d+(?:\.\d+)?)s?['\"]?", detail_or_message, re.IGNORECASE)
        if m_delay:
            try:
                reset_epoch = time.time() + float(m_delay.group(1))
            except Exception:
                pass

    # 3. Parse natural language reset: "resets in 1h 2m 3s" / "reset in 5m" / etc.
    if reset_epoch is None:
        m_nl = re.search(
            r"resets?\s+in\s+(?:(\d+(?:\.\d+)?)\s*h)?\s*(?:(\d+(?:\.\d+)?)\s*m)?\s*(?:(\d+(?:\.\d+)?)\s*s)?",
            detail_or_message,
            re.IGNORECASE,
        )
        if m_nl and any(m_nl.groups()):
            hours = float(m_nl.group(1) or 0)
            minutes = float(m_nl.group(2) or 0)
            seconds = float(m_nl.group(3) or 0)
            total_seconds = hours * 3600 + minutes * 60 + seconds
            if total_seconds > 0:
                reset_epoch = time.time() + total_seconds

    # 4. Default fallback: 1 hour if not specified
    if reset_epoch is None:
        reset_epoch = time.time() + 3600

    with _MODEL_COOLDOWNS_LOCK:
        _MODEL_COOLDOWNS[canonical_model] = reset_epoch
        _log(f"[Cooldown] Recorded cooldown for '{canonical_model}' until {datetime.fromtimestamp(reset_epoch, tz=timezone.utc).isoformat()}")


def is_model_in_cooldown(model: str) -> bool:
    if not model:
        return False
    keys_to_check = [model]
    try:
        resolved = resolve_backend_model(model)[0]
        if resolved not in keys_to_check:
            keys_to_check.append(resolved)
    except Exception:
        pass

    now = time.time()
    with _MODEL_COOLDOWNS_LOCK:
        in_cooldown = False
        for key in keys_to_check:
            until = _MODEL_COOLDOWNS.get(key)
            if until is not None:
                if now >= until:
                    del _MODEL_COOLDOWNS[key]
                    _log(f"[Cooldown] Cooldown expired for '{key}', restored to service")
                else:
                    in_cooldown = True
        return in_cooldown


def get_active_cooldowns() -> dict[str, dict[str, Any]]:
    """Return dictionary of currently active model cooldowns with seconds remaining."""
    now = time.time()
    active = {}
    with _MODEL_COOLDOWNS_LOCK:
        expired = []
        for model, until in _MODEL_COOLDOWNS.items():
            rem = until - now
            if rem <= 0:
                expired.append(model)
            else:
                active[model] = {
                    "resets_in_seconds": round(rem, 1),
                    "reset_at": datetime.fromtimestamp(until, tz=timezone.utc).isoformat(),
                }
        for model in expired:
            del _MODEL_COOLDOWNS[model]
    return active


def get_auto_fallback_model(requested_model: str) -> str | None:
    """Return requested_model if not in cooldown, else None."""
    if not is_model_in_cooldown(requested_model):
        return requested_model
    return None


def select_fallback_candidate(chain: list[str]) -> str | None:
    """Find the first healthy model in the specified fallback chain."""
    for m in chain:
        m_clean = m.strip()
        if not m_clean:
            continue
        if not is_model_in_cooldown(m_clean):
            return m_clean
    return None


def resolve_backend_model(requested: str) -> tuple[str, str | None]:
    """Resolve any requested model string to (backend_model_id, thinking_level) dynamically."""
    live_models = get_available_models()
    if not requested:
        fallback = get_default_model() or (live_models[0] if live_models else "")
        return fallback, None

    effective_requested = requested

    # 1. Exact match
    if effective_requested in live_models:
        thinking = "high" if "high" in effective_requested else ("low" if "low" in effective_requested else None)
        return effective_requested, thinking

    # 2. Normalized match (e.g. 'claude-sonnet-5.5' -> 'claude-sonnet-5-5')
    norm = effective_requested.replace(".", "-").lower()

    # Common aliases
    if norm in ("dall-e-3", "dall-e-2", "image", "image-gen"):
        img_model = next((m for m in live_models if "image" in m.lower()), "")
        if img_model:
            return img_model, None
    if norm in ("claude-sonnet", "sonnet", "claude-3-5-sonnet", "claude-3-7-sonnet"):
        norm = "claude-sonnet-5-5"
    elif norm in ("claude-opus", "opus"):
        norm = "claude-opus-5-5"
    elif norm in ("gemini-flash", "flash"):
        norm = "gemini-3.8-flash"
    elif norm in ("gemini-pro", "pro"):
        norm = "gemini-2.5-pro"

    cands = [m for m in live_models if m.lower() == norm or m.lower().startswith(f"{norm}-") or norm in m.lower()]
    if cands:
        preferred = (
            [c for c in cands if "-medium" in c] or
            [c for c in cands if "-low" in c] or
            [c for c in cands if "-high" in c] or
            cands
        )
        chosen = preferred[0]
        thinking = "high" if "-high" in chosen else ("low" if "-low" in chosen else None)
        return chosen, thinking

    # Default fallback
    return requested, None


# ──────────────────────────────────────────────────────────────────────────────
# Message & Envelope Transformers
# ──────────────────────────────────────────────────────────────────────────────

def _content_to_text(content) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks = []
        for block in content:
            if not isinstance(block, dict):
                chunks.append(str(block))
                continue
            btype = block.get("type", "text")
            if btype == "text":
                chunks.append(block.get("text", ""))
            elif btype == "image_url":
                iu = block.get("image_url")
                url = iu.get("url", "") if isinstance(iu, dict) else str(iu or "")
                chunks.append(f"[image: {url[:50]}...]")
            else:
                chunks.append(str(block.get("text") or ""))
        return "\n".join(c for c in chunks if c)
    return str(content)


def _openai_tools_to_gemini(tools: list) -> list:
    if not tools:
        return []
    decls = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        func = tool.get("function") if tool.get("type") == "function" else tool
        if not isinstance(func, dict):
            continue
        name = func.get("name")
        if not name:
            continue
        decl = {"name": name, "description": func.get("description", "")}
        params = func.get("parameters")
        if isinstance(params, dict) and params:
            decl["parameters"] = params
        decls.append(decl)
    return [{"functionDeclarations": decls}] if decls else []


def _tool_choice_to_gemini(tool_choice) -> str | None:
    if not tool_choice or tool_choice == "auto":
        return "AUTO"
    if tool_choice == "none":
        return "NONE"
    if tool_choice == "required":
        return "ANY"
    if isinstance(tool_choice, dict) and tool_choice.get("type") == "function":
        return "ANY"
    return None


def _build_gemini_request(body: dict, backend_model: str, thinking_level: str | None = None) -> dict:
    messages = body.get("messages", [])
    if not isinstance(messages, list):
        messages = []

    system_texts = []
    contents = []
    tool_call_name_map = {}

    # Map tool call IDs to real function names
    for msg in messages:
        for tc in (msg.get("tool_calls") or []):
            if isinstance(tc, dict):
                tid = tc.get("id")
                tfn = (tc.get("function") or {}).get("name")
                if tid and tfn:
                    tool_call_name_map[tid] = tfn
                    if tid.startswith("call_"):
                        raw_id = tid[5:]
                        tool_call_name_map[raw_id] = tfn
                        if "|" in raw_id:
                            tool_call_name_map[raw_id.split("|", 1)[0]] = tfn

    def _append_turn(target_contents: list, target_role: str, target_parts: list):
        if not target_parts:
            return
        if target_contents and target_contents[-1].get("role") == target_role:
            target_contents[-1]["parts"].extend(target_parts)
        else:
            target_contents.append({"role": target_role, "parts": list(target_parts)})

    for msg in messages:
        role = msg.get("role", "user")

        if role == "system":
            text = _content_to_text(msg.get("content"))
            if text:
                system_texts.append(text)
            continue

        if role == "tool":
            tool_call_id = msg.get("tool_call_id", "")
            content_text = _content_to_text(msg.get("content"))
            response_obj = {}
            if content_text:
                try:
                    parsed = json.loads(content_text)
                    response_obj = parsed if isinstance(parsed, dict) else {"result": parsed}
                except (json.JSONDecodeError, TypeError):
                    response_obj = {"result": content_text}

            # Function name must match original function call name exactly
            fn_name = msg.get("name") or tool_call_name_map.get(tool_call_id)
            if not fn_name and tool_call_id.startswith("call_"):
                raw_id = tool_call_id[5:]
                fn_name = tool_call_name_map.get(raw_id)
                if not fn_name and "|" in raw_id:
                    fn_name = tool_call_name_map.get(raw_id.split("|", 1)[0])
            if not fn_name:
                fn_name = "tool_result"

            fc_response_id = ""
            if tool_call_id.startswith("call_"):
                raw = tool_call_id[5:]
                fc_response_id = raw.split("|", 1)[0] if "|" in raw else raw

            func_resp = {"name": fn_name, "response": response_obj}
            if fc_response_id:
                func_resp["id"] = fc_response_id
            _append_turn(contents, "user", [{"functionResponse": func_resp}])
            continue

        if role == "assistant":
            parts = []
            text = _content_to_text(msg.get("content"))
            if text and not (text.startswith("[stream error:") or text.startswith("[Error:")):
                parts.append({"text": text})

            for tc in (msg.get("tool_calls") or []):
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function") or {}
                fn_name = fn.get("name", "")
                args_str = fn.get("arguments", "{}")
                try:
                    args = json.loads(args_str) if isinstance(args_str, str) else args_str
                except (json.JSONDecodeError, TypeError):
                    args = {"raw": str(args_str)}
                if fn_name:
                    tc_id = tc.get("id", "") or ""
                    fc_id = ""
                    thought_sig = ""
                    if tc_id.startswith("call_"):
                        raw = tc_id[5:]
                        if "|" in raw:
                            fc_id, thought_sig = raw.split("|", 1)
                        else:
                            fc_id = raw
                    fc_obj = {"name": fn_name, "args": args}
                    if fc_id:
                        fc_obj["id"] = fc_id
                    part_obj = {"functionCall": fc_obj}
                    part_obj["thoughtSignature"] = thought_sig or "skip_thought_signature_validator"
                    parts.append(part_obj)
            if parts:
                _append_turn(contents, "model", parts)
            continue

        if role == "developer":
            text = _content_to_text(msg.get("content"))
            if text:
                system_texts.append(text)
            continue

        # Default user role
        text = _content_to_text(msg.get("content"))
        parts = []
        if text:
            parts.append({"text": text})
        for tc in (msg.get("tool_calls") or []):
            if isinstance(tc, dict):
                fn = tc.get("function") or {}
                if fn.get("name"):
                    parts.append({"functionCall": {"name": fn["name"], "args": fn.get("arguments", {})}})
        if parts:
            _append_turn(contents, "user", parts)

    # Invariant: Requests must always terminate with a user turn
    while contents and contents[-1].get("role") == "model":
        contents.pop()
    if not contents or contents[-1].get("role") != "user":
        _append_turn(contents, "user", [{"text": "Continue"}])

    system_instruction = (
        {"parts": [{"text": "\n\n".join(s for s in system_texts if s)}]}
        if system_texts else
        {"parts": [{"text": DEFAULT_SYSTEM_INSTRUCTION}]}
    )

    inner = {"contents": contents, "systemInstruction": system_instruction}

    tools = _openai_tools_to_gemini(body.get("tools"))
    if tools:
        inner["tools"] = tools

    tool_choice = body.get("tool_choice")
    mode = _tool_choice_to_gemini(tool_choice)
    if mode:
        inner["toolConfig"] = {"functionCallingConfig": {"mode": mode}}

    gen_config = {}
    if body.get("temperature") is not None:
        gen_config["temperature"] = float(body["temperature"])
    if body.get("top_p") is not None:
        gen_config["topP"] = float(body["top_p"])
    if body.get("max_tokens") is not None:
        gen_config["maxOutputTokens"] = int(body["max_tokens"])
    elif body.get("max_completion_tokens") is not None:
        gen_config["maxOutputTokens"] = int(body["max_completion_tokens"])
    if thinking_level:
        gen_config["thinkingConfig"] = {"thinkingLevel": thinking_level, "includeThoughts": False}
    if gen_config:
        inner["generationConfig"] = gen_config

    inner["safetySettings"] = [
        {"category": cat, "threshold": "BLOCK_NONE"}
        for cat in (
            "HARM_CATEGORY_HARASSMENT",
            "HARM_CATEGORY_HATE_SPEECH",
            "HARM_CATEGORY_SEXUALLY_EXPLICIT",
            "HARM_CATEGORY_DANGEROUS_CONTENT",
        )
    ]

    return {
        "project": get_project_id(),
        "model": backend_model,
        "request": inner,
        "requestType": "agent",
        "userAgent": "antigravity",
        "requestId": f"agent-{uuid.uuid4().hex}",
    }


# Matches /(v1|v1beta)/models/{model}:{action}
_GEMINI_PASSTHROUGH_RE = re.compile(
    r"^/(?:v1|v1beta)/models/([^:]+):(generateContent|streamGenerateContent)$"
)


def _build_passthrough_envelope(model: str, gemini_body: dict) -> dict:
    backend_model, thinking_level = resolve_backend_model(model)
    inner = dict(gemini_body)

    if thinking_level and "generationConfig" in inner:
        tc = inner["generationConfig"].setdefault("thinkingConfig", {})
        tc.setdefault("thinkingLevel", thinking_level)

    if "safetySettings" not in inner:
        inner["safetySettings"] = [
            {"category": cat, "threshold": "BLOCK_NONE"}
            for cat in (
                "HARM_CATEGORY_HARASSMENT",
                "HARM_CATEGORY_HATE_SPEECH",
                "HARM_CATEGORY_SEXUALLY_EXPLICIT",
                "HARM_CATEGORY_DANGEROUS_CONTENT",
            )
        ]

    return {
        "project": get_project_id(),
        "model": backend_model,
        "request": inner,
        "requestType": "agent",
        "userAgent": "antigravity",
        "requestId": f"agent-{uuid.uuid4().hex}",
    }


# ──────────────────────────────────────────────────────────────────────────────
# OpenAI / Gemini Response Serialization
# ──────────────────────────────────────────────────────────────────────────────

def _extract_parts(parts: list) -> tuple[str, list, str | None]:
    text_chunks = []
    tool_calls = []
    finish_hint = None

    for part in parts or []:
        if not isinstance(part, dict):
            continue
        if part.get("text"):
            text_chunks.append(part["text"])
        if "functionCall" in part:
            fc = part["functionCall"] or {}
            name = fc.get("name", "")
            args = fc.get("args", {})
            fc_id = fc.get("id", "")
            thought_sig = part.get("thoughtSignature") or part.get("thought_signature") or ""
            if name:
                encoded_id = f"call_{fc_id}|{thought_sig}" if (fc_id or thought_sig) else f"call_{uuid.uuid4().hex[:20]}"
                tool_calls.append({
                    "id": encoded_id,
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": json.dumps(args) if isinstance(args, (dict, list)) else str(args),
                    },
                })
        if part.get("executableCode"):
            code = (part["executableCode"] or {}).get("code", "")
            if code:
                text_chunks.append(f"```python\n{code}\n```")
        if part.get("codeExecutionResult"):
            out = (part["codeExecutionResult"] or {}).get("output", "")
            if out:
                text_chunks.append(f"```\n{out}\n```")

    if tool_calls:
        finish_hint = "tool_calls"
    return "".join(text_chunks), tool_calls, finish_hint


def _approx_token_count(text: str) -> int:
    if not text:
        return 0
    # ~4 characters per token heuristic
    return max(1, (len(text) + 3) // 4)
def _extract_usage(response: dict) -> dict:
    usage = response.get("usageMetadata") or {}
    if not usage:
        cands = response.get("candidates") or []
        if cands:
            usage = cands[0].get("usageMetadata") or {}
    pt = usage.get("promptTokenCount", 0) or 0
    ct = usage.get("candidatesTokenCount", 0) or usage.get("completionTokenCount", 0) or 0
    return {
        "prompt_tokens": pt,
        "completion_tokens": ct,
        "total_tokens": pt + ct,
    }


def gemini_response_to_openai(response: dict, model: str) -> dict:
    candidates = response.get("candidates") or []
    text = ""
    tool_calls = []
    finish_reason = "stop"

    if candidates:
        cand = candidates[0]
        parts = (cand.get("content") or {}).get("parts") or []
        text, tool_calls, _ = _extract_parts(parts)
        if tool_calls:
            finish_reason = "tool_calls"
        else:
            fr = (cand.get("finishReason") or "").upper()
            finish_reason = "length" if fr == "MAX_TOKENS" else ("content_filter" if fr in ("SAFETY", "BLOCKLIST") else "stop")
    else:
        text = "[No candidates returned]"

    message = {"role": "assistant", "content": text if text else None}
    if tool_calls:
        message["tool_calls"] = tool_calls

    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": message,
            "finish_reason": finish_reason,
        }],
        "usage": _extract_usage(response),
    }


# ──────────────────────────────────────────────────────────────────────────────
# Network Client
# ──────────────────────────────────────────────────────────────────────────────

class UpstreamError(Exception):
    def __init__(self, message, status=502, body=""):
        super().__init__(message)
        self.status = status
        self.body = body


def _make_upstream_request(url: str, envelope: dict, streaming: bool = False) -> Request:
    body = json.dumps(envelope).encode()
    req = Request(url, data=body, method="POST")
    req.add_header("Authorization", f"Bearer {get_access_token()}")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", ANTIGRAVITY_USER_AGENT)
    req.add_header("X-Goog-Api-Client", "google-cloud-sdk vscode_cloudshelleditor/0.1")
    req.add_header("Client-Metadata", CLIENT_METADATA)
    if streaming:
        req.add_header("Accept", "text/event-stream")
    return req


def call_generate_content(envelope: dict) -> dict:
    req = _make_upstream_request(GENERATE_CONTENT_URL, envelope, streaming=False)
    try:
        with urlopen(req, timeout=UPSTREAM_TIMEOUT) as resp:
            payload = json.loads(resp.read().decode())
            return payload.get("response", payload)
    except HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode()
        except Exception:
            pass
        if e.code == 429 or "RESOURCE_EXHAUSTED" in detail:
            record_model_cooldown(envelope.get("model", ""), detail)
        raise UpstreamError(f"Upstream HTTP {e.code}: {detail}", status=e.code, body=detail)
    except Exception as e:
        raise UpstreamError(f"Upstream network error: {e}", status=502)


class StreamResponse:
    def __init__(self, resp):
        self._resp = resp
        self._gen = self._generate()

    def _generate(self):
        try:
            for line in self._resp:
                line_str = line.decode("utf-8", errors="replace").strip() if isinstance(line, bytes) else str(line).strip()
                if not line_str or line_str.startswith(":") or line_str.startswith("event:") or line_str.startswith("id:"):
                    continue
                if line_str.startswith("data:"):
                    data_str = line_str[5:].strip()
                    if data_str == "[DONE]":
                        yield {"__done__": True}
                        break
                    try:
                        yield json.loads(data_str)
                    except json.JSONDecodeError:
                        pass
        finally:
            self.close()

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._gen)

    def close(self):
        try:
            self._resp.close()
        except Exception:
            pass


def stream_generate_content(envelope: dict):
    req = _make_upstream_request(STREAM_GENERATE_CONTENT_URL, envelope, streaming=True)
    try:
        resp = urlopen(req, timeout=UPSTREAM_TIMEOUT)
    except HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode()
        except Exception:
            pass
        if e.code == 429 or "RESOURCE_EXHAUSTED" in detail:
            record_model_cooldown(envelope.get("model", ""), detail)
        raise UpstreamError(f"Upstream stream HTTP {e.code}: {detail}", status=e.code, body=detail)
    except Exception as e:
        raise UpstreamError(f"Upstream stream network error: {e}", status=502)

    return StreamResponse(resp)


def fetch_quota_summary() -> dict:
    token = get_access_token()
    req = Request(USER_QUOTA_SUMMARY_URL, data=b"{}", method="POST")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", ANTIGRAVITY_USER_AGENT)
    req.add_header("X-Goog-Api-Client", "google-cloud-sdk vscode_cloudshelleditor/0.1")
    req.add_header("Client-Metadata", CLIENT_METADATA)
    with urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


# ──────────────────────────────────────────────────────────────────────────────
# HTTP Server & Router
# ──────────────────────────────────────────────────────────────────────────────

class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "AntigravityUniversalProxy/2.1"

    def log_message(self, fmt, *args):
        pass

    def _send_cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS, PUT, DELETE")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Max-Age", "86400")

    def do_OPTIONS(self):
        self.send_response(204)
        self._send_cors_headers()
        self.end_headers()

    def _send_sse_headers(self, served_model: str | None = None):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        if served_model:
            self.send_header("x-litellm-model-id", served_model)
            self.send_header("X-Served-Model", served_model)
        self._send_cors_headers()
        self.end_headers()

    def _write_sse_event(self, data: str | bytes, event_type: str | None = None) -> bool:
        try:
            if event_type:
                self.wfile.write(f"event: {event_type}\n".encode())
            if isinstance(data, bytes):
                self.wfile.write(b"data: " + data + b"\n\n")
            else:
                self.wfile.write(f"data: {data}\n\n".encode())
            self.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionResetError):
            return False
        except Exception:
            return False

    def _send_json(self, code: int, data: dict, extra_headers: list[tuple[str, str]] | None = None):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self._send_cors_headers()
        self.send_header("Content-Length", str(len(body)))
        if extra_headers:
            for k, v in extra_headers:
                self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _is_anthropic_path(self) -> bool:
        path = urlparse(self.path).path
        return path in ("/v1/messages", "/messages")

    def _send_error_json(self, code: str, message: str, err_type: str = "api_error", http_code: int = 400, extra_headers: list[tuple[str, str]] | None = None):
        headers = list(extra_headers) if extra_headers else []
        if http_code == 429:
            retry_seconds = None
            # 1. Look for retryDelay
            m = re.search(r"['\"]?retryDelay['\"]?\s*:\s*['\"]?(\d+(?:\.\d+)?)s?['\"]?", message)
            if m:
                try:
                    retry_seconds = max(1, int(float(m.group(1))))
                except Exception:
                    pass
            # 2. Look for quotaResetTimeStamp
            if retry_seconds is None:
                m_ts = re.search(r"['\"]?quotaResetTimeStamp['\"]?\s*:\s*['\"]?([0-9T:\-Z\.]+)['\"]?", message)
                if m_ts:
                    try:
                        ts_str = m_ts.group(1).rstrip("Z") + "+00:00"
                        target_dt = datetime.fromisoformat(ts_str)
                        retry_seconds = max(1, int((target_dt - datetime.now(timezone.utc)).total_seconds()))
                    except Exception:
                        pass
            # 3. Look for Resets in 138h3m54s
            if retry_seconds is None:
                m_txt = re.search(r"resets?\s+in\s+(?:(\d+(?:\.\d+)?)\s*h)?\s*(?:(\d+(?:\.\d+)?)\s*m)?\s*(?:(\d+(?:\.\d+)?)\s*s)?", message, re.IGNORECASE)
                if m_txt and any(m_txt.groups()):
                    h = float(m_txt.group(1) or 0)
                    m_val = float(m_txt.group(2) or 0)
                    s = float(m_txt.group(3) or 0)
                    retry_seconds = max(1, int(h * 3600 + m_val * 60 + s))

            if retry_seconds is not None:
                headers.append(("Retry-After", str(retry_seconds)))

        # Anthropic format error for Anthropic endpoints
        if self._is_anthropic_path():
            anthropic_type = "invalid_request_error"
            if http_code == 401:
                anthropic_type = "authentication_error"
            elif http_code == 403:
                anthropic_type = "permission_error"
            elif http_code == 404:
                anthropic_type = "not_found_error"
            elif http_code == 413:
                anthropic_type = "request_too_large"
            elif http_code == 429:
                anthropic_type = "rate_limit_error"
            elif http_code >= 500:
                anthropic_type = "api_error"

            self._send_json(http_code, {
                "type": "error",
                "error": {
                    "type": anthropic_type,
                    "message": message,
                }
            }, extra_headers=headers)
            return

        self._send_json(http_code, {
            "error": {
                "message": message,
                "type": err_type,
                "param": None,
                "code": code,
            }
        }, extra_headers=headers)

    def _read_body(self) -> dict | None:
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
        except (ValueError, TypeError):
            length = 0
        if length > MAX_PAYLOAD_SIZE:
            self.close_connection = True
            self._send_error_json("payload_too_large", f"Payload size {length} exceeds {MAX_PAYLOAD_SIZE} limit", err_type="invalid_request_error", http_code=413)
            return None
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    def _log_req(self, method, path, status, duration):
        _log(f"{method} {path} {status} {duration:.3f}s")

    # ── GET Routes ────────────────────────────────────────────────────────────

    def do_GET(self):
        t0 = time.time()
        path = urlparse(self.path).path
        try:
            if path in ("/health", "/"):
                self._send_json(200, {
                    "status": "ok",
                    "service": "antigravity-proxy",
                    "version": "2.2.0",
                    "project_id": get_project_id(),
                    "default_model": get_default_model(),
                    "active_cooldowns": get_active_cooldowns(),
                })
            elif path in ("/v1/models", "/models"):
                self._handle_models_openai()
            elif path.startswith("/v1/models/"):
                self._handle_model_detail(path[len("/v1/models/"):])
            elif path == "/v1beta/models":
                self._handle_models_gemini()
            elif path in ("/v1/usage", "/usage"):
                self._handle_usage()
            else:
                self._send_error_json("not_found", f"Endpoint {path} not found", http_code=404)
        except Exception as e:
            _log(f"GET {path} error: {e}")
            self._send_error_json("internal_error", str(e), http_code=500)
        finally:
            self._log_req("GET", path, 200, time.time() - t0)

    def _handle_models_openai(self):
        now = int(time.time())
        live_models = get_available_models()
        data = []
        seen = set()

        # Add all exact live backend models and dynamic dot-formatted aliases
        for m in live_models:
            if m not in seen:
                data.append({"id": m, "object": "model", "created": now, "owned_by": "google"})
                seen.add(m)
            dot_alias = re.sub(r"-(\d+)-(\d+)", r"-\1.\2", m)
            if dot_alias != m and dot_alias not in seen:
                data.append({"id": dot_alias, "object": "model", "created": now, "owned_by": "google"})
                seen.add(dot_alias)

        self._send_json(200, {"object": "list", "data": data})

    def _handle_model_detail(self, model_id: str):
        now = int(time.time())
        backend, _ = resolve_backend_model(model_id)
        self._send_json(200, {
            "id": model_id,
            "object": "model",
            "created": now,
            "owned_by": "google",
            "backend_target": backend
        })

    def _handle_models_gemini(self):
        live_models = get_available_models()
        items = []
        for m in live_models:
            items.append({
                "name": f"models/{m}",
                "version": "001",
                "displayName": m,
                "description": f"Google Antigravity model: {m}",
                "inputTokenLimit": 1048576,
                "outputTokenLimit": 65536,
                "supportedGenerationMethods": ["generateContent", "countTokens"],
            })
        self._send_json(200, {"models": items})

    def _handle_usage(self):
        try:
            quota = fetch_quota_summary()
            self._send_json(200, {
                "status": "ok",
                "project_id": get_project_id(),
                "active_cooldowns": get_active_cooldowns(),
                "quota": quota,
            })
        except Exception as e:
            self._send_error_json("upstream_error", f"Failed to retrieve quota: {e}", http_code=502)

    # ── POST Routes ───────────────────────────────────────────────────────────

    def do_POST(self):
        t0 = time.time()
        path = urlparse(self.path).path
        try:
            if path in ("/v1/chat/completions", "/chat/completions"):
                self._handle_chat()
            elif path in ("/v1/completions", "/completions"):
                self._handle_completions()
            elif path in ("/v1/messages", "/messages"):
                self._handle_anthropic_messages()
            elif path in ("/v1/images/generations", "/images/generations"):
                self._handle_images()
            elif path in ("/v1/cooldown/clear", "/cooldown/clear"):
                with _MODEL_COOLDOWNS_LOCK:
                    _MODEL_COOLDOWNS.clear()
                self._send_json(200, {"status": "ok", "message": "All model cooldowns cleared"})
            elif _GEMINI_PASSTHROUGH_RE.match(path):
                self._handle_gemini_passthrough(path)
            else:
                self._send_error_json("not_found", f"Endpoint {path} not found", http_code=404)
        except UpstreamError as e:
            _log(f"POST {path} upstream error: {e}")
            self._send_error_json("upstream_error", str(e), http_code=e.status)
        except Exception as e:
            _log(f"POST {path} error: {e}\n{traceback.format_exc()}")
            err_msg = str(e)
            if "429" in err_msg or "RESOURCE_EXHAUSTED" in err_msg:
                self._send_error_json("upstream_error", err_msg, http_code=429)
            else:
                self._send_error_json("internal_error", err_msg, http_code=500)
        finally:
            self._log_req("POST", path, 200, time.time() - t0)

    # ── OpenAI Chat Completions Handler ───────────────────────────────────────

    def _get_fallback_chain(self, body: dict) -> list[str]:
        """Extract fallback chain from headers or body if explicitly requested.
        Supports:
          - Header 'X-Fallback-Chain': 'gemini-4-argon-low, gemini-3.8-flash-medium'
          - Header 'X-Fallback-Models': '...'
          - Body 'fallback_chain': ['model1', 'model2']
        """
        raw_header = self.headers.get("X-Fallback-Chain") or self.headers.get("X-Fallback-Models")
        if raw_header:
            return [m.strip() for m in raw_header.split(",") if m.strip()]
        if isinstance(body, dict) and "fallback_chain" in body:
            fb = body.get("fallback_chain")
            if isinstance(fb, list):
                return [str(m).strip() for m in fb if str(m).strip()]
            if isinstance(fb, str):
                return [m.strip() for m in fb.split(",") if m.strip()]
        return []

    def _handle_chat(self):
        body = self._read_body()
        if body is None:
            return
        if not isinstance(body, dict):
            self._send_error_json("invalid_request", "Body must be JSON object", http_code=400)
            return

        openai_model = body.get("model") or get_default_model()
        stream = bool(body.get("stream", False))
        fallback_chain = self._get_fallback_chain(body)

        # Check if requested model is in cooldown
        effective_model = openai_model
        if is_model_in_cooldown(openai_model):
            if fallback_chain:
                cand = select_fallback_candidate(fallback_chain)
            else:
                cand = get_auto_fallback_model(openai_model)
            if cand and cand != openai_model:
                _log(f"[Auto-Fallback] '{openai_model}' is in cooldown. Automatically using healthy fallback model '{cand}'")
                effective_model = cand

        backend_model, thinking_level = resolve_backend_model(effective_model)

        def make_envelope(m, th):
            b_m, t_l = resolve_backend_model(m)
            return _build_gemini_request(body, b_m, t_l)

        try:
            envelope = make_envelope(effective_model, thinking_level)
        except Exception as e:
            self._send_error_json("request_error", f"Failed to build request: {e}", http_code=400)
            return

        if stream:
            stream_options = body.get("stream_options") or {}
            include_usage = bool(stream_options.get("include_usage", False))
            self._stream_chat(
                envelope,
                effective_model,
                include_usage=include_usage,
                retry_builder=make_envelope,
                fallback_chain=fallback_chain,
            )
        else:
            self._nonstream_chat(
                envelope,
                effective_model,
                retry_builder=make_envelope,
                fallback_chain=fallback_chain,
            )

    def _nonstream_chat(self, envelope: dict, openai_model: str, retry_builder=None, fallback_chain=None):
        try:
            gemini_resp = call_generate_content(envelope)
            headers = [("x-litellm-model-id", openai_model), ("X-Served-Model", openai_model)]
            self._send_json(200, gemini_response_to_openai(gemini_resp, openai_model), extra_headers=headers)
        except UpstreamError as e:
            # If 429 quota exhausted, loop through fallback candidates sequentially
            if (e.status == 429 or "RESOURCE_EXHAUSTED" in str(e)) and retry_builder and fallback_chain:
                tried = {openai_model, envelope.get("model", "")}
                while True:
                    cand = select_fallback_candidate(fallback_chain)
                    if not cand or cand in tried:
                        break
                    tried.add(cand)
                    try:
                        _log(f"[Auto-Fallback] Upstream 429 on '{envelope.get('model')}'. Retrying with candidate '{cand}'")
                        new_env = retry_builder(cand, None)
                        gemini_resp = call_generate_content(new_env)
                        headers = [("x-litellm-model-id", cand), ("X-Served-Model", cand)]
                        self._send_json(200, gemini_response_to_openai(gemini_resp, cand), extra_headers=headers)
                        return
                    except UpstreamError as re_err:
                        _log(f"[Auto-Fallback] Non-stream retry error on '{cand}': {re_err}")
                        if re_err.status != 429 and "RESOURCE_EXHAUSTED" not in str(re_err):
                            break
                    except Exception as re_err:
                        _log(f"[Auto-Fallback] Non-stream retry exception on '{cand}': {re_err}")
                        break
            self._send_error_json("upstream_error", str(e), http_code=e.status)

    def _stream_chat(self, envelope: dict, openai_model: str, include_usage: bool = False, retry_builder=None, fallback_chain=None):
        # Peak first event or check for immediate upstream failure BEFORE sending HTTP 200
        gen = None
        first_event = None
        served_model = openai_model
        try:
            gen = stream_generate_content(envelope)
            first_event = next(gen)
        except UpstreamError as e:
            # If 429 quota exhausted, loop through fallback candidates sequentially before SSE headers
            if (e.status == 429 or "RESOURCE_EXHAUSTED" in str(e)) and retry_builder and fallback_chain:
                tried = {openai_model, envelope.get("model", "")}
                while True:
                    cand = select_fallback_candidate(fallback_chain)
                    if not cand or cand in tried:
                        break
                    tried.add(cand)
                    try:
                        _log(f"[Auto-Fallback] Upstream 429 on stream. Retrying immediately with candidate '{cand}'")
                        new_env = retry_builder(cand, None)
                        gen = stream_generate_content(new_env)
                        first_event = next(gen)
                        served_model = cand
                        break
                    except UpstreamError as re_err:
                        _log(f"[Auto-Fallback] Stream retry error on '{cand}': {re_err}")
                        if re_err.status != 429 and "RESOURCE_EXHAUSTED" not in str(re_err):
                            break
                    except Exception as re_err:
                        _log(f"[Auto-Fallback] Stream retry exception on '{cand}': {re_err}")
                        break
            if gen is None or first_event is None:
                self._send_error_json("upstream_error", str(e), http_code=e.status)
                return
        except StopIteration:
            pass
        except Exception as e:
            self._send_error_json("upstream_error", str(e), http_code=502)
            return

        if gen is None:
            return

        completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
        created = int(time.time())

        self._send_sse_headers(served_model=served_model)
        send_sse = lambda obj: self._write_sse_event(json.dumps(obj))

        # First role chunk with served_model clearly identified
        if not send_sse({
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": served_model,
            "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}],
        }):
            gen.close()
            return

        seen_tool_indexes = set()
        emitted_tool_keys = set()
        finish_reason = "stop"
        total_usage: dict | None = None
        has_tool_calls = False

        def iter_events():
            if first_event is not None:
                yield first_event
            yield from gen

        try:
            for event in iter_events():
                if not isinstance(event, dict) or event.get("__done__"):
                    break
                resp = event.get("response", event)
                if isinstance(resp, dict):
                    meta = resp.get("usageMetadata")
                    if isinstance(meta, dict):
                        total_usage = meta

                cands = resp.get("candidates") or []
                if not cands:
                    continue
                cand = cands[0]
                parts = (cand.get("content") or {}).get("parts") or []

                text_chunks = []
                tool_deltas = []
                for part in parts or []:
                    if not isinstance(part, dict):
                        continue
                    if part.get("text"):
                        text_chunks.append(part["text"])
                    if "functionCall" in part:
                        has_tool_calls = True
                        fc = part["functionCall"] or {}
                        name = fc.get("name", "")
                        args = fc.get("args", {})
                        fc_id = fc.get("id", "")
                        thought_sig = part.get("thoughtSignature") or part.get("thought_signature") or ""
                        args_str = json.dumps(args) if isinstance(args, (dict, list)) else str(args)

                        idx = len(seen_tool_indexes)
                        encoded_id = f"call_{fc_id}|{thought_sig}" if (fc_id or thought_sig) else f"call_{uuid.uuid4().hex[:20]}"
                        call_key = (fc_id, name) if fc_id else (name, args_str)

                        if call_key in emitted_tool_keys:
                            continue
                        emitted_tool_keys.add(call_key)

                        if idx not in seen_tool_indexes:
                            seen_tool_indexes.add(idx)
                            # First chunk for this tool call sends id, type, name, and initial arguments
                            tool_deltas.append({
                                "index": idx,
                                "id": encoded_id,
                                "type": "function",
                                "function": {
                                    "name": name,
                                    "arguments": args_str,
                                },
                            })
                        else:
                            # If ever emitted under same index, do not duplicate arguments string
                            tool_deltas.append({
                                "index": idx,
                                "function": {
                                    "arguments": "",
                                },
                            })

                text_delta = "".join(text_chunks)

                if cand.get("finishReason"):
                    fr_up = str(cand["finishReason"]).upper()
                    finish_reason = "tool_calls" if has_tool_calls else ("length" if fr_up == "MAX_TOKENS" else "stop")

                delta = {}
                if text_delta:
                    delta["content"] = text_delta
                if tool_deltas:
                    delta["tool_calls"] = tool_deltas

                if delta:
                    ok = send_sse({
                        "id": completion_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": served_model,
                        "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
                    })
                    if not ok:
                        gen.close()
                        return

            # Final stop chunk
            final_chunk = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": served_model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}],
            }
            if not send_sse(final_chunk):
                gen.close()
                return

            # If stream_options.include_usage was requested, send standard usage-only chunk
            if include_usage and total_usage:
                usage_chunk = {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": served_model,
                    "choices": [],
                    "usage": {
                        "prompt_tokens": total_usage.get("promptTokenCount", 0),
                        "completion_tokens": total_usage.get("candidatesTokenCount", 0),
                        "total_tokens": total_usage.get("totalTokenCount", 0),
                    },
                }
                if not send_sse(usage_chunk):
                    gen.close()
                    return

            self._write_sse_event(b"[DONE]")
        except (BrokenPipeError, ConnectionResetError):
            gen.close()
        except Exception as e:
            _log(f"Stream error: {e}")
            gen.close()
            self._write_sse_event(json.dumps({'error': {'message': str(e)}}))
            self._write_sse_event(b"[DONE]")

    # ── OpenAI Text Completions Handler (Legacy / Code Completion) ────────────

    def _handle_completions(self):
        body = self._read_body()
        if body is None:
            return
        if not isinstance(body, dict):
            self._send_error_json("invalid_request", "Body must be JSON object", http_code=400)
            return

        prompt = body.get("prompt", "")
        if isinstance(prompt, list):
            prompt = "\n".join(str(p) for p in prompt)

        model = body.get("model") or get_default_model()
        stream = bool(body.get("stream", False))
        backend_model, thinking_level = resolve_backend_model(model)

        chat_body = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": body.get("max_tokens"),
            "temperature": body.get("temperature"),
            "stream": stream,
        }

        try:
            envelope = _build_gemini_request(chat_body, backend_model, thinking_level)
        except Exception as e:
            self._send_error_json("request_error", str(e), http_code=400)
            return

        if not stream:
            try:
                gemini_resp = call_generate_content(envelope)
                cand = (gemini_resp.get("candidates") or [{}])[0]
                parts = (cand.get("content") or {}).get("parts") or []
                text = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
                self._send_json(200, {
                    "id": f"cmpl-{uuid.uuid4().hex[:24]}",
                    "object": "text_completion",
                    "created": int(time.time()),
                    "model": model,
                    "choices": [{"text": text, "index": 0, "logprobs": None, "finish_reason": "stop"}],
                    "usage": _extract_usage(gemini_resp),
                })
            except UpstreamError as e:
                self._send_error_json("upstream_error", str(e), http_code=e.status)
        else:
            completion_id = f"cmpl-{uuid.uuid4().hex[:24]}"
            created = int(time.time())

            stream_resp = None
            try:
                stream_resp = stream_generate_content(envelope)
            except UpstreamError as e:
                self._send_error_json("upstream_error", str(e), http_code=e.status)
                return
            except Exception as e:
                self._send_error_json("upstream_error", str(e), http_code=502)
                return

            self._send_sse_headers()
            try:
                for event in stream_resp:
                    if not isinstance(event, dict) or event.get("__done__"):
                        break
                    resp = event.get("response", event)
                    cands = resp.get("candidates") or []
                    if not cands:
                        continue
                    parts = (cands[0].get("content") or {}).get("parts") or []
                    delta_text = "".join(p.get("text", "") for p in parts if isinstance(p, dict) and p.get("text"))
                    if delta_text:
                        chunk_payload = json.dumps({'id': completion_id, 'object': 'text_completion', 'created': created, 'model': model, 'choices': [{'text': delta_text, 'index': 0, 'finish_reason': None}]})
                        if not self._write_sse_event(chunk_payload):
                            if stream_resp:
                                stream_resp.close()
                            break
                self._write_sse_event(b"[DONE]")
            except (BrokenPipeError, ConnectionResetError):
                if stream_resp:
                    stream_resp.close()
            except Exception:
                if stream_resp:
                    stream_resp.close()

    # ── Anthropic Messages API Handler ────────────────────────────────────────

    def _handle_anthropic_messages(self):
        body = self._read_body()
        if body is None:
            return
        if not isinstance(body, dict):
            self._send_error_json("invalid_request", "Body must be JSON object", http_code=400)
            return

        model = body.get("model") or get_default_model()
        stream = bool(body.get("stream", False))
        system = body.get("system")
        raw_msgs = body.get("messages", [])
        fallback_chain = self._get_fallback_chain(body)

        # Check if requested model is in cooldown
        effective_model = model
        if is_model_in_cooldown(model) and fallback_chain:
            cand = select_fallback_candidate(fallback_chain)
            if cand and cand != model:
                _log(f"[Auto-Fallback] Anthropic model '{model}' is in cooldown. Automatically using healthy fallback model '{cand}'")
                effective_model = cand

        conv_messages = []
        if system:
            conv_messages.append({"role": "system", "content": _content_to_text(system)})

        for m in raw_msgs:
            role = m.get("role", "user")
            content = m.get("content")
            if isinstance(content, list):
                text_parts = []
                tool_calls = []
                for b in content:
                    if isinstance(b, dict):
                        btype = b.get("type")
                        if btype == "text":
                            text_parts.append(b.get("text", ""))
                        elif btype == "tool_use":
                            tool_calls.append({
                                "id": b.get("id"),
                                "type": "function",
                                "function": {"name": b.get("name"), "arguments": json.dumps(b.get("input", {}))},
                            })
                        elif btype == "tool_result":
                            conv_messages.append({
                                "role": "tool",
                                "tool_call_id": b.get("tool_use_id"),
                                "content": _content_to_text(b.get("content")),
                            })
                msg_dict = {"role": role, "content": "\n".join(text_parts)}
                if tool_calls:
                    msg_dict["tool_calls"] = tool_calls
                conv_messages.append(msg_dict)
            else:
                conv_messages.append({"role": role, "content": _content_to_text(content)})

        # Tools conversion
        anthropic_tools = body.get("tools", [])
        openai_tools = []
        for t in anthropic_tools:
            if isinstance(t, dict):
                openai_tools.append({
                    "type": "function",
                    "function": {
                        "name": t.get("name"),
                        "description": t.get("description", ""),
                        "parameters": t.get("input_schema", {}),
                    }
                })

        backend_model, thinking_level = resolve_backend_model(effective_model)
        chat_body = {
            "model": effective_model,
            "messages": conv_messages,
            "max_tokens": body.get("max_tokens", 4096),
            "temperature": body.get("temperature"),
            "tools": openai_tools if openai_tools else None,
            "stream": stream,
        }

        def make_anthropic_envelope(m):
            b_m, t_l = resolve_backend_model(m)
            c_b = dict(chat_body)
            c_b["model"] = m
            return _build_gemini_request(c_b, b_m, t_l)

        try:
            envelope = make_anthropic_envelope(effective_model)
        except Exception as e:
            self._send_error_json("request_error", str(e), http_code=400)
            return

        if not stream:
            gemini_resp = None
            served_model = effective_model
            try:
                gemini_resp = call_generate_content(envelope)
            except UpstreamError as e:
                if (e.status == 429 or "RESOURCE_EXHAUSTED" in str(e)) and fallback_chain:
                    tried = {effective_model, envelope.get("model", "")}
                    while True:
                        cand = select_fallback_candidate(fallback_chain)
                        if not cand or cand in tried:
                            break
                        tried.add(cand)
                        try:
                            _log(f"[Auto-Fallback] Anthropic upstream 429. Retrying with '{cand}'")
                            new_env = make_anthropic_envelope(cand)
                            gemini_resp = call_generate_content(new_env)
                            served_model = cand
                            break
                        except Exception as re_err:
                            _log(f"[Auto-Fallback] Anthropic retry on '{cand}' failed: {re_err}")
                if gemini_resp is None:
                    self._send_error_json("upstream_error", str(e), http_code=e.status)
                    return

            cand = (gemini_resp.get("candidates") or [{}])[0]
            parts = (cand.get("content") or {}).get("parts") or []
            text, tool_calls, _ = _extract_parts(parts)

            content_blocks = []
            if text:
                content_blocks.append({"type": "text", "text": text})
            for tc in tool_calls:
                fn = tc.get("function") or {}
                try:
                    args = json.loads(fn.get("arguments", "{}"))
                except Exception:
                    args = {"raw": fn.get("arguments")}
                content_blocks.append({
                    "type": "tool_use",
                    "id": tc.get("id"),
                    "name": fn.get("name"),
                    "input": args,
                })

            usage = _extract_usage(gemini_resp)
            headers = [("x-litellm-model-id", served_model), ("X-Served-Model", served_model)]
            self._send_json(200, {
                "id": f"msg_{uuid.uuid4().hex[:24]}",
                "type": "message",
                "role": "assistant",
                "content": content_blocks,
                "model": served_model,
                "stop_reason": "tool_use" if tool_calls else "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": usage["prompt_tokens"], "output_tokens": usage["completion_tokens"]},
            }, extra_headers=headers)
        else:
            # Anthropic streaming
            msg_id = f"msg_{uuid.uuid4().hex[:24]}"

            stream_resp = None
            served_model = effective_model
            try:
                stream_resp = stream_generate_content(envelope)
            except UpstreamError as e:
                if (e.status == 429 or "RESOURCE_EXHAUSTED" in str(e)) and fallback_chain:
                    tried = {effective_model, envelope.get("model", "")}
                    while True:
                        cand = select_fallback_candidate(fallback_chain)
                        if not cand or cand in tried:
                            break
                        tried.add(cand)
                        try:
                            _log(f"[Auto-Fallback] Anthropic stream 429. Retrying with '{cand}'")
                            new_env = make_anthropic_envelope(cand)
                            stream_resp = stream_generate_content(new_env)
                            served_model = cand
                            break
                        except Exception as re_err:
                            _log(f"[Auto-Fallback] Anthropic stream retry on '{cand}' failed: {re_err}")
                if stream_resp is None:
                    self._send_error_json("upstream_error", str(e), http_code=e.status)
                    return
            except Exception as e:
                self._send_error_json("upstream_error", str(e), http_code=502)
                return

            self._send_sse_headers(served_model=served_model)
            send_event = lambda event_type, data: self._write_sse_event(json.dumps(data), event_type=event_type)

            try:
                # Pre-calculate approximate prompt tokens
                approx_prompt_tokens = 0
                for m in conv_messages:
                    approx_prompt_tokens += _approx_token_count(m.get("content") or "")
                if anthropic_tools:
                    approx_prompt_tokens += _approx_token_count(json.dumps(anthropic_tools))

                total_usage: dict | None = None
                output_tokens_count = 0

                if not send_event("message_start", {
                    "type": "message_start",
                    "message": {
                        "id": msg_id,
                        "type": "message",
                        "role": "assistant",
                        "model": served_model,
                        "content": [],
                        "stop_reason": None,
                        "stop_sequence": None,
                        "usage": {"input_tokens": max(1, approx_prompt_tokens), "output_tokens": 0},
                    }
                }):
                    if stream_resp:
                        stream_resp.close()
                    return

                text_block_index: int | None = None
                tool_use_count = 0
                has_tool_use = False

                for event in stream_resp:
                    if not isinstance(event, dict) or event.get("__done__"):
                        break
                    resp = event.get("response", event)
                    if isinstance(resp, dict):
                        meta = resp.get("usageMetadata")
                        if isinstance(meta, dict):
                            total_usage = meta

                    cands = resp.get("candidates") or []
                    if not cands:
                        continue
                    parts = (cands[0].get("content") or {}).get("parts") or []

                    for part in parts:
                        if not isinstance(part, dict):
                            continue
                        part_text = part.get("text")
                        if part_text:
                            output_tokens_count += _approx_token_count(part_text)
                            if text_block_index is None:
                                text_block_index = tool_use_count
                                if not send_event("content_block_start", {
                                    "type": "content_block_start",
                                    "index": text_block_index,
                                    "content_block": {"type": "text", "text": ""},
                                }):
                                    if stream_resp:
                                        stream_resp.close()
                                    return
                            if not send_event("content_block_delta", {
                                "type": "content_block_delta",
                                "index": text_block_index,
                                "delta": {"type": "text_delta", "text": part_text},
                            }):
                                if stream_resp:
                                    stream_resp.close()
                                return

                        if "functionCall" in part:
                            has_tool_use = True
                            fc = part["functionCall"] or {}
                            name = fc.get("name", "")
                            args = fc.get("args", {})
                            fc_id = fc.get("id") or f"toolu_{uuid.uuid4().hex[:20]}"
                            thought_sig = part.get("thoughtSignature") or part.get("thought_signature") or ""
                            encoded_id = f"{fc_id}|{thought_sig}" if thought_sig else fc_id

                            # Close open text block if any before starting tool_use block
                            if text_block_index is not None:
                                if not send_event("content_block_stop", {
                                    "type": "content_block_stop",
                                    "index": text_block_index,
                                }):
                                    if stream_resp:
                                        stream_resp.close()
                                    return
                                text_block_index = None

                            current_tool_index = tool_use_count
                            tool_use_count += 1

                            # Emit content_block_start for tool_use
                            if not send_event("content_block_start", {
                                "type": "content_block_start",
                                "index": current_tool_index,
                                "content_block": {
                                    "type": "tool_use",
                                    "id": encoded_id,
                                    "name": name,
                                    "input": {},
                                },
                            }):
                                if stream_resp:
                                    stream_resp.close()
                                return

                            # Emit content_block_delta with partial_json
                            args_json = json.dumps(args) if isinstance(args, (dict, list)) else str(args)
                            output_tokens_count += _approx_token_count(args_json)
                            if not send_event("content_block_delta", {
                                "type": "content_block_delta",
                                "index": current_tool_index,
                                "delta": {
                                    "type": "input_json_delta",
                                    "partial_json": args_json,
                                },
                            }):
                                if stream_resp:
                                    stream_resp.close()
                                return

                            # Emit content_block_stop
                            if not send_event("content_block_stop", {
                                "type": "content_block_stop",
                                "index": current_tool_index,
                            }):
                                if stream_resp:
                                    stream_resp.close()
                                return

                # Close text block if still open
                if text_block_index is not None:
                    if not send_event("content_block_stop", {
                        "type": "content_block_stop",
                        "index": text_block_index,
                    }):
                        if stream_resp:
                            stream_resp.close()
                        return

                final_output_tokens = (
                    total_usage.get("candidatesTokenCount", output_tokens_count)
                    if isinstance(total_usage, dict)
                    else max(1, output_tokens_count)
                )
                stop_reason = "tool_use" if has_tool_use else "end_turn"

                if not send_event("message_delta", {
                    "type": "message_delta",
                    "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                    "usage": {"output_tokens": final_output_tokens},
                }):
                    if stream_resp:
                        stream_resp.close()
                    return

                send_event("message_stop", {"type": "message_stop"})
            except (BrokenPipeError, ConnectionResetError):
                if stream_resp:
                    stream_resp.close()
            except Exception as e:
                _log(f"Anthropic stream error: {e}")
                if stream_resp:
                    stream_resp.close()

    # ── OpenAI Image Generations Handler ──────────────────────────────────────

    def _handle_images(self):
        body = self._read_body()
        if body is None:
            return
        if not isinstance(body, dict):
            self._send_error_json("invalid_request", "Body must be JSON object", http_code=400)
            return

        prompt = body.get("prompt", "")
        if not prompt:
            self._send_error_json("invalid_request", "Prompt is required", http_code=400)
            return

        size = str(body.get("size") or "1024x1024").lower()
        aspect_ratio = "1:1"
        if "1792x1024" in size or "1344x768" in size:
            aspect_ratio = "16:9"
        elif "1024x1792" in size or "768x1344" in size:
            aspect_ratio = "9:16"
        elif "3:2" in size:
            aspect_ratio = "3:2"

        live_models = get_available_models()
        img_model = next((m for m in live_models if "image" in m.lower()), "image-gen")
        envelope = {
            "project": get_project_id(),
            "model": img_model,
            "request": {
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {
                    "imageConfig": {"aspectRatio": aspect_ratio},
                    "candidateCount": 1
                }
            },
            "requestType": "image_gen",
            "userAgent": "antigravity",
            "requestId": f"agent-{uuid.uuid4().hex}"
        }

        try:
            resp = call_generate_content(envelope)
            cands = resp.get("candidates") or []
            if not cands:
                raise RuntimeError("No image candidates returned from Google")
            parts = (cands[0].get("content") or {}).get("parts") or []
            b64_data = ""
            for p in parts:
                if isinstance(p, dict) and "inlineData" in p:
                    b64_data = p["inlineData"].get("data", "")
                    break
            if not b64_data:
                raise RuntimeError("No image data in response")

            self._send_json(200, {
                "created": int(time.time()),
                "data": [{"b64_json": b64_data}]
            })
        except UpstreamError as e:
            self._send_error_json("upstream_error", str(e), http_code=e.status)
        except Exception as e:
            self._send_error_json("image_error", f"Image generation failed: {e}", http_code=500)

    # ── Native Gemini Passthrough Handler ─────────────────────────────────────

    def _handle_gemini_passthrough(self, path: str):
        body = self._read_body()
        if body is None:
            return
        if not isinstance(body, dict):
            self._send_error_json("invalid_request", "Invalid JSON body", http_code=400)
            return

        m = _GEMINI_PASSTHROUGH_RE.match(path)
        model = m.group(1)
        action = m.group(2)

        try:
            envelope = _build_passthrough_envelope(model, body)
        except Exception as e:
            self._send_error_json("request_error", f"Failed to build envelope: {e}", http_code=400)
            return

        if action == "generateContent":
            try:
                gemini_resp = call_generate_content(envelope)
                self._send_json(200, gemini_resp)
            except UpstreamError as e:
                self._send_error_json("upstream_error", str(e), http_code=e.status)
        else:
            # streamGenerateContent passthrough
            stream_resp = None
            try:
                stream_resp = stream_generate_content(envelope)
            except UpstreamError as e:
                self._send_error_json("upstream_error", str(e), http_code=e.status)
                return
            except Exception as e:
                self._send_error_json("upstream_error", str(e), http_code=502)
                return

            self._send_sse_headers()

            try:
                for event in stream_resp:
                    if not isinstance(event, dict) or event.get("__done__"):
                        break
                    if not self._write_sse_event(json.dumps(event)):
                        if stream_resp:
                            stream_resp.close()
                        break
                self._write_sse_event(b"[DONE]")
            except (BrokenPipeError, ConnectionResetError):
                if stream_resp:
                    stream_resp.close()
            except Exception:
                if stream_resp:
                    stream_resp.close()


# ──────────────────────────────────────────────────────────────────────────────
# Main Entry Point
# ──────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Antigravity Universal OpenAI/Anthropic/Gemini Proxy")
    parser.add_argument("--port", type=int, default=8877, help="Port to listen on (default: 8877)")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="Host interface to bind to (default: 127.0.0.1)")
    args = parser.parse_args()

    _log("Starting Antigravity Universal Proxy...")
    _read_token_from_disk()
    get_access_token()
    get_project_id()

    # Pre-warm dynamic model catalog in background
    threading.Thread(target=get_available_models, daemon=True).start()

    server = ThreadingHTTPServer((args.host, args.port), ProxyHandler)
    server.daemon_threads = True
    _log(f"Listening on http://{args.host}:{args.port}")
    _log("Endpoints active:")
    _log("  • OpenAI Chat:        POST /v1/chat/completions")
    _log("  • OpenAI Completions:   POST /v1/completions")
    _log("  • Anthropic Messages:   POST /v1/messages")
    _log("  • Image Generations:    POST /v1/images/generations")
    _log("  • Gemini Passthrough:   POST /v1beta/models/{model}:generateContent (and stream)")
    _log("  • Live Models (Dynamic):GET  /v1/models, GET /v1beta/models")
    _log("  • Quota & Usage:        GET  /v1/usage")
    _log("  • Healthcheck:          GET  /health")

    def _shutdown(signum, frame):
        _log(f"Received signal {signum}, shutting down cleanly...")
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    try:
        server.serve_forever()
    finally:
        _log("Server stopped, closing...")
        server.server_close()


if __name__ == "__main__":
    main()
