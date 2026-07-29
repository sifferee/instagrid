import React, { useState, useEffect } from 'react'
import { api } from '../api/client'

const s = {
  btn: { padding: '6px 14px', border: 'none', borderRadius: 6, cursor: 'pointer', fontSize: 13, fontWeight: 500 },
  btnPrimary: { background: '#238636', color: '#fff' },
  btnDanger: { background: '#da3633', color: '#fff' },
  btnSecondary: { background: '#30363d', color: '#e6edf3' },
  input: { padding: '7px 10px', background: '#0d1117', border: '1px solid #30363d', borderRadius: 6, color: '#e6edf3', fontSize: 13, outline: 'none' },
  select: { padding: '7px 10px', background: '#0d1117', border: '1px solid #30363d', borderRadius: 6, color: '#e6edf3', fontSize: 13 },
  table: { width: '100%', borderCollapse: 'collapse' },
  th: { textAlign: 'left', padding: '10px 12px', borderBottom: '1px solid #30363d', color: '#8b949e', fontSize: 12, fontWeight: 600, textTransform: 'uppercase' },
  td: { padding: '10px 12px', borderBottom: '1px solid #21262d', fontSize: 14 },
  card: { background: '#161b22', borderRadius: 8, border: '1px solid #30363d', padding: 16, marginBottom: 16 },
}

export default function NichesPage() {
  const [niches, setNiches] = useState([])
  const [pools, setPools] = useState([])
  const [newName, setNewName] = useState('')
  const [newPoolId, setNewPoolId] = useState('')
  const [editId, setEditId] = useState(null)
  const [editName, setEditName] = useState('')
  const [editPoolId, setEditPoolId] = useState('')
  const [error, setError] = useState('')

  const load = () => {
    api.getNiches().then(setNiches).catch(e => setError(e.message))
    fetch('/api/proxies/pools').then(r => r.json()).then(setPools).catch(() => {})
  }

  useEffect(() => { load() }, [])

  const create = async () => {
    if (!newName.trim()) return
    try {
      await fetch('/api/niches', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name: newName.trim(),
          proxy_pool_id: newPoolId ? parseInt(newPoolId) : null,
        }),
      })
      setNewName('')
      setNewPoolId('')
      setError('')
      load()
    } catch (e) { setError(e.message) }
  }

  const save = async (id) => {
    try {
      await fetch(`/api/niches/${id}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name: editName.trim() || undefined,
          proxy_pool_id: editPoolId === '' ? null : (editPoolId === '0' ? null : parseInt(editPoolId)),
        }),
      })
      setEditId(null)
      setError('')
      load()
    } catch (e) { setError(e.message) }
  }

  const remove = async (id, name) => {
    if (!confirm(`Удалить нишу «${name}»? Аккаунты останутся без ниши.`)) return
    try {
      await api.deleteNiche(id)
      setError('')
      load()
    } catch (e) { setError(e.message) }
  }

  const staticPools = pools.filter(p => p.pool_type === 'static')

  return (
    <div>
      <h2 style={{ margin: '0 0 16px', fontSize: 22 }}>Ниши</h2>
      {error && <div style={{ color: '#f85149', marginBottom: 12, fontSize: 13 }}>{error}</div>}

      <div style={{ ...s.card, display: 'flex', gap: 8, alignItems: 'center' }}>
        <input
          style={{ ...s.input, flex: 1 }}
          placeholder="Название новой ниши"
          value={newName}
          onChange={e => setNewName(e.target.value)}
          onKeyDown={e => e.key === 'Enter' && create()}
        />
        <select style={s.select} value={newPoolId} onChange={e => setNewPoolId(e.target.value)}>
          <option value="">Без пула прокси</option>
          {staticPools.map(p => (
            <option key={p.id} value={p.id}>{p.name} ({p.available_count || 0} своб.)</option>
          ))}
        </select>
        <button style={{ ...s.btn, ...s.btnPrimary }} onClick={create}>Создать</button>
      </div>

      <table style={s.table}>
        <thead>
          <tr>
            <th style={s.th}>Ниша</th>
            <th style={s.th}>Аккаунтов</th>
            <th style={s.th}>Пул прокси</th>
            <th style={{ ...s.th, width: 220 }}>Действия</th>
          </tr>
        </thead>
        <tbody>
          {niches.map(n => (
            <tr key={n.id}>
              <td style={s.td}>
                {editId === n.id ? (
                  <input
                    style={s.input}
                    value={editName}
                    onChange={e => setEditName(e.target.value)}
                    onKeyDown={e => e.key === 'Enter' && save(n.id)}
                    autoFocus
                  />
                ) : n.name}
              </td>
              <td style={s.td}>{n.account_count}</td>
              <td style={s.td}>
                {editId === n.id ? (
                  <select style={s.select} value={editPoolId} onChange={e => setEditPoolId(e.target.value)}>
                    <option value="0">Без пула</option>
                    {staticPools.map(p => (
                      <option key={p.id} value={p.id}>{p.name}</option>
                    ))}
                  </select>
                ) : (
                  <span style={{ color: n.pool_name ? '#58a6ff' : '#484f58' }}>
                    {n.pool_name || '—'}
                  </span>
                )}
              </td>
              <td style={s.td}>
                {editId === n.id ? (
                  <div style={{ display: 'flex', gap: 6 }}>
                    <button style={{ ...s.btn, ...s.btnPrimary }} onClick={() => save(n.id)}>Сохранить</button>
                    <button style={{ ...s.btn, ...s.btnSecondary }} onClick={() => setEditId(null)}>Отмена</button>
                  </div>
                ) : (
                  <div style={{ display: 'flex', gap: 6 }}>
                    <button style={{ ...s.btn, ...s.btnSecondary }} onClick={() => {
                      setEditId(n.id)
                      setEditName(n.name)
                      setEditPoolId(n.proxy_pool_id || '0')
                    }}>Редактировать</button>
                    <button style={{ ...s.btn, ...s.btnDanger }} onClick={() => remove(n.id, n.name)}>Удалить</button>
                  </div>
                )}
              </td>
            </tr>
          ))}
          {niches.length === 0 && (
            <tr><td style={{ ...s.td, color: '#8b949e' }} colSpan={4}>Нет ниш. Создайте первую.</td></tr>
          )}
        </tbody>
      </table>
    </div>
  )
}
