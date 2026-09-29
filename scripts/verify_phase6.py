"""第六阶段 A 的真机验收脚本（Playwright + 真实浏览器 + 真实 OCR）。

验的是「用户拿到一份 PDF，能不能真的换到一份能用的 Word」，所以每一处判定
都落在**下载到的 DOCX 本身**上：用标准库解包、按分页符切页、读正文文字、
数内嵌图片。界面上写着「转换完成」不算数 —— 转出一份空文档的接口同样返回 200。

分段：

    A 段  入口（首页卡片、/pdf 列表页、路由、两条质量声明）
    B 段  文字版 PDF 全流程：中文 / 英文 / 多页（页序）/ 中英混排
    C 段  扫描版 PDF 全流程：真实 OCR，原图 + 可编辑文字
    D 段  进度条显示的是真实阶段，OCR 期间给的是真实页码而不是假百分比
    E 段  缺 OCR 的后端：扫描件如实说清、**文字版 PDF 仍可用**
    F 段  负例与不泄露（空 / 损坏 / 加密 / 超大 / 传错格式）
    G 段  移动端 375 / 390 / 414 无横向溢出
    H 段  前五阶段回归（五个阶段的入口与两条真实转换）

用法：
    1. 先构建前端并启动后端（后端会顺带托管 frontend/dist）：
           cd frontend && npm run build
           cd backend && .venv\\Scripts\\python -m uvicorn main:app --port 8011
    2. python scripts/verify_phase6.py

可用环境变量覆盖：
    FILETOOLS_WEB_BASE      默认 http://127.0.0.1:8011（直接打后端托管的 dist）
    FILETOOLS_BACKEND_PY    后端 venv 的 python，用于生成素材与校验下载结果
    FILETOOLS_NOOCR_PORT    E 段缺 OCR 后端的端口，默认 8014
    FILETOOLS_WORK_DIR      复用上一轮的素材目录，省掉重新生成的时间

注意：不要用 Vite 开发服务器（5173）跑本脚本 —— 它会把自己后端的地址
代理给 /api，两者版本不一致时结果没有意义。
"""

from __future__ import annotations

import html
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
NOOCR_PORT = int(os.environ.get("FILETOOLS_NOOCR_PORT", "8014"))
NOOCR_BASE = f"http://127.0.0.1:{NOOCR_PORT}"

# Windows 控制台默认是 GBK，直接打印「✓」会抛 UnicodeEncodeError 把整轮跑挂掉
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

WORK = pathlib.Path(
    os.environ.get("FILETOOLS_WORK_DIR") or tempfile.mkdtemp(prefix="filetools-verify-p6-")
)
SAMPLES = WORK / "samples"
DOWNLOADS = WORK / "downloads"
SERVER_TEMP = WORK / "server-temp"
REPORT = ROOT / "scripts" / "verify_phase6_report.txt"
NOOCR_LOG = WORK / "backend-noocr.log"

#: 验收样张正文里的标记。用和 pytest 样张不同的串：
#: 万一产物其实来自别处，这里对不上就能发现。
PAGE_MARKS = ("ACCEPT6-A", "ACCEPT6-B", "ACCEPT6-C")
EN_MARK = "ACCEPT6-EN"
MIX_TEXT_MARK = "ACCEPT6-MIXTEXT"
#: 扫描件上的字。只用**实测识别完全正确**的纯中文与数字：
#: 中英混排的识别结果会有空格/全半角差异，拿它当断言只会得到看起来像 bug 的假红。
SCAN_MARKS = ("文件转换测试系统", "采购合同")

ROUTE = "/pdf/to-word"
MOBILE_WIDTHS = (375, 390, 414)

#: 结果页必须出现的东西（规格 §结果页）
RESULT_FIELDS = ("类型", "页数", "识别方式")
QUALITY_NOTES = (
    "PDF → Word 无法保证 100% 还原原始排版。",
    "复杂表格、特殊字体、图片和多栏布局可能出现排版差异。",
)

