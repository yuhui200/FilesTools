#!/usr/bin/env python3
"""FileTools 的**最终总验收入口**（Final Hardening §十四）。

它**不替代**任何一个既有验收脚本 —— 那些脚本各自深挖一个平台
（``verify_branding.py`` 逐像素量图标、``verify_desktop.py`` 真的构建、
``verify_mobile_phase11a.py`` 跑 107 条），而这一份回答的是另一个问题：

    「作为一个准备交付的产品，该在的东西是不是都在、是不是都对得上？」

所以它只做**快而广**的横切检查：版本是否只有一个真相、四平台的品牌是不是
同一份源装的、各平台的产物与脚本是否就位、各消费方指向的文件是不是真的在盘上。
深挖交给那三个脚本，它们的结果由本文件**汇总引用**，不在这里重跑
（重跑一遍等于把整轮回归的时长翻倍，而它并不产生新的信息）。

**环境够不着的项一律输出 ``NOT EXECUTED`` 并写明原因，绝不输出 PASS。**
这条比什么都重要：一份把所有格子都涂绿、里面却混着「配置存在」冒充
「真机验过」的报告，比一份诚实的报告危险得多。

跑法::

    python scripts/verify_final.py

    FILETOOLS_BACKEND_PY   覆盖后端解释器路径（默认 backend/.venv）
"""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 版本检查复用 sync_version 的读取器 —— 校验与写入必须是同一套代码，
# 否则只是在测「脚本 B 能不能同意脚本 A」。
sys.path.insert(0, str(ROOT / "scripts"))
import sync_version as sv  # noqa: E402

# 禁用产品名清单也复用 —— 两处各写一份就等于自己造了第二份真相
# （那份清单还解释了为什么它得拆开拼，见 verify_branding.py 的注释）。
import verify_branding as vb  # noqa: E402

REPORT = ROOT / "scripts" / "verify_final_report.txt"

BRANDING_SOURCE = ROOT / "branding" / "source"
BRANDING_GENERATED = ROOT / "branding" / "generated"
ICON_SVG = BRANDING_SOURCE / "filetools-icon.svg"
LOGO_SVG = BRANDING_SOURCE / "filetools-logo.svg"

FRONTEND_DIST = ROOT / "frontend" / "dist"
FRONTEND_DIST_DESKTOP = ROOT / "frontend" / "dist-desktop"
MOBILE_ASSETS = ROOT / "mobile" / "assets"
DESKTOP_ICONS = ROOT / "desktop" / "src-tauri" / "icons"
INSTALLER = ROOT / "desktop" / "artifacts" / "FileTools-Setup-x64.exe"
TAURI_CONF = ROOT / "desktop" / "src-tauri" / "tauri.conf.json"

#: 品牌产物里必须存在的那些（相对 ``branding/generated/``）。
REQUIRED_BRAND_ASSETS = (
    "manifest.sha256",
    "web/favicon.svg",
    "web/favicon.ico",
    "web/apple-touch-icon.png",
    "web/icon-192.png",
    "web/icon-512.png",
    "web/manifest.webmanifest",
    "ios/icon-1024.png",
    "windows/icon.ico",
    "windows/icon-256.png",
)

#: 各平台消费方（相对仓库根）-> 它该拿到的那一份品牌产物。
BRAND_CONSUMERS = (
    ("frontend/public/favicon.svg", "web/favicon.svg"),
    ("frontend/public/favicon.ico", "web/favicon.ico"),
    ("frontend/public/icon-512.png", "web/icon-512.png"),
    ("mobile/assets/icon.png", "android/icon.png"),
    ("mobile/assets/favicon.png", "android/favicon.png"),
    ("desktop/src-tauri/icons/icon.ico", "windows/icon.ico"),
)

#: 判「这一项本轮真跑过」的验收脚本。缺一个就说明交付不完整。
REQUIRED_SCRIPTS = (
    "verify_phase2.py", "verify_phase3.py", "verify_phase4.py", "verify_phase5.py",
    "verify_phase6.py", "verify_phase7.py", "verify_phase8.py", "verify_phase9.py",
    "verify_phase10.py", "verify_phase10a.py",
    "verify_phase9a_live.py", "verify_markup_live.py", "verify_text_live.py",
    "verify_branding.py", "verify_desktop.py", "verify_mobile_phase11a.py",
    "verify_final.py", "verify_release.py",
    "generate_branding.py", "sync_version.py", "build_windows.py",
    "build_release.py",
)

