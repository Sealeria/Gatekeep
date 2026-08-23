# Copyright (c) 2026 Sealeria
# SPDX-License-Identifier: AGPL-3.0-or-later
# Licensed under the GNU Affero General Public License v3.0

"""Light loop watcher: detect repeated identical tool calls and relax crush for the session.

Softens age_elide/CCR so the agent can see results again (no hard abort).
Circuit breaker: repeated file reads or searches trigger a lossless bypass.
"""

from __future__ import annotations

import threading

from engines.optimizer import _extract_file_path, canonical_json_dumps

LOOP_REPEAT = 3
DECOMPRESS_WINDOW = 4
DECOMPRESS_REPEAT = 2
DECOMPRESS_TURNS = 2
_RELAXED: dict[str, bool] = {}
_DECOMPRESS: dict[str, int] = {}
_lock = threading.Lock()

_READ_TOOLS = frozenset(
    {"read", "read_file", "Read", "view", "view_file", "cat", "get_file_contents"}
)
_SEARCH_TOOLS = frozenset(
    {"grep", "Grep", "glob", "Glob", "ls", "list_dir", "list_files", "find", "search", "search_files"}
)

_NUDGE = (
    "[proxy] Repeated identical tool calls detected; "
    "stop re-running the same command; finish the task with what you have."
)


def clear_session(session_key: str | None) -> None:
    if not session_key:
        return
    with _lock:
        _RELAXED.pop(session_key, None)
        _DECOMPRESS.pop(session_key, None)


def is_relaxed(session_key: str | None) -> bool:
    if not session_key:
        return False
    with _lock:
        return bool(_RELAXED.get(session_key))


def is_decompress(session_key: str | None) -> bool:
    if not session_key:
        return False
    with _lock:
        return _DECOMPRESS.get(session_key, 0) > 0


def tick_decompress(session_key: str | None) -> None:
    """Decay decompress after a request that ran in lossless mode."""
    if not session_key:
        return
    with _lock:
        remaining = _DECOMPRESS.get(session_key, 0)
        if remaining <= 0:
            return
        remaining -= 1
        if remaining <= 0:
            _DECOMPRESS.pop(session_key, None)
        else:
            _DECOMPRESS[session_key] = remaining


def is_bypass_compression(session_key: str | None) -> bool:
    return is_relaxed(session_key) or is_decompress(session_key)


def _fingerprint(name: str, inp) -> str:
    try:
        body = canonical_json_dumps(inp) if inp is not None else ""
    except Exception:
        body = str(inp)
    return f"{name}|{body}"


def _recent_tool_uses(messages: list, limit: int) -> list[tuple[str, object, str | None]]:
    """Trailing tool_use blocks as (name, input, file_path)."""
    uses: list[tuple[str, object, str | None]] = []
    for msg in reversed(messages):
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for b in content:
            if isinstance(b, dict) and b.get("type") == "tool_use":
                name = (b.get("name") or "").lower()
                inp = b.get("input") or {}
                path = _extract_file_path(b)
                uses.append((name, inp, path))
        if len(uses) >= limit:
            break
    return list(reversed(uses))


def _trailing_repeat_count(messages: list) -> tuple[int, str]:
    """How many consecutive trailing tool_uses share the same fingerprint."""
    fps: list[str] = []
    for msg in messages:
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for b in content:
            if isinstance(b, dict) and b.get("type") == "tool_use":
                fps.append(_fingerprint(b.get("name") or "", b.get("input")))
    if not fps:
        return 0, ""
    last = fps[-1]
    n = 0
    for fp in reversed(fps):
        if fp != last:
            break
        n += 1
    return n, last


def _detect_decompress(messages: list) -> bool:
    """Same file read or identical search >= DECOMPRESS_REPEAT in last DECOMPRESS_WINDOW uses."""
    recent = _recent_tool_uses(messages, DECOMPRESS_WINDOW)
    if len(recent) < DECOMPRESS_REPEAT:
        return False

    read_paths = [path for name, _, path in recent if name in _READ_TOOLS and path]
    if read_paths:
        from collections import Counter

        if any(c >= DECOMPRESS_REPEAT for c in Counter(read_paths).values()):
            return True

    search_fps = [
        _fingerprint(name, inp)
        for name, inp, _ in recent
        if name in _SEARCH_TOOLS
    ]
    if len(search_fps) >= DECOMPRESS_REPEAT:
        from collections import Counter

        if any(c >= DECOMPRESS_REPEAT for c in Counter(search_fps).values()):
            return True
    return False


def _inject_nudge(payload: dict) -> bool:
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        return False
    last = messages[-1]
    if not isinstance(last, dict) or last.get("role") != "user":
        return False
    content = last.get("content")
    if isinstance(content, str):
        if _NUDGE in content:
            return False
        last["content"] = content + "\n\n" + _NUDGE
        return True
    if isinstance(content, list):
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text" and _NUDGE in (b.get("text") or ""):
                return False
        content = list(content)
        content.append({"type": "text", "text": _NUDGE})
        last["content"] = content
        return True
    return False


def observe(payload: dict, session_key: str | None) -> list[str]:
    """Update relax/decompress flags from history; inject soft nudge once."""
    cats: list[str] = []
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return cats

    if is_decompress(session_key):
        cats.append("loop_decompress")
        return cats

    if is_relaxed(session_key):
        cats.append("loop_relax")
        return cats

    if _detect_decompress(messages):
        if session_key:
            with _lock:
                _DECOMPRESS[session_key] = DECOMPRESS_TURNS
        cats.append("loop_decompress")
        return cats

    n, _fp = _trailing_repeat_count(messages)
    if n < LOOP_REPEAT or not session_key:
        return cats

    with _lock:
        _RELAXED[session_key] = True
    cats.append("loop_relax")
    if _inject_nudge(payload):
        cats.append("loop_nudge")
    return cats
