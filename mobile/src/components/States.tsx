/**
 * 加载 / 空 / 出错三态（规格 §二十六 / §二十七）。
 *
 * 三态各有一个组件、各自有明确的语义，是因为移动端最常见的三种「界面看起来
 * 卡住了」都是这三件事没分清：
 *
 * - 把**加载中**画成空白 —— 用户以为 App 坏了
 * - 把**空列表**画成空白 —— 用户以为 App 坏了
 * - 把**出错**画成空列表 —— 用户以为数据真的就是空的，不会重试
 *
 * 出错一律走 `utils/errorMessages.ts`：服务端的 `message` 优先，
 * 按 `code` 补一句「接下来能做什么」。**任何情况下都不显示堆栈、
 * 文件路径或内部模块名**（规格 §十四）。
 */

import React from 'react'
import { ActivityIndicator, StyleSheet, View } from 'react-native'

import { AppText } from './AppText'
import { Button } from './Button'
import { ApiError, CLIENT_CODES } from '@/services/api/client'
import { RADIUS, SPACING, useColors } from '@/theme'
import { explainError } from '@/utils/errorMessages'

export function LoadingView({ label = '加载中…' }: { label?: string }) {
  const colors = useColors()
  return (
    <View style={styles.centered}>
      <ActivityIndicator size="large" color={colors.primary} />
      <AppText tone="muted" style={{ marginTop: SPACING.md }}>
        {label}
      </AppText>
    </View>
  )
}

export interface EmptyViewProps {
  /** 一句话说明这里为什么是空的 */
  title: string
  /** 可以做点什么 */
  hint?: string
  actionLabel?: string
  onAction?: () => void
  /** 不撑满高度（嵌在卡片里用） */
  inline?: boolean
}

export function EmptyView({ title, hint, actionLabel, onAction, inline = false }: EmptyViewProps) {
  return (
    <View style={inline ? styles.inline : styles.centered}>
      <AppText variant="subheading" tone="muted" center>
        {title}
      </AppText>
      {hint ? (
        <AppText tone="faint" center style={{ marginTop: SPACING.sm }}>
          {hint}
        </AppText>
      ) : null}
      {actionLabel && onAction ? (
        <Button label={actionLabel} onPress={onAction} variant="secondary" style={{ marginTop: SPACING.lg }} />
      ) : null}
    </View>
  )
}

export interface ErrorViewProps {
  /** 抓到的异常。不是 :class:`ApiError` 时按「未知错误」显示，不显示堆栈 */
  error: unknown
  onRetry?: () => void
  inline?: boolean
}

export function ErrorView({ error, onRetry, inline = false }: ErrorViewProps) {
  const colors = useColors()

  const code = error instanceof ApiError ? error.code : null
  const message = error instanceof Error ? error.message : null
  const { title, hint } = explainError(code, message)

  return (
    <View style={inline ? styles.inline : styles.centered}>
      <View
        style={[
          styles.badge,
          {
            backgroundColor: colors.dangerSoft,
            borderColor: colors.danger,
            borderRadius: RADIUS.md,
          },
        ]}
      >
        <AppText variant="subheading" tone="danger" center bold>
          {title}
        </AppText>
        {hint ? (
          <AppText center style={{ color: colors.danger, marginTop: SPACING.sm }}>
            {hint}
          </AppText>
        ) : null}
      </View>
      {onRetry ? (
        <Button label="重试" onPress={onRetry} variant="secondary" style={{ marginTop: SPACING.lg }} />
      ) : null}
    </View>
  )
}

/** 一句轻提示（保存失败、分享不可用之类）。比弹窗轻，比无声强 */
export function Notice({ text, tone = 'warning' }: { text: string; tone?: 'warning' | 'danger' | 'success' }) {
  const colors = useColors()
  const skin = {
    warning: { background: colors.warningSoft, border: colors.warning, text: colors.warning },
    danger: { background: colors.dangerSoft, border: colors.danger, text: colors.danger },
    success: { background: colors.successSoft, border: colors.success, text: colors.success },
  }[tone]

  return (
    <View
      style={[
        styles.notice,
        { backgroundColor: skin.background, borderColor: skin.border, borderRadius: RADIUS.sm },
      ]}
    >
      <AppText variant="label" style={{ color: skin.text }}>
        {text}
      </AppText>
    </View>
  )
}

/**
 * 把任意异常转成一句**可以显示**的话。
 *
 * 组件里 `catch (e)` 之后不要自己去读 `e.message` —— 那可能是
 * `TypeError: undefined is not an object`，显示出来对用户毫无意义，
 * 还漏了内部实现。统一走这里。
 */
export function describeError(error: unknown): string {
  if (error instanceof ApiError) {
    return explainError(error.code, error.message).title
  }
  return explainError(CLIENT_CODES.response, null).title
}

const styles = StyleSheet.create({
  centered: { flex: 1, alignItems: 'center', justifyContent: 'center', padding: SPACING.xl },
  inline: { alignItems: 'center', justifyContent: 'center', padding: SPACING.lg },
  badge: { borderWidth: 1, padding: SPACING.lg, maxWidth: 420, width: '100%' },
  notice: { borderWidth: 1, padding: SPACING.md, marginTop: SPACING.md },
})
