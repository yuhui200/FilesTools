import { useCallback, useMemo, useState } from 'react'

import { Alert } from '@/components/Alert'
import { Button } from '@/components/Button'
import { Dropzone } from '@/components/Dropzone'
import { IconArrowRight } from '@/components/Icons'
import { PdfFileCard } from '@/components/PdfFileCard'
import { PdfProgressPanel } from '@/components/PdfProgressPanel'
import { PdfResultPanel, type ResultStat } from '@/components/PdfResultPanel'
import { RadioGroup, type RadioOption } from '@/components/RadioGroup'
import { ToolPage } from '@/components/ToolPage'
import type { PdfSubmit } from '@/hooks/usePdfMultiTask'
import { usePdfMultiTask } from '@/hooks/usePdfMultiTask'
import { usePdfProgress, type PdfProgressLabels } from '@/hooks/usePdfProgress'
import { useServerConfig } from '@/hooks/useServerConfig'
import { docToPdf, type TxtLayoutParams } from '@/services/api'
import type { DocKind, DocTargetOption, PublicConfig, TxtOrientation } from '@/types'
import { explainError } from '@/utils/errorMessages'
import { formatBytes, formatPercent } from '@/utils/format'
import { STALE_BACKEND_MESSAGE, featureAvailability } from '@/utils/serverFeature'

interface DocToPdfProps {
  kind: DocKind
}

interface DocMeta {
  title: string
  description: string
  /** 校验提示里的名词 */
  kindLabel: string
  /** 允许上传的扩展名，从服务端配置里取，避免两边写死不一致 */
  extensions: (config: PublicConfig) => string[]
}

/**
 * 每个格式族的页面文案。
 *
 * 键与 ``DocKind`` 完全对应，加一种格式时这里漏写就编译不过。
 */
const DOC_META: Record<DocKind, DocMeta> = {
  word: {
    title: 'Word 转 PDF',
    description:
      '把 Word 文档转成 PDF。转换由服务器上的 LibreOffice 完成，排版与 Word 基本一致但不完全相同；原始文档在转换完成后立即从服务器删除。',
    kindLabel: 'Word 文档',
    extensions: (config) => config.allowed_word_extensions,
  },
  excel: {
    title: 'Excel 转 PDF',
    description:
      '把 Excel 表格转成 PDF。工作簿里的全部工作表会依次排进同一份 PDF，隐藏的工作表不会导出；原始表格在转换完成后立即从服务器删除。',
    kindLabel: 'Excel 表格',
    extensions: (config) => config.allowed_excel_extensions,
  },
  ppt: {
    title: 'PPT 转 PDF',
    description:
      '把 PowerPoint 演示文稿转成 PDF，一页幻灯片对应 PDF 的一页。隐藏的幻灯片不会导出；原始文件在转换完成后立即从服务器删除。',
    kindLabel: 'PowerPoint 演示文稿',
    extensions: (config) => config.allowed_powerpoint_extensions,
  },
  txt: {
    title: 'TXT 转 PDF',
    description:
      '把纯文本按你选的字体、字号、页面大小和方向排版成 PDF。排版由本服务直接完成，不经过 Office 转换组件，因此这一项在没装组件的服务器上同样可用。',
    kindLabel: '文本文件',
    extensions: (config) => config.allowed_text_extensions,
  },
}

/** 文档转换的状态条文案：用户看到的是「转换」，不是笼统的「处理」 */
const DOC_PROGRESS_LABELS: PdfProgressLabels = {
  steps: [
    { key: 'upload', label: '上传文件' },
    { key: 'detect', label: '识别格式' },
    { key: 'convert', label: '转换文件' },
    { key: 'result', label: '生成 PDF' },
  ],
  workingHints: ['正在识别文档…', '正在转换…', '正在生成 PDF…'],
}

/** 档位的中文名；服务端通过 doc_target_presets 决定有哪些档位 */
const TARGET_LABELS: Record<DocTargetOption, string> = {
  none: '不限制',
  '500kb': '≤ 500 KB',
  '1mb': '≤ 1 MB',
  '2mb': '≤ 2 MB',
  '5mb': '≤ 5 MB',
  '10mb': '≤ 10 MB',
  custom: '自定义',
}

/** 与服务端一致的取值范围（MB），见 routers/pdf_params.py */
const MIN_TARGET_MB = 0.1
const MAX_TARGET_MB = 500

/**
 * TXT 排版的两组选项名。
 *
 * 与 backend/office/txt_to_pdf.py 的 PAGE_SIZE_LABELS / ORIENTATION_LABELS 对应。
 * 页面大小只给标准纸张：图片转 PDF 的「自动」是「跟随图片尺寸」，
 * 纯文本没有这个概念。
 */
