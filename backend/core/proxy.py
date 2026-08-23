# Copyright (c) 2026 Sealeria
# SPDX-License-Identifier: AGPL-3.0-or-later
# Licensed under the GNU Affero General Public License v3.0

import asyncio
import json
import os
import time

import httpx
from fastapi import Request, Response
from fastapi.responses import StreamingResponse

import database
from core.providers import (
    base_url_for,
    is_anthropic_messages_payload,
    is_openai_chat_payload,
    is_openai_responses_payload,
    resolve_provider,
    should_optimize_payload,
)
from engines import optimizer
from gklog import get_logger

log = get_logger(__name__)

STRIPPED_REQUEST_HEADERS = {"host", "content-length", "connection", "accept-encoding"}
STRIPPED_RESPONSE_HEADERS = {"content-length", "connection", "transfer-encoding"}

_client = httpx.AsyncClient(timeout=None, follow_redirects=False, http2=True)
# ChatGPT Codex streams hang on h2 for some paths; force HTTP/1.1 there.
_chatgpt_client = httpx.AsyncClient(timeout=None, follow_redirects=False, http2=False)


async def _forward_cursor_server_config(
    request: Request,
    path: str,
    headers: dict[str, str],
    body: bytes,
    base_url: str,
) -> Response:
    """Intercept GetServerConfig and rewrite agent URLs before the client caches them."""
    from engines import cursor_rewrite

    start = time.perf_counter()
    agent_origin = cursor_rewrite.agent_public_url()
    url = f"{base_url}/{path}"

    upstream_request = _client.build_request(
        method=request.method,
        url=url,
        headers=headers,
        params=request.query_params,
        content=body,
    )
    upstream_response = await _client.send(upstream_request, stream=False)
    raw = upstream_response.content

    new_raw, rewrites = cursor_rewrite.maybe_rewrite_server_config(path, raw, agent_origin)
    tag = "server_config_rewrite" if new_raw != raw else "server_config_noop"
    if rewrites:
        log.debug(f"[PROXY] GetServerConfig rewrite: {', '.join(rewrites)}")
    else:
        log.debug(f"[PROXY] GetServerConfig rewrite missed /{path} (no agent URLs in body)")

    latency_ms = round((time.perf_counter() - start) * 1000)
    log.debug(
        f"[PROXY] GetServerConfig done in {latency_ms}ms "
        f"({len(raw)}B -> {len(new_raw)}B)"
    )

    response_headers = {
        k: v for k, v in upstream_response.headers.items()
        if k.lower() not in STRIPPED_RESPONSE_HEADERS
    }
    response_headers.pop("content-encoding", None)
    response_headers.pop("Content-Encoding", None)

    return Response(
        content=new_raw,
        status_code=upstream_response.status_code,
        headers=response_headers,
        media_type=upstream_response.headers.get("content-type"),
    )



