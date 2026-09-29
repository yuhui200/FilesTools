/**
 * ``options_schema`` 的读写（第九阶段 §二十二）。
 *
 * 这个模块是**纯函数**：不碰 React、不碰网络、不认任何具体键名。
 * 页面上摆出来的控件与提交时序列化出来的 JSON 都从这里的同一份
 * ``ConversionOptionSpec[]`` 来 —— 渲染与提交同一条数据，因此不可能出现
 * 「界面上改的是 A、发出去的是 B」。
 *
 * 目标很实际：将来加 MP4 / MP3 / EPUB / CSV / SRT 时**不用重新设计
 * 参数面板**，服务端多一条带 schema 的能力，界面自动长出对应的控件。
 *
 * ## 值的形状
 *
 * 表单里一律是 ``string | boolean``：
 *
 * * ``enum`` / ``string`` / ``integer`` / ``number`` → 字符串（输入框的原值）；
 * * ``boolean`` → 真正的布尔。
 *
 * 提交时才按 schema 声明的类型还原成 JSON（见 :func:`serializeOptions`）。
 * 中间不引入第三种表示，是因为输入框本来就只能给出字符串，
 * 提前转成数字只会让「用户刚敲了一个 ``-``」这种中间状态无处安放。
 */

import type { ConversionOptionSpec, ConversionOptionsSchema } from '@/types'

/** 表单里的一个值 */
export type OptionValue = string | boolean

/** 一组参数。键是 schema 的扁平点号键 */
export type OptionValues = Record<string, OptionValue>

/**
 * 一项的默认值，转成表单表示。
 *
 * ``null`` / ``undefined`` 的默认值给**空串**而不是 ``"null"``：
 * 空串在序列化时会被丢掉（见 :func:`serializeOptions`），
 * 正好等于「用户没填」。
 */
export function defaultValueOf(item: ConversionOptionSpec): OptionValue {
  if (item.type === 'boolean') return item.default === true
  if (item.default === null || item.default === undefined) return ''
  return String(item.default)
}

/** 一整份 schema 的默认值 */
export function defaultOptionValues(schema: ConversionOptionsSchema | null): OptionValues {
  const values: OptionValues = {}
  for (const item of schema?.items ?? []) {
    values[item.key] = defaultValueOf(item)
  }
  return values
}

/**
 * 这一项现在该不该显示。
 *
 * ``visible_when`` 是 ``{键: 期望值}``，全部相等才显示。比较时两边都按字符串
 * 比：``visible_when`` 里的期望值来自服务端（``{"resize.mode": "custom"}``），
 * 而表单值是字符串，布尔项则可能真的是布尔 —— 统一 ``String()`` 之后
 * ``"true"`` 与 ``true`` 不会互相错过。
 */
export function itemVisible(item: ConversionOptionSpec, values: OptionValues): boolean {
  const when = item.visible_when
  if (!when) return true
  return Object.entries(when).every(
    ([key, expected]) => String(values[key] ?? '') === String(expected ?? ''),
  )
}

/** 当前可见的项，保持 schema 原本的次序 */
export function visibleItems(
  schema: ConversionOptionsSchema | null,
  values: OptionValues,
): ConversionOptionSpec[] {
  return (schema?.items ?? []).filter((item) => itemVisible(item, values))
}

/**
 * 表单值 → 提交用的 JSON。
 *
 * 三条规则，都在这里、只有这里：
 *
 * 1. **只发明面上看得见的项**。隐藏项带着值提交，服务端会按
 *    ``visible_when`` **明确拒绝**（不是静默忽略）—— 「选了自定义尺寸、
 *    填完宽高又改回原尺寸」的那份残留值不该让整批失败。
 * 2. **空值不发**。空串与 ``null`` 都表示「用户没填」，发一个 ``""``
 *    会被数字解析器判成非法。
 * 3. **按 schema 声明的类型还原**：``integer``/``number`` 发数字，
 *    ``boolean`` 发布尔，其余发字符串。
 */
