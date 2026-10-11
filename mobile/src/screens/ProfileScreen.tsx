/**
 * 我的。
 *
 * 三块内容，每一块都是**能验证的事实**，不是装饰：
 *
 * - **服务器**：显示当前地址、它的来源（自己设的 / 安装包自带），并且**能改**。
 *   这里曾经是只读的，理由是「让用户手填等于把配置错误推迟到某次转换失败才暴露」——
 *   那个理由对**服务端部署者**成立，对**拿到 APK 的人**不成立：Android 上
 *   后端就在他自己那台机器上，地址只有他知道，从安装包里改不了地址的包
 *   等于连不上任何一台真实服务器（0.1.0 那份发行说明正是这么写错的）。
 *   现在的做法是两条路都给：打包时配默认值，装在设备上还能改，
 *   并且**如实告诉用户当前用的是哪一种**。
 * - **外观**：浅色 / 深色 / 跟随系统。
 * - **自检**：把服务端 `/api/config` 给的错误码清单与本地的文案表对一遍。
 *   服务端加了一个新错误码而 App 没跟上时，这里会如实列出缺哪几个，
 *   而不是等用户撞上再显示一句「处理失败」。
 */

import React, { useMemo, useState } from 'react'
import { Pressable, ScrollView, StyleSheet, TextInput, View } from 'react-native'

import { AppText } from '@/components/AppText'
import { Card, SectionHeader } from '@/components/Card'
import { Screen } from '@/components/Screen'
import { Notice } from '@/components/States'
import { apiBaseUrlProblem, normalizeApiBaseUrl } from '@/services/api/client'
import { useApiConfig } from '@/state/ApiConfigProvider'
import { useCapabilities } from '@/state/CapabilitiesProvider'
import { GUTTER, RADIUS, SPACING, TOUCH_TARGET, useColors } from '@/theme'
import { THEME_MODES, useTheme } from '@/theme'
import { APP_VERSION } from '@/types'
import { KNOWN_ERROR_CODES, missingErrorCodes } from '@/utils/errorMessages'
import { formatDuration } from '@/utils/labels'

