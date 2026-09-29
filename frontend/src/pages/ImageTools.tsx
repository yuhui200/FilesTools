import { useEffect, useMemo } from 'react'
import { Link } from 'react-router-dom'

import { Alert } from '@/components/Alert'
import { ToolCard } from '@/components/ToolCard'
import { IMAGE_TOOLS } from '@/config/tools'
import { useConversionCapabilities } from '@/hooks/useConversionCapabilities'
import type { ConversionCapabilities } from '@/types'
import { conversionFormatLabel } from '@/utils/conversion'

const TITLE = '在线图片工具：压缩、格式转换、调整尺寸 | FileTools'
const DESCRIPTION =
  '上传图片即可压缩到指定体积、在支持的格式之间互转、调整尺寸或转成 PDF。全部在服务器端处理，下载后自动删除。'

/**
 * 图片工具（第十阶段 A §五十三–§五十六）。
 *
 * ## 这一页为什么不写死格式清单
 *
 * 它以前只在下面挂三张固定的卡片，卡片文案里写着「JPG、PNG、WEBP」。
 * 那个说法在第九阶段就已经过期了（BMP / GIF / TIFF / PDF / ICO 都能转），
 * 到第十阶段更是不止 —— 而且**有些格式是看服务器装没装组件的**：
 * HEIC 就没有对所有人都可用的道理。
 *
 * 所以这一页从 ``GET /api/conversion/capabilities`` 读当前**真的**能做的
 * 事情，把源格式与它能转成的目标原样铺出来。服务器缺组件时那些格子会
 * 从矩阵里消失、并附上原因（``notes``），页面因此不会宣传一件做不到的事。
 *
 * ## 旧页面一个都没删（§五十三）
 *
 * 上面那三张卡片仍然指向原来的 ``/image/compress``、``/image/convert``、
 * ``/image/resize`` —— 那是三条**有自己一套专门界面**的老路径（拖动、
 * 对比、目标体积搜索），比在统一中心里选参数更顺手，没有理由撤掉。
 * 这一页做的是「多给你一条知道全貌的路」，不是替换。
 */
