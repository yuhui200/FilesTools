#!/usr/bin/env python3
"""Android 发行包修复的验收：**明文策略**与**运行期地址配置**这两件事有没有真的落地。

## 这个脚本在防什么

0.1.0 那份 APK 装得上、打得开、界面全正常，**但一次转换也做不了**。两个原因：

1. 清单里既没有 ``android:usesCleartextTraffic``、也没有 ``android:networkSecurityConfig``，
   而 ``targetSdkVersion`` 是 36 —— 系统按「拒绝明文 HTTP」的默认策略办。
2. 后端地址是打包时烤死的，装到设备上**没有任何入口**能改。

两件事都不会让构建失败、不会让 TypeScript 报错、也不会让「在 Expo Go 里跑一遍」变红。
所以它们**只能靠断言守**，而且必须断言在**真的产物**上（APK 里的二进制 XML），
不是断言在源码「看起来对」上 —— 源码里写着 `networkSecurityConfig` 而 prebuild
没把它写进清单，是完全可能的（0.1.0 就是这样）。

## 分三段

* **A 生成物**：``mobile/android/`` 里 prebuild 出来的三个 source set，
  清单属性与 ``network_security_config.xml`` 的内容。
* **B 产物**：``release/v<VERSION>/android/*.apk`` 里**真正的**二进制清单与资源，
  用 SDK 自带的 ``apkanalyzer`` 解出来读。环境里没有 apkanalyzer 就标 NOT EXECUTED，
  **不判 PASS** —— 没读到的 APK 不能算验过。
* **C 源码**：运行期地址配置的那几处连接（Provider 的装配顺序、client 不许碰本地存储等）。
  这些是静态断言，但每一条都对应一个**会静默失效**的具体故障。

跑法::

    python scripts/verify_android_release.py
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parent.parent
MOBILE = ROOT / "mobile"
ANDROID_PROJECT = MOBILE / "android"
REPORT = ROOT / "scripts" / "verify_android_release_report.txt"

#: 资源名。prebuild 插件与清单引用必须指同一个。
RESOURCE = "network_security_config"

#: release 清单里**必须**放行的三个主机。它们对外部主机没有意义
#: （手机上的 ``127.0.0.1`` 是手机自己，``10.0.2.2`` 只有模拟器解析得了），
#: 是「后端跑在同一台设备上」与 ``adb reverse`` 这两条用法能成立的前提。
REQUIRED_RELEASE_HOSTS = {"localhost", "127.0.0.1", "10.0.2.2"}

#: ``EXPO_PUBLIC_API_BASE_URL`` 在打包时是 ``http://127.0.0.1:8011``，
#: 它的主机已经在上面那三个里，所以 release 白名单**应当恰好**是这三个。
#: 多一个就说明「白名单」变成了「谁都能进」；少一个说明回环用不了。
EXPECTED_RELEASE_HOSTS = REQUIRED_RELEASE_HOSTS

results: list[tuple[str, bool, str]] = []
not_executed: list[tuple[str, str]] = []
notes: list[str] = []


def check(area: str, ok: bool, label: str) -> None:
    results.append((area, bool(ok), label))
    print(f"  {'PASS' if ok else 'FAIL'}  {label}", flush=True)


def skip(area: str, label: str, reason: str) -> None:
    not_executed.append((area, f"{label} —— {reason}"))
    print(f"  NOT EXECUTED  {label} —— {reason}", flush=True)


def note(text: str) -> None:
    notes.append(text)
    print(f"  NOTE  {text}", flush=True)


def section(title: str) -> None:
    print(f"\n=== {title} ===", flush=True)


def read_version() -> str:
    return (ROOT / "VERSION").read_text(encoding="utf-8").strip()


#: 块注释与行注释。行注释**放过 ``://``** —— 否则 ``http://127.0.0.1`` 会被从中间
#: 切开，后面的代码跟注释粘成一片，扫出来的东西全是假的。
#: （同一条正则与同样的理由在 ``verify_mobile_phase11a.py`` 里已有一份；
#: 两边各自持有，不互相 import —— 验收脚本之间不该有依赖。）
BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)
LINE_COMMENT = re.compile(r"(?<!:)//[^\n]*")


def strip_comments(text: str) -> str:
    """去掉注释再扫。

    这个脚本第一版就是栽在这里：``client.ts`` 的注释里写着「**不要** import 本地
    存储」「写成 ``process.env[name]`` 替换不掉」，插件的注释里写着「0.1.0 的清单里
    没有 ``usesCleartextTraffic``」—— 全是**在解释为什么不能这么做**，却被当成了
    「这么做了」。断言打在注释上，只会在改注释的时候乱叫，那样的检查留不住。
    """
    return LINE_COMMENT.sub("", BLOCK_COMMENT.sub("", text))


# ----------------------------------------------------------------------
# 生成物的解析
# ----------------------------------------------------------------------


def application_attrs(manifest_path: Path) -> dict[str, str]:
    """读生成清单里 ``<application>`` 的属性（键取**本地名**，去掉命名空间前缀）。

    ⚠️ 必须按本地名取。``xml.etree`` 会把 ``android:networkSecurityConfig`` 展开成
    ``{http://schemas.android.com/apk/res/android}networkSecurityConfig`` ——
    按 ``key.startswith("android:")`` 过滤的话**一个属性都留不下**，
    于是「清单里没有 networkSecurityConfig」这种最要命的结论会变成假的
    （这个脚本第一版就是这么写的，当场把三条本来正确的断言报成红的）。
    """
    root = ET.parse(manifest_path).getroot()
    application = root.find("application")
    if application is None:
        return {}
    # 只关心 android: 命名空间下的那几个；按本地名收，重名不可能（清单里
    # 一个属性只能有一个命名空间）。
    return {
        key.split("}")[-1]: value
        for key, value in application.attrib.items()
        if "}" in key
    }


def parse_network_config(path: Path) -> dict:
    """把一个 ``network-security-config`` XML 读成可断言的结构。

    ``xml.etree`` 而不是正则：这个文件的结构就是全部信息，
    正则匹配出来的「像是对了」不代表 XML 真能被系统接受。
    """
    root = ET.parse(path).getroot()
    base = root.find("base-config")
    domains: set[str] = set()
    for config in root.findall("domain-config"):
        for domain in config.findall("domain"):
            domains.add((domain.text or "").strip())
    return {
        "base_cleartext": (base.get("cleartextTrafficPermitted") if base is not None else None),
        "domains": domains,
        "domain_config_cleartext": sorted(
            {c.get("cleartextTrafficPermitted") for c in root.findall("domain-config")}
        ),
    }


# ----------------------------------------------------------------------
# A 段：prebuild 生成物
# ----------------------------------------------------------------------


def section_generated(version: str) -> None:
    section("A 生成物（mobile/android/ 的清单与资源）")
    area = "GENERATED"

    plugin = MOBILE / "plugins" / "withAndroidNetworkSecurity.js"
    check(area, plugin.is_file(),
          f"插件在 {plugin.relative_to(ROOT).as_posix()}")

    app_json = (MOBILE / "app.json").read_text(encoding="utf-8")
    check(area, "./plugins/withAndroidNetworkSecurity" in app_json,
          "app.json 的 plugins 里注册了这个插件")

    # versionName 是 **prebuild 时**从 `app.json` 的 `expo.version` 抄进 build.gradle 的。
    # 所以「改了 VERSION 却没重跑 prebuild」会得到一个代码是新的、版本号是旧的 APK ——
    # 本轮就是这么栽的：VERSION 已经是 0.1.1，打出来的包装的却是 0.1.0。
    # 这类错不会让构建失败，只会让「这个包是哪个版本」变成一句没有依据的话。
    gradle = ANDROID_PROJECT / "app" / "build.gradle"
    if gradle.is_file():
        found = re.search(r'versionName\s+"([^"]*)"', gradle.read_text(encoding="utf-8"))
        check(area, found is not None and found.group(1) == version,
              f"生成的 build.gradle 里 versionName = {found.group(1) if found else None!r}"
              f"（VERSION 是 {version} —— 对不上说明改完版本没重跑 prebuild）")

    if plugin.is_file():
        source = strip_comments(plugin.read_text(encoding="utf-8"))
        # 不用 usesCleartextTraffic 是**有意的**：它在 networkSecurityConfig 在场时
        # 会被系统忽略。两个都写，读代码的人会以为前者生效 —— 那正是 0.1.0
        # 「以为 debug 那套也管 release」的同一种误读。
        check(area, "usesCleartextTraffic" not in source,
              "插件不用 usesCleartextTraffic（它在有 networkSecurityConfig 时会被忽略，"
              "留着只会误导）")
        check(area, "cleartextTrafficPermitted=\"false\"" in source.replace("'", '"'),
              "插件里 release 的 base-config 写的是 false")

    if not ANDROID_PROJECT.is_dir():
        skip(area, "prebuild 生成物", "mobile/android/ 不存在 —— 先跑 npx expo prebuild")
        return

    for source_set in ("main", "debug", "debugOptimized"):
        manifest = ANDROID_PROJECT / "app" / "src" / source_set / "AndroidManifest.xml"
        if not manifest.is_file():
            check(area, False, f"src/{source_set}/AndroidManifest.xml 存在")
            continue
        attrs = application_attrs(manifest)
        if source_set == "main":
            check(area, attrs.get("networkSecurityConfig") == f"@xml/{RESOURCE}",
                  f"release 清单的 <application> 挂着 networkSecurityConfig"
                  f"（实际 {attrs.get('networkSecurityConfig')!r}）")
            check(area, "usesCleartextTraffic" not in attrs,
                  "release 清单**没有** usesCleartextTraffic（两个都写会互相盖住）")
        else:
            # debug 变体沿用 prebuild 模板的行为：全开明文。它只覆盖 debug，
            # 正式 Release 用的是 src/main 那份。
            check(area, attrs.get("usesCleartextTraffic") == "true",
                  f"{source_set} 清单保持模板的 usesCleartextTraffic=true")

    for source_set, expect_cleartext in (("main", "false"), ("debug", "true"), ("debugOptimized", "true")):
        resource = ANDROID_PROJECT / "app" / "src" / source_set / "res" / "xml" / f"{RESOURCE}.xml"
        if not resource.is_file():
            check(area, False, f"src/{source_set}/res/xml/{RESOURCE}.xml 存在")
            continue
        parsed = parse_network_config(resource)
        check(area, parsed["base_cleartext"] == expect_cleartext,
              f"{source_set} 的 base-config cleartextTrafficPermitted={parsed['base_cleartext']}"
              f"（期望 {expect_cleartext}）")
        if source_set == "main":
            check(area, parsed["domains"] >= REQUIRED_RELEASE_HOSTS,
                  f"release 白名单含回环与模拟器宿主别名（实际 {sorted(parsed['domains'])}）")
            check(area, parsed["domains"] == EXPECTED_RELEASE_HOSTS,
                  f"release 白名单**恰好**是期望的三个主机"
                  f"（多出来的是 {sorted(parsed['domains'] - EXPECTED_RELEASE_HOSTS)}，"
                  f"少的是 {sorted(EXPECTED_RELEASE_HOSTS - parsed['domains'])}）")
            note("release 的白名单是显式的、构建时确定的：默认全拒，只对列出来的主机开明文。"
                 "「没有全局放开任意 HTTP」这句话在这份清单里是可以一条条数出来的。")


# ----------------------------------------------------------------------
# B 段：真的 APK
# ----------------------------------------------------------------------


def sdk_roots() -> list[Path]:
    roots: list[Path] = []
    for name in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        value = os.environ.get(name)
        if value:
            roots.append(Path(value))
    local = ANDROID_PROJECT / "local.properties"
    if local.is_file():
        for line in local.read_text(encoding="utf-8").splitlines():
            if line.startswith("sdk.dir="):
                roots.append(Path(line.split("=", 1)[1].replace("\\:", ":").replace("\\\\", "/")))
    roots.append(Path("D:/Android/Sdk"))
    return roots


def find_apkanalyzer() -> Path | None:
    for root in sdk_roots():
        for pattern in ("cmdline-tools/*/bin/apkanalyzer.bat", "cmdline-tools/*/bin/apkanalyzer"):
            for candidate in root.glob(pattern):
                return candidate
    return None


def find_aapt2() -> Path | None:
    """``aapt2`` 用来把 ``@ref/0x…`` 还原成资源名与包内路径（见下）。"""
    for root in sdk_roots():
        for pattern in ("build-tools/*/aapt2.exe", "build-tools/*/aapt2"):
            found = sorted(root.glob(pattern))
            if found:
                return found[-1]                      # 目录名带版本号，取最新的
    return None


def run_tool(tool: Path, *argv: str) -> tuple[int, str]:
    try:
        done = subprocess.run(
            [str(tool), *argv], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=300, shell=(os.name == "nt"),
        )
    except (OSError, subprocess.SubprocessError) as problem:
        return 1, f"{type(problem).__name__}: {problem}"
    return done.returncode, (done.stdout or "")


def run_apkanalyzer(analyzer: Path, *argv: str) -> tuple[int, str]:
    return run_tool(analyzer, *argv)


#: ``aapt2 dump resources`` 里一条资源定义的两行：
#:     resource 0x7f130005 xml/network_security_config
#:       () (file) res/8G.xml type=XML
RESOURCE_DEF = re.compile(r"^\s*resource (0x[0-9a-fA-F]+) ([a-z-]+)/(\S+)\s*$")
RESOURCE_FILE = re.compile(r"^\s*\(.*?\) \(file\) (\S+) type=(\w+)")


def dump_apk_resources(aapt2: Path, apk: Path) -> dict[str, tuple[str, str, str]]:
    """``资源 id -> (类型, 名字, 包内路径)``。

    为什么非要有这一步：**release 构建会把资源路径和资源名一起混淆掉**
    （AGP 的 resource optimization），APK 里躺着的是 ``res/8G.xml``，
    清单里写的是 ``@ref/0x7f130005``。所以「按名字找 res/xml/xxx.xml」这种
    查法在 release 包上**永远找不到** —— 不是策略没进去，是查法不对。
    本轮第一版脚本就是这么写的，真拿 0.1.0/0.1.1 的包一跑就会假红。
    """
    code, dumped = run_tool(aapt2, "dump", "resources", str(apk))
    if code != 0:
        return {}

    table: dict[str, tuple[str, str, str]] = {}
    last_id: str | None = None
    for line in dumped.splitlines():
        definition = RESOURCE_DEF.match(line)
        if definition:
            last_id = definition.group(1).lower()
            table[last_id] = (definition.group(2), definition.group(3), "")
            continue
        entry = RESOURCE_FILE.match(line)
        if entry and last_id is not None and table[last_id][2] == "":
            kind, name, _ = table[last_id]
            table[last_id] = (kind, name, entry.group(1))
            last_id = None
    return table


def section_apk(version: str) -> None:
    section("B 真的 APK（release/v%s/android/）" % version)
    area = "APK"

    folder = ROOT / "release" / f"v{version}" / "android"
    apks = sorted(folder.glob("*.apk")) if folder.is_dir() else []
    if not apks:
        skip(area, "已发布 APK", f"{folder.relative_to(ROOT).as_posix()} 里没有 .apk"
                                  " —— 先跑 build_release.py")
        return

    apk = apks[0]
    check(area, len(apks) == 1, f"发行目录里恰好一份 APK（{len(apks)} 份）")
    note(f"验的是 {apk.relative_to(ROOT).as_posix()}（{apk.stat().st_size:,} 字节）")

    analyzer = find_apkanalyzer()
    if analyzer is None:
        skip(area, "APK 里的清单与明文策略",
             "本机找不到 apkanalyzer，读不了 APK 内部的二进制清单与资源")
        return

    code, dumped = run_apkanalyzer(analyzer, "manifest", "print", str(apk))
    if code != 0 or "<manifest" not in dumped:
        skip(area, "APK 里的清单", f"apkanalyzer manifest print 退出码 {code}")
        return

    # 版本号：**必须**是这一版的版本号。prebuild 是把 app.json 的 expo.version
    # 抄进 build.gradle 的，所以「改了 VERSION 却没重跑 prebuild」会打出
    # 一个代码是新的、版本号是旧的真包 —— 构建不会报错，只是这个包说不清自己是哪版。
    # 本轮就是这么栽过一次（0.1.1 的代码 / 0.1.0 的 versionName），加在这钉死。
    code, name = run_apkanalyzer(analyzer, "manifest", "version-name", str(apk))
    check(area, code == 0 and name.strip() == version,
          f"**已发布 APK** 的 versionName 是 {name.strip() or '（读不出来）'}"
          f"（VERSION 是 {version}）")

    application = re.search(r"<application\b[^>]*>", dumped, re.S)
    if application is None:
        check(area, False, "解出来的清单里有 <application>")
        return
    tag = application.group(0)
    check(area, "usesCleartextTraffic" not in tag,
          "**已发布 APK** 的清单里没有 usesCleartextTraffic")
    # 有 networkSecurityConfig 时，系统**只**认它，android:usesCleartextTraffic 会被忽略。
    # 两条一起断言：既要有策略，也不能留那个「看着像开着」的假开关。

    reference = re.search(r'android:networkSecurityConfig="([^"]+)"', tag)
    check(area, reference is not None,
          "**已发布 APK** 的清单里挂着 networkSecurityConfig")
    if reference is None:
        return

    # 清单里那一串可能是 ``@xml/network_security_config``（没混淆），
    # 也可能是 ``@ref/0x7f130005``（release 混淆后）。两种都要能落回同一个资源，
    # 光比对字面量会把混淆后的正确包判成红的。
    value = reference.group(1)
    aapt2 = find_aapt2()
    if aapt2 is None:
        skip(area, "APK 里的明文策略",
             "本机找不到 aapt2，读不了资源 id 与包内路径的对应关系")
        return
    table = dump_apk_resources(aapt2, apk)
    if not table:
        skip(area, "APK 里的明文策略", "aapt2 dump resources 没吐出资源表")
        return

    if value.startswith("@ref/") or value.startswith("@0x"):
        key = value.split("/")[-1].lower()
        resolved = table.get(key)
        check(area, resolved is not None and resolved[:2] == ("xml", RESOURCE),
              f"清单里的 {value} 指向 xml/{RESOURCE}"
              f"（实际 {resolved[:2] if resolved else '查不到'}）")
        target = resolved[2] if resolved else ""
    else:
        check(area, value == f"@xml/{RESOURCE}",
              f"清单里的明文策略引用是 @xml/{RESOURCE}（实际 {value}）")
        target = next((path for kind, res, path in table.values()
                       if (kind, res) == ("xml", RESOURCE)), "")

    if not target:
        check(area, False, f"**已发布 APK** 里定位得到 xml/{RESOURCE} 的包内路径")
        return
    note(f"包内路径 {target}（release 构建把资源名与路径都混淆了，按名字找是找不到的）")

    code, decoded = run_apkanalyzer(analyzer, "resources", "xml", "--file", target, str(apk))
    if code != 0 or "<network-security-config" not in decoded:
        check(area, False,
              f"**已发布 APK** 里能解出 {target}（退出码 {code}）"
              + (f"：{decoded.strip()[:200]}" if decoded.strip() else ""))
    else:
        root = ET.fromstring(decoded[decoded.index("<network-security-config"):])
        base = root.find("base-config")
        check(area, base is not None and base.get("cleartextTrafficPermitted") == "false",
              "**已发布 APK** 里 release 策略的 base-config 是 cleartextTrafficPermitted=false")
        domains = {(d.text or "").strip()
                   for cfg in root.findall("domain-config") for d in cfg.findall("domain")}
        check(area, domains == EXPECTED_RELEASE_HOSTS,
              f"**已发布 APK** 里的白名单恰好是期望的三个主机（实际 {sorted(domains)}）")

    # 包里的 JS bundle：证明装的是这一版的代码，而不是「构建成功了但用的是旧产物」
    with ZipFile(apk) as bundle:
        names = bundle.namelist()
        entry = next((n for n in names if n.endswith(".bundle") or n.endswith(".jsbundle")), None)
        if entry is None:
            skip(area, "APK 里的 JS bundle", "包里找不到 .bundle")
        else:
            text = bundle.read(entry).decode("utf-8", "replace")
            # 0.1.1 才有的码：它只在这版新增的「明文被挡」分支里出现
            check(area, "cleartext_blocked" in text,
                  "APK 的 JS bundle 里有本版新增的 cleartext_blocked 分支")
            check(area, "EXPO_PUBLIC_API_BASE_URL" not in text,
                  "bundle 里**没有**变量名本身（babel 是纯文本替换，证明地址是字面量而不是运行时读的）")
            # 端口要**限定在合法范围**（1–65535），不能图省事写成 ``\d+``。
            # JS bundle 是一张首尾相连的字符串表，条与条之间没有任何分隔：
            # 默认地址后面紧跟着的正好是一个以数字开头的哈希串
            # （实测 ``…:8011`` + ``90ab078c57…``），贪婪的 ``\d+`` 会把它一起吞掉，
            # 于是报告里印出一个根本不存在的 ``http://127.0.0.1:801190``。
            # 地址本身没错，错的是这里把「下一条字符串」当成了端口的一部分。
            addresses = sorted(set(re.findall(
                r"http://\d+\.\d+\.\d+\.\d+:(?:6553[0-5]|655[0-2]\d|65[0-4]\d\d"
                r"|6[0-4]\d\d\d|[1-5]\d\d\d\d|[1-9]\d{0,3})", text)))
            note(f"bundle 里烤着的默认地址：{addresses or '（一个都没有）'} —— 它只是"
                 "「出厂默认值」，用户可以在 App 里改（见 C 段）")


# ----------------------------------------------------------------------
# C 段：运行期地址配置的几处连接
# ----------------------------------------------------------------------


def section_source() -> None:
    section("C 运行期地址配置（源码里的连接点）")
    area = "SOURCE"

    client = (MOBILE / "src" / "services" / "api" / "client.ts")
    if not client.is_file():
        check(area, False, "src/services/api/client.ts 存在")
        return
    # **先剥注释再扫**：这个文件的注释里逐字写着「不要 import 本地存储」
    # 「写成 process.env[name] 替换不掉」，不剥的话扫出来全是假红。
    text = strip_comments(client.read_text(encoding="utf-8"))

    for symbol in ("getApiBaseUrl", "setApiBaseUrlOverride", "normalizeApiBaseUrl",
                   "apiBaseUrlProblem", "CLIENT_CODES"):
        check(area, re.search(rf"export (?:function|const) {symbol}\b", text) is not None,
              f"client.ts 导出 {symbol}")

    check(area, "process.env.EXPO_PUBLIC_API_BASE_URL" in text,
          "读法仍是 process.env.EXPO_PUBLIC_API_BASE_URL 这个成员表达式")
    check(area, "process.env[" not in text,
          "没有 process.env[...] 这种替换不掉的读法")
    # 这条是**硬约束**：verify_mobile_phase11a.py 会把 errorMessages.ts（它 import
    # client.ts 的 CLIENT_CODES）编成 CJS 交给 node 跑，产物里一旦出现原生模块的
    # require，那个脚本就整个跑不起来。
    check(area, "async-storage" not in text and "@/storage" not in text,
          "client.ts **不碰**本地存储（它要能被编译成 CJS 交给 node 跑）")

    provider = MOBILE / "src" / "state" / "ApiConfigProvider.tsx"
    check(area, provider.is_file(), "src/state/ApiConfigProvider.tsx 存在")
    if provider.is_file():
        source = strip_comments(provider.read_text(encoding="utf-8"))
        check(area, "@/storage/kv" in source, "ApiConfigProvider 通过 storage/kv 读写本地值")
        check(area, "setApiBaseUrlOverride" in source,
              "ApiConfigProvider 把读到的值写进 client 的覆盖位")
        check(area, "ready" in source, "ApiConfigProvider 暴露 ready（下游据此等地址就绪）")

    layout = strip_comments((MOBILE / "app" / "_layout.tsx").read_text(encoding="utf-8"))
    api_at = layout.find("<ApiConfigProvider>")
    caps_at = layout.find("<CapabilitiesProvider>")
    check(area, api_at != -1 and caps_at != -1,
          "根布局同时装配了 ApiConfigProvider 与 CapabilitiesProvider")
    # 顺序错了的话，CapabilitiesProvider 一挂载就拿着**构建时**的地址发一轮请求，
    # 改过地址的用户每次冷启动都会先看到一次「连不上」。
    check(area, -1 < api_at < caps_at,
          "ApiConfigProvider 在 CapabilitiesProvider **外面**（地址先就绪，再拉能力表）")

    caps = strip_comments(
        (MOBILE / "src" / "state" / "CapabilitiesProvider.tsx").read_text(encoding="utf-8")
    )
    check(area, re.search(r"if \(!addressReady\) return", caps) is not None,
          "CapabilitiesProvider 在地址就绪前不发请求")
    check(area, re.search(r"\}, \[load, addressReady, baseUrl\]\)", caps) is not None,
          "地址变了会重拉能力表（换了服务器就是另一份工具表）")

    profile = strip_comments(
        (MOBILE / "src" / "screens" / "ProfileScreen.tsx").read_text(encoding="utf-8")
    )
    check(area, "TextInput" in profile, "「我的」页有可编辑的地址输入框")
    check(area, "useApiConfig" in profile, "「我的」页用的是运行期的地址，不是构建时常量")
    check(area, "API_BASE_URL" not in profile,
          "「我的」页不再直接引用构建时常量 API_BASE_URL")
    check(area, "apiBaseUrlProblem" in profile, "保存前会校验地址（能看出来的错不留给网络层）")

    errors = strip_comments(
        (MOBILE / "src" / "utils" / "errorMessages.ts").read_text(encoding="utf-8")
    )
    web_errors = strip_comments(
        (ROOT / "frontend" / "src" / "utils" / "errorMessages.ts").read_text(encoding="utf-8")
    )
    check(area, "CLIENT_CODES.cleartext" in errors,
          "移动端文案表里有明文被挡这一条（不能混进「无法连接服务器」）")
    check(area, "cleartext_blocked" not in web_errors,
          "Web 端文案表里**没有**这条 —— 它是 Android 系统策略的产物，不属于浏览器")


# ----------------------------------------------------------------------
# 报告与汇总（骨架照抄 verify_release.py）
# ----------------------------------------------------------------------

SPEC_AREAS = ("GENERATED", "APK", "SOURCE")


def area_summary() -> list[tuple[str, bool, int]]:
    area_order: list[str] = []
    for area, _ok, _label in results:
        if area not in area_order:
            area_order.append(area)
    ordered = [a for a in SPEC_AREAS if a in area_order]
    ordered += [a for a in area_order if a not in SPEC_AREAS]

    area_ok: dict[str, bool] = {a: True for a in area_order}
    for area, ok, _label in results:
        area_ok[area] = area_ok[area] and ok

    skipped: dict[str, int] = {}
    for area, _reason in not_executed:
        skipped[area] = skipped.get(area, 0) + 1

    return [(a, area_ok[a], skipped.get(a, 0)) for a in ordered]


def write_report() -> tuple[int, int]:
    passed = sum(1 for _a, ok, _l in results if ok)
    failed = [f"[{a}] {label}" for a, ok, label in results if not ok]

    lines = ["FILETOOLS ANDROID RELEASE FIX VERIFICATION", "=" * 40, ""]
    for area, ok, skipped in area_summary():
        suffix = f"（{skipped} 项 NOT EXECUTED）" if skipped else ""
        lines.append(f"[{'PASS' if ok else 'FAIL'}] {area}{suffix}")

    lines += ["", f"TOTAL: {passed} / {len(results)}",
              f"FAILED: {len(failed)}", f"NOT EXECUTED: {len(not_executed)}"]
    if notes:
        lines += ["", "如实记录（不是断言）："] + [f"  · {n}" for n in notes]
    if not_executed:
        lines += ["", "未执行的项及原因："] + [f"  - [{a}] {label}" for a, label in not_executed]
    if failed:
        lines += ["", "失败项："] + [f"  - {f}" for f in failed]
    else:
        lines += ["", "全部通过（未执行项见上，它们**不算通过**）。"]

    REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return passed, len(failed)


def main() -> int:
    print("FILETOOLS ANDROID RELEASE FIX VERIFICATION")
    print("=" * 40)
    print(f"仓库根：{ROOT}")

    version = read_version()
    section_generated(version)
    section_apk(version)
    section_source()

    passed, failed_count = write_report()

    print("\n" + "=" * 40)
    for area, ok, skipped in area_summary():
        suffix = f"（{skipped} 项 NOT EXECUTED）" if skipped else ""
        print(f"[{'PASS' if ok else 'FAIL'}] {area}{suffix}")
    print(f"\nTOTAL: {passed} / {len(results)}")
    print(f"FAILED: {failed_count}")
    print(f"NOT EXECUTED: {len(not_executed)}")
    for area, label in not_executed:
        print(f"  - [{area}] {label}")
    print(f"报告写入 {REPORT.relative_to(ROOT).as_posix()}")
    if failed_count:
        print("\n失败项：")
        for area, ok, label in results:
            if not ok:
                print(f"  - [{area}] {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
