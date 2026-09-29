"""第九阶段的真机验收脚本（Playwright + 真实 Chromium + 真实产物打开）。

第九阶段把 ``/convert`` 从「四条硬编码路径」改成了**配置驱动**的能力中心，
所以这个脚本要证的与前面几个阶段不同：

1. **界面说的就是服务端说的**（§十五/§十六）—— 页面上每一个目标格式、
   每一个参数控件、每一句「只转第一帧」的说明，都必须能在
   ``/api/conversion/capabilities`` 里逐条对上。前端自己若还留着一份
   手抄的能力矩阵，这里就会当场失配。
2. **新格式真的转得出来**（§六/§七）—— 而且**产物要真打开**：图片用
   ``PIL.Image.open`` 看格式 / 尺寸 / 像素 / EXIF / DPI，DOCX 用
   ``python-docx`` 读段落，PDF 用 PyMuPDF 读页数与页面文字，ZIP 用
   ``zipfile`` 逐个成员读。只看 HTTP 200 说明不了任何事 ——
   转出一张损坏的图同样是 200。
3. **选项真的落到像素上**（§九）—— 旋转 90° 要看到宽高互换、只缩不放、
   质量 10 与 95 的体积关系、``metadata=remove`` 之后 EXIF 真的没了。
4. **多帧文件只取第一帧，并且如实说出来**（决策 C）—— 光验「只取第一帧」
   不够，界面必须**常驻**地把这件事说给用户，参数面板与结果卡上都要有。
5. **移动端真的能用**（§三十七）—— 375 / 390 / 414 三档，无横向溢出、
   按钮够大。

分段：

    A 段  能力目录是前端唯一来源
    B 段  新格式真机转换 + 产物真打开（§六/§七）
    C 段  参数面板由 options_schema 驱动（§二十二）
    D 段  图片选项真的生效（§九）
    E 段  多帧 GIF / 多页 TIFF 只取第一帧，且如实说明（决策 C）
    F 段  图片 → PDF 的排版几何（§十）
    G 段  六张 PDF 工具卡（决策 B）
    H 段  移动端 375 / 390 / 414
    I 段  安全：假扩展名 / 穿越文件名 / 绕过参数上限（§四十五/§四十六）
    J 段  所有对外文本都不泄露内部细节（§三十四）

跑法（驱动用带 Playwright 的解释器，素材与校验用后端 venv）::

    cd frontend && npm run build
    cd backend && .venv/Scripts/python -m uvicorn main:app --host 127.0.0.1 --port 8011
    python scripts/verify_phase9.py

**不要用 Vite 开发服务器（5173）跑本脚本** —— 它会把 ``/api`` 代理到它自己
配置里那个后端，两者版本不一致时结果没有意义（这一条是踩过的坑：
:8000 上还跑着旧代码时，浏览器验收会报出一批根本不存在的「回归」）。

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

# Windows 控制台默认是 GBK，直接打印「✓」会抛 UnicodeEncodeError 把整轮跑挂掉
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

WORK = pathlib.Path(tempfile.mkdtemp(prefix="filetools-verify-p9-"))
SAMPLES = WORK / "samples"
DOWNLOADS = WORK / "downloads"
REPORT = ROOT / "scripts" / "verify_phase9_report.txt"

ROUTE = "/convert"
MOBILE_WIDTHS = (375, 390, 414)
CAPABILITIES_URL = "/api/conversion/capabilities"

#: 1 磅 = 25.4/72 毫米。页边距与自定义页面尺寸都要用它换算。
MM = 72.0 / 25.4

#: 对外文本里绝不能出现的东西（与 phase8 同一张表）。
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
# 素材：一律用后端 venv 生成（它才有 Pillow / PyMuPDF）
# ----------------------------------------------------------------------

_FIXTURE_CODE = r'''
import os, sys

import pymupdf
from PIL import Image, ImageDraw

out = sys.argv[1]
os.makedirs(out, exist_ok=True)


def photo(w, h, color=(40, 90, 160)):
    """一眼能认出来的图：底色 + 白色边框 + 两条对角线。"""
    img = Image.new("RGB", (w, h), color)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, w - 1, h - 1], outline=(255, 255, 255), width=3)
    d.line([0, 0, w, h], fill=(255, 200, 0), width=5)
    d.line([w, 0, 0, h], fill=(255, 200, 0), width=5)
    return img


def tagged():
    """带 EXIF 的相机信息，用来验 metadata 的保留 / 清除。"""
    exif = Image.Exif()
    exif[0x010F] = "FileToolsCamera"      # Make
    exif[0x0110] = "FT-9"                 # Model
    exif[0x0131] = "FileTools Verifier"   # Software
    return exif


# 主图：400x300，带 EXIF
photo(400, 300).save(os.path.join(out, "photo.jpg"), format="JPEG", quality=92, exif=tagged())
photo(400, 300).save(os.path.join(out, "photo.png"), format="PNG")

# 大图：验「只缩不放」的预设。2400x1200 的长边比 small(1024) 与 medium(1600) 都大
photo(2400, 1200).save(os.path.join(out, "big.jpg"), format="JPEG", quality=92)

# 方形图：PNG->ICO 用（ICO 有最小边长要求）
photo(512, 512).save(os.path.join(out, "square.png"), format="PNG")

# 宽图：验填充 / 适应的落点差异
photo(600, 200).save(os.path.join(out, "wide.jpg"), format="JPEG", quality=92)

# 多帧 GIF：第一帧红、第二帧蓝。产物必须是红的 —— 只看「转出来了」看不出这个
red = photo(200, 150, (220, 40, 40))
blue = photo(200, 150, (40, 40, 220))
red.save(os.path.join(out, "anim.gif"), format="GIF", save_all=True,
         append_images=[blue], duration=200, loop=0)

# 多页 TIFF：同上一黑一白两页
red.save(os.path.join(out, "scan.tiff"), format="TIFF", save_all=True,
         append_images=[blue])

# 文本 / Markdown / HTML
with open(os.path.join(out, "note.txt"), "w", encoding="utf-8") as fh:
    fh.write("FileTools 第九阶段验收\n第二行：TXT 转 DOCX / HTML / MD 都要走通。\n")

with open(os.path.join(out, "doc.md"), "w", encoding="utf-8") as fh:
    fh.write(
        "# FileTools 第九阶段\n\n"
        "这是一段**粗体**与*斜体*混排的正文。\n\n"
        "- 第一项\n- 第二项\n\n"
        "> 引用一行\n\n"
        "```\ncode block\n```\n"
    )

with open(os.path.join(out, "page.html"), "w", encoding="utf-8") as fh:
    fh.write(
        "<!doctype html><html><head><meta charset='utf-8'><title>验收页</title></head>"
        "<body><h1>FileTools 第九阶段验收页</h1>"
        "<p>HTML 转 PDF 与 HTML 转 TXT 都要走通。</p>"
        "<ul><li>甲</li><li>乙</li></ul></body></html>"
    )


def make_pdf(name, pages, label):
    """每一页写上「DOC1-2」这样的文字，合并 / 拆分才能靠抽文字验顺序。"""
    doc = pymupdf.open()
    for index in range(1, pages + 1):
        page = doc.new_page(width=595, height=842)
        page.insert_text((72, 120), f"{label}-{index}", fontsize=24)
    doc.save(os.path.join(out, name))
    doc.close()


make_pdf("doc1.pdf", 2, "DOC1")
make_pdf("doc2.pdf", 1, "DOC2")
make_pdf("doc3.pdf", 3, "DOC3")

# 假扩展名：内容其实是 PDF
with open(os.path.join(out, "fake.png"), "wb") as fh:
    fh.write(open(os.path.join(out, "doc2.pdf"), "rb").read())
'''


def find_backend_python() -> pathlib.Path:
    """后端 venv 的 python —— 用它生成素材、打开下载结果。"""
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
    utf-8 读，只要子进程打印一个汉字就在**读取线程**里 UnicodeDecodeError，
    于是 ``communicate()`` 拿回 ``stdout=None``，真正的报错被一起吃掉。
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

_INSPECT_IMAGE = r'''
import json, sys
from PIL import Image

path = sys.argv[1]
with Image.open(path) as img:
    width, height = img.size
    dpi = img.info.get("dpi")
    data = {
        "format": img.format,
        "width": width,
        "height": height,
        "mode": img.mode,
        "frames": getattr(img, "n_frames", 1),
        "exif_keys": sorted(dict(img.getexif()).keys()),
        "dpi": [round(float(dpi[0]), 1), round(float(dpi[1]), 1)] if dpi else None,
        # 取样点要避开素材自己画的白边框与两条对角线（它们在正中交叉），
        # 否则「第一帧是不是红的」会取到黄色，报出一个根本不存在的失败
        "probe": list(
            img.convert("RGB").getpixel((round(width * 0.3), round(height * 0.5)))
        ),
    }
print(json.dumps(data))
'''

_INSPECT_PDF = r'''
import json, sys
import pymupdf

doc = pymupdf.open(sys.argv[1])
pages = []
for page in doc:
    rect = page.rect
    images = page.get_images(full=True)
    pages.append({
        "width": round(rect.width, 1),
        "height": round(rect.height, 1),
        "text": page.get_text().strip(),
        "images": len(images),
    })
data = {"pages": len(doc), "page_list": pages}
doc.close()
print(json.dumps(data))
'''

_INSPECT_DOCX = r'''
import json, sys

import docx

document = docx.Document(sys.argv[1])
paragraphs = [p.text for p in document.paragraphs]
# Normal 样式的东亚字体：build_docx 目前只设置这一个
normal = document.styles["Normal"]
rpr = normal.element.find(
    "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}rPr"
)
fonts = {}
if rpr is not None:
    rfonts = rpr.find(
        "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}rFonts"
    )
    if rfonts is not None:
        for key, value in rfonts.attrib.items():
            fonts[key.split("}")[-1]] = value
print(json.dumps({"paragraphs": paragraphs, "fonts": fonts}))
'''

_INSPECT_ZIP = r'''
import json, sys, zipfile
import pymupdf

names = []
with zipfile.ZipFile(sys.argv[1]) as archive:
    for info in archive.infolist():
        item = {"name": info.filename, "size": info.file_size}
        if info.filename.lower().endswith(".pdf"):
            data = archive.read(info)
            doc = pymupdf.open(stream=data, filetype="pdf")
            item["pages"] = len(doc)
            item["text"] = " ".join(p.get_text().strip() for p in doc)
            doc.close()
        names.append(item)
print(json.dumps(names))
'''

_INSPECT_TEXT = r'''
import json, sys

with open(sys.argv[1], "rb") as fh:
    raw = fh.read()
print(json.dumps({"text": raw.decode("utf-8", errors="replace")}))
'''


def _inspect(code: str, path: pathlib.Path):
    return json.loads(_run_backend_python(code, path))


def inspect_image(path: pathlib.Path) -> dict:
    return _inspect(_INSPECT_IMAGE, path)


def inspect_pdf(path: pathlib.Path) -> dict:
    return _inspect(_INSPECT_PDF, path)


def inspect_docx(path: pathlib.Path) -> dict:
    return _inspect(_INSPECT_DOCX, path)


def inspect_zip(path: pathlib.Path) -> list[dict]:
    return _inspect(_INSPECT_ZIP, path)


def inspect_text(path: pathlib.Path) -> str:
    return _inspect(_INSPECT_TEXT, path)["text"]


# ----------------------------------------------------------------------
# 能力目录：直接从服务端拿，测试里**不抄一份**
# ----------------------------------------------------------------------

def fetch_capabilities() -> dict:
    with urllib.request.urlopen(f"{BASE}{CAPABILITIES_URL}", timeout=30) as response:
        return json.load(response)


def fetch_json(path: str):
    try:
        with urllib.request.urlopen(f"{BASE}{path}", timeout=30) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8"))


# ----------------------------------------------------------------------
# 浏览器小工具
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
    """把素材塞进 file input。``scope`` 给定时只在那个卡片里找。"""
    target = (scope or page).locator("input[type=file]").first
    target.set_input_files([str(SAMPLES / name) for name in names])
    page.wait_for_timeout(400)


def upload_raw(page: Page, name: str, content: bytes, mime: str, scope=None) -> None:
    """塞一个**文件名与内容都由测试指定**的文件（安全那一段要用）。"""
    target = (scope or page).locator("input[type=file]").first
    target.set_input_files([{"name": name, "mimeType": mime, "buffer": content}])
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
    page.wait_for_timeout(200)


def set_option(page: Page, key: str, value: str, scope=None) -> None:
    """设置一项参数。控件形状由 schema 决定，所以四种都试一遍。

    键里带点（``resize.mode``），选择器一律加引号。

    ``scope`` 是控件所在的容器：PDF 工具卡用 ``div.card``，六张卡同页，
    不限定范围的话 ``[name^=...]`` 会跨卡命中第一张的同名控件。
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


