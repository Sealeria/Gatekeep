import { useCallback, useEffect, useState } from 'react'

function fmtRate(n) {
  const v = Number(n ?? 0)
  if (v >= 1) return `$${v.toFixed(2)}`
  if (v >= 0.01) return `$${v.toFixed(3)}`
  return `$${v.toFixed(4)}`
}

export default function SettingsPage({ onCleared }) {
  const [pricing, setPricing] = useState(null)
  const [busy, setBusy] = useState(null)
  const [msg, setMsg] = useState(null)
  const [err, setErr] = useState(null)

  const loadPrices = useCallback(async () => {
    try {
      const res = await fetch('/api/prices')
      if (!res.ok) throw new Error('offline')
      setPricing(await res.json())
      setErr(null)
    } catch (e) {
      setErr(e.message)
    }
  }, [])

  useEffect(() => {
    loadPrices()
  }, [loadPrices])

  const refreshPrices = async () => {
    setBusy('prices')
    setMsg(null)
    try {
      const res = await fetch('/api/prices/refresh', { method: 'POST' })
      const data = await res.json().catch(() => ({}))
      if (!res.ok) throw new Error(data.detail || 'failed')
      setMsg(`Updated ${data.models} models`)
      await loadPrices()
    } catch (e) {
      setErr(String(e.message || e))
    } finally {
      setBusy(null)
    }
  }

  const clearData = async () => {
    if (!window.confirm('Clear all logs and cache?')) return
    setBusy('clear')
    setMsg(null)
    try {
      const res = await fetch('/api/data/clear', { method: 'POST' })
      const data = await res.json().catch(() => ({}))
      if (!res.ok) throw new Error('failed')
      setMsg(`Cleared ${data.cleared_logs ?? 0} logs`)
      onCleared?.()
    } catch (e) {
      setErr(String(e.message || e))
    } finally {
      setBusy(null)
    }
  }

  const models = pricing?.models ?? []

  return (
    <>
      <div className="or-card" style={{ marginBottom: 24 }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 }}>
          <h2 className="or-eyebrow" style={{ margin: 0 }}>
            Data
          </h2>
          <span className="mono" style={{ fontSize: 12, color: 'var(--faint)' }}>
            {pricing?.updated_at ? `${pricing.updated_at} · ${pricing.count} in DB` : 'prices not fetched'}
          </span>
        </div>
        <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap' }}>
          <button type="button" className="or-btn or-btn-primary" disabled={busy === 'prices'} onClick={refreshPrices}>
            {busy === 'prices' ? '…' : 'Update prices'}
          </button>
          <button type="button" className="or-btn" disabled={busy === 'clear'} onClick={clearData}>
            {busy === 'clear' ? '…' : 'Clear logs'}
          </button>
        </div>
      </div>

      {(msg || err) && (
        <p style={{ marginBottom: 16, color: err ? '#fca5a5' : 'var(--muted)', fontSize: 13 }}>{err || msg}</p>
      )}

      <div className="or-table-wrap">
        <div className="or-table-toolbar">
          <span>Popular models</span>
          <span style={{ fontSize: 12 }}>per MTok · OpenRouter-style ranking</span>
        </div>
        <table className="or-table">
          <thead>
            <tr>
              <th>Model</th>
              <th className="num">Input</th>
              <th className="num">Output</th>
              <th>Source</th>
            </tr>
          </thead>
          <tbody>
            {models.length === 0 && (
              <tr>
                <td colSpan={4} style={{ textAlign: 'center', padding: 24, color: 'var(--faint)' }}>
                  No prices loaded
                </td>
              </tr>
            )}
            {models.map((m) => (
              <tr key={m.display || m.model}>
                <td className="mono">{m.display || m.model}</td>
                <td className="num mono">{fmtRate(m.input_per_mtok)}</td>
                <td className="num mono">{fmtRate(m.output_per_mtok)}</td>
                <td style={{ color: 'var(--muted)' }}>{m.source}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  )
}
