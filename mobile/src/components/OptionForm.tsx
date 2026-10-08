/**
 * 动态参数面板（规格 §二十五）。
 *
 * **这里没有任何一种格式的名字。** 一个字段画成什么样，完全由服务端那条
 * 能力的 `options_schema.items[].type` 决定 —— 加 MP4、EPUB、CSV 时
 * 这个文件一行都不用改。任何 `if (target === 'png')` 都是把能力矩阵
 * 抄进客户端，正是规格 §八 明令禁止的东西。
 *
 * 数值输入用「− 输入框 +」而不是滑杆：`@react-native-community/slider`
 * 是一个新依赖，而质量这类参数服务端本来就给了 `presets` 快捷档，
 * 「按档 + 微调」在手机上比一根 3 毫米宽的滑杆好按得多。
 */

import React from 'react'
import { Pressable, StyleSheet, Switch, TextInput, View } from 'react-native'

import { AppText } from './AppText'
import { RADIUS, SPACING, TOUCH_TARGET, useColors } from '@/theme'
import type { ConversionOptionEnumValue, ConversionOptionSpec, ConversionOptionsSchema } from '@/types'
import { choicesFor, clampToSpec, visibleItems } from '@/utils/options'

export interface OptionFormProps {
  schema: ConversionOptionsSchema | null
  values: Record<string, string>
  onChange: (key: string, value: string) => void
  /** 动态枚举（目前只有字体）的值域，来自 `/api/config` */
  dynamicValues?: Readonly<Record<string, ConversionOptionEnumValue[]>>
}

export function OptionForm({ schema, values, onChange, dynamicValues = {} }: OptionFormProps) {
  const items = visibleItems(schema, values, dynamicValues)

  if (items.length === 0) {
    return (
      <AppText tone="faint">
        这个转换没有可调参数，直接开始就行。
      </AppText>
    )
  }

  return (
    <View>
      {items.map((spec) => (
        <Field
          key={spec.key}
          spec={spec}
          value={values[spec.key] ?? ''}
          onChange={(next) => onChange(spec.key, next)}
          dynamicValues={dynamicValues}
        />
      ))}
    </View>
  )
}

interface FieldProps {
  spec: ConversionOptionSpec
  value: string
  onChange: (value: string) => void
  dynamicValues: Readonly<Record<string, ConversionOptionEnumValue[]>>
}

function Field({ spec, value, onChange, dynamicValues }: FieldProps) {
  return (
    <View style={styles.field}>
      <View style={styles.fieldHead}>
        <AppText variant="label" bold>
          {spec.label}
        </AppText>
        {spec.unit ? (
          <AppText variant="caption" tone="faint" style={{ marginLeft: SPACING.xs }}>
            （{spec.unit}）
          </AppText>
        ) : null}
      </View>

      {spec.type === 'boolean' ? (
        <BooleanField spec={spec} value={value} onChange={onChange} />
      ) : spec.type === 'enum' ? (
        <ChoiceRow
          choices={choicesFor(spec, dynamicValues)}
          value={value}
          onChange={onChange}
        />
      ) : spec.type === 'string' ? (
        <TextField value={value} onChange={onChange} placeholder={spec.label} />
      ) : (
        <NumberField spec={spec} value={value} onChange={onChange} />
      )}

      {spec.help ? (
        <AppText variant="caption" tone="faint" style={{ marginTop: SPACING.xs }}>
          {spec.help}
        </AppText>
      ) : null}
    </View>
  )
}

/** 一行可点的选项（枚举）。放在这里的理由是 enum 与 presets 的渲染完全一样 */
function ChoiceRow({
  choices,
  value,
  onChange,
}: {
  choices: ConversionOptionEnumValue[]
  value: string
  onChange: (value: string) => void
}) {
  const colors = useColors()
  if (choices.length === 0) return null

  return (
    <View style={styles.chips}>
      {choices.map((choice) => {
        const selected = choice.value === value
        return (
          <Pressable
            key={choice.value}
            accessibilityRole="radio"
            accessibilityState={{ selected }}
            onPress={() => onChange(choice.value)}
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
              {choice.label}
            </AppText>
          </Pressable>
        )
      })}
    </View>
  )
}

function BooleanField({
  spec,
  value,
  onChange,
}: {
  spec: ConversionOptionSpec
  value: string
  onChange: (value: string) => void
}) {
  const colors = useColors()
  const on = value === 'true' || value === '1'

  return (
    <Pressable
      accessibilityRole="switch"
      accessibilityState={{ checked: on }}
      onPress={() => onChange(on ? 'false' : 'true')}
      style={styles.switchRow}
    >
      <AppText tone="muted">{on ? '是' : '否'}</AppText>
      {/* Switch 自己不吃点击，靠外面这层 Pressable 统一处理 ——
          否则点文字没反应、点开关才有反应，是移动端最典型的手感问题 */}
      <Switch
        value={on}
        onValueChange={(next) => onChange(next ? 'true' : 'false')}
        trackColor={{ false: colors.track, true: colors.primary }}
        thumbColor="#ffffff"
      />
    </Pressable>
  )
}

