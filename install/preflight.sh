#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PORT="${GATEKEEP_PORT:-9477}"
AGENT_PORT="${GATEKEEP_AGENT_PORT:-9478}"

echo "==> Gatekeep preflight"

command -v docker >/dev/null || { echo "FAIL: install Docker"; exit 1; }
docker compose version >/dev/null 2>&1 || { echo "FAIL: docker compose plugin required"; exit 1; }

if [ ! -f .env ] && [ -f .env.example ]; then
  cp .env.example .env
  echo "OK  created .env from .env.example — edit GATEKEEP_PUBLIC_URL for remote access"
fi

port_busy() {
  local p=$1
  if command -v ss >/dev/null 2>&1; then
    ss -ltn | grep -q ":${p} "
  elif command -v netstat >/dev/null 2>&1; then
    netstat -an | grep -q ":${p}.*LISTEN"
  else
    return 1
  fi
}

if port_busy "$PORT"; then
  echo "WARN port ${PORT} already in use"
fi
if port_busy "$AGENT_PORT"; then
  echo "WARN port ${AGENT_PORT} already in use"
fi

echo "OK  preflight passed"
echo "Next: docker compose up -d --build && ./install/doctor.sh"
