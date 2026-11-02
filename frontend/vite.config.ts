import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
export default defineConfig({ plugins: [react()], server: { host: '127.0.0.1', port: 5173, proxy: { '/api/v1': { target: 'http://127.0.0.1:8011', ws: true } } }, test: { environment: 'jsdom', setupFiles: './src/testSetup.ts', exclude: ['**/node_modules/**', '**/dist/**', '**/.remote-access-src/**'] } })
