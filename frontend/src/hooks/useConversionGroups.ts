/**
 * 统一转换中心的状态编排（第七阶段 §二十一）。
 *
 * 一句话：**一组一个批次，各跑各的**。
 *
 * 用户丢进来一堆混合文件，页面按源格式自动分组（图片一组、Word 一组…），
 * 每组自己选目标格式、自己开始、自己轮询、自己出结果。某一组失败不影响
 * 其它组 —— 这正是规格 §二十一 要的「批量任务之间互相独立」。
 *
 * 为什么不用 ``useBatchTask``：那个 hook 一次只管一个批次，而且把
 * ``BatchResponse`` 写死在类型里。这里的每一组都是一个独立批次，
 * 词表也是第七阶段那一套（status / completed / cancelling）。
 * 两者共用的只有最底下的轮询原语（``pollConversionBatch``）。
 */

import { useCallback, useEffect, useRef, useState } from 'react'

import {
  ApiError,
  cancelConversionBatch,
  createConversionTasks,
  downloadResult,
  getConversionBatch,
  newProgressId,
  retryConversionTask,
} from '@/services/api'
import { isAbort, messageOf, pollConversionBatch } from '@/services/taskRunner'
import type { ConversionBatchStatus, ConversionCapabilities, ConversionCapability } from '@/types'
import {
  capabilityFor,
  defaultTargetOf,
  groupFiles,
  type UnsupportedFile,
} from '@/utils/conversion'
import {
  defaultOptionValues,
  serializeOptions,
  type OptionValue,
  type OptionValues,
} from '@/utils/conversionOptions'
import { fileExtension, toBytes } from '@/utils/format'
import { recordHistory } from '@/utils/history'
import { checkFile, checkFiles, type UploadRules } from '@/utils/validation'

import { SIZE_BYTES, type SizeOption } from './useBatchTask'

/** 「最近处理」里显示的操作名 */
const TOOL_LABEL = '统一转换中心'

/** 目标最大文件大小。**不在任何一条能力的 ``options_schema`` 里** ——
 *  「结果不超过多大」是跨格式的后处理，所以它仍然走第七阶段的扁平字段。 */
export interface SizeOptions {
  targetSize: SizeOption
  customSize: string
  sizeUnit: 'KB' | 'MB'
}

/**
 * 一组的转换参数。
 *
 * ``values`` 是 ``options_schema`` 的键 → 表单值，**原样**保存用户敲进去的
 * 字符串 —— 渲染与提交读的都是它，中间没有第二份「界面状态」。
 * 换目标格式时只补上新 schema 缺的默认值，旧键留在袋子里不动：
 * 序列化只发当前 schema 认识的键，所以残留值不会跑到请求里去。
 */
export interface GroupOptions extends SizeOptions {
  values: OptionValues
}

const DEFAULT_SIZE_OPTIONS: SizeOptions = {
  targetSize: 'none',
  customSize: '',
  sizeUnit: 'MB',
}

export interface ConversionGroupState {
  id: string
  sourceType: string
  sourceLabel: string
  /** 这一种源格式的全部能力，来自 capabilities，推荐目标在前 */
  entries: ConversionCapability[]
  target: string
  files: File[]
  options: GroupOptions

  // ---- 提交之后的现场 ----
  /** 批次快照；没开始过就是 null */
  batch: ConversionBatchStatus | null
  statusUrl: string | null
  cancelUrl: string | null
  /** 正在上传（服务端还没受理） */
  submitting: boolean
  uploadPercent: number
  /** 已经点过取消、还没拿到回应 */
  cancelPending: boolean
  /** 正在重试的那一项 */
  retrying: string | null
  /** 提交阶段的错误（网络 / 校验），与「某个文件转换失败」不是一回事 */
  error: string | null
  errorCode: string | null
}

