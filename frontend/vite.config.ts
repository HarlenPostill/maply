import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

const API = 'http://127.0.0.1:8000';

// Local-only dashboard. Everything is bundled; no CDNs or external fonts.
export default defineConfig({
  base: '/',
  plugins: [react()],
  server: {
    host: '127.0.0.1',
    port: 5173,
    strictPort: true,
    proxy: {
      '/api': { target: API, changeOrigin: false },
      '/files': { target: API, changeOrigin: false },
      '/mock': { target: API, changeOrigin: false },
    },
  },
  preview: {
    host: '127.0.0.1',
    proxy: {
      '/api': { target: API, changeOrigin: false },
      '/files': { target: API, changeOrigin: false },
      '/mock': { target: API, changeOrigin: false },
    },
  },
  build: {
    outDir: 'dist',
    assetsDir: 'assets',
    emptyOutDir: true,
    sourcemap: false,
    chunkSizeWarningLimit: 1200,
  },
});
