# Copyright (c) 2026 Sealeria
# SPDX-License-Identifier: AGPL-3.0-or-later
# Licensed under the GNU Affero General Public License v3.0

import ast
import difflib
import hashlib
import json
import re
import threading

import tiktoken

try:
    from .error_guard import STACKTRACE_CEILING, is_error_payload, preserve_diagnostic_text
except ImportError:
    from error_guard import STACKTRACE_CEILING, is_error_payload, preserve_diagnostic_text

_ANTI_YAP = (
    "[SYSTEM DIRECTIVE: Output code and tool calls ONLY. "
    "Omit all preambles, greetings, and conversational wrap-ups.]"
)

_CODE_FENCE_SPLIT = re.compile(r"(```.*?```)", re.DOTALL)

_FILLER_PATTERNS = [
    re.compile(r"(?im)^[ \t]*(certainly|sure(?: thing)?|of course|great question|absolutely)!\s*"),
    re.compile(r"(?i)i hope (this|that) helps!?\.?"),
    re.compile(r"(?i)please let me know if you (have any questions|need (any )?(further )?(help|assistance)|need anything else)\.?"),
    re.compile(r"(?i)i'?d be happy to help( you)?( with (that|this))?!?\.?"),
    re.compile(r"(?i)feel free to (ask|reach out) if you (have|need) [^.\n]*\.?"),
    re.compile(r"(?i)thank you for (your patience|asking)!?\.?"),
]

# Phase 2.5: history pruning (sliding window over long multi-turn conversations)
HISTORY_WINDOW_THRESHOLD = 6
PRESERVE_TAIL_MESSAGES = 4
LARGE_CODE_BLOCK_MIN_LINES = 5
CODE_OMIT_PLACEHOLDER = "[Previous context/code omitted for brevity]"

LOG_TRUNCATE_MIN_LINES = 20
LOG_HEAD_LINES = 5
LOG_TAIL_LINES = 10
_ERROR_KEYWORDS = re.compile(r"(?i)\b(error|traceback|exception|failed)\b")
# ASCII/braille spinner frames and blank lines carry zero information —
# strip them before counting/keeping so they never eat a head/tail slot.
_NOISE_LINE = re.compile(r"^[\s\-\\/|.⠀-⣿]*$")

# Anthropic cache alignment: up to 4 breakpoints per Anthropic's API limit
MAX_CACHE_BREAKPOINTS = 4
CACHE_SYSTEM_CHAR_THRESHOLD = 3500
CACHE_HISTORY_DEPTH_THRESHOLD = 3

# Newest-turn aggressive tool_result truncation (the "mutable tail")
NEWEST_TOOL_RESULT_MAX_CHARS = 800

# Session file-read delta tracking
_FILE_READ_TOOL_NAMES = {"read", "read_file", "view", "view_file", "cat", "get_file_contents"}
_FILE_PATH_INPUT_KEYS = ("file_path", "path", "target_file", "filepath")
MAX_TRACKED_SESSIONS = 200

ANTI_YAP_MIN_CHARS = 500

# Exploratory tool-call chain squashing (within pruned history only)
_EXPLORATORY_TOOL_NAMES = {"glob", "grep", "ls", "list_dir", "list_files", "find", "search", "search_files"}
EXPLORATION_RUN_MIN = 3
EXPLORATION_MIN_RESULT_LINES = 40

# Dynamic max_tokens clamping for simple/short exchanges
MAX_TOKENS_CLAMP_TARGET = 2048
SIMPLE_QUERY_CHAR_THRESHOLD = 100

# Response cache
MAX_CACHED_RESPONSES = 500

# ponytail: flat rough estimate of the output tokens a "be concise" system instruction
# typically shaves off a reply (cut preamble/postscript) — not a measured value, we never
# see the model's actual response here.
ESTIMATED_ANTI_YAP_OUTPUT_TOKENS_SAVED = 60

try:
    _ENCODING = tiktoken.get_encoding("cl100k_base")
except Exception:
    _ENCODING = None


def count_tokens(text: str) -> int:
    if not text:
        return 0
    if _ENCODING is None:
        return len(text) // 4
    return len(_ENCODING.encode(text))


