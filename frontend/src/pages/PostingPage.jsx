import React, { useState, useEffect } from 'react'
import { api } from '../api/client'

const s = {
  card: { background: '#161b22', border: '1px solid #30363d', borderRadius: 8, padding: 16, marginBottom: 16 },
  btn: { padding: '8px 16px', borderRadius: 6, border: 'none', cursor: 'pointer', fontSize: 13, fontWeight: 600 },
  btnPrimary: { background: '#238636', color: '#fff' },
  btnSecondary: { background: '#30363d', color: '#e6edf3' },
  btnDanger: { background: '#da3633', color: '#fff' },
  btnWarning: { background: '#d29922', color: '#000' },
  input: { background: '#0d1117', border: '1px solid #30363d', borderRadius: 6, padding: '8px 12px', color: '#e6edf3', fontSize: 13 },
  badge: (color) => ({ display: 'inline-block', padding: '4px 12px', borderRadius: 12, fontSize: 12, fontWeight: 600, background: color, color: '#fff', marginRight: 8 }),
  table: { width: '100%', borderCollapse: 'collapse', fontSize: 13 },
  th: { padding: '8px 12px', textAlign: 'left', borderBottom: '1px solid #30363d', color: '#8b949e', fontWeight: 600 },
  td: { padding: '8px 12px', borderBottom: '1px solid #21262d' },
  flex: { display: 'flex', gap: 12, alignItems: 'center', flexWrap: 'wrap' },
  checkbox: { width: 16, height: 16, cursor: 'pointer' },
}

