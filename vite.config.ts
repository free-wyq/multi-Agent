import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Tauri 模式：前端走纯 react 插件，产物输出到 dist/ 供 src-tauri 加载
// （原 Electron 主进程/preload 不再构建）
export default defineConfig({
  plugins: [react()],
  // Tauri dev 时 vite dev server 仍由 cargo tauri 启动并注入
  server: {
    port: 5173,
    strictPort: true,
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
  },
})
