# Copyright (c) 2026 Sealeria
# SPDX-License-Identifier: AGPL-3.0-or-later
# Licensed under the GNU Affero General Public License v3.0

"""Multi-provider upstream routing for Gatekeep."""

from __future__ import annotations

import os

ANTHROPIC_BASE_URL = os.getenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com").rstrip("/")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com").rstrip("/")
CHATGPT_BASE_URL = os.getenv("CHATGPT_BASE_URL", "https://chatgpt.com").rstrip("/")
GOOGLE_BASE_URL = os.getenv(
    "GOOGLE_BASE_URL", "https://generativelanguage.googleapis.com"
).rstrip("/")
MISTRAL_BASE_URL = os.getenv("MISTRAL_BASE_URL", "https://api.mistral.ai").rstrip("/")
CURSOR_BASE_URL = os.getenv("CURSOR_BASE_URL", "https://api2.cursor.sh").rstrip("/")
FREEBUFF_BASE_URL = os.getenv("FREEBUFF_BASE_URL", "https://www.codebuff.com").rstrip("/")
DEVIN_BASE_URL = os.getenv("DEVIN_BASE_URL", "https://api.devin.ai").rstrip("/")

_MISTRAL_MODEL_PREFIXES = (
    "mistral",
    "codestral",
    "devstral",
    "ministral",
    "pixtral",
    "magistral",
    "open-mistral",
    "open-mixtral",
)

# Path prefixes → provider (first match wins after header hints).
_PATH_PROVIDERS: tuple[tuple[str, str], ...] = (
    ("aiserver.", "cursor"),
    ("agent.v1.", "cursor"),
    ("api/v1/freebuff", "freebuff"),
    ("backend-api/", "chatgpt"),
    ("api/codex", "chatgpt"),
    ("codex/", "chatgpt"),
    ("codex-backend/", "chatgpt"),
    ("ps/", "chatgpt"),
    ("plugins/", "chatgpt"),
    ("v3beta", "devin"),
    ("v3/", "devin"),
    ("v1/messages", "anthropic"),
    ("v1/complete", "anthropic"),
    ("v1/responses", "openai"),
    ("v1/chat/completions", "openai"),
    ("v1/completions", "openai"),
    ("v1/embeddings", "openai"),
    ("v1/models", "openai"),  # API-key Codex; ChatGPT Codex uses chatgpt.com
    ("v1beta/", "google"),
    ("v1/projects/", "google"),
)


def _header_map(headers) -> dict[str, str]:
    return {str(k).lower(): str(v) for k, v in headers.items()}


def _is_mistral_model(model: str | None) -> bool:
    if not model:
        return False
    m = model.strip().lower()
    return any(m.startswith(p) or f"/{p}" in m for p in _MISTRAL_MODEL_PREFIXES)


def resolve_provider(headers, path: str, model: str | None = None) -> str:
    """Return provider id for upstream routing."""
    h = _header_map(headers)
    path_l = (path or "").lstrip("/").lower()
    ctype = h.get("content-type", "")

    # Explicit override from client (Vibe custom provider, etc.)
    override = (h.get("x-gatekeep-provider") or "").strip().lower()
    if override in {
        "anthropic", "openai", "chatgpt", "google", "mistral",
        "cursor", "freebuff", "devin",
    }:
        return override

    # Cursor agent CLI (--endpoint / CURSOR_API_ENDPOINT) — ConnectRPC
    if path_l.startswith("aiserver.") or path_l.startswith("agent.v1."):
        return "cursor"
    if "connect+" in ctype or ctype.startswith("application/connect"):
        return "cursor"
    if "application/grpc" in ctype:
        return "cursor"

    # Freebuff / Codebuff session API (when client can be pointed here)
    if path_l.startswith("api/v1/freebuff"):
        return "freebuff"

    # Codex ChatGPT subscription backend (chatgpt_base_url -> Gatekeep)
    if (
        path_l.startswith("backend-api/")
        or path_l.startswith("api/codex")
        or path_l.startswith("codex/")
        or path_l.startswith("codex-backend/")
        or path_l.startswith("ps/")
        or path_l.startswith("oauth/codex")
        or path_l.startswith("plugins/")
    ):
        return "chatgpt"

    # Devin Cognition API (when DEVIN_API_URL / similar points here)
    if path_l.startswith("v3/") or path_l.startswith("v3beta"):
        return "devin"
    if h.get("x-devin-org") or h.get("x-cognition-org"):
        return "devin"

    # Explicit Google signals
    if "x-goog-api-key" in h or "x-goog-user-project" in h:
        return "google"
    if path_l.startswith("v1beta/") or "generativelanguage" in path_l:
        return "google"

    # Explicit Anthropic signals (before Authorization — Claude Code sends both sometimes)
    if "anthropic-version" in h or "anthropic-beta" in h:
        return "anthropic"
    if path_l.startswith("v1/messages") or path_l.startswith("v1/complete"):
        return "anthropic"

    # Mistral / Vibe (OpenAI-compatible wire, different upstream host)
    if _is_mistral_model(model):
        return "mistral"

    # OpenAI / Codex Responses
    if path_l.startswith("v1/responses") or path_l.startswith("v1/chat/"):
        return "openai"
    if path_l.startswith("v1/embeddings") or path_l == "v1/models" or path_l.startswith("v1/models/"):
        # Ambiguous /v1/models: prefer OpenAI if Bearer, else Anthropic-style clients rarely hit this
        if "authorization" in h and not h.get("x-api-key", "").startswith("sk-ant"):
            return "openai"
        if "authorization" in h:
            return "openai"

    if "x-api-key" in h:
        key = h.get("x-api-key") or ""
        if key.startswith("sk-ant"):
            return "anthropic"
        # Google AI Studio keys are often passed as x-goog-api-key; some proxies use x-api-key
        if path_l.startswith("v1beta"):
            return "google"
        return "anthropic"

    if "authorization" in h:
        return "openai"

    for prefix, provider in _PATH_PROVIDERS:
        if path_l.startswith(prefix):
            return provider

    return "anthropic"