# Caveman NLP compression: SACROSANCT — \b word boundaries mean these never
# match inside identifiers (snake_case/camelCase) or file paths, since `_`
# and letters are all \w with no boundary between them. Logical-constraint
# words (not/no/never/if/else/false/true) are never in these patterns.
_INLINE_CODE_SPLIT = re.compile(r"(`[^`\n]+`)")
_DETERMINERS = re.compile(r"(?i)\b(a|an|the)\b ?")
_FILLER_PREAMBLE = re.compile(r"(?im)^[ \t]*(so|well|basically|essentially|actually|just|simply)\b[,:]?\s*")
_POLITE_AUX = re.compile(r"(?i)\b(could you please|would you please|can you please|could you|would you|can you)\b\s*")


def _caveman_words(text: str) -> str:
    text = _FILLER_PREAMBLE.sub("", text)
    text = _POLITE_AUX.sub("", text)
    text = _DETERMINERS.sub("", text)
    return text


def _clean_prose(text: str) -> str:
    for pattern in _FILLER_PATTERNS:
        text = pattern.sub("", text)
    segments = _INLINE_CODE_SPLIT.split(text)
    text = "".join(seg if i % 2 == 1 else _caveman_words(seg) for i, seg in enumerate(segments))
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text


def _clean_code_block(block: str) -> str:
    lines = [line.rstrip() for line in block.split("\n")]
    cleaned = []
    blank_run = 0
    for line in lines:
        if line == "":
            blank_run += 1
            if blank_run > 1:
                continue
        else:
            blank_run = 0
        cleaned.append(line)
    return "\n".join(cleaned)


def optimize_text(text: str) -> str:
    if not text:
        return text
    segments = _CODE_FENCE_SPLIT.split(text)
    out = [
        _clean_code_block(seg) if i % 2 == 1 else _clean_prose(seg)
        for i, seg in enumerate(segments)
    ]
    return "".join(out).strip()


def inject_anti_yap(system_prompt):
    if system_prompt is None:
        return _ANTI_YAP
    if isinstance(system_prompt, str):
        return f"{system_prompt}\n\n{_ANTI_YAP}"
    if isinstance(system_prompt, list):
        return system_prompt + [{"type": "text", "text": _ANTI_YAP}]
    return system_prompt


def _content_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = []
        for block in content:
            if isinstance(block, str):
                texts.append(block)
            elif isinstance(block, dict):
                btype = block.get("type")
                if btype in ("text", "input_text", "output_text") or (
                    isinstance(block.get("text"), str) and btype not in ("tool_use", "function_call")
                ):
                    if isinstance(block.get("text"), str):
                        texts.append(block["text"])
                elif btype == "tool_result" and "content" in block:
                    texts.append(_content_text(block["content"]))
        return "\n".join(texts)
    return ""


def _optimize_content(content):
    if isinstance(content, str):
        return optimize_text(content)
    if isinstance(content, list):
        new_blocks = []
        for block in content:
            if isinstance(block, dict) and block.get("type") in ("text", "input_text", "output_text"):
                block = dict(block)
                block["text"] = optimize_text(block.get("text", ""))
            new_blocks.append(block)
        return new_blocks
    return content


def _walk_schema(node, parts):
    if not isinstance(node, dict):
        return
    if isinstance(node.get("description"), str):
        parts.append(node["description"])
    properties = node.get("properties")
    if isinstance(properties, dict):
        for prop in properties.values():
            _walk_schema(prop, parts)
    _walk_schema(node.get("items"), parts)


def _tools_text(tools) -> str:
    if not isinstance(tools, list):
        return ""

    parts = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        if isinstance(tool.get("name"), str):
            parts.append(tool["name"])
        if isinstance(tool.get("description"), str):
            parts.append(tool["description"])
        _walk_schema(tool.get("input_schema"), parts)
        fn = tool.get("function")
        if isinstance(fn, dict):
            if isinstance(fn.get("name"), str):
                parts.append(fn["name"])
            if isinstance(fn.get("description"), str):
                parts.append(fn["description"])
            _walk_schema(fn.get("parameters"), parts)
    return "\n".join(parts)


def _tools_text_nested(tools) -> str:
    """Flatten Codex namespace/custom/function tool trees for token metrics."""
    if not isinstance(tools, list):
        return ""
    parts = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        parts.append(_tools_text([tool]))
        nested = tool.get("tools")
        if isinstance(nested, list):
            parts.append(_tools_text_nested(nested))
        # top-level function shape (name/description/parameters on tool itself)
        if isinstance(tool.get("parameters"), dict):
            _walk_schema(tool["parameters"], parts)
    return "\n".join(p for p in parts if p)


