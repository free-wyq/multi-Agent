import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Electron 模式：前端产物输出到 dist/，资源用相对路径以便 file:// 加载
export default defineConfig({
  plugins: [react()],
  base: './',
  server: {
    port: 5173,
    strictPort: true,
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
  },
})
