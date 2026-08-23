import { useState, useEffect, useCallback, useMemo, useRef } from 'react'
import SettingsPage from './Settings.jsx'
import WikiPage from './Wiki.jsx'
import Toggle from './Toggle.jsx'

const POLL = 3000
const PAGE_SIZE = 40

const RANGES = [
  { id: '1h', label: '1 Hour' },
  { id: '24h', label: '1 Day' },
  { id: '7d', label: '7 Days' },
  { id: '30d', label: '1 Month' },
  { id: 'all', label: 'All' },
]

const TOGGLES = [
  { key: 'compression_enabled', label: 'Compression', hint: 'Payload optimize + response cache' },
  { key: 'anti_yap_enabled', label: 'Anti-yap', hint: 'Shorter output estimates' },
  { key: 'history_pruning_enabled', label: 'History pruning', hint: 'Trim conversation history' },
  { key: 'log_truncation_enabled', label: 'Log truncation', hint: 'Errors + head/tail only' },
  { key: 'aggressive_enabled', label: 'Aggressive', hint: 'Maximum savings mode' },
]

const METRICS = [
  {
    id: 'sent',
    label: 'Tokens used',
    card: (s) => fmt(s?.total_optimized_input_tokens),
    cardSub: (s) => {
      const raw = s?.total_original_input_tokens || 0
      const sent = s?.total_optimized_input_tokens || 0
      return raw ? `${((sent / raw) * 100).toFixed(0)}% of raw wire forwarded` : 'after compression'
    },
    bucket: (b) => b.optimized_input_tokens || 0,
    fmt: (v) => fmt(v),
    chart: 'area',
  },
  {
    id: 'saved',
    label: 'Tokens saved',
    card: (s) => fmt(s?.total_tokens_saved),
    cardDelta: (c) => c?.tokens_saved_pct,
    bucket: (b) => b.input_tokens_saved || 0,
    fmt: (v) => fmt(v),
    chart: 'bar',
  },
  {
    id: 'rate',
    label: 'Save rate',
    card: (s) => `${Number(s?.avg_save_pct ?? 0).toFixed(1)}%`,
    cardDelta: (c) => c?.save_rate_pct,
    bucket: (b) => {
      const o = b.original_input_tokens || 0
      return o ? ((b.input_tokens_saved || 0) / o) * 100 : 0
    },
    fmt: (v) => `${v.toFixed(1)}%`,
    chart: 'area',
  },
  {
    id: 'prompt',
    label: 'Prompt tokens',
    card: (s) => fmt(s?.total_original_input_tokens),
    cardDelta: (c) => c?.original_input_pct,
    bucket: (b) => b.original_input_tokens || 0,
    fmt: (v) => fmt(v),
    chart: 'area',
  },
  {
    id: 'requests',
    label: 'Requests',
    card: (s) => fmt(s?.total_requests),
    cardDelta: (c) => c?.requests_pct,
    bucket: (b) => b.requests || 0,
    fmt: (v) => fmt(v),
    chart: 'bar',
  },
  {
    id: 'usd',
    label: 'Est. saved',
    card: (s) => fmtUsd(s?.estimated_usd_saved),
    cardDelta: (c) => c?.usd_saved_pct,
    bucket: (b) => b.input_tokens_saved || 0,
    fmt: (v) => fmt(v) + ' tok',
    chart: 'bar',
  },
]

function fmt(n) {
  const v = Number(n ?? 0)
  if (Math.abs(v) >= 1_000_000) return `${(v / 1_000_000).toFixed(2)}M`
  if (Math.abs(v) >= 10_000) return `${(v / 1000).toFixed(1)}k`
  return v.toLocaleString()
}

function fmtUsd(n) {
  return `$${Number(n ?? 0).toFixed(2)}`
}

function parseTs(raw) {
  if (!raw) return null
  const s = String(raw).trim().replace(' ', 'T')
  const normalized = s.endsWith('Z') || /[+-]\d{2}:\d{2}$/.test(s) ? s : `${s}Z`
  const d = new Date(normalized)
  return Number.isNaN(d.getTime()) ? null : d
}

function fmtTime(iso) {
  const d = parseTs(iso)
  if (!d) return iso || '—'
  return d.toLocaleString(undefined, {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  })
}

