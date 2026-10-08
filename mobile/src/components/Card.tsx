/**
 * 卡片与分组标题。
 *
 * 深色模式下卡片的边界靠 `border` 而不是阴影 —— 阴影在深底上几乎看不见，
 * 用户看到的是一堆糊在一起的方块。
 */

import React from 'react'
import { Pressable, StyleSheet, View, type StyleProp, type ViewStyle } from 'react-native'

import { AppText } from './AppText'
import { RADIUS, SPACING, TOUCH_TARGET, useColors } from '@/theme'

export interface CardProps {
  children: React.ReactNode
  /** 可点时包一层 Pressable，且保证 ≥44 高 */
  onPress?: () => void
  style?: StyleProp<ViewStyle>
  /** 紧凑内边距（列表项用） */
  compact?: boolean
}

export function Card({ children, onPress, style, compact = false }: CardProps) {
  const colors = useColors()
  const skin: StyleProp<ViewStyle> = [
    styles.card,
    {
      backgroundColor: colors.surface,
      borderColor: colors.border,
      padding: compact ? SPACING.md : SPACING.lg,
    },
    style,
  ]

  if (!onPress) return <View style={skin}>{children}</View>

  return (
    <Pressable
      accessibilityRole="button"
      onPress={onPress}
      style={({ pressed }) => [skin, pressed ? { opacity: 0.75 } : null]}
    >
      {children}
    </Pressable>
  )
}

/** 分组标题（「图片」「文档」这种） */
export function SectionHeader({ title, hint }: { title: string; hint?: string }) {
  const colors = useColors()
  return (
    <View style={styles.sectionHeader}>
      <AppText variant="label" tone="muted" bold>
        {title}
      </AppText>
      {hint ? (
        <AppText variant="caption" tone="faint" style={{ marginLeft: SPACING.sm }}>
          {hint}
        </AppText>
      ) : null}
      <View style={[styles.rule, { backgroundColor: colors.divider }]} />
    </View>
  )
}

const styles = StyleSheet.create({
  card: {
    borderRadius: RADIUS.md,
    borderWidth: StyleSheet.hairlineWidth * 2,
    minHeight: TOUCH_TARGET,
  },
  sectionHeader: {
    flexDirection: 'row',
    alignItems: 'center',
    marginTop: SPACING.lg,
    marginBottom: SPACING.sm,
  },
  rule: { flex: 1, height: 1, marginLeft: SPACING.sm },
})
