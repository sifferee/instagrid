const BASE = '/api'

async function request(path, options = {}) {
  const url = BASE + path
  const res = await fetch(url, {
    headers: { 'Content-Type': 'application/json', ...options.headers },
    ...options,
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }))
    throw new Error(err.detail || res.statusText)
  }
  return res.json()
}

async function uploadFile(path, file, extraFields = {}) {
  const form = new FormData()
  form.append('file', file)
  for (const [k, v] of Object.entries(extraFields)) {
    if (v != null) form.append(k, v)
  }
  const res = await fetch(BASE + path, { method: 'POST', body: form })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }))
    throw new Error(err.detail || res.statusText)
  }
  return res.json()
}

export const api = {
  // Ниши
  getNiches: () => request('/niches'),
  createNiche: (name) => request('/niches', { method: 'POST', body: JSON.stringify({ name }) }),
  renameNiche: (id, name) => request(`/niches/${id}`, { method: 'PUT', body: JSON.stringify({ name }) }),
  deleteNiche: (id) => request(`/niches/${id}`, { method: 'DELETE' }),

  // Аккаунты
  getAccounts: (params = {}) => {
    const q = new URLSearchParams()
    if (params.niche_id != null) q.set('niche_id', params.niche_id)
    if (params.status) q.set('status', params.status)
    const qs = q.toString()
    return request('/accounts' + (qs ? '?' + qs : ''))
  },
  createAccount: (data) => request('/accounts', { method: 'POST', body: JSON.stringify(data) }),
  updateAccount: (id, data) => request(`/accounts/${id}`, { method: 'PUT', body: JSON.stringify(data) }),
  deleteAccount: (id) => request(`/accounts/${id}`, { method: 'DELETE' }),
  bulkImport: (data) => request('/accounts/bulk-import', { method: 'POST', body: JSON.stringify(data) }),
  moveAccounts: (account_ids, niche_id) => request('/accounts/move', { method: 'POST', body: JSON.stringify({ account_ids, niche_id }) }),

  // Прокси
  getPools: () => request('/proxies/pools'),
  createPool: (data) => request('/proxies/pools', { method: 'POST', body: JSON.stringify(data) }),
  updatePool: (id, data) => request(`/proxies/pools/${id}`, { method: 'PUT', body: JSON.stringify(data) }),
  deletePool: (id) => request(`/proxies/pools/${id}`, { method: 'DELETE' }),
  getProxies: (poolId) => request(`/proxies/pools/${poolId}/proxies`),
  bulkAddProxies: (data) => request('/proxies/bulk-add', { method: 'POST', body: JSON.stringify(data) }),
  deleteProxy: (id) => request(`/proxies/proxy/${id}`, { method: 'DELETE' }),

  // Контент — видео
  uploadVideoZip: (file, niche_id) => uploadFile('/content/videos/upload', file, { niche_id }),
  getVideos: (params = {}) => {
    const q = new URLSearchParams()
    if (params.niche_id != null) q.set('niche_id', params.niche_id)
    if (params.status) q.set('status', params.status)
    return request('/content/videos' + (q.toString() ? '?' + q : ''))
  },
  distributeVideos: (niche_id) => request('/content/videos/distribute' + (niche_id ? '?niche_id=' + niche_id : ''), { method: 'POST' }),
  assignVideo: (video_id, account_id) => request('/content/videos/assign', { method: 'POST', body: JSON.stringify({ video_id, account_id }) }),
  deleteVideo: (id) => request(`/content/videos/${id}`, { method: 'DELETE' }),
  getVideoStats: (niche_id) => request('/content/videos/stats' + (niche_id ? '?niche_id=' + niche_id : '')),

  // Контент — описания
  importDescriptions: (descriptions, niche_id) => request('/content/descriptions/import', { method: 'POST', body: JSON.stringify({ descriptions, niche_id }) }),
  generateDescriptions: (data) => request('/content/descriptions/generate', { method: 'POST', body: JSON.stringify(data) }),
  getDescriptions: (params = {}) => {
    const q = new URLSearchParams()
    if (params.niche_id != null) q.set('niche_id', params.niche_id)
    if (params.status) q.set('status', params.status)
    if (params.source) q.set('source', params.source)
    return request('/content/descriptions' + (q.toString() ? '?' + q : ''))
  },
  distributeDescriptions: (niche_id) => request('/content/descriptions/distribute' + (niche_id ? '?niche_id=' + niche_id : ''), { method: 'POST' }),
  updateDescription: (id, text) => request(`/content/descriptions/${id}`, { method: 'PUT', body: JSON.stringify({ text }) }),
  deleteDescription: (id) => request(`/content/descriptions/${id}`, { method: 'DELETE' }),
  distributeAll: (niche_id) => request('/content/distribute-all' + (niche_id ? '?niche_id=' + niche_id : ''), { method: 'POST' }),
  getContentStats: (niche_id) => request('/content/stats' + (niche_id ? '?niche_id=' + niche_id : '')),
  getAccountContent: (id) => request(`/content/account/${id}`),

  // Постинг
  manualPost: (account_ids, reels_count) => request('/posting/manual', { method: 'POST', body: JSON.stringify({ account_ids, reels_count }) }),
  startAutoPost: (data) => request('/posting/auto/start', { method: 'POST', body: JSON.stringify(data) }),
  stopAutoPost: () => request('/posting/auto/stop', { method: 'POST' }),
  getPostingStatus: () => request('/posting/status'),

  // Сторис
  uploadStoryPhotos: (file, niche_id) => uploadFile('/stories/photos/upload', file, { niche_id }),
  getStoryStats: (account_id) => request('/stories/stats' + (account_id ? '?account_id=' + account_id : '')),

  // Чекер
  importCheckers: (data) => request('/checker/accounts/import', { method: 'POST', body: JSON.stringify({ data }) }),
  getCheckers: () => request('/checker/accounts'),
  getAliveCheckers: () => request('/checker/accounts/alive'),
  manualCheck: (account_ids) => request('/checker/check', { method: 'POST', body: JSON.stringify({ account_ids }) }),
  startAutoCheck: (niche_id) => request('/checker/auto/start', { method: 'POST', body: JSON.stringify({ niche_id }) }),
  stopAutoCheck: () => request('/checker/auto/stop', { method: 'POST' }),
  getCheckerStatus: () => request('/checker/status'),
  getCheckerResults: () => request('/checker/results'),
  getAccountStats: (id) => request(`/checker/stats/account/${id}`),
  getNicheStats: (id) => request(`/checker/stats/niche/${id}`),
}