export function ImageTools() {
  const { capabilities, loading, error } = useConversionCapabilities()

  useEffect(() => {
    const previousTitle = document.title
    document.title = TITLE
    const meta = document.querySelector<HTMLMetaElement>('meta[name="description"]')
    const previousDescription = meta?.content ?? null
    if (meta) meta.content = DESCRIPTION
    return () => {
      document.title = previousTitle
      if (meta && previousDescription !== null) meta.content = previousDescription
    }
  }, [])

  const rows = useMemo(() => imageMatrixRows(capabilities), [capabilities])
  // 图片组的 operation（目前是「查看图片信息」）。从能力目录里筛，
  // 不写死 ID —— 将来再加一个图片侧的单文件工具，这里自动跟上。
  const operations = (capabilities?.operations ?? []).filter(
    (entry) => entry.category === 'image',
  )

  return (
    <div className="mx-auto max-w-6xl px-4 py-10 sm:px-6 sm:py-14">
      <nav className="flex items-center gap-2 text-sm text-slate-500" aria-label="面包屑">
        <Link to="/" className="transition hover:text-slate-900">
          首页
        </Link>
        <span aria-hidden="true">/</span>
        <span className="text-slate-900">图片工具</span>
      </nav>

      <h1 className="mt-4 text-2xl font-bold tracking-tight text-slate-900 sm:text-3xl">
        图片工具
      </h1>
      <p className="mt-3 max-w-2xl text-slate-600">
        压缩、格式转换、尺寸调整，以及旋转、翻转、裁剪与清除拍摄信息。
        所有处理都在服务器端完成后立即删除原文件。
      </p>

      <div className="mt-8 grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {IMAGE_TOOLS.map((tool) => (
          <ToolCard key={tool.id} tool={tool} />
        ))}
      </div>

      {/* 服务器缺组件时**必须说**：能力被悄悄藏起来，用户只会以为网站坏了 */}
      {capabilities && capabilities.notes.length > 0 && (
        <div className="mt-6 space-y-2">
          {capabilities.notes.map((note) => (
            <Alert key={note} tone="info">
              {note}
            </Alert>
          ))}
        </div>
      )}
      {error !== null && (
        <div className="mt-6">
          <Alert tone="error">{error}</Alert>
        </div>
      )}

      <section className="mt-10" aria-labelledby="image-matrix-heading">
        <h2
          id="image-matrix-heading"
          className="text-lg font-semibold tracking-tight text-slate-900"
        >
          当前可用的图片格式
        </h2>
        <p className="mt-2 max-w-2xl text-sm text-slate-600">
          这是服务器此刻真的能做的事，由能力目录给出。点任意一种格式直接进统一转换中心，
          可以一次选多个文件、选目标格式后批量转换。
        </p>

        {loading && <p className="mt-4 text-sm text-slate-500">正在读取服务器能力…</p>}

        {!loading && rows.length === 0 && error === null && (
          <p className="mt-4 text-sm text-slate-500">
            服务器当前没有可用的图片转换能力。
          </p>
        )}

        {rows.length > 0 && (
          <ul className="mt-4 divide-y divide-slate-100 overflow-hidden rounded-xl ring-1 ring-slate-200">
            {rows.map((row) => (
              <li
                key={row.value}
                className="flex flex-col gap-2 bg-white px-4 py-3 sm:flex-row sm:items-center sm:gap-4 sm:px-5"
              >
                <div className="sm:w-48 sm:shrink-0">
                  <p className="text-sm font-medium text-slate-900">{row.label}</p>
                  <p className="mt-0.5 text-xs text-slate-500">{row.extensions.join(' ')}</p>
                </div>
                <ul className="flex flex-wrap gap-1.5">
                  {row.targets.map((target) => (
                    <li key={target.value}>
                      <Link
                        to="/convert"
                        className="inline-block rounded-full border border-slate-300 bg-white px-2.5 py-1 text-xs font-medium text-slate-700 transition hover:border-brand-400 hover:text-brand-700"
                      >
                        → {target.label}
                      </Link>
                    </li>
                  ))}
                </ul>
              </li>
            ))}
          </ul>
        )}

        <p className="mt-3 text-xs text-slate-500">
          多帧 GIF 与多页 TIFF 只转换第一帧。清除元数据指可安全剥离的 EXIF，
          不做像素级擦除。
        </p>
      </section>

      {operations.length > 0 && (
        <section className="mt-10" aria-labelledby="image-operations-heading">
          <h2
            id="image-operations-heading"
            className="text-lg font-semibold tracking-tight text-slate-900"
          >
            不产生新文件的工具
          </h2>
          <p className="mt-2 max-w-2xl text-sm text-slate-600">
            这类工具只看不改：不生成新文件，也就没有下载这一步。入口都在统一转换中心。
          </p>

          <ul className="mt-4 grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {operations.map((entry) => (
              <li
                key={entry.id}
                className="rounded-xl bg-white p-4 ring-1 ring-slate-200 transition hover:ring-brand-300"
              >
                <p className="text-sm font-semibold text-slate-900">{entry.display_name}</p>
                {entry.note && <p className="mt-1.5 text-xs text-slate-600">{entry.note}</p>}
                <Link
                  to="/convert"
                  className="mt-3 inline-block text-sm font-medium text-brand-600 hover:text-brand-700"
                >
                  去统一转换中心 →
                </Link>
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  )
}

interface MatrixRow {
  value: string
  label: string
  extensions: string[]
  targets: { value: string; label: string }[]
}

/**
 * 把能力目录里的矩阵收成「图片源 → 目标」的行。
 *
 * 一律以 ``capabilities`` 为准：
 *
 * * 哪些格式算图片 —— 看 ``formats[].category``（服务端给的类别），
 *   不在这里写一串扩展名；
 * * 一个源能转成什么 —— 看 ``matrix``（已经剔除了缺组件的格子）；
 * * 名字怎么显示 —— 看 ``formats[].label``，前端不自己拼。
 *
 * ``matrix`` 是 ``Record<源, 目标[]>``，键的顺序由服务端决定，
 * 这里原样保留（它就是能力表里的顺序）。
 */
function imageMatrixRows(capabilities: ConversionCapabilities | null): MatrixRow[] {
  if (capabilities === null) return []

  return capabilities.formats
    .filter((format) => format.category === 'image' && format.is_source)
    .map((format) => ({
      value: format.value,
      label: format.label,
      extensions: format.extensions,
      targets: (capabilities.matrix[format.value] ?? []).map((target) => ({
        value: target,
        label: conversionFormatLabel(capabilities, target),
      })),
    }))
    .filter((row) => row.targets.length > 0)
}