def group_card(page: Page, filename: str):
    """按文件名定位那一组的卡片。"""
    return page.locator("section.card").filter(has_text=filename).first


def wait_group_done(page: Page, filename: str, timeout: int = 240_000) -> str:
    """等这一组出结果或出错，返回卡片里的文字。出错立刻返回，不干等。"""
    card = group_card(page, filename)
    card.locator("button", has_text=re.compile("下载结果|打包下载")).first.wait_for(
        timeout=timeout
    )
    page.wait_for_timeout(300)
    return card.inner_text()


_download_seq = 0


def download_in(page: Page, scope, pattern: str = "下载结果|打包下载") -> pathlib.Path:
    """点一个下载按钮并把文件存下来。"""
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


def convert_via_ui(
    page: Page,
    filename: str,
    target: str,
    options: dict[str, str] | None = None,
    timeout: int = 240_000,
) -> pathlib.Path:
    """走一遍真实的界面流程：上传 → 选目标 → 调参数 → 开始 → 下载。

    每一趟都重新进页面，保证「同页多组」不会互相干扰 ——
    这个脚本验的是单组路径，多组并发由 phase7 覆盖。
    """
    page.goto(f"{BASE}{ROUTE}", wait_until="networkidle")
    upload(page, filename)
    pick_target(page, target)
    for key, value in (options or {}).items():
        set_option(page, key, value)
    page.locator("button", has_text=re.compile("^开始转换这")).first.click()
    wait_group_done(page, filename, timeout=timeout)
    return download_in(page, group_card(page, filename))


