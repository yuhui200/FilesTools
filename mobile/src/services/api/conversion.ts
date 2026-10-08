/**
 * 提交转换任务（规格 §十五）。
 *
 * 走的是**既有**的 `POST /api/conversion/tasks`，没有、也不会有
 * `/mobile/convert` 之类的第二条接口 —— 后端是唯一的文件处理真相。
 *
 * 参数分两路：老的九个扁平字段（`quality_preset` / `width` / …）与
 * 第九阶段的 `options`（扁平点号键 JSON 串）。Mobile **只走 `options`**
 * 这一路：它是从 capability 的 `options_schema` 逐项渲染出来的，
 * 和 `capability_id` 一起提交后，服务端只按那一条能力的 schema 校验，
 * 多出来的键会明确报错。走扁平字段就等于在客户端另写一套参数映射，
 * 而那正是「加一种格式要改两处」的来源。
 */

import { File } from 'expo-file-system'

import type { ConversionBatchCreated, MobileFile } from '@/types'
import { ApiError, CLIENT_CODES, IS_DEV, postForm } from './client'

export interface SubmitConversionInput {
  /** 要转换的文件。顺序即结果里的 index 顺序 */
  files: MobileFile[]
  /** 目标格式，例如 png / pdf / docx */
  targetType: string
  /** 选中的能力 ID，例如 image.jpg-to-png */
  capabilityId: string
  /**
   * 参数，扁平点号键（`resize.mode` / `quality` …）。
   * 键名与 capability 的 `options_schema` 逐字相同 —— 渲染与提交同源。
   */
  options?: Record<string, unknown>
  /**
   * 与 files 顺序对齐的进度 id（可选）。
   * 只有 PDF → Word 会用它上报真实页码，其余转换传了也没人读。
   */
  progressIds?: string[]
}

/**
 * 原生上传时，一个文件在 multipart 里该长什么样。
 *
 * **这个类型是 `expo/fetch` 的约定，不是 React Native 的。**
 *
 * Expo SDK 57 起，全局 `fetch` 换成了 `expo/fetch`
 * （见 `expo/src/winter/runtime.native.ts`），它**自己**把 `FormData`
 * 序列化成 multipart。它的编码器只认三种 part：字符串、`Blob`、
 * 以及**带 `bytes()` 的对象**（`expo/src/winter/fetch/convertFormData.ts`）。
 * React Native 那个 `{uri, name, type}` 三件套**不在其中** —— 落到
 * 最后一支 `throw new Error('Unsupported FormDataPart implementation')`。
 *
 * 这个异常发生在**开 socket 之前**，于是被 `client.ts` 归一成
 * 「无法连接服务器」，而服务端一条日志都没有。看上去像网络断了，
 * 实际是序列化没走通 —— 排查时很容易查错方向。
 */
interface NativeFilePart {
  name: string
  type: string
  /** 编码器在拼 body 时调它。名字就叫 `bytes`，不能改 */
  bytes: () => Promise<Uint8Array>
}

/** 一个 part 读完了就一直用这份字节，编码器重复调也不会再读一次盘 */
function fromBytes(name: string, type: string, bytes: Uint8Array): NativeFilePart {
  return { name, type, bytes: () => Promise.resolve(bytes) }
}

/**
 * 把一个待上传文件变成原生上能用的 part。
 *
 * 必须借 `expo-file-system` 的 `File` 来读盘（它有 `bytes()`）。
 * 但**不能直接把 `File` 当 part 用**：编码器取文件名读的是 `part.name`，
 * 而 `File.name` 是「盘上那个文件的名字」—— 文档选择器落盘时用的是
 * 随机名（`DocumentPicker/<uuid>.<ext>`，见
 * `DocumentPickerModule.kt::copyDocumentToCacheDirectory`），直接用会把
 * 源文件名报成一串 uuid，转换结果也跟着错名。所以这里自己包一层，
 * 文件名**用用户选的那个**。
 *
 * 代价是文件要进内存一次。这是 `expo/fetch` 本身的代价，不是这里引入的：
 * 它先把整个 body `join` 成一个 `Uint8Array` 再发。绕不过去 —— RN 的
 * `Blob` 只支持「从别的 Blob 建」（`Libraries/Blob/Blob.js`），
 * 没法把字节包成一个带 `name` 的 Blob。
 */
async function nativeFilePart(file: MobileFile): Promise<NativeFilePart> {
  const type = file.mimeType ?? 'application/octet-stream'
  try {
    return fromBytes(file.name, type, await new File(file.uri).bytes())
  } catch (caught) {
    if (IS_DEV) {
      const detail = caught as { name?: string; message?: string }
      console.warn('[upload] 读盘失败：', file.uri, detail?.name, detail?.message)
    }
    // 读盘这一步在这里做完，是为了让失败**能被归到「文件」这一类**。
    // 留给编码器去读的话，原生异常会逃出 `fetch`，被 `client.ts` 归一成
    // 「无法连接服务器」—— 用户于是去查网络，而问题其实在这个文件。
    throw new ApiError(
      `读不到「${file.name}」，请重新选择这个文件`,
      CLIENT_CODES.file,
    )
  }
}

/**
 * 往表单里塞一个文件。
 *
 * 判断依据是**手上有没有浏览器那个真对象**，不是 `Platform.OS`：
 * Web 给 `asset.file`（真 `File`，浏览器的 `FormData` 只认它，塞普通对象
 * 会被字符串化成 `"[object Object]"`，服务端收到的「文件」就是两个方括号）；
 * 原生给上面那个 `bytes()` 对象。
 *
 * `lib.dom` 的 `FormData.append` 只声明了 `Blob`，原生这个形状是
 * `expo/fetch` 自己的约定，类型里没有，只能显式断言。
 */
async function appendFile(form: FormData, file: MobileFile): Promise<void> {
  const part = file.blob ?? (await nativeFilePart(file))
  form.append('files', part as unknown as Blob)
}

/**
 * 把待上传文件拼成 multipart 表单。
 *
 * 是 `async` 只因为原生读盘那一步（`File.bytes()`）是异步的 —— 表单本身
 * 仍然是同步拼好的。调用方拿到的永远是拼完整的表单。
 */
export async function buildForm(input: SubmitConversionInput): Promise<FormData> {
  const form = new FormData()

  for (const file of input.files) {
    await appendFile(form, file)
  }

  form.append('target_type', input.targetType)
  form.append('capability_id', input.capabilityId)

  if (input.options && Object.keys(input.options).length > 0) {
    form.append('options', JSON.stringify(input.options))
  }

  for (const id of input.progressIds ?? []) {
    form.append('progress_ids', id)
  }

  return form
}

/**
 * 提交一批文件，立刻拿到批次号（服务端返回 202）。
 *
 * 转换在后台队列里逐个进行，**这一步返回不代表转换成功** ——
 * 之后要靠 `tasks.ts` 轮询。
 */
export async function submitConversion(
  input: SubmitConversionInput,
  options: { signal?: AbortSignal } = {},
): Promise<ConversionBatchCreated> {
  return postForm<ConversionBatchCreated>(
    '/api/conversion/tasks',
    await buildForm(input),
    { signal: options.signal },
  )
}

