# Copyright (c) 2026 Sealeria
# SPDX-License-Identifier: AGPL-3.0-or-later
# Licensed under the GNU Affero General Public License v3.0

"""Max-savings pipeline tuned for Claude prompt-cache safety.

Design rules:
- Transforms on tools/system/tool_results are pure functions of content.
  Growing history must not rewrite earlier forwarded bytes (otherwise Claude
  bills a full cache miss).
- Position-based "keep last N verbatim" is forbidden — that rewrites a message
  the moment it falls out of the tail.
- Stale stubs / file-deltas intentionally rewrite one older slot once when a
  newer result appears; after that the stub is stable.
- Quality: keep errors/signals; CCR stores originals for retrieve.
"""

from __future__ import annotations

import hashlib
import json
import re

from engines import ccr
from engines.error_guard import (
    STACKTRACE_CEILING,
    is_error_payload,
    preserve_diagnostic_text,
    preserve_stacktrace_blocks,
)
from engines.optimizer import (
    _ERROR_KEYWORDS,
    _NOISE_LINE,
    _content_text,
    _extract_file_path,
    _first_sentence,
    _truncate_head_tail_chars,
    optimize_text,
)

CCR_MIN_CHARS = 800
CCR_PREVIEW_CHARS = 180
# Single cap for ALL tool_results — must not depend on message index.
TOOL_RESULT_MAX = 220
STALE_STUB = "[STALE]"

_JSON_ARRAY_HINT = re.compile(r"^\s*\[")
_GREP_LINE = re.compile(r"^[^:\n]+:\d+")
# Soft trim only — do not gut Claude Code's operating instructions.
_SYSTEM_BOILERPLATE = re.compile(
    r"(?is)\n{0,2}(output format guidelines)[^\n]{0,200}(\n[^\n]{0,200}){0,4}"
)

# OpenAI-compat / Codex only — stronger than Claude-safe aggressive_trim_system.
_HARD_NOISE_BLOCKS = re.compile(
    r"(?is)"
    r"<recommended_plugins\b[^>]*>.*?</recommended_plugins>"
    r"|<apps_instructions\b[^>]*>.*?</apps_instructions>"
    r"|<skills_instructions\b[^>]*>.*?</skills_instructions>"
    r"|<permissions[_ ]instructions\b[^>]*>.*?</permissions[_ ]instructions>"
)
_HARD_MD_SECTIONS = re.compile(
    r"(?ims)^#{1,3}\s+(Instruction hierarchy|Critical instructions|"
    r"Output format|Communication|Tone|Safety|Refusal|Formatting)\b.*?"
    r"(?=^#{1,3}\s+|\Z)"
)


