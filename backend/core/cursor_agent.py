# Copyright (c) 2026 Sealeria
# SPDX-License-Identifier: AGPL-3.0-or-later
# Licensed under the GNU Affero General Public License v3.0

"""HTTP/2 duplex relay for Cursor agent.v1.AgentService/Run."""

from __future__ import annotations

import asyncio
import ssl
import time
from collections.abc import AsyncIterator
from urllib.parse import urlparse

import h2.config
import h2.connection
import h2.events
from fastapi import Request
from fastapi.responses import Response, StreamingResponse

import database
from engines.connect_frames import ConnectFrameBuffer
from engines import wirecrush
from gklog import get_logger

log = get_logger(__name__)

DEFAULT_AGENT_UPSTREAM = "https://agentn.global.api5.cursor.sh"

_warm_lock = asyncio.Lock()
_warm_host = ""
_session: "_UpstreamSession | None" = None


class H2FlowControl:
    """Unblocks outbound writers when WINDOW_UPDATE arrives."""

    def __init__(self) -> None:
        self._waiters: dict[int, asyncio.Event] = {}

    def notify(self, event: h2.events.WindowUpdated) -> None:
        if event.stream_id == 0:
            for ev in list(self._waiters.values()):
                ev.set()
            self._waiters.clear()
            return
        ev = self._waiters.pop(event.stream_id, None)
        if ev is not None:
            ev.set()

    async def wait_window(self, conn: h2.connection.H2Connection, stream_id: int) -> None:
        while conn.local_flow_control_window(stream_id) < 1:
            ev = asyncio.Event()
            self._waiters[stream_id] = ev
            await ev.wait()


async def send_h2_data(
    conn: h2.connection.H2Connection,
    writer: asyncio.StreamWriter,
    fc: H2FlowControl,
    stream_id: int,
    data: bytes,
    *,
    end_stream: bool = False,
    lock: asyncio.Lock | None = None,
) -> None:
    offset = 0
    total = len(data)

    async def _window() -> int:
        if lock is not None:
            async with lock:
                return conn.local_flow_control_window(stream_id)
        return conn.local_flow_control_window(stream_id)

    while True:
        while await _window() < 1:
            await fc.wait_window(conn, stream_id)
        window = await _window()
        if offset >= total:
            if end_stream:
                if lock is not None:
                    async with lock:
                        conn.send_data(stream_id, b"", end_stream=True)
                        out = conn.data_to_send()
                else:
                    conn.send_data(stream_id, b"", end_stream=True)
                    out = conn.data_to_send()
                if out:
                    writer.write(out)
                    await writer.drain()
            return
        chunk_size = min(window, total - offset, conn.max_outbound_frame_size)
        is_last = end_stream and offset + chunk_size >= total
        if lock is not None:
            async with lock:
                conn.send_data(stream_id, data[offset : offset + chunk_size], end_stream=is_last)
                out = conn.data_to_send()
        else:
            conn.send_data(stream_id, data[offset : offset + chunk_size], end_stream=is_last)
            out = conn.data_to_send()
        if out:
            writer.write(out)
            await writer.drain()
        offset += chunk_size
        if is_last:
            return


