import { useCallback, useEffect, useMemo, useState } from 'react'

import { Alert } from '@/components/Alert'
import { Button } from '@/components/Button'
import { ConversionGroupCard } from '@/components/ConversionGroupCard'
import { ConversionOperations } from '@/components/ConversionOperations'
import { ConversionSummary } from '@/components/ConversionSummary'
import { ConversionUnsupported } from '@/components/ConversionUnsupported'
import { Dropzone } from '@/components/Dropzone'
import { ToolPage } from '@/components/ToolPage'
import { useConversionGroups } from '@/hooks/useConversionGroups'
import { useFilePreviews, type FilePreview } from '@/hooks/useFilePreviews'
import { useServerConfig } from '@/hooks/useServerConfig'
import { fetchConversionCapabilities } from '@/services/api'
import type { ConversionCapabilities } from '@/types'
import { sourceGroupKey } from '@/utils/conversion'
import { formatBytes } from '@/utils/format'

const EMPTY_PREVIEW: FilePreview = { url: null, width: null, height: null }

/** capabilities 到位之前的占位：空矩阵，任何文件都分不出组 */
const EMPTY_CAPABILITIES: ConversionCapabilities = {
  matrix: {},
  groups: [],
  targets: [],
  office_available: false,
  pdf_to_word_available: false,
  ocr_available: false,
  notes: [],
  pdf_to_word_note: '',
  // 第九阶段追加的四个键。空着就是「还不知道」，分组自然也就分不出来 ——
  // 与「服务器说什么都不能转」在界面上是同一个结果，都等真数据到位。
  categories: [],
  formats: [],
  conversions: [],
  operations: [],
}

/** 页面的 SEO 标题与描述。离开页面时还原，不影响其它页面 */
const TITLE = 'Free Online File Converter | FileTools'
const DESCRIPTION =
  '在线上传图片、Word、Excel、PPT、TXT 与 PDF，选择目标格式后一次批量转换，逐项或打包下载。'

