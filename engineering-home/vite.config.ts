import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';

const target = process.env.EH_DEV_BACKEND_PROXY;
if (target) {
  const url = new URL(target);
  if (url.protocol !== 'http:' || !['localhost', '127.0.0.1', '[::1]'].includes(url.hostname) || url.username || url.password || url.search || url.hash || url.pathname !== '/') throw new Error('Dev proxy must be a loopback HTTP origin');
}
export default defineConfig({
  root: 'web', plugins: [react(), tailwindcss()],
  server: { host: '127.0.0.1', port: 5173, strictPort: true,
    proxy: target ? { '/api': { target, changeOrigin: false } } : undefined },
  build: { outDir: '../dist/web', emptyOutDir: true },
});
