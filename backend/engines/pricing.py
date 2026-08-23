# Copyright (c) 2026 Sealeria
# SPDX-License-Identifier: AGPL-3.0-or-later
# Licensed under the GNU Affero General Public License v3.0

"""Model pricing: pull public LiteLLM price table, store locally, resolve per model."""

from __future__ import annotations

import json
import urllib.request
from datetime import datetime, timezone

import database

LITELLM_PRICES_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/"
    "model_prices_and_context_window.json"
)

# Fallbacks when a model isn't in the table yet (USD per million tokens).
_DEFAULT_INPUT = 3.0
_DEFAULT_OUTPUT = 15.0

KEEP_LITELLM_PROVIDERS = (
    "anthropic",
    "openai",
    "azure",
    "openrouter",
    "google",
    "deepseek",
    "meta",
    "mistral",
    "mistralai",
    "qwen",
    "xai",
)

# OpenRouter usage + Cursor-relevant models (display label, LiteLLM lookup keys).
POPULAR_MODELS: list[tuple[str, list[str]]] = [
    ("openai/gpt-4o", ["gpt-4o", "openai/gpt-4o", "openai/gpt-4o-2024-05-13"]),
    ("openai/gpt-4o-mini", ["gpt-4o-mini", "openai/gpt-4o-mini"]),
    ("openai/gpt-4.1", ["gpt-4.1", "openai/gpt-4.1"]),
    ("openai/gpt-4.1-mini", ["gpt-4.1-mini", "openai/gpt-4.1-mini"]),
    ("openai/o3-mini", ["o3-mini", "openai/o3-mini"]),
    ("anthropic/claude-sonnet-4", ["claude-sonnet-4-20250514", "claude-sonnet-4-5", "claude-sonnet-4"]),
    ("anthropic/claude-3.5-sonnet", ["claude-3-5-sonnet-20241022", "claude-3-5-sonnet-latest"]),
    ("anthropic/claude-haiku-4.5", ["claude-haiku-4-5-20251001", "claude-haiku-4-5"]),
    ("anthropic/claude-3.5-haiku", ["claude-3-5-haiku-20241022", "claude-3-5-haiku-latest"]),
    ("anthropic/claude-opus-4", ["claude-opus-4-20250514", "claude-opus-4-1", "claude-opus-4"]),
    ("google/gemini-2.0-flash", ["gemini/gemini-2.0-flash", "gemini-2.0-flash", "gemini-2.0-flash"]),
    ("google/gemini-1.5-pro", ["gemini/gemini-1.5-pro", "gemini-1.5-pro"]),
    ("deepseek/deepseek-chat", ["deepseek/deepseek-chat", "deepseek-chat"]),
    ("deepseek/deepseek-r1", ["deepseek/deepseek-r1", "deepseek-reasoner"]),
    ("meta-llama/llama-3.1-70b-instruct", ["meta-llama/llama-3.1-70b-instruct", "llama-3.1-70b-instruct"]),
    ("mistral/mistral-large", ["mistral/mistral-large-latest", "mistral-large-latest"]),
    ("qwen/qwen-2.5-coder-32b", ["qwen/qwen-2.5-coder-32b-instruct"]),
    ("x-ai/grok-2", ["xai/grok-2", "grok-2"]),
]

# Seed popular model rates so USD works before the first fetch.
_SEED = {
    "gpt-4o": (2.5, 10.0),
    "gpt-4o-mini": (0.15, 0.6),
    "gpt-4.1": (2.0, 8.0),
    "gpt-4.1-mini": (0.4, 1.6),
    "o3-mini": (1.1, 4.4),
    "claude-haiku-4-5-20251001": (1.0, 5.0),
    "claude-sonnet-4-20250514": (3.0, 15.0),
    "claude-3-5-sonnet-20241022": (3.0, 15.0),
    "claude-3-5-haiku-20241022": (0.8, 4.0),
    "claude-opus-4-20250514": (15.0, 75.0),
    "gemini/gemini-2.0-flash": (0.1, 0.4),
    "gemini/gemini-1.5-pro": (1.25, 5.0),
    "deepseek/deepseek-chat": (0.14, 0.28),
    "deepseek/deepseek-r1": (0.55, 2.19),
    "meta-llama/llama-3.1-70b-instruct": (0.52, 0.75),
    "mistral/mistral-large-latest": (2.0, 6.0),
    "qwen/qwen-2.5-coder-32b-instruct": (0.2, 0.8),
}

STALE_AFTER_HOURS = 24


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ensure_seed_prices() -> None:
    ts = _now()
    for model, (inp, out) in _SEED.items():
        database.upsert_model_price(model, inp, out, ts, "seed")


def _normalize_keys(model: str) -> list[str]:
    if not model:
        return []
    m = model.strip()
    keys = [m]
    if m.startswith("anthropic/"):
        keys.append(m[len("anthropic/") :])
    else:
        keys.append(f"anthropic/{m}")
    # drop dated suffix: claude-haiku-4-5-20251001 → claude-haiku-4-5
    parts = m.rsplit("-", 1)
    if len(parts) == 2 and parts[1].isdigit() and len(parts[1]) >= 8:
        keys.append(parts[0])
        keys.append(f"anthropic/{parts[0]}")
    return keys