#: 安全相关的那几个测试模块。它们**存在**是底线；真的跑没跑由 pytest 那一轮回答，
#: 本文件只负责不让它们被悄悄删掉（§八「防护不能只存在代码里」）。
REQUIRED_SECURITY_TESTS = (
    "test_image_bomb.py",
    "test_svg_security.py",
    "test_markup_security.py",
    "test_pdf_filename_safety.py",
)

#: §十四 点名要的汇总区域，按它给的顺序。本脚本额外多出来的区域排在它们后面。
SPEC_AREAS = (
    "VERSION", "BRANDING", "WEB", "BACKEND", "MOBILE", "DESKTOP", "SECURITY", "REGRESSION",
)

results: list[tuple[str, bool, str]] = []   # (区域, 是否通过, 标签)
not_executed: list[tuple[str, str]] = []    # (区域, 原因)
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


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def run(argv: list[str], cwd: Path, timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv, cwd=str(cwd), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout, shell=(os.name == "nt"),
    )


# ----------------------------------------------------------------------
# VERSION —— 全平台唯一的版本真相源
# ----------------------------------------------------------------------

def section_version() -> None:
    section("VERSION")
    area = "VERSION"

    if not sv.VERSION_FILE.exists():
        check(area, False, f"{sv.VERSION_FILE.name} 存在")
        return
    check(area, True, "VERSION 文件存在")

    try:
        version = sv.read_source_version()
    except SystemExit as exc:  # 不是 MAJOR.MINOR.PATCH
        check(area, False, "VERSION 是 MAJOR.MINOR.PATCH 形式")
        note(str(exc))
        return
    check(area, True, f"VERSION = {version}（MAJOR.MINOR.PATCH 形式）")

    # 每一个消费方都点名断言，而不是「跑一遍 --check 看退出码」——
    # 报告里要能一眼看出**是哪一个**落点漂了。
    consumers: list[tuple[str, str | None]] = []

    for rel, keys in (
        ("frontend/package.json", ("version",)),
        ("mobile/package.json", ("version",)),
        ("mobile/app.json", ("expo", "version")),
    ):
        node: object = read_json(ROOT / rel)
        for key in keys:
            node = node.get(key, {}) if isinstance(node, dict) else {}
        consumers.append((f"{rel} {'/'.join(keys)}", node if isinstance(node, str) else None))

    for rel in ("frontend/package-lock.json", "mobile/package-lock.json"):
        data = read_json(ROOT / rel)
        consumers.append((f"{rel} version", data.get("version")))
        consumers.append((f"{rel} packages[''].version",
                          data.get("packages", {}).get("", {}).get("version")))

    consumers.append(("desktop/src-tauri/Cargo.toml [package].version",
                      sv.cargo_package_version()))

    # 移动端那个 TS 常量：它是**被写进去**的，不是读出来的
    ts_path = ROOT / "mobile" / "src" / "types" / "index.ts"
    ts_match = sv.TS_APP_VERSION.search(ts_path.read_text(encoding="utf-8"))
    consumers.append(("mobile/src/types/index.ts APP_VERSION",
                      ts_match.group(2) if ts_match else None))

    for label, got in consumers:
        check(area, got == version, f"{label} == {version}" + ("" if got == version else f"（实际 {got!r}）"))

    # 后端是**运行期读**的：这里真的把后端解释器拉起来问一遍，
    # 而不是读源码里有没有那行 —— 要证的是「跑起来是对的」。
    backend_py = vb.gb.BACKEND_PYTHON
    if not Path(backend_py).exists():
        skip(area, "后端运行期解析出的版本", f"找不到后端解释器 {backend_py}")
    else:
        proc = run(
            [str(backend_py), "-c", "import config; print(config.settings.APP_VERSION)"],
            cwd=ROOT / "backend", timeout=120,
        )
        got = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else None
        check(area, got == version, f"backend/config.py 运行期解析出 {version}" +
              ("" if got == version else f"（实际 {got!r}；stderr: {proc.stderr.strip()[:200]}）"))