export interface ConversionGroups {
  groups: ConversionGroupState[]
  /** 认得出格式、但服务器现在转不了的文件 */
  unsupported: UnsupportedFile[]
  /** 已经收下的全部文件数 */
  fileCount: number
  busy: boolean
  downloading: boolean
  downloadError: string | null
  error: string | null
  errorCode: string | null
  /** 已经下载过的单项（一次性令牌，取过就没了） */
  takenItems: ReadonlySet<string>
  /**
   * 桌面端落盘后的绝对路径，键是任务号。
   *
   * 这里是一张表而不是一个值：一次可能连着下好几个单项，每一项落在哪
   * 都得留一份。Web 上**恒为空对象**（下载交给浏览器，见
   * ``components/DesktopSavedFile.tsx``）。
   */
  savedItemPaths: Readonly<Record<string, string>>
  /** 桌面端落盘后的绝对路径，键是组的 id；Web 上恒为空对象 */
  savedGroupPaths: Readonly<Record<string, string>>

  addFiles: (incoming: File[]) => void
  removeFile: (groupId: string, index: number) => void
  setTarget: (groupId: string, target: string) => void
  /** 改一个选项。键就是 schema 的键，值就是表单值 */
  setOptionValue: (groupId: string, key: string, value: OptionValue) => void
  setSizeOptions: (groupId: string, changes: Partial<SizeOptions>) => void
  startGroup: (groupId: string) => void
  startAll: () => void
  cancelGroup: (groupId: string) => void
  retryTask: (groupId: string, taskId: string) => void
  downloadItem: (taskId: string, url: string, filename: string) => void
  downloadGroup: (groupId: string) => void
  removeGroup: (groupId: string) => void
  /** 清空「不支持」清单 */
  clearUnsupported: () => void
  /**
   * Dropzone 在校验阶段刷下来的文件（见 ``Dropzone.onReject``）。
   * 格式不支持的进「不支持」清单，只是太大或空文件的仍走红字提示。
   */
  rejectFiles: (files: File[]) => void
  /** 展示一条本地校验的错误提示（Dropzone 校验失败时用） */
  reportError: (message: string) => void
  dismissError: () => void
  reset: () => void
}

/** 这一组选中的那条能力。选不出来（同格式）时返回 null */
function selectedEntry(
  capabilities: ConversionCapabilities,
  group: ConversionGroupState,
): ConversionCapability | null {
  return capabilityFor(capabilities, group.sourceType, group.target)
}

/**
 * 一组初始参数：默认目标那条能力的 schema 的默认值。
 *
 * 默认目标可能是空串（这一种源一个目标都没有），那时 schema 为 null，
 * 得到的就是一个空袋子 —— 与「这条能力没有选项」是同一件事。
 */
function initialOptions(
  entries: ConversionCapability[],
  target: string,
): GroupOptions {
  const entry = entries.find((item) => item.target_type === target) ?? null
  return {
    values: defaultOptionValues(entry?.options_schema ?? null),
    ...DEFAULT_SIZE_OPTIONS,
  }
}

/**
 * 换目标格式时把新 schema 里缺的键补上，**已有的值原样留着**。
 *
 * 用户从 JPG 换到 PNG 再换回来，刚调好的质量应该还在；
 * 而 PNG 才有的东西在换回去时不发出去 —— 序列化只认当前 schema。
 */
function mergeDefaults(
  values: OptionValues,
  entry: ConversionCapability | null,
): OptionValues {
  const defaults = defaultOptionValues(entry?.options_schema ?? null)
  const merged: OptionValues = { ...defaults }
  for (const key of Object.keys(defaults)) {
    const current = values[key]
    if (current !== undefined) merged[key] = current
  }
  return merged
}

interface Options {
  rules: UploadRules
  maxFiles: number
  maxTotalBytes: number
  capabilities: ConversionCapabilities
}

let sequence = 0

function nextId(): string {
  sequence += 1
  return `g${sequence}`
}

