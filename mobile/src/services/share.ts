/**
 * 分享 / 用其它 App 打开结果（规格 §十八）。
 *
 * 传进来的必须是**本地文件地址**（`services/download.ts` 产出的那个），
 * 不是服务端的 `download_url` —— 那个是一次性的、而且还在服务器上，
 * 交给系统分享面板只会得到一句“无法分享”。
 *
 * ## 不编 UTI
 *
 * iOS 的 `UTI` 与 Android 的 `mimeType` 是两套命名。我们只发得出 MIME
 * （服务端结果里带的 `media_type`），所以**只在 Android 上传 `mimeType`**，
 * iOS 让它按文件扩展名自己认。硬编一张 UTI 表去猜，猜错了分享目标会少一半，
 * 而且没有任何办法在测试里发现。
 */

import { Platform } from 'react-native'
import * as Sharing from 'expo-sharing'

import { ApiError, CLIENT_CODES } from '@/services/api/client'

export interface ShareRequest {
  /** 本地文件地址（`file://…` / `blob:…`） */
  uri: string
  /** 文件名，分享面板的标题与部分目标的默认文件名用它 */
  name?: string
  /** 结果 MIME，例如 `image/png` */
  mimeType?: string
}

/**
 * 这台设备现在能不能分享。
 *
 * 返回 false 是**正常情况**而不是错误：Web 上没有 Web Share API 的浏览器、
 * 或者页面不是 HTTPS，都会是 false。界面据此把「分享」按钮换成「下载」。
 */
export async function isShareAvailable(): Promise<boolean> {
  try {
    return await Sharing.isAvailableAsync()
  } catch {
    return false
  }
}

/**
 * 打开系统分享面板。
 *
 * 用户在面板里点了取消**不算失败**。不同平台上「取消」有的表现为正常
 * resolve、有的表现为 reject 一个带 cancel 字样的错误，两种都当成功处理
 * （见下）。只有真的打不开面板才抛错。
 */
export async function shareFile(request: ShareRequest): Promise<void> {
  if (!request.uri) {
    throw new ApiError('没有可分享的文件，请先下载', CLIENT_CODES.file)
  }

  if (!(await isShareAvailable())) {
    throw new ApiError(
      Platform.OS === 'web'
        ? '这个浏览器不支持分享文件，请直接下载'
        : '这台设备不支持分享文件',
      CLIENT_CODES.file,
    )
  }

  try {
    await Sharing.shareAsync(request.uri, {
      ...(request.name ? { dialogTitle: request.name } : {}),
      // Android 认 mimeType；iOS 认 UTI，而 UTI 我们发不出来（见文件头），不猜
      ...(Platform.OS === 'android' && request.mimeType ? { mimeType: request.mimeType } : {}),
    })
  } catch (error) {
    const message = error instanceof Error ? error.message : ''
    // 用户取消在不同平台上分别表现为 reject 或 resolve，这里两种都当成功处理
    if (/cancel/i.test(message)) return
    throw new ApiError('无法打开分享面板，请重试', CLIENT_CODES.file)
  }
}