def _crush_json_conversation(
    payload: dict,
    provider: str,
    original_body: bytes,
    *,
    compression_enabled: bool,
    anti_yap_enabled: bool,
    history_pruning_enabled: bool,
    log_truncation_enabled: bool,
    aggressive_enabled: bool,
) -> tuple[bytes, list, int, int, str, str, dict]:
    """CPU-heavy JSON crush — run via asyncio.to_thread so uvicorn stays responsive."""
    original_text = optimizer.extract_text(payload)
    original_tokens = optimizer.count_tokens(original_text)
    original_sample = original_text[: database.PAYLOAD_SAMPLE_CHARS]

    bypass = False
    if compression_enabled:
        session_key = optimizer.derive_session_key(payload)
        from engines import loopwatch

        loopwatch.observe(payload, session_key)
        bypass = loopwatch.is_bypass_compression(session_key)
        if is_anthropic_messages_payload(payload):
            payload = optimizer.apply_file_delta_tracking(payload, session_key)
            if not aggressive_enabled and not bypass:
                payload = optimizer.apply_newest_tool_result_compaction(payload)
    else:
        session_key = None

    openai_compat = not is_anthropic_messages_payload(payload)
    prune_hist = (
        history_pruning_enabled
        and (not aggressive_enabled or openai_compat)
        and not bypass
    )
    prune_logs = (
        log_truncation_enabled
        and (not aggressive_enabled or openai_compat)
        and not bypass
    )

    payload, categories = optimizer.optimize_payload(
        payload,
        compression_enabled=compression_enabled,
        anti_yap_enabled=anti_yap_enabled,
        history_pruning_enabled=prune_hist,
        log_truncation_enabled=prune_logs,
    )

    if aggressive_enabled and compression_enabled and is_anthropic_messages_payload(payload):
        from engines import aggressive as aggressive_mod

        categories = list(categories) + aggressive_mod.apply_aggressive(
            payload, session_key=session_key
        )
    elif aggressive_enabled and compression_enabled and is_openai_chat_payload(payload) and not bypass:
        from engines import aggressive as aggressive_mod

        categories = list(categories) + aggressive_mod.apply_aggressive_openai(
            payload, session_key=session_key
        )
    elif aggressive_enabled and compression_enabled and is_openai_responses_payload(payload):
        from engines import aggressive as aggressive_mod

        categories = list(categories) + aggressive_mod.apply_aggressive_responses(
            payload, session_key=session_key
        )
    elif compression_enabled and is_anthropic_messages_payload(payload):
        from engines import extras

        categories = list(categories) + extras.apply_cache_and_structure_fixes(payload)

    optimized_text = optimizer.extract_text(payload)
    optimized_tokens = optimizer.count_tokens(optimized_text)
    optimized_sample = optimized_text[: database.PAYLOAD_SAMPLE_CHARS]
    payload = optimizer.apply_anthropic_cache_alignment(payload, provider)

    saved = original_tokens - optimized_tokens
    if saved < 0:
        body = original_body
        optimized_tokens = original_tokens
        categories = []
    else:
        body = optimizer.canonical_json_dumps(payload)
    if compression_enabled and session_key:
        from engines import loopwatch

        if loopwatch.is_decompress(session_key):
            loopwatch.tick_decompress(session_key)
    return (
        body,
        list(categories) if categories else [],
        original_tokens,
        optimized_tokens,
        original_sample,
        optimized_sample,
        payload,
    )


