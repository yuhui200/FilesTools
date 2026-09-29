import { Alert } from '@/components/Alert'
import { Button } from '@/components/Button'
import { FileList } from '@/components/FileList'
import { IconArrowRight, IconDownload, IconTrash } from '@/components/Icons'
import type { ConversionGroupState, SizeOptions } from '@/hooks/useConversionGroups'
import type { FilePreview } from '@/hooks/useFilePreviews'
import type { ConversionCapabilities } from '@/types'
import type { OptionValue } from '@/utils/conversionOptions'
import {
  capabilityFor,
  conversionStatusLabel,
  conversionTargetLabel,
} from '@/utils/conversion'
import { explainError } from '@/utils/errorMessages'

import { ConversionOptions } from './ConversionOptions'
import { ConversionTargetSelector } from './ConversionTargetSelector'
import { ConversionTaskList } from './ConversionTaskList'

interface ConversionGroupCardProps {
  group: ConversionGroupState
  capabilities: ConversionCapabilities
  /** 这一组文件的缩略图，顺序与 group.files 一致 */
  previews: FilePreview[]
  maxFiles: number
  /** 单个文件大小上限，供目标大小校验用 */
  maxBytes: number
  takenItems: ReadonlySet<string>
  downloading: boolean
  onChangeTarget: (groupId: string, target: string) => void
  onChangeOption: (groupId: string, key: string, value: OptionValue) => void
  onChangeSize: (groupId: string, changes: Partial<SizeOptions>) => void
  onRemoveFile: (groupId: string, index: number) => void
  onStart: (groupId: string) => void
  onCancel: (groupId: string) => void
  onRetry: (groupId: string, taskId: string) => void
  onDownloadItem: (taskId: string, url: string, filename: string) => void
  onDownloadGroup: (groupId: string) => void
  onRemoveGroup: (groupId: string) => void
}

/**
 * 一组文件：同一类源格式 → 一个目标格式。
 *
 * 没开始的组显示文件清单与参数面板；开始之后换成进度、每个文件的状态与结果。
 * **已经开始过的组不再允许改目标格式或增删文件** —— 那批任务的目标在提交时
 * 就定死了，让人以为还能改才是真的误导。
 */
