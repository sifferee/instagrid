import React, { useState, useEffect, useRef } from 'react'
import { api } from '../api/client'

const s = {
  card: { background: '#161b22', border: '1px solid #30363d', borderRadius: 8, padding: 16, marginBottom: 16 },
  btn: { padding: '8px 16px', borderRadius: 6, border: 'none', cursor: 'pointer', fontSize: 13, fontWeight: 600 },
  btnPrimary: { background: '#238636', color: '#fff' },
  btnSecondary: { background: '#30363d', color: '#e6edf3' },
  btnDanger: { background: '#da3633', color: '#fff' },
  input: { background: '#0d1117', border: '1px solid #30363d', borderRadius: 6, padding: '8px 12px', color: '#e6edf3', width: '100%', fontSize: 13 },
  textarea: { background: '#0d1117', border: '1px solid #30363d', borderRadius: 6, padding: '8px 12px', color: '#e6edf3', width: '100%', fontSize: 13, minHeight: 120, resize: 'vertical', fontFamily: 'inherit' },
  badge: (color) => ({ display: 'inline-block', padding: '4px 12px', borderRadius: 12, fontSize: 12, fontWeight: 600, background: color, color: '#fff', marginRight: 8 }),
  table: { width: '100%', borderCollapse: 'collapse', fontSize: 13 },
  th: { padding: '8px 12px', textAlign: 'left', borderBottom: '1px solid #30363d', color: '#8b949e', fontWeight: 600 },
  td: { padding: '8px 12px', borderBottom: '1px solid #21262d' },
  h2: { fontSize: 18, fontWeight: 600, marginBottom: 16 },
  flex: { display: 'flex', gap: 12, alignItems: 'center', flexWrap: 'wrap' },
}