# ----------------------------------------------------------------------
# BRANDING —— 唯一设计源 + 各平台装的是同一份
# ----------------------------------------------------------------------

def section_branding() -> None:
    section("BRANDING")
    area = "BRANDING"

    for label, path in (("App Icon 源", ICON_SVG), ("Brand Logo 源", LOGO_SVG)):
        check(area, path.exists(), f"{label}存在：{path.relative_to(ROOT).as_posix()}")

    if ICON_SVG.exists():
        icon_text = ICON_SVG.read_text(encoding="utf-8")
        try:
            ET.fromstring(icon_text)
            check(area, True, "App Icon 源是合法 XML")
        except ET.ParseError as exc:
            check(area, False, f"App Icon 源是合法 XML（{exc}）")
        # Icon 不许含文字：缩到 16 px 会糊成一团，而 favicon 就是这个尺寸
        check(area, "<text" not in icon_text, "App Icon 源不含任何 <text>（缩到 16px 不能有字）")

    if LOGO_SVG.exists():
        check(area, "FileTools" in LOGO_SVG.read_text(encoding="utf-8"),
              "Brand Logo 源含 FileTools 字标")

    for rel in REQUIRED_BRAND_ASSETS:
        path = BRANDING_GENERATED / rel
        check(area, path.exists(), f"品牌产物存在：branding/generated/{rel}")

    # 各平台装的那一份必须与 generated/ 里那份**逐字节相同** ——
    # 手改产物目录会被这里抓出来。
    for consumer_rel, source_rel in BRAND_CONSUMERS:
        consumer = ROOT / consumer_rel
        source = BRANDING_GENERATED / source_rel
        if not consumer.exists():
            check(area, False, f"{consumer_rel} 存在")
            continue
        if not source.exists():
            check(area, False, f"{consumer_rel} 的来源 branding/generated/{source_rel} 存在")
            continue
        same = vb.sha256(consumer) == vb.sha256(source)
        check(area, same, f"{consumer_rel} 与 branding/generated/{source_rel} 逐字节相同")


# ----------------------------------------------------------------------
# 唯一真源：全仓库不许出现第三套图标 / 禁用产品名
# ----------------------------------------------------------------------

def section_single_source() -> None:
    section("唯一真源（不许有第二套）")
    area = "SINGLE SOURCE"

    sources = {p for p in (ICON_SVG, LOGO_SVG) if p.exists()}
    blobs = {vb.sha256(p) for p in sources}
    rogues: list[str] = []
    copies = 0
    for relative in vb.walk({".svg"}):
        absolute = ROOT / relative
        if absolute in sources:
            continue
        digest = vb.sha256(absolute)
        if digest in blobs:
            copies += 1
        else:
            rogues.append(relative)
    check(area, not rogues, f"全仓库没有第三份内容不同的 SVG（逐字节副本 {copies} 个）")
    for rogue in rogues:
        note(f"游离 SVG：{rogue}")

    hits: list[str] = []
    for relative in vb.walk({".md", ".ts", ".tsx", ".py", ".json", ".html", ".yml", ".toml", ".rs"}):
        text = (ROOT / relative).read_text(encoding="utf-8", errors="replace")
        for match in vb.FORBIDDEN_NAMES.finditer(text):
            hits.append(f"{relative}: {match.group(0)!r}")
    check(area, not hits, "全仓库没有 §十六 那 5 个错误产品名变体")
    for hit in hits[:10]:
        note(f"命中禁用产品名：{hit}")


# ----------------------------------------------------------------------
# WEB
# ----------------------------------------------------------------------

