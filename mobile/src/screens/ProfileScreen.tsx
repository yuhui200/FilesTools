/**
 * 我的。
 *
 * 三块内容，每一块都是**能验证的事实**，不是装饰：
 *
 * - **服务器**：显示当前地址与连通状态。地址来自打包时写死的
 *   `EXPO_PUBLIC_API_BASE_URL`，界面只读不改 —— 让用户在这里手填，
 *   就等于把「配置错了」这件事推迟到某次转换失败才暴露。
 * - **外观**：浅色 / 深色 / 跟随系统。
 * - **自检**：把服务端 `/api/config` 给的错误码清单与本地的文案表对一遍。
 *   服务端加了一个新错误码而 App 没跟上时，这里会如实列出缺哪几个，
 *   而不是等用户撞上再显示一句「处理失败」。
 */

import React, { useMemo } from 'react'
import { ScrollView, StyleSheet, View } from 'react-native'

import { AppText } from '@/components/AppText'
import { Card, SectionHeader } from '@/components/Card'
import { Screen } from '@/components/Screen'
import { Notice } from '@/components/States'
import { API_BASE_URL, isApiConfigured } from '@/services/api/client'
import { useCapabilities } from '@/state/CapabilitiesProvider'
import { GUTTER, RADIUS, SPACING, TOUCH_TARGET, useColors } from '@/theme'
import { THEME_MODES, useTheme } from '@/theme'
import { APP_VERSION } from '@/types'
import { KNOWN_ERROR_CODES, missingErrorCodes } from '@/utils/errorMessages'
import { formatDuration } from '@/utils/labels'
import { Pressable } from 'react-native'

export default function ProfileScreen() {
  const colors = useColors()
  const { mode, setMode } = useTheme()
  const { config, error, loading, reload, capabilities } = useCapabilities()

  const missingCodes = useMemo(
    () => (config ? missingErrorCodes(config.error_codes) : []),
    [config],
  )

  return (
    <Screen title="我的">
      <ScrollView contentContainerStyle={styles.content}>
        <SectionHeader title="服务器" />
        <Card>
          <AppText variant="label" tone="muted">
            地址
          </AppText>
          <AppText style={{ marginTop: SPACING.xs }} selectable>
            {isApiConfigured ? API_BASE_URL : '未配置'}
          </AppText>
          <AppText variant="caption" tone="faint" style={{ marginTop: SPACING.sm }}>
            在 mobile/.env 里的 EXPO_PUBLIC_API_BASE_URL 设置，改完要重新启动或重新构建。
            所有转换都在这个服务器上完成，App 只负责把文件传上去、把结果取回来。
          </AppText>

          <View style={[styles.divider, { backgroundColor: colors.divider }]} />

          <AppText variant="label" tone="muted">
            状态
          </AppText>
          <AppText style={{ marginTop: SPACING.xs }} tone={error ? 'danger' : 'success'}>
            {error
              ? '连不上'
              : loading
                ? '正在连接…'
                : `已连接，可用转换 ${capabilities?.conversions.filter((item) => item.available).length ?? 0} 种`}
          </AppText>

          <Pressable
            accessibilityRole="button"
            onPress={() => void reload()}
            style={({ pressed }) => [
              styles.recheck,
              {
                borderColor: colors.border,
                borderRadius: RADIUS.sm,
                opacity: pressed ? 0.6 : 1,
              },
            ]}
          >
            <AppText variant="label" tone="primary">
              重新检测
            </AppText>
          </Pressable>
        </Card>

        <SectionHeader title="外观" />
        <Card>
          {THEME_MODES.map((option, index) => {
            const selected = option.value === mode
            return (
              <Pressable
                key={option.value}
                accessibilityRole="radio"
                accessibilityState={{ selected }}
                onPress={() => setMode(option.value)}
                style={({ pressed }) => [
                  styles.themeRow,
                  index > 0 ? { borderTopColor: colors.divider, borderTopWidth: 1 } : null,
                  { opacity: pressed ? 0.6 : 1 },
                ]}
              >
                <AppText>{option.label}</AppText>
                <AppText tone={selected ? 'primary' : 'faint'} bold={selected}>
                  {selected ? '✓' : ''}
                </AppText>
              </Pressable>
            )
          })}
        </Card>

        <SectionHeader title="关于" />
        <Card>
          <Row label="版本" value={APP_VERSION} />
          <Row
            label="任务记录保留"
            value={config ? formatDuration(config.task_ttl_seconds) || '由服务器决定' : '—'}
          />
          <Row
            label="结果文件保留"
            value={config ? formatDuration(config.file_ttl_seconds) || '由服务器决定' : '—'}
          />
          <Row label="本地文案覆盖的错误码" value={`${KNOWN_ERROR_CODES.length} 个`} />
        </Card>

        {config ? (
          missingCodes.length === 0 ? (
            <Notice text="错误码文案与服务端一致，没有漏配。" tone="success" />
          ) : (
            <Notice
              text={`服务端会返回 ${missingCodes.length} 个本地没有文案的错误码：${missingCodes.join('、')}。遇到它们时只会显示通用提示。`}
              tone="warning"
            />
          )
        ) : (
          <Notice text="没取到服务器配置，无法核对错误码文案。" tone="warning" />
        )}

        <AppText variant="caption" tone="faint" style={styles.footnote}>
          这个 App 不含账号、登录、支付与会员功能；转换记录只存在这台设备上，不会上传。
        </AppText>
      </ScrollView>
    </Screen>
  )
}

function Row({ label, value }: { label: string; value: string }) {
  const colors = useColors()
  return (
    <View style={[styles.row, { borderBottomColor: colors.divider }]}>
      <AppText tone="muted">{label}</AppText>
      <AppText tone="faint">{value}</AppText>
    </View>
  )
}

const styles = StyleSheet.create({
  content: { paddingHorizontal: GUTTER, paddingBottom: SPACING.xxl },
  divider: { height: 1, marginVertical: SPACING.md },
  recheck: {
    minHeight: TOUCH_TARGET,
    marginTop: SPACING.md,
    borderWidth: 1,
    alignItems: 'center',
    justifyContent: 'center',
  },
  themeRow: {
    minHeight: TOUCH_TARGET,
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
  },
  row: {
    minHeight: TOUCH_TARGET,
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
  },
  footnote: { marginTop: SPACING.xl },
})
