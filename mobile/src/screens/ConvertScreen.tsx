/**
 * 工具详情：选文件 → 调参数 → 提交（规格 §十三 / §十五）。
 *
 * 这一页是整条链路的收敛点，也是**唯一**把「能力 → 文件 → 参数 → 任务」
 * 串起来的地方。参数面板完全由这条能力的 `options_schema` 渲染，
 * 提交时按同一份 schema 序列化 —— 渲染与提交读的是同一份数据，
 * 中间没有第二处硬编码的键名（规格 §二十五）。
 */

import { Stack, useLocalSearchParams, useRouter } from 'expo-router'
import React, { useCallback, useMemo, useState } from 'react'
import { Pressable, ScrollView, StyleSheet, View } from 'react-native'

import { AppText } from '@/components/AppText'
import { Button } from '@/components/Button'
import { Card, SectionHeader } from '@/components/Card'
import { OptionForm } from '@/components/OptionForm'
import { Screen } from '@/components/Screen'
import { EmptyView, ErrorView, LoadingView, Notice } from '@/components/States'
import { submitConversion } from '@/services/api/conversion'
import { pickFiles } from '@/services/filePicker'
import { recordSubmission } from '@/services/history'
import { useCapabilities } from '@/state/CapabilitiesProvider'
import { RADIUS, SPACING, TOUCH_TARGET, useColors } from '@/theme'
import type { ConversionOptionEnumValue, MobileFile } from '@/types'
import { formatBytes } from '@/utils/labels'
import { buildPayload, initialValues } from '@/utils/options'

