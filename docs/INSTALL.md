# Gatekeep Install

Primary install path: **Docker Compose** (~5 minutes).

## Quick start

```bash
git clone <repo> && cd gatekeep
cp .env.example .env          # edit GATEKEEP_PUBLIC_URL on remote hosts
./install/install.sh
```

Or manually:

```bash
docker compose up -d --build
./install/doctor.sh
```

Open **http://127.0.0.1:9477/dashboard/** (or your `GATEKEEP_PUBLIC_URL`).

## Remote / central server

Edit `.env` before `docker compose up`:

```env
GATEKEEP_PUBLIC_URL=http://your-server:9477
GATEKEEP_AGENT_URL=http://your-server:9478
GATEKEEP_API_KEY=optional-secret
```

Expose ports **9477** (proxy + dashboard) and **9478** (Cursor agent).

Clients send `Authorization: Bearer <GATEKEEP_API_KEY>` when the key is set.

## Wire coding CLIs (client machines)

Paste in **PowerShell** (do not wrap in `powershell -Command`):

```powershell
$g='http://your-server:9477'; $a='http://your-server:9478'; $f=Join-Path $env:TEMP 'gk-wire.ps1'; irm "$g/install/wire-agents.ps1" -OutFile $f; & $f -GatekeepUrl $g -AgentUrl $a
```

Replace the URL with your `GATEKEEP_PUBLIC_URL` / agent URL. The dashboard Wiki tab shows the exact command for your server.

Dev (from repo checkout):

```powershell
powershell -File install/wire-agents.ps1 -GatekeepUrl http://your-server:9477
```

Sets Claude, Cursor, Codex, Vibe to point at Gatekeep. Does **not** install the server.

Manual overrides:

| CLI | Env / config |
|-----|----------------|
| Claude Code | `ANTHROPIC_BASE_URL` |
| Cursor | `CURSOR_API_ENDPOINT` + `GATEKEEP_AGENT_URL` |
| Codex | `chatgpt_base_url` in `~/.codex/config.toml` |
| Vibe | `api_base` in `~/.vibe/config.toml` |

## Local dev (no Docker)

```bash
pip install -r backend/requirements.txt
cd frontend && npm install && npm run build
cd ../backend && uvicorn main:app --host 127.0.0.1 --port 9477
```

Set `GATEKEEP_DEBUG=1` for verbose proxy logging.

## Operations

| Command | Action |
|---------|--------|
| `docker compose up -d` | Start |
| `docker compose down` | Stop |
| `docker compose logs -f` | Logs |
| `./install/doctor.sh` | Health check |

Data (SQLite logs/settings) persists in Docker volume `gatekeep-data`.

## Environment reference

See [.env.example](../.env.example).