def operation_card(page: Page, name: str):
    """按标题定位一张 PDF 工具卡。工具卡是 ``div.card``，与分组卡的
    ``section.card`` 不同标签，所以不会互相串。"""
    return page.locator("div.card").filter(
        has=page.get_by_role("heading", name=name, exact=True)
    ).first


def run_operation(
    page: Page,
    name: str,
    files: list[str],
    options: dict[str, str] | None = None,
    timeout: int = 240_000,
) -> pathlib.Path:
    """走一遍 PDF 工具卡的流程：上传 → 调参数 → 开始 → 下载。"""
    page.goto(f"{BASE}{ROUTE}", wait_until="networkidle")
    card = operation_card(page, name)
    card.scroll_into_view_if_needed()
    upload(page, *files, scope=card)
    for key, value in (options or {}).items():
        set_option(page, key, value, scope=card)
    card.locator("button", has_text="开始处理").first.click()
    card.locator("button", has_text=re.compile("下载结果")).first.wait_for(timeout=timeout)
    page.wait_for_timeout(300)
    return download_in(page, card, "下载结果")


# ----------------------------------------------------------------------
# A 段：能力目录是前端唯一来源（§十五 / §十六）
# ----------------------------------------------------------------------

def section_a(page: Page, caps: dict) -> None:
    section("A 段：能力目录是前端唯一来源")

    hits: list[str] = []
    page.on(
        "request",
        lambda request: hits.append(request.url)
        if CAPABILITIES_URL in request.url
        else None,
    )
    page.goto(f"{BASE}{ROUTE}", wait_until="networkidle")
    page.wait_for_timeout(800)
    check(len(hits) == 1, f"页面只向 {CAPABILITIES_URL} 要了一次能力目录（{len(hits)} 次）")

    # 上传一张 JPG，看它给出的目标格式是不是恰好等于服务端登记的
    upload(page, "photo.jpg")
    page.wait_for_timeout(500)

    offered = page.eval_on_selector_all(
        "input[name^='conversion-target-']",
        "els => els.map(e => e.value)",
    )
    served = [entry["target_type"] for entry in caps["conversions"]]
    check(
        len(offered) > 0 and all(value in served for value in offered),
        f"界面给出的 {len(offered)} 个目标格式全部登记在 capabilities 里",
    )

    # 服务端为 jpg 登记了多少条**可用**能力，界面就得给出多少个单选项
    # （一条不多一条不少）。不可用的按转换中心的规矩根本不画出来
    jpg_entries = [
        entry
        for entry in caps["conversions"]
        if entry["source_type"] == "jpg" and entry["available"]
    ]
    check(
        len(offered) == len(jpg_entries),
        f"JPG 的目标格式数量与能力目录一致（界面 {len(offered)}，服务端可用 {len(jpg_entries)}）",
    )

    # 「推荐」与「其他格式」的分栏必须照条目的 recommended 标签来
    page_text = body(page)
    has_recommended = any("recommended" in entry["tags"] for entry in jpg_entries)
    has_others = any("recommended" not in entry["tags"] for entry in jpg_entries)
    check(
        ("推荐" in page_text) == has_recommended,
        "「推荐」一栏的出现与服务端打的 recommended 标签一致",
    )
    check(
        ("其他格式" in page_text) == has_others,
        "「其他格式」一栏如实呈现剩下的目标",
    )


# ----------------------------------------------------------------------
# B 段：新格式真机转换（§六 / §七）
# ----------------------------------------------------------------------

def section_b(page: Page) -> None:
    section("B 段：新格式真机转换 + 产物真打开")

    # --- 图片家族：六种输入 -> 六种输出里的代表组合
    cases = [
        ("photo.jpg", "png", "PNG", (400, 300)),
        ("photo.jpg", "webp", "WEBP", (400, 300)),
        ("photo.jpg", "bmp", "BMP", (400, 300)),
        ("photo.jpg", "gif", "GIF", (400, 300)),
        ("photo.jpg", "tiff", "TIFF", (400, 300)),
    ]
    for source, target, expected_format, size in cases:
        try:
            path = convert_via_ui(page, source, target)
            data = inspect_image(path)
            check(
                data["format"] == expected_format and (data["width"], data["height"]) == size,
                f"{source} → {target}：产物真是 {data['format']} {data['width']}×{data['height']}",
            )
        except Exception as exc:  # noqa: BLE001 - 验收脚本要把失败报成一行，不能整轮挂掉
            check(False, f"{source} → {target} 跑通（{type(exc).__name__}: {exc}）")

    # --- PNG → ICO（§七 只此一条，决策 F）
    try:
        path = convert_via_ui(page, "square.png", "ico")
        data = inspect_image(path)
        check(
            data["format"] == "ICO" and data["width"] == data["height"]
            and data["width"] in (16, 32, 48, 64, 128, 256),
            f"PNG → ICO：产物真是 {data['format']} {data['width']}×{data['height']}",
        )
    except Exception as exc:  # noqa: BLE001
        check(False, f"PNG → ICO 跑通（{type(exc).__name__}: {exc}）")

    # --- 文本家族：TXT → DOCX / HTML / MD
    try:
        path = convert_via_ui(page, "note.txt", "docx")
        data = inspect_docx(path)
        joined = "\n".join(data["paragraphs"])
        check(
            "FileTools 第九阶段验收" in joined,
            f"TXT → DOCX：python-docx 读到的段落含原文（{len(data['paragraphs'])} 段）",
        )
        check(
            bool(data["fonts"]),
            f"TXT → DOCX：Normal 样式真的设了字体（{data['fonts']}）",
        )
    except Exception as exc:  # noqa: BLE001
        check(False, f"TXT → DOCX 跑通（{type(exc).__name__}: {exc}）")

    try:
        path = convert_via_ui(page, "note.txt", "html")
        text = inspect_text(path)
        check(
            "<" in text and "FileTools 第九阶段验收" in text,
            "TXT → HTML：产物是 HTML 且含原文",
        )
    except Exception as exc:  # noqa: BLE001
        check(False, f"TXT → HTML 跑通（{type(exc).__name__}: {exc}）")

    try:
        path = convert_via_ui(page, "note.txt", "md")
        text = inspect_text(path)
        check("FileTools 第九阶段验收" in text, "TXT → MD：产物含原文")
    except Exception as exc:  # noqa: BLE001
        check(False, f"TXT → MD 跑通（{type(exc).__name__}: {exc}）")

    # --- Markdown：→ HTML / PDF / TXT
    try:
        path = convert_via_ui(page, "doc.md", "html")
        text = inspect_text(path)
        check(
            "<h1" in text.lower() and "第九阶段" in text
            and "<strong>" in text and "<em>" in text
            and "<blockquote>" in text
            and "<pre><code>code block</code></pre>" in text,
            "MD → HTML：标题 / 强调 / 引用 / 代码块都真的渲染成了标签",
        )
        # 列表在 IR 里是**段落级**的（段落 + 记号），不是嵌套的 <ul>/<li>；
        # 所以这里钉的是「两项都在、各自成一个列表段落」，不是 <li> 的个数。
        check(
            text.count('class="list-item"') == 2
            and "第一项" in text
            and "第二项" in text,
            "MD → HTML：两条列表项各自成为一个列表段落",
        )
        # 代码块只该有一层 <code>（曾经出来的是 <pre><code><code>…）
        check(
            "<code><code>" not in text,
            "MD → HTML：代码块没有被包成两层 <code>",
        )
    except Exception as exc:  # noqa: BLE001
        check(False, f"MD → HTML 跑通（{type(exc).__name__}: {exc}）")

    try:
        path = convert_via_ui(page, "doc.md", "pdf")
        data = inspect_pdf(path)
        text = " ".join(page_["text"] for page_ in data["page_list"])
        check(
            data["pages"] >= 1 and "FileTools" in text,
            f"MD → PDF：PyMuPDF 打开得到 {data['pages']} 页且抽得到标题文字",
        )
    except Exception as exc:  # noqa: BLE001
        check(False, f"MD → PDF 跑通（{type(exc).__name__}: {exc}）")

    try:
        path = convert_via_ui(page, "doc.md", "txt")
        text = inspect_text(path)
        check(
            "FileTools" in text and "#" not in text,
            "MD → TXT：标题文字在，Markdown 记号没了",
        )
    except Exception as exc:  # noqa: BLE001
        check(False, f"MD → TXT 跑通（{type(exc).__name__}: {exc}）")

    # --- HTML：→ PDF / TXT
    try:
        path = convert_via_ui(page, "page.html", "pdf")
        data = inspect_pdf(path)
        text = " ".join(page_["text"] for page_ in data["page_list"])
        check(
            data["pages"] >= 1 and "HTML 转 PDF" in text,
            f"HTML → PDF：PyMuPDF 打开得到 {data['pages']} 页且抽得到正文",
        )
    except Exception as exc:  # noqa: BLE001
        check(False, f"HTML → PDF 跑通（{type(exc).__name__}: {exc}）")

    try:
        path = convert_via_ui(page, "page.html", "txt")
        text = inspect_text(path)
        check(
            "HTML 转 PDF" in text and "<p>" not in text,
            "HTML → TXT：正文在，标签没了",
        )
    except Exception as exc:  # noqa: BLE001
        check(False, f"HTML → TXT 跑通（{type(exc).__name__}: {exc}）")


