"""第三阶段的真机验收脚本（Playwright + 真实浏览器）。

覆盖六个 PDF 工具：图片转 PDF、PDF 转图片、PDF 合并、PDF 拆分、
PDF 页面删除 / 提取、PDF 压缩，以及统一状态文案（§12）、文件安全限制（§13）、
错误提示（损坏 / 加密 / 非 PDF / 超页数）和第一、二阶段未被破坏。

用法：
    1. 先启动后端（8000）和前端（5173）
    2. 安装依赖：pip install playwright && playwright install chromium
    3. python scripts/verify_phase3.py

可用环境变量覆盖默认地址：
    FILETOOLS_WEB_BASE   默认 http://localhost:5173
    FILETOOLS_BACKEND_PY 后端 venv 的 python，用于生成测试文件、校验下载结果
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import tempfile

from playwright.sync_api import Page, expect, sync_playwright

ROOT = pathlib.Path(__file__).resolve().parent.parent
BASE = os.environ.get("FILETOOLS_WEB_BASE", "http://localhost:5173")

# Windows 控制台默认是 GBK，直接打印「✓」会抛 UnicodeEncodeError 把整轮跑挂掉
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

WORK = pathlib.Path(tempfile.mkdtemp(prefix="filetools-verify-pdf-"))
IMAGES = WORK / "images"
PDFS = WORK / "pdfs"
DOWNLOADS = WORK / "downloads"
REPORT = WORK / "report.txt"

# ----------------------------------------------------------------------
# 测试素材：图片用 Pillow 生成，PDF 用 PyMuPDF 生成
#
# 每一页都写上了「DOC6-3」这样的文字，这样下载回来的 PDF 可以靠抽取文字
# 验证页序 —— 合并和拆分最容易错的就是顺序，只看页数看不出来。
# ----------------------------------------------------------------------

_FIXTURE_CODE = r'''
import io, os, sys

from PIL import Image, ImageDraw, ImageFilter
import pymupdf

out = sys.argv[1]
imgs = os.path.join(out, "images")
pdfs = os.path.join(out, "pdfs")
os.makedirs(imgs, exist_ok=True)
os.makedirs(pdfs, exist_ok=True)


def photo(w, h):
    """有渐变、图形和模糊的「类照片」图片：纯色图压缩率过高，测不出压缩效果。"""
    img = Image.linear_gradient("L").resize((w, h)).convert("RGB")
    draw = ImageDraw.Draw(img)
    for i in range(8):
        draw.ellipse(
            [i * w // 10, h // 5, i * w // 10 + w // 6, h // 5 + h // 6],
            fill=(200, 80, 40),
        )
    return img.filter(ImageFilter.GaussianBlur(1.2))


def save(img, name, **kwargs):
    img.save(os.path.join(imgs, name), **kwargs)


save(photo(1600, 1200), "wide.jpg", quality=88)     # 横向
save(photo(1200, 1600), "tall.jpg", quality=88)     # 纵向
save(photo(1000, 1000), "square.png")               # 正方形、PNG

logo = Image.new("RGBA", (800, 600), (0, 0, 0, 0))
ImageDraw.Draw(logo).ellipse([40, 40, 760, 560], fill=(30, 120, 220, 255))
save(logo, "logo.png")

photo(120, 120).convert("P").save(os.path.join(imgs, "anim.gif"), format="GIF")
save(photo(300, 200), "bitmap.bmp")                 # 第九阶段新增：BMP
save(photo(300, 200), "scan.tiff", format="TIFF")   # 第九阶段新增：TIFF
with open(os.path.join(imgs, "notes.txt"), "w") as fh:
    fh.write("hello")

# 第九阶段之后 GIF 已经是**支持**的图片格式，拿它当「不支持的格式」的负例
# 只是在测「断言过时了没有」。负例必须挑真的不支持的东西 —— §四十 点名的
# MP3 是这批里最容易伪造文件头的一个（ID3v2 头之后全是零字节）。
with open(os.path.join(imgs, "audio.mp3"), "wb") as fh:
    fh.write(b"ID3\x03\x00\x00\x00" + b"\x00" * 64)


def make_pdf(name, pages, label):
    doc = pymupdf.open()
    for index in range(pages):
        page = doc.new_page(width=595.0, height=842.0)
        page.insert_text((72, 120), "%s-%d" % (label, index + 1), fontsize=28)
    doc.save(os.path.join(pdfs, name))
    doc.close()


make_pdf("doc3.pdf", 3, "DOC3")
make_pdf("doc6.pdf", 6, "DOC6")
make_pdf("doc12.pdf", 12, "DOC12")
make_pdf("doc120.pdf", 120, "DOC120")
make_pdf("huge.pdf", 520, "HUGE")          # 超过 500 页上限

# 图片型 PDF：三个页面各塞一张大 JPEG，压缩才有东西可压
buf = io.BytesIO()
photo(2000, 1500).save(buf, format="JPEG", quality=95)
jpeg = buf.getvalue()
doc = pymupdf.open()
for _ in range(3):
    page = doc.new_page(width=2000, height=1500)
    page.insert_image(pymupdf.Rect(0, 0, 2000, 1500), stream=jpeg)
doc.save(os.path.join(pdfs, "image_pdf.pdf"))
doc.close()

# 头部合法但数据被截断：属于「文件损坏」（截断到 30% 时 MuPDF 打不开）
src = os.path.join(pdfs, "doc3.pdf")
raw = open(src, "rb").read()
with open(os.path.join(pdfs, "corrupt.pdf"), "wb") as fh:
    fh.write(raw[: int(len(raw) * 0.3)])

# 加密 PDF：能做但不能处理，必须给出明确原因
doc = pymupdf.open()
doc.new_page().insert_text((72, 72), "secret")
doc.save(
    os.path.join(pdfs, "encrypted.pdf"),
    encryption=pymupdf.PDF_ENCRYPT_AES_256,
    owner_pw="owner",
    user_pw="user",
)
doc.close()

# 扩展名是 .pdf，内容完全不是 PDF
with open(os.path.join(pdfs, "fake.pdf"), "wb") as fh:
    fh.write(b"this is definitely not a pdf" * 40)
'''

# 检查 PDF：页数、每页尺寸、每页文字
_INSPECT_CODE = r'''
import json, sys
import pymupdf

doc = pymupdf.open(sys.argv[1])
print(json.dumps({
    "pages": doc.page_count,
    "sizes": [[round(page.rect.width, 1), round(page.rect.height, 1)] for page in doc],
    "texts": [page.get_text().strip() for page in doc],
}, ensure_ascii=False))
doc.close()
'''

# 检查 ZIP：成员名、体积，以及每个成员自己的页数 / 文字 / 图片格式
_ZIP_CODE = r'''
import io, json, sys, zipfile

from PIL import Image
import pymupdf

archive = zipfile.ZipFile(sys.argv[1])
entries = []
for name in archive.namelist():
    data = archive.read(name)
    item = {"name": name, "bytes": len(data)}
    if name.lower().endswith(".pdf"):
        doc = pymupdf.open(stream=data, filetype="pdf")
        item["pages"] = doc.page_count
        item["texts"] = [page.get_text().strip() for page in doc]
        doc.close()
    else:
        image = Image.open(io.BytesIO(data))
        item["image_format"] = image.format
        item["size"] = list(image.size)
    entries.append(item)
print(json.dumps(entries, ensure_ascii=False))
'''


def find_backend_python() -> pathlib.Path:
    """后端 venv 的 python —— 用它生成素材、校验下载结果。"""
    override = os.environ.get("FILETOOLS_BACKEND_PY")
    if override:
        return pathlib.Path(override)
    for candidate in (
        ROOT / "backend" / ".venv" / "Scripts" / "python.exe",
        ROOT / "backend" / ".venv" / "bin" / "python",
    ):
        if candidate.exists():
            return candidate
    return pathlib.Path(sys.executable)


BACKEND_PYTHON = find_backend_python()


def _run_backend_python(code: str, *args: object) -> str:
    """用后端的解释器跑一段代码，返回它的 stdout。

    ``errors="replace"`` 与 ``PYTHONIOENCODING=utf-8`` 都是必须的：子进程的 stdout
    是管道，这时 Python 按 Windows 本地代码页（GBK）写字节，父进程按 utf-8 读，
    只要子进程打印一个汉字就 UnicodeDecodeError —— 而且是在 subprocess 自己的
    **读取线程**里炸。线程一死，``communicate()`` 拿回的就是 ``stdout=None``，
    于是真正的报错（子进程自己为什么失败、甚至只是打印了一句中文）会被一起吃掉，
    在这一层看起来是「json.loads 收到了 None」这种莫名其妙的 TypeError。
    见 verify_phase5.py 里同名函数上的同一段说明。
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
        _run_backend_python(_FIXTURE_CODE, WORK)
    except RuntimeError as exc:
        raise SystemExit(f"生成测试素材失败：\n{exc}") from exc


