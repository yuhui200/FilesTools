import type { ReactNode } from 'react'
import { Link } from 'react-router-dom'

import type { ToolCategory } from '@/config/tools'

import { Alert } from './Alert'
import { IconShield } from './Icons'

const CATEGORY_LABEL: Record<ToolCategory, string> = {
  convert: '格式转换',
  image: '图片工具',
  pdf: 'PDF工具',
  doc: '文档转换',
}

const CATEGORY_PATH: Record<ToolCategory, string> = {
  convert: '/convert',
  image: '/image',
  pdf: '/pdf',
  doc: '/doc',
}

interface ToolPageProps {
  category: ToolCategory
  title: string
  description: ReactNode
  /** 后端不可达时的提示 */
  offlineMessage?: string | null
  children: ReactNode
}

/**
 * 工具页的统一外壳：面包屑 + 标题 + 说明 + 内容 + 安全说明。
 *
 * 六个 PDF 页面长得不一样，但页头和页脚是同一套东西。
 * 抽出来是为了保证它们和第一、二阶段的图片工具页面完全一致（§14），
 * 而不是各写一份、细节慢慢走偏。
 */
export function ToolPage({
  category,
  title,
  description,
  offlineMessage,
  children,
}: ToolPageProps) {
  return (
    <div className="mx-auto max-w-3xl px-4 py-10 sm:px-6 sm:py-14">
      <nav className="flex items-center gap-2 text-sm text-slate-500" aria-label="面包屑">
        <Link to="/" className="transition hover:text-slate-900">
          首页
        </Link>
        <span aria-hidden="true">/</span>
        <Link to={CATEGORY_PATH[category]} className="transition hover:text-slate-900">
          {CATEGORY_LABEL[category]}
        </Link>
        <span aria-hidden="true">/</span>
        <span className="text-slate-900">{title}</span>
      </nav>

      <h1 className="mt-4 text-2xl font-bold tracking-tight text-slate-900 sm:text-3xl">
        {title}
      </h1>
      <p className="mt-3 text-slate-600">{description}</p>

      {offlineMessage && (
        <div className="mt-6">
          <Alert tone="warning">{offlineMessage}</Alert>
        </div>
      )}

      <div className="mt-8 space-y-4">{children}</div>

      <div className="mt-8 flex items-start gap-3 rounded-2xl border border-slate-200 bg-white p-4 text-sm text-slate-600">
        <IconShield className="mt-0.5 h-4 w-4 shrink-0 text-emerald-600" />
        <p>
          上传的文件会以随机文件名存放在服务器临时目录中，结果下载后自动删除，
          原始文件最多保留 30 分钟用于连续操作。服务器不会长期保存你的文件。
        </p>
      </div>
    </div>
  )
}
