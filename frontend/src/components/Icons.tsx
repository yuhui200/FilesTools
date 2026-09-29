/** 内联 SVG 图标。不引第三方图标库，减少依赖体积。 */

import type { SVGProps } from 'react'

export type IconProps = SVGProps<SVGSVGElement>

function Svg({ children, ...props }: IconProps) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.8}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
      {...props}
    >
      {children}
    </svg>
  )
}

export function IconCompress(props: IconProps) {
  return (
    <Svg {...props}>
      <polyline points="4 14 10 14 10 20" />
      <polyline points="20 10 14 10 14 4" />
      <line x1="14" y1="10" x2="21" y2="3" />
      <line x1="3" y1="21" x2="10" y2="14" />
    </Svg>
  )
}

export function IconConvert(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="m17 2 4 4-4 4" />
      <path d="M3 11v-1a4 4 0 0 1 4-4h14" />
      <path d="m7 22-4-4 4-4" />
      <path d="M21 13v1a4 4 0 0 1-4 4H3" />
    </Svg>
  )
}

export function IconResize(props: IconProps) {
  return (
    <Svg {...props}>
      <polyline points="15 3 21 3 21 9" />
      <polyline points="9 21 3 21 3 15" />
      <line x1="21" y1="3" x2="14" y2="10" />
      <line x1="3" y1="21" x2="10" y2="14" />
    </Svg>
  )
}

export function IconImages(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M18 22H4a2 2 0 0 1-2-2V6" />
      <path d="m22 13-1.3-1.3a2.4 2.4 0 0 0-3.4 0L11 18" />
      <circle cx="12" cy="8" r="2" />
      <rect width="16" height="16" x="6" y="2" rx="2" />
    </Svg>
  )
}

export function IconFileImage(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z" />
      <path d="M14 2v4a2 2 0 0 0 2 2h4" />
      <circle cx="10" cy="12" r="1.6" />
      <path d="m20 16-1.6-1.6a2 2 0 0 0-2.8 0L10 20" />
    </Svg>
  )
}

export function IconUploadCloud(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M4 14.9A7 7 0 1 1 15.7 8h1.8a4.5 4.5 0 0 1 2.5 8.2" />
      <path d="M12 12v9" />
      <path d="m16 16-4-4-4 4" />
    </Svg>
  )
}

export function IconDownload(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
      <polyline points="7 10 12 15 17 10" />
      <line x1="12" y1="15" x2="12" y2="3" />
    </Svg>
  )
}

export function IconTrash(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M3 6h18" />
      <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6" />
      <path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" />
      <line x1="10" y1="11" x2="10" y2="17" />
      <line x1="14" y1="11" x2="14" y2="17" />
    </Svg>
  )
}

export function IconCheck(props: IconProps) {
  return (
    <Svg {...props}>
      <polyline points="20 6 9 17 4 12" />
    </Svg>
  )
}

export function IconCheckCircle(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M22 11.1V12a10 10 0 1 1-5.9-9.1" />
      <polyline points="22 4 12 14 9 11" />
    </Svg>
  )
}

export function IconAlert(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="m21.7 18-8-14a2 2 0 0 0-3.5 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.7-3Z" />
      <line x1="12" y1="9" x2="12" y2="13.5" />
      <line x1="12" y1="17" x2="12.01" y2="17" />
    </Svg>
  )
}

export function IconX(props: IconProps) {
  return (
    <Svg {...props}>
      <line x1="18" y1="6" x2="6" y2="18" />
      <line x1="6" y1="6" x2="18" y2="18" />
    </Svg>
  )
}

export function IconArrowRight(props: IconProps) {
  return (
    <Svg {...props}>
      <line x1="5" y1="12" x2="19" y2="12" />
      <polyline points="12 5 19 12 12 19" />
    </Svg>
  )
}

export function IconShield(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10Z" />
      <path d="m9 12 2 2 4-4" />
    </Svg>
  )
}

export function IconZap(props: IconProps) {
  return (
    <Svg {...props}>
      <polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2" />
    </Svg>
  )
}

/** 统一转换中心：一堆格式收进一个入口。 */
export function IconUniversal(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M12 3 3 7.5 12 12l9-4.5L12 3Z" />
      <path d="m3 12 9 4.5 9-4.5" />
      <path d="m3 16.5 9 4.5 9-4.5" />
    </Svg>
  )
}

export function IconImage(props: IconProps) {
  return (
    <Svg {...props}>
      <rect width="18" height="18" x="3" y="3" rx="2" />
      <circle cx="9" cy="9" r="2" />
      <path d="m21 15-3.1-3.1a2 2 0 0 0-2.8 0L6 21" />
    </Svg>
  )
}

export function IconFile(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z" />
      <path d="M14 2v4a2 2 0 0 0 2 2h4" />
    </Svg>
  )
}

/** 旋转的加载指示器 */
export function IconSpinner({ className = 'h-5 w-5', ...props }: IconProps) {
  return (
    <Svg className={`animate-spin ${className}`} {...props}>
      <path d="M21 12a9 9 0 1 1-6.2-8.6" />
    </Svg>
  )
}

export function IconMenu(props: IconProps) {
  return (
    <Svg {...props}>
      <line x1="4" y1="7" x2="20" y2="7" />
      <line x1="4" y1="12" x2="20" y2="12" />
      <line x1="4" y1="17" x2="20" y2="17" />
    </Svg>
  )
}

