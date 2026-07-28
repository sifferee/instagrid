import React, { useState, useEffect, useCallback } from 'react'

const req = async (path, opts = {}) => {
  const res = await fetch(path, opts)
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }))
    throw new Error(err.detail || res.statusText)
  }
  return res.json()
}

function PhotoGrid({ photos, selected, onToggle }) {
  return (
    <div style={{
      display: 'grid', gridTemplateColumns: 'repeat(6, 1fr)', gap: 6,
      maxHeight: 240, overflowY: 'auto', padding: 4,
    }}>
      {photos.map(p => {
        const isSel = selected.includes(p.id)
        return (
          <div key={p.id} onClick={() => onToggle(p.id)}
            style={{
              position: 'relative', cursor: 'pointer', borderRadius: 6, overflow: 'hidden',
              border: isSel ? '2px solid #238636' : '2px solid transparent',
              opacity: isSel ? 1 : 0.65, transition: 'all 0.12s',
            }}>
            <img src={`/api/stories/photos/${p.id}/preview`} alt={p.filename}
              style={{ width: '100%', aspectRatio: '9/16', objectFit: 'cover', display: 'block' }}
              loading="lazy" />
            {isSel && (
              <div style={{
                position: 'absolute', top: 3, right: 3,
                background: '#238636', borderRadius: '50%', width: 18, height: 18,
                display: 'flex', alignItems: 'center', justifyContent: 'center',
                color: '#fff', fontSize: 11, fontWeight: 700,
              }}>✓</div>
            )}
          </div>
        )
      })}
    </div>
  )
}

