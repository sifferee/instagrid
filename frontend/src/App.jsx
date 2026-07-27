import React from 'react'
import { BrowserRouter, Routes, Route } from 'react-router-dom'
import Layout from './components/Layout'
import NichesPage from './pages/NichesPage'
import AccountsPage from './pages/AccountsPage'
import ProxiesPage from './pages/ProxiesPage'

export default function App() {
  return (
    <BrowserRouter>
      <Layout>
        <Routes>
          <Route path="/" element={<NichesPage />} />
          <Route path="/accounts" element={<AccountsPage />} />
          <Route path="/proxies" element={<ProxiesPage />} />
        </Routes>
      </Layout>
    </BrowserRouter>
  )
}
