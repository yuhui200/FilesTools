export * from './api'

import type { ConversionStatus } from './api'

/**
 * 一个待上传的文件（规格 §十）。
 *
 * Android 的 `content://` 与 iOS 的 `file://` 行为不同（iOS 还可能给一个
 * 只在本次会话有效的临时路径），所以**业务组件一律不碰这个差异** ——
 * 由 `services/filePicker.ts` 统一产出这个形状，其余代码只认这三个字段。
 *
 * `size` / `mimeType` 可能拿不到（部分 Android provider 不返回大小），
 * 所以是可选的。**但都不能当作可信输入**：真正的类型判定在服务端
 * （规格 §三十六 / §三十七），这里只用来做界面上的初步过滤和提示。
 */
export interface MobileFile {
  uri: string
  name: string
  size?: number
  mimeType?: string
  /**
   * Web 专用的上传载荷：浏览器里那个真正的 `File` 对象。
   *
   * 浏览器的 `FormData` 只认 `Blob` / `File` / 字符串 —— 塞一个普通对象进去
   * 会被字符串化成 `"[object Object]"`，服务端收到的「文件」就是这两个方括号，
   * 结果是 400。所以 Web 上必须把真对象带上。
   *
   * 原生端留空，由 `services/api/conversion.ts` 按 `uri` 自己造一个
   * `expo/fetch` 认的 part（**不是** RN 那个 `{uri, name, type}` 三件套，
   * Expo SDK 57 的全局 `fetch` 已经不认它了，原因写在那边）。
   */
  blob?: Blob
}

/**
 * 本地历史里的一条记录（规格 §十九）。
 *
 * **只存元数据，不存文件本体** —— 转换结果在服务器的临时目录里，
 * 过期就没了；App 这边存一份副本既占空间、又和「一次性下载令牌」的
 * 设计打架（令牌用过就失效，存下来也下不动）。
 */
export interface HistoryEntry {
  /** 批次号，同时是这条记录的主键 */
  batchId: string
  /** 提交时的第一条文件名，用作列表标题 */
  filename: string
  sourceType: string
  targetType: string
  /** 提交时的能力 ID，失败后「重新转换」要按它回到同一个工具 */
  capabilityId: string
  /** 提交时的文件数：单文件与批量的结果卡文案不同 */
  total: number
  /** 最后一次看到的整批状态 */
  status: ConversionStatus
  /** 提交那一刻的时间戳（毫秒）。列表按它倒序 */
  createdAt: number
  /** 结果文件名（成功的项），用于「重新下载」时显示 */
  resultFilename: string | null
  /** 服务器上这份结果是不是已经取不到了（过期 / 被清理 / 已下载过） */
  resultExpired: boolean
}

/** App 版本号。与 `mobile/package.json` 的 version 一起改，见「我的」页 */
export const APP_VERSION = '0.1.0'