function fmtLogTime(iso) {
  const d = parseTs(iso)
  if (!d) return iso || '—'
  return d.toLocaleString(undefined, {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  })
}

function savePct(log) {
  if (!log?.original_input_tokens) return 0
  return (log.input_tokens_saved / log.original_input_tokens) * 100
}

function modelLabel(r) {
  return (r.model || r.tag || r.provider || '—').slice(0, 40)
}

function Spark({ values, active, h = 32 }) {
  const pts = values?.length ? values : [0]
  const max = Math.max(...pts, 1)
  const min = Math.min(...pts, 0)
  const span = Math.max(max - min, 1)
  const w = 100
  const step = pts.length > 1 ? w / (pts.length - 1) : w
  const path = pts
    .map((v, i) => {
      const x = i * step
      const y = h - ((v - min) / span) * (h - 4) - 2
      return `${i === 0 ? 'M' : 'L'}${x.toFixed(1)},${y.toFixed(1)}`
    })
    .join(' ')
  const dot =
    active != null && pts[active] != null
      ? (() => {
          const x = active * step
          const y = h - ((pts[active] - min) / span) * (h - 4) - 2
          return <circle cx={x} cy={y} r="2.5" fill="#fff" />
        })()
      : null
  return (
    <svg viewBox={`0 0 ${w} ${h}`} className="or-stat-spark" preserveAspectRatio="none">
      <path d={path} fill="none" stroke="#9ca3af" strokeWidth="1.5" vectorEffect="non-scaling-stroke" />
      {dot}
    </svg>
  )
}

function Delta({ value }) {
  if (value == null || Number.isNaN(value)) return null
  if (Math.abs(value) >= 500) return <span className="or-stat-delta">new this period</span>
  const sign = value > 0 ? '+' : ''
  return (
    <span className="or-stat-delta">
      {sign}
      {Number(value).toFixed(1)}% vs prev
    </span>
  )
}

function HeadroomHero({ stats }) {
  const wrapRef = useRef(null)
  const [hoverX, setHoverX] = useState(null)

  const stretch = Number(stats?.stretch_multiplier ?? 1)
  const raw = stats?.total_original_input_tokens || 0
  const sent = stats?.total_optimized_input_tokens || 0
  const saved = stats?.total_tokens_saved || 0
  const sentPct = raw ? Math.min(100, (sent / raw) * 100) : 0
  const crushPct = raw ? Math.max(0, 100 - sentPct) : 0

  const onMove = (e) => {
    const rect = wrapRef.current?.getBoundingClientRect()
    if (!rect) return
    const x = Math.max(0, Math.min(100, ((e.clientX - rect.left) / rect.width) * 100))
    setHoverX(x)
  }

  const hoverSent = hoverX != null && hoverX <= sentPct
  const tipLeft = hoverX ?? sentPct / 2

  return (
    <section className="gk-hero or-enter" aria-label="Effective headroom">
      <div className="gk-hero-top">
        <div>
          <p className="gk-eyebrow">Effective headroom</p>
          <h1 className="gk-headline">
            ×{stretch.toFixed(1)}
            <span className="gk-headline-badge">observed</span>
          </h1>
        </div>
      </div>

      <div className="gk-gauge-wrap" ref={wrapRef} onMouseMove={onMove} onMouseLeave={() => setHoverX(null)}>
        <div className="gk-gauge" role="img" aria-label={`${fmt(sent)} sent of ${fmt(raw)} raw tokens`}>
          <div className="gk-gauge-sent" style={{ width: `${sentPct}%` }} />
          <div className="gk-gauge-crush" style={{ width: `${crushPct}%` }} />
        </div>
        {hoverX != null && (
          <div className="gk-gauge-tip" style={{ left: `${tipLeft}%` }}>
            {hoverSent ? `forwarded · ${fmt(sent)}` : `crushed · ${fmt(saved)}`}
          </div>
        )}
        <div className="gk-gauge-labels">
          <span>
            raw <strong>{fmt(raw)}</strong>
          </span>
          <span>
            sent <strong>{fmt(sent)}</strong>
          </span>
          <span>
            gap <strong>{fmt(saved)}</strong>
          </span>
        </div>
        <p className="gk-gauge-footnote">
          Calculated from session token delta. Results update dynamically based on context depth and payload
          structure.
        </p>
      </div>
    </section>
  )
}

