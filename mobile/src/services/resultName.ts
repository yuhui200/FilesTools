/**
 * 结果文件落盘时该叫什么名字 —— 一个纯函数，零 import。
 *
 * 这个文件**刻意零 import**（与 `./api/filenameEncoding.ts` 同一个理由）：
 * `scripts/verify_mobile_phase11a.py` 会用一份最小的 tsconfig 把纯逻辑模块
 * 编成 CJS 交给 node 跑，任何 `expo-*` / `react-native` 的 import 都会
 * 把那条测试路断掉 —— 而 `download.ts` 正是 import 了一堆原生模块的那个，
 * 所以判据必须放在这里、不能留在它里面。
 *
 * ## 为什么客户端要自己改名
 *
 * 服务端随任务一起给出结果文件名，界面上显示的就是它。但**原生下载落盘时
 * 用的不是这个名**：Android 那边是
 * `URLUtil.guessFileName(url, Content-Disposition, Content-Type)`，
 * 它只认 RFC 6266 里**普通**的 `filename="…"` 形式。而结果名含非 ASCII 时，
 * Starlette 按 RFC 5987 只发扩展形式 `filename*=utf-8''%E6%B5%8B…`，
 * 普通形式一个字都不发 —— `guessFileName` 认不出来，就回退成 URL 的最后一段，
 * 也就是那个**一次性下载令牌**。
 *
 * 症状（2026-10-11 的 Android 真机复验实测）：界面上写着「测试图片.jpg」，
 * 盘上却叫 `b1f9e0520d348bba127e8c9196b3f8db.jpg`；分享 / 打开把它交给
 * 别的 App，对方收到的就是那串令牌。纯 ASCII 的结果名走普通形式，
 * 所以这条从 0.1.0 起一直藏着 —— 只有中文名才触发。
 *
 * 改名本来就该由客户端做：`result.filename` 是服务端随任务给的、
 * 界面已经在显示的那个名字，比让系统去猜响应头可靠。Web 分支一直用的
 * 就是它（`download.ts::downloadOnWeb`），这里只是让原生跟上，
 * 顺带把两个平台的口径统一成同一个。
 *
 * ## 判据
 *
 * 返回**要改成的名字**；不需要改时返回 `null`，调用方据此跳过改名。
 *
 * 名字是从网络来的，所以这里只放行「一个普通的文件基名」。拒绝的不是
 * 「好不好看」，而是**会改变文件落在哪里**的那几种：改名的底层实现
 * （`File.rename(newName)`）是按「父目录 + 新名字」拼路径的，
 * 放一个带 `/` 或 `..` 的名字进去，等于把文件挪到别的目录去。
 */
export function solveResultName(
  wanted: string | null | undefined,
  current: string,
): string | null {
  if (typeof wanted !== 'string') return null

  // 前后空白一律削掉：名字要拿去和 `current` 比、要真的用作文件名，
  // 留着一个尾空格会让「看起来一样」的两个名字变成两个不同的文件。
  const name = wanted.trim()
  if (!name) return null

  // `.` / `..` 是目录本身，不是文件名
  if (name === '.' || name === '..') return null

  // 路径分量：`/` 是 POSIX 的，`\` 是 Windows 的（Android 上也当分隔符处理）
  if (name.includes('/') || name.includes('\\')) return null

  // 已经叫这个名了（ASCII 名走普通形式时就是这种情况）—— 不必多此一举
  if (name === current) return null

  return name
}
