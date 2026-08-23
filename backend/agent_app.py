# Copyright (c) 2026 Sealeria
# SPDX-License-Identifier: AGPL-3.0-or-later
# Licensed under the GNU Affero General Public License v3.0

"""Cursor agent listener ASGI app (Hypercorn h2c)."""

from __future__ import annotations

from fastapi import FastAPI, Request, Response

from core.cursor_agent import forward_agent_bidi
from core.providers import base_url_for
from gklog import get_logger

log = get_logger(__name__)
_STRIPPED = frozenset(
    {"host", "content-length", "connection", "accept-encoding", "transfer-encoding"}
)

agent_app = FastAPI(title="Gatekeep Agent")


@agent_app.api_route("/{path:path}", methods=["POST"])
async def agent_post(request: Request, path: str) -> Response:
    path_l = (path or "").lstrip("/").lower()
    if not path_l.startswith("agent.v1."):
        return Response(status_code=404, content="not found")
    headers = {
        k: v for k, v in request.headers.items() if k.lower() not in _STRIPPED
    }
    upstream = base_url_for("cursor", path)
    log.info(f"[PROXY] agent hypercorn POST /{path} -> {upstream}")
    return await forward_agent_bidi(request, path, headers, upstream)


@agent_app.api_route("/{path:path}", methods=["GET", "HEAD"])
async def agent_probe(_request: Request, path: str) -> Response:
    if path in ("", "/"):
        return Response(status_code=200, content="gatekeep-agent")
    if path.rstrip("/") == "json/version":
        return Response(status_code=404, content="")
    return Response(status_code=404, content="not found")
