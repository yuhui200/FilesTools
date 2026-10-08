/**
 * 文件选择（规格 §十）。
 *
 * **平台差异全部收在这一层**：Android 给的是 `content://`（可能读不到
 * 文件名与大小），iOS 给的是临时沙盒路径，Web 给的是 `File` 对象。
 * 业务组件只认 :class:`MobileFile` 那三个字段，不许自己判断平台。
 *
 * 这里返回的 `mimeType` / 扩展名**只是体验层过滤**，不是安全判断 ——
 * 真正的类型判定在服务端按文件内容做（规格 §三十六 / §三十七）。
 */

import * as DocumentPicker from 'expo-document-picker'

import { CLIENT_CODES, ApiError } from '@/services/api/client'
import type { MobileFile } from '@/types'

export interface PickOptions {
  /** 允许多选。转换接口本身就支持一次提交多个，默认允许 */
  multiple?: boolean
  /**
   * 允许的 MIME 类型。不传就是不限类型。
   *
   * 注意这是**系统文件选择器**的过滤条件，`image` 这类通配在
   * Android 与 iOS 上表现不同（有的机型会把它退化成「只能选图片」，
   * 有的干脆忽略）。所以它只用来减少误选，不能当成白名单 ——
   * 选进来的文件仍要交给服务端判定。
   */
  mimeTypes?: string[]
}

/**
 * 打开系统文件选择器。
 *
 * 用户取消时返回**空数组**而不是抛异常 —— 「没选文件」是正常操作，
 * 不是错误。调用方因此不需要 try/catch 就能处理。
 */
export async function pickFiles(options: PickOptions = {}): Promise<MobileFile[]> {
  const { multiple = true, mimeTypes } = options

  let result: DocumentPicker.DocumentPickerResult
  try {
    result = await DocumentPicker.getDocumentAsync({
      type: mimeTypes && mimeTypes.length > 0 ? mimeTypes : '*/*',
      multiple,
      // ⚠️ 必须**关掉**。
      //
      // 打开时（默认行为）Android 会把文件复制到 `context.cacheDir` ——
      // 那是 **Expo Go 宿主 App** 的缓存目录
      // （`host.exp.exponent/cache/DocumentPicker/<uuid>.<ext>`），
      // 而 Expo Go 给每个体验的是**受限**的文件权限：只有体验自己的目录可用。
      // 于是 `expo-file-system` 读这个 `file://` 会明确拒绝：
      //
      //     Missing 'READ' permission for accessing the file.
      //
      // 关掉之后拿到的是文档提供方给的 `content://`。`expo-file-system`
      // 对 content URI **不做路径权限检查**
      // （`FileSystemPath.kt::checkPermission` 里 `uri.isContentUri` 直接返回 true），
      // 由 `ContentProviderFile` 走 ContentResolver 读 —— 这条在 Expo Go 里能走通。
      //
      // 代价：读的是原始授权，不落副本。授权覆盖本次选择所在的 Activity，
      // 我们选完几秒内就上传，够用。
      copyToCacheDirectory: false,
      // ⚠️ 必须显式关掉。这个参数在**原生端无效**，但 Web 端默认是开着的，
      // 而 `expo-document-picker` 的入口函数把默认值写成了 `true` ——
      // 于是 Web 上每选一个文件都会先被读成一整条 base64 字符串。
      // 我们从来不用 base64（上传走 FormData 的原生文件对象），
      // 几十 MB 的文件白读一遍，手机浏览器上直接就是一次内存尖峰。
      base64: false,
    })
  } catch {
    throw new ApiError('无法打开文件选择器，请重试', CLIENT_CODES.file)
  }

  if (result.canceled || !result.assets) return []

  return result.assets.map(toMobileFile)
}

/** 把系统给的 asset 归一成 :class:`MobileFile` */
function toMobileFile(asset: DocumentPicker.DocumentPickerAsset): MobileFile {
  return {
    uri: asset.uri,
    name: nameOf(asset),
    // size 在部分 Android 提供方那里拿不到，如实留空，不编一个 0
    ...(typeof asset.size === 'number' ? { size: asset.size } : {}),
    ...(asset.mimeType ? { mimeType: asset.mimeType } : {}),
    // 只有 Web 会给这个字段（`@platform web`）。上传时非它不可 ——
    // 见 `MobileFile.blob` 的注释，没有它浏览器会把文件发成 "[object Object]"
    ...(asset.file ? { blob: asset.file } : {}),
  }
}

/**
 * 文件名兜底。
 *
 * 少数 Android 文档提供方不给 `name`（或给一个空串）。上传时没有文件名，
 * 服务端就只能显示一个编号，用户认不出是哪个文件 —— 所以从 URI 最后
 * 一段还原。还原不出来时给一个中性名字，**不猜扩展名**：
 * 猜错会让服务端的类型判定与界面提示打架。
 */
function nameOf(asset: DocumentPicker.DocumentPickerAsset): string {
  const given = (asset.name ?? '').trim()
  if (given !== '') return given

  const raw = asset.uri.split('?')[0]
  const last = raw.substring(raw.lastIndexOf('/') + 1)
  const decoded = safeDecode(last)
  if (decoded !== '' && decoded.includes('.')) return decoded

  const extension = extensionOf(asset.mimeType)
  return extension === '' ? '未命名文件' : `未命名文件${extension}`
}

function safeDecode(text: string): string {
  try {
    return decodeURIComponent(text)
  } catch {
    // URI 里有非法的百分号编码，原样用
    return text
  }
}

/** 极少数常见的 MIME 才给扩展名 —— 给不出来就不给，不硬猜 */
const MIME_EXTENSIONS: Record<string, string> = {
  'image/jpeg': '.jpg',
  'image/png': '.png',
  'image/webp': '.webp',
  'image/bmp': '.bmp',
  'image/gif': '.gif',
  'image/tiff': '.tiff',
  'image/heic': '.heic',
  'image/svg+xml': '.svg',
  'application/pdf': '.pdf',
  'text/plain': '.txt',
  'text/markdown': '.md',
  'text/html': '.html',
  'application/vnd.openxmlformats-officedocument.wordprocessingml.document': '.docx',
  'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': '.xlsx',
  'application/vnd.openxmlformats-officedocument.presentationml.presentation': '.pptx',
}

function extensionOf(mimeType: string | undefined): string {
  if (!mimeType) return ''
  return MIME_EXTENSIONS[mimeType.toLowerCase()] ?? ''
}