#: 出错响应里绝不能出现的东西（§十三 / §十八）
FORBIDDEN_FRAGMENTS = (
    "Traceback",
    'File "',
    "\\",                       # Windows 路径分隔符
    "/tmp/",
    "AppData",
    "site-packages",
    "filetools_",
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

import pymupdf

from tests.conftest import (
    build_empty_pdf,
    build_english_pdf,
    build_mixed_pdf,
    build_scanned_pdf,
    build_text_pdf,
)

out = pathlib.Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)
marks = sys.argv[2:5]
en_mark, mix_mark = sys.argv[5:7]
scan_marks = sys.argv[7:9]

# 中文文字版：三页，每页一个只属于自己的标记 —— 页序只能靠它来验
(out / "文字版-中文.pdf").write_bytes(
    build_text_pdf(
        ["中文验收报告", "第一页标记 " + marks[0]],
        ["第二章 数据处理", "第二页标记 " + marks[1]],
        ["第三章 结论", "第三页标记 " + marks[2]],
    )
)

# 英文文字版
(out / "文字版-英文.pdf").write_bytes(
    build_english_pdf(
        ["English Acceptance Report", "Page one mark " + en_mark],
        ["Second Chapter", "Page two mark " + en_mark],
    )
)

# 扫描版：整页就是一张图，没有任何文字层，只能靠 OCR
(out / "扫描版.pdf").write_bytes(build_scanned_pdf(list(scan_marks)))

# 混排：第 1 页是字、第 2 页是图、第 3 页又是字
(out / "混排.pdf").write_bytes(
    build_mixed_pdf(
        [
            ("text", ["混合文档首页", "文字页标记 " + mix_mark]),
            ("scan", scan_marks[0]),
            ("text", ["混合文档末页", "文字页标记 " + mix_mark]),
        ]
    )
)

(out / "空.pdf").write_bytes(build_empty_pdf())
(out / "损坏.pdf").write_bytes(b"%PDF-1.7\n" + b"\x00" * 400)
(out / "改名.pdf").write_bytes(b"MZ\x90\x00" + b"\x00" * 2048)

# 加密 PDF：PyMuPDF 能读 0 页的文档但存不出 0 页的文档，所以手写内容
doc = pymupdf.open()
try:
    page = doc.new_page()
    page.insert_text((72, 100), "locked acceptance document", fontsize=12)
    locked = doc.tobytes(
        encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw="owner", user_pw="secret"
    )
finally:
    doc.close()
(out / "加密.pdf").write_bytes(locked)

print(json.dumps(sorted(p.name for p in out.iterdir()), ensure_ascii=False))
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


# ----------------------------------------------------------------------
# DOCX 事实：只用标准库读，所以能抓到「python-docx 自己读得回来、
# Word 读不回来」这类故障
# ----------------------------------------------------------------------

DOCX_REQUIRED_PARTS = ("[Content_Types].xml", "_rels/.rels", "word/document.xml")


def docx_facts(path: pathlib.Path) -> dict:
    """下载到的 DOCX 的客观事实。判定一律用它，不看界面文案。"""
    with zipfile.ZipFile(path) as archive:
        broken = archive.testzip()
        names = archive.namelist()
        xml = archive.read("word/document.xml").decode("utf-8", "replace")
        media = [name for name in names if name.startswith("word/media/")]

    # 分页符是 DOCX 里页序**唯一**真实的表示：DOCX 没有「页」这个东西
    segments = xml.split('<w:br w:type="page"/>')
    # 去掉标签后剩下的就是文字；&amp; 这类实体要还原，否则比对会莫名其妙地差一个字符
    plain = html.unescape(re.sub(r"<[^>]+>", "", xml))
    return {
        "broken": broken,
        "missing": [part for part in DOCX_REQUIRED_PARTS if part not in names],
        "pages": segments,
        "page_texts": [html.unescape(re.sub(r"<[^>]+>", "", part)) for part in segments],
        "text": plain,
        "media": media,
        "has_docx_rels": "word/_rels/document.xml.rels" in names,
    }


