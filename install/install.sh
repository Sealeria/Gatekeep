#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo "==> Gatekeep install (Docker)"

bash install/preflight.sh
docker compose up -d --build
bash install/doctor.sh

echo ""
echo "Gatekeep is up."
echo "  Dashboard: ${GATEKEEP_PUBLIC_URL:-http://127.0.0.1:9477}/dashboard/"
echo "  Wire CLIs: powershell -File install/wire-agents.ps1 -GatekeepUrl <url>"
