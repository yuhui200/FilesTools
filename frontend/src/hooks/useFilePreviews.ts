import { useEffect, useRef, useState } from 'react'

export interface FilePreview {
  url: string | null
  width: number | null
  height: number | null
}

const EMPTY: FilePreview = { url: null, width: null, height: null }

/**
 * 为一批本地文件生成预览地址，并读出各自的真实像素尺寸。
 *
 * 尺寸调整页需要用它做「保持宽高比例」的联动计算
 * （原图 4032 × 3024，宽度填 1920 → 高度自动变 1440）。
 *
 * 返回的数组与传入的 files 一一对应；文件变化时统一回收旧的 object URL。
 */
export function useFilePreviews(files: File[]): FilePreview[] {
  const [previews, setPreviews] = useState<FilePreview[]>([])
  const urls = useRef<string[]>([])

  useEffect(() => {
    // 先回收上一批地址，避免内存泄漏
    for (const url of urls.current) {
      URL.revokeObjectURL(url)
    }
    urls.current = []

    if (files.length === 0) {
      setPreviews([])
      return
    }

    const created = files.map((file) => URL.createObjectURL(file))
    urls.current = created

    // 先给出地址占位，尺寸读完后再补上，界面不会空白等待
    setPreviews(created.map((url) => ({ url, width: null, height: null })))

    let cancelled = false

    created.forEach((url, index) => {
      const image = new Image()
      image.onload = () => {
        if (cancelled) return
        setPreviews((current) => {
          const next = [...current]
          next[index] = { url, width: image.naturalWidth, height: image.naturalHeight }
          return next
        })
      }
      // 解码失败时保留地址占位，尺寸留空
      image.src = url
    })

    return () => {
      cancelled = true
    }
  }, [files])

  // 组件卸载时清理
  useEffect(
    () => () => {
      for (const url of urls.current) {
        URL.revokeObjectURL(url)
      }
      urls.current = []
    },
    [],
  )

  return previews.length === files.length ? previews : files.map(() => EMPTY)
}
