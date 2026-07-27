import React from 'react'
import { NavLink } from 'react-router-dom'

const navItems = [
  { to: '/', label: 'Ниши' },
  { to: '/accounts', label: 'Аккаунты' },
  { to: '/proxies', label: 'Прокси' },
]

export default function Layout({ children }) {
  return (
    <div style={{ display: 'flex', minHeight: '100vh', background: '#0d1117', color: '#e6edf3' }}>
      <nav style={{
        width: 200, padding: '20px 0', background: '#161b22',
        borderRight: '1px solid #30363d', flexShrink: 0
      }}>
        <div style={{ padding: '0 16px 20px', fontSize: 20, fontWeight: 700, color: '#58a6ff' }}>
          InstaGrid
        </div>
        {navItems.map(item => (
          <NavLink
            key={item.to}
            to={item.to}
            style={({ isActive }) => ({
              display: 'block', padding: '10px 16px', color: isActive ? '#58a6ff' : '#8b949e',
              textDecoration: 'none', background: isActive ? '#1f2937' : 'transparent',
              borderLeft: isActive ? '3px solid #58a6ff' : '3px solid transparent',
              fontSize: 14,
            })}
          >
            {item.label}
          </NavLink>
        ))}
      </nav>
      <main style={{ flex: 1, padding: 24, overflow: 'auto' }}>
        {children}
      </main>
    </div>
  )
}
