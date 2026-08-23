# Copyright (c) 2026 Sealeria
# SPDX-License-Identifier: AGPL-3.0-or-later
# Licensed under the GNU Affero General Public License v3.0

﻿from __future__ import annotations

import gzip

_ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
_GZIP_MAGIC = b"\x1f\x8b"
_MAX_OUT = 256 * 1024 * 1024


def decode_request_body(body: bytes, headers: dict[str, str]) -> tuple[bytes, bool]:
    """Return (body, decoded). On success, strip Content-Encoding from headers."""
    if not body:
        return body, False

    enc = ""
    for key, val in headers.items():
        if key.lower() == "content-encoding":
            enc = (val or "").strip().lower()
            break

    codecs = [c.strip() for c in enc.split(",") if c.strip()] if enc else []
    if not codecs:
        if body.startswith(_ZSTD_MAGIC):
            codecs = ["zstd"]
        elif body.startswith(_GZIP_MAGIC):
            codecs = ["gzip"]
        else:
            return body, False
    if len(codecs) != 1:
        return body, False

    codec = codecs[0]
    try:
        if codec in ("zstd", "zst"):
            import zstandard as zstd

            out = zstd.ZstdDecompressor().decompress(body, max_output_size=_MAX_OUT)
        elif codec in ("gzip", "x-gzip"):
            out = gzip.decompress(body)
            if len(out) > _MAX_OUT:
                return body, False
        else:
            return body, False
    except Exception:
        return body, False

    for key in list(headers.keys()):
        if key.lower() == "content-encoding":
            del headers[key]
    return out, True