def normalized(text: str) -> str:
    """去掉所有空白再比对：抽取出来的文字里会凭空多出空格（行内分栏、缩进）。"""
    return "".join(text.split())


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


# ----------------------------------------------------------------------
# 页面操作
# ----------------------------------------------------------------------

_download_seq = 0


def select_file(page: Page, route: str, source: pathlib.Path, *, base: str = BASE) -> None:
    page.goto(f"{base}{route}", wait_until="networkidle")
    page.wait_for_selector("input[type=file]", state="attached", timeout=30_000)
    page.set_input_files("input[type=file]", str(source))
    page.wait_for_selector("text=开始转换", timeout=30_000)


def start_and_wait(page: Page, *, timeout: int = 240_000) -> None:
    """点开始，等结果面板出现。

    结束信号必须是「下载 Word」而不是「转换完成」：结果面板顶部那句
    「✓ 转换完成」和别处的静态文案都可能先出现，一等就返回，读到的是还没转完的页面。
    """
    button = page.get_by_role("button", name="开始转换")
    for _ in range(150):
        if button.is_enabled():
            break
        page.wait_for_timeout(200)
    assert button.is_enabled(), "开始按钮是禁用的，流程走不下去"
    button.click()
    page.wait_for_selector("text=下载 Word", timeout=timeout)
    page.wait_for_timeout(300)


def download(page: Page) -> pathlib.Path:
    global _download_seq
    with page.expect_download(timeout=120_000) as info:
        page.get_by_role("button", name="下载 Word").click()
    item = info.value
    _download_seq += 1
    DOWNLOADS.mkdir(parents=True, exist_ok=True)
    target = DOWNLOADS / f"{_download_seq:02d}-{item.suggested_filename}"
    item.save_as(target)
    return target


def convert(page: Page, source: pathlib.Path, *, base: str = BASE, timeout: int = 240_000):
    """走完一遍：选文件 → 开始 → 下载。返回 (结果页文字, 下载到的文件)。"""
    select_file(page, ROUTE, source, base=base)
    start_and_wait(page, timeout=timeout)
    text = body(page)
    return text, download(page)


# ----------------------------------------------------------------------
# 直接打接口：浏览器看到的只是渲染后的文字，这里看的是原始响应体
# ----------------------------------------------------------------------


def post_pdf(
    endpoint: str,
    filename: str,
    data: bytes,
    form: dict | None = None,
    *,
    base: str = BASE,
) -> tuple[int, str]:
    boundary = uuid.uuid4().hex
    parts = [
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            f"Content-Type: application/pdf\r\n\r\n"
        ).encode()
        + data
        + b"\r\n"
    ]
    for key, value in (form or {}).items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode()
        )
    parts.append(f"--{boundary}--\r\n".encode())

    request = urllib.request.Request(
        f"{base}/api/office/{endpoint}",
        data=b"".join(parts),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=600) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def check_no_leak(raw: str, label: str) -> None:
    leaked = [fragment for fragment in FORBIDDEN_FRAGMENTS if fragment in raw]
    check(not leaked, f"{label}：响应里没有泄露内部细节" + (f"（泄露了 {leaked}）" if leaked else ""))


def config_of(base: str) -> dict:
    with urllib.request.urlopen(f"{base}/api/config", timeout=10) as response:
        return json.load(response)


# ----------------------------------------------------------------------
# A 段：入口
# ----------------------------------------------------------------------


