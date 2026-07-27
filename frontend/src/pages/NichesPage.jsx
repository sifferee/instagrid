import React, { useState, useEffect } from 'react'
import { api } from '../api/client'

const s = {
  btn: { padding: '6px 14px', border: 'none', borderRadius: 6, cursor: 'pointer', fontSize: 13, fontWeight: 500 },
  btnPrimary: { background: '#238636', color: '#fff' },
  btnDanger: { background: '#da3633', color: '#fff' },
  btnSecondary: { background: '#30363d', color: '#e6edf3' },
  input: { padding: '7px 10px', background: '#0d1117', border: '1px solid #30363d', borderRadius: 6, color: '#e6edf3', fontSize: 13, outline: 'none' },
  table: { width: '100%', borderCollapse: 'collapse' },
  th: { textAlign: 'left', padding: '10px 12px', borderBottom: '1px solid #30363d', color: '#8b949e', fontSize: 12, fontWeight: 600, textTransform: 'uppercase' },
  td: { padding: '10px 12px', borderBottom: '1px solid #21262d', fontSize: 14 },
  card: { background: '#161b22', borderRadius: 8, border: '1px solid #30363d', padding: 16, marginBottom: 16 },
}

export default function NichesPage() {
  const [niches, setNiches] = useState([])
  const [newName, setNewName] = useState('')
  const [editId, setEditId] = useState(null)
  const [editName, setEditName] = useState('')
  const [error, setError] = useState('')

  const load = () => api.getNiches().then(setNiches).catch(e => setError(e.message))

  useEffect(() => { load() }, [])

  const create = async () => {
    if (!newName.trim()) return
    try {
      await api.createNiche(newName.trim())
      setNewName('')
      setError('')
      load()
    } catch (e) { setError(e.message) }
  }

  const rename = async (id) => {
    if (!editName.trim()) return
    try {
      await api.renameNiche(id, editName.trim())
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
        <button style={{ ...s.btn, ...s.btnPrimary }} onClick={create}>Создать</button>
      </div>

      <table style={s.table}>
        <thead>
          <tr>
            <th style={s.th}>Ниша</th>
            <th style={s.th}>Аккаунтов</th>
            <th style={{ ...s.th, width: 180 }}>Действия</th>
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
                    onKeyDown={e => e.key === 'Enter' && rename(n.id)}
                    autoFocus
                  />
                ) : n.name}
              </td>
              <td style={s.td}>{n.account_count}</td>
              <td style={s.td}>
                {editId === n.id ? (
                  <div style={{ display: 'flex', gap: 6 }}>
                    <button style={{ ...s.btn, ...s.btnPrimary }} onClick={() => rename(n.id)}>Сохранить</button>
                    <button style={{ ...s.btn, ...s.btnSecondary }} onClick={() => setEditId(null)}>Отмена</button>
                  </div>
                ) : (
                  <div style={{ display: 'flex', gap: 6 }}>
                    <button style={{ ...s.btn, ...s.btnSecondary }} onClick={() => { setEditId(n.id); setEditName(n.name) }}>Переименовать</button>
                    <button style={{ ...s.btn, ...s.btnDanger }} onClick={() => remove(n.id, n.name)}>Удалить</button>
                  </div>
                )}
              </td>
            </tr>
          ))}
          {niches.length === 0 && (
            <tr><td style={{ ...s.td, color: '#8b949e' }} colSpan={3}>Нет ниш. Создайте первую.</td></tr>
          )}
        </tbody>
      </table>
    </div>
  )
}
