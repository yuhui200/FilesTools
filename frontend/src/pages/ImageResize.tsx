import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link } from 'react-router-dom'

import { Alert } from '@/components/Alert'
import { BatchResultPanel } from '@/components/BatchResultPanel'
import { Button } from '@/components/Button'
import { Dropzone } from '@/components/Dropzone'
import { FileList } from '@/components/FileList'
import { IconArrowRight, IconShield } from '@/components/Icons'
import { ResizeFields } from '@/components/ResizeFields'
import { TargetSizeField } from '@/components/TargetSizeField'
import { TaskProgressPanel } from '@/components/TaskProgressPanel'
import { useBatchTask } from '@/hooks/useBatchTask'
import { useFilePreviews } from '@/hooks/useFilePreviews'
import { useServerConfig } from '@/hooks/useServerConfig'
import { resizeImages } from '@/services/api'
import { formatBytes } from '@/utils/format'

export function ImageResize() {
  const { config, offlineMessage } = useServerConfig()
  const [width, setWidth] = useState('')
  const [height, setHeight] = useState('')
  const [keepAspect, setKeepAspect] = useState(true)
  // 用户是否动过宽高输入框。没动过之前不弹「请至少填写宽度或高度」的红色提示
  const [touched, setTouched] = useState(false)

  const rules = useMemo(
    () => ({
      allowedExtensions: config.allowed_image_extensions,
      maxBytes: config.max_upload_bytes,
    }),
    [config.allowed_image_extensions, config.max_upload_bytes],
  )

  // 保持比例时，宽高都为空则不发送 —— 由用户填写的那个边推导另一边
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
      resizeImages({
        files,
        width: toEdge(width),
        height: toEdge(height),
        keepAspect,
        // 尺寸调整页不提供质量设置：未指定时用服务端默认质量 80，
        // PNG 则保持无损编码；指定了目标大小时由后端自动优化质量
        qualityValue: null,
        targetBytes: params.targetBytes,
        onUploadProgress: params.onUploadProgress,
        signal: params.signal,
      }),
    [width, height, keepAspect],
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

  const first = previews[0]
  const originalWidth = first?.width ?? null
  const originalHeight = first?.height ?? null
  // 等比联动用的比例，来自第一张图片
  const aspect =
    originalWidth && originalHeight ? originalWidth / originalHeight : null

  /**
   * 只有单张图片时，才把原始宽高填进输入框。
   *
   * 这样用户一进来就有可用的起点（也不会还没操作就报「请至少填写宽度或高度」）。
   * 批量处理时两个框都留空，让用户只填一个边，各图按自身比例缩放。
   * 只在第一次拿到尺寸时填，之后不再覆盖用户的输入。
   */
  const seeded = useRef(false)
  useEffect(() => {
    if (seeded.current || task.files.length !== 1) return
    if (!originalWidth || !originalHeight) return
    seeded.current = true
    setWidth(String(originalWidth))
    setHeight(String(originalHeight))
  }, [task.files.length, originalWidth, originalHeight])

  // 文件清空后，下次上传重新按新图片填充
  useEffect(() => {
    if (task.files.length > 0) return
    seeded.current = false
    setTouched(false)
    setWidth('')
    setHeight('')
  }, [task.files.length])

  /**
   * 批量处理时不做等比联动。
   *
   * 多张图片的宽高比可能各不相同，按第一张的比例填上另一边，等于给所有图片
   * 套了一个尺寸框。批量时改成「只保留用户正在编辑的那一边」，
   * 后端就会让每张图片按各自的原始比例缩放。
   */
  const multiple = task.files.length > 1

  const handleWidthChange = useCallback(
    (value: string) => {
      setWidth(value)
      setTouched(true)
      if (!keepAspect) return
      if (multiple) {
        setHeight('')
        return
      }
      if (!aspect) return
      const next = Number(value)
      if (Number.isFinite(next) && next > 0) {
        setHeight(String(Math.max(1, Math.round(next / aspect))))
      }
    },
    [aspect, keepAspect, multiple],
  )

  const handleHeightChange = useCallback(
    (value: string) => {
      setHeight(value)
      setTouched(true)
      if (!keepAspect) return
      if (multiple) {
        setWidth('')
        return
      }
      if (!aspect) return
      const next = Number(value)
      if (Number.isFinite(next) && next > 0) {
        setWidth(String(Math.max(1, Math.round(next * aspect))))
      }
    },
    [aspect, keepAspect, multiple],
  )

  const handleKeepAspectChange = useCallback(
    (checked: boolean) => {
      setKeepAspect(checked)
      if (!checked || !aspect || multiple) return
      // 打开开关时以已填写的一边为准，重算另一边
      const currentWidth = Number(width)
      const currentHeight = Number(height)
      if (Number.isFinite(currentWidth) && currentWidth > 0) {
        setHeight(String(Math.max(1, Math.round(currentWidth / aspect))))
      } else if (Number.isFinite(currentHeight) && currentHeight > 0) {
        setWidth(String(Math.max(1, Math.round(currentHeight * aspect))))
      }
    },
    [aspect, multiple, width, height],
  )

  const handlePreset = useCallback((size: { width: number; height: number }) => {
    setWidth(String(size.width))
    setHeight(String(size.height))
    setTouched(true)
  }, [])

  const dimensionError = useMemo(() => {
    const parsedWidth = toEdge(width)
    const parsedHeight = toEdge(height)

    if (parsedWidth === null && parsedHeight === null) {
      // 刚上传、还没输入时不报错，只把按钮禁掉（ResizeFields 会给出中性提示）
      return touched ? '请至少填写宽度或高度' : null
    }
    if (parsedWidth !== null && parsedWidth > config.max_image_edge) {
      return `宽度不能超过 ${config.max_image_edge} px`
    }
    if (parsedHeight !== null && parsedHeight > config.max_image_edge) {
      return `高度不能超过 ${config.max_image_edge} px`
    }
    if (width.trim() !== '' && parsedWidth === null) return '宽度必须是大于 0 的整数'
    if (height.trim() !== '' && parsedHeight === null) return '高度必须是大于 0 的整数'
    return null
  }, [width, height, config.max_image_edge, touched])

  const handleSelect = useCallback(
    (files: File[]) => {
      task.addFiles(files)
    },
    [task],
  )

  // 至少填了一个合法的边才允许开始（后端要求至少一个边）
  const hasEdge = toEdge(width) !== null || toEdge(height) !== null

  const canStart =
    task.files.length > 0 && !busy && hasEdge && !dimensionError && !task.targetError

  return (
    <div className="mx-auto max-w-3xl px-4 py-10 sm:px-6 sm:py-14">
      <nav className="flex items-center gap-2 text-sm text-slate-500" aria-label="面包屑">
        <Link to="/" className="transition hover:text-slate-900">
          首页
        </Link>
        <span aria-hidden="true">/</span>
        <span className="text-slate-900">调整图片尺寸</span>
      </nav>

      <h1 className="mt-4 text-2xl font-bold tracking-tight text-slate-900 sm:text-3xl">
        调整图片尺寸
      </h1>
      <p className="mt-3 text-slate-600">
        按像素调整图片宽高，可锁定宽高比例、使用常用尺寸，并限制结果的文件大小。
        一次最多 {config.max_batch_files} 张，多于一张时自动打包成 ZIP 下载。
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

        {/* 2. 处理中 / 整批失败 */}
        {showProgress && (
          <TaskProgressPanel
            title="尺寸调整"
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

            {/* 尺寸设置 */}
            <div className="mt-7">
              <ResizeFields
                width={width}
                height={height}
                keepAspect={keepAspect}
                originalWidth={originalWidth}
                originalHeight={originalHeight}
                multiple={multiple}
                disabled={busy}
                error={dimensionError}
                onWidthChange={handleWidthChange}
                onHeightChange={handleHeightChange}
                onKeepAspectChange={handleKeepAspectChange}
                onPreset={handlePreset}
              />
            </div>

            {/* 目标大小 */}
            <fieldset className="mt-7">
              <legend className="text-sm font-medium text-slate-900">目标最大文件大小</legend>
              <p className="mt-1 text-xs text-slate-500">
                程序会先按上面的尺寸调整图片，再自动优化压缩质量，使结果不超过该大小。
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
                {task.files.length > 1 ? '批量调整' : '开始调整'}
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

/** 把输入框里的文本转成合法的像素值；空或非法都返回 null（表示不指定这一边） */
function toEdge(value: string): number | null {
  if (value.trim() === '') return null
  const parsed = Number(value)
  if (!Number.isInteger(parsed) || parsed <= 0) return null
  return parsed
}
