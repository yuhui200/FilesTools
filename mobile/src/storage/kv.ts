/**
 * 本地键值存储。
 *
 * 只做一件事：把 AsyncStorage 的读写**都包上 try/catch**。
 * 这不是防御性编程的洁癖 —— 移动端的存储是真会失败的：
 * 磁盘写满、系统在后台清数据、企业设备策略禁掉存储，都会让
 * `AsyncStorage.setItem` 抛出来。主题偏好或历史记录读不到，
 * 绝不该让整个 App 白屏。
 *
 * 读失败一律当作「没有这个键」，写失败**如实返回 false**，
 * 由调用方决定要不要提示（历史记录写不进去就得说一声，
 * 否则用户以为存了、下次打开却没有）。
 */

import AsyncStorage from '@react-native-async-storage/async-storage'

const PREFIX = 'filetools.'

export function key(name: string): string {
  return `${PREFIX}${name}`
}

export async function readJson<T>(name: string): Promise<T | null> {
  try {
    const raw = await AsyncStorage.getItem(key(name))
    if (raw === null) return null
    return JSON.parse(raw) as T
  } catch {
    // 读不到或解不开都当作「没有」：损坏的本地数据不该让页面挂掉
    return null
  }
}

export async function writeJson(name: string, value: unknown): Promise<boolean> {
  try {
    await AsyncStorage.setItem(key(name), JSON.stringify(value))
    return true
  } catch {
    return false
  }
}

export async function remove(name: string): Promise<void> {
  try {
    await AsyncStorage.removeItem(key(name))
  } catch {
    // 删不掉不影响任何功能
  }
}
