import { useCallback, useEffect, useState } from 'react'

const GATEKEEP_PORT_DEFAULT = 9477
const AGENT_PORT_DEFAULT = 9478

function wireRemoteCommand(origin, agent) {
  return (
    `$g='${origin}'; $a='${agent}'; $f=Join-Path $env:TEMP 'gk-wire.ps1'; ` +
    `irm "${origin}/install/wire-agents.ps1" -OutFile $f; ` +
    `& $f -GatekeepUrl $g -AgentUrl $a`
  )
}

function wireRemoteCommandCmd(origin, agent) {
  return (
    `powershell -NoProfile -ExecutionPolicy Bypass -Command ` +
    `"& { $g='${origin}'; $a='${agent}'; $f=Join-Path $env:TEMP 'gk-wire.ps1'; ` +
    `irm ($g+'/install/wire-agents.ps1') -OutFile $f; ` +
    `& $f -GatekeepUrl $g -AgentUrl $a }"`
  )
}

function wikiInfoFromBrowser() {
  const origin = window.location.origin.replace(/\/$/, '')
  const { protocol, hostname, port } = window.location
  const agentPort = port && Number(port) === GATEKEEP_PORT_DEFAULT ? AGENT_PORT_DEFAULT : AGENT_PORT_DEFAULT
  const agent = `${protocol}//${hostname}:${agentPort}`
  return {
    gatekeep_url: origin,
    agent_url: agent,
    dashboard_url: `${origin}/dashboard/`,
    api_key_required: false,
    wire_command: wireRemoteCommand(origin, agent),
    wire_command_cmd: wireRemoteCommandCmd(origin, agent),
    wire_command_local: `powershell -File install/wire-agents.ps1 -GatekeepUrl "${origin}" -AgentUrl "${agent}"`,
    wire_script_url: `${origin}/install/wire-agents.ps1`,
    fallback: true,
  }
}
const AGENTS = [
  { id: 'overview', label: 'Overview' },
  { id: 'claude', label: 'Claude Code' },
  { id: 'cursor', label: 'Cursor' },
  { id: 'codex', label: 'Codex' },
  { id: 'vibe', label: 'Vibe' },
  { id: 'docker', label: 'Docker' },
]

function CopyBlock({ label, text }) {
  const [copied, setCopied] = useState(false)
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text)
      setCopied(true)
      window.setTimeout(() => setCopied(false), 1500)
    } catch {
      /* ignore */
    }
  }
  return (
    <div className="gk-wiki-block">
      {label && <div className="gk-wiki-block-label">{label}</div>}
      <pre className="gk-wiki-pre mono">{text}</pre>
      <button type="button" className="or-btn gk-wiki-copy" onClick={copy}>
        {copied ? 'Copied' : 'Copy'}
      </button>
    </div>
  )
}

function UrlRow({ label, url }) {
  return (
    <div className="gk-wiki-url">
      <span className="gk-wiki-url-label">{label}</span>
      <a href={url} className="mono gk-wiki-url-link" target="_blank" rel="noopener noreferrer">
        {url}
      </a>
    </div>
  )
}

function WireOverviewHint() {
  return (
    <p className="gk-wiki-note">
      First time? Use the one-liner on <strong>Overview</strong>. It wires Claude, Cursor, Codex, and Vibe in one step.
    </p>
  )
}