class _RelayConnection:
    """One upstream h2 connection per agent Run (no shared stream state)."""

    def __init__(self) -> None:
        self._host = ""
        self._conn: h2.connection.H2Connection | None = None
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._reader_task: asyncio.Task | None = None
        self._writer_task: asyncio.Task | None = None
        self._out_queue: asyncio.Queue | None = None
        self._fc = H2FlowControl()
        self._conn_lock = asyncio.Lock()
        self._sid: int | None = None
        self._resp_queue: asyncio.Queue | None = None
        self._meta: dict | None = None

    async def _connect(self, upstream_base: str) -> None:
        parsed = urlparse(upstream_base)
        host = parsed.hostname or ""
        port = parsed.port or 443
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(
                host, port, ssl=ssl.create_default_context(), server_hostname=host
            ),
            timeout=15,
        )
        conn = h2.connection.H2Connection(
            config=h2.config.H2Configuration(client_side=True)
        )
        conn.initiate_connection()
        writer.write(conn.data_to_send())
        await writer.drain()
        self._host = host
        self._conn = conn
        self._reader = reader
        self._writer = writer
        self._reader_task = asyncio.create_task(self._read_loop())

    async def _read_loop(self) -> None:
        assert self._reader is not None and self._conn is not None
        try:
            while True:
                data = await self._reader.read(65536)
                if not data:
                    break
                async with self._conn_lock:
                    events = list(self._conn.receive_data(data))
                batch_data: list[bytes] = []
                batch_trailers = None
                batch_eof = False
                for event in events:
                    sid = getattr(event, "stream_id", None)
                    if isinstance(event, h2.events.ResponseReceived):
                        if sid == self._sid and self._meta is not None:
                            status = 0
                            for k, v in event.headers:
                                ks = k.decode() if isinstance(k, bytes) else k
                                vs = v.decode() if isinstance(v, bytes) else str(v)
                                if ks == ":status":
                                    status = int(vs)
                                    self._meta["status"] = status
                                elif not ks.startswith(":"):
                                    self._meta["headers"][ks] = vs
                            log.debug(f"[PROXY] agent upstream status={status}")
                            self._meta["ready"].set()
                    elif isinstance(event, h2.events.DataReceived):
                        if sid == self._sid and self._resp_queue is not None:
                            async with self._conn_lock:
                                self._conn.acknowledge_received_data(
                                    event.flow_controlled_length, sid
                                )
                                out = self._conn.data_to_send()
                            if out and self._writer is not None:
                                self._writer.write(out)
                                await self._writer.drain()
                            log.debug(f"[PROXY] agent upstream data {len(event.data)}B")
                            batch_data.append(event.data)
                    elif isinstance(event, h2.events.TrailersReceived):
                        if sid == self._sid:
                            batch_trailers = event.headers
                    elif isinstance(event, h2.events.StreamEnded):
                        if sid == self._sid:
                            batch_eof = True
                    elif isinstance(event, h2.events.StreamReset):
                        if sid == self._sid and self._resp_queue is not None:
                            self._resp_queue.put_nowait(None)
                    elif isinstance(event, h2.events.WindowUpdated):
                        self._fc.notify(event)
                    elif isinstance(event, h2.events.ConnectionTerminated):
                        if self._resp_queue is not None:
                            self._resp_queue.put_nowait(None)
                        return
                if self._sid is not None and self._resp_queue is not None:
                    for chunk in batch_data:
                        self._resp_queue.put_nowait(chunk)
                    if batch_trailers is not None:
                        log.debug(f"[PROXY] agent upstream trailers sid={self._sid}")
                        self._resp_queue.put_nowait(("trailers", batch_trailers))
                    if batch_eof:
                        self._resp_queue.put_nowait(None)
                async with self._conn_lock:
                    await self._flush()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning(f"[PROXY] agent upstream read error: {exc}")
            if self._resp_queue is not None:
                self._resp_queue.put_nowait(None)

    async def _flush(self) -> None:
        if self._conn is None or self._writer is None:
            return
        out = self._conn.data_to_send()
        if out:
            self._writer.write(out)
            await self._writer.drain()

    async def _write_loop(self) -> None:
        assert self._conn is not None and self._writer is not None
        assert self._out_queue is not None and self._sid is not None
        conn = self._conn
        sid = self._sid
        try:
            while True:
                msg = await self._out_queue.get()
                if msg is None:
                    break
                if msg["type"] == "DATA":
                    await send_h2_data(
                        conn,
                        self._writer,
                        self._fc,
                        sid,
                        msg["data"],
                        end_stream=False,
                        lock=self._conn_lock,
                    )
                elif msg["type"] == "END_STREAM":
                    await send_h2_data(
                        conn,
                        self._writer,
                        self._fc,
                        sid,
                        b"",
                        end_stream=True,
                        lock=self._conn_lock,
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning(f"[PROXY] agent upstream write error: {exc}")

    async def close(self) -> None:
        if self._out_queue is not None:
            self._out_queue.put_nowait(None)
        if self._writer_task is not None:
            self._writer_task.cancel()
            try:
                await self._writer_task
            except asyncio.CancelledError:
                pass
            self._writer_task = None
        self._out_queue = None
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
            self._reader_task = None
        if self._writer is not None:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except OSError:
                pass
        self._writer = None
        self._reader = None
        self._conn = None
        self._sid = None
        self._resp_queue = None
        self._meta = None

    async def relay(
        self,
        body_iter: AsyncIterator[bytes],
        resp_queue: asyncio.Queue,
        meta: dict,
        *,
        upstream_base: str,
        path: str,
        headers: dict[str, str],
        crush: bool,
        aggressive: bool = False,
        turn_done: asyncio.Event | None = None,
    ) -> tuple[int, int, int]:
        t0 = time.perf_counter()
        await self._connect(upstream_base)
        assert self._conn is not None
        conn = self._conn
        sid = conn.get_next_available_stream_id()
        self._sid = sid
        self._resp_queue = resp_queue
        self._meta = meta
        h2h = _upstream_h2_headers(self._host, path, headers)
        async with self._conn_lock:
            conn.send_headers(sid, h2h, end_stream=False)
            await self._flush()

        orig = 0
        sent = 0
        proxy_ms = 0
        done = turn_done or asyncio.Event()
        body_done = asyncio.Event()
        self._out_queue = asyncio.Queue()
        self._writer_task = asyncio.create_task(self._write_loop())

        async def heartbeats() -> None:
            try:
                while not body_done.is_set() and not done.is_set():
                    await asyncio.sleep(5)
                    if body_done.is_set() or done.is_set():
                        return
                    if self._out_queue is not None:
                        await self._out_queue.put({"type": "DATA", "data": _HEARTBEAT_FRAME})
            except Exception:
                pass

        hb_task = asyncio.create_task(heartbeats())
        req_buf = ConnectFrameBuffer()
        seen: set[str] = set()
        try:
            async for chunk in body_iter:
                if not chunk:
                    continue
                orig += len(chunk)
                for frame_wire in req_buf.feed(chunk):
                    out = frame_wire
                    if crush and len(frame_wire) > 20:
                        crushed, saved_bytes = wirecrush.crush_connect_body(
                            frame_wire,
                            aggressive=aggressive,
                            seen=seen if aggressive else None,
                        )
                        out = crushed
                        if len(frame_wire) > 2000:
                            pct = 100 * saved_bytes / len(frame_wire) if frame_wire else 0
                            log.debug(
                                f"[PROXY] agent crush frame "
                                f"{len(frame_wire)}->{len(crushed)} ({pct:.0f}% saved)"
                            )
                            if aggressive and pct < 70:
                                wirecrush.capture_agent_frame(
                                    frame_wire, crushed, aggressive=aggressive
                                )
                    sent += len(out)
                    await self._out_queue.put({"type": "DATA", "data": out})
            if req_buf._buf:
                tail = req_buf._buf
                req_buf._buf = b""
                out = tail
                if crush and len(tail) > 20:
                    crushed, _ = wirecrush.crush_connect_body(
                        tail,
                        aggressive=aggressive,
                        seen=seen if aggressive else None,
                    )
                    out = crushed
                sent += len(out)
                await self._out_queue.put({"type": "DATA", "data": out})
            body_done.set()
            hb_task.cancel()
            try:
                await hb_task
            except asyncio.CancelledError:
                pass
            log.debug(f"[PROXY] agent upstream req done orig={orig} sent={sent}")
            proxy_ms = round((time.perf_counter() - t0) * 1000)
            await self._out_queue.put({"type": "END_STREAM"})
            await self._out_queue.put(None)
            if self._writer_task is not None:
                await self._writer_task
                self._writer_task = None
            await done.wait()
        except Exception as exc:
            log.warning(f"[PROXY] agent upstream req error: {exc}")
            if self._resp_queue is not None:
                self._resp_queue.put_nowait(None)
            raise
        finally:
            done.set()
            hb_task.cancel()
            try:
                await hb_task
            except asyncio.CancelledError:
                pass
        return orig, sent, proxy_ms


class _UpstreamSession:
    """Legacy warm-only helper (TLS preconnect)."""

    def __init__(self) -> None:
        self._host = ""

    async def warm(self, upstream_base: str) -> None:
        global _warm_host
        parsed = urlparse(upstream_base)
        host = parsed.hostname or ""
        if _warm_host == host:
            return
        port = parsed.port or 443
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(
                host, port, ssl=ssl.create_default_context(), server_hostname=host
            ),
            timeout=15,
        )
        writer.close()
        await writer.wait_closed()
        _warm_host = host
        self._host = host
        log.debug(f"[PROXY] agent upstream h2 warm OK {host}")