export function serializeOptions(
  schema: ConversionOptionsSchema | null,
  values: OptionValues,
): Record<string, unknown> {
  const payload: Record<string, unknown> = {}

  for (const item of visibleItems(schema, values)) {
    const raw = values[item.key]
    if (raw === undefined || raw === null) continue
    if (item.type === 'boolean') {
      payload[item.key] = raw === true || raw === 'true'
      continue
    }
    const text = typeof raw === 'string' ? raw.trim() : String(raw)
    if (text === '') continue

    if (item.type === 'integer' || item.type === 'number') {
      const number = Number(text)
      // 输入框里的半截数字（``"1."`` / ``"-"``）不该被当成 0 发出去
      if (!Number.isFinite(number)) continue
      payload[item.key] = number
      continue
    }
    payload[item.key] = text
  }

  return payload
}

/**
 * 一项当前值的中文说法，给折叠行的摘要用。
 *
 * 枚举走它的 ``label``，布尔说「是 / 否」，其余原样带上单位。
 * 认不出的情况返回 ``null``，调用方就不显示这一项 ——
 * 宁可少说一句，也不要把内部的键名或原始值摆给用户看。
 */
export function describeValue(
  item: ConversionOptionSpec,
  value: OptionValue | undefined,
): string | null {
  if (value === undefined) return null

  if (item.type === 'boolean') return value === true || value === 'true' ? '是' : '否'

  const text = typeof value === 'string' ? value.trim() : String(value)
  if (text === '') return null

  if (item.type === 'enum') {
    return item.enum?.find((entry) => entry.value === text)?.label ?? null
  }
  return item.unit ? `${text} ${item.unit}` : text
}

/**
 * 一组参数里**与默认值不同**的那些，用来在折叠状态下说明用户改了什么。
 *
 * 只比较可见项：把「自定义宽度」这种当前根本不该出现的键算进摘要，
 * 会让摘要与面板说的不是一回事。
 */
export function changedItems(
  schema: ConversionOptionsSchema | null,
  values: OptionValues,
): ConversionOptionSpec[] {
  return visibleItems(schema, values).filter((item) => {
    const current = values[item.key]
    const fallback = defaultValueOf(item)
    if (item.type === 'boolean') return (current === true) !== (fallback === true)
    return String(current ?? '').trim() !== String(fallback ?? '').trim()
  })
}

/**
 * 整数 / 数字项在当前范围下该不该用滑杆。
 *
 * 规则与键名无关，只看声明出来的跨度：范围窄的（质量 10–100、字号 8–32）
 * 拖一下比敲数字快；跨度大的（宽度上限 12000）拖不准，只能用输入框。
 * 阈值 200 是「拖动大约 2 像素一格」的经验值。
 */
const SLIDER_MAX_SPAN = 200

export function prefersSlider(item: ConversionOptionSpec): boolean {
  if (item.type !== 'integer' && item.type !== 'number') return false
  if (item.min === undefined || item.max === undefined) return false
  return item.max - item.min <= SLIDER_MAX_SPAN
}

/** 数字项的步长：服务端给了就用它的，整数默认 1，小数默认 0.1 */
export function stepOf(item: ConversionOptionSpec): number {
  if (item.step !== undefined && item.step > 0) return item.step
  return item.type === 'integer' ? 1 : 0.1
}

/**
 * 枚举项用什么控件。
 *
 * 取值多于 5 个、或标签偏长时改用下拉框 —— 一排单选框在小屏上
 * 会变成一条很长的竖列，而 ``select`` 天生是折叠的（§三十七 的移动端要求）。
 */
const RADIO_MAX_OPTIONS = 5

export function prefersSelect(item: ConversionOptionSpec): boolean {
  const values = item.enum ?? []
  if (values.length > RADIO_MAX_OPTIONS) return true
  return values.some((entry) => entry.label.length > 12)
}

/**
 * 这一项该不该显示单位后缀。
 *
 * ``%`` / ``像素`` 之类跟着数字走；但 ``page_size.width_mm`` 的单位是
 * 「毫米」而标签已经写着「自定义宽度」，加上反而啰嗦 —— 由 :func:`unitSuffix`
 * 统一判一次，控件里不再各判各的。
 */
export function unitSuffix(item: ConversionOptionSpec): string | null {
  return item.unit ?? null
}