# ----------------------------------------------------------------------
# C 段：参数面板由 options_schema 驱动（§二十二）
# ----------------------------------------------------------------------

def section_c(page: Page, caps: dict) -> None:
    section("C 段：参数面板由 options_schema 驱动")

    def schema_items(capability_id: str) -> list[dict]:
        entry = next(item for item in caps["conversions"] if item["id"] == capability_id)
        return list((entry["options_schema"] or {}).get("items", []))

    def schema_labels(capability_id: str) -> list[str]:
        return [item["label"] for item in schema_items(capability_id)]

    def visible_at_defaults(item: dict, items: list[dict]) -> bool:
        """照 ``visible_when`` 的语义，在默认取值下这一项该不该画出来。

        面板是按 schema 长的，所以「该画的都画了」只是一半 ——
        「不该画的确实没画」是另一半，少了它 ``visible_when`` 就是死字段：
        默认「保持原尺寸」时却摆着宽度/高度两个输入框，用户会以为
        不填就不生效，填了才发现被忽略。
        """
        for key, expected in (item.get("visible_when") or {}).items():
            parent = next((other for other in items if other["key"] == key), None)
            if parent is None or parent.get("default") != expected:
                return False
        return True

    def panel_legends(page: Page) -> set[str]:
        """面板上每个参数项自己的名字。

        两种来源：枚举 / 数值 / 文本项用 ``<legend>``；布尔项没有 legend，
        名字在 ``fieldset`` 直接子标签的 ``<span>`` 里（复选框的 ``<input>``
        排在它前面，所以不能按 ``:first-child`` 找）。
        只取**直接子标签**，单选组里那些取值标签就不会混进来。
        """
        return set(
            page.eval_on_selector_all(
                "section.card fieldset legend, section.card fieldset > label span",
                "els => els.map(e => e.innerText.trim()).filter(Boolean)",
            )
        )

    page.goto(f"{BASE}{ROUTE}", wait_until="networkidle")
    upload(page, "photo.jpg")
    pick_target(page, "png")
    page.wait_for_timeout(400)
    legends = panel_legends(page)
    items = schema_items("image.jpg-to-png")
    shown = [item["label"] for item in items if visible_at_defaults(item, items)]
    hidden = [item["label"] for item in items if not visible_at_defaults(item, items)]
    check(
        all(label in legends for label in shown),
        f"JPG → PNG 面板画出了 schema 在默认取值下该画的每一项"
        f"（{len(shown)}/{len(items)} 项，缺 {[l for l in shown if l not in legends] or '无'}）",
    )
    # 反过来也要对：``visible_when`` 不满足的项一个都不许冒出来
    check(
        not any(label in legends for label in hidden),
        f"JPG → PNG 面板没有画 visible_when 还没满足的项"
        f"（应藏 {len(hidden)} 项：{'、'.join(hidden)}）",
    )
    check(
        "图片质量" not in legends,
        "JPG → PNG 面板上没有「图片质量」（无损目标不该出现无意义的旋钮）",
    )

    # 把「尺寸」切到「自定义」，被 visible_when 压住的宽度/高度必须当场出现 ——
    # 这条是上面那条的正面证明：不是永远不画，是等着前置项满足
    set_option(page, "resize.mode", "custom")
    page.wait_for_timeout(300)
    legends = panel_legends(page)
    check(
        "宽度" in legends and "高度" in legends,
        "「尺寸」切到「自定义」后，宽度与高度两个输入项才出现",
    )

    page.goto(f"{BASE}{ROUTE}", wait_until="networkidle")
    upload(page, "photo.jpg")
    pick_target(page, "webp")
    page.wait_for_timeout(400)
    legends = panel_legends(page)
    check(
        "图片质量" in legends and "图片质量" in schema_labels("image.jpg-to-webp"),
        "JPG → WEBP 面板上有「图片质量」，与 schema 一致",
    )

    # 字体枚举是**运行期**由服务端填的（服务器上真装了的字），
    # 界面必须一字不差地照搬，不能自己写一份名单
    served_fonts = []
    for entry in caps["conversions"]:
        if entry["id"] != "document.txt-to-pdf":
            continue
        for item in (entry["options_schema"] or {}).get("items", []):
            if item["key"] == "font":
                served_fonts = [choice["label"] for choice in item.get("enum", [])]
    check(len(served_fonts) >= 2, f"服务端为「字体」填了 {len(served_fonts)} 个可选值")

    page.goto(f"{BASE}{ROUTE}", wait_until="networkidle")
    upload(page, "note.txt")
    pick_target(page, "pdf")
    page.wait_for_timeout(500)
    # 控件是下拉框还是单选组由取值个数与标签长度决定，两种都数一遍
    drawn_fonts = page.eval_on_selector_all(
        "select[name^='conversion-font-'] option, "
        "label:has(input[name^='conversion-font-']) span",
        "els => els.map(e => (e.textContent || '').trim()).filter(Boolean)",
    )
    panel_text = body(page)
    check(
        bool(served_fonts) and all(font in panel_text for font in served_fonts),
        f"TXT → PDF 的字体选项逐个来自服务端（{len(served_fonts)} 个）",
    )
    check(
        len(drawn_fonts) == len(served_fonts),
        f"字体控件画出的取值个数与服务端一致（界面 {len(drawn_fonts)}，服务端 {len(served_fonts)}）",
    )


