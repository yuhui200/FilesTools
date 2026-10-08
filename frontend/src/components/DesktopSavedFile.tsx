import { useState } from 'react'

import { isDesktop, openResultFile, revealResultFile } from '@/services/desktop'

import { Alert } from './Alert'
import { Button } from './Button'
import { IconArrowRight, IconCheckCircle } from './Icons'

interface DesktopSavedFileProps {
  /**
   * 结果文件的落盘绝对路径。
   *
   * `null` 表示这次下载不是桌面端落盘的（Web 上永远是这样），或者还没下载过。
   */
  path: string | null
}

/**
 * 桌面端专属：结果已经落到本机「下载」目录之后，把位置和后续动作摆出来。
 *
 * 为什么 Web 上没有这一段：Web 的下载交给浏览器的下载栏，那里本来就有
 * 「打开 / 在文件夹中显示」，我们再做一遍是重复。而桌面端（Tauri）里
 * 浏览器那套是失效的，落盘由我们自己完成（见 `services/desktop.ts`），
 * 所以后续两个动作也得我们自己给。
 *
 * `isDesktop` 是构建期常量，Web 构建里这里是死代码，整块会被摇掉 ——
 * 所以这个组件在 Web 上**恒不渲染**，不会改变 Web 的任何行为。
 */
export function DesktopSavedFile({ path }: DesktopSavedFileProps) {
  const [busy, setBusy] = useState<'open' | 'reveal' | null>(null)
  const [error, setError] = useState<string | null>(null)

  if (!isDesktop || !path) return null

  const run = async (kind: 'open' | 'reveal') => {
    setBusy(kind)
    setError(null)
    try {
      if (kind === 'open') {
        await openResultFile(path)
      } else {
        await revealResultFile(path)
      }
    } catch (caught) {
      setError(caught instanceof Error && caught.message ? caught.message : '操作失败，请重试')
    } finally {
      setBusy(null)
    }
  }

  return (
    <div className="mt-3 rounded-lg border border-emerald-200 bg-emerald-50/60 p-3">
      <p className="flex items-start gap-2 text-xs text-slate-700">
        <IconCheckCircle className="mt-px h-4 w-4 shrink-0 text-emerald-600" />
        <span>
          已保存到 <span className="break-all font-mono text-slate-900">{path}</span>
        </span>
      </p>

      <div className="mt-2 flex flex-wrap gap-2 pl-6">
        <Button
          size="sm"
          variant="secondary"
          icon={<IconArrowRight className="h-3.5 w-3.5" />}
          loading={busy === 'open'}
          disabled={busy !== null}
          onClick={() => void run('open')}
        >
          打开
        </Button>
        <Button
          size="sm"
          variant="ghost"
          loading={busy === 'reveal'}
          disabled={busy !== null}
          onClick={() => void run('reveal')}
        >
          在文件夹中显示
        </Button>
      </div>

      {error && (
        <div className="mt-2 pl-6">
          <Alert tone="error">{error}</Alert>
        </div>
      )}
    </div>
  )
}
