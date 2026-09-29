/**
 * 服务器组件可用性的**三态**判定。
 *
 * 「后端明确说没有」和「后端压根没提这个字段」是两件不同的事，
 * 不能都写成 `!value` 混为一谈：
 *
 * - `unavailable` —— 后端明确回了 `false`，组件确实没装。
 *   这是**服务器的问题**，该让管理员去装组件。
 * - `unknown` —— 响应里**没有这个字段**。这通常不是「没有组件」，
 *   而是**运行中的后端比前端旧**（字段是后来的阶段才加的），
 *   那个接口本身都不存在。此时说「缺少组件」，是让用户去找一个
 *   并不存在的缺失组件 —— 把人往错的方向支使。
 *
 * 实际踩到过：旧后端进程 + 新前端产物时，PDF 转 Word 页面显示
 * 「当前服务器缺少 Word 生成组件，请联系管理员。」，而服务器上
 * python-docx 装得好好的，真正该做的是重启后端。
 */
export type FeatureAvailability = 'available' | 'unavailable' | 'unknown'

export function featureAvailability(
  value: boolean | undefined | null,
): FeatureAvailability {
  if (value === true) return 'available'
  if (value === false) return 'unavailable'
  return 'unknown'
}

/**
 * 后端版本过旧、缺少接口时的统一说明。
 *
 * 与「缺少组件」的区别在于**它指出了真正该做的事**：重启/更新后端，
 * 而不是去找一个并不存在、也不需要安装的组件。
 */
export const STALE_BACKEND_MESSAGE =
  '当前后端服务版本比页面旧，缺少这个功能所需的接口。请重启或更新后端服务后刷新页面。'