# ----------------------------------------------------------------------
# D 段：图片选项真的生效（§九）
# ----------------------------------------------------------------------

def section_d(page: Page) -> None:
    section("D 段：图片选项真的生效")

    try:
        path = convert_via_ui(page, "photo.jpg", "png", {"rotation": "90"})
        data = inspect_image(path)
        check(
            (data["width"], data["height"]) == (300, 400),
            f"旋转 90°：400×300 变成 {data['width']}×{data['height']}",
        )
    except Exception as exc:  # noqa: BLE001
        check(False, f"旋转 90° 生效（{type(exc).__name__}: {exc}）")

    try:
        path = convert_via_ui(page, "big.jpg", "png", {"resize.mode": "small"})
        data = inspect_image(path)
        check(
            max(data["width"], data["height"]) == 1024,
            f"预设「小」：长边收到 1024（实际 {max(data['width'], data['height'])}）",
        )
    except Exception as exc:  # noqa: BLE001
        check(False, f"预设「小」生效（{type(exc).__name__}: {exc}）")

    try:
        # 小图选「小」不应该被放大 —— 只缩不放
        path = convert_via_ui(page, "photo.jpg", "png", {"resize.mode": "small"})
        data = inspect_image(path)
        check(
            (data["width"], data["height"]) == (400, 300),
            "只缩不放：小图选「小」尺寸不变，没有被放大",
        )
    except Exception as exc:  # noqa: BLE001
        check(False, f"只缩不放（{type(exc).__name__}: {exc}）")

    try:
        low_path = convert_via_ui(page, "photo.jpg", "webp", {"quality": "10"})
        high_path = convert_via_ui(page, "photo.jpg", "webp", {"quality": "95"})
        low = inspect_image(low_path)
        high = inspect_image(high_path)
        check(
            low["format"] == "WEBP" and high["format"] == "WEBP",
            "质量 10 与 95 都转出了 WEBP",
        )
        # 同一张图，质量越低体积越小 —— 滑杆真的落到了编码器上
        check(
            low_path.stat().st_size < high_path.stat().st_size,
            f"质量 10 比 95 小（{low_path.stat().st_size} < {high_path.stat().st_size} 字节）",
        )
    except Exception as exc:  # noqa: BLE001
        check(False, f"质量选项生效（{type(exc).__name__}: {exc}）")

    try:
        kept = inspect_image(convert_via_ui(page, "photo.jpg", "png", {"metadata": "keep"}))
        removed = inspect_image(
            convert_via_ui(page, "photo.jpg", "png", {"metadata": "remove"})
        )
        check(
            len(kept["exif_keys"]) > 0,
            f"metadata=keep：产物真的带着 EXIF（{len(kept['exif_keys'])} 项）",
        )
        check(
            len(removed["exif_keys"]) == 0,
            f"metadata=remove：产物的 EXIF 清干净了（{len(removed['exif_keys'])} 项）",
        )
    except Exception as exc:  # noqa: BLE001
        check(False, f"metadata 选项生效（{type(exc).__name__}: {exc}）")

    try:
        data = inspect_image(convert_via_ui(page, "photo.jpg", "png", {"dpi": "300"}))
        check(
            data["dpi"] is not None and abs(data["dpi"][0] - 300) < 2,
            f"DPI=300：产物记下了 {data['dpi']}",
        )
    except Exception as exc:  # noqa: BLE001
        check(False, f"DPI 选项生效（{type(exc).__name__}: {exc}）")


# ----------------------------------------------------------------------
# E 段：多帧只取第一帧，并如实说明（决策 C）
# ----------------------------------------------------------------------

def section_e(page: Page, caps: dict) -> None:
    section("E 段：多帧 GIF / 多页 TIFF 只取第一帧，且常驻说明")

    def note_of(capability_id: str) -> str:
        entry = next(
            (item for item in caps["conversions"] if item["id"] == capability_id), None
        )
        return (entry or {}).get("note") or ""

    gif_note = note_of("image.gif-to-png")
    tiff_note = note_of("image.tiff-to-png")
    jpg_note = note_of("image.jpg-to-png")
    check(bool(gif_note) and "第一帧" in gif_note, f"服务端为 GIF 源登记了说明：{gif_note}")
    check(bool(tiff_note) and "第一页" in tiff_note, f"服务端为 TIFF 源登记了说明：{tiff_note}")
    check(jpg_note == "", "单帧来源（JPG）不带这句说明 —— 不该无差别地吓唬用户")

    # 源图第一帧是红的、第二帧是蓝的。产物是红的才叫「只取第一帧」
    for source, target, note, first_color in (
        ("anim.gif", "png", gif_note, "红"),
        ("scan.tiff", "png", tiff_note, "红"),
    ):
        try:
            page.goto(f"{BASE}{ROUTE}", wait_until="networkidle")
            upload(page, source)
            pick_target(page, target)
            page.wait_for_timeout(500)
            panel = body(page)
            check(
                note in panel,
                f"{source}：参数面板上常驻着「{note[:18]}…」",
            )

            page.locator("button", has_text=re.compile("^开始转换这")).first.click()
            wait_group_done(page, source)
            settled = group_card(page, source).inner_text()
            check(
                note in settled,
                f"{source}：结果卡上也给了同一句说明",
            )
            path = download_in(page, group_card(page, source))
            data = inspect_image(path)
            red, green, blue = data["probe"]
            check(
                red > 150 and green < 100 and blue < 100,
                f"{source}：产物是第一帧（{first_color}），不是第二帧（取样 {data['probe']}）",
            )
        except Exception as exc:  # noqa: BLE001
            check(False, f"{source} 只取第一帧（{type(exc).__name__}: {exc}）")


# ----------------------------------------------------------------------
# F 段：图片 → PDF 的排版几何（§十）
# ----------------------------------------------------------------------

