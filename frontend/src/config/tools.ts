/** 工具清单 —— 首页卡片、导航和路由都从这里取，避免多处重复维护。 */

import type { ComponentType } from 'react'

import {
  IconCompress,
  IconConvert,
  IconExcel,
  IconFileImage,
  IconImages,
  IconMerge,
  IconPageRemove,
  IconResize,
  IconSlide,
  IconSplit,
  IconTextFile,
  IconUniversal,
  IconWord,
  type IconProps,
} from '@/components/Icons'

/** 首页的一级分类。是闭合联合类型：加一个分类要同时改 ToolPage / ToolList / App。 */
export type ToolCategory = 'convert' | 'image' | 'pdf' | 'doc'

export interface ToolDefinition {
  id: string
  name: string
  description: string
  /** 前端路由；未上线的工具为 null */
  path: string | null
  icon: ComponentType<IconProps>
  category: ToolCategory
  /** 是否已上线。未上线时卡片不可点击，只做展示 */
  available: boolean
}

export const TOOLS: ToolDefinition[] = [
  {
    id: 'universal-converter',
    name: '统一转换中心',
    description: '上传任意支持的文件，自动识别类型、自动分组，一次转完并打包下载。',
    path: '/convert',
    icon: IconUniversal,
    category: 'convert',
    available: true,
  },
  {
    id: 'image-compress',
    name: '图片压缩',
    description: '指定目标大小，自动调整质量和尺寸，压到指定体积以内。',
    path: '/image/compress',
    icon: IconCompress,
    category: 'image',
    available: true,
  },
  {
    id: 'image-convert',
    name: '图片格式转换',
    // 不再列举格式名。这份清单是写死的，而**服务器装没装 HEIC 组件**这种事
    // 它无从得知 —— 列了就是在替服务器承诺一件它可能做不到的事（§五十六）。
    // 想看当下列得出哪些，去图片工具页那一栏（它读的是能力目录）。
    description: '在服务器支持的图片格式之间互转，也能转成 PDF，最多一次 50 张。',
    path: '/image/convert',
    icon: IconConvert,
    category: 'image',
    available: true,
  },
  {
    id: 'image-resize',
    name: '调整图片尺寸',
    description: '自定义宽高或选用常用尺寸，可锁定宽高比例。',
    path: '/image/resize',
    icon: IconResize,
    category: 'image',
    available: true,
  },
  {
    id: 'image-to-pdf',
    name: '图片转 PDF',
    description: '一次上传多张图片，可拖动调整顺序，合并成一个 PDF 文件。',
    path: '/pdf/from-images',
    icon: IconImages,
    category: 'pdf',
    available: true,
  },
  {
    id: 'pdf-to-image',
    name: 'PDF 转图片',
    description: '把 PDF 的指定页面导出成 JPG、PNG 或 WEBP 图片。',
    path: '/pdf/to-images',
    icon: IconFileImage,
    category: 'pdf',
    available: true,
  },
  {
    id: 'pdf-compress',
    name: 'PDF 压缩',
    description: '按等级重新编码页面图片，也可指定压到某个大小以内。',
    path: '/pdf/compress',
    icon: IconCompress,
    category: 'pdf',
    available: true,
  },
  {
    id: 'pdf-merge',
    name: 'PDF 合并',
    description: '把多个 PDF 按你排定的顺序合并成一个文件。',
    path: '/pdf/merge',
    icon: IconMerge,
    category: 'pdf',
    available: true,
  },
  {
    id: 'pdf-split',
    name: 'PDF 拆分',
    description: '每页一个文件、按范围拆分，或只挑出需要的页面。',
    path: '/pdf/split',
    icon: IconSplit,
    category: 'pdf',
    available: true,
  },
  {
    id: 'pdf-edit-pages',
    name: 'PDF 页面删除 / 提取',
    description: '看着页面缩略图点选，删掉不要的页，或只留下要的页。',
    path: '/pdf/edit-pages',
    icon: IconPageRemove,
    category: 'pdf',
    available: true,
  },
  {
    id: 'pdf-to-word',
    name: 'PDF 转 Word',
    description: '文字版 PDF 直接提取文字，扫描件自动逐页识别，输出可编辑的 .docx。',
    path: '/pdf/to-word',
    icon: IconWord,
    category: 'pdf',
    available: true,
  },
  {
    id: 'doc-word',
    name: 'Word 转 PDF',
    description: '把 .docx / .doc 文档转成 PDF，可顺便限制结果文件的大小。',
    path: '/doc/word',
    icon: IconWord,
    category: 'doc',
    available: true,
  },
  {
    id: 'doc-excel',
    name: 'Excel 转 PDF',
    description: '把 .xlsx / .xls 表格转成 PDF，多个工作表会一起导出。',
    path: '/doc/excel',
    icon: IconExcel,
    category: 'doc',
    available: true,
  },
  {
    id: 'doc-ppt',
    name: 'PPT 转 PDF',
    description: '把 .pptx / .ppt 演示文稿转成 PDF，一页幻灯片一页 PDF。',
    path: '/doc/ppt',
    icon: IconSlide,
    category: 'doc',
    available: true,
  },
  {
    id: 'doc-txt',
    name: 'TXT 转 PDF',
    description: '把纯文本排版成 PDF，可自选字体、字号、页面大小和方向。',
    path: '/doc/txt',
    icon: IconTextFile,
    category: 'doc',
    available: true,
  },
]

export const CONVERT_TOOLS = TOOLS.filter((tool) => tool.category === 'convert')
export const IMAGE_TOOLS = TOOLS.filter((tool) => tool.category === 'image')
export const PDF_TOOLS = TOOLS.filter((tool) => tool.category === 'pdf')
export const DOC_TOOLS = TOOLS.filter((tool) => tool.category === 'doc')

/**
 * 首页「全部工具」的分组，顺序即展示顺序。
 *
 * 写成数组是为了首页能用一个循环渲染 —— 之前每个分类一段写死的标题 + map，
 * 开第三个分类就要抄第三遍。导航链接也从这里生成，少一处会走偏的地方。
 *
 * 统一转换中心（第七阶段）排在最前：它是「不知道该进哪个页面」时的入口，
 * 放在最后等于让人先看完全部十四个专用工具才找到它。
 */
export const TOOL_GROUPS: { label: string; path: string; tools: ToolDefinition[] }[] = [
  { label: '格式转换', path: '/convert', tools: CONVERT_TOOLS },
  { label: '图片工具', path: '/image', tools: IMAGE_TOOLS },
  { label: 'PDF 工具', path: '/pdf', tools: PDF_TOOLS },
  { label: '文档转换', path: '/doc', tools: DOC_TOOLS },
]

/** 导航栏配置 */
export const NAV_LINKS = [
  { label: '首页', to: '/' },
  ...TOOL_GROUPS.map((group) => ({ label: group.label, to: group.path })),
] as const
