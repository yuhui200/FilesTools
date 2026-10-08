/**
 * 根布局。
 *
 * 这里只做两件事：装配三个 Provider，然后给导航定一个 Stack。
 * 四个 Tab 在 `(tabs)/_layout.tsx`，**不在这里** —— 详情页
 * （选参数、看进度）必须能盖住 Tab 栏，所以它们得是 Stack 上的兄弟，
 * 而不是 Tab 里的页面。
 *
 * 没有全局状态库：App 的状态只有「能力表」「主题」「历史」三份，
 * 前两份各有一个 Context，历史是即用即读。引入 Redux 之类只会让
 * 这十几个页面变重。
 */

import { Stack } from 'expo-router'
import { StatusBar } from 'expo-status-bar'
import React from 'react'
import { SafeAreaProvider } from 'react-native-safe-area-context'

import { CapabilitiesProvider } from '@/state/CapabilitiesProvider'
import { ThemeProvider, useColors, useTheme } from '@/theme'

function RootStack() {
  const colors = useColors()
  const { scheme } = useTheme()

  return (
    <>
      {/* 深色主题下状态栏文字要转白，否则在深底上看不见 */}
      <StatusBar style={scheme === 'dark' ? 'light' : 'dark'} />
      <Stack
        screenOptions={{
          headerShown: false,
          contentStyle: { backgroundColor: colors.background },
        }}
      >
        <Stack.Screen name="(tabs)" />
        {/*
          两个详情页用系统导航栏而不是自绘的返回条：
          左滑返回、Android 的返回键、iPad 上的转场全都由系统接管，
          自己画一个「←」按钮这三样就全丢了。
        */}
        <Stack.Screen
          name="convert/[capabilityId]"
          options={{
            headerShown: true,
            title: '设置',
            headerStyle: { backgroundColor: colors.surface },
            headerTintColor: colors.text,
            headerTitleStyle: { color: colors.text },
          }}
        />
        <Stack.Screen
          name="task/[batchId]"
          options={{
            headerShown: true,
            title: '处理中',
            headerStyle: { backgroundColor: colors.surface },
            headerTintColor: colors.text,
            headerTitleStyle: { color: colors.text },
          }}
        />
      </Stack>
    </>
  )
}

export default function RootLayout() {
  return (
    <SafeAreaProvider>
      <ThemeProvider>
        <CapabilitiesProvider>
          <RootStack />
        </CapabilitiesProvider>
      </ThemeProvider>
    </SafeAreaProvider>
  )
}
