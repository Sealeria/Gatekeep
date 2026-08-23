# Copyright (c) 2026 Sealeria
# SPDX-License-Identifier: AGPL-3.0-or-later
# Licensed under the GNU Affero General Public License v3.0

"""Extra pure token-count reducers (no LLM, no quality-critical signal drop).

Cache rule: one-way stubs only — once shortened, never restored.
"""

from __future__ import annotations

import hashlib
import json
import re

from engines.optimizer import _content_text
from engines.error_guard import is_error_payload
from engines import ccr

# Age-out: keep this many newest tool_result blocks full (after crush); older → micro stub.
# Live zone is larger so smoke/Bash output stays visible and agents don't re-loop.
KEEP_RECENT_TOOL_RESULTS = 16
# Last N tool_results skip CCR entirely (light preprocess only via early maxsave).
LIVE_ZONE_SKIP_CCR = 12
MICRO_STUB = "·"
DUP_STUB = "[dup]"
ELIDED_INPUT = {}
_BASH_NAMES = frozenset({"Bash", "bash", "Shell", "shell", "BashTool"})
_FILE_RW_NAMES = frozenset(
    {
        "read",
        "read_file",
        "Read",
        "view",
        "view_file",
        "Write",
        "write",
        "Edit",
        "edit",
        "cat",
        "get_file_contents",
    }
)

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]|\x1b\].*?\x07|\r")
_PROGRESS = re.compile(
    r"(?im)^.*("
    r"\d+%|"
    r"eta\s|\belapsed\b|"
    r"[⠀-⣿]{3,}|"  # braille spinners
    r"[=-]{3,}>|"
    r"downloading|extracting|resolving|fetching packages"
    r").*$"
)
_BLANK_RUN = re.compile(r"\n{3,}")
_TRAIL_WS = re.compile(r"[ \t]+\n")
_BASE64_BLOB = re.compile(r"(?:data:[^;]+;base64,)?[A-Za-z0-9+/]{400,}={0,2}")


def strip_ansi(text: str) -> str:
    if not text or "\x1b" not in text and "\r" not in text:
        return text
    return _ANSI.sub("", text)


def squeeze_ws(text: str) -> str:
    if not text:
        return text
    text = _TRAIL_WS.sub("\n", text)
    text = _BLANK_RUN.sub("\n\n", text)
    return text.strip() if text.endswith("\n") or text.startswith("\n") else text


def strip_base64_blobs(text: str) -> str:
    if not text or len(text) < 400:
        return text
    return _BASE64_BLOB.sub("[b64]", text)


def drop_progress_lines(text: str) -> str:
    if not text or text.count("\n") < 6:
        return text
    lines = text.split("\n")
    kept = [l for l in lines if not _PROGRESS.match(l)]
    if len(kept) >= len(lines) - 2:
        return text
    return "\n".join(kept)


def compact_json_text(text: str) -> str:
    """minify JSON; uniform object-arrays → TOON-ish header/rows."""
    s = (text or "").strip()
    if len(s) < 80 or s[0] not in "{[":
        return text
    try:
        data = json.loads(s)
    except (ValueError, TypeError):
        return text

    if isinstance(data, list) and data and all(isinstance(x, dict) for x in data):
        keys = list(data[0].keys())
        if keys and all(list(x.keys()) == keys for x in data):
            def cell(v):
                if v is None:
                    return ""
                if isinstance(v, (dict, list)):
                    return json.dumps(v, ensure_ascii=False, separators=(",", ":"))
                return str(v).replace("|", "\\|").replace("\n", " ")

            rows = ["|".join(keys)] + ["|".join(cell(x.get(k)) for k in keys) for x in data]
            toon = f"[n={len(data)}]\n" + "\n".join(rows)
            compact = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
            return toon if len(toon) < len(compact) else compact

    try:
        compact = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return text
    return compact if len(compact) < len(s) else text


