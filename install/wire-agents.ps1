# Wire coding CLIs to Gatekeep once. Crush is automatic for any JSON chat
# payload that hits Gatekeep (no per-tool optimizer toggle).
#
# Usage (from repo root):
#   powershell -File install/wire-agents.ps1
#   powershell -File install/wire-agents.ps1 -GatekeepUrl http://your-server:9477

param(
    [string]$GatekeepUrl = $env:GATEKEEP_PUBLIC_URL,
    [string]$AgentUrl = $env:GATEKEEP_AGENT_URL
)

$ErrorActionPreference = "Stop"

if (-not $GatekeepUrl) {
    $port = if ($env:GATEKEEP_PORT) { $env:GATEKEEP_PORT } else { "9477" }
    $GatekeepUrl = "http://127.0.0.1:$port"
}
$GatekeepUrl = $GatekeepUrl.TrimEnd("/")

if (-not $AgentUrl) {
    if ($GatekeepUrl -match "^https?://([^:/]+)") {
        $gkHost = $Matches[1]
        $agentPort = if ($env:GATEKEEP_AGENT_PORT) { $env:GATEKEEP_AGENT_PORT } else { "9478" }
        $scheme = if ($GatekeepUrl.StartsWith("https")) { "https" } else { "http" }
        $AgentUrl = "${scheme}://${gkHost}:${agentPort}"
    } else {
        $AgentUrl = "http://127.0.0.1:9478"
    }
}
$AgentUrl = $AgentUrl.TrimEnd("/")

$Gatekeep = $GatekeepUrl
$utf8 = New-Object System.Text.UTF8Encoding $false

Write-Host "Gatekeep wire -> $Gatekeep"

# Claude Code
[Environment]::SetEnvironmentVariable("ANTHROPIC_BASE_URL", $Gatekeep, "User")
$env:ANTHROPIC_BASE_URL = $Gatekeep
$claudeDir = Join-Path $env:USERPROFILE ".claude"
$claude = Join-Path $claudeDir "settings.json"
New-Item -ItemType Directory -Force -Path $claudeDir | Out-Null
if (Test-Path $claude) {
  $j = Get-Content $claude -Raw -Encoding UTF8 | ConvertFrom-Json
} else {
  $j = [pscustomobject]@{ env = [pscustomobject]@{} }
}
if (-not $j.env) { $j | Add-Member -NotePropertyName env -NotePropertyValue ([pscustomobject]@{}) }
$j.env | Add-Member -NotePropertyName ANTHROPIC_BASE_URL -NotePropertyValue $Gatekeep -Force
($j | ConvertTo-Json -Depth 20) | Set-Content -Encoding utf8 $claude
Write-Host "  OK Claude  ANTHROPIC_BASE_URL (User env + settings.json)"

# Cursor agent
[Environment]::SetEnvironmentVariable("CURSOR_API_ENDPOINT", $Gatekeep, "User")
[Environment]::SetEnvironmentVariable("GATEKEEP_AGENT_URL", $AgentUrl, "User")
$env:CURSOR_API_ENDPOINT = $Gatekeep
$env:GATEKEEP_AGENT_URL = $AgentUrl
$cliCfg = Join-Path $env:USERPROFILE ".cursor\cli-config.json"
if (Test-Path $cliCfg) {
  $j = Get-Content $cliCfg -Raw -Encoding UTF8 | ConvertFrom-Json
  if ($j.PSObject.Properties.Name -contains "serverConfigCache") {
    $j.PSObject.Properties.Remove("serverConfigCache")
  }
  if (-not $j.network) { $j | Add-Member -NotePropertyName network -NotePropertyValue ([pscustomobject]@{}) }
  $j.network | Add-Member -NotePropertyName useHttp1ForAgent -NotePropertyValue $true -Force
  ($j | ConvertTo-Json -Depth 30) | Set-Content -Encoding utf8 $cliCfg
  Write-Host "  OK Cursor  CURSOR_API_ENDPOINT + useHttp1ForAgent + cache cleared"
} else {
  Write-Host "  OK Cursor  CURSOR_API_ENDPOINT (User env + this session)"
}

# Codex (ChatGPT subscription backend — not openai_base_url)
$codexDir = Join-Path $env:USERPROFILE ".codex"
New-Item -ItemType Directory -Force -Path $codexDir | Out-Null
$codexCfg = Join-Path $codexDir "config.toml"
$existing = if (Test-Path $codexCfg) { Get-Content $codexCfg -Raw } else { "" }
if ($existing -notmatch 'chatgpt_base_url') {
  $block = @"
chatgpt_base_url = `"$Gatekeep`"

"@
  [IO.File]::WriteAllText($codexCfg, $block + $existing, $utf8)
} else {
  $existing = $existing -replace 'chatgpt_base_url\s*=\s*"[^"]*"', "chatgpt_base_url = `"$Gatekeep`""
  [IO.File]::WriteAllText($codexCfg, $existing, $utf8)
}
Write-Host "  OK Codex   chatgpt_base_url"

# Mistral Vibe
$vibeDir = Join-Path $env:USERPROFILE ".vibe"
New-Item -ItemType Directory -Force -Path $vibeDir | Out-Null
$vibeCfg = Join-Path $vibeDir "config.toml"
$vibeToml = @"
theme = "auto"
active_model = "devstral-small"

[[providers]]
name = "mistral"
api_base = "$Gatekeep/v1"
api_style = "openai"
backend = "mistral"

[[models]]
name = "devstral-small-latest"
provider = "mistral"
alias = "devstral-small"
"@
[IO.File]::WriteAllText($vibeCfg, $vibeToml, $utf8)
Write-Host "  OK Vibe    mistral api_base"

Write-Host ""
Write-Host "Done. Restart your terminal, then run your CLI as usual."