export function Convert() {
  const { config, offlineMessage } = useServerConfig()
  const [capabilities, setCapabilities] = useState<ConversionCapabilities | null>(null)
  const [capabilityError, setCapabilityError] = useState<string | null>(null)

  useEffect(() => {
    const controller = new AbortController()
    fetchConversionCapabilities(controller.signal)
      .then((value) => setCapabilities(value))
      .catch(() => {
        if (controller.signal.aborted) return
        setCapabilityError(
          '读取服务器支持的转换类型失败，暂时无法上传。请刷新页面重试；问题持续出现时可以稍后再来。',
        )
      })
    return () => controller.abort()
  }, [])

  // 页面标题与描述（§三十六）。离开时还原成站点默认值，
  // 不能只设不清 —— 那样从本页跳到别的工具页会顶着转换中心的标题。
  useEffect(() => {
    const previousTitle = document.title
    document.title = TITLE
    const meta = document.querySelector<HTMLMetaElement>('meta[name="description"]')
    const previousDescription = meta?.content ?? null
    if (meta) meta.content = DESCRIPTION
    return () => {
      document.title = previousTitle
      if (meta && previousDescription !== null) meta.content = previousDescription
    }
  }, [])

  /**
   * 拖拽区的白名单 = 各工具白名单 ∪ 能力目录里登记为「可作输入」的扩展名。
   *
   * 它只决定「系统文件选择器默认筛出什么」以及「哪些扩展名连试都不用试」，
   * 能不能转由服务端的 capabilities 说了算 —— 两者不是一回事：
   * 服务器没装 LibreOffice 时 .docx 仍在白名单里（格式本身是支持的），
   * 它会落到「不支持转换」清单并说明原因，而不是被悄悄吞掉。
   *
   * 后半截是第九阶段补上的。`/api/config` 那几份清单是**按工具**分的
   * （图片 / PDF / Word / Excel / PPT / 文本），而这里是**全站**工作台，
   * 它的输入域由能力目录决定。少了这一步，`doc.md`、`page.html` 会在本地
   * 就被拦下，连一次上传的机会都没有 —— 服务端明明认得它们。能力目录才是
   * 唯一事实来源（§十六/§四十二），前端不再手抄第二份格式表。
   */
  const rules = useMemo(() => {
    const declaredSources =
      capabilities?.formats.filter((item) => item.is_source).flatMap((item) => item.extensions) ??
      []
    return {
      allowedExtensions: [
        ...new Set([
          ...config.allowed_image_extensions,
          ...config.allowed_pdf_extensions,
          ...config.allowed_word_extensions,
          ...config.allowed_excel_extensions,
          ...config.allowed_powerpoint_extensions,
          ...config.allowed_text_extensions,
          ...declaredSources,
        ]),
      ],
      maxBytes: config.max_upload_bytes,
    }
  }, [config, capabilities])

  const task = useConversionGroups({
    rules,
    maxFiles: config.max_batch_files,
    maxTotalBytes: config.max_batch_total_bytes,
    // capabilities 还没到手时不让分组：前端不自己维护一份源格式清单（§十九）
    capabilities: capabilities ?? EMPTY_CAPABILITIES,
  })

  const { groups, unsupported, addFiles, clearUnsupported } = task
  // 缩略图按「所有文件拼成一串」生成，再按各组在串里的位置切片 ——
  // 一个文件一个 hook 会随文件数变多，这里是固定一次调用。
  const allFiles = useMemo(() => groups.flatMap((group) => group.files), [groups])
  const allPreviews = useFilePreviews(allFiles)

  const offsets = useMemo(() => {
    let offset = 0
    return groups.map((group) => {
      const start = offset
      offset += group.files.length
      return start
    })
  }, [groups])

  const handleSelect = useCallback(
    (files: File[]) => {
      addFiles(files)
    },
    [addFiles],
  )

  const pendingGroups = groups.filter(
    (group) => group.batch === null && !group.submitting,
  ).length
  const canStartAll = pendingGroups > 0 && !task.busy
  const totalBytes = allFiles.reduce((sum, file) => sum + file.size, 0)
  /** 能力矩阵到手之前不开放上传：分不出组的话文件只会白白落进「不支持」 */
  const ready = capabilities !== null

  return (
    <ToolPage
      category="convert"
      title="统一转换中心"
      description={
        <>
          上传任意支持的文件，系统会按格式自动分组，每组选好目标格式后一次批量转换，
          结果可以逐个下载，也可以打包成 ZIP 一起拿走。一次最多 {config.max_batch_files} 个文件。
        </>
      }
      offlineMessage={offlineMessage}
    >
      {capabilityError && <Alert tone="error">{capabilityError}</Alert>}

      {/* 服务器缺组件时如实说明：这些类型的转换成不了，不是用户操作的问题 */}
      {capabilities && capabilities.notes.length > 0 && (
        <Alert tone="warning">
          <p className="font-medium">部分转换当前不可用</p>
          <ul className="mt-1 space-y-0.5">
            {capabilities.notes.map((note) => (
              <li key={note} className="text-xs">
                {note}
              </li>
            ))}
          </ul>
        </Alert>
      )}

      {task.error && (
        <Alert tone="error" onDismiss={task.dismissError}>
          {task.error}
        </Alert>
      )}

      {task.downloadError && <Alert tone="error">{task.downloadError}</Alert>}

      {/* 1. 上传 */}
      {ready && groups.length === 0 && unsupported.length === 0 && (
        <Dropzone
          multiple
          onSelect={handleSelect}
          onError={task.reportError}
          onReject={task.rejectFiles}
          rules={rules}
          kind="文件"
          hint={`支持图片、Word、Excel、PPT、TXT、HTML、Markdown 与 PDF，单个最大 ${formatBytes(
            config.max_upload_bytes,
          )}，一次最多 ${config.max_batch_files} 个`}
        />
      )}

      {/* 2. 汇总 + 全部开始 */}
      {(groups.length > 0 || unsupported.length > 0) && (
        <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-3">
          <ConversionSummary
            groups={groups}
            unsupportedCount={unsupported.length}
            pendingGroups={pendingGroups}
          />
          <div className="flex items-center gap-2">
            {pendingGroups > 1 && (
              <Button size="sm" disabled={!canStartAll} onClick={task.startAll}>
                全部开始
              </Button>
            )}
            {totalBytes > 0 && (
              <span className="text-xs text-slate-500">
                {allFiles.length} 个文件 · 合计 {formatBytes(totalBytes)}
              </span>
            )}
          </div>
        </div>
      )}

      {/* 3. 各组 */}
      {groups.map((group, position) => (
        <ConversionGroupCard
          key={group.id}
          group={group}
          capabilities={capabilities ?? EMPTY_CAPABILITIES}
          previews={previewsFor(
            group.sourceType,
            capabilities,
            allPreviews,
            offsets[position] ?? 0,
            group.files.length,
          )}
          maxFiles={config.max_batch_files}
          maxBytes={config.max_upload_bytes}
          takenItems={task.takenItems}
          downloading={task.downloading}
          savedItemPaths={task.savedItemPaths}
          savedGroupPath={task.savedGroupPaths[group.id] ?? null}
          onChangeTarget={task.setTarget}
          onChangeOption={task.setOptionValue}
          onChangeSize={task.setSizeOptions}
          onRemoveFile={task.removeFile}
          onStart={task.startGroup}
          onCancel={task.cancelGroup}
          onRetry={task.retryTask}
          onDownloadItem={task.downloadItem}
          onDownloadGroup={task.downloadGroup}
          onRemoveGroup={task.removeGroup}
        />
      ))}

      {/* 4. 继续添加 */}
      {ready && groups.length > 0 && allFiles.length < config.max_batch_files && (
        <Dropzone
          compact
          multiple
          onSelect={handleSelect}
          onError={task.reportError}
          onReject={task.rejectFiles}
          rules={rules}
          kind="文件"
          hint="可以继续添加，新文件会按格式自动并进对应的组"
        />
      )}

      {/* 5. 转不了的文件 */}
      <ConversionUnsupported
        files={unsupported}
        notes={capabilities?.notes ?? []}
        onClear={clearUnsupported}
      />

      {groups.length > 0 && (
        <div className="flex justify-end">
          <Button size="sm" variant="ghost" onClick={task.reset}>
            清空全部
          </Button>
        </div>
      )}

      {/* 6. PDF 工具（§十二 的 operation 条目）：与批量转换同一页，不让用户
             先想「这个该去哪个页面」。能力、参数、放行哪些文件全部来自服务端 */}
      <ConversionOperations
        operations={capabilities?.operations ?? []}
        categories={capabilities?.categories ?? []}
        formats={capabilities?.formats ?? []}
        maxBytes={config.max_upload_bytes}
      />
    </ToolPage>
  )
}

/**
 * 取这一组的缩略图。
 *
 * 非图片的组一律给空占位：把 PDF / DOCX 的 object URL 塞进 `<img>`
 * 只会得到一个碎图标，不如老老实实显示文件图标。
 */
function previewsFor(
  sourceType: string,
  capabilities: ConversionCapabilities | null,
  allPreviews: FilePreview[],
  offset: number,
  count: number,
): FilePreview[] {
  const group = capabilities ? sourceGroupKey(capabilities, sourceType) : null
  if (group !== 'image') return Array.from({ length: count }, () => EMPTY_PREVIEW)
  // 尺寸还没读出来的那些用空占位补上，位置不能错 —— 缩略图与文件靠下标对齐
  return Array.from({ length: count }, (_, index) => allPreviews[offset + index] ?? EMPTY_PREVIEW)
}
