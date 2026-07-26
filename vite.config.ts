/// <reference types="vitest/config" />
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
  // 单测配置：jsdom 环境，src/**/*.{test,spec}.{ts,tsx} + __tests__/ 目录
  test: {
    globals: true,
    environment: 'jsdom',
    setupFiles: ['./src/test/setup.ts'],
    include: ['src/**/*.{test,spec}.{ts,tsx}'],
    css: false,
    passWithNoTests: true,
  },
})
