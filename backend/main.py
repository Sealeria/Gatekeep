# Copyright (c) 2026 Sealeria
# SPDX-License-Identifier: AGPL-3.0-or-later
# Licensed under the GNU Affero General Public License v3.0

import asyncio
import logging
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

import config
import database
from core.proxy import forward_request
from engines import pricing
from gklog import get_logger

log = get_logger(__name__)

FRONTEND_DIST = Path(__file__).resolve().parent.parent / "frontend" / "dist"
INSTALL_DIR = Path(__file__).resolve().parent.parent / "install"
WIRE_AGENTS_SCRIPT = INSTALL_DIR / "wire-agents.ps1"

_SUPPRESSED_ACCESS_LOG_PATHS = (
    "/api/stats",
    "/api/settings",
    "/api/logs",
    "/api/prices",
    "/api/server-info",
    "/dashboard",
    "/favicon.ico",
)


class EndpointFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        return not any(path in message for path in _SUPPRESSED_ACCESS_LOG_PATHS)


logging.getLogger("uvicorn.access").addFilter(EndpointFilter())

app = FastAPI(title="AI Gateway Proxy")


_DASHBOARD_API = (
    "/api/stats",
    "/api/settings",
    "/api/logs",
    "/api/prices",
    "/api/server-info",
    "/api/data/clear",
    "/api/prices/refresh",
)


@app.middleware("http")
async def gatekeep_api_key_middleware(request: Request, call_next):
    if not config.API_KEY:
        return await call_next(request)
    path = request.url.path
    if path.startswith("/dashboard") or path.startswith("/install/") or path in _DASHBOARD_API or path.startswith("/api/settings/"):
        return await call_next(request)
    if config.is_authorized(request):
        return await call_next(request)
    return JSONResponse(status_code=401, content={"detail": "Unauthorized"})


@app.on_event("startup")
async def _startup():
    database.init_db()
    pricing.ensure_seed_prices()
    log.info(f"[PROXY] Gatekeep listening config host={config.HOST} port={config.PORT}")

    if os.environ.get("GATEKEEP_DISABLE_AGENT", "").strip() in ("1", "true", "yes"):
        log.debug("[PROXY] Cursor agent listener disabled (GATEKEEP_DISABLE_AGENT)")
    else:
        from core.cursor_agent_server import start_agent_http_server

        async def _run_agent() -> None:
            srv = await start_agent_http_server(config.AGENT_HOST, config.AGENT_PORT)
            async with srv:
                await srv.serve_forever()

        app.state.agent_task = asyncio.create_task(_run_agent())
        log.info(
            f"[PROXY] Agent URL rewrite target: {config.agent_public_url()} "
            f"(listener {config.AGENT_HOST}:{config.AGENT_PORT})"
        )

        async def _warm_agent_upstream() -> None:
            from core.cursor_agent import DEFAULT_AGENT_UPSTREAM, warm_upstream

            await warm_upstream(DEFAULT_AGENT_UPSTREAM)

        asyncio.create_task(_warm_agent_upstream())

    def _auto():
        result = pricing.maybe_auto_refresh()
        if result and not result.get("error"):
            log.info(f"[PRICING] auto-refreshed {result.get('models')} models @ {result.get('updated_at')}")
        elif result and result.get("error"):
            log.warning(f"[PRICING] auto-refresh failed: {result['error']}")

    await asyncio.to_thread(_auto)


@app.on_event("shutdown")
async def _shutdown():
    task = getattr(app.state, "agent_task", None)
    if task is not None:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        app.state.agent_task = None