def section_web() -> None:
    section("WEB")
    area = "WEB"

    index = FRONTEND_DIST / "index.html"
    if not index.exists():
        skip(area, "Web 前端产物", "frontend/dist/ 不存在，先跑 `cd frontend && npm run build`")
        return
    check(area, True, "frontend/dist/index.html 存在")

    html = index.read_text(encoding="utf-8")
    refs = re.findall(r'(?:src|href)="([^"]+)"', html)
    local = [r for r in refs if not r.startswith(("http://", "https://", "//", "data:"))]
    check(area, bool(local), f"index.html 里有 {len(local)} 条本地引用")

    missing = [r for r in local if not (FRONTEND_DIST / r.lstrip("./")).exists()]
    check(area, not missing, "index.html 引用的本地文件都在盘上")
    for item in missing:
        note(f"缺文件：{item}")

    check(area, (FRONTEND_DIST / "manifest.webmanifest").exists(),
          "PWA manifest 在产物里（frontend/public/ 会被原样拷进去）")


# ----------------------------------------------------------------------
# BACKEND
# ----------------------------------------------------------------------

def section_backend() -> None:
    section("BACKEND")
    area = "BACKEND"

    for name in ("requirements.txt", "requirements-dev.txt",
                 "requirements-heic.txt", "requirements-ocr.txt"):
        check(area, (ROOT / "backend" / name).exists(), f"backend/{name} 存在")

    # 只防「测试被整批删掉」，不钉具体数字 —— 钉死了每加一个模块都要来改这里，
    # 那是在制造第二份真相。今天实测 46 个。
    module_count = len(list((ROOT / "backend" / "tests").glob("test_*.py")))
    check(area, module_count >= 40, f"后端测试模块数 = {module_count}（>= 40）")

    # 主模块真的能被导入 —— 「文件都在」不等于「服务起得来」
    backend_py = vb.gb.BACKEND_PYTHON
    if not Path(backend_py).exists():
        skip(area, "主模块可导入", f"找不到后端解释器 {backend_py}")
        return
    proc = run([str(backend_py), "-c", "import main; print('ok', len(main.app.routes))"],
               cwd=ROOT / "backend", timeout=180)
    if proc.returncode != 0:
        check(area, False, "后端主模块可导入")
        note(f"import main 失败：{proc.stderr.strip()[-300:]}")
    else:
        check(area, True, f"后端主模块可导入（{proc.stdout.strip().splitlines()[-1]} 条路由）")


# ----------------------------------------------------------------------
# SECURITY
# ----------------------------------------------------------------------

#: 直接问后端自己要那几个白名单，而不是去 grep 源码文本。
#: 「源码里有 `script` 这个词」和「运行时的禁用集合里真的有 script」不是一回事，
#: 前者连注释里提一句都能满足。
_SECURITY_PROBE = (
    "import json, config;"
    "from compressors import svg;"
    "from office import markup_parse as mp;"
    "print(json.dumps({"
    "'upload': config.settings.MAX_UPLOAD_BYTES,"
    "'forbidden_tags': sorted(svg._FORBIDDEN_TAGS),"
    "'allowed_mimes': list(svg._ALLOWED_DATA_MIME),"
    "'link_schemes': list(mp._SAFE_LINK_SCHEMES),"
    "}, ensure_ascii=False))"
)


