/**
 * 工具页（规格 §七）。
 *
 * 列表、分类、搜索**全部由服务端的能力表派生**。分类标签取自响应里的
 * `categories`，不在这里写「图片 / 文档 / PDF」这三个词 —— 服务端将来
 * 加一个 audio 分类，这里自动就有那个标签。
 */

import { useRouter } from 'expo-router'
import React, { useCallback, useMemo, useState } from 'react'
import { FlatList, Pressable, RefreshControl, StyleSheet, TextInput, View } from 'react-native'

import { AppText } from '@/components/AppText'
import { Screen } from '@/components/Screen'
import { EmptyView, ErrorView, LoadingView } from '@/components/States'
import { ToolRow } from '@/components/ToolRow'
import { useCapabilities } from '@/state/CapabilitiesProvider'
import { GUTTER, RADIUS, SPACING, TOUCH_TARGET, useColors } from '@/theme'
import type { ConversionCapability } from '@/types'

export default function ToolsScreen() {
  const colors = useColors()
  const router = useRouter()
  const { tools, groups, loading, error, reload } = useCapabilities()

  const [query, setQuery] = useState('')
  const [category, setCategory] = useState<string | null>(null)
  const [refreshing, setRefreshing] = useState(false)

  const filtered = useMemo<ConversionCapability[]>(() => {
    const text = query.trim().toLowerCase()
    return tools.filter((tool) => {
      if (category && tool.category !== category) return false
      if (text === '') return true
      // 三个字段一起搜：用户可能打「PDF」（显示名）、「docx」（目标）、
      // 「word」（显示名里的词），只搜显示名会漏掉后两种
      return (
        tool.display_name.toLowerCase().includes(text) ||
        tool.source_type.toLowerCase().includes(text) ||
        tool.target_type.toLowerCase().includes(text)
      )
    })
  }, [tools, category, query])

  const onRefresh = useCallback(async () => {
    setRefreshing(true)
    await reload()
    setRefreshing(false)
  }, [reload])

  if (loading && tools.length === 0) {
    return (
      <Screen title="工具">
        <LoadingView label="正在读取服务器支持的功能…" />
      </Screen>
    )
  }

  if (error && tools.length === 0) {
    return (
      <Screen title="工具">
        <ErrorView error={error} onRetry={() => void reload()} />
      </Screen>
    )
  }

  return (
    <Screen title="工具">
      <View style={styles.filters}>
        <TextInput
          value={query}
          onChangeText={setQuery}
          placeholder="搜索格式或工具名"
          placeholderTextColor={colors.textFaint}
          accessibilityLabel="搜索工具"
          style={[
            styles.search,
            {
              color: colors.text,
              backgroundColor: colors.surface,
              borderColor: colors.border,
              borderRadius: RADIUS.sm,
            },
          ]}
        />

        <View style={styles.chips}>
          <FilterChip
            label="全部"
            selected={category === null}
            onPress={() => setCategory(null)}
          />
          {groups.map((group) => (
            <FilterChip
              key={group.category.value}
              label={group.category.label}
              selected={category === group.category.value}
              onPress={() => setCategory(group.category.value)}
            />
          ))}
        </View>
      </View>

      <FlatList
        data={filtered}
        keyExtractor={(item) => item.id}
        contentContainerStyle={styles.list}
        keyboardShouldPersistTaps="handled"
        refreshControl={
          <RefreshControl refreshing={refreshing} onRefresh={() => void onRefresh()} tintColor={colors.primary} />
        }
        renderItem={({ item }) => (
          <ToolRow tool={item} onPress={() => router.push(`/convert/${item.id}`)} />
        )}
        ListEmptyComponent={
          <EmptyView
            inline
            title={query.trim() === '' ? '这类里还没有工具' : `没有匹配「${query.trim()}」的工具`}
            hint={
              query.trim() === ''
                ? '换一个分类看看。'
                : '换个关键词试试，比如只打格式名：docx、png。'
            }
          />
        }
      />
    </Screen>
  )
}

function FilterChip({
  label,
  selected,
  onPress,
}: {
  label: string
  selected: boolean
  onPress: () => void
}) {
  const colors = useColors()
  return (
    <Pressable
      accessibilityRole="radio"
      accessibilityState={{ selected }}
      onPress={onPress}
      style={({ pressed }) => [
        styles.chip,
        {
          backgroundColor: selected ? colors.primarySoft : colors.surface,
          borderColor: selected ? colors.primary : colors.border,
          opacity: pressed ? 0.7 : 1,
        },
      ]}
    >
      <AppText variant="label" tone={selected ? 'primary' : 'muted'} bold={selected}>
        {label}
      </AppText>
    </Pressable>
  )
}

const styles = StyleSheet.create({
  filters: { paddingHorizontal: GUTTER, paddingBottom: SPACING.sm },
  search: {
    minHeight: TOUCH_TARGET,
    borderWidth: 1,
    paddingHorizontal: SPACING.md,
  },
  chips: { flexDirection: 'row', flexWrap: 'wrap', marginTop: SPACING.sm },
  chip: {
    minHeight: TOUCH_TARGET,
    justifyContent: 'center',
    paddingHorizontal: SPACING.md,
    marginRight: SPACING.sm,
    marginBottom: SPACING.sm,
    borderWidth: 1,
    borderRadius: RADIUS.pill,
  },
  list: { paddingHorizontal: GUTTER, paddingBottom: SPACING.xxl },
})
