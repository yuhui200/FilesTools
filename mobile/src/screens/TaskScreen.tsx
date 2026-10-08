/**
 * 进度与结果（规格 §十七 / §十八 / §二十）。
 *
 * ## 这一页刻意不做的三件事
 *
 * 1. **不编进度。** 只有 PDF → Word 会上报真实页码，其余转换那一项是
 *    `null` —— 那时画不确定条，不拿「已过秒数」凑一个百分比。
 * 2. **不把「正在取消」说成「已取消」。** 取消是协作式的：还在排队的
 *    立刻停，正在跑的那个停不下来，状态会如实停在 `cancelling`。
 * 3. **不提供第二次下载。** `download_url` 是一次性令牌，下载过就作废。
 *    所以结果一旦落到本地，按钮就从「下载」变成「分享 / 打开」——
 *    再点一次网络下载只会得到 404。
 */

import { Stack, useLocalSearchParams, useRouter } from 'expo-router'
import React, { useCallback, useEffect, useMemo, useState } from 'react'
import { ScrollView, StyleSheet, View } from 'react-native'

import { AppText } from '@/components/AppText'
import { Button } from '@/components/Button'
import { Card, SectionHeader } from '@/components/Card'
import { ProgressBar } from '@/components/ProgressBar'
import { Screen } from '@/components/Screen'
import { EmptyView, ErrorView, LoadingView, Notice } from '@/components/States'
import { retryTask } from '@/services/api/tasks'
import { ApiError, isApiConfigured } from '@/services/api/client'
import { downloadResult, type DownloadedFile } from '@/services/download'
import { updateHistory } from '@/services/history'
import { isShareAvailable, shareFile } from '@/services/share'
import { useBatchPolling } from '@/hooks/useBatchPolling'
import { SPACING, useColors } from '@/theme'
import type { ConversionTaskStatus } from '@/types'
import { explainError } from '@/utils/errorMessages'
import { STATUS_LABELS, formatBytes, statusTone } from '@/utils/labels'

