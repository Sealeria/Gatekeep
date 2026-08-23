# Copyright (c) 2026 Sealeria
# SPDX-License-Identifier: AGPL-3.0-or-later
# Licensed under the GNU Affero General Public License v3.0

"""Compress-Cache-Retrieve store for aggressive tool_result compression.

Originals stay in process RAM. Claude Code cannot execute our retrieve tool, so
the proxy rewrites failed/empty ccr_retrieve tool_results on the next request.
"""

from __future__ import annotations

import hashlib
import threading
import time

MAX_ENTRIES = 256
DEFAULT_TTL_S = 6 * 3600

RETRIEVE_TOOL_NAME = "ccr_retrieve"
CCR_FULFILLED_KEY = "_ccr_fulfilled"

RETRIEVE_TOOL = {
    "name": RETRIEVE_TOOL_NAME,
    "description": (
        "Retrieve a full tool output previously compressed by the proxy. "
        "Call with the hash from a [CCR hash=...] marker when the truncated "
        "view is insufficient."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "hash": {
                "type": "string",
                "description": "CCR hash from the compression marker.",
            }
        },
        "required": ["hash"],
    },
}


class CcrStore:
    def __init__(self, max_entries: int = MAX_ENTRIES, ttl_s: int = DEFAULT_TTL_S):
        self._max = max_entries
        self._ttl = ttl_s
        self._lock = threading.Lock()
        self._items: dict[str, tuple[float, str]] = {}

    def put(self, text: str) -> str:
        key = hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()[:16]
        now = time.time()
        with self._lock:
            self._evict_locked(now)
            if len(self._items) >= self._max and key not in self._items:
                oldest = min(self._items, key=lambda k: self._items[k][0])
                self._items.pop(oldest, None)
            self._items[key] = (now, text)
        return key

    def get(self, key: str):
        if not key:
            return None
        now = time.time()
        with self._lock:
            self._evict_locked(now)
            item = self._items.get(key)
            if not item:
                return None
            ts, text = item
            self._items[key] = (now, text)
            return text

    def _evict_locked(self, now: float) -> None:
        expired = [k for k, (ts, _) in self._items.items() if now - ts > self._ttl]
        for k in expired:
            self._items.pop(k, None)


_store = CcrStore()


def store(text: str) -> str:
    return _store.put(text)


def recall(key: str):
    return _store.get(key)


def marker(hash_key: str, original_chars: int, preview: str) -> str:
    preview = preview.replace("\n", " ").strip()
    if len(preview) > 240:
        preview = preview[:240] + "…"
    return (
        f"[CCR hash={hash_key} chars={original_chars}] {preview}\n"
        f"(Full output stored locally. Call tool {RETRIEVE_TOOL_NAME} "
        f'with {{"hash":"{hash_key}"}} if you need more detail.)'
    )


def is_ccr_fulfilled(block: dict) -> bool:
    return bool(isinstance(block, dict) and block.get(CCR_FULFILLED_KEY))


def clear_ccr_fulfilled_flags(messages: list) -> None:
    if not isinstance(messages, list):
        return
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict):
                block.pop(CCR_FULFILLED_KEY, None)


def inject_retrieve_tool(tools):
    if not isinstance(tools, list):
        tools = []
    if any(isinstance(t, dict) and t.get("name") == RETRIEVE_TOOL_NAME for t in tools):
        return tools
    return list(tools) + [dict(RETRIEVE_TOOL)]


def fulfill_retrieve_tool_results(messages: list) -> bool:
    """Replace ccr_retrieve tool_results (incl. Claude Code execution errors)
    with the stored original. Returns True if any block was rewritten."""
    if not isinstance(messages, list):
        return False
    changed = False
    for msg in messages:
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not (isinstance(block, dict) and block.get("type") == "tool_result"):
                continue
            # Match via pending tool_use id → name requires a scan; cheaper:
            # only rewrite when content looks like a failed/unknown tool OR
            # explicitly requests our hash. We resolve hash from sibling
            # assistant tool_use below.
            pass

    # Build tool_use_id -> hash from assistant messages
    id_to_hash = {}
    for msg in messages:
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not (isinstance(block, dict) and block.get("type") == "tool_use"):
                continue
            if (block.get("name") or "") != RETRIEVE_TOOL_NAME:
                continue
            input_ = block.get("input") or {}
            h = input_.get("hash") if isinstance(input_, dict) else None
            if isinstance(h, str) and block.get("id"):
                id_to_hash[block["id"]] = h

    if not id_to_hash:
        return False

    for msg in messages:
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not (isinstance(block, dict) and block.get("type") == "tool_result"):
                continue
            h = id_to_hash.get(block.get("tool_use_id"))
            if not h:
                continue
            text = recall(h)
            if text is None:
                block["content"] = f"[CCR miss: hash={h} expired or unknown]"
                block["is_error"] = True
            else:
                block["content"] = text
                block[CCR_FULFILLED_KEY] = True
                block.pop("is_error", None)
            changed = True
    return changed
