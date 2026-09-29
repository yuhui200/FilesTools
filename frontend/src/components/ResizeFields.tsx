interface Size {
  width: number
  height: number
}

const PRESET_GROUPS: { label: string; sizes: Size[] }[] = [
  {
    label: '社交媒体',
    sizes: [
      { width: 1080, height: 1080 },
      { width: 1080, height: 1350 },
      { width: 1080, height: 1920 },
    ],
  },
  {
    label: '视频',
    sizes: [
      { width: 1920, height: 1080 },
      { width: 1280, height: 720 },
    ],
  },
  {
    label: '网页',
    sizes: [{ width: 1200, height: 630 }],
  },
]

interface ResizeFieldsProps {
  width: string
  height: string
  keepAspect: boolean
  /** 第一张图片的原始尺寸，用于展示与等比联动；未知时为 null */
  originalWidth: number | null
  originalHeight: number | null
  /** 是否一次处理多张图片 */
  multiple: boolean
  disabled?: boolean
  error: string | null
  onWidthChange: (value: string) => void
  onHeightChange: (value: string) => void
  onKeepAspectChange: (value: boolean) => void
  onPreset: (size: Size) => void
}

/**
 * 尺寸调整参数面板。
 *
 * 保持宽高比例时会按第一张图片的比例联动另一个输入框；
 * 批量处理时后端对每张图片各用自己的比例，这里会额外说明。
 */
export function ResizeFields({
  width,
  height,
  keepAspect,
  originalWidth,
  originalHeight,
  multiple,
  disabled = false,
  error,
  onWidthChange,
  onHeightChange,
  onKeepAspectChange,
  onPreset,
}: ResizeFieldsProps) {
  const parsedWidth = Number(width)
  const parsedHeight = Number(height)
  const hasWidth = Number.isFinite(parsedWidth) && parsedWidth > 0
  const hasHeight = Number.isFinite(parsedHeight) && parsedHeight > 0

  // 保持比例时，单张图片会完整放进用户填写的宽高范围内（不裁剪、不变形）。
  // 批量处理时每张图片的宽高比可能不同，不按第一张推算。
  const fitted =
    keepAspect && !multiple && hasWidth && hasHeight && originalWidth && originalHeight
      ? fitInside(originalWidth, originalHeight, parsedWidth, parsedHeight)
      : null

  return (
    <div>
      {/* 原始尺寸 */}
      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
        <span className="text-sm text-slate-500">原始尺寸：</span>
        <span className="text-sm font-medium tabular-nums text-slate-900">
          {originalWidth && originalHeight
            ? `${originalWidth} × ${originalHeight} px`
            : '读取中…'}
        </span>
        {multiple && (
          <span className="text-xs text-slate-400">（第一张图片，其余图片各自按自身比例处理）</span>
        )}
      </div>

      {/* 自定义尺寸 */}
      <p className="mt-5 text-sm font-medium text-slate-900">自定义尺寸</p>
      <div className="mt-2.5 flex flex-wrap items-end gap-2.5 sm:gap-3">
        <label className="block">
          <span className="mb-1.5 block text-xs text-slate-500">宽度</span>
          <span className="flex items-center gap-1.5">
            <input
              type="number"
              min={1}
              step={1}
              inputMode="numeric"
              value={width}
              disabled={disabled}
              placeholder={originalWidth ? String(originalWidth) : '例如 1920'}
              onChange={(event) => onWidthChange(event.target.value)}
              className="field w-24 sm:w-32"
            />
            <span className="text-xs text-slate-500">px</span>
          </span>
        </label>

        <span className="pb-3 text-slate-400" aria-hidden="true">
          ×
        </span>

        <label className="block">
          <span className="mb-1.5 block text-xs text-slate-500">高度</span>
          <span className="flex items-center gap-1.5">
            <input
              type="number"
              min={1}
              step={1}
              inputMode="numeric"
              value={height}
              disabled={disabled}
              placeholder={originalHeight ? String(originalHeight) : '例如 1080'}
              onChange={(event) => onHeightChange(event.target.value)}
              className="field w-24 sm:w-32"
            />
            <span className="text-xs text-slate-500">px</span>
          </span>
        </label>
      </div>

      {error && <p className="mt-2 text-xs text-red-600">{error}</p>}
      {!error && !hasWidth && !hasHeight && (
        <p className="mt-2 text-xs text-slate-500">
          请填写宽度或高度（至少填一个），填写后即可开始调整。
        </p>
      )}

      {/* 保持宽高比例 */}
      <label className="mt-4 flex w-fit cursor-pointer items-center gap-2.5">
        <input
          type="checkbox"
          checked={keepAspect}
          disabled={disabled}
          onChange={(event) => onKeepAspectChange(event.target.checked)}
          className="h-4 w-4 rounded border-slate-300 text-brand-600 focus:ring-brand-500"
        />
        <span className="text-sm text-slate-700">保持宽高比例</span>
      </label>

      <p className="mt-2 text-xs text-slate-500">
        {keepAspect
          ? multiple
            ? '批量处理时每张图片按自身的原始比例缩放：只填宽度或高度其中一个。填写两个则所有图片完整放进该尺寸内。'
            : '修改一个输入框，另一个会按原图比例自动计算。'
          : '宽高各自独立设置，图片会被拉伸到指定尺寸。'}
      </p>

      {fitted && (fitted.width !== parsedWidth || fitted.height !== parsedHeight) && (
        <p className="mt-1.5 text-xs text-brand-700">
          保持比例后实际输出：{fitted.width} × {fitted.height} px（完整放入，不裁剪）
        </p>
      )}

      {/* 常用尺寸 */}
      <div className="mt-6">
        <p className="text-sm font-medium text-slate-900">常用尺寸</p>
        <div className="mt-2.5 space-y-3">
          {PRESET_GROUPS.map((group) => (
            <div key={group.label}>
              <p className="mb-1.5 text-xs text-slate-500">{group.label}</p>
              <div className="flex flex-wrap gap-2">
                {group.sizes.map((size) => {
                  const active = size.width === parsedWidth && size.height === parsedHeight
                  return (
                    <button
                      key={`${size.width}x${size.height}`}
                      type="button"
                      disabled={disabled}
                      aria-pressed={active}
                      onClick={() => onPreset(size)}
                      className={[
                        'rounded-lg border px-3 py-1.5 text-sm tabular-nums transition disabled:opacity-50',
                        active
                          ? 'border-brand-500 bg-brand-50 text-brand-700'
                          : 'border-slate-200 bg-white text-slate-600 hover:border-slate-300 hover:bg-slate-50',
                      ].join(' ')}
                    >
                      {size.width} × {size.height}
                    </button>
                  )
                })}
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}

/** 按比例把图片完整放进 width × height 的框内 */
function fitInside(
  originalWidth: number,
  originalHeight: number,
  width: number,
  height: number,
): Size {
  const scale = Math.min(width / originalWidth, height / originalHeight)
  return {
    width: Math.max(1, Math.round(originalWidth * scale)),
    height: Math.max(1, Math.round(originalHeight * scale)),
  }
}