def find_archive_extraction() -> list[str]:
    """扫第一方后端代码，找出**解包压缩包**的调用。

    用 AST 而不是 ``grep ".extract("``：``pdfplumber`` 的
    ``Table.extract()`` 长得一模一样，只是它返回的是二维字符串表，
    和压缩包毫无关系 —— 纯文本匹配会把 ``backend/pdf/analyzer.py``
    冤枉进来（本文件第一版就是这么写的，跑出来才发现）。

    所以判据是**接收者**：
    ``x.extractall()`` 无条件命中（这份代码里没有任何正当用法）；
    ``x.extract()`` 只在 ``x`` 看起来是压缩包对象时命中。
    """
    archive_re = re.compile(r"zip|zf|tar|archive", re.IGNORECASE)
    #: 造出压缩包对象的调用 —— 用来认出「变量名叫 z2 但确实是 zipfile」的情况
    opener_re = re.compile(r"ZipFile\(|TarFile\(|tarfile\.open\(|zipfile\.", re.IGNORECASE)
    offenders: list[str] = []
    for path in (ROOT / "backend").rglob("*.py"):
        relative = path.relative_to(ROOT)
        if any(part in {".venv", "__pycache__", "tests"} for part in relative.parts):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue

        # 先把「绑定到压缩包对象的变量名」收出来，否则 z2 = ZipFile(...)
        # 之后再调 z2.extract(...) 会因为名字里没有 zip 而漏掉
        archive_names: set[str] = set()
        for node in ast.walk(tree):
            targets: list[ast.expr] = []
            value: ast.expr | None = None
            if isinstance(node, ast.Assign):
                targets, value = list(node.targets), node.value
            elif isinstance(node, (ast.AnnAssign, ast.AugAssign)) and node.value is not None:
                targets, value = [node.target], node.value
            elif isinstance(node, ast.withitem):
                value = node.context_expr
                if node.optional_vars is not None:
                    targets = [node.optional_vars]
            if value is not None and isinstance(value, ast.Call) \
                    and opener_re.search(ast.unparse(value)):
                for target in targets:
                    if isinstance(target, ast.Name):
                        archive_names.add(target.id)

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute):
                if func.attr == "extractall":
                    offenders.append(f"{relative.as_posix()}:{node.lineno} extractall")
                elif func.attr == "unpack_archive":
                    offenders.append(f"{relative.as_posix()}:{node.lineno} unpack_archive")
                elif func.attr == "extract":
                    receiver = ast.unparse(func.value)
                    root = receiver.split(".")[0]
                    if archive_re.search(receiver) or root in archive_names:
                        offenders.append(f"{relative.as_posix()}:{node.lineno} extract")
            elif isinstance(func, ast.Name) and func.id == "unpack_archive":
                offenders.append(f"{relative.as_posix()}:{node.lineno} unpack_archive")
    return offenders


def section_security() -> None:
    """§八：防线的**存在**在这里证，防线的**行为**由 pytest 那几个模块证。

    两件事都要有。只跑 pytest 而不管这些模块还在不在，一次误删就能让
    「安全审计通过」变成一句没有依据的话；只看文件在不在，又是在拿
    「配置存在」冒充「真的挡得住」。所以下面每个断言都落在**活的对象**上：
    问的是后端运行时的真实白名单，不是源码里出现过某个词。
    """
    section("SECURITY")
    area = "SECURITY"

    for name in REQUIRED_SECURITY_TESTS:
        path = ROOT / "backend" / "tests" / name
        if not path.exists():
            check(area, False, f"安全测试模块在：tests/{name}")
            continue
        # 空文件也算「在」，所以顺带数一下里面真的有几个测试函数
        cases = len(re.findall(r"^def test_", path.read_text(encoding="utf-8"), re.MULTILINE))
        check(area, cases > 0, f"tests/{name} 有 {cases} 条测试")

    # Zip Slip 在这一层是**结构上不可能**，不是「校验过了」：
    # 第一方代码里根本没有解包动作 —— 每个 ZipFile 要么只读指定条目，
    # 要么是 mode="w" 在写包。哪天有人加了一句 extractall，这里会红。
    offenders = find_archive_extraction()
    check(area, not offenders,
          "第一方代码里没有解包动作（Zip Slip 结构上不可能）"
          + (f"（命中 {offenders}）" if offenders else ""))

    backend_py = vb.gb.BACKEND_PYTHON
    if not Path(backend_py).exists():
        skip(area, "运行时安全白名单", f"找不到后端解释器 {backend_py}")
        return
    proc = run([str(backend_py), "-c", _SECURITY_PROBE], cwd=ROOT / "backend", timeout=180)
    if proc.returncode != 0:
        check(area, False, "能读出后端运行时的安全白名单")
        note(f"安全探针失败：{proc.stderr.strip()[-300:]}")
        return
    facts = json.loads(proc.stdout.strip().splitlines()[-1])

    check(area, facts["upload"] > 0,
          f"上传上限 > 0（{facts['upload'] / 1024 / 1024:.0f} MB）")
    check(area, "script" in facts["forbidden_tags"],
          f"SVG 净化的连内容一起删的标签里有 script（共 {len(facts['forbidden_tags'])} 个）")
    # 允许 SVG 里再嵌一份 SVG，等于只净化了外层 —— 里层没人查过
    check(area, "image/svg+xml" not in facts["allowed_mimes"],
          f"SVG 里允许内联的 MIME 只有栅格图（{len(facts['allowed_mimes'])} 种，不含 image/svg+xml）")
    check(area, facts["link_schemes"] == ["http://", "https://", "mailto:"],
          f"HTML 链接白名单是穷举的 {facts['link_schemes']}（其余一律只留文字）")