export default function TaskScreen() {
  const colors = useColors()
  const router = useRouter()
  const params = useLocalSearchParams<{ batchId: string | string[] }>()
  const batchId = Array.isArray(params.batchId) ? params.batchId[0] : params.batchId

  const [shareReady, setShareReady] = useState(false)

  const polling = useBatchPolling({
    batchId: batchId ?? null,
    onSettled: useCallback(
      (snapshot) => {
        // 整批定下来时把历史里的那条更新掉。找不到记录（历史被清了）
        // 就什么都不做 —— 不凭空造一条出来
        const first = snapshot.tasks.find((task) => task.result)
        void updateHistory(snapshot.batch_id, {
          status: snapshot.status,
          resultFilename: first?.result?.filename ?? null,
          resultExpired: snapshot.result?.expired ?? false,
        })
      },
      [],
    ),
  })

  useEffect(() => {
    let alive = true
    void isShareAvailable().then((ready) => {
      if (alive) setShareReady(ready)
    })
    return () => {
      alive = false
    }
  }, [])

  const counts = polling.counts
  const snapshot = polling.snapshot
  const status = snapshot?.status ?? counts?.status ?? 'queued'
  const total = counts?.total ?? snapshot?.total ?? 0
  const finished =
    (counts?.completed ?? 0) + (counts?.failed ?? 0) + (counts?.cancelled ?? 0)

  /** 下载到本地的结果，按任务号索引。有值就说明这个结果已经不能再下第二次了 */
  const [downloaded, setDownloaded] = useState<Record<string, DownloadedFile>>({})
  const [busy, setBusy] = useState<Record<string, boolean>>({})
  const [failed, setFailed] = useState<Record<string, ApiError>>({})

  const onDownload = useCallback(
    async (task: ConversionTaskStatus) => {
      const result = task.result
      if (!result?.download_url) return
      setBusy((previous) => ({ ...previous, [task.task_id]: true }))
      setFailed((previous) => omit(previous, task.task_id))
      try {
        const file = await downloadResult({
          url: result.download_url,
          filename: result.filename,
        })
        setDownloaded((previous) => ({ ...previous, [task.task_id]: file }))
      } catch (caught) {
        setFailed((previous) => ({
          ...previous,
          [task.task_id]:
            caught instanceof ApiError ? caught : new ApiError('下载失败', 'network_error'),
        }))
      } finally {
        setBusy((previous) => ({ ...previous, [task.task_id]: false }))
      }
    },
    [],
  )

  const onShare = useCallback(
    async (task: ConversionTaskStatus) => {
      const local = downloaded[task.task_id]
      if (!local) return
      setFailed((previous) => omit(previous, task.task_id))
      try {
        await shareFile({
          uri: local.uri,
          name: local.name,
          ...(task.result?.media_type ? { mimeType: task.result.media_type } : {}),
        })
      } catch (caught) {
        setFailed((previous) => ({
          ...previous,
          [task.task_id]:
            caught instanceof ApiError ? caught : new ApiError('分享失败', 'file_error'),
        }))
      }
    },
    [downloaded],
  )

  const onRetry = useCallback(
    async (task: ConversionTaskStatus) => {
      setBusy((previous) => ({ ...previous, [task.task_id]: true }))
      setFailed((previous) => omit(previous, task.task_id))
      try {
        // 重试把这一项重新排回队列，整批于是**不再是终态** —— 但轮询循环
        // 到终态就停了。顺序要紧：先拉一次快照（界面立刻从「失败」变回
        // 「等待中」），再重启轮询（它才会一路跟到下一次结束）。
        // 少了 restart，界面会停在失败态，用户以为重试没生效。
        await retryTask(task.task_id)
        await polling.refresh()
        polling.restart()
        setBusy((previous) => omit(previous, task.task_id))
      } catch (caught) {
        setFailed((previous) => ({
          ...previous,
          [task.task_id]:
            caught instanceof ApiError ? caught : new ApiError('重试失败', 'network_error'),
        }))
        setBusy((previous) => omit(previous, task.task_id))
      }
    },
    [polling],
  )

  const title = useMemo(() => {
    if (status === 'completed') return '已完成'
    if (status === 'failed') return '处理失败'
    if (status === 'cancelled') return '已取消'
    if (counts?.cancelling) return '正在取消'
    return '处理中'
  }, [status, counts?.cancelling])

  if (!isApiConfigured) {
    return (
      <Screen>
        <ErrorView error={new ApiError('没有配置服务器地址', 'config_failed')} />
      </Screen>
    )
  }

  if (!batchId) {
    return (
      <Screen>
        <EmptyView title="缺少任务号" hint="这条链接不完整，回工具列表重新提交一次吧。" actionLabel="回到工具" onAction={() => router.replace('/tools')} />
      </Screen>
    )
  }

  // 第一次请求还没回来：既没有计数也没有快照
  if (!counts && !snapshot && !polling.error) {
    return (
      <Screen>
        <Stack.Screen options={{ title: '处理中' }} />
        <LoadingView label="正在获取任务状态…" />
      </Screen>
    )
  }

  if (polling.error && !snapshot) {
    return (
      <Screen>
        <Stack.Screen options={{ title: '任务' }} />
        <ErrorView error={polling.error} onRetry={() => void polling.refresh()} />
        <View style={styles.footer}>
          <Button label="回到工具" variant="ghost" block onPress={() => router.replace('/tools')} />
        </View>
      </Screen>
    )
  }

  return (
    <Screen noBottomInset>
      <Stack.Screen options={{ title }} />
      <ScrollView contentContainerStyle={styles.content}>
        <Card>
          <View style={styles.progressHead}>
            <AppText variant="subheading" bold>
              {STATUS_LABELS[status as keyof typeof STATUS_LABELS] ?? status}
            </AppText>
            <AppText tone="muted">
              {total > 0 ? `${finished} / ${total}` : ''}
            </AppText>
          </View>
          <View style={{ marginTop: SPACING.md }}>
            <ProgressBar value={typeof counts?.progress === 'number' ? counts.progress : null} />
          </View>
          <AppText variant="caption" tone="faint" style={{ marginTop: SPACING.sm }}>
            {counts?.cancelling
              ? '已经请求取消：还在排队的会立刻停，正在处理的这个要跑完才能停。'
              : '结果会保留一小段时间，请尽快下载；下载过一次后链接就会失效。'}
          </AppText>

          {polling.gaveUp ? (
            <Notice
              text="等待时间已到上限，不再自动刷新。服务器可能还在处理，可以点下面的「刷新状态」再看看。"
              tone="warning"
            />
          ) : null}

          {polling.error ? (
            <Notice text={`${explainError(polling.error.code, polling.error.message).title}（自动刷新已暂停）`} tone="warning" />
          ) : null}

          <View style={styles.progressActions}>
            <Button label="刷新状态" variant="secondary" onPress={() => void polling.refresh()} />
            {!snapshot || snapshot.status === 'queued' || snapshot.status === 'processing' ? (
              <Button
                label="取消"
                variant="ghost"
                disabled={Boolean(counts?.cancelling)}
                onPress={() => void polling.cancel()}
              />
            ) : null}
          </View>
        </Card>

        {snapshot ? (
          <>
            <SectionHeader title="文件" hint={`${snapshot.tasks.length} 个`} />
            {snapshot.tasks.map((task) => (
              <TaskCard
                key={task.task_id}
                task={task}
                busy={Boolean(busy[task.task_id])}
                local={downloaded[task.task_id] ?? null}
                failure={failed[task.task_id] ?? null}
                shareReady={shareReady}
                onDownload={() => void onDownload(task)}
                onShare={() => void onShare(task)}
                onRetry={() => void onRetry(task)}
              />
            ))}

            {snapshot.result?.download_url ? (
              <Card style={{ marginTop: SPACING.md }}>
                <AppText variant="subheading" bold>
                  全部结果
                </AppText>
                <AppText variant="caption" tone="muted" style={{ marginTop: SPACING.xs }}>
                  {snapshot.completed > 1
                    ? '成功的结果会打包成一个 ZIP 下载。'
                    : '下载这一份结果。'}
                </AppText>
                <Button
                  label="打包下载"
                  block
                  style={{ marginTop: SPACING.md }}
                  onPress={() =>
                    void downloadResult({
                      url: snapshot.result!.download_url!,
                      filename: snapshot.result?.archive_filename ?? 'filetools.zip',
                    }).catch(() => {
                      // 整包下载失败时给一句通用提示；单项的失败各自显示在自己的卡片上
                    })
                  }
                />
              </Card>
            ) : snapshot.result?.expired ? (
              <Notice text="结果已经被下载过或已过期，服务器上已经删除了。需要的话重新处理一次。" tone="warning" />
            ) : null}
          </>
        ) : (
          <AppText tone="faint" center style={styles.waitingHint}>
            明细会在处理过程中陆续出现。
          </AppText>
        )}

        <View style={styles.footer}>
          <Button label="回到工具" variant="secondary" block onPress={() => router.replace('/tools')} />
        </View>
      </ScrollView>
    </Screen>
  )
}

