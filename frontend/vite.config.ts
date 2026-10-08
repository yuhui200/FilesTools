import { fileURLToPath, URL } from 'node:url'

import react from '@vitejs/plugin-react'
import { defineConfig, loadEnv } from 'vite'

// frontend/ 目录本身 —— Vite 只从这里读 .env
const ENV_DIR = fileURLToPath(new URL('.', import.meta.url))

const DEFAULT_BACKEND = 'http://127.0.0.1:8000'

/**
 * 桌面端（Tauri）烤死的后端地址。
 *
 * §四十六 决定：桌面端只连本机的 `http://127.0.0.1:8000`，不做「服务器地址」
 * 设置界面。用 `define` 在**构建期**把它替换成字面量，而不是让桌面端跑起来
 * 再去嗅探 —— 构建期常量是确定的，也不需要在 webview 里判断全局变量存不存在。
 *
 * 代价：换后端地址要重新构建。这是有意的取舍，已写进 README 的已知限制。
 */
const DESKTOP_BACKEND = 'http://127.0.0.1:8000'

export default defineConfig(({ mode }) => {
  // 桌面端单开一条 mode。它只改三件事：base 改成相对路径、产物落到
  // dist-desktop/、把后端地址烤进产物。**Web 的产出一个字都不变** ——
  // 所以后端托管的 frontend/dist 与所有 verify_phase*.py 的路径断言都不受影响。
  const desktop = mode === 'desktop'

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
    // 桌面端的页面由 Tauri 的自定义协议从 `http://tauri.localhost` 提供，
    // 产物里必须用**相对**路径引用 assets —— 以 `/` 开头的绝对路径会解析到
    // 协议根之外，打包出来就是一片白屏。Web 保持 `/` 不动。
    base: desktop ? './' : '/',
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
      // 桌面端另落一个目录，**不动 dist/**：后端就是从 dist/ 托管 Web 版的，
      // 两者共用一个目录会让「跑一次桌面构建」把线上页面换掉。
      outDir: desktop ? 'dist-desktop' : 'dist',
      sourcemap: false,
    },
    // 只有桌面端需要烤地址。Web 走 `''`（相对路径，由 Vite 代理或同源托管处理），
    // 传空对象让 mode==='production' 的产出与今天逐字节等价。
    define: desktop
      ? { 'import.meta.env.VITE_API_BASE_URL': JSON.stringify(DESKTOP_BACKEND) }
      : {},
  }
})
