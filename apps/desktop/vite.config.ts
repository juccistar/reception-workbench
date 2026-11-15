import { defineConfig } from 'vite';
import { fileURLToPath } from 'node:url';
export default defineConfig({
  root: fileURLToPath(new URL('.', import.meta.url)),
  base: './',
  build: { outDir: '../../artifacts/app/renderer', emptyOutDir: true, sourcemap: false,
    rollupOptions: { input: { index: fileURLToPath(new URL('./index.html', import.meta.url)), launcher: fileURLToPath(new URL('./launcher.html', import.meta.url)) } } },
});
