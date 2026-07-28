import React, { useState, useEffect, useCallback } from 'react'
import { api } from '../api/client'

export default function StoriesPage() {
  const [stats, setStats] = useState(null)
  const [uploading, setUploading] = useState(false)
  const [uploadResult, setUploadResult] = useState(null)
  const [niches, setNiches] = useState([])
  const [selectedNiche, setSelectedNiche] = useState('')

  const loadStats = useCallback(async () => {
    try {
      const data = await api.get('/api/stories/stats')
      setStats(data)
    } catch (e) {
      console.error('Failed to load stats:', e)
    }
  }, [])

  const loadNiches = useCallback(async () => {
    try {
      const data = await api.get('/api/niches')
      setNiches(data)
    } catch (e) {
      console.error('Failed to load niches:', e)
    }
  }, [])

  useEffect(() => {
    loadStats()
    loadNiches()
  }, [loadStats, loadNiches])

  const handleUpload = async (e) => {
    const file = e.target.files?.[0]
    if (!file) return

    setUploading(true)
    setUploadResult(null)

    const formData = new FormData()
    formData.append('file', file)
    if (selectedNiche) formData.append('niche_id', selectedNiche)

    try {
      const result = await api.postForm('/api/stories/photos/upload', formData)
      setUploadResult(result)
      loadStats()
    } catch (e) {
      setUploadResult({ error: e.message })
    } finally {
      setUploading(false)
    }
  }

  const cardStyle = {
    background: '#161b22', border: '1px solid #30363d', borderRadius: 8,
    padding: 20, marginBottom: 16,
  }
  const labelStyle = { color: '#8b949e', fontSize: 12, marginBottom: 4 }
  const valueStyle = { color: '#e6edf3', fontSize: 24, fontWeight: 700 }

  return (
    <div>
      <h1 style={{ fontSize: 24, fontWeight: 700, marginBottom: 24, color: '#e6edf3' }}>
        Сторис
      </h1>

      {/* Статистика */}
      {stats && (
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(150px, 1fr))', gap: 12, marginBottom: 24 }}>
          <div style={cardStyle}>
            <div style={labelStyle}>Всего</div>
            <div style={valueStyle}>{stats.total}</div>
          </div>
          <div style={cardStyle}>
            <div style={labelStyle}>Опубликовано</div>
            <div style={{ ...valueStyle, color: '#3fb950' }}>{stats.posted}</div>
          </div>
          <div style={cardStyle}>
            <div style={labelStyle}>Ошибки</div>
            <div style={{ ...valueStyle, color: stats.failed > 0 ? '#f85149' : '#e6edf3' }}>{stats.failed}</div>
          </div>
          <div style={cardStyle}>
            <div style={labelStyle}>Последняя</div>
            <div style={{ color: '#e6edf3', fontSize: 14 }}>
              {stats.last_posted_at
                ? new Date(stats.last_posted_at * 1000).toLocaleString('ru-RU')
                : '—'}
            </div>
          </div>
        </div>
      )}

      {/* Загрузка фото */}
      <div style={cardStyle}>
        <h2 style={{ fontSize: 16, fontWeight: 600, marginBottom: 16, color: '#e6edf3' }}>
          Загрузить фото для сторис (ZIP)
        </h2>
        <div style={{ display: 'flex', gap: 12, alignItems: 'center', flexWrap: 'wrap' }}>
          <select
            value={selectedNiche}
            onChange={e => setSelectedNiche(e.target.value)}
            style={{
              background: '#0d1117', color: '#e6edf3', border: '1px solid #30363d',
              borderRadius: 6, padding: '8px 12px', fontSize: 14,
            }}
          >
            <option value="">Все ниши</option>
            {niches.map(n => <option key={n.id} value={n.id}>{n.name}</option>)}
          </select>

          <label style={{
            background: '#238636', color: '#fff', padding: '8px 16px', borderRadius: 6,
            cursor: uploading ? 'wait' : 'pointer', fontSize: 14, fontWeight: 500,
            opacity: uploading ? 0.6 : 1,
          }}>
            {uploading ? 'Загрузка...' : 'Выбрать ZIP'}
            <input
              type="file"
              accept=".zip"
              onChange={handleUpload}
              disabled={uploading}
              style={{ display: 'none' }}
            />
          </label>
        </div>

        {uploadResult && (
          <div style={{
            marginTop: 12, padding: '8px 12px', borderRadius: 6, fontSize: 13,
            background: uploadResult.error ? '#3b1219' : '#0d2818',
            color: uploadResult.error ? '#f85149' : '#3fb950',
            border: `1px solid ${uploadResult.error ? '#f8514933' : '#3fb95033'}`,
          }}>
            {uploadResult.error
              ? `Ошибка: ${uploadResult.error}`
              : `Добавлено: ${uploadResult.added}, дубликатов: ${uploadResult.duplicates}`}
          </div>
        )}
      </div>

      {/* Инфо об автотриггере */}
      <div style={{ ...cardStyle, borderColor: '#1f6feb33' }}>
        <h2 style={{ fontSize: 16, fontWeight: 600, marginBottom: 8, color: '#58a6ff' }}>
          Автотриггер
        </h2>
        <p style={{ color: '#8b949e', fontSize: 13, lineHeight: 1.6, margin: 0 }}>
          Чекер проверяет рилсы каждые 1-2 часа. Когда рилс набирает 10 000+ просмотров,
          система автоматически постит сторис с кликабельной ссылкой.
          Порог рандомизирован (10 000 – 12 777), максимум 1 сторис в 24 часа на аккаунт,
          задержка ±10-20 мин для естественности.
        </p>
      </div>
    </div>
  )
}