def section_f(page: Page) -> None:
    section("F 段：图片 → PDF 的排版几何")

    name = "图片合成 PDF"

    try:
        path = run_operation(
            page, name, ["photo.jpg"], {"page_size": "auto", "margin": "medium"}
        )
        data = inspect_pdf(path)
        first = data["page_list"][0]
        expected = round(400 + 10 * MM * 2, 1)
        check(
            abs(first["width"] - expected) <= 2 and abs(first["height"] - round(300 + 10 * MM * 2, 1)) <= 2,
            f"原图尺寸 + 10 毫米页边距：{first['width']}×{first['height']}（期望 {expected}）",
        )
    except Exception as exc:  # noqa: BLE001
        check(False, f"图片合成 PDF 的页边距（{type(exc).__name__}: {exc}）")

    # A4 有两条规则要分开验：
    # 「跟随图片」时横向图（photo.jpg 是 400×300）要出**横向** A4；
    # 明确指定纵向时才是 595.3×841.9。把两条混在一起看，就分不清
    # 「方向真的跟着图片走了」还是「方向参数根本没生效」。
    try:
        path = run_operation(
            page, name, ["photo.jpg"], {"page_size": "a4", "orientation": "auto", "margin": "none"}
        )
        data = inspect_pdf(path)
        first = data["page_list"][0]
        check(
            abs(first["width"] - 841.9) <= 2 and abs(first["height"] - 595.3) <= 2,
            f"A4 + 方向跟随图片：横向图出横向页 {first['width']}×{first['height']}"
            f"（期望 841.9×595.3）",
        )
    except Exception as exc:  # noqa: BLE001
        check(False, f"图片合成 PDF 的 A4（{type(exc).__name__}: {exc}）")

    try:
        path = run_operation(
            page, name, ["photo.jpg"], {"page_size": "a4", "orientation": "portrait", "margin": "none"}
        )
        data = inspect_pdf(path)
        first = data["page_list"][0]
        check(
            abs(first["width"] - 595.3) <= 2 and abs(first["height"] - 841.9) <= 2,
            f"A4 + 指定纵向：横向图也出纵向页 {first['width']}×{first['height']}"
            f"（期望 595.3×841.9）",
        )
    except Exception as exc:  # noqa: BLE001
        check(False, f"图片合成 PDF 指定纵向（{type(exc).__name__}: {exc}）")

    try:
        path = run_operation(page, name, ["photo.jpg", "wide.jpg"], {"page_size": "a4"})
        data = inspect_pdf(path)
        check(
            data["pages"] == 2 and all(p["images"] == 1 for p in data["page_list"]),
            f"两张图合成 {data['pages']} 页，每页一张图（每图一页）",
        )
    except Exception as exc:  # noqa: BLE001
        check(False, f"两张图合成一份 PDF（{type(exc).__name__}: {exc}）")


# ----------------------------------------------------------------------
# G 段：工具卡（决策 B）
# ----------------------------------------------------------------------

def section_g(page: Page, caps: dict) -> None:
    section("G 段：工具卡")

    page.goto(f"{BASE}{ROUTE}", wait_until="networkidle")

    # 期望顺序要按**页面自己的规则**算，不能直接拿 ``caps["operations"]`` 比。
    #
    # ``ConversionOperations.tsx`` 是按**类别分组**画的：先按能力目录里的类别
    # 顺序（图片 → 文档 → PDF）分组，组内保持服务端登记顺序。而
    # ``caps["operations"]`` 是**登记顺序**，两者不是一回事。
    #
    # 第九阶段两种顺序碰巧一致 —— 那时全部 operation 都是 PDF 类，只有一组，
    # 分组等于不分组。第十阶段 A 加了第一条图片类 operation（查看图片元数据），
    # 这条断言才第一次红：页面把它画在图片组（最前），登记顺序里它在最后。
    #
    # 这里仍然断言**严格相等**（不是 ``set`` 比较、不是超集）：顺序对界面是有
    # 意义的，顺序错了就该红。只是期望值从「登记顺序」改成「页面分组规则下的
    # 顺序」——把断言从一句偶然成立的假设，改成真正的界面契约。
    order = {category["value"]: index for index, category in enumerate(caps["categories"])}
    served = [
        entry["display_name"]
        for entry in sorted(
            caps["operations"], key=lambda e: order.get(e["category"], len(order))
        )
    ]
    drawn = page.eval_on_selector_all(
        "div.card h3", "els => els.map(e => e.innerText.trim())"
    )
    check(
        drawn == served,
        f"页面画出的工具卡与服务端登记的一一对应（{len(drawn)} 张：{'、'.join(drawn)}）",
    )

    # 每张卡只收自己那几种输入（``accepts`` 是服务端声明的）：喂错文件，
    # 卡片会把它挡在外面，于是「开始处理」根本不出现 —— 量到的是自己喂错了，
    # 不是产品缺按钮。所以按 accepts 挑素材，不写死「都喂 PDF」。
    fixtures_by_type = {"pdf": "doc1.pdf", "jpg": "photo.jpg", "png": "photo.png"}

    def fixture_for(entry: dict) -> str:
        for accepted in entry["accepts"]:
            if accepted in fixtures_by_type:
                return fixtures_by_type[accepted]
        raise AssertionError(
            f"{entry['id']} 声明收 {entry['accepts']}，脚本没有对应素材"
        )

    # 取消按钮只在**选了文件之后**才画（没文件时没有可取消的东西）。
    # 所以每张卡都先放一个文件进去，再问它有没有取消 —— 否则量的是空状态，
    # 那个状态本来就该没有按钮。
    missing_cancel = []
    for entry in caps["operations"]:
        display = entry["display_name"]
        card = operation_card(page, display)
        card.scroll_into_view_if_needed()
        upload(page, fixture_for(entry), scope=card)
        card.locator("button", has_text="开始处理").first.wait_for(timeout=30_000)
        if card.locator("button", has_text=re.compile("取消")).count() == 0:
            missing_cancel.append(display)
    check(not missing_cancel, f"每张卡选好文件后都有取消按钮（缺：{missing_cancel or '无'}）")

    # 必填项没填时不该能开始（提取 / 删除页面的「页面」是必填）。
    # 先回一趟干净的页面：上面每张卡都塞了文件，而有文件时
    # ``PdfOperationCard`` 就把拖拽区换成文件列表了，``input[type=file]``
    # 已经不在 DOM 里 —— 再往它上面传文件会等到超时。
    page.goto(f"{BASE}{ROUTE}", wait_until="networkidle")
    card = operation_card(page, "PDF 页面提取")
    card.scroll_into_view_if_needed()
    upload(page, "doc3.pdf", scope=card)
    card.locator("button", has_text="开始处理").first.wait_for(timeout=30_000)
    check(
        card.locator("button", has_text="开始处理").first.is_disabled(),
        "「页面提取」没填页码时开始按钮是禁用的",
    )
    check(
        "还有必填项没填" in card.inner_text(),
        "「页面提取」如实说明还差哪一项",
    )

    # --- 合并：三份 PDF 共 6 页，且顺序必须对
    try:
        path = run_operation(page, "PDF 合并", ["doc1.pdf", "doc2.pdf", "doc3.pdf"])
        data = inspect_pdf(path)
        texts = [p["text"] for p in data["page_list"]]
        expected = ["DOC1-1", "DOC1-2", "DOC2-1", "DOC3-1", "DOC3-2", "DOC3-3"]
        check(
            data["pages"] == 6 and texts == expected,
            f"合并：{data['pages']} 页且顺序为 {texts}",
        )
    except Exception as exc:  # noqa: BLE001
        check(False, f"PDF 合并（{type(exc).__name__}: {exc}）")

    # --- 拆分：每页一个，装进 ZIP
    try:
        path = run_operation(page, "PDF 拆分", ["doc3.pdf"])
        members = inspect_zip(path)
        check(
            len(members) == 3 and all(item.get("pages") == 1 for item in members),
            f"拆分：ZIP 里 {len(members)} 个成员，每个 1 页",
        )
        check(
            all("/" not in item["name"] and ".." not in item["name"] for item in members),
            "拆分的 ZIP 里没有路径穿越成员",
        )
    except Exception as exc:  # noqa: BLE001
        check(False, f"PDF 拆分（{type(exc).__name__}: {exc}）")

    # --- 压缩：页数与页面尺寸都不该变
    try:
        path = run_operation(page, "PDF 压缩", ["doc3.pdf"])
        data = inspect_pdf(path)
        check(
            data["pages"] == 3 and all(p["text"] for p in data["page_list"]),
            f"压缩：仍是 {data['pages']} 页且文字还在",
        )
    except Exception as exc:  # noqa: BLE001
        check(False, f"PDF 压缩（{type(exc).__name__}: {exc}）")

    # --- 提取：只留第 1、3 页
    try:
        path = run_operation(page, "PDF 页面提取", ["doc3.pdf"], {"pages": "1,3"})
        data = inspect_pdf(path)
        texts = [p["text"] for p in data["page_list"]]
        check(
            data["pages"] == 2 and texts == ["DOC3-1", "DOC3-3"],
            f"提取：得到 {data['pages']} 页且内容为 {texts}",
        )
    except Exception as exc:  # noqa: BLE001
        check(False, f"PDF 页面提取（{type(exc).__name__}: {exc}）")

    # --- 删除：删掉第 1 页
    try:
        path = run_operation(page, "PDF 页面删除", ["doc3.pdf"], {"pages": "1"})
        data = inspect_pdf(path)
        texts = [p["text"] for p in data["page_list"]]
        check(
            data["pages"] == 2 and texts == ["DOC3-2", "DOC3-3"],
            f"删除：得到 {data['pages']} 页且内容为 {texts}",
        )
    except Exception as exc:  # noqa: BLE001
        check(False, f"PDF 页面删除（{type(exc).__name__}: {exc}）")


