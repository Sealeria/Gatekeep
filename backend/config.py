# Copyright (c) 2026 Sealeria
# SPDX-License-Identifier: AGPL-3.0-or-later
# Licensed under the GNU Affero General Public License v3.0

"""Gatekeep deployment settings (env-driven, safe defaults for server bind)."""

from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()

HOST = os.getenv("GATEKEEP_HOST", os.getenv("HOST", "0.0.0.0"))
PORT = int(os.getenv("GATEKEEP_PORT", os.getenv("PORT", "9477")))
AGENT_HOST = os.getenv("GATEKEEP_AGENT_HOST", HOST)
AGENT_PORT = int(os.getenv("GATEKEEP_AGENT_PORT", "9478"))
API_KEY = (os.getenv("GATEKEEP_API_KEY") or "").strip()


def _url_host(host: str) -> str:
    if host in ("0.0.0.0", "::", "[::]", ""):
        return "127.0.0.1"
    return host


def gatekeep_public_url() -> str:
    explicit = (
        os.getenv("GATEKEEP_PUBLIC_URL") or os.getenv("GATEKEEP_BASE_URL") or ""
    ).strip().rstrip("/")
    if explicit:
        return explicit
    return f"http://{_url_host(HOST)}:{PORT}"


def agent_public_url() -> str:
    explicit = (os.getenv("GATEKEEP_AGENT_URL") or "").strip().rstrip("/")
    if explicit:
        return explicit
    base = (os.getenv("GATEKEEP_PUBLIC_URL") or os.getenv("GATEKEEP_BASE_URL") or "").strip().rstrip("/")
    if base:
        from urllib.parse import urlparse, urlunparse

        p = urlparse(base)
        host = p.hostname or _url_host(HOST)
        scheme = p.scheme or "http"
        return f"{scheme}://{host}:{AGENT_PORT}"
    return f"http://{_url_host(AGENT_HOST)}:{AGENT_PORT}"


def is_authorized(request) -> bool:
    if not API_KEY:
        return True
    auth = (request.headers.get("authorization") or "").strip()
    if auth.lower().startswith("bearer ") and auth[7:].strip() == API_KEY:
        return True
    if (request.headers.get("x-gatekeep-key") or "").strip() == API_KEY:
        return True
    return False
