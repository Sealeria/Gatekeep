# Copyright (c) 2026 Sealeria
# SPDX-License-Identifier: AGPL-3.0-or-later
# Licensed under the GNU Affero General Public License v3.0

"""Extra savings techniques (no model routing): cache-fix, digest, defer, intent cache."""

from __future__ import annotations

import hashlib
import re

from engines import ccr
from engines.optimizer import _content_text, canonical_json_dumps

# Keep this many tools with full schemas; rest get defer_loading stubs.
DEFER_KEEP_FULL = 5
DIGEST_MIN_CHARS = 400
PRESERVE_RECENT_MESSAGES = 4

_VOLATILE_SYSTEM = [
    re.compile(r"(?im)^.*\bcc_version\b.*$", re.M),
    re.compile(r"(?im)^.*\bclaude.?code.?version\b.*$", re.M),
    re.compile(r"(?i)\bcurrent (date|time)\s*[:=]\s*[^\n]+"),
    re.compile(r"(?i)\btoday'?s date\s*[:=]\s*[^\n]+"),
    re.compile(r"\b20\d{2}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?\b"),
]

_WS = re.compile(r"\s+")


def strip_volatile_system(system):
    """Remove fingerprint/timestamp lines that bust Anthropic prefix cache."""

    def scrub(text: str) -> str:
        if not text:
            return text
        original = text
        for pat in _VOLATILE_SYSTEM:
            text = pat.sub("", text)
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        # Never emit empty text blocks — Anthropic 400s on those.
        return text if text else original

    if isinstance(system, str):
        return scrub(system)
    if isinstance(system, list):
        out = []
        for block in system:
            if isinstance(block, dict) and block.get("type") == "text":
                text = scrub(block.get("text", ""))
                if not text.strip():
                    continue
                block = dict(block)
                block["text"] = text
                out.append(block)
            else:
                out.append(block)
        return out if out else system
    return system


def stabilize_tools(tools: list) -> list:
    """Deterministic tool order (by name). CCR retrieve stays last."""
    if not isinstance(tools, list) or len(tools) < 2:
        return tools
    retrieve = [t for t in tools if isinstance(t, dict) and t.get("name") == ccr.RETRIEVE_TOOL_NAME]
    rest = [t for t in tools if not (isinstance(t, dict) and t.get("name") == ccr.RETRIEVE_TOOL_NAME)]
    rest_sorted = sorted(
        rest,
        key=lambda t: (t.get("name") or "") if isinstance(t, dict) else "",
    )
    return rest_sorted + retrieve


def apply_defer_loading(tools: list) -> tuple[list, bool]:
    """Mark overflow / MCP tools as defer_loading stubs (Anthropic tool search)."""
    if not isinstance(tools, list) or not tools:
        return tools, False
    changed = False
    out = []
    full_kept = 0
    for tool in tools:
        if not isinstance(tool, dict):
            out.append(tool)
            continue
        name = tool.get("name") or ""
        t = dict(tool)
        if name == ccr.RETRIEVE_TOOL_NAME:
            out.append(t)
            continue
        is_mcp = name.startswith("mcp__") or "__" in name[:6]
        keep_full = full_kept < DEFER_KEEP_FULL and not is_mcp
        if keep_full:
            full_kept += 1
            out.append(t)
            continue
        # Stub: name + short description + empty schema + defer_loading
        if not t.get("defer_loading"):
            t["defer_loading"] = True
            changed = True
        if isinstance(t.get("description"), str) and len(t["description"]) > 32:
            t["description"] = t["description"][:29] + "…"
            changed = True
        schema = t.get("input_schema")
        if isinstance(schema, dict) and (schema.get("properties") or schema.get("required")):
            t["input_schema"] = {"type": "object", "properties": {}}
            changed = True
        out.append(t)
    return out, changed


def _digest_text(text: str) -> str:
    text = (text or "").strip()
    if len(text) < DIGEST_MIN_CHARS:
        return text
    # Keep signal lines (errors / paths / headings), else head+tail.
    lines = [l for l in text.split("\n") if l.strip()]
    keep = []
    for line in lines:
        low = line.lower()
        if any(k in low for k in ("error", "traceback", "failed", "exception", "todo", "fix")):
            keep.append(line[:200])
        elif re.search(r"[\\/][\w.-]+\.\w+", line):
            keep.append(line[:200])
        if len(keep) >= 8:
            break
    if keep:
        body = "\n".join(keep)
        return f"[DIGEST {len(text)} chars]\n{body}"
    return (
        f"[DIGEST {len(text)} chars]\n"
        f"{text[:220]}\n…\n{text[-180:]}"
    )


