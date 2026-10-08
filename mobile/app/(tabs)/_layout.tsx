/**
 * 四个底部 Tab（规格 §六）：首页 / 工具 / 历史 / 我的。
 *
 * Tab 图标用字符，不引图标库 —— 四个图标不值得再加一个依赖，
 * 而且 `@expo/vector-icons` 会在首屏多加载一份字体文件。
 */

import { Tabs } from 'expo-router'
import React from 'react'
import { Text, type ColorValue } from 'react-native'

import { useColors } from '@/theme'

function TabIcon({ glyph, color }: { glyph: string; color: ColorValue }) {
  return <Text style={{ fontSize: 20, color }}>{glyph}</Text>
}

export default function TabsLayout() {
  const colors = useColors()

  return (
    <Tabs
      screenOptions={{
        headerShown: false,
        tabBarActiveTintColor: colors.primary,
        tabBarInactiveTintColor: colors.textFaint,
        tabBarStyle: {
          backgroundColor: colors.surface,
          borderTopColor: colors.border,
        },
        tabBarLabelStyle: { fontSize: 12 },
        sceneStyle: { backgroundColor: colors.background },
      }}
    >
      <Tabs.Screen
        name="index"
        options={{
          title: '首页',
          tabBarIcon: ({ color }) => <TabIcon glyph="⌂" color={color} />,
        }}
      />
      <Tabs.Screen
        name="tools"
        options={{
          title: '工具',
          tabBarIcon: ({ color }) => <TabIcon glyph="▤" color={color} />,
        }}
      />
      <Tabs.Screen
        name="history"
        options={{
          title: '历史',
          tabBarIcon: ({ color }) => <TabIcon glyph="◷" color={color} />,
        }}
      />
      <Tabs.Screen
        name="profile"
        options={{
          title: '我的',
          tabBarIcon: ({ color }) => <TabIcon glyph="◑" color={color} />,
        }}
      />
    </Tabs>
  )
}
