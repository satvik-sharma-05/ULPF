import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Proxies /api to the chatbot FastAPI backend and /analytics-api to the
// analytics FastAPI backend during `npm run dev`, so the frontend can call
// relative paths (fetch('/api/ask'), fetch('/analytics-api/overview')) with
// no CORS configuration needed at all in the common dev setup - only
// cross-origin access (opening the frontend from a different host than the
// APIs) actually needs the CORS middleware each backend already sets up.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: process.env.VITE_API_URL || 'http://localhost:8000',
        changeOrigin: true,
      },
      '/analytics-api': {
        target: process.env.VITE_ANALYTICS_API_URL || 'http://localhost:8010',
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/analytics-api/, '/api/analytics'),
      },
      '/ingest-api': {
        target: process.env.VITE_INGEST_API_URL || 'http://localhost:8020',
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/ingest-api/, '/api/ingest'),
      },
    },
  },
})
