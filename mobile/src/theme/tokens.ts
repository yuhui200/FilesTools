/**
 * 设计令牌。
 *
 * 色板与 Web 端（`frontend/tailwind.config.js` 的 `brand` = Tailwind indigo）
 * **是同一套**：规格 §二十四 允许复用颜色与品牌，但要求 UI 重新实现 ——
 * 所以这里给的是数值，不是 Tailwind 类名。
 *
 * 深浅两套不是把浅色反过来：深色下的主色要往亮里挪一档（indigo-600 → 400），
 * 否则 #4f46e5 压在深底上对比度不够，按钮会糊成一块。
 */

export const BRAND = {
  50: '#eef2ff',
  100: '#e0e7ff',
  200: '#c7d2fe',
  300: '#a5b4fc',
  400: '#818cf8',
  500: '#6366f1',
  600: '#4f46e5',
  700: '#4338ca',
  800: '#3730a3',
  900: '#312e81',
} as const

export interface Palette {
  /** 页面底色 */
  background: string
  /** 卡片 / 列表项底色 */
  surface: string
  /** 次级块（输入框底、分组头） */
  surfaceMuted: string
  border: string
  /** 分隔线，比 border 更淡 */
  divider: string
  text: string
  textMuted: string
  textFaint: string
  primary: string
  /** 主色按下态 */
  primaryPressed: string
  /** 压在主色上的文字 */
  onPrimary: string
  /** 主色的浅底（标签、选中态背景） */
  primarySoft: string
  /** primarySoft 上的文字 */
  onPrimarySoft: string
  danger: string
  dangerSoft: string
  success: string
  successSoft: string
  warning: string
  warningSoft: string
  /** 进度条 / 骨架屏的空槽 */
  track: string
  /** 半透明遮罩 */
  overlay: string
}

export const LIGHT: Palette = {
  background: '#f8fafc',
  surface: '#ffffff',
  surfaceMuted: '#f1f5f9',
  border: '#e2e8f0',
  divider: '#f1f5f9',
  text: '#0f172a',
  textMuted: '#475569',
  textFaint: '#94a3b8',
  primary: BRAND[600],
  primaryPressed: BRAND[700],
  onPrimary: '#ffffff',
  primarySoft: BRAND[50],
  onPrimarySoft: BRAND[700],
  danger: '#dc2626',
  dangerSoft: '#fef2f2',
  success: '#15803d',
  successSoft: '#f0fdf4',
  warning: '#b45309',
  warningSoft: '#fffbeb',
  track: '#e2e8f0',
  overlay: 'rgba(15, 23, 42, 0.45)',
}

export const DARK: Palette = {
  background: '#0b1120',
  surface: '#1e293b',
  surfaceMuted: '#334155',
  border: '#334155',
  divider: '#1e293b',
  text: '#f1f5f9',
  textMuted: '#cbd5e1',
  textFaint: '#94a3b8',
  primary: BRAND[500],
  primaryPressed: BRAND[400],
  onPrimary: '#ffffff',
  primarySoft: '#312e81',
  onPrimarySoft: BRAND[200],
  danger: '#f87171',
  dangerSoft: '#3f1d1d',
  success: '#4ade80',
  successSoft: '#14301f',
  warning: '#fbbf24',
  warningSoft: '#3a2c0c',
  track: '#334155',
  overlay: 'rgba(2, 6, 23, 0.6)',
}

/** 间距刻度（4 的倍数）。规格 §二十三 要「高信息密度但不拥挤」。 */
export const SPACING = {
  xs: 4,
  sm: 8,
  md: 12,
  lg: 16,
  xl: 24,
  xxl: 32,
} as const

export const RADIUS = {
  sm: 8,
  md: 12,
  lg: 16,
  pill: 999,
} as const

export const FONT = {
  title: 28,
  heading: 20,
  subheading: 17,
  body: 15,
  label: 13,
  caption: 12,
} as const

/**
 * 最小可点区域（规格 §二十三 要求 ≥ 44×44）。
 *
 * 所有可点组件的 `minHeight` 都取自这里，**不写裸数字** ——
 * 44 这个数出现过七八次之后，改一次就会漏掉几处。
 */
export const TOUCH_TARGET = 44

/** 页面左右留白 */
export const GUTTER = SPACING.lg
