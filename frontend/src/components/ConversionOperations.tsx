import { PdfOperationCard } from '@/components/PdfOperationCard'
import type { ConversionCapability, ConversionCategoryOption, ConversionFormatOption } from '@/types'

interface ConversionOperationsProps {
  /** ``capabilities.operations`` 原样传进来，不在这里筛 */
  operations: ConversionCapability[]
  /** ``capabilities.categories``：分组标题按它取，前端不自己拼类别名 */
  categories: ConversionCategoryOption[]
  formats: ConversionFormatOption[]
  maxBytes: number
}

/**
 * 每个类别一句话的说明。
 *
 * 文案按类别手写（这是给用户看的散文，不是能力数据），但**分几栏、
 * 每栏叫什么**由 ``categories`` 决定 —— 未知类别会落到最后那句通用说明上，
 * 不会因为「忘了加一个分支」而整栏消失。
 */
const CATEGORY_DESCRIPTIONS: Record<string, string> = {
  pdf: '合并、拆分、压缩、删页、提取页面与多图合成 PDF。这些是整份文件级的操作，不会排进上面的批量队列，点开始后直接处理。',
  image: '只看不改：读取图片信息，不生成新文件，也就没有下载这一步。',
}
const FALLBACK_DESCRIPTION =
  '这些是整份文件级的操作，不会排进上面的批量队列，点开始后直接处理。'

/**
 * 统一转换中心里的 PDF 工具区（9b，决策 B）。
 *
 * 合并 / 拆分 / 压缩 / 删页 / 提取页 / 多图合成 PDF 这六件事
 * **不是** 1→1 的转换，塞进批量队列只会两边都别扭，所以它们在注册表里
 * 登记成 ``operation`` 条目、执行仍走各自原来的接口，而入口与参数面板
 * 收在这里 —— 用户不必先想「这个该去哪个页面」。
 *
 * 摆哪几张卡片、叫什么名字、收什么文件、有哪些参数，全部读自服务端；
 * 这里**没有**一份手抄的工具清单（§十六）。
 */
export function ConversionOperations({
  operations,
  categories,
  formats,
  maxBytes,
}: ConversionOperationsProps) {
  if (operations.length === 0) return null

  // 按类别分栏。第十阶段 A 之前这里只有 PDF 操作，所有卡片都塞在一句
  // 「PDF 工具」底下；加了「查看图片信息」之后，那张卡片就会挂在一个
  // 说自己是 PDF 的标题下面 —— 标题在说谎，用户也找不到它。
  //
  // 类别的顺序取 ``categories``（服务端给的顺序），不在前端排。
  const groups = categories
    .map((category) => ({
      category,
      entries: operations.filter((entry) => entry.category === category.value),
    }))
    .filter((group) => group.entries.length > 0)
  // 类别认不出来的条目也要露出来，不能因为分类对不上就消失
  const known = new Set(categories.map((category) => category.value))
  const orphans = operations.filter((entry) => !known.has(entry.category))

  return (
    <>
      {[...groups, ...(orphans.length > 0 ? [{ category: null, entries: orphans }] : [])].map(
        (group) => (
          <section
            key={group.category?.value ?? 'unknown'}
            className="border-t border-slate-200 pt-8"
          >
            <div className="max-w-2xl">
              <h2 className="text-lg font-semibold tracking-tight text-slate-900">
                {group.category ? `${group.category.label}工具` : '其它工具'}
              </h2>
              <p className="mt-2 text-sm leading-relaxed text-slate-600">
                {(group.category && CATEGORY_DESCRIPTIONS[group.category.value]) ??
                  FALLBACK_DESCRIPTION}
              </p>
            </div>

            <div className="mt-5 grid grid-cols-1 gap-4 lg:grid-cols-2">
              {group.entries.map((entry) => (
                <PdfOperationCard
                  key={entry.id}
                  entry={entry}
                  formats={formats}
                  maxBytes={maxBytes}
                />
              ))}
            </div>
          </section>
        ),
      )}
    </>
  )
}