def run_part_a(page: Page) -> None:
    section("A 段：入口")

    page.goto(BASE + "/", wait_until="networkidle")
    text = body(page)
    check("PDF 转 Word" in text, "首页有「PDF 转 Word」卡片")

    page.goto(BASE + "/pdf", wait_until="networkidle")
    check("PDF 转 Word" in body(page), "/pdf 列表页列出了 PDF 转 Word")

    page.goto(BASE + ROUTE, wait_until="networkidle")
    text = body(page)
    check("PDF 转 Word" in text, f"{ROUTE} 打开的是「PDF 转 Word」")

    # 两条质量声明必须在**上传之前**就能看到：用户是在决定要不要用的时候
    # 需要知道它做不到什么，而不是白等一场之后
    for note in QUALITY_NOTES:
        check(note in text, f"页面直接写着「{note}」")

    # 顶栏导航能到 PDF 工具
    page.goto(BASE + "/", wait_until="networkidle")
    page.get_by_role("link", name="PDF 工具").first.click()
    page.wait_for_load_state("networkidle")
    check("/pdf" in page.url, f"顶栏「PDF工具」能进列表页（{page.url}）")

    health = config_of(BASE)
    check(health.get("pdf_to_word_available") is True, "/api/config 报 pdf_to_word_available = true")
    check(
        isinstance(health.get("ocr_languages"), list) and health["ocr_languages"],
        f"/api/config 报出 OCR 字符集 {health.get('ocr_languages')}",
    )


# ----------------------------------------------------------------------
# B 段：文字版 PDF
# ----------------------------------------------------------------------


def check_result_page(text: str, *, kind: str, pages: int, method: str, label: str) -> None:
    """结果页要如实说清四件事（规格 §结果页）。"""
    for field in RESULT_FIELDS:
        check(field in text, f"{label}：结果页有「{field}」一项")
    check(kind in text, f"{label}：结果页写明类型是「{kind}」")
    check(f"{pages} 页" in text, f"{label}：结果页写明「{pages} 页」")
    check(method in text, f"{label}：结果页写明识别方式是「{method}」")
    check(".docx" in text, f"{label}：结果页给出了输出文件名（.docx）")


def check_docx_opens(path: pathlib.Path, label: str) -> dict:
    """产物本身：能打开、页序对、文字在。这三条比 HTTP 200 硬得多。"""
    facts = docx_facts(path)
    check(facts["broken"] is None, f"{label}：DOCX 压缩包完整（zipfile 自检通过）")
    check(not facts["missing"], f"{label}：DOCX 结构正常（必需条目齐全）")
    return facts


def run_part_b(page: Page) -> None:
    section("B 段：文字版 PDF")

    # --- 中文 ---
    text, target = convert(page, SAMPLES / "文字版-中文.pdf")
    check_result_page(text, kind="文字 PDF", pages=3, method="直接提取文字层", label="中文三页")
    check(target.suffix == ".docx", f"中文三页：下载下来的确实是 Word（{target.name}）")

    facts = check_docx_opens(target, "中文三页")
    check(
        len(facts["pages"]) == 3,
        f"中文三页：分页符把文档切成 3 段（实际 {len(facts['pages'])}）",
    )
    for index, mark in enumerate(PAGE_MARKS):
        check(
            mark in normalized(facts["page_texts"][index]),
            f"中文三页：第 {index + 1} 段的文字是第 {index + 1} 页的（找了 {mark}）",
        )
    check("中文验收报告" in normalized(facts["text"]), "中文三页：正文文字完整")
    check(not facts["media"], "中文三页：纯文字页没有塞进多余的图片")

    # --- 英文 ---
    text, target = convert(page, SAMPLES / "文字版-英文.pdf")
    check_result_page(text, kind="文字 PDF", pages=2, method="直接提取文字层", label="英文两页")
    facts = check_docx_opens(target, "英文两页")
    check(len(facts["pages"]) == 2, f"英文两页：切成 2 段（实际 {len(facts['pages'])}）")
    check(
        EN_MARK in normalized(facts["text"]) and "English Acceptance Report" in facts["text"],
        "英文两页：英文原文完整（空格没有被吃掉）",
    )

    # --- 中英混排 + 逐页判定：第 2 页是图，只该它走 OCR ---
    text, target = convert(page, SAMPLES / "混排.pdf")
    check("混合 PDF" in text, "混排：结果页写明类型是「混合 PDF」")
    check("直接提取 + OCR 识别" in text, "混排：结果页写明识别方式是「直接提取 + OCR 识别」")
    check("3 页" in text, "混排：结果页写明 3 页")
    facts = check_docx_opens(target, "混排")
    check(len(facts["pages"]) == 3, f"混排：切成 3 段（实际 {len(facts['pages'])}）")
    check(MIX_TEXT_MARK in normalized(facts["page_texts"][0]), "混排：第 1 页是直接提取的文字")
    check(MIX_TEXT_MARK in normalized(facts["page_texts"][2]), "混排：第 3 页是直接提取的文字")
    check(
        len(facts["media"]) >= 1,
        f"混排：扫描的那一页把原图保留进了 Word（内嵌 {len(facts['media'])} 张图）",
    )
    check(
        normalized(SCAN_MARKS[0]) in normalized(facts["page_texts"][1]),
        f"混排：第 2 页（扫描页）识别出了文字「{SCAN_MARKS[0]}」",
    )


