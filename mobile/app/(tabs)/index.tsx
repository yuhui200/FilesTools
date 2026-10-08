/**
 * 路由文件只做一件事：把 `src/screens` 里的页面接到 expo-router 上。
 *
 * 页面的实现在 `src/screens/` —— 那边不依赖「文件即路由」这套约定，
 * 将来要加单元测试或者换导航库，改的只是这一层薄壳。
 */

export { default } from '@/screens/HomeScreen'
