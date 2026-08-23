# Copyright (c) 2026 Sealeria
# SPDX-License-Identifier: AGPL-3.0-or-later
# Licensed under the GNU Affero General Public License v3.0

"""Raw ASGI agent listener: send http.response.start before reading h2 bidi body."""

from __future__ import annotations

import asyncio
import time

import database
from core.cursor_agent import _relay
from core.providers import base_url_for
from gklog import get_logger

log = get_logger(__name__)

_STRIPPED = frozenset(
    {"host", "content-length", "connection", "accept-encoding", "transfer-encoding"}
)
_HOP = _STRIPPED


async def _handle_run(receive, send, path: str, headers: dict[str, str], upstream: str) -> None:
    settings = await asyncio.to_thread(database.get_settings)
    crush = bool(settings.get("compression_enabled", 1))
    aggressive = crush and bool(settings.get("aggressive_enabled", 1))
    start = time.perf_counter()
    turn_done = asyncio.Event()
    client_queue: asyncio.Queue[bytes | None] = asyncio.Queue()
    resp_queue: asyncio.Queue = asyncio.Queue()
    meta: dict = {"status": 200, "headers": {}, "ready": asyncio.Event()}
    relay_result: dict[str, int] = {}
    client_total = 0
    client_open = True

    async def body_iter():
        while True:
            chunk = await client_queue.get()
            if chunk is None:
                return
            yield chunk

    async def run_relay() -> None:
        try:
            orig, sent, proxy_ms = await _relay(
                body_iter(),
                resp_queue,
                meta,
                upstream_base=upstream,
                path=path,
                headers=headers,
                crush=crush,
                aggressive=aggressive,
                turn_done=turn_done,
            )
            relay_result["orig"] = orig
            relay_result["sent"] = sent
            relay_result["proxy_ms"] = proxy_ms
        except Exception as exc:
            log.warning(f"[PROXY] agent relay error /{path}: {exc}")
            resp_queue.put_nowait(None)

    # h2 bidi: response must start before Hypercorn delivers request body chunks
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"application/connect+proto")],
        }
    )
    log.info(f"[PROXY] agent asgi response.start /{path} -> {upstream}")

    relay_task = asyncio.create_task(run_relay())
    resp_wait: asyncio.Task | None = asyncio.create_task(resp_queue.get())
    recv_wait: asyncio.Task | None = asyncio.create_task(receive())

    resp_bytes = 0
    try:
        while True:
            wait_set: set[asyncio.Task] = set()
            if resp_wait is not None:
                wait_set.add(resp_wait)
            if recv_wait is not None:
                wait_set.add(recv_wait)
            if not wait_set:
                break
            done, _ = await asyncio.wait(wait_set, return_when=asyncio.FIRST_COMPLETED)

            if resp_wait is not None and resp_wait in done:
                chunk = resp_wait.result()
                resp_wait = asyncio.create_task(resp_queue.get())
                if chunk is None:
                    break
                resp_bytes += len(chunk)
                await send({"type": "http.response.body", "body": chunk, "more_body": True})
                if resp_bytes and resp_bytes % 5000 < len(chunk):
                    log.info(f"[PROXY] agent asgi -> client {resp_bytes}B so far")

            if recv_wait is not None and recv_wait in done:
                message = recv_wait.result()
                recv_wait = asyncio.create_task(receive())
                mtype = message.get("type", "")
                if mtype == "http.request":
                    chunk = message.get("body", b"")
                    if chunk:
                        client_total += len(chunk)
                        await client_queue.put(chunk)
                elif mtype == "http.disconnect":
                    client_open = False
                    recv_wait = None
                    await client_queue.put(None)
                else:
                    log.debug(f"[PROXY] agent asgi recv {mtype}")
    finally:
        log.debug(f"[PROXY] agent asgi client body {client_total}B open={client_open}")
        await send({"type": "http.response.body", "body": b"", "more_body": False})
        turn_done.set()
        if recv_wait is not None:
            recv_wait.cancel()
        if resp_wait is not None:
            resp_wait.cancel()
        await relay_task

    log.info(f"[PROXY] agent asgi -> client {resp_bytes}B")
    orig = relay_result.get("orig", 0)
    sent = relay_result.get("sent", 0)
    saved = max(0, orig - sent)
    tag = (
        "wire_proto_crush_aggressive"
        if saved > 0 and aggressive
        else ("wire_proto_crush" if saved > 0 else "passthrough")
    )
    ot, st = max(1, orig // 4), max(1, sent // 4)
    latency = relay_result.get("proxy_ms", round((time.perf_counter() - start) * 1000))
    if saved:
        pct = 100 * saved / orig if orig else 0
        log.info(f"[OPTIMIZER] [cursor] agent {ot} -> {st} | Saved: {ot - st} ({pct:.1f}%) [{tag}]")
    await asyncio.to_thread(
        database.log_request,
        None,
        "cursor",
        ot,
        st,
        0,
        latency,
        tag,
        path[:200],
        f"[agent {orig}->{sent}B]" if saved else "",
    )


async def agent_asgi_app(scope, receive, send) -> None:
    if scope.get("type") != "http":
        return
    method = scope.get("method", "GET")
    path = (scope.get("path") or "/").lstrip("/")

    if method in ("GET", "HEAD") and path in ("", "/"):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        body = b"gatekeep-agent" if method == "GET" else b""
        await send({"type": "http.response.body", "body": body, "more_body": False})
        return

    if method != "POST" or not path.lower().startswith("agent.v1."):
        await send({"type": "http.response.start", "status": 404, "headers": []})
        await send({"type": "http.response.body", "body": b"not found", "more_body": False})
        return

    headers = {
        k.decode("latin-1"): v.decode("latin-1")
        for k, v in scope.get("headers", [])
        if k.decode("latin-1").lower() not in _HOP
    }
    upstream = base_url_for("cursor", path)
    await _handle_run(receive, send, path, headers, upstream)