def extract_text(payload: dict) -> str:
    parts = []
    if "system" in payload:
        parts.append(_content_text(payload["system"]))
    for msg in payload.get("messages") or []:
        if isinstance(msg, dict):
            parts.append(_content_text(msg.get("content")))
    if isinstance(payload.get("prompt"), str):
        parts.append(payload["prompt"])
    # OpenAI Responses / Codex-ish
    inp = payload.get("input")
    if isinstance(inp, str):
        parts.append(inp)
    elif isinstance(inp, list):
        for item in inp:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                parts.append(_content_text(item.get("content")))
                if isinstance(item.get("text"), str):
                    parts.append(item["text"])
                if isinstance(item.get("output"), str):
                    parts.append(item["output"])
                elif isinstance(item.get("output"), list):
                    for part in item["output"]:
                        if isinstance(part, dict) and isinstance(part.get("text"), str):
                            parts.append(part["text"])
                if isinstance(item.get("instructions"), str):
                    parts.append(item["instructions"])
                # Codex ChatGPT: tools live under input[].type=additional_tools
                if item.get("type") == "additional_tools" and isinstance(item.get("tools"), list):
                    tools_text = _tools_text_nested(item["tools"])
                    if tools_text:
                        parts.append(tools_text)
    if isinstance(payload.get("instructions"), str):
        parts.append(payload["instructions"])
    tools_text = _tools_text(payload.get("tools"))
    if tools_text:
        parts.append(tools_text)
    return "\n".join(p for p in parts if p)



