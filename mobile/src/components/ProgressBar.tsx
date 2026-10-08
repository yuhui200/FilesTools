/**
 * 进度条（规格 §二十）。
 *
 * 有一条硬规则：**真实的百分比才画确定的条**。只有 PDF → Word 会上报
 * 真实页码进度，别的转换服务端给的是 `null` —— 那时画一条来回跑的
 * 不确定条，绝不拿「已过秒数」去凑一个数字。用户看到 47% 卡了十分钟，
 * 比看到一个转圈要难受得多。
 */

import React, { useEffect, useRef } from 'react'
import { Animated, Easing, StyleSheet, View } from 'react-native'

import { RADIUS, useColors } from '@/theme'

export interface ProgressBarProps {
  /** 0–100；`null` 走不确定动画 */
  value: number | null
  /** 条高，默认 6 */
  height?: number
}

export function ProgressBar({ value, height = 6 }: ProgressBarProps) {
  const colors = useColors()
  const indeterminate = value === null

  // 不确定态的滑块。用 Animated 的循环，卸载时必须 stop ——
  // 一个跑着的 Animation 会一直占着原生驱动，页面切走后还在耗电
  const slide = useRef(new Animated.Value(0)).current
  useEffect(() => {
    if (!indeterminate) return
    const loop = Animated.loop(
      Animated.timing(slide, {
        toValue: 1,
        duration: 1100,
        easing: Easing.inOut(Easing.ease),
        useNativeDriver: true,
      }),
    )
    loop.start()
    return () => loop.stop()
  }, [indeterminate, slide])

  const clamped = value === null ? 0 : Math.max(0, Math.min(100, value))

  return (
    <View
      accessibilityRole="progressbar"
      style={[styles.track, { height, backgroundColor: colors.track, borderRadius: height / 2 }]}
    >
      {indeterminate ? (
        <Animated.View
          style={[
            styles.fill,
            {
              backgroundColor: colors.primary,
              borderRadius: height / 2,
              width: '35%',
              transform: [
                {
                  translateX: slide.interpolate({
                    inputRange: [0, 1],
                    // 从完全在左边外面滑到完全在右边外面。宽度按百分比算，
                    // 具体像素随容器变，所以给一个足够大的范围
                    outputRange: [-200, 320],
                  }),
                },
              ],
            },
          ]}
        />
      ) : (
        <View
          style={[
            styles.fill,
            {
              backgroundColor: colors.primary,
              borderRadius: height / 2,
              width: `${clamped}%`,
            },
          ]}
        />
      )}
    </View>
  )
}

const styles = StyleSheet.create({
  track: { width: '100%', overflow: 'hidden' },
  fill: { height: '100%' },
})