export default function ConvertScreen() {
  const colors = useColors()
  const router = useRouter()
  const params = useLocalSearchParams<{ capabilityId: string | string[] }>()
  const capabilityId = Array.isArray(params.capabilityId) ? params.capabilityId[0] : params.capabilityId

  const { findTool, fontOptions, loading } = useCapabilities()
  const tool = findTool(capabilityId ?? '')

  const dynamicValues = useMemo<Record<string, ConversionOptionEnumValue[]>>(
    () => ({ fonts: fontOptions }),
    [fontOptions],
  )

  const [files, setFiles] = useState<MobileFile[]>([])
  const [values, setValues] = useState<Record<string, string> | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const [failure, setFailure] = useState<unknown>(null)

  // 初值依赖 schema，而 schema 要等能力表加载完才有 —— 所以用「没算过就算一次」
  // 而不是 useEffect：能力表在 App 启动时就拉好了，进到这一页通常已经在内存里，
  // 用 render 期计算可以少一帧空白
  const effectiveValues = useMemo(
    () => values ?? initialValues(tool?.options_schema ?? null, dynamicValues),
    [values, tool, dynamicValues],
  )

  const onChange = useCallback(
    (key: string, value: string) => {
      setValues({ ...effectiveValues, [key]: value })
    },
    [effectiveValues],
  )

  const onPick = useCallback(async () => {
    setFailure(null)
    try {
      const picked = await pickFiles({ multiple: true })
      if (picked.length === 0) return
      setFiles((previous) => [...previous, ...picked])
    } catch (caught) {
      setFailure(caught)
    }
  }, [])

  const onSubmit = useCallback(async () => {
    if (!tool || files.length === 0) return
    setSubmitting(true)
    setFailure(null)
    try {
      const batch = await submitConversion({
        files,
        targetType: tool.target_type,
        capabilityId: tool.id,
        options: buildPayload(tool.options_schema, effectiveValues, dynamicValues),
      })

      // 先记历史再跳转：跳过去之后这一页就卸载了，历史写不进去用户
      // 就再也找不到这次转换。写失败**不阻断**跳转 —— 任务已经在服务器上跑了
      await recordSubmission({
        batchId: batch.batch_id,
        filename: files[0]?.name ?? '未命名文件',
        sourceType: tool.source_type,
        targetType: tool.target_type,
        capabilityId: tool.id,
        total: batch.total,
        status: 'queued',
        createdAt: Date.now(),
        resultFilename: null,
        resultExpired: false,
      })

      // replace 而不是 push：返回时应该回到工具列表，而不是又掉进这个表单
      router.replace(`/task/${batch.batch_id}`)
    } catch (caught) {
      setFailure(caught)
    } finally {
      setSubmitting(false)
    }
  }, [tool, files, effectiveValues, dynamicValues, router])

  if (loading && !tool) {
    return (
      <Screen title="设置">
        <LoadingView label="正在读取工具信息…" />
      </Screen>
    )
  }

  if (!tool) {
    return (
      <Screen title="设置">
        <EmptyView
          title="找不到这个工具"
          hint="它可能已经下线，或者这条链接来自旧版本。回工具列表重新选一个吧。"
          actionLabel="回到工具列表"
          onAction={() => router.replace('/tools')}
        />
      </Screen>
    )
  }

  const canSubmit = files.length > 0 && tool.available && !submitting

  return (
    <>
      <Stack.Screen options={{ title: tool.display_name }} />
      <Screen noBottomInset>
        <ScrollView contentContainerStyle={styles.content} keyboardShouldPersistTaps="handled">
          <AppText variant="caption" tone="faint">
            {tool.source_type.toUpperCase()} → {tool.target_type.toUpperCase()}
          </AppText>

          {tool.note ? <Notice text={tool.note} tone="warning" /> : null}

          {!tool.available ? (
            <Notice
              text="这个转换现在不能用：服务器缺少对应的组件。重新上传同样会失败。"
              tone="danger"
            />
          ) : null}

          <SectionHeader title="文件" hint={`已选 ${files.length} 个`} />
          {files.length === 0 ? (
            <Card>
              <AppText tone="muted">
                还没有选文件。可以一次选多个，服务器会逐个处理，其中一个失败不影响其它。
              </AppText>
            </Card>
          ) : (
            files.map((file, index) => (
              <FileRow
                key={`${file.uri}-${index}`}
                file={file}
                onRemove={() => setFiles((previous) => previous.filter((_, i) => i !== index))}
              />
            ))
          )}

          <Button
            label={files.length === 0 ? '选择文件' : '继续添加'}
            variant="secondary"
            block
            style={{ marginTop: SPACING.md }}
            onPress={() => void onPick()}
          />

          <SectionHeader title="参数" />
          <Card>
            <OptionForm
              schema={tool.options_schema}
              values={effectiveValues}
              onChange={onChange}
              dynamicValues={dynamicValues}
            />
          </Card>

          {/* 失败时按错误码给「接下来能做什么」。不显示重试按钮 ——
              重试就是再按一次「开始转换」，多一个按钮只会让人以为
              两个按钮做的事不一样 */}
          {failure ? <ErrorView error={failure} inline /> : null}

          {/* Android 上的返回键与手势也要能走，所以提交按钮不是唯一的出路 */}
          <View style={styles.actions}>
            <Button
              label={submitting ? '正在提交…' : '开始转换'}
              block
              loading={submitting}
              disabled={!canSubmit}
              onPress={() => void onSubmit()}
            />
            {!tool.available ? (
              <AppText variant="caption" tone="faint" center style={{ marginTop: SPACING.sm }}>
                这个工具当前不可用
              </AppText>
            ) : null}
          </View>
        </ScrollView>
      </Screen>
    </>
  )
}

function FileRow({ file, onRemove }: { file: MobileFile; onRemove: () => void }) {
  const colors = useColors()
  const size = formatBytes(file.size)

  return (
    <Card compact style={styles.fileRow}>
      <View style={styles.fileMain}>
        <AppText numberOfLines={1}>{file.name}</AppText>
        {size ? (
          <AppText variant="caption" tone="faint" style={{ marginTop: SPACING.xs }}>
            {size}
          </AppText>
        ) : null}
      </View>
      <Pressable
        accessibilityRole="button"
        accessibilityLabel={`移除 ${file.name}`}
        onPress={onRemove}
        style={({ pressed }) => [
          styles.remove,
          { backgroundColor: colors.surfaceMuted, borderRadius: RADIUS.sm, opacity: pressed ? 0.6 : 1 },
        ]}
      >
        <AppText tone="faint">✕</AppText>
      </Pressable>
    </Card>
  )
}

const styles = StyleSheet.create({
  content: { paddingBottom: SPACING.xxl },
  fileRow: { flexDirection: 'row', alignItems: 'center', marginBottom: SPACING.sm },
  fileMain: { flex: 1, paddingRight: SPACING.sm },
  remove: {
    width: TOUCH_TARGET,
    height: TOUCH_TARGET,
    alignItems: 'center',
    justifyContent: 'center',
  },
  actions: { marginTop: SPACING.xl },
})