function StatCard({ label, value, delta, spark, active, hoverIdx, onClick }) {
  return (
    <button type="button" className={`or-stat ${active ? 'or-stat--active' : ''}`} onClick={onClick}>
      <div className="or-stat-label">{label}</div>
      <div className="or-stat-value">{value}</div>
      <Spark values={spark} active={active ? hoverIdx : null} />
      {delta}
    </button>
  )
}

function InteractiveChart({ series, metric, hoverIdx, onHover }) {
  const wrapRef = useRef(null)

  if (!series?.length) {
    return <p style={{ color: 'var(--faint)', margin: 0, fontSize: 13 }}>No activity in this period.</p>
  }

  const values = series.map((b) => metric.bucket(b))
  const max = Math.max(...values, 1)
  const w = 100
  const h = 100
  const step = series.length > 1 ? w / (series.length - 1) : w

  const handleMove = (e) => {
    const rect = wrapRef.current?.getBoundingClientRect()
    if (!rect) return
    const x = Math.max(0, Math.min(rect.width, e.clientX - rect.left))
    const idx =
      series.length <= 1 ? 0 : Math.round((x / rect.width) * (series.length - 1))
    onHover(idx)
  }

  const pts = values.map((v, i) => {
    const x = i * step
    const y = h - (v / max) * (h - 8) - 4
    return [x, y, v]
  })

  const hi = hoverIdx ?? -1
  const tip = hi >= 0 ? series[hi] : null
  const tipVal = hi >= 0 ? values[hi] : null

  return (
    <div
      ref={wrapRef}
      className="or-chart-interactive"
      onMouseMove={handleMove}
      onMouseLeave={() => onHover(null)}
    >
      <svg viewBox={`0 0 ${w} ${h}`} className="or-chart" preserveAspectRatio="none">
        {metric.chart === 'bar' ? (
          pts.map(([x, y, v], i) => {
            const barW = Math.max(step - 1, 1)
            const bh = h - y - 4
            return (
              <rect
                key={i}
                x={x - barW / 2}
                y={y}
                width={barW}
                height={bh}
                fill={i === hi ? '#d4d4d4' : '#2a2a2a'}
                className="or-chart-bar"
              />
            )
          })
        ) : (
          <>
            {(() => {
              const line = pts.map((p, i) => `${i === 0 ? 'M' : 'L'}${p[0].toFixed(1)},${p[1].toFixed(1)}`).join(' ')
              const area = `${line} L${pts[pts.length - 1][0].toFixed(1)},${h} L0,${h} Z`
              return (
                <>
                  <path d={area} fill="rgba(255,255,255,0.05)" />
                  <path d={line} fill="none" stroke="#d4d4d4" strokeWidth="1.2" vectorEffect="non-scaling-stroke" />
                </>
              )
            })()}
          </>
        )}
        {hi >= 0 && (
          <>
            <line x1={pts[hi][0]} y1={0} x2={pts[hi][0]} y2={h} stroke="#3f3f3f" strokeWidth="0.5" />
            <circle cx={pts[hi][0]} cy={pts[hi][1]} r="2.5" fill="#fff" />
          </>
        )}
      </svg>
      {tip && (
        <div
          className="or-chart-tip"
          style={{ left: `${series.length <= 1 ? 50 : (hi / (series.length - 1)) * 100}%` }}
        >
          <div className="or-chart-tip-time">{fmtTime(tip.t)}</div>
          <div className="or-chart-tip-val mono">{metric.fmt(tipVal)}</div>
          <div className="or-chart-tip-sub mono">
            {fmt(tip.requests || 0)} req · raw {fmt(tip.original_input_tokens)} · sent{' '}
            {fmt(tip.optimized_input_tokens)}
          </div>
        </div>
      )}
    </div>
  )
}