def _run_checker(code: str, target: pathlib.Path):
    return json.loads(_run_backend_python(code, target))


def inspect_pdf(path: pathlib.Path) -> dict:
    """用后端的 PyMuPDF 打开下载到的 PDF，返回页数 / 尺寸 / 文字。"""
    return _run_checker(_INSPECT_CODE, path)


def inspect_zip(path: pathlib.Path) -> list[dict]:
    """打开下载到的 ZIP，逐个成员取出页数或图片格式。"""
    return _run_checker(_ZIP_CODE, path)


results: list[tuple[bool, str]] = []
console_errors: list[str] = []


def check(ok: bool, label: str) -> None:
    results.append((ok, label))
    print(f"{'PASS' if ok else 'FAIL'}  {label}")


def attach(page: Page) -> None:
    page.on(
        "console",
        lambda msg: console_errors.append(f"[console.{msg.type}] {msg.text}")
        if msg.type == "error"
        else None,
    )
    page.on("pageerror", lambda err: console_errors.append(f"[pageerror] {err}"))


def upload(page: Page, *paths: str) -> None:
    """把文件塞进隐藏的 file input（页面上第一个，也就是主上传区）。"""
    page.locator("input[type=file]").first.set_input_files(
        [str((IMAGES if p.endswith((".jpg", ".png", ".gif", ".txt")) else PDFS) / p) for p in paths]
    )
    page.wait_for_timeout(500)


