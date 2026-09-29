"""第七阶段的真机验收脚本（Playwright + 真实浏览器 + 真实转换产物）。

验的是「用户丢进来一堆混合文件，能不能真的换回一堆能用的文件」，所以每一处
判定都落在**下载到的产物本身**上：DOCX 用 python-docx 打开读正文，PDF 用
PyMuPDF 打开数页数并提取文字，图片用 Pillow 真解码读 format/width/height，
ZIP 用标准库真解压、逐个取出来再用 Pillow 验一遍。
界面上写着「转换完成」不算数 —— 转出一份空文档的接口同样返回 200。

分段：

    A 段  入口与能力（首页卡片、/convert、路由、SEO、能力矩阵的形状）
    B 段  单文件全链路：DOCX→PDF / XLSX→PDF / TXT→PDF / PDF→Word（含扫描件 OCR）
          / PNG→WEBP / JPG→PNG / 图片→PDF，逐个真产物断言
    C 段  自动分组：混合上传后按源格式分成几组、各组参数面板正确
    D 段  批量：10 个文件、ZIP 真解压逐个验格式、无路径穿越；1 个文件不打包
    E 段  失败与不支持（改名文件、空文件、PDF→PPT、DOCX→WEBP、界面上的不支持块）
    F 段  取消：协作式的诚实性 —— 正在跑的项必须是 cancelling，不是 cancelled
    G 段  进度真实性：真实页码，没有可信刻度就**不给百分比**
    H 段  负例与不泄露（FORBIDDEN_FRAGMENTS、路径穿越、404、409）
    I 段  移动端 375 / 390 / 414 无横向溢出
    J 段  前六阶段回归（十四个入口 + 两条真实转换）

用法：
    1. 先构建前端并启动后端（后端会顺带托管 frontend/dist）：
           cd frontend && npm run build
           cd backend && .venv\\Scripts\\python -m uvicorn main:app --port 8011
    2. python scripts/verify_phase7.py

可用环境变量覆盖：
    FILETOOLS_WEB_BASE      默认 http://127.0.0.1:8011（直接打后端托管的 dist）
    FILETOOLS_BACKEND_PY    后端 venv 的 python，用于生成素材与校验下载结果
    FILETOOLS_WORK_DIR      复用上一轮的素材目录，省掉重新生成的时间

注意：不要用 Vite 开发服务器（5173）跑本脚本 —— 它会把自己后端的地址
代理给 /api，两者版本不一致时结果没有意义。
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
import zipfile

from playwright.sync_api import Page, sync_playwright

ROOT = pathlib.Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
BASE = os.environ.get("FILETOOLS_WEB_BASE", "http://127.0.0.1:8011").rstrip("/")

# Windows 控制台默认是 GBK，直接打印「✓」会抛 UnicodeEncodeError 把整轮跑挂掉
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

WORK = pathlib.Path(
    os.environ.get("FILETOOLS_WORK_DIR") or tempfile.mkdtemp(prefix="filetools-verify-p7-")
)
SAMPLES = WORK / "samples"
DOWNLOADS = WORK / "downloads"
UNPACKED = WORK / "unpacked"
REPORT = ROOT / "scripts" / "verify_phase7_report.txt"

ROUTE = "/convert"
MOBILE_WIDTHS = (375, 390, 414)

#: 验收样张里的标记。刻意与 pytest 样张用不同的串：
#: 万一产物其实来自别处，这里对不上就能发现。
DOCX_MARK = "ACCEPT7DOCX"
TXT_MARK = "ACCEPT7TXT"
PDF_MARK = "ACCEPT7PDF"

#: 扫描件的页数。取消那一段要靠它撑出足够长的处理窗口（OCR 是逐页做的）
SCAN_PAGES = 5

#: 出错响应里绝不能出现的东西（§四十三）
FORBIDDEN_FRAGMENTS = (
    "Traceback",
    'File "',
    "\\",                       # Windows 路径分隔符
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
)


# ----------------------------------------------------------------------
# 样张：复用后端测试里的那套构造器，避免同一份排版规则在这里再抄一遍
# ----------------------------------------------------------------------

_SAMPLE_CODE = r'''
import json, pathlib, sys

from tests.conftest import (
    build_animated_gif,
    build_docx_bytes,
    build_image_bytes,
    build_labeled_pdf,
    build_scanned_pdf,
    build_xlsx_bytes,
)

out = pathlib.Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)
docx_mark, txt_mark, pdf_mark, scan_pages = sys.argv[2:6]

# Word：三段真实文字，转成 PDF 之后回来还能读到
(out / "季度报告.docx").write_bytes(build_docx_bytes(docx_mark, paragraphs=3))
# Excel
(out / "台账.xlsx").write_bytes(build_xlsx_bytes(docx_mark))
# TXT：不需要 LibreOffice，纯排版
(out / "说明.txt").write_text(
    "验收用纯文本\n" + txt_mark + "\n第三行：中英文混排 with English words 42。\n",
    encoding="utf-8",
)
# PDF：三页，每页一个只属于自己的标记 —— 页序只能靠它来验
(out / "报告.pdf").write_bytes(build_labeled_pdf(pdf_mark, pages=3))
# 扫描件：纯图片页，OCR 要逐页跑，正好用来测取消与真实页码
(out / "扫描件.pdf").write_bytes(
    build_scanned_pdf([f"{pdf_mark}-S{i}" for i in range(1, int(scan_pages) + 1)])
)
# 图片：三张，格式各不相同
(out / "照片.jpg").write_bytes(build_image_bytes(1200, 800, "JPEG"))
# 同一张照片换 .jpeg 后缀：与 .jpg 是同一个源类型，应当并进同一组
(out / "照片.jpeg").write_bytes(build_image_bytes(800, 600, "JPEG"))
(out / "插画.png").write_bytes(build_image_bytes(600, 600, "PNG"))
(out / "图标.webp").write_bytes(build_image_bytes(400, 300, "WEBP"))

# 十个文件用来验 ZIP：统一命名，方便按名字逐个查
for i in range(1, 11):
    (out / f"批量{i:02d}.png").write_bytes(build_image_bytes(320 + i, 240 + i, "PNG"))

# 第九阶段新增的三种图片源格式，各来一张
(out / "位图.bmp").write_bytes(build_image_bytes(480, 360, "BMP"))
(out / "动图.gif").write_bytes(build_animated_gif())
(out / "扫描页.tiff").write_bytes(build_image_bytes(500, 400, "TIFF"))

# 负例
(out / "改名.pdf").write_bytes(b"MZ\x90\x00" + b"\x00" * 2048)
(out / "空.png").write_bytes(b"")
# **不在能力矩阵里**的格式。第九阶段之后 GIF/BMP/TIFF 都能转了，
# 这个位置必须换成一个真的不受支持的格式，否则「不支持的文件单独成块」
# 这条检查会因为「文件其实支持」而失败 —— 那是断言过时，不是功能坏了。
# MP3 是 §四十 明令不实现的格式之一，拿它当负例最诚实。
(out / "录音.mp3").write_bytes(b"ID3\x03\x00\x00\x00" + b"\x00" * 64)

print(json.dumps(sorted(p.name for p in out.iterdir()), ensure_ascii=False))
'''

#: 产物校验（在后端 venv 里跑，那里有 PyMuPDF / Pillow / python-docx）
_VERIFY_CODE = r'''
import json, pathlib, sys

path = pathlib.Path(sys.argv[1])
kind = sys.argv[2]
info = {"kind": kind, "exists": path.exists(),
        "size": path.stat().st_size if path.exists() else 0}
if not info["exists"]:
    print(json.dumps(info, ensure_ascii=False)); raise SystemExit

if kind == "image":
    from PIL import Image
    with Image.open(path) as img:
        img.load()          # 真解码一遍：只看文件头识别不出被截断的图
        info.update(format=img.format, width=img.width, height=img.height)

elif kind == "image_dir":
    from PIL import Image
    items = []
    for child in sorted(path.iterdir()):
        if not child.is_file():
            continue
        with Image.open(child) as img:
            img.load()
            items.append({"name": child.name, "format": img.format,
                          "width": img.width, "height": img.height})
    info["items"] = items

elif kind == "pdf":
    import pymupdf
    with pymupdf.open(path) as doc:
        if doc.needs_pass:
            raise SystemExit("PDF 有密码")
        info["pages"] = doc.page_count
        info["text"] = "".join(page.get_text() for page in doc)

elif kind == "docx":
    import zipfile
    with zipfile.ZipFile(path) as archive:
        info["broken"] = archive.testzip()
        info["has_document"] = "word/document.xml" in archive.namelist()
    import docx
    document = docx.Document(str(path))
    info["paragraphs"] = len(document.paragraphs)
    info["text"] = "\n".join(p.text for p in document.paragraphs)

elif kind == "zip":
    import zipfile
    with zipfile.ZipFile(path) as archive:
        info["broken"] = archive.testzip()
        info["names"] = sorted(archive.namelist())

else:
    raise SystemExit("unknown kind " + kind)

print(json.dumps(info, ensure_ascii=False))
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
    """在 backend 的 venv 里跑一段代码，把它的 stdout 原样带回来。

    ``PYTHONIOENCODING=utf-8`` 是必须的：子进程的 stdout 是管道，
    这时 Python 按 Windows 本地代码页（GBK）写字节，父进程按 utf-8 读，
    只要子进程打印一个汉字就 UnicodeDecodeError。
    """
    out = subprocess.run(
        [str(BACKEND_PYTHON), "-c", code, *[str(a) for a in args]],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=dict(os.environ, PYTHONIOENCODING="utf-8"),
        cwd=str(BACKEND),  # 样张构造器在 tests/ 里，得让 backend/ 在 sys.path 上
    )
    if out.returncode != 0:
        raise RuntimeError(out.stderr or out.stdout)
    return out.stdout


def inspect(path: pathlib.Path, kind: str) -> dict:
    """产物本身的客观事实。判定一律用它，不看界面文案。"""
    return json.loads(_run_backend_python(_VERIFY_CODE, path, kind))


def squashed(text: object) -> str:
    """去掉所有空白再比。PDF 抽出来的文字常在词中间夹换行。"""
    return "".join(str(text).split())


# ----------------------------------------------------------------------
# 结果收集
# ----------------------------------------------------------------------

results: list[tuple[bool, str]] = []
console_errors: list[str] = []


def check(ok: bool, label: str) -> None:
    results.append((bool(ok), label))
    print(f"{'PASS' if ok else 'FAIL'}  {label}")


def section(title: str) -> None:
    print(f"\n=== {title} ===")


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


def hrefs_on(page: Page) -> list[str]:
    """**主内容区**里所有 ``<a href>``。

    只看 ``main``：导航栏那个全站通用的「开始使用 → /image/compress」按钮
    在每一页都有，把它算进来就等于要求「/pdf 页面不许有页头」。
    要比的是这一页**列了什么**，不是站点外壳长什么样。
    """
    return page.eval_on_selector_all(
        "main a[href]", "els => els.map(el => el.getAttribute('href'))"
    )


def wait_for_text(page: Page, needle: str, *, timeout: int = 60_000) -> bool:
    """等某段文字出现；超时返回 False 而不是抛异常。

    调用方要断言的是「有没有」，所以拿不到就如实报 FAIL ——
    让 playwright 的异常冒出去只会把整轮验收打断在一半。
    """
    try:
        page.wait_for_selector(f"text={needle}", timeout=timeout)
        return True
    except Exception:
        return False


def poll_until(page: Page, predicate, *, timeout: int = 30_000, step: int = 250) -> bool:
    """反复读页面，直到条件成立或超时。返回是否等到了。"""
    deadline = time.monotonic() + timeout / 1000
    while time.monotonic() < deadline:
        if predicate():
            return True
        page.wait_for_timeout(step)
    return False


# ----------------------------------------------------------------------
# 页面操作
# ----------------------------------------------------------------------

_download_seq = 0


def open_convert(page: Page, *, base: str = BASE) -> None:
    page.goto(f"{base}{ROUTE}", wait_until="networkidle")
    # 能力矩阵到手之后才开放上传，这里等它就位
    page.wait_for_selector("input[type=file]", state="attached", timeout=30_000)


def upload(page: Page, *sources: pathlib.Path) -> None:
    """一次性把若干文件塞进上传区（第一段里的那个 input）。

    开始转换之后还会出现「继续添加」用的第二个 input，所以这里固定取第一个。
    """
    page.set_input_files("input[type=file]", [str(item) for item in sources])
    page.wait_for_timeout(500)


def choose_target(page: Page, name: str, *, index: int = 0) -> None:
    """选一个格式。

    真正的 ``<input type=radio>`` 是 ``sr-only`` 的（1×1 且被裁掉），
    Playwright 的行动性检查会判定它「点不到」；用户实际点的是外面那层
    ``<label>``，所以这里也点 label。点完必须确认真的选上了 ——
    没选上的话后面下载到的就是另一个格式，那才是最费解的失败。
    """
    radio = page.get_by_role("radio", name=name, exact=False).nth(index)
    radio.evaluate("el => el.closest('label').click()")
    page.wait_for_timeout(200)
    check(radio.is_checked(), f"选中了「{name}」")


def group_download_button(page: Page):
    """整批结果的下载按钮；还没有就是 None。"""
    packed = page.get_by_role("button", name="打包下载")
    if packed.count():
        return packed.first
    single = page.get_by_role("button", name="下载结果")
    if single.count():
        return single.first
    return None


def start_group(page: Page, index: int = 0, *, timeout: int = 300_000) -> None:
    """点第 index 组的「开始转换这 N 个文件」，等这一组的结果按钮出现。"""
    page.get_by_role("button", name="开始转换这").nth(index).click()
    if not poll_until(page, lambda: group_download_button(page) is not None, timeout=timeout):
        raise AssertionError(f"等了 {timeout} ms 也没等到这一组的结果按钮")


def download(page: Page, name: str, *, timeout: int = 120_000) -> pathlib.Path:
    """按下某个下载按钮并把文件存下来。"""
    global _download_seq
    with page.expect_download(timeout=timeout) as info:
        page.get_by_role("button", name=name).first.click()
    item = info.value
    _download_seq += 1
    DOWNLOADS.mkdir(parents=True, exist_ok=True)
    target = DOWNLOADS / f"{_download_seq:02d}-{item.suggested_filename}"
    item.save_as(target)
    return target


def convert_one(page: Page, source: pathlib.Path, target: str, *, timeout: int = 300_000):
    """上传一个文件 → 选目标格式 → 开始 → 下载。返回下载到的文件。"""
    open_convert(page)
    upload(page, source)
    choose_target(page, target)
    start_group(page, timeout=timeout)
    text = body(page)
    return download(page, "打包下载" if "打包下载" in text else "下载结果")


# ----------------------------------------------------------------------
# 直接打接口：浏览器看到的只是渲染后的文字，这里看的是原始响应体
# ----------------------------------------------------------------------


def post_tasks(
    files: list[tuple[str, bytes]],
    target_type: str,
    form: dict | None = None,
    *,
    progress_ids: list[str] | None = None,
    base: str = BASE,
) -> tuple[int, str]:
    """直接往 /api/conversion/tasks 发一次 multipart。

    浏览器那条路径验的是「界面能不能用」，这一条验的是「响应体本身对不对」——
    错误码、计数、「有没有泄露内部信息」这些在渲染后的界面上根本看不到。
    """
    boundary = uuid.uuid4().hex
    parts: list[bytes] = []
    for filename, data in files:
        parts.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="files"; filename="{filename}"\r\n'
                f"Content-Type: application/octet-stream\r\n\r\n"
            ).encode()
            + data
            + b"\r\n"
        )
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="target_type"\r\n\r\n'
        f"{target_type}\r\n".encode()
    )
    for key, value in (form or {}).items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n'
            f"{value}\r\n".encode()
        )
    for pid in progress_ids or []:
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="progress_ids"\r\n\r\n'
            f"{pid}\r\n".encode()
        )
    parts.append(f"--{boundary}--\r\n".encode())

    request = urllib.request.Request(
        f"{base}/api/conversion/tasks",
        data=b"".join(parts),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=600) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def get_json(url: str, *, method: str = "GET", base: str = BASE) -> tuple[int, object]:
    request = urllib.request.Request(f"{base}{url}", method=method)
    try:
        with urllib.request.urlopen(request, timeout=600) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        # 出错时也先当 JSON 解一次：错误码就藏在响应体里，
        # 拿字符串去正则匹配会把「结构对不对」这件事丢掉
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, raw


TERMINAL = ("completed", "failed", "cancelled")


def wait_batch(
    status_url: str,
    *,
    timeout: float = 600.0,
    interval: float = 0.4,
    on_poll=None,
) -> dict:
    """轮询一批直到它定下来，返回最后一份快照。

    ``on_poll`` 让调用方在每次拿到快照时顺便记点东西（比如「有没有出现过
    真实页码」），不必为此再写一个轮询循环。
    """
    deadline = time.monotonic() + timeout
    snapshot: dict = {}
    while time.monotonic() < deadline:
        _, payload = get_json(status_url)
        if not isinstance(payload, dict):
            raise AssertionError(f"查询批次失败：{payload}")
        snapshot = payload
        if on_poll is not None:
            on_poll(snapshot)
        if snapshot.get("status") in TERMINAL:
            return snapshot
        time.sleep(interval)
    raise AssertionError(f"等了 {timeout} 秒这一批还没定下来：{snapshot.get('status')}")


def error_code_of(payload: object) -> str:
    """从错误响应里取错误码。

    业务错误统一是 ``{"error": {"code", "message"}}``（main.py 里那个
    ``FileToolsError`` 处理器），FastAPI 自己的 HTTPException 走 ``detail``。
    两种都认，免得把「形状变了」误判成「码不对」。
    """
    if not isinstance(payload, dict):
        return ""
    for key in ("error", "detail"):
        holder = payload.get(key)
        if isinstance(holder, dict):
            return str(holder.get("code", ""))
    return ""


def as_text(payload: object) -> str:
    return payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)


def check_no_leak(raw: str, label: str) -> None:
    leaked = [fragment for fragment in FORBIDDEN_FRAGMENTS if fragment in raw]
    check(not leaked, f"{label}：响应里没有泄露内部细节" + (f"（泄露了 {leaked}）" if leaked else ""))


def config_of(base: str = BASE) -> dict:
    with urllib.request.urlopen(f"{base}/api/config", timeout=10) as response:
        return json.load(response)


def capabilities_of(base: str = BASE) -> dict:
    with urllib.request.urlopen(f"{base}/api/conversion/capabilities", timeout=10) as response:
        return json.load(response)


def sample_bytes(name: str) -> bytes:
    return (SAMPLES / name).read_bytes()


# ----------------------------------------------------------------------
# A 段：入口与能力
# ----------------------------------------------------------------------

CONVERT_TITLE = "Free Online File Converter | FileTools"


def run_part_a(page: Page) -> None:
    section("A 段：入口与能力")

    page.goto(BASE + "/", wait_until="networkidle")
    text = body(page)
    check("统一转换中心" in text, "首页出现了「统一转换中心」")
    check(page.locator("a[href='/convert']").count() > 0, "首页有指向 /convert 的链接")

    page.goto(BASE + "/convert", wait_until="networkidle")
    check("统一转换中心" in body(page), "/convert 打开的是统一转换中心")

    open_convert(page)
    text = body(page)
    check("拖放文件到这里" in text or "选择文件" in text, "页面上有上传区")
    check(page.title() == CONVERT_TITLE, f"标题换成了本页专用的（当前：{page.title()}）")
    meta = page.get_attribute("meta[name=description]", "content") or ""
    check("批量" in meta, f"meta description 换成了本页的说明（{meta[:20]}…）")

    page.goto(BASE + "/", wait_until="networkidle")
    check(
        page.title() != CONVERT_TITLE,
        "离开页面后标题还原，不会一直顶着转换中心的标题",
    )

    # 顶栏导航能进转换中心（NAV_LINKS 是从 TOOL_GROUPS 派生的，跟着一起变）
    page.get_by_role("link", name="格式转换").first.click()
    page.wait_for_load_state("networkidle")
    check(page.url.endswith(ROUTE), f"顶栏「格式转换」进的是列表页")
    check(page.locator("a[href='/convert']").count() > 0, "列表页里能再点进统一转换中心")

    # 能力矩阵：界面上能选什么完全由它决定，所以它本身得是对的
    caps = capabilities_of()
    check(caps["office_available"] is True, "服务器报 Office 转换可用")
    check(caps["pdf_to_word_available"] is True, "服务器报 PDF 转 Word 可用")
    check(caps["ocr_available"] is True, "服务器报文字识别可用")
    check(caps["notes"] == [], f"组件齐全时没有「不可用」说明（{caps['notes']}）")

    matrix = caps["matrix"]
    # 第九阶段在 ``txt`` 之后追加了 html / md 两种源格式（都是自写的纯标准库
    # 子集，不依赖任何可选组件）；第十阶段 A 又追加了 ``svg``（由
    # ``compressors.svg`` 走 PyMuPDF 渲染，同样零新增依赖）与 ``heic``
    # （由可选的 ``pillow-heif`` 提供）。既有的十五个位置逐字不变 ——
    # 这里写的是精确集合，不是超集：少一种、多一种、换一种都会当场红。
    #
    # **HEIC 那几格依赖机器上装了 ``pillow-heif``**：没装时矩阵里根本不会
    # 出现 ``heic``（§六：探测不到就不发布）。这条依赖不是藏着的 ——
    # 上面那句 ``caps["notes"] == []`` 就是它的守卫：组件缺一个，
    # ``notes`` 会带一句「不可用」的说明，那一条先红。
    expected_sources = {
        "jpg", "png", "webp", "bmp", "gif", "tiff", "heic",
        "doc", "docx", "xls", "xlsx", "ppt", "pptx", "txt",
        "html", "md", "svg", "pdf",
    }
    check(set(matrix) == expected_sources, f"矩阵覆盖全部十八种源格式（{sorted(matrix)}）")
    check(
        matrix["jpg"] == ["png", "webp", "bmp", "gif", "tiff", "heic", "pdf"],
        f"JPG 的目标是 {matrix['jpg']}",
    )
    check(
        matrix["png"] == ["jpg", "webp", "bmp", "gif", "tiff", "ico", "heic", "pdf"],
        f"PNG 的目标是 {matrix['png']}（只有 PNG 多一个 ICO）",
    )
    # HEIC 是唯一一个**两个方向都要编解码器**的源格式：解码方向（heic→位图）
    # 要 libde265，编码方向（位图→heic）要 libx265。§六 允许只发布其中一半，
    # 但一条都不许伪造 —— 这里是「两个方向都在」时的精确真值。
    check(
        matrix["heic"] == ["jpg", "png", "webp", "bmp", "gif", "tiff", "pdf"],
        f"HEIC 的七个目标是 {matrix['heic']}",
    )
    check(
        sorted(source for source, targets in matrix.items() if "heic" in targets)
        == ["bmp", "gif", "jpg", "png", "tiff", "webp"],
        "产出 HEIC 的是六种位图，不含 SVG（矢量要先栅格化，规格没点名）",
    )
    check(matrix["pdf"] == ["docx"], f"PDF 只能转 Word（{matrix['pdf']}）")
    check("pptx" not in matrix["pdf"], "矩阵里没有 PDF → PPT 这种做不到的组合")
    check(
        all(targets == ["pdf"] for source, targets in matrix.items()
            if source in ("doc", "docx", "xls", "xlsx", "ppt", "pptx")),
        "六个 Office 格式都只能转 PDF",
    )
    check(
        matrix["txt"] == ["pdf", "docx", "html", "md"],
        f"TXT 的四个目标是 {matrix['txt']}",
    )
    check(
        matrix["html"] == ["pdf", "txt"],
        f"HTML 的目标是 {matrix['html']}",
    )
    check(
        matrix["md"] == ["pdf", "html", "txt"],
        f"Markdown 的目标是 {matrix['md']}",
    )
    # 第十阶段 A §九 字面：SVG→PNG/JPG/WEBP/PDF。顺序按规格那句写的来。
    # 矢量导出（PDF）与栅格化（另一个三个）是两族实现，这里只验对外承诺。
    check(
        matrix["svg"] == ["jpg", "png", "webp", "pdf"],
        f"SVG 的四个目标是 {matrix['svg']}",
    )
    check(
        [source for source, targets in matrix.items() if "md" in targets] == ["txt"],
        "只有 TXT 产出 Markdown（HTML→MD 规格没点名，不做）",
    )
    check(
        sorted(source for source, targets in matrix.items() if "docx" in targets)
        == ["pdf", "txt"],
        "产出 Word 的只有 PDF 与 TXT 两条",
    )
    check(
        "ico" not in set(matrix["jpg"]) | set(matrix["webp"]) | set(matrix["bmp"])
        | set(matrix["gif"]) | set(matrix["tiff"]) | set(matrix["svg"])
        | set(matrix["heic"]),
        "只有 PNG → ICO，别的源不该出现这个目标",
    )

    check(
        [group["key"] for group in caps["groups"]] == ["image", "office", "text", "pdf"],
        f"四组按固定顺序给出（{[g['key'] for g in caps['groups']]}）",
    )
    for group in caps["groups"]:
        check(bool(group["sources"]), f"「{group['label']}」这一组有源格式")
    image_group = caps["groups"][0]
    check(
        set(image_group["targets"]) == {"jpg", "png", "webp", "bmp", "gif", "tiff",
                                        "ico", "heic", "pdf"},
        f"图片组的目标格式是 {image_group['targets']}",
    )
    check(
        all(target["extension"].startswith(".") for target in caps["targets"]),
        "每个目标格式都带着结果扩展名",
    )
    check(
        "扫描版 PDF 会自动识别文字" in caps["pdf_to_word_note"],
        f"给了 PDF 转 Word 的识别提示（{caps['pdf_to_word_note']}）",
    )

    # 界面上真的把矩阵用起来了：能选的目标与矩阵一致
    open_convert(page)
    upload(page, SAMPLES / "照片.jpg")
    count = page.locator("input[name^='conversion-target-']").count()
    # 第九阶段把图片目标扩到了 6 个（PNG / WEBP / BMP / GIF / TIFF / PDF），
    # 第十阶段 A 又加了 HEIC（图片互转 + 图片转 PDF 共 7 个）。
    #
    # 这个数字是**界面自己渲染出来的**，不是我们算好了喂给它的：能力目录
    # 里 JPG 有多少个可用目标，这里就该有多少个单选按钮。所以它同时也是
    # 「前端没有硬编码能力矩阵」这条规矩的见证人 —— 前端要是抄了一份写死的
    # 列表，加 HEIC 之后这里会停在 6。
    check(count == 7, f"JPG 组给了 7 个目标格式（当前 {count} 个）")
    check(
        page.get_by_role("radio", name="PDF", exact=False).count() == 1,
        "其中一个目标是 PDF",
    )
    check(
        page.get_by_role("radio", name="JPG", exact=False).count() == 0,
        "没有「JPG → JPG」这种没意义的同格式转换",
    )


# ----------------------------------------------------------------------
# B 段：单文件全链路
# ----------------------------------------------------------------------


def run_part_b(page: Page) -> None:
    section("B 段：单文件全链路（真产物）")

    # --- DOCX → PDF ---
    target = convert_one(page, SAMPLES / "季度报告.docx", "PDF")
    check(target.suffix == ".pdf", f"DOCX→PDF：下载下来的确实是 PDF（{target.name}）")
    facts = inspect(target, "pdf")
    check(facts["pages"] >= 1, f"DOCX→PDF：PDF 能打开且页数是 {facts['pages']}")
    check(
        DOCX_MARK in squashed(facts["text"]),
        "DOCX→PDF：PDF 正文里读得到源文档里的文字（不是一份空 PDF）",
    )

    # --- XLSX → PDF ---
    target = convert_one(page, SAMPLES / "台账.xlsx", "PDF")
    facts = inspect(target, "pdf")
    check(facts["pages"] >= 1, f"XLSX→PDF：PDF 能打开且页数是 {facts['pages']}")
    check(DOCX_MARK in squashed(facts["text"]), "XLSX→PDF：表格里的内容进了 PDF")

    # --- TXT → PDF（不经过 LibreOffice 的那条路）---
    target = convert_one(page, SAMPLES / "说明.txt", "PDF")
    facts = inspect(target, "pdf")
    check(facts["pages"] >= 1, f"TXT→PDF：PDF 能打开且页数是 {facts['pages']}")
    check(TXT_MARK in squashed(facts["text"]), "TXT→PDF：PDF 正文里读得到 TXT 里的文字")

    # --- PDF → Word ---
    target = convert_one(page, SAMPLES / "报告.pdf", "Word")
    check(target.suffix == ".docx", f"PDF→Word：下载下来的确实是 Word（{target.name}）")
    facts = inspect(target, "docx")
    check(facts["broken"] is None, "PDF→Word：DOCX 压缩包完整（zipfile 自检通过）")
    check(facts["has_document"] is True, "PDF→Word：DOCX 里有 word/document.xml")
    check(PDF_MARK in squashed(facts["text"]), "PDF→Word：DOCX 正文里读得到源 PDF 里的文字")

    # --- 扫描版 PDF → Word（真 OCR）---
    target = convert_one(page, SAMPLES / "扫描件.pdf", "Word", timeout=600_000)
    facts = inspect(target, "docx")
    check(
        PDF_MARK in squashed(facts["text"]),
        "扫描件→Word：OCR 认出了图片里的文字（不是一份空 DOCX）",
    )

    # --- PNG → WEBP ---
    target = convert_one(page, SAMPLES / "插画.png", "WEBP")
    check(target.suffix == ".webp", f"PNG→WEBP：下载下来的确实是 WEBP（{target.name}）")
    facts = inspect(target, "image")
    check(facts["format"] == "WEBP", f"PNG→WEBP：Pillow 认出格式是 {facts['format']}")
    check(
        (facts["width"], facts["height"]) == (600, 600),
        f"PNG→WEBP：尺寸保持不变（{facts['width']}×{facts['height']}）",
    )

    # --- JPG → PNG ---
    target = convert_one(page, SAMPLES / "照片.jpg", "PNG")
    facts = inspect(target, "image")
    check(facts["format"] == "PNG", f"JPG→PNG：Pillow 认出格式是 {facts['format']}")
    check(
        (facts["width"], facts["height"]) == (1200, 800),
        f"JPG→PNG：尺寸保持不变（{facts['width']}×{facts['height']}）",
    )

    # --- 图片 → PDF（每张各出一份）---
    target = convert_one(page, SAMPLES / "照片.jpg", "PDF")
    facts = inspect(target, "pdf")
    check(facts["pages"] == 1, f"图片→PDF：单个文件出的是 1 页 PDF（当前 {facts['pages']} 页）")


# ----------------------------------------------------------------------
# C 段：自动分组
# ----------------------------------------------------------------------


def run_part_c(page: Page) -> None:
    section("C 段：自动分组")

    open_convert(page)
    upload(
        page,
        SAMPLES / "季度报告.docx",
        SAMPLES / "报告.pdf",
        SAMPLES / "插画.png",
        SAMPLES / "说明.txt",
    )
    page.wait_for_timeout(800)
    text = body(page)

    for label in ("Word 文档", "PDF 文档", "PNG 图片", "文本文件"):
        check(label in text, f"混合上传后出现了「{label}」这一组")

    starts = page.get_by_role("button", name="开始转换这").count()
    check(starts == 4, f"四类文件分成了四组（当前 {starts} 组）")
    check(page.get_by_role("button", name="全部开始").count() == 1, "多组时给了「全部开始」")
    check("4 个文件 ·" in text, "页首汇总报出总数 4 个文件")

    # 参数面板按「源是哪一组、目标产出什么」显示，不是每行都摆一排输入框。
    # 第九阶段起面板由服务端的 options_schema 驱动，标签取自 schema
    # （``resize.mode`` 的 legend 是「尺寸」，不再是第七阶段的「调整尺寸」），
    # 所以这里断言的是**语义组**而不是某一串界面文案：
    # 尺寸是图片组独有的，别的三组一个都没有。
    check(
        text.count("图片质量") == 1,
        f"只有图片组有「图片质量」面板（当前 {text.count('图片质量')} 处）",
    )
    size_groups = page.get_by_role("group", name="尺寸", exact=True).count()
    check(
        size_groups == 1,
        f"只有图片组有「尺寸」面板（当前 {size_groups} 处）",
    )
    check(
        text.count("目标最大文件大小") == 2,
        f"图片组与 Office 组有「目标最大文件大小」（当前 {text.count('目标最大文件大小')} 处）",
    )
    check("扫描版 PDF 会自动识别文字" in text, "PDF 组的默认目标是 Word，并给出识别提示")

    # 四组各用一组单选按钮名，选 A 组不会清掉 B 组
    radios = page.locator("input[name^='conversion-target-']")
    names = {radios.nth(i).get_attribute("name") for i in range(radios.count())}
    check(len(names) == 4, f"四组的目标格式各用一组单选按钮（当前 {len(names)} 组名字）")

    quality = page.locator("input[name^='conversion-quality-']")
    quality_names = {quality.nth(i).get_attribute("name") for i in range(quality.count())}
    check(len(quality_names) == 1, f"质量设置也按组命名（{quality_names}）")

    # 选一个组的目标，别的组不受影响
    before = page.locator("input[name^='conversion-target-']:checked").count()
    choose_target(page, "WEBP")
    after = page.locator("input[name^='conversion-target-']:checked").count()
    check(
        before == after == 4,
        f"改动一组的选项不会清掉其它组的选择（改前 {before} 组已选，改后 {after} 组已选）",
    )

    # 同源格式的文件并进同一组：``.jpeg`` 与 ``.jpg`` 在后端是同一个源类型
    # （``registry.EXTENSIONS_BY_SOURCE``），前端按 capabilities 分组，两者也该
    # 合在一起；``.png`` 是另一种源格式，另起一组。
    open_convert(page)
    upload(page, SAMPLES / "照片.jpg", SAMPLES / "照片.jpeg", SAMPLES / "插画.png")
    page.wait_for_timeout(600)
    text = body(page)
    group_count = page.get_by_role("button", name="开始转换这").count()
    check(group_count == 2, f"jpg 与 jpeg 合成一组、png 另一组（当前 {group_count} 组）")
    check("2 个文件" in text, "JPG 那组报出 2 个文件")
    check("1 个文件" in text, "PNG 那组报出 1 个文件")
    check(page.get_by_role("button", name="全部开始").count() == 1, "两组时给了「全部开始」")

    # 图片 → PDF 在第九阶段起**有**版式参数（§十），不再是一句「去别的页面调」。
    # PNG 的默认目标是 JPG，所以先确认默认下摆的是图片那套（质量），
    # 选了 PDF 之后整块换成 PDF 那套（页面大小/方向/页边距/适应方式），
    # 并且页面上不再出现指向旧页面的链接 —— 一个入口就够了。
    open_convert(page)
    upload(page, SAMPLES / "插画.png")
    page.wait_for_timeout(600)
    check("页面大小" not in body(page), "目标是 JPG 时不显示图片转 PDF 的版式参数")
    check("图片质量" in body(page), "目标是 JPG 时有图片质量面板")
    check(page.get_by_role("button", name="全部开始").count() == 0, "只有一组时不给「全部开始」")
    choose_target(page, "PDF")
    page.wait_for_timeout(300)
    text = body(page)
    for label in ("页面大小", "页面方向", "页边距", "图片适应方式"):
        check(label in text, f"选了 PDF 之后出现「{label}」")
    check("图片质量" not in text, "图片转 PDF 时不再摆图片质量面板")
    check(
        page.get_by_role("link", name="图片转 PDF").count() == 0,
        "不再把用户支到旧的「图片转 PDF」页面",
    )

    # 不支持的扩展名单独成块，并说清是哪个文件、为什么
    open_convert(page)
    upload(page, SAMPLES / "录音.mp3")
    page.wait_for_timeout(500)
    text = body(page)
    check("1 个文件不支持转换" in text, "MP3 进了「不支持转换」块")
    check("录音.mp3" in text, "块里指名道姓列出是哪个文件")
    check("暂不支持 MP3 格式" in text, "并给出了具体原因")
    check(page.get_by_role("button", name="开始转换这").count() == 0, "它没有被算进可转换的组")


# ----------------------------------------------------------------------
# D 段：批量
# ----------------------------------------------------------------------


def run_part_d(page: Page) -> None:
    section("D 段：批量与 ZIP")

    batch = [SAMPLES / f"批量{i:02d}.png" for i in range(1, 11)]
    open_convert(page)
    upload(page, *batch)
    page.wait_for_timeout(900)
    text = body(page)
    check("10 个文件 ·" in text, "10 个文件进了同一组")
    check(page.get_by_role("button", name="开始转换这").count() == 1, "10 个文件只分成一组")

    choose_target(page, "WEBP")
    start_group(page)

    text = body(page)
    check("10/10" in squashed(text), "整批进度显示 10 / 10")
    check("已完成 10 个" in text, "结果区给出「已完成 10 个」")

    target = download(page, "打包下载")
    check(target.suffix == ".zip", f"10 个成功项打成了 ZIP（{target.name}）")
    facts = inspect(target, "zip")
    check(facts["broken"] is None, "ZIP 能完整解压（zipfile 自检通过）")
    names = facts["names"]
    check(len(names) == 10, f"ZIP 里正好有 10 个文件（当前 {len(names)} 个）")
    check(all(name.endswith(".webp") for name in names), f"ZIP 里每个都是 .webp（{names[:3]}…）")
    check(
        all("/" not in name and ".." not in name for name in names),
        "ZIP 内是扁平结构，没有目录也没有 ../ 穿越",
    )

    # 真解压出来，逐个用 Pillow 验：只数名字证明不了里面是能打开的图
    UNPACKED.mkdir(parents=True, exist_ok=True)
    for stale in UNPACKED.iterdir():
        stale.unlink()
    with zipfile.ZipFile(target) as archive:
        archive.extractall(UNPACKED)
    items = inspect(UNPACKED, "image_dir")["items"]
    check(len(items) == 10, f"解压出 10 个文件（当前 {len(items)} 个）")
    check(
        all(item["format"] == "WEBP" for item in items),
        f"解压出来的每一个都能被 Pillow 解码成 WEBP（{sorted({i['format'] for i in items})}）",
    )
    check(
        {item["name"] for item in items} == set(names),
        "解压出来的文件名与 ZIP 清单一致",
    )

    # 1 个成功项不打包，直接把文件给出来
    target = convert_one(page, SAMPLES / "插画.png", "WEBP")
    check(target.suffix == ".webp", f"只有 1 个成功项时给的是文件而不是 ZIP（{target.name}）")
    check(inspect(target, "image")["format"] == "WEBP", "那一个文件本身是能打开的 WEBP")


# ----------------------------------------------------------------------
# E 段：失败与不支持
# ----------------------------------------------------------------------


def run_part_e(page: Page) -> None:
    section("E 段：失败与不支持")

    # --- 改名文件：内容是 Windows 可执行文件，必须按内容而不是扩展名判 ---
    status, raw = post_tasks([("改名.pdf", sample_bytes("改名.pdf"))], "docx")
    check(status == 202, f"改名文件被受理后逐项判定（HTTP {status}）")
    snapshot = wait_batch(json.loads(raw)["status_url"])
    task = snapshot["tasks"][0]
    check(task["status"] == "failed", "改名成 PDF 的可执行文件被判定为失败")
    # 判定依据是内容不是后缀：它顶着 .pdf 的名字，却没能通过 PDF 校验，
    # 所以 source_type 绝不会是 "pdf"（被拒的项会被清空，这里是空串）。
    check(
        task["source_type"] != "pdf",
        f"服务端没有信它 .pdf 这个后缀（source_type={task['source_type']!r}）",
    )
    check(bool(task["error_message"]), f"失败项给出了具体原因（{task['error_message']}）")
    check(task["can_retry"] is False, "这种「文件本身不行」的项不给重试（§十六）")
    check_no_leak(raw + as_text(snapshot), "改名文件")

    # --- 空文件 ---
    status, raw = post_tasks([("空.png", b"")], "webp")
    check(status in (202, 400), f"空文件返回 {status}，不是 500")
    if status == 202:
        snapshot = wait_batch(json.loads(raw)["status_url"])
        check(snapshot["failed"] == 1, "空文件被记成一项失败")
        check(snapshot["completed"] == 0, "空文件没有产出结果")
    check_no_leak(raw, "空文件")

    # --- 组合不在矩阵里：PDF → PPT ---
    status, raw = post_tasks([("报告.pdf", sample_bytes("报告.pdf"))], "pptx")
    check(status == 400, f"PDF → PPT 被拒绝（HTTP {status}）")
    check("UNSUPPORTED_CONVERSION" in raw, "拒绝的理由是 UNSUPPORTED_CONVERSION")
    check_no_leak(raw, "PDF → PPT")

    # --- 源与目标都在矩阵里，但这一对不存在：DOCX → WEBP ---
    status, raw = post_tasks([("季度报告.docx", sample_bytes("季度报告.docx"))], "webp")
    check(status == 202, f"DOCX → WEBP 逐项判定（HTTP {status}）")
    snapshot = wait_batch(json.loads(raw)["status_url"])
    task = snapshot["tasks"][0]
    check(task["status"] == "failed", "DOCX → WEBP 这一项失败了")
    check(
        task["error_code"] == "UNSUPPORTED_CONVERSION",
        f"错误码是 UNSUPPORTED_CONVERSION（{task['error_code']}）",
    )
    message = task["error_message"] or ""
    check(
        "Word" in message and "WEBP" in message,
        f"原因里说清了是哪两种格式（{message}）",
    )
    check(snapshot["failed"] == 1, "整批如实报告 1 个失败")
    check(snapshot["result"] is None, "全部失败时不给下载入口")
    check_no_leak(as_text(snapshot), "DOCX → WEBP")

    # --- 一个失败不影响同批的其它文件 ---
    status, raw = post_tasks(
        [("插画.png", sample_bytes("插画.png")), ("季度报告.docx", sample_bytes("季度报告.docx"))],
        "webp",
    )
    check(status == 202, f"混合成功与失败的一批返回 {status}")
    snapshot = wait_batch(json.loads(raw)["status_url"])
    check(snapshot["completed"] == 1, f"能转的那一个照常成功（完成 {snapshot['completed']} 个）")
    check(snapshot["failed"] == 1, f"不能转的那一个如实失败（失败 {snapshot['failed']} 个）")
    check(
        snapshot["result"] is not None and snapshot["result"]["completed"] == 1,
        "部分成功时仍然给出下载入口",
    )
    check_no_leak(as_text(snapshot), "部分失败")

    # --- 界面：可转换的照常成组，转不了的单独列出来 ---
    #
    # 第九阶段之后 GIF 已是**可转换**的源格式，所以这里换成 MP3 —— 它仍然
    # 不在能力矩阵里（§四十）。负例必须挑真的不支持的东西，
    # 否则这条检查就只是在测「断言过时了没有」。
    open_convert(page)
    upload(page, SAMPLES / "录音.mp3", SAMPLES / "插画.png")
    page.wait_for_timeout(600)
    text = body(page)
    check("1 个文件不支持转换" in text, "界面把 MP3 拎出来单独说明")
    check(page.get_by_role("button", name="开始转换这").count() == 1, "图片仍能正常成组")
    check("1 个文件 ·" in text, "不支持的文件没有被算进可转换的组")

    # --- 界面：第九阶段新增的三种图片源格式要能正常成组 ---
    #
    # 分组键是**源格式**，不是「都是图片」：BMP / GIF / TIFF 是三种不同的源，
    # 各自能转的目标不一样（GIF 转不了 GIF），所以三组才是对的。
    # 这里逐组点名源格式，比笼统地数按钮个数更强。
    open_convert(page)
    upload(page, SAMPLES / "位图.bmp", SAMPLES / "动图.gif", SAMPLES / "扫描页.tiff")
    page.wait_for_timeout(600)
    text = body(page)
    check("3 个文件 · 合计" in text, "BMP / GIF / TIFF 三个文件都进了可转换的组")
    for label in ("BMP 图片", "GIF 图片", "TIFF 图片"):
        check(label in text, f"界面上认出了「{label}」这个源格式")
    check(
        page.get_by_role("button", name="开始转换这").count() == 3,
        "三种新格式各自成组，每组一个开始按钮",
    )
    check("不支持转换" not in text, "新格式不再被界面当成不支持的文件")

    # --- 界面：改名文件转换失败时给的是中文原因，且不带内部细节 ---
    open_convert(page)
    upload(page, SAMPLES / "改名.pdf")
    page.get_by_role("button", name="开始转换这").first.click()
    appeared = poll_until(
        page,
        lambda: "失败" in body(page) and group_download_button(page) is None,
        timeout=180_000,
    )
    check(appeared, "改名文件在界面上如实显示为失败")
    text = body(page)
    check(
        any(word in text for word in ("损坏", "无法", "不支持", "格式")),
        "失败原因是一句看得懂的中文",
    )
    leaked = [fragment for fragment in FORBIDDEN_FRAGMENTS if fragment in text]
    check(not leaked, f"界面上没有泄露内部细节（{leaked}）")
    check(
        page.get_by_role("button", name="重试").count() == 0,
        "这种失败项界面上不给「重试」按钮",
    )


# ----------------------------------------------------------------------
# F 段：取消（协作式，且诚实）
# ----------------------------------------------------------------------


def run_part_f(page: Page) -> None:
    section("F 段：取消")

    # --- 正在跑的项必须显示 cancelling，不能谎报成「已取消」 ---
    status, raw = post_tasks(
        [("扫描件.pdf", sample_bytes("扫描件.pdf"))],
        "docx",
        progress_ids=["verify7cancel01"],
    )
    check(status == 202, f"OCR 任务提交返回 {status}")
    created = json.loads(raw)
    status_url = created["status_url"]

    def is_processing() -> bool:
        code, snapshot = get_json(status_url)
        return (
            code == 200
            and isinstance(snapshot, dict)
            and snapshot["tasks"][0]["status"] == "processing"
        )

    deadline = time.monotonic() + 180
    started = False
    while time.monotonic() < deadline:
        if is_processing():
            started = True
            break
        time.sleep(0.2)
    check(started, "OCR 任务进入了处理中状态")

    code, after = get_json(created["cancel_url"], method="POST")
    check(code == 200, f"取消请求返回 {code}")
    check(isinstance(after, dict) and after["cancelling"] is True, "响应里 cancelling 为真")
    if isinstance(after, dict):
        first = after["tasks"][0]
        check(
            first["status"] == "cancelling",
            f"正在跑的这一项如实报成 cancelling 而不是 cancelled（{first['status']}）",
        )
        check(first["can_retry"] is False, "已请求取消的批次里不再提供重试")
        check_no_leak(as_text(after), "取消响应")

    final = wait_batch(status_url)
    check(final["status"] == "cancelled", f"整批最终状态是 cancelled（{final['status']}）")
    check(final["cancelled"] == 1, f"那一项最终确实被取消了（{final['cancelled']} 个）")
    check(final["failed"] == 0, "取消没有被误报成失败")
    check(final["result"] is None, "全被取消时不给下载入口")

    # --- 排队中的项立刻取消，不是拖到跑完 ---
    #
    # 「立刻」由上面那个响应断言（取消那一刻还剩几项在排队），它不受时序影响。
    # 最终计数则**不能**断言成「十项全取消」：一次能跑两个（QUEUE_WORKERS），
    # 而 PNG→WEBP 只要几十毫秒，取消请求到达之前跑完一两项是正常的，
    # 拿它当失败就是在测运气。这里断言的是真正不变的那条规则 ——
    # 没跑完的都被取消掉（不会有人悄悄跑到结束），取消不算失败，
    # 整批状态与「到底有没有产出结果」一致。
    batch = [(f"批量{i:02d}.png", sample_bytes(f"批量{i:02d}.png")) for i in range(1, 11)]
    status, raw = post_tasks(batch, "webp")
    created = json.loads(raw)
    code, after = get_json(created["cancel_url"], method="POST")
    check(code == 200, f"十项批次取消返回 {code}")
    if isinstance(after, dict):
        check(
            after["queued"] <= 1,
            f"还在排队的项被立刻取消，不会等到跑完（剩余排队 {after['queued']} 项）",
        )
    final = wait_batch(created["status_url"])
    check(
        final["cancelled"] >= 1,
        f"取消确实拦下了一些项（拦下 {final['cancelled']} 项）",
    )
    check(
        final["cancelled"] == 10 - final["completed"],
        f"没跑完的全部被取消，没有一个悄悄跑到结束"
        f"（取消 {final['cancelled']} / 完成 {final['completed']}）",
    )
    check(final["failed"] == 0, "没有把取消算成失败")
    if final["completed"] == 0:
        check(final["status"] == "cancelled", "一个都没成功时整批如实报成 cancelled")
        check(final["result"] is None, "全被取消时不给下载入口")
    else:
        check(
            final["status"] == "completed",
            f"取消前已经跑出来的结果予以保留，整批报 completed（{final['status']}）",
        )
        check(
            final["result"] is not None and final["result"]["completed"] == final["completed"],
            "保留下来的结果照常可以下载",
        )

    # --- 界面上的说法 ---
    open_convert(page)
    upload(page, SAMPLES / "扫描件.pdf")
    page.get_by_role("button", name="开始转换这").first.click()
    check(
        poll_until(
            page, lambda: page.get_by_role("button", name="取消").count() > 0, timeout=180_000
        ),
        "转换过程中界面上出现「取消」按钮",
    )
    page.get_by_role("button", name="取消").first.click()
    honest = poll_until(
        page,
        lambda: "已请求取消，正在等待当前文件处理完" in body(page),
        timeout=90_000,
    )
    check(honest, "点过取消之后界面如实说「已请求取消，正在等待当前文件处理完」")
    check(
        wait_for_text(page, "已取消", timeout=300_000),
        "等当前文件跑完之后，界面才改口说「已取消」",
    )


# ----------------------------------------------------------------------
# G 段：进度真实性
# ----------------------------------------------------------------------


def run_part_g(page: Page) -> None:
    section("G 段：进度真实性")

    # --- 真实页码：扫描件转 Word 期间能看到真实的第几页 ---
    seen_pages: list[tuple[int | None, int | None]] = []
    status, raw = post_tasks(
        [("扫描件.pdf", sample_bytes("扫描件.pdf"))],
        "docx",
        progress_ids=["verify7progress1"],
    )
    check(status == 202, f"扫描件转 Word 提交返回 {status}")
    created = json.loads(raw)

    final = wait_batch(
        created["status_url"],
        on_poll=lambda snapshot: seen_pages.append(
            (snapshot["tasks"][0]["page"], snapshot["tasks"][0]["page_count"])
        ),
    )
    check(final["tasks"][0]["status"] == "completed", "扫描件转 Word 成功了")
    reported = [pair for pair in seen_pages if pair[0] is not None]
    check(bool(reported), f"转换过程中读到了真实页码（{reported[:4]}）")
    check(
        all(pair[1] == SCAN_PAGES for pair in reported),
        f"报出的总页数是源文件的真实页数 {SCAN_PAGES}（见过 {sorted({p[1] for p in reported})}）",
    )
    check(
        all(1 <= pair[0] <= pair[1] for pair in reported),
        "页号始终落在 1 到总页数之间",
    )
    check_no_leak(as_text(final), "OCR 进度")

    # --- 图片转换没有可信刻度：progress 只能是 null，不许编一个数字 ---
    batch = [(f"批量{i:02d}.png", sample_bytes(f"批量{i:02d}.png")) for i in range(1, 11)]
    status, raw = post_tasks(batch, "webp")
    created = json.loads(raw)
    seen_progress: list[object] = []
    final = wait_batch(
        created["status_url"],
        on_poll=lambda snapshot: seen_progress.extend(
            task["progress"] for task in snapshot["tasks"]
        ),
    )
    check(final["completed"] == 10, f"十张图全部转换成功（{final['completed']} 个）")
    fabricated = [value for value in seen_progress if value is not None and value != 100.0]
    check(
        not fabricated,
        "图片转换期间没有出现过编造的中间百分比"
        f"（出现过的值：{sorted({str(v) for v in seen_progress})}）",
    )

    # --- 轻量进度端点与整批快照必须自洽 ---
    code, light = get_json(created["progress_url"])
    check(code == 200, f"轻量进度端点返回 {code}")
    if isinstance(light, dict):
        summed = (
            light["queued"]
            + light["processing"]
            + light["completed"]
            + light["failed"]
            + light["cancelled"]
        )
        check(summed == light["total"], f"轻量进度的五项计数加起来正好是总数（{summed}）")
        check(light["total"] == 10, f"轻量进度报出总数 10（{light['total']}）")
        check(light["status"] == final["status"], "轻量进度的整批状态与整批快照一致")
        check(light["progress"] == final["progress"], "两边的进度百分比一致")

    # --- 界面上不出现编造的**进度**百分比 ---
    open_convert(page)
    upload(page, *[SAMPLES / f"批量{i:02d}.png" for i in range(1, 11)])
    choose_target(page, "WEBP")
    start_group(page)
    #
    # 第十阶段 A 给结果行加了「体积省了多少」的徽章（§二十七–§三十二：
    # `ConversionTaskRow` 渲染 `-74%` 这种**带符号**的差值）。这是量出来的
    # 真数字，不是编造的进度，但它也含一个 `%`。
    #
    # 原来这里一把抓所有 `\d+%` 再要求全都是 100%，于是把一个真数字判成了
    # 假的 —— 又是**代理指标分不清两件事**：`-74%` 是体积差，
    # `74%` 才是进度。所以改成按**符号**分：进度百分比从不带符号，
    # 带 `+`/`-` 的一律是体积差，放行。无符号的那一支判据一个字没改。
    found = re.findall(r"([+-]?)(\d+(?:\.\d+)?)%", body(page))
    percents = sorted({f"{number}%" for sign, number in found if sign == ""})
    check(
        all(value == "100%" for value in percents),
        f"界面上的进度百分比只有 100%（出现过：{percents}）",
    )


# ----------------------------------------------------------------------
# H 段：负例与不泄露
# ----------------------------------------------------------------------


def run_part_h(page: Page) -> None:
    section("H 段：负例与不泄露")

    config = config_of()
    check(config["max_batch_files"] >= 1, f"/api/config 报出批量上限 {config['max_batch_files']}")
    check(
        config["max_batch_total_bytes"] > config["max_upload_bytes"],
        "整批上限大于单个文件上限",
    )
    check(
        "UNSUPPORTED_CONVERSION" in config["error_codes"],
        "新错误码 UNSUPPORTED_CONVERSION 进了 /api/config",
    )
    check(
        "TASK_NOT_RETRYABLE" in config["error_codes"],
        "新错误码 TASK_NOT_RETRYABLE 进了 /api/config",
    )

    # --- 路径穿越：文件名里带 ../ 也不能影响服务器目录 ---
    status, raw = post_tasks([("../../evil.png", sample_bytes("插画.png"))], "webp")
    check(status == 202, f"带 ../ 的文件名返回 {status}")
    created = json.loads(raw)
    final = wait_batch(created["status_url"])
    task = final["tasks"][0]
    check(task["status"] == "completed", "../ 文件名不影响转换本身成功")
    result = task["result"] or {}
    result_name = result.get("filename", "")
    check(".." not in result_name, f"结果文件名里没有 ../（{result_name}）")
    check("/" not in result_name, f"结果文件名里没有路径分隔符（{result_name}）")
    check(result_name.endswith(".webp"), f"结果文件名的扩展名是对的（{result_name}）")
    check_no_leak(raw, "路径穿越")

    # 真下下来看一眼：名字被清洗过，内容还得是能打开的图
    download_url = result.get("download_url") or ""
    check(bool(download_url), "被清洗过名字的那一项仍然给了下载地址")
    if download_url:
        with urllib.request.urlopen(f"{BASE}{download_url}", timeout=120) as response:
            payload = response.read()
        DOWNLOADS.mkdir(parents=True, exist_ok=True)
        escaped = DOWNLOADS / "traversal.webp"
        escaped.write_bytes(payload)
        facts = inspect(escaped, "image")
        check(
            facts["format"] == "WEBP",
            f"清洗过名字的结果仍然是一张能打开的图（{facts['format']}）",
        )

    # --- 不存在的批次 ---
    code, payload = get_json("/api/conversion/tasks/does-not-exist:0")
    check(code == 404, f"不存在的批次返回 {code}")
    check(error_code_of(payload) == "TASK_NOT_FOUND", f"错误码是 TASK_NOT_FOUND（{error_code_of(payload)}）")
    check_no_leak(as_text(payload), "不存在的批次")

    code, payload = get_json("/api/conversion/tasks/does-not-exist:0/progress")
    check(code == 404, f"不存在的批次的进度查询也返回 {code}")

    # --- 重试一个已完成的任务：409 + TASK_NOT_RETRYABLE ---
    status, raw = post_tasks([("插画.png", sample_bytes("插画.png"))], "webp")
    created = json.loads(raw)
    final = wait_batch(created["status_url"])
    task_id = final["tasks"][0]["task_id"]
    check(task_id.startswith(created["batch_id"] + ":"), f"任务号是「批次号:序号」（{task_id}）")
    code, payload = get_json(f"/api/conversion/tasks/{task_id}/retry", method="POST")
    check(code == 409, f"重试一个已完成的任务返回 {code}")
    check(
        error_code_of(payload) == "TASK_NOT_RETRYABLE",
        f"拒绝的理由是 TASK_NOT_RETRYABLE（{error_code_of(payload)}）",
    )
    check_no_leak(as_text(payload), "重试非失败项")

    # --- 重试一个「文件本身不行」的失败项：同样拒绝 ---
    status, raw = post_tasks([("改名.pdf", sample_bytes("改名.pdf"))], "docx")
    created = json.loads(raw)
    final = wait_batch(created["status_url"])
    task_id = final["tasks"][0]["task_id"]
    code, payload = get_json(f"/api/conversion/tasks/{task_id}/retry", method="POST")
    check(code == 409, f"重试一个不可重试的失败项返回 {code}")
    check_no_leak(as_text(payload), "重试不可重试项")

    # --- 形状不对的任务号 ---
    code, payload = get_json("/api/conversion/tasks/no-colon-here/retry", method="POST")
    check(code in (400, 404), f"形状不对的任务号返回 {code}，不是 500")
    check_no_leak(as_text(payload), "形状不对的任务号")


# ----------------------------------------------------------------------
# I 段：移动端
# ----------------------------------------------------------------------


def run_part_i(p) -> None:
    section("I 段：移动端布局")
    device = p.devices["iPhone 13"]
    browser = p.chromium.launch()
    try:
        for width in MOBILE_WIDTHS:
            ctx = browser.new_context(
                **{**device, "viewport": {"width": width, "height": 844}},
                accept_downloads=True,
            )
            page = ctx.new_page()
            attach(page)

            worst = 0
            worst_route = ""
            for route in ("/", ROUTE):
                page.goto(BASE + route, wait_until="networkidle")
                overflow = page.evaluate(
                    "() => document.documentElement.scrollWidth"
                    " - document.documentElement.clientWidth"
                )
                if overflow > worst:
                    worst, worst_route = overflow, route
            check(
                worst <= 1,
                f"{width}px：首页与转换中心没有横向溢出"
                f"（最大 {worst}px{' @' + worst_route if worst_route else ''}）",
            )

            # 有了文件之后再看一次：分组卡片、参数面板、任务行都在了
            open_convert(page)
            page.set_input_files(
                "input[type=file]",
                [str(SAMPLES / "插画.png"), str(SAMPLES / "报告.pdf"), str(SAMPLES / "说明.txt")],
            )
            page.wait_for_timeout(1200)
            overflow = page.evaluate(
                "() => document.documentElement.scrollWidth"
                " - document.documentElement.clientWidth"
            )
            check(overflow <= 1, f"{width}px：分组卡片与参数面板没有横向溢出（{overflow}px）")

            starts = page.get_by_role("button", name="开始转换这")
            check(starts.count() == 3, f"{width}px：三组各自给出了开始按钮")
            for index in range(starts.count()):
                box = starts.nth(index).bounding_box()
                height = round(box["height"]) if box else 0
                check(height >= 44, f"{width}px：第 {index + 1} 个开始按钮高 {height}px，够手指点")

            ctx.close()
    finally:
        browser.close()


# ----------------------------------------------------------------------
# J 段：前六阶段回归
# ----------------------------------------------------------------------

PHASE_ROUTES = (
    "/image/compress",
    "/image/convert",
    "/image/resize",
    "/pdf/from-images",
    "/pdf/to-images",
    "/pdf/compress",
    "/pdf/merge",
    "/pdf/split",
    "/pdf/edit-pages",
    "/pdf/to-word",
    "/doc/word",
    "/doc/excel",
    "/doc/ppt",
    "/doc/txt",
)

#: 每个分类列表页**应该**列出的入口。取自 ``frontend/src/config/tools.ts``
#: 的 TOOL_GROUPS —— 手抄一份是有意的：这份脚本要能独立地判「页面
#: 有没有列错类」，从被测代码里现算就等于拿页面自己证明页面自己。
CATEGORY_TOOL_ROUTES = {
    "image": ("/image/compress", "/image/convert", "/image/resize"),
    "pdf": (
        "/pdf/from-images",
        "/pdf/to-images",
        "/pdf/compress",
        "/pdf/merge",
        "/pdf/split",
        "/pdf/edit-pages",
        "/pdf/to-word",
    ),
    "doc": ("/doc/word", "/doc/excel", "/doc/ppt", "/doc/txt"),
}


def run_part_j(page: Page) -> None:
    section("J 段：前六阶段回归")

    for route in PHASE_ROUTES:
        page.goto(BASE + route, wait_until="networkidle")
        page.wait_for_selector("input[type=file]", state="attached", timeout=30_000)
        check(page.locator("input[type=file]").count() > 0, f"{route} 仍能打开并接受文件")

    # 每个分类页列的是**自己那类**的工具。
    #
    # 原来这里判的是「页面文本里有没有『统一转换中心』」—— 那是个
    # **代理指标**，它分不清两件事：「这一页就是统一转换中心」和
    # 「这一页提了一句统一转换中心」。第十阶段 A 按 §五十三 把 /image
    # 升级成了「图片工具」页，它现在会把「不产生新文件的工具（查看图片信息）」
    # 指进统一转换中心 —— 提了一句，但它仍然是图片那一类的列表页。
    # 于是代理指标把一个好页面判成了坏的。
    #
    # 所以改成直接判那件事本身：分类页必须列出自己那类的入口，
    # 且不许混进别类的入口。逐条按 href 判，比子串匹配**更严**，不是更松。
    for category, own in CATEGORY_TOOL_ROUTES.items():
        page.goto(f"{BASE}/{category}", wait_until="networkidle")
        check(len(body(page).strip()) > 0, f"/{category} 列表页有内容")
        hrefs = {href for href in hrefs_on(page) if href}
        missing = sorted(set(own) - hrefs)
        check(not missing, f"/{category} 列表页列出了自己那类的入口（缺 {missing}）")
        foreign = sorted(
            href
            for other, routes in CATEGORY_TOOL_ROUTES.items()
            if other != category
            for href in routes
            if href in hrefs
        )
        check(not foreign, f"/{category} 列表页没有混进别类的入口（多 {foreign}）")

    # /convert 仍然是那个统一转换中心
    page.goto(f"{BASE}/convert", wait_until="networkidle")
    check("统一转换中心" in body(page), "/convert 列表页就是统一转换中心")

    # 真跑一次第一阶段的图片格式转换：页面能打开证明不了接口没被改坏
    page.goto(BASE + "/image/convert", wait_until="networkidle")
    page.wait_for_selector("input[type=file]", state="attached", timeout=30_000)
    page.set_input_files("input[type=file]", str(SAMPLES / "插画.png"))
    page.wait_for_timeout(400)
    choose_target(page, "WEBP")
    page.get_by_role("button", name="开始转换").first.click()
    check(wait_for_text(page, "下载", timeout=300_000), "第一阶段的图片格式转换仍然跑得通")

    # 再跑一次第五阶段的 Word → PDF：统一中心复用的就是这条实现
    page.goto(BASE + "/doc/word", wait_until="networkidle")
    page.wait_for_selector("input[type=file]", state="attached", timeout=30_000)
    page.set_input_files("input[type=file]", str(SAMPLES / "季度报告.docx"))
    page.wait_for_timeout(400)
    page.get_by_role("button", name="开始转换").first.click()
    check(wait_for_text(page, "下载", timeout=300_000), "第五阶段的 Word → PDF 仍然跑得通")


# ----------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------


def build_samples() -> None:
    if (SAMPLES / "批量10.png").exists() and (SAMPLES / "扫描件.pdf").exists():
        print(f"复用已有样张：{SAMPLES}")
        return
    print("正在生成验收样张…")
    _run_backend_python(_SAMPLE_CODE, SAMPLES, DOCX_MARK, TXT_MARK, PDF_MARK, SCAN_PAGES)
    print(f"  样张目录：{SAMPLES}")


def main() -> int:
    build_samples()

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 1000}, accept_downloads=True)
        attach(page)
        try:
            run_part_a(page)
            run_part_b(page)
            run_part_c(page)
            run_part_d(page)
            run_part_e(page)
            run_part_f(page)
            run_part_g(page)
            run_part_h(page)
            run_part_j(page)
        finally:
            browser.close()
        run_part_i(p)

    # 负例那几条本来就会打出一串 4xx，控制台里的「Failed to load resource」不算 JS 报错
    js_errors = [
        line
        for line in console_errors
        if line.startswith("[pageerror]")
        or ("[console.error]" in line and "Failed to load resource" not in line)
    ]
    check(not js_errors, f"浏览器控制台没有 JS 报错（{len(js_errors)} 条）")
    for line in js_errors[:5]:
        print(f"    {line}")

    passed = sum(1 for ok, _ in results if ok)
    failed = [label for ok, label in results if not ok]
    print(f"\n通过 {passed} 项，失败 {len(failed)} 项")
    for label in failed:
        print(f"  失败：{label}")

    REPORT.write_text(
        "\n".join(
            [f"{'PASS' if ok else 'FAIL'}  {label}" for ok, label in results]
            + ["", f"通过 {passed} 项，失败 {len(failed)} 项"]
        ),
        encoding="utf-8",
    )
    print(f"工作目录：{WORK}")
    print(f"报告：{REPORT}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
