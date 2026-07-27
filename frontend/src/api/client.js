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
}
