import { defineConfig, loadEnv } from 'vite';

export default defineConfig(({ mode }) => ({
  server: {
    host: '127.0.0.1',
    proxy: {
      '/api': loadEnv(mode, '.', 'CIELINK_DASHBOARD_').CIELINK_DASHBOARD_API || 'http://127.0.0.1:8001',
    },
  },
}));