export default function ContentPage() {
  const [stats, setStats] = useState(null)
  const [videos, setVideos] = useState([])
  const [descriptions, setDescriptions] = useState([])
  const [tab, setTab] = useState('videos')
  const [descText, setDescText] = useState('')
  const [genRef, setGenRef] = useState('')
  const [genCount, setGenCount] = useState(50)
  const [genKey, setGenKey] = useState('')
  const [msg, setMsg] = useState('')
  const fileRef = useRef()

  const load = async () => {
    try {
      setStats(await api.getContentStats())
      setVideos(await api.getVideos())
      setDescriptions(await api.getDescriptions())
    } catch (e) { setMsg('Ошибка: ' + e.message) }
  }
  useEffect(() => { load() }, [])

  const uploadZip = async () => {
    const file = fileRef.current?.files[0]
    if (!file) return
    setMsg('Загружаю...')
    try {
      const r = await api.uploadVideoZip(file)
      setMsg(`Добавлено: ${r.added}, дублей: ${r.duplicates}, пропущено: ${r.skipped}`)
      fileRef.current.value = ''
      load()
    } catch (e) { setMsg('Ошибка: ' + e.message) }
  }

  const distribute = async () => {
    setMsg('Распределяю...')
    try {
      const r = await api.distributeAll()
      setMsg(`Видео: ${r.videos.assigned} назначено. Описания: ${r.descriptions.assigned} назначено.`)
      load()
    } catch (e) { setMsg('Ошибка: ' + e.message) }
  }

  const importDescs = async () => {
    if (!descText.trim()) return
    const lines = descText.trim().split('\n').filter(l => l.trim())
    try {
      const r = await api.importDescriptions(lines)
      setMsg(`Добавлено описаний: ${r.added}`)
      setDescText('')
      load()
    } catch (e) { setMsg('Ошибка: ' + e.message) }
  }

  const generateDescs = async () => {
    if (!genRef.trim()) return
    setMsg('Генерирую описания через Claude...')
    try {
      const r = await api.generateDescriptions({ reference_text: genRef, count: genCount, api_key: genKey || undefined })
      setMsg(`Сгенерировано: ${r.generated}`)
      setGenRef('')
      load()
    } catch (e) { setMsg('Ошибка: ' + e.message) }
  }

  const vs = stats?.videos || {}
  const ds = stats?.descriptions || {}

  return (
    <div>
      <h1 style={{ fontSize: 22, fontWeight: 700, marginBottom: 20 }}>Контент</h1>

      {msg && <div style={{ ...s.card, borderColor: '#58a6ff' }}>{msg}</div>}

      {/* Статистика / Плашки */}
      {stats && (
        <div style={{ ...s.card, ...s.flex }}>
          <span style={s.badge(vs.badge === 'spare_videos' ? '#238636' : vs.badge === 'unfilled_accounts' ? '#da3633' : '#30363d')}>
            {vs.badge === 'spare_videos' ? '✓ Есть резервные видео' : vs.badge === 'unfilled_accounts' ? '⚠ Не все аккаунты заполнены' : '● Сбалансировано'}
          </span>
          <span style={{ color: '#8b949e', fontSize: 13 }}>
            Видео: {vs.assigned || 0} назначено / {vs.unassigned || 0} свободно / {vs.total_videos || 0} всего
          </span>
          <span style={{ color: '#8b949e', fontSize: 13 }}>
            Описания: {ds.assigned || 0} назначено / {ds.unassigned || 0} свободно / {ds.total || 0} всего
          </span>
          <button onClick={distribute} style={{ ...s.btn, ...s.btnPrimary }}>Распределить всё</button>
        </div>
      )}

      {/* Табы */}
      <div style={{ ...s.flex, marginBottom: 16 }}>
        <button onClick={() => setTab('videos')} style={{ ...s.btn, ...(tab === 'videos' ? s.btnPrimary : s.btnSecondary) }}>Видео</button>
        <button onClick={() => setTab('descriptions')} style={{ ...s.btn, ...(tab === 'descriptions' ? s.btnPrimary : s.btnSecondary) }}>Описания</button>
        <button onClick={() => setTab('generate')} style={{ ...s.btn, ...(tab === 'generate' ? s.btnPrimary : s.btnSecondary) }}>Claude генерация</button>
      </div>

      {/* Видео */}
      {tab === 'videos' && (
        <div>
          <div style={{ ...s.card, ...s.flex }}>
            <input ref={fileRef} type="file" accept=".zip" style={s.input} />
            <button onClick={uploadZip} style={{ ...s.btn, ...s.btnPrimary, whiteSpace: 'nowrap' }}>Загрузить ZIP</button>
          </div>
          <table style={s.table}>
            <thead><tr>
              <th style={s.th}>ID</th><th style={s.th}>Файл</th><th style={s.th}>Статус</th>
              <th style={s.th}>Аккаунт</th><th style={s.th}>Размер</th><th style={s.th}></th>
            </tr></thead>
            <tbody>
              {videos.map(v => (
                <tr key={v.id}>
                  <td style={s.td}>{v.id}</td>
                  <td style={s.td}>{v.filename}</td>
                  <td style={s.td}><span style={s.badge(v.status === 'assigned' ? '#238636' : v.status === 'posted' ? '#58a6ff' : '#30363d')}>{v.status}</span></td>
                  <td style={s.td}>{v.account_id || '—'}</td>
                  <td style={s.td}>{(v.file_size / 1024 / 1024).toFixed(1)} MB</td>
                  <td style={s.td}><button onClick={async () => { await api.deleteVideo(v.id); load() }} style={{ ...s.btn, ...s.btnDanger, padding: '4px 10px' }}>✕</button></td>
                </tr>
              ))}
              {videos.length === 0 && <tr><td colSpan={6} style={{ ...s.td, color: '#8b949e' }}>Нет видео</td></tr>}
            </tbody>
          </table>
        </div>
      )}

      {/* Описания — ручной пул */}
      {tab === 'descriptions' && (
        <div>
          <div style={s.card}>
            <div style={s.h2}>Импорт пула описаний</div>
            <textarea style={s.textarea} value={descText} onChange={e => setDescText(e.target.value)} placeholder="Каждое описание на отдельной строке..." />
            <div style={{ marginTop: 8 }}>
              <button onClick={importDescs} style={{ ...s.btn, ...s.btnPrimary }}>Импортировать</button>
            </div>
          </div>
          <table style={s.table}>
            <thead><tr>
              <th style={s.th}>ID</th><th style={s.th}>Текст</th><th style={s.th}>Источник</th>
              <th style={s.th}>Статус</th><th style={s.th}>Аккаунт</th><th style={s.th}></th>
            </tr></thead>
            <tbody>
              {descriptions.map(d => (
                <tr key={d.id}>
                  <td style={s.td}>{d.id}</td>
                  <td style={{ ...s.td, maxWidth: 400, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{d.text}</td>
                  <td style={s.td}>{d.source}</td>
                  <td style={s.td}><span style={s.badge(d.status === 'assigned' ? '#238636' : '#30363d')}>{d.status}</span></td>
                  <td style={s.td}>{d.account_id || '—'}</td>
                  <td style={s.td}><button onClick={async () => { await api.deleteDescription(d.id); load() }} style={{ ...s.btn, ...s.btnDanger, padding: '4px 10px' }}>✕</button></td>
                </tr>
              ))}
              {descriptions.length === 0 && <tr><td colSpan={6} style={{ ...s.td, color: '#8b949e' }}>Нет описаний</td></tr>}
            </tbody>
          </table>
        </div>
      )}

      {/* Claude генерация */}
      {tab === 'generate' && (
        <div style={s.card}>
          <div style={s.h2}>Автогенерация через Claude API</div>
          <div style={{ marginBottom: 12 }}>
            <label style={{ color: '#8b949e', fontSize: 12 }}>API ключ (опционально, если не задан в config.env)</label>
            <input style={s.input} type="password" value={genKey} onChange={e => setGenKey(e.target.value)} placeholder="sk-ant-..." />
          </div>
          <div style={{ marginBottom: 12 }}>
            <label style={{ color: '#8b949e', fontSize: 12 }}>Эталонное описание</label>
            <textarea style={s.textarea} value={genRef} onChange={e => setGenRef(e.target.value)} placeholder="🔥 Check this out! Amazing content... #viral #trending" />
          </div>
          <div style={{ ...s.flex, marginBottom: 12 }}>
            <label style={{ color: '#8b949e', fontSize: 12 }}>Количество:</label>
            <input style={{ ...s.input, width: 80 }} type="number" value={genCount} onChange={e => setGenCount(Number(e.target.value))} min={1} max={200} />
            <button onClick={generateDescs} style={{ ...s.btn, ...s.btnPrimary }}>Сгенерировать</button>
          </div>
        </div>
      )}
    </div>
  )
}
