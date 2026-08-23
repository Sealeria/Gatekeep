# Copyright (c) 2026 Sealeria
# SPDX-License-Identifier: AGPL-3.0-or-later
# Licensed under the GNU Affero General Public License v3.0

"""Rewrite Cursor GetServerConfig agent URLs back through Gatekeep."""

from __future__ import annotations

from engines.connect_frames import map_frame_payloads
from engines.wirecrush import (
    _encode_field,
    _looks_like_message,
    _parse_fields,
    _read_varint,
    _write_varint,
)

# aiserver.v1.GetServerConfigResponse.http2_config (enum Http2Config)
_HTTP2_CONFIG_FIELD = 7
# HTTP2_CONFIG_FORCE_BIDI_DISABLED = 3 (cursor-grpc proto; forces HTTP/1.1 agent)
_FORCE_BIDI_DISABLED = 3

_BLOCKED = frozenset({
    "api2.cursor.sh",
    "api3.cursor.sh",
    "api.cursor.sh",
    "repo42.cursor.sh",
    "staging.cursor.sh",
    "dev-staging.cursor.sh",
})

_agent_upstream: str | None = None


def agent_public_url() -> str:
    from config import agent_public_url as _agent_public_url

    return _agent_public_url()


def is_server_config_path(path: str) -> bool:
    p = (path or "").lstrip("/").lower()
    return "getserverconfig" in p or "serverconfigservice" in p


def get_agent_upstream() -> str | None:
    return _agent_upstream


def _remember_agent_upstream(url: str) -> None:
    global _agent_upstream
    u = url.strip().rstrip("/")
    if u.startswith("https://"):
        _agent_upstream = u


def _is_agent_run_url(url: str) -> bool:
    s = url.strip()
    if not s.startswith("https://"):
        return False
    try:
        from urllib.parse import urlparse

        host = (urlparse(s).hostname or "").lower()
    except ValueError:
        return False
    if not host.endswith(".cursor.sh"):
        return False
    if host in _BLOCKED:
        return False
    return host.startswith(("agent.", "agentn.", "agent-gcpp", "agent-"))


def _rewrite_agent_url(s: str, gatekeep: str) -> str:
    if not _is_agent_run_url(s):
        return s
    _remember_agent_upstream(s)
    return gatekeep.rstrip("/")


def _walk_strings(data: bytes, gatekeep: str) -> tuple[bytes, bool]:
    try:
        fields = _parse_fields(data)
    except ValueError:
        return data, False
    out = bytearray()
    changed = False
    for fn, wt, val in fields:
        if wt == 0 and fn == _HTTP2_CONFIG_FIELD:
            if val != bytes([_FORCE_BIDI_DISABLED]):
                val = bytes([_FORCE_BIDI_DISABLED])
                changed = True
        elif wt == 2:
            try:
                s = val.decode("utf-8")
            except UnicodeDecodeError:
                s = None
            if s is not None and _is_agent_run_url(s):
                new = _rewrite_agent_url(s, gatekeep)
                if new != s:
                    val = new.encode("utf-8")
                    changed = True
            elif _looks_like_message(val):
                nested_val, nested_changed = _walk_strings(val, gatekeep)
                if nested_changed:
                    val = nested_val
                    changed = True
        out.extend(_encode_field(fn, wt, val))
    return (bytes(out) if changed else data), changed


def rewrite_protobuf_agent_urls(data: bytes, gatekeep: str) -> bytes:
    new, _ = _walk_strings(data, gatekeep)
    return new


def rewrite_connect_response(body: bytes, gatekeep: str) -> bytes:
    if not body:
        return body

    def mapper(raw: bytes) -> bytes:
        return rewrite_protobuf_agent_urls(raw, gatekeep)

    mapped, changed = map_frame_payloads(body, mapper)
    if changed:
        return mapped
    return rewrite_protobuf_agent_urls(body, gatekeep)


def maybe_rewrite_server_config(path: str, body: bytes, gatekeep: str) -> tuple[bytes, list[str]]:
    if not is_server_config_path(path):
        return body, []
    if not body or not gatekeep:
        return body, []

    before = _agent_upstream
    new_body = rewrite_connect_response(body, gatekeep)
    rewrites: list[str] = []
    if new_body != body:
        rewrites.append(f"agent_urls->{gatekeep}")
    elif _agent_upstream:
        rewrites.append(f"agent_urls_unchanged->{gatekeep}")
    if _agent_upstream and _agent_upstream != before:
        rewrites.append(f"cached_upstream={_agent_upstream}")
    return new_body, rewrites
