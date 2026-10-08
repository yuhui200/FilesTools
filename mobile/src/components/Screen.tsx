/**
 * 页面外壳。
 *
 * 三件事统一在这里做，页面自己不许重复：
 * 1. 底色跟随主题（深色模式下漏一个页面就是白闪一下）
 * 2. 顶部避开刘海、底部避开 Home Indicator
 * 3. 左右留白 `GUTTER`
 *
 * 不含滚动 —— 有的页面要 `FlatList`、有的要 `ScrollView`、有的要固定头部，
 * 把滚动塞进来就得为这三种情况各开一个开关。
 */

import React from 'react'
import { StyleSheet, View, type StyleProp, type ViewStyle } from 'react-native'
import { useSafeAreaInsets } from 'react-native-safe-area-context'

import { AppText } from './AppText'
import { GUTTER, SPACING, useColors } from '@/theme'

export interface ScreenProps {
  children: React.ReactNode
  /** 大标题，显示在内容上方 */
  title?: string
  /** 标题右边的东西（刷新按钮之类） */
  headerRight?: React.ReactNode
  /** 关掉左右留白（整宽列表用） */
  bleed?: boolean
  /** 关掉底部留白（列表自带 paddingBottom 时用） */
  noBottomInset?: boolean
  style?: StyleProp<ViewStyle>
}

export function Screen({
  children,
  title,
  headerRight,
  bleed = false,
  noBottomInset = false,
  style,
}: ScreenProps) {
  const colors = useColors()
  const insets = useSafeAreaInsets()

  return (
    <View
      style={[
        styles.root,
        {
          backgroundColor: colors.background,
          paddingTop: insets.top,
          paddingBottom: noBottomInset ? 0 : insets.bottom,
        },
        style,
      ]}
    >
      {title ? (
        <View style={styles.header}>
          <AppText variant="title">{title}</AppText>
          {headerRight ? <View style={styles.headerRight}>{headerRight}</View> : null}
        </View>
      ) : null}
      <View style={[styles.body, bleed ? null : { paddingHorizontal: GUTTER }]}>{children}</View>
    </View>
  )
}

const styles = StyleSheet.create({
  root: { flex: 1 },
  header: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    paddingHorizontal: GUTTER,
    paddingTop: SPACING.sm,
    paddingBottom: SPACING.md,
  },
  headerRight: { flexDirection: 'row', alignItems: 'center' },
  body: { flex: 1 },
})
