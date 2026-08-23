# Copyright (c) 2026 Sealeria
# SPDX-License-Identifier: AGPL-3.0-or-later
# Licensed under the GNU Affero General Public License v3.0

import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

RANGE_PRESETS: dict[str, timedelta] = {
    "1h": timedelta(hours=1),
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
}

_data_dir = Path(os.getenv("GATEKEEP_DATA_DIR", Path(__file__).parent))
DB_PATH = _data_dir / "proxy_data.db"

PAYLOAD_SAMPLE_CHARS = 4000

# Dashboard noise: OTLP, analytics, dashboard probes — not LLM savings.
_NOISE_SAMPLE_MARKERS = (
    "v1/traces",
    "analyticsservice/track",
    "analyticsservice/submit",
    "dashboardservice/",
    "aiservice/availablemodels",
    "aiservice/getusablemodels",
    "aiservice/getdefaultmodelforcli",
    "aiservice/getclidownloadurl",
    '{"logs":',
    '{"events":',
    "service.name",
    "cursor-agent-",
)
_MEANINGFUL_CATEGORIES = (
    "wire_proto_crush",
    "wire_proto_crush_aggressive",
    "cache_hit",
    "intent_cache",
    "anti_yap",
    "pruning",
    "ccr",
    "aggressive",
    "prose",
    "log_truncation",
    "unchanged",
)
_MIN_DASHBOARD_SAVED = 50


def is_dashboard_noise(
    *,
    savings_category: str | None,
    input_tokens_saved: int,
    original_payload_sample: str | None,
) -> bool:
    saved = input_tokens_saved or 0
    raw_cat = savings_category or "passthrough"
    cat = raw_cat.lower()
    sample = (original_payload_sample or "").strip().lower()

    if "serverconfigservice" in sample or "server_config" in cat:
        return True
    if saved >= _MIN_DASHBOARD_SAVED:
        return False
    if saved > 0:
        return True
    for tag in _MEANINGFUL_CATEGORIES:
        if tag in cat and tag not in ("unchanged",):
            return False
    if sample.startswith("agent.v1."):
        return False
    if any(m in sample for m in _NOISE_SAMPLE_MARKERS):
        return True
    if sample.startswith("post /aiserver.v1."):
        return True
    if cat in ("passthrough", "server_config_noop") and len(sample) <= 24:
        return True
    return cat == "passthrough" and not sample

# ponytail: flat cap + FIFO-by-age eviction on response_cache, no real TTL —
# fine for a local single-user dev proxy.
MAX_CACHED_RESPONSES = 500

_DEFAULT_SETTINGS = {
    "compression_enabled": 1,
    "anti_yap_enabled": 1,
    "history_pruning_enabled": 1,
    "log_truncation_enabled": 1,
    "aggressive_enabled": 1,
}


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = _connect()
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS settings ("
            "key TEXT PRIMARY KEY, "
            "value INTEGER NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS request_logs ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "timestamp DATETIME DEFAULT CURRENT_TIMESTAMP, "
            "model TEXT, "
            "provider TEXT, "
            "original_input_tokens INTEGER, "
            "optimized_input_tokens INTEGER, "
            "input_tokens_saved INTEGER, "
            "estimated_output_tokens_saved INTEGER, "
            "latency_ms INTEGER, "
            "savings_category TEXT, "
            "original_payload_sample TEXT, "
            "optimized_payload_sample TEXT, "
            "cache_hit INTEGER DEFAULT 0, "
            "upstream_input_tokens INTEGER, "
            "upstream_output_tokens INTEGER, "
            "upstream_cache_read_tokens INTEGER, "
            "upstream_cache_creation_tokens INTEGER)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_request_logs_timestamp "
            "ON request_logs(timestamp)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS response_cache ("
            "request_hash TEXT PRIMARY KEY, "
            "status_code INTEGER, "
            "headers TEXT, "
            "body BLOB, "
            "created_at DATETIME DEFAULT CURRENT_TIMESTAMP)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS model_prices ("
            "model TEXT PRIMARY KEY, "
            "input_per_mtok REAL NOT NULL, "
            "output_per_mtok REAL NOT NULL, "
            "updated_at TEXT NOT NULL, "
            "source TEXT)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS meta ("
            "key TEXT PRIMARY KEY, "
            "value TEXT NOT NULL)"
        )
        for key, value in _DEFAULT_SETTINGS.items():
            conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, value))
        conn.commit()
    finally:
        conn.close()


