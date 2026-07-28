import React, { useState, useEffect } from 'react'
import { api } from '../api/client'

const s = {
  card: { background: '#161b22', border: '1px solid #30363d', borderRadius: 8, padding: 16, marginBottom: 16 },
  btn: { padding: '8px 16px', borderRadius: 6, border: 'none', cursor: 'pointer', fontSize: 13, fontWeight: 600 },
  btnPrimary: { background: '#238636', color: '#fff' },
  btnSecondary: { background: '#30363d', color: '#e6edf3' },
  btnDanger: { background: '#da3633', color: '#fff' },
  btnWarning: { background: '#d29922', color: '#000' },
  input: { background: '#0d1117', border: '1px solid #30363d', borderRadius: 6, padding: '8px 12px', color: '#e6edf3', width: '100%', fontSize: 13 },
  textarea: { background: '#0d1117', border: '1px solid #30363d', borderRadius: 6, padding: '8px 12px', color: '#e6edf3', width: '100%', fontSize: 13, minHeight: 100, resize: 'vertical', fontFamily: 'inherit' },
  badge: (color) => ({ display: 'inline-block', padding: '4px 12px', borderRadius: 12, fontSize: 12, fontWeight: 600, background: color, color: '#fff', marginRight: 8 }),
  table: { width: '100%', borderCollapse: 'collapse', fontSize: 13 },
  th: { padding: '8px 12px', textAlign: 'left', borderBottom: '1px solid #30363d', color: '#8b949e', fontWeight: 600 },
  td: { padding: '8px 12px', borderBottom: '1px solid #21262d' },
  flex: { display: 'flex', gap: 12, alignItems: 'center', flexWrap: 'wrap' },
  stat: { textAlign: 'center', padding: 16 },
  statNum: { fontSize: 28, fontWeight: 700, color: '#58a6ff' },
  statLabel: { fontSize: 12, color: '#8b949e', marginTop: 4 },
}