def apply_extractive_digest(payload: dict) -> bool:
    """Shrink old text-only turns in-place (pure per-message; keeps tool pairing)."""
    messages = payload.get("messages")
    if not isinstance(messages, list) or len(messages) <= PRESERVE_RECENT_MESSAGES + 1:
        return False
    changed = False
    cutoff = len(messages) - PRESERVE_RECENT_MESSAGES
    for msg in messages[1:cutoff]:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if isinstance(content, str):
            dig = _digest_text(content)
            if dig != content:
                msg["content"] = dig
                changed = True
            continue
        if not isinstance(content, list):
            continue
        # Skip messages that carry tool_use / tool_result (pairing + live work).
        types = {b.get("type") for b in content if isinstance(b, dict)}
        if "tool_use" in types or "tool_result" in types:
            continue
        new_blocks = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                block = dict(block)
                dig = _digest_text(block.get("text", ""))
                if dig != block.get("text"):
                    block["text"] = dig
                    changed = True
                new_blocks.append(block)
            else:
                new_blocks.append(block)
        msg["content"] = new_blocks
    return changed


def strip_media_blocks(payload: dict) -> bool:
    """Drop image/base64 document blocks from history (huge + rarely needed twice)."""
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return False
    changed = False
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        new_blocks = []
        for block in content:
            if isinstance(block, dict) and block.get("type") in ("image", "document", "file"):
                new_blocks.append(
                    {"type": "text", "text": f"[{block.get('type')} omitted by proxy]"}
                )
                changed = True
            else:
                new_blocks.append(block)
        if changed:
            msg["content"] = new_blocks
    return changed


def sanitize_thinking_blocks(payload: dict) -> bool:
    """Drop empty/omitted thinking blocks that desync Claude Code prompt cache."""
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return False
    changed = False
    for msg in messages:
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        new_blocks = []
        for block in content:
            if not isinstance(block, dict):
                new_blocks.append(block)
                continue
            btype = block.get("type")
            if btype == "thinking":
                thinking = block.get("thinking")
                # Empty or whitespace-only (incl. CC "omitted" placeholders)
                if not (isinstance(thinking, str) and thinking.strip()):
                    changed = True
                    continue
            if btype == "redacted_thinking" and not block.get("data"):
                changed = True
                continue
            new_blocks.append(block)
        if len(new_blocks) != len(content):
            msg["content"] = new_blocks
            changed = True
    return changed


def fold_repeated_log_lines(text: str) -> str:
    """Lossless-ish fold of consecutive near-identical log lines (digit/ts vary)."""
    if not text or text.count("\n") < 8:
        return text
    num = re.compile(r"\d+")
    ts = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?")

    def templ(line: str) -> str:
        return num.sub("{}", ts.sub("{}", line))

    lines = text.split("\n")
    out = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            out.append(line)
            i += 1
            continue
        t = templ(line)
        j = i + 1
        while j < len(lines) and lines[j].strip() and templ(lines[j]) == t:
            j += 1
        run = j - i
        if run >= 4:
            out.append(f"{t}  [×{run} folded]")
            i = j
        else:
            out.extend(lines[i:j])
            i = j
    return "\n".join(out)


def apply_cache_and_structure_fixes(payload: dict) -> list:
    """Run cache-fix + digest + defer + media strip. Returns category tags."""
    cats = []
    if sanitize_thinking_blocks(payload):
        cats.append("thinking_sanitize")

    if "system" in payload:
        before = _content_text(payload["system"])
        payload["system"] = strip_volatile_system(payload["system"])
        if _content_text(payload["system"]) != before:
            cats.append("cache_fix")

    if apply_extractive_digest(payload):
        cats.append("digest")

    if strip_media_blocks(payload):
        cats.append("media_strip")

    if isinstance(payload.get("tools"), list) and payload["tools"]:
        before_names = [t.get("name") if isinstance(t, dict) else None for t in payload["tools"]]
        payload["tools"] = stabilize_tools(payload["tools"])
        after_names = [t.get("name") if isinstance(t, dict) else None for t in payload["tools"]]
        payload["tools"], deferred = apply_defer_loading(payload["tools"])
        if before_names != after_names and "cache_fix" not in cats:
            cats.append("cache_fix")
        if deferred:
            cats.append("defer_loading")

    return cats


def normalize_intent_text(text: str) -> str:
    return _WS.sub(" ", (text or "").strip().lower())


def compute_intent_hash(payload: dict, provider: str) -> str | None:
    """Hash for short plain-text user turns (retries / identical asks). None = skip."""
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        return None
    last = messages[-1]
    if not isinstance(last, dict) or last.get("role") != "user":
        return None
    content = last.get("content")
    if isinstance(content, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return None
        text = _content_text(content)
    elif isinstance(content, str):
        text = content
    else:
        return None
    text = normalize_intent_text(text)
    if not text or len(text) > 800:
        return None
    first = messages[0] if isinstance(messages[0], dict) else {}
    first_text = normalize_intent_text(_content_text(first.get("content")))
    seed = {
        "provider": provider,
        "model": payload.get("model"),
        "system": payload.get("system"),
        "first": first_text,
        "n": len(messages),
        "intent": text,
    }
    return "intent:" + hashlib.sha256(canonical_json_dumps(seed)).hexdigest()