@app.get("/api/server-info")
async def get_server_info(request: Request):
    """Public URLs for dashboard wiki. Prefers request Host when env is unset."""
    explicit = (os.getenv("GATEKEEP_PUBLIC_URL") or os.getenv("GATEKEEP_BASE_URL") or "").strip()
    if explicit:
        base = explicit.rstrip("/")
    else:
        base = str(request.base_url).rstrip("/")

    explicit_agent = (os.getenv("GATEKEEP_AGENT_URL") or "").strip()
    if explicit_agent:
        agent = explicit_agent.rstrip("/")
    else:
        from urllib.parse import urlparse

        p = urlparse(base)
        host = p.hostname or "127.0.0.1"
        scheme = p.scheme or "http"
        agent = f"{scheme}://{host}:{config.AGENT_PORT}"

    wire_local = f'powershell -File install/wire-agents.ps1 -GatekeepUrl "{base}" -AgentUrl "{agent}"'
    wire_ps = (
        f"$g='{base}'; $a='{agent}'; $f=Join-Path $env:TEMP 'gk-wire.ps1'; "
        f'irm "{base}/install/wire-agents.ps1" -OutFile $f; '
        f"& $f -GatekeepUrl $g -AgentUrl $a"
    )
    wire_cmd = (
        "powershell -NoProfile -ExecutionPolicy Bypass -Command "
        f"\"& {{ $g='{base}'; $a='{agent}'; $f=Join-Path $env:TEMP 'gk-wire.ps1'; "
        f"irm ($g+'/install/wire-agents.ps1') -OutFile $f; "
        f"& $f -GatekeepUrl $g -AgentUrl $a }}\""
    )

    return {
        "gatekeep_url": base,
        "agent_url": agent,
        "dashboard_url": f"{base}/dashboard/",
        "api_key_required": bool(config.API_KEY),
        "wire_command": wire_ps,
        "wire_command_cmd": wire_cmd,
        "wire_command_local": wire_local,
        "wire_script_url": f"{base}/install/wire-agents.ps1",
    }


@app.get("/install/wire-agents.ps1")
async def get_wire_agents_script():
    if not WIRE_AGENTS_SCRIPT.is_file():
        raise HTTPException(status_code=404, detail="wire-agents.ps1 not found")
    return FileResponse(
        WIRE_AGENTS_SCRIPT,
        media_type="text/plain; charset=utf-8",
        filename="wire-agents.ps1",
    )


@app.get("/api/settings")
async def get_settings():
    return database.get_settings()


@app.post("/api/settings/{key}")
async def update_setting(key: str, request: Request):
    body = await request.json()
    if "value" not in body:
        raise HTTPException(status_code=400, detail="Missing 'value' in request body")
    value = int(bool(body["value"]))
    database.set_setting(key, value)
    # Aggressive mode replaces mild history/log passes; force toggles on so
    # the UI doesn't show them as "off" while dimmed under MAX.
    if key == "aggressive_enabled" and value == 1:
        database.set_setting("history_pruning_enabled", 1)
        database.set_setting("log_truncation_enabled", 1)
    return {"key": key, "value": value, "settings": database.get_settings()}


@app.get("/api/stats")
async def get_stats(include_noise: bool = False, range: str = "24h"):
    return database.get_stats(include_noise=include_noise, range_key=range)


@app.get("/api/logs")
async def get_logs(
    limit: int = 20,
    offset: int = 0,
    include_noise: bool = False,
    range: str = "24h",
):
    return database.get_logs(
        limit=limit, offset=offset, include_noise=include_noise, range_key=range
    )


@app.get("/api/prices")
async def get_prices():
    return pricing.pricing_status()


@app.post("/api/prices/refresh")
async def refresh_prices():
    try:
        result = await asyncio.to_thread(pricing.refresh_prices_from_litellm)
        return result
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/api/data/clear")
async def clear_data():
    return database.clear_request_data()


@app.get("/")
async def root_redirect(request: Request):
    # Browsers → dashboard. API clients (Codex MCP, etc.) must not get HTML.
    accept = (request.headers.get("accept") or "").lower()
    if "text/html" in accept:
        return RedirectResponse(url="/dashboard/")
    return {"service": "gatekeep", "dashboard": "/dashboard/"}


@app.get("/dashboard")
async def dashboard_redirect():
    return RedirectResponse(url="/dashboard/")


class _NoCacheStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        if path in ("", "index.html") or path.endswith(".html"):
            response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return response


if FRONTEND_DIST.is_dir():
    app.mount(
        "/dashboard",
        _NoCacheStaticFiles(directory=FRONTEND_DIST, html=True),
        name="dashboard",
    )


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
async def catch_all(request: Request, path: str):
    return await forward_request(request, path)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host=config.HOST, port=config.PORT, log_level="info")
