/**
 * 首页。
 *
 * 内容**全部从服务端派生**：能做什么、有多少条、哪些不可用，都来自
 * `/api/conversion/capabilities`。这里一个格式名都不写死 ——
 * 服务器加了 GIF→PNG，首页不用改代码、不用发版。
 */

import { useFocusEffect, useRouter } from 'expo-router'
import React, { useCallback, useMemo, useState } from 'react'
import { RefreshControl, ScrollView, StyleSheet, View } from 'react-native'

import { AppText } from '@/components/AppText'
import { Button } from '@/components/Button'
import { Card, SectionHeader } from '@/components/Card'
import { Screen } from '@/components/Screen'
import { ErrorView, LoadingView } from '@/components/States'
import { ToolRow } from '@/components/ToolRow'
import { ApiError, CLIENT_CODES, isApiConfigured } from '@/services/api/client'
import { loadHistory } from '@/services/history'
import { useCapabilities } from '@/state/CapabilitiesProvider'
import { GUTTER, RADIUS, SPACING, useColors } from '@/theme'
import type { HistoryEntry } from '@/types'
import { STATUS_LABELS, batchTitle, formatRelative } from '@/utils/labels'

/** 首页列几个快捷入口。不够就少列几个，**不凑数** */
const QUICK_LIMIT = 6

export default function HomeScreen() {
  const colors = useColors()
  const router = useRouter()
  const { tools, capabilities, loading, error, reload } = useCapabilities()
  const [recent, setRecent] = useState<HistoryEntry[]>([])
  const [refreshing, setRefreshing] = useState(false)

  // 每次回到这个 Tab 都重读历史：用户刚刚在别的页面转完一个文件，
  // 切回来应该立刻看到它。用 effect 而不是 focus 事件是因为
  // Tab 一直是挂载着的，effect 不会重跑。
  useFocusEffect(
    useCallback(() => {
      let alive = true
      void loadHistory().then((list) => {
        if (alive) setRecent(list.slice(0, 3))
      })
      return () => {
        alive = false
      }
    }, []),
  )

  const quickTools = useMemo(() => {
    const available = tools.filter((tool) => tool.available)
    // 服务端标了 recommended 的排前面（tags 是能力自带的，不是这里编的）
    const recommended = available.filter((tool) => tool.tags.includes('recommended'))
    const rest = available.filter((tool) => !tool.tags.includes('recommended'))
    return [...recommended, ...rest].slice(0, QUICK_LIMIT)
  }, [tools])

  const onRefresh = useCallback(async () => {
    setRefreshing(true)
    await reload()
    setRecent((await loadHistory()).slice(0, 3))
    setRefreshing(false)
  }, [reload])

  if (!isApiConfigured) {
    // 没配地址时整个 App 都不能用，首页必须第一个说清楚，而不是显示空列表
    return (
      <Screen title="FileTools">
        <ErrorView error={new ApiError('没有配置服务器地址', CLIENT_CODES.config)} />
      </Screen>
    )
  }

  if (loading && tools.length === 0) {
    return (
      <Screen title="FileTools">
        <LoadingView label="正在读取服务器支持的功能…" />
      </Screen>
    )
  }

  if (error && tools.length === 0) {
    return (
      <Screen title="FileTools">
        <ErrorView error={error} onRetry={() => void reload()} />
      </Screen>
    )
  }

  const unavailable = tools.length - tools.filter((tool) => tool.available).length

  return (
    <Screen title="FileTools">
      <ScrollView
        contentContainerStyle={styles.content}
        refreshControl={
          <RefreshControl refreshing={refreshing} onRefresh={() => void onRefresh()} tintColor={colors.primary} />
        }
      >
        <View style={[styles.hero, { backgroundColor: colors.primary, borderRadius: RADIUS.lg }]}>
          <AppText variant="heading" style={{ color: colors.onPrimary }} bold>
            换个格式，就这么简单
          </AppText>
          <AppText style={{ color: colors.onPrimary, marginTop: SPACING.sm, opacity: 0.9 }}>
            {capabilities
              ? `服务器现在可以做 ${tools.filter((tool) => tool.available).length} 种转换。`
              : '正在读取服务器支持的功能…'}
            {unavailable > 0 ? `另有 ${unavailable} 种因服务器缺少组件暂时不可用。` : ''}
          </AppText>
          <Button
            label="选一个工具开始"
            variant="secondary"
            block
            style={{ marginTop: SPACING.lg }}
            onPress={() => router.push('/tools')}
          />
        </View>

        {quickTools.length > 0 ? (
          <>
            <SectionHeader title="常用" />
            {quickTools.map((tool) => (
              <ToolRow
                key={tool.id}
                tool={tool}
                compact
                onPress={() => router.push(`/convert/${tool.id}`)}
              />
            ))}
          </>
        ) : null}

        {recent.length > 0 ? (
          <>
            <SectionHeader title="最近处理" hint="只保留最近 50 条" />
            {recent.map((entry) => (
              <Card
                key={entry.batchId}
                compact
                style={styles.recentCard}
                onPress={() => router.push(`/task/${entry.batchId}`)}
              >
                <AppText numberOfLines={1}>{batchTitle(entry.filename, entry.total)}</AppText>
                <View style={styles.recentMeta}>
                  <AppText variant="caption" tone="faint">
                    {entry.sourceType.toUpperCase()} → {entry.targetType.toUpperCase()}
                  </AppText>
                  <AppText variant="caption" tone="faint">
                    {STATUS_LABELS[entry.status]} · {formatRelative(entry.createdAt)}
                  </AppText>
                </View>
              </Card>
            ))}
          </>
        ) : (
          <>
            <SectionHeader title="最近处理" />
            <Card>
              <AppText tone="muted">还没有处理过文件。上面挑一个工具试试。</AppText>
            </Card>
          </>
        )}

        {capabilities && capabilities.notes.length > 0 ? (
          <>
            <SectionHeader title="服务器说明" />
            <Card>
              {capabilities.notes.map((note) => (
                <AppText key={note} variant="caption" tone="muted" style={styles.note}>
                  · {note}
                </AppText>
              ))}
            </Card>
          </>
        ) : null}
      </ScrollView>
    </Screen>
  )
}

const styles = StyleSheet.create({
  content: { paddingHorizontal: GUTTER, paddingBottom: SPACING.xxl },
  hero: { padding: SPACING.xl, marginTop: SPACING.sm },
  recentCard: { marginBottom: SPACING.sm },
  recentMeta: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    marginTop: SPACING.xs,
  },
  note: { marginBottom: SPACING.xs },
})
