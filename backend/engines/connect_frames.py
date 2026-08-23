# Copyright (c) 2026 Sealeria
# SPDX-License-Identifier: AGPL-3.0-or-later
# Licensed under the GNU Affero General Public License v3.0

"""Connect protocol framing: [flags:1][len:4 BE][payload], optional gzip (flag bit 0)."""

from __future__ import annotations

import gzip
from collections.abc import Callable

from gklog import get_logger

log = get_logger(__name__)

Frame = tuple[int, bytes]  # (flags, payload)


def iter_frames(body: bytes) -> list[Frame] | None:
    """Parse Connect body into frames; None if not framed."""
    if len(body) < 5:
        return None
    frames: list[Frame] = []
    i = 0
    while i + 5 <= len(body):
        flags = body[i]
        ln = int.from_bytes(body[i + 1 : i + 5], "big")
        i += 5
        if ln < 0 or i + ln > len(body):
            return None
        frames.append((flags, body[i : i + ln]))
        i += ln
    if i != len(body):
        return None
    return frames


def encode_frames(frames: list[Frame]) -> bytes:
    out = bytearray()
    for flags, payload in frames:
        out.append(flags)
        out.extend(len(payload).to_bytes(4, "big"))
        out.extend(payload)
    return bytes(out)


def drain_complete_frames(buf: bytes) -> tuple[bytes, list[Frame]]:
    """Return (remainder, complete frames) from a possibly partial buffer."""
    frames: list[Frame] = []
    i = 0
    n = len(buf)
    while i + 5 <= n:
        flags = buf[i]
        ln = int.from_bytes(buf[i + 1 : i + 5], "big")
        end = i + 5 + ln
        if end > n:
            break
        frames.append((flags, buf[i + 5 : end]))
        i = end
    return buf[i:], frames


def frame_to_bytes(flags: int, payload: bytes, *, decompress: bool = False) -> bytes:
    if decompress and (flags & 0x01):
        try:
            payload = gzip.decompress(payload)
            flags &= ~0x01
        except OSError:
            pass
    return bytes([flags]) + len(payload).to_bytes(4, "big") + payload


class ConnectFrameBuffer:
    """Reassemble Connect frames split across h2 DATA chunks."""

    def __init__(self, *, decompress: bool = False) -> None:
        self._buf = b""
        self._decompress = decompress

    def feed(self, chunk: bytes) -> list[bytes]:
        self._buf += chunk
        out: list[bytes] = []
        while True:
            self._buf, frames = drain_complete_frames(self._buf)
            if not frames:
                break
            for flags, payload in frames:
                out.append(frame_to_bytes(flags, payload, decompress=self._decompress))
        return out

    def flush_complete(self) -> list[bytes]:
        """Emit complete frames still held in the reassembly buffer."""
        return self.feed(b"")


def debug_connect_chunk(data: bytes, *, label: str = "chunk") -> bool:
    """Log Connect envelope flags; True when EndStream (0x02) bit is set."""
    if len(data) < 5:
        if data:
            log.debug(f"[PROXY DEBUG] {label}: {len(data)}B (partial envelope head)")
        return False
    flags = data[0]
    is_end = bool(flags & 0x02)
    log.debug(
        f"[PROXY DEBUG] {label}: {len(data)}B flags={hex(flags)} "
        f"IsEndStream={is_end} payload_len={int.from_bytes(data[1:5], 'big')}"
    )
    return is_end


def decompress_connect_body(body: bytes) -> bytes:
    """Strip gzip from Connect frames for clients that expect plain envelopes."""
    frames = iter_frames(body)
    if frames is None:
        return body
    out: list[Frame] = []
    changed = False
    for flags, payload in frames:
        if flags & 0x01:
            try:
                payload = gzip.decompress(payload)
                flags &= ~0x01
                changed = True
            except OSError:
                pass
        out.append((flags, payload))
    return encode_frames(out) if changed else body


def map_frame_payloads(
    body: bytes,
    fn: Callable[[bytes], bytes],
    *,
    skip_end_stream: bool = True,
) -> tuple[bytes, bool]:
    """Apply fn to each frame payload (decompress gzip first if flagged)."""
    frames = iter_frames(body)
    if frames is None:
        new = fn(body)
        return new, new != body

    out: list[Frame] = []
    changed = False
    for flags, payload in frames:
        if skip_end_stream and (flags & 0x02):
            out.append((flags, payload))
            continue
        raw = payload
        compressed = bool(flags & 0x01)
        if compressed:
            try:
                raw = gzip.decompress(payload)
            except OSError:
                raw = payload
                compressed = False
        mapped = fn(raw)
        if mapped != raw:
            changed = True
        if compressed:
            new_payload = gzip.compress(mapped, compresslevel=6)
            out.append((flags | 0x01, new_payload))
        else:
            out.append((flags, mapped))
    if not changed:
        return body, False
    return encode_frames(out), True