def pick(page: Page, field: str, value: str) -> None:
    """选中一个单选项：单选框是 sr-only 的，真实用户点的是外层 label。"""
    label = page.locator(f"label:has(input[name='{field}'][value='{value}'])")
    label.evaluate("el => el.scrollIntoView({block: 'center'})")
    page.wait_for_timeout(120)
    label.click()
    page.wait_for_timeout(250)


def body(page: Page) -> str:
    return page.inner_text("body")


def wait_result(page: Page, timeout: int = 180_000) -> None:
    """等结果面板（「✓ …」）或错误提示出现 —— 出错时立刻返回，不干等三分钟。"""
    page.wait_for_function(
        "() => document.body.innerText.includes('✓ ') "
        "|| !!document.querySelector('[role=alert]')",
        timeout=timeout,
    )
    page.wait_for_timeout(300)


_download_seq = 0


def download(page: Page, button: str = "下载文件") -> tuple[str, pathlib.Path]:
    """点下载并把文件存下来，返回 (浏览器给的文件名, 本地路径)。"""
    global _download_seq
    with page.expect_download() as info:
        page.get_by_role("button", name=button).click()
    item = info.value
    _download_seq += 1
    DOWNLOADS.mkdir(parents=True, exist_ok=True)
    target = DOWNLOADS / f"{_download_seq:02d}-{item.suggested_filename}"
    item.save_as(target)
    return item.suggested_filename, target


# ----------------------------------------------------------------------
# 一、首页与导航（§2、§15）
# ----------------------------------------------------------------------


def check_home(page: Page) -> None:
    page.goto(BASE, wait_until="networkidle")
    for route, name in (
        ("/pdf/from-images", "图片转 PDF"),
        ("/pdf/to-images", "PDF 转图片"),
        ("/pdf/compress", "PDF 压缩"),
        ("/pdf/merge", "PDF 合并"),
        ("/pdf/split", "PDF 拆分"),
        ("/pdf/edit-pages", "PDF 页面删除 / 提取"),
    ):
        check(
            page.locator(f"a[href='{route}']").count() > 0,
            f"首页有「{name}」入口",
        )

    page.goto(f"{BASE}/pdf", wait_until="networkidle")
    text = body(page)
    check(
        all(name in text for name in ("图片转 PDF", "PDF 转图片", "PDF 压缩", "PDF 合并", "PDF 拆分")),
        "PDF 工具页列出全部六个功能",
    )
    check("正在开发中" not in text, "PDF 工具页没有「正在开发中」的占位提示")

    # 图片工具仍在（§2 不能破坏前两阶段）
    page.goto(f"{BASE}/image", wait_until="networkidle")
    check("图片压缩" in body(page), "图片工具页仍然可用")


# ----------------------------------------------------------------------
# 二、图片 → PDF（§3）
# ----------------------------------------------------------------------


