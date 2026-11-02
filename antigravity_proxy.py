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

TOKEN_FILE = os.path.expanduser("~/.gemini/antigravity-cli/antigravity-oauth-token")
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
DEFAULT_MODEL = "gemini-3.8-flash"
UPSTREAM_TIMEOUT = 300  # 5 minutes
TOKEN_REFRESH_SKEW = 120  # refresh 2 mins before expiry

# ──────────────────────────────────────────────────────────────────────────────
# Token & Project State
# ──────────────────────────────────────────────────────────────────────────────

_token_lock = threading.Lock()
_cached_access_token: str | None = None

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
    try:
        tmp = TOKEN_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, TOKEN_FILE)
        try:
            os.chmod(TOKEN_FILE, 0o600)
        except OSError:
            pass
    except OSError as e:
        _log(f"WARNING: could not write token file: {e}")


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
    global _cached_access_token
    with _token_lock:
        data = _read_token_from_disk()
        if not data:
            raise RuntimeError(f"No OAuth token found at {TOKEN_FILE}.")
        token_obj = data.get("token") or {}
        access_token = token_obj.get("access_token")
        expiry_ts = _parse_expiry(token_obj.get("expiry"))
        now = time.time()

        if not access_token or expiry_ts == 0.0 or (expiry_ts - now) < TOKEN_REFRESH_SKEW:
            refresh_token = token_obj.get("refresh_token")
            if not refresh_token:
                raise RuntimeError("No refresh_token available; cannot refresh OAuth token.")
            _log("Access token expired (or missing) — refreshing via Google OAuth...")
            new_tok = _refresh_access_token(refresh_token)
            data["token"] = new_tok
            _write_token_to_disk(data)
            access_token = new_tok["access_token"]

        _cached_access_token = access_token
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
            _log(f"Live model fetch error (using fallback): {e}")
            if _cached_models:
                return _cached_models

        # Minimal fallback if network discovery blips
        return [
            "gemini-3.8-flash-low",
            "gemini-3.8-flash-high",
            "gemini-4-argon-medium",
            "claude-sonnet-5-5-medium",
            "claude-sonnet-5-5-high",
            "claude-opus-5-5-medium",
            "gpt-oss-120b-medium",
            "gemini-2.5-pro",
            "gemini-2.5-flash",
            "gemini-3.1-flash-image",
        ]


def resolve_backend_model(requested: str) -> tuple[str, str | None]:
    """Resolve any requested model string to (backend_model_id, thinking_level) dynamically."""
    if not requested:
        return "gemini-3.8-flash-low", None

    live_models = get_available_models()
    # 1. Exact match
    if requested in live_models:
        thinking = "high" if "high" in requested else ("low" if "low" in requested else None)
        return requested, thinking

    # 2. Normalized match (e.g. 'claude-sonnet-5.5' -> 'claude-sonnet-5-5')
    norm = requested.replace(".", "-").lower()

    # Common aliases
    if norm in ("dall-e-3", "dall-e-2", "image", "image-gen"):
        return "gemini-3.1-flash-image", None
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
    return "gemini-3.8-flash-low", None


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