# ----------------------------------------------------------------------
# C 段：扫描版 PDF（真实 OCR）
# ----------------------------------------------------------------------


def run_part_c(page: Page) -> None:
    section("C 段：扫描版 PDF（真实 OCR）")

    config = config_of(BASE)
    if not config.get("ocr_available"):
        check(False, "本机后端没有装 OCR 组件，C 段无法验收 —— 请先装 requirements-ocr.txt")
        return

    started = time.monotonic()
    text, target = convert(page, SAMPLES / "扫描版.pdf")
    elapsed = time.monotonic() - started
    print(f"  扫描件转换耗时 {elapsed:.1f} 秒")

    check_result_page(text, kind="扫描 PDF", pages=2, method="OCR 识别", label="扫描两页")
    facts = check_docx_opens(target, "扫描两页")
    check(len(facts["pages"]) == 2, f"扫描两页：切成 2 段（实际 {len(facts['pages'])}）")

    # 原图必须保留：OCR 会认错字，用户得能对照原件核对
    check(
        len(facts["media"]) == 2,
        f"扫描两页：两页的原图都保留进了 Word（内嵌 {len(facts['media'])} 张图）",
    )
    check(facts["has_docx_rels"], "扫描两页：图片关系表齐全（图是真嵌进去的，不是空引用）")

    # 文字是**可编辑的真段落**，不是图片上的字
    for index, mark in enumerate(SCAN_MARKS):
        check(
            normalized(mark) in normalized(facts["page_texts"][index]),
            f"扫描两页：第 {index + 1} 页识别出了「{mark}」",
        )
    check(
        "以下文字由 OCR 识别" in facts["text"],
        "扫描两页：每页的识别结果上方标明了这是 OCR 认出来的",
    )

    # 结果页要如实提醒 OCR 可能认错字
    check("OCR" in text and "错字" in text, "扫描两页：结果页提醒识别结果可能有错字")


# ----------------------------------------------------------------------
# D 段：进度是真实的，不是编的
# ----------------------------------------------------------------------

#: 状态条上允许出现的文案。**没有一个是百分比数字**：
#: 除了上传（XHR 的真实字节进度）和 OCR 期间的真实页码，这一阶段
#: 拿不出任何诚实的百分比，所以那里显示的是阶段名。
ALLOWED_STATUS = (
    "正在上传 PDF…",
    "等待处理",
    "正在分析 PDF…",
    "正在检测文字层…",
    "正在提取内容…",
    "正在生成 Word…",
    "正在验证 Word…",
)


def capture_progress(page: Page, source: pathlib.Path, *, base: str = BASE) -> list[str]:
    """点开始之后不停读状态条，把出现过的文案都收集起来。

    状态条要**按步骤文案定位**，不能拿 ``[role=status]`` 的第一个：
    ``Alert`` 组件的信息提示也是 ``role=status``，而页面上那两条质量声明
    排在状态条前面，取第一个会一直读到质量声明 —— 那是个看起来很像
    「进度条坏了」的假红。步骤名「生成 Word」只有状态条里有。
    """
    select_file(page, ROUTE, source, base=base)
    seen: list[str] = []
    button = page.get_by_role("button", name="开始转换")
    for _ in range(150):
        if button.is_enabled():
            break
        page.wait_for_timeout(200)
    button.click()
    for _ in range(400):  # 最多 80 秒
        text = body(page)
        if "下载 Word" in text:
            break
        panel = page.locator("[role=status]").filter(has_text="生成 Word").first
        if panel.count() > 0:
            line = panel.inner_text().splitlines()[0].strip()
            if line and (not seen or seen[-1] != line):
                seen.append(line)
        page.wait_for_timeout(200)
    return seen


