# Copyright (c) 2026 Sealeria
# SPDX-License-Identifier: AGPL-3.0-or-later
# Licensed under the GNU Affero General Public License v3.0

"""Crush Cursor/Codex Connect+protobuf wire bodies."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from engines import aggressive as aggressive_mod
from engines import optimizer
from engines.connect_frames import map_frame_payloads
from engines.error_guard import (
    FILE_READ_LINE_CAP,
    STACKTRACE_CEILING,
    is_error_payload,
    preserve_diagnostic_text,
)

_MIN_STR = 80
MAX_PROTO_DEPTH = 32
_AGGR_MIN_STR = 60
_AGGR_MAX_STR = 100
_AGGR_DEDUP_MIN = 150
_PRINTABLE_RE = re.compile(r"[\x09\x0a\x0d\x20-\x7e]")
_DUMPED = 0
_AGENT_CAPTURES = 0


def capture_agent_frame(raw: bytes, crushed: bytes, *, aggressive: bool) -> None:
    """Save low-save frames for offline analysis (max 8)."""
    global _AGENT_CAPTURES
    if len(raw) < 2000:
        return
    saved = len(raw) - len(crushed)
    pct = saved / len(raw) * 100 if raw else 0
    if pct >= 70:
        return
    if _AGENT_CAPTURES >= 8:
        return
    _AGENT_CAPTURES += 1
    try:
        from pathlib import Path

        d = Path(__file__).resolve().parents[2] / "bench" / "runs" / "agent-frames"
        d.mkdir(parents=True, exist_ok=True)
        tag = f"{len(raw)}-{int(pct)}"
        (d / f"{_AGENT_CAPTURES}-{tag}-raw.bin").write_bytes(raw[:500_000])
        (d / f"{_AGENT_CAPTURES}-{tag}-crushed.bin").write_bytes(crushed[:500_000])
        (d / f"{_AGENT_CAPTURES}-{tag}.meta.txt").write_text(
            f"raw={len(raw)} crushed={len(crushed)} saved={saved} pct={pct:.1f} aggr={aggressive}\n",
            encoding="utf-8",
        )
    except OSError:
        pass


def analyze_proto_leaves(data: bytes, path: tuple[int, ...] = ()) -> list[tuple[str, int, int, str]]:
    """Return list of (kind, field_path_len, size, preview) for large leaves."""
    out: list[tuple[str, int, int, str]] = []
    try:
        fields = _parse_fields(data)
    except ValueError:
        return [("unparsed", len(path), len(data), data[:40].hex())]
    for fn, wt, val in fields:
        p = path + (fn,)
        if wt != 2:
            continue
        if _looks_like_message(val):
            out.extend(analyze_proto_leaves(val, p))
        elif _is_text_blob(val):
            preview = val[:60].decode("utf-8", errors="replace").replace("\n", " ")
            out.append(("text", fn, len(val), preview))
        else:
            out.append(("bin", fn, len(val), val[:24].hex()))
    return out


def _dump_miss(body: bytes, path: str, content_type: str) -> None:
    global _DUMPED
    if _DUMPED >= 5 or len(body) < 400:
        return
    if "analytics" in (path or "").lower() or "trackevents" in (path or "").lower():
        return
    _DUMPED += 1
    try:
        from pathlib import Path

        d = Path(__file__).resolve().parents[2] / "bench" / "runs"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"wire-miss-{_DUMPED}.bin").write_bytes(body[:400_000])
        (d / f"wire-miss-{_DUMPED}.meta.txt").write_text(
            f"path={path}\nctype={content_type}\nlen={len(body)}\nhead={body[:48].hex()}\n",
            encoding="utf-8",
        )
    except OSError:
        pass


def _read_varint(buf: bytes, i: int) -> tuple[int, int]:
    shift = 0
    n = 0
    while i < len(buf):
        b = buf[i]
        i += 1
        n |= (b & 0x7F) << shift
        if not (b & 0x80):
            return n, i
        shift += 7
        if shift > 70:
            break
    raise ValueError("bad varint")


def _write_varint(n: int) -> bytes:
    out = bytearray()
    while n > 0x7F:
        out.append((n & 0x7F) | 0x80)
        n >>= 7
    out.append(n & 0x7F)
    return bytes(out)


def _is_text_blob(raw: bytes) -> bool:
    if len(raw) < _MIN_STR:
        return False
    try:
        s = raw.decode("utf-8")
    except UnicodeDecodeError:
        return False
    if "\x00" in s:
        return False
    printable = sum(1 for c in s if _PRINTABLE_RE.match(c) or ord(c) > 127)
    return printable / max(1, len(s)) > 0.85


def _parse_fields(buf: bytes) -> list[tuple[int, int, bytes]]:
    fields: list[tuple[int, int, bytes]] = []
    i = 0
    n = len(buf)
    while i < n:
        key, i = _read_varint(buf, i)
        fn, wt = key >> 3, key & 7
        if fn == 0:
            raise ValueError("field 0")
        if wt == 0:
            _, i2 = _read_varint(buf, i)
            fields.append((fn, wt, buf[i:i2]))
            i = i2
        elif wt == 1:
            if i + 8 > n:
                raise ValueError("short64")
            fields.append((fn, wt, buf[i : i + 8]))
            i += 8
        elif wt == 2:
            ln, i = _read_varint(buf, i)
            if i + ln > n:
                raise ValueError("shortLD")
            fields.append((fn, wt, buf[i : i + ln]))
            i += ln
        elif wt == 5:
            if i + 4 > n:
                raise ValueError("short32")
            fields.append((fn, wt, buf[i : i + 4]))
            i += 4
        else:
            raise ValueError(f"wire {wt}")
    return fields


def _mostly_utf8_text(raw: bytes) -> bool:
    if not raw:
        return False
    try:
        s = raw.decode("utf-8")
    except UnicodeDecodeError:
        return False
    if "\x00" in s:
        return False
    printable = sum(1 for c in s if _PRINTABLE_RE.match(c) or ord(c) > 127)
    return printable / len(s) > 0.85


def _looks_like_message(raw: bytes) -> bool:
    if len(raw) < 2:
        return False
    try:
        fields = _parse_fields(raw)
    except ValueError:
        return False
    if not fields or any(fn < 1 for fn, _, _ in fields):
        return False
    # Reject text that only false-parses as fixed64/varint (e.g. "import re").
    if not any(wt == 2 for _, wt, _ in fields) and _mostly_utf8_text(raw):
        return False
    return True


def _encode_field(fn: int, wt: int, val: bytes) -> bytes:
    head = _write_varint((fn << 3) | wt)
    if wt == 2:
        return head + _write_varint(len(val)) + val
    return head + val


def _should_crush_context(s: str, *, aggressive: bool = False) -> bool:
    if aggressive and len(s) >= _AGGR_MIN_STR:
        return True
    if "tool output line" in s or "Certainly!" in s:
        return True
    if len(s) < _MIN_STR:
        return False
    if "Context dump" in s or "[...truncated...]" in s:
        return True
    if s.count("\n") >= 10 and len(s) > 500:
        return True
    return len(s) > 1200


def _looks_like_model_catalog(s: str) -> bool:
    if len(s) < 400:
        return False
    markers = (".fast", ".thinking", "composer-", "claude-", "gpt-", "grok-", "gemini-")
    hits = sum(1 for m in markers if m in s)
    return hits >= 4 and s.count("\n") + s.count(".") > 20


def _crush_model_catalog(s: str) -> str:
    return "[models]"


def _crush_agent_skill_desc(s: str) -> str:
    return "[skill]"


def _crush_agent_mcp_block(_: bytes) -> bytes:
    return b"\n\x04[mcp]"


def _crush_agent_rules_block(s: str, *, aggressive: bool = False) -> str:
    limit = 80 if aggressive else 200
    if len(s) <= limit:
        return s
    return "[ctx]"


def _crush_fat_prompt_blob(s: str) -> str | None:
    if "tool output line" not in s and not (
        "Certainly!" in s and s.count("Certainly!") >= 3
    ):
        return None
    if "Reply with exactly:" in s:
        head = s.split("Context dump", 1)[0].strip()
        return f"{head}\n\nContext dump (ignore):\n[crushed]"
    return "[crushed context]"


def _looks_like_json(s: str) -> bool:
    t = s.lstrip()[:1]
    return t in ("{", "[")


def _sanitize_controls(s: str) -> str:
    """Strip control chars that break JSON string literals if re-embedded."""
    return "".join(c if (ord(c) >= 32 or c in "\n\t\r") else " " for c in s)


def _safe_truncate(s: str, max_chars: int) -> str:
    s = _sanitize_controls(s)
    if len(s) <= max_chars:
        return s
    head = max_chars // 2
    tail = max_chars - head
    return f"{s[:head]} …[{len(s) - max_chars} chars]… {s[-tail:]}"


def _crush_json_text(s: str, *, aggressive: bool) -> str:
    try:
        obj = json.loads(s)
    except ValueError:
        return _safe_truncate(s, _AGGR_MAX_STR if aggressive else 1500)
    crushed = crush_json_value(obj, aggressive=aggressive)
    if isinstance(crushed, dict):
        out = optimizer.canonical_json_dumps(crushed)
        return out.decode("utf-8") if isinstance(out, bytes) else out
    return json.dumps(crushed, separators=(",", ":"), ensure_ascii=False)


def _crush_text(s: str, *, aggressive: bool = False) -> str:
    if is_error_payload(s):
        return preserve_diagnostic_text(s, ceiling=STACKTRACE_CEILING)
    if _looks_like_json(s):
        return _crush_json_text(s, aggressive=aggressive)
    t = optimizer.optimize_text(s)
    if aggressive:
        t = aggressive_mod.crush_text(t)
        if len(t) > _AGGR_MAX_STR:
            t = _safe_truncate(t, _AGGR_MAX_STR)
        return _sanitize_controls(t)
    if len(t) > 2500:
        t = _safe_truncate(t, 1800)
    elif len(t) > 900:
        t = _safe_truncate(t, 850)
    return _sanitize_controls(t)


def _crush_text_bytes(val: bytes, *, aggressive: bool, seen: set[str] | None = None) -> bytes | None:
    if len(val) < _AGGR_MIN_STR:
        return None
    try:
        s = val.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if is_error_payload(s):
        preserved = preserve_diagnostic_text(s, ceiling=STACKTRACE_CEILING)
        new = preserved.encode("utf-8")
        return new if len(new) < len(val) else None
    if not aggressive and not _should_crush_context(s, aggressive=False):
        return None
    if aggressive and seen is not None and len(s) >= _AGGR_DEDUP_MIN:
        digest = hashlib.sha256(s.encode("utf-8")).hexdigest()[:12]
        if digest in seen:
            if _looks_like_json(s):
                stub = json.dumps({"_dup": digest, "_n": len(s)}, separators=(",", ":")).encode()
            else:
                stub = f"[dup {digest} len={len(s)}]".encode("utf-8")
            return stub if len(stub) < len(val) else None
        seen.add(digest)
    new = _crush_text(s, aggressive=aggressive).encode("utf-8")
    return new if len(new) < len(val) else None


def _aggressive_sweep(
    data: bytes,
    field_path: tuple[int, ...] = (),
    *,
    aggressive: bool = False,
    seen: set[str] | None = None,
    depth: int = 0,
) -> tuple[bytes, int]:
    """Second pass: crush any large UTF-8 leaf still left after field-aware crush."""
    if depth > MAX_PROTO_DEPTH:
        return data, 0
    try:
        fields = _parse_fields(data)
    except ValueError:
        return data, 0
    out = bytearray()
    saved = 0
    changed = False
    for fn, wt, val in fields:
        path = field_path + (fn,)
        if wt == 2:
            if _looks_like_message(val):
                # Do not collapse here — crush_protobuf_message already did;
                # a second pass would re-hash the soft-crushed body as a new file.
                val, nested_saved = _aggressive_sweep(
                    val, path, aggressive=aggressive, seen=seen, depth=depth + 1
                )
                saved += nested_saved
                if nested_saved:
                    changed = True
            elif _is_text_blob(val):
                crushed = _crush_text_bytes(val, aggressive=True, seen=seen)
                if crushed is not None:
                    saved += len(val) - len(crushed)
                    val = crushed
                    changed = True
            else:
                crushed = _crush_text_bytes(val, aggressive=True, seen=seen)
                if crushed is not None:
                    saved += len(val) - len(crushed)
                    val = crushed
                    changed = True
        out.extend(_encode_field(fn, wt, val))
    return (bytes(out) if changed else data, saved)


def _extract_line_text(fv: bytes) -> str | None:
    """Bare UTF-8 or Cursor line wrapper {line_no:varint, text:string}."""
    parsed = _parse_line_wrapper(fv)
    if parsed is not None:
        return parsed[1].decode("utf-8", errors="replace")
    try:
        s = fv.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if "\x00" in s:
        return None
    return s


def _parse_line_wrapper(fv: bytes) -> tuple[bytes, bytes] | None:
    """Return (lineno_varint_bytes, text_bytes) for {1:varint, 2:string}."""
    if not _looks_like_message(fv):
        return None
    try:
        nested = _parse_fields(fv)
    except ValueError:
        return None
    lineno = b""
    text: bytes | None = None
    for nfn, nwt, nv in nested:
        if nfn == 1 and nwt == 0:
            lineno = nv
        elif nfn == 2 and nwt == 2:
            if b"\x00" in nv:
                return None
            try:
                nv.decode("utf-8")
            except UnicodeDecodeError:
                return None
            text = nv
        elif nwt == 0:
            continue
        else:
            return None
    if text is None or not lineno:
        return None
    return lineno, text


def _encode_line_wrapper(lineno_varint: bytes, text: bytes) -> bytes:
    return _encode_field(1, 0, lineno_varint) + _encode_field(2, 2, text)


def _try_collapse_line_message(
    val: bytes,
    *,
    aggressive: bool,
    seen: set[str] | None = None,
) -> bytes | None:
    """File crush: re-read stub; first read per-line cap (same wrapper count).

    Pack/join (drop wrappers) and blank-mids hang tool-use. Capping every line
    keeps structure + a prefix of each line so mid-file symbols stay visible.
    Source files under FILE_READ_LINE_CAP lines are never line-capped.
    """
    if not aggressive or len(val) < 800:
        return None
    try:
        fields = _parse_fields(val)
    except ValueError:
        return None

    filename = b""
    line_fn = 2
    wrappers: list[tuple[bytes, bytes]] = []
    other: list[tuple[int, int, bytes]] = []
    for fn, wt, fv in fields:
        if wt != 2:
            other.append((fn, wt, fv))
            continue
        if (
            fn == 1
            and not _looks_like_message(fv)
            and 0 < len(fv) < 260
            and not wrappers
        ):
            try:
                fv.decode("utf-8")
                filename = fv
                continue
            except UnicodeDecodeError:
                pass
        parsed = _parse_line_wrapper(fv)
        if parsed is None:
            other.append((fn, wt, fv))
            continue
        wrappers.append(parsed)
        line_fn = fn

    if len(wrappers) < 24:
        return None

    # Preserve complete source reads for normal-sized files.
    if len(wrappers) < FILE_READ_LINE_CAP:
        return None

    texts = [t.decode("utf-8", errors="replace") for _, t in wrappers]
    joined = "\n".join(texts)
    if len(joined) < 600:
        return None

    digest = hashlib.sha256(
        (filename or b"") + b"\0" + joined.encode("utf-8")
    ).hexdigest()[:12]
    key = f"file:{digest}"
    pending = f"~{key}"

    def _prefix() -> bytearray:
        out = bytearray()
        if filename:
            out.extend(_encode_field(1, 2, filename))
        for fn, wt, fv in other:
            out.extend(_encode_field(fn, wt, fv))
        return out

    if seen is not None and key in seen:
        out = _prefix()
        stub = (
            f"[dup file {digest} lines={len(wrappers)} len={len(joined)}]"
        ).encode("utf-8")
        out.extend(
            _encode_field(line_fn, 2, _encode_line_wrapper(wrappers[0][0], stub))
        )
        return bytes(out) if len(out) < len(val) - 50 else None

    if seen is not None:
        seen.add(pending)

    # Cap every line; keep head/tail slightly richer.
    keep_head, keep_tail = 40, 20
    head_cap, mid_cap, tail_cap = 72, 32, 56
    n = len(wrappers)
    out = _prefix()
    changed = False
    for i, (lineno, text) in enumerate(wrappers):
        if i < keep_head:
            cap = head_cap
        elif i >= n - keep_tail:
            cap = tail_cap
        else:
            cap = mid_cap
        s = text.decode("utf-8", errors="replace")
        if len(s) > cap:
            new = _safe_truncate(s, cap).encode("utf-8")
            changed = True
        else:
            new = text
        out.extend(_encode_field(line_fn, 2, _encode_line_wrapper(lineno, new)))

    return bytes(out) if changed and len(out) < len(val) - 80 else None


def commit_file_seen(seen: set[str] | None) -> None:
    """Promote provisional '~file:…' marks after a Connect body is fully crushed."""
    if not seen:
        return
    for x in list(seen):
        if x.startswith("~file:"):
            seen.discard(x)
            seen.add(x[1:])


def _minimal_skill_entry(val: bytes) -> bytes:
    """Keep a short skill id; drop MCP prose bundled in RequestContext."""
    try:
        fields = _parse_fields(val)
    except ValueError:
        return val
    name = b"[skill]"
    for fn, wt, field_val in fields:
        if fn == 1 and wt == 2 and field_val:
            name = field_val[:28]
            break
    return _encode_field(1, 2, name)


def crush_protobuf_message(
    data: bytes,
    field_path: tuple[int, ...] = (),
    *,
    aggressive: bool = False,
    seen: set[str] | None = None,
    depth: int = 0,
) -> tuple[bytes, int]:
    """Return (message, bytes_saved_in_proto)."""
    if depth > MAX_PROTO_DEPTH:
        return data, 0
    if aggressive and seen is None:
        seen = set()
    try:
        fields = _parse_fields(data)
    except ValueError:
        return data, 0
    out = bytearray()
    saved = 0
    changed = False
    for fn, wt, val in fields:
        path = field_path + (fn,)
        if wt == 2:
            skill_limit = 40 if aggressive else 80
            mcp_limit = 50 if aggressive else 500
            mcp_nested_limit = 80 if aggressive else 200
            skill_desc_limit = 40 if aggressive else 80
            rules_limit = 80 if aggressive else 200
            if fn == 29 and len(val) > skill_limit:
                new = _minimal_skill_entry(val)
                if len(new) < len(val):
                    saved += len(val) - len(new)
                    val = new
                    changed = True
            elif fn == 34 and len(val) > mcp_limit:
                stub = _encode_field(1, 0, bytes([1]))
                saved += len(val) - len(stub)
                val = stub
                changed = True
            elif path[-2:] == (34, 2) and len(val) > mcp_nested_limit:
                new = _crush_agent_mcp_block(val)
                if len(new) < len(val):
                    saved += len(val) - len(new)
                    val = new
                    changed = True
            elif path[-2:] == (29, 3) and len(val) > skill_desc_limit:
                new = _crush_agent_skill_desc(val.decode("utf-8", errors="replace")).encode(
                    "utf-8"
                )
                if len(new) < len(val):
                    saved += len(val) - len(new)
                    val = new
                    changed = True
            elif path[-2:] == (14, 2) and len(val) > rules_limit:
                try:
                    s = val.decode("utf-8")
                except UnicodeDecodeError:
                    pass
                else:
                    new = _crush_agent_rules_block(s, aggressive=aggressive).encode("utf-8")
                    if len(new) < len(val):
                        saved += len(val) - len(new)
                        val = new
                        changed = True
            elif _looks_like_message(val):
                collapsed = _try_collapse_line_message(
                    val, aggressive=aggressive, seen=seen
                )
                if collapsed is not None:
                    saved += len(val) - len(collapsed)
                    val = collapsed
                    changed = True
                else:
                    nested, nested_saved = crush_protobuf_message(
                        val, path, aggressive=aggressive, seen=seen, depth=depth + 1
                    )
                    if nested != val:
                        val = nested
                        saved += nested_saved
                        changed = True
            else:
                try:
                    s = val.decode("utf-8")
                except UnicodeDecodeError:
                    pass
                else:
                    fat = _crush_fat_prompt_blob(s)
                    if fat is not None:
                        new = fat.encode("utf-8")
                    elif _looks_like_model_catalog(s):
                        new = _crush_model_catalog(s).encode("utf-8")
                    elif _should_crush_context(s, aggressive=aggressive):
                        new = _crush_text_bytes(val, aggressive=aggressive, seen=seen)
                    elif len(s) > 300 and 29 in path:
                        new = _crush_agent_skill_desc(s).encode("utf-8")
                    else:
                        new = None
                    if new is not None and len(new) < len(val):
                        saved += len(val) - len(new)
                        val = new
                        changed = True
        out.extend(_encode_field(fn, wt, val))
    result = bytes(out) if changed else data
    if aggressive and len(result) > 500:
        swept, sweep_saved = _aggressive_sweep(
            result, field_path, aggressive=aggressive, seen=seen, depth=depth
        )
        if sweep_saved:
            result = swept
            saved += sweep_saved
    return result, saved


def crush_connect_body(
    body: bytes,
    *,
    aggressive: bool = False,
    seen: set[str] | None = None,
) -> tuple[bytes, int]:
    """Crush framed or raw Connect body; returns (body, proto_bytes_saved)."""
    if aggressive and seen is None:
        seen = set()

    def mapper(raw: bytes) -> bytes:
        crushed, _ = crush_protobuf_message(raw, aggressive=aggressive, seen=seen)
        return crushed

    mapped, changed = map_frame_payloads(body, mapper)
    if changed:
        commit_file_seen(seen)
        frames_saved = max(0, len(body) - len(mapped))
        return mapped, frames_saved

    crushed, saved = crush_protobuf_message(body, aggressive=aggressive, seen=seen)
    commit_file_seen(seen)
    return crushed, saved if crushed != body else 0


def drain_crush_frames(
    buf: bytes,
    *,
    aggressive: bool = False,
    seen: set[str] | None = None,
) -> tuple[bytes, list[bytes]]:
    """Extract complete Connect frames from buf, crush each; return (remainder, frames)."""
    out: list[bytes] = []
    i = 0
    n = len(buf)
    while i + 5 <= n:
        ln = int.from_bytes(buf[i + 1 : i + 5], "big")
        end = i + 5 + ln
        if end > n:
            break
        frame = buf[i:end]
        crushed, _ = crush_connect_body(frame, aggressive=aggressive, seen=seen)
        out.append(crushed)
        i = end
    return buf[i:], out


def crush_json_value(node: Any, *, aggressive: bool = False) -> Any:
    if isinstance(node, str):
        if len(node) < (_AGGR_MIN_STR if aggressive else _MIN_STR):
            return node
        return _crush_text(node, aggressive=aggressive)
    if isinstance(node, list):
        return [crush_json_value(x, aggressive=aggressive) for x in node]
    if isinstance(node, dict):
        return {k: crush_json_value(v, aggressive=aggressive) for k, v in node.items()}
    return node


def crush_json_body(body: bytes, *, aggressive: bool = False) -> bytes | None:
    try:
        obj = json.loads(body)
    except ValueError:
        return None
    if not isinstance(obj, (dict, list)):
        return None
    crushed = crush_json_value(obj, aggressive=aggressive)
    if isinstance(crushed, dict):
        return optimizer.canonical_json_dumps(crushed)
    return json.dumps(crushed, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def crush_wire_body(
    body: bytes,
    *,
    path: str,
    content_type: str,
    aggressive: bool = False,
) -> tuple[bytes, list[str], int]:
    """Return (new_body, categories, bytes_saved)."""
    if not body or len(body) < 200:
        return body, [], 0
    ctype = (content_type or "").lower()
    path_l = (path or "").lstrip("/").lower()

    if (
        "connect+proto" in ctype
        or "application/grpc" in ctype
        or "protobuf" in ctype
        or path_l.startswith(("aiserver.", "agent.v1."))
    ):
        new, saved = crush_connect_body(body, aggressive=aggressive)
        if saved > 0 or (new != body and len(new) < len(body)):
            tag = "wire_proto_crush_aggressive" if aggressive else "wire_proto_crush"
            return new, [tag], max(saved, len(body) - len(new))
        _dump_miss(body, path, content_type)
        return body, [], 0

    if "connect+json" in ctype or body.lstrip()[:1] in (b"{", b"["):
        new = crush_json_body(body, aggressive=aggressive)
        if new is not None and len(new) < len(body):
            return new, ["wire_json_crush"], len(body) - len(new)
        if len(body) > 800:
            _dump_miss(body, path, content_type)

    return body, [], 0
