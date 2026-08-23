# Copyright (c) 2026 Sealeria
# SPDX-License-Identifier: AGPL-3.0-or-later
# Licensed under the GNU Affero General Public License v3.0

"""Cursor agent listener (HTTP/1.1 or HTTP/2 prior-knowledge)."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from typing import Any

import h2.config
import h2.connection
import h2.events
from h2.connection import ConnectionState

import database
from engines.connect_frames import ConnectFrameBuffer, debug_connect_chunk
from core.cursor_agent import H2FlowControl, send_h2_data, _relay
from core.providers import base_url_for
from gklog import get_logger

log = get_logger(__name__)

_H2_PREFACE = b"PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n"
_AGENT_RUN_PATH = "/agent.v1.AgentService/Run"
_H2_FRAME = {0: "DATA", 1: "HEADERS", 3: "RST", 4: "SETTINGS", 7: "GOAWAY", 8: "WIN"}


def _dump_h2_frames(buf: bytes, label: str) -> None:
    pos = 24 if buf.startswith(_H2_PREFACE) else 0
    out: list[str] = []
    while pos + 9 <= len(buf):
        ln = int.from_bytes(buf[pos : pos + 3], "big")
        ftype = buf[pos + 3]
        sid = int.from_bytes(buf[pos + 5 : pos + 9], "big") & 0x7FFFFFFF
        name = _H2_FRAME.get(ftype, str(ftype))
        out.append(f"{name}(sid={sid},len={ln})")
        pos += 9 + ln
    log.info(f"[PROXY] agent h2 frames {label}: {' -> '.join(out[:20])}")


_HOP = frozenset(
    {"host", "content-length", "connection", "transfer-encoding", "accept-encoding"}
)


async def _flush(conn: h2.connection.H2Connection, writer: asyncio.StreamWriter) -> None:
    out = conn.data_to_send()
    if out:
        writer.write(out)
        await writer.drain()


_CONNECT_END_STREAM = b"\x02\x00\x00\x00\x00"


def _normalize_h2_trailers(hdrs) -> list[tuple[bytes, bytes]]:
    out: list[tuple[bytes, bytes]] = []
    if not hdrs:
        return out
    for item in hdrs:
        if hasattr(item, "name") and hasattr(item, "value"):
            k, v = item.name, item.value
        else:
            k, v = item[0], item[1]
        kb = k if isinstance(k, bytes) else str(k).encode("latin-1")
        vb = v if isinstance(v, bytes) else str(v).encode("latin-1")
        out.append((kb, vb))
    return out


def _h2_header_map(headers) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in headers:
        if hasattr(item, "name") and hasattr(item, "value"):
            k, v = item.name, item.value
        else:
            k, v = item[0], item[1]
        ks = k.decode("latin-1") if isinstance(k, bytes) else str(k)
        vs = v.decode("latin-1") if isinstance(v, bytes) else str(v)
        out[ks] = vs
    return out


class _BodyStream:
    def __init__(self) -> None:
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self.client_ended = False

    def push(self, chunk: bytes | None) -> None:
        if chunk is None:
            self.client_ended = True
        self._queue.put_nowait(chunk)

    async def iter_bytes(self) -> AsyncIterator[bytes]:
        while True:
            chunk = await self._queue.get()
            if chunk is None:
                return
            yield chunk


async def _run_relay(
    path: str,
    headers: dict[str, str],
    body: _BodyStream,
    *,
    send_response_start,
    send_data,
    send_trailers,
    end_stream,
) -> None:
    upstream = base_url_for("cursor", path)
    settings = await asyncio.to_thread(database.get_settings)
    crush = bool(settings.get("compression_enabled", 1))
    aggressive = crush and bool(settings.get("aggressive_enabled", 1))
    turn_done = asyncio.Event()
    resp_queue: asyncio.Queue = asyncio.Queue()
    meta: dict = {"status": 200, "headers": {}, "ready": asyncio.Event()}

    async def body_iter() -> AsyncIterator[bytes]:
        async for chunk in body.iter_bytes():
            yield chunk

    relay_task = asyncio.create_task(
        _relay(
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
    )
    log.info(f"[PROXY] agent.v1 /{path} -> {upstream} (duplex relay crush={crush} aggr={aggressive})")

    resp_buf = ConnectFrameBuffer(decompress=True)
    resp_bytes = 0
    saw_end_envelope = False
    pending_trailers = None

    async def _emit_to_client(wire: bytes) -> None:
        nonlocal resp_bytes, saw_end_envelope
        if debug_connect_chunk(wire, label="Upstream->Client"):
            saw_end_envelope = True
        resp_bytes += len(wire)
        await send_data(wire)

    async def _flush_resp_to_client() -> None:
        for wire in resp_buf.flush_complete():
            await _emit_to_client(wire)
        if resp_buf._buf:
            log.debug(
                f"[PROXY DEBUG] resp reassembly tail {len(resp_buf._buf)}B "
                f"hex={resp_buf._buf[:32].hex()}"
            )
            await send_data(resp_buf._buf)
            resp_buf._buf = b""

    async def _half_close_client(*, trailers_sent: bool) -> bool:
        nonlocal saw_end_envelope
        if pending_trailers is not None:
            await send_trailers(pending_trailers)
            return True
        if not trailers_sent:
            if not saw_end_envelope:
                log.debug("[PROXY] agent injecting Connect EndStream envelope (0x02)")
                await send_data(_CONNECT_END_STREAM)
                saw_end_envelope = True
            await end_stream()
        return trailers_sent

    trailers_sent = False
    try:
        while True:
            item = await resp_queue.get()
            if item is None:
                break
            if isinstance(item, tuple) and item[0] == "trailers":
                pending_trailers = item[1]
                continue
            for wire in resp_buf.feed(item):
                await _emit_to_client(wire)
        while not resp_queue.empty():
            late = resp_queue.get_nowait()
            if late is None:
                continue
            if isinstance(late, tuple) and late[0] == "trailers":
                pending_trailers = late[1]
                continue
            for wire in resp_buf.feed(late):
                await _emit_to_client(wire)
    except Exception as exc:
        log.warning(f"[PROXY] agent response stream error: {exc}")
    finally:
        await _flush_resp_to_client()
        trailers_sent = await _half_close_client(trailers_sent=trailers_sent)
        turn_done.set()

    log.debug(
        f"[PROXY] agent upstream -> client {resp_bytes}B "
        f"end_envelope={saw_end_envelope} trailers={trailers_sent}"
    )

    try:
        orig, sent, proxy_ms = await relay_task
    except Exception as exc:
        log.warning(f"[PROXY] agent relay error: {exc}")
        return

    saved = max(0, orig - sent)
    tag = (
        "wire_proto_crush_aggressive"
        if saved > 0 and aggressive
        else ("wire_proto_crush" if saved > 0 else "passthrough")
    )
    ot, st = max(1, orig // 4), max(1, sent // 4)
    if saved:
        pct = 100 * saved / orig if orig else 0
        log.info(
            f"[OPTIMIZER] [cursor] agent {ot} -> {st} | Saved: {ot - st} ({pct:.1f}%) "
            f"[{tag}] proxy={proxy_ms}ms"
        )
    await asyncio.to_thread(
        database.log_request,
        None,
        "cursor",
        ot,
        st,
        0,
        proxy_ms,
        tag,
        path[:200],
        f"[agent {orig}->{sent}B]" if saved else "",
    )


async def _serve_h2(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, first: bytes) -> None:
    conn = h2.connection.H2Connection(config=h2.config.H2Configuration(client_side=False))
    conn_lock = asyncio.Lock()
    fc = H2FlowControl()
    streams: dict[int, dict[str, Any]] = {}
    pending: set[asyncio.Task] = set()

    async def _ack_flow_control(stream_id: int, length: int) -> None:
        """Ack inbound DATA and flush WINDOW_UPDATE before any downstream work."""
        async with conn_lock:
            conn.acknowledge_received_data(length, stream_id)
            out = conn.data_to_send()
        if out:
            writer.write(out)
            await writer.drain()

    async def _flush_locked() -> None:
        async with conn_lock:
            out = conn.data_to_send()
        if out:
            writer.write(out)
            await writer.drain()

    async def on_stream(sid: int, st: dict[str, Any]) -> None:
        async def send_response_start(status: int, headers: dict[str, str]) -> None:
            rh = [(b":status", str(status).encode()), (b"content-type", b"application/connect+proto")]
            for k, v in headers.items():
                kl = k.lower()
                if kl not in ("content-length", "connection", "transfer-encoding", "content-type"):
                    rh.append((kl.encode(), v.encode()))
            async with conn_lock:
                if conn.state_machine.state == ConnectionState.CLOSED:
                    return
                conn.send_headers(sid, rh, end_stream=False)
            await _flush_locked()

        async def send_data(chunk: bytes) -> None:
            async with conn_lock:
                if conn.state_machine.state == ConnectionState.CLOSED:
                    log.debug(f"[PROXY] agent h2 send_data skipped (conn closed) {len(chunk)}B")
                    return
                if not st.get("headers_sent"):
                    conn.send_headers(
                        sid,
                        [
                            (b":status", b"200"),
                            (b"content-type", b"application/connect+proto"),
                        ],
                        end_stream=False,
                    )
                    st["headers_sent"] = True
                    log.debug(f"[PROXY] agent h2 response headers+{len(chunk)}B")
                    out = conn.data_to_send()
                else:
                    out = b""
            if out:
                writer.write(out)
                await writer.drain()
            await send_h2_data(conn, writer, fc, sid, chunk, end_stream=False, lock=conn_lock)

        async def send_trailers(hdrs) -> None:
            th = _normalize_h2_trailers(hdrs)
            async with conn_lock:
                if conn.state_machine.state == ConnectionState.CLOSED:
                    return
                log.debug(f"[PROXY] agent h2 upstream trailers sid={sid} n={len(th)}")
                conn.send_headers(sid, th, end_stream=True)
            await _flush_locked()

        async def end_stream() -> None:
            async with conn_lock:
                if conn.state_machine.state == ConnectionState.CLOSED:
                    return
                log.debug(f"[PROXY] agent h2 response END_STREAM sid={sid}")
                conn.end_stream(sid)
            await _flush_locked()

        try:
            await _run_relay(
                st["path"],
                st["headers"],
                st["body"],
                send_response_start=send_response_start,
                send_data=send_data,
                send_trailers=send_trailers,
                end_stream=end_stream,
            )
        except Exception as exc:
            log.warning(f"[PROXY] agent h2 stream error: {exc}")
            async with conn_lock:
                if conn.state_machine.state != ConnectionState.CLOSED:
                    conn.reset_stream(sid, error_code=0)
            await _flush_locked()

    async def handle_events(events) -> None:
        for event in events:
            ename = type(event).__name__
            if ename not in ("WindowUpdated", "SettingsAcknowledged", "RemoteSettingsChanged"):
                log.debug(f"[PROXY] agent h2 event {ename}")
            if isinstance(event, h2.events.RequestReceived):
                hdrs = _h2_header_map(event.headers)
                path_b = hdrs.get(":path") or _AGENT_RUN_PATH
                if "agent.v1.AgentService/Run" not in path_b:
                    async with conn_lock:
                        conn.reset_stream(event.stream_id, error_code=7)
                    await _flush_locked()
                    continue
                path = path_b.lstrip("/")
                out_headers = {
                    k: v for k, v in hdrs.items() if not k.startswith(":") and k.lower() not in _HOP
                }
                st = {
                    "path": path,
                    "headers": out_headers,
                    "body": _BodyStream(),
                    "headers_sent": False,
                    "client_ended": False,
                }
                streams[event.stream_id] = st
                log.debug(f"[PROXY] agent h2 POST /{path} sid={event.stream_id}")
                # Connect bidi: 200 headers before request body finishes
                async with conn_lock:
                    if conn.state_machine.state != ConnectionState.CLOSED:
                        conn.send_headers(
                            event.stream_id,
                            [
                                (b":status", b"200"),
                                (b"content-type", b"application/connect+proto"),
                            ],
                            end_stream=False,
                        )
                        st["headers_sent"] = True
                await _flush_locked()
                task = asyncio.create_task(on_stream(event.stream_id, st))
                pending.add(task)
                task.add_done_callback(pending.discard)
            elif isinstance(event, h2.events.DataReceived):
                await _ack_flow_control(event.stream_id, event.flow_controlled_length)
                st = streams.get(event.stream_id)
                if st:
                    st["body"].push(event.data)
                    log.debug(f"[PROXY] agent h2 client data {len(event.data)}B")
            elif isinstance(event, h2.events.WindowUpdated):
                fc.notify(event)
            elif isinstance(event, h2.events.StreamEnded):
                st = streams.get(event.stream_id)
                if st:
                    st["client_ended"] = True
                    st["body"].push(None)
            elif isinstance(event, h2.events.ConnectionTerminated):
                log.debug(f"[PROXY] agent h2 ConnectionTerminated err={event.error_code}")
                for st in streams.values():
                    st["body"].push(None)
            elif isinstance(event, h2.events.StreamReset):
                st = streams.get(event.stream_id)
                log.debug(f"[PROXY] agent h2 StreamReset sid={event.stream_id} err={event.error_code}")
                if st:
                    st["body"].push(None)

    async with conn_lock:
        conn.initiate_connection()
    await _flush_locked()

    async with conn_lock:
        first_events = conn.receive_data(first)
    await _flush_locked()
    await handle_events(first_events)
    await _flush_locked()

    while True:
        try:
            data = await asyncio.wait_for(reader.read(65536), timeout=120.0)
        except asyncio.TimeoutError:
            if not pending:
                break
            continue
        if not data:
            if not pending:
                break
            await asyncio.sleep(0.05)
            continue
        async with conn_lock:
            events = conn.receive_data(data)
        await _flush_locked()
        await handle_events(events)
        await _flush_locked()

    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


async def _read_h1_body(
    reader: asyncio.StreamReader, headers: dict[str, str], initial: bytes
) -> bytes:
    te = (headers.get("Transfer-Encoding") or headers.get("transfer-encoding") or "").lower()
    if "chunked" in te:
        buf = initial
        out = bytearray()
        while True:
            while b"\r\n" not in buf:
                more = await reader.read(65536)
                if not more:
                    return bytes(out)
                buf += more
            line, _, buf = buf.partition(b"\r\n")
            size = int(line.split(b";", 1)[0].strip(), 16)
            if size == 0:
                return bytes(out)
            while len(buf) < size + 2:
                more = await reader.read(65536)
                if not more:
                    return bytes(out)
                buf += more
            out.extend(buf[:size])
            buf = buf[size + 2 :]
    cl = int(headers.get("Content-Length") or headers.get("content-length") or "0")
    body = bytearray(initial)
    while len(body) < cl:
        chunk = await reader.read(min(65536, cl - len(body)))
        if not chunk:
            break
        body.extend(chunk)
    return bytes(body)


async def _serve_h1(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, first: bytes) -> None:
    buf = first
    while b"\r\n\r\n" not in buf and len(buf) < 65536:
        buf += await reader.read(65536)
    head, _, rest = buf.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    req_line = lines[0].split()
    if len(req_line) < 2:
        log.warning(f"[PROXY] agent h1 bad request line: {lines[0][:120]!r}")
        writer.close()
        return
    method, target = req_line[0], req_line[1]
    if method != "POST" or not target.rstrip("/").endswith("/agent.v1.AgentService/Run"):
        if method in ("GET", "HEAD") and target.rstrip("/") in ("", "/"):
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 14\r\n\r\nGatekeep-agent\r\n")
            await writer.drain()
            writer.close()
            return
        log.warning(f"[PROXY] agent h1 reject {method} {target[:120]}")
        writer.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")
        await writer.drain()
        writer.close()
        return
    path = target.lstrip("/")
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip()] = v.strip()
    body = _BodyStream()
    initial = rest

    async def _pump_h1_body() -> None:
        payload = await _read_h1_body(reader, headers, initial)
        if payload:
            body.push(payload)
        body.push(None)

    pump = asyncio.create_task(_pump_h1_body())
    log.debug(f"[PROXY] agent h1 POST /{path} initial={len(initial)}B")

    async def send_response_start(status: int, resp_headers: dict[str, str]) -> None:
        lines_out = [f"HTTP/1.1 {status} OK", "transfer-encoding: chunked", "content-type: application/connect+proto"]
        for k, v in resp_headers.items():
            if k.lower() not in ("content-length", "connection", "transfer-encoding", "content-type"):
                lines_out.append(f"{k}: {v}")
        writer.write("\r\n".join(lines_out).encode() + b"\r\n\r\n")
        await writer.drain()

    async def send_data(chunk: bytes) -> None:
        writer.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
        await writer.drain()

    async def send_trailers(_hdrs) -> None:
        pass

    async def end_stream() -> None:
        writer.write(b"0\r\n\r\n")
        await writer.drain()

    try:
        await _run_relay(
            path,
            headers,
            body,
            send_response_start=send_response_start,
            send_data=send_data,
            send_trailers=send_trailers,
            end_stream=end_stream,
        )
    finally:
        pump.cancel()
    try:
        writer.close()
        await writer.wait_closed()
    except OSError:
        pass


async def _serve_connection(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    peer = writer.get_extra_info("peername")
    try:
        first = await reader.read(81936)
        if not first:
            return
        kind = "h2" if first.startswith(_H2_PREFACE) else "h1"
        log.debug(f"[PROXY] agent inbound {kind} len={len(first)} from={peer}")
        if kind == "h2":
            _dump_h2_frames(first, "client")
            await _serve_h2(reader, writer, first)
        else:
            await _serve_h1(reader, writer, first)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.warning(f"[PROXY] agent conn error: {exc}")
    finally:
        if not writer.is_closing():
            try:
                writer.close()
                await writer.wait_closed()
            except OSError:
                pass


async def start_agent_http_server(
    host: str | None = None, port: int | None = None
) -> asyncio.Server:
    from config import AGENT_HOST, AGENT_PORT

    host = host if host is not None else AGENT_HOST
    port = port if port is not None else AGENT_PORT
    srv = await asyncio.start_server(_serve_connection, host, port, reuse_address=True)
    log.info(f"[PROXY] Cursor agent listener on {host}:{port} (h1+h2)")
    return srv
