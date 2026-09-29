import { Link } from 'react-router-dom'

import { ToolCard } from '@/components/ToolCard'
import type { ToolCategory, ToolDefinition } from '@/config/tools'
import { CONVERT_TOOLS, DOC_TOOLS, IMAGE_TOOLS, PDF_TOOLS } from '@/config/tools'

const CONTENT: Record<ToolCategory, { title: string; description: string }> = {
  convert: {
    title: '格式转换',
    description:
      '不确定该用哪个工具时从这里开始：上传任意支持的文件，系统自动识别格式并分组，选好目标格式后一次批量转换，结果可以逐个下载或打包成 ZIP。',
  },
  image: {
    title: '图片工具',
    description: '压缩、格式转换、尺寸调整 —— 所有处理都在服务器端完成后立即删除原文件。',
  },
  pdf: {
    title: 'PDF 工具',
    description:
      '图片转 PDF、PDF 转图片、PDF 转 Word、压缩、合并、拆分，以及页面删除与提取。所有文件处理完成后自动从服务器删除。',
  },
  doc: {
    title: '文档转换',
    description:
      '把 Word、Excel、PowerPoint 和纯文本转成 PDF。转换完成后原始文档立即从服务器删除。',
  },
}

/** 分类 -> 工具列表。查表而不用三元，开新分类时不会漏掉这里。 */
const TOOLS_BY_CATEGORY: Record<ToolCategory, ToolDefinition[]> = {
  convert: CONVERT_TOOLS,
  image: IMAGE_TOOLS,
  pdf: PDF_TOOLS,
  doc: DOC_TOOLS,
}

interface ToolListProps {
  category: ToolCategory
}

export function ToolList({ category }: ToolListProps) {
  const content = CONTENT[category]
  const tools = TOOLS_BY_CATEGORY[category]

  return (
    <div className="mx-auto max-w-6xl px-4 py-10 sm:px-6 sm:py-14">
      <nav className="flex items-center gap-2 text-sm text-slate-500" aria-label="面包屑">
        <Link to="/" className="transition hover:text-slate-900">
          首页
        </Link>
        <span aria-hidden="true">/</span>
        <span className="text-slate-900">{content.title}</span>
      </nav>

      <h1 className="mt-4 text-2xl font-bold tracking-tight text-slate-900 sm:text-3xl">
        {content.title}
      </h1>
      <p className="mt-3 max-w-2xl text-slate-600">{content.description}</p>

      <div className="mt-8 grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {tools.map((tool) => (
          <ToolCard key={tool.id} tool={tool} />
        ))}
      </div>
    </div>
  )
}
