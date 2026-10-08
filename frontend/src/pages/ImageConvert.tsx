import { useCallback, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'

import { Alert } from '@/components/Alert'
import { BatchResultPanel } from '@/components/BatchResultPanel'
import { Button } from '@/components/Button'
import { Dropzone } from '@/components/Dropzone'
import { FileList } from '@/components/FileList'
import { IconArrowRight, IconShield } from '@/components/Icons'
import { QualityField } from '@/components/QualityField'
import { RadioGroup, type RadioOption } from '@/components/RadioGroup'
import { TargetSizeField } from '@/components/TargetSizeField'
import { TaskProgressPanel } from '@/components/TaskProgressPanel'
import { useBatchTask } from '@/hooks/useBatchTask'
import { useFilePreviews } from '@/hooks/useFilePreviews'
import { useServerConfig } from '@/hooks/useServerConfig'
import { convertImages } from '@/services/api'
import type { OutputFormat } from '@/types'
import { fileExtension, formatBytes } from '@/utils/format'

const FORMAT_OPTIONS: RadioOption<OutputFormat>[] = [
  { value: 'jpg', label: 'JPG', hint: '体积最小，适合照片' },
  { value: 'png', label: 'PNG', hint: '无损，支持透明' },
  { value: 'webp', label: 'WEBP', hint: '体积更小，现代浏览器支持好' },
]

/** 内部格式名 -> 扩展名，用于判断文件当前是什么格式 */
const EXTENSION_OF: Record<OutputFormat, string> = {
  jpg: '.jpg',
  png: '.png',
  webp: '.webp',
}

const LABEL_OF: Record<OutputFormat, string> = {
  jpg: 'JPG',
  png: 'PNG',
  webp: 'WEBP',
}

export function ImageConvert() {
  const { config, offlineMessage } = useServerConfig()
  const [targetFormat, setTargetFormat] = useState<OutputFormat>('png')

  const rules = useMemo(
    () => ({
      allowedExtensions: config.allowed_image_extensions,
      maxBytes: config.max_upload_bytes,
    }),
    [config.allowed_image_extensions, config.max_upload_bytes],
  )

  const submit = useCallback(
    (
      files: File[],
      params: {
        qualityValue: number
        targetBytes: number | null
        onUploadProgress: (ratio: number) => void
        signal: AbortSignal
      },
    ) =>
      convertImages({
        files,
        targetFormat,
        // PNG 是无损格式，界面上不提供质量设置，也就不发送质量参数，
        // 后端会保持无损编码而不是做有损量化
        qualityValue: targetFormat === 'png' ? null : params.qualityValue,
        targetBytes: params.targetBytes,
        onUploadProgress: params.onUploadProgress,
        signal: params.signal,
      }),
    [targetFormat],
  )

  const task = useBatchTask({
    maxFiles: config.max_batch_files,
    maxBytes: config.max_upload_bytes,
    maxTotalBytes: config.max_batch_total_bytes,
    rules,
    defaultQuality: config.default_quality_value,
    submit,
  })

  const previews = useFilePreviews(task.files)
  const busy = task.status === 'uploading' || task.status === 'processing'
  const showProgress = task.files.length > 0 && (busy || task.status === 'failed')

  /**
   * 已经是目标格式的文件。
   *
   * 同格式转换没有意义，这里直接提示并在全部同格式时禁用按钮，
   * 避免用户白等一次处理。
   */
  const alreadyTarget = useMemo(() => {
    const extension = EXTENSION_OF[targetFormat]
    const aliases = targetFormat === 'jpg' ? ['.jpg', '.jpeg'] : [extension]
    return task.files.filter((file) => aliases.includes(fileExtension(file.name)))
  }, [task.files, targetFormat])

  const allSameFormat = task.files.length > 0 && alreadyTarget.length === task.files.length
  const someSameFormat = alreadyTarget.length > 0 && !allSameFormat

  const handleSelect = useCallback(
    (files: File[]) => {
      task.addFiles(files)
    },
    [task],
  )

  const canStart = task.files.length > 0 && !busy && !allSameFormat && !task.targetError

  return (
    <div className="mx-auto max-w-3xl px-4 py-10 sm:px-6 sm:py-14">
      <nav className="flex items-center gap-2 text-sm text-slate-500" aria-label="面包屑">
        <Link to="/" className="transition hover:text-slate-900">
          首页
        </Link>
        <span aria-hidden="true">/</span>
        <span className="text-slate-900">图片格式转换</span>
      </nav>

      <h1 className="mt-4 text-2xl font-bold tracking-tight text-slate-900 sm:text-3xl">
        图片格式转换
      </h1>
      <p className="mt-3 text-slate-600">
        在 JPG、PNG、WEBP 之间自由转换，一次最多 {config.max_batch_files} 张，
        多于一张时自动打包成 ZIP 下载。
      </p>

      {offlineMessage && (
        <div className="mt-6">
          <Alert tone="warning">{offlineMessage}</Alert>
        </div>
      )}

      <div className="mt-8 space-y-4">
        {task.error && (
          <Alert tone="error" onDismiss={task.dismissError}>
            {task.error}
          </Alert>
        )}

        {/* 1. 上传 */}
        {task.files.length === 0 && task.status !== 'done' && (
          <Dropzone
            multiple
            onSelect={handleSelect}
            onError={task.reportError}
            rules={rules}
            hint={`支持 ${config.allowed_image_extensions
              .map((ext) => ext.replace('.', '').toUpperCase())
              .join('、')}，单个最大 ${formatBytes(config.max_upload_bytes)}，最多 ${
              config.max_batch_files
            } 个`}
          />
        )}

        {/* 2. 处理中 / 整批失败（每个文件的状态与原因都在面板里） */}
        {showProgress && (
          <TaskProgressPanel
            title="格式转换"
            uploadPercent={task.status === 'uploading' ? task.uploadPercent : null}
            snapshot={task.snapshot}
            pendingFiles={task.files}
            error={
              task.status === 'failed'
                ? { code: task.errorCode ?? 'PROCESSING_FAILED', message: task.error ?? '' }
                : null
            }
            action={
              task.status === 'failed' ? (
                <Button size="sm" variant="secondary" onClick={task.reset}>
                  返回修改设置
                </Button>
              ) : null
            }
          />
        )}

        {/* 3. 结果 */}
        {task.status === 'done' && task.result && (
          <BatchResultPanel
            result={task.result}
            previews={previews}
            expired={task.resultExpired}
            onDownload={task.download}
            onReset={task.reset}
            downloading={task.downloading}
            downloadError={task.downloadError}
            savedPath={task.savedPath}
          />
        )}

        {/* 4. 设置面板 */}
        {task.files.length > 0 && task.status === 'idle' && (
          <div className="card animate-fade-in-up p-5 sm:p-6">
            <FileList
              files={task.files}
              previews={previews}
              onRemove={task.removeFile}
              disabled={busy}
              maxFiles={config.max_batch_files}
            />

            {task.files.length < config.max_batch_files && (
              <div className="mt-4">
                <Dropzone
                  compact
                  multiple
                  onSelect={handleSelect}
                  onError={task.reportError}
                  rules={rules}
                  hint="可以继续添加，也可以直接把文件拖到这里"
                />
              </div>
            )}

            {/* 目标格式 */}
            <fieldset className="mt-7">
              <legend className="text-sm font-medium text-slate-900">目标格式</legend>
              <p className="mt-1 text-xs text-slate-500">选择要转换成的图片格式。</p>
              <div className="mt-3">
                <RadioGroup
                  name="target-format"
                  value={targetFormat}
                  options={FORMAT_OPTIONS}
                  onChange={setTargetFormat}
                  disabled={busy}
                />
              </div>

              {allSameFormat && (
                <div className="mt-3">
                  <Alert tone="warning">
                    当前图片已经是 {LABEL_OF[targetFormat]} 格式，无需转换。
                    请选择其他目标格式，或直接下载原图。
                  </Alert>
                </div>
              )}

              {someSameFormat && (
                <div className="mt-3">
                  <Alert tone="info">
                    有 {alreadyTarget.length} 个文件已经是 {LABEL_OF[targetFormat]} 格式，
                    会重新编码一次；其余 {task.files.length - alreadyTarget.length} 个正常转换。
                  </Alert>
                </div>
              )}
            </fieldset>

            {/* 质量：PNG 是无损格式，不做有损压缩 */}
            <fieldset className="mt-7">
              <legend className="text-sm font-medium text-slate-900">图片质量</legend>
              <div className="mt-3">
                {targetFormat === 'png' ? (
                  <Alert tone="info">
                    PNG 是无损格式，不做有损压缩，因此不需要设置 JPG 那样的质量。
                    如果希望明显减小体积，建议转换成 JPG 或 WEBP。
                  </Alert>
                ) : (
                  <QualityField
                    value={task.qualityChoice}
                    customValue={task.qualityValue}
                    defaultValue={config.default_quality_value}
                    onChange={task.setQualityChoice}
                    onCustomChange={task.setQualityValue}
                    disabled={busy}
                  />
                )}
              </div>
            </fieldset>

            {/* 目标大小 */}
            <fieldset className="mt-7">
              <legend className="text-sm font-medium text-slate-900">目标最大文件大小</legend>
              <p className="mt-1 text-xs text-slate-500">
                选择后会自动优化压缩质量，使结果不超过该大小。
              </p>
              <div className="mt-3">
                <TargetSizeField
                  value={task.targetOption}
                  customValue={task.customTargetValue}
                  customUnit={task.customTargetUnit}
                  maxBytes={config.max_upload_bytes}
                  error={task.targetError}
                  onChange={task.setTargetOption}
                  onCustomChange={task.setCustomTarget}
                  disabled={busy}
                />
              </div>
            </fieldset>

            <div className="mt-8 flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
              <p className="order-2 text-xs text-slate-500 sm:order-1">
                {task.files.length} 个文件 · 合计{' '}
                {formatBytes(task.files.reduce((sum, file) => sum + file.size, 0))}
              </p>
              <Button
                size="lg"
                onClick={task.start}
                disabled={!canStart}
                icon={<IconArrowRight className="h-4 w-4" />}
                className="order-1 sm:order-2"
              >
                开始转换
              </Button>
            </div>
          </div>
        )}
      </div>

      <div className="mt-8 flex items-start gap-3 rounded-2xl border border-slate-200 bg-white p-4 text-sm text-slate-600">
        <IconShield className="mt-0.5 h-4 w-4 shrink-0 text-emerald-600" />
        <p>
          上传的文件会以随机文件名存放在服务器临时目录中，处理完成后自动删除。
          下载链接仅可使用一次，服务器不会长期保存你的文件。
        </p>
      </div>
    </div>
  )
}