function TaskCard({
  task,
  busy,
  local,
  failure,
  shareReady,
  onDownload,
  onShare,
  onRetry,
}: {
  task: ConversionTaskStatus
  busy: boolean
  local: DownloadedFile | null
  failure: ApiError | null
  shareReady: boolean
  onDownload: () => void
  onShare: () => void
  onRetry: () => void
}) {
  const colors = useColors()
  const tone = statusTone(task.status)
  const toneColor: Record<string, string> = {
    success: colors.success,
    danger: colors.danger,
    primary: colors.primary,
    muted: colors.textMuted,
    warning: colors.warning,
  }

  const explanation = task.error_code ? explainError(task.error_code, task.error_message) : null

  return (
    <Card compact style={styles.taskCard}>
      <View style={styles.taskHead}>
        <AppText style={styles.taskName} numberOfLines={1}>
          {task.source_filename}
        </AppText>
        <AppText variant="label" style={{ color: toneColor[tone] }}>
          {STATUS_LABELS[task.status]}
        </AppText>
      </View>

      {task.status === 'processing' || task.status === 'cancelling' ? (
        <View style={{ marginTop: SPACING.sm }}>
          <ProgressBar value={typeof task.progress === 'number' ? task.progress : null} height={4} />
          {/* 只有 PDF → Word 会报真实页码。没有就不显示行号，不编 */}
          {task.page !== null && task.page_count !== null ? (
            <AppText variant="caption" tone="faint" style={{ marginTop: SPACING.xs }}>
              第 {task.page} / {task.page_count} 页
              {task.stage ? ` · ${task.stage}` : ''}
            </AppText>
          ) : null}
        </View>
      ) : null}

      {explanation ? (
        <View style={{ marginTop: SPACING.sm }}>
          <AppText variant="label" tone="danger">
            {explanation.title}
          </AppText>
          {explanation.hint ? (
            <AppText variant="caption" tone="muted" style={{ marginTop: SPACING.xs }}>
              {explanation.hint}
            </AppText>
          ) : null}
        </View>
      ) : null}

      {task.result ? (
        <AppText variant="caption" tone="faint" style={{ marginTop: SPACING.sm }}>
          {task.result.filename}
          {formatBytes(task.result.output_size) ? ` · ${formatBytes(task.result.output_size)}` : ''}
          {task.result.original_size && task.result.output_size && task.result.output_size < task.result.original_size
            ? ` · 比原来小 ${formatBytes(task.result.original_size - task.result.output_size)}`
            : ''}
        </AppText>
      ) : null}

      {failure ? (
        <AppText variant="caption" tone="danger" style={{ marginTop: SPACING.sm }}>
          {explainError(failure.code, failure.message).title}
        </AppText>
      ) : null}

      <View style={styles.taskActions}>
        {task.result && !local ? (
          <Button
            label={busy ? '下载中…' : '下载'}
            variant="primary"
            loading={busy}
            onPress={onDownload}
          />
        ) : null}

        {local ? (
          <>
            <AppText variant="caption" tone="success" style={styles.savedHint}>
              已保存到本机
            </AppText>
            {shareReady ? (
              <Button label="分享 / 打开" variant="secondary" onPress={onShare} />
            ) : (
              <AppText variant="caption" tone="faint">
                这台设备不支持分享，文件已保存在 App 的缓存里。
              </AppText>
            )}
          </>
        ) : null}

        {task.can_retry ? (
          <Button
            label={busy ? '重试中…' : '重试'}
            variant="ghost"
            loading={busy}
            onPress={onRetry}
          />
        ) : null}
      </View>

      {task.auto_retry_count > 0 ? (
        <AppText variant="caption" tone="faint" style={{ marginTop: SPACING.xs }}>
          服务器已自动重新排队 {task.auto_retry_count} 次
        </AppText>
      ) : null}
    </Card>
  )
}

/** 从对象里去掉一个键（不可变，供 setState 用） */
function omit<T>(source: Record<string, T>, key: string): Record<string, T> {
  if (!(key in source)) return source
  const next = { ...source }
  delete next[key]
  return next
}

const styles = StyleSheet.create({
  content: { paddingBottom: SPACING.xxl },
  progressHead: { flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between' },
  progressActions: {
    flexDirection: 'row',
    gap: SPACING.sm,
    marginTop: SPACING.lg,
  },
  taskCard: { marginBottom: SPACING.sm },
  taskHead: { flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between' },
  taskName: { flex: 1, paddingRight: SPACING.sm },
  taskActions: {
    flexDirection: 'row',
    alignItems: 'center',
    flexWrap: 'wrap',
    gap: SPACING.sm,
    marginTop: SPACING.md,
  },
  savedHint: { marginRight: SPACING.xs },
  waitingHint: { marginTop: SPACING.xl },
  footer: { marginTop: SPACING.xl },
})