function AgentBody({ agent, info }) {
  const g = info.gatekeep_url
  const a = info.agent_url

  if (agent === 'overview') {
    return (
      <>
        <p className="gk-wiki-lead">
          Point your coding CLI at this Gatekeep instance once. Compression runs automatically on every request.
        </p>
        <UrlRow label="Proxy" url={g} />
        <UrlRow label="Dashboard" url={info.dashboard_url} />
        <UrlRow label="Cursor agent" url={a} />
        {info.api_key_required && (
          <p className="gk-wiki-note">API key auth is enabled. Include <code className="mono">Authorization: Bearer …</code> on CLI requests.</p>
        )}
        <h3 className="gk-wiki-h3">Wire your PC (Windows)</h3>
        <CopyBlock
          label="PowerShell (paste in an open terminal)"
          text={info.wire_command}
        />
        {info.wire_command_cmd && (
          <CopyBlock
            label="CMD (from cmd.exe only, not from PowerShell)"
            text={info.wire_command_cmd}
          />
        )}
        {info.wire_command_local && (
          <>
            <h3 className="gk-wiki-h3">From repo checkout</h3>
            <CopyBlock text={info.wire_command_local} />
          </>
        )}
        <p className="gk-wiki-hint">Sets Claude, Cursor, Codex, and Vibe on this machine. Restart terminal after.</p>
      </>
    )
  }

  if (agent === 'claude') {
    return (
      <>
        <p className="gk-wiki-lead">Claude Code uses the Anthropic Messages API via Gatekeep.</p>
        <WireOverviewHint />
        <h3 className="gk-wiki-h3">Claude only</h3>
        <CopyBlock
          label="Environment (User)"
          text={`ANTHROPIC_BASE_URL=${g}`}
        />
        <CopyBlock
          label="PowerShell"
          text={`[Environment]::SetEnvironmentVariable("ANTHROPIC_BASE_URL", "${g}", "User")`}
        />
        <p className="gk-wiki-hint">Restart terminal / Claude Code after. Uses your Anthropic account; no extra config.</p>
      </>
    )
  }

  if (agent === 'cursor') {
    return (
      <>
        <p className="gk-wiki-lead">Cursor CLI uses ConnectRPC with an agent listener rewrite.</p>
        <WireOverviewHint />
        <h3 className="gk-wiki-h3">Cursor only</h3>
        <CopyBlock
          label="Environment (User)"
          text={`CURSOR_API_ENDPOINT=${g}
GATEKEEP_AGENT_URL=${a}`}
        />
        <CopyBlock
          label="PowerShell"
          text={`[Environment]::SetEnvironmentVariable("CURSOR_API_ENDPOINT", "${g}", "User")
[Environment]::SetEnvironmentVariable("GATEKEEP_AGENT_URL", "${a}", "User")`}
        />
        <p className="gk-wiki-hint">
          Restart terminal. Clears <span className="mono">serverConfigCache</span> in{' '}
          <span className="mono">~/.cursor/cli-config.json</span> when present.
        </p>
      </>
    )
  }

  if (agent === 'codex') {
    return (
      <>
        <p className="gk-wiki-lead">OpenAI Codex (ChatGPT subscription) routes via chatgpt.com paths on Gatekeep.</p>
        <WireOverviewHint />
        <h3 className="gk-wiki-h3">Codex only (~/.codex/config.toml)</h3>
        <CopyBlock
          text={`chatgpt_base_url = "${g}"

[model_providers.gatekeep]
name = "gatekeep"
base_url = "${g}/backend-api/codex"
requires_openai_auth = true

model_provider = "gatekeep"`}
        />
        <p className="gk-wiki-hint">Uses your ChatGPT account. <span className="mono">chatgpt_base_url</span> alone does not route model turns.</p>
      </>
    )
  }

  if (agent === 'vibe') {
    return (
      <>
        <p className="gk-wiki-lead">Mistral Vibe uses your Mistral account via browser sign-in, same as the default CLI setup.</p>
        <WireOverviewHint />
        <h3 className="gk-wiki-h3">Vibe only (~/.vibe/config.toml)</h3>
        <CopyBlock
          text={`[[providers]]
name = "mistral"
api_base = "${g}/v1"
api_style = "openai"
backend = "mistral"

[[models]]
name = "devstral-small-latest"
provider = "mistral"
alias = "devstral-small"`}
        />
        <p className="gk-wiki-hint">
          Run <span className="mono">vibe --setup</span> to sign in with your Mistral account if needed. No API key required.
        </p>
      </>
    )
  }

  if (agent === 'docker') {
    return (
      <>
        <p className="gk-wiki-lead">Run Gatekeep on a server with Docker Compose.</p>
        <CopyBlock
          label="Install"
          text={`cp .env.example .env
# edit GATEKEEP_PUBLIC_URL=${g}
docker compose up -d --build
./install/doctor.sh`}
        />
        <CopyBlock
          label=".env (remote server)"
          text={`GATEKEEP_PUBLIC_URL=${g}
GATEKEEP_AGENT_URL=${a}
GATEKEEP_HOST=0.0.0.0
GATEKEEP_PORT=9477
GATEKEEP_AGENT_PORT=9478`}
        />
        <p className="gk-wiki-hint">Expose ports 9477 (proxy) and 9478 (Cursor agent) on your firewall or reverse proxy.</p>
      </>
    )
  }

  return null
}

export default function WikiPage() {
  const [agent, setAgent] = useState('overview')
  const [info, setInfo] = useState(null)

  const load = useCallback(async () => {
    try {
      const res = await fetch('/api/server-info')
      if (!res.ok) throw new Error(String(res.status))
      setInfo(await res.json())
    } catch {
      setInfo(wikiInfoFromBrowser())
    }
  }, [])

  useEffect(() => {
    load()
  }, [load])

  if (!info) {
    return <p style={{ color: 'var(--faint)' }}>Loading wiki…</p>
  }

  return (
    <div className="gk-wiki">
      {info.fallback && (
        <p className="gk-wiki-note" style={{ gridColumn: '1 / -1', marginBottom: 0 }}>
          URLs from this browser session. Restart Gatekeep after updates for server-configured URLs.
        </p>
      )}
      <nav className="gk-wiki-nav" aria-label="Agent setup">
        {AGENTS.map((a) => (
          <button
            key={a.id}
            type="button"
            className={`gk-wiki-nav-btn ${agent === a.id ? 'gk-wiki-nav-btn--on' : ''}`}
            onClick={() => setAgent(a.id)}
          >
            {a.label}
          </button>
        ))}
      </nav>
      <article className="gk-wiki-body or-card">
        <h2 className="gk-wiki-title">{AGENTS.find((a) => a.id === agent)?.label}</h2>
        <AgentBody agent={agent} info={info} />
      </article>
    </div>
  )
}