def measure_wire_tokens(body: bytes, payload: dict | None = None) -> tuple[int, str]:
    """BPE (or byte/4) estimate of request size for any provider — crush or not."""
    sample_cap = 4000
    sample = ""
    text = ""
    if isinstance(payload, dict):
        text = extract_text(payload)
        sample = text[:sample_cap] if text else ""
    if not text and body:
        text = body.decode("utf-8", errors="ignore")
        if not sample:
            sample = text[:sample_cap]
    if text:
        return count_tokens(text), sample
    if body:
        approx = max(1, len(body) // 4)
        return approx, f"[binary {len(body)}B ~{approx} tok]"
    return 0, ""



def _truncate_verbose_logs(text: str) -> str:
    if is_error_payload(text):
        return preserve_diagnostic_text(text, ceiling=STACKTRACE_CEILING)
    lines = [l for l in text.split("\n") if not _NOISE_LINE.match(l)]
    if len(lines) <= LOG_TRUNCATE_MIN_LINES:
        return "\n".join(lines)

    keep = set(range(0, LOG_HEAD_LINES)) | set(range(len(lines) - LOG_TAIL_LINES, len(lines)))
    for i, line in enumerate(lines):
        if _ERROR_KEYWORDS.search(line):
            keep.add(i)

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
            out.append(f"[... Truncated {i - start} lines of verbose logs ...]")
    return "\n".join(out)


_PYTHON_FENCE = re.compile(r"^```python\s*\n(.*)\n```$", re.DOTALL | re.IGNORECASE)


def _distill_python_signatures(code: str) -> str:
    """AST-based signature-only view of REFERENCE Python code (old history
    context, never the active/newest turn): keeps public class/function
    signatures with their type annotations, drops private (leading single
    underscore, non-dunder) helpers, docstrings, and all bodies.

    Python-only by design — a generic multi-language AST/tree-sitter parse
    is out of scope for a local proxy. Falls back to the untouched original
    on any parse failure or if `ast.unparse` isn't available (< Python 3.9);
    never guess at code we can't actually parse."""
    if not hasattr(ast, "unparse"):
        return code
    try:
        tree = ast.parse(code)
    except (SyntaxError, ValueError):
        return code

    def is_public(name: str) -> bool:
        return not name.startswith("_") or (name.startswith("__") and name.endswith("__"))

    def stub(node):
        node.body = [ast.Expr(value=ast.Constant(value=Ellipsis))]
        node.decorator_list = []

    kept = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and is_public(node.name):
            stub(node)
            kept.append(node)
        elif isinstance(node, ast.ClassDef):
            methods = [
                item for item in node.body
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and is_public(item.name)
            ]
            if methods:
                for m in methods:
                    stub(m)
                node.body = methods
                node.decorator_list = []
                kept.append(node)

    if not kept:
        return code
    try:
        module = ast.Module(body=kept, type_ignores=[])
        ast.fix_missing_locations(module)
        return ast.unparse(module)
    except Exception:
        return code


def _prune_middle_content(text: str, log_truncation_enabled: bool) -> str:
    if not text:
        return text
    segments = _CODE_FENCE_SPLIT.split(text)
    out = []
    for i, seg in enumerate(segments):
        lines = seg.split("\n")
        if i % 2 == 1:  # fenced code block, incl. the ``` fence lines
            py_match = _PYTHON_FENCE.match(seg)
            distilled = _distill_python_signatures(py_match.group(1)) if py_match else None
            if distilled and distilled.strip() and distilled != py_match.group(1):
                # Python reference code: signature stub always wins over crude
                # truncation, regardless of length — it's strictly more useful.
                out.append(f"```python\n{distilled}\n```")
            elif log_truncation_enabled and len(lines) > LOG_TRUNCATE_MIN_LINES:
                out.append(_truncate_verbose_logs(seg))
            elif len(lines) - 2 >= LARGE_CODE_BLOCK_MIN_LINES:
                out.append(CODE_OMIT_PLACEHOLDER)
            else:
                out.append(seg)
        else:  # prose — may itself be an unfenced log/stacktrace dump
            if log_truncation_enabled and len(lines) > LOG_TRUNCATE_MIN_LINES:
                out.append(_truncate_verbose_logs(seg))
            else:
                out.append(seg)
    return "".join(out)


def _prune_content_value(value, log_truncation_enabled: bool):
    if isinstance(value, str):
        return _prune_middle_content(value, log_truncation_enabled)
    if isinstance(value, list):
        return [_prune_content_block(b, log_truncation_enabled) for b in value]
    return value


def _prune_content_block(block, log_truncation_enabled: bool):
    # tool_use (pending tool call args) and any other block type pass through untouched.
    if not isinstance(block, dict):
        return block
    if block.get("type") == "text":
        block = dict(block)
        block["text"] = _prune_middle_content(block.get("text", ""), log_truncation_enabled)
        return block
    if block.get("type") == "tool_result" and "content" in block:
        block = dict(block)
        block["content"] = _prune_content_value(block["content"], log_truncation_enabled)
        return block
    return block


def _exploratory_tool_name(msg) -> str:
    """Name of the exploratory tool if `msg` is a single-block assistant
    tool_use for glob/grep/ls/find/etc, else ''."""
    if not isinstance(msg, dict) or msg.get("role") != "assistant":
        return ""
    content = msg.get("content")
    if not isinstance(content, list) or len(content) != 1:
        return ""
    block = content[0]
    if isinstance(block, dict) and block.get("type") == "tool_use":
        name = (block.get("name") or "").lower()
        if name in _EXPLORATORY_TOOL_NAMES:
            return name
    return ""


def _squash_exploratory_chains(messages: list) -> list:
    """Condense runs of >= EXPLORATION_RUN_MIN consecutive exploratory
    tool_use/tool_result pairs (glob/grep/ls/find chains an agent used to
    look around) down to a one-line marker each. Only ever rewrites a
    tool_result's *content*, in place — count, order, and every
    tool_use/tool_result id stay exactly as they were, so pairing can never
    break."""
    out = list(messages)
    n = len(out)
    i = 0
    while i < n - 1:
        run = []
        j = i
        while j < n - 1:
            name = _exploratory_tool_name(out[j])
            nxt = out[j + 1]
            if not name or not isinstance(nxt, dict) or nxt.get("role") != "user":
                break
            nxt_content = nxt.get("content")
            if not isinstance(nxt_content, list) or not nxt_content:
                break
            if not (isinstance(nxt_content[0], dict) and nxt_content[0].get("type") == "tool_result"):
                break
            run.append((j, j + 1, name))
            j += 2
        if len(run) >= EXPLORATION_RUN_MIN:
            for _use_i, res_i, name in run:
                result_msg = dict(out[res_i])
                blocks = list(result_msg["content"])
                block = dict(blocks[0])
                result_text = _content_text(block.get("content"))
                if result_text.count("\n") + 1 < EXPLORATION_MIN_RESULT_LINES:
                    continue
                marker = f"[Exploration: {name} — output condensed]"
                block["content"] = [{"type": "text", "text": marker}] if isinstance(block.get("content"), list) else marker
                blocks[0] = block
                result_msg["content"] = blocks
                out[res_i] = result_msg
            i = j
        else:
            i += 1
    return out


def prune_history(
    messages: list, log_truncation_enabled: bool = True, turn_squashing_enabled: bool = True
) -> list:
    """Sliding window: keep the first message + last N verbatim, shrink large
    code/log content in between. Never drops or reorders a message, so
    tool_use/tool_result pairing (which Anthropic requires to stay adjacent)
    is never broken."""
    if not isinstance(messages, list) or len(messages) <= HISTORY_WINDOW_THRESHOLD:
        return messages

    first = messages[0]
    tail = messages[-PRESERVE_TAIL_MESSAGES:]
    middle = messages[1:-PRESERVE_TAIL_MESSAGES]

    if turn_squashing_enabled:
        middle = _squash_exploratory_chains(middle)

    pruned_middle = []
    for msg in middle:
        if isinstance(msg, dict) and "content" in msg:
            msg = dict(msg)
            msg["content"] = _prune_content_value(msg["content"], log_truncation_enabled)
        pruned_middle.append(msg)

    return [first, *pruned_middle, *tail]


def canonical_json_dumps(data: dict) -> bytes:
    """Deterministic, whitespace-free serialization so identical payloads
    always produce identical bytes — required for Anthropic/OpenAI's
    byte-prefix cache matching to actually hit."""
    return json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _count_cache_breakpoints(payload: dict) -> int:
    count = 0
    system = payload.get("system")
    if isinstance(system, list):
        count += sum(1 for b in system if isinstance(b, dict) and "cache_control" in b)
    tools = payload.get("tools")
    if isinstance(tools, list):
        count += sum(1 for t in tools if isinstance(t, dict) and "cache_control" in t)
    for msg in payload.get("messages") or []:
        content = msg.get("content") if isinstance(msg, dict) else None
        if isinstance(content, list):
            count += sum(1 for b in content if isinstance(b, dict) and "cache_control" in b)
    return count


def apply_anthropic_cache_alignment(payload: dict, provider: str) -> dict:
    """Anthropic-only: mark up to 4 breakpoints as cacheable — the system
    prompt (if large), the tool definitions, and the second-to-last message.

    If the payload already carries ANY cache_control marker anywhere, leave
    it alone entirely. Claude Code CLI (and other clients) manage their own
    cache boundaries natively; stacking more breakpoints on top of theirs
    risks an invalid/conflicting ordering — which is exactly what produced
    the 400 Bad Request errors this fix addresses."""
    if provider != "anthropic" or _count_cache_breakpoints(payload) > 0:
        return payload

    breakpoints = 0

    system = payload.get("system")
    system_text = system if isinstance(system, str) else _content_text(system)
    if breakpoints < MAX_CACHE_BREAKPOINTS and len(system_text) > CACHE_SYSTEM_CHAR_THRESHOLD:
        if isinstance(system, str):
            payload["system"] = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
            breakpoints += 1
        elif isinstance(system, list) and system and isinstance(system[-1], dict):
            system[-1]["cache_control"] = {"type": "ephemeral"}
            breakpoints += 1

    tools = payload.get("tools")
    if breakpoints < MAX_CACHE_BREAKPOINTS and isinstance(tools, list) and tools and isinstance(tools[-1], dict):
        tools[-1]["cache_control"] = {"type": "ephemeral"}
        breakpoints += 1

    messages = payload.get("messages")
    if breakpoints < MAX_CACHE_BREAKPOINTS and isinstance(messages, list) and len(messages) >= CACHE_HISTORY_DEPTH_THRESHOLD:
        target_msg = messages[-2]
        if isinstance(target_msg, dict):
            content = target_msg.get("content")
            if isinstance(content, str):
                target_msg["content"] = [{"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}]
                breakpoints += 1
            elif isinstance(content, list) and content and isinstance(content[-1], dict):
                content[-1]["cache_control"] = {"type": "ephemeral"}
                breakpoints += 1

    return payload


def _truncate_head_tail_chars(text: str, max_chars: int) -> str:
    if len(text) <= max_chars * 2:
        return text
    omitted = len(text) - max_chars * 2
    return f"{text[:max_chars]}\n... [{omitted} chars truncated] ...\n{text[-max_chars:]}"


def apply_newest_tool_result_compaction(payload: dict) -> dict:
    """Aggressive head/tail cap, applied ONLY to tool_result blocks in the
    very last message — the "mutable tail". Older tool_result content is
    left to the gentler history-pruning pass so it doesn't destabilize the
    cached prefix on every turn."""
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        return payload

    last_msg = messages[-1]
    if not isinstance(last_msg, dict) or last_msg.get("role") != "user":
        return payload
    content = last_msg.get("content")
    if not isinstance(content, list):
        return payload

    for block in content:
        if not (isinstance(block, dict) and block.get("type") == "tool_result"):
            continue
        inner = block.get("content")
        if isinstance(inner, str):
            block["content"] = _truncate_head_tail_chars(inner, NEWEST_TOOL_RESULT_MAX_CHARS)
        elif isinstance(inner, list):
            for b in inner:
                if isinstance(b, dict) and b.get("type") == "text":
                    b["text"] = _truncate_head_tail_chars(b.get("text", ""), NEWEST_TOOL_RESULT_MAX_CHARS)

    return payload


def _extract_file_path(tool_use_block: dict):
    name = (tool_use_block.get("name") or "").lower()
    if name not in _FILE_READ_TOOL_NAMES:
        return None
    input_ = tool_use_block.get("input") or {}
    for key in _FILE_PATH_INPUT_KEYS:
        value = input_.get(key)
        if isinstance(value, str):
            return value
    return None


class SessionFileStateTracker:
    """Per-session in-memory SHA-256 cache of file paths -> last-seen content.
    A repeated read of an unchanged file collapses to a one-line marker; a
    repeated read of a changed file collapses to a unified diff.

    Each call re-walks the *entire* message list rather than picking up from
    a remembered position. That's deliberate, not a missed optimization: a
    real client always resends its own original, untransformed history (it
    has no idea what we forwarded upstream), so a chronological first-sight
    at message index K must always resolve the same way regardless of which
    request it arrives in — otherwise an already-cached prefix would change
    bytes between turns and defeat Anthropic's prompt cache. A fresh walk
    keeps the transform a pure function of the payload; the per-session
    bucket below only isolates concurrent conversations from each other, it
    carries no meaning across calls.

    ponytail: single process-wide dict, no TTL/eviction beyond a flat size
    cap — fine for a local single-user dev proxy; swap for a real TTL cache
    if this ever runs long-lived/multi-tenant.
    """

    def __init__(self):
        self._sessions = {}
        self._lock = threading.Lock()

    @staticmethod
    def _hash(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()

    def _replace(self, files: dict, path: str, content: str) -> str:
        new_hash = self._hash(content)
        previous = files.get(path)
        files[path] = (new_hash, content)
        if previous is None:
            return content
        prev_hash, prev_content = previous
        if prev_hash == new_hash:
            return f"[FILE UNCHANGED: {path}]"
        diff = "\n".join(
            difflib.unified_diff(
                prev_content.splitlines(), content.splitlines(),
                fromfile=path, tofile=path, n=3, lineterm="",
            )
        )
        return diff or f"[FILE UNCHANGED: {path}]"

    def process(self, session_key: str, messages: list) -> None:
        if not isinstance(messages, list):
            return
        with self._lock:
            if session_key not in self._sessions and len(self._sessions) >= MAX_TRACKED_SESSIONS:
                self._sessions.pop(next(iter(self._sessions)), None)
            files = {}
            self._sessions[session_key] = files

            pending_paths = {}
            for msg in messages:
                if not isinstance(msg, dict):
                    continue
                content = msg.get("content")
                if not isinstance(content, list):
                    continue
                if msg.get("role") == "assistant":
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "tool_use":
                            path = _extract_file_path(block)
                            if path:
                                pending_paths[block.get("id")] = path
                elif msg.get("role") == "user":
                    for block in content:
                        if not (isinstance(block, dict) and block.get("type") == "tool_result"):
                            continue
                        path = pending_paths.get(block.get("tool_use_id"))
                        if not path:
                            continue
                        text = _content_text(block.get("content"))
                        if not text:
                            continue
                        replacement = self._replace(files, path, text)
                        if isinstance(block.get("content"), str):
                            block["content"] = replacement
                        elif isinstance(block.get("content"), list):
                            block["content"] = [{"type": "text", "text": replacement}]

    def clear(self, session_key: str) -> None:
        with self._lock:
            self._sessions.pop(session_key, None)


_file_tracker = SessionFileStateTracker()


def derive_session_key(payload: dict) -> str:
    """Best-effort conversation fingerprint: no real session protocol exists
    at the HTTP-proxy layer, so approximate "same conversation" via a hash of
    the system prompt + first message, which stay constant turn to turn."""
    messages = payload.get("messages")
    first = messages[0] if isinstance(messages, list) and messages else None
    seed = json.dumps([payload.get("system"), first], sort_keys=True, default=str)[:2000]
    return hashlib.sha256(seed.encode("utf-8", errors="ignore")).hexdigest()[:16]


def apply_file_delta_tracking(payload: dict, session_key: str) -> dict:
    messages = payload.get("messages")
    if isinstance(messages, list):
        _file_tracker.process(session_key, messages)
    return payload


def clamp_max_tokens(payload: dict) -> dict:
    """Cap max_tokens for obviously short/simple exchanges (a confirmation,
    a one-line check) — no reason to hold open an 8192-token budget for a
    reply that's never going to use it."""
    max_tokens = payload.get("max_tokens")
    if not isinstance(max_tokens, int) or max_tokens <= MAX_TOKENS_CLAMP_TARGET:
        return payload
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        return payload
    last = messages[-1]
    if not isinstance(last, dict) or last.get("role") != "user":
        return payload
    text = _content_text(last.get("content")).strip()
    if not text or len(text) >= SIMPLE_QUERY_CHAR_THRESHOLD:
        return payload
    if any(marker in text for marker in ("```", "def ", "class ", "http://", "https://")):
        return payload
    payload["max_tokens"] = MAX_TOKENS_CLAMP_TARGET
    return payload


# ponytail: cuts on the first '.' or newline with no abbreviation handling
# (an "e.g." or "etc." early in a description truncates there too) — accepted
# lossy ceiling for a deterministic, single-pass minifier. Upgrade to a real
# sentence tokenizer if a tool description gets mangled in practice.
_FIRST_SENTENCE_BREAK = re.compile(r"[.\n]")


def _first_sentence(text: str) -> str:
    if not text:
        return text
    m = _FIRST_SENTENCE_BREAK.search(text)
    head = text[: m.start()].strip() if m else text.strip()
    if not head:
        return text.strip()
    return head if head.endswith((".", "!", "?")) else head + "."


def _minify_schema_node(node):
    """Recurse into a JSON-schema fragment, shrinking `description` strings
    while leaving type/properties/required/enum and key order untouched."""
    if not isinstance(node, dict):
        return node
    new_node = dict(node)
    if isinstance(new_node.get("description"), str):
        new_node["description"] = _first_sentence(new_node["description"])
    if isinstance(new_node.get("properties"), dict):
        new_node["properties"] = {k: _minify_schema_node(v) for k, v in new_node["properties"].items()}
    if isinstance(new_node.get("items"), dict):
        new_node["items"] = _minify_schema_node(new_node["items"])
    return new_node


def minify_tools_deterministic(tools):
    """Shrink verbose tool/parameter descriptions to their first sentence.
    type/properties/required/enum, key order, and any existing cache_control
    marker are left exactly as they were — a pure function of the input, so
    the same tools array always minifies to the same bytes turn to turn and
    Anthropic's prompt-cache hash on the tools block stays valid."""
    if not isinstance(tools, list):
        return tools
    out = []
    for tool in tools:
        if not isinstance(tool, dict):
            out.append(tool)
            continue
        new_tool = dict(tool)
        if isinstance(new_tool.get("description"), str):
            new_tool["description"] = _first_sentence(new_tool["description"])
        if isinstance(new_tool.get("input_schema"), dict):
            new_tool["input_schema"] = _minify_schema_node(new_tool["input_schema"])
        # OpenAI / Mistral function tools
        fn = new_tool.get("function")
        if isinstance(fn, dict):
            fn = dict(fn)
            if isinstance(fn.get("description"), str):
                fn["description"] = _first_sentence(fn["description"])
            if isinstance(fn.get("parameters"), dict):
                fn["parameters"] = _minify_schema_node(fn["parameters"])
            new_tool["function"] = fn
        out.append(new_tool)
    return out


def optimize_payload(
    payload: dict,
    compression_enabled: bool = True,
    anti_yap_enabled: bool = True,
    history_pruning_enabled: bool = True,
    log_truncation_enabled: bool = True,
):
    """Returns (payload, categories) — categories lists which techniques were
    active for this request (for the dashboard's savings_category badge)."""
    # tool_choice and tool_use blocks (pending tool call arguments) are never
    # read or written anywhere in this module — only "tools" descriptions are
    # (see minify_tools_deterministic).
    categories = []

    if compression_enabled and isinstance(payload.get("tools"), list) and payload["tools"]:
        payload["tools"] = minify_tools_deterministic(payload["tools"])
        categories.append("tool_minification")

    # Never inject the anti-yap directive on short prompts — its own cost
    # (~35 tokens) can exceed anything a one-line query has to save.
    anti_yap_active = anti_yap_enabled and len(extract_text(payload)) > ANTI_YAP_MIN_CHARS

    if anti_yap_active and "system" in payload:
        payload["system"] = inject_anti_yap(payload["system"])
        categories.append("anti_yap")

    messages = payload.get("messages")
    if history_pruning_enabled and isinstance(messages, list) and len(messages) > HISTORY_WINDOW_THRESHOLD:
        pruned_messages = prune_history(
            messages, log_truncation_enabled=log_truncation_enabled, turn_squashing_enabled=True
        )
        payload["messages"] = pruned_messages
        categories.append("pruning")
        if log_truncation_enabled:
            categories.append("log_truncation")
        if any("[Exploration:" in _content_text(m.get("content")) for m in pruned_messages if isinstance(m, dict)):
            categories.append("turn_squashing")

    if compression_enabled:
        payload = clamp_max_tokens(payload)
        categories.append("prose")

    for msg in payload.get("messages") or []:
        if not isinstance(msg, dict):
            continue
        if msg.get("role") == "system":
            if anti_yap_active:
                msg["content"] = inject_anti_yap(msg.get("content"))
        elif compression_enabled:
            msg["content"] = _optimize_content(msg.get("content"))

    if compression_enabled and isinstance(payload.get("prompt"), str):
        payload["prompt"] = optimize_text(payload["prompt"])

    return payload, categories


def compute_request_hash(payload: dict, provider: str) -> str:
    """Cache key over the semantically-relevant part of a request (provider +
    model + system + full message history). Computed on the ORIGINAL,
    pre-optimization payload so it reflects what the client actually asked,
    independent of anything this module does to it turn to turn."""
    key_payload = {
        "provider": provider,
        "model": payload.get("model"),
        "system": payload.get("system"),
        "messages": payload.get("messages"),
    }
    return hashlib.sha256(canonical_json_dumps(key_payload)).hexdigest()


_SSE_DATA_LINE = re.compile(r"^data:\s*(.+)$")


def extract_upstream_usage(raw_body: bytes) -> dict:
    """Best-effort parse of a completed upstream response (SSE stream or
    plain JSON body) for its `usage` object — the provider's own billed
    token figures, not our estimate. Merges every usage-bearing SSE event
    (Anthropic splits cache stats into message_start and the final output
    count into message_delta) so the result has whatever fields were sent."""
    text = raw_body.decode("utf-8", errors="ignore")
    usage = {}
    saw_sse = False

    def _ingest(u):
        if not isinstance(u, dict):
            return
        flat = {k: v for k, v in u.items() if v is not None and not isinstance(v, (dict, list))}
        # Newer Anthropic: cache_creation: {ephemeral_5m_input_tokens, ephemeral_1h_...}
        creation = u.get("cache_creation")
        if isinstance(creation, dict):
            total = 0
            for v in creation.values():
                if isinstance(v, (int, float)):
                    total += int(v)
            if total and "cache_creation_input_tokens" not in flat:
                flat["cache_creation_input_tokens"] = total
        usage.update(flat)

    for line in text.splitlines():
        m = _SSE_DATA_LINE.match(line.strip())
        if not m or m.group(1) in ("", "[DONE]"):
            continue
        try:
            obj = json.loads(m.group(1))
        except ValueError:
            continue
        saw_sse = True
        if isinstance(obj, dict):
            _ingest(obj.get("usage"))
            msg = obj.get("message")
            if isinstance(msg, dict):
                _ingest(msg.get("usage"))
    if not saw_sse:
        try:
            obj = json.loads(text)
            if isinstance(obj, dict):
                _ingest(obj.get("usage"))
        except ValueError:
            pass
    return usage