def check_from_images(page: Page) -> None:
    page.goto(f"{BASE}/pdf/from-images", wait_until="networkidle")
    upload(page, "wide.jpg", "tall.jpg", "square.png")

    text = body(page)
    check("已选 3 个文件" in text, "多张图片都进了列表")
    check("1600 × 1200" in text and "1000 × 1000" in text, "列表里能看到每张图的像素尺寸")
    check("等待处理" in text, "选好文件后显示「等待处理」（§12）")

    page.get_by_role("button", name="生成 PDF").click()
    wait_result(page)
    text = body(page)
    check("✓ 处理完成" in text, "图片转 PDF 成功")
    check("3 张" in text and "3 页" in text, "结果页显示图片数与 PDF 页数")

    name, path = download(page)
    check(name.endswith(".pdf"), f"下载图片转 PDF 的结果（{name}）")
    info = inspect_pdf(path)
    check(info["pages"] == 3, "生成的 PDF 是 3 页")
    check(
        info["sizes"] == [[1600.0, 1200.0], [1200.0, 1600.0], [1000.0, 1000.0]],
        f"自动页面尺寸下每页尺寸跟随图片（实际 {info['sizes']}）",
    )

    # 排序：把 tall.jpg 上移一位，第一页应当变成纵向的 1200×1600
    page.get_by_role("button", name="调整设置").click()
    page.wait_for_timeout(300)
    page.get_by_role("button", name="把 tall.jpg 上移").click()
    page.wait_for_timeout(300)
    page.get_by_role("button", name="生成 PDF").click()
    wait_result(page)
    _, path = download(page)
    info = inspect_pdf(path)
    check(
        info["sizes"][0] == [1200.0, 1600.0],
        f"上移后顺序真的变了（第 1 页 {info['sizes'][0]}）",
    )

    # A4 + 跟随图片方向：横向图片应当得到横向的 A4
    page.goto(f"{BASE}/pdf/from-images", wait_until="networkidle")
    upload(page, "wide.jpg")
    pick(page, "page-size", "a4")
    pick(page, "margin", "medium")
    page.get_by_role("button", name="生成 PDF").click()
    wait_result(page)
    _, path = download(page)
    info = inspect_pdf(path)
    width, height = info["sizes"][0]
    check(
        abs(width - 841.9) < 1.5 and abs(height - 595.3) < 1.5,
        f"A4 + 横向图片得到横向 A4（实际 {width} × {height}）",
    )

    # 页边距「中」= 10 毫米 = 28.35 磅（第九阶段 §十 把档位改成了整毫米）：
    # 自动尺寸下每边多出 28.35 磅
    page.goto(f"{BASE}/pdf/from-images", wait_until="networkidle")
    upload(page, "square.png")
    pick(page, "margin", "medium")
    page.get_by_role("button", name="生成 PDF").click()
    wait_result(page)
    _, path = download(page)
    info = inspect_pdf(path)
    margin_pt = 10 * 72 / 25.4
    expected = round(1000 + margin_pt * 2, 1)
    check(
        all(abs(value - expected) < 0.3 for value in info["sizes"][0]),
        f"页边距「中」在图片四周各留 10 毫米（{margin_pt:.2f} 磅，"
        f"期望 {expected}，实际 {info['sizes'][0]}）",
    )

    # PNG 与透明通道
    page.goto(f"{BASE}/pdf/from-images", wait_until="networkidle")
    upload(page, "logo.png")
    page.get_by_role("button", name="生成 PDF").click()
    wait_result(page)
    check("✓ 处理完成" in body(page), "带透明通道的 PNG 也能转 PDF")

    # 自定义尺寸的本地校验
    page.goto(f"{BASE}/pdf/from-images", wait_until="networkidle")
    upload(page, "wide.jpg")
    pick(page, "page-size", "custom")
    page.locator("input[type=number]").first.fill("5")
    page.wait_for_timeout(300)
    text = body(page)
    check(
        "自定义页面宽度需要在 20–2000 毫米之间" in text,
        "自定义尺寸超出范围时给出明确提示",
    )
    check(
        page.get_by_role("button", name="生成 PDF").is_disabled(),
        "自定义尺寸非法时禁用生成按钮",
    )

    # 非图片文件
    page.goto(f"{BASE}/pdf/from-images", wait_until="networkidle")
    page.locator("input[type=file]").first.set_input_files(str(IMAGES / "audio.mp3"))
    page.wait_for_timeout(500)
    check(
        "暂不支持该文件格式" in body(page),
        "图片转 PDF 拒绝不支持的图片格式",
    )

    # 第九阶段新增的三种图片格式在**这个页面**也要真的能用 —— 只更新负例、
    # 不验证正例的话，等于把「新格式到底通没通」这件事留给了别的脚本。
    for name in ("anim.gif", "bitmap.bmp", "scan.tiff"):
        page.goto(f"{BASE}/pdf/from-images", wait_until="networkidle")
        page.locator("input[type=file]").first.set_input_files(str(IMAGES / name))
        page.wait_for_timeout(300)
        check(
            "暂不支持该文件格式" not in body(page),
            f"图片转 PDF 现在接受 {name}",
        )


# ----------------------------------------------------------------------
# 三、PDF → 图片（§4）
# ----------------------------------------------------------------------