def _get_session() -> _UpstreamSession:
    global _session
    if _session is None:
        _session = _UpstreamSession()
    return _session


async def warm_upstream(upstream_base: str) -> None:
    try:
        await _get_session().warm(upstream_base)
    except Exception as exc:
        log.warning(f"[PROXY] agent upstream warm failed: {exc}")

_HOP = frozenset(
    {"host", "content-length", "connection", "transfer-encoding", "accept-encoding"}
)
_KEEP_HEADERS = frozenset(
    {
        "authorization",
        "x-cursor-checksum",
        "cursor-client-version",
        "x-cursor-client-version",
        "x-cursor-client-type",
        "x-ghost-mode",
        "x-original-request-id",
        "x-request-id",
        "connect-protocol-version",
        "connect-content-encoding",
        "connect-accept-encoding",
        "content-type",
        "user-agent",
        "traceparent",
        "tracestate",
    }
)

# Connect frame: protobuf field 7 empty (Cursor agent heartbeat)
_HEARTBEAT_FRAME = b"\x00\x00\x00\x00\x02\x3a\x00"


async def _flush(conn: h2.connection.H2Connection, writer: asyncio.StreamWriter) -> None:
    out = conn.data_to_send()
    if out:
        writer.write(out)
        await writer.drain()


def _upstream_h2_headers(host: str, path: str, headers: dict[str, str]) -> list[tuple[str, str]]:
    req_path = "/" + path.lstrip("/")
    h2h: list[tuple[str, str]] = [
        (":method", "POST"),
        (":path", req_path),
        (":authority", host),
        (":scheme", "https"),
    ]
    seen: set[str] = set()
    for key in _KEEP_HEADERS:
        if key == "connect-accept-encoding":
            continue
        for k, v in headers.items():
            if k.lower() == key and key not in seen:
                h2h.append((key, str(v)))
                seen.add(key)
                break
    for k, v in headers.items():
        kl = k.lower()
        if kl in _HOP or kl.startswith(":") or kl in seen:
            continue
        h2h.append((kl, str(v)))
        seen.add(kl)
    if not any(h[0] == "te" for h in h2h):
        h2h.append(("te", "trailers"))
    return h2h


