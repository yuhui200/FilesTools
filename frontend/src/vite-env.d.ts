/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** 后端地址；留空表示使用相对路径（由 Vite 代理或同源部署处理） */
  readonly VITE_API_BASE_URL?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
