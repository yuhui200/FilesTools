import { useCallback, useMemo, useState } from 'react'

/**
 * 「选中的一组页面 + 用户排定的顺序」（第四阶段 §11）。
 *
 * 页面的**集合**来自页码输入框和缩略图点选（升序、去重，与后端
 * ``parse_page_range`` 一致）；**顺序**只由拖拽和上移 / 下移改变。
 * 两者分开之后，用户在文本框里补一页不会把他刚拖好的顺序打乱，
 * 而拖拽也不需要反过来改写文本框。
 *
 * ``ordered`` 是纯派生值：留在集合里的页保持原来的相对顺序，
 * 新加入的页按页码顺序追加到末尾。因此不存在「顺序与集合不一致」的中间状态。
 */
export interface PageOrder {
  /** 按输出顺序排列的页码（1 开始） */
  ordered: number[]
  /** 把第 from 页移到 to 位置 */
  move: (from: number, to: number) => void
  /** 顺序是否与升序不同（用来提示用户排序已生效） */
  reordered: boolean
}

export function usePageOrder(selected: number[]): PageOrder {
  const [order, setOrder] = useState<number[]>([])

  const ordered = useMemo(() => {
    const chosen = new Set(selected)
    const kept = order.filter((page) => chosen.has(page))
    const added = selected.filter((page) => !kept.includes(page))
    return [...kept, ...added]
  }, [order, selected])

  const move = useCallback(
    (from: number, to: number) => {
      setOrder(() => {
        const next = [...ordered]
        const [moved] = next.splice(from, 1)
        if (moved === undefined) return ordered
        next.splice(to, 0, moved)
        return next
      })
    },
    [ordered],
  )

  const reordered = useMemo(() => {
    const ascending = [...ordered].sort((a, b) => a - b)
    return ordered.some((page, index) => page !== ascending[index])
  }, [ordered])

  return { ordered, move, reordered }
}