async def _relay(
    body_iter: AsyncIterator[bytes],
    resp_queue: asyncio.Queue,
    meta: dict,
    *,
    upstream_base: str,
    path: str,
    headers: dict[str, str],
    crush: bool,
    aggressive: bool = False,
    turn_done: asyncio.Event | None = None,
) -> tuple[int, int, int]:
    upstream = (upstream_base or "").strip() or DEFAULT_AGENT_UPSTREAM
    relay = _RelayConnection()
    try:
        return await relay.relay(
            body_iter,
            resp_queue,
            meta,
            upstream_base=upstream,
            path=path,
            headers=headers,
            crush=crush,
            aggressive=aggressive,
            turn_done=turn_done,
        )
    finally:
        await relay.close()


async def forward_agent_bidi(
    request: Request,
    path: str,
    headers: dict[str, str],
    upstream_base: str,
) -> Response:
    settings = await asyncio.to_thread(database.get_settings)
    crush = bool(settings.get("compression_enabled", 1))
    # Agent duplex: mild wirecrush only. Aggressive protobuf crush broke tool
    # frames and caused reconnect/loops. Ask-mode JSON paths still use dashboard toggles.
    aggressive = False
    upstream = (upstream_base or "").strip() or DEFAULT_AGENT_UPSTREAM

    resp_queue: asyncio.Queue = asyncio.Queue()
    client_queue: asyncio.Queue[bytes | None] = asyncio.Queue()
    meta: dict = {"status": 200, "headers": {}, "ready": asyncio.Event()}
    start = time.perf_counter()
    relay_result: dict[str, int] = {}
    turn_done = asyncio.Event()

    async def body_iter() -> AsyncIterator[bytes]:
        while True:
            chunk = await client_queue.get()
            if chunk is None:
                return
            yield chunk

    async def stream() -> AsyncIterator[bytes]:
        relay_exc: BaseException | None = None
        client_total = 0
        client_open = True

        async def run_relay() -> None:
            nonlocal relay_exc
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
                relay_exc = exc
                log.warning(f"[PROXY] agent relay error /{path}: {exc}")
                resp_queue.put_nowait(None)

        relay_task = asyncio.create_task(run_relay())
        resp_wait: asyncio.Task | None = asyncio.create_task(resp_queue.get())
        recv_wait: asyncio.Task | None = asyncio.create_task(request.receive())

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
                    yield chunk

                if recv_wait is not None and recv_wait in done:
                    message = recv_wait.result()
                    recv_wait = None
                    if message["type"] == "http.request":
                        chunk = message.get("body", b"")
                        if chunk:
                            client_total += len(chunk)
                            await client_queue.put(chunk)
                        if message.get("more_body", False):
                            recv_wait = asyncio.create_task(request.receive())
                        else:
                            client_open = False
                            await client_queue.put(None)
                    elif message["type"] == "http.disconnect":
                        client_open = False
                        await client_queue.put(None)
        finally:
            log.debug(f"[PROXY] agent client body {client_total}B open={client_open}")
            turn_done.set()
            if recv_wait is not None:
                recv_wait.cancel()
            if resp_wait is not None:
                resp_wait.cancel()
            await relay_task

        if relay_exc:
            raise relay_exc

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
            log.info(
                f"[OPTIMIZER] [cursor] agent {ot} -> {st} "
                f"| Saved: {ot - st} ({pct:.1f}%) [{tag}]"
            )
        else:
            log.debug(f"[PROXY] agent passthrough /{path} bytes={orig}")
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

    log.info(f"[PROXY] agent.v1 /{path} -> {upstream} (connect duplex)")
    return StreamingResponse(
        stream(),
        status_code=200,
        headers={"content-type": "application/connect+proto"},
        media_type="application/connect+proto",
    )
