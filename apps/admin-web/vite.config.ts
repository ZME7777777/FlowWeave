import { defineConfig, loadEnv } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, '.', '');
  return {
    base: env.VITE_ADMIN_BASE_PATH || '/admin/',
    plugins: [react()],
    server: {
      port: 5174,
      proxy: {
        '/admin-api': {
          target: env.VITE_ADMIN_API_PROXY_TARGET || 'http://localhost:8091',
          changeOrigin: true,
        },
      },
    },
  };
});