# ----------------------------------------------------------------------
# H 段：移动端（§三十七）
# ----------------------------------------------------------------------

def section_h(page: Page, caps: dict) -> None:
    section("H 段：移动端 375 / 390 / 414")

    for width in MOBILE_WIDTHS:
        page.set_viewport_size({"width": width, "height": 844})
        for route in ("/", ROUTE):
            page.goto(f"{BASE}{route}", wait_until="networkidle")
            overflow = page.evaluate(
                "() => document.documentElement.scrollWidth - document.documentElement.clientWidth"
            )
            check(overflow <= 1, f"{width}px：{route} 无横向溢出（{overflow}px）")

        # 工具卡在窄屏必须是一列（lg:grid-cols-2 到窄屏要退回一列）。
        # 判据是「所有卡片的左边距相同且都装得下」—— 两列时必然有两个左边界。
        page.goto(f"{BASE}{ROUTE}", wait_until="networkidle")
        boxes = page.eval_on_selector_all(
            "div.card",
            "els => els.map(e => { const r = e.getBoundingClientRect();"
            " return {x: Math.round(r.x), w: Math.round(r.width)} })",
        )
        lefts = {box["x"] for box in boxes}
        check(
            bool(boxes) and len(lefts) == 1,
            f"{width}px：{len(boxes)} 张工具卡排成一列（左边界 {sorted(lefts)}）",
        )
        check(
            bool(boxes) and all(box["w"] <= width for box in boxes),
            f"{width}px：工具卡都没有超出视口（最宽 {max((b['w'] for b in boxes), default=0)}px）",
        )

        # 上传一张图，量**主要操作按钮**够不够手指点。
        #
        # 只量主操作（开始转换 / 开始处理 / 下载结果 / 打包下载）。
        # 页面上还有一批 size="sm" 的次级按钮（移除 / 重置 / 全选），
        # 高 36px —— 那是第一到第八阶段一路沿用的次级尺寸，本次不动它们
        # （见报告的已知限制一节，如实写明，不假装全站按钮都 ≥44）。
        upload(page, "photo.jpg")
        page.wait_for_timeout(500)
        heights = page.eval_on_selector_all(
            "section.card button, div.card button",
            "els => els"
            ".filter(e => /开始转换|开始处理|下载结果|打包下载/.test(e.innerText))"
            ".map(e => ({t: e.innerText.trim().slice(0, 12),"
            " h: Math.round(e.getBoundingClientRect().height)}))",
        )
        short = [item for item in heights if item["h"] < 44]
        check(
            bool(heights) and not short,
            f"{width}px：{len(heights)} 个主要操作按钮都 ≥44px"
            f"（最矮 {min((i['h'] for i in heights), default=0)}px"
            f"{'，不足的：' + str(short) if short else ''}）",
        )
        page.goto(f"{BASE}{ROUTE}", wait_until="networkidle")

    page.set_viewport_size({"width": 1440, "height": 900})


# ----------------------------------------------------------------------
# I 段：安全（§四十五 / §四十六）
# ----------------------------------------------------------------------

