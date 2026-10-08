/**
 * 文字。
 *
 * 存在的理由只有一个：**不让每个页面自己去 `useColors()` 再拼一遍颜色**。
 * 规格 §二十三 要深色模式，如果每处 `<Text>` 都写 `{ color: colors.text }`，
 * 漏掉一处就是深色底上一行黑字。
 */

import React from 'react'
import { Text, type TextProps, type TextStyle } from 'react-native'

import { FONT, useColors } from '@/theme'

type Variant = 'title' | 'heading' | 'subheading' | 'body' | 'label' | 'caption'

/** 语义色。名字按**用途**取，不按颜色取 —— 深色模式下 primary 并不是同一个色值 */
export type Tone = 'default' | 'muted' | 'faint' | 'primary' | 'danger' | 'success' | 'warning' | 'onPrimary'

const SIZE: Record<Variant, number> = {
  title: FONT.title,
  heading: FONT.heading,
  subheading: FONT.subheading,
  body: FONT.body,
  label: FONT.label,
  caption: FONT.caption,
}

const WEIGHT: Partial<Record<Variant, TextStyle['fontWeight']>> = {
  title: '700',
  heading: '600',
  subheading: '600',
  label: '500',
}

export interface AppTextProps extends TextProps {
  variant?: Variant
  tone?: Tone
  /** 加粗的强调（比换 variant 更轻，用于行内） */
  bold?: boolean
  center?: boolean
}

export function AppText({
  variant = 'body',
  tone = 'default',
  bold = false,
  center = false,
  style,
  ...rest
}: AppTextProps) {
  const colors = useColors()

  const TONE: Record<Tone, string> = {
    default: colors.text,
    muted: colors.textMuted,
    faint: colors.textFaint,
    primary: colors.primary,
    danger: colors.danger,
    success: colors.success,
    warning: colors.warning,
    onPrimary: colors.onPrimary,
  }

  return (
    <Text
      {...rest}
      style={[
        { fontSize: SIZE[variant], color: TONE[tone], lineHeight: Math.round(SIZE[variant] * 1.45) },
        WEIGHT[variant] ? { fontWeight: WEIGHT[variant] } : null,
        bold ? { fontWeight: '700' } : null,
        center ? { textAlign: 'center' } : null,
        style,
      ]}
    />
  )
}
