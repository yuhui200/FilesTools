/**
 * 文件名编码声明 —— 上传时告诉服务端「我这边的文件名是编码过的」。
 *
 * 这个文件**刻意零 import**：`scripts/verify_mobile_phase11a.py` 会用一份
 * 最小的 tsconfig 把纯逻辑模块编成 CJS 交给 node 跑，任何 `react-native`
 * 或 `expo-*` 的 import 都会把这条测试路断掉。判据本身也只依赖一个字符串，
 * 没有必要为了「像 React Native 代码」而引入 `Platform`。
 *
 * ## 为什么要有它
 *
 * multipart 的 `filename=` 参数各家客户端填的不一样：浏览器填**原始 UTF-8**，
 * 而 Expo SDK 57 的原生 `fetch`（`expo/fetch`）会无条件过一遍
 * `encodeURIComponent`（见 `expo/src/winter/fetch/convertFormData.ts`
 * 的 `encodeFilename`，没有开关可关）。
 *
 * 服务端从字符串上分不出这两种：`a%20b.png` 既可能是「文件就叫这个名字」，
 * 也可能是「文件叫 `a b.png` 被编码了」。所以服务端不猜 ——
 * **只在客户端显式声明时才解**（`backend/routers/uploads.py`）。
 *
 * ## 什么时候声明
 *
 * 声明的内容是「本请求里的文件名经过了 URI 组件编码」，那就只有
 * **真的会用 `expo/fetch` 那个编码器**的运行时要声明：
 *
 * - 原生（android / ios）：走 `expo/fetch`，会编码 → **必须声明**；
 * - Web：走浏览器自己的 `FormData`，发的是原始 UTF-8 → **绝不能声明**，
 *   声明了会把一个字面叫 `a%20b.png` 的文件解成 `a b.png`，那是回归。
 *
 * 字段名与取值必须与 `backend/routers/uploads.py` 里的字面量逐字相同，
 * 两侧由后端测试对账（`backend/tests/test_filename_encoding.py`）。
 */

/** 说明文件名编码方式的表单字段名 */
export const FILENAME_ENCODING_FIELD = 'filename_encoding'

/** 目前唯一认得的编码值：URI 组件编码（`encodeURIComponent`） */
export const URI_COMPONENT = 'uri-component'

/**
 * 这个运行时的文件名会不会被 URI 组件编码。
 *
 * 判据是「跑的是不是浏览器」，因为编码是 `expo/fetch` 干的、
 * 而它只在原生上替换掉全局 `fetch`。收成一个带名字的函数是为了让
 * 「谁该声明」这件事只有一个说法，而不是散在调用点上的 `Platform.OS !== 'web'`。
 */
export function declaresUriEncodedFilenames(platformOS: string): boolean {
  return platformOS !== 'web'
}