function TextField({
  value,
  onChange,
  placeholder,
}: {
  value: string
  onChange: (value: string) => void
  placeholder: string
}) {
  const colors = useColors()
  return (
    <TextInput
      value={value}
      onChangeText={onChange}
      placeholder={placeholder}
      placeholderTextColor={colors.textFaint}
      style={[
        styles.input,
        {
          color: colors.text,
          backgroundColor: colors.surface,
          borderColor: colors.border,
          borderRadius: RADIUS.sm,
        },
      ]}
    />
  )
}

function NumberField({
  spec,
  value,
  onChange,
}: {
  spec: ConversionOptionSpec
  value: string
  onChange: (value: string) => void
}) {
  const colors = useColors()
  const step = spec.step ?? (spec.type === 'integer' ? 1 : 0.1)

  const nudge = (direction: 1 | -1) => {
    const current = Number(value)
    const base = Number.isFinite(current) ? current : (spec.min ?? 0)
    onChange(clampToSpec(spec, String(base + direction * step)))
  }

  return (
    <View>
      <View style={styles.stepperRow}>
        <StepButton label="−" onPress={() => nudge(-1)} disabled={atBound(spec, value, 'min')} />
        <TextInput
          value={value}
          onChangeText={(text) => onChange(text)}
          // 失焦时再夹一次边界：一边打字一边夹会把「1」夹成「10」，
          // 用户永远打不出 100
          onBlur={() => onChange(clampToSpec(spec, value))}
          keyboardType={spec.type === 'integer' ? 'number-pad' : 'decimal-pad'}
          style={[
            styles.numberInput,
            {
              color: colors.text,
              backgroundColor: colors.surface,
              borderColor: colors.border,
              borderRadius: RADIUS.sm,
            },
          ]}
        />
        <StepButton label="+" onPress={() => nudge(1)} disabled={atBound(spec, value, 'max')} />
      </View>

      {spec.presets && spec.presets.length > 0 ? (
        <View style={[styles.chips, { marginTop: SPACING.sm }]}>
          {spec.presets.map((preset) => {
            const selected = preset.value === value
            return (
              <Pressable
                key={preset.value}
                accessibilityRole="radio"
                accessibilityState={{ selected }}
                onPress={() => onChange(preset.value)}
                style={({ pressed }) => [
                  styles.chip,
                  {
                    backgroundColor: selected ? colors.primarySoft : colors.surface,
                    borderColor: selected ? colors.primary : colors.border,
                    opacity: pressed ? 0.7 : 1,
                  },
                ]}
              >
                <AppText variant="caption" tone={selected ? 'primary' : 'muted'} bold={selected}>
                  {preset.label}
                </AppText>
              </Pressable>
            )
          })}
        </View>
      ) : null}
    </View>
  )
}

/** 到顶/到底时把步进按钮置灰 —— 按了没反应的按钮比灰按钮更让人困惑 */
function atBound(spec: ConversionOptionSpec, value: string, bound: 'min' | 'max'): boolean {
  const limit = bound === 'min' ? spec.min : spec.max
  if (limit === undefined) return false
  const current = Number(value)
  if (!Number.isFinite(current)) return false
  return bound === 'min' ? current <= limit : current >= limit
}

function StepButton({
  label,
  onPress,
  disabled,
}: {
  label: string
  onPress: () => void
  disabled: boolean
}) {
  const colors = useColors()
  return (
    <Pressable
      accessibilityRole="button"
      accessibilityLabel={label === '−' ? '减少' : '增加'}
      accessibilityState={{ disabled }}
      disabled={disabled}
      onPress={onPress}
      style={({ pressed }) => [
        styles.stepButton,
        {
          backgroundColor: colors.surfaceMuted,
          borderColor: colors.border,
          borderRadius: RADIUS.sm,
          opacity: disabled ? 0.35 : pressed ? 0.7 : 1,
        },
      ]}
    >
      <AppText variant="subheading" bold tone={disabled ? 'faint' : 'default'}>
        {label}
      </AppText>
    </Pressable>
  )
}

const styles = StyleSheet.create({
  field: { marginBottom: SPACING.lg },
  fieldHead: { flexDirection: 'row', alignItems: 'baseline', marginBottom: SPACING.sm },
  chips: { flexDirection: 'row', flexWrap: 'wrap' },
  chip: {
    minHeight: TOUCH_TARGET,
    justifyContent: 'center',
    paddingHorizontal: SPACING.md,
    marginRight: SPACING.sm,
    marginBottom: SPACING.sm,
    borderWidth: 1,
    borderRadius: RADIUS.pill,
  },
  switchRow: {
    minHeight: TOUCH_TARGET,
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
  },
  stepperRow: { flexDirection: 'row', alignItems: 'center' },
  stepButton: {
    width: TOUCH_TARGET,
    height: TOUCH_TARGET,
    alignItems: 'center',
    justifyContent: 'center',
    borderWidth: 1,
  },
  numberInput: {
    flex: 1,
    height: TOUCH_TARGET,
    marginHorizontal: SPACING.sm,
    borderWidth: 1,
    paddingHorizontal: SPACING.md,
    textAlign: 'center',
  },
  input: {
    minHeight: TOUCH_TARGET,
    borderWidth: 1,
    paddingHorizontal: SPACING.md,
  },
})