export default function PostingPage() {
  const [accounts, setAccounts] = useState([])
  const [selected, setSelected] = useState(new Set())
  const [reelsCount, setReelsCount] = useState(3)
  const [status, setStatus] = useState(null)
  const [results, setResults] = useState([])
  const [mode, setMode] = useState('manual')
  const [msg, setMsg] = useState('')
  const [polling, setPolling] = useState(false)

  useEffect(() => {
    api.getAccounts({ status: 'logged_in' }).then(setAccounts).catch(() => {})
    api.getPostingStatus().then(s => {
      setStatus(s)
      setResults(s.results || [])
      if (s.is_running) setPolling(true)
    }).catch(() => {})
  }, [])

  useEffect(() => {
    if (!polling) return
    const interval = setInterval(async () => {
      try {
        const s = await api.getPostingStatus()
        setStatus(s)
        setResults(s.results || [])
        if (!s.is_running) setPolling(false)
      } catch {}
    }, 5000)
    return () => clearInterval(interval)
  }, [polling])

  const toggleAll = () => {
    if (selected.size === accounts.length) setSelected(new Set())
    else setSelected(new Set(accounts.map(a => a.id)))
  }

  const toggle = (id) => {
    const next = new Set(selected)
    next.has(id) ? next.delete(id) : next.add(id)
    setSelected(next)
  }

  const startManual = async () => {
    if (selected.size === 0) return setMsg('Выбери аккаунты')
    setMsg('Запускаю постинг...')
    try {
      const r = await api.manualPost([...selected], reelsCount)
      setResults(r.results)
      setMsg(`Готово: ${r.completed} аккаунтов обработано`)
    } catch (e) { setMsg('Ошибка: ' + e.message) }
  }

  const startAuto = async () => {
    setMsg('Запускаю автопостинг...')
    try {
      await api.startAutoPost({
        account_ids: selected.size > 0 ? [...selected] : undefined,
        reels_count: reelsCount,
        loop_forever: true,
      })
      setMsg('Автопостинг запущен')
      setPolling(true)
    } catch (e) { setMsg('Ошибка: ' + e.message) }
  }

  const stopAuto = async () => {
    try {
      await api.stopAutoPost()
      setMsg('Остановка...')
    } catch (e) { setMsg('Ошибка: ' + e.message) }
  }

  const isRunning = status?.is_running
  const progress = status?.progress || {}

  return (
    <div>
      <h1 style={{ fontSize: 22, fontWeight: 700, marginBottom: 20 }}>Постинг</h1>

      {msg && <div style={{ ...s.card, borderColor: '#58a6ff' }}>{msg}</div>}

      {/* Статус */}
      {isRunning && (
        <div style={{ ...s.card, borderColor: '#238636' }}>
          <div style={s.flex}>
            <span style={s.badge('#238636')}>● Работает</span>
            <span style={{ color: '#8b949e', fontSize: 13 }}>
              {progress.processed || 0}/{progress.total || 0} аккаунтов | {progress.total_reels_posted || 0} рилсов
            </span>
            <button onClick={stopAuto} style={{ ...s.btn, ...s.btnDanger }}>Остановить</button>
          </div>
        </div>
      )}

      {/* Настройки */}
      <div style={{ ...s.card, ...s.flex }}>
        <div style={s.flex}>
          <button onClick={() => setMode('manual')} style={{ ...s.btn, ...(mode === 'manual' ? s.btnPrimary : s.btnSecondary) }}>Ручной</button>
          <button onClick={() => setMode('auto')} style={{ ...s.btn, ...(mode === 'auto' ? s.btnWarning : s.btnSecondary) }}>Авто</button>
        </div>
        <label style={{ color: '#8b949e', fontSize: 12 }}>Рилсов:</label>
        <input type="number" value={reelsCount} onChange={e => setReelsCount(Number(e.target.value))} min={1} max={10} style={{ ...s.input, width: 60 }} />
        {mode === 'manual' && <button onClick={startManual} disabled={isRunning} style={{ ...s.btn, ...s.btnPrimary }}>Запустить</button>}
        {mode === 'auto' && !isRunning && <button onClick={startAuto} style={{ ...s.btn, ...s.btnWarning }}>Запустить автоцикл</button>}
      </div>

      {/* Выбор аккаунтов */}
      <div style={s.card}>
        <div style={{ ...s.flex, marginBottom: 12 }}>
          <span style={{ fontSize: 14, fontWeight: 600 }}>Аккаунты ({accounts.length})</span>
          <button onClick={toggleAll} style={{ ...s.btn, ...s.btnSecondary, padding: '4px 10px' }}>
            {selected.size === accounts.length ? 'Снять все' : 'Выбрать все'}
          </button>
          <span style={{ color: '#8b949e', fontSize: 12 }}>Выбрано: {selected.size}</span>
        </div>
        <table style={s.table}>
          <thead><tr>
            <th style={s.th}></th><th style={s.th}>Username</th><th style={s.th}>Статус</th>
            <th style={s.th}>Прокси</th><th style={s.th}>Последний постинг</th>
          </tr></thead>
          <tbody>
            {accounts.map(a => (
              <tr key={a.id} onClick={() => toggle(a.id)} style={{ cursor: 'pointer' }}>
                <td style={s.td}><input type="checkbox" checked={selected.has(a.id)} readOnly style={s.checkbox} /></td>
                <td style={s.td}>{a.username}</td>
                <td style={s.td}><span style={s.badge('#238636')}>{a.status}</span></td>
                <td style={s.td}>{a.proxy_host ? `${a.proxy_host}:${a.proxy_port}` : '—'}</td>
                <td style={s.td}>{a.last_action_at ? new Date(a.last_action_at * 1000).toLocaleString() : '—'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {/* Результаты */}
      {results.length > 0 && (
        <div style={s.card}>
          <div style={{ fontSize: 14, fontWeight: 600, marginBottom: 12 }}>Результаты</div>
          <table style={s.table}>
            <thead><tr>
              <th style={s.th}>Username</th><th style={s.th}>Статус</th>
              <th style={s.th}>Рилсов</th><th style={s.th}>Время</th><th style={s.th}>Сообщение</th>
            </tr></thead>
            <tbody>
              {results.map((r, i) => (
                <tr key={i}>
                  <td style={s.td}>{r.username}</td>
                  <td style={s.td}><span style={s.badge(r.status === 'success' ? '#238636' : '#da3633')}>{r.status}</span></td>
                  <td style={s.td}>{r.reels_posted}/{r.reels_target}</td>
                  <td style={s.td}>{r.duration_sec}s</td>
                  <td style={s.td}>{r.message || '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
