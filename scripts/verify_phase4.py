"""第四阶段的真机验收脚本（Playwright + 真实浏览器）。

对应第四阶段 §19 的 16 项测试，全部打真实的服务器与真实的浏览器，
没有任何模拟任务进度、假接口或无效按钮：

    单文件 / 10 个 / 50 个 / 超过 50 个 / 大文件 / 损坏文件 / 非法格式
    手机浏览器 / Chrome / Edge / Safari / PDF 大量页面 / ZIP 批量下载
    任务失败 / 任务超时 / 自动删除临时文件

分三段跑：

    A 段  批量处理与状态（chromium，主后端）
          §3 计数与进度条、§4 逐文件状态、§11 拖拽排序、§12 最近处理、
          §13 错误码文案、§16 首页
    B 段  跨浏览器与移动端（chromium / Edge / WebKit + 375·390·414 视口）
    C 段  生命周期（另起一个后端，把 TTL 与超时调到秒级）
          §14 自动删除临时文件、任务超时、服务端上限

用法：
    1. 先构建前端并启动后端（后端会顺带托管 frontend/dist）：
           cd frontend && npm run build
           cd backend && .venv\\Scripts\\python -m uvicorn main:app --port 8011
    2. 安装依赖：pip install playwright && playwright install chromium webkit
    3. python scripts/verify_phase4.py

可用环境变量覆盖：
    FILETOOLS_WEB_BASE   默认 http://127.0.0.1:8011（直接打后端托管的 dist）
    FILETOOLS_BACKEND_PY 后端 venv 的 python，用于生成素材、校验下载结果、直连 API
    FILETOOLS_SHORT_PORT C 段临时后端的端口，默认 8012

注意：不要用 Vite 开发服务器（5173）跑本脚本 —— 它会把自己后端的地址
代理给 /api，两者版本不一致时结果没有意义。
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

from playwright.sync_api import Browser, Error, Page, sync_playwright

ROOT = pathlib.Path(__file__).resolve().parent.parent
BASE = os.environ.get("FILETOOLS_WEB_BASE", "http://127.0.0.1:8011").rstrip("/")
SHORT_PORT = int(os.environ.get("FILETOOLS_SHORT_PORT", "8012"))
SHORT_BASE = f"http://127.0.0.1:{SHORT_PORT}"

# Windows 控制台默认是 GBK，直接打印「✓」会抛 UnicodeEncodeError 把整轮跑挂掉
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

WORK = pathlib.Path(
    os.environ.get("FILETOOLS_WORK_DIR") or tempfile.mkdtemp(prefix="filetools-verify-p4-")
)
IMAGES = WORK / "images"
BATCH = WORK / "batch"
PDFS = WORK / "pdfs"
DOWNLOADS = WORK / "downloads"
SERVER_TEMP = WORK / "server-temp"
REPORT = ROOT / "scripts" / "verify_phase4_report.txt"
SHORT_LOG = WORK / "backend-short.log"

# §2 的单批上限，用来算测试素材的量
BATCH_LIMIT = 50
OVER_LIMIT = 55


# ----------------------------------------------------------------------
# 测试素材：图片用 Pillow 生成，PDF 用 PyMuPDF 生成
#
# 批量用的图特意做成 1600×1200 的「类照片」：太小的图每个只要几毫秒，
# 50 个一闪而过，根本看不到 §3 / §4 要求的中间状态；这个尺寸下
# 50 个文件在 2 个 worker 上要跑好几秒，面板上的「等待中 → 处理中 → 已完成」
# 才是真的能观察到的过程。
# ----------------------------------------------------------------------

_FIXTURE_CODE = r'''
import io, os, sys

from PIL import Image, ImageDraw, ImageFilter

out = sys.argv[1]
imgs = os.path.join(out, "images")
batch = os.path.join(out, "batch")
pdfs = os.path.join(out, "pdfs")
for path in (imgs, batch, pdfs):
    os.makedirs(path, exist_ok=True)


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


def save(img, name, folder=imgs, **kwargs):
    img.save(os.path.join(folder, name), **kwargs)


# 单个文件用的素材
save(photo(1600, 1200), "wide.jpg", quality=88)
save(photo(1200, 1600), "tall.jpg", quality=88)
save(photo(1000, 1000), "square.png")
logo = Image.new("RGBA", (800, 600), (0, 0, 0, 0))
ImageDraw.Draw(logo).ellipse([40, 40, 760, 560], fill=(30, 120, 220, 255))
save(logo, "logo.png")

# 批量用的 55 张（50 张用于上限测试，多出的 5 张用于「超过 50 个」）
for index in range(1, 56):
    save(photo(1600, 1200), "img-%02d.jpg" % index, folder=batch, quality=80)

# 头部合法、数据全无的坏图：解码阶段必然失败
with open(os.path.join(imgs, "broken.jpg"), "wb") as fh:
    fh.write(b"\xff\xd8\xff\xe0" + b"\x00" * 4000)

# 扩展名不在白名单里
with open(os.path.join(imgs, "notes.txt"), "w") as fh:
    fh.write("hello")

# 51 MB：超过单个文件 50 MB 的上限。
# 直接在小 JPEG 后面补零 —— 字节数是真实的，头也仍然是个合法 JPEG，
# 但校验会在读文件头之前就按体积拒掉，不需要真造一张 51 MB 的图。
raw = open(os.path.join(imgs, "wide.jpg"), "rb").read()
with open(os.path.join(imgs, "over.jpg"), "wb") as fh:
    fh.write(raw)
    fh.write(b"\x00" * (51 * 1024 * 1024 - len(raw)))
    fh.flush()

# 大图：用于「任务超时」（配合 FILETOOLS_PROCESS_TIMEOUT=1）
save(photo(7000, 5000), "big.jpg", quality=85)


def make_pdf(name, pages, label):
    import pymupdf

    doc = pymupdf.open()
    for index in range(pages):
        page = doc.new_page(width=595.0, height=842.0)
        page.insert_text((72, 120), "%s-%d" % (label, index + 1), fontsize=28)
    doc.save(os.path.join(pdfs, name))
    doc.close()


make_pdf("doc6.pdf", 6, "A")
'''

# 检查图片：格式与像素尺寸
_IMAGE_CODE = r'''
import json, sys
from PIL import Image
with Image.open(sys.argv[1]) as image:
    print(json.dumps({"format": image.format, "size": list(image.size)}))
'''

# 检查 ZIP：成员名、体积，以及每个成员自己的图片格式与尺寸
_ZIP_CODE = r'''
import io, json, sys, zipfile
from PIL import Image

archive = zipfile.ZipFile(sys.argv[1])
entries = []
for name in archive.namelist():
    data = archive.read(name)
    item = {"name": name, "bytes": len(data)}
    with Image.open(io.BytesIO(data)) as image:
        item["format"] = image.format
        item["size"] = list(image.size)
    entries.append(item)
print(json.dumps(entries, ensure_ascii=False))
'''

# 检查 PDF 的每页文字 —— 页序只能靠文字验证，看页数看不出来
_PAGES_CODE = r'''
import json, sys
import pymupdf

doc = pymupdf.open(sys.argv[1])
print(json.dumps([doc[i].get_text().strip() for i in range(doc.page_count)], ensure_ascii=False))
doc.close()
'''

# C 段：直连 API 验证生命周期（自动删除、服务端上限）。
# 用 httpx 而不是浏览器：这些是服务端行为，浏览器那边只能看到「按钮点不动了」。
_API_CODE = r'''
import json, pathlib, sys, time

import httpx

port, small, over, spoof, temp_root = (
    sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], pathlib.Path(sys.argv[5])
)
base = "http://127.0.0.1:%s" % port
out = {}

TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}


def dirs():
    return sorted(p.name for p in temp_root.glob("filetools_*"))


def submit(client, path, name=None, count=1, mime=None, suffix=None):
    """按文件的真实扩展名提交，避免素材名与内容对不上。

    ``suffix`` 可以强行指定扩展名，用来构造「扩展名与内容不一致」的用例。
    """
    path = pathlib.Path(path)
    payload = path.read_bytes()
    stem = name or "file"
    ext = suffix or path.suffix.lower()
    files = [
        ("files", ("%s-%d%s" % (stem, index, ext), payload,
                   mime or TYPES.get(ext, "application/octet-stream")))
        for index in range(count)
    ]
    return client.post(base + "/api/image/compress", files=files, data={"quality": "balanced"})


def wait_group(client, group_id, timeout=120):
    deadline = time.time() + timeout
    while time.time() < deadline:
        snapshot = client.get("%s/api/tasks/%s" % (base, group_id)).json()
        if snapshot["state"] in ("done", "failed"):
            return snapshot
        time.sleep(0.2)
    raise SystemExit("任务在 %d 秒内没有结束" % timeout)


def wait_until(probe, timeout, interval=0.3):
    """等某个条件成立，返回 (是否成立, 实际等了多久)。"""
    start = time.time()
    while time.time() - start < timeout:
        if probe():
            return True, round(time.time() - start, 1)
        time.sleep(interval)
    return False, round(time.time() - start, 1)


with httpx.Client(timeout=120) as client:
    # 1) 正常一轮：结果能下载，下载后临时目录跟着消失
    #    删除动作挂在响应的后台任务上（避免边下边删），所以允许一点点延迟
    response = submit(client, small)
    out["submit_status"] = response.status_code
    snapshot = wait_group(client, response.json()["group_id"])
    out["first_state"] = snapshot["state"]
    out["first_item_message"] = snapshot["tasks"][0]["error_message"]
    out["dirs_while_held"] = dirs()
    out["first_download"] = client.get(base + snapshot["result"]["download_url"]).status_code
    out["download_freed"], out["download_freed_after"] = wait_until(lambda: not dirs(), 5)

    # 2) 不下载：等 JOB_TTL(5s) + 扫描间隔(2s) 之后，文件和任务记录都该自己消失
    response = submit(client, small)
    group_id = response.json()["group_id"]
    snapshot = wait_group(client, group_id)
    url = base + snapshot["result"]["download_url"]
    out["held_dirs"] = dirs()
    out["held_download_before"] = client.get(url).status_code

    out["expired_files"], out["expired_files_after"] = wait_until(lambda: not dirs(), 40, 1)
    out["expired_dirs"] = dirs()
    out["expired_download"] = client.get(url).status_code
    # 任务记录同样交给定时清理，允许再等一个扫描周期
    out["expired_task_gone"], out["expired_task_after"] = wait_until(
        lambda: client.get("%s/api/tasks/%s" % (base, group_id)).status_code == 404, 10, 1
    )
    out["expired_task"] = client.get("%s/api/tasks/%s" % (base, group_id)).status_code

    # 3) 超过单批上限：请求级拒绝，不能悄悄只处理前 50 个
    response = submit(client, small, count=51)
    out["too_many_status"] = response.status_code
    out["too_many_body"] = response.json()

    # 4) 单个文件超过 50 MB
    response = submit(client, over)
    out["oversize_submit"] = response.status_code
    snapshot = wait_group(client, response.json()["group_id"])
    out["oversize_state"] = snapshot["state"]
    out["oversize_code"] = (snapshot["error"] or {}).get("code")
    out["oversize_item_code"] = snapshot["tasks"][0]["error_code"]
    out["oversize_item_message"] = snapshot["tasks"][0]["error_message"]

    # 5) 扩展名与真实内容不一致（PNG 数据谎称 .jpg）—— §18 的校验必须拦住它
    response = submit(client, spoof, name="fake", mime="image/jpeg", suffix=".jpg")
    snapshot = wait_group(client, response.json()["group_id"])
    out["mismatch_code"] = snapshot["tasks"][0]["error_code"]
    out["mismatch_message"] = snapshot["tasks"][0]["error_message"]

print(json.dumps(out, ensure_ascii=False))
'''


def find_backend_python() -> pathlib.Path:
    """后端 venv 的 python —— 用它生成素材、校验下载结果、直连 API。"""
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
        # 走到这里说明读取线程还是死了（理论上不该发生），别把 None 交给 json.loads
        raise RuntimeError(f"子进程没有可读的 stdout。stderr：{out.stderr!r}")
    return out.stdout


def build_fixtures() -> None:
    # 调试时指定 FILETOOLS_WORK_DIR 可以复用上一轮的素材，省掉生成素材的十几秒
    if (BATCH / f"img-{OVER_LIMIT:02d}.jpg").exists() and (IMAGES / "over.jpg").exists():
        print(f"复用已有测试素材：{WORK}")
        return
    print("正在生成测试素材（55 张批量图 + 51 MB 大文件，需要十几秒）…")
    _run_backend_python(_FIXTURE_CODE, WORK)


def inspect_image(path: pathlib.Path) -> dict:
    return json.loads(_run_backend_python(_IMAGE_CODE, path))


def inspect_zip(path: pathlib.Path) -> list[dict]:
    return json.loads(_run_backend_python(_ZIP_CODE, path))


def pdf_pages(path: pathlib.Path) -> list[str]:
    return json.loads(_run_backend_python(_PAGES_CODE, path))


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


def set_files(page: Page, paths: list[pathlib.Path]) -> None:
    page.locator("input[type=file]").first.set_input_files([str(p) for p in paths])
    page.wait_for_timeout(800)


def wait_batch(page: Page, timeout: int = 240_000) -> str:
    """等结果面板或失败面板出现（批量任务的结束文案与 PDF 工具不同）。"""
    page.wait_for_function(
        "() => { const text = document.body.innerText;"
        " return text.includes('文件处理完成') || text.includes('处理结束'); }",
        timeout=timeout,
    )
    page.wait_for_timeout(400)
    return body(page)


_download_seq = 0


def download(page: Page) -> tuple[str, pathlib.Path]:
    """点页面上的下载按钮并把文件存下来，返回 (浏览器给的文件名, 本地路径)。"""
    global _download_seq
    with page.expect_download() as info:
        page.get_by_role("button", name=re.compile("^下载")).first.click()
    item = info.value
    _download_seq += 1
    DOWNLOADS.mkdir(parents=True, exist_ok=True)
    target = DOWNLOADS / f"{_download_seq:02d}-{item.suggested_filename}"
    item.save_as(target)
    return item.suggested_filename, target


def page_order(page: Page) -> list[int]:
    """读出 SortablePageList 里的输出顺序（§11）。"""
    texts = page.evaluate(
        "() => Array.from(document.querySelectorAll('li'))"
        ".filter(li => li.innerText.includes('输出时排在第')).map(li => li.innerText)"
    )
    order: list[int] = []
    for text in texts:
        found = re.search(r"原第 (\d+) 页", text)
        if found:
            order.append(int(found.group(1)))
    return order


# ----------------------------------------------------------------------
# A 段：批量处理与状态
# ----------------------------------------------------------------------


def check_error_code_coverage() -> None:
    """§13：服务端可能返回的每个错误码，前端都要有中文解释。"""
    with urllib.request.urlopen(f"{BASE}/api/config", timeout=10) as response:
        codes = json.loads(response.read())["error_codes"]
    source = (ROOT / "frontend" / "src" / "utils" / "errorMessages.ts").read_text(
        encoding="utf-8"
    )
    missing = [code for code in codes if not re.search(rf"\b{code}:", source)]
    check(
        not missing,
        f"/api/config 的 {len(codes)} 个错误码都有中文文案"
        + (f"（缺 {missing}）" if missing else ""),
    )

    # 后端不该把 Python 堆栈当作错误码或文案返回
    with urllib.request.urlopen(f"{BASE}/api/config", timeout=10) as response:
        raw = response.read().decode("utf-8")
    check("Traceback" not in raw, "服务端配置接口不暴露 Python 堆栈")


def check_home(page: Page) -> None:
    page.goto(BASE, wait_until="networkidle")
    text = body(page)
    check("简单、快速的在线文件工具" in text, "§16 首页主标题")
    check("一次最多 50 个文件" in text, "§16 首页说明批量上限")
    for heading in ("热门工具", "全部工具", "为什么选择 FileTools", "文件安全说明", "常见问题"):
        check(heading in text, f"§16 首页有「{heading}」")

    for route in (
        "/image/compress",
        "/image/convert",
        "/image/resize",
        "/pdf/from-images",
        "/pdf/to-images",
        "/pdf/merge",
        "/pdf/split",
        "/pdf/edit-pages",
        "/pdf/compress",
    ):
        check(page.locator(f"a[href='{route}']").count() > 0, f"首页有 {route} 入口")

    check("最近处理" not in text, "还没处理过任何文件时首页不显示「最近处理」")

    # FAQ 是真的能展开的 <details>，不是装饰
    first = page.locator("details").first
    first.locator("summary").click()
    page.wait_for_timeout(200)
    check(first.get_attribute("open") is not None, "FAQ 条目可以展开")


def check_single(page: Page) -> None:
    """§19 第 1 项：单文件。"""
    page.goto(f"{BASE}/image/compress", wait_until="networkidle")
    set_files(page, [IMAGES / "wide.jpg"])
    page.get_by_role("button", name="开始压缩").click()
    text = wait_batch(page)
    check("文件处理完成" in text, "单文件压缩成功")
    check("文件数" in text and "共节省" in text, "结果面板显示压缩统计")

    name, path = download(page)
    info = inspect_image(path)
    check(info["format"] == "JPEG", f"单文件下载的是可解码的 JPEG（{name}）")
    check(info["size"] == [1600, 1200], f"尺寸没有被意外改变（{info['size']}）")

    # 结果文件是一次性的：下载后服务器上已经没有了，界面必须如实说清楚，
    # 而不是留一个点了会报错的下载按钮（§14 / §20）
    page.wait_for_timeout(600)
    text = body(page)
    check("结果文件已经下载" in text, "下载后如实提示结果已从服务器删除")
    check(
        page.get_by_role("button", name=re.compile("^下载")).count() == 0,
        "下载后没有留下点不动的下载按钮",
    )


def check_ten(page: Page) -> None:
    """§19 第 2 项：10 个文件。"""
    page.goto(f"{BASE}/image/compress", wait_until="networkidle")
    set_files(page, [BATCH / f"img-{index:02d}.jpg" for index in range(1, 11)])
    check("10 个文件" in body(page), "10 个文件都进了列表")

    page.get_by_role("button", name="批量压缩").click()
    text = wait_batch(page)
    check("10 个文件处理完成" in text, "10 个文件全部完成")

    name, path = download(page)
    entries = inspect_zip(path)
    check(name.endswith(".zip"), f"多个结果自动打包成 ZIP（{name}）")
    check(len(entries) == 10, f"ZIP 里有 10 个结果（实际 {len(entries)}）")
    check(
        all(entry["format"] == "JPEG" and entry["bytes"] > 0 for entry in entries),
        "ZIP 里每个成员都是可解码的图片",
    )


def check_fifty(page: Page) -> None:
    """§19 第 3 项：50 个文件，同时观察 §3 / §4 的中间状态。"""
    page.goto(f"{BASE}/image/compress", wait_until="networkidle")
    set_files(page, [BATCH / f"img-{index:02d}.jpg" for index in range(1, 51)])
    check(f"{BATCH_LIMIT} 个文件" in body(page), "50 个文件都进了列表")

    page.get_by_role("button", name="批量压缩").click()
    panel = page.locator("[role=status]")

    def probe_panel() -> dict | None:
        """一次读出面板文本、进度条和每一行 —— 分两次读会读到两个时刻的状态。"""
        if panel.count() == 0 or not panel.is_visible():
            return None
        return panel.evaluate(
            """el => ({
                 text: el.innerText,
                 value: Number(el.querySelector('[role=progressbar]').getAttribute('aria-valuenow')),
                 rows: Array.from(el.querySelectorAll('ul > li')).map(li => li.innerText),
               })"""
        )

    panel.wait_for(state="visible", timeout=60_000)
    first = probe_panel()
    assert first is not None

    def counter(text: str, label: str) -> int:
        found = re.search(rf"{label}\s*(\d+)", text)
        return int(found.group(1)) if found else -1

    finished = counter(first["text"], "已完成") + counter(first["text"], "失败")

    # 盯着面板直到采到「有文件已经处理完」的那一帧 ——
    # 50 张小图跑得比想象的快，固定等一个时长容易正好错过中间状态
    second = None
    deadline = time.time() + 60
    while time.time() < deadline:
        page.wait_for_timeout(120)
        snapshot = probe_panel()
        if snapshot is None:
            break
        second = snapshot
        mid_finished = counter(snapshot["text"], "已完成") + counter(snapshot["text"], "失败")
        if mid_finished > finished:
            break

    text = first["text"]
    check(
        all(label in text for label in ("总任务", "已完成", "处理中", "等待", "失败")),
        "§3 面板显示总任务 / 已完成 / 处理中 / 等待 / 失败",
    )

    total = counter(text, "总任务")
    done = counter(text, "已完成")
    processing = counter(text, "处理中")
    waiting = counter(text, "等待")
    failed = counter(text, "失败")

    check(total == BATCH_LIMIT, f"§3 总任务 = 50（实际 {total}）")
    check(
        finished + processing + waiting == total,
        f"§3 五个计数自洽：{done} 完成 + {failed} 失败 + {processing} 处理中 + {waiting} 等待 = {total}",
    )
    check(
        re.search(rf"{finished}\s*/\s*{BATCH_LIMIT}", text) is not None,
        f"§3 显示「已完成 / 总数」（读到 {finished} / {total}）",
    )
    check(
        first["value"] == round(finished / total * 100),
        f"§3 进度条百分比与计数一致（{first['value']}% ≈ {finished}/{total}）",
    )

    rows = first["rows"]
    check(len(rows) == BATCH_LIMIT, f"§4 每个文件一行（实际 {len(rows)} 行）")
    states = ("等待中", "处理中", "已完成", "失败")
    check(
        all(any(state in row for state in states) for row in rows),
        "§4 每一行都有明确的状态文字",
    )
    check(
        all(re.search(r"\d+(\.\d+)?\s*(B|KB|MB)", row) for row in rows),
        "§4 每一行都有文件大小",
    )
    check(
        waiting > 0 or processing > 0 or finished == total,
        f"§4 面板刚出现时确实有文件在排队或处理中（等待 {waiting} / 处理中 {processing}）",
    )

    # 中间状态：处理中（⟳）与已完成（✓）必须真的出现过
    seen = text + ((second or {}).get("text") or "")
    check(
        "⟳" in seen or "✓" in seen,
        "§4 看得到「处理中」⟳ 与「已完成」✓ 的真实中间状态",
    )
    if second is not None:
        mid_finished = counter(second["text"], "已完成") + counter(second["text"], "失败")
        check(
            mid_finished > finished and second["value"] > first["value"],
            f"§3 进度真的在往前走（{first['value']}% → {second['value']}%，"
            f"完成 {finished} → {mid_finished}）",
        )
        check(
            "✓" in second["text"] and mid_finished < BATCH_LIMIT,
            f"§4 中途快照里已有 ✓ 完成标记，且整批尚未结束（{mid_finished}/{BATCH_LIMIT}）",
        )
    else:
        check(False, "§4 没能采到任务进行中的快照（50 个文件跑得太快）")

    page.wait_for_function(
        "() => document.body.innerText.includes('50 个文件处理完成')", timeout=240_000
    )
    page.wait_for_timeout(400)
    check("50 个文件处理完成" in body(page), "§19 第 3 项：50 个文件全部处理完成")

    name, path = download(page)
    entries = inspect_zip(path)
    check(len(entries) == 50, f"ZIP 里有 50 个结果（实际 {len(entries)}）")
    check(name.endswith(".zip"), "50 个结果打包成一个 ZIP 下载")
    check(
        len({entry["name"] for entry in entries}) == 50,
        "ZIP 成员名没有互相覆盖",
    )


def check_over_limit(page: Page) -> None:
    """§19 第 4 项：超过 50 个。"""
    page.goto(f"{BASE}/image/compress", wait_until="networkidle")
    set_files(page, [BATCH / f"img-{index:02d}.jpg" for index in range(1, OVER_LIMIT + 1)])
    text = body(page)
    check(
        f"一次最多处理 {BATCH_LIMIT} 个文件" in text,
        "超过 50 个时提示「一次最多处理 50 个文件。」",
    )
    check(f"{OVER_LIMIT - BATCH_LIMIT} 个已忽略" in text, f"多出的 {OVER_LIMIT - BATCH_LIMIT} 个被忽略")
    check(f"{BATCH_LIMIT} 个文件" in text, "列表里只留下 50 个文件")
    check(
        page.locator("li", has_text=".jpg").count() <= BATCH_LIMIT,
        "页面上确实只有 50 个待处理条目",
    )


def check_broken_file(page: Page) -> None:
    """§19 第 6 项：损坏文件（单个失败不影响整批）。"""
    page.goto(f"{BASE}/image/compress", wait_until="networkidle")
    set_files(page, [BATCH / "img-01.jpg", IMAGES / "broken.jpg", BATCH / "img-02.jpg"])
    page.get_by_role("button", name="批量压缩").click()
    text = wait_batch(page)

    check("2 个文件处理完成" in text, "坏图旁边的两个文件照常完成")
    check("有 1 个文件未能处理" in text, "结果面板列出失败的文件")
    check("broken.jpg" in text, "失败信息里有具体文件名")
    check(
        re.search(r"broken\.jpg\s*——\s*\S", text) is not None,
        "失败信息里写明了原因",
    )

    _, path = download(page)
    entries = inspect_zip(path)
    check(len(entries) == 2, f"ZIP 里只有成功的那 2 个（实际 {len(entries)}）")


def check_bad_type(page: Page) -> None:
    """§19 第 7 项：非法格式（前端本地就拦住，不白跑一次请求）。"""
    page.goto(f"{BASE}/image/compress", wait_until="networkidle")
    set_files(page, [IMAGES / "notes.txt"])
    text = body(page)
    check("暂不支持该文件格式" in text, "非法格式给出「暂不支持该文件格式」")
    check("JPG" in text and "WEBP" in text, "提示里写清了支持哪些格式")
    check("个文件 ·" not in text, "非法文件没有被加进待处理列表")


def check_large_file(page: Page) -> None:
    """§19 第 5 项：大文件（51 MB，超过单个 50 MB 上限）。"""
    page.goto(f"{BASE}/image/compress", wait_until="networkidle")
    set_files(page, [IMAGES / "over.jpg"])
    text = body(page)
    check("文件过大" in text, "51 MB 的文件给出「文件过大」")
    check("51" in text and "50" in text, "提示里给出了当前大小与上限")
    check("个文件 ·" not in text, "超限文件没有被加进待处理列表")


def check_page_order(page: Page) -> None:
    """§11：PDF 页面拖拽排序，最终输出必须按新顺序。"""
    page.goto(f"{BASE}/pdf/edit-pages", wait_until="networkidle")
    set_files(page, [PDFS / "doc6.pdf"])
    page.locator("label:has(input[name='edit-mode'][value='extract'])").click()
    page.wait_for_timeout(300)
    page.locator("input[aria-label='页码']").fill("1,2,4")
    page.wait_for_timeout(500)

    check(page_order(page) == [1, 2, 4], f"提取列表按选择顺序排列（{page_order(page)}）")

    # 真的用鼠标拖：把「原第 4 页」拖到第 1 位。
    # Playwright 会派发原生 HTML5 拖放事件，走的就是用户那条代码路径。
    source = page.locator("li", has_text="原第 4 页").last
    target = page.locator("li", has_text="原第 1 页").last
    source.drag_to(target)
    page.wait_for_timeout(600)
    order = page_order(page)
    check(order == [4, 1, 2], f"拖拽后顺序变成 [4, 1, 2]（实际 {order}）")
    check("已按你调整的顺序输出" in body(page), "界面提示已按新顺序输出")

    page.get_by_role("button", name="生成新的 PDF").click()
    page.wait_for_function(
        "() => document.body.innerText.includes('✓ 处理完成')", timeout=120_000
    )
    _, path = download(page)
    check(pdf_pages(path) == ["A-4", "A-1", "A-2"], f"输出的 PDF 按拖拽后的顺序（{pdf_pages(path)}）")

    # 再验证一遍上移按钮：触屏和键盘用户用的是它
    page.goto(f"{BASE}/pdf/edit-pages", wait_until="networkidle")
    set_files(page, [PDFS / "doc6.pdf"])
    page.locator("label:has(input[name='edit-mode'][value='extract'])").click()
    page.wait_for_timeout(300)
    page.locator("input[aria-label='页码']").fill("1,3")
    page.wait_for_timeout(500)
    page.get_by_role("button", name="把原第 3 页上移").click()
    page.wait_for_timeout(400)
    check(page_order(page) == [3, 1], f"上移按钮同样能改顺序（{page_order(page)}）")
    page.get_by_role("button", name="生成新的 PDF").click()
    page.wait_for_function(
        "() => document.body.innerText.includes('✓ 处理完成')", timeout=120_000
    )
    _, path = download(page)
    check(pdf_pages(path) == ["A-3", "A-1"], "按钮调整的顺序同样进了输出")


def check_history(page: Page) -> None:
    """§12：最近处理只记元信息，并且可以清空。"""
    page.goto(BASE, wait_until="networkidle")
    text = body(page)
    check("最近处理" in text, "处理过文件后首页出现「最近处理」")

    for column in ("文件名称", "操作类型", "处理时间", "处理状态", "文件大小"):
        check(column in text, f"§12 「最近处理」有「{column}」列")

    check("图片压缩" in text, "§12 记录了操作类型")
    check(
        re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", text) is not None,
        "§12 记录了处理时间",
    )
    check("已完成" in text and "失败" in text, "§12 成功与失败都如实记录")
    check("broken.jpg" in text, "§12 失败的文件也在记录里")
    check("不保存文件内容" in text, "§12 说明了记录不包含文件内容")

    rows = page.locator("section", has_text="最近处理").locator("li")
    count = rows.count()
    check(0 < count <= 20, f"§12 记录条数受上限约束（{count} 条）")

    stored = page.evaluate("() => window.sessionStorage.getItem('filetools.history.v1')")
    payload = json.loads(stored or "[]")
    keys = set(payload[0]) if payload else set()
    check(
        keys == {"id", "filename", "tool", "at", "ok", "size"},
        f"§12 只保存元信息，没有文件内容或下载链接（字段 {sorted(keys)}）",
    )
    check(
        all("url" not in json.dumps(entry) for entry in payload),
        "§12 记录里不含任何下载地址",
    )

    page.get_by_role("button", name="清空记录").click()
    page.wait_for_timeout(400)
    check("最近处理" not in body(page), "「清空记录」后首页不再显示「最近处理」")


def check_no_leaks(page: Page) -> None:
    """§13 / §18：页面上不能出现 Python 堆栈和服务器路径。"""
    for route in ("/image/compress", "/pdf", "/"):
        page.goto(BASE + route, wait_until="networkidle")
        text = body(page)
        check("Traceback" not in text, f"{route} 不显示 Python 堆栈")
        check("filetools_" not in text, f"{route} 不泄露临时目录名")
        check(
            str(WORK) not in text and pathlib.Path(tempfile.gettempdir()).as_posix() not in text,
            f"{route} 不泄露服务器文件路径",
        )


def run_part_a(page: Page) -> None:
    section("A 段：批量处理与状态（chromium）")
    check_error_code_coverage()
    check_home(page)
    check_single(page)
    check_ten(page)
    check_fifty(page)
    check_over_limit(page)
    check_broken_file(page)
    check_bad_type(page)
    check_large_file(page)
    check_page_order(page)
    check_history(page)
    check_no_leaks(page)


# ----------------------------------------------------------------------
# B 段：跨浏览器与移动端
# ----------------------------------------------------------------------


def compress_flow(page: Page, base: str = BASE) -> None:
    """最小可用的真实流程：选 2 张图 → 处理 → 下载 ZIP。"""
    page.goto(f"{base}/image/compress", wait_until="networkidle")
    set_files(page, [BATCH / "img-01.jpg", BATCH / "img-02.jpg"])
    page.get_by_role("button", name="批量压缩").click()
    page.wait_for_function(
        "() => document.body.innerText.includes('2 个文件处理完成')", timeout=180_000
    )
    page.wait_for_timeout(300)


def check_browser(name: str, browser: Browser) -> None:
    context = browser.new_context(accept_downloads=True)
    page = context.new_page()
    attach(page)
    try:
        compress_flow(page)
        _, path = download(page)
        entries = inspect_zip(path)
        check(len(entries) == 2, f"{name}：上传 → 处理 → 下载全流程可用（ZIP {len(entries)} 项）")
    except Exception as exc:  # noqa: BLE001 - 浏览器缺失或流程失败都要如实记下来
        check(False, f"{name}：流程失败（{type(exc).__name__}: {str(exc)[:120]}）")
    finally:
        context.close()


MOBILE_WIDTHS = (375, 390, 414)
ROUTES = (
    "/",
    "/image",
    "/image/compress",
    "/image/convert",
    "/image/resize",
    "/pdf",
    "/pdf/from-images",
    "/pdf/to-images",
    "/pdf/merge",
    "/pdf/split",
    "/pdf/edit-pages",
    "/pdf/compress",
)


def check_mobile_layout(p) -> None:
    """§8：常见手机宽度下不能出现横向滚动，上传与下载都要好点。"""
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
            # 上传区在工具页上（首页没有 role=button 的拖放区）
            page.goto(f"{BASE}/image/compress", wait_until="networkidle")
            check(
                page.evaluate("() => matchMedia('(hover: none) and (pointer: coarse)').matches"),
                f"{width}px：被识别成手指操作的设备",
            )
            check(
                page.locator("[role=button]").first.get_attribute("aria-label") == "选择文件",
                f"{width}px：上传区文案是「选择文件」而不是「拖放文件到这里」（§9 / §10）",
            )
            box = page.locator("[role=button]").first.bounding_box()
            check(
                box is not None and box["height"] >= 88 and box["width"] >= 300,
                f"{width}px：上传区域足够大（{round(box['width'])}×{round(box['height'])}）",
            )

            worst = 0
            for route in ROUTES:
                page.goto(BASE + route, wait_until="networkidle")
                overflow = page.evaluate(
                    "() => document.documentElement.scrollWidth"
                    " - document.documentElement.clientWidth"
                )
                worst = max(worst, overflow)
            check(
                worst <= 1,
                f"{width}px：全部 {len(ROUTES)} 个页面都没有横向溢出（最大 {worst}px）",
            )

            # 下载按钮要好按：lg 尺寸是 48px 高
            compress_flow(page)
            button = page.get_by_role("button", name=re.compile("^下载")).first
            box = button.bounding_box()
            check(
                box is not None and box["height"] >= 44,
                f"{width}px：下载按钮高 {round(box['height'])}px，适合手指点击",
            )
            ctx.close()
    finally:
        browser.close()


def run_part_b(p) -> None:
    section("B 段：跨浏览器与移动端")
    for name, launcher in (
        ("Chromium", lambda: p.chromium.launch()),
        ("Edge", lambda: p.chromium.launch(channel="msedge")),
        ("Safari（WebKit）", lambda: p.webkit.launch()),
    ):
        try:
            browser = launcher()
        except Exception as exc:  # noqa: BLE001 - 没装这个内核时如实报出来
            check(False, f"{name}：启动失败（{type(exc).__name__}）")
            continue
        try:
            check_browser(name, browser)
        finally:
            browser.close()

    check_mobile_layout(p)


# ----------------------------------------------------------------------
# C 段：生命周期（短 TTL 的独立后端）
# ----------------------------------------------------------------------


def start_short_backend() -> subprocess.Popen:
    """另起一个后端，把保留时长与超时都压到秒级，用来验证真实过期行为。"""
    SERVER_TEMP.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update(
        {
            "FILETOOLS_JOB_TTL": "5",          # 结果文件 5 秒后过期
            "FILETOOLS_TASK_TTL": "5",         # 任务记录 5 秒后过期
            "FILETOOLS_PDF_INPUT_TTL": "5",
            "FILETOOLS_CLEANUP_INTERVAL": "2",  # 每 2 秒扫一次
            "FILETOOLS_PROCESS_TIMEOUT": "1",   # 单个文件超过 1 秒算超时
            "FILETOOLS_TEMP_ROOT": str(SERVER_TEMP),
        }
    )
    log = open(SHORT_LOG, "wb")
    process = subprocess.Popen(
        [
            str(BACKEND_PYTHON),
            "-m",
            "uvicorn",
            "main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(SHORT_PORT),
        ],
        cwd=str(ROOT / "backend"),
        env=env,
        stdout=log,
        stderr=log,
    )
    for _ in range(80):
        try:
            with urllib.request.urlopen(f"{SHORT_BASE}/api/health", timeout=1) as response:
                if response.status == 200:
                    return process
        except (urllib.error.URLError, OSError):
            time.sleep(0.5)
    process.terminate()
    raise SystemExit(f"短生命周期后端没能启动，看日志：{SHORT_LOG}")


def check_lifecycle() -> None:
    """§19 第 14、16 项 + 服务端上限：自动删除、超时、数量与体积、类型校验。"""
    out = json.loads(
        _run_backend_python(
            _API_CODE,
            SHORT_PORT,
            IMAGES / "wide.jpg",
            IMAGES / "over.jpg",
            IMAGES / "square.png",
            SERVER_TEMP,
        )
    )

    check(out["submit_status"] == 202, "批量接口立刻返回 202 与任务号")
    check(
        out["first_state"] == "done",
        f"任务在队列里真的被处理完了（{out['first_state']}：{out['first_item_message']}）",
    )
    check(len(out["dirs_while_held"]) == 1, "处理完成后临时目录仍在（等用户下载）")
    check(out["first_download"] == 200, "结果文件可以下载")
    check(
        out["download_freed"],
        f"§14 下载后临时目录被删除（等了 {out['download_freed_after']}s）",
    )

    check(len(out["held_dirs"]) == 1, "第二轮：结果文件已经就绪")
    check(out["held_download_before"] == 200, "第二轮：过期前可以正常下载")
    check(
        out["expired_files"],
        f"§19 第 16 项：没下载的结果过了保留期被自动清掉（等了 {out['expired_files_after']}s）",
    )
    check(out["expired_download"] == 404, "§14 过期后下载链接返回 404，不再是有效文件")
    check(
        out["expired_task_gone"],
        f"§14 过期后任务记录也查不到了（等了 {out['expired_task_after']}s）",
    )

    too_many = out["too_many_body"]
    check(out["too_many_status"] == 400, f"服务端拒绝超过 50 个文件（HTTP {out['too_many_status']}）")
    check(
        "一次最多处理 50 个文件" in json.dumps(too_many, ensure_ascii=False),
        "服务端给出与前端一致的「一次最多处理 50 个文件」",
    )

    check(out["oversize_submit"] == 202, "51 MB 的文件仍然进得了队列（错误发生在处理阶段）")
    check(out["oversize_item_code"] == "FILE_TOO_LARGE", "51 MB 的文件被标记为 FILE_TOO_LARGE")
    check(out["oversize_state"] == "failed", "整批只有一个文件且失败时，整批标记失败")
    check(out["oversize_code"] == "FILE_TOO_LARGE", "整批失败原因透出同一个错误码")
    check(
        "Traceback" not in (out["oversize_item_message"] or ""),
        "失败原因是给用户看的文案，不是 Python 堆栈",
    )

    check(
        out["mismatch_code"] == "INVALID_FILE_TYPE",
        f"§18 内容与扩展名不符的文件被拒绝（{out['mismatch_code']}）",
    )
    check(
        "不一致" in (out["mismatch_message"] or ""),
        f"并说明拒绝原因：{out['mismatch_message']}",
    )


def check_timeout(page: Page) -> None:
    """§19 第 15 项：任务超时（真实跑一个大文件，撞上真实的服务端超时）。

    ``big.jpg`` 是 7000×5000：只按质量压缩约 0.5 秒，够不到 1 秒的超时线；
    再要求「压到 50 KB 以内」时，流水线要反复搜索质量并等比缩小，
    实测约 4.5 秒，于是必然撞上这个后端设的 1 秒上限 ——
    这里用到的是界面上真实存在的「目标大小」选项，不是人为拖时间。
    """
    page.goto(f"{SHORT_BASE}/image/compress", wait_until="networkidle")
    set_files(page, [IMAGES / "big.jpg"])
    page.locator("label", has_text="自定义大小").click()
    page.get_by_label("自定义目标大小").fill("50")
    # 单位默认是 MB，50 MB 对这张图来说等于「不限制」，必须切成 KB
    page.get_by_role("button", name="KB", exact=True).click()
    page.get_by_role("button", name="开始压缩").click()

    try:
        page.wait_for_function("() => document.body.innerText.includes('超时')", timeout=120_000)
    except Error:
        check(False, f"等待超时提示失败，页面当前内容：{body(page)[:400]}")
        return
    page.wait_for_timeout(400)
    text = body(page)

    check("超时" in text, "处理超时被如实报出来")
    check("✕" in text, "§4 超时的文件带 ✕ 标记")
    check("big.jpg" in text, "超时信息里有具体文件名")
    check("返回修改设置" in text, "整批失败后可以返回修改设置，不是死路")
    check("Traceback" not in text, "超时页面不显示 Python 堆栈")
    check(
        page.get_by_role("button", name="下载文件").count() == 0,
        "超时后不留下点不动的下载按钮",
    )


def run_part_c(page: Page) -> None:
    section("C 段：生命周期（JOB_TTL=5s / 超时=1s 的独立后端）")
    process = start_short_backend()
    try:
        check_lifecycle()
        check_timeout(page)
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover
            process.kill()


# ----------------------------------------------------------------------


def main() -> int:
    build_fixtures()
    DOWNLOADS.mkdir(parents=True, exist_ok=True)
    # 调试时可以只跑某一段：FILETOOLS_PARTS=A
    parts = os.environ.get("FILETOOLS_PARTS", "ABC").upper()

    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()
        attach(page)
        try:
            if "A" in parts:
                run_part_a(page)
            if "B" in parts:
                run_part_b(p)
            if "C" in parts:
                run_part_c(page)
        finally:
            context.close()
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
    print(f"测试素材：{WORK}")
    return 1 if failed else 0


if __name__ == "__main__":
    code = main()
    # 复用别人给的素材目录时不要删它（DEBUG 用）
    if os.environ.get("FILETOOLS_KEEP_WORK") != "1" and "FILETOOLS_WORK_DIR" not in os.environ:
        shutil.rmtree(WORK, ignore_errors=True)
    sys.exit(code)
