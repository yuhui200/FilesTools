import { useCallback, useId, useRef, useState } from 'react'

import { useCoarsePointer } from '@/hooks/useCoarsePointer'
import { checkFiles, type UploadRules } from '@/utils/validation'

import { IconUploadCloud } from './Icons'

interface DropzoneProps {
  /** 校验通过的文件 */
  onSelect: (files: File[]) => void
  /** 校验失败时的中文提示 */
  onError: (message: string) => void
  /**
   * 没通过校验的文件。给了这个回调，就不再弹那条概括性的红字提示 ——
   * 调用方会把「哪个文件、为什么」逐条列出来（统一转换中心的
   * 「N 个文件不支持转换」，§二十一）。其它页面不传，行为与原来一致。
   */
  onReject?: (files: File[]) => void
  rules: UploadRules
  multiple?: boolean
  disabled?: boolean
  /** 校验提示里的名词，例如「图片」「文件」 */
  kind?: string
  /** 底部展示的支持格式文案 */
  hint?: string
  /** 紧凑模式：用于已经有文件时「继续添加」，占位更小 */
  compact?: boolean
}

export function Dropzone({
  onSelect,
  onError,
  onReject,
  rules,
  multiple = false,
  disabled = false,
  kind = '图片',
  hint,
  compact = false,
}: DropzoneProps) {
  const inputId = useId()
  const inputRef = useRef<HTMLInputElement>(null)
  // 用计数器而不是布尔值，避免拖过子元素时反复触发 leave
  const dragDepth = useRef(0)
  const [dragging, setDragging] = useState(false)
  // 手指操作的设备上「拖放」做不到，文案改成「选择文件」（§9 / §10）
  const touch = useCoarsePointer()

  const accept = rules.allowedExtensions.join(',')

  const handleFiles = useCallback(
    (fileList: FileList | null) => {
      if (!fileList || fileList.length === 0) return

      const incoming = Array.from(fileList)
      const files = multiple ? incoming : incoming.slice(0, 1)
      const { accepted, error } = checkFiles(files, rules, kind)
      // checkFiles 保留的是同一批 File 引用，所以这里能按身份差出来
      const rejected = files.filter((file) => !accepted.includes(file))

      if (rejected.length > 0 && onReject) onReject(rejected)
      else if (error) onError(error)

      if (accepted.length > 0) onSelect(accepted)
    },
    [kind, multiple, onError, onReject, onSelect, rules],
  )

  const openPicker = useCallback(() => {
    if (disabled) return
    inputRef.current?.click()
  }, [disabled])

  const handleDrop = useCallback(
    (event: React.DragEvent<HTMLDivElement>) => {
      event.preventDefault()
      dragDepth.current = 0
      setDragging(false)
      if (disabled) return
      handleFiles(event.dataTransfer.files)
    },
    [disabled, handleFiles],
  )

  const handleDragEnter = useCallback(
    (event: React.DragEvent<HTMLDivElement>) => {
      event.preventDefault()
      if (disabled) return
      dragDepth.current += 1
      setDragging(true)
    },
    [disabled],
  )

  const handleDragLeave = useCallback((event: React.DragEvent<HTMLDivElement>) => {
    event.preventDefault()
    dragDepth.current = Math.max(0, dragDepth.current - 1)
    if (dragDepth.current === 0) setDragging(false)
  }, [])

  const handleKeyDown = useCallback(
    (event: React.KeyboardEvent<HTMLDivElement>) => {
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault()
        openPicker()
      }
    },
    [openPicker],
  )

  return (
    <div>
      <div
        role="button"
        tabIndex={disabled ? -1 : 0}
        aria-label={touch ? '选择文件' : '选择或拖放文件'}
        aria-disabled={disabled || undefined}
        onClick={openPicker}
        onKeyDown={handleKeyDown}
        onDrop={handleDrop}
        onDragOver={(event) => event.preventDefault()}
        onDragEnter={handleDragEnter}
        onDragLeave={handleDragLeave}
        className={[
          'flex cursor-pointer flex-col items-center justify-center rounded-2xl border-2 border-dashed',
          compact ? 'px-5 py-6 text-center' : 'px-6 py-12 text-center sm:py-16',
          'transition',
          disabled
            ? 'cursor-not-allowed border-slate-200 bg-slate-50 opacity-70'
            : dragging
              ? 'border-brand-500 bg-brand-50 ring-4 ring-brand-500/10'
              : 'border-slate-300 bg-white hover:border-brand-400 hover:bg-brand-50/40',
        ].join(' ')}
      >
        <span
          className={[
            'grid place-items-center rounded-2xl transition',
            compact ? 'h-10 w-10' : 'h-14 w-14',
            dragging ? 'bg-brand-100 text-brand-600' : 'bg-slate-100 text-slate-400',
          ].join(' ')}
        >
          <IconUploadCloud className={compact ? 'h-5 w-5' : 'h-7 w-7'} />
        </span>

        <p className={compact ? 'mt-3 text-sm font-medium text-slate-900' : 'mt-5 text-base font-medium text-slate-900'}>
          {dragging ? '松开鼠标即可上传' : touch ? '选择文件' : '拖放文件到这里'}
        </p>
        <p className={compact ? 'mt-0.5 text-xs text-slate-500' : 'mt-1 text-sm text-slate-500'}>
          {touch
            ? multiple
              ? '点击打开系统文件选择器，可以一次选多个'
              : '点击打开系统文件选择器'
            : '或者点击选择文件'}
        </p>

        {hint && (
          <p className={compact ? 'mt-3 text-xs text-slate-400' : 'mt-5 text-xs text-slate-400'}>
            {hint}
          </p>
        )}
      </div>

      <input
        ref={inputRef}
        id={inputId}
        type="file"
        accept={accept}
        multiple={multiple}
        disabled={disabled}
        className="hidden"
        onChange={(event) => {
          handleFiles(event.target.files)
          // 重置以便重复选择同一个文件时仍能触发 change
          event.target.value = ''
        }}
      />
    </div>
  )
}
