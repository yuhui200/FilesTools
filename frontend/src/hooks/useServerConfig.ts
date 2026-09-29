import { useEffect, useState } from 'react'

import { fetchPublicConfig } from '@/services/api'
import type { PublicConfig } from '@/types'

/**
 * 后端不可用时的兜底配置。
 * 数值与 backend/config.py 保持一致，避免前端直接白屏。
 */
export const FALLBACK_CONFIG: PublicConfig = {
  max_upload_bytes: 50 * 1024 * 1024,
  // 三个数与 config.py 对齐（MAX_BATCH_FILES / MAX_BATCH_TOTAL_BYTES /
  // OFFICE_CONVERT_TIMEOUT_SECONDS）。以前这里是 20 / 150MB / 120s，
  // 与后端不符 —— 兜底配置本来就该是后端的一份镜像，不是第三套限制。
  max_batch_files: 50,
  max_batch_total_bytes: 300 * 1024 * 1024,
  // 第九阶段 §七 起后端多了 BMP / GIF / TIFF，第十阶段 A 又多了 HEIC / HEIF。
  // 这里跟着走：连不上后端时宁可放行得宽一点（用户拿到的是「网络不通」），
  // 也不要用一份过期的白名单回他「暂不支持该文件格式」—— 那是句假话。
  allowed_image_extensions: [
    '.bmp',
    '.gif',
    '.heic',
    '.heif',
    '.jpeg',
    '.jpg',
    '.png',
    '.tif',
    '.tiff',
    '.webp',
  ],
  quality_presets: ['high', 'balanced', 'strong'],
  // 85 与 config.py 的 DEFAULT_QUALITY_VALUE 对齐。这里曾经停在 80 ——
  // 第十阶段 A §二十七 把默认质量从 80 改成 85 并推广到**所有**路径
  // （目的就是消灭「同一张图在两个页面不选质量会得到两份结果」），
  // 兜底值当时漏改了。它不报错，只会让连不上后端时滑杆显示 80，
  // 而服务端按 85 处理 —— 正是那次改动要消灭的那类不一致。
  default_quality_value: 85,
  max_image_edge: 12000,
  output_formats: ['jpg', 'png', 'webp'],
  process_timeout_seconds: 60,
  allowed_pdf_extensions: ['.pdf'],
  max_pdf_pages: 500,
  pdf_thumbnail_max_pages: 60,
  pdf_thumbnail_width: 220,
  pdf_render_max_dpi: 300,
  pdf_render_default_dpi: 110,
  pdf_export_max_pages: 100,
  pdf_page_sizes: ['a4', 'a5', 'letter'],
  pdf_margins: ['none', 'small', 'medium', 'large'],
  pdf_compress_levels: ['light', 'balanced', 'strong'],
  default_pdf_compress_level: 'balanced',
  pdf_input_ttl_seconds: 30 * 60,
  // 这三份的顺序也跟服务端对齐（服务端是 sorted(...)）。**顺序不是无关紧要
  // 的细节**：这几份列表会被渲染进文件选择器的 accept 提示里，兜底时排成
  // 另一个样子，用户看到的就是另一句话。第十阶段 C 的机械对账
  // （scripts/verify_phase10.py D 段）当场把这三处挑了出来。
  allowed_word_extensions: ['.doc', '.docx'],
  allowed_excel_extensions: ['.xls', '.xlsx'],
  allowed_powerpoint_extensions: ['.ppt', '.pptx'],
  allowed_text_extensions: ['.txt'],
  doc_timeout_seconds: 180,
  // 连不上后端时无法得知服务器装没装转换组件。默认按「可用」渲染，
  // 缺组件时由服务端返回 503 和那句中文提示 —— 不假装知道一个不知道的事实。
  doc_conversion_available: true,
  doc_target_presets: ['none', '500kb', '1mb', '2mb', '5mb', '10mb', 'custom'],
  // 字体列表平时是服务端运行时探测出来的，这里只能给一个占位项。
  // 连接正常时这个值根本不会被用到；连不上后端时也排不了版，
  // 给「内置字体」比给一份本机可能根本没有的字体名单更诚实。
  txt_fonts: [{ value: 'china-s', label: '内置字体' }],
  txt_font_size: { min: 8, max: 32, default: 11 },
  txt_page_sizes: ['a4', 'a5', 'letter'],
  txt_orientations: ['portrait', 'landscape'],
  // PDF 转 Word。和上面同一个道理：连不上后端时不知道组件装没装，
  // 一律按「可用」渲染，真缺组件时由服务端返回 503 和中文提示。
  pdf_to_word_available: true,
  ocr_available: true,
  ocr_languages: ['chi_sim', 'eng'],
  pdf_to_word_max_ocr_pages: 30,
  pdf_to_word_timeout_seconds: 300,
}

export interface ServerConfigState {
  config: PublicConfig
  loading: boolean
  /** 后端不可达时的提示；为 null 表示一切正常 */
  offlineMessage: string | null
}

export function useServerConfig(): ServerConfigState {
  const [state, setState] = useState<ServerConfigState>({
    config: FALLBACK_CONFIG,
    loading: true,
    offlineMessage: null,
  })

  useEffect(() => {
    const controller = new AbortController()

    fetchPublicConfig(controller.signal)
      .then((config) => {
        setState({ config, loading: false, offlineMessage: null })
      })
      .catch((error: unknown) => {
        if (error instanceof DOMException && error.name === 'AbortError') return
        setState({
          config: FALLBACK_CONFIG,
          loading: false,
          offlineMessage:
            '未能连接后端服务，当前显示的是默认限制。请确认后端已启动（默认 http://127.0.0.1:8000）。',
        })
      })

    return () => controller.abort()
  }, [])

  return state
}
