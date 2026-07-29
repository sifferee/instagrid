import React, { useState, useEffect } from 'react'
import { api } from '../api/client'

const s = {
  btn: { padding: '6px 14px', border: 'none', borderRadius: 6, cursor: 'pointer', fontSize: 13, fontWeight: 500 },
  btnPrimary: { background: '#238636', color: '#fff' },
  btnDanger: { background: '#da3633', color: '#fff' },
  btnSecondary: { background: '#30363d', color: '#e6edf3' },
  input: { padding: '7px 10px', background: '#0d1117', border: '1px solid #30363d', borderRadius: 6, color: '#e6edf3', fontSize: 13, outline: 'none' },
  select: { padding: '7px 10px', background: '#0d1117', border: '1px solid #30363d', borderRadius: 6, color: '#e6edf3', fontSize: 13 },
  textarea: { padding: '8px 10px', background: '#0d1117', border: '1px solid #30363d', borderRadius: 6, color: '#e6edf3', fontSize: 13, fontFamily: 'monospace', resize: 'vertical', outline: 'none' },
  table: { width: '100%', borderCollapse: 'collapse' },
  th: { textAlign: 'left', padding: '10px 12px', borderBottom: '1px solid #30363d', color: '#8b949e', fontSize: 12, fontWeight: 600, textTransform: 'uppercase' },
  td: { padding: '10px 12px', borderBottom: '1px solid #21262d', fontSize: 14 },
  card: { background: '#161b22', borderRadius: 8, border: '1px solid #30363d', padding: 16, marginBottom: 16 },
  badge: (color) => ({ display: 'inline-block', padding: '2px 8px', borderRadius: 10, fontSize: 11, fontWeight: 600, background: color + '22', color }),
}

const STATUS_COLORS = { new: '#58a6ff', logged_in: '#3fb950', cooldown: '#d29922', dead: '#f85149' }
const STATUS_LABELS = { new: 'Новый', logged_in: 'Залогинен', cooldown: 'Cooldown', dead: 'Мёртвый' }