// ----------------------------------------------------------------------
// PDF 工具（第三阶段）
// ----------------------------------------------------------------------

/** PDF 合并：两份文档叠在一起 */
export function IconMerge(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M8 3H5a2 2 0 0 0-2 2v3" />
      <path d="M16 21h3a2 2 0 0 0 2-2v-3" />
      <path d="M3 8v8a2 2 0 0 0 2 2h3" />
      <path d="M21 16V8a2 2 0 0 0-2-2h-3" />
      <path d="M12 8v8" />
      <path d="m9 11 3-3 3 3" />
    </Svg>
  )
}

/** PDF 拆分：一份文档裂成几份 */
export function IconSplit(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M12 3v6" />
      <path d="M12 15v6" />
      <path d="m9 6 3-3 3 3" />
      <path d="m9 18 3 3 3-3" />
      <path d="M7 10h10a2 2 0 0 1 2 2v0" />
      <path d="M17 14H7a2 2 0 0 1-2-2v0" />
    </Svg>
  )
}

/** 页面删除：带叉的页面 */
export function IconPageRemove(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z" />
      <path d="M14 2v4a2 2 0 0 0 2 2h4" />
      <path d="m9.5 12.5 5 5" />
      <path d="m14.5 12.5-5 5" />
    </Svg>
  )
}

/** 页面提取：从页面堆里取出一张 */
export function IconPageExtract(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z" />
      <path d="M14 2v4a2 2 0 0 0 2 2h4" />
      <rect x="8" y="12" width="8" height="6" rx="1" />
      <path d="M12 12V9" />
      <path d="m10 11 2-2 2 2" />
    </Svg>
  )
}

/** 拖动排序用的抓手 */
export function IconGrip(props: IconProps) {
  return (
    <Svg {...props}>
      <circle cx="9" cy="6" r="1" />
      <circle cx="15" cy="6" r="1" />
      <circle cx="9" cy="12" r="1" />
      <circle cx="15" cy="12" r="1" />
      <circle cx="9" cy="18" r="1" />
      <circle cx="15" cy="18" r="1" />
    </Svg>
  )
}

export function IconArrowUp(props: IconProps) {
  return (
    <Svg {...props}>
      <line x1="12" y1="19" x2="12" y2="5" />
      <polyline points="5 12 12 5 19 12" />
    </Svg>
  )
}

export function IconArrowDown(props: IconProps) {
  return (
    <Svg {...props}>
      <line x1="12" y1="5" x2="12" y2="19" />
      <polyline points="19 12 12 19 5 12" />
    </Svg>
  )
}

export function IconPlus(props: IconProps) {
  return (
    <Svg {...props}>
      <line x1="12" y1="5" x2="12" y2="19" />
      <line x1="5" y1="12" x2="19" y2="12" />
    </Svg>
  )
}

export function IconClock(props: IconProps) {
  return (
    <Svg {...props}>
      <circle cx="12" cy="12" r="9" />
      <polyline points="12 7 12 12 15.5 14" />
    </Svg>
  )
}

export function IconPhone(props: IconProps) {
  return (
    <Svg {...props}>
      <rect x="6" y="2.5" width="12" height="19" rx="2.5" />
      <line x1="10.5" y1="18.5" x2="13.5" y2="18.5" />
    </Svg>
  )
}

// ----------------------------------------------------------------------
// 文档转换（第五阶段）
//
// 四个图标共用同一个页面轮廓，靠里面的图形区分格式 —— 它们本来就是
// 「同一种东西的不同格式」，形状一致比各画各的更好认。
// ----------------------------------------------------------------------

/** 页面轮廓，四个文档图标共用 */
function DocSheet() {
  return (
    <>
      <path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z" />
      <path d="M14 2v4a2 2 0 0 0 2 2h4" />
    </>
  )
}

/** Word 转 PDF：页面里的 W */
export function IconWord(props: IconProps) {
  return (
    <Svg {...props}>
      <DocSheet />
      <polyline points="8.5 11 10 17 12 14 14 17 15.5 11" />
    </Svg>
  )
}

/** Excel 转 PDF：页面里的表格 */
export function IconExcel(props: IconProps) {
  return (
    <Svg {...props}>
      <DocSheet />
      <rect x="7.5" y="10.5" width="9" height="7.5" rx="1" />
      <line x1="12" y1="10.5" x2="12" y2="18" />
      <line x1="7.5" y1="14.25" x2="16.5" y2="14.25" />
    </Svg>
  )
}

/** PPT 转 PDF：页面里的柱状图 */
export function IconSlide(props: IconProps) {
  return (
    <Svg {...props}>
      <DocSheet />
      <line x1="9" y1="18" x2="9" y2="13.5" />
      <line x1="12" y1="18" x2="12" y2="10.5" />
      <line x1="15" y1="18" x2="15" y2="15" />
    </Svg>
  )
}

/** TXT 转 PDF：页面里的文字行 */
export function IconTextFile(props: IconProps) {
  return (
    <Svg {...props}>
      <DocSheet />
      <line x1="8" y1="11.5" x2="16" y2="11.5" />
      <line x1="8" y1="15" x2="16" y2="15" />
      <line x1="8" y1="18.5" x2="13" y2="18.5" />
    </Svg>
  )
}
