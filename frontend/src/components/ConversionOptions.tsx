import { Alert } from '@/components/Alert'
import { OptionField } from '@/components/ConversionOptionField'
import { TargetSizeField } from '@/components/TargetSizeField'
import type { SizeOptions } from '@/hooks/useConversionGroups'
import type { ConversionCapabilities, ConversionCapability } from '@/types'
import { capabilityFor } from '@/utils/conversion'
import { itemVisible, type OptionValue, type OptionValues } from '@/utils/conversionOptions'
import { toBytes } from '@/utils/format'

interface ConversionOptionsProps {
  /** 所属分组的 id，用来给单选按钮分组（同页多组时不能同名） */
  groupId: string
  sourceType: string
  targetType: string
  capabilities: ConversionCapabilities
  values: OptionValues
  size: SizeOptions
  /** 单个文件的大小上限，供目标大小校验用 */
  maxBytes: number
  fileCount: number
  disabled?: boolean
  onValueChange: (key: string, value: OptionValue) => void
  onSizeChange: (changes: Partial<SizeOptions>) => void
}

/**
 * 一组的转换参数（第九阶段 §二十二）。
 *
 * **面板完全由服务端的 ``options_schema`` 驱动**：摆哪些控件、叫什么名字、
 * 取值范围、什么时候出现，全部读自那一条能力的 schema，页面上没有任何
 * 「图片才有质量、PNG 没有质量」这类判断。于是加一种格式（MP4 / EPUB / CSV）
 * 只需要服务端多一条带 schema 的能力，这里一行都不用改。
 *
 * 用户改的值按 schema 的键原样存在 ``values`` 里，提交时由**同一个模块**
 * 序列化成 ``options`` JSON —— 渲染与提交同一条数据。
 *
 * 「目标最大文件大小」是唯一还留在面板上的老控件：它不在任何 schema 里
 * （是跨格式的后处理，不属于某一条能力），仍然走第七阶段的扁平字段。
 */
export function ConversionOptions({
  groupId,
  sourceType,
  targetType,
  capabilities,
  values,
  size,
  maxBytes,
  fileCount,
  disabled = false,
  onValueChange,
  onSizeChange,
}: ConversionOptionsProps) {
  const entry = capabilityFor(capabilities, sourceType, targetType)
  const items = entry?.options_schema?.items ?? []
  const visible = items.filter((item) => itemVisible(item, values))

  // 目标大小只在这两种情况下有意义（与第七阶段一致）：
  // 结果仍是图片（按体积重编码），或 Office 转 PDF（转完再压）。
  // 判据取自服务端：结果的 MIME 是图片，或者源是 Office 那几个格式。
  const showTargetSize = producesImage(capabilities, entry) || isOfficeSource(capabilities, sourceType)

  const customBytes =
    size.targetSize === 'custom' ? toBytes(Number(size.customSize), size.sizeUnit) : null
  const sizeError =
    size.targetSize !== 'custom'
      ? null
      : customBytes === null
        ? '请输入有效的目标大小'
        : customBytes > maxBytes
          ? '目标大小超出单个文件的大小上限'
          : null

  if (visible.length === 0 && !showTargetSize && !entry?.note) return null

  return (
    <div className="mt-5 space-y-6 border-t border-slate-200 pt-5">
      {/* 一组一个目标格式、一套参数：说清楚，免得以为可以逐个文件调 */}
      {fileCount > 1 && (
        <p className="text-xs text-slate-500">
          这一组的 {fileCount} 个文件会按同一套参数处理。
        </p>
      )}

      {visible.map((item) => (
        <OptionField
          key={item.key}
          groupId={groupId}
          item={item}
          value={values[item.key]}
          disabled={disabled}
          onChange={(value) => onValueChange(item.key, value)}
        />
      ))}

      {/* 「多帧文件只取第一帧」这类必须说给用户听的事实（决策 C）。
          它来自能力的 note，不是界面里写死的一句文案。 */}
      {entry?.note && <Alert tone="info">{entry.note}</Alert>}

      {showTargetSize && (
        <fieldset>
          <legend className="text-sm font-medium text-slate-900">目标最大文件大小</legend>
          <p className="mt-1 text-xs text-slate-500">
            {producesImage(capabilities, entry)
              ? '选择后会自动优化压缩质量，使结果不超过该大小。'
              : '选择后会在转换完成时压缩结果，尽量不超过该大小。'}
          </p>
          <div className="mt-3">
            <TargetSizeField
              name={`conversion-size-${groupId}`}
              value={size.targetSize}
              customValue={size.customSize}
              customUnit={size.sizeUnit}
              maxBytes={maxBytes}
              error={sizeError}
              onChange={(targetSize) => onSizeChange({ targetSize })}
              onCustomChange={(customSize, sizeUnit) => onSizeChange({ customSize, sizeUnit })}
              disabled={disabled}
            />
          </div>
        </fieldset>
      )}
    </div>
  )
}

/**
 * 这一格能力的产物是不是一张图片。
 *
 * 判据是服务端给的 MIME，不是格式名单：``image/*`` 就是图片。
 * 前端因此不需要知道 bmp / gif / tiff 的存在（§十六）。
 */
function producesImage(
  capabilities: ConversionCapabilities,
  entry: ConversionCapability | null,
): boolean {
  if (!entry) return false
  const mediaType = capabilities.formats.find(
    (format) => format.value === entry.target_type,
  )?.media_type
  return mediaType?.startsWith('image/') ?? false
}

/** 源格式归 Office（LibreOffice）那一路吗。同样是服务端给的分类 */
function isOfficeSource(capabilities: ConversionCapabilities, sourceType: string): boolean {
  return (
    capabilities.groups.find((group) => group.key === 'office')?.sources.some(
      (source) => source.value === sourceType,
    ) ?? false
  )
}