def run_part_d(page: Page) -> None:
    section("D 段：进度是真实的阶段，不是假的百分比")

    seen = capture_progress(page, SAMPLES / "扫描版.pdf")
    print(f"  状态条依次出现：{seen}")

    unknown = [
        line
        for line in seen
        if not any(line.startswith(prefix) for prefix in ALLOWED_STATUS)
        and not re.fullmatch(r"正在进行 OCR…（第 \d+ / 共 \d+ 页）", line)
    ]
    check(not unknown, f"状态条上的文案都是真实阶段" + (f"（出现了没见过的 {unknown}）" if unknown else ""))

    ocr_lines = [line for line in seen if line.startswith("正在进行 OCR…")]
    check(bool(ocr_lines), "转换过程中状态条显示过「正在进行 OCR…」")
    check(
        any(re.fullmatch(r"正在进行 OCR…（第 \d+ / 共 \d+ 页）", line) for line in ocr_lines),
        f"OCR 期间给的是真实页码而不是假百分比（{ocr_lines[:2]}）",
    )

    # 下载掉，别把结果留在服务器上影响后面的用例
    download(page)


# ----------------------------------------------------------------------
# E 段：缺 OCR 的后端
# ----------------------------------------------------------------------


def start_noocr_backend() -> subprocess.Popen:
    """另起一个后端，用 ``FILETOOLS_OCR_DISABLED=1`` 关掉 OCR。

    这是**唯一**能真实复现「服务器没装 OCR 组件」的办法：
    在同一个进程里 monkeypatch 只是测试内部的假动作，
    而这里前端拿到的是真的 ``ocr_available === false``。
    """
    SERVER_TEMP.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update(
        {
            "FILETOOLS_OCR_DISABLED": "1",
            "FILETOOLS_TEMP_ROOT": str(SERVER_TEMP),
            "PYTHONIOENCODING": "utf-8",
        }
    )
    log = open(NOOCR_LOG, "wb")
    process = subprocess.Popen(
        [
            str(BACKEND_PYTHON),
            "-m",
            "uvicorn",
            "main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(NOOCR_PORT),
        ],
        cwd=str(BACKEND),
        env=env,
        stdout=log,
        stderr=log,
    )
    for _ in range(80):
        try:
            with urllib.request.urlopen(f"{NOOCR_BASE}/api/health", timeout=1) as response:
                if response.status == 200:
                    return process
        except (urllib.error.URLError, OSError):
            time.sleep(0.5)
    process.terminate()
    raise SystemExit(f"缺 OCR 的后端没能启动，看日志：{NOOCR_LOG}")


