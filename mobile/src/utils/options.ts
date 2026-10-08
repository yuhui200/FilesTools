/**
 * `options_schema` 的纯逻辑（规格 §二十五）。
 *
 * 与渲染分开，是因为这里的三条规则**错一条服务端就报错**，而它们又
 * 完全用不着 React 就能验证：
 *
 * 1. **值一律是字符串。** 服务端的 `conversion/options.py` 对 `integer`
 *    收 `"85"`、对 `boolean` 收 `"true"`，转成 Python 类型是它的事。
 *    客户端要是自作主张发数字/布尔，两边的类型判断就得各维护一遍。
 * 2. **隐藏的项不能提交。** 服务端 `_check_visibility` 对「键在、
 *    但它的 `visible_when` 不成立」是**明确报错**（不是静默忽略）。
 *    所以「用户选了自定义尺寸又改回原尺寸、宽高还留在表单里」这件事，
 *    必须在客户端就被挡掉。
 * 3. **默认值要填进去。** 不填等于没选，而服务端的默认值不一定等于
 *    界面显示的那个 —— 两边各写一份默认值是漂移的开始。
 */

import type { ConversionOptionEnumValue, ConversionOptionSpec, ConversionOptionsSchema } from '@/types'

/** 一条 spec 在当前取值下该不该显示 */
export function isOptionVisible(
  spec: ConversionOptionSpec,
  values: Readonly<Record<string, string>>,
): boolean {
  const conditions = spec.visible_when
  if (!conditions) return true

  for (const [dependency, expected] of Object.entries(conditions)) {
    const actual = values[dependency]
    // 依赖项没值 = 不成立。服务端对「依赖项缺失」是跳过检查，我们更严一点：
    // 依赖项都还没选，这个子项本来也不该冒出来
    if (actual === undefined || actual !== String(expected)) return false
  }
  return true
}

/**
 * 一条 spec 可选的值。
 *
 * `dynamic` 非空时真实值域只有运行期才知道（目前只有字体：
 * 服务器上没装的字体不该出现在选项里），由调用方从 `/api/config` 拿。
 * **动态项拿不到值域时返回空数组** —— 那时界面不渲染这一项，
 * 而不是渲染一个空下拉框。
 */
export function choicesFor(
  spec: ConversionOptionSpec,
  dynamicValues: Readonly<Record<string, ConversionOptionEnumValue[]>>,
): ConversionOptionEnumValue[] {
  if (spec.dynamic) return dynamicValues[spec.dynamic] ?? []
  return spec.enum ?? []
}

/** 表单初值：所有项的默认值，一律转成字符串 */
export function initialValues(
  schema: ConversionOptionsSchema | null,
  dynamicValues: Readonly<Record<string, ConversionOptionEnumValue[]>>,
): Record<string, string> {
  const values: Record<string, string> = {}
  for (const spec of schema?.items ?? []) {
    // 动态项的值域是空的（服务器没装任何字体）时不要填默认值 ——
    // 填了就会提交一个界面上根本没有的取值
    if (spec.dynamic && choicesFor(spec, dynamicValues).length === 0) continue
    values[spec.key] = toWire(spec.default)
  }

  // 默认值之间也可能满足 visible_when（例如 resize.mode 默认 original，
  // 于是 resize.width 默认不显示）。这里不再做二次清理：提交时的
  // buildPayload 会再过滤一次，界面渲染也按 visible 过滤。
  return values
}

/** 把 spec 的默认值转成线上格式的字符串 */
function toWire(value: unknown): string {
  if (value === null || value === undefined) return ''
  if (typeof value === 'boolean') return value ? 'true' : 'false'
  return String(value)
}

/**
 * 收敛成提交用的 `options`。
 *
 * **只留当前可见、且值非空的项**。空串要丢掉：那是「用户清空了输入框」，
 * 不是「用户选了空」—— 服务端对空串会按类型各报一次错（`必须是数字`），
 * 而用户什么都没做错。
 */
export function buildPayload(
  schema: ConversionOptionsSchema | null,
  values: Readonly<Record<string, string>>,
  dynamicValues: Readonly<Record<string, ConversionOptionEnumValue[]>>,
): Record<string, string> {
  const payload: Record<string, string> = {}
  for (const spec of schema?.items ?? []) {
    if (!isOptionVisible(spec, values)) continue
    if (spec.dynamic && choicesFor(spec, dynamicValues).length === 0) continue
    const raw = values[spec.key]
    if (raw === undefined || raw === '') continue
    payload[spec.key] = raw
  }
  return payload
}

/**
 * 界面上按顺序该画哪些项。
 *
 * 一并把「动态项没有值域」的过滤掉 —— 否则渲染层每个控件都要记着
 * 判断一次，漏一处就是一个点不开的空控件。
 */
export function visibleItems(
  schema: ConversionOptionsSchema | null,
  values: Readonly<Record<string, string>>,
  dynamicValues: Readonly<Record<string, ConversionOptionEnumValue[]>>,
): ConversionOptionSpec[] {
  return (schema?.items ?? []).filter(
    (spec) =>
      isOptionVisible(spec, values) &&
      (!spec.dynamic || choicesFor(spec, dynamicValues).length > 0),
  )
}

/** 当前值对应的中文名（折叠摘要、结果卡回显用）；enum 之外直接回显数值 + 单位 */
export function labelFor(
  spec: ConversionOptionSpec,
  value: string | undefined,
  dynamicValues: Readonly<Record<string, ConversionOptionEnumValue[]>>,
): string {
  if (value === undefined || value === '') return ''
  const choice = choicesFor(spec, dynamicValues).find((item) => item.value === value)
  if (choice) return choice.label
  return spec.unit ? `${value}${spec.unit}` : value
}

/** 把当前取值夹进 `min`/`max`（步进器用） */
export function clampToSpec(spec: ConversionOptionSpec, input: string): string {
  const numeric = Number(input)
  if (!Number.isFinite(numeric)) return input
  let next = numeric
  if (spec.min !== undefined && next < spec.min) next = spec.min
  if (spec.max !== undefined && next > spec.max) next = spec.max
  return String(spec.type === 'integer' ? Math.round(next) : next)
}