def get_meta(key: str):
    conn = _connect()
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    finally:
        conn.close()
    return row["value"] if row else None


def set_meta(key: str, value: str) -> None:
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        conn.commit()
    finally:
        conn.close()


def upsert_model_price(model: str, input_per_mtok: float, output_per_mtok: float, updated_at: str, source: str) -> None:
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO model_prices (model, input_per_mtok, output_per_mtok, updated_at, source) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(model) DO UPDATE SET "
            "input_per_mtok = excluded.input_per_mtok, "
            "output_per_mtok = excluded.output_per_mtok, "
            "updated_at = excluded.updated_at, "
            "source = excluded.source",
            (model, input_per_mtok, output_per_mtok, updated_at, source),
        )
        conn.commit()
    finally:
        conn.close()


def count_model_prices() -> int:
    conn = _connect()
    try:
        return conn.execute("SELECT COUNT(*) AS n FROM model_prices").fetchone()["n"]
    finally:
        conn.close()


def get_all_model_prices() -> dict:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT model, input_per_mtok, output_per_mtok FROM model_prices"
        ).fetchall()
    finally:
        conn.close()
    return {
        row["model"]: {"input_per_mtok": row["input_per_mtok"], "output_per_mtok": row["output_per_mtok"]}
        for row in rows
    }


def list_model_prices(limit: int = 40) -> list:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT model, input_per_mtok, output_per_mtok, updated_at, source "
            "FROM model_prices ORDER BY model LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def list_all_model_prices() -> list:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT model, input_per_mtok, output_per_mtok, updated_at, source "
            "FROM model_prices"
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def _log_is_visible(row: sqlite3.Row | dict, *, include_noise: bool) -> bool:
    if include_noise:
        return True
    if isinstance(row, sqlite3.Row):
        row = dict(row)
    return not is_dashboard_noise(
        savings_category=row.get("savings_category"),
        input_tokens_saved=row.get("input_tokens_saved") or 0,
        original_payload_sample=row.get("original_payload_sample"),
    )


