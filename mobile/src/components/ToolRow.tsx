/**
 * 工具列表里的一行。
 *
 * 不可用的能力**仍然显示**，只是标注出来 —— 服务器没装 LibreOffice 时，
 * 用户搜「PDF 转 Word」应该看得到它并知道为什么不能用，而不是搜不到、
 * 以为 App 没这个功能。
 */

import React from 'react'
import { StyleSheet, View } from 'react-native'

import { AppText } from './AppText'
import { Card } from './Card'
import { RADIUS, SPACING, useColors } from '@/theme'
import type { ConversionCapability } from '@/types'

export interface ToolRowProps {
  tool: ConversionCapability
  onPress: () => void
  /** 紧凑模式（首页的快捷入口用），不显示说明文字 */
  compact?: boolean
}

export function ToolRow({ tool, onPress, compact = false }: ToolRowProps) {
  const colors = useColors()

  return (
    <Card onPress={onPress} compact style={styles.card}>
      <View style={styles.row}>
        <View style={styles.main}>
          <AppText variant="subheading" tone={tool.available ? 'default' : 'faint'}>
            {tool.display_name}
          </AppText>
          {!compact ? (
            <AppText variant="caption" tone="faint" style={{ marginTop: SPACING.xs }}>
              {tool.source_type.toUpperCase()} → {tool.target_type.toUpperCase()}
            </AppText>
          ) : null}
        </View>

        {tool.available ? (
          <AppText variant="subheading" tone="faint">
            ›
          </AppText>
        ) : (
          <View
            style={[
              styles.badge,
              { backgroundColor: colors.surfaceMuted, borderRadius: RADIUS.pill },
            ]}
          >
            <AppText variant="caption" tone="faint">
              当前不可用
            </AppText>
          </View>
        )}
      </View>

      {/* note 是服务端给的「需要说给用户听的一句话」，必须照原样显示 ——
          它通常是一个真实限制（多帧文件只取第一帧），不是装饰 */}
      {!compact && tool.note ? (
        <AppText variant="caption" tone="warning" style={{ marginTop: SPACING.sm }}>
          {tool.note}
        </AppText>
      ) : null}
    </Card>
  )
}

const styles = StyleSheet.create({
  card: { marginBottom: SPACING.sm },
  row: { flexDirection: 'row', alignItems: 'center' },
  main: { flex: 1, paddingRight: SPACING.sm },
  badge: { paddingHorizontal: SPACING.sm, paddingVertical: SPACING.xs },
})