export default function ProfileScreen() {
  const colors = useColors()
  const { mode, setMode } = useTheme()
  const { config, error, loading, reload, capabilities } = useCapabilities()
  const { ready, baseUrl, defaultBaseUrl, isOverridden, save, reset } = useApiConfig()

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
            {ready ? baseUrl || '未配置' : '正在读取…'}
          </AppText>
          <AppText variant="caption" tone="faint" style={{ marginTop: SPACING.sm }}>
            {sourceLabel(ready, baseUrl, isOverridden)}
            所有转换都在这个服务器上完成，App 只负责把文件传上去、把结果取回来。
          </AppText>

          {ready ? (
            <ServerAddressEditor
              baseUrl={baseUrl}
              defaultBaseUrl={defaultBaseUrl}
              isOverridden={isOverridden}
              save={save}
              reset={reset}
            />
          ) : null}

          <View style={[styles.divider, { backgroundColor: colors.divider }]} />

          <AppText variant="label" tone="muted">
            状态
          </AppText>
          <AppText style={{ marginTop: SPACING.xs }} tone={error ? 'danger' : 'success'}>
            {!ready
              ? '正在读取…'
              : error
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

/** 地址是从哪来的。**必须如实说** —— 用户有权知道改了会不会跟着走。 */
function sourceLabel(ready: boolean, baseUrl: string, isOverridden: boolean): string {
  if (!ready) return ''
  if (!baseUrl) return '还没有设置服务器地址。'
  return isOverridden
    ? '这是你在这台设备上设置的地址，跟着这台设备走。'
    : '这是安装包里自带的默认地址。'
}

/**
 * 改服务器地址。
 *
 * 收起时只露一个「改地址」按钮：绝大多数用户装完就该用它，多一个输入框
 * 只会让人以为必须填点什么。展开后才出现输入框、校验、保存、恢复默认。
 *
 * **保存**做两件事，顺序有意为之：先让新地址立即生效，再写本地存储。
 * 写盘会失败（磁盘满、企业设备策略），但「这次能用」不该跟着一起失败 ——
 * 写不进去只影响「下次还记不记得」，界面上如实说一句。
 */
function ServerAddressEditor({
  baseUrl,
  defaultBaseUrl,
  isOverridden,
  save,
  reset,
}: {
  baseUrl: string
  defaultBaseUrl: string
  isOverridden: boolean
  save: (value: string) => Promise<boolean>
  reset: () => Promise<void>
}) {
  const colors = useColors()
  const [open, setOpen] = useState(false)
  const [draft, setDraft] = useState(baseUrl)
  const [problem, setProblem] = useState<string | null>(null)
  const [notPersisted, setNotPersisted] = useState(false)
  const [busy, setBusy] = useState(false)

  const startEditing = () => {
    setDraft(baseUrl)
    setProblem(null)
    setNotPersisted(false)
    setOpen(true)
  }

  const commit = async () => {
    const invalid = apiBaseUrlProblem(draft)
    if (invalid) {
      setProblem(invalid)
      return
    }
    setBusy(true)
    // 校验用的是原始输入，存的也是原始输入；`normalizeApiBaseUrl` 在
    // client 那一侧统一做（存储、输入框、环境变量三条路口径一致）
    const persisted = await save(normalizeApiBaseUrl(draft))
    setBusy(false)
    setProblem(null)
    setNotPersisted(!persisted)
    setOpen(false)
  }

  if (!open) {
    return (
      <View>
        {notPersisted ? (
          <AppText variant="caption" tone="warning" style={{ marginTop: SPACING.sm }}>
            新地址已经生效，但没能写进这台设备的存储里 —— 下次打开 App
            会退回原来的地址，你需要再设一次。
          </AppText>
        ) : null}
        <Pressable
          accessibilityRole="button"
          onPress={startEditing}
          style={({ pressed }) => [
            styles.recheck,
            { borderColor: colors.border, borderRadius: RADIUS.sm, opacity: pressed ? 0.6 : 1 },
          ]}
        >
          <AppText variant="label" tone="primary">
            改地址
          </AppText>
        </Pressable>
      </View>
    )
  }

  return (
    <View style={{ marginTop: SPACING.md }}>
      <TextInput
        value={draft}
        onChangeText={setDraft}
        placeholder="http://192.168.1.10:8011"
        placeholderTextColor={colors.textFaint}
        // 地址里没有大写意义，自动纠正只会把 10.0.2.2 改成别的词
        autoCapitalize="none"
        autoCorrect={false}
        keyboardType="url"
        inputMode="url"
        style={[
          styles.input,
          {
            color: colors.text,
            backgroundColor: colors.surface,
            borderColor: problem ? colors.danger : colors.border,
            borderRadius: RADIUS.sm,
          },
        ]}
      />
      {problem ? (
        <AppText variant="caption" tone="danger" style={{ marginTop: SPACING.xs }}>
          {problem}
        </AppText>
      ) : null}
      <AppText variant="caption" tone="faint" style={{ marginTop: SPACING.sm }}>
        {/* 这条限制是操作系统的，不是 App 的，所以要说清「怎么办」而不是「不行」 */}
        Android 默认不允许明文 HTTP。安装包只放行了打包时就写死的那几个主机；
        要连局域网里的 http:// 服务器，请按 README 用你服务器的地址重新打包，
        或者让它走 https://。
      </AppText>

      <View style={styles.editorActions}>
        <Pressable
          accessibilityRole="button"
          disabled={busy}
          onPress={() => void commit()}
          style={({ pressed }) => [
            styles.recheck,
            styles.actionGrow,
            {
              borderColor: colors.primary,
              backgroundColor: colors.primary,
              borderRadius: RADIUS.sm,
              opacity: pressed || busy ? 0.6 : 1,
            },
          ]}
        >
          <AppText variant="label" tone="onPrimary">
            保存并使用
          </AppText>
        </Pressable>
        <Pressable
          accessibilityRole="button"
          onPress={() => setOpen(false)}
          style={({ pressed }) => [
            styles.recheck,
            styles.actionGrow,
            { borderColor: colors.border, borderRadius: RADIUS.sm, opacity: pressed ? 0.6 : 1 },
          ]}
        >
          <AppText variant="label" tone="muted">
            取消
          </AppText>
        </Pressable>
      </View>

      {isOverridden && defaultBaseUrl ? (
        <Pressable
          accessibilityRole="button"
          onPress={() => {
            void reset()
            setOpen(false)
            setNotPersisted(false)
          }}
          style={({ pressed }) => [
            styles.recheck,
            { borderColor: colors.border, borderRadius: RADIUS.sm, opacity: pressed ? 0.6 : 1 },
          ]}
        >
          <AppText variant="label" tone="muted">
            恢复成安装包自带的地址
          </AppText>
        </Pressable>
      ) : null}
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
  editorActions: { flexDirection: 'row', gap: SPACING.sm },
  actionGrow: { flex: 1 },
  input: {
    minHeight: TOUCH_TARGET,
    borderWidth: 1,
    paddingHorizontal: SPACING.md,
    fontSize: 15,
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