def check_to_images(page: Page) -> None:
    page.goto(f"{BASE}/pdf/to-images", wait_until="networkidle")
    upload(page, "doc3.pdf")

    text = body(page)
    check("doc3.pdf" in text and "共 3 页" in text, "上传后显示文件名与真实页数")
    check("等待处理" in text, "上传完成后进入「等待处理」（§12）")

    # PNG 不显示质量选项，JPG / WEBP 显示
    pick(page, "target-format", "png")
    text = body(page)
    check("PNG 是无损格式" in text, "选择 PNG 时说明不做有损压缩")
    check(
        page.get_by_role("radio", name="高质量").count() == 0,
        "选择 PNG 时不显示 JPG 的质量选项（§4）",
    )
    pick(page, "target-format", "jpg")
    check(
        page.get_by_role("radio", name="高质量").count() > 0,
        "选择 JPG 时显示质量选项",
    )

    # 页面范围与 ZIP 提示
    page.get_by_label("页面范围").fill("1-3")
    page.wait_for_timeout(300)
    check("将导出 3 页，自动打包成 pdf_pages.zip" in body(page), "多页导出提示会打包 ZIP")

    page.get_by_role("button", name="开始导出").click()
    wait_result(page)
    text = body(page)
    check("✓ 处理完成" in text, "PDF 转图片成功")
    check("3 张" in text, "结果页显示导出张数")

    name, path = download(page, "下载 ZIP")
    check(name == "pdf_pages.zip", f"多页结果打包成 pdf_pages.zip（实际 {name}）")
    entries = inspect_zip(path)
    check(
        [item["name"] for item in entries] == ["page-01.jpg", "page-02.jpg", "page-03.jpg"],
        f"ZIP 里是 page-NN.jpg（实际 {[item['name'] for item in entries]}）",
    )
    check(
        all(item.get("image_format") == "JPEG" for item in entries),
        "导出的图片真的是 JPEG",
    )
    check(
        all(item["size"][0] > 1000 for item in entries),
        f"高清档导出的是大图（第 1 张 {entries[0]['size']}）",
    )

    # 单页：直接给图片文件，不打包
    page.get_by_role("button", name="换个设置再导出").click()
    page.wait_for_timeout(300)
    page.get_by_label("页面范围").fill("2")
    page.wait_for_timeout(300)
    check("将导出 1 页" in body(page), "只导出一页时不打包")
    page.get_by_role("button", name="开始导出").click()
    wait_result(page)
    name, path = download(page)
    check(name == "page-02.jpg", f"单页导出直接给 page-02.jpg（实际 {name}）")
    check(path.stat().st_size > 10_000, "单页导出的图片体积合理")

    # PNG 导出
    page.get_by_role("button", name="换个设置再导出").click()
    page.wait_for_timeout(300)
    pick(page, "target-format", "png")
    page.get_by_label("页面范围").fill("1")
    page.wait_for_timeout(300)
    page.get_by_role("button", name="开始导出").click()
    wait_result(page)
    name, path = download(page)
    check(name == "page-01.png", f"导出 PNG（实际 {name}）")

    # 页码越界
    page.get_by_role("button", name="换个设置再导出").click()
    page.wait_for_timeout(300)
    page.get_by_label("页面范围").fill("9")
    page.wait_for_timeout(300)
    check(
        "第 9 页不存在，该 PDF 共 3 页" in body(page),
        "页码越界时提示共有几页",
    )
    check(
        page.get_by_role("button", name="开始导出").is_disabled(),
        "页码越界时禁用导出按钮",
    )

    # 一次导出上限（§13 资源限制）
    page.goto(f"{BASE}/pdf/to-images", wait_until="networkidle")
    upload(page, "doc120.pdf")
    page.get_by_label("页面范围").fill("all")
    page.wait_for_timeout(400)
    text = body(page)
    check("一次最多导出 100 页" in text, "超过单次导出上限时给出明确提示")
    check(
        page.get_by_role("button", name="开始导出").is_disabled(),
        "超过导出上限时禁用按钮",
    )


# ----------------------------------------------------------------------
# 四、PDF 合并（§6）
# ----------------------------------------------------------------------


def check_merge(page: Page) -> None:
    page.goto(f"{BASE}/pdf/merge", wait_until="networkidle")
    upload(page, "doc3.pdf", "doc6.pdf")

    text = body(page)
    check("已选 2 个文件" in text, "两个 PDF 都进了列表")
    check(
        page.get_by_role("button", name="把 doc6.pdf 上移").count() > 0
        and page.get_by_role("button", name="把 doc6.pdf 下移").count() > 0
        and page.get_by_role("button", name="移除 doc6.pdf").count() > 0,
        "每个文件都有上移 / 下移 / 删除按钮（§6）",
    )

    page.get_by_role("button", name="合并 PDF").click()
    wait_result(page)
    text = body(page)
    check("✓ 处理完成" in text, "PDF 合并成功")
    check("9 页" in text, "结果页显示合并后的总页数（3 + 6）")

    name, path = download(page)
    check(name == "merged.pdf", f"输出文件名是 merged.pdf（实际 {name}）")
    info = inspect_pdf(path)
    check(info["pages"] == 9, "合并后的 PDF 是 9 页")
    check(
        info["texts"] == [f"DOC3-{i}" for i in (1, 2, 3)] + [f"DOC6-{i}" for i in range(1, 7)],
        f"合并顺序与列表一致（实际 {info['texts']}）",
    )

    # 调整顺序后重新合并
    page.get_by_role("button", name="调整顺序").click()
    page.wait_for_timeout(300)
    page.get_by_role("button", name="把 doc6.pdf 上移").click()
    page.get_by_role("button", name="合并 PDF").click()
    wait_result(page)
    _, path = download(page)
    info = inspect_pdf(path)
    check(
        info["texts"][0] == "DOC6-1",
        f"上移后合并顺序跟着变（第 1 页 {info['texts'][0]}）",
    )

    # 删除一个文件
    page.get_by_role("button", name="调整顺序").click()
    page.wait_for_timeout(300)
    page.get_by_role("button", name="移除 doc3.pdf").click()
    page.wait_for_timeout(300)
    text = body(page)
    check("已选 1 个文件" in text, "删除按钮真的移除了文件")
    check("只选了一个文件" in text, "只选一个文件时给出提示")


# ----------------------------------------------------------------------
# 五、PDF 拆分（§7）
# ----------------------------------------------------------------------