def aggressive_trim_system_hard(system):
    """Quota-stretch trim for Mistral/Vibe system prompts (not Codex OS)."""
    if isinstance(system, str):
        text = optimize_text(system)
        text = _SYSTEM_BOILERPLATE.sub("\n", text)
        text = _HARD_NOISE_BLOCKS.sub("\n[omitted]\n", text)
        text = _HARD_MD_SECTIONS.sub("\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        if len(text) > 4500:
            text = text[:2000] + "\n…[system trimmed]…\n" + text[-1500:]
        elif len(text) > 2800:
            text = text[:1400] + "\n…[system trimmed]…\n" + text[-900:]
        return text if text else system
    if isinstance(system, list):
        out = []
        for block in system:
            if isinstance(block, dict) and block.get("type") in ("text", "input_text"):
                original = block.get("text", "")
                trimmed = aggressive_trim_system_hard(original)
                if not isinstance(trimmed, str) or not trimmed.strip():
                    if original.strip():
                        block = dict(block)
                        block["text"] = original
                        out.append(block)
                    continue
                block = dict(block)
                block["text"] = trimmed
                out.append(block)
            else:
                out.append(block)
        return out if out else system
    return system



def aggressive_trim_codex_developer(text: str) -> str:
    """Strip Codex noise blocks but keep agent operating instructions intact."""
    if not isinstance(text, str) or len(text) < 400:
        return text
    out = optimize_text(text)
    out = _HARD_NOISE_BLOCKS.sub("\n[omitted]\n", out)
    out = re.sub(r"\n{3,}", "\n\n", out).strip()
    return out if out else text


def _crush_log_lines(text: str) -> str:
    if is_error_payload(text):
        return preserve_stacktrace_blocks(text, ceiling=STACKTRACE_CEILING)
    lines = [l for l in text.split("\n") if not _NOISE_LINE.match(l)]
    if len(lines) <= 24:
        return "\n".join(lines)
    keep = set(range(0, 4)) | set(range(len(lines) - 6, len(lines)))
    for i, line in enumerate(lines):
        if _ERROR_KEYWORDS.search(line):
            keep.add(i)
            keep.update(range(max(0, i - 1), min(len(lines), i + 2)))
    out = []
    i = 0
    while i < len(lines):
        if i in keep:
            out.append(lines[i])
            i += 1
        else:
            start = i
            while i < len(lines) and i not in keep:
                i += 1
            out.append(f"[…{i - start} lines crushed…]")
    return "\n".join(out)


def _crush_json_array(text: str) -> str:
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return text
    if not isinstance(data, list) or len(data) <= 12:
        return text
    head, tail, sample = 3, 3, 4
    middle = data[head:-tail] if len(data) > head + tail else []
    step = max(1, len(middle) // sample) if middle else 1
    sampled = middle[::step][:sample]
    view = {
        "_n": len(data),
        "_head": data[:head],
        "_sample": sampled,
        "_tail": data[-tail:],
    }
    return json.dumps(view, ensure_ascii=False, separators=(",", ":"))


def _crush_grepish(text: str) -> str:
    lines = text.split("\n")
    hits = [l for l in lines if _GREP_LINE.match(l) or _ERROR_KEYWORDS.search(l)]
    if len(hits) < 8 or len(hits) >= len(lines) * 0.8:
        return text
    if len(hits) <= 40:
        return "\n".join(hits) + f"\n[{len(lines) - len(hits)} non-match lines omitted]"
    return (
        "\n".join(hits[:20])
        + f"\n[…{len(hits) - 40} matches omitted…]\n"
        + "\n".join(hits[-20:])
        + f"\n[{len(lines) - len(hits)} non-match lines omitted]"
    )


def crush_text(text: str) -> str:
    if not text or len(text) < 400:
        return text
    if is_error_payload(text):
        return preserve_diagnostic_text(text, ceiling=STACKTRACE_CEILING)
    # Fold repeated log lines before coarser crushers (lossless template fold).
    from engines.extras import fold_repeated_log_lines

    folded = fold_repeated_log_lines(text)
    if folded != text:
        text = folded
    stripped = text.strip()
    if _JSON_ARRAY_HINT.match(stripped) and stripped.endswith("]"):
        crushed = _crush_json_array(stripped)
        if crushed != stripped:
            return crushed
    if sum(1 for l in text.split("\n") if _GREP_LINE.match(l)) >= 8:
        return _crush_grepish(text)
    if len(text.split("\n")) > 30:
        return _crush_log_lines(text)
    return text


def _ccr_wrap(text: str) -> str:
    """Pure: same text always → same marker/crush (CCR hash is sha256 of text)."""
    if not text or len(text) < 400:
        return text
    if is_error_payload(text):
        return preserve_diagnostic_text(text, ceiling=STACKTRACE_CEILING)
    crushed = crush_text(text)
    if len(text) < CCR_MIN_CHARS:
        return _truncate_head_tail_chars(crushed, TOOL_RESULT_MAX)
    if len(crushed) < CCR_MIN_CHARS:
        return _truncate_head_tail_chars(crushed, TOOL_RESULT_MAX)
    h = ccr.store(text)
    preview = crushed[:CCR_PREVIEW_CHARS]
    return _truncate_head_tail_chars(ccr.marker(h, len(text), preview), TOOL_RESULT_MAX)


def _map_tool_result_content(content, mapper):
    if isinstance(content, str):
        return mapper(content)
    if isinstance(content, list):
        out = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                b = dict(b)
                b["text"] = mapper(b.get("text", ""))
                out.append(b)
            else:
                out.append(b)
        return out
    return content


def apply_pure_tool_result_ccr(payload: dict, *, relax: bool = False) -> bool:
    """Crush tool_results via a content-pure function (cache-prefix safe).

    Live-zone / loop-relax results are left intact so agents keep smoke signal.
    """
    if relax:
        return False
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return False
    from engines import maxsave

    live = maxsave.live_zone_result_locs(messages)
    changed = False
    for mi, msg in enumerate(messages):
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for bi, block in enumerate(content):
            if not (isinstance(block, dict) and block.get("type") == "tool_result"):
                continue
            if (mi, bi) in live:
                continue
            if ccr.is_ccr_fulfilled(block):
                continue
            # Never rewrite CCR fulfill targets mid-flight
            before = _content_text(block.get("content"))
            if len(before) < 400:
                continue
            if is_error_payload(before, is_error_flag=bool(block.get("is_error"))):
                block["content"] = _map_tool_result_content(
                    block.get("content"),
                    lambda t: preserve_diagnostic_text(t, ceiling=STACKTRACE_CEILING),
                )
                if _content_text(block.get("content")) != before:
                    changed = True
                continue
            if before.startswith("[CCR hash=") or before.startswith("[STALE:"):
                continue
            if before.startswith("[FILE UNCHANGED:"):
                continue
            block["content"] = _map_tool_result_content(block.get("content"), _ccr_wrap)
            if _content_text(block.get("content")) != before:
                changed = True
    return changed


def apply_stale_tool_compaction(payload: dict) -> bool:
    """One-time rewrite of older same-path reads → stub; then stable."""
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return False

    pending = {}
    occurrences = []
    for mi, msg in enumerate(messages):
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        if msg.get("role") == "assistant":
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    path = _extract_file_path(block)
                    if path and block.get("id"):
                        pending[block["id"]] = path
        elif msg.get("role") == "user":
            for bi, block in enumerate(content):
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    path = pending.get(block.get("tool_use_id"))
                    if path:
                        occurrences.append((mi, bi, path))

    latest = {}
    for mi, bi, path in occurrences:
        latest[path] = (mi, bi)

    changed = False
    for mi, bi, path in occurrences:
        if latest.get(path) == (mi, bi):
            continue
        msg = messages[mi]
        content = list(msg["content"])
        block = dict(content[bi])
        if _content_text(block.get("content")) == STALE_STUB:
            continue
        block["content"] = STALE_STUB
        content[bi] = block
        msg["content"] = content
        changed = True
    return changed


def hard_minify_tools(tools):
    """Deterministic schema shrink — identical tools in → identical tools out."""
    if not isinstance(tools, list):
        return tools

    def strip_schema(node):
        if not isinstance(node, dict):
            return node
        out = {}
        for k, v in node.items():
            if k in ("description", "title", "examples", "default", "$schema", "$defs"):
                continue
            if k == "properties" and isinstance(v, dict):
                out[k] = {pk: strip_schema(pv) for pk, pv in v.items()}
            elif k == "items" and isinstance(v, dict):
                out[k] = strip_schema(v)
            else:
                out[k] = v
        return out

    out = []
    for tool in tools:
        if not isinstance(tool, dict):
            out.append(tool)
            continue
        t = dict(tool)
        if t.get("name") == ccr.RETRIEVE_TOOL_NAME:
            out.append(t)
            continue
        if isinstance(t.get("description"), str):
            t["description"] = _first_sentence(t["description"])[:48]
        if isinstance(t.get("input_schema"), dict):
            t["input_schema"] = strip_schema(t["input_schema"])
        # Drop non-structural schema noise (examples/defaults/$defs titles).
        for junk in ("title", "examples", "$schema", "$defs", "default"):
            t.pop(junk, None)
        out.append(t)
    return out


def aggressive_trim_system(system):
    """Light deterministic cleanup — prefer quality over gutting the system prompt."""
    if isinstance(system, str):
        text = optimize_text(system)
        text = _SYSTEM_BOILERPLATE.sub("\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        # Only extreme runaway system prompts get head/tail (still pure).
        if len(text) > 20000:
            text = text[:8000] + "\n…[system trimmed]…\n" + text[-6000:]
        return text if text else system
    if isinstance(system, list):
        out = []
        for block in system:
            if isinstance(block, dict) and block.get("type") in ("text", "input_text"):
                original = block.get("text", "")
                trimmed = aggressive_trim_system(original)
                if not isinstance(trimmed, str) or not trimmed.strip():
                    if original.strip():
                        block = dict(block)
                        block["text"] = original
                        out.append(block)
                    continue
                block = dict(block)
                block["text"] = trimmed
                out.append(block)
            else:
                out.append(block)
        return out if out else system
    return system


def hard_minify_codex_tools(tools):
    """Mild Codex tool minify — never gut `exec` (tool-use loops otherwise)."""
    if not isinstance(tools, list):
        return tools

    def strip_schema(node):
        if not isinstance(node, dict):
            return node
        out = {}
        for k, v in node.items():
            if k in ("description", "title", "examples", "default", "$schema", "$defs"):
                continue
            if k == "properties" and isinstance(v, dict):
                out[k] = {pk: strip_schema(pv) for pk, pv in v.items()}
            elif k == "items" and isinstance(v, dict):
                out[k] = strip_schema(v)
            else:
                out[k] = v
        return out

    def minify_one(tool):
        if not isinstance(tool, dict):
            return tool
        t = dict(tool)
        name = t.get("name")
        if name == "exec" and isinstance(t.get("description"), str):
            # Keep enough orchestration docs for tool-use; trim the novel-length dump.
            if len(t["description"]) > 3500:
                t["description"] = t["description"][:2800] + "\n…[trimmed]…"
        elif isinstance(t.get("description"), str):
            t["description"] = _first_sentence(t["description"])[:120]
        fn = t.get("function")
        if isinstance(fn, dict):
            fn = dict(fn)
            if isinstance(fn.get("description"), str):
                fn["description"] = _first_sentence(fn["description"])[:120]
            if isinstance(fn.get("parameters"), dict):
                fn["parameters"] = strip_schema(fn["parameters"])
            t["function"] = fn
        if isinstance(t.get("parameters"), dict):
            t["parameters"] = strip_schema(t["parameters"])
        if isinstance(t.get("tools"), list):
            t["tools"] = [minify_one(x) for x in t["tools"]]
        return t

    return [minify_one(tool) for tool in tools]


def hard_minify_openai_tools(tools):
    """Shrink OpenAI/Mistral/Codex tools (function + nested namespace/custom trees)."""
    if not isinstance(tools, list):
        return tools

    def strip_schema(node):
        if not isinstance(node, dict):
            return node
        out = {}
        for k, v in node.items():
            if k in ("description", "title", "examples", "default", "$schema", "$defs"):
                continue
            if k == "properties" and isinstance(v, dict):
                out[k] = {pk: strip_schema(pv) for pk, pv in v.items()}
            elif k == "items" and isinstance(v, dict):
                out[k] = strip_schema(v)
            else:
                out[k] = v
        return out

    def minify_one(tool):
        if not isinstance(tool, dict):
            return tool
        t = dict(tool)
        fn = t.get("function")
        if isinstance(fn, dict):
            fn = dict(fn)
            if fn.get("name") == ccr.RETRIEVE_TOOL_NAME:
                return t
            if isinstance(fn.get("description"), str):
                fn["description"] = _first_sentence(fn["description"])[:48]
            if isinstance(fn.get("parameters"), dict):
                fn["parameters"] = strip_schema(fn["parameters"])
            t["function"] = fn
        if t.get("name") == ccr.RETRIEVE_TOOL_NAME:
            return t
        if isinstance(t.get("description"), str):
            t["description"] = _first_sentence(t["description"])[:48]
        if isinstance(t.get("parameters"), dict):
            t["parameters"] = strip_schema(t["parameters"])
        if isinstance(t.get("input_schema"), dict):
            t["input_schema"] = strip_schema(t["input_schema"])
        if isinstance(t.get("tools"), list):
            t["tools"] = [minify_one(x) for x in t["tools"]]
        for junk in ("title", "examples", "$schema", "$defs", "default"):
            t.pop(junk, None)
        return t

    return [minify_one(tool) for tool in tools]



# Keep newest N OpenAI tool-role messages intact (after crush); older → micro stub.
_OPENAI_KEEP_TOOLS = 4
_OPENAI_TOOL_MAX = 160
_OPENAI_DUP = "[dup]"
_OPENAI_STUB = "·"


def apply_aggressive_openai(payload: dict, session_key: str | None = None) -> list:
    """Aggressive crush for OpenAI-compat chat (Mistral/Vibe/Codex API-key)."""
    cats: list[str] = []
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return cats

    from engines import maxsave

    tool_idxs = [
        i
        for i, m in enumerate(messages)
        if isinstance(m, dict) and m.get("role") == "tool"
    ]
    live = set(tool_idxs[-_OPENAI_KEEP_TOOLS:]) if tool_idxs else set()
    seen: set[str] = set()

    for i, msg in enumerate(messages):
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role == "system":
            before = msg.get("content")
            if isinstance(before, str) and len(before) > 200:
                trimmed = aggressive_trim_system_hard(before)
                if isinstance(trimmed, str) and trimmed != before:
                    msg["content"] = trimmed
                    cats.append("system_trim")
            continue
        if role != "tool":
            content = msg.get("content")
            if not isinstance(content, str):
                continue
            # User pastes + long assistant turns (history bloat).
            if role == "user" and len(content) > 1200:
                crushed = maxsave.preprocess_tool_text(content)
                crushed = crush_text(crushed)
                if len(crushed) > 900:
                    crushed = _truncate_head_tail_chars(crushed, 800)
                if crushed != content:
                    msg["content"] = crushed
                    cats.append("user_crush")
            elif role == "assistant" and len(content) > 1500:
                crushed = crush_text(content)
                if len(crushed) > 1000:
                    crushed = _truncate_head_tail_chars(crushed, 900)
                if crushed != content:
                    msg["content"] = crushed
                    cats.append("assistant_crush")
            continue

        content = msg.get("content")
        if not isinstance(content, str) or len(content) < 60:
            continue
        if content in (_OPENAI_STUB, _OPENAI_DUP) or content.startswith("[CCR"):
            continue

        digest = hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()[:16]
        if digest in seen:
            msg["content"] = _OPENAI_DUP
            cats.append("tool_dedup")
            continue
        seen.add(digest)

        if i not in live:
            if is_error_payload(content):
                crushed = preserve_diagnostic_text(content, ceiling=STACKTRACE_CEILING)
                if crushed != content:
                    msg["content"] = crushed
                    cats.append("error_preserve")
                continue
            msg["content"] = _OPENAI_STUB
            cats.append("age_elide")
            continue

        crushed = maxsave.preprocess_tool_text(content)
        if is_error_payload(crushed):
            crushed = preserve_diagnostic_text(crushed, ceiling=STACKTRACE_CEILING)
        else:
            crushed = crush_text(crushed)
        if len(content) >= CCR_MIN_CHARS and len(crushed) >= 400:
            h = ccr.store(content)
            crushed = _truncate_head_tail_chars(
                ccr.marker(h, len(content), crushed[:CCR_PREVIEW_CHARS]),
                _OPENAI_TOOL_MAX,
            )
            cats.append("ccr")
        elif len(crushed) > _OPENAI_TOOL_MAX:
            crushed = _truncate_head_tail_chars(crushed, _OPENAI_TOOL_MAX)
            cats.append("tool_crush")
        elif crushed != content:
            cats.append("tool_crush")
        if crushed != content:
            msg["content"] = crushed

    if isinstance(payload.get("tools"), list) and payload["tools"]:
        payload["tools"] = hard_minify_openai_tools(payload["tools"])
        # Skip CCR retrieve inject — adds tokens; Mistral/Codex rarely call it.
        cats.append("hard_tool_minify")

    # Deduplicate category tags while preserving order
    return list(dict.fromkeys(cats))


def _inject_openai_retrieve_tool(tools: list) -> list:
    if any(
        isinstance(t, dict)
        and (
            t.get("name") == ccr.RETRIEVE_TOOL_NAME
            or (
                isinstance(t.get("function"), dict)
                and t["function"].get("name") == ccr.RETRIEVE_TOOL_NAME
            )
        )
        for t in tools
    ):
        return tools
    return list(tools) + [
        {
            "type": "function",
            "function": {
                "name": ccr.RETRIEVE_TOOL_NAME,
                "description": ccr.RETRIEVE_TOOL["description"][:120],
                "parameters": ccr.RETRIEVE_TOOL["input_schema"],
            },
        }
    ]


def _crush_responses_output_text(text: str) -> str:
    if not isinstance(text, str) or len(text) < 80:
        return text
    from engines import maxsave

    crushed = maxsave.preprocess_tool_text(text)
    if is_error_payload(crushed):
        return preserve_diagnostic_text(crushed, ceiling=STACKTRACE_CEILING)
    crushed = crush_text(crushed)
    if len(text) >= CCR_MIN_CHARS and len(crushed) >= 400:
        h = ccr.store(text)
        return _truncate_head_tail_chars(
            ccr.marker(h, len(text), crushed[:CCR_PREVIEW_CHARS]),
            _OPENAI_TOOL_MAX,
        )
    if len(crushed) > _OPENAI_TOOL_MAX:
        return _truncate_head_tail_chars(crushed, _OPENAI_TOOL_MAX)
    return crushed


def apply_aggressive_responses(payload: dict, session_key: str | None = None) -> list:
    """Aggressive crush for OpenAI/Codex Responses API (input[] items)."""
    cats: list[str] = []
    instructions = payload.get("instructions")
    if isinstance(instructions, str) and len(instructions) > 400:
        trimmed = aggressive_trim_system_hard(instructions)
        if isinstance(trimmed, str) and trimmed != instructions:
            payload["instructions"] = trimmed
            cats.append("system_trim")

    raw_input = payload.get("input")
    if isinstance(raw_input, str) and len(raw_input) > 1200:
        crushed = _crush_responses_output_text(raw_input)
        if len(crushed) > 900:
            crushed = _truncate_head_tail_chars(crushed, 800)
        if crushed != raw_input:
            payload["input"] = crushed
            cats.append("user_crush")
        return list(dict.fromkeys(cats))

    if not isinstance(raw_input, list):
        if isinstance(payload.get("tools"), list) and payload["tools"]:
            payload["tools"] = hard_minify_openai_tools(payload["tools"])
            cats.append("hard_tool_minify")
        return list(dict.fromkeys(cats))

    # Collect tool/function outputs for age-elide + dedup
    tool_idxs: list[int] = []
    for i, item in enumerate(raw_input):
        if not isinstance(item, dict):
            continue
        t = item.get("type")
        if t in ("function_call_output", "tool_result", "computer_call_output"):
            tool_idxs.append(i)
        elif item.get("role") == "tool":
            tool_idxs.append(i)
    live = set(tool_idxs[-_OPENAI_KEEP_TOOLS:]) if tool_idxs else set()
    seen: set[str] = set()

    for i, item in enumerate(raw_input):
        if not isinstance(item, dict):
            continue
        t = item.get("type")
        # function_call_output.output / computer_call_output.output
        if t in ("function_call_output", "computer_call_output", "tool_result"):
            out = item.get("output")
            if not isinstance(out, str):
                # sometimes list of content parts
                if isinstance(out, list):
                    for part in out:
                        if isinstance(part, dict) and isinstance(part.get("text"), str):
                            before = part["text"]
                            if len(before) < 60:
                                continue
                            digest = hashlib.sha256(
                                before.encode("utf-8", errors="replace")
                            ).hexdigest()[:16]
                            if digest in seen or i not in live:
                                part["text"] = _OPENAI_DUP if digest in seen else _OPENAI_STUB
                                if digest in seen:
                                    cats.append("tool_dedup")
                                else:
                                    cats.append("age_elide")
                                continue
                            seen.add(digest)
                            crushed = _crush_responses_output_text(before)
                            if crushed != before:
                                part["text"] = crushed
                                cats.append("tool_crush")
                continue
            if len(out) < 60:
                continue
            digest = hashlib.sha256(out.encode("utf-8", errors="replace")).hexdigest()[:16]
            if digest in seen:
                item["output"] = _OPENAI_DUP
                cats.append("tool_dedup")
                continue
            seen.add(digest)
            if i not in live:
                if is_error_payload(out):
                    crushed = preserve_diagnostic_text(out, ceiling=STACKTRACE_CEILING)
                    if crushed != out:
                        item["output"] = crushed
                        cats.append("error_preserve")
                    continue
                item["output"] = _OPENAI_STUB
                cats.append("age_elide")
                continue
            crushed = _crush_responses_output_text(out)
            if crushed != out:
                item["output"] = crushed
                cats.append("tool_crush")
            continue

        if item.get("role") == "tool" and isinstance(item.get("content"), str):
            content = item["content"]
            if len(content) < 60:
                continue
            digest = hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()[:16]
            if digest in seen:
                item["content"] = _OPENAI_DUP
                cats.append("tool_dedup")
                continue
            seen.add(digest)
            if i not in live:
                if is_error_payload(content):
                    crushed = preserve_diagnostic_text(content, ceiling=STACKTRACE_CEILING)
                    if crushed != content:
                        item["content"] = crushed
                        cats.append("error_preserve")
                    continue
                item["content"] = _OPENAI_STUB
                cats.append("age_elide")
                continue
            crushed = _crush_responses_output_text(content)
            if crushed != content:
                item["content"] = crushed
                cats.append("tool_crush")
            continue

        # User / env blobs in input (plugins, cwd dumps, pastes)
        if item.get("role") == "user":
            content = item.get("content")
            if isinstance(content, str) and len(content) > 1200:
                crushed = _crush_responses_output_text(content)
                if len(crushed) > 900:
                    crushed = _truncate_head_tail_chars(crushed, 800)
                if crushed != content:
                    item["content"] = crushed
                    cats.append("user_crush")
            elif isinstance(content, list):
                for part in content:
                    if (
                        isinstance(part, dict)
                        and part.get("type") in ("input_text", "text")
                        and isinstance(part.get("text"), str)
                        and len(part["text"]) > 900
                    ):
                        before = part["text"]
                        # Drop recurring Codex plugin catalogs entirely.
                        if "<recommended_plugins" in before or "<apps_instructions" in before:
                            part["text"] = "[omitted]"
                            cats.append("user_crush")
                            continue
                        # Keep real user tasks intact; only truncate huge dumps.
                        if len(before) > 6000:
                            crushed = _truncate_head_tail_chars(before, 4000)
                            if crushed != before:
                                part["text"] = crushed
                                cats.append("user_crush")

    # Codex: tools + developer prompts live inside input[] (not top-level tools/instructions)
    for item in raw_input:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "additional_tools" and isinstance(item.get("tools"), list):
            before = json.dumps(item["tools"], separators=(",", ":"), ensure_ascii=False)
            item["tools"] = hard_minify_codex_tools(item["tools"])
            after = json.dumps(item["tools"], separators=(",", ":"), ensure_ascii=False)
            if after != before:
                cats.append("hard_tool_minify")
        if item.get("role") == "developer":
            content = item.get("content")
            if isinstance(content, str) and len(content) > 400:
                trimmed = aggressive_trim_codex_developer(content)
                if isinstance(trimmed, str) and trimmed != content:
                    item["content"] = trimmed
                    cats.append("system_trim")
            elif isinstance(content, list):
                for part in content:
                    if (
                        isinstance(part, dict)
                        and part.get("type") in ("input_text", "text")
                        and isinstance(part.get("text"), str)
                        and len(part["text"]) > 400
                    ):
                        before_txt = part["text"]
                        # Keep main Codex OS prompt; only strip skills/apps noise.
                        if before_txt.lstrip().startswith("You are Codex"):
                            trimmed = aggressive_trim_codex_developer(before_txt)
                        else:
                            trimmed = aggressive_trim_codex_developer(before_txt)
                        if isinstance(trimmed, str) and trimmed != before_txt:
                            part["text"] = trimmed
                            cats.append("system_trim")

    if isinstance(payload.get("tools"), list) and payload["tools"]:
        payload["tools"] = hard_minify_openai_tools(payload["tools"])
        cats.append("hard_tool_minify")

    return list(dict.fromkeys(cats))


def apply_aggressive(payload: dict, session_key: str | None = None) -> list:
    cats = []
    if ccr.fulfill_retrieve_tool_results(payload.get("messages") or []):
        cats.append("ccr_fulfill")

    from engines import loopwatch, maxsave

    cats.extend(loopwatch.observe(payload, session_key))
    relax = loopwatch.is_relaxed(session_key) or loopwatch.is_decompress(session_key)

    if loopwatch.is_decompress(session_key):
        cats.extend(maxsave.apply_maxsave(payload, lossless_only=True))
        cats.append("loop_decompress")
        ccr.clear_ccr_fulfilled_flags(payload.get("messages") or [])
        return cats

    # Preprocess before CCR so crushed form hashes/stores cleaner.
    cats.extend(maxsave.apply_maxsave(payload))

    if apply_stale_tool_compaction(payload):
        cats.append("stale_compaction")

    if apply_pure_tool_result_ccr(payload, relax=relax):
        cats.append("ccr")

    # Age-elide after CCR so spill/retrieve still works for crushed bodies.
    cats.extend(maxsave.apply_maxsave_late(payload, relax=relax))

    if isinstance(payload.get("tools"), list):
        payload["tools"] = hard_minify_tools(payload["tools"])
        payload["tools"] = ccr.inject_retrieve_tool(payload["tools"])
        cats.append("hard_tool_minify")

    if "system" in payload:
        before = _content_text(payload["system"])
        payload["system"] = aggressive_trim_system(payload["system"])
        if _content_text(payload["system"]) != before:
            cats.append("system_trim")

    from engines import extras

    cats.extend(extras.apply_cache_and_structure_fixes(payload))
    # Re-run unused-tool pass after defer so mid-session schemas stay tiny.
    if maxsave.drop_unused_tool_schemas(payload) and "unused_tools" not in cats:
        cats.append("unused_tools")
    maxsave.drop_empty_text_blocks(payload)
    ccr.clear_ccr_fulfilled_flags(payload.get("messages") or [])

    return cats
