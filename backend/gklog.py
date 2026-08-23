# Copyright (c) 2026 Sealeria
# SPDX-License-Identifier: AGPL-3.0-or-later
# Licensed under the GNU Affero General Public License v3.0

"""Gatekeep logging helpers."""

from __future__ import annotations

import logging
import os

_CONFIGURED = False


def setup_logging() -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    debug = os.getenv("GATEKEEP_DEBUG", "").strip().lower() in ("1", "true", "yes")
    level = logging.DEBUG if debug else logging.INFO
    logging.basicConfig(level=level, format="%(message)s")
    _CONFIGURED = True


def get_logger(name: str = "gatekeep") -> logging.Logger:
    setup_logging()
    return logging.getLogger(name)
