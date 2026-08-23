# Agent / CLI ↔ Gatekeep

## Principle

**Point a CLI at Gatekeep once → crush is on by default.**  
No per-tool optimizer switch. Dashboard toggles stay on (`compression` + `aggressive` = 1).

One-shot wire (Claude, Cursor, Codex, Vibe):

```powershell
powershell -File install/wire-agents.ps1
```

Then run agents normally. Gatekeep must be up: `http://127.0.0.1:9477`.

## What gets crushed automatically

| Wire shape | CLIs | Behavior |
|------------|------|----------|
| Anthropic Messages | Claude Code, OpenCode | Full aggressive crush |
| OpenAI/Mistral `chat/completions` + `messages` | Vibe | Mild crush (tools/history/prose) |
| Cursor ConnectRPC | Cursor | Wirecrush (protobuf) |
| Codex Responses (`backend-api/codex`, zstd JSON) | Codex (ChatGPT auth) | Aggressive Responses crush |

Unsupported without a base-URL hook: Freebuff (hardcoded host). Devin after `devin auth login`.

## Already wired on this machine

- Claude: `ANTHROPIC_BASE_URL`
- Cursor: `CURSOR_API_ENDPOINT` (User env + PowerShell profile)
- Codex: `model_provider = "gatekeep"` + `[model_providers.gatekeep]` → `http://127.0.0.1:9477/backend-api/codex` (`requires_openai_auth = true`). `chatgpt_base_url` alone does **not** route model turns.
- Vibe: `api_base` → Gatekeep in `~/.vibe/config.toml`

## Dashboard

http://127.0.0.1:9477/dashboard/
