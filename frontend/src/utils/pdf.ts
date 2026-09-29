/**
 * 页码解析与展示。
 *
 * 规则与后端 ``pdf/pages.py`` 保持一致（1 开始、支持 1-3、1,3,5、all），
 * 好让用户在输入框里立刻看到问题，而不必等一次网络往返。
 * 服务端仍会重新校验一遍 —— 这里是提前提示，不是唯一防线。
 */

/** 与后端一致的格式提示 */
const FORMAT_HINT = '页面范围格式不正确，示例：1-3、1,3,5、2-6，或填 all 表示全部页面'

const RANGE_RE = /^(\d+)\s*[-–—~]\s*(\d+)$/
const SINGLE_RE = /^\d+$/
const SEPARATORS = /[,，;；\s]+/

export interface PageSelection {
  /** 升序、去重后的页码（1 开始） */
  pages: number[]
  error: string | null
}

/** 把一段文本解析成页码列表，出错时给出中文提示。 */
export function parsePageSelection(raw: string, pageCount: number): PageSelection {
  const text = raw.trim()
  if (text === '') return { pages: [], error: null }

  if (text.toLowerCase() === 'all') {
    return { pages: rangeOf(1, pageCount), error: null }
  }

  const pages = new Set<number>()
  for (const token of text.split(SEPARATORS)) {
    if (!token) continue

    const range = RANGE_RE.exec(token)
    if (range) {
      const start = Number(range[1])
      const end = Number(range[2])
      if (start > end) {
        return { pages: [], error: `页码范围 ${token} 不正确，起始页不能大于结束页` }
      }
      const bad = firstOutOfBounds([start, end], pageCount, token)
      if (bad) return { pages: [], error: bad }
      for (let page = start; page <= end; page += 1) pages.add(page)
      continue
    }

    if (SINGLE_RE.test(token)) {
      const page = Number(token)
      const bad = firstOutOfBounds([page], pageCount, token)
      if (bad) return { pages: [], error: bad }
      pages.add(page)
      continue
    }

    return { pages: [], error: FORMAT_HINT }
  }

  if (pages.size === 0) return { pages: [], error: '请至少选择一页' }
  return { pages: [...pages].sort((a, b) => a - b), error: null }
}

function firstOutOfBounds(
  numbers: number[],
  pageCount: number,
  token: string,
): string | null {
  for (const number of numbers) {
    if (number < 1) return `页码 ${token} 不存在，页码从 1 开始`
    if (number > pageCount) return `第 ${number} 页不存在，该 PDF 共 ${pageCount} 页`
  }
  return null
}

/**
 * 按书写顺序解析页码，**不排序、不去重**。
 *
 * 与 :func:`parsePageSelection` 只差这一点：提取页面、自定义页面拆分
 * 的输出页序由用户决定（第四阶段 §11 支持拖拽排序，最终输出必须按照新顺序），
 * 所以这两处必须保留顺序。规则和后端 ``pdf.pages.parse_page_sequence``
 * 一一对应，报错文案也保持一致。
 */
export function parsePageSequence(raw: string, pageCount: number): PageSelection {
  const text = raw.trim()
  if (text === '') return { pages: [], error: null }

  if (text.toLowerCase() === 'all') {
    return { pages: rangeOf(1, pageCount), error: null }
  }

  const pages: number[] = []
  for (const token of text.split(SEPARATORS)) {
    if (!token) continue

    const range = RANGE_RE.exec(token)
    if (range) {
      const start = Number(range[1])
      const end = Number(range[2])
      if (start > end) {
        return { pages: [], error: `页码范围 ${token} 不正确，起始页不能大于结束页` }
      }
      const bad = firstOutOfBounds([start, end], pageCount, token)
      if (bad) return { pages: [], error: bad }
      for (let page = start; page <= end; page += 1) pages.push(page)
      continue
    }

    if (SINGLE_RE.test(token)) {
      const page = Number(token)
      const bad = firstOutOfBounds([page], pageCount, token)
      if (bad) return { pages: [], error: bad }
      pages.push(page)
      continue
    }

    return { pages: [], error: FORMAT_HINT }
  }

  if (pages.length === 0) return { pages: [], error: '请至少选择一页' }
  return { pages, error: null }
}

/**
 * 把页码按给定顺序写成 `3,1,5`（不做区间压缩，顺序必须看得出来）。
 *
 * 恰好是 1..N 的完整升序时改写成 ``all``：那是「全部页面」的标准写法，
 * 上千页的 PDF 也不会拼出一串几千字符的页码。
 */
export function formatPageSequence(pages: number[], pageCount = 0): string {
  if (
    pageCount > 0 &&
    pages.length === pageCount &&
    pages.every((page, index) => page === index + 1)
  ) {
    return 'all'
  }
  return pages.join(',')
}

function rangeOf(start: number, end: number): number[] {
  const pages: number[] = []
  for (let page = start; page <= end; page += 1) pages.push(page)
  return pages
}

/** 把页码列表压缩成 `1-3,5` 这样的展示文案（与后端 format_page_list 一致）。 */
export function formatPageList(pages: number[]): string {
  const ordered = [...new Set(pages)].sort((a, b) => a - b)
  const first = ordered[0]
  if (first === undefined) return ''

  const parts: string[] = []
  let start = first
  let previous = first

  for (const page of ordered.slice(1)) {
    if (page === previous + 1) {
      previous = page
      continue
    }
    parts.push(span(start, previous))
    start = page
    previous = page
  }
  parts.push(span(start, previous))
  return parts.join(',')
}

function span(start: number, end: number): string {
  return start === end ? String(start) : `${start}-${end}`
}

/** 页码的两位展示形式：1 -> 01（与结果文件名 part-01.pdf 同一套编号） */
export function padPage(page: number): string {
  return page < 10 ? `0${page}` : String(page)
}

/** 已选页数的中文摘要，过长时只给出数量。 */
export function describeSelection(pages: number[], pageCount: number): string {
  if (pages.length === 0) return '尚未选择任何页面'
  if (pageCount > 0 && pages.length === pageCount) return `已选中全部 ${pageCount} 页`
  const label = formatPageList(pages)
  return `已选中 ${pages.length} 页：${label.length > 40 ? `${label.slice(0, 40)}…` : label}`
}