def crush_cli_tables(text: str) -> str:
    """Collapse wide whitespace-padded CLI tables (docker ps, kubectl, ls -l)."""
    if not text or text.count("\n") < 4:
        return text
    lines = text.split("\n")
    # Many lines with 2+ multi-space columns
    padded = sum(1 for l in lines if re.search(r"\S  +\S  +\S", l))
    if padded < max(4, len(lines) // 3):
        return text
    out = []
    for l in lines:
        out.append(re.sub(r"  +", "\t", l.strip()) if l.strip() else l)
    return "\n".join(out)


def preprocess_tool_text(text: str) -> str:
    if not text:
        return text
    before = text
    text = strip_ansi(text)
    text = strip_base64_blobs(text)
    text = drop_progress_lines(text)
    text = crush_cli_tables(text)
    text = compact_json_text(text)
    text = squeeze_ws(text)
    return text if text else before


def _map_text(content, mapper):
    if isinstance(content, str):
        return mapper(content)
    if isinstance(content, list):
        out = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                b = dict(b)
                b["text"] = mapper(b.get("text", ""))
                out.append(b)
            elif isinstance(b, dict) and b.get("type") in ("image", "document", "file"):
                out.append({"type": "text", "text": MICRO_STUB})
            else:
                out.append(b)
        return out
    return content


def apply_tool_result_preprocess(payload: dict) -> bool:
    messages = payload.get("messages")
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
            if ccr.is_ccr_fulfilled(block):
                continue
            before = _content_text(block.get("content"))
            if len(before) < 40 or before in (MICRO_STUB, DUP_STUB) or before.startswith("["):
                continue
            block["content"] = _map_text(block.get("content"), preprocess_tool_text)
            if _content_text(block.get("content")) != before:
                changed = True
    return changed


def apply_duplicate_tool_result_stubs(payload: dict) -> bool:
    """Identical tool_result bodies → micro stub after first occurrence (pure)."""
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return False
    seen: set[str] = set()
    changed = False
    for msg in messages:
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for i, block in enumerate(content):
            if not (isinstance(block, dict) and block.get("type") == "tool_result"):
                continue
            text = _content_text(block.get("content"))
            if len(text) < 80 or text in (MICRO_STUB, DUP_STUB):
                continue
            if text.startswith("[STALE") or text.startswith("[CCR") or text.startswith("[FILE"):
                continue
            h = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()
            if h in seen:
                content[i] = dict(block)
                content[i]["content"] = DUP_STUB
                changed = True
            else:
                seen.add(h)
        if changed:
            msg["content"] = content
    return changed


def _iter_tool_result_locs(messages: list) -> list[tuple[int, int]]:
    locs = []
    for mi, msg in enumerate(messages):
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for bi, block in enumerate(content):
            if isinstance(block, dict) and block.get("type") == "tool_result":
                locs.append((mi, bi))
    return locs


def live_zone_result_locs(messages: list) -> set[tuple[int, int]]:
    locs = _iter_tool_result_locs(messages)
    return set(locs[-LIVE_ZONE_SKIP_CCR:]) if locs else set()


def _tool_use_name_by_id(messages: list) -> dict[str, str]:
    names: dict[str, str] = {}
    for msg in messages:
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for b in content:
            if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("id"):
                names[b["id"]] = b.get("name") or ""
    return names


def _unresolved_error_locs(messages: list) -> set[tuple[int, int]]:
    """Tool results with errors that have no later successful tool execution."""
    locs = _iter_tool_result_locs(messages)
    protected: set[tuple[int, int]] = set()
    for idx, (mi, bi) in enumerate(locs):
        block = messages[mi]["content"][bi]
        text = _content_text(block.get("content"))
        if not is_error_payload(text, is_error_flag=bool(block.get("is_error"))):
            continue
        later_success = False
        for mi2, bi2 in locs[idx + 1 :]:
            block2 = messages[mi2]["content"][bi2]
            text2 = _content_text(block2.get("content"))
            if not is_error_payload(text2, is_error_flag=bool(block2.get("is_error"))):
                later_success = True
                break
        if not later_success:
            protected.add((mi, bi))
    return protected


def apply_age_elide(payload: dict, *, relax: bool = False) -> bool:
    """One-way: tool_results older than newest K → MICRO_STUB; matching tool_use inputs → {}."""
    if relax:
        return False
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return False
    locs = _iter_tool_result_locs(messages)
    if len(locs) <= KEEP_RECENT_TOOL_RESULTS:
        return False

    keep = set(locs[-KEEP_RECENT_TOOL_RESULTS:])
    names = _tool_use_name_by_id(messages)
    sticky_errors = _unresolved_error_locs(messages)
    # Extra: never elide Bash/Shell or recent file read/write in the broader live tail.
    live = set(locs[-max(KEEP_RECENT_TOOL_RESULTS, LIVE_ZONE_SKIP_CCR) :])
    elide_ids = set()
    changed = False
    for mi, bi in locs:
        if (mi, bi) in keep:
            continue
        if (mi, bi) in sticky_errors:
            continue
        block = messages[mi]["content"][bi]
        if ccr.is_ccr_fulfilled(block):
            continue
        tid = block.get("tool_use_id")
        tname = names.get(tid or "")
        if (mi, bi) in live and tname in _BASH_NAMES:
            continue
        if (mi, bi) in live and tname in _FILE_RW_NAMES:
            continue
        if tid:
            elide_ids.add(tid)
        text = _content_text(block.get("content"))
        if text == MICRO_STUB:
            continue
        content = list(messages[mi]["content"])
        nb = dict(content[bi])
        nb["content"] = MICRO_STUB
        content[bi] = nb
        messages[mi]["content"] = content
        changed = True

    if not elide_ids:
        return changed

    for msg in messages:
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        new_content = []
        local = False
        for block in content:
            if (
                isinstance(block, dict)
                and block.get("type") == "tool_use"
                and block.get("id") in elide_ids
            ):
                inp = block.get("input")
                if inp != ELIDED_INPUT and inp != {}:
                    block = dict(block)
                    block["input"] = dict(ELIDED_INPUT)
                    local = True
                    changed = True
            new_content.append(block)
        if local:
            msg["content"] = new_content
    return changed


def drop_unused_tool_schemas(payload: dict) -> bool:
    """Tools never referenced in tool_use → empty schema + tiny description."""
    tools = payload.get("tools")
    messages = payload.get("messages")
    if not isinstance(tools, list) or not isinstance(messages, list):
        return False
    used = set()
    for msg in messages:
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for b in content:
            if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name"):
                used.add(b["name"])
    if not used:
        return False  # first turns: defer_loading already handles overflow
    changed = False
    out = []
    for tool in tools:
        if not isinstance(tool, dict):
            out.append(tool)
            continue
        name = tool.get("name") or ""
        if name in used or name.endswith("ccr_retrieve") or "retrieve" in name.lower():
            out.append(tool)
            continue
        t = dict(tool)
        if isinstance(t.get("description"), str) and len(t["description"]) > 24:
            t["description"] = t["description"][:21] + "…"
            changed = True
        schema = t.get("input_schema")
        if isinstance(schema, dict) and schema.get("properties"):
            t["input_schema"] = {"type": "object", "properties": {}}
            changed = True
        t["defer_loading"] = True
        out.append(t)
    payload["tools"] = out
    return changed


def strip_git_instruction_bloat(system) -> tuple[object, bool]:
    """Remove known Claude Code git-workflow walls from system text (deterministic)."""
    patterns = [
        re.compile(
            r"(?is)\n{0,2}#?\s*Committing changes with git\b.*?(?=\n# |\n## |\n[A-Z][a-z].{20,}|\Z)"
        ),
        re.compile(
            r"(?is)\n{0,2}#?\s*Creating pull requests\b.*?(?=\n# |\n## |\Z)"
        ),
        re.compile(r"(?is)\n{0,2}IMPORTANT:.*?(?:git commit|HEREDOC).*?(?=\n\n|\Z)"),
    ]

    def scrub(text: str) -> tuple[str, bool]:
        if not text or len(text) < 400:
            return text, False
        original = text
        for pat in patterns:
            text = pat.sub("\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        if not text:
            return original, False
        return text, text != original

    if isinstance(system, str):
        return scrub(system)
    if isinstance(system, list):
        changed = False
        out = []
        for block in system:
            if isinstance(block, dict) and block.get("type") == "text":
                new_t, ch = scrub(block.get("text", ""))
                if ch:
                    block = dict(block)
                    block["text"] = new_t
                    changed = True
                if new_t.strip():
                    out.append(block)
            else:
                out.append(block)
        return (out if out else system), changed
    return system, False


_SYSTEM_REMINDER = re.compile(r"(?is)<system-reminder>.*?</system-reminder>")
_BASH_GIT_WALL = re.compile(
    r"(?is)#\s*Committing changes with git\b.*?(?=#\s*Creating pull requests|\Z)"
)
_BASH_PR_WALL = re.compile(
    r"(?is)#\s*Creating pull requests\b.*?(?=#\s*Other common operations|\Z)"
)
_BASH_OTHER_GH = re.compile(r"(?is)#\s*Other common operations\b.*")
_GIT_SAFETY_INLINE = re.compile(r"(?is)Git Safety Protocol:.*?(?=\n\n|\n# |\Z)")

BASH_GIT_STUB = (
    "Git: commit/push/PR only if user asks. No force, no config edits, "
    "no hook skips, no amend unless asked. Prefer named git add. HEREDOC messages."
)


def strip_system_reminders(text: str) -> str:
    if not text or "<system-reminder>" not in text.lower():
        return text
    out = _SYSTEM_REMINDER.sub("", text)
    return squeeze_ws(out) if out != text else text


def shrink_bash_git_bloat(text: str) -> str:
    if not text or "Committing changes with git" not in text:
        return text
    text = _BASH_GIT_WALL.sub("\n" + BASH_GIT_STUB + "\n", text)
    text = _BASH_PR_WALL.sub("\n", text)
    text = _BASH_OTHER_GH.sub("\n", text)
    text = _GIT_SAFETY_INLINE.sub("", text)
    return squeeze_ws(text)


def apply_message_reminder_strip(payload: dict) -> bool:
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return False
    changed = False

    def map_text(t: str) -> str:
        return strip_system_reminders(t)

    for msg in messages:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if isinstance(content, str):
            new = map_text(content)
            if new != content:
                msg["content"] = new
                changed = True
            continue
        if not isinstance(content, list):
            continue
        for i, block in enumerate(content):
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                new = map_text(block["text"])
                if new != block["text"]:
                    content[i] = dict(block)
                    content[i]["text"] = new
                    changed = True
            elif block.get("type") == "tool_result":
                before = _content_text(block.get("content"))
                mapped = _map_text(block.get("content"), map_text)
                if _content_text(mapped) != before:
                    content[i] = dict(block)
                    content[i]["content"] = mapped
                    changed = True
        if changed:
            msg["content"] = content
    return changed


def shrink_tool_description_walls(tools: list) -> bool:
    if not isinstance(tools, list):
        return False
    changed = False
    for i, tool in enumerate(tools):
        if not isinstance(tool, dict):
            continue
        desc = tool.get("description")
        if not isinstance(desc, str) or len(desc) < 200:
            continue
        new = shrink_bash_git_bloat(desc)
        # Drop long "example:" / "IMPORTANT:" protocol tails after first paragraph
        if len(new) > 400:
            parts = re.split(r"\n{2,}", new, maxsplit=1)
            if len(parts) == 2 and len(parts[1]) > 300:
                new = parts[0].strip()
        if new != desc:
            tools[i] = dict(tool)
            tools[i]["description"] = new
            changed = True
    return changed


def apply_maxsave(payload: dict, *, lossless_only: bool = False) -> list:
    """Early pass: lossless-ish preprocess + dedup (before CCR store)."""
    cats = []
    if not lossless_only:
        if apply_message_reminder_strip(payload):
            cats.append("reminder_strip")
        if isinstance(payload.get("tools"), list) and shrink_tool_description_walls(payload["tools"]):
            cats.append("git_trim")
    if apply_tool_result_preprocess(payload):
        cats.append("cli_toon")
    if not lossless_only:
        if apply_duplicate_tool_result_stubs(payload):
            cats.append("dup_stub")
        if "system" in payload:
            new_sys, ch = strip_git_instruction_bloat(payload["system"])
            if ch:
                payload["system"] = new_sys
                if "git_trim" not in cats:
                    cats.append("git_trim")
    return cats


def drop_empty_text_blocks(payload: dict) -> None:
    """Anthropic 400s on empty text blocks — scrub after aggressive transforms."""

    def clean_system(system):
        if isinstance(system, str):
            return system if system.strip() else " "
        if isinstance(system, list):
            out = []
            for block in system:
                if isinstance(block, dict) and block.get("type") == "text":
                    t = block.get("text") or ""
                    if not str(t).strip():
                        continue
                    out.append(block)
                else:
                    out.append(block)
            return out if out else system
        return system

    if "system" in payload:
        payload["system"] = clean_system(payload["system"])

    messages = payload.get("messages")
    if not isinstance(messages, list):
        return
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if isinstance(content, str):
            if not content.strip():
                msg["content"] = "."
            continue
        if not isinstance(content, list):
            continue
        new_blocks = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                t = block.get("text") or ""
                if not str(t).strip():
                    continue
                new_blocks.append(block)
            elif isinstance(block, dict) and block.get("type") == "tool_result":
                c = block.get("content")
                if isinstance(c, str) and not c.strip():
                    block = dict(block)
                    block["content"] = MICRO_STUB
                elif isinstance(c, list):
                    nb = []
                    for b in c:
                        if isinstance(b, dict) and b.get("type") == "text":
                            if not (b.get("text") or "").strip():
                                continue
                        nb.append(b)
                    block = dict(block)
                    block["content"] = nb if nb else MICRO_STUB
                new_blocks.append(block)
            else:
                new_blocks.append(block)
        if not new_blocks and msg.get("role") == "user":
            new_blocks = [{"type": "text", "text": "."}]
        msg["content"] = new_blocks


def apply_maxsave_late(payload: dict, *, relax: bool = False) -> list:
    """After CCR: age-elide + unused schemas (originals already spilled)."""
    cats = []
    if apply_age_elide(payload, relax=relax):
        cats.append("age_elide")
    if drop_unused_tool_schemas(payload):
        cats.append("unused_tools")
    drop_empty_text_blocks(payload)
    return cats
