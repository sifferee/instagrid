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

const STATUS_COLORS = { available: '#3fb950', bound: '#58a6ff', burned: '#f85149' }
const STATUS_LABELS = { available: 'Свободен', bound: 'Привязан', burned: 'Сожжён' }

export default function ProxiesPage() {
  const [pools, setPools] = useState([])
  const [selectedPool, setSelectedPool] = useState(null)
  const [proxies, setProxies] = useState([])
  const [newPoolName, setNewPoolName] = useState('')
  const [newPoolType, setNewPoolType] = useState('static')
  const [mobileHost, setMobileHost] = useState('')
  const [mobilePort, setMobilePort] = useState('')
  const [mobileUser, setMobileUser] = useState('')
  const [mobilePass, setMobilePass] = useState('')
  const [rotationUrl, setRotationUrl] = useState('')
  const [showAddProxies, setShowAddProxies] = useState(false)
  const [proxyData, setProxyData] = useState('')
  const [importResult, setImportResult] = useState(null)
  const [error, setError] = useState('')

  const loadPools = () => api.getPools().then(setPools).catch(e => setError(e.message))
  const loadProxies = (poolId) => api.getProxies(poolId).then(setProxies).catch(e => setError(e.message))

  useEffect(() => { loadPools() }, [])
  useEffect(() => { if (selectedPool) loadProxies(selectedPool.id) }, [selectedPool])

  const createPool = async () => {
    if (!newPoolName.trim()) return
    try {
      const data = { name: newPoolName.trim(), pool_type: newPoolType }
      if (newPoolType === 'mobile') {
        data.rotation_url = rotationUrl || null
        data.proxy_host = mobileHost || null
        data.proxy_port = mobilePort ? parseInt(mobilePort) : null
        data.proxy_username = mobileUser || null
        data.proxy_password = mobilePass || null
      }
      await api.createPool(data)
      setNewPoolName('')
      setRotationUrl('')
      setMobileHost(''); setMobilePort(''); setMobileUser(''); setMobilePass('')
      setError('')
      loadPools()
    } catch (e) { setError(e.message) }
  }

  const deletePool = async (id, name) => {
    if (!confirm(`Удалить пул «${name}» и все его прокси?`)) return
    try {
      await api.deletePool(id)
      if (selectedPool?.id === id) { setSelectedPool(null); setProxies([]) }
      loadPools()
    } catch (e) { setError(e.message) }
  }

  const addProxies = async () => {
    if (!proxyData.trim() || !selectedPool) return
    try {
      const result = await api.bulkAddProxies({ pool_id: selectedPool.id, data: proxyData })
      setImportResult(result)
      setProxyData('')
      loadProxies(selectedPool.id)
      loadPools()
    } catch (e) { setError(e.message) }
  }

  const deleteProxy = async (id) => {
    try {
      await api.deleteProxy(id)
      loadProxies(selectedPool.id)
      loadPools()
    } catch (e) { setError(e.message) }
  }

  return (
    <div>
      <h2 style={{ margin: '0 0 16px', fontSize: 22 }}>Прокси-пулы</h2>
      {error && <div style={{ color: '#f85149', marginBottom: 12, fontSize: 13 }}>{error}</div>}

      <div style={s.card}>
        <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
          <input style={{ ...s.input, flex: 1, minWidth: 200 }} placeholder="Название пула" value={newPoolName} onChange={e => setNewPoolName(e.target.value)} />
          <select style={s.select} value={newPoolType} onChange={e => setNewPoolType(e.target.value)}>
            <option value="static">Статический</option>
            <option value="mobile">Мобильный</option>
          </select>
          <button style={{ ...s.btn, ...s.btnPrimary }} onClick={createPool}>Создать пул</button>
        </div>
        {newPoolType === 'mobile' && (
          <div style={{ display: 'flex', gap: 8, marginTop: 8, flexWrap: 'wrap' }}>
            <input style={{ ...s.input, width: 200 }} placeholder="Хост прокси" value={mobileHost} onChange={e => setMobileHost(e.target.value)} />
            <input style={{ ...s.input, width: 80 }} placeholder="Порт" value={mobilePort} onChange={e => setMobilePort(e.target.value)} />
            <input style={{ ...s.input, width: 140 }} placeholder="Логин" value={mobileUser} onChange={e => setMobileUser(e.target.value)} />
            <input style={{ ...s.input, width: 140 }} placeholder="Пароль" value={mobilePass} onChange={e => setMobilePass(e.target.value)} />
            <input style={{ ...s.input, flex: 1, minWidth: 200 }} placeholder="URL ротации IP" value={rotationUrl} onChange={e => setRotationUrl(e.target.value)} />
          </div>
        )}
      </div>

      <div style={{ display: 'flex', gap: 16 }}>
        <div style={{ width: 320, flexShrink: 0 }}>
          {pools.map(p => (
            <div
              key={p.id}
              onClick={() => { setSelectedPool(p); setShowAddProxies(false); setImportResult(null) }}
              style={{
                ...s.card, cursor: 'pointer',
                borderColor: selectedPool?.id === p.id ? '#58a6ff' : '#30363d',
                display: 'flex', justifyContent: 'space-between', alignItems: 'center',
              }}
            >
              <div>
                <div style={{ fontWeight: 600, marginBottom: 4 }}>{p.name}</div>
                <div style={{ fontSize: 12, color: '#8b949e' }}>
                  {p.pool_type === 'static'
                    ? `${p.available_count} свободных / ${p.proxy_count} всего`
                    : 'Мобильный'}
                </div>
              </div>
              <button
                style={{ ...s.btn, ...s.btnDanger, padding: '4px 10px' }}
                onClick={(e) => { e.stopPropagation(); deletePool(p.id, p.name) }}
              >✕</button>
            </div>
          ))}
          {pools.length === 0 && (
            <div style={{ color: '#8b949e', fontSize: 13 }}>Нет пулов. Создайте первый.</div>
          )}
        </div>

        <div style={{ flex: 1 }}>
          {selectedPool && selectedPool.pool_type === 'static' && (
            <>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 }}>
                <h3 style={{ margin: 0, fontSize: 16 }}>{selectedPool.name} — прокси</h3>
                <button style={{ ...s.btn, ...s.btnPrimary }} onClick={() => setShowAddProxies(!showAddProxies)}>
                  {showAddProxies ? 'Скрыть' : 'Добавить прокси'}
                </button>
              </div>

              {showAddProxies && (
                <div style={s.card}>
                  <div style={{ marginBottom: 8, fontSize: 13, color: '#8b949e' }}>
                    Формат: <code>login:password@hostname:port</code>, по строке
                  </div>
                  <textarea
                    style={{ ...s.textarea, width: '100%', minHeight: 100 }}
                    placeholder="user1:pass1@gw.proxy.com:823&#10;user2:pass2@gw.proxy.com:823"
                    value={proxyData}
                    onChange={e => setProxyData(e.target.value)}
                  />
                  <div style={{ marginTop: 8, display: 'flex', gap: 8, alignItems: 'center' }}>
                    <button style={{ ...s.btn, ...s.btnPrimary }} onClick={addProxies}>Добавить</button>
                    {importResult && (
                      <span style={{ fontSize: 13, color: '#3fb950' }}>
                        Добавлено: {importResult.imported}
                        {importResult.errors?.length > 0 && <span style={{ color: '#f85149' }}>, ошибок: {importResult.errors.length}</span>}
                      </span>
                    )}
                  </div>
                </div>
              )}

              <table style={s.table}>
                <thead>
                  <tr>
                    <th style={s.th}>Прокси</th>
                    <th style={s.th}>Статус</th>
                    <th style={s.th}>Аккаунт</th>
                    <th style={{ ...s.th, width: 60 }}></th>
                  </tr>
                </thead>
                <tbody>
                  {proxies.map(p => (
                    <tr key={p.id}>
                      <td style={{ ...s.td, fontFamily: 'monospace', fontSize: 12 }}>
                        {p.host}:{p.port}{p.username ? `:${p.username}:***` : ''}
                      </td>
                      <td style={s.td}>
                        <span style={s.badge(STATUS_COLORS[p.status])}>
                          {STATUS_LABELS[p.status]}
                        </span>
                      </td>
                      <td style={{ ...s.td, color: '#8b949e' }}>{p.bound_account || '—'}</td>
                      <td style={s.td}>
                        <button style={{ ...s.btn, ...s.btnDanger, padding: '4px 10px' }} onClick={() => deleteProxy(p.id)}>✕</button>
                      </td>
                    </tr>
                  ))}
                  {proxies.length === 0 && (
                    <tr><td style={{ ...s.td, color: '#8b949e' }} colSpan={4}>Пул пуст. Добавьте прокси.</td></tr>
                  )}
                </tbody>
              </table>
            </>
          )}

          {selectedPool && selectedPool.pool_type === 'mobile' && (
            <div style={s.card}>
              <h3 style={{ margin: '0 0 12px', fontSize: 16 }}>{selectedPool.name} — мобильный прокси</h3>
              <div style={{ fontSize: 13, color: '#8b949e', lineHeight: 1.8 }}>
                <div>Хост: <span style={{ color: '#e6edf3' }}>{selectedPool.proxy_host || '—'}:{selectedPool.proxy_port || '—'}</span></div>
                <div>Логин: <span style={{ color: '#e6edf3' }}>{selectedPool.proxy_username || '—'}</span></div>
                <div>URL ротации: <span style={{ color: '#e6edf3', wordBreak: 'break-all' }}>{selectedPool.rotation_url || '—'}</span></div>
              </div>
            </div>
          )}

          {!selectedPool && (
            <div style={{ color: '#8b949e', fontSize: 13 }}>Выберите пул слева.</div>
          )}
        </div>
      </div>
    </div>
  )
}