const PAGE_SIZE_LABELS: Record<string, string> = {
  a4: 'A4',
  a5: 'A5',
  letter: 'Letter',
}

const ORIENTATION_LABELS: Record<TxtOrientation, string> = {
  portrait: '纵向',
  landscape: '横向',
}

export function DocToPdf({ kind }: DocToPdfProps) {
  const { config, offlineMessage } = useServerConfig()
  const meta = DOC_META[kind]
  const isTxt = kind === 'txt'

  const [target, setTarget] = useState<DocTargetOption>('none')
  const [customMb, setCustomMb] = useState('1')

  /**
   * TXT 的四个排版选项。
   *
   * 初值都是 null，真正的取值在下面按「用户选过就用选的，没选过就用服务端给的
   * 第一个/默认值」推出来 —— 这样不用等 /api/config 回来再 setState，
   * 也不会先渲染一帧假值。服务端解析表单时用的正是同一套默认值
   * （见 routers/pdf_params.py::parse_txt_options），两边不会打架。
   */
  const [txtFont, setTxtFont] = useState<string | null>(null)
  const [txtFontSize, setTxtFontSize] = useState<string | null>(null)
  const [txtPageSize, setTxtPageSize] = useState<string | null>(null)
  const [txtOrientation, setTxtOrientation] = useState<TxtOrientation | null>(null)

  const rules = useMemo(
    () => ({
      allowedExtensions: meta.extensions(config),
      maxBytes: config.max_upload_bytes,
    }),
    [config, meta],
  )

  /** 只渲染能给出中文名的档位，服务端加了新档位而前端还不认识时不会显示成空白 */
  const targetOptions = useMemo<RadioOption<DocTargetOption>[]>(
    () =>
      config.doc_target_presets
        .filter((value): value is DocTargetOption => value in TARGET_LABELS)
        .map((value) => ({ value, label: TARGET_LABELS[value] })),
    [config.doc_target_presets],
  )

  /** 字体也是运行时探测出来的：服务器上没装的字体不该出现在选项里 */
  const fontOptions = useMemo<RadioOption<string>[]>(
    () => config.txt_fonts.map((item) => ({ value: item.value, label: item.label })),
    [config.txt_fonts],
  )

  const pageSizeOptions = useMemo<RadioOption<string>[]>(
    () =>
      config.txt_page_sizes.map((value) => ({
        value,
        label: PAGE_SIZE_LABELS[value] ?? value.toUpperCase(),
      })),
    [config.txt_page_sizes],
  )

  /** 方向是闭合的两种，认不出来的值不渲染 —— 没有中文名可给 */
  const orientationOptions = useMemo<RadioOption<TxtOrientation>[]>(
    () =>
      config.txt_orientations
        .filter((value): value is TxtOrientation => value in ORIENTATION_LABELS)
        .map((value) => ({ value, label: ORIENTATION_LABELS[value] })),
    [config.txt_orientations],
  )

  const font = txtFont ?? fontOptions[0]?.value ?? ''
  const fontSizeText = txtFontSize ?? String(config.txt_font_size.default)
  const pageSize = txtPageSize ?? pageSizeOptions[0]?.value ?? ''
  const orientation = txtOrientation ?? orientationOptions[0]?.value ?? null

  /** 行内字号错误提示；没填、填了非整数、超出服务端范围都拦在点按钮之前 */
  const fontSizeError = useMemo(() => {
    const { min, max } = config.txt_font_size
    const raw = fontSizeText.trim()
    if (raw === '') return '请填写字体大小。'
    const value = Number(raw)
    if (!Number.isFinite(value) || !Number.isInteger(value)) {
      return '字体大小必须是整数。'
    }
    if (value < min || value > max) return `字体大小需要在 ${min}–${max} 之间。`
    return null
  }, [config.txt_font_size, fontSizeText])

  /**
   * TXT 要发出去的四个参数。任一项没准备好就是 null，提交按钮据此禁用 ——
   * 半个参数发出去只会得到服务端的默认值，而界面显示的却是用户选的那个。
   */
  const txtParams = useMemo<TxtLayoutParams | null>(() => {
    if (!isTxt || fontSizeError) return null
    if (!font || !pageSize || !orientation) return null
    return { font, fontSize: Number(fontSizeText), pageSize, orientation }
  }, [font, fontSizeError, fontSizeText, isTxt, orientation, pageSize])

  const submit: PdfSubmit = useCallback(
    (files, options) => {
      const [file] = files
      if (!file) throw new Error('没有可转换的文件，请重新选择')
      if (isTxt) {
        if (!txtParams) throw new Error('排版选项还没填好，请检查后再试')
        return docToPdf({ kind, file, target: null, targetMb: null, txt: txtParams, ...options })
      }
      return docToPdf({
        kind,
        file,
        target,
        targetMb: target === 'custom' ? customMb : null,
        ...options,
      })
    },
    [customMb, isTxt, kind, target, txtParams],
  )

  const task = usePdfMultiTask({
    maxFiles: 1,
    maxTotalBytes: config.max_upload_bytes,
    rules,
    kind: meta.kindLabel,
    label: meta.title,
    submit,
  })

  const progress = usePdfProgress(task.stage, task.uploadPercent, '正在上传文档…', DOC_PROGRESS_LABELS)

  /**
   * 缺组件只挡 Office 三种格式。
   *
   * TXT 是本服务自己用 PyMuPDF 排版的，根本不经过 LibreOffice ——
   * 服务器没装组件时界面若把 TXT 也一起禁用，就是关掉了一个其实能用的功能。
   * 后端 receive_office 同样对 TXT 豁免这个检查，两边保持一致。
   */
  const officeAvailability = featureAvailability(config.doc_conversion_available)
  const unavailable = officeAvailability === 'unavailable' && !isTxt
  /** 后端比前端旧，连文档转换接口都没有 —— 与「没装组件」不是一回事 */
  const staleBackend = officeAvailability === 'unknown' && !isTxt
  /** 两种情况都不能开工，但要说清楚是哪一种 */
  const blocked = unavailable || staleBackend
  const file = task.files[0] ?? null
  const result = task.result

  const targetError = useMemo(() => {
    if (isTxt) return null
    if (target !== 'custom') return null
    if (customMb.trim() === '') return '选择自定义目标大小时，请填写目标大小（MB）。'
    const value = Number(customMb)
    if (!Number.isFinite(value)) return '目标大小必须是数字（单位 MB）。'
    if (value < MIN_TARGET_MB || value > MAX_TARGET_MB) {
      return `目标大小需要在 ${MIN_TARGET_MB}–${MAX_TARGET_MB} MB 之间。`
    }
    return null
  }, [target, customMb])

  /**
   * 体积变化。转换后的 PDF 常常比原文档大（Office 文件本身就是压缩过的 XML），
   * 所以这里如实显示带符号的百分比，而不是只显示「节省」。
   */
  const sizeChange = useMemo(() => {
    if (!result || !result.original_size || result.original_size <= 0) return null
    return (result.size / result.original_size - 1) * 100
  }, [result])

  const stats: ResultStat[] = result
    ? [
        { label: '原文件', value: formatBytes(result.original_size ?? 0) },
        { label: 'PDF 大小', value: formatBytes(result.size), tone: 'brand' },
        {
          label: '页数',
          value: result.page_count === null ? '—' : `${result.page_count} 页`,
        },
        {
          label: '体积变化',
          value:
            sizeChange === null
              ? '—'
              : `${sizeChange <= 0 ? '-' : '+'}${formatPercent(Math.abs(sizeChange))}`,
          tone: sizeChange !== null && sizeChange <= 0 ? 'emerald' : 'muted',
        },
      ]
    : []

  const explanation = task.error ? explainError(task.errorCode, task.error) : null
  const canStart =
    !blocked && !targetError && !task.busy && file !== null && (!isTxt || txtParams !== null)

  return (
    <ToolPage
      category="doc"
      title={meta.title}
      description={meta.description}
      offlineMessage={offlineMessage}
    >
      {/* 缺组件要在上传之前就说清楚：让用户白传一份 50 MB 的文件是不负责任的 */}
      {unavailable && (
        <Alert tone="warning">
          当前服务器缺少 Office 转换组件，请联系管理员。
        </Alert>
      )}

      {/* 后端比前端旧：接口不存在，不是组件没装。指错方向比不说更糟 */}
      {staleBackend && <Alert tone="warning">{STALE_BACKEND_MESSAGE}</Alert>}

      {explanation && (
        <Alert tone="error" onDismiss={task.dismissError}>
          <p className="font-medium">{explanation.title}</p>
          {explanation.hint && <p className="mt-0.5 text-xs">{explanation.hint}</p>}
        </Alert>
      )}

      {!file && (
        <Dropzone
          onSelect={(files) => task.addFiles(files)}
          onError={task.reportError}
          rules={rules}
          kind={meta.kindLabel}
          disabled={blocked}
          hint={`支持 ${rules.allowedExtensions.join(' / ')}，单个最大 ${formatBytes(
            config.max_upload_bytes,
          )}`}
        />
      )}

      <PdfProgressPanel
        stage={task.stage}
        progress={progress}
        hasFiles={file !== null}
        labels={DOC_PROGRESS_LABELS}
      />

      {file && !task.busy && !result && (
        <div className="card animate-fade-in-up p-5 sm:p-6">
          <PdfFileCard
            filename={file.name}
            size={file.size}
            onRemove={task.clearFiles}
            disabled={task.busy}
          />

          {isTxt ? (
            <>
              <fieldset className="mt-7">
                <legend className="text-sm font-medium text-slate-900">字体</legend>
                <p className="mt-1 text-xs text-slate-500">
                  列表是服务器上实际装了的字体。没有装中文字体的服务器只会给出「内置字体」一项。
                </p>
                <div className="mt-3">
                  <RadioGroup
                    name="txt-font"
                    value={font}
                    options={fontOptions}
                    onChange={setTxtFont}
                    disabled={task.busy}
                  />
                </div>
              </fieldset>

              <fieldset className="mt-7">
                <legend className="text-sm font-medium text-slate-900">字体大小</legend>
                <div className="mt-3 flex flex-wrap items-center gap-3">
                  <input
                    type="number"
                    inputMode="numeric"
                    min={config.txt_font_size.min}
                    max={config.txt_font_size.max}
                    step="1"
                    value={fontSizeText}
                    disabled={task.busy}
                    onChange={(event) => setTxtFontSize(event.target.value)}
                    aria-label="字体大小"
                    className="field w-24"
                  />
                  <span className="text-xs text-slate-500">
                    可填 {config.txt_font_size.min}–{config.txt_font_size.max}，正文常用 10–12
                  </span>
                </div>
                {fontSizeError && (
                  <div className="mt-3">
                    <Alert tone="error">{fontSizeError}</Alert>
                  </div>
                )}
              </fieldset>

              <fieldset className="mt-7">
                <legend className="text-sm font-medium text-slate-900">页面大小</legend>
                <div className="mt-3">
                  <RadioGroup
                    name="txt-page-size"
                    value={pageSize}
                    options={pageSizeOptions}
                    onChange={setTxtPageSize}
                    disabled={task.busy}
                  />
                </div>
              </fieldset>

              <fieldset className="mt-7">
                <legend className="text-sm font-medium text-slate-900">页面方向</legend>
                <div className="mt-3">
                  <RadioGroup
                    name="txt-orientation"
                    value={orientation ?? 'portrait'}
                    options={orientationOptions}
                    onChange={setTxtOrientation}
                    disabled={task.busy}
                    columns={2}
                  />
                </div>
              </fieldset>
            </>
          ) : (
            <fieldset className="mt-7">
              <legend className="text-sm font-medium text-slate-900">最大文件大小</legend>
              <p className="mt-1 text-xs text-slate-500">
                转换完成后会尽量把 PDF 压到这个大小以内。转换组件无法直接指定输出体积，
                所以这是尽力而为 —— 达不到时结果里会如实写明，不会假装成功。
              </p>
              <div className="mt-3">
                <RadioGroup
                  name="doc-target-size"
                  value={target}
                  options={targetOptions}
                  onChange={setTarget}
                  disabled={task.busy}
                />
              </div>

              {target === 'custom' && (
                <div className="mt-3 flex flex-wrap items-center gap-3">
                  <input
                    type="number"
                    inputMode="decimal"
                    min={MIN_TARGET_MB}
                    max={MAX_TARGET_MB}
                    step="0.1"
                    value={customMb}
                    disabled={task.busy}
                    onChange={(event) => setCustomMb(event.target.value)}
                    aria-label="自定义目标大小"
                    className="field w-28"
                  />
                  <span className="text-xs text-slate-500">
                    MB，可填 {MIN_TARGET_MB}–{MAX_TARGET_MB}
                  </span>
                </div>
              )}

              {targetError && (
                <div className="mt-3">
                  <Alert tone="error">{targetError}</Alert>
                </div>
              )}
            </fieldset>
          )}

          <div className="mt-4">
            <Alert tone="info">
              {file.name} · {formatBytes(file.size)}，将转换成 PDF
              {isTxt
                ? '，按上面选的排版生成。'
                : target === 'none'
                  ? '，不限制大小。'
                  : '，并尽量压到目标大小以内。'}
              {/* TXT 不经过 LibreOffice，说成「要启动转换组件」是假话 */}
              {isTxt
                ? 'TXT 由本服务直接排版，不需要启动转换组件，通常一两秒就完成。'
                : '文档转换需要在本机启动一次转换组件，通常十秒左右，文档越大越久。'}
            </Alert>
          </div>

          <div className="mt-8 flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
            <p className="order-2 text-xs text-slate-500 sm:order-1">
              转换完成后原始文档会立即从服务器删除
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

      {result && (
        <PdfResultPanel
          result={result}
          stats={stats}
          doneTitle="✓ 转换完成"
          onDownload={task.download}
          onReset={task.reset}
          resetLabel={isTxt ? '换个排版再转' : '换个大小再转'}
          onRestart={task.clearFiles}
          restartLabel="换一份文件"
          downloading={task.downloading}
          downloadError={task.downloadError}
          expired={task.resultExpired}
        />
      )}
    </ToolPage>
  )
}
