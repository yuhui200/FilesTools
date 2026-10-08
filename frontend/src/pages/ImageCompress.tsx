import { useCallback, useMemo } from 'react'
import { Link } from 'react-router-dom'

import { Alert } from '@/components/Alert'
import { BatchResultPanel } from '@/components/BatchResultPanel'
import { Button } from '@/components/Button'
import { Dropzone } from '@/components/Dropzone'
import { FileList } from '@/components/FileList'
import { IconArrowRight, IconShield } from '@/components/Icons'
import { QualityField } from '@/components/QualityField'
import { TargetSizeField } from '@/components/TargetSizeField'
import { TaskProgressPanel } from '@/components/TaskProgressPanel'
import { useBatchTask } from '@/hooks/useBatchTask'
import { useFilePreviews } from '@/hooks/useFilePreviews'
import { useServerConfig } from '@/hooks/useServerConfig'
import { compressImages } from '@/services/api'
import { formatBytes } from '@/utils/format'

export function ImageCompress() {
  const { config, offlineMessage } = useServerConfig()

  const rules = useMemo(
    () => ({
      allowedExtensions: config.allowed_image_extensions,
      maxBytes: config.max_upload_bytes,
    }),
    [config.allowed_image_extensions, config.max_upload_bytes],
  )

  /**
   * 压缩接口按**档位名**（high / balanced / strong）传参，
   * 不像格式转换那样传具体数值 —— 所以这里用 params.qualityChoice
   * 而不是 params.qualityValue，并且关闭「自定义质量」档。
   */
  const submit = useCallback(
    (
      files: File[],
      params: {
        qualityChoice: 'high' | 'balanced' | 'strong' | 'custom'
        targetBytes: number | null
        onUploadProgress: (ratio: number) => void
        signal: AbortSignal
      },
    ) =>
      compressImages({
        files,
        quality: params.qualityChoice === 'custom' ? 'balanced' : params.qualityChoice,
        targetBytes: params.targetBytes,
        onUploadProgress: params.onUploadProgress,
        signal: params.signal,
      }),
    [],
  )

  const task = useBatchTask({
    maxFiles: config.max_batch_files,
    maxBytes: config.max_upload_bytes,
    maxTotalBytes: config.max_batch_total_bytes,
    rules,
    defaultQuality: config.default_quality_value,
    allowCustomQuality: false,
    submit,
  })

  const previews = useFilePreviews(task.files)
  const busy = task.status === 'uploading' || task.status === 'processing'
  const showProgress = task.files.length > 0 && (busy || task.status === 'failed')

  const totalBytes = task.files.reduce((sum, file) => sum + file.size, 0)
  const canStart = task.files.length > 0 && !busy && !task.targetError

  const handleSelect = useCallback(
    (files: File[]) => {
      task.addFiles(files)
    },
    [task],
  )

  return (
    <div className="mx-auto max-w-3xl px-4 py-10 sm:px-6 sm:py-14">
      {/* 面包屑 */}
      <nav className="flex items-center gap-2 text-sm text-slate-500" aria-label="面包屑">
        <Link to="/" className="transition hover:text-slate-900">
          首页
        </Link>
        <span aria-hidden="true">/</span>
        <span className="text-slate-900">图片压缩</span>
      </nav>

      <h1 className="mt-4 text-2xl font-bold tracking-tight text-slate-900 sm:text-3xl">
        图片压缩
      </h1>
      <p className="mt-3 text-slate-600">
        一次最多 {config.max_batch_files} 张图片，选择目标大小后系统会自动调整画质与尺寸，
        把每张图片压到指定体积以内；多于一张时自动打包成 ZIP 下载。
      </p>

      {offlineMessage && (
        <div className="mt-6">
          <Alert tone="warning">{offlineMessage}</Alert>
        </div>
      )}

      <div className="mt-8 space-y-4">
        {task.error && task.status !== 'failed' && (
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

        {/* 2. 处理中 / 整批失败 */}
        {showProgress && (
          <TaskProgressPanel
            title="图片压缩"
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

            {/* 目标大小 */}
            <fieldset className="mt-7">
              <legend className="text-sm font-medium text-slate-900">目标大小</legend>
              <p className="mt-1 text-xs text-slate-500">
                选择后会自动寻找满足体积的最高画质，必要时等比缩小图片尺寸。
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

            {/* 压缩质量 */}
            <fieldset className="mt-7">
              <legend className="text-sm font-medium text-slate-900">压缩质量</legend>
              <div className="mt-3">
                <QualityField
                  value={task.qualityChoice}
                  customValue={task.qualityValue}
                  defaultValue={config.default_quality_value}
                  onChange={task.setQualityChoice}
                  onCustomChange={task.setQualityValue}
                  disabled={busy}
                  allowCustom={false}
                  hint="决定画质的取舍区间；指定了目标大小时，系统会在此基础上寻找最合适的质量。"
                />
              </div>
            </fieldset>

            <div className="mt-8 flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
              <p className="order-2 text-xs text-slate-500 sm:order-1">
                {task.files.length} 个文件 · 合计 {formatBytes(totalBytes)}
                {task.targetBytes === null
                  ? ' · 不限制目标大小'
                  : ` · 目标 ${formatBytes(task.targetBytes)} 以内`}
              </p>
              <Button
                size="lg"
                onClick={task.start}
                disabled={!canStart}
                icon={<IconArrowRight className="h-4 w-4" />}
                className="order-1 sm:order-2"
              >
                {task.files.length > 1 ? '批量压缩' : '开始压缩'}
              </Button>
            </div>
          </div>
        )}
      </div>

      {/* 安全说明 */}
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