def check_split(page: Page) -> None:
    # 每页一个 PDF → 自动打包
    page.goto(f"{BASE}/pdf/split", wait_until="networkidle")
    upload(page, "doc6.pdf")
    text = body(page)
    check("part-01.pdf" in text and "doc6_parts.zip" in text, "提示拆出的文件名与 ZIP 名")

    page.get_by_role("button", name="开始拆分").click()
    wait_result(page)
    text = body(page)
    check("✓ 处理完成" in text, "拆分成功")
    check("6 个" in text, "每页一个时拆出 6 份")

    name, path = download(page, "下载 ZIP")
    check(name == "doc6_parts.zip", f"ZIP 名与服务端一致（实际 {name}）")
    entries = inspect_zip(path)
    check(
        [item["name"] for item in entries]
        == [f"part-{i:02d}.pdf" for i in range(1, 7)],
        f"ZIP 里是 part-01 … part-06（实际 {[item['name'] for item in entries]}）",
    )
    check(all(item["pages"] == 1 for item in entries), "每个 part 都是 1 页")
    check(
        [item["texts"][0] for item in entries] == [f"DOC6-{i}" for i in range(1, 7)],
        "拆出来的页序正确",
    )

    # 按范围拆分：1-2 / 3-4 / 5-6
    page.goto(f"{BASE}/pdf/split", wait_until="networkidle")
    upload(page, "doc6.pdf")
    pick(page, "split-mode", "ranges")
    page.get_by_label("第 1 份的页面范围").fill("1-2")
    page.get_by_role("button", name="再加一份").click()
    page.get_by_label("第 2 份的页面范围").fill("3-4")
    page.get_by_role("button", name="再加一份").click()
    page.get_by_label("第 3 份的页面范围").fill("5-6")
    page.wait_for_timeout(400)
    check("含第 1-2 页，共 2 页" in body(page), "每个范围都能看出包含哪些页")

    page.get_by_role("button", name="开始拆分").click()
    wait_result(page)
    _, path = download(page, "下载 ZIP")
    entries = inspect_zip(path)
    check(
        [item["name"] for item in entries] == ["part-01.pdf", "part-02.pdf", "part-03.pdf"],
        f"按范围拆出 3 份（实际 {[item['name'] for item in entries]}）",
    )
    check(
        [item["pages"] for item in entries] == [2, 2, 2],
        f"每份的页数与填写的范围一致（实际 {[item['pages'] for item in entries]}）",
    )
    check(
        entries[1]["texts"] == ["DOC6-3", "DOC6-4"],
        f"第二份是第 3、4 页（实际 {entries[1]['texts']}）",
    )

    # 范围里写了不存在的页
    page.goto(f"{BASE}/pdf/split", wait_until="networkidle")
    upload(page, "doc6.pdf")
    pick(page, "split-mode", "ranges")
    page.get_by_label("第 1 份的页面范围").fill("9")
    page.wait_for_timeout(400)
    check("第 9 页不存在，该 PDF 共 6 页" in body(page), "范围越界时给出明确提示")

    # 自定义页面：1,3,5
    page.goto(f"{BASE}/pdf/split", wait_until="networkidle")
    upload(page, "doc6.pdf")
    pick(page, "split-mode", "selected")
    page.get_by_label("要保留的页面").fill("1,3,5")
    page.wait_for_timeout(400)
    text = body(page)
    check("selected-pages.pdf" in text, "自定义页面拆分的输出名是 selected-pages.pdf")

    page.get_by_role("button", name="开始拆分").click()
    wait_result(page)
    name, path = download(page)
    check(name == "selected-pages.pdf", f"输出文件名与提示一致（实际 {name}）")
    info = inspect_pdf(path)
    check(
        info["texts"] == ["DOC6-1", "DOC6-3", "DOC6-5"],
        f"只保留选中的页且顺序不变（实际 {info['texts']}）",
    )


# ----------------------------------------------------------------------
# 六、PDF 页面删除 / 提取（§8、§9）
# ----------------------------------------------------------------------


