import React from 'react'
import { BrowserRouter, Routes, Route } from 'react-router-dom'
import Layout from './components/Layout'
import NichesPage from './pages/NichesPage'
import AccountsPage from './pages/AccountsPage'
import ProxiesPage from './pages/ProxiesPage'
import ContentPage from './pages/ContentPage'
import PostingPage from './pages/PostingPage'
import CheckerPage from './pages/CheckerPage'
import StoriesPage from './pages/StoriesPage'

export default function App() {
  return (
    <BrowserRouter>
      <Layout>
        <Routes>
          <Route path="/" element={<NichesPage />} />
          <Route path="/accounts" element={<AccountsPage />} />
          <Route path="/proxies" element={<ProxiesPage />} />
          <Route path="/content" element={<ContentPage />} />
          <Route path="/posting" element={<PostingPage />} />
          <Route path="/stories" element={<StoriesPage />} />
          <Route path="/checker" element={<CheckerPage />} />
        </Routes>
      </Layout>
    </BrowserRouter>
  )
}