def base_url_for(provider: str, path: str = "") -> str:
    path_l = (path or "").lstrip("/").lower()
    if provider == "cursor" and path_l.startswith("agent.v1."):
        from core.cursor_agent import DEFAULT_AGENT_UPSTREAM
        from engines.cursor_rewrite import get_agent_upstream

        upstream = get_agent_upstream()
        if upstream:
            return upstream
        return DEFAULT_AGENT_UPSTREAM
    if provider == "openai":
        return OPENAI_BASE_URL
    if provider == "chatgpt":
        return CHATGPT_BASE_URL
    if provider == "google":
        return GOOGLE_BASE_URL
    if provider == "mistral":
        return MISTRAL_BASE_URL
    if provider == "cursor":
        return CURSOR_BASE_URL
    if provider == "freebuff":
        return FREEBUFF_BASE_URL
    if provider == "devin":
        return DEVIN_BASE_URL
    return ANTHROPIC_BASE_URL


def should_optimize_payload(payload: dict, path: str) -> bool:
    """Crush JSON chat/responses payloads (Anthropic, OpenAI/Mistral, Codex)."""
    if not isinstance(payload, dict):
        return False
    path_l = (path or "").lstrip("/").lower()
    if "count_tokens" in path_l:
        return False
    # Binary / proprietary — no JSON crush
    if path_l.startswith("aiserver.") or path_l.startswith("api/v1/freebuff"):
        return False
    if path_l.startswith("v3/") or path_l.startswith("v3beta"):
        return False
    # Analytics / plugins without conversation body
    if path_l.startswith("codex/analytics") or "/analytics" in path_l:
        return False
    if path_l.startswith("ps/plugins") or path_l.endswith("/mcp"):
        return False
    # Conversation-shaped JSON (incl. Codex ChatGPT Responses under backend-api/)
    if (
        "messages" in payload
        or "prompt" in payload
        or "input" in payload
        or "instructions" in payload
    ):
        return True
    return False


def is_anthropic_messages_payload(payload: dict) -> bool:
    """True when aggressive/CCR Anthropic tool_result blocks are expected."""
    if not isinstance(payload, dict):
        return False
    if "anthropic" in str(payload.get("model") or "").lower():
        return True
    for msg in payload.get("messages") or []:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") in (
                    "tool_use", "tool_result", "thinking",
                ):
                    return True
    return "system" in payload and isinstance(payload.get("system"), (str, list))


def is_openai_chat_payload(payload: dict) -> bool:
    """OpenAI / Mistral chat.completions JSON (not Anthropic Messages)."""
    if not isinstance(payload, dict) or is_anthropic_messages_payload(payload):
        return False
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        return False
    tools = payload.get("tools")
    if isinstance(tools, list) and any(
        isinstance(t, dict) and (t.get("type") == "function" or isinstance(t.get("function"), dict))
        for t in tools
    ):
        return True
    return any(
        isinstance(m, dict) and m.get("role") in ("system", "user", "assistant", "tool")
        for m in messages
    )


def is_openai_responses_payload(payload: dict) -> bool:
    """OpenAI / Codex Responses API (input[] + optional instructions/tools)."""
    if not isinstance(payload, dict) or is_anthropic_messages_payload(payload):
        return False
    if is_openai_chat_payload(payload):
        return False
    if not isinstance(payload.get("input"), (list, str)):
        return False
    return True