def check_edit_pages(page: Page) -> None:
    page.goto(f"{BASE}/pdf/edit-pages", wait_until="networkidle")
    upload(page, "doc6.pdf")

    text = body(page)
    check("Page 01" in text and "Page 06" in text, "缩略图卡片显示页码 Page 01 这样的编号（§10）")
    thumbs = page.locator("img[alt$='页预览']")
    check(thumbs.count() == 6, f"六页都有缩略图（实际 {thumbs.count()} 张）")
    check(
        thumbs.first.get_attribute("src").endswith("/thumb/0"),
        "缩略图确实来自后端渲染接口",
    )
    check("请先选择要删除的页面。" in text, "没选页面时说明要先选（删除模式）")
    check(
        page.get_by_role("button", name="生成新的 PDF").is_disabled(),
        "没选页面时禁用「生成新的 PDF」",
    )

    # 点缩略图选中，删除态明显
    page.get_by_role("button", name="第 3 页", exact=True).click()
    page.wait_for_timeout(300)
    text = body(page)
    check("将删除" in text, "选中的页面有明显的删除态标记")
    check(
        page.get_by_role("button", name="第 3 页（已选中）").get_attribute("aria-pressed") == "true",
        "缩略图的选中状态对读屏软件可见",
    )
    check(page.get_by_label("页码").input_value() == "3", "点缩略图会同步到页码输入框")

    # 全选 → 不能删光
    page.get_by_role("button", name="全选").click()
    page.wait_for_timeout(300)
    check("不能删除全部页面，请至少保留一页。" in body(page), "不允许删掉所有页面")
    check(
        page.get_by_role("button", name="生成新的 PDF").is_disabled(),
        "会删光页面时禁用按钮",
    )

    # 清空后填页码删除 2、4
    page.get_by_role("button", name="清空选择").click()
    page.wait_for_timeout(200)
    page.get_by_label("页码").fill("2,4")
    page.wait_for_timeout(300)
    check("doc6_edited.pdf" in body(page), "提示输出文件名是 <原名>_edited.pdf")
    check(
        page.get_by_role("button", name="第 2 页（已选中）").count() > 0
        and page.get_by_role("button", name="第 4 页（已选中）").count() > 0,
        "页码输入框与缩略图是同一份选择",
    )

    page.get_by_role("button", name="生成新的 PDF").click()
    wait_result(page)
    text = body(page)
    check("✓ 处理完成" in text, "删除页面成功")
    check("4 页" in text and "已删除" in text and "2 页" in text, "结果页显示新页数与删掉的页数")

    name, path = download(page)
    check(name == "doc6_edited.pdf", f"输出文件名正确（实际 {name}）")
    info = inspect_pdf(path)
    check(
        info["texts"] == ["DOC6-1", "DOC6-3", "DOC6-5", "DOC6-6"],
        f"删掉的是第 2、4 页（实际 {info['texts']}）",
    )

    # 提取页面：1,3,5
    page.goto(f"{BASE}/pdf/edit-pages", wait_until="networkidle")
    upload(page, "doc6.pdf")
    pick(page, "edit-mode", "extract")
    page.wait_for_timeout(300)
    check("请先选择要提取的页面。" in body(page), "没选页面时说明要先选（提取模式）")

    page.get_by_label("页码").fill("1,3,5")
    page.wait_for_timeout(300)
    check("selected_pages.pdf" in body(page), "提取的输出文件名是 selected_pages.pdf（§9）")
    check("将保留 3 页" in body(page), "提示将保留几页")

    page.get_by_role("button", name="生成新的 PDF").click()
    wait_result(page)
    check("已提取" in body(page), "结果页显示已提取页数")

    name, path = download(page)
    check(name == "selected_pages.pdf", f"提取的文件名正确（实际 {name}）")
    info = inspect_pdf(path)
    check(
        info["texts"] == ["DOC6-1", "DOC6-3", "DOC6-5"],
        f"提取的就是选中的页（实际 {info['texts']}）",
    )


# ----------------------------------------------------------------------
# 七、PDF 压缩（§5）
# ----------------------------------------------------------------------


def check_compress(page: Page) -> None:
    original = (PDFS / "image_pdf.pdf").stat().st_size

    page.goto(f"{BASE}/pdf/compress", wait_until="networkidle")
    upload(page, "image_pdf.pdf")
    text = body(page)
    check("原文件" in text and "3 页" in text, "上传后显示原文件大小与页数")

    pick(page, "compress-level", "strong")
    page.wait_for_timeout(300)
    check("96 DPI" in body(page), "选择高压缩时说明会降到多少 DPI")

    # 自定义目标大小的取值校验
    pick(page, "target-size", "custom")
    page.get_by_label("自定义目标大小").fill("0")
    page.wait_for_timeout(300)
    check("目标大小需要在 0.1–500 MB 之间" in body(page), "自定义目标大小超出范围时给出提示")
    check(
        page.get_by_role("button", name="开始压缩").is_disabled(),
        "目标大小非法时禁用压缩按钮",
    )

    # 目标比原文件还大：如实说明，不假装
    page.get_by_label("自定义目标大小").fill("5")
    page.wait_for_timeout(300)
    check(
        "目标大小已经大于原文件" in body(page),
        "目标大于原文件时如实说明不会变大",
    )

    pick(page, "target-size", "none")
    page.wait_for_timeout(300)
    check("将按「高压缩」压缩" in body(page), "说明将按哪个等级压缩")

    page.get_by_role("button", name="开始压缩").click()
    wait_result(page)
    text = body(page)
    check("✓ 压缩完成" in text, "压缩成功且文案是「✓ 压缩完成」（§5）")
    check("原文件" in text and "压缩后" in text and "节省" in text, "结果页给出压缩前后对比")

    name, path = download(page)
    check(name == "image_pdf_compressed.pdf", f"输出文件名正确（实际 {name}）")
    compressed = path.stat().st_size
    check(
        compressed < original,
        f"压缩后的文件真的更小（{original} → {compressed} 字节）",
    )
    info = inspect_pdf(path)
    check(info["pages"] == 3, "压缩没有改变页数")


# ----------------------------------------------------------------------
# 八、错误处理与文件安全（§12、§13、§17）
# ----------------------------------------------------------------------


