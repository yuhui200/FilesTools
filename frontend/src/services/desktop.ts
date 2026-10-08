/**
 * 桌面端（Windows / Tauri 2）专属能力：把结果文件落盘、打开、在文件夹中定位。
 *
 * ## 为什么需要这个模块
 *
 * 浏览器里「下载」是靠 `<a download>` + `URL.createObjectURL` 做的，见
 * `api.ts` 的 `downloadResult`。但在 Tauri 的 WebView2 里，自定义协议下这套是
 * **静默 no-op** —— 不报错、不下载、什么都不发生。所以桌面端必须绕开浏览器，
 * 把字节直接交给 Rust 写盘（见 `desktop/src-tauri/src/result_files.rs`）。
 *
 * ## 零 npm 依赖
 *
 * 这里用的是 `tauri.conf.json` 里 `withGlobalTauri: true` 注入的全局
 * `window.__TAURI__`，**不装 `@tauri-apps/api`**。全局没有类型，所以下面
 * 只声明用得到的那一点点。
 *
 * ## 这个模块在 Web 端必须是死代码
 *
 * 顶层**不做任何事**（没有常量、没有副作用），所有函数体内部才去读全局。
 * 这样 Rollup 在 Web 构建里能把整个模块摇掉 —— Web 产物不受桌面端影响，
 * 这一条有断言盯着（`scripts/verify_desktop.py`）。
 */

/** 当前产物是不是桌面端。构建期常量：Web 是 `production`，桌面是 `desktop`。 */
export const isDesktop = import.meta.env.MODE === 'desktop'

/** `window.__TAURI__.core.invoke` 的最小签名 */
type Invoke = (
  command: string,
  payload?: unknown,
  options?: { headers?: Record<string, string> },
) => Promise<unknown>

/**
 * 取出注入的 `invoke`。
 *
 * 读不到就说明这个产物跑在了非 Tauri 环境里（比如把 dist-desktop/ 直接丢进
 * 浏览器打开）。**明确报错**，不要静默退化成「什么都没发生」——
 * 那正是 WebView2 里浏览器下载的表现，最难查。
 */
function tauriInvoke(): Invoke {
  const api = (globalThis as { __TAURI__?: { core?: { invoke?: Invoke } } }).__TAURI__
  const invoke = api?.core?.invoke
  if (typeof invoke !== 'function') {
    throw new Error('桌面端接口不可用，请重新安装 FileTools')
  }
  return invoke
}

/** 把 Tauri 拒绝出来的东西（规范说是任意值，实践上是 `Err` 里的字符串）变成 Error */
function asError(caught: unknown, fallback: string): Error {
  if (caught instanceof Error) return caught
  if (typeof caught === 'string' && caught.trim() !== '') return new Error(caught)
  return new Error(fallback)
}

/**
 * 把结果字节写进系统的「下载」目录，返回落盘的绝对路径。
 *
 * 走 Tauri 的**原始二进制 IPC**：载荷是 `Uint8Array`，Tauri 会当作
 * `application/octet-stream` 直接送过去，Rust 侧收到 `InvokeBody::Raw`。
 * 不走 JSON —— 那要 base64，大文件白胖三分之一。
 *
 * 文件名搭在请求头上，值必须 `encodeURIComponent` 编码过：fetch 的 `Headers`
 * 做的是 ByteString 转换，中文这种码位大于 0xFF 的字符**会当场抛 TypeError**。
 * 中文文件名是最常见的情况，不编码等于这条路径根本不成立。
 */
export async function saveResultToDownloads(bytes: Uint8Array, filename: string): Promise<string> {
  const invoke = tauriInvoke()
  try {
    const saved = await invoke('save_result', bytes, {
      headers: { 'x-filetools-filename': encodeURIComponent(filename) },
    })
    if (typeof saved !== 'string' || saved === '') {
      throw new Error('保存失败：桌面端没有返回落盘路径')
    }
    return saved
  } catch (caught) {
    throw asError(caught, '保存失败，请重试')
  }
}

/** 用系统默认程序打开一个已落盘的结果文件 */
export async function openResultFile(path: string): Promise<void> {
  const invoke = tauriInvoke()
  try {
    await invoke('open_result', { path })
  } catch (caught) {
    throw asError(caught, '打开失败，请重试')
  }
}

/** 在资源管理器里打开所在文件夹并选中这个文件 */
export async function revealResultFile(path: string): Promise<void> {
  const invoke = tauriInvoke()
  try {
    await invoke('reveal_result', { path })
  } catch (caught) {
    throw asError(caught, '打开文件夹失败，请重试')
  }
}