function diffLines(a, b) {
  const A = (a || '').split('\n')
  const B = (b || '').split('\n')
  const n = A.length
  const m = B.length
  const dp = Array.from({ length: n + 1 }, () => new Array(m + 1).fill(0))
  for (let i = n - 1; i >= 0; i--)
    for (let j = m - 1; j >= 0; j--)
      dp[i][j] = A[i] === B[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1])
  const out = []
  let i = 0
  let j = 0
  while (i < n && j < m) {
    if (A[i] === B[j]) {
      out.push({ t: 'same', s: A[i] })
      i++
      j++
    } else if (dp[i + 1][j] >= dp[i][j + 1]) {
      out.push({ t: 'del', s: A[i++] })
    } else {
      out.push({ t: 'add', s: B[j++] })
    }
  }
  while (i < n) out.push({ t: 'del', s: A[i++] })
  while (j < m) out.push({ t: 'add', s: B[j++] })
  return out
}

function Drawer({ log, onClose }) {
  if (!log) return null
  const diff = diffLines(log.original_payload_sample, log.optimized_payload_sample)
  return (
    <div className="or-drawer-bg" onClick={onClose}>
      <div className="or-drawer" onClick={(e) => e.stopPropagation()}>
        <div className="or-drawer-head">
          <div>
            <div className="mono" style={{ fontWeight: 600, color: 'var(--text)' }}>
              req/{log.id}
            </div>
            <div style={{ fontSize: 12, color: 'var(--faint)', marginTop: 4 }}>
              {fmtLogTime(log.timestamp)} · {log.provider} · {savePct(log).toFixed(1)}% saved
            </div>
          </div>
          <button type="button" className="or-btn" onClick={onClose}>
            Close
          </button>
        </div>
        <div className="or-drawer-body">
          <table className="or-table" style={{ marginBottom: 16 }}>
            <tbody>
              {[
                ['Raw tokens', fmt(log.original_input_tokens)],
                ['Sent tokens', fmt(log.optimized_input_tokens)],
                ['Saved', fmt(log.input_tokens_saved)],
                ['Model', modelLabel(log)],
                ['Technique', log.savings_category || '—'],
              ].map(([k, v]) => (
                <tr key={k}>
                  <td>{k}</td>
                  <td className="num mono">{v}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="or-eyebrow">Before</p>
          <pre className="or-pre">{log.original_payload_sample || '(empty)'}</pre>
          <p className="or-eyebrow" style={{ marginTop: 16 }}>
            After
          </p>
          <pre className="or-pre">{log.optimized_payload_sample || '(empty)'}</pre>
          <p className="or-eyebrow" style={{ marginTop: 16 }}>
            Diff
          </p>
          <pre className="or-pre">
            {diff.map((l, i) => (
              <div
                key={i}
                style={{
                  opacity: l.t === 'same' ? 0.35 : 1,
                  color: l.t === 'add' ? '#86efac' : l.t === 'del' ? '#fca5a5' : 'inherit',
                  textDecoration: l.t === 'del' ? 'line-through' : 'none',
                }}
              >
                {l.t === 'add' ? '+ ' : l.t === 'del' ? '- ' : '  '}
                {l.s}
              </div>
            ))}
          </pre>
        </div>
      </div>
    </div>
  )
}

export default function App() {
  const [page, setPage] = useState('activity')
  const [subtab, setSubtab] = useState('overview')
  const [settingsTab, setSettingsTab] = useState('configuration')
  const [activeMetric, setActiveMetric] = useState('sent')
  const [chartHover, setChartHover] = useState(null)
  const [techniqueFilter, setTechniqueFilter] = useState(null)
  const [livePulse, setLivePulse] = useState(false)
  const [stats, setStats] = useState(null)
  const [error, setError] = useState(null)
  const [range, setRange] = useState('24h')
  const [logs, setLogs] = useState([])
  const [logsTotal, setLogsTotal] = useState(0)
  const [logsHasMore, setLogsHasMore] = useState(false)
  const [logsLoading, setLogsLoading] = useState(false)
  const [showNoise, setShowNoise] = useState(false)
  const [selected, setSelected] = useState(null)

  const q = `include_noise=${showNoise ? 1 : 0}&range=${encodeURIComponent(range)}`

  const refresh = useCallback(async () => {
    try {
      const res = await fetch(`/api/stats?${q}`)
      if (!res.ok) throw new Error('offline')
      setStats(await res.json())
      setError(null)
      setLivePulse(true)
      window.setTimeout(() => setLivePulse(false), 700)
    } catch (e) {
      setError(e.message)
    }
  }, [q])

  const loadLogs = useCallback(
    async (offset = 0, append = false) => {
      setLogsLoading(true)
      try {
        const res = await fetch(`/api/logs?limit=${PAGE_SIZE}&offset=${offset}&${q}`)
        if (!res.ok) throw new Error('offline')
        const data = await res.json()
        setLogs((prev) => (append ? [...prev, ...data.logs] : data.logs))
        setLogsTotal(data.total)
        setLogsHasMore(data.has_more)
      } catch (e) {
        setError(e.message)
      } finally {
        setLogsLoading(false)
      }
    },
    [q],
  )

  useEffect(() => {
    const tick = () => {
      if (document.visibilityState !== 'visible') return
      refresh()
      loadLogs(0, false)
    }
    tick()
    const id = setInterval(tick, POLL)
    document.addEventListener('visibilitychange', tick)
    return () => {
      clearInterval(id)
      document.removeEventListener('visibilitychange', tick)
    }
  }, [refresh, loadLogs])

  useEffect(() => {
    const onKey = (e) => {
      if (e.key === 'Escape') setSelected(null)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  useEffect(() => {
    setChartHover(null)
  }, [range, activeMetric])

  const toggleSetting = async (key, next) => {
    setStats((s) => {
      if (!s) return s
      const settings = { ...s.settings, [key]: next ? 1 : 0 }
      if (key === 'aggressive_enabled' && next) {
        settings.history_pruning_enabled = 1
        settings.log_truncation_enabled = 1
      }
      return { ...s, settings }
    })
    try {
      await fetch(`/api/settings/${key}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ value: next ? 1 : 0 }),
      })
    } catch {
      refresh()
    }
  }

  const cmp = stats?.compare_previous
  const settings = stats?.settings ?? {}
  const aggressiveOn = !!settings.aggressive_enabled
  const metricDef = METRICS.find((m) => m.id === activeMetric) || METRICS[0]

  const chartSeries = useMemo(() => {
    const ts = stats?.timeseries || []
    if (ts.some((b) => (b.original_input_tokens || 0) > 0 || (b.requests || 0) > 0)) return ts
    if (stats?.total_requests) {
      return [
        {
          t: new Date().toISOString(),
          original_input_tokens: stats.total_original_input_tokens || 0,
          input_tokens_saved: stats.total_tokens_saved || 0,
          optimized_input_tokens: stats.total_optimized_input_tokens || 0,
          requests: stats.total_requests || 0,
        },
      ]
    }
    return []
  }, [stats])

  const sparks = useMemo(() => {
    const out = {}
    for (const m of METRICS) out[m.id] = chartSeries.map((b) => m.bucket(b))
    return out
  }, [chartSeries])

  const techniques = Object.entries(stats?.technique_hits || {})
    .map(([name, v]) => ({ name, count: v.count || 0, saved: v.saved || 0 }))
    .sort((a, b) => b.saved - a.saved)
    .slice(0, 15)

  const filteredLogs = useMemo(() => {
    if (!techniqueFilter) return logs
    return logs.filter((r) => (r.savings_category || '').includes(techniqueFilter))
  }, [logs, techniqueFilter])

  const onTechniqueClick = (name) => {
    setTechniqueFilter((prev) => (prev === name ? null : name))
    setSubtab('requests')
  }

  return (
    <div className="or-shell">
      <header className="or-nav">
        <div className="or-nav-left">
          <div className="or-logo">
            <img
              src={`${import.meta.env.BASE_URL}favicon.svg`}
              alt=""
              className="or-logo-mark"
              width={18}
              height={18}
            />
            Gatekeep
          </div>
          <nav className="or-nav-links" aria-label="Primary">
            <button
              type="button"
              className={`or-nav-link ${page === 'activity' ? 'or-nav-link--on' : ''}`}
              onClick={() => setPage('activity')}
            >
              Activity
            </button>
            <button
              type="button"
              className={`or-nav-link ${page === 'settings' ? 'or-nav-link--on' : ''}`}
              onClick={() => setPage('settings')}
            >
              Settings
            </button>
          </nav>
        </div>
        <div className="or-nav-right">
          <span className={`or-live ${error ? 'or-live--bad' : 'or-live--ok'} ${livePulse ? 'or-live--pulse' : ''}`}>
            {error ? 'offline' : 'live'}
          </span>
          <div className="or-seg" role="group" aria-label="Time range">
            {RANGES.map((r) => (
              <button
                key={r.id}
                type="button"
                className={`or-seg-btn ${range === r.id ? 'or-seg-btn--on' : ''}`}
                onClick={() => setRange(r.id)}
              >
                {r.label}
              </button>
            ))}
          </div>
        </div>
      </header>

      <main className="or-page">
        {page === 'activity' && (
          <>
            <div className="or-subtabs">
              <button
                type="button"
                className={`or-subtab ${subtab === 'overview' ? 'or-subtab--on' : ''}`}
                onClick={() => setSubtab('overview')}
              >
                Overview
              </button>
              <button
                type="button"
                className={`or-subtab ${subtab === 'requests' ? 'or-subtab--on' : ''}`}
                onClick={() => setSubtab('requests')}
              >
                Requests
                {techniqueFilter && <span className="or-subtab-badge">1</span>}
              </button>
            </div>

            {subtab === 'overview' && (
              <div key={range}>
                <HeadroomHero stats={stats} />

                <div className="gk-metric-rail">
                  {METRICS.map((m) => (
                    <StatCard
                      key={m.id}
                      label={m.label}
                      value={m.card(stats)}
                      delta={
                        m.cardSub ? (
                          <span className="or-stat-delta">{m.cardSub(stats)}</span>
                        ) : (
                          <Delta value={m.cardDelta?.(cmp)} />
                        )
                      }
                      spark={sparks[m.id]}
                      active={activeMetric === m.id}
                      hoverIdx={chartHover}
                      onClick={() => setActiveMetric(m.id)}
                    />
                  ))}
                </div>

                <div className="or-card or-chart-panel or-enter">
                  <div className="or-chart-head">
                    <h2 className="or-eyebrow" style={{ margin: 0 }}>
                      {metricDef.label}
                    </h2>
                    <span className="mono or-chart-hint">Scrub timeline · click metric above</span>
                  </div>
                  <InteractiveChart
                    series={chartSeries}
                    metric={metricDef}
                    hoverIdx={chartHover}
                    onHover={setChartHover}
                  />
                  <p style={{ margin: '12px 0 0', fontSize: 12, color: 'var(--faint)' }} className="mono">
                    Sent {fmt(stats?.total_optimized_input_tokens)} · avg {stats?.avg_latency_ms ?? 0}ms ·{' '}
                    {chartSeries.length} buckets
                  </p>
                </div>

                {techniques.length > 0 && (
                  <div className="or-table-wrap or-enter">
                    <div className="or-table-toolbar">
                      <span>Crush techniques</span>
                      <span className="mono">Click row → filter requests</span>
                    </div>
                    <table className="or-table">
                      <thead>
                        <tr>
                          <th>Technique</th>
                          <th className="num">Tokens saved</th>
                          <th className="num">Hits</th>
                        </tr>
                      </thead>
                      <tbody>
                        {techniques.map((r) => (
                          <tr
                            key={r.name}
                            className={`or-row-action ${techniqueFilter === r.name ? 'or-row--selected' : ''}`}
                            onClick={() => onTechniqueClick(r.name)}
                          >
                            <td className="mono">{r.name}</td>
                            <td className="num mono">{fmt(r.saved)}</td>
                            <td className="num mono" style={{ color: 'var(--muted)' }}>
                              {r.count}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </div>
            )}

            {subtab === 'requests' && (
              <div key={`${range}-${techniqueFilter}`} className="or-enter">
                <div className="or-table-wrap">
                  <div className="or-table-toolbar">
                    <span>
                      Requests · {filteredLogs.length}
                      {techniqueFilter ? ` filtered` : `/${logsTotal}`}
                    </span>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
                      {techniqueFilter && (
                        <button type="button" className="or-chip" onClick={() => setTechniqueFilter(null)}>
                          {techniqueFilter} ×
                        </button>
                      )}
                      <label style={{ display: 'flex', alignItems: 'center', gap: 8, cursor: 'pointer' }}>
                        <input
                          type="checkbox"
                          className="or-check"
                          checked={showNoise}
                          onChange={(e) => setShowNoise(e.target.checked)}
                        />
                        telemetry
                      </label>
                    </div>
                  </div>
                  <table className="or-table">
                    <thead>
                      <tr>
                        <th>Time</th>
                        <th>Provider</th>
                        <th>Model</th>
                        <th className="num">Prompt</th>
                        <th className="num">Sent</th>
                        <th className="num">Saved</th>
                        <th className="num">%</th>
                      </tr>
                    </thead>
                    <tbody>
                      {filteredLogs.length === 0 && (
                        <tr>
                          <td colSpan={7} style={{ textAlign: 'center', padding: 32, color: 'var(--faint)' }}>
                            {techniqueFilter ? 'No requests for this technique' : 'No requests in this period'}
                          </td>
                        </tr>
                      )}
                      {filteredLogs.map((r) => (
                        <tr
                          key={r.id}
                          className={`or-row-action ${selected?.id === r.id ? 'or-row--selected' : ''}`}
                          onClick={() => setSelected(r)}
                        >
                          <td className="mono" style={{ color: 'var(--muted)' }}>
                            {fmtLogTime(r.timestamp)}
                          </td>
                          <td>{r.provider}</td>
                          <td className="mono" style={{ color: 'var(--muted)' }}>
                            {modelLabel(r)}
                          </td>
                          <td className="num mono">{fmt(r.original_input_tokens)}</td>
                          <td className="num mono">{fmt(r.optimized_input_tokens)}</td>
                          <td className="num mono">{fmt(r.input_tokens_saved)}</td>
                          <td className="num mono">{savePct(r).toFixed(1)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                  {logsHasMore && !techniqueFilter && (
                    <div style={{ padding: 14, textAlign: 'center', borderTop: '1px solid var(--border)' }}>
                      <button
                        type="button"
                        className="or-btn"
                        disabled={logsLoading}
                        onClick={() => loadLogs(logs.length, true)}
                      >
                        {logsLoading ? '…' : 'Load more'}
                      </button>
                    </div>
                  )}
                </div>
              </div>
            )}
          </>
        )}

        {page === 'settings' && (
          <>
            <div className="or-subtabs">
              <button
                type="button"
                className={`or-subtab ${settingsTab === 'configuration' ? 'or-subtab--on' : ''}`}
                onClick={() => setSettingsTab('configuration')}
              >
                Configuration
              </button>
              <button
                type="button"
                className={`or-subtab ${settingsTab === 'wiki' ? 'or-subtab--on' : ''}`}
                onClick={() => setSettingsTab('wiki')}
              >
                Wiki
              </button>
            </div>

            {settingsTab === 'configuration' && (
              <>
                <div className="or-toggle-list">
                  {TOGGLES.map((t) => {
                    const locked =
                      aggressiveOn && (t.key === 'history_pruning_enabled' || t.key === 'log_truncation_enabled')
                    return (
                      <div key={t.key} className="or-toggle-row">
                        <div className="or-toggle-info">
                          <div className="or-toggle-label">{t.label}</div>
                          <div className="or-toggle-hint">{t.hint}</div>
                        </div>
                        <Toggle
                          checked={!!settings[t.key]}
                          disabled={locked}
                          onChange={(next) => toggleSetting(t.key, next)}
                        />
                      </div>
                    )
                  })}
                </div>

                <SettingsPage
                  onCleared={() => {
                    refresh()
                    loadLogs(0, false)
                  }}
                />
              </>
            )}

            {settingsTab === 'wiki' && <WikiPage />}
          </>
        )}
      </main>

      <footer className="or-site-foot">
        <div className="or-made-by">
          <a
            href="https://sealeria.com"
            target="_blank"
            rel="noopener noreferrer"
            className="or-made-by-link"
            aria-label="made by Sealeria"
          >
            <span className="or-made-by-text">made by</span>
            <span className="or-made-by-brand">SEALERIA</span>
          </a>
        </div>
      </footer>

      <Drawer log={selected} onClose={() => setSelected(null)} />
    </div>
  )
}