export function useConversionGroups(options: Options): ConversionGroups {
  const { rules, maxFiles, maxTotalBytes, capabilities } = options

  const [groups, setGroups] = useState<ConversionGroupState[]>([])
  const [unsupported, setUnsupported] = useState<UnsupportedFile[]>([])
  const [error, setError] = useState<string | null>(null)
  const [errorCode, setErrorCode] = useState<string | null>(null)
  const [downloading, setDownloading] = useState(false)
  const [downloadError, setDownloadError] = useState<string | null>(null)
  const [takenItems, setTakenItems] = useState<ReadonlySet<string>>(new Set())
  // 桌面端落盘后的绝对路径。按「单项 / 整组」分开记，键分别是任务号与组 id；
  // Web 上两张表恒为空（见 components/DesktopSavedFile.tsx）
  const [savedItemPaths, setSavedItemPaths] = useState<Record<string, string>>({})
  const [savedGroupPaths, setSavedGroupPaths] = useState<Record<string, string>>({})

  /** 每一组一个 AbortController：一组取消不影响其它组 */
  const controllers = useRef(new Map<string, AbortController>())
  /** 正在提交 / 正在轮询的组，用来挡住连点两次「开始」 */
  const running = useRef(new Set<string>())

  useEffect(
    () => () => {
      for (const controller of controllers.current.values()) controller.abort()
      controllers.current.clear()
      running.current.clear()
    },
    [],
  )

  const patch = useCallback((id: string, changes: Partial<ConversionGroupState>) => {
    setGroups((current) =>
      current.map((group) => (group.id === id ? { ...group, ...changes } : group)),
    )
  }, [])

  /**
   * 轮询一个批次直到它定下来，并在结束时记一笔「最近处理」。
   *
   * 取消之后**必须继续轮询**：已经在跑的那一个还没停，界面要一直显示
   * 「正在取消」直到它真的结束（§十五）。
   */
  const follow = useCallback(
    async (groupId: string, statusUrl: string, files: File[] | null) => {
      const controller = new AbortController()
      controllers.current.set(groupId, controller)
      running.current.add(groupId)

      try {
        const final = await pollConversionBatch(statusUrl, {
          signal: controller.signal,
          onSnapshot: (snapshot) => patch(groupId, { batch: snapshot }),
        })
        patch(groupId, { batch: final })

        if (files) {
          // 文件大小取自本地文件：快照里只有**结果**的大小，
          // 而「最近处理」记的是输入文件的大小（与图片工具一致）
          recordHistory(
            final.tasks.map((task) => ({
              filename: task.source_filename,
              tool: TOOL_LABEL,
              ok: task.status === 'completed',
              size: files[task.index]?.size ?? 0,
            })),
          )
        }
      } catch (caught) {
        // 用户主动取消轮询（翻页、重新开始）不是错误，安静退出
        if (!isAbort(caught)) {
          patch(groupId, {
            error: messageOf(caught, '任务状态查询失败，请刷新页面重试'),
            errorCode: errorCodeOf(caught),
          })
        }
      } finally {
        if (controllers.current.get(groupId) === controller) controllers.current.delete(groupId)
        running.current.delete(groupId)
      }
    },
    [patch],
  )

  const start = useCallback(
    async (group: ConversionGroupState) => {
      if (running.current.has(group.id) || group.batch || group.files.length === 0) return
      running.current.add(group.id)

      const controller = new AbortController()
      controllers.current.set(group.id, controller)
      patch(group.id, {
        submitting: true,
        uploadPercent: 0,
        error: null,
        errorCode: null,
        cancelPending: false,
      })

      try {
        const entry = selectedEntry(capabilities, group)
        const created = await createConversionTasks({
          files: group.files,
          targetType: group.target,
          // 声明选中的是哪一条能力：服务端据此把参数白名单收窄成这一条的
          // schema，多出来的键会明确报错而不是被静默忽略
          capabilityId: entry?.id ?? '',
          // 渲染与提交读的是同一份 schema（§二十二）：面板上摆的就是这里发的
          options: serializeOptions(entry?.options_schema ?? null, group.options.values),
          targetBytes: resolveTargetBytes(group.options),
          // 每个文件一个进度 id：只有 PDF 转 Word 会用它上报真实页码，
          // 其余转换拿不到真实百分比，界面上就不显示百分比（§十二）
          progressIds: group.files.map(() => newProgressId()),
          onUploadProgress: (ratio) => patch(group.id, { uploadPercent: ratio * 100 }),
          signal: controller.signal,
        })

        patch(group.id, {
          submitting: false,
          uploadPercent: 100,
          statusUrl: created.status_url,
          cancelUrl: created.cancel_url,
        })
        // 不移出 running：从「上传完成」到「第一份快照到达」之间 group.batch
        // 还是 null，那个窗口里连点两次「开始」会提交两批。
        // follow 会接手这个 id，结束时再清掉。
        await follow(group.id, created.status_url, group.files)
      } catch (caught) {
        if (!isAbort(caught)) {
          patch(group.id, {
            submitting: false,
            error: messageOf(caught, '提交失败，请重试'),
            errorCode: errorCodeOf(caught),
          })
        } else {
          patch(group.id, { submitting: false })
        }
        running.current.delete(group.id)
        controllers.current.delete(group.id)
      }
    },
    [capabilities, follow, patch],
  )

  const startGroup = useCallback(
    (groupId: string) => {
      const group = groups.find((item) => item.id === groupId)
      if (group) void start(group)
    },
    [groups, start],
  )

  const startAll = useCallback(() => {
    for (const group of groups) {
      if (!group.batch && !group.submitting) void start(group)
    }
  }, [groups, start])

  const addFiles = useCallback(
    (incoming: File[]) => {
      const fileCount = groups.reduce((sum, group) => sum + group.files.length, 0)
      const room = maxFiles - fileCount
      if (room <= 0) {
        setErrorCode(null)
        setError(`一次最多处理 ${maxFiles} 个文件，请先移除一些再添加。`)
        return
      }

      // 扩展名就不在白名单里的，直接进「不支持」块并写明原因。
      // 用一句「暂不支持该文件格式」的红色提示打发掉，用户既看不到是哪个
      // 文件、也不知道为什么，等于把信息丢了（§二十一）。
      const unknown: UnsupportedFile[] = []
      const known: File[] = []
      for (const file of incoming) {
        const extension = fileExtension(file.name)
        if (extension === '' || !rules.allowedExtensions.includes(extension)) {
          unknown.push(unsupportedFileOf(file, extension))
          continue
        }
        known.push(file)
      }

      const { accepted, error: rejection } = checkFiles(known, rules, '文件')
      const taken = accepted.slice(0, room)
      const dropped = accepted.length - taken.length

      setErrorCode(null)
      if (rejection) {
        setError(rejection)
      } else if (dropped > 0) {
        setError(`一次最多处理 ${maxFiles} 个文件，多出的 ${dropped} 个已忽略。`)
      } else {
        setError(null)
      }

      const { groups: added, unsupported: rejected } = groupFiles(taken, capabilities)

      // 同源格式的文件合进同一个「还没开始」的组 —— 一组只能有一个目标格式，
      // 分开成两组反而要设置两遍。已经开始过的组不再收新文件：
      // 那批任务的目标格式早就定死了，塞进去只会让人以为它也转了。
      const merged = [...groups]
      for (const group of added) {
        const index = merged.findIndex(
          (item) => item.sourceType === group.sourceType && !item.batch && !item.submitting,
        )
        const existing = index >= 0 ? merged[index] : undefined
        if (existing) {
          merged[index] = { ...existing, files: [...existing.files, ...group.files] }
          continue
        }
        // 一组只能有一个目标格式，默认给第一个**推荐**目标（服务端的标签说了算）
        const target = defaultTargetOf(group.entries)
        merged.push({
          id: nextId(),
          sourceType: group.sourceType,
          sourceLabel: group.sourceLabel,
          entries: group.entries,
          target,
          files: group.files,
          options: initialOptions(group.entries, target),
          batch: null,
          statusUrl: null,
          cancelUrl: null,
          submitting: false,
          uploadPercent: 0,
          cancelPending: false,
          retrying: null,
          error: null,
          errorCode: null,
        })
      }

      setUnsupported((current) => [...current, ...unknown, ...rejected])
      setGroups(merged)

      const total = merged.reduce(
        (sum, group) => sum + group.files.reduce((inner, file) => inner + file.size, 0),
        0,
      )
      if (total > maxTotalBytes) {
        setError(
          `一次最多处理 ${Math.round(maxTotalBytes / (1024 * 1024))} MB 的文件，请分批上传。`,
        )
      }
    },
    [capabilities, groups, maxFiles, maxTotalBytes, rules],
  )

  const removeFile = useCallback(
    (groupId: string, index: number) => {
      setGroups((current) =>
        current
          .map((group) =>
            group.id === groupId && !group.batch && !group.submitting
              ? { ...group, files: group.files.filter((_, position) => position !== index) }
              : group,
          )
          // 文件被删光的组就没有存在的意义了
          .filter((group) => group.files.length > 0 || group.batch !== null),
      )
    },
    [],
  )

  const setTarget = useCallback(
    (groupId: string, target: string) => {
      setGroups((current) =>
        current.map((group) => {
          if (group.id !== groupId || group.batch || group.submitting) return group
          const entry = group.entries.find((item) => item.target_type === target) ?? null
          return {
            ...group,
            target,
            // 新目标缺的选项补上默认值，已有的值留着：换个格式再换回来，
            // 刚调好的参数不该被清空
            options: { ...group.options, values: mergeDefaults(group.options.values, entry) },
          }
        }),
      )
    },
    [],
  )

  const setOptionValue = useCallback((groupId: string, key: string, value: OptionValue) => {
    setGroups((current) =>
      current.map((group) =>
        group.id === groupId
          ? { ...group, options: { ...group.options, values: { ...group.options.values, [key]: value } } }
          : group,
      ),
    )
  }, [])

  const setSizeOptions = useCallback((groupId: string, changes: Partial<SizeOptions>) => {
    setGroups((current) =>
      current.map((group) =>
        group.id === groupId ? { ...group, options: { ...group.options, ...changes } } : group,
      ),
    )
  }, [])

  const cancelGroup = useCallback(
    (groupId: string) => {
      const group = groups.find((item) => item.id === groupId)
      if (!group?.cancelUrl || !group.batch) return
      if (group.batch.status === 'completed' || group.batch.status === 'cancelled') return

      patch(groupId, { cancelPending: true })
      // 取消是协作式的：服务端立刻停掉还在排队的，已经在跑的那个要等它自己结束。
      // 拿到的最新快照如实反映这件事，随后由轮询继续跟进到整批定下来。
      void cancelConversionBatch(group.cancelUrl)
        .then((snapshot) => patch(groupId, { batch: snapshot, cancelPending: false }))
        .catch((caught: unknown) =>
          patch(groupId, {
            cancelPending: false,
            error: messageOf(caught, '取消失败，请稍后重试'),
            errorCode: errorCodeOf(caught),
          }),
        )
    },
    [groups, patch],
  )

  const retryTask = useCallback(
    (groupId: string, taskId: string) => {
      const group = groups.find((item) => item.id === groupId)
      if (!group) return

      patch(groupId, { retrying: taskId, error: null, errorCode: null })
      // 重试会产出一份**新的**结果，上一轮那次落盘的路径已经指不到东西了
      setSavedItemPaths((current) => dropKeys(current, [taskId]))
      void retryConversionTask(taskId)
        .then((snapshot) => {
          patch(groupId, { batch: snapshot, retrying: null })
          // 重试会让整批重新动起来，接着轮询到它再次定下来。
          // 不记「最近处理」：重试还是同一批文件，记两次只会重复。
          if (group.statusUrl) void follow(groupId, group.statusUrl, null)
        })
        .catch((caught: unknown) =>
          patch(groupId, {
            retrying: null,
            error: messageOf(caught, '重试失败，请重新上传文件'),
            errorCode: errorCodeOf(caught),
          }),
        )
    },
    [follow, groups, patch],
  )

  const downloadItem = useCallback(
    (taskId: string, url: string, filename: string) => {
      setDownloading(true)
      setDownloadError(null)
      // 令牌是一次性的：取过就再也拿不到了，界面上要如实标出来。
      // 只有真的取走了（或者服务端说已经没有了）才标记 ——
      // 网络断了的情况下令牌还在，不该把按钮禁掉。
      const markTaken = () => setTakenItems((current) => new Set(current).add(taskId))

      void downloadResult(url, filename)
        .then((outcome) => {
          markTaken()
          if (outcome.kind === 'desktop') {
            setSavedItemPaths((current) => ({ ...current, [taskId]: outcome.path }))
          }
        })
        .catch((caught: unknown) => {
          if (caught instanceof ApiError && caught.status === 404) markTaken()
          setDownloadError(messageOf(caught, '下载失败，请重试'))
        })
        .finally(() => setDownloading(false))
    },
    [],
  )

  const downloadGroup = useCallback(
    (groupId: string) => {
      const group = groups.find((item) => item.id === groupId)
      const result = group?.batch?.result
      const statusUrl = group?.statusUrl
      if (!statusUrl || !result?.download_url) return

      const filename = result.archived
        ? (result.archive_filename ?? 'filetools-converted.zip')
        : (result.items[0]?.filename ?? 'result')

      setDownloading(true)
      setDownloadError(null)
      void downloadResult(result.download_url, filename)
        .then(async (outcome) => {
          if (outcome.kind === 'desktop') {
            setSavedGroupPaths((current) => ({ ...current, [groupId]: outcome.path }))
          }
          // 结果是一次性的：下载完再查一次，页面就能如实显示「已取走」
          try {
            patch(groupId, { batch: await getConversionBatch(statusUrl) })
          } catch {
            // 查询失败不影响这次下载已经成功的事实
          }
        })
        .catch((caught: unknown) => setDownloadError(messageOf(caught, '下载失败，请重试')))
        .finally(() => setDownloading(false))
    },
    [groups, patch],
  )

  const removeGroup = useCallback(
    (groupId: string) => {
      controllers.current.get(groupId)?.abort()
      controllers.current.delete(groupId)
      running.current.delete(groupId)
      // 这一组连同它那一串任务号都从界面上没了，落盘路径表里对应的条目
      // 也就永远没人再读了 —— 顺手清掉，别让表随着操作次数一直涨
      const removed = groups.find((group) => group.id === groupId)
      setGroups((current) => current.filter((group) => group.id !== groupId))
      setSavedGroupPaths((current) => dropKeys(current, [groupId]))
      setSavedItemPaths((current) =>
        dropKeys(current, (removed?.batch?.tasks ?? []).map((task) => task.task_id)),
      )
    },
    [groups],
  )

  const dismissError = useCallback(() => {
    setError(null)
    setErrorCode(null)
    setDownloadError(null)
  }, [])

  const clearUnsupported = useCallback(() => setUnsupported([]), [])

  /**
   * Dropzone 挡下来的文件。
   *
   * 为什么单独一个入口而不是直接复用 ``addFiles``：Dropzone 在一次选择里
   * 分两次回调（通过的一批、没通过的一批），而 ``addFiles`` 读的是当前的
   * ``groups`` 快照而不是函数式更新 —— 同一轮里调两次，后一次的 setGroups
   * 会把前一次刚加进去的文件整个覆盖掉。这里只动 ``unsupported`` 与错误
   * 提示，两个都是函数式更新，不会互相踩。
   *
   * 分类也在这里做，Dropzone 只管把没通过的原样交出来：不在白名单里的
   * 进「不支持转换」清单（§二十一）；格式本身支持、只是太大或空文件的，
   * 仍然走红字提示 —— 把它们塞进「不支持」块里名不副实。
   */
  const rejectFiles = useCallback(
    (files: File[]) => {
      const entries: UnsupportedFile[] = []
      let message: string | null = null

      for (const file of files) {
        const extension = fileExtension(file.name)
        if (extension === '' || !rules.allowedExtensions.includes(extension)) {
          entries.push(unsupportedFileOf(file, extension))
          continue
        }
        message ??= checkFile(file, rules, '文件')
      }

      if (entries.length > 0) {
        setUnsupported((current) => [...current, ...entries])
      }
      if (message) {
        setErrorCode(null)
        setError(message)
      }
    },
    [rules],
  )

  const reset = useCallback(() => {
    for (const controller of controllers.current.values()) controller.abort()
    controllers.current.clear()
    running.current.clear()
    setGroups([])
    setUnsupported([])
    setError(null)
    setErrorCode(null)
    setDownloadError(null)
    setTakenItems(new Set())
    setSavedItemPaths({})
    setSavedGroupPaths({})
  }, [])

  const fileCount = groups.reduce((sum, group) => sum + group.files.length, 0)
  const busy = groups.some((group) => group.submitting) || downloading

  return {
    groups,
    unsupported,
    fileCount,
    busy,
    downloading,
    downloadError,
    error,
    errorCode,
    takenItems,
    savedItemPaths,
    savedGroupPaths,
    addFiles,
    removeFile,
    setTarget,
    setOptionValue,
    setSizeOptions,
    startGroup,
    startAll,
    cancelGroup,
    retryTask,
    downloadItem,
    downloadGroup,
    removeGroup,
    clearUnsupported,
    rejectFiles,
    reportError: (message: string) => {
      setErrorCode(null)
      setError(message)
    },
    dismissError,
    reset,
  }
}

