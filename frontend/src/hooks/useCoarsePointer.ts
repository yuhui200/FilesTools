import { useEffect, useState } from 'react'

/** 手指操作的设备：手机、平板（`hover: none` 且 `pointer: coarse`） */
const QUERY = '(hover: none) and (pointer: coarse)'

/**
 * 当前设备是不是「用手指点」的（第四阶段 §9 / §10）。
 *
 * 上传区域的文案要分设备：电脑上「拖放文件到这里」是对的，
 * 手机上根本拖不动，只该说「选择文件」——点开系统文件选择器。
 * 用媒体查询而不是 UA 判断：能触摸的笔记本仍然有鼠标，
 * 而 iPad 接上键盘后依旧是用手指点。
 *
 * 服务端渲染 / 首帧拿不到 matchMedia 时按「非触摸」处理，
 * 桌面端因此不会出现文案闪烁；触摸设备最多在首帧后纠正一次。
 */
export function useCoarsePointer(): boolean {
  const [coarse, setCoarse] = useState(() =>
    typeof window === 'undefined' || typeof window.matchMedia !== 'function'
      ? false
      : window.matchMedia(QUERY).matches,
  )

  useEffect(() => {
    if (typeof window.matchMedia !== 'function') return
    const query = window.matchMedia(QUERY)
    const update = () => setCoarse(query.matches)
    update()
    query.addEventListener('change', update)
    return () => query.removeEventListener('change', update)
  }, [])

  return coarse
}