def run_part_e(page: Page) -> None:
    section("E 段：缺 OCR 的后端")
    process = start_noocr_backend()
    try:
        config = config_of(NOOCR_BASE)
        check(config.get("ocr_available") is False, "缺 OCR 的后端如实上报 ocr_available = false")
        check(
            config.get("pdf_to_word_available") is True,
            "缺 OCR 不影响 pdf_to_word_available —— 文字版 PDF 还能转",
        )

        # 前端在上传之前就说清楚
        page.goto(NOOCR_BASE + ROUTE, wait_until="networkidle")
        text = body(page)
        check(
            "当前服务器未安装 OCR 组件，暂时无法处理扫描 PDF。" in text,
            "页面上直接给出「当前服务器未安装 OCR 组件，暂时无法处理扫描 PDF。」",
        )
        check("文字版 PDF 仍可正常转换" in text, "并且说明文字版 PDF 仍可正常转换")

        # 扫描件：不是 500，也不是白屏，是一句能看懂的中文
        select_file(page, ROUTE, SAMPLES / "扫描版.pdf", base=NOOCR_BASE)
        button = page.get_by_role("button", name="开始转换")
        check(
            button.is_enabled(),
            "缺 OCR 时按钮仍然可用 —— 用户传的可能是一份文字版 PDF，不该替他判死",
        )
        button.click()
        # 等错误提示里那句建议出现：页面上本来就有一句静态的「未安装 OCR 组件」，
        # 等它等于没等，会读到还没提交的页面
        appeared = wait_for_text(page, "需要处理扫描件请联系管理员", timeout=60_000)
        text = body(page)
        check(appeared and "未安装 OCR 组件" in text, "扫描件在缺 OCR 的服务器上给出中文说明")
        check(
            "需要处理扫描件请联系管理员" in text,
            "错误提示建议的是「联系管理员」而不是「请重试」—— 重试治不了没装组件",
        )
        check("Traceback" not in text, "界面上没有 Python 堆栈")
        check("下载 Word" not in text, "失败了就没有下载按钮 —— 不假装成功")

        # --- 最重要的一条：文字版 PDF 在没装 OCR 的服务器上照常能转 ---
        text, target = convert(page, SAMPLES / "文字版-中文.pdf", base=NOOCR_BASE)
        facts = check_docx_opens(target, "缺 OCR 时的文字版")
        check(
            PAGE_MARKS[0] in normalized(facts["text"]),
            "缺 OCR 的服务器上，文字版 PDF 依然转得出真正的 Word",
        )

        # 原始响应体：浏览器渲染后的文字看不出响应里到底带了什么
        status, raw = post_pdf(
            "pdf-to-word",
            "扫描版.pdf",
            (SAMPLES / "扫描版.pdf").read_bytes(),
            base=NOOCR_BASE,
        )
        check(status == 503, f"扫描件直接打接口是 503 而不是 500（实际 {status}）")
        check("OCR_UNAVAILABLE" in raw, "接口返回的错误码是 OCR_UNAVAILABLE")
        check("未安装 OCR 组件" in raw, "服务端说清是没装组件，而不是笼统的失败")
        check_no_leak(raw, "缺 OCR 时的扫描件")
    finally:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()


# ----------------------------------------------------------------------
# F 段：负例与不泄露
# ----------------------------------------------------------------------


def run_part_f(page: Page) -> None:
    section("F 段：负例与不泄露")

    cases = (
        ("空.pdf", "PDF_EMPTY", 400, "一页都没有"),
        ("损坏.pdf", "CORRUPTED_FILE", 400, ""),
        ("加密.pdf", "PDF_ENCRYPTED", 400, "密码"),
        ("改名.pdf", "INVALID_FILE_TYPE", 415, ""),
    )
    for name, code, expected_status, phrase in cases:
        status, raw = post_pdf("pdf-to-word", name, (SAMPLES / name).read_bytes())
        check(
            status == expected_status and code in raw,
            f"{name}：{expected_status} / {code}（实际 {status}）",
        )
        if phrase:
            check(phrase in raw, f"{name}：错误里说清了「{phrase}」")
        check_no_leak(raw, name)

    # 超大文件：必须在**读进内存之前**就被挡住
    limit = config_of(BASE)["max_upload_bytes"]
    oversized = b"%PDF-1.4\n" + b"0" * (limit + 1024)
    status, raw = post_pdf("pdf-to-word", "超大.pdf", oversized)
    check(status == 413, f"超过 {limit} 字节的文件被拒（实际 {status}）")
    check_no_leak(raw, "超大文件")

    # 界面上传空 PDF：说清原因，不留一个点了没反应的按钮
    page.goto(BASE + ROUTE, wait_until="networkidle")
    page.wait_for_selector("input[type=file]", state="attached", timeout=30_000)
    page.set_input_files("input[type=file]", str(SAMPLES / "空.pdf"))
    page.wait_for_selector("text=开始转换", timeout=30_000)
    page.get_by_role("button", name="开始转换").click()
    page.wait_for_timeout(2_500)
    text = body(page)
    check("一页都没有" in text, "空 PDF 在界面上给出中文原因")
    check("Traceback" not in text, "界面上没有 Python 堆栈")