export default function CheckerPage() {
  const [tab, setTab] = useState('dashboard')
  const [checkers, setCheckers] = useState([])
  const [alive, setAlive] = useState(null)
  const [accounts, setAccounts] = useState([])
  const [results, setResults] = useState([])
  const [checkerStatus, setCheckerStatus] = useState(null)
  const [importText, setImportText] = useState('')
  const [selected, setSelected] = useState(new Set())
  const [msg, setMsg] = useState('')
  const [polling, setPolling] = useState(false)

  const load = async () => {
    try {
      setCheckers(await api.getCheckers())
      setAlive(await api.getAliveCheckers())
      setAccounts(await api.getAccounts({ status: 'logged_in' }))
      const st = await api.getCheckerStatus()
      setCheckerStatus(st)
      if (st.is_running) setPolling(true)
    } catch {}
  }
  useEffect(() => { load() }, [])

  useEffect(() => {
    if (!polling) return
    const interval = setInterval(async () => {
      try {
        const st = await api.getCheckerStatus()
        setCheckerStatus(st)
        const res = await api.getCheckerResults()
        setResults(res)
        if (!st.is_running) { setPolling(false); load() }
      } catch {}
    }, 5000)
    return () => clearInterval(interval)
  }, [polling])

  const importCheckers = async () => {
    if (!importText.trim()) return
    try {
      const r = await api.importCheckers(importText)
      setMsg(`Добавлено чекеров: ${r.added}`)
      setImportText('')
      load()
    } catch (e) { setMsg('Ошибка: ' + e.message) }
  }

  const manualCheck = async () => {
    if (selected.size === 0) return setMsg('Выбери аккаунты для проверки')
    setMsg('Проверяю...')
    try {
      const r = await api.manualCheck([...selected])
      setResults(r.results)
      setMsg(`Проверено: ${r.checked}`)
    } catch (e) { setMsg('Ошибка: ' + e.message) }
  }

  const startAuto = async () => {
    try {
      await api.startAutoCheck()
      setMsg('Автоцикл запущен (каждые 50-90 мин)')
      setPolling(true)
    } catch (e) { setMsg('Ошибка: ' + e.message) }
  }

  const stopAuto = async () => {
    try {
      await api.stopAutoCheck()
      setMsg('Останавливаю...')
    } catch (e) { setMsg('Ошибка: ' + e.message) }
  }

  const toggle = (id) => {
    const next = new Set(selected)
    next.has(id) ? next.delete(id) : next.add(id)
    setSelected(next)
  }

  const isRunning = checkerStatus?.is_running

  return (
    <div>
      <h1 style={{ fontSize: 22, fontWeight: 700, marginBottom: 20 }}>Чекер</h1>

      {msg && <div style={{ ...s.card, borderColor: '#58a6ff' }}>{msg}</div>}

      {/* Статус + алерты */}
      {alive && (
        <div style={{ ...s.card, borderColor: alive.alive < alive.min_required ? '#da3633' : '#30363d' }}>
          <div style={s.flex}>
            <span style={s.badge(alive.alive >= alive.min_required ? '#238636' : '#da3633')}>
              Чекеров: {alive.alive} (мин. {alive.min_required})
            </span>
            {isRunning && <span style={s.badge('#238636')}>● Авто-чек работает</span>}
            {checkerStatus?.alerts?.map((a, i) => <span key={i} style={s.badge('#da3633')}>{a}</span>)}
          </div>
        </div>
      )}

      {/* Табы */}
      <div style={{ ...s.flex, marginBottom: 16 }}>
        <button onClick={() => setTab('dashboard')} style={{ ...s.btn, ...(tab === 'dashboard' ? s.btnPrimary : s.btnSecondary) }}>Проверка</button>
        <button onClick={() => setTab('checkers')} style={{ ...s.btn, ...(tab === 'checkers' ? s.btnPrimary : s.btnSecondary) }}>Чекер-аккаунты</button>
        <button onClick={() => setTab('results')} style={{ ...s.btn, ...(tab === 'results' ? s.btnPrimary : s.btnSecondary) }}>Результаты ({results.length})</button>
      </div>

      {/* Проверка */}
      {tab === 'dashboard' && (
        <div>
          <div style={{ ...s.card, ...s.flex }}>
            <button onClick={manualCheck} disabled={isRunning} style={{ ...s.btn, ...s.btnPrimary }}>Проверить выбранные</button>
            {!isRunning ? (
              <button onClick={startAuto} style={{ ...s.btn, ...s.btnWarning }}>Запустить автоцикл</button>
            ) : (
              <button onClick={stopAuto} style={{ ...s.btn, ...s.btnDanger }}>Остановить</button>
            )}
          </div>

          <table style={s.table}>
            <thead><tr>
              <th style={s.th}></th><th style={s.th}>Username</th><th style={s.th}>Статус</th>
            </tr></thead>
            <tbody>
              {accounts.map(a => (
                <tr key={a.id} onClick={() => toggle(a.id)} style={{ cursor: 'pointer' }}>
                  <td style={s.td}><input type="checkbox" checked={selected.has(a.id)} readOnly style={{ width: 16, height: 16, cursor: 'pointer' }} /></td>
                  <td style={s.td}>{a.username}</td>
                  <td style={s.td}><span style={s.badge(a.status === 'logged_in' ? '#238636' : a.status === 'dead' ? '#da3633' : '#d29922')}>{a.status}</span></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* Чекер-аккаунты */}
      {tab === 'checkers' && (
        <div>
          <div style={s.card}>
            <div style={{ fontSize: 14, fontWeight: 600, marginBottom: 8 }}>Импорт чекеров</div>
            <textarea style={s.textarea} value={importText} onChange={e => setImportText(e.target.value)} placeholder="login:pass:2fa:host:port:user:pass (по строке)" />
            <div style={{ marginTop: 8 }}><button onClick={importCheckers} style={{ ...s.btn, ...s.btnPrimary }}>Импортировать</button></div>
          </div>
          <table style={s.table}>
            <thead><tr>
              <th style={s.th}>ID</th><th style={s.th}>Username</th><th style={s.th}>Статус</th>
              <th style={s.th}>Прокси</th><th style={s.th}>Последний раз</th>
            </tr></thead>
            <tbody>
              {checkers.map(c => (
                <tr key={c.id}>
                  <td style={s.td}>{c.id}</td>
                  <td style={s.td}>{c.username}</td>
                  <td style={s.td}><span style={s.badge(c.status === 'active' ? '#238636' : '#da3633')}>{c.status}</span></td>
                  <td style={s.td}>{c.proxy_host ? `${c.proxy_host}:${c.proxy_port}` : '—'}</td>
                  <td style={s.td}>{c.last_used_at ? new Date(c.last_used_at * 1000).toLocaleString() : '—'}</td>
                </tr>
              ))}
              {checkers.length === 0 && <tr><td colSpan={5} style={{ ...s.td, color: '#8b949e' }}>Нет чекеров</td></tr>}
            </tbody>
          </table>
        </div>
      )}

      {/* Результаты */}
      {tab === 'results' && (
        <div>
          <table style={s.table}>
            <thead><tr>
              <th style={s.th}>Аккаунт</th><th style={s.th}>Чекер</th><th style={s.th}>Подписчики</th>
              <th style={s.th}>Рилсов</th><th style={s.th}>Бан</th><th style={s.th}>Сообщение</th>
            </tr></thead>
            <tbody>
              {results.map((r, i) => (
                <tr key={i}>
                  <td style={s.td}>{r.target_username}</td>
                  <td style={s.td}>{r.checker_username}</td>
                  <td style={s.td}>{r.profile?.followers ?? '—'}</td>
                  <td style={s.td}>{r.profile?.reels_count ?? '—'}</td>
                  <td style={s.td}>
                    {r.profile?.is_banned ? <span style={s.badge('#da3633')}>БАН</span> : <span style={s.badge('#238636')}>OK</span>}
                  </td>
                  <td style={s.td}>{r.message || '—'}</td>
                </tr>
              ))}
              {results.length === 0 && <tr><td colSpan={6} style={{ ...s.td, color: '#8b949e' }}>Нет результатов</td></tr>}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
