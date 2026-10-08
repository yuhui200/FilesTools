/**
 * 历史（规格 §十九）。
 *
 * 本地记录，**只有元数据**。结果本体在服务器的临时目录里，下载过一次或
 * 过了保留期就取不到了 —— 所以列表里的「结果已失效」是**如实显示**，
 * 不是错误状态：点进去仍然能看到当初转了什么、参数是什么。
 */

import { useFocusEffect, useRouter } from 'expo-router'
import React, { useCallback, useState } from 'react'
import { FlatList, Pressable, StyleSheet, View } from 'react-native'

import { AppText } from '@/components/AppText'
import { Card } from '@/components/Card'
import { Screen } from '@/components/Screen'
import { EmptyView } from '@/components/States'
import { clearHistory, loadHistory, removeHistoryEntry } from '@/services/history'
import { GUTTER, RADIUS, SPACING, TOUCH_TARGET, useColors } from '@/theme'
import type { HistoryEntry } from '@/types'
import { STATUS_LABELS, batchTitle, formatRelative, statusTone } from '@/utils/labels'

export default function HistoryScreen() {
  const colors = useColors()
  const router = useRouter()
  const [entries, setEntries] = useState<HistoryEntry[]>([])
  const [loaded, setLoaded] = useState(false)

  const refresh = useCallback(async () => {
    setEntries(await loadHistory())
    setLoaded(true)
  }, [])

  useFocusEffect(
    useCallback(() => {
      void refresh()
    }, [refresh]),
  )

  const onDelete = useCallback(
    async (batchId: string) => {
      await removeHistoryEntry(batchId)
      await refresh()
    },
    [refresh],
  )

  const onClear = useCallback(async () => {
    await clearHistory()
    await refresh()
  }, [refresh])

  return (
    <Screen
      title="历史"
      headerRight={
        entries.length > 0 ? (
          <Pressable
            accessibilityRole="button"
            accessibilityLabel="清空历史"
            onPress={() => void onClear()}
            style={({ pressed }) => [styles.headerButton, { opacity: pressed ? 0.6 : 1 }]}
          >
            <AppText variant="label" tone="primary">
              清空
            </AppText>
          </Pressable>
        ) : null
      }
    >
      <FlatList
        data={entries}
        keyExtractor={(item) => item.batchId}
        contentContainerStyle={styles.list}
        renderItem={({ item }) => (
          <HistoryCard
            entry={item}
            onOpen={() => router.push(`/task/${item.batchId}`)}
            onDelete={() => void onDelete(item.batchId)}
          />
        )}
        ListEmptyComponent={
          loaded ? (
            <EmptyView
              inline
              title="还没有处理记录"
              hint="在「工具」里选一个工具转换文件，这里就会留下记录。记录只存在这台设备上。"
              actionLabel="去挑工具"
              onAction={() => router.push('/tools')}
            />
          ) : null
        }
      />
    </Screen>
  )
}

function HistoryCard({
  entry,
  onOpen,
  onDelete,
}: {
  entry: HistoryEntry
  onOpen: () => void
  onDelete: () => void
}) {
  const colors = useColors()
  const tone = statusTone(entry.status)

  const toneColor: Record<string, string> = {
    success: colors.success,
    danger: colors.danger,
    primary: colors.primary,
    muted: colors.textMuted,
    warning: colors.warning,
  }

  // ⚠️ **卡片本身不可点，两个按钮是并排的兄弟。**
  //
  // 之前写成 `<Card onPress>` 把整行包成按钮、删除按钮嵌在里面 —— 在 Web 上
  // 那就是 `<button>` 套 `<button>`，是**非法 HTML**：React 会报
  // 「In HTML, <button> cannot be a descendant of <button>」并提示 hydration
  // 会失败；屏幕阅读器与键盘 Tab 也拿不到里面那个删除键（焦点落在外层，
  // 里层形同虚设）。原生端嵌套 Pressable 是合法的，所以这个坑只在 Web 上现形，
  // 但 Web 是这套代码的一个真实目标平台，不能当没看见。
  //
  // 改法：卡片退成普通容器，「打开」的点击区只盖住文字那一块，删除键与它并列。
  return (
    <Card compact style={styles.card}>
      <View style={styles.row}>
        <Pressable
          accessibilityRole="button"
          onPress={onOpen}
          style={({ pressed }) => [styles.main, pressed ? { opacity: 0.65 } : null]}
        >
          <AppText numberOfLines={1}>{batchTitle(entry.filename, entry.total)}</AppText>
          <View style={styles.meta}>
            <AppText variant="caption" tone="faint">
              {entry.sourceType.toUpperCase()} → {entry.targetType.toUpperCase()}
            </AppText>
            <AppText variant="caption" style={{ color: toneColor[tone] }}>
              {STATUS_LABELS[entry.status]}
            </AppText>
          </View>
          <AppText variant="caption" tone="faint" style={{ marginTop: SPACING.xs }}>
            {formatRelative(entry.createdAt)}
            {entry.status === 'completed'
              ? entry.resultExpired
                ? ' · 结果已失效，需重新处理'
                : ' · 结果可下载'
              : ''}
          </AppText>
        </Pressable>

        <Pressable
          accessibilityRole="button"
          accessibilityLabel="删除这条记录"
          onPress={onDelete}
          // 44×44 的点击区：这个按钮在视觉上只是个小叉，但手指需要的那块
          // 面积一点都不能少
          style={({ pressed }) => [
            styles.deleteButton,
            { backgroundColor: colors.surfaceMuted, borderRadius: RADIUS.sm, opacity: pressed ? 0.6 : 1 },
          ]}
        >
          <AppText tone="faint">✕</AppText>
        </Pressable>
      </View>
    </Card>
  )
}

const styles = StyleSheet.create({
  list: { paddingHorizontal: GUTTER, paddingBottom: SPACING.xxl },
  card: { marginBottom: SPACING.sm },
  row: { flexDirection: 'row', alignItems: 'center' },
  // 「打开」那一块自己扛 44 的最小高度 —— 它现在是独立的 Pressable，
  // 不再靠外层 Card 兜底了
  main: { flex: 1, paddingRight: SPACING.sm, minHeight: TOUCH_TARGET, justifyContent: 'center' },
  meta: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: SPACING.sm,
    marginTop: SPACING.xs,
  },
  headerButton: {
    minHeight: TOUCH_TARGET,
    justifyContent: 'center',
    paddingHorizontal: SPACING.sm,
  },
  deleteButton: {
    width: TOUCH_TARGET,
    height: TOUCH_TARGET,
    alignItems: 'center',
    justifyContent: 'center',
  },
})