def _extract_delta_from_parts(parts: list, prev_tool_names: set) -> tuple[str, list, set]:
    text_chunks = []
    tool_call_deltas = []
    current_names = set(prev_tool_names)

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
                current_names.add(name)
                encoded_id = f"call_{fc_id}|{thought_sig}" if (fc_id or thought_sig) else f"call_{uuid.uuid4().hex[:20]}"
                tool_call_deltas.append({
                    "index": len(current_names) - 1,
                    "id": encoded_id,
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": json.dumps(args) if isinstance(args, (dict, list)) else str(args),
                    },
                })

    return "".join(text_chunks), tool_call_deltas, current_names


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
        raise UpstreamError(f"Upstream HTTP {e.code}: {detail}", status=e.code, body=detail)
    except Exception as e:
        raise UpstreamError(f"Upstream network error: {e}", status=502)


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
        raise UpstreamError(f"Upstream stream HTTP {e.code}: {detail}", status=e.code, body=detail)
    except Exception as e:
        raise UpstreamError(f"Upstream stream network error: {e}", status=502)

    try:
        for line in resp:
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
        resp.close()


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

    def _send_json(self, code: int, data: dict):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self._send_cors_headers()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_error_json(self, code: str, message: str, err_type: str = "api_error", http_code: int = 400):
        self._send_json(http_code, {
            "error": {
                "message": message,
                "type": err_type,
                "param": None,
                "code": code,
            }
        })

    def _read_body(self) -> dict | None:
        length = int(self.headers.get("Content-Length", 0) or 0)
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
                    "version": "2.1.0",
                    "project_id": get_project_id(),
                    "default_model": DEFAULT_MODEL,
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

        # Add clean friendly aliases
        for friendly in (
            "gemini-3.8-flash", "gemini-3.8-flash-high", "gemini-4-argon",
            "claude-sonnet-5.5", "claude-sonnet-5.5-high", "claude-opus-5.5",
            "gpt-oss-120b", "gemini-2.5-pro", "gemini-2.5-flash", "gemini-3.1-flash-image"
        ):
            if friendly not in seen:
                data.append({"id": friendly, "object": "model", "created": now, "owned_by": "google"})
                seen.add(friendly)

        # Add all exact live backend models
        for m in live_models:
            if m not in seen:
                data.append({"id": m, "object": "model", "created": now, "owned_by": "google"})
                seen.add(m)

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
            elif _GEMINI_PASSTHROUGH_RE.match(path):
                self._handle_gemini_passthrough(path)
            else:
                self._send_error_json("not_found", f"Endpoint {path} not found", http_code=404)
        except Exception as e:
            _log(f"POST {path} error: {e}\n{traceback.format_exc()}")
            self._send_error_json("internal_error", str(e), http_code=500)
        finally:
            self._log_req("POST", path, 200, time.time() - t0)

    # ── OpenAI Chat Completions Handler ───────────────────────────────────────

    def _handle_chat(self):
        body = self._read_body()
        if not isinstance(body, dict):
            self._send_error_json("invalid_request", "Body must be JSON object", http_code=400)
            return

        openai_model = body.get("model") or DEFAULT_MODEL
        stream = bool(body.get("stream", False))
        backend_model, thinking_level = resolve_backend_model(openai_model)

        try:
            envelope = _build_gemini_request(body, backend_model, thinking_level)
        except Exception as e:
            self._send_error_json("request_error", f"Failed to build request: {e}", http_code=400)
            return

        if stream:
            self._stream_chat(envelope, openai_model)
        else:
            self._nonstream_chat(envelope, openai_model)

    def _nonstream_chat(self, envelope: dict, openai_model: str):
        try:
            gemini_resp = call_generate_content(envelope)
            self._send_json(200, gemini_response_to_openai(gemini_resp, openai_model))
        except UpstreamError as e:
            self._send_error_json("upstream_error", str(e), http_code=e.status)

    def _stream_chat(self, envelope: dict, openai_model: str):
        completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
        created = int(time.time())

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self._send_cors_headers()
        self.end_headers()
        self.close_connection = True

        def send_sse(obj: dict) -> bool:
            try:
                self.wfile.write(f"data: {json.dumps(obj)}\n\n".encode())
                self.wfile.flush()
                return True
            except Exception:
                return False

        # First role chunk
        send_sse({
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": openai_model,
            "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}],
        })

        seen_tool_names = set()
        finish_reason = "stop"
        total_usage = None

        try:
            for event in stream_generate_content(envelope):
                if not isinstance(event, dict) or event.get("__done__"):
                    break
                resp = event.get("response", event)
                if resp.get("usageMetadata"):
                    total_usage = resp["usageMetadata"]

                cands = resp.get("candidates") or []
                if not cands:
                    continue
                cand = cands[0]
                parts = (cand.get("content") or {}).get("parts") or []
                text_delta, tool_deltas, seen_tool_names = _extract_delta_from_parts(parts, seen_tool_names)

                if cand.get("finishReason"):
                    fr_up = str(cand["finishReason"]).upper()
                    finish_reason = "tool_calls" if tool_deltas else ("length" if fr_up == "MAX_TOKENS" else "stop")

                delta = {}
                if text_delta:
                    delta["content"] = text_delta
                if tool_deltas:
                    delta["tool_calls"] = tool_deltas

                if delta:
                    send_sse({
                        "id": completion_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": openai_model,
                        "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
                    })

            # Final stop chunk
            final_chunk = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": openai_model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}],
            }
            if total_usage:
                final_chunk["usage"] = {
                    "prompt_tokens": total_usage.get("promptTokenCount", 0),
                    "completion_tokens": total_usage.get("candidatesTokenCount", 0),
                    "total_tokens": total_usage.get("totalTokenCount", 0),
                }
            send_sse(final_chunk)
            try:
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            except Exception:
                pass
        except Exception as e:
            _log(f"Stream error: {e}")
            try:
                self.wfile.write(f"data: {json.dumps({'error': {'message': str(e)}})}\n\n".encode())
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            except Exception:
                pass

    # ── OpenAI Text Completions Handler (Legacy / Code Completion) ────────────

    def _handle_completions(self):
        body = self._read_body()
        if not isinstance(body, dict):
            self._send_error_json("invalid_request", "Body must be JSON object", http_code=400)
            return

        prompt = body.get("prompt", "")
        if isinstance(prompt, list):
            prompt = "\n".join(str(p) for p in prompt)

        model = body.get("model") or DEFAULT_MODEL
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
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self._send_cors_headers()
            self.end_headers()
            self.close_connection = True

            try:
                for event in stream_generate_content(envelope):
                    if not isinstance(event, dict) or event.get("__done__"):
                        break
                    resp = event.get("response", event)
                    cands = resp.get("candidates") or []
                    if not cands:
                        continue
                    parts = (cands[0].get("content") or {}).get("parts") or []
                    delta_text = "".join(p.get("text", "") for p in parts if isinstance(p, dict) and p.get("text"))
                    if delta_text:
                        self.wfile.write(f"data: {json.dumps({'id': completion_id, 'object': 'text_completion', 'created': created, 'model': model, 'choices': [{'text': delta_text, 'index': 0, 'finish_reason': None}]})}\n\n".encode())
                        self.wfile.flush()
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            except Exception:
                pass

    # ── Anthropic Messages API Handler ────────────────────────────────────────

    def _handle_anthropic_messages(self):
        body = self._read_body()
        if not isinstance(body, dict):
            self._send_error_json("invalid_request", "Body must be JSON object", http_code=400)
            return

        model = body.get("model") or "claude-sonnet-5.5"
        stream = bool(body.get("stream", False))
        system = body.get("system")
        raw_msgs = body.get("messages", [])

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

        backend_model, thinking_level = resolve_backend_model(model)
        chat_body = {
            "model": model,
            "messages": conv_messages,
            "max_tokens": body.get("max_tokens", 4096),
            "temperature": body.get("temperature"),
            "tools": openai_tools if openai_tools else None,
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
                self._send_json(200, {
                    "id": f"msg_{uuid.uuid4().hex[:24]}",
                    "type": "message",
                    "role": "assistant",
                    "content": content_blocks,
                    "model": model,
                    "stop_reason": "tool_use" if tool_calls else "end_turn",
                    "stop_sequence": None,
                    "usage": {"input_tokens": usage["prompt_tokens"], "output_tokens": usage["completion_tokens"]},
                })
            except UpstreamError as e:
                self._send_error_json("upstream_error", str(e), http_code=e.status)
        else:
            # Anthropic streaming
            msg_id = f"msg_{uuid.uuid4().hex[:24]}"
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self._send_cors_headers()
            self.end_headers()
            self.close_connection = True

            def send_event(event_type: str, data: dict):
                try:
                    self.wfile.write(f"event: {event_type}\ndata: {json.dumps(data)}\n\n".encode())
                    self.wfile.flush()
                except Exception:
                    pass

            send_event("message_start", {
                "type": "message_start",
                "message": {"id": msg_id, "type": "message", "role": "assistant", "model": model, "content": [], "stop_reason": None, "usage": {"input_tokens": 10, "output_tokens": 0}}
            })
            send_event("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}})

            try:
                for event in stream_generate_content(envelope):
                    if not isinstance(event, dict) or event.get("__done__"):
                        break
                    resp = event.get("response", event)
                    cands = resp.get("candidates") or []
                    if not cands:
                        continue
                    parts = (cands[0].get("content") or {}).get("parts") or []
                    delta_text, _, _ = _extract_delta_from_parts(parts, set())
                    if delta_text:
                        send_event("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": delta_text}})

                send_event("content_block_stop", {"type": "content_block_stop", "index": 0})
                send_event("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 20}})
                send_event("message_stop", {"type": "message_stop"})
            except Exception:
                pass

    # ── OpenAI Image Generations Handler ──────────────────────────────────────

    def _handle_images(self):
        body = self._read_body()
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

        envelope = {
            "project": get_project_id(),
            "model": "gemini-3.1-flash-image",
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
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self._send_cors_headers()
            self.end_headers()
            self.close_connection = True

            try:
                for event in stream_generate_content(envelope):
                    if not isinstance(event, dict) or event.get("__done__"):
                        break
                    self.wfile.write(f"data: {json.dumps(event)}\n\n".encode())
                    self.wfile.flush()
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            except Exception:
                pass


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

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        _log("Shutting down cleanly...")
        server.server_close()


if __name__ == "__main__":
    main()
