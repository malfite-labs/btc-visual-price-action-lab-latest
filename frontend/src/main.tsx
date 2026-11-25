import React from 'react'
import { createRoot } from 'react-dom/client'
import App from './App'
import KlineWindow from './KlineWindow'
import './style.css'

const path = window.location.pathname
createRoot(document.getElementById('root')!).render(<React.StrictMode>{
  path === '/kline' || path === '/live-kline' ? <KlineWindow live={path === '/live-kline'} /> : <App />
}</React.StrictMode>)
