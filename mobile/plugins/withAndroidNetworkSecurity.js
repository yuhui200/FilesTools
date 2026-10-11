/**
 * Android 明文流量（cleartext）策略 —— 本地 Expo config plugin。
 *
 * ## 这个文件为什么存在
 *
 * v0.1.0 发出去的 APK **一次转换都做不了**：清单里既没有
 * `android:usesCleartextTraffic`，也没有 `android:networkSecurityConfig`，
 * 而 `targetSdkVersion` 是 36 —— Android 对 API 28+ 的既定默认就是**拒绝明文 HTTP**，
 * 本项目的后端又只有明文 HTTP。App 装得上、打得开、界面全正常，
 * 但每一个请求都被系统挡在 socket 之前，界面只能说一句「无法连接服务器」。
 *
 * 当时之所以没发现，是因为 `expo prebuild` 的模板**只把**
 * `android:usesCleartextTraffic="true"` 写进 `src/debug/` 与
 * `src/debugOptimized/` 两个清单，**`src/main/`（release 用的那个）没有**；
 * 而移动端的验收一直是在 **Expo Go** 里跑的 —— Expo Go 宿主 App 自己的清单
 * 是开着明文的，所以一路绿灯。
 *
 * `mobile/android/` 是 prebuild 产物、被 `mobile/.gitignore` 整个忽略，
 * 所以**直接改那些生成文件不作数**（下次 prebuild 就没了，而且不进版本库）。
 * 修必须落到 prebuild 配置上 —— 就是这里。
 *
 * ## 策略（规格：不全局放开任意明文 HTTP）
 *
 * - **Release（`src/main/`）**：`base-config cleartextTrafficPermitted="false"` —— 默认**全拒**。
 *   只对显式列进 `<domain-config>` 的主机开明文，一共三类：
 *     1. 回环与模拟器宿主别名（`localhost` / `127.0.0.1` / `10.0.2.2`）——
 *        手机上的 `127.0.0.1` 是手机自己，`10.0.2.2` 只在模拟器里有意义，
 *        开这两个不构成对外部的明文许可，但让「后端跑在同一台设备上」和
 *        `adb reverse` 这两条**本工具明确支持的用法**能用。
 *     2. 构建时配置的后端地址里的主机（**仅当它是 `http://` 时**）——
 *        地址从 `EXPO_PUBLIC_API_BASE_URL` 读，和 App 里那个地址**同一个来源**，
 *        不产生第二份真相。用 `https://` 就一个明文条目都不加。
 *     3. `FILETOOLS_ANDROID_CLEARTEXT_HOSTS` 里额外点名的主机（逗号分隔），
 *        给「后端在局域网、地址又不想写进 `.env`」的场合留一个口子。
 *   **没有 `EXPO_PUBLIC_API_BASE_URL`、也没有这个变量时，明文许可 = 只有第 1 类。**
 *   想连局域网上的 HTTP 后端，构建时把地址或主机名给出来，别指望运行时能绕过去 ——
 *   这是操作系统的策略，不是 App 的策略。
 *
 * - **Debug（`src/debug/` 与 `src/debugOptimized/`）**：`base-config` 直接
 *   `cleartextTrafficPermitted="true"`，与今天 prebuild 模板给的行为**完全一致**。
 *   开发时随便连哪台机器都行，而这份宽松**只存在于 debug 变体**，
 *   正式 Release 的安全边界一点没动。
 *
 * ## 生效方式
 *
 * `src/main/res/xml/network_security_config.xml` 是标准资源名，
 * debug/debugOptimized 各自在自己那一档 `res/xml/` 下放一份同名文件 ——
 * **Android 的资源合并会让变体覆盖 main**，所以两种策略可以共存而互不干扰。
 * 清单里的 `android:networkSecurityConfig` 只写一次（在 main 上），
 * 三种构建都指向同一个资源名，内容由变体决定。
 */

const {
  withAndroidManifest,
  withDangerousMod,
  AndroidConfig,
} = require('@expo/config-plugins')
const fs = require('fs')
const path = require('path')

/** 资源名。改这里就要改清单里的引用，所以只在一处写死。 */
const RESOURCE_NAME = 'network_security_config'

/**
 * 回环与模拟器宿主别名。**这两种地址对外部主机没有意义**：
 * `127.0.0.1` / `localhost` 在手机上指的是手机自己，`10.0.2.2` 只有
 * Android 模拟器解析得了（它被硬编码成「跑模拟器的那台机器」）。
 * 允许它们的明文不构成「任意 HTTP」的口子，却是下面两条用法能成立的前提：
 * 后端就跑在同一台设备上、以及真机用 `adb reverse tcp:8011 tcp:8011` 做端口转发。
 */
