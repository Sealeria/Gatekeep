# Copyright (c) 2026 Sealeria
# SPDX-License-Identifier: AGPL-3.0-or-later
# Licensed under the GNU Affero General Public License v3.0

"""Dual-state error protection — preserve diagnostics, crush generic noise."""

from __future__ import annotations

import re

STACKTRACE_CEILING = 3000
FILE_READ_LINE_CAP = 400

_EXIT_NONZERO = re.compile(
    r"(?im)(?:"
    r"exit(?:\s+|-)?code\s*[=:]?\s*(?!0\b)\d+"
    r"|exited with code (?!(?:0\b))\d+"
    r"|command failed(?: with exit code (?!(?:0\b))\d+)?"
    r"|returned non-zero exit status"
    r")"
)
_STDERR_MARKERS = re.compile(r"(?im)^stderr:\s*\S")
_ERROR_LINE = re.compile(
    r"(?im)(?:"
    r"traceback \(most recent call last\)"
    r"|^panic:"
    r"|unhandledpromiserejection"
    r"|syntaxerror:"
    r"|typeerror:"
    r"|referenceerror:"
    r"|error ts\d+"
    r"|error:\s"
    r"|fatal error:"
    r"|compilation failed"
    r"|build failed"
    r"|npm err!"
    r"|pytest\.|\bFAILED\b"
    r")"
)
_TRACEBACK_START = re.compile(r"Traceback \(most recent call last\):", re.I)


def is_error_payload(
    text: str,
    *,
    tool_name: str | None = None,
    is_error_flag: bool = False,
) -> bool:
    """True when output looks like a failed command or diagnostic failure."""
    if is_error_flag:
        return True
    if not text or not str(text).strip():
        return False
    s = str(text)
    if _EXIT_NONZERO.search(s):
        return True
    if _STDERR_MARKERS.search(s) and len(s.strip()) > 20:
        return True
    if _ERROR_LINE.search(s):
        return True
    low = s.lower()
    if "traceback (most recent call last)" in low:
        return True
    if tool_name and (tool_name or "").lower() in ("bash", "shell", "bashtool"):
        if re.search(r"(?im)^error:", s) and len(s) < 8000:
            return True
    return False


def _traceback_block_end(lines: list[str], start: int) -> int:
    """End index (exclusive) of a contiguous traceback block."""
    end = start + 1
    while end < len(lines):
        line = lines[end]
        if not line.strip():
            if end + 1 < len(lines) and (
                lines[end + 1].startswith(" ")
                or lines[end + 1].startswith("\t")
                or _TRACEBACK_START.search(lines[end + 1])
            ):
                end += 1
                continue
            break
        stripped = line.lstrip()
        if line[0] in (" ", "\t") or stripped.startswith('File "'):
            end += 1
            continue
        if _TRACEBACK_START.search(line):
            end += 1
            continue
        # Exception summary line (ValueError: ..., panic: ..., etc.)
        if end > start + 1 and _ERROR_LINE.search(line):
            end += 1
            continue
        if end > start + 1:
            break
        end += 1
    return end


def extract_stacktrace_blocks(text: str) -> list[tuple[int, int]]:
    """Return (start_line, end_line) spans for traceback blocks."""
    lines = text.split("\n")
    spans: list[tuple[int, int]] = []
    i = 0
    while i < len(lines):
        if _TRACEBACK_START.search(lines[i]):
            end = _traceback_block_end(lines, i)
            spans.append((i, end))
            i = end
        else:
            i += 1
    return spans


def preserve_stacktrace_blocks(text: str, *, ceiling: int = STACKTRACE_CEILING) -> str:
    """Keep full traceback blocks; apply ceiling to the preserved diagnostic body."""
    if not text:
        return text
    spans = extract_stacktrace_blocks(text)
    if not spans:
        if is_error_payload(text) and len(text) > ceiling:
            return _head_tail(text, ceiling)
        return text
    lines = text.split("\n")
    keep = set()
    for start, end in spans:
        keep.update(range(start, end))
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
            skipped = i - start
            if skipped:
                out.append(f"[…{skipped} non-trace lines omitted…]")
    preserved = "\n".join(out)
    if len(preserved) > ceiling:
        return _head_tail(preserved, ceiling)
    return preserved


def preserve_diagnostic_text(text: str, *, ceiling: int = STACKTRACE_CEILING) -> str:
    """Full diagnostic preservation for errors — bypass aggressive char caps."""
    if not text:
        return text
    if _TRACEBACK_START.search(text):
        return preserve_stacktrace_blocks(text, ceiling=ceiling)
    if is_error_payload(text):
        if len(text) <= ceiling:
            return text
        return _head_tail(text, ceiling)
    return text


def crush_log_lines_safe(text: str, crusher) -> str:
    """Run log crusher unless payload is error diagnostics."""
    if is_error_payload(text):
        return preserve_diagnostic_text(text)
    return crusher(text)


def _head_tail(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    head = max_chars * 2 // 3
    tail = max_chars - head - 40
    omitted = len(text) - head - tail
    return f"{text[:head]}\n…[{omitted} chars omitted]…\n{text[-tail:]}"