def _parse_log_timestamp(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        s = ts.strip().replace(" ", "T")
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            # SQLite CURRENT_TIMESTAMP is UTC.
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def _timestamp_to_api(ts: str | None) -> str | None:
    dt = _parse_log_timestamp(ts)
    if dt is None:
        return ts
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _row_for_api(row: dict) -> dict:
    out = dict(row)
    if out.get("timestamp"):
        out["timestamp"] = _timestamp_to_api(out["timestamp"])
    return out


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_range(range_key: str | None) -> str:
    key = (range_key or "24h").strip().lower()
    return key if key in RANGE_PRESETS or key == "all" else "24h"


def _granularity_for_range(range_key: str, span: timedelta | None) -> str:
    if range_key == "1h":
        return "5m"
    if range_key == "24h":
        return "hour"
    if range_key in ("7d", "30d"):
        return "day"
    if span is None:
        return "day"
    if span <= timedelta(hours=2):
        return "5m"
    if span <= timedelta(days=2):
        return "hour"
    if span <= timedelta(days=90):
        return "day"
    return "week"


def _bucket_start(ts: datetime, granularity: str) -> datetime:
    ts = ts.replace(second=0, microsecond=0)
    if granularity == "5m":
        return ts.replace(minute=(ts.minute // 5) * 5)
    if granularity == "hour":
        return ts.replace(minute=0)
    if granularity == "day":
        return ts.replace(hour=0, minute=0)
    # week: Monday 00:00
    start = ts.replace(hour=0, minute=0)
    return start - timedelta(days=start.weekday())


def _iter_bucket_starts(since: datetime, until: datetime, granularity: str):
    cur = _bucket_start(since, granularity)
    end = _bucket_start(until, granularity)
    step = {
        "5m": timedelta(minutes=5),
        "hour": timedelta(hours=1),
        "day": timedelta(days=1),
        "week": timedelta(weeks=1),
    }[granularity]
    while cur <= end:
        yield cur
        cur += step


def _filter_rows_by_window(
    rows: list[dict], *, since: datetime | None, until: datetime | None
) -> list[dict]:
    if since is None and until is None:
        return rows
    out: list[dict] = []
    for row in rows:
        ts = _parse_log_timestamp(row.get("timestamp"))
        if ts is None:
            continue
        if since is not None and ts < since:
            continue
        if until is not None and ts >= until:
            continue
        out.append(row)
    return out


def _visible_rows(*, include_noise: bool) -> list[dict]:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT id, timestamp, model, provider, original_input_tokens, optimized_input_tokens, "
            "input_tokens_saved, estimated_output_tokens_saved, latency_ms, savings_category, "
            "original_payload_sample, optimized_payload_sample, cache_hit, "
            "upstream_input_tokens, upstream_output_tokens, upstream_cache_read_tokens, upstream_cache_creation_tokens "
            "FROM request_logs ORDER BY id DESC"
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows if _log_is_visible(r, include_noise=include_noise)]


def _aggregate_rows(rows: list[dict]) -> dict:
    total_requests = len(rows)
    total_original_input_tokens = sum(r.get("original_input_tokens") or 0 for r in rows)
    total_optimized_input_tokens = sum(r.get("optimized_input_tokens") or 0 for r in rows)
    total_input_tokens_saved = sum(r.get("input_tokens_saved") or 0 for r in rows)
    total_output_tokens_saved = sum(r.get("estimated_output_tokens_saved") or 0 for r in rows)
    total_cache_hits = sum(r.get("cache_hit") or 0 for r in rows)
    total_upstream_cache_read_tokens = sum(r.get("upstream_cache_read_tokens") or 0 for r in rows)
    total_upstream_cache_creation_tokens = sum(r.get("upstream_cache_creation_tokens") or 0 for r in rows)
    total_upstream_input_tokens = sum(r.get("upstream_input_tokens") or 0 for r in rows)
    total_upstream_output_tokens = sum(r.get("upstream_output_tokens") or 0 for r in rows)
    latencies = [r.get("latency_ms") or 0 for r in rows]
    avg_latency_ms = sum(latencies) / len(latencies) if latencies else 0.0

    cat_acc: dict[str, dict[str, int]] = {}
    for row in rows:
        raw = row.get("savings_category") or "unchanged"
        saved = row.get("input_tokens_saved") or 0
        for tag in raw.split(","):
            tag = tag.strip() or "unchanged"
            bucket = cat_acc.setdefault(tag, {"count": 0, "saved": 0})
            bucket["count"] += 1
            bucket["saved"] += saved

    total_tokens_saved = total_input_tokens_saved + total_output_tokens_saved
    original = total_original_input_tokens or 0
    saved_in = total_input_tokens_saved or 0
    avg_save_pct = (saved_in / original * 100) if original else 0.0
    forwarded = total_optimized_input_tokens or 0
    stretch = (original / forwarded) if forwarded else 1.0

    from engines import pricing as pricing_mod

    estimated_usd = 0.0
    for row in rows:
        estimated_usd += pricing_mod.estimate_usd_saved(
            row.get("input_tokens_saved") or 0,
            row.get("estimated_output_tokens_saved") or 0,
            row.get("model") or "",
        )

    return {
        "total_requests": total_requests,
        "total_original_input_tokens": original,
        "total_optimized_input_tokens": forwarded,
        "total_input_tokens_saved": saved_in,
        "total_output_tokens_saved": total_output_tokens_saved,
        "total_tokens_saved": total_tokens_saved,
        "avg_save_pct": round(avg_save_pct, 1),
        "stretch_multiplier": round(stretch, 2),
        "estimated_usd_saved": round(estimated_usd, 6),
        "avg_latency_ms": round(avg_latency_ms, 1),
        "total_cache_hits": total_cache_hits,
        "total_upstream_cache_read_tokens": total_upstream_cache_read_tokens,
        "total_upstream_cache_creation_tokens": total_upstream_cache_creation_tokens,
        "total_upstream_input_tokens": total_upstream_input_tokens,
        "total_upstream_output_tokens": total_upstream_output_tokens,
        "technique_hits": dict(cat_acc),
    }


def _pct_change(current: float, previous: float) -> float | None:
    if previous == 0:
        return None if current == 0 else 100.0
    return round((current - previous) / previous * 100, 1)


def _empty_bucket(start: datetime) -> dict:
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    return {
        "t": start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "requests": 0,
        "original_input_tokens": 0,
        "optimized_input_tokens": 0,
        "input_tokens_saved": 0,
        "output_tokens_saved": 0,
    }


def _build_timeseries(
    rows: list[dict], *, since: datetime, until: datetime, granularity: str
) -> list[dict]:
    since = _bucket_start(since, granularity)
    until = _bucket_start(until, granularity)
    buckets: dict[datetime, dict] = {}
    for start in _iter_bucket_starts(since, until, granularity):
        buckets[start] = _empty_bucket(start)
    for row in rows:
        ts = _parse_log_timestamp(row.get("timestamp"))
        if ts is None:
            continue
        key = _bucket_start(ts, granularity)
        if key not in buckets:
            buckets[key] = _empty_bucket(key)
        b = buckets[key]
        b["requests"] += 1
        b["original_input_tokens"] += row.get("original_input_tokens") or 0
        b["optimized_input_tokens"] += row.get("optimized_input_tokens") or 0
        b["input_tokens_saved"] += row.get("input_tokens_saved") or 0
        b["output_tokens_saved"] += row.get("estimated_output_tokens_saved") or 0
    return [buckets[k] for k in sorted(buckets)]


def get_logs(
    limit: int = 20,
    offset: int = 0,
    *,
    include_noise: bool = False,
    range_key: str = "24h",
) -> dict:
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))
    rk = _normalize_range(range_key)
    visible = _visible_rows(include_noise=include_noise)
    now = _now_utc()
    if rk == "all":
        since = None
    else:
        since = now - RANGE_PRESETS[rk]
    filtered = _filter_rows_by_window(visible, since=since, until=None)
    total = len(filtered)
    page = [_row_for_api(r) for r in filtered[offset : offset + limit]]
    return {
        "logs": page,
        "total": total,
        "limit": limit,
        "offset": offset,
        "has_more": offset + len(page) < total,
        "include_noise": include_noise,
        "range": rk,
    }


