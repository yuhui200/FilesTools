/**
 * 按钮（规格 §二十三：可点区域 ≥ 44×44）。
 *
 * 高度直接取 :data:`TOUCH_TARGET`，四个变体共用一套尺寸 —— 不在各页面里
 * 写 `paddingVertical` 那种「大概够大」的做法，那种做法在真机上总有几个
 * 按钮差几个像素点不中。
 */

import React from 'react'
import {
  ActivityIndicator,
  Pressable,
  StyleSheet,
  View,
  type StyleProp,
  type ViewStyle,
} from 'react-native'

import { AppText } from './AppText'
import { RADIUS, SPACING, TOUCH_TARGET, useColors } from '@/theme'

export type ButtonVariant = 'primary' | 'secondary' | 'ghost' | 'danger'

export interface ButtonProps {
  label: string
  onPress: () => void
  variant?: ButtonVariant
  disabled?: boolean
  /** 转圈并屏蔽点击。**不改变按钮宽度**，否则一按就跳一下 */
  loading?: boolean
  /** 铺满一行 */
  block?: boolean
  style?: StyleProp<ViewStyle>
}

export function Button({
  label,
  onPress,
  variant = 'primary',
  disabled = false,
  loading = false,
  block = false,
  style,
}: ButtonProps) {
  const colors = useColors()
  const inactive = disabled || loading

  const palette: Record<ButtonVariant, { background: string; border: string; text: string }> = {
    primary: { background: colors.primary, border: colors.primary, text: colors.onPrimary },
    secondary: { background: colors.surface, border: colors.border, text: colors.text },
    ghost: { background: 'transparent', border: 'transparent', text: colors.primary },
    danger: { background: colors.danger, border: colors.danger, text: '#ffffff' },
  }
  const skin = palette[variant]

  return (
    <Pressable
      accessibilityRole="button"
      accessibilityLabel={label}
      accessibilityState={{ disabled: inactive, busy: loading }}
      disabled={inactive}
      onPress={onPress}
      style={({ pressed }) => [
        styles.base,
        {
          backgroundColor: skin.background,
          borderColor: skin.border,
          // 按下态：主色压深一档。禁用态整体降透明度，不换成另一种灰 ——
          // 换色会让「不可用」看起来像「另一种按钮」
          opacity: inactive ? 0.45 : 1,
          ...(pressed && !inactive && variant === 'primary'
            ? { backgroundColor: colors.primaryPressed }
            : {}),
          ...(pressed && !inactive && variant !== 'primary' ? { opacity: 0.7 } : {}),
        },
        block ? styles.block : null,
        style,
      ]}
    >
      <View style={styles.content}>
        {loading ? <ActivityIndicator size="small" color={skin.text} /> : null}
        <AppText
          variant="subheading"
          bold
          style={{ color: skin.text, marginLeft: loading ? SPACING.sm : 0 }}
        >
          {label}
        </AppText>
      </View>
    </Pressable>
  )
}

const styles = StyleSheet.create({
  base: {
    minHeight: TOUCH_TARGET,
    borderRadius: RADIUS.md,
    borderWidth: 1,
    paddingHorizontal: SPACING.lg,
    justifyContent: 'center',
    alignItems: 'center',
  },
  block: { alignSelf: 'stretch' },
  content: { flexDirection: 'row', alignItems: 'center' },
})