export default function StoriesPage() {
  const [stats, setStats] = useState(null)
  const [photos, setPhotos] = useState([])
  const [templates, setTemplates] = useState([])
  const [niches, setNiches] = useState([])
  const [uploading, setUploading] = useState(false)
  const [uploadResult, setUploadResult] = useState(null)
  const [uploadNiche, setUploadNiche] = useState('')

  const [showForm, setShowForm] = useState(false)
  const [form, setForm] = useState({ name: '', photo_ids: [], link_url: '', cta_text: 'Learn More', niche_ids: [] })
  const [allNiches, setAllNiches] = useState(false)
  const [photoMode, setPhotoMode] = useState('pool') // 'pool' | 'zip'
  const [zipUploading, setZipUploading] = useState(false)
  const [zipResult, setZipResult] = useState(null)

  const load = useCallback(async () => {
    try {
      const [s, p, t, n] = await Promise.all([
        req('/api/stories/stats'), req('/api/stories/photos'),
        req('/api/stories/templates'), req('/api/niches'),
      ])
      setStats(s); setPhotos(p); setTemplates(t); setNiches(n)
    } catch (e) { console.error(e) }
  }, [])

  useEffect(() => { load() }, [load])

  const handleMainUpload = async (e) => {
    const file = e.target.files?.[0]
    if (!file) return
    setUploading(true); setUploadResult(null)
    const fd = new FormData()
    fd.append('file', file)
    if (uploadNiche) fd.append('niche_id', uploadNiche)
    try {
      const r = await fetch('/api/stories/photos/upload', { method: 'POST', body: fd }).then(r => r.json())
      setUploadResult(r); load()
    } catch (e) { setUploadResult({ error: e.message }) }
    finally { setUploading(false) }
  }

  // ZIP upload для конкретного шаблона — фотки идут в пул и авто-выбираются
  const handleTemplateZip = async (e) => {
    const file = e.target.files?.[0]
    if (!file) return
    setZipUploading(true); setZipResult(null)
    const fd = new FormData()
    fd.append('file', file)
    try {
      const r = await fetch('/api/stories/photos/upload', { method: 'POST', body: fd }).then(r => r.json())
      // Перезагружаем фото и авто-выбираем загруженные (последние N)
      const allPhotos = await req('/api/stories/photos')
      setPhotos(allPhotos)
      // Авто-выбираем загруженные (последние r.added штук)
      if (r.added > 0) {
        const newIds = allPhotos.slice(0, r.added).map(p => p.id)
        setForm(f => ({ ...f, photo_ids: [...new Set([...f.photo_ids, ...newIds])] }))
      }
      setZipResult(r)
    } catch (e) { setZipResult({ error: e.message }) }
    finally { setZipUploading(false) }
  }

  const togglePhoto = (id) => {
    setForm(f => ({
      ...f,
      photo_ids: f.photo_ids.includes(id)
        ? f.photo_ids.filter(x => x !== id)
        : [...f.photo_ids, id],
    }))
  }

  const selectAll = () => setForm(f => ({ ...f, photo_ids: photos.map(p => p.id) }))
  const selectNone = () => setForm(f => ({ ...f, photo_ids: [] }))

  const createTemplate = async () => {
    if (!form.name || !form.photo_ids.length || !form.link_url) return
    try {
      await req('/api/stories/templates', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name: form.name, photo_ids: form.photo_ids,
          link_url: form.link_url, cta_text: form.cta_text || 'Learn More',
          niche_ids: allNiches ? null : form.niche_ids,
        }),
      })
      setForm({ name: '', photo_ids: [], link_url: '', cta_text: 'Learn More', niche_ids: [] })
      setAllNiches(false); setShowForm(false); setPhotoMode('pool'); setZipResult(null); load()
    } catch (e) { alert(e.message) }
  }

  const toggleTemplate = async (id, active) => {
    await req(`/api/stories/templates/${id}`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ is_active: !active }),
    }); load()
  }

  const deleteTemplate = async (id) => {
    if (!confirm('Удалить шаблон?')) return
    await req(`/api/stories/templates/${id}`, { method: 'DELETE' }); load()
  }

  const card = { background: '#161b22', border: '1px solid #30363d', borderRadius: 8, padding: 20, marginBottom: 16 }
  const btn = { background: '#238636', color: '#fff', border: 'none', borderRadius: 6, padding: '8px 16px', cursor: 'pointer', fontSize: 14, fontWeight: 500 }
  const btnSm = { ...btn, padding: '5px 12px', fontSize: 12 }
  const btnGhost = { ...btnSm, background: 'transparent', border: '1px solid #30363d', color: '#8b949e' }
  const input = { background: '#0d1117', color: '#e6edf3', border: '1px solid #30363d', borderRadius: 6, padding: '8px 12px', fontSize: 14, width: '100%', boxSizing: 'border-box' }

  return (
    <div>
      <h1 style={{ fontSize: 24, fontWeight: 700, marginBottom: 24, color: '#e6edf3' }}>Сторис</h1>

      {stats && (
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(130px, 1fr))', gap: 12, marginBottom: 24 }}>
          {[
            { l: 'Всего', v: stats.total, c: '#e6edf3' },
            { l: 'Опубликовано', v: stats.posted, c: '#3fb950' },
            { l: 'Ошибки', v: stats.failed, c: stats.failed > 0 ? '#f85149' : '#e6edf3' },
            { l: 'Фото в пуле', v: photos.length, c: '#e6edf3' },
            { l: 'Шаблонов', v: templates.length, c: '#58a6ff' },
          ].map(s => (
            <div key={s.l} style={card}>
              <div style={{ color: '#8b949e', fontSize: 12 }}>{s.l}</div>
              <div style={{ color: s.c, fontSize: 22, fontWeight: 700 }}>{s.v}</div>
            </div>
          ))}
        </div>
      )}

      {/* Загрузка основного пула */}
      <div style={card}>
        <h2 style={{ fontSize: 16, fontWeight: 600, marginBottom: 12, color: '#e6edf3' }}>Основной пул фото (ZIP)</h2>
        <div style={{ display: 'flex', gap: 12, alignItems: 'center', flexWrap: 'wrap' }}>
          <select value={uploadNiche} onChange={e => setUploadNiche(e.target.value)} style={{ ...input, width: 'auto' }}>
            <option value="">Все ниши</option>
            {niches.map(n => <option key={n.id} value={n.id}>{n.name}</option>)}
          </select>
          <label style={{ ...btn, opacity: uploading ? 0.6 : 1 }}>
            {uploading ? 'Загрузка...' : 'Выбрать ZIP'}
            <input type="file" accept=".zip" onChange={handleMainUpload} disabled={uploading} style={{ display: 'none' }} />
          </label>
        </div>
        {uploadResult && (
          <div style={{ marginTop: 10, padding: '6px 12px', borderRadius: 6, fontSize: 13,
            background: uploadResult.error ? '#3b1219' : '#0d2818', color: uploadResult.error ? '#f85149' : '#3fb950' }}>
            {uploadResult.error ? `Ошибка: ${uploadResult.error}` : `Добавлено: ${uploadResult.added}, дубликатов: ${uploadResult.duplicates}`}
          </div>
        )}
      </div>

      {/* Шаблоны */}
      <div style={card}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 }}>
          <h2 style={{ fontSize: 16, fontWeight: 600, color: '#e6edf3', margin: 0 }}>Шаблоны сторис</h2>
          <button onClick={() => { setShowForm(!showForm); setPhotoMode('pool'); setZipResult(null) }} style={btn}>
            {showForm ? 'Отмена' : '+ Создать шаблон'}
          </button>
        </div>

        {showForm && (
          <div style={{ background: '#0d1117', borderRadius: 8, padding: 16, marginBottom: 16, border: '1px solid #30363d' }}>
            <div style={{ display: 'grid', gap: 12 }}>
              <div>
                <label style={{ color: '#8b949e', fontSize: 12, display: 'block', marginBottom: 4 }}>Название</label>
                <input style={input} value={form.name} onChange={e => setForm({ ...form, name: e.target.value })} placeholder="Оффер 1" />
              </div>

              {/* Фото: выбор режима */}
              <div>
                <label style={{ color: '#8b949e', fontSize: 12, display: 'block', marginBottom: 8 }}>
                  Фото ({form.photo_ids.length} выбрано)
                </label>
                <div style={{ display: 'flex', gap: 6, marginBottom: 10 }}>
                  <button onClick={() => setPhotoMode('pool')}
                    style={{ ...btnSm, background: photoMode === 'pool' ? '#1f6feb' : '#21262d' }}>
                    Из основного пула
                  </button>
                  <button onClick={() => setPhotoMode('zip')}
                    style={{ ...btnSm, background: photoMode === 'zip' ? '#1f6feb' : '#21262d' }}>
                    Загрузить отдельный ZIP
                  </button>
                  {photos.length > 0 && photoMode === 'pool' && (
                    <>
                      <button onClick={selectAll} style={btnGhost}>Все</button>
                      <button onClick={selectNone} style={btnGhost}>Сбросить</button>
                    </>
                  )}
                </div>

                {photoMode === 'pool' && photos.length > 0 && (
                  <PhotoGrid photos={photos} selected={form.photo_ids} onToggle={togglePhoto} />
                )}
                {photoMode === 'pool' && photos.length === 0 && (
                  <div style={{ color: '#8b949e', fontSize: 13 }}>Сначала загрузи фото в основной пул</div>
                )}
                {photoMode === 'zip' && (
                  <div>
                    <label style={{ ...btnSm, background: '#1f6feb', cursor: zipUploading ? 'wait' : 'pointer', display: 'inline-block' }}>
                      {zipUploading ? 'Загрузка...' : 'Загрузить ZIP для этого шаблона'}
                      <input type="file" accept=".zip" onChange={handleTemplateZip} disabled={zipUploading} style={{ display: 'none' }} />
                    </label>
                    {zipResult && (
                      <div style={{ marginTop: 8, fontSize: 13, color: zipResult.error ? '#f85149' : '#3fb950' }}>
                        {zipResult.error ? `Ошибка: ${zipResult.error}` : `Загружено ${zipResult.added} фото → автовыбрано`}
                      </div>
                    )}
                    {form.photo_ids.length > 0 && (
                      <div style={{ marginTop: 8 }}>
                        <PhotoGrid photos={photos.filter(p => form.photo_ids.includes(p.id))} selected={form.photo_ids} onToggle={togglePhoto} />
                      </div>
                    )}
                  </div>
                )}
              </div>

              <div>
                <label style={{ color: '#8b949e', fontSize: 12, display: 'block', marginBottom: 4 }}>Ссылка</label>
                <input style={input} value={form.link_url} onChange={e => setForm({ ...form, link_url: e.target.value })}
                  placeholder="https://your-site.com/offer" />
              </div>
              <div>
                <label style={{ color: '#8b949e', fontSize: 12, display: 'block', marginBottom: 4 }}>Текст кнопки</label>
                <input style={input} value={form.cta_text} onChange={e => setForm({ ...form, cta_text: e.target.value })} />
              </div>
              <div>
                <label style={{ color: '#8b949e', fontSize: 12, display: 'block', marginBottom: 4 }}>Ниши</label>
                <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8 }}>
                  <input type="checkbox" checked={allNiches}
                    onChange={e => { setAllNiches(e.target.checked); if (e.target.checked) setForm({ ...form, niche_ids: [] }) }}
                    id="all-niches" />
                  <label htmlFor="all-niches" style={{ color: '#e6edf3', fontSize: 13 }}>Все ниши</label>
                </div>
                {!allNiches && (
                  <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                    {niches.map(n => {
                      const sel = form.niche_ids.includes(n.id)
                      return (
                        <div key={n.id} onClick={() => setForm(f => ({
                          ...f, niche_ids: sel ? f.niche_ids.filter(i => i !== n.id) : [...f.niche_ids, n.id],
                        }))}
                          style={{
                            background: sel ? '#1f6feb33' : '#21262d',
                            border: `1px solid ${sel ? '#1f6feb' : '#30363d'}`,
                            borderRadius: 6, padding: '4px 10px', cursor: 'pointer', fontSize: 13, color: '#e6edf3',
                          }}>{n.name}</div>
                      )
                    })}
                  </div>
                )}
              </div>
              <button onClick={createTemplate}
                style={{ ...btn, width: '100%', opacity: (!form.name || !form.photo_ids.length || !form.link_url) ? 0.4 : 1 }}
                disabled={!form.name || !form.photo_ids.length || !form.link_url}>
                Создать шаблон
              </button>
            </div>
          </div>
        )}

        {templates.length === 0 && !showForm && (
          <div style={{ color: '#8b949e', fontSize: 14 }}>Нет шаблонов. Создайте первый.</div>
        )}
        {templates.map(t => (
          <div key={t.id} style={{ padding: '12px 0', borderBottom: '1px solid #21262d', opacity: t.is_active ? 1 : 0.45 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
              <div style={{ display: 'flex', gap: 3, flexShrink: 0 }}>
                {(t.photos || []).slice(0, 5).map(p => (
                  <img key={p.id} src={`/api/stories/photos/${p.id}/preview`}
                    style={{ width: 28, height: 50, objectFit: 'cover', borderRadius: 3 }} />
                ))}
                {(t.photos || []).length > 5 && (
                  <div style={{ width: 28, height: 50, borderRadius: 3, background: '#21262d',
                    display: 'flex', alignItems: 'center', justifyContent: 'center', color: '#8b949e', fontSize: 10 }}>
                    +{t.photos.length - 5}
                  </div>
                )}
              </div>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ color: '#e6edf3', fontWeight: 600, fontSize: 14 }}>{t.name}</div>
                <div style={{ color: '#58a6ff', fontSize: 12, marginTop: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{t.link_url}</div>
                <div style={{ color: '#8b949e', fontSize: 11, marginTop: 1 }}>
                  {t.photos?.length || 0} фото · {t.cta_text}
                  {t.niches?.length > 0 ? ` · ${t.niches.map(n => n.name).join(', ')}` : ' · Все ниши'}
                </div>
              </div>
              <button onClick={() => toggleTemplate(t.id, t.is_active)}
                style={{ ...btnSm, background: t.is_active ? '#21262d' : '#238636' }}>
                {t.is_active ? 'Выкл' : 'Вкл'}
              </button>
              <button onClick={() => deleteTemplate(t.id)} style={{ ...btnSm, background: '#da3633' }}>✕</button>
            </div>
          </div>
        ))}
      </div>

      <div style={{ ...card, borderColor: '#1f6feb33' }}>
        <h2 style={{ fontSize: 16, fontWeight: 600, marginBottom: 8, color: '#58a6ff' }}>Автотриггер</h2>
        <p style={{ color: '#8b949e', fontSize: 13, lineHeight: 1.6, margin: 0 }}>
          Когда рилс набирает 10 000+ просмотров — сторис каждые 24ч (±20-30 мин).
          Случайный шаблон для ниши → случайное фото из шаблона. Пока аккаунт жив.
        </p>
      </div>
    </div>
  )
}