# ----------------------------------------------------------------------
# MOBILE
# ----------------------------------------------------------------------

def section_mobile() -> None:
    section("MOBILE")
    area = "MOBILE"

    app_json = ROOT / "mobile" / "app.json"
    if not app_json.exists():
        check(area, False, "mobile/app.json 存在")
        return
    expo = read_json(app_json).get("expo", {})
    check(area, expo.get("name") == "FileTools", "app.json 的 expo.name == FileTools")

    # 断言的是「app.json 指的每一个资产都真的在盘上」，不是我自己猜的文件名 ——
    # 猜的那一版拿 Expo 模板的 ``adaptive-icon.png`` 去要文件，
    # 而本项目的自适应图标是 background / foreground / monochrome 三张，
    # 于是报了一条**不存在的**缺陷。文件名归 app.json 管，这里只负责它指得准不准。
    referenced: list[tuple[str, str]] = []
    if isinstance(expo.get("icon"), str):
        referenced.append(("expo.icon", expo["icon"]))
    adaptive = expo.get("android", {}).get("adaptiveIcon", {})
    for key, value in adaptive.items():
        # 只认**图片键**：adaptiveIcon 底下还有 backgroundColor 这类颜色值，
        # 拿它去找文件会报一条假缺陷（上一版正这么报的）。
        if isinstance(value, str) and key != "backgroundColor":
            referenced.append((f"android.adaptiveIcon.{key}", value))
    for key in ("web", "android", "ios"):
        if isinstance(expo.get(key, {}).get("favicon"), str):
            referenced.append((f"{key}.favicon", expo[key]["favicon"]))

    check(area, bool(referenced), f"app.json 里点到了 {len(referenced)} 个资产")
    for label, value in referenced:
        target = (ROOT / "mobile" / value.lstrip("./")).resolve()
        check(area, target.exists(), f"app.json 的 {label} = {value} 在盘上")

    if not (ROOT / "mobile" / "node_modules").exists():
        skip(area, "TypeScript 类型检查", "mobile/node_modules 不存在")
        return
    proc = run(["npx", "tsc", "--noEmit"], cwd=ROOT / "mobile", timeout=600)
    check(area, proc.returncode == 0, "npx tsc --noEmit 通过（0 error）")
    if proc.returncode != 0:
        note(f"tsc 输出：{(proc.stdout or proc.stderr).strip()[-400:]}")

    # 原生上传必须用 Expo SDK 57 认的那种 part，且必须关掉 copyToCacheDirectory ——
    # 这两条是第十一阶段 A 抓出来的两个真缺陷，退回去就会「连不上服务器」。
    conversion = (ROOT / "mobile" / "src" / "services" / "api" / "conversion.ts")
    picker = (ROOT / "mobile" / "src" / "services" / "filePicker.ts")
    if conversion.exists():
        text = conversion.read_text(encoding="utf-8")
        check(area, "bytes:" in text and "Promise<Uint8Array>" in text,
              "上传用的是 expo/fetch 认的 { name, type, bytes() } part")

        # 只量 `NativeFilePart` 这个 interface 的**声明本身**：它必须给出 name / type /
        # bytes，且**不许**声明 uri。整文件瞎找 `{uri, name, type}` 会误伤 ——
        # 这个文件的注释里正解释着「RN 那个三件套为什么用不了」，
        # 上一版就是这么报了一条假缺陷的。
        block = re.search(r"interface NativeFilePart \{(.*?)\n\}", text, re.DOTALL)
        if block is None:
            check(area, False, "conversion.ts 里能读到 NativeFilePart 的声明")
        else:
            body = block.group(1)
            has_all = all(re.search(rf"^\s*{f}\??:", body, re.MULTILINE)
                          for f in ("name", "type", "bytes"))
            check(area, has_all and not re.search(r"^\s*uri\??:", body, re.MULTILINE),
                  "NativeFilePart 声明了 name/type/bytes 且**没有** uri 字段")
    if picker.exists():
        check(area, "copyToCacheDirectory: false" in picker.read_text(encoding="utf-8"),
              "DocumentPicker 关掉了 copyToCacheDirectory")