async def forward_request(request: Request, path: str) -> Response:
    headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in STRIPPED_REQUEST_HEADERS
    }

    path_l = (path or "").lstrip("/").lower()
    provider = resolve_provider(request.headers, path, None)
    headers = {k: v for k, v in headers.items() if k.lower() != "x-gatekeep-provider"}

    if provider == "cursor" and path_l.startswith("agent.v1.") and request.method == "POST":
        from core.cursor_agent import forward_agent_bidi

        upstream = base_url_for(provider, path)
        return await forward_agent_bidi(request, path, headers, upstream)

    body = await request.body()
    # Codex 0.144+ (and others) send zstd/gzip JSON — crush needs plaintext.
    if body:
        from engines.body_codec import decode_request_body

        body, decoded = decode_request_body(body, headers)
        if decoded:
            log.info(f"[PROXY] decoded body /{path} -> {len(body)}B")

    if request.method == "POST":
        from engines import cursor_rewrite

        if provider == "cursor" and cursor_rewrite.is_server_config_path(path):
            return await _forward_cursor_server_config(
                request, path, headers, body, base_url_for(provider, path)
            )

    model_hint = None
    if request.method == "POST" and body:
        try:
            peek = json.loads(body)
            if isinstance(peek, dict) and isinstance(peek.get("model"), str):
                model_hint = peek["model"]
        except ValueError:
            pass

    provider = resolve_provider(request.headers, path, model_hint)
    # Drop internal routing hint before upstream
    headers = {k: v for k, v in headers.items() if k.lower() != "x-gatekeep-provider"}
    base_url = base_url_for(provider, path)
    url = f"{base_url}/{path}"

    log_id = None
    request_hash = None
    intent_hash = None
    should_cache = False

    if request.method == "POST" and body:
        try:
            payload = json.loads(body)
        except ValueError:
            payload = None
        if isinstance(payload, dict) and should_optimize_payload(payload, path):
            start = time.perf_counter()
            original_body = body

            settings = await asyncio.to_thread(database.get_settings)
            compression_enabled = bool(settings.get("compression_enabled", 1))
            anti_yap_enabled = bool(settings.get("anti_yap_enabled", 1))
            history_pruning_enabled = bool(settings.get("history_pruning_enabled", 1))
            log_truncation_enabled = bool(settings.get("log_truncation_enabled", 1))
            aggressive_enabled = bool(settings.get("aggressive_enabled", 1))

            # Exact-hash cache only. Skip for chatgpt/cursor (replay loops).
            # Token counting is CPU-heavy — do it inside to_thread crush, not here.
            _no_cache_providers = {"chatgpt", "cursor"}
            should_cache = compression_enabled and provider not in _no_cache_providers
            intent_hash = None
            original_tokens = 0
            original_sample = ""
            if should_cache:
                from engines import extras

                request_hash = optimizer.compute_request_hash(payload, provider)
                # Cheap sample for cache-hit log only
                original_sample = str(payload.get("model") or "")[:200]
                cached = await asyncio.to_thread(database.get_cached_response, request_hash)
                hit_kind = "cache_hit"
                if cached:
                    latency_ms = round((time.perf_counter() - start) * 1000)
                    log.info(
                        f"[OPTIMIZER] Cache HIT ({hit_kind}) - 0ms upstream, "
                        f"replay"
                    )
                    await asyncio.to_thread(
                        database.log_request,
                        payload.get("model"),
                        provider,
                        0,
                        0,
                        0,
                        latency_ms,
                        hit_kind,
                        original_sample,
                        "",
                        True,
                    )
                    cached_headers = json.loads(cached["headers"])

                    async def replay(_body=cached["body"]):
                        yield _body

                    return StreamingResponse(
                        replay(),
                        status_code=cached["status_code"],
                        headers=cached_headers,
                        media_type=cached_headers.get("content-type", "text/event-stream"),
                    )

            (
                body,
                categories,
                original_tokens,
                optimized_tokens,
                original_sample,
                optimized_sample,
                payload,
            ) = await asyncio.to_thread(
                _crush_json_conversation,
                payload,
                provider,
                original_body,
                compression_enabled=compression_enabled,
                anti_yap_enabled=anti_yap_enabled,
                history_pruning_enabled=history_pruning_enabled,
                log_truncation_enabled=log_truncation_enabled,
                aggressive_enabled=aggressive_enabled,
            )
            saved = max(0, original_tokens - optimized_tokens)
            pct = (saved / original_tokens * 100) if original_tokens else 0.0
            log.info(
                f"[OPTIMIZER] [{provider}] Input Tokens: {original_tokens} -> {optimized_tokens} "
                f"| Saved: {saved} tokens ({pct:.1f}%)"
            )

            estimated_output_tokens_saved = (
                optimizer.ESTIMATED_ANTI_YAP_OUTPUT_TOKENS_SAVED if "anti_yap" in categories else 0
            )
            latency_ms = round((time.perf_counter() - start) * 1000)

            log_id = await asyncio.to_thread(
                database.log_request,
                payload.get("model"),
                provider,
                original_tokens,
                optimized_tokens,
                estimated_output_tokens_saved,
                latency_ms,
                ",".join(categories) if categories else "unchanged",
                original_sample,
                optimized_sample,
            )
        else:
            # Cursor Connect / Codex ChatGPT / other non-Messages JSON
            settings = await asyncio.to_thread(database.get_settings)
            compression_enabled = bool(settings.get("compression_enabled", 1))
            aggressive_enabled = bool(settings.get("aggressive_enabled", 1))
            start = time.perf_counter()
            _codex_path = (path or "").lstrip("/").lower().startswith("backend-api/codex/")
            if _codex_path:
                # Avoid tiktoken on the event loop — was freezing Gatekeep under Codex.
                original_tokens = max(1, len(body) // 4)
                original_sample = f"[codex passthrough {len(body)}B]"
            else:
                original_tokens, original_sample = await asyncio.to_thread(
                    optimizer.measure_wire_tokens,
                    body,
                    payload if isinstance(payload, dict) else None,
                )
            categories: list[str] = []
            optimized_tokens = original_tokens
            optimized_sample = original_sample
            # Skip wirecrush on Codex ChatGPT JSON (route-only; crush hung / looped).
            _skip_wire = _codex_path
            if compression_enabled and len(body) >= 200 and not _skip_wire:
                from engines import wirecrush

                ctype = request.headers.get("content-type", "")
                orig_body = body
                orig_len = len(body)
                new_body, categories, bytes_saved = await asyncio.to_thread(
                    lambda: wirecrush.crush_wire_body(
                        body,
                        path=path,
                        content_type=ctype,
                        aggressive=compression_enabled and aggressive_enabled,
                    )
                )
                if categories and (bytes_saved > 0 or len(new_body) < orig_len):
                    body = new_body
                    if orig_body.lstrip()[:1] in (b"{", b"["):
                        try:
                            optimized_tokens, optimized_sample = optimizer.measure_wire_tokens(
                                body, json.loads(body)
                            )
                        except ValueError:
                            optimized_tokens = max(1, len(body) // 4)
                            optimized_sample = f"[wire crush {orig_len}->{len(body)}B]"
                    else:
                        original_tokens = max(original_tokens, max(1, orig_len // 4))
                        optimized_tokens = max(1, len(body) // 4)
                        optimized_sample = (
                            f"[wire crush {orig_len}->{len(body)}B "
                            f"saved={bytes_saved or orig_len - len(body)}]"
                        )
            saved = max(0, original_tokens - optimized_tokens)
            tag = ",".join(categories) if categories and saved else "passthrough"
            latency_ms = round((time.perf_counter() - start) * 1000)
            if saved:
                pct = saved / original_tokens * 100 if original_tokens else 0.0
                log.info(
                    f"[OPTIMIZER] [{provider}] wire {original_tokens} -> {optimized_tokens} "
                    f"| Saved: {saved} ({pct:.1f}%) [{tag}]"
                )
            else:
                log.debug(
                    f"[PROXY] passthrough provider={provider} path=/{path} "
                    f"wire_tokens={original_tokens}"
                )
            log_id = await asyncio.to_thread(
                database.log_request,
                model_hint
                or (
                    payload.get("model")
                    if isinstance(payload, dict) and isinstance(payload.get("model"), str)
                    else None
                ),
                provider,
                original_tokens,
                optimized_tokens,
                0,
                latency_ms,
                tag,
                original_sample or path[:200],
                optimized_sample if saved else "",
            )
    else:
        # GET/HEAD probes — no body to measure
        await asyncio.to_thread(
            database.log_request,
            model_hint,
            provider,
            0,
            0,
            0,
            0,
            "passthrough",
            f"{request.method} /{path}"[:200],
            "",
        )

    log.info(f"[PROXY] {request.method} /{path} -> {provider} ({base_url})")

    client = _chatgpt_client if provider == "chatgpt" else _client
    upstream_request = client.build_request(
        method=request.method,
        url=url,
        headers=headers,
        params=request.query_params,
        content=body,
    )

    upstream_response = await client.send(upstream_request, stream=True)

    response_headers = {
        k: v for k, v in upstream_response.headers.items()
        if k.lower() not in STRIPPED_RESPONSE_HEADERS
    }
    response_headers.pop("content-encoding", None)
    response_headers.pop("Content-Encoding", None)

    async def stream_body():
        chunks = [] if (should_cache or log_id) else None
        try:
            async for chunk in upstream_response.aiter_bytes():
                if chunks is not None:
                    chunks.append(chunk)
                yield chunk
        finally:
            await upstream_response.aclose()
            if chunks is not None and upstream_response.status_code < 300:
                full_body = b"".join(chunks)
                if should_cache and request_hash:
                    await asyncio.to_thread(
                        database.store_cached_response,
                        request_hash,
                        upstream_response.status_code,
                        json.dumps(response_headers),
                        full_body,
                    )
                    if intent_hash and intent_hash != request_hash:
                        await asyncio.to_thread(
                            database.store_cached_response,
                            intent_hash,
                            upstream_response.status_code,
                            json.dumps(response_headers),
                            full_body,
                        )
                if log_id:
                    usage = optimizer.extract_upstream_usage(full_body)
                    if usage:
                        await asyncio.to_thread(database.update_upstream_usage, log_id, usage)

    return StreamingResponse(
        stream_body(),
        status_code=upstream_response.status_code,
        headers=response_headers,
        media_type=upstream_response.headers.get("content-type", "text/event-stream"),
    )