// ----------------------------------------------------------------------

/**
 * 从一张「键 → 值」表里删掉若干键。
 *
 * 一个键都没删掉时**原样返回入参**：这些表是 state，返回新对象会白白触发
 * 一次重渲，而这个函数在「移除一组」里是每点一次都要走的路径。
 */
function dropKeys(map: Record<string, string>, keys: Iterable<string>): Record<string, string> {
  let next: Record<string, string> | null = null
  for (const key of keys) {
    if (key in map) {
      next ??= { ...map }
      delete next[key]
    }
  }
  return next ?? map
}

function errorCodeOf(caught: unknown): string {
  return caught instanceof ApiError ? caught.code : 'PROCESSING_FAILED'
}

/**
 * 一个进不了转换的文件：原因要具体到能照着改（§二十一）。
 *
 * 「暂不支持 GIF 格式」比「暂不支持该文件格式」有用得多 —— 文件名就在旁边，
 * 用户要的是知道该换成什么。
 */
function unsupportedFileOf(file: File, extension: string): UnsupportedFile {
  return {
    file,
    reason: extension
      ? `暂不支持 ${extension.replace('.', '').toUpperCase()} 格式`
      : '这个文件没有扩展名，无法判断格式',
  }
}

/** 目标大小档位 -> 字节数。'none' 与填错的自定义值都表示「不限制」 */
function resolveTargetBytes(options: SizeOptions): number | null {
  if (options.targetSize === 'none') return null
  if (options.targetSize === 'custom') {
    const bytes = toBytes(Number(options.customSize), options.sizeUnit)
    return bytes !== null && bytes > 0 ? bytes : null
  }
  return SIZE_BYTES[options.targetSize]
}