# ----------------------------------------------------------------------
# DESKTOP
# ----------------------------------------------------------------------

def section_desktop() -> None:
    section("DESKTOP")
    area = "DESKTOP"

    if not TAURI_CONF.exists():
        check(area, False, "desktop/src-tauri/tauri.conf.json 存在")
        return
    conf = read_json(TAURI_CONF)
    check(area, conf.get("productName") == "FileTools", "tauri.conf.json 的 productName == FileTools")
    check(area, conf.get("identifier") == "com.filetools.app",
          "identifier == com.filetools.app（与 mobile/app.json 同一身份）")
    check(area, conf.get("app", {}).get("withGlobalTauri") is True,
          "withGlobalTauri 打开（前端零 npm 依赖）")

    mobile_app = read_json(ROOT / "mobile" / "app.json").get("expo", {})
    same_ios = mobile_app.get("ios", {}).get("bundleIdentifier") == conf.get("identifier")
    same_android = mobile_app.get("android", {}).get("package") == conf.get("identifier")
    check(area, same_ios and same_android,
          "桌面 identifier 与移动端 bundleIdentifier / package 逐字相同")

    # 桌面端不含任何转换引擎 —— 这是 §四/§二十二 的硬要求
    rust_dir = ROOT / "desktop" / "src-tauri" / "src"
    rust_sources = sorted(rust_dir.glob("*.rs")) if rust_dir.is_dir() else []
    check(area, bool(rust_sources), f"Rust 源文件在（{len(rust_sources)} 个）")
    joined = "\n".join(p.read_text(encoding="utf-8") for p in rust_sources)
    forbidden_engine = [w for w in ("pymupdf", "PyMuPDF", "docx", "PIL", "soffice", "tesseract")
                        if w in joined]
    check(area, not forbidden_engine,
          "桌面端 Rust 侧没有任何转换引擎" + (f"（命中 {forbidden_engine}）" if forbidden_engine else ""))

    for name in ("icon.ico", "icon-256.png"):
        check(area, (DESKTOP_ICONS / name).exists(), f"desktop/src-tauri/icons/{name} 存在")

    # 构建产物：环境不支持时**不判绿**，如实写 NOT EXECUTED
    if INSTALLER.exists():
        size = INSTALLER.stat().st_size
        check(area, size > 1_000_000, f"安装器在：desktop/artifacts/FileTools-Setup-x64.exe（{size:,} 字节）")
    else:
        reason = "desktop/artifacts/ 下没有安装器，先跑 python scripts/build_windows.py"
        skip(area, "Windows 安装器产物", reason)

    if FRONTEND_DIST_DESKTOP.exists():
        check(area, True, "frontend/dist-desktop/ 存在（Tauri 消费的前端产物）")
    else:
        skip(area, "桌面前端产物", "frontend/dist-desktop/ 不存在，先跑 `npm run build:desktop`")


# ----------------------------------------------------------------------
# DOCKER / 环境受限的平台
# ----------------------------------------------------------------------

