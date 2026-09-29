"""第五阶段的真机验收脚本（Playwright + 真实浏览器 + 真实转换）。

验的是「用户拿到一份 Word / Excel / PPT / TXT，能不能真的换到一份能用的 PDF」，
所以每一处判定都落在**下载到的 PDF 本身**上：用 PyMuPDF 打开它、读它的页数、
页面尺寸、嵌入字体和正文文字。界面上写着「转换完成」不算数 ——
转出一页白纸的接口同样返回 200。

分段：

    A 段  入口（首页分类、/doc 列表页、四张卡片）
    B 段  四种 OOXML/TXT 格式 + 三种旧格式，各走一遍完整流程并校验产物
    C 段  TXT 四个排版选项真的改变产物（页数 / 页面尺寸 / 嵌入字体）
    D 段  最大文件大小：达不到时如实说明，不谎称达标
    E 段  负例与不泄露（改名可执行文件、损坏文档、传错页面）
    F 段  缺组件（另起一个后端，把组件路径指到不存在的地方）
    G 段  移动端 375 / 390 / 414 无横向溢出

用法：
    1. 先构建前端并启动后端（后端会顺带托管 frontend/dist）：
           cd frontend && npm run build
           cd backend && .venv\\Scripts\\python -m uvicorn main:app --port 8011
    2. python scripts/verify_phase5.py

可用环境变量覆盖：
    FILETOOLS_WEB_BASE      默认 http://127.0.0.1:8011（直接打后端托管的 dist）
    FILETOOLS_BACKEND_PY    后端 venv 的 python，用于生成素材与校验下载结果
    FILETOOLS_NOCOMP_PORT   F 段缺组件后端的端口，默认 8013
    FILETOOLS_WORK_DIR      复用上一轮的素材目录，省掉重新生成的时间

注意：不要用 Vite 开发服务器（5173）跑本脚本 —— 它会把自己后端的地址
代理给 /api，两者版本不一致时结果没有意义。

旧格式（.doc / .xls / .ppt）的样张是 scripts/make_office_fixtures.py 生成的
真文件；没生成过的话 B 段会如实报 FAIL，而不是悄悄跳过。
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

from playwright.sync_api import Page, sync_playwright

ROOT = pathlib.Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
BASE = os.environ.get("FILETOOLS_WEB_BASE", "http://127.0.0.1:8011").rstrip("/")
NOCOMP_PORT = int(os.environ.get("FILETOOLS_NOCOMP_PORT", "8013"))
NOCOMP_BASE = f"http://127.0.0.1:{NOCOMP_PORT}"

# Windows 控制台默认是 GBK，直接打印「✓」会抛 UnicodeEncodeError 把整轮跑挂掉
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

WORK = pathlib.Path(
    os.environ.get("FILETOOLS_WORK_DIR") or tempfile.mkdtemp(prefix="filetools-verify-p5-")
)
SAMPLES = WORK / "samples"
DOWNLOADS = WORK / "downloads"
SERVER_TEMP = WORK / "server-temp"
REPORT = ROOT / "scripts" / "verify_phase5_report.txt"
NOCOMP_LOG = WORK / "backend-nocomp.log"

#: 验收样张正文里的标记。用和 pytest 样张不同的串：
#: 万一产物其实来自别处，这里对不上就能发现。
ACCEPT_MARKER = "ACCEPT-验收标记-2026"
TXT_MARKER = "TXT-验收标记-2026"

MOBILE_WIDTHS = (375, 390, 414)

#: 出错响应里绝不能出现的东西（§十三 / §十八）
FORBIDDEN_FRAGMENTS = (
    "Traceback",
    'File "',
    "\\",                       # Windows 路径分隔符
    "/tmp/",
    "AppData",
    "site-packages",
    "filetools_",
    "office-converter-profile",
    "soffice",
)


# ----------------------------------------------------------------------
# 样张：复用后端测试里的那套构造器，避免同一份 XML 在这里再抄一遍
# ----------------------------------------------------------------------

_SAMPLE_CODE = r'''
import json, pathlib, shutil, sys

# 从 tests/conftest.py 借样张构造器：同一份 XML 在验收脚本里再抄一遍，
# 迟早会和 pytest 用的那份漂移，而两边分头绿着谁也发现不了。
from tests.conftest import (
    OFFICE_MARKER,
    build_docx_bytes,
    build_pptx_bytes,
    build_xlsx_bytes,
    zip_bytes,
)

marker, txt_marker, out, fixtures = sys.argv[1:5]
out = pathlib.Path(out)
fixtures = pathlib.Path(fixtures)
out.mkdir(parents=True, exist_ok=True)

(out / "季度报告.docx").write_bytes(build_docx_bytes(marker))
(out / "季度表格.xlsx").write_bytes(build_xlsx_bytes(marker))
(out / "产品发布.pptx").write_bytes(build_pptx_bytes(marker, slides=3))
(out / "长篇报告.docx").write_bytes(build_docx_bytes(marker, paragraphs=800))

body = "".join(
    "第%d段：这是一份用于验收的纯文本，中英文混排 with English words。\n" % i
    for i in range(1, 201)
)
(out / "会议记录.txt").write_text(txt_marker + "\n" + body, encoding="utf-8")
(out / "gbk笔记.txt").write_bytes(
    (txt_marker + "\n" + body).encode("gbk")
)

# 负例：容器齐全但正文 XML 坏了
(out / "坏文档.docx").write_bytes(
    zip_bytes(
        {
            "[Content_Types].xml": (
                '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org'
                '/package/2006/content-types"><Override PartName="/word/document.xml" '
                'ContentType="application/vnd.openxmlformats-officedocument.'
                'wordprocessingml.document.main+xml"/></Types>'
            ),
            "word/document.xml": "<w:document>没有闭合的标签 <<<",
        }
    )
)
# 负例：可执行文件改名成 .docx
(out / "发票.docx").write_bytes(b"MZ\x90\x00" + b"\x00" * 2048)

# 旧格式样张（由 scripts/make_office_fixtures.py 生成并提交到版本库）
for name in ("sample.doc", "sample.xls", "sample.ppt"):
    source = fixtures / name
    if source.is_file():
        shutil.copyfile(source, out / name)

print(json.dumps(sorted(p.name for p in out.iterdir()), ensure_ascii=False))
'''

#: 读一份 PDF 的客观事实：页数、页面尺寸、嵌入字体、正文
_PDF_CODE = r'''
import json, sys

import pymupdf

doc = pymupdf.open(sys.argv[1])
try:
    fonts = sorted({item[3] for i in range(doc.page_count) for item in doc.get_page_fonts(i)})
    print(json.dumps({
        "pages": doc.page_count,
        "rect": [round(doc[0].rect.width, 1), round(doc[0].rect.height, 1)],
        "fonts": fonts,
        "text": "".join(page.get_text() for page in doc),
    }, ensure_ascii=False))
finally:
    doc.close()
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
    只要子进程打印一个汉字就 UnicodeDecodeError —— 而且是在**读取线程**里炸，
    真正的报错（子进程自己为什么失败）会被一起吃掉，看起来像是脚本莫名其妙挂了。
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


def pdf_facts(path: pathlib.Path) -> dict:
    """下载到的 PDF 的客观事实。判定一律用它，不看界面文案。"""
    return json.loads(_run_backend_python(_PDF_CODE, path))


def normalized(text: str) -> str:
    """去掉所有空白再比对。

    LibreOffice 会把拉丁字母和汉字拆成两段，抽出来的文字里会凭空多一个空格
    （实测 `ACCEPT-验收标记-2026` 会变成 `ACCEPT- 验收标记-2026`）。
    """
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


# ----------------------------------------------------------------------
# 页面操作
# ----------------------------------------------------------------------

_download_seq = 0


def pick(page: Page, group: str, value: str) -> None:
    """点中某个 RadioGroup 里的一个选项。"""
    page.locator(f'label:has(input[name="{group}"][value="{value}"])').click()


def convert(
    page: Page,
    route: str,
    source: pathlib.Path,
    *,
    base: str = BASE,
    options=None,
) -> tuple[str, pathlib.Path]:
    """在某个文档页上走一遍：选文件 → （选选项）→ 开始转换 → 下载。

    返回 (页面上的完整文字, 下载到的文件路径)。

    结束信号必须是「下载文件」而不是「转换完成」：选项区那句静态说明
    「转换完成后原始文档会立即从服务器删除」里就带着「转换完成」四个字，
    一等就立刻返回，读到的是还没转换的页面。
    """
    global _download_seq
    page.goto(f"{base}{route}", wait_until="networkidle")
    page.wait_for_selector("input[type=file]", state="attached", timeout=30_000)
    page.set_input_files("input[type=file]", str(source))
    page.wait_for_selector("text=开始转换", timeout=30_000)

    if options is not None:
        options(page)
        page.wait_for_timeout(200)

    button = page.get_by_role("button", name="开始转换")
    for _ in range(150):
        if button.is_enabled():
            break
        page.wait_for_timeout(200)
    assert button.is_enabled(), f"{route} 上的开始按钮是禁用的，流程走不下去"
    button.click()

    page.wait_for_selector("text=下载文件", timeout=180_000)
    page.wait_for_timeout(300)
    text = body(page)

    with page.expect_download(timeout=120_000) as info:
        page.get_by_role("button", name="下载文件").click()
    item = info.value
    _download_seq += 1
    DOWNLOADS.mkdir(parents=True, exist_ok=True)
    target = DOWNLOADS / f"{_download_seq:02d}-{item.suggested_filename}"
    item.save_as(target)
    return text, target


def rejected(page: Page, route: str, source: pathlib.Path, *, base: str = BASE) -> str:
    """传一个**扩展名合法、内容有问题**的文件：走完上传，等服务端拒绝。

    这里等的是**结果本身**（`✓` 结果面板或 `role=alert` 的错误条），
    不是一个固定的秒数。原因很实在：这一路上排着 LibreOffice，
    一个坏 docx 要它先起进程、再失败，实测 8–13 秒；固定等 2.5 秒
    会在机器稍忙时读到一张还没更新的页面，把「服务端说得很清楚」
    报成失败 —— 那是脚本在说谎，不是产品有问题。
    """
    page.goto(f"{base}{route}", wait_until="networkidle")
    page.wait_for_selector("input[type=file]", state="attached", timeout=30_000)
    page.set_input_files("input[type=file]", str(source))
    page.wait_for_selector("text=开始转换", timeout=30_000)
    page.get_by_role("button", name="开始转换").click()
    page.wait_for_function(
        "() => document.body.innerText.includes('✓ ') "
        "|| !!document.querySelector('[role=alert]')",
        timeout=180_000,
    )
    page.wait_for_timeout(300)
    return body(page)


def rejected_locally(page: Page, route: str, source: pathlib.Path) -> tuple[str, bool]:
    """传一个**扩展名就不属于这一页**的文件，返回 (页面文字, 有没有开始按钮)。

    这类文件在前端就被挡住了（`utils/validation.ts` 按各页的 accept 列表判断），
    根本发不出去，所以断言点不是服务端文案，而是「界面说清了该传什么」
    和「没有留下一个点了也没用的按钮」。
    """
    page.goto(f"{BASE}{route}", wait_until="networkidle")
    page.wait_for_selector("input[type=file]", state="attached", timeout=30_000)
    page.set_input_files("input[type=file]", str(source))
    page.wait_for_timeout(1_500)
    button = page.get_by_role("button", name="开始转换")
    visible = button.count() > 0 and button.first.is_visible()
    return body(page), visible


# ----------------------------------------------------------------------
# 直接打接口：浏览器看到的只是渲染后的文字，这里看的是原始响应体，
# 「不泄露」这条只有看原文才算验过
# ----------------------------------------------------------------------


def post_doc(endpoint: str, filename: str, data: bytes, form: dict | None = None) -> tuple[int, str]:
    boundary = uuid.uuid4().hex
    parts = [
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            f"Content-Type: application/octet-stream\r\n\r\n"
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
        f"{BASE}/api/office/{endpoint}",
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


# ----------------------------------------------------------------------
# A 段：入口
# ----------------------------------------------------------------------

DOC_ROUTES = ("/doc/word", "/doc/excel", "/doc/ppt", "/doc/txt")
DOC_TITLES = ("Word 转 PDF", "Excel 转 PDF", "PPT 转 PDF", "TXT 转 PDF")


def run_part_a(page: Page) -> None:
    section("A 段：入口")

    page.goto(BASE + "/", wait_until="networkidle")
    text = body(page)
    check("文档转换" in text, "首页有一级分类「文档转换」")
    for title in DOC_TITLES:
        check(title in text, f"首页有「{title}」卡片")

    page.goto(BASE + "/doc", wait_until="networkidle")
    text = body(page)
    check(all(title in text for title in DOC_TITLES), "/doc 列表页列出了四个工具")

    for route, title in zip(DOC_ROUTES, DOC_TITLES):
        page.goto(BASE + route, wait_until="networkidle")
        text = body(page)
        check(title in text, f"{route} 打开的是「{title}」")

    # 顶栏导航能到文档转换
    page.goto(BASE + "/", wait_until="networkidle")
    page.get_by_role("link", name="文档转换").first.click()
    page.wait_for_load_state("networkidle")
    check("/doc" in page.url, f"顶栏「文档转换」能进列表页（{page.url}）")


# ----------------------------------------------------------------------
# B 段：四种新格式 + 三种旧格式
# ----------------------------------------------------------------------


def check_conversion(
    page: Page,
    route: str,
    source: pathlib.Path,
    marker: str,
    label: str,
    *,
    expected_pages: int | None = None,
) -> pathlib.Path:
    """走完一遍流程，并用下载到的 PDF 本身回答「转成功了吗」。"""
    text, target = convert(page, route, source)
    facts = pdf_facts(target)
    content = normalized(facts["text"])

    check(facts["pages"] >= 1, f"{label}：产物能打开，共 {facts['pages']} 页")
    if expected_pages is not None:
        check(
            facts["pages"] == expected_pages,
            f"{label}：页数是 {facts['pages']}（期望 {expected_pages}）",
        )
    check(
        marker in content,
        f"{label}：PDF 里读得到原文（找了 {marker!r}）",
    )
    check(target.suffix == ".pdf", f"{label}：下载下来的确实是 PDF（{target.name}）")
    return target


def run_part_b(page: Page) -> None:
    section("B 段：四种新格式")
    check_conversion(page, "/doc/word", SAMPLES / "季度报告.docx", ACCEPT_MARKER, "Word → PDF")
    check_conversion(
        page,
        "/doc/excel",
        SAMPLES / "季度表格.xlsx",
        ACCEPT_MARKER,
        "Excel → PDF",
        expected_pages=2,  # 两张工作表
    )

    text, target = convert(page, "/doc/ppt", SAMPLES / "产品发布.pptx")
    facts = pdf_facts(target)
    check(facts["pages"] == 3, f"PPT → PDF：三页幻灯片出三页 PDF（{facts['pages']}）")
    check(ACCEPT_MARKER in normalized(facts["text"]), "PPT → PDF：PDF 里读得到原文")
    check("3 页幻灯片" in text, "PPT → PDF：说明里如实报了幻灯片页数")

    check_conversion(page, "/doc/txt", SAMPLES / "会议记录.txt", TXT_MARKER, "TXT → PDF")
    check_conversion(page, "/doc/txt", SAMPLES / "gbk笔记.txt", TXT_MARKER, "GBK 编码的 TXT")

    section("B 段：旧格式（.doc / .xls / .ppt）")
    legacy = [
        ("/doc/word", "sample.doc", "LEGACY-DOC-标记文字", "旧版 .doc"),
        ("/doc/excel", "sample.xls", "LEGACY-XLS-标记文字", "旧版 .xls"),
        ("/doc/ppt", "sample.ppt", "LEGACY-PPT-标记文字", "旧版 .ppt"),
    ]
    for route, name, marker, label in legacy:
        source = SAMPLES / name
        if not source.is_file():
            check(False, f"{label}：缺少样张 {name}（跑 scripts/make_office_fixtures.py 生成）")
            continue
        check_conversion(page, route, source, marker, label)


# ----------------------------------------------------------------------
# C 段：TXT 的四个排版选项
# ----------------------------------------------------------------------


#: 各页面规格的期望尺寸（pt），来自 settings.PDF_PAGE_SIZES。
#: 写死在这里是有意的：如果哪天页面尺寸表被改动，这条会红，提醒人确认是不是手滑。
EXPECTED_RECTS = {
    "a4": (595.3, 841.9),
    "a5": (419.5, 595.3),
    "letter": (612.0, 792.0),
}


def close_to(actual: list[float], expected: tuple[float, float], tolerance: float = 1.5) -> bool:
    return abs(actual[0] - expected[0]) <= tolerance and abs(actual[1] - expected[1]) <= tolerance


def run_part_c(page: Page) -> None:
    section("C 段：TXT 的排版选项真的改变产物")
    source = SAMPLES / "会议记录.txt"

    def build(size: str, page_size: str, orientation: str, font: str | None = None):
        def apply(p: Page) -> None:
            p.get_by_role("spinbutton", name="字体大小").fill(size)
            pick(p, "txt-page-size", page_size)
            pick(p, "txt-orientation", orientation)
            if font is not None:
                pick(p, "txt-font", font)
            p.wait_for_timeout(200)

        text, target = convert(page, "/doc/txt", source, options=apply)
        return text, pdf_facts(target)

    _, a4 = build("11", "a4", "portrait")
    _, a5 = build("11", "a5", "portrait")
    _, letter = build("11", "letter", "portrait")
    _, landscape = build("11", "a4", "landscape")
    _, small = build("8", "a4", "portrait")
    _, large = build("24", "a4", "portrait")

    for key, facts in (("a4", a4), ("a5", a5), ("letter", letter)):
        check(
            close_to(facts["rect"], EXPECTED_RECTS[key]),
            f"页面大小 {key.upper()} 排出来是 {facts['rect']}（期望 {list(EXPECTED_RECTS[key])}）",
        )
    check(
        landscape["rect"][0] > landscape["rect"][1],
        f"横向页面的宽大于高：{landscape['rect']}",
    )
    check(
        abs(landscape["rect"][0] - a4["rect"][1]) <= 1.5,
        f"横向就是把纵向转 90 度（{a4['rect']} → {landscape['rect']}）",
    )
    check(
        large["pages"] > small["pages"],
        f"字号真的换了：8 号 {small['pages']} 页 → 24 号 {large['pages']} 页",
    )

    # 字体选项是服务器探测出来的，不是写死的假控件：换一项，PDF 里嵌入的字体名要跟着换。
    # 选项只在「选了文件、还没转换」时渲染，所以要重新进一次页面把文件选上。
    page.goto(BASE + "/doc/txt", wait_until="networkidle")
    page.wait_for_selector("input[type=file]", state="attached", timeout=30_000)
    page.set_input_files("input[type=file]", str(source))
    page.wait_for_selector('input[name="txt-font"]', state="attached", timeout=30_000)

    with urllib.request.urlopen(f"{BASE}/api/config", timeout=10) as response:
        served_fonts = json.load(response)["txt_fonts"]
    checked_fonts = [font["value"] for font in served_fonts]

    fonts = page.locator('input[name="txt-font"]')
    rendered_fonts = [fonts.nth(index).get_attribute("value") for index in range(fonts.count())]
    check(
        rendered_fonts == checked_fonts,
        f"界面上的字体项就是服务器探测到的那几项（{rendered_fonts}）—— 不是写死的假控件",
    )

    if len(rendered_fonts) >= 2:
        _, first = build("11", "a4", "portrait", font=rendered_fonts[0])
        _, second = build("11", "a4", "portrait", font=rendered_fonts[1])
        check(
            first["fonts"] != second["fonts"],
            f"换字体真的换了嵌入的字体：{rendered_fonts[0]} {first['fonts']}"
            f" → {rendered_fonts[1]} {second['fonts']}",
        )
    else:
        # 服务器上一个中文字体都没装时只给一项内置字体，这是配置里说明过的降级行为，
        # 不是缺陷 —— 如实记下来，不假装比过。
        check(
            True,
            f"服务器只探测到 {len(rendered_fonts)} 种字体，没有可比较的两项（如实记录）",
        )


# ----------------------------------------------------------------------
# D 段：最大文件大小
# ----------------------------------------------------------------------


def run_part_d(page: Page) -> None:
    section("D 段：最大文件大小")

    def target(p: Page) -> None:
        pick(p, "doc-target-size", "custom")
        p.get_by_role("spinbutton", name="自定义目标大小").fill("0.1")
        p.wait_for_timeout(200)

    text, downloaded = convert(
        page, "/doc/word", SAMPLES / "长篇报告.docx", options=target
    )
    size = downloaded.stat().st_size

    check(size > 100 * 1024, f"样张排出来确实超过目标（{size / 1024:.1f} KB > 100 KB）")
    check("仍未达到" in text, "压不到目标时如实写「仍未达到」")
    check("已达到目标大小" not in text, "没有达到却说达到")
    check(
        re.search(r"当前 [\d.]+ ?(KB|MB)", text) is not None,
        "说明里给出了当前的真实大小，用户可据此判断",
    )
    check(pdf_facts(downloaded)["pages"] >= 1, "压不到目标也照样给出一份能打开的 PDF")

    # 本来就比目标小：不该做无谓的压缩
    def loose(p: Page) -> None:
        pick(p, "doc-target-size", "10mb")
        p.wait_for_timeout(200)

    text, _ = convert(page, "/doc/word", SAMPLES / "季度报告.docx", options=loose)
    check("没有再做压缩" in text, "结果本来就没超目标时，如实说明没有压缩")


# ----------------------------------------------------------------------
# E 段：负例与不泄露
# ----------------------------------------------------------------------


def run_part_e(page: Page) -> None:
    section("E 段：负例与不泄露")

    text = rejected(page, "/doc/word", SAMPLES / "发票.docx")
    check("与扩展名不一致" in text, "改名成 .docx 的可执行文件在界面上被拒并说清原因")

    text = rejected(page, "/doc/word", SAMPLES / "坏文档.docx")
    check(
        "无法读取该 Office 文件，请检查文件是否损坏。" in text,
        "损坏的 Office 文档给出指定的那句中文提示",
    )

    # 扩展名就不属于这一页的文件：前端就会挡住，所以这里验的是
    # 「说清了该传什么」+「没有留下一个点了也没用的按钮」。
    for route, source, expected in (
        ("/doc/word", "季度表格.xlsx", "DOCX"),
        ("/doc/excel", "sample.doc", "XLSX"),
        ("/doc/txt", "季度报告.docx", "TXT"),
    ):
        text, has_button = rejected_locally(page, route, SAMPLES / source)
        check(
            "暂不支持该文件格式" in text and expected in text,
            f"{source} 传到 {route}：界面直接说明这一页只收 {expected} 一类",
        )
        check(not has_button, f"{source} 传到 {route}：不显示开始按钮 —— 不留无效按钮")

    # 服务端那一层同样要拦（前端不是唯一防线）：直接打接口，绕过界面
    status, raw = post_doc(
        "word-to-pdf", "表格.xlsx", (SAMPLES / "季度表格.xlsx").read_bytes()
    )
    check(status == 415, f"Excel 直接打到 Word 接口是 415（实际 {status}）")
    check(
        "只能转换" in raw and "Excel" in raw,
        "服务端说清「你传错了地方」，而不是笼统的「格式不支持」",
    )
    check_no_leak(raw, "传错页面")

    status, raw = post_doc(
        "excel-to-pdf", "老表格.xls", (SAMPLES / "sample.doc").read_bytes()
    )
    check(
        status == 415 and "与扩展名不一致" in raw,
        f"旧格式之间改名（.doc 冒充 .xls）被服务端拦下（实际 {status}）",
    )
    check_no_leak(raw, "旧格式改名")

    # 原始响应体：浏览器渲染后的文字看不出响应里到底带了什么
    broken = (SAMPLES / "坏文档.docx").read_bytes()
    status, raw = post_doc("word-to-pdf", "坏文档.docx", broken)
    check(status == 400, f"损坏文档直接打接口也是 400（实际 {status}）")
    check("无法读取该 Office 文件" in raw, "接口原始响应里是那句中文提示")
    check_no_leak(raw, "损坏文档")

    status, raw = post_doc(
        "word-to-pdf", "发票.docx", b"MZ\x90\x00" + b"\x00" * 2048
    )
    check(status == 415, f"改名可执行文件直接打接口是 415（实际 {status}）")
    check_no_leak(raw, "改名可执行文件")

    status, raw = post_doc("word-to-pdf", "报告.docx", b"")
    check(status in (400, 415), f"空文件被拒（实际 {status}）")
    check_no_leak(raw, "空文件")

    status, raw = post_doc(
        "word-to-pdf", "报告.docx", (SAMPLES / "季度报告.docx").read_bytes(),
        {"target": "1gb"},
    )
    check(status == 400, f"非法目标档位被拒（实际 {status}）")
    check("目标大小参数无效" in raw, "非法档位给出明确的中文说明")
    check_no_leak(raw, "非法目标档位")


# ----------------------------------------------------------------------
# F 段：缺组件
# ----------------------------------------------------------------------


def start_nocomp_backend() -> subprocess.Popen:
    """另起一个后端，把 LibreOffice 路径指到一个不存在的地方。

    这是**唯一**能真实复现「服务器没装转换组件」的办法：
    在同一个进程里 monkeypatch 只是测试内部的假动作，
    而这里前端拿到的是真的 `doc_conversion_available === false`。
    """
    SERVER_TEMP.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update(
        {
            "FILETOOLS_LIBREOFFICE_PATH": str(WORK / "no-such-libreoffice" / "soffice.com"),
            "FILETOOLS_TEMP_ROOT": str(SERVER_TEMP),
        }
    )
    log = open(NOCOMP_LOG, "wb")
    process = subprocess.Popen(
        [
            str(BACKEND_PYTHON),
            "-m",
            "uvicorn",
            "main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(NOCOMP_PORT),
        ],
        cwd=str(BACKEND),
        env=env,
        stdout=log,
        stderr=log,
    )
    for _ in range(80):
        try:
            with urllib.request.urlopen(f"{NOCOMP_BASE}/api/health", timeout=1) as response:
                if response.status == 200:
                    return process
        except (urllib.error.URLError, OSError):
            time.sleep(0.5)
    process.terminate()
    raise SystemExit(f"缺组件后端没能启动，看日志：{NOCOMP_LOG}")


def run_part_f(page: Page) -> None:
    section("F 段：缺组件（另起后端）")
    process = start_nocomp_backend()
    try:
        with urllib.request.urlopen(f"{NOCOMP_BASE}/api/config", timeout=10) as response:
            config = json.load(response)
        check(
            config.get("doc_conversion_available") is False,
            "缺组件的后端如实上报 doc_conversion_available = false",
        )

        page.goto(NOCOMP_BASE + "/doc/word", wait_until="networkidle")
        page.wait_for_selector("input[type=file]", state="attached", timeout=30_000)
        page.set_input_files("input[type=file]", str(SAMPLES / "季度报告.docx"))
        page.wait_for_selector("text=开始转换", timeout=30_000)
        page.wait_for_timeout(1_000)
        text = body(page)

        check(
            "当前服务器缺少 Office 转换组件，请联系管理员。" in text,
            "页面上直接给出「当前服务器缺少 Office 转换组件，请联系管理员。」",
        )
        check(
            not page.get_by_role("button", name="开始转换").is_enabled(),
            "开始按钮是禁用的 —— 不留无效按钮",
        )

        # TXT 不需要转换组件，在同一个后端上必须照常可用
        page.goto(NOCOMP_BASE + "/doc/txt", wait_until="networkidle")
        page.wait_for_selector("input[type=file]", state="attached", timeout=30_000)
        page.set_input_files("input[type=file]", str(SAMPLES / "会议记录.txt"))
        page.wait_for_selector("text=开始转换", timeout=30_000)
        page.wait_for_timeout(800)
        check(
            page.get_by_role("button", name="开始转换").is_enabled(),
            "TXT 页在缺组件的服务器上照样能开始（TXT 不走 Office 组件）",
        )
        text, target = convert(
            page, "/doc/txt", SAMPLES / "会议记录.txt", base=NOCOMP_BASE
        )
        check(
            TXT_MARKER in normalized(pdf_facts(target)["text"]),
            "缺组件时 TXT 依然转得出真正的 PDF",
        )
    finally:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()


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
            for route in ("/", "/doc", *DOC_ROUTES):
                page.goto(BASE + route, wait_until="networkidle")
                overflow = page.evaluate(
                    "() => document.documentElement.scrollWidth"
                    " - document.documentElement.clientWidth"
                )
                if overflow > worst:
                    worst, worst_route = overflow, route
            check(
                worst <= 1,
                f"{width}px：文档转换的 {2 + len(DOC_ROUTES)} 个页面都没有横向溢出"
                f"（最大 {worst}px{' @' + worst_route if worst_route else ''}）",
            )

            # 手机上走一遍 TXT：上传区、选项、按钮都要能点
            box = page.locator("[role=button]").first.bounding_box()
            check(
                box is not None and box["height"] >= 88 and box["width"] >= 300,
                f"{width}px：上传区足够大（{round(box['width'])}×{round(box['height'])}）",
            )
            page.goto(BASE + "/doc/txt", wait_until="networkidle")
            page.wait_for_selector("input[type=file]", state="attached", timeout=30_000)
            page.set_input_files("input[type=file]", str(SAMPLES / "会议记录.txt"))
            page.wait_for_selector("text=开始转换", timeout=30_000)
            button = page.get_by_role("button", name="开始转换")
            button_box = button.bounding_box()
            check(
                button_box is not None and button_box["height"] >= 44,
                f"{width}px：开始按钮高 {round(button_box['height']) if button_box else 0}px，适合手指点击",
            )
            ctx.close()
    finally:
        browser.close()


# ----------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------


def build_samples() -> None:
    if (SAMPLES / "长篇报告.docx").exists() and (SAMPLES / "会议记录.txt").exists():
        print(f"复用已有样张：{SAMPLES}")
        return
    print("正在生成验收样张…")
    names = _run_backend_python(
        _SAMPLE_CODE, ACCEPT_MARKER, TXT_MARKER, SAMPLES, BACKEND / "tests" / "fixtures"
    )
    print(f"  已生成：{json.loads(names.strip().splitlines()[-1])}")


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
