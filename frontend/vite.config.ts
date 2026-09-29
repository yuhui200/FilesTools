import { fileURLToPath, URL } from 'node:url'

import react from '@vitejs/plugin-react'
import { defineConfig, loadEnv } from 'vite'

// frontend/ 目录本身 —— Vite 只从这里读 .env
const ENV_DIR = fileURLToPath(new URL('.', import.meta.url))

const DEFAULT_BACKEND = 'http://127.0.0.1:8000'

export default defineConfig(({ mode }) => {
  // 必须是函数形式才能拿到 mode 再调 loadEnv。
  // 之前这里直接读 process.env，而 Vite **不会**把 .env 的值灌进 process.env
  // （只放进 config.env / import.meta.env），于是写在 frontend/.env 里的
  // VITE_BACKEND_URL 是静默失效的 —— 代理 target 一直是兜底值，且不报任何错。
  // 第三个参数传 '' 表示不做前缀过滤，把 .env 里所有变量都读进来。
  const fromEnvFile = loadEnv(mode, ENV_DIR, '')

  // 优先级：命令行/系统环境变量 > frontend/.env > 兜底默认值。
  // 顺序不能反，否则命令行上 `VITE_BACKEND_URL=... npm run dev` 会被 .env 覆盖。
  const BACKEND =
    process.env.VITE_BACKEND_URL ?? fromEnvFile.VITE_BACKEND_URL ?? DEFAULT_BACKEND

  return {
    plugins: [react()],
    resolve: {
      alias: {
        '@': fileURLToPath(new URL('./src', import.meta.url)),
      },
    },
    server: {
      port: 5173,
      // 开发时把 /api 代理到后端，前后端同源，无需处理跨域
      proxy: {
        '/api': {
          target: BACKEND,
          changeOrigin: true,
        },
      },
    },
    build: {
      outDir: 'dist',
      sourcemap: false,
    },
  }
})
