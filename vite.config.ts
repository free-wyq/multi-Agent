import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import electron from 'vite-plugin-electron'
import renderer from 'vite-plugin-electron-renderer'

export default defineConfig({
  plugins: [
    react(),
    electron([
      {
        // 主进程入口
        entry: 'electron/main.ts',
      },
      {
        // preload 脚本
        entry: 'electron/preload.ts',
        onstart(args) {
          args.reload()
        },
      },
    ]),
    renderer(),
  ],
  server: {
    port: 5173,
    host: true,
  },
})