export default function AccountsPage() {
  const [accounts, setAccounts] = useState([])
  const [niches, setNiches] = useState([])
  const [pools, setPools] = useState([])
  const [filterNiche, setFilterNiche] = useState('')
  const [filterStatus, setFilterStatus] = useState('')
  const [showImport, setShowImport] = useState(false)
  const [importNiche, setImportNiche] = useState('')
  const [importData, setImportData] = useState('')
  const [importResult, setImportResult] = useState(null)
  const [selected, setSelected] = useState(new Set())
  const [moveNiche, setMoveNiche] = useState('')
  const [error, setError] = useState('')
  const [loginPoolId, setLoginPoolId] = useState('')
  const [loginState, setLoginState] = useState(null)
  const [loginRunning, setLoginRunning] = useState(false)

  const load = () => {
    const params = {}
    if (filterNiche) params.niche_id = filterNiche
    if (filterStatus) params.status = filterStatus
    api.getAccounts(params).then(setAccounts).catch(e => setError(e.message))
  }

  useEffect(() => {
    api.getNiches().then(setNiches)
    fetch('/api/proxies/pools').then(r => r.json()).then(setPools).catch(() => {})
  }, [])
  useEffect(() => { load() }, [filterNiche, filterStatus])

  const doImport = async () => {
    if (!importData.trim()) return
    try {
      const result = await api.bulkImport({
        niche_id: importNiche || null,
        data: importData,
      })
      setImportResult(result)
      setImportData('')
      load()
    } catch (e) { setError(e.message) }
  }

  const remove = async (id) => {
    if (!confirm('Удалить аккаунт? Привязанный прокси тоже удалится.')) return
    try {
      await api.deleteAccount(id)
      load()
    } catch (e) { setError(e.message) }
  }

  const toggleSelect = (id) => {
    const next = new Set(selected)
    next.has(id) ? next.delete(id) : next.add(id)
    setSelected(next)
  }

  const selectAll = () => {
    if (selected.size === accounts.length) setSelected(new Set())
    else setSelected(new Set(accounts.map(a => a.id)))
  }

  const doMove = async () => {
    if (selected.size === 0) return
    try {
      await api.moveAccounts([...selected], moveNiche || null)
      setSelected(new Set())
      load()
    } catch (e) { setError(e.message) }
  }

  const startLogin = async () => {
    if (!loginPoolId) { alert('Выбери пул прокси'); return }
    const ids = [...selected]
    if (!ids.length) { alert('Выбери аккаунты'); return }
    setLoginRunning(true)
    try {
      await fetch('/api/login/start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ account_ids: ids, pool_id: Number(loginPoolId) }),
      })
      const poll = setInterval(async () => {
        const res = await fetch('/api/login/status').then(r => r.json())
        setLoginState(res)
        if (!res.running) {
          clearInterval(poll)
          setLoginRunning(false)
          load()
        }
      }, 2000)
    } catch (e) {
      alert(e.message)
      setLoginRunning(false)
    }
  }

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 }}>
        <h2 style={{ margin: 0, fontSize: 22 }}>Аккаунты ({accounts.length})</h2>
        <button
          style={{ ...s.btn, ...s.btnPrimary }}
          onClick={() => setShowImport(!showImport)}
        >
          {showImport ? 'Скрыть импорт' : 'Массовый импорт'}
        </button>
      </div>

      {error && <div style={{ color: '#f85149', marginBottom: 12, fontSize: 13 }}>{error}</div>}

      {showImport && (
        <div style={s.card}>
          <div style={{ marginBottom: 8, fontSize: 13, color: '#8b949e' }}>
            Формат: <code>login:password</code> или <code>login:password:2fa_secret</code>, по одному на строку
          </div>
          <div style={{ display: 'flex', gap: 8, marginBottom: 8 }}>
            <select style={s.select} value={importNiche} onChange={e => setImportNiche(e.target.value)}>
              <option value="">Без ниши</option>
              {niches.map(n => <option key={n.id} value={n.id}>{n.name}</option>)}
            </select>
          </div>
          <textarea
            style={{ ...s.textarea, width: '100%', minHeight: 120 }}
            placeholder="user1:pass1&#10;user2:pass2:JBSWY3DPEHPK3PXP"
            value={importData}
            onChange={e => setImportData(e.target.value)}
          />
          <div style={{ marginTop: 8, display: 'flex', gap: 8, alignItems: 'center' }}>
            <button style={{ ...s.btn, ...s.btnPrimary }} onClick={doImport}>Импортировать</button>
            {importResult && (
              <span style={{ fontSize: 13, color: '#3fb950' }}>
                Импортировано: {importResult.imported}
                {importResult.errors?.length > 0 && (
                  <span style={{ color: '#f85149' }}>, ошибок: {importResult.errors.length}</span>
                )}
              </span>
            )}
          </div>
        </div>
      )}

      <div style={{ ...s.card, display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
        <select style={s.select} value={filterNiche} onChange={e => setFilterNiche(e.target.value)}>
          <option value="">Все ниши</option>
          {niches.map(n => <option key={n.id} value={n.id}>{n.name}</option>)}
        </select>
        <select style={s.select} value={filterStatus} onChange={e => setFilterStatus(e.target.value)}>
          <option value="">Все статусы</option>
          {Object.entries(STATUS_LABELS).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
        </select>
        {selected.size > 0 && (
          <>
            <span style={{ color: '#8b949e', fontSize: 13 }}>Выбрано: {selected.size}</span>
            <select style={s.select} value={moveNiche} onChange={e => setMoveNiche(e.target.value)}>
              <option value="">Без ниши</option>
              {niches.map(n => <option key={n.id} value={n.id}>{n.name}</option>)}
            </select>
            <button style={{ ...s.btn, ...s.btnSecondary }} onClick={doMove}>Переместить</button>
            <div style={{ width: 1, height: 20, background: '#30363d' }} />
            <select style={s.select} value={loginPoolId} onChange={e => setLoginPoolId(e.target.value)}>
              <option value="">Пул прокси</option>
              {pools.filter(p => p.pool_type === 'static').map(p => <option key={p.id} value={p.id}>{p.name}</option>)}
            </select>
            <button
              style={{ ...s.btn, background: '#1f6feb', color: '#fff', opacity: loginRunning ? 0.6 : 1 }}
              onClick={startLogin}
              disabled={loginRunning}
            >
              {loginRunning ? `Логин... (${loginState?.done || 0}/${loginState?.total || 0})` : 'Логин'}
            </button>
          </>
        )}
      </div>

      <table style={s.table}>
        <thead>
          <tr>
            <th style={{ ...s.th, width: 32 }}>
              <input type="checkbox" checked={selected.size === accounts.length && accounts.length > 0} onChange={selectAll} />
            </th>
            <th style={s.th}>Username</th>
            <th style={s.th}>Ниша</th>
            <th style={s.th}>Статус</th>
            <th style={s.th}>Прокси</th>
            <th style={s.th}>2FA</th>
            <th style={{ ...s.th, width: 80 }}></th>
          </tr>
        </thead>
        <tbody>
          {accounts.map(a => (
            <tr key={a.id} style={{ background: selected.has(a.id) ? '#1f2937' : 'transparent' }}>
              <td style={s.td}>
                <input type="checkbox" checked={selected.has(a.id)} onChange={() => toggleSelect(a.id)} />
              </td>
              <td style={s.td}>{a.username}</td>
              <td style={{ ...s.td, color: '#8b949e' }}>{a.niche_name || '—'}</td>
              <td style={s.td}>
                <span style={s.badge(STATUS_COLORS[a.status] || '#8b949e')}>
                  {STATUS_LABELS[a.status] || a.status}
                </span>
              </td>
              <td style={{ ...s.td, color: '#8b949e', fontSize: 12 }}>
                {a.proxy_host ? `${a.proxy_host}:${a.proxy_port}` : '—'}
              </td>
              <td style={{ ...s.td, color: a.totp_secret ? '#3fb950' : '#484f58' }}>
                {a.totp_secret ? '✓' : '—'}
              </td>
              <td style={s.td}>
                <button style={{ ...s.btn, ...s.btnDanger, padding: '4px 10px' }} onClick={() => remove(a.id)}>✕</button>
              </td>
            </tr>
          ))}
          {accounts.length === 0 && (
            <tr><td style={{ ...s.td, color: '#8b949e' }} colSpan={7}>Нет аккаунтов. Импортируйте первые.</td></tr>
          )}
        </tbody>
      </table>

      {loginState && loginState.results && loginState.results.length > 0 && (
        <div style={{ ...s.card, marginTop: 16 }}>
          <h3 style={{ margin: '0 0 12px', fontSize: 15, color: '#e6edf3' }}>
            Результаты логина ({loginState.success} ✓ / {loginState.failed} ✗)
          </h3>
          {loginState.results.map((r, i) => (
            <div key={i} style={{
              display: 'flex', gap: 12, padding: '6px 0', borderBottom: '1px solid #21262d',
              fontSize: 13, alignItems: 'center',
            }}>
              <span style={{ color: r.success ? '#3fb950' : '#f85149', fontWeight: 600 }}>
                {r.success ? '✓' : '✗'}
              </span>
              <span style={{ color: '#e6edf3', minWidth: 140 }}>{r.username}</span>
              <span style={s.badge(r.success ? '#3fb950' : '#f85149')}>{r.status}</span>
              <span style={{ color: '#8b949e', flex: 1 }}>{r.message}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
