import react from '@vitejs/plugin-react';
import { defineConfig } from 'vitest/config';

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/api/v1': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: false,
        bypass(req, res) {
          if (/^\/api\/v1\/(?:health\/ready|metrics)(?:\/|\?|$)/.test(req.url ?? '')) {
            res.statusCode = 404;
            res.end();
            return false;
          }
        },
      },
    },
  },
  test: {
    environment: 'jsdom',
    setupFiles: ['./src/test-setup.ts'],
    clearMocks: true,
  },
});