def section_environment_limited() -> None:
    section("环境受限的项（NOT EXECUTED 而不是 PASS）")
    area = "ENVIRONMENT"

    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    check(area, "COPY VERSION /app/VERSION" in dockerfile,
          "Dockerfile 里有 COPY VERSION /app/VERSION")

    docker = run(["docker", "--version"], cwd=ROOT, timeout=30)
    if docker.returncode != 0:
        skip(area, "docker build", "本机没有可用的 docker（开发机是 Windows）")
    else:
        # 有 docker 也不在这里自动构建：镜像要 400 MB 的 LibreOffice，
        # 属于发布动作而不是验收动作。如实标出来，不假装跑过。
        skip(area, "docker build", "本机有 docker，但 CI/发布流程里才构建，本轮未执行")

    # iOS：没有 macOS 就一行都跑不了
    if sys.platform == "darwin":
        skip(area, "iOS 构建", "本机是 macOS，但本轮未执行 expo run:ios")
    else:
        skip(area, "iOS 构建", "本机不是 macOS，没有 Xcode —— 只能查配置与资产")

    # Android 真机：AVD 是模拟器，不是「真机」
    skip(area, "Android 真机（独立于模拟器）验收",
         "本轮环境里没有接上物理 Android 设备；AVD 只能证明原生路径跑得通，"
         "不能算真机验收")

    # ARM64
    cargo = run(["cargo", "--version"], cwd=ROOT, timeout=30)
    if cargo.returncode == 0:
        targets = run(["rustup", "target", "list", "--installed"], cwd=ROOT, timeout=60)
        installed = targets.stdout.split()
        if "aarch64-pc-windows-msvc" in installed:
            check(area, True, "ARM64 target 已安装")
        else:
            skip(area, "Windows ARM64 构建",
                 f"本机只装了 {len(installed)} 个 target（{', '.join(installed)}），没有 aarch64")
    else:
        skip(area, "Windows ARM64 构建", "本机没有 cargo")


# ----------------------------------------------------------------------
# 回归入口齐备 + 汇总
# ----------------------------------------------------------------------

def section_regression() -> None:
    section("REGRESSION（验收脚本一个都不许少）")
    area = "REGRESSION"
    for name in REQUIRED_SCRIPTS:
        check(area, (ROOT / "scripts" / name).exists(), f"scripts/{name} 存在")


def area_summary() -> list[tuple[str, bool, int]]:
    """按 §十四 给的顺序排区域；本脚本多出来的区域跟在后面。

    每个区域带回它有几项标了 NOT EXECUTED —— 有的话就在那一行后面写出来，
    免得出现「`[PASS] ENV` 底下紧跟着 4 条没执行的项」这种看着像自相矛盾的输出。
    """
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

    lines = [
        "FILETOOLS FINAL VERIFICATION",
        "=" * 28,
        "",
    ]
    for area, ok, skipped in area_summary():
        suffix = f"（{skipped} 项 NOT EXECUTED）" if skipped else ""
        lines.append(f"[{'PASS' if ok else 'FAIL'}] {area}{suffix}")

    lines += [
        "",
        f"TOTAL: {passed} / {len(results)}",
        f"FAILED: {len(failed)}",
        f"NOT EXECUTED: {len(not_executed)}",
    ]
    if notes:
        lines += ["", "如实记录（不是断言）："]
        lines += [f"  · {n}" for n in notes]
    if not_executed:
        lines += ["", "未执行的项及原因："]
        lines += [f"  - [{a}] {label}" for a, label in not_executed]
    if failed:
        lines += ["", "失败项："]
        lines += [f"  - {f}" for f in failed]
    else:
        lines += ["", "全部通过（未执行项见上，它们**不算通过**）。"]

    REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return passed, len(failed)


def main() -> int:
    print("FILETOOLS FINAL VERIFICATION")
    print("=" * 28)
    print(f"仓库根：{ROOT}")

    section_version()
    section_branding()
    section_single_source()
    section_web()
    section_backend()
    section_security()
    section_mobile()
    section_desktop()
    section_environment_limited()
    section_regression()

    passed, failed_count = write_report()

    # §十四 要的那一块：先逐区域给结论，再给总数。
    # NOT EXECUTED 的区域既不进 PASS 也不进 FAIL —— 它没被执行过，
    # 报成 PASS 就是假绿，报成 FAIL 又是在冤枉代码。
    print("\n" + "=" * 28)
    for area, ok, skipped in area_summary():
        suffix = f"（{skipped} 项 NOT EXECUTED）" if skipped else ""
        print(f"[{'PASS' if ok else 'FAIL'}] {area}{suffix}")

    total = len(results)
    print(f"\nTOTAL: {passed} / {total}")
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