def check_errors(page: Page) -> None:
    cases = (
        ("corrupt.pdf", "PDF 文件损坏，无法读取。", "损坏的 PDF"),
        ("fake.pdf", "暂不支持该文件格式，请上传 PDF 文件。", "内容不是 PDF 的 .pdf 文件"),
        ("encrypted.pdf", "该 PDF 已加密", "加密的 PDF"),
        ("huge.pdf", "超过一次最多处理 500 页的限制", "超过页数上限的 PDF"),
    )
    for filename, expected, label in cases:
        page.goto(f"{BASE}/pdf/to-images", wait_until="networkidle")
        upload(page, filename)
        page.wait_for_timeout(1200)
        text = body(page)
        check(expected in text, f"{label}给出明确的中文提示")
        check(
            "Something went wrong" not in text and "请求失败（HTTP" not in text,
            f"{label}没有落到通用报错文案（§12）",
        )
        check(
            page.locator("input[type=file]").count() > 0,
            f"{label}被拒绝后仍可重新选择文件",
        )

    # 扩展名不在白名单
    page.goto(f"{BASE}/pdf/to-images", wait_until="networkidle")
    page.locator("input[type=file]").first.set_input_files(str(IMAGES / "notes.txt"))
    page.wait_for_timeout(600)
    text = body(page)
    check("暂不支持该文件格式" in text, "非 PDF 扩展名在本地就被拦下")
    check("图片。" not in text.split("暂不支持")[1][:40], "提示里的名词是「文件」而不是「图片」")


# ----------------------------------------------------------------------
# 九、移动端、桌面端与第一、二阶段回归
# ----------------------------------------------------------------------


def check_layout(page: Page) -> None:
    routes = (
        "/",
        "/pdf",
        "/pdf/from-images",
        "/pdf/to-images",
        "/pdf/merge",
        "/pdf/split",
        "/pdf/edit-pages",
        "/pdf/compress",
    )
    page.set_viewport_size({"width": 390, "height": 844})
    for route in routes:
        page.goto(BASE + route, wait_until="networkidle")
        overflow = page.evaluate(
            "() => document.documentElement.scrollWidth - document.documentElement.clientWidth"
        )
        check(overflow <= 1, f"移动端 {route} 无横向溢出（溢出 {overflow}px）")

    page.set_viewport_size({"width": 1440, "height": 900})
    for route in ("/", "/pdf", "/pdf/from-images", "/pdf/compress"):
        page.goto(BASE + route, wait_until="networkidle")
        overflow = page.evaluate(
            "() => document.documentElement.scrollWidth - document.documentElement.clientWidth"
        )
        check(overflow <= 1, f"桌面端 {route} 无横向溢出（溢出 {overflow}px）")


def check_phase1_2(page: Page) -> None:
    page.goto(f"{BASE}/image/compress", wait_until="networkidle")
    page.locator("input[type=file]").first.set_input_files(str(IMAGES / "wide.jpg"))
    page.wait_for_timeout(600)
    check("wide.jpg" in body(page), "第一阶段的图片压缩页仍可上传")
    page.get_by_role("button", name="开始压缩").click()
    expect(page.get_by_text("文件处理完成")).to_be_visible(timeout=90_000)
    check(True, "第一阶段的压缩流程仍然可用")

    page.goto(f"{BASE}/image/convert", wait_until="networkidle")
    page.locator("input[type=file]").first.set_input_files(str(IMAGES / "square.png"))
    page.wait_for_timeout(600)
    pick(page, "target-format", "jpg")
    page.get_by_role("button", name="开始转换").click()
    expect(page.get_by_text("文件处理完成")).to_be_visible(timeout=90_000)
    check(True, "第二阶段的格式转换流程仍然可用")


def run(page: Page) -> None:
    check_home(page)
    check_from_images(page)
    check_to_images(page)
    check_merge(page)
    check_split(page)
    check_edit_pages(page)
    check_compress(page)
    check_errors(page)
    check_layout(page)
    check_phase1_2(page)


def main() -> int:
    build_fixtures()
    DOWNLOADS.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()
        attach(page)
        try:
            run(page)
        finally:
            browser.close()

    # 浏览器会把「服务端返回 4xx」也记成 console error，这是刻意拒绝坏文件的正常现象
    ignored = ("Failed to load resource",)
    real_errors = [e for e in console_errors if not any(x in e for x in ignored)]
    rejected = [e for e in console_errors if any(x in e for x in ignored)]

    print("\n--- 控制台报错 ---")
    for err in real_errors or ["（无）"]:
        print(err)
    print(f"（服务端正常拒绝坏文件产生的 4xx 记录 {len(rejected)} 条，已忽略）")
    check(not real_errors, "浏览器控制台无 JS 报错")

    failed = [label for ok, label in results if not ok]
    lines = [f"{'PASS' if ok else 'FAIL'}  {label}" for ok, label in results]
    lines.append("")
    lines.append(f"共 {len(results)} 项，通过 {len(results) - len(failed)} 项")
    lines.append(f"服务端拒绝坏文件的 4xx 记录：{len(rejected)} 条（正常）")
    if real_errors:
        lines.append("控制台 JS 报错：")
        lines.extend("  " + err for err in real_errors)
    if failed:
        lines.append("未通过：")
        lines.extend("  - " + label for label in failed)
    else:
        lines.append("全部通过")
    REPORT.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n共 {len(results)} 项，通过 {len(results) - len(failed)} 项")
    print(f"详细报告：{REPORT}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
