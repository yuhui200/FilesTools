"""第十阶段 A 的真机验收脚本（Playwright + 真实 Chromium + 真实产物打开）。

第十阶段 A 把「图片引擎」补齐了：旋转、翻转、裁剪、缩放、按目标体积压缩、
元数据、预览，以及把图片工具页变成能力驱动的一页。这个脚本要证的正是
**这些界面上的旋钮真的拧到了像素上**，而且**没有一句话说过头**：

1. **能力目录是唯一真相**（§五十二/§五十六）—— 图片工具页上列出的源格式
   必须与 ``/api/conversion/capabilities`` 的矩阵逐个一致；首页 FAQ 里
   提到的格式也一个都不能多。前端若还留着一份手抄的格式表，这里当场失配。
   这一条同时守住「服务器缺组件时不得宣传」：缺组件的格子在矩阵里就没有，
   页面上却出现了 —— 那就是在替服务器承诺一件它做不到的事。
2. **参数面板的快捷档来自服务端**（§二十七/§二十九）—— 质量那七档、目标
   体积那几档，页面上出现的按钮文案必须能在 ``options_schema`` 的
   ``presets`` 里逐条对上；点了按钮，输入框要真的被填上。
3. **选项落到像素上**（§十六–§三十二）—— 旋转 90° 宽高互换、翻转真的镜像
   （用只在左上角有一块色的素材，方向错了一眼看得出来）、裁剪改变尺寸、
   预设只缩不放、质量 90 比 50 大、``target_bytes`` 达标与否**与实测一致**、
   ``metadata=remove`` 之后 EXIF 真的没了。产物一律用 ``PIL.Image.open``
   打开，不看 HTTP 200 —— 转出一张坏图同样是 200。
4. **预览复用下载那条路**（§三十九–§四十一）—— 结果行的缩略图必须真的被
   浏览器解码出来（``naturalWidth > 0``），地址指向 ``/api/preview/``，
   而且它与下载下来的**是同一个文件**；看过之后下载仍然可用（预览不吃令牌）。
5. **压缩如实报告**（§三十一/§三十二）—— 结果行显示「原来 → 现在」；
   没达到目标体积时必须写明「未达到目标大小」，达到了就不许写。
6. **EXIF 方向归一化**（§三十七/§三十八）—— 手机竖拍那种「像素横着、靠
   标记显示成竖的」图，转出来必须是摆正的，且标记不留在结果里。
7. **移动端真的能用**（§五十六）—— 375 / 390 / 414 三档，无横向溢出。

跑法（浏览器驱动用带 Playwright 的解释器，素材与校验用后端 venv）::

    cd frontend && npm run build
    cd backend && .venv/Scripts/python -m uvicorn main:app --host 127.0.0.1 --port 8011
    python scripts/verify_phase10a.py

**不要用 Vite 开发服务器（5173）跑本脚本** —— 它把 ``/api`` 代理到别处，
版本不一致时会报出一批根本不存在的「回归」。

几处**踩过的坑**写在这里，免得下次又踩：

* 目标格式**没有自转换**。``matrix['jpg']`` 里没有 ``jpg`` —— 拿 jpg 源
  转 jpg 会得到 ``UNSUPPORTED_CONVERSION``（「暂不支持把「JPG 图片」转换
  为「JPG」」）。所以下面每一对 源→目标 都**先从能力目录查过**，
  没有一对是手写猜的。
* ``target_bytes`` 有下界（``MIN_TARGET_BYTES``，本机 5120）。填一个更小的
  数不是「压得更狠」，而是 400。脚本从 schema 的 ``min`` 取这个下界。
* 转换任务的创建返回 **202**，不是 200。

可用环境变量覆盖：
    FILETOOLS_WEB_BASE      默认 http://127.0.0.1:8011（直接打后端托管的 dist）
    FILETOOLS_BACKEND_PY    后端 venv 的 python，用于生成素材与打开下载结果
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request

from playwright.sync_api import Page, sync_playwright

ROOT = pathlib.Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
BASE = os.environ.get("FILETOOLS_WEB_BASE", "http://127.0.0.1:8011").rstrip("/")

# Windows 控制台默认是 GBK，直接打印一个装饰符号就会抛 UnicodeEncodeError
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

WORK = pathlib.Path(tempfile.mkdtemp(prefix="filetools-verify-p10a-"))
SAMPLES = WORK / "samples"
DOWNLOADS = WORK / "downloads"
REPORT = ROOT / "scripts" / "verify_phase10a_report.txt"

ROUTE = "/convert"
IMAGE_TOOLS_ROUTE = "/image"
MOBILE_WIDTHS = (375, 390, 414)
CAPABILITIES_URL = "/api/conversion/capabilities"

#: 对外文本里绝不能出现的东西（与 phase8 / phase9 同一张表）。
FORBIDDEN_FRAGMENTS = (
    "Traceback",
    'File "',
    "\\",
    "/tmp/",
    "AppData",
    "site-packages",
    "filetools_",
    "soffice",
    "LibreOffice",
    "rapidocr",
    "onnxruntime",
    "python-docx",
    "docx_writer",
    "pymupdf",
    "fitz",
    "PIL",
    "venv",
)

results: list[tuple[bool, str]] = []
console_errors: list[str] = []


def check(ok: bool, label: str) -> None:
    results.append((ok, label))
    print(f"{'PASS' if ok else 'FAIL'}  {label}", flush=True)


def section(title: str) -> None:
    print(f"\n=== {title} ===", flush=True)


# ----------------------------------------------------------------------
# 素材：一律用后端 venv 生成（它才有 Pillow）
# ----------------------------------------------------------------------

_FIXTURE_CODE = r'''
import os, sys

from PIL import Image, ImageDraw

out = sys.argv[1]
os.makedirs(out, exist_ok=True)


def tagged():
    """相机信息，用来验 metadata 的保留 / 清除。"""
    exif = Image.Exif()
    exif[0x010F] = "FileToolsCamera"      # Make
    exif[0x0110] = "FT-10A"               # Model
    exif[0x0131] = "FileTools Verifier"   # Software
    return exif


# 主图 400x300。带 EXIF 的存成 JPG，另存一份带 EXIF 的 PNG ——
# **PNG 那份才是压缩与元数据测试的源**：目标格式里没有自转换，
# 拿 JPG 源去转 JPG 是转不了的（见脚本开头的「踩过的坑」）。
photo = Image.new("RGB", (400, 300), (40, 90, 160))
ImageDraw.Draw(photo).rectangle([0, 0, 399, 299], outline=(255, 255, 255), width=3)
photo.save(os.path.join(out, "photo.jpg"), format="JPEG", quality=92, exif=tagged())
photo.save(os.path.join(out, "tagged.png"), format="PNG", exif=tagged())


def noisy(width, height):
    grain = Image.effect_noise((width, height), 48).convert("L")
    return Image.merge("RGB", (grain, grain, grain))


# 噪点大图：按目标体积压缩要真搜得动。纯色图一压就到极限，
# 搜不出「质量在变、必要时在缩尺寸」的过程。
noisy(2400, 1200).save(os.path.join(out, "big.png"), format="PNG")
# 质量档对比用的小一号噪点图：900x600 足够让 q90 与 q50 拉开差距，
# 又不用为了动一下质量滑杆去解码一张 7 MB 的 PNG。
noisy(900, 600).save(os.path.join(out, "texture.png"), format="PNG")


# 方向素材：**只有左上角**有一块红，其余全白。
# 镜像与旋转都会把它挪到别的角上 —— 「转了吗、往哪转的」一眼看得出来，
# 比只比尺寸强得多（§七十二：不能只检查文件存在）。
def corner(w=200, h=150):
    canvas = Image.new("RGB", (w, h), (255, 255, 255))
    ImageDraw.Draw(canvas).rectangle([0, 0, w // 4, h // 4], fill=(200, 30, 30))
    return canvas


corner().save(os.path.join(out, "corner.png"), format="PNG")


# EXIF 方向 6（顺时针 90° 才正）：存储像素是 40x20 的横图，
# 四角颜色互不相同 —— 摆正之后必须是 20x40，且颜色按 6 档重排。
# 质量拉满 + 4:4:4：下面比的是**具体颜色**，色度抽样会把象限边界糊成过渡色。
TL, TR, BL, BR = (255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 255)
stored = Image.new("RGB", (40, 20), TL)
stored.paste(TR, (20, 0, 40, 10))
stored.paste(BL, (0, 10, 20, 20))
stored.paste(BR, (20, 10, 40, 20))
exif = stored.getexif()
exif[0x0112] = 6
stored.save(os.path.join(out, "oriented.jpg"), format="JPEG", quality=100,
            subsampling=0, exif=exif)


# 假扩展名：内容其实是 PDF（安全那一段用）
pdf = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n"
with open(os.path.join(out, "fake.png"), "wb") as fh:
    fh.write(pdf)
'''


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


def _run_backend_python(code: str, *args: object) -> str:
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
        env=dict(os.environ, PYTHONIOENCODING="utf-8"),
    )
    if out.returncode != 0:
        raise RuntimeError(out.stderr or out.stdout)
    if out.stdout is None:
        raise RuntimeError(f"子进程没有可读的 stdout。stderr：{out.stderr!r}")
    return out.stdout


def build_fixtures() -> None:
    try:
        _run_backend_python(_FIXTURE_CODE, SAMPLES)
    except RuntimeError as exc:
        raise SystemExit(f"生成测试素材失败：\n{exc}") from exc


# ----------------------------------------------------------------------
# 打开下载到的产物。**这才是「真的转出来了」的证据**
# ----------------------------------------------------------------------

#: 采样点取 12% / 88%，不是象限中心（25% / 75%）。
#:
#: ``corner.png`` 的红块占左上整块（0–25% 见方），12% 稳稳在块内；
#: 而象限中心 (25%, 25%) 恰好是红块右下角**那个**像素，差一个像素就翻。
#: 方向素材摆正后是 20×40，12% / 88% 也分别落在左右两列、上下两行之内。
_INSPECT_IMAGE = r'''
import json, sys
from PIL import Image

with Image.open(sys.argv[1]) as img:
    width, height = img.size
    rgb = img.convert("RGB")
    print(json.dumps({
        "format": img.format,
        "width": width,
        "height": height,
        "mode": img.mode,
        "corners": [
            list(rgb.getpixel((round(width * 0.12), round(height * 0.12)))),
            list(rgb.getpixel((round(width * 0.88), round(height * 0.12)))),
            list(rgb.getpixel((round(width * 0.12), round(height * 0.88)))),
            list(rgb.getpixel((round(width * 0.88), round(height * 0.88)))),
        ],
        "exif_keys": sorted(dict(img.getexif()).keys()),
        "exif_make": img.getexif().get(0x010F),
        "exif_model": img.getexif().get(0x0110),
        "exif_orientation": img.getexif().get(0x0112),
    }))
'''


def inspect_image(path: pathlib.Path) -> dict:
    return json.loads(_run_backend_python(_INSPECT_IMAGE, path))


#: 四角颜色的期望值与 ``tests/test_image_orientation.py`` 的 ``EXPECTED`` 同源。
RED, GREEN, BLUE, WHITE = (255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 255)
CORNER_RED, CORNER_WHITE = (200, 30, 30), (255, 255, 255)


def close(pixel: list[int], want: tuple[int, int, int], tolerance: int = 12) -> bool:
    """有损格式（JPEG / WEBP）不可能逐位还原，按容差比。

    容差 12 而不是 2 —— 与 pytest 那份同一个理由：采样点在纯色块内部，
    离边界很远，量化误差到不了这里，但不同版本的编码器在色度上会有
    几个灰阶的出入，写死 ±2 会变成一个随库版本飘的假失败。
    """
    return all(abs(a - b) <= tolerance for a, b in zip(pixel, want))


# ----------------------------------------------------------------------
# 服务端：能力目录
# ----------------------------------------------------------------------

def fetch_capabilities() -> dict:
    with urllib.request.urlopen(f"{BASE}{CAPABILITIES_URL}", timeout=30) as response:
        return json.load(response)


def fetch_bytes(url: str) -> tuple[int, str, bytes]:
    """取一个地址的状态码、Content-Type 与正文。"""
    try:
        with urllib.request.urlopen(f"{BASE}{url}", timeout=60) as response:
            return response.status, response.headers.get("content-type", ""), response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.headers.get("content-type", ""), b""


class Catalogue:
    """能力目录的一个薄包装。

    所有「源格式 → 目标格式」的组合都从这里查，**没有一处手写**：
    自转换不存在（jpg 转不了 jpg）、缺组件时某些格子会消失，
    手写的组合会在这些地方变成假失败。
    """

    def __init__(self, data: dict) -> None:
        self.data = data
        self.category = {item["value"]: item["category"] for item in data["formats"]}
        self.label = {item["value"]: item["label"] for item in data["formats"]}
        self.entry = {item["id"]: item for item in data["conversions"]}

    @property
    def image_sources(self) -> list[str]:
        return [
            value for value in self.data["matrix"] if self.category.get(value) == "image"
        ]

    def targets(self, source: str) -> list[str]:
        return list(self.data["matrix"].get(source, []))

    def has(self, source: str, target: str) -> bool:
        return target in self.data["matrix"].get(source, [])

    def spec(self, conversion_id: str, key: str) -> dict:
        """某个转换条目下某个参数的 schema。"""
        for item in self.entry[conversion_id]["options_schema"]["items"]:
            if item["key"] == key:
                return item
        raise KeyError(f"{conversion_id} 没有参数 {key}")


def target_bytes_floor(catalogue: Catalogue) -> int:
    """``target_bytes`` 的下界 —— 从 schema 取，不在这里抄一个数字。"""
    return int(catalogue.spec("image.png-to-jpg", "target_bytes")["min"])


# ----------------------------------------------------------------------
# 浏览器小工具（与 phase9 同一套，减少「两个脚本各有一套点法」的漂移）
# ----------------------------------------------------------------------

def attach(page: Page) -> None:
    page.on(
        "console",
        lambda msg: console_errors.append(f"[console.{msg.type}] {msg.text}")
        if msg.type == "error"
        else None,
    )
    page.on("pageerror", lambda err: console_errors.append(f"[pageerror] {err}"))


def body(page: Page) -> str:
    return page.inner_text("body")


def upload(page: Page, *names: str, scope=None) -> None:
    target = (scope or page).locator("input[type=file]").first
    target.set_input_files([str(SAMPLES / name) for name in names])
    page.wait_for_timeout(400)


def upload_raw(page: Page, name: str, content: bytes, mime: str) -> None:
    page.locator("input[type=file]").first.set_input_files(
        [{"name": name, "mimeType": mime, "buffer": content}]
    )
    page.wait_for_timeout(400)


def pick_target(page: Page, value: str) -> None:
    """选目标格式。单选框是 sr-only 的，真实用户点的是外层 label。

    ``name`` 是 ``conversion-target-<groupId>``，组 id 由前端生成，
    所以这里按前缀匹配 —— 不去猜一个测试根本不掌握的值。
    """
    label = page.locator(
        f"label:has(input[name^='conversion-target-'][value='{value}'])"
    ).first
    label.evaluate("el => el.scrollIntoView({block: 'center'})")
    page.wait_for_timeout(100)
    label.click()
    page.wait_for_timeout(250)


def set_option(page: Page, key: str, value: str, scope=None) -> None:
    """设置一项参数。控件形状由 schema 决定，所以四种都试一遍。

    键里带点（``resize.mode``），选择器一律加引号；``scope`` 是控件所在的
    容器，不限定范围的话 ``[name^=...]`` 会跨组命中第一组的同名控件。
    等一等一律用 ``page`` —— ``Locator`` 上没有 ``wait_for_timeout``。
    """
    root = scope if scope is not None else page
    selector = f"[name^='conversion-{key}-']"

    radio = root.locator(f"label:has(input{selector}[value='{value}'])").first
    if radio.count() > 0:
        radio.evaluate("el => el.scrollIntoView({block: 'center'})")
        page.wait_for_timeout(80)
        radio.click()
        page.wait_for_timeout(150)
        return

    select = root.locator(f"select{selector}").first
    if select.count() > 0:
        select.select_option(value)
        page.wait_for_timeout(150)
        return

    checkbox = root.locator(f"input[type=checkbox]{selector}").first
    if checkbox.count() > 0:
        if (value == "true") != checkbox.is_checked():
            checkbox.click()
        page.wait_for_timeout(150)
        return

    number = root.locator(f"input{selector}").first
    number.fill(value)
    page.wait_for_timeout(150)


def option_control(page: Page, key: str):
    return page.locator(f"[name^='conversion-{key}-']").first


def group_card(page: Page, filename: str):
    """按文件名定位那一组的卡片。"""
    return page.locator("section.card").filter(has_text=filename).first


def wait_group_done(page: Page, filename: str, timeout: int = 240_000) -> str:
    card = group_card(page, filename)
    card.locator("button", has_text=re.compile("下载结果|打包下载")).first.wait_for(
        timeout=timeout
    )
    page.wait_for_timeout(300)
    return card.inner_text()


_download_seq = 0


def download_in(page: Page, scope, pattern: str = "下载结果|打包下载") -> pathlib.Path:
    global _download_seq
    button = scope.locator("button", has_text=re.compile(pattern)).first
    with page.expect_download() as info:
        button.click()
    item = info.value
    _download_seq += 1
    DOWNLOADS.mkdir(parents=True, exist_ok=True)
    target = DOWNLOADS / f"{_download_seq:02d}-{item.suggested_filename}"
    item.save_as(target)
    return target


def start_and_wait(page: Page, filename: str, timeout: int = 240_000) -> str:
    page.locator("button", has_text=re.compile("^开始转换这")).first.click()
    return wait_group_done(page, filename, timeout=timeout)


def convert_via_ui(
    page: Page,
    filename: str,
    target: str,
    options: dict[str, str] | None = None,
    timeout: int = 240_000,
) -> pathlib.Path:
    """走一遍真实的界面流程：上传 → 选目标 → 调参数 → 开始 → 下载。

    ``options`` 是**有序**的字典（Python 3.7+ 保证插入顺序），
    有依赖关系的参数必须按依赖顺序写 —— 例如 ``resize.mode=custom``
    要排在 ``resize.width`` 前面，否则后者还没渲染出来。
    """
    page.goto(f"{BASE}{ROUTE}", wait_until="networkidle")
    upload(page, filename)
    pick_target(page, target)
    for key, value in (options or {}).items():
        set_option(page, key, value)
    start_and_wait(page, filename, timeout=timeout)
    return download_in(page, group_card(page, filename))


# ======================================================================
# A 段：能力目录里的图片选项（服务端）
# ======================================================================

#: 第十阶段 A 新增的、必须出现在图片 → 图片条目上的参数键。
_REQUIRED_IMAGE_OPTIONS = (
    "quality",
    "target_bytes",
    "resize.mode",
    "resize.width",
    "resize.height",
    "resize.keep_aspect",
    "resize.fit",
    "crop.ratio",
    "crop.x",
    "crop.y",
    "crop.width",
    "crop.height",
    "rotation",
    "rotation.expand",
    "flip",
    "dpi",
    "metadata",
)


def section_a(page: Page, catalogue: Catalogue) -> None:
    section("A 能力目录：图片参数与快捷档")

    entry = catalogue.entry["image.png-to-jpg"]
    keys = {spec["key"] for spec in entry["options_schema"]["items"]}
    missing = [key for key in _REQUIRED_IMAGE_OPTIONS if key not in keys]
    check(not missing, f"图片条目带齐了第十阶段 A 的参数（缺 {missing}）")

    quality = catalogue.spec("image.png-to-jpg", "quality")
    presets = [item["value"] for item in quality.get("presets", [])]
    check(
        presets == ["100", "90", "85", "80", "70", "60", "50"],
        f"质量七档由服务端下发（{presets}）",
    )
    check(str(quality["default"]) == "85", f"质量默认 85（实际 {quality['default']}）")

    tb = catalogue.spec("image.png-to-jpg", "target_bytes")
    check(
        len(tb.get("presets", [])) >= 4,
        f"目标体积有几档快捷值（{len(tb.get('presets', []))} 档）",
    )
    check(int(tb["min"]) > 0, f"目标体积有明确下界（≥ {tb['min']} 字节，低于它会 400）")

    # 无损目标不该给质量旋钮（§二十七：PNG / BMP / TIFF 不给无意义的旋钮）
    lossless_keys = {
        spec["key"]
        for spec in catalogue.entry["image.jpg-to-png"]["options_schema"]["items"]
    }
    check("quality" not in lossless_keys, "无损目标（PNG）不出现质量参数")

    # 预览标志与预览地址必须同进同退
    movable = {"jpg", "png", "webp", "bmp", "gif"}
    flagged = {
        item["target_type"]
        for item in catalogue.data["conversions"]
        if item["supports_preview"]
    }
    check(flagged <= movable, f"声明可预览的都是位图格式（多出来的 {flagged - movable}）")
    check(flagged == movable, "可预览的目标恰好是那五种位图")

    # 下面每一对 源→目标 都从能力目录查过；先证明这套查询本身是通的
    pairs = (("jpg", "png"), ("png", "bmp"), ("png", "jpg"), ("jpg", "webp"))
    check(
        all(catalogue.has(*pair) for pair in pairs),
        f"脚本要用到的源→目标组合都存在（{len(pairs)} 对）",
    )
    check(
        not catalogue.has("jpg", "jpg") and not catalogue.has("png", "png"),
        "能力目录里没有自转换（脚本据此避开 jpg→jpg 这种不存在的组合）",
    )


# ======================================================================
# B 段：图片工具页由能力目录驱动
# ======================================================================

def section_b(page: Page, catalogue: Catalogue) -> None:
    section("B 图片工具页：矩阵来自能力目录")

    page.goto(f"{BASE}{IMAGE_TOOLS_ROUTE}", wait_until="networkidle")
    page.wait_for_timeout(600)
    text = body(page)

    check(
        page.locator("h1").first.inner_text().strip() == "图片工具",
        "图片工具页的 h1 是「图片工具」",
    )

    sources = catalogue.image_sources
    check(len(sources) >= 6, f"服务端给了 {len(sources)} 种图片源格式")

    # 这一栏是**两列**：左边是源格式，右边是它能转成的目标。
    # 必须分开取 —— 第一版拿整页文本做子串匹配，结果 ICO 被误判成
    # 「宣传了不存在的格式」：ICO 是 PNG 的目标（PNG→ICO 真的存在），
    # 只是它不作为源出现。**判据写错会把对的东西判成错的**，
    # 所以这里按结构取，两个角色各断言各的。
    matrix = page.locator("section[aria-labelledby='image-matrix-heading']")
    source_labels = matrix.locator("li > div > p:first-child").all_inner_texts()
    chip_labels = [
        chip.strip().lstrip("→").strip()
        for chip in matrix.locator("li > ul > li > a").all_inner_texts()
    ]

    want_sources = {catalogue.label[value] for value in sources}
    check(
        set(source_labels) == want_sources,
        "左列的源格式恰好是矩阵里的图片源"
        f"（多 {sorted(set(source_labels) - want_sources)}，"
        f"少 {sorted(want_sources - set(source_labels))}）",
    )

    want_targets = {
        catalogue.label.get(target, target.upper())
        for value in sources
        for target in catalogue.targets(value)
    }
    check(
        set(chip_labels) == want_targets,
        "右列的目标恰好是矩阵里的目标"
        f"（多 {sorted(set(chip_labels) - want_targets)}，"
        f"少 {sorted(want_targets - set(chip_labels))}）",
    )
    # 目标里出现、但从不作为源出现的格式（本机的 ICO）：它只能出现在
    # 右列，绝不能跑到左列去 —— 那会变成「能上传 ICO」的假承诺。
    target_only = want_targets - want_sources
    check(
        not (target_only & set(source_labels)),
        f"只作目标的格式没有混进源列（{sorted(target_only)}）",
    )

    # 旧页面一个都没删（§五十三）
    for route, name in (
        ("/image/compress", "图片压缩"),
        ("/image/convert", "图片格式转换"),
        ("/image/resize", "调整图片尺寸"),
    ):
        check(
            page.locator(f"a[href='{route}']").count() > 0,
            f"图片工具页仍保留「{name}」入口",
        )

    # 缺组件的说明必须露出来（本机什么都不缺时这一条自动通过）
    notes = catalogue.data["notes"]
    if notes:
        check(
            all(note[:12] in text for note in notes),
            f"缺组件的说明写在页面上（{len(notes)} 条）",
        )
    else:
        check(True, "本机没有缺失组件，无需提示（矩阵已含全部能力）")

    # 只转第一帧、清除元数据的真实边界：写在页面上，不藏着
    check(
        "只转换第一帧" in text and "不做像素级擦除" in text,
        "多帧只转第一帧与元数据清除的边界写在页面上",
    )


# ======================================================================
# C 段：参数面板的快捷档
# ======================================================================

def section_c(page: Page, catalogue: Catalogue) -> None:
    section("C 参数面板：快捷档点了要真的填进输入框")

    source, target = "tagged.png", "jpg"

    page.goto(f"{BASE}{ROUTE}", wait_until="networkidle")
    upload(page, source)
    pick_target(page, target)

    quality = catalogue.spec("image.png-to-jpg", "quality")
    labels = [item["label"] for item in quality["presets"]]
    panel = group_card(page, source)

    missing = [
        label
        for label in labels
        if panel.get_by_role("button", name=label, exact=True).count() == 0
    ]
    check(not missing, f"七个质量档按钮都渲染出来了（缺 {missing}）")

    wanted = next(item for item in quality["presets"] if item["value"] == "90")
    panel.get_by_role("button", name=wanted["label"], exact=True).first.click()
    page.wait_for_timeout(200)
    got = option_control(page, "quality").input_value()
    check(got == "90", f"点「{wanted['label']}」之后输入框是 90（实际 {got}）")

    # 目标体积的快捷档同理 —— 它证明这套渲染器不是给质量开的后门
    tb = catalogue.spec("image.png-to-jpg", "target_bytes")
    first = tb["presets"][0]
    panel.get_by_role("button", name=first["label"], exact=True).first.click()
    page.wait_for_timeout(200)
    got = option_control(page, "target_bytes").input_value()
    check(
        got == first["value"],
        f"点「{first['label']}」之后目标体积是 {first['value']}（实际 {got}）",
    )


# ======================================================================
# D 段：几何真的生效（产物真打开）
# ======================================================================

def section_d(page: Page, catalogue: Catalogue) -> None:
    section("D 几何：选项落到像素上")

    # ---- 不翻转时红块留在左上：这是下面三条的对照，缺了它们
    #      「翻转生效」可能只是原图本来就长这样 ----
    plain = convert_via_ui(page, "corner.png", "bmp")
    info = inspect_image(plain)
    check(
        (info["width"], info["height"]) == (200, 150),
        f"不做任何处理时尺寸不变（{info['width']}×{info['height']}）",
    )
    check(
        close(info["corners"][0], CORNER_RED) and close(info["corners"][1], CORNER_WHITE),
        f"不翻转时红块留在左上（四角 {info['corners']}）",
    )

    # ---- 旋转 90°：宽高互换，左上角那块红转到右上角 ----
    # 目标用 BMP（无损）：上面比的是具体颜色，有损格式的振铃会干扰。
    rotated = convert_via_ui(page, "corner.png", "bmp", {"rotation": "90"})
    info = inspect_image(rotated)
    check(
        (info["width"], info["height"]) == (150, 200),
        f"旋转 90° 后宽高互换（{info['width']}×{info['height']}）",
    )
    check(
        close(info["corners"][0], CORNER_WHITE) and close(info["corners"][1], CORNER_RED),
        f"顺时针 90°：红块从左上转到右上（四角 {info['corners']}）",
    )

    # ---- 翻转：水平翻转把红块挪到右上，垂直翻转挪到左下 ----
    flipped = convert_via_ui(page, "corner.png", "bmp", {"flip": "horizontal"})
    info = inspect_image(flipped)
    check(
        (info["width"], info["height"]) == (200, 150),
        f"水平翻转不改变尺寸（{info['width']}×{info['height']}）",
    )
    check(
        close(info["corners"][0], CORNER_WHITE) and close(info["corners"][1], CORNER_RED),
        f"水平翻转：红块到右上（四角 {info['corners']}）",
    )

    flipped_v = convert_via_ui(page, "corner.png", "bmp", {"flip": "vertical"})
    info_v = inspect_image(flipped_v)
    check(
        close(info_v["corners"][0], CORNER_WHITE)
        and close(info_v["corners"][2], CORNER_RED),
        f"垂直翻转：红块到左下（四角 {info_v['corners']}）",
    )
    # 水平与垂直翻转不是同一张图 —— 否则上面两条可能都在测同一件事
    check(
        flipped.read_bytes() != flipped_v.read_bytes(),
        "水平翻转与垂直翻转产出的不是同一张图",
    )

    # ---- 裁剪 ----
    cropped = convert_via_ui(
        page,
        "photo.jpg",
        "png",
        {"crop.width": "200", "crop.height": "100", "crop.x": "0", "crop.y": "0"},
    )
    info = inspect_image(cropped)
    check(
        (info["width"], info["height"]) == (200, 100),
        f"裁剪产出 200×100（实际 {info['width']}×{info['height']}）",
    )

    # ---- 预设：w640 是「宽 640」（允许放大），large 是「长边 2560」（只缩不放）----
    shrunk = convert_via_ui(page, "big.png", "jpg", {"resize.mode": "w640"})
    info = inspect_image(shrunk)
    check(
        (info["width"], info["height"]) == (640, 320),
        f"预设 w640 等比缩到 640×320（实际 {info['width']}×{info['height']}）",
    )

    small = convert_via_ui(page, "corner.png", "bmp", {"resize.mode": "large"})
    info = inspect_image(small)
    check(
        (info["width"], info["height"]) == (200, 150),
        f"小图选「大」档不被放大（仍是 {info['width']}×{info['height']}）",
    )

    # ---- 质量档：同一张图，90 必须比 50 大 ----
    high = convert_via_ui(page, "texture.png", "jpg", {"quality": "90"})
    low = convert_via_ui(page, "texture.png", "jpg", {"quality": "50"})
    check(
        high.stat().st_size > low.stat().st_size,
        f"质量 90 的产物比 50 大（{high.stat().st_size} > {low.stat().st_size}）",
    )

    # ---- 自定义尺寸：fit 放进框内，stretch 拉满 ----
    # 顺序要紧：resize.fit / resize.width 都要等 resize.mode=custom 之后才出现
    fitted = convert_via_ui(
        page,
        "photo.jpg",
        "png",
        {
            "resize.mode": "custom",
            "resize.width": "100",
            "resize.height": "100",
            "resize.fit": "fit",
        },
    )
    info = inspect_image(fitted)
    check(
        (info["width"], info["height"]) == (100, 75),
        f"放入 100×100 的框：等比缩成 100×75（实际 {info['width']}×{info['height']}）",
    )

    stretched = convert_via_ui(
        page,
        "photo.jpg",
        "png",
        {
            "resize.mode": "custom",
            "resize.width": "100",
            "resize.height": "100",
            "resize.fit": "stretch",
        },
    )
    info = inspect_image(stretched)
    check(
        (info["width"], info["height"]) == (100, 100),
        f"拉伸到 100×100（实际 {info['width']}×{info['height']}）",
    )


# ======================================================================
# E 段：按目标体积压缩（如实报告）
# ======================================================================

def section_e(page: Page, catalogue: Catalogue) -> None:
    section("E 按目标体积压缩：达标与否必须与实测一致")

    floor = target_bytes_floor(catalogue)
    reachable = 100 * 1024

    page.goto(f"{BASE}{ROUTE}", wait_until="networkidle")
    upload(page, "big.png")
    pick_target(page, "jpg")
    set_option(page, "target_bytes", str(reachable))
    card_text = start_and_wait(page, "big.png")
    ok_result = download_in(page, group_card(page, "big.png"))
    ok_size = ok_result.stat().st_size

    check(ok_size <= reachable, f"产物不超过目标体积（{ok_size} ≤ {reachable}）")
    check("→" in card_text, "结果卡显示「原来 → 现在」的体积对比")
    check("未达到目标大小" not in card_text, "达标时**不**出现「未达到目标大小」")

    # 极限目标：用 schema 里的下界，而不是自己编一个更小的数（那会直接 400）
    page.goto(f"{BASE}{ROUTE}", wait_until="networkidle")
    upload(page, "big.png")
    pick_target(page, "jpg")
    set_option(page, "target_bytes", str(floor))
    hard_text = start_and_wait(page, "big.png")
    hard_result = download_in(page, group_card(page, "big.png"))
    hard_size = hard_result.stat().st_size

    # 这一条是 §三十一/§三十二 的要害：**报的必须与量出来的一致**。
    # 两种情况都是合格的，唯独「量出来没达标、页面上却说达标」不合格。
    if hard_size <= floor:
        check(not ("未达到目标大小" in hard_text), f"达到了下界目标（{hard_size} ≤ {floor}）时不许说没达到")
    else:
        check(
            "未达到目标大小" in hard_text,
            f"没达到目标体积（{hard_size} > {floor}）时页面如实写明「未达到目标大小」",
        )
    check(hard_size < ok_size, f"目标更紧时产物更小（{hard_size} < {ok_size}）")


# ======================================================================
# F 段：元数据与 EXIF 方向（§三十三–§三十八）
# ======================================================================

def section_f(page: Page) -> None:
    section("F 元数据与 EXIF 方向")

    kept = convert_via_ui(page, "tagged.png", "jpg", {"metadata": "keep"})
    info = inspect_image(kept)
    check(
        info["exif_make"] == "FileToolsCamera" and info["exif_model"] == "FT-10A",
        f"metadata=keep 保住了相机信息（{info['exif_make']} / {info['exif_model']}）",
    )

    stripped = convert_via_ui(page, "tagged.png", "jpg", {"metadata": "remove"})
    info = inspect_image(stripped)
    check(
        info["exif_make"] is None and info["exif_model"] is None and not info["exif_keys"],
        f"metadata=remove 之后相机信息真的没了（{info['exif_make']} / {info['exif_model']}）",
    )

    # EXIF 方向 6：存储像素是 40×20 的横图，摆正之后必须是 20×40。
    # 期望的四角颜色与 tests/test_image_orientation.py 的 EXPECTED[6] 同源：
    # 顺时针 90° 之后 左上=蓝 右上=红 左下=白 右下=绿。
    upright = convert_via_ui(page, "oriented.jpg", "png")
    info = inspect_image(upright)
    check(
        (info["width"], info["height"]) == (20, 40),
        f"方向 6 的图转出来是竖的（{info['width']}×{info['height']}）",
    )
    want = (BLUE, RED, WHITE, GREEN)
    check(
        all(close(got, expected) for got, expected in zip(info["corners"], want)),
        f"方向 6 的四角配色摆对了（{info['corners']} 期望 {list(want)}）",
    )
    check(
        info["exif_orientation"] in (None, 1),
        f"结果里不再留着方向标记（{info['exif_orientation']}）",
    )


# ======================================================================
# G 段：预览与下载（§三十九–§四十一）
# ======================================================================

def section_g(page: Page) -> None:
    section("G 预览：复用下载那条路，且不吃令牌")

    page.goto(f"{BASE}{ROUTE}", wait_until="networkidle")
    upload(page, "photo.jpg")
    pick_target(page, "png")
    start_and_wait(page, "photo.jpg")

    card = group_card(page, "photo.jpg")
    thumbnail = card.locator("img[src^='/api/preview/']").first
    check(thumbnail.count() > 0, "结果行里有预览缩略图")

    preview_bytes = b""
    if thumbnail.count() > 0:
        src = thumbnail.get_attribute("src") or ""
        # 浏览器真的把它解码出来了才算数：一个 404 的 <img> 在 DOM 里
        # 一样存在，只是没有自然宽度（naturalWidth === 0）。
        page.wait_for_timeout(900)
        check(
            thumbnail.evaluate("el => el.complete && el.naturalWidth > 0"),
            f"缩略图被浏览器真的解码出来了（{src}）",
        )
        status, content_type, preview_bytes = fetch_bytes(src)
        check(
            status == 200 and content_type.startswith("image/"),
            f"预览地址不带任何凭据直接打开就是图片（{status} {content_type}）",
        )

    # 预览之后下载仍然可用（预览**不消耗**下载令牌）
    result = download_in(page, card)
    info = inspect_image(result)
    check(
        (info["width"], info["height"]) == (400, 300),
        f"预览过之后仍能下载到结果（{info['width']}×{info['height']}）",
    )
    check(
        preview_bytes == result.read_bytes(),
        f"预览与下载是同一份文件（{len(preview_bytes)} 字节）",
    )

    # 不可预览的目标不挂缩略图 —— 也不该有一个点下去报错的按钮
    page.goto(f"{BASE}{ROUTE}", wait_until="networkidle")
    upload(page, "photo.jpg")
    pick_target(page, "pdf")
    start_and_wait(page, "photo.jpg")
    card = group_card(page, "photo.jpg")
    check(
        card.locator("img[src^='/api/preview/']").count() == 0,
        "PDF 结果不挂预览缩略图",
    )
    check(
        card.locator("button", has_text=re.compile("下载结果|打包下载")).count() > 0,
        "PDF 结果照样能下载（只是不内联预览）",
    )


# ======================================================================
# H 段：安全与不泄露
# ======================================================================

def section_h(page: Page, catalogue: Catalogue) -> None:
    section("H 安全与对外文本")

    # 假扩展名：扩展名是 .png，内容是 PDF。前端的本地校验只看扩展名，
    # 所以它会真的被传上去 —— 拦住它的是服务端的内容识别。
    page.goto(f"{BASE}{ROUTE}", wait_until="networkidle")
    upload_raw(page, "fake.png", (SAMPLES / "fake.png").read_bytes(), "image/png")
    if page.locator("label:has(input[name^='conversion-target-'])").count() > 0:
        pick_target(page, "jpg")
        page.locator("button", has_text=re.compile("^开始转换这")).first.click()
        page.wait_for_function(
            "() => { const t = document.body.innerText;"
            " return t.includes('不支持') || t.includes('失败'); }",
            timeout=180_000,
        )
    text = body(page)
    check(
        any(word in text for word in ("不支持", "无法", "损坏", "失败")),
        "伪装成 PNG 的 PDF 被内容识别拦下，并给出中文说明",
    )
    leaks = [fragment for fragment in FORBIDDEN_FRAGMENTS if fragment in text]
    check(not leaks, f"这条错误提示不含内部细节（{leaks}）")

    # 首页 FAQ 的格式清单必须与能力目录一致
    page.goto(BASE, wait_until="networkidle")
    details = page.locator("details").filter(has_text="支持哪些文件格式？").first
    details.locator("summary").click()
    page.wait_for_timeout(300)
    answer = details.locator("p").first.inner_text()

    # 只取**清单那一段**（第一个句号之前）。后面那段是与可用性无关的
    # 行为说明，里面会出现「Word」「Excel」这类词，拿它做格式名匹配
    # 会误报 —— 与上面 ICO 那次是同一个教训：判据要按角色分。
    listing = answer.split("。")[0]
    listed = {label for label in catalogue.label.values() if label in listing}
    want = {catalogue.label[value] for value in catalogue.data["matrix"]}
    check(
        listed == want,
        "首页 FAQ 列出的格式恰好是矩阵里此刻能作为输入的格式"
        f"（多 {sorted(listed - want)}，少 {sorted(want - listed)}）",
    )
    check(len(listed) >= 4, f"清单确实非空（{len(listed)} 种）")

    leaks = [fragment for fragment in FORBIDDEN_FRAGMENTS if fragment in answer]
    check(not leaks, f"FAQ 文案不含内部细节（{leaks}）")

    page.goto(f"{BASE}{IMAGE_TOOLS_ROUTE}", wait_until="networkidle")
    page.wait_for_timeout(600)
    leaks = [fragment for fragment in FORBIDDEN_FRAGMENTS if fragment in body(page)]
    check(not leaks, f"图片工具页不含内部细节（{leaks}）")


# ======================================================================
# I 段：移动端
# ======================================================================

def section_i(page: Page) -> None:
    section("I 移动端 375 / 390 / 414")

    for width in MOBILE_WIDTHS:
        page.set_viewport_size({"width": width, "height": 844})
        for route in (BASE, f"{BASE}{IMAGE_TOOLS_ROUTE}", f"{BASE}{ROUTE}"):
            page.goto(route, wait_until="networkidle")
            page.wait_for_timeout(400)
            overflow = page.evaluate(
                "() => document.documentElement.scrollWidth"
                " - document.documentElement.clientWidth"
            )
            name = route[len(BASE):] or "/"
            check(overflow <= 1, f"{width}px 下 {name} 无横向溢出（溢出 {overflow}px）")

    # 图片工具页在窄屏上仍然可读、可点
    page.set_viewport_size({"width": 375, "height": 844})
    page.goto(f"{BASE}{IMAGE_TOOLS_ROUTE}", wait_until="networkidle")
    page.wait_for_timeout(600)
    check(
        page.get_by_role("heading", name="当前可用的图片格式").count() > 0,
        "375px 下格式矩阵的标题仍在",
    )
    # 只在矩阵那一节里找标签。整页 `a[href='/convert']` 的**第一个**在
    # DOM 里是导航栏的「格式转换」（NAV_LINKS 里就有 /convert），而它在
    # 窄屏上是 `hidden ... md:flex` —— display:none 的元素 bounding_box()
    # 返回 None，于是「可点」被判成了失败。**又是判据写错，不是页面坏了。**
    matrix = page.locator("section[aria-labelledby='image-matrix-heading']")
    chip = matrix.locator("a[href='/convert']").first
    box = chip.bounding_box()
    check(
        box is not None and box["width"] > 0 and box["height"] >= 20,
        f"375px 下矩阵里的目标标签可点（{box}）",
    )

    # 光量尺寸还不够 —— 真的点一下，看它是不是把用户带进统一转换中心。
    # 这才是「可点」的直接证据；尺寸只是它的影子。
    if box is not None:
        chip.click()
        page.wait_for_timeout(400)
        check(
            page.url.rstrip("/") == f"{BASE}{ROUTE}".rstrip("/"),
            f"375px 下点矩阵里的目标标签真的进了统一转换中心（{page.url}）",
        )


# ======================================================================
# 汇总
# ======================================================================

def write_report() -> tuple[int, int]:
    """把结果写成报告文件，返回 ``(通过, 总数)``。

    返回计数是为了让**屏幕上那个数**和**文件里那个数**出自同一处：
    打印自己再数一遍，两者就有机会不一样（第一版正是如此）。
    """
    passed = sum(1 for ok, _ in results if ok)
    failed = [label for ok, label in results if not ok]
    lines = [
        "第十阶段 A 真机验收报告",
        f"地址：{BASE}",
        f"通过：{passed} / {len(results)}",
        "",
    ]
    if failed:
        lines.append("失败项：")
        lines.extend(f"  - {label}" for label in failed)
        lines.append("")
    if console_errors:
        lines.append(f"浏览器控制台错误 {len(console_errors)} 条：")
        lines.extend(f"  - {item}" for item in console_errors[:20])
    else:
        lines.append("浏览器控制台无错误。")
    REPORT.write_text("\n".join(lines), encoding="utf-8")
    return passed, len(results)


def summary() -> int:
    # 控制台那一条**必须**先入列 —— 它也是一条断言。第一版把它放在打印之后，
    # 于是屏幕上写着「72/72」而报告文件里写着「73/73」：同一趟跑出两个数，
    # 谁看了都得先怀疑其中一个。现在打印的数直接取自写文件的那一次统计。
    check(not console_errors, f"浏览器控制台无错误（{len(console_errors)} 条）")

    passed, total = write_report()
    print(f"\n=== 汇总：{passed}/{total} 通过 ===", flush=True)
    print(f"报告写入 {REPORT}", flush=True)

    failed = [label for ok, label in results if not ok]
    if failed:
        print("\n失败项：", flush=True)
        for label in failed:
            print(f"  - {label}", flush=True)
        return 1
    return 0


def main() -> int:
    build_fixtures()
    catalogue = Catalogue(fetch_capabilities())

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        page = context.new_page()
        attach(page)

        try:
            section_a(page, catalogue)
            section_b(page, catalogue)
            section_c(page, catalogue)
            section_d(page, catalogue)
            section_e(page, catalogue)
            section_f(page)
            section_g(page)
            section_h(page, catalogue)
            section_i(page)
        finally:
            context.close()
            browser.close()

    return summary()


if __name__ == "__main__":
    raise SystemExit(main())
