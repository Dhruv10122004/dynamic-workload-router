import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// The dashboard always calls /api/v1 on its own origin (plan §10). In production the
// controller serves dist/; in `npm run dev` Vite forwards /api to the local controller.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: { '/api': process.env.DWR_DEV_API || 'http://127.0.0.1:8000' },
  },
})