const DEVICE_LOCAL_HOSTS = ['localhost', '127.0.0.1', '10.0.2.2']

/** 环境变量里点名的主机，逗号分隔。空值、多余空格、重复项都在这里收敛掉。 */
function hostsFromEnv() {
  return String(process.env.FILETOOLS_ANDROID_CLEARTEXT_HOSTS || '')
    .split(',')
    .map((item) => item.trim().toLowerCase())
    .filter(Boolean)
}

/**
 * 从构建时配置的后端地址里取出主机名 —— **只认 `http://`**。
 *
 * `https://` 不需要任何明文许可，硬塞一个 `<domain>` 进去只会让
 * 「这份清单到底开了什么」变得难读。取不到就返回 null，不猜。
 */
function hostFromApiBaseUrl() {
  const raw = String(process.env.EXPO_PUBLIC_API_BASE_URL || '').trim()
  if (!raw) return null
  let url
  try {
    url = new URL(raw)
  } catch {
    // 地址写错了（少了协议之类）。这里**不报错**：那是 App 该报的错，
    // 构建期抛异常只会把「地址没配好」变成「项目构建不了」。
    return null
  }
  if (url.protocol !== 'http:') return null
  return url.hostname.toLowerCase() || null
}

/** 去重后按字母序排好 —— 同一份输入必须生成同一个文件，否则产物不可复现。 */
function releaseHosts() {
  const hosts = new Set(DEVICE_LOCAL_HOSTS)
  const fromUrl = hostFromApiBaseUrl()
  if (fromUrl) hosts.add(fromUrl)
  for (const host of hostsFromEnv()) hosts.add(host)
  return [...hosts].sort()
}

function renderConfig(hosts) {
  const lines = [
    '<?xml version="1.0" encoding="utf-8"?>',
    '<!--',
    '  由 mobile/plugins/withAndroidNetworkSecurity.js 生成，**不要手改**。',
    '  mobile/android/ 是 expo prebuild 的产物且被 .gitignore 忽略，',
    '  改它不作数：下一次 prebuild 就会被覆盖。要改策略请改那个插件。',
    '-->',
    '<network-security-config>',
    '  <base-config cleartextTrafficPermitted="false" />',
  ]
  if (hosts.length > 0) {
    lines.push('  <domain-config cleartextTrafficPermitted="true">')
    for (const host of hosts) {
      lines.push(`    <domain includeSubdomains="false">${host}</domain>`)
    }
    lines.push('  </domain-config>')
  }
  lines.push('</network-security-config>', '')
  return lines.join('\n')
}

function renderDevConfig() {
  return [
    '<?xml version="1.0" encoding="utf-8"?>',
    '<!--',
    '  开发变体（debug / debugOptimized）专用：明文**全开**，与 expo prebuild',
    '  模板原来的行为一致。它只覆盖 debug 变体的资源，正式 Release 用的',
    '  是 src/main 下那份「默认全拒 + 白名单」的策略。',
    '  由 mobile/plugins/withAndroidNetworkSecurity.js 生成，**不要手改**。',
    '-->',
    '<network-security-config>',
    '  <base-config cleartextTrafficPermitted="true" />',
    '</network-security-config>',
    '',
  ].join('\n')
}

function writeResXml(projectRoot, sourceSet, xml) {
  const dir = path.join(projectRoot, 'app', 'src', sourceSet, 'res', 'xml')
  fs.mkdirSync(dir, { recursive: true })
  const target = path.join(dir, `${RESOURCE_NAME}.xml`)
  const previous = fs.existsSync(target) ? fs.readFileSync(target, 'utf8') : null
  if (previous !== xml) fs.writeFileSync(target, xml, 'utf8')
  return target
}

/** 往 `<application>` 上挂 `android:networkSecurityConfig`，已经对了就不动。 */
function withNetworkSecurityConfigAttribute(config) {
  return withAndroidManifest(config, (cfg) => {
    const application = AndroidConfig.Manifest.getMainApplicationOrThrow(cfg.modResults)
    application.$['android:networkSecurityConfig'] = `@xml/${RESOURCE_NAME}`
    return cfg
  })
}

/** 把三份 XML 落到三个 source set 里。 */
function withNetworkSecurityConfigFiles(config) {
  return withDangerousMod(config, [
    'android',
    (cfg) => {
      const projectRoot = cfg.modRequest.platformProjectRoot
      writeResXml(projectRoot, 'main', renderConfig(releaseHosts()))
      writeResXml(projectRoot, 'debug', renderDevConfig())
      writeResXml(projectRoot, 'debugOptimized', renderDevConfig())
      return cfg
    },
  ])
}

module.exports = function withAndroidNetworkSecurity(config) {
  return withNetworkSecurityConfigFiles(withNetworkSecurityConfigAttribute(config))
}