def lookup_rates(model: str) -> tuple[float, float]:
    """Return (input_usd_per_mtok, output_usd_per_mtok)."""
    prices = database.get_all_model_prices()
    for key in _normalize_keys(model):
        row = prices.get(key)
        if row:
            return row["input_per_mtok"], row["output_per_mtok"]
    # substring soft match (haiku/sonnet/opus)
    lower = (model or "").lower()
    for name, row in prices.items():
        if name.lower() in lower or lower in name.lower():
            return row["input_per_mtok"], row["output_per_mtok"]
    return _DEFAULT_INPUT, _DEFAULT_OUTPUT


def estimate_usd_saved(input_tokens_saved: int, output_tokens_saved: int, model: str) -> float:
    inp, out = lookup_rates(model)
    return (input_tokens_saved or 0) / 1_000_000 * inp + (output_tokens_saved or 0) / 1_000_000 * out


def _match_price_row(by_name: dict[str, dict], patterns: list[str]) -> dict | None:
    for pat in patterns:
        if pat in by_name:
            return by_name[pat]
    pats = [p.lower() for p in patterns]
    for name, row in by_name.items():
        nl = name.lower()
        for pat in pats:
            if nl == pat or nl.endswith("/" + pat) or pat in nl:
                return row
    return None


def list_popular_model_prices(limit: int = 24) -> list[dict]:
    """Return curated popular models for the dashboard (not alphabetical dump)."""
    ensure_seed_prices()
    rows = database.list_all_model_prices()
    by_name = {r["model"]: r for r in rows}
    out: list[dict] = []
    seen: set[str] = set()
    for display, patterns in POPULAR_MODELS:
        if len(out) >= limit:
            break
        row = _match_price_row(by_name, patterns)
        if not row:
            for pat in patterns:
                if pat in _SEED:
                    inp, out = _SEED[pat]
                    row = {
                        "model": pat,
                        "input_per_mtok": inp,
                        "output_per_mtok": out,
                        "updated_at": database.get_meta("prices_updated_at") or _now(),
                        "source": "seed",
                    }
                    break
        if not row or row["model"] in seen:
            continue
        seen.add(row["model"])
        out.append({**row, "display": display})
    return out


def refresh_prices_from_litellm() -> dict:
    """Download LiteLLM price JSON and upsert anthropic/openai-ish entries."""
    req = urllib.request.Request(LITELLM_PRICES_URL, headers={"User-Agent": "ai-gateway-proxy/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read()
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("unexpected prices payload")

    ts = _now()
    upserted = 0
    for name, info in data.items():
        if name.startswith("sample_") or not isinstance(info, dict):
            continue
        litellm_provider = (info.get("litellm_provider") or "").lower()
        if litellm_provider not in KEEP_LITELLM_PROVIDERS:
            n = name.lower()
            if not any(
                n.startswith(p)
                for p in ("claude", "gpt", "gemini", "deepseek", "llama", "mistral", "qwen", "grok")
            ):
                continue
        try:
            in_tok = float(info.get("input_cost_per_token") or 0)
            out_tok = float(info.get("output_cost_per_token") or 0)
        except (TypeError, ValueError):
            continue
        if in_tok <= 0 and out_tok <= 0:
            continue
        database.upsert_model_price(
            name,
            in_tok * 1_000_000,
            out_tok * 1_000_000,
            ts,
            "litellm",
        )
        upserted += 1

    database.set_meta("prices_updated_at", ts)
    database.set_meta("prices_source", "litellm")
    database.set_meta("prices_count", str(upserted))
    return {"updated_at": ts, "models": upserted, "source": "litellm"}


def maybe_auto_refresh() -> dict | None:
    """Refresh if never updated or older than STALE_AFTER_HOURS."""
    ensure_seed_prices()
    updated = database.get_meta("prices_updated_at")
    if updated:
        try:
            then = datetime.strptime(updated, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            age_h = (datetime.now(timezone.utc) - then).total_seconds() / 3600
            if age_h < STALE_AFTER_HOURS:
                return None
        except ValueError:
            pass
    try:
        return refresh_prices_from_litellm()
    except Exception as exc:
        # Fail open — keep seed/last known prices.
        database.set_meta("prices_last_error", f"{type(exc).__name__}: {exc}")
        return {"error": str(exc)}


def pricing_status() -> dict:
    ensure_seed_prices()
    prices = list_popular_model_prices(limit=24)
    return {
        "updated_at": database.get_meta("prices_updated_at"),
        "source": database.get_meta("prices_source") or ("seed" if prices else None),
        "count": database.count_model_prices(),
        "last_error": database.get_meta("prices_last_error"),
        "stale_after_hours": STALE_AFTER_HOURS,
        "models": prices,
    }
