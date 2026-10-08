#!/usr/bin/env python3
"""从唯一真源生成四平台全部图标，并安装到各自的消费方。

## 为什么是两段式

本项目**没有**任何 SVG 光栅化工具（无 ImageMagick / Inkscape / rsvg / cairosvg），
而两个 Python 各有各的缺口：

* 系统 Python —— 有 **Playwright + Chromium**（能把矢量渲成任意边长的透明 PNG），
  但**没有 Pillow**；
* ``backend/.venv`` —— 有 **Pillow 12.3.0**（能写多尺寸 ICO），但**没有 Playwright**。

所以本脚本跑在**系统 Python** 下，把「多尺寸 ICO 编码」这一小段交给
``backend/.venv`` 的解释器去跑（沿用仓库 ``scripts/verify_phase10.py`` 里既有的
``find_backend_python()`` 探测约定）。**两边都不新增任何依赖。**

## 唯一真源

``branding/source/filetools-icon.svg`` —— 全仓库只有这一个图标设计源。
本脚本产出的每一个文件都是它的**派生物**；改图标 = 改那个文件 + 重跑本脚本。
``scripts/verify_branding.py`` 会扫全仓库，出现第三份内容不同的 SVG 即判红。

## 五种渲染方式（对应不同平台的硬约束）

===================  ==========================================================
``plain``            原样渲染，圆角外是透明 —— favicon / PWA 图标
``flat``             图标合成到不透明 ``#4f46e5`` 上，圆角外也填满 ——
                     **iOS 图标不许有 alpha**，不合成的话系统会填黑
``padded``           图形缩到画布 ~66% 居中贴透明底 —— Android 自适应图标的
                     安全区（108dp 里只有内圈 72dp 保证可见，边缘会被裁）
``mono``             只留形状、统一刷白、去掉底色 —— Android 主题图标
``solid``            纯 ``#4f46e5`` 满幅 —— Android 自适应图标的背景层
===================  ==========================================================

## 用法

    python scripts/generate_branding.py            # 生成 + 安装到消费方
    python scripts/generate_branding.py --check    # 只渲染到临时目录比对，不写盘
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import pathlib
import re
import shutil
import struct
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent

BRANDING = ROOT / "branding"
SOURCE = BRANDING / "source"
GENERATED = BRANDING / "generated"

ICON_SVG = SOURCE / "filetools-icon.svg"
LOGO_SVG = SOURCE / "filetools-logo.svg"

BACKEND = ROOT / "backend"

#: 品牌主色。与 ``frontend/index.html`` 的 ``theme-color``、
#: ``tailwind.config`` 的 ``brand-600``、``mobile/app.json`` 的
#: ``adaptiveIcon.backgroundColor`` 同一个值 —— 改这里就要一起改那三处，
#: ``verify_branding.py`` 会断言它们相等。
BRAND_HEX = "#4f46e5"

#: 图标 ``viewBox`` 的边长
VIEWBOX = 32

#: Android 自适应图标里，前景图形占画布的比例。
#: 自适应图标是 108dp 画布、内圈 72dp 安全区，72/108 ≈ 0.667。
#: 取 0.664 留一点余量，保证图形四角在任何裁切形状下都不被切掉。
FOREGROUND_RATIO = 0.664

#: 两套 ICO 各要包含的尺寸（§十三 要求 Windows 至少 16/24/32/48/64/128/256）
WEB_ICO_SIZES = (16, 24, 32, 48)
WINDOWS_ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)

# ---------------------------------------------------------------------------
# 输出清单： (相对 generated/ 的路径, 边长像素, 渲染方式)
# ---------------------------------------------------------------------------

OUTPUTS: list[tuple[str, int, str]] = [
    # ---- Web：浏览器标签页 / 书签 / PWA / iOS 加到主屏 ----
    ("web/favicon-32.png", 32, "plain"),
    ("web/favicon-48.png", 48, "plain"),
    ("web/apple-touch-icon.png", 180, "flat"),  # iOS 不许透明
    ("web/icon-192.png", 192, "plain"),
    ("web/icon-512.png", 512, "plain"),
    # ---- Android（Expo 的 app.json 直接指向这几个文件名）----
    ("android/icon.png", 1024, "flat"),  # expo.icon：兼作 iOS 图标，故不许有 alpha
    ("android/favicon.png", 48, "plain"),  # expo.web.favicon
    ("android/android-icon-background.png", 512, "solid"),
    ("android/android-icon-foreground.png", 512, "padded"),
    ("android/android-icon-monochrome.png", 432, "mono"),
    # 传统 launcher 图标五档（§十三 要求 mdpi/hdpi/xhdpi/xxhdpi/xxxhdpi）
    ("android/mipmap-mdpi/ic_launcher.png", 48, "flat"),
    ("android/mipmap-hdpi/ic_launcher.png", 72, "flat"),
    ("android/mipmap-xhdpi/ic_launcher.png", 96, "flat"),
    ("android/mipmap-xxhdpi/ic_launcher.png", 144, "flat"),
    ("android/mipmap-xxxhdpi/ic_launcher.png", 192, "flat"),
    # ---- iOS ----
    ("ios/icon-1024.png", 1024, "flat"),
    # ---- Windows（Tauri 的 bundle.icon 指向它们）----
    ("windows/icon-256.png", 256, "plain"),
]

#: 同一份 PNG 还要以另一套文件名进 ICO 的容器
ICO_TARGETS: list[tuple[str, tuple[int, ...]]] = [
    ("web/favicon.ico", WEB_ICO_SIZES),
    ("windows/icon.ico", WINDOWS_ICO_SIZES),
]

#: generated/<平台>/ 里的东西装到哪去
INSTALL_MAP: list[tuple[str, pathlib.Path]] = [
    ("web", ROOT / "frontend" / "public"),
    ("android", ROOT / "mobile" / "assets"),
    ("windows", ROOT / "desktop" / "src-tauri" / "icons"),
]

#: 两份 SVG 真源也各抄一份进 web/。它们**不是派生物**，是源文件本身的逐字节
#: 副本 —— 浏览器要按 URL 取文件，而 frontend/public/ 才是 Vite 的发布目录。
WEB_SVG_COPY = "web/favicon.svg"
WEB_LOGO_COPY = "web/filetools-logo.svg"

PWA_DESCRIPTION = (
    "FileTools —— 简单、快速的在线文件工具：图片压缩、格式转换、尺寸调整与 PDF 处理。"
    "文件处理完自动删除，不上传留存。"
)

# ---------------------------------------------------------------------------
# 后端的解释器（约定抄自 scripts/verify_phase10.py）
# ---------------------------------------------------------------------------


def find_backend_python() -> pathlib.Path:
    override = os.environ.get("FILETOOLS_BACKEND_PY")
    if override:
        return pathlib.Path(override)
    for candidate in (
        BACKEND / ".venv" / "Scripts" / "python.exe",
        BACKEND / ".venv" / "bin" / "python",
    ):
        if candidate.exists():
            return candidate
    return pathlib.Path(sys.executable)


BACKEND_PYTHON = find_backend_python()


def _run_backend_python(code: str, *args: object, cwd: pathlib.Path | None = None) -> str:
    """用后端的解释器跑一段代码，返回它的 stdout。

    ``errors="replace"`` 与 ``PYTHONIOENCODING=utf-8`` 都是必须的：子进程的
    stdout 是管道，这时 Python 按 Windows 本地代码页（GBK）写字节，父进程按
    utf-8 读，只要子进程打印一个汉字就在**读取线程**里 UnicodeDecodeError。
    """
    out = subprocess.run(
        [str(BACKEND_PYTHON), "-c", code, *[str(a) for a in args]],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(cwd or BACKEND),
        env=dict(os.environ, PYTHONIOENCODING="utf-8"),
    )
    if out.returncode != 0:
        raise RuntimeError(out.stderr or out.stdout)
    if out.stdout is None:
        raise RuntimeError(f"子进程没有可读的 stdout。stderr：{out.stderr!r}")
    return out.stdout


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------


def _icon_data_uri() -> str:
    """把图标源内联成 data URI。

    用 data URI 而不是 ``file://`` 有两个实在的好处：``set_content`` 出来的页面
    不是 file 源，去加载 ``file://`` 图片会被 Chromium 拦掉；而且本仓库的路径里
    有中文（``D:\\系统\\FileTools``），走 URL 编码容易出岔子。
    """
    encoded = base64.b64encode(ICON_SVG.read_bytes()).decode("ascii")
    return f"data:image/svg+xml;base64,{encoded}"


def _inline_icon_svg() -> str:
    """把图标源内联进 HTML，并覆写成**单色遮罩**。

    Android 的主题图标（monochrome）要的是一张「只有形状」的图：系统拿它当遮罩
    自己染色。所以把底色的 ``<rect>`` 摘掉、所有 ``path`` 统一刷白、顺手抹掉
    ``opacity`` —— 留一个 0.92 的半透明会让遮罩边缘不干净。
    """
    text = ICON_SVG.read_text(encoding="utf-8").strip()
    match = re.match(r"<svg\b[^>]*>", text)
    if match is None:
        raise SystemExit(f"{ICON_SVG} 不以 <svg> 开头，没法内联")
    style = "<style>rect{fill:none}path{fill:#fff;opacity:1}</style>"
    return text[: match.end()] + style + text[match.end() :]


def _page_html(mode: str, size: int) -> str:
    if mode == "solid":
        return (
            "<!doctype html><meta charset='utf-8'>"
            f"<style>html,body{{margin:0;width:{size}px;height:{size}px;background:{BRAND_HEX}}}"
            "</style>"
        )

    if mode == "mono":
        return (
            "<!doctype html><meta charset='utf-8'>"
            f"<style>html,body{{margin:0}}svg{{display:block;width:{size}px;height:{size}px}}"
            "</style>" + _inline_icon_svg()
        )

    background = BRAND_HEX if mode == "flat" else "transparent"
    if mode == "padded":
        inner = round(size * FOREGROUND_RATIO)
        body = "display:flex;align-items:center;justify-content:center"
        img = f'<img src="{_icon_data_uri()}" style="width:{inner}px;height:{inner}px">'
    else:
        body = "display:block"
        img = f'<img src="{_icon_data_uri()}" style="width:{size}px;height:{size}px">'

    return (
        "<!doctype html><meta charset='utf-8'>"
        f"<style>html,body{{margin:0;width:{size}px;height:{size}px;background:{background}}}"
        f"body{{{body}}}</style>{img}"
    )


def render_pngs(jobs: list[tuple[str, int, str]], out_dir: pathlib.Path) -> dict[str, pathlib.Path]:
    """把一批 (相对路径, 边长, 方式) 渲成 PNG，返回 {相对路径: 落盘路径}。"""
    from playwright.sync_api import sync_playwright

    written: dict[str, pathlib.Path] = {}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        # device_scale_factor=1 是关键：默认会跟着系统 DPI 走，
        # 在 125%/150% 缩放的 Windows 上会渲出 1.25 倍甚至 1.5 倍的图，
        # 尺寸断言当场全红。
        context = browser.new_context(device_scale_factor=1)
        page = context.new_page()
        try:
            for rel, size, mode in jobs:
                target = out_dir / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                page.set_viewport_size({"width": size, "height": size})
                page.set_content(_page_html(mode, size))
                # flat / solid 是要不透明底的，其余一律挖透明
                page.screenshot(
                    path=str(target),
                    omit_background=mode not in ("flat", "solid"),
                )
                written[rel] = target
        finally:
            context.close()
            browser.close()
    return written


# ---------------------------------------------------------------------------
# ICO 编码（交给 backend/.venv 的 Pillow）
# ---------------------------------------------------------------------------

_ICO_CODE = '''
import io, json, pathlib, struct, sys
from PIL import Image, BmpImagePlugin

master = pathlib.Path(sys.argv[1])
out = pathlib.Path(sys.argv[2])
sizes = sorted(set(int(s) for s in json.loads(sys.argv[3])))

# 边长 >= 这个值的条目用 PNG 压缩，其余用 BMP(DIB)。
#
# 这不是随手挑的分界：Windows 自己产出的 .ico、以及 Tauri 用来生成图标的
# ico crate，都遵循「小尺寸 BMP、256 用 PNG」这个惯例。Tauri 的 NSIS 安装器
# 要读这个文件当 installerIcon，跟惯例保持一致是风险最低的做法。
#
# （Pillow 自带的 ICO 编码器两条路都走不通：默认**全部**写 PNG，而
# bitmap_format="bmp" 又**全部**写 BMP。所以容器这里自己拼。）
PNG_FROM = 256


def dib_payload(frame):
    """把一个 RGBA 帧编成 ICO 里的 BMP 条目。

    Pillow 的 "DIB" 出口写的是裸的 BITMAPINFOHEADER + 像素，正好是 ICO 要的形状
    （"BMP" 出口会多出 14 字节的 BITMAPFILEHEADER，那 14 字节在 ICO 里是多余的）。
    唯一要改的是高度：ICO 规定 DIB 的 biHeight 写**真实高度的两倍**，因为颜色位
    之下还隐含一层 AND 掩码。32bpp 下系统不读那层掩码，Pillow 自己写 ICO 时也
    不写它，所以这里同样只改高度、不补掩码。
    """
    buffer = io.BytesIO()
    frame.save(buffer, format="DIB")
    payload = buffer.getvalue()
    if len(payload) < 12:
        raise SystemExit("Pillow 写的 DIB 短得不像话：%d 字节" % len(payload))
    return payload[:8] + struct.pack("<i", frame.height * 2) + payload[12:]


image = Image.open(master)
if image.mode != "RGBA":
    image = image.convert("RGBA")

frames = []
for size in sizes:
    if not 0 < size <= 256:
        raise SystemExit("ICO 单边必须在 1..256，收到 %d" % size)
    if size > max(image.size):
        raise SystemExit("母图只有 %s，放不出 %d" % (image.size, size))
    frame = image.resize((size, size), Image.Resampling.LANCZOS)
    if size >= PNG_FROM:
        buffer = io.BytesIO()
        frame.save(buffer, format="PNG")
        bits, colors, payload = 32, 0, buffer.getvalue()
    else:
        bits, colors = BmpImagePlugin.SAVE[frame.mode][1:]
        payload = dib_payload(frame)
    frames.append((size, bits, colors, payload))

offset = 6 + 16 * len(frames)
directory = b""
blobs = b""
for size, bits, colors, payload in frames:
    # ICO 用 0 表示 256（一个字节装不下 256）
    directory += struct.pack(
        "<BBBBHHII",
        size if size < 256 else 0,
        size if size < 256 else 0,
        colors,
        0,
        1,
        bits,
        len(payload),
        offset,
    )
    offset += len(payload)
    blobs += payload

out.write_bytes(struct.pack("<HHH", 0, 1, len(frames)) + directory + blobs)
print(json.dumps({"frames": [[s, b] for s, b, _c, _p in frames]}))
'''


def build_ico(master: pathlib.Path, target: pathlib.Path, sizes: tuple[int, ...]) -> None:
    """用 Pillow + 自拼容器合成多尺寸 ICO。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    _run_backend_python(_ICO_CODE, master, target, json.dumps(list(sizes)))
    if not target.exists():
        raise SystemExit(f"没写出 {target}")


