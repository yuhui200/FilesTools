/** 上传前的本地校验：尽早给出提示，避免白跑一次网络请求。 */

import { fileExtension, formatBytes } from './format'

export interface UploadRules {
  allowedExtensions: string[]
  maxBytes: number
}

/** 校验单个文件；通过返回 null，否则返回中文错误提示。 */
export function checkFile(file: File, rules: UploadRules, kind = '图片'): string | null {
  if (file.size === 0) {
    return '文件为空，请换一个文件'
  }

  if (file.size > rules.maxBytes) {
    return `文件过大，请上传更小的文件（当前 ${formatBytes(file.size)}，上限 ${formatBytes(
      rules.maxBytes,
    )}）。`
  }

  const ext = fileExtension(file.name)
  if (!rules.allowedExtensions.includes(ext)) {
    return `暂不支持该文件格式，请上传 ${joinWithOr(
      rules.allowedExtensions.map((e) => e.replace('.', '').toUpperCase()),
    )} ${kind}。`
  }

  return null
}

/** 用中文顿号连接，最后一项前用「或」：["JPG","PNG","WEBP"] -> "JPG、PNG 或 WEBP" */
function joinWithOr(items: string[]): string {
  if (items.length <= 1) return items[0] ?? ''
  return `${items.slice(0, -1).join('、')} 或 ${items[items.length - 1]}`
}

/** 批量校验，返回第一个错误信息和通过校验的文件。 */
export function checkFiles(
  files: File[],
  rules: UploadRules,
  kind = '图片',
): { accepted: File[]; error: string | null } {
  const accepted: File[] = []
  let error: string | null = null

  for (const file of files) {
    const message = checkFile(file, rules, kind)
    if (message) {
      error ??= message
    } else {
      accepted.push(file)
    }
  }

  return { accepted, error }
}
