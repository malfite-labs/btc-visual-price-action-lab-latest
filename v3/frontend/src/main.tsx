import React from 'react'
import { createRoot } from 'react-dom/client'
import App from './App'
import KlineWindow from './KlineWindow'
import './style.css'

createRoot(document.getElementById('root')!).render(<React.StrictMode>{window.location.pathname === '/kline' ? <KlineWindow /> : <App />}</React.StrictMode>)