# ----------------------------------------------------------------------
# G 段：移动端
# ----------------------------------------------------------------------


def run_part_g(p) -> None:
    section("G 段：移动端布局")
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
            for route in ("/", "/pdf", ROUTE):
                page.goto(BASE + route, wait_until="networkidle")
                overflow = page.evaluate(
                    "() => document.documentElement.scrollWidth"
                    " - document.documentElement.clientWidth"
                )
                if overflow > worst:
                    worst, worst_route = overflow, route
            check(
                worst <= 1,
                f"{width}px：PDF 转 Word 相关页面没有横向溢出"
                f"（最大 {worst}px{' @' + worst_route if worst_route else ''}）",
            )

            page.goto(BASE + ROUTE, wait_until="networkidle")
            page.wait_for_selector("input[type=file]", state="attached", timeout=30_000)
            page.set_input_files("input[type=file]", str(SAMPLES / "文字版-中文.pdf"))
            page.wait_for_selector("text=开始转换", timeout=30_000)
            button = page.get_by_role("button", name="开始转换")
            box = button.bounding_box()
            check(
                box is not None and box["height"] >= 44,
                f"{width}px：开始按钮高 {round(box['height']) if box else 0}px，适合手指点击",
            )
            ctx.close()
    finally:
        browser.close()


# ----------------------------------------------------------------------
# H 段：前五阶段回归
# ----------------------------------------------------------------------

PHASE_ROUTES = (
    "/image/compress",
    "/pdf/from-images",
    "/pdf/to-images",
    "/doc/word",
)


def run_part_h(page: Page) -> None:
    section("H 段：前五阶段回归")

    for route in PHASE_ROUTES:
        page.goto(BASE + route, wait_until="networkidle")
        page.wait_for_selector("input[type=file]", state="attached", timeout=30_000)
        check(page.locator("input[type=file]").count() > 0, f"{route} 仍能打开并接受文件")

    # 真跑一次第一阶段的图片压缩与第五阶段的 Word 转 PDF：
    # 「页面能打开」证明不了接口没被改坏
    sample = SAMPLES / "季度报告.docx"
    if sample.exists():
        select_file(page, "/doc/word", sample)
        page.get_by_role("button", name="开始转换").click()
        page.wait_for_selector("text=下载文件", timeout=180_000)
        check(True, "第五阶段的 Word → PDF 仍然跑得通（下载按钮出现了）")


# ----------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------


def build_samples() -> None:
    if (SAMPLES / "文字版-中文.pdf").exists() and (SAMPLES / "季度报告.docx").exists():
        print(f"复用已有样张：{SAMPLES}")
        return
    print("正在生成验收样张…")
    names = _run_backend_python(
        _SAMPLE_CODE, SAMPLES, *PAGE_MARKS, EN_MARK, MIX_TEXT_MARK, *SCAN_MARKS
    )
    print(f"  已生成：{json.loads(names.strip().splitlines()[-1])}")


def build_phase5_sample() -> None:
    """第五阶段的 Word 样张（H 段回归用），借它的构造器生成。"""
    if (SAMPLES / "季度报告.docx").exists():
        return
    _run_backend_python(
        "import pathlib, sys;"
        " from tests.conftest import build_docx_bytes;"
        " pathlib.Path(sys.argv[1]).write_bytes(build_docx_bytes(sys.argv[2]))",
        SAMPLES / "季度报告.docx",
        "ACCEPT6-第五阶段回归",
    )


def main() -> int:
    build_samples()
    build_phase5_sample()

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
            run_part_h(page)
        finally:
            browser.close()
        run_part_g(p)

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