def read_ico_sizes(path: pathlib.Path) -> list[tuple[int, int, bool]]:
    """纯 stdlib 读 ICO 目录，返回 [(宽, 高, 是不是 PNG 条目)]。

    自己解一遍而不是问 Pillow，是为了让「Pillow 到底写进去了什么」有个
    **独立**的确认。顺带回答一个真问题：小尺寸那条 NSIS 用的 ``installerIcon``
    能不能吃 —— ICO 允许每条目是 PNG 或 BMP，而 NSIS 对 PNG 条目支持不全，
    所以这里把每条的编码格式一并读出来。
    """
    raw = path.read_bytes()
    if len(raw) < 6:
        raise SystemExit(f"{path} 只有 {len(raw)} 字节，不是合法 ICO")
    reserved, kind, count = struct.unpack_from("<HHH", raw, 0)
    if reserved != 0 or kind != 1:
        raise SystemExit(f"{path} 不是 ICO（reserved={reserved}, type={kind}）")
    entries = []
    for index in range(count):
        offset = 6 + index * 16
        width, height = struct.unpack_from("<BB", raw, offset)
        image_offset = struct.unpack_from("<I", raw, offset + 12)[0]
        is_png = raw[image_offset : image_offset + 8] == b"\x89PNG\r\n\x1a\n"
        entries.append((width or 256, height or 256, is_png))
    return entries


