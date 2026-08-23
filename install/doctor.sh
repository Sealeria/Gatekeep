#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PORT="${GATEKEEP_PORT:-9477}"
AGENT_PORT="${GATEKEEP_AGENT_PORT:-9478}"
BASE="${GATEKEEP_PUBLIC_URL:-http://127.0.0.1:${PORT}}"

echo "==> Gatekeep doctor"

if ! command -v docker >/dev/null 2>&1; then
  echo "FAIL: docker not found"
  exit 1
fi

if ! docker compose ps --status running 2>/dev/null | grep -q gatekeep; then
  echo "WARN: gatekeep container not running (start with: docker compose up -d)"
fi

for p in "$PORT" "$AGENT_PORT"; do
  if command -v ss >/dev/null 2>&1; then
    ss -ltn | grep -q ":${p} " && echo "OK  port ${p} listening" || echo "WARN port ${p} not listening"
  elif command -v netstat >/dev/null 2>&1; then
    netstat -an | grep -q ":${p}.*LISTEN" && echo "OK  port ${p} listening" || echo "WARN port ${p} not listening"
  fi
done

if curl -fsS "${BASE}/api/stats" >/dev/null; then
  echo "OK  GET ${BASE}/api/stats"
else
  echo "FAIL GET ${BASE}/api/stats"
  exit 1
fi

if curl -fsS -o /dev/null -w "" "${BASE}/dashboard/"; then
  echo "OK  GET ${BASE}/dashboard/"
else
  echo "WARN dashboard not reachable at ${BASE}/dashboard/"
fi

echo "==> doctor passed"
