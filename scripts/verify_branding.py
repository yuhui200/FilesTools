#!/usr/bin/env python3
"""第十一阶段 A 补充的品牌统一验收（§十九 的九项，逐项落成可执行断言）。

这个脚本要证的不是「文件都在」，而是下面这几件**真的成立**的事：

1. **只有一个设计源**（§十/§十四）—— 全仓库扫一遍 SVG，除 ``branding/source/``
   那两个之外，任何一份内容不同的 SVG 都是「某个平台自己又设计了一套」，
   当场判红。内容**逐字节相同**的副本合法（那是安装，不是第二份设计）。
2. **App Icon 与 Brand Logo 没有互用**（§十一/§十二）—— 图标源不许含 ``<text>``
   （缩到 16 px 会糊），锁排源必须含 ``FileTools`` 字标。
3. **每个产物的尺寸与像素真的对**（§十三）—— 不是 ``Path.exists()``，
   而是把每个 PNG/ICO 打开，逐项断言边长、通道数、四角 alpha、纯色背景的色值、
   Android 自适应图标的安全区留白。
4. **各平台的副本与 ``generated/`` 逐字节相同** —— 手改产物目录会被抓出来。
5. **生成是确定性的** —— 现场用同一套渲染器重渲一张 512，与盘上的逐字节比。
6. **App 名与版本号全仓库唯一**（§十六/§十七）—— §十六 列出的那 5 个错误写法
   零命中（清单见 ``FORBIDDEN_NAMES``，那里解释了为什么它得拆开拼）；版本号从
   ``VERSION`` 一路对到后端**运行期解析出来的值**。

跑法（本脚本用**系统 Python** 跑，需要 Playwright；Pillow 那部分自动交给
``backend/.venv`` 的解释器，两边都不新增依赖）::

    python scripts/verify_branding.py

    FILETOOLS_BACKEND_PY   覆盖后端解释器路径
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

ROOT = pathlib.Path(__file__).resolve().parent.parent

# 复用生成脚本的渲染器与安装表 —— 校验和生成必须用**同一套**代码，
# 否则就只是在测「脚本 B 能不能同意脚本 A」。
sys.path.insert(0, str(ROOT / "scripts"))
import generate_branding as gb  # noqa: E402
import sync_version as sv  # noqa: E402

BRANDING = ROOT / "branding"
SOURCE = BRANDING / "source"
GENERATED = BRANDING / "generated"

ICON_SVG = SOURCE / "filetools-icon.svg"
LOGO_SVG = SOURCE / "filetools-logo.svg"
VERSION_FILE = ROOT / "VERSION"
TAURI_CONF = ROOT / "desktop" / "src-tauri" / "tauri.conf.json"
CARGO_TOML = ROOT / "desktop" / "src-tauri" / "Cargo.toml"

PUBLIC = ROOT / "frontend" / "public"
ASSETS = ROOT / "mobile" / "assets"
WIN_ICONS = ROOT / "desktop" / "src-tauri" / "icons"

REPORT = ROOT / "scripts" / "verify_branding_report.txt"

BRAND_HEX = "#4f46e5"
BRAND_RGBA = [0x4F, 0x46, 0xE5, 255]

#: §十六 禁止的 5 个产品名变体：中间带空格的单数形态与复数形态、中间不带空格的
#: 单数形态，以及正确拼写后面接 `App` / `Pro` 的两种。
#:
#: ⚠️ 这里**故意把词拆开拼**，不写成字面量。本脚本要扫全仓库找这几个词，如果它们
#: 以字面量出现在本文件里，扫描就会命中自己 —— 那正是 CI 那次「诊断块被自己的
#: `-rs` 写瞎」的同一类事故：**检查器成了它自己唯一的失败项**。拆开拼之后本文件里
#: 不存在这些字符串，扫描也就不需要给自己开后门（开一个 exclude("verify_branding.py")
#: 就等于承认「规则对写规则的人无效」）。
_WORD = "File" + "Tool"
_SPACED = "File" + " Tool"
FORBIDDEN_NAMES = re.compile(rf"\b{_SPACED}s?\b|\b{_WORD}\b|{_WORD}s App|{_WORD}s Pro")
#: 大小写敏感是故意的：这里查的是**展示名**，而依赖名（`file-tools` 之类）
#: 大小写不同，不该误伤。

#: 遍历仓库时跳过的目录：依赖、构建产物、版本控制
SKIP_DIRS = {
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".expo",
    "dist",
    "dist-desktop",
    "target",
    "gen",
    "artifacts",
    ".accept-work",
}

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ----------------------------------------------------------------------
# 骨架（与 scripts/verify_*.py 一致）
# ----------------------------------------------------------------------

results: list[tuple[bool, str]] = []
notes: list[str] = []
_drift: list[str] = []


def check(ok: bool, label: str) -> None:
    results.append((bool(ok), label))
    print(f"{'PASS' if ok else 'FAIL'}  {label}", flush=True)


def note(text: str) -> None:
    notes.append(text)
    print(f"NOTE  {text}", flush=True)


def section(title: str) -> None:
    print(f"\n=== {title} ===", flush=True)


def sha256(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_json(path: pathlib.Path):
    return json.loads(path.read_text(encoding="utf-8"))


def walk(suffixes: set[str]) -> list[pathlib.Path]:
    """遍历仓库里的文件，跳过依赖与构建产物。返回相对路径排序后的列表。"""
    found: list[pathlib.Path] = []
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(ROOT)
        if any(part in SKIP_DIRS for part in relative.parts):
            continue
        if path.suffix.lower() in suffixes:
            found.append(relative)
    return sorted(found)


# ----------------------------------------------------------------------
# 图像实测（交给 backend/.venv 的 Pillow）
# ----------------------------------------------------------------------

_INSPECT_CODE = r'''
import json, pathlib, sys
from PIL import Image

root = pathlib.Path(sys.argv[1])
report = {}
for raw in json.loads(sys.argv[2]):
    path = root / raw
    entry = {"exists": path.exists()}
    if not entry["exists"]:
        report[raw] = entry
        continue
    entry["bytes"] = path.stat().st_size
    with Image.open(path) as image:
        entry["size"] = list(image.size)
        entry["mode"] = image.mode
        entry["format"] = image.format
        if image.format == "ICO":
            entry["ico_sizes"] = sorted(list(s) for s in image.info.get("sizes", []))
        rgba = image.convert("RGBA")

    width, height = entry["size"]
    pixels = rgba.load()
    entry["corners"] = [
        list(pixels[0, 0]),
        list(pixels[width - 1, 0]),
        list(pixels[0, height - 1]),
        list(pixels[width - 1, height - 1]),
    ]
    entry["center"] = list(pixels[width // 2, height // 2])

    colors = rgba.getcolors(maxcolors=1 << 22)
    entry["color_count"] = len(colors) if colors is not None else None
    if colors is not None:
        entry["top_colors"] = [[list(c), n] for n, c in sorted(colors, reverse=True)[:3]]

    mask = rgba.getchannel("A").point(lambda v: 255 if v > 8 else 0)
    box = mask.getbbox()
    entry["alpha_bbox"] = list(box) if box else None

    # 外圈 5 行的最大 alpha：判断图形有没有顶到画布边缘（自适应图标的安全区）
    alpha = rgba.getchannel("A")
    edge = 0
    for x in range(width):
        for y in list(range(5)) + list(range(height - 5, height)):
            value = alpha.getpixel((x, y))
            if value > edge:
                edge = value
    entry["edge_alpha_max"] = edge
    report[raw] = entry

print(json.dumps(report))
'''

#: (路径, 期望边长, 是否允许有 alpha 通道)
EXPECTED_IMAGES: list[tuple[str, int, bool]] = [
    # Web
    ("frontend/public/favicon-32.png", 32, True),
    ("frontend/public/favicon-48.png", 48, True),
    ("frontend/public/apple-touch-icon.png", 180, False),
    ("frontend/public/icon-192.png", 192, True),
    ("frontend/public/icon-512.png", 512, True),
    # Android
    ("mobile/assets/icon.png", 1024, False),
    ("mobile/assets/favicon.png", 48, True),
    ("mobile/assets/android-icon-background.png", 512, False),
    ("mobile/assets/android-icon-foreground.png", 512, True),
    ("mobile/assets/android-icon-monochrome.png", 432, True),
    ("mobile/assets/mipmap-mdpi/ic_launcher.png", 48, False),
    ("mobile/assets/mipmap-hdpi/ic_launcher.png", 72, False),
    ("mobile/assets/mipmap-xhdpi/ic_launcher.png", 96, False),
    ("mobile/assets/mipmap-xxhdpi/ic_launcher.png", 144, False),
    ("mobile/assets/mipmap-xxxhdpi/ic_launcher.png", 192, False),
    # iOS
    ("branding/generated/ios/icon-1024.png", 1024, False),
    # Windows
    ("desktop/src-tauri/icons/icon-256.png", 256, True),
]

#: (路径, ICO 至少要包含的边长集合)
EXPECTED_ICOS: list[tuple[str, set[int]]] = [
    ("frontend/public/favicon.ico", {16, 24, 32, 48}),
    ("desktop/src-tauri/icons/icon.ico", {16, 24, 32, 48, 64, 128, 256}),
    ("branding/generated/windows/icon.ico", {16, 24, 32, 48, 64, 128, 256}),
]

IMAGES = [path for path, _, _ in EXPECTED_IMAGES] + [path for path, _ in EXPECTED_ICOS]
INSPECTED: dict[str, dict] = {}

#: 允许出现 logo / icon / favicon 字样的目录（其余地方冒出这类文件就是「又做了一套」）
IMAGE_ALLOWED_PREFIXES = (
    "branding/",
    "mobile/assets/",
    "frontend/public/",
    "desktop/src-tauri/icons/",
)


def inspect_images() -> None:
    raw = gb._run_backend_python(_INSPECT_CODE, ROOT, json.dumps(IMAGES))
    INSPECTED.update(json.loads(raw))


def entry(path: str) -> dict:
    return INSPECTED.get(path, {"exists": False})


# ----------------------------------------------------------------------
# §一  两个设计源
# ----------------------------------------------------------------------


def section_sources() -> None:
    section("§一 两个设计源（App Icon 与 Brand Logo）")

    for label, path in (("图标源", ICON_SVG), ("锁排源", LOGO_SVG)):
        check(path.exists(), f"{label}存在：{path.relative_to(ROOT).as_posix()}")

    roots: dict[pathlib.Path, ET.Element] = {}
    for path in (ICON_SVG, LOGO_SVG):
        name = path.relative_to(ROOT).as_posix()
        if not path.exists():
            check(False, f"{name} 是合法 XML")
            continue
        try:
            root = ET.fromstring(path.read_text(encoding="utf-8"))
        except ET.ParseError as exc:
            check(False, f"{name} 是合法 XML（解析失败：{exc}）")
            continue
        roots[path] = root
        check(True, f"{name} 是合法 XML")
        check(
            bool(root.get("viewBox")),
            f"{name} 有 viewBox（={root.get('viewBox')!r}）",
        )

    if ICON_SVG in roots:
        text = ICON_SVG.read_text(encoding="utf-8")
        viewbox = roots[ICON_SVG].get("viewBox", "").split()
        check(
            len(viewbox) == 4 and viewbox[2] == viewbox[3],
            f"图标源的 viewBox 是正方形（={' '.join(viewbox)}）",
        )
        check(BRAND_HEX in text.lower(), f"图标源用了品牌主色 {BRAND_HEX}")
        check("<text" not in text, "图标源不含 <text>（App 图标不许带文字）")

    if LOGO_SVG in roots:
        text = LOGO_SVG.read_text(encoding="utf-8")
        check("FileTools" in text, "锁排源含 FileTools 字标")
        check("<text" in text, "锁排源用 <text> 排字标")
        check(BRAND_HEX in text.lower(), f"锁排源用了品牌主色 {BRAND_HEX}")


# ----------------------------------------------------------------------
# §二  唯一真源
# ----------------------------------------------------------------------


def section_single_source() -> None:
    section("§二 唯一真源：全仓库不许有第三份内容不同的图标")

    svgs = walk({".svg"})
    sources = [p for p in (ICON_SVG, LOGO_SVG) if p.exists()]
    blobs = {sha256(p): p for p in sources}

    duplicates: list[str] = []
    copies = 0
    for relative in svgs:
        absolute = ROOT / relative
        if absolute in sources:
            continue
        if sha256(absolute) in blobs:
            copies += 1
            continue
        duplicates.append(relative.as_posix())

    check(
        not duplicates,
        f"没有第三份内容不同的 SVG（扫到 {len(svgs)} 份，其中 {copies} 份是真源副本）",
    )
    for item in duplicates[:10]:
        note(f"多出来的图标设计：{item}")


# ----------------------------------------------------------------------
# §三  Windows / Android / iOS / Web 的产物
# ----------------------------------------------------------------------


def section_products() -> None:
    section("§三 各平台产物的尺寸、通道与像素")

    for path, size, allow_alpha in EXPECTED_IMAGES:
        info = entry(path)
        if not info.get("exists"):
            check(False, f"{path} 存在")
            continue
        check(
            info["size"] == [size, size],
            f"{path} 是 {size}×{size}（实测 {info['size'][0]}×{info['size'][1]}）",
        )
        has_alpha = "A" in info["mode"]
        if allow_alpha:
            check(
                info["corners"][0][3] == 0,
                f"{path} 四角透明（圆角外不该被填色）",
            )
        else:
            check(
                not has_alpha,
                f"{path} 没有 alpha 通道（mode={info['mode']}）",
            )

    section("§三 b  iOS：图标不许有 alpha（系统会拿黑色填透明处）")
    ios_info = entry("branding/generated/ios/icon-1024.png")
    check(
        ios_info.get("exists") and "A" not in ios_info.get("mode", "A"),
        f"iOS 1024 图标无 alpha（mode={ios_info.get('mode')}）",
    )

    section("§三 c  Android 自适应图标：背景纯色 + 前景留出安全区")
    background = entry("mobile/assets/android-icon-background.png")
    if background.get("exists"):
        check(
            background["color_count"] == 1,
            f"自适应背景是纯色（实测 {background['color_count']} 种颜色）",
        )
        check(
            background["center"] == BRAND_RGBA,
            f"自适应背景是 {BRAND_HEX}（实测 {background['center']}）",
        )

    foreground = entry("mobile/assets/android-icon-foreground.png")
    if foreground.get("exists"):
        check(
            foreground["edge_alpha_max"] == 0,
            f"前景外圈全透明（实测最大 alpha={foreground['edge_alpha_max']}）",
        )
        box = foreground["alpha_bbox"]
        if box:
            width = box[2] - box[0]
            height = box[3] - box[1]
            ratio = width / foreground["size"][0]
            check(
                abs(width - height) <= 2,
                f"前景图形是正方形（实测 {width}×{height}）",
            )
            # 自适应图标是 108dp 画布、内圈 72dp 安全区，图形只该占约 66%
            check(
                0.60 <= ratio <= 0.72,
                f"前景图形占画布 {ratio:.1%}，落在安全区 60%–72% 内",
            )

    section("§三 d  ICO 内含的尺寸档位")
    for path, required in EXPECTED_ICOS:
        info = entry(path)
        if not info.get("exists"):
            check(False, f"{path} 存在")
            continue
        sizes = {w for w, _ in map(tuple, info.get("ico_sizes", []))}
        missing = sorted(required - sizes)
        check(
            not missing,
            f"{path} 含 {sorted(required)} 全部档位"
            + (f"，缺 {missing}" if missing else f"（实测 {sorted(sizes)}）"),
        )


# ----------------------------------------------------------------------
# §四  各平台副本与 generated/ 逐字节相同
# ----------------------------------------------------------------------


def section_installed() -> None:
    section("§四 各消费方的文件与 branding/generated/ 逐字节相同")

    for platform, destination in gb.INSTALL_MAP:
        origin = GENERATED / platform
        if not origin.is_dir():
            check(False, f"branding/generated/{platform}/ 存在")
            continue
        produced = sorted(p for p in origin.rglob("*") if p.is_file())
        mismatched: list[str] = []
        for item in produced:
            target = destination / item.relative_to(origin)
            if not target.exists() or sha256(target) != sha256(item):
                mismatched.append(target.relative_to(ROOT).as_posix())
        check(
            not mismatched,
            f"branding/generated/{platform}/ 的 {len(produced)} 个文件都已原样安装到 "
            f"{destination.relative_to(ROOT).as_posix()}/",
        )
        for item in mismatched[:10]:
            note(f"与生成结果不一致：{item}")


# ----------------------------------------------------------------------
# §五  Web 消费方
# ----------------------------------------------------------------------


def section_web() -> None:
    section("§五 Web：index.html 与 PWA manifest")

    index = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    for label, pattern in (
        ("favicon.svg", r'<link[^>]+rel="icon"[^>]+href="/favicon\.svg"'),
        ("apple-touch-icon", r'<link[^>]+rel="apple-touch-icon"[^>]+href="/apple-touch-icon\.png"'),
        ("manifest", r'<link[^>]+rel="manifest"[^>]+href="/manifest\.webmanifest"'),
    ):
        check(
            re.search(pattern, index) is not None,
            f"index.html 声明了 {label}",
        )

    manifest_path = PUBLIC / "manifest.webmanifest"
    if not manifest_path.exists():
        check(False, "frontend/public/manifest.webmanifest 存在")
        return
    manifest = read_json(manifest_path)
    check(manifest.get("name") == "FileTools", f"manifest name = {manifest.get('name')!r}")
    check(
        manifest.get("short_name") == "FileTools",
        f"manifest short_name = {manifest.get('short_name')!r}",
    )
    check(
        manifest.get("theme_color", "").lower() == BRAND_HEX,
        f"manifest theme_color = {manifest.get('theme_color')!r}",
    )
    missing = [
        icon["src"]
        for icon in manifest.get("icons", [])
        if not (PUBLIC / pathlib.PurePosixPath(icon["src"]).name).exists()
    ]
    check(
        not missing,
        f"manifest 声明的 {len(manifest.get('icons', []))} 个图标都在盘上"
        + (f"，缺 {missing}" if missing else ""),
    )

    check(
        (ROOT / "frontend" / "public" / "favicon.svg").exists(),
        "frontend/public/favicon.svg 存在（由 branding 安装回来）",
    )


# ----------------------------------------------------------------------
# §六  移动端配置
# ----------------------------------------------------------------------


def section_mobile() -> None:
    section("§六 移动端 app.json 指向的图标都存在且配色正确")

    app = read_json(ROOT / "mobile" / "app.json")["expo"]

    icon = app.get("icon")
    check(bool(icon), f"expo.icon 已配置（{icon!r}）")
    if icon:
        check(
            (ROOT / "mobile" / icon).exists(),
            f"expo.icon 指向的文件存在（{icon}）",
        )

    adaptive = app.get("android", {}).get("adaptiveIcon", {})
    for key in ("foregroundImage", "backgroundImage", "monochromeImage"):
        value = adaptive.get(key)
        if value:
            check(
                (ROOT / "mobile" / value).exists(),
                f"adaptiveIcon.{key} 指向的文件存在（{value}）",
            )
        else:
            check(False, f"adaptiveIcon.{key} 已配置")
    check(
        adaptive.get("backgroundColor", "").lower() == BRAND_HEX,
        f"adaptiveIcon.backgroundColor = {adaptive.get('backgroundColor')!r}"
        f"（模板默认曾是 #E6F4FE 浅蓝，与 Web 的 indigo 不是一个色系）",
    )

    favicon = app.get("web", {}).get("favicon")
    check(
        bool(favicon) and (ROOT / "mobile" / favicon).exists(),
        f"expo.web.favicon 指向的文件存在（{favicon!r}）",
    )

    ios = app.get("ios", {})
    check(
        ios.get("bundleIdentifier") == "com.filetools.app",
        f"ios.bundleIdentifier = {ios.get('bundleIdentifier')!r}",
    )
    check(ios.get("supportsTablet") is True, "ios.supportsTablet 为 true（iPad 复用同一张 1024）")


# ----------------------------------------------------------------------
# §七  App 名
# ----------------------------------------------------------------------


def section_app_name() -> None:
    section("§七 App 名统一为 FileTools（§十六 的五个变体零命中）")

    app_name = read_json(ROOT / "mobile" / "app.json")["expo"].get("name")
    check(app_name == "FileTools", f"app.json expo.name = {app_name!r}")

    title = re.search(r"<title>(.*?)</title>", (ROOT / "frontend" / "index.html").read_text("utf-8"))
    check(
        title is not None and title.group(1).startswith("FileTools"),
        f"index.html 的 <title> 以 FileTools 开头（{title.group(1) if title else None!r}）",
    )

    manifest_name = read_json(PUBLIC / "manifest.webmanifest").get("name")
    check(manifest_name == "FileTools", f"PWA manifest name = {manifest_name!r}")

    haystack_extensions = {".ts", ".tsx", ".js", ".jsx", ".json", ".md", ".py", ".html", ".css", ".rs", ".toml"}
    hits: list[str] = []
    for relative in walk(haystack_extensions):
        try:
            text = (ROOT / relative).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for number, line in enumerate(text.splitlines(), 1):
            if FORBIDDEN_NAMES.search(line):
                hits.append(f"{relative.as_posix()}:{number}: {line.strip()[:80]}")
    check(
        not hits,
        f"全仓库没有 §十六 禁止的那 5 个产品名变体（{len(hits)} 处）",
    )
    for item in hits[:10]:
        note(item)

    # Windows 端的 App 名由 Tauri 配置决定，属第 5 步的产物
    if TAURI_CONF.exists():
        conf = read_json(TAURI_CONF)
        check(
            conf.get("productName") == "FileTools",
            f"tauri.conf.json productName = {conf.get('productName')!r}",
        )
        check(
            conf.get("identifier") == "com.filetools.app",
            f"tauri.conf.json identifier = {conf.get('identifier')!r}（与移动端两处一致）",
        )
    else:
        note(
            "desktop/src-tauri/tauri.conf.json 还不存在（第 5 步才创建），"
            "本次未断言 Windows 端的 productName / identifier；它们由 src 与 "
            "scripts/verify_desktop.py 负责。"
        )


# ----------------------------------------------------------------------
# §八  版本唯一真相源
# ----------------------------------------------------------------------


def section_version() -> None:
    section("§八 版本唯一真相源")

    version = VERSION_FILE.read_text(encoding="utf-8").strip()
    check(
        bool(re.fullmatch(r"\d+\.\d+\.\d+", version)),
        f"VERSION = {version!r} 是 MAJOR.MINOR.PATCH 形式",
    )

    def want(actual, label: str) -> None:
        check(actual == version, f"{label} = {actual!r}")

    want(read_json(ROOT / "frontend" / "package.json").get("version"), "frontend/package.json version")
    want(read_json(ROOT / "mobile" / "package.json").get("version"), "mobile/package.json version")
    want(read_json(ROOT / "mobile" / "app.json")["expo"].get("version"), "mobile/app.json expo.version")

    for name in ("frontend", "mobile"):
        lock = read_json(ROOT / name / "package-lock.json")
        want(lock.get("version"), f"{name}/package-lock.json version")
        want(
            lock.get("packages", {}).get("", {}).get("version"),
            f"{name}/package-lock.json packages[''].version",
        )
        # 两份 lockfile 的 name 必须跟各自的 package.json 对齐 ——
        # mobile 那份曾经是陈旧的 "mobile"，与 package.json 的 "filetools-mobile"
        # 对不上，那是手动改过 package.json 却没重新生成 lock 留下的。
        package_name = read_json(ROOT / name / "package.json").get("name")
        check(
            lock.get("name") == package_name,
            f"{name}/package-lock.json name = {lock.get('name')!r}，与 package.json 的 {package_name!r} 一致",
        )
        check(
            lock.get("packages", {}).get("", {}).get("name") == package_name,
            f"{name}/package-lock.json packages[''].name 也一致",
        )

    types_path = ROOT / "mobile" / "src" / "types" / "index.ts"
    found = re.search(
        r"^export const APP_VERSION = '([^']*)'", types_path.read_text(encoding="utf-8"), re.MULTILINE
    )
    check(found is not None, "mobile/src/types/index.ts 里有 APP_VERSION")
    if found:
        want(found.group(1), "mobile/src/types/index.ts APP_VERSION")

    # 后端**运行期**解析出来的值 —— 不是读文件，是真的 import 一次拿 settings
    try:
        runtime = gb._run_backend_python("from config import settings; print(settings.APP_VERSION)").strip()
    except RuntimeError as exc:
        check(False, f"backend/config.py 能 import 并解析出 APP_VERSION（{exc}）")
    else:
        want(runtime, "backend/config.py 运行期 settings.APP_VERSION")

    if TAURI_CONF.exists():
        conf = read_json(TAURI_CONF)
        declared = conf.get("version")
        if isinstance(declared, str) and declared.endswith(".json"):
            resolved = read_json((TAURI_CONF.parent / declared).resolve()).get("version")
            want(resolved, f"tauri.conf.json 的 version 指向 {declared}，解析出")
        else:
            want(declared, "tauri.conf.json version")

    # Cargo 的 ``[package].version`` 是**纯形式**的一份（决定 FileTools.exe 版本的
    # 是上面那个 tauri.conf.json），但「存在且没用」正是最容易漂移的副本。
    # 复用 sync_version.py 的解析器，两边对「哪个 version 才算数」的判断保证一致。
    if CARGO_TOML.exists():
        want(sv.cargo_package_version(), "desktop/src-tauri/Cargo.toml [package].version")

    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    check(
        bool(re.search(r"^COPY\s+VERSION\s+/app/VERSION\s*$", dockerfile, re.MULTILINE)),
        "Dockerfile 里有 COPY VERSION /app/VERSION"
        "（镜像只 COPY backend/，不加这行容器会报不出正确版本）",
    )


# ----------------------------------------------------------------------
# §九  没有重复的非官方图标
# ----------------------------------------------------------------------


def git_ignored(paths: list[str]) -> set[str]:
    """从 ``paths`` 里挑出被 ``.gitignore`` 排除的那些（用 git 自己的判断）。

    §九 问的是「**仓库里**有没有游离的品牌资产」，而 ``walk()`` 走的是文件系统：
    它靠一份**手维护的** ``SKIP_DIRS`` 排除依赖与构建产物，那份清单会和现实漂 ——
    ``mobile/android/`` 是 ``expo prebuild`` 现场生成的脚手架，
    ``mobile/.gitignore:41`` 把整个目录排除了、``git ls-files`` 一个都不跟踪，
    它**不在仓库里**。把它的 ``splashscreen_logo.png``（Expo 从品牌图标生成的副本，
    不是第二份真源）算成「游离资产」是假红。

    所以这里不自己再维护一份忽略清单，直接问 git —— 它才是「什么在仓库里」的真相源。

    两个坑都在这一行里，写下来免得下次又踩：

    * **不能用 ``text=True``。** 它在 Windows 上会把写进 stdin 的 ``\\n``
      翻译成 ``\\r\\n``，于是 git 收到的路径**末尾带 CR**，回显时按「含特殊字符的路径」
      加上引号 —— 拿回来的字符串就跟候选路径对不上了。症状很有辨识度：
      ``n`` 个路径里前 ``n-1`` 个脏、最后一个干净（``join`` 没给它尾随换行）。
    * **用 ``-z``。** 它让输出以 NUL 分隔、且**不加引号**，本来就该拿它做机器可读的输出。
      走字节、不走文本模式，翻译层就整个绕开了。
    """
    if not paths:
        return set()
    try:
        done = subprocess.run(
            ["git", "check-ignore", "--stdin", "-z"],
            cwd=str(ROOT),
            input=b"\0".join(p.encode("utf-8") for p in paths) + b"\0",
            capture_output=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return set()
    return {chunk.decode("utf-8", "replace") for chunk in done.stdout.split(b"\0") if chunk}


def section_no_rogue_icons() -> None:
    section("§九 没有游离在品牌体系之外的图标/Logo")

    pattern = re.compile(r"(logo|icon|favicon)", re.IGNORECASE)
    candidates: list[str] = []
    for relative in walk({".png", ".ico", ".svg"}):
        posix = relative.as_posix()
        if not pattern.search(relative.name):
            continue
        if posix.startswith(IMAGE_ALLOWED_PREFIXES):
            continue
        candidates.append(posix)

    ignored = git_ignored(candidates)
    rogue = [item for item in candidates if item not in ignored]

    check(
        not rogue,
        f"logo / icon / favicon 命名的图片只出现在品牌目录里（{len(rogue)} 个游离文件）",
    )
    for item in rogue[:10]:
        note(f"游离的图标文件：{item}")
    for item in sorted(ignored)[:10]:
        note(f"跳过的生成物（被 .gitignore 排除，不在仓库里）：{item}")

    manifest = GENERATED / "manifest.sha256"
    if not manifest.exists():
        check(False, "branding/generated/manifest.sha256 存在")
        return
    bad: list[str] = []
    total = 0
    for line in manifest.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        digest, _, raw = line.partition("  ")
        total += 1
        target = ROOT / raw.strip()
        if not target.exists() or sha256(target) != digest:
            bad.append(raw.strip())
    check(
        not bad,
        f"manifest.sha256 里 {total} 条哈希全部与盘上一致" + (f"，{len(bad)} 条对不上" if bad else ""),
    )


# ----------------------------------------------------------------------
# §十  生成是确定性的
# ----------------------------------------------------------------------


def section_determinism() -> None:
    section("§十 生成是确定性的（现场重渲一次与盘上比）")

    on_disk = GENERATED / "web" / "icon-512.png"
    if not on_disk.exists():
        check(False, "branding/generated/web/icon-512.png 存在")
        return

    # 比字节必须在 with 里面：TemporaryDirectory 一退出，刚渲出来的那张就没了。
    with tempfile.TemporaryDirectory(prefix="filetools-verify-brand-") as tmp:
        fresh = gb.render_pngs([("fresh-512.png", 512, "plain")], pathlib.Path(tmp))
        produced = fresh["fresh-512.png"]
        check(
            sha256(produced) == sha256(on_disk),
            "现场重渲的 512 图标与 branding/generated/web/icon-512.png 逐字节相同",
        )


# ----------------------------------------------------------------------
# 汇总
# ----------------------------------------------------------------------


def write_report() -> tuple[int, int]:
    passed = sum(1 for ok, _ in results if ok)
    failed = [label for ok, label in results if not ok]
    lines = [
        "第十一阶段 A 补充（四平台品牌统一）验收报告",
        f"真源：{ICON_SVG.relative_to(ROOT).as_posix()}",
        f"通过：{passed} / {len(results)}",
        "",
    ]
    if notes:
        lines.append("如实记录（不是断言）：")
        lines.extend(f"  · {item}" for item in notes)
        lines.append("")
    if failed:
        lines.append("失败项：")
        lines.extend(f"  - {label}" for label in failed)
    else:
        lines.append("全部通过。")
    REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return passed, len(results)


def summary() -> int:
    passed, total = write_report()
    print(f"\n=== 汇总：{passed}/{total} 通过 ===", flush=True)
    print(f"报告写入 {REPORT.relative_to(ROOT).as_posix()}", flush=True)
    failed = [label for ok, label in results if not ok]
    if failed:
        print("\n失败项：", flush=True)
        for label in failed:
            print(f"  - {label}", flush=True)
        return 1
    return 0


def main() -> int:
    print(f"仓库根：{ROOT}")
    print(f"后端解释器（Pillow）：{gb.BACKEND_PYTHON}")

    inspect_images()
    missing = [path for path in IMAGES if not entry(path).get("exists")]
    if missing:
        print(f"\n有 {len(missing)} 个待检文件不存在，先跑 python scripts/generate_branding.py")

    section_sources()
    section_single_source()
    section_products()
    section_installed()
    section_web()
    section_mobile()
    section_app_name()
    section_version()
    section_no_rogue_icons()
    section_determinism()

    return summary()


if __name__ == "__main__":
    sys.exit(main())