def get_settings() -> dict:
    conn = _connect()
    try:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
        settings = {row["key"]: row["value"] for row in rows}
        # Heal stale UI state: Aggressive implies mild history/log opts are on.
        if settings.get("aggressive_enabled"):
            dirty = False
            for key in ("history_pruning_enabled", "log_truncation_enabled"):
                if not settings.get(key):
                    conn.execute(
                        "INSERT INTO settings (key, value) VALUES (?, 1) "
                        "ON CONFLICT(key) DO UPDATE SET value = 1",
                        (key,),
                    )
                    settings[key] = 1
                    dirty = True
            if dirty:
                conn.commit()
    finally:
        conn.close()
    return settings


def set_setting(key: str, value: int) -> None:
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        conn.commit()
    finally:
        conn.close()


def log_request(
    model,
    provider: str,
    original_input_tokens: int,
    optimized_input_tokens: int,
    estimated_output_tokens_saved: int,
    latency_ms: int,
    savings_category: str,
    original_payload_sample: str,
    optimized_payload_sample: str,
    cache_hit: bool = False,
) -> int:
    saved = original_input_tokens - optimized_input_tokens
    if is_dashboard_noise(
        savings_category=savings_category,
        input_tokens_saved=saved,
        original_payload_sample=original_payload_sample,
    ):
        return 0
    conn = _connect()
    try:
        cur = conn.execute(
            "INSERT INTO request_logs ("
            "model, provider, original_input_tokens, optimized_input_tokens, input_tokens_saved, "
            "estimated_output_tokens_saved, latency_ms, savings_category, "
            "original_payload_sample, optimized_payload_sample, cache_hit"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                model,
                provider,
                original_input_tokens,
                optimized_input_tokens,
                original_input_tokens - optimized_input_tokens,
                estimated_output_tokens_saved,
                latency_ms,
                savings_category,
                original_payload_sample,
                optimized_payload_sample,
                int(cache_hit),
            ),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def update_upstream_usage(log_id: int, usage: dict) -> None:
    """Record the provider's own authoritative usage figures against an
    already-logged request, once the upstream response has fully arrived."""
    if not usage or not log_id:
        return
    inp = usage.get("input_tokens")
    if inp is None:
        inp = usage.get("prompt_tokens")
    out = usage.get("output_tokens")
    if out is None:
        out = usage.get("completion_tokens")
    conn = _connect()
    try:
        conn.execute(
            "UPDATE request_logs SET "
            "upstream_input_tokens = ?, upstream_output_tokens = ?, "
            "upstream_cache_read_tokens = ?, upstream_cache_creation_tokens = ? "
            "WHERE id = ?",
            (
                inp,
                out,
                usage.get("cache_read_input_tokens"),
                usage.get("cache_creation_input_tokens"),
                log_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def get_cached_response(request_hash: str):
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT status_code, headers, body FROM response_cache WHERE request_hash = ?",
            (request_hash,),
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def store_cached_response(request_hash: str, status_code: int, headers_json: str, body: bytes) -> None:
    conn = _connect()
    try:
        count = conn.execute("SELECT COUNT(*) AS n FROM response_cache").fetchone()["n"]
        if count >= MAX_CACHED_RESPONSES:
            conn.execute(
                "DELETE FROM response_cache WHERE request_hash IN ("
                "SELECT request_hash FROM response_cache ORDER BY created_at ASC LIMIT ?)",
                (count - MAX_CACHED_RESPONSES + 1,),
            )
        conn.execute(
            "INSERT OR REPLACE INTO response_cache (request_hash, status_code, headers, body) VALUES (?, ?, ?, ?)",
            (request_hash, status_code, headers_json, body),
        )
        conn.commit()
    finally:
        conn.close()


def clear_request_data() -> dict:
    """Wipe request logs + response cache. Settings are preserved."""
    conn = _connect()
    try:
        logs = conn.execute("SELECT COUNT(*) AS n FROM request_logs").fetchone()["n"]
        cache = conn.execute("SELECT COUNT(*) AS n FROM response_cache").fetchone()["n"]
        conn.execute("DELETE FROM request_logs")
        conn.execute("DELETE FROM response_cache")
        conn.commit()
    finally:
        conn.close()
    return {"cleared_logs": logs, "cleared_cache": cache}


def get_stats(
    recent_limit: int = 40,
    *,
    include_noise: bool = False,
    range_key: str = "24h",
) -> dict:
    rk = _normalize_range(range_key)
    visible = _visible_rows(include_noise=include_noise)
    now = _now_utc()

    if rk == "all":
        until = now
        span = None
        since = None
        if visible:
            ts_vals = [_parse_log_timestamp(r.get("timestamp")) for r in visible]
            ts_vals = [t for t in ts_vals if t is not None]
            if ts_vals:
                since = min(ts_vals)
                span = until - since
    else:
        delta = RANGE_PRESETS[rk]
        since = now - delta
        until = now
        span = delta

    current_rows = _filter_rows_by_window(visible, since=since, until=None)
    agg = _aggregate_rows(current_rows)
    recent = [_row_for_api(r) for r in current_rows[:recent_limit]]

    granularity = _granularity_for_range(rk, span)
    timeseries: list[dict] = []
    if since is not None and current_rows:
        timeseries = _build_timeseries(
            current_rows, since=since, until=until, granularity=granularity
        )

    prev_agg: dict | None = None
    if since is not None and rk != "all":
        prev_since = since - (until - since)
        prev_rows = _filter_rows_by_window(visible, since=prev_since, until=since)
        prev_agg = _aggregate_rows(prev_rows)

    compare: dict | None = None
    if prev_agg is not None:
        compare = {
            "requests_pct": _pct_change(agg["total_requests"], prev_agg["total_requests"]),
            "tokens_saved_pct": _pct_change(
                agg["total_tokens_saved"], prev_agg["total_tokens_saved"]
            ),
            "original_input_pct": _pct_change(
                agg["total_original_input_tokens"], prev_agg["total_original_input_tokens"]
            ),
            "save_rate_pct": _pct_change(agg["avg_save_pct"], prev_agg["avg_save_pct"]),
            "usd_saved_pct": _pct_change(
                agg["estimated_usd_saved"], prev_agg["estimated_usd_saved"]
            ),
        }

    return {
        **agg,
        "range": rk,
        "granularity": granularity,
        "timeseries": timeseries,
        "compare_previous": compare,
        "pricing": {
            "updated_at": get_meta("prices_updated_at"),
            "source": get_meta("prices_source"),
            "count": count_model_prices(),
        },
        "settings": get_settings(),
        "recent_logs": recent,
        "include_noise": include_noise,
    }