export function ConversionGroupCard({
  group,
  capabilities,
  previews,
  maxFiles,
  maxBytes,
  takenItems,
  downloading,
  onChangeTarget,
  onChangeOption,
  onChangeSize,
  onRemoveFile,
  onStart,
  onCancel,
  onRetry,
  onDownloadItem,
  onDownloadGroup,
  onRemoveGroup,
}: ConversionGroupCardProps) {
  const batch = group.batch
  const used = batch !== null
  const busy = group.submitting

  const result = batch?.result ?? null
  const settled = batch?.status === 'completed' || batch?.status === 'failed' || batch?.status === 'cancelled'
  const cancellable = used && batch !== null && batch.status === 'processing'

  const downloadUrl = result?.download_url ?? null
  // 这一格能力的如实说明（如「GIF 只转第一帧」）。参数面板里已经有了，
  // 结果卡上再给一次 —— 用户是看着结果确认「我拿到的是什么」的，
  // 那正是这句话最该出现的地方。数据来自服务端，不是界面文案。
  const capabilityNote = capabilityFor(capabilities, group.sourceType, group.target)?.note ?? null

  const error = group.error ? explainError(group.errorCode, group.error) : null
  const batchExplanation = batch?.error
    ? explainError(batch.error.code, batch.error.message)
    : null

  return (
    <section className="card overflow-hidden">
      {/* 头部：源 → 目标 */}
      <header className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2 border-b border-slate-200 bg-slate-50/80 px-5 py-4 sm:px-6">
        <div className="flex min-w-0 flex-wrap items-center gap-x-2.5 gap-y-1">
          <span className="text-sm font-medium text-slate-900">{group.sourceLabel}</span>
          <IconArrowRight className="h-4 w-4 shrink-0 text-slate-400" />
          <span className="text-sm font-medium text-brand-700">
            {conversionTargetLabel(capabilities, group.target)}
          </span>
          <span className="text-xs text-slate-500">
            {group.files.length} 个文件
            {used && ` · ${conversionStatusLabel(batch.status)}`}
          </span>
        </div>

        <div className="flex shrink-0 items-center gap-2">
          {cancellable && (
            <Button
              size="sm"
              variant="ghost"
              loading={group.cancelPending}
              onClick={() => onCancel(group.id)}
            >
              取消
            </Button>
          )}
          {settled && (
            <Button
              size="sm"
              variant="ghost"
              icon={<IconTrash className="h-4 w-4" />}
              onClick={() => onRemoveGroup(group.id)}
            >
              移除
            </Button>
          )}
        </div>
      </header>

      <div className="px-5 py-4 sm:px-6">
        {/* 提交阶段的错误（网络 / 校验），与「某个文件转换失败」不是一回事 */}
        {error && (
          <div className="mb-4">
            <Alert tone="error">
              <p className="font-medium">{error.title}</p>
              {error.hint && <p className="mt-0.5 text-xs">{error.hint}</p>}
            </Alert>
          </div>
        )}

        {/* 还没开始：文件清单 + 目标格式 + 参数 */}
        {!used && (
          <>
            <FileList
              files={group.files}
              previews={previews}
              onRemove={(index) => onRemoveFile(group.id, index)}
              disabled={busy}
              maxFiles={maxFiles}
            />

            <div className="mt-6">
              <p className="text-sm font-medium text-slate-900">转换为</p>
              <div className="mt-3">
                <ConversionTargetSelector
                  groupId={group.id}
                  entries={group.entries}
                  value={group.target}
                  capabilities={capabilities}
                  disabled={busy}
                  onChange={(target) => onChangeTarget(group.id, target)}
                />
              </div>
            </div>

            {/* PDF 转 Word 的文字识别提示：直接沿用第六阶段那套文案 */}
            {group.target === 'docx' && capabilities.pdf_to_word_note && (
              <div className="mt-4">
                <Alert tone="info">{capabilities.pdf_to_word_note}</Alert>
              </div>
            )}

            <ConversionOptions
              groupId={group.id}
              sourceType={group.sourceType}
              targetType={group.target}
              capabilities={capabilities}
              values={group.options.values}
              size={group.options}
              maxBytes={maxBytes}
              fileCount={group.files.length}
              disabled={busy}
              onValueChange={(key, value) => onChangeOption(group.id, key, value)}
              onSizeChange={(changes) => onChangeSize(group.id, changes)}
            />

            <div className="mt-6 flex flex-wrap items-center gap-3">
              <Button
                loading={busy}
                disabled={group.files.length === 0}
                onClick={() => onStart(group.id)}
              >
                {busy
                  ? `正在上传 ${Math.round(group.uploadPercent)}%`
                  : `开始转换这 ${group.files.length} 个文件`}
              </Button>
            </div>
          </>
        )}

        {/* 已经提交：整批进度 + 逐文件状态 */}
        {used && batch && (
          <>
            <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
              <p className="text-sm text-slate-600">
                {batch.cancelling
                  ? '已请求取消，正在等待当前文件处理完…'
                  : batch.status === 'processing'
                    ? '正在转换…'
                    : batch.status === 'queued'
                      ? '排队中…'
                      : conversionStatusLabel(batch.status)}
              </p>
              <p className="text-sm tabular-nums text-slate-500">
                <span className="text-xl font-semibold text-slate-900">
                  {batch.completed + batch.failed + batch.cancelled}
                </span>
                <span className="mx-1">/</span>
                {batch.total}
              </p>
            </div>

            {/* 已定下来的文件数 / 总数：这是真实进度，不是估的 */}
            <div
              className="mt-3 h-2.5 w-full overflow-hidden rounded-full bg-slate-200"
              role="progressbar"
              aria-valuemin={0}
              aria-valuemax={100}
              aria-valuenow={Math.round(batch.progress)}
            >
              <div
                className={[
                  'h-full rounded-full transition-[width] duration-300 ease-out',
                  batch.failed > 0 && settled ? 'bg-amber-500' : 'bg-brand-500',
                ].join(' ')}
                style={{ width: `${Math.min(Math.max(batch.progress, 0), 100)}%` }}
              />
            </div>

            {batchExplanation && (
              <div className="mt-3">
                <Alert tone="error">
                  <p className="font-medium">{batchExplanation.title}</p>
                  {batchExplanation.hint && (
                    <p className="mt-0.5 text-xs">{batchExplanation.hint}</p>
                  )}
                </Alert>
              </div>
            )}

            <ConversionTaskList
              batch={batch}
              takenItems={takenItems}
              downloading={downloading}
              retrying={group.retrying}
              onRetry={(taskId) => onRetry(group.id, taskId)}
              onDownload={onDownloadItem}
            />

            {/* 整批下载：1 个成功项就是那个文件，≥2 个才是 ZIP */}
            {settled && result && (result.completed > 0 || result.archived) && (
              <div className="mt-5 flex flex-wrap items-center gap-3 border-t border-slate-100 pt-4">
                {downloadUrl ? (
                  <Button
                    loading={downloading}
                    icon={<IconDownload className="h-4 w-4" />}
                    onClick={() => onDownloadGroup(group.id)}
                  >
                    {result.archived ? `打包下载 ${result.completed} 个结果` : '下载结果'}
                  </Button>
                ) : (
                  <span className="text-sm text-slate-500">结果已取走或已过期，请重新转换。</span>
                )}
                <span className="text-xs text-slate-500">
                  已完成 {result.completed} 个
                  {result.failed > 0 && ` · 失败 ${result.failed} 个`}
                  {result.cancelled > 0 && ` · 已取消 ${result.cancelled} 个`}
                </span>
              </div>
            )}

            {settled && capabilityNote && (
              <p className="mt-3 text-xs text-slate-500">{capabilityNote}</p>
            )}

            {settled && result && result.notes.length > 0 && (
              <ul className="mt-3 space-y-1">
                {result.notes.map((note) => (
                  <li key={note} className="text-xs text-slate-500">
                    {note}
                  </li>
                ))}
              </ul>
            )}
          </>
        )}

        {/* 上传阶段：服务端还没有任务记录 */}
        {busy && (
          <div className="mt-4">
            <div className="h-2.5 w-full overflow-hidden rounded-full bg-slate-200">
              <div
                className="h-full rounded-full bg-brand-500 transition-[width] duration-300 ease-out"
                style={{ width: `${Math.min(Math.max(group.uploadPercent, 0), 100)}%` }}
              />
            </div>
            <p className="mt-2 text-xs text-slate-500">
              正在上传 {group.files.length} 个文件… {Math.round(group.uploadPercent)}%
            </p>
          </div>
        )}
      </div>
    </section>
  )
}