def section_i(page: Page) -> None:
    section("I 段：安全")

    # 1) 假扩展名：内容是 PDF，名字叫 fake.png。浏览器这一层看不出来
    #    （扩展名与 MIME 都是客户端说了算，§四十五 明令不许信），
    #    服务端按真实内容判成 PDF，而 PDF 转不了 JPG —— 必须明确说出来。
    page.goto(f"{BASE}{ROUTE}", wait_until="networkidle")
    upload(page, "fake.png")
    page.wait_for_timeout(600)
    pick_target(page, "jpg")
    page.locator("button", has_text=re.compile("^开始转换这")).first.click()
    page.wait_for_timeout(4000)
    page_text = body(page)
    check(
        "暂不支持该文件格式" in page_text,
        "假扩展名（PDF 内容叫 .png）被内容校验拦下并给出中文说明",
    )
    check(
        not [token for token in FORBIDDEN_FRAGMENTS if token in page_text],
        "这条失败信息里没有任何内部细节",
    )

    # 2) 穿越文件名：上传名里带 ../，服务端既不能写出目录，下载名也要 sanitize
    page.goto(f"{BASE}{ROUTE}", wait_until="networkidle")
    with open(SAMPLES / "photo.png", "rb") as fh:
        payload = fh.read()
    upload_raw(page, "../../evil.png", payload, "image/png")
    page.wait_for_timeout(600)
    try:
        pick_target(page, "jpg")
        page.locator("button", has_text=re.compile("^开始转换这")).first.click()
        wait_group_done(page, "evil.png")
        path = download_in(page, group_card(page, "evil.png"))
        check(
            ".." not in path.name and "/" not in path.name and "\\" not in path.name,
            f"穿越文件名被 sanitize（下载得到 {path.name}）",
        )
    except Exception as exc:  # noqa: BLE001
        check(False, f"穿越文件名被安全处理（{type(exc).__name__}: {exc}）")

    # 3) 参数绕不过资源上限：schema 之外的键、以及超出上限的值
    status, payload = fetch_json(
        "/api/conversion/capabilities?source_type=jpg&target_type=png"
    )
    check(status == 200 and len(payload["conversions"]) >= 1, "能力目录支持按 source/target 过滤")

    # 4) 越界参数：直接打接口，确认服务端明确拒绝且文案干净
    import httpx  # noqa: PLC0415 - 只有这一小段需要，放在顶部会让整脚本依赖 httpx

    with httpx.Client(base_url=BASE, timeout=60) as client:
        with open(SAMPLES / "photo.jpg", "rb") as fh:
            image = fh.read()
        bad = client.post(
            "/api/conversion/tasks",
            files={"files": ("photo.jpg", image, "image/jpeg")},
            data={
                "target_type": "png",
                "capability_id": "image.jpg-to-png",
                "options": json.dumps({"resize.mode": "custom", "resize.width": 99999}),
            },
        )
        check(
            bad.status_code == 400,
            f"超出上限的宽度被拒绝（HTTP {bad.status_code}）",
        )
        message = bad.text
        check(
            "12000" in message or "宽度" in message,
            f"越界参数给出的是中文上限说明：{message[:80]}",
        )

        unknown = client.post(
            "/api/conversion/tasks",
            files={"files": ("photo.jpg", image, "image/jpeg")},
            data={
                "target_type": "png",
                "capability_id": "image.jpg-to-png",
                "options": json.dumps({"definitely.not.an.option": 1}),
            },
        )
        check(
            unknown.status_code == 400 and "不适用于该转换" in unknown.text,
            f"schema 之外的参数键被拒绝（HTTP {unknown.status_code}：{unknown.text[:70]}）",
        )

        # 枚举值也不能乱填 —— 服务端要回一句能照着改的中文，而不是抛异常
        bogus_enum = client.post(
            "/api/conversion/tasks",
            files={"files": ("photo.jpg", image, "image/jpeg")},
            data={
                "target_type": "png",
                "capability_id": "image.jpg-to-png",
                "options": json.dumps({"resize.mode": "definitely-not-a-mode"}),
            },
        )
        check(
            bogus_enum.status_code == 400
            and not [t for t in FORBIDDEN_FRAGMENTS if t in bogus_enum.text],
            f"非法枚举值被拒绝且文案干净（HTTP {bogus_enum.status_code}）",
        )


# ----------------------------------------------------------------------
# J 段：不泄露内部细节（§三十四）
# ----------------------------------------------------------------------

def section_j(caps: dict) -> None:
    section("J 段：对外文本不泄露内部细节")

    def leaked(text: str) -> list[str]:
        return [token for token in FORBIDDEN_FRAGMENTS if token in text]

    raw = json.dumps(caps, ensure_ascii=False)
    check(not leaked(raw), f"能力目录原文没有禁止片段（{leaked(raw) or '干净'}）")

    probes = [
        "/api/conversion/capabilities?source_type=nope",
        "/api/conversion/tasks/does-not-exist",
        "/api/conversion/tasks/does-not-exist:9",
    ]
    dirty: list[str] = []
    for path in probes:
        try:
            _, payload = fetch_json(path)
            found = leaked(json.dumps(payload, ensure_ascii=False))
            if found:
                dirty.append(f"{path} -> {found}")
        except Exception as exc:  # noqa: BLE001
            dirty.append(f"{path} -> {type(exc).__name__}")
    check(not dirty, f"三个异常入口的响应体都干净（{dirty or '无泄露'}）")


# ----------------------------------------------------------------------

def run(page: Page, caps: dict) -> None:
    """逐段跑。**一段崩了不拖垮整轮** —— 验收脚本的价值是一次跑完把问题
    全列出来；让一个意料之外的超时把后面几段的结果一起吞掉，等于白跑一趟，
    而且崩溃点看起来像「脚本坏了」而不是「这里有情况」。
    崩掉的那段自己记一条失败，异常类型与消息照实写进报告。
    """
    sections = (
        ("A 段（能力目录）", lambda: section_a(page, caps)),
        ("B 段（新格式真机转换）", lambda: section_b(page)),
        ("C 段（参数面板）", lambda: section_c(page, caps)),
        ("D 段（图片选项）", lambda: section_d(page)),
        ("E 段（多帧说明）", lambda: section_e(page, caps)),
        ("F 段（图片→PDF）", lambda: section_f(page)),
        ("G 段（PDF 工具卡）", lambda: section_g(page, caps)),
        ("H 段（移动端）", lambda: section_h(page, caps)),
        ("I 段（安全）", lambda: section_i(page)),
        ("J 段（不泄露）", lambda: section_j(caps)),
    )
    for name, call in sections:
        try:
            call()
        except Exception as exc:  # noqa: BLE001 - 崩溃本身就是要报出来的结果
            check(False, f"{name} 整段中断（{type(exc).__name__}: {str(exc)[:200]}）")


def main() -> int:
    try:
        caps = fetch_capabilities()
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(f"读不到 {BASE}{CAPABILITIES_URL}：{exc}\n后端起来了吗？")

    print(
        f"能力目录：{len(caps['conversions'])} 条转换 / {len(caps['operations'])} 条操作",
        flush=True,
    )
    build_fixtures()
    DOWNLOADS.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(accept_downloads=True, locale="zh-CN")
        page = context.new_page()
        attach(page)
        try:
            run(page, caps)
        finally:
            browser.close()

    # 服务端返回 4xx 时浏览器也会记一条 console error，那是刻意拒绝坏文件的正常现象
    ignored = ("Failed to load resource",)
    real_errors = [item for item in console_errors if not any(x in item for x in ignored)]
    rejected = [item for item in console_errors if any(x in item for x in ignored)]

    print("\n--- 控制台报错 ---")
    for item in real_errors or ["（无）"]:
        print(item)
    print(f"（服务端正常拒绝坏文件产生的 4xx 记录 {len(rejected)} 条，已忽略）")
    check(not real_errors, "浏览器控制台无 JS 报错")

    failed = [label for ok, label in results if not ok]
    lines = [f"{'PASS' if ok else 'FAIL'}  {label}" for ok, label in results]
    lines.append("")
    lines.append(f"共 {len(results)} 项，通过 {len(results) - len(failed)} 项")
    lines.append(f"服务端拒绝坏文件的 4xx 记录：{len(rejected)} 条（正常）")
    if real_errors:
        lines.append("控制台 JS 报错：")
        lines.extend("  " + item for item in real_errors)
    if failed:
        lines.append("未通过：")
        lines.extend("  - " + label for label in failed)
    else:
        lines.append("全部通过")
    REPORT.write_text("\n".join(lines), encoding="utf-8")

    print(f"\n共 {len(results)} 项，通过 {len(results) - len(failed)} 项")
    print(f"工作目录：{WORK}")
    print(f"报告：{REPORT}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