# ---------------------------------------------------------------------------
# 文本产物
# ---------------------------------------------------------------------------


def pwa_manifest() -> str:
    return (
        json.dumps(
            {
                "name": "FileTools",
                "short_name": "FileTools",
                "description": PWA_DESCRIPTION,
                "start_url": "/",
                "display": "standalone",
                "background_color": "#ffffff",
                "theme_color": BRAND_HEX,
                "icons": [
                    {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png"},
                    {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png"},
                    {"src": "/favicon.svg", "sizes": "any", "type": "image/svg+xml"},
                ],
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n"
    )


def sha256_of(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def produce(stage: pathlib.Path) -> list[tuple[str, pathlib.Path]]:
    """把全部 PNG / ICO / 文本产物渲到 ``stage`` 下，返回 [(相对路径, 文件)]。"""
    rendered = render_pngs(OUTPUTS, stage)
    produced = list(rendered.items())

    # ICO 要的是「同一张图的不同边长」，而上面那批边长是为各平台挑过的，不一定
    # 覆盖 ICO 的全集。所以单独渲一张 256 的当母图，各档由 Pillow 从它降采样 ——
    # 降采样比直接按小边长渲染更接近人们在 Windows 上看到的图标观感（真实的
    # 小尺寸图标都做过手工像素对齐，矢量直缩在 16px 下笔画会偏细）。
    with tempfile.TemporaryDirectory(prefix="filetools-ico-") as tmp:
        masters = render_pngs([("master.png", 256, "plain")], pathlib.Path(tmp))
        master = masters["master.png"]
        for rel, sizes in ICO_TARGETS:
            target = stage / rel
            build_ico(master, target, sizes)
            produced.append((rel, target))

    for relative, origin in ((WEB_SVG_COPY, ICON_SVG), (WEB_LOGO_COPY, LOGO_SVG)):
        copy = stage / relative
        copy.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(origin, copy)
        produced.append((relative, copy))

    manifest = stage / "web" / "manifest.webmanifest"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(pwa_manifest(), encoding="utf-8")
    produced.append(("web/manifest.webmanifest", manifest))

    return sorted(produced)


def install(stage: pathlib.Path) -> int:
    """把 ``stage`` 里的产物复制到各消费方。返回复制的文件数。"""
    copied = 0
    for platform, destination in INSTALL_MAP:
        origin = stage / platform
        if not origin.is_dir():
            raise SystemExit(f"{origin} 不存在，生成阶段有问题")
        destination.mkdir(parents=True, exist_ok=True)
        for item in sorted(origin.rglob("*")):
            if not item.is_file():
                continue
            relative = item.relative_to(origin)
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(item, target)
            copied += 1
    return copied


def write_source_manifest(produced: list[tuple[str, pathlib.Path]]) -> pathlib.Path:
    """写 ``branding/generated/manifest.sha256``：真源 + 全部派生物的哈希。"""
    lines = [
        "# FileTools 品牌资产哈希清单 —— 由 scripts/generate_branding.py 生成",
        "# 格式： <sha256>  <路径>（相对仓库根）",
        f"{sha256_of(ICON_SVG)}  {ICON_SVG.relative_to(ROOT).as_posix()}",
    ]
    logo = SOURCE / "filetools-logo.svg"
    if logo.exists():
        lines.append(f"{sha256_of(logo)}  {logo.relative_to(ROOT).as_posix()}")
    for rel, path in produced:
        lines.append(f"{sha256_of(path)}  {(GENERATED / rel).relative_to(ROOT).as_posix()}")
    manifest = GENERATED / "manifest.sha256"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return manifest


def check(stage: pathlib.Path, produced: list[tuple[str, pathlib.Path]]) -> list[str]:
    """拿刚渲出来的产物跟盘上的比。返回漂移描述列表（空 = 一致）。"""
    drift: list[str] = []
    for rel, fresh in produced:
        on_disk = GENERATED / rel
        rel_display = on_disk.relative_to(ROOT).as_posix()
        if not on_disk.exists():
            drift.append(f"{rel_display} 不存在，需要重新生成")
            continue
        if sha256_of(on_disk) != sha256_of(fresh):
            drift.append(f"{rel_display} 与重新渲染的结果不一致")
    # 反向：盘上有、这轮没生成的（清单删过条目而没重新生成）
    for item in sorted(GENERATED.rglob("*")):
        if not item.is_file() or item.name == "manifest.sha256":
            continue
        rel = item.relative_to(GENERATED).as_posix()
        if rel not in {name for name, _ in produced}:
            drift.append(f"{item.relative_to(ROOT).as_posix()} 不在生成清单里，应删除")
    return drift


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="只校验，不写任何文件")
    args = parser.parse_args()

    if not ICON_SVG.exists():
        raise SystemExit(f"找不到图标真源 {ICON_SVG}")

    print(f"图标真源：{ICON_SVG.relative_to(ROOT).as_posix()}（{ICON_SVG.stat().st_size} 字节）")
    print(f"后端解释器（Pillow）：{BACKEND_PYTHON}")

    if args.check:
        with tempfile.TemporaryDirectory(prefix="filetools-branding-") as tmp:
            produced = produce(pathlib.Path(tmp))
            drift = check(pathlib.Path(tmp), produced)
        if drift:
            print(f"\n品牌资产漂移（{len(drift)} 处）：")
            for item in drift:
                print(f"  · {item}")
            print("\n跑一次 python scripts/generate_branding.py 修好它。")
            return 1
        print(f"\n品牌资产一致：{len(produced)} 个产物，逐一与重新渲染的结果逐字节相同。")
        return 0

    GENERATED.mkdir(parents=True, exist_ok=True)
    produced = produce(GENERATED)

    print(f"\n生成 {len(produced)} 个产物：")
    for rel, path in produced:
        print(f"  · {rel}  {path.stat().st_size} 字节")

    # ICO 的目录自己解一遍，把「里面到底有几档、是不是 PNG 条目」摊开
    for rel, _sizes in ICO_TARGETS:
        entries = read_ico_sizes(GENERATED / rel)
        detail = "、".join(f"{w}×{h}{'(PNG)' if png else '(BMP)'}" for w, h, png in entries)
        print(f"\n{rel} 内含 {len(entries)} 档：{detail}")

    copied = install(GENERATED)
    print(f"\n已安装 {copied} 个文件到消费方：")
    for platform, destination in INSTALL_MAP:
        print(f"  · branding/generated/{platform}/ → {destination.relative_to(ROOT).as_posix()}/")

    manifest = write_source_manifest(produced)
    print(f"\n哈希清单：{manifest.relative_to(ROOT).as_posix()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
