import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  build: {
    rollupOptions: {
      // 两个入口（v3.0 / D140）：`/` = 桌面壳，`/m/` = 手机壳。
      // 同一份构建产物、同一套 shared 代码，只有"壳"是两套。
      input: {
        desktop: 'index.html',
        mobile: 'm/index.html',
      },
    },
  },
  server: {
    port: 5173,
    // 前端只调 /api/*；由 Vite 代理到本机后端（AGENTS.md §6）
    proxy: {
      '/api': 'http://127.0.0.1:8001',
      // 主动消息走 WebSocket（api.md §7）；代理要显式开 ws
      '/ws': { target: 'ws://127.0.0.1:8001', ws: true },
    },
  },
})
