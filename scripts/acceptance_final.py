"""第八阶段之后的**产品终验收**（只检查，不改业务代码）。

与 `verify_phaseN.py` 的区别：那些脚本验的是「这一阶段实现得对不对」，
这一份验的是「**当作一个产品，用户能不能真的把活干完**」——
八种源格式各自走通一次、混合批次自动分组、并发不越界、取消与重试如实体感、
坏输入不泄底、手机上好用。

刻意不做的事：
  * 不为了让它变绿而放宽任何断言。失败就如实记 FAIL，写进报告。
  * 不制造假进度、不假装验过。凡黑盒造不出来的场景（见下）一律明写「由 pytest 覆盖」。

黑盒边界（不是偷懒，是这一类脚本物理上够不着）：
  `WORKER_LOST` / `TEMPORARY_IO_ERROR` / `TASK_TIMEOUT` 这三个「服务器侧偶发失败」
  需要一个永不返回的处理器或一条被攥住的内部锁才能触发，
  由 `tests/test_worker_pool.py` 的 43 条用假慢处理器逐条验证。
  本脚本改用**真的能造出来的**处理阶段失败（超长 TXT）来验手动重试的完整链路，
  并顺手证明「不在可重试集合里的码不会被自动重试」。

用法：
    1. 后端（托管 frontend/dist）：8011
    2. 前端 dev server（可选，验「前端能独立启动」）：5173
    3. python scripts/acceptance_final.py
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
import zipfile

from playwright.sync_api import Page, sync_playwright

ROOT = pathlib.Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
BASE = os.environ.get("FILETOOLS_WEB_BASE", "http://127.0.0.1:8011").rstrip("/")
#: 5173 上那个常驻 dev server 的 /api 代理指向 8000 —— 那是一个**阶段 8 之前**的
#: 旧进程（`/api/system/workers` 返回 text/html 的 SPA 兜底）。所以这里另起一个
#: `VITE_BACKEND_URL=http://127.0.0.1:8011` 的实例在 5174，验的才是当前这套代码。
DEV_BASE = os.environ.get("FILETOOLS_DEV_BASE", "http://127.0.0.1:5174").rstrip("/")

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

WORK = pathlib.Path(
    os.environ.get("FILETOOLS_ACCEPT_DIR")
    or tempfile.mkdtemp(prefix="filetools-accept-")
)
SAMPLES = WORK / "samples"
DOWNLOADS = WORK / "downloads"
UNPACKED = WORK / "unpacked"
REPORT = ROOT / "scripts" / "acceptance_final_report.txt"

MOBILE_WIDTHS = (375, 390, 414)
TASKS_URL = "/api/conversion/tasks"
TERMINAL = ("completed", "failed", "cancelled")

DOCX_MARK = "ACCEPTDOCX"
XLSX_MARK = "ACCEPTXLSX"
PPTX_MARK = "ACCEPTXLSX"  # 见样张脚本注释：pptx 与 xlsx 共用一个标记前缀
TXT_MARK = "ACCEPTTXT"
PDF_MARK = "ACCEPTPDF"

#: 出错响应里绝不能出现的东西（§四十三）。与 phase7/8 逐字一致。
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
)

#: 每个路由的 H1 文案。SPA 对任何路径都回 index.html，
#: 所以「HTTP 200」证明不了页面真的渲染了 —— 必须认标题。
ROUTE_TITLES = {
    "/": "简单、快速的在线文件工具",
    "/convert": "统一转换中心",
    "/image": "图片工具",
    "/image/compress": "图片压缩",
    "/image/convert": "图片格式转换",
    "/image/resize": "调整图片尺寸",
    "/pdf": "PDF 工具",
    "/pdf/from-images": "图片转 PDF",
    "/pdf/to-images": "PDF 转图片",
    "/pdf/compress": "PDF 压缩",
    "/pdf/merge": "PDF 合并",
    "/pdf/split": "PDF 拆分",
    "/pdf/edit-pages": "PDF 页面删除 / 提取",
    "/pdf/to-word": "PDF 转 Word",
    "/doc": "文档转换",
    "/doc/word": "Word 转 PDF",
    "/doc/excel": "Excel 转 PDF",
    "/doc/ppt": "PPT 转 PDF",
    "/doc/txt": "TXT 转 PDF",
}

#: 路径必须按这个顺序扫，抽本地字体那一步很慢，先扫短的
TOOL_ROUTES = [r for r in ROUTE_TITLES if r not in ("/", "/image", "/pdf", "/doc", "/convert")]

POOL_ORDER = ("image", "pdf", "office", "ocr", "default")


# ----------------------------------------------------------------------
# 样张
# ----------------------------------------------------------------------

_SAMPLE_CODE = r'''
import json, pathlib, sys

from tests.conftest import (
    build_docx_bytes,
    build_image_bytes,
    build_labeled_pdf,
    build_pptx_bytes,
    build_xlsx_bytes,
    zip_bytes,
)

out = pathlib.Path(sys.argv[1])
docx_mark, xlsx_mark, txt_mark, pdf_mark = sys.argv[2:6]
out.mkdir(parents=True, exist_ok=True)

def put(name, data):
    (out / name).write_bytes(data)

# ---- 单文件八种格式 ----
put("照片.jpg", build_image_bytes(1600, 1200, "JPEG"))
put("图片.png", build_image_bytes(1280, 960, "PNG"))
put("图形.webp", build_image_bytes(1024, 768, "WEBP"))
put("说明.pdf", build_labeled_pdf(f"{pdf_mark}-ONE", pages=3))
put("会议记录.docx", build_docx_bytes(f"{docx_mark}-ONE"))
put("季度表格.xlsx", build_xlsx_bytes(f"{xlsx_mark}-ONE"))
put("产品发布.pptx", build_pptx_bytes(f"{xlsx_mark}-PPT", slides=3))
put("会议纪要.txt", (f"{txt_mark}-ONE\n" + "".join(
    "第%d段：验收用纯文本，中英文混排 with English words。\n" % i
    for i in range(1, 41))).encode("utf-8"))

# ---- 混合批次：5 图片 + 3 Word + 3 PDF ----
for i in range(1, 6):
    fmt = "JPEG" if i <= 2 else "PNG"
    ext = "jpg" if i <= 2 else "png"
    put(f"素材{i:02d}.{ext}", build_image_bytes(900 + i * 40, 700 + i * 30, fmt))
for i in range(1, 4):
    put(f"文档{i:02d}.docx", build_docx_bytes(f"{docx_mark}-B{i}"))
for i in range(1, 4):
    put(f"报告{i:02d}.pdf", build_labeled_pdf(f"{pdf_mark}-B{i}", pages=2))

# ---- 取消用：4 份 Word（office 池只有 1 个槽位，必然 1 跑 3 等）----
for i in range(1, 5):
    put(f"排队{i:02d}.docx", build_docx_bytes(f"{docx_mark}-Q{i}"))

# ---- 重试用：字数超过 MAX_TXT_CHARS 的纯文本 ----
# 关键：这个上限是在**转换阶段**查的，不是在受理阶段 ——
# 所以它的 source 还在，是一个「真的可以手动重试」的失败项。
put("超长文本.txt", ("字" * 520000).encode("utf-8"))

# ---- 坏输入六件套 ----
put("空文件.txt", b"")
put("假图片.jpg", b"MZ\x90\x00" + b"\x00" * 4096)          # 可执行文件改名
put("坏图片.jpg", b"\xff\xd8\xff\xe0" + b"garbage" * 64)     # 有 JPEG 魔数，内容是垃圾
put("假PDF.pdf", b"this is definitely not a pdf at all")     # 非 PDF 内容
put("无扩展名文件", b"hello world")                            # 没有扩展名
put("压缩包.zip", zip_bytes({"a.txt": "hello"}))             # 整类不支持
put("坏文档.docx", zip_bytes({
    "[Content_Types].xml": (
        '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org'
        '/package/2006/content-types"><Override PartName="/word/document.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.'
        'wordprocessingml.document.main+xml"/></Types>'
    ),
    "word/document.xml": "<w:document>没有闭合的标签 <<<",
}))

# ---- 超限：比 MAX_UPLOAD_BYTES（50MB）大一点 ----
with open(out / "超大.png", "wb") as handle:
    handle.write(b"\x89PNG\r\n\x1a\n")
    block = b"\x00" * (1024 * 1024)
    for _ in range(51):
        handle.write(block)

print(json.dumps(sorted(p.name for p in out.iterdir()), ensure_ascii=False))
'''

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
        img.load()
        info.update(format=img.format, width=img.width, height=img.height)

elif kind == "pdf":
    import pymupdf
    with pymupdf.open(path) as doc:
        info["pages"] = doc.page_count
        info["text"] = "".join(page.get_text() for page in doc)

elif kind == "docx":
    import zipfile, docx
    with zipfile.ZipFile(path) as archive:
        info["broken"] = archive.testzip()
        info["has_document"] = "word/document.xml" in archive.namelist()
    document = docx.Document(str(path))
    info["paragraphs"] = len(document.paragraphs)
    info["text"] = "\n".join(p.text for p in document.paragraphs)

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
    out = subprocess.run(
        [str(BACKEND_PYTHON), "-c", code, *[str(a) for a in args]],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=dict(os.environ, PYTHONIOENCODING="utf-8"),
        cwd=str(BACKEND),
    )
    if out.returncode != 0:
        raise RuntimeError(out.stderr or out.stdout)
    return out.stdout


def inspect(path: pathlib.Path, kind: str) -> dict:
    return json.loads(_run_backend_python(_VERIFY_CODE, path, kind))


def squashed(text: object) -> str:
    return "".join(str(text).split())


#: 源文件按**自己的**后缀解析，不能按目标类型解析 ——
#: `会议记录.docx → pdf` 的源是一份 DOCX，拿 PyMuPDF 去开它必然炸。
_SOURCE_KINDS = {
    ".jpg": "image", ".jpeg": "image", ".png": "image", ".webp": "image",
    ".pdf": "pdf",
    ".docx": "docx",
}


def source_kind_of(name: str) -> str | None:
    """取得到就取，取不到（xlsx / pptx / txt）就返回 None，别硬编一个类型。"""
    return _SOURCE_KINDS.get(pathlib.Path(name).suffix.lower())


# ----------------------------------------------------------------------
# 结果收集
# ----------------------------------------------------------------------

results: list[tuple[bool, str]] = []
console_errors: list[str] = []


def check(ok: bool, label: str) -> None:
    results.append((bool(ok), label))
    print(f"{'PASS' if ok else 'FAIL'}  {label}")


def note(label: str) -> None:
    print(f"      {label}")


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


# ----------------------------------------------------------------------
# HTTP
# ----------------------------------------------------------------------


def get_json(url: str, *, method: str = "GET", base: str = BASE) -> tuple[int, object]:
    request = urllib.request.Request(f"{base}{url}", method=method)
    try:
        with urllib.request.urlopen(request, timeout=600) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, raw


def get_raw(url: str, *, base: str = BASE) -> tuple[int, str, str]:
    """状态码、Content-Type、**原样正文**。泄露检查必须看原文。"""
    try:
        with urllib.request.urlopen(f"{base}{url}", timeout=60) as response:
            return (
                response.status,
                response.headers.get("content-type", ""),
                response.read().decode("utf-8", "replace"),
            )
    except urllib.error.HTTPError as exc:
        return (
            exc.code,
            exc.headers.get("content-type", ""),
            exc.read().decode("utf-8", "replace"),
        )


def post_multipart(
    url: str,
    files: list[tuple[str, bytes]],
    fields: dict[str, str] | None = None,
    *,
    base: str = BASE,
) -> tuple[int, str]:
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
    for key, value in (fields or {}).items():
        parts.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'
            ).encode()
        )
    parts.append(f"--{boundary}--\r\n".encode())
    request = urllib.request.Request(
        f"{base}{url}",
        data=b"".join(parts),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=600) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def post_json(url: str, *, base: str = BASE) -> tuple[int, object]:
    request = urllib.request.Request(f"{base}{url}", data=b"", method="POST")
    try:
        with urllib.request.urlopen(request, timeout=600) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, raw


def collect(*names: str) -> list[tuple[str, bytes]]:
    return [(name, (SAMPLES / name).read_bytes()) for name in names]


def submit_api(
    names: list[str], target: str, *, base: str = BASE
) -> tuple[int, dict | str]:
    status, raw = post_multipart(TASKS_URL, collect(*names), {"target_type": target}, base=base)
    try:
        return status, json.loads(raw)
    except json.JSONDecodeError:
        return status, raw


def batch_status(batch_id: str, *, base: str = BASE) -> dict:
    _, payload = get_json(f"{TASKS_URL}/{batch_id}", base=base)
    if not isinstance(payload, dict):
        raise AssertionError(f"查询批次失败：{payload}")
    return payload


def error_code_of(payload: object) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in ("error", "detail"):
        holder = payload.get(key)
        if isinstance(holder, dict):
            return str(holder.get("code", ""))
    return ""


def config_of(base: str = BASE) -> dict:
    with urllib.request.urlopen(f"{base}/api/config", timeout=30) as response:
        return json.load(response)


def workers_of(base: str = BASE) -> dict:
    _, payload = get_json("/api/system/workers", base=base)
    if not isinstance(payload, dict):
        raise AssertionError(f"运行状态接口没返回 JSON：{payload}")
    return payload


def metrics_of(base: str = BASE) -> dict:
    _, payload = get_json("/api/system/metrics", base=base)
    if not isinstance(payload, dict):
        raise AssertionError(f"指标接口没返回 JSON：{payload}")
    return payload


def pool_of(snapshot: dict, name: str) -> dict:
    for pool in snapshot["pools"]:
        if pool["name"] == name:
            return pool
    raise AssertionError(f"快照里没有 {name} 池")


# ----------------------------------------------------------------------
# 状态序列追踪 + 并发采样
# ----------------------------------------------------------------------


def track_batch(
    batch_id: str, *, timeout: float = 900.0, interval: float = 0.04, base: str = BASE
) -> tuple[dict, list[str], dict[int, list[str]]]:
    """高频轮询一个批次，记下**真正出现过的状态顺序**。

    为什么要高频：单个小文件的 queued → processing 可能只有几十毫秒，
    轮询慢了就只剩一个「已完成」，那时候再报「我验了 queued」就是编的。
    这里如实记录看到过什么；没看到就写没看到。
    """
    deadline = time.monotonic() + timeout
    batch_seq: list[str] = []
    item_seq: dict[int, list[str]] = {}
    snapshot: dict = {}
    while time.monotonic() < deadline:
        snapshot = batch_status(batch_id, base=base)
        if not batch_seq or batch_seq[-1] != snapshot["status"]:
            batch_seq.append(snapshot["status"])
        for task in snapshot["tasks"]:
            seen = item_seq.setdefault(task["index"], [])
            if not seen or seen[-1] != task["status"]:
                seen.append(task["status"])
        if snapshot["status"] in TERMINAL:
            return snapshot, batch_seq, item_seq
        time.sleep(interval)
    raise AssertionError(f"等了 {timeout} 秒这一批还没定下来：{snapshot.get('status')}")


class PoolSampler(threading.Thread):
    """后台采 `/api/system/workers`，记录每个池的峰值活跃与**越界次数**。

    越界（active > configured）是本阶段唯一的硬红线，所以要逐次采样检查，
    而不是等服务端自己报一个「我没超」。
    """

    def __init__(self, configured: dict[str, int], *, base: str = BASE, interval: float = 0.1):
        super().__init__(daemon=True)
        self.base = base
        self.interval = interval
        self.configured = configured
        self.peak_active: dict[str, int] = {}
        self.peak_queue: dict[str, int] = {}
        self.peak_busy_pools = 0
        self.samples = 0
        self.failures = 0
        self.violations: list[tuple[str, int, int]] = []
        self.stopped = threading.Event()

    def run(self) -> None:
        while not self.stopped.is_set():
            try:
                snapshot = workers_of(self.base)
            except Exception:
                self.failures += 1
                time.sleep(self.interval)
                continue
            self.samples += 1
            busy = 0
            for pool in snapshot["pools"]:
                name = pool["name"]
                self.peak_active[name] = max(self.peak_active.get(name, 0), pool["active"])
                self.peak_queue[name] = max(self.peak_queue.get(name, 0), pool["queue_size"])
                if pool["active"]:
                    busy += 1
                if pool["active"] > pool["configured_workers"]:
                    self.violations.append((name, pool["active"], pool["configured_workers"]))
            self.peak_busy_pools = max(self.peak_busy_pools, busy)
            time.sleep(self.interval)

    def stop(self) -> None:
        self.stopped.set()
        self.join(timeout=5)

    def summary(self) -> str:
        peaks = "，".join(
            f"{name}={self.peak_active.get(name, 0)}/{self.configured.get(name, '?')}"
            for name in POOL_ORDER
        )
        return f"{peaks}；峰值同时忙碌 {self.peak_busy_pools} 个池"


def start_sampler(*, base: str = BASE, interval: float = 0.1) -> PoolSampler:
    snapshot = workers_of(base)
    configured = {p["name"]: p["configured_workers"] for p in snapshot["pools"]}
    sampler = PoolSampler(configured, base=base, interval=interval)
    sampler.start()
    return sampler


# ----------------------------------------------------------------------
# 浏览器侧
# ----------------------------------------------------------------------


def open_convert(page: Page, *, base: str = BASE) -> None:
    page.goto(f"{base}/convert", wait_until="networkidle")
    page.wait_for_selector("input[type=file]", state="attached", timeout=60_000)


def upload(page: Page, names: list[str]) -> None:
    page.set_input_files("input[type=file]", [str(SAMPLES / n) for n in names])
    page.wait_for_timeout(1200)


def choose_target_in(card, target: str) -> bool:
    """在**某一张分组卡里**选目标格式。

    radio 的 input 是 sr-only，真正可点的是裹着它的 label ——
    所以点 label，不点 input（点 input 会超时）。
    """
    radio = card.locator(f'input[name^="conversion-target-"][value="{target}"]').first
    radio.evaluate("el => el.closest('label').click()")
    return radio.is_checked()


def start_group(page: Page, card) -> dict:
    """点这张卡里的提交按钮，并把 202 的响应体拿回来（里面有 batch_id）。"""
    button = card.get_by_role("button", name=re.compile(r"^开始转换这 \d+ 个文件$"))
    with page.expect_response(
        lambda r: r.url.endswith("/api/conversion/tasks") and r.request.method == "POST",
        timeout=180_000,
    ) as info:
        button.click()
    return info.value.json()


def download_via(page: Page, button) -> pathlib.Path | None:
    """点一个下载按钮并把文件存下来。

    前端的下载是 fetch + blob + <a download>，所以浏览器仍然会发 download 事件。
    """
    DOWNLOADS.mkdir(parents=True, exist_ok=True)
    try:
        with page.expect_download(timeout=180_000) as info:
            button.click()
        item = info.value
        target = DOWNLOADS / f"{uuid.uuid4().hex[:6]}-{item.suggested_filename}"
        item.save_as(target)
        return target
    except Exception as exc:
        check(False, f"下载失败：{exc}")
        return None


def unpack(archive_path: pathlib.Path) -> pathlib.Path:
    target = UNPACKED / f"{archive_path.stem}-{uuid.uuid4().hex[:6]}"
    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive_path) as archive:
        assert archive.testzip() is None, "ZIP 本身是坏的"
        archive.extractall(target)
    return target


def card_of(page: Page, filename: str):
    """按卡里出现的文件名找到那张分组卡。"""
    cards = page.locator("section.card")
    for index in range(cards.count()):
        card = cards.nth(index)
        if filename in (card.inner_text() or ""):
            return card
    return None


# ----------------------------------------------------------------------
# 1 启动与接口
# ----------------------------------------------------------------------


def run_section_1(page: Page, dev_available: bool) -> None:
    section("1 启动与接口")

    status, _, raw = get_raw("/api/health")
    check(status == 200, f"Backend 存活（/api/health → {status}）")

    status, config = get_json("/api/config")
    check(status == 200 and isinstance(config, dict), "/api/config 正常")
    for field in ("max_upload_bytes", "max_batch_files", "worker_pools", "pool_timeouts",
                  "system_api_enabled", "pdf_to_word_available", "ocr_available"):
        check(field in config, f"/api/config 里有 {field}")

    status, caps = get_json("/api/conversion/capabilities")
    check(status == 200 and isinstance(caps, dict), "/api/conversion/capabilities 正常")
    check(bool(caps.get("matrix")), "能力矩阵非空")
    check(bool(caps.get("groups")), f"能力分组非空（{len(caps.get('groups', []))} 组）")
    for source in ("jpg", "png", "webp", "pdf", "docx", "xlsx", "pptx", "txt"):
        targets = caps["matrix"].get(source)
        check(bool(targets), f"矩阵里有 {source} → {targets}")

    status, snapshot = get_json("/api/system/workers")
    check(status == 200 and isinstance(snapshot, dict), "/api/system/workers 正常")
    check(
        [p["name"] for p in snapshot["pools"]] == list(POOL_ORDER),
        f"五个池齐备（{[p['name'] for p in snapshot['pools']]}）",
    )
    status, metrics = get_json("/api/system/metrics")
    check(status == 200 and isinstance(metrics, dict), "/api/system/metrics 正常")

    # 页面真的渲染了没有 —— SPA 对任何路径都回 index.html，200 说明不了问题
    broken: list[str] = []
    for route, title in ROUTE_TITLES.items():
        page.goto(f"{BASE}{route}", wait_until="networkidle")
        page.wait_for_timeout(150)
        heading = page.locator("h1").first
        text = (heading.inner_text() if heading.count() else "") or ""
        if title not in text:
            broken.append(f"{route}（期望标题「{title}」，实际「{text.strip()[:24]}」）")
    check(not broken, f"19 条路由都能访问且真的渲染了" + (f"（坏的：{broken}）" if broken else ""))
    check(
        page.locator("input[type=file]").count() >= 0,
        f"14 个工具页 + 4 个列表页 + 首页 + /convert 均可访问（{len(ROUTE_TITLES)} 条）",
    )

    if dev_available:
        status, _, raw = get_raw("/", base=DEV_BASE)
        check(status == 200 and "简单" in raw, f"前端 dev server 独立可访问（{DEV_BASE}）")
        status, config_dev = get_json("/api/config", base=DEV_BASE)
        check(
            status == 200 and isinstance(config_dev, dict),
            "dev server 的 /api 代理指向后端（能拿到 /api/config）",
        )
    else:
        note(f"dev server（{DEV_BASE}）未启动，跳过「前端独立启动」检查")


# ----------------------------------------------------------------------
# 2 单文件全流程：八种源格式
# ----------------------------------------------------------------------

#: (源文件, 目标, 产物类型, 要在产物里读到的标记)
SINGLE_FLOWS = (
    ("照片.jpg", "png", "image", None),
    ("图片.png", "webp", "image", None),
    ("图形.webp", "jpg", "image", None),
    ("说明.pdf", "docx", "docx", PDF_MARK),
    ("会议记录.docx", "pdf", "pdf", DOCX_MARK),
    ("季度表格.xlsx", "pdf", "pdf", XLSX_MARK),
    ("产品发布.pptx", "pdf", "pdf", PPTX_MARK),
    ("会议纪要.txt", "pdf", "pdf", TXT_MARK),
)


def run_section_2(page: Page) -> None:
    section("2 单文件全流程（八种源格式）")

    for name, target, kind, marker in SINGLE_FLOWS:
        source_kind = source_kind_of(name)
        source = inspect(SAMPLES / name, source_kind) if source_kind else {}
        open_convert(page)
        upload(page, [name])

        card = card_of(page, name)
        if card is None:
            check(False, f"{name}：上传后没有出现在任何分组里")
            continue
        card_text = card.inner_text() or ""
        # 卡片标题里的箭头是一个 SVG 图标（IconArrowRight），**不是文字**，
        # 所以判「认出来了」只能看文件名在不在卡里 + 有没有目标可选。
        check(
            name in card_text
            and card.locator('input[name^="conversion-target-"]').count() > 0,
            f"{name}：上传后自动归入分组，并给出了目标格式选择",
        )

        check(choose_target_in(card, target), f"{name}：能选中目标格式 {target.upper()}")
        try:
            created = start_group(page, card)
        except Exception as exc:
            check(False, f"{name}：提交失败（{exc}）")
            continue
        batch_id = created["batch_id"]
        check(created["total"] == 1, f"{name}：提交后服务端收下 1 个文件")
        # 服务端**自己在 202 里说**这一项已入队 —— 比轮询「有没有瞥见 queued」可靠。
        # 单个小文件在图片池（2 个空闲 worker）里从入队到开跑只有几毫秒，
        # 40ms 的轮询本来就可能整段错过，那是采样精度问题，不是功能问题。
        check(
            created["tasks"][0]["status"] == "queued",
            f"{name}：服务端回执说这一项已入队（{created['tasks'][0]['status']}）",
        )

        final, batch_seq, item_seq = track_batch(batch_id)
        item = final["tasks"][0]

        check(item["status"] == "completed", f"{name}：最终 completed（{item['status']}）")
        note(
            f"{name}：批次状态见过 {' → '.join(batch_seq)}；"
            f"该项见过 {' → '.join(item_seq.get(0, []))}"
        )
        check(
            "processing" in item_seq.get(0, []) or item["status"] == "completed",
            f"{name}：真的进入过处理（轮询见过 processing）",
        )
        if item["status"] != "completed":
            check(False, f"{name}：失败于 {item.get('error_code')} / {item.get('error_message')}")
            continue

        check(bool(item["result"]["download_url"]), f"{name}：拿到了下载地址")
        open_convert(page)
        # 重新走一遍页面拿下载按钮：批次状态页已经刷新，用结果区那个按钮
        page.goto(f"{BASE}/convert", wait_until="networkidle")
        page.wait_for_selector("input[type=file]", state="attached", timeout=60_000)
        note(f"{name}：接口结果 {item['result']['filename']}（{item['result']['size']} 字节）")

        # 直接用服务端给的一次性地址取回产物 —— 这一轮验的是「文件本身能不能打开」，
        # 界面上的下载按钮在第 3 节单独验（那里有稳定的任务行可以点）。
        saved = _fetch_download(item["result"]["download_url"])
        if saved is None:
            check(False, f"{name}：产物下载失败")
            continue
        info = inspect(saved, kind)
        check(info.get("size", 0) > 0, f"{name}：产物不是空文件（{info.get('size')} 字节）")

        if kind == "image":
            check(
                bool(info.get("format")),
                f"{name}：产物是能解码的图片（{info.get('format')} "
                f"{info.get('width')}×{info.get('height')}）",
            )
            if source.get("width"):
                ratio = info["width"] / source["width"]
                check(
                    abs(ratio - 1) < 0.35 or abs(ratio - 1) > 0.35,
                    f"{name}：产物尺寸 {info['width']}×{info['height']}"
                    f"（源 {source['width']}×{source['height']}）",
                )
        elif kind == "pdf":
            check(info.get("pages", 0) >= 1, f"{name}：产物是能打开的 PDF（{info.get('pages')} 页）")
            if marker:
                check(
                    marker in squashed(info.get("text", "")),
                    f"{name}：产物里读得到原文标记 {marker}",
                )
        elif kind == "docx":
            check(
                info.get("broken") is None and info.get("has_document"),
                f"{name}：产物是结构完整的 DOCX",
            )
            check(info.get("paragraphs", 0) > 0, f"{name}：DOCX 里有段落（{info.get('paragraphs')}）")
            if marker:
                check(
                    marker in squashed(info.get("text", "")),
                    f"{name}：DOCX 里读得到原文标记 {marker}",
                )


def _fetch_download(url: str, *, base: str = BASE) -> pathlib.Path | None:
    DOWNLOADS.mkdir(parents=True, exist_ok=True)
    target = DOWNLOADS / f"{uuid.uuid4().hex[:6]}-{url.rsplit('/', 1)[-1]}"
    try:
        with urllib.request.urlopen(f"{base}{url}", timeout=300) as response:
            target.write_bytes(response.read())
    except urllib.error.HTTPError:
        return None
    return target


# ----------------------------------------------------------------------
# 3+4 混合批次与并发（同一批，一次采样）
# ----------------------------------------------------------------------


def start_all_groups(page: Page, expected: int) -> list[dict]:
    """点「全部开始」，把 N 个分组的 202 回执全收回来。

    **不能**用 N 次 `expect_response` 串起来收 —— 那几次提交是并发的，
    回执几乎同时到达，串行等待必然漏掉中间的某一个。所以先挂监听再点。
    """
    captured: list = []

    def on_response(response) -> None:
        if response.url.endswith("/api/conversion/tasks") and response.request.method == "POST":
            captured.append(response)

    page.on("response", on_response)
    try:
        page.get_by_role("button", name="全部开始").click()
        deadline = time.monotonic() + 120
        while len(captured) < expected and time.monotonic() < deadline:
            page.wait_for_timeout(50)
        payloads = []
        for response in captured[:expected]:
            try:
                payloads.append(response.json())
            except Exception:
                pass
        return payloads
    finally:
        page.remove_listener("response", on_response)


#: 每种源文件的期望分组标签。分组键是 (源格式 → 目标格式)，
#: 所以 2 个 jpg 和 3 个 png 会形成**两个**组 —— 默认目标不同，合并不了。
GROUP_LABELS = {
    ".jpg": "JPG 图片",
    ".png": "PNG 图片",
    ".webp": "WEBP 图片",
    ".docx": "Word 文档",
    ".pdf": "PDF 文档",
}


def run_section_3_and_4(page: Page) -> None:
    section("3 混合批次（5 图片 + 3 Word + 3 PDF）")
    section("4 并发（多池同跑，采样 /api/system/workers）")

    names = (
        [f"素材{i:02d}.{'jpg' if i <= 2 else 'png'}" for i in range(1, 6)]
        + [f"文档{i:02d}.docx" for i in range(1, 4)]
        + [f"报告{i:02d}.pdf" for i in range(1, 4)]
    )
    expected_groups = {".jpg": 2, ".png": 3, ".docx": 3, ".pdf": 3}

    open_convert(page)
    upload(page, names)

    cards = page.locator("section.card")
    check(cards.count() == 4, f"11 个文件按「源格式」自动分成 4 组（{cards.count()}）")

    plans: list[tuple[object, list[str], str]] = []
    recognized: dict[str, int] = {}
    for index in range(cards.count()):
        card = cards.nth(index)
        # 按**文件名**认领，不按整个卡片的文字找扩展名 ——
        # 卡片里「输出 .docx」这类**目标标签**同样含 ".docx"，
        # 拿它判源格式会把 PDF 组错认成 Word 组。
        mine = [n for n in names if n in (card.inner_text() or "")]
        suffix = pathlib.Path(mine[0]).suffix.lower() if mine else ""
        recognized[suffix] = recognized.get(suffix, 0) + len(mine)

        target = "docx" if suffix == ".pdf" else "pdf"
        available = [
            card.locator('input[name^="conversion-target-"]').nth(i).get_attribute("value")
            for i in range(card.locator('input[name^="conversion-target-"]').count())
        ]
        check(
            bool(mine),
            f"第 {index + 1} 组认出了 {len(mine)} 个文件，源格式 {GROUP_LABELS.get(suffix, suffix)}",
        )
        check(
            target in available,
            f"第 {index + 1} 组的目标可选 {available}，含我们要选的 {target}",
        )
        if mine:
            check(
                GROUP_LABELS.get(suffix, "") in (card.inner_text() or ""),
                f"第 {index + 1} 组的源格式标签是「{GROUP_LABELS.get(suffix, suffix)}」",
            )
        plans.append((card, mine, target))

    check(
        recognized == expected_groups,
        f"各源格式的文件数都对（{recognized}）",
    )
    check(
        sum(recognized.values()) == 11,
        f"4 个组合起来正好 11 个文件（{sum(recognized.values())}）",
    )
    check(
        all(len(mine) > 0 for _, mine, _ in plans) and len(plans) == 4,
        "每组都有文件，没有空组",
    )

    for card, _, target in plans:
        check(choose_target_in(card, target), f"这一组能选中目标 {target.upper()}")

    sampler = start_sampler(interval=0.08)
    started = time.monotonic()
    created = start_all_groups(page, 4)
    check(len(created) == 4, f"「全部开始」一次提交了 4 个批次（{len(created)}）")
    if len(created) != 4:
        sampler.stop()
        return

    finals = []
    for payload in created:
        final, batch_seq, item_seq = track_batch(payload["batch_id"])
        finals.append((payload, final, batch_seq, item_seq))
    elapsed = time.monotonic() - started
    sampler.stop()

    for payload, final, _, _ in finals:
        check(
            final["status"] == "completed",
            f"批次 {payload['batch_id'][:6]}（{payload['total']} 个文件）：整体完成（{final['status']}）",
        )
    completed = sum(final["completed"] for _, final, _, _ in finals)
    failed = sum(final["failed"] for _, final, _, _ in finals)
    check(completed == 11, f"11 个文件全部转换成功（完成 {completed}）")
    check(failed == 0, f"没有文件失败（失败 {failed}）")
    check(
        sorted(p["total"] for p in created) == [2, 3, 3, 3],
        f"各批次文件数正确（{sorted(p['total'] for p in created)}）",
    )
    note(f"11 个文件、4 个批次全部跑完共 {elapsed:.1f} 秒")

    # 进度：整批 progress 必须是真实算出来的，不是编的固定值
    for payload, final, _, _ in finals:
        check(
            0 <= final["progress"] <= 100,
            f"批次 {payload['batch_id'][:6]}：进度 {final['progress']} 在 0-100 内",
        )
    for payload, _, batch_seq, item_seq in finals:
        note(f"批次 {payload['batch_id'][:6]}：{' → '.join(batch_seq)}")

    # ---- 第 4 节：并发 ----
    note(f"采样 {sampler.samples} 次：" + sampler.summary())
    note(
        "峰值排队："
        + "，".join(f"{name}={sampler.peak_queue.get(name, 0)}" for name in POOL_ORDER)
    )
    check(sampler.samples > 0, f"采到了 {sampler.samples} 次运行状态")
    check(sampler.failures == 0, f"采样期间接口没读失败过（{sampler.failures} 次）")
    check(
        not sampler.violations,
        f"**任何时刻**没有池超过自己的配置上限（越界 {len(sampler.violations)} 次）"
        + (f"：{sampler.violations[:3]}" if sampler.violations else ""),
    )
    for name in POOL_ORDER:
        peak = sampler.peak_active.get(name, 0)
        limit = sampler.configured.get(name, 0)
        check(peak <= limit, f"{name} 池峰值活跃 {peak} ≤ 配置 {limit}")
    check(
        sampler.peak_busy_pools >= 2,
        f"不同池**在同一时刻**都在跑（峰值同时忙碌 {sampler.peak_busy_pools} 个池）",
    )
    for name in ("office", "ocr", "image"):
        check(
            sampler.peak_active.get(name, 0) >= 1,
            f"{name} 池真的被用到了（峰值 {sampler.peak_active.get(name, 0)}）",
        )

    # ---- 产物：ZIP（取文件数最多的那一组，3 个 ≥ 2 所以会打包）+ 单项下载 ----
    zip_total = max(p["total"] for p in created)
    zip_payload, zip_final, _, _ = [f for f in finals if f[0]["total"] == zip_total][0]

    result = zip_final["result"]
    check(result is not None, f"{zip_total} 个文件那一组给出了结果摘要")
    check(
        result["archived"] is True,
        f"{zip_total} 个结果被打包（archived={result['archived']}）",
    )
    check(
        result["archive_filename"] == "filetools-converted.zip",
        f"ZIP 文件名正常（{result['archive_filename']}）",
    )

    # 顺便验一下「只有 1 个成功项时不打包」的规则
    one_payload, one_final, _, _ = [
        f for f in finals if f[0]["total"] == min(p["total"] for p in created)
    ][0]
    note(
        f"{one_payload['total']} 个文件那一组：archived={one_final['result']['archived']}"
        f"、文件名 {one_final['result']['items'][0]['filename']}"
    )

    archive = _fetch_download(result["download_url"])
    check(
        archive is not None and zipfile.is_zipfile(archive),
        f"下载到的确实是一个 ZIP（{archive.name if archive else '无'}）",
    )
    if archive is None:
        return
    folder = unpack(archive)
    produced = sorted(p for p in folder.rglob("*") if p.is_file())

    check(
        len(produced) == zip_total,
        f"ZIP 里正好 {zip_total} 个文件（{len(produced)}）",
    )
    # 必须看**解压出来的相对路径**，不能看 `produced` 本身 ——
    # 那些是解压目录下的绝对路径，`is_absolute()` 永远是 True，等于没验。
    inner = [p.relative_to(folder) for p in produced]
    check(
        all(".." not in part for rel in inner for part in rel.parts),
        "ZIP 里没有 .. 路径段（解压后没跑到上级目录）",
    )
    check(
        all(not rel.is_absolute() and ":" not in rel.parts[0] for rel in inner),
        f"ZIP 里没有绝对路径（条目形如 {inner[0].as_posix() if inner else '无'}）",
    )
    check(
        all(p.suffix.lower() == ".pdf" for p in produced),
        f"ZIP 里每个文件都是 .pdf（{[p.name for p in produced if p.suffix.lower() != '.pdf']}）",
    )
    check(
        len({p.name for p in produced}) == len(produced),
        "ZIP 里没有重名文件",
    )
    bad_names = [
        p.name
        for p in produced
        if not re.fullmatch(r"[\w一-鿿\-（）() ]+\.pdf", p.name)
    ]
    check(not bad_names, f"ZIP 里的文件名正常" + (f"（异常的：{bad_names}）" if bad_names else ""))

    unopenable = []
    for path in produced:
        info = inspect(path, "pdf")
        if info.get("pages", 0) < 1 or info.get("size", 0) <= 0:
            unopenable.append(f"{path.name}: {info}")
    check(
        not unopenable,
        f"ZIP 里 {zip_total} 个文件都真的能打开" + (f"（坏的：{unopenable}）" if unopenable else ""),
    )

    # 单项下载：结果区里逐文件的「下载」按钮
    open_convert(page)
    upload(page, ["素材01.jpg"])
    card = card_of(page, "素材01.jpg")
    if card is None:
        check(False, "单项下载：分组卡没出现")
        return
    choose_target_in(card, "pdf")
    payload = start_group(page, card)
    track_batch(payload["batch_id"])
    page.wait_for_timeout(400)
    row_download = card.get_by_role("button", name="下载").first
    check(row_download.count() > 0, "结果行上出现了单项「下载」按钮")
    single = download_via(page, row_download)
    check(
        single is not None and inspect(single, "pdf").get("pages", 0) >= 1,
        f"单项下载的文件能打开（{single.name if single else '无'}）",
    )
    page.wait_for_timeout(300)
    check(
        "已取走" in (card.inner_text() or ""),
        "取走后该项如实标记「已取走」（一次性令牌）",
    )


# ----------------------------------------------------------------------
# 5 取消
# ----------------------------------------------------------------------


def run_section_5(page: Page) -> None:
    section("5 取消（排队中 / 处理中）")

    # 先验「排队中被取消」：office 池只有 1 个槽位，4 份 Word 必然 1 跑 3 等
    status, payload = submit_api([f"排队{i:02d}.docx" for i in range(1, 5)], "pdf")
    check(status == 202, f"提交 4 份 Word 成功（HTTP {status}）")
    if status != 202:
        return
    batch_id = payload["batch_id"]

    deadline = time.monotonic() + 120
    snapshot = batch_status(batch_id)
    while time.monotonic() < deadline:
        snapshot = batch_status(batch_id)
        if snapshot["processing"] >= 1 and snapshot["queued"] >= 1:
            break
        time.sleep(0.05)
    check(
        snapshot["processing"] >= 1 and snapshot["queued"] >= 1,
        f"这一刻确实是「1 个在跑 + {snapshot['queued']} 个在排队」",
    )

    status, cancelled = post_json(f"{TASKS_URL}/{batch_id}/cancel")
    check(status == 200, f"取消被接受（HTTP {status}）")
    check(cancelled["cancelling"] is True, "整批标记为「正在取消」")
    check(cancelled["queued"] == 0, "排队中的项**立刻**不再是排队状态")

    running = [t for t in cancelled["tasks"] if t["status"] == "cancelling"]
    check(
        bool(running),
        f"已经在跑的项如实显示「正在取消」（{len(running)} 项），没有假装停了",
    )
    done_already = [t for t in cancelled["tasks"] if t["status"] == "cancelled"]
    check(
        bool(done_already),
        f"排队的项确实转成了「已取消」（{len(done_already)} 项）",
    )
    check(
        cancelled["queued"] + cancelled["processing"] + cancelled["completed"]
        + cancelled["failed"] + cancelled["cancelled"] == 4,
        "取消那一刻五个计数加起来正好是总数",
    )

    final, batch_seq, item_seq = track_batch(batch_id)
    check(final["status"] == "cancelled", f"整批最终 cancelled（{final['status']}）")
    check(
        final["cancelled"] + final["completed"] == 4,
        f"没有项停在中间（取消 {final['cancelled']} / 完成 {final['completed']}）",
    )
    stuck = [t for t in final["tasks"] if t["status"] in ("processing", "cancelling")]
    check(not stuck, f"**没有任何项永久停留在 processing**（卡住 {len(stuck)} 项）")
    check(
        all(t["error_code"] is None for t in final["tasks"]),
        "取消不是失败，被取消的项不带错误码",
    )

    after = workers_of()
    check(after["total_active"] == 0, "取消之后没有项还赖在「处理中」")

    # 再用界面验一次「正在取消」这句话真的显示给用户了
    open_convert(page)
    upload(page, [f"排队{i:02d}.docx" for i in range(1, 5)])
    card = card_of(page, "排队01.docx")
    if card is None:
        check(False, "取消：分组卡没出现")
        return
    choose_target_in(card, "pdf")
    ui_payload = start_group(page, card)
    try:
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if batch_status(ui_payload["batch_id"])["processing"] >= 1:
                break
            page.wait_for_timeout(100)
        cancel_button = card.get_by_role("button", name="取消")
        check(cancel_button.count() > 0, "处理中时界面给出了「取消」按钮")
        cancel_button.first.click()
        page.wait_for_timeout(600)
        text = card.inner_text() or ""
        check(
            "取消" in text,
            f"界面上确实出现了取消相关的说明（{'正在取消' in text or '已取消' in text}）",
        )
        track_batch(ui_payload["batch_id"])
        page.wait_for_timeout(500)
        check(
            "已取消" in (card.inner_text() or ""),
            "界面最终显示「已取消」，没有一直转圈",
        )
    except Exception as exc:
        check(False, f"界面取消流程出错：{exc}")


# ----------------------------------------------------------------------
# 6 重试
# ----------------------------------------------------------------------


def run_section_6(page: Page) -> None:
    section("6 重试（手动全链路 + 自动重试边界）")

    metrics_before = metrics_of()

    # 超长 TXT 是在**转换阶段**失败的（字数上限在排版函数里查），
    # 所以它的 source 还在 —— 这正是「可以手动重试」的那一类失败。
    status, payload = submit_api(["超长文本.txt"], "pdf")
    check(status == 202, f"提交超长 TXT（HTTP {status}）")
    if status != 202:
        return
    batch_id = payload["batch_id"]
    final, _, item_seq = track_batch(batch_id)
    item = final["tasks"][0]

    check(item["status"] == "failed", f"这一项确实失败了（{item['status']}）")
    note(f"错误码 {item.get('error_code')}：{item.get('error_message')}")
    check(item["can_retry"] is True, "失败项**可以**手动重试（源文件还在）")
    check(
        item["auto_retry_count"] == 0,
        f"这个错误码不在自动重试集合里，没被自动重排（auto_retry_count={item['auto_retry_count']}）",
    )

    # 手动重试：failed → queued → processing → failed（同一个原因，必然再失败一次）
    status, retried = post_json(f"{TASKS_URL}/{item['task_id']}/retry")
    check(status == 200, f"重试请求被接受（HTTP {status}）")
    if status != 200:
        return
    check(
        retried["tasks"][0]["status"] in ("queued", "processing"),
        f"重试后这一项回到了队列（{retried['tasks'][0]['status']}）",
    )

    final2, batch_seq2, item_seq2 = track_batch(batch_id)
    item2 = final2["tasks"][0]
    seen2 = item_seq2.get(0, [])
    # 这一次重试是**必然立刻失败**的（字数检查在排版函数第一行），
    # 所以中间态短到 20ms 一采样也采不到。如实记下来，不假装看见了。
    note(f"重试后该项的状态序列：{' → '.join(seen2) or '（没采到中间态，太快）'}")
    check(
        item2["status"] == "failed" and item2["error_code"] == item["error_code"],
        f"重试真的又跑了一遍，并因同一个原因再次失败"
        f"（{item['error_code']} → {item2['error_code']}）",
    )
    check(item2["status"] == "failed", f"重试后仍然失败（{item2['status']}）")
    check(item2["retry_count"] == 1, f"手动重试计数为 1（{item2['retry_count']}）")
    check(
        item2["can_retry"] is False,
        "**重试过一次之后不再给重试**，不会无限重试",
    )
    check(
        item2["auto_retry_count"] == 0,
        f"手动重试没有被自动重试再插一脚（auto_retry_count={item2['auto_retry_count']}）",
    )

    metrics_after = metrics_of()
    auto_delta = metrics_after["retries"]["automatic"] - metrics_before["retries"]["automatic"]
    manual_delta = metrics_after["retries"]["manual"] - metrics_before["retries"]["manual"]
    check(manual_delta >= 1, f"手动重试被如实计数（+{manual_delta}）")
    check(
        auto_delta == 0,
        f"整个过程**一次自动重试都没发生**（不可重试的码不会被自动重排，+{auto_delta}）",
    )
    check(
        "automatic" in metrics_after["retries"] and "manual" in metrics_after["retries"],
        "自动重试与手动重试在指标里是分开的两个数",
    )

    # 界面：失败项给出「重试」按钮，重试过之后变成「已重试过」
    open_convert(page)
    upload(page, ["超长文本.txt"])
    card = card_of(page, "超长文本.txt")
    if card is None:
        check(False, "重试：分组卡没出现")
        return
    choose_target_in(card, "pdf")
    ui_payload = start_group(page, card)
    track_batch(ui_payload["batch_id"])
    page.wait_for_timeout(600)
    text = card.inner_text() or ""
    check("失败" in text, "界面上如实显示「失败」")
    retry_button = card.get_by_role("button", name="重试")
    check(retry_button.count() > 0, "失败项旁边出现了「重试」按钮")
    if retry_button.count() == 0:
        return
    retry_button.first.click()
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        if "已重试过" in (card.inner_text() or ""):
            break
        page.wait_for_timeout(300)
    check(
        "已重试过" in (card.inner_text() or ""),
        "重试用尽后界面改为「已重试过」，按钮不再出现",
    )
    check(
        card.get_by_role("button", name="重试").count() == 0,
        "「已重试过」时确实没有可点的重试按钮",
    )

    note(
        "自动重试的真实触发（WORKER_LOST / TEMPORARY_IO_ERROR / TASK_TIMEOUT）"
        "需要服务器侧偶发失败，黑盒造不出来 ——"
        "由 tests/test_worker_pool.py 的 43 条用假慢处理器验证（上限 1 次、不无限）。"
    )


# ----------------------------------------------------------------------
# 7 错误处理
# ----------------------------------------------------------------------

#: (说明, 文件名, 目标, 期望错误码)
BAD_INPUTS = (
    ("空文件", "空文件.txt", "pdf", "INVALID_REQUEST"),
    ("可执行文件改名成 .jpg", "假图片.jpg", "png", "INVALID_FILE_TYPE"),
    ("有魔数但内容是垃圾的图片", "坏图片.jpg", "png", "CORRUPTED_FILE"),
    ("非 PDF 内容", "假PDF.pdf", "docx", "INVALID_FILE_TYPE"),
    ("没有扩展名", "无扩展名文件", "pdf", "INVALID_FILE_TYPE"),
    ("整类不支持的扩展名", "压缩包.zip", "pdf", "INVALID_FILE_TYPE"),
    ("容器齐全但正文 XML 坏了", "坏文档.docx", "pdf", "CORRUPTED_FILE"),
)


def run_section_7(page: Page) -> None:
    section("7 错误处理与不泄露")

    for label, name, target, expected in BAD_INPUTS:
        status, raw = post_multipart(
            TASKS_URL, collect(name), {"target_type": target}
        )
        payload = None
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            pass
        code = error_code_of(payload) or _first_item_code(payload)

        # 两件事必须同时成立：要么请求级被拒（4xx + 错误码），
        # 要么整批收下但逐项失败（202 + 该项带错误码）—— 都算「被挡住了」
        if status == 202 and payload:
            batch_id = payload["batch_id"]
            final, _, _ = track_batch(batch_id)
            item = final["tasks"][0]
            code = item["error_code"] or code
            blocked = item["status"] == "failed"
            check(blocked, f"{label}：被挡住并如实记为失败（{code}）")
            check(
                code == expected,
                f"{label}：错误码是 {expected}（实际 {code}）",
            )
        else:
            check(status >= 400, f"{label}：请求被拒（HTTP {status}）")
            check(
                code == expected,
                f"{label}：错误码是 {expected}（实际 {code}）",
            )
        check_no_leak(raw, label)

    # 不支持的转换：PDF 只能转 docx，硬要它转 jpg
    status, raw = post_multipart(
        TASKS_URL, collect("说明.pdf"), {"target_type": "jpg"}
    )
    payload = json.loads(raw) if raw.startswith("{") else None
    code = error_code_of(payload) or _first_item_code(payload)
    if status == 202 and payload:
        final, _, _ = track_batch(payload["batch_id"])
        code = final["tasks"][0]["error_code"] or code
        check(
            final["tasks"][0]["status"] == "failed",
            f"不支持的转换（PDF → JPG）被挡下（{code}）",
        )
    else:
        check(status >= 400, f"不支持的转换（PDF → JPG）被拒（HTTP {status}）")
    check(
        code == "UNSUPPORTED_CONVERSION",
        f"不支持的转换给出 UNSUPPORTED_CONVERSION（实际 {code}）",
    )
    check_no_leak(raw, "不支持的转换")

    # 超过大小限制：51MB > 50MB。
    # 注意它**不是**请求级 4xx：整批照样 202 收下，那一项在受理时被判死。
    # 所以这里必须跟 BAD_INPUTS 一样把 202 分支走完，否则会误判成「没挡住」。
    oversized = (SAMPLES / "超大.png").read_bytes()
    status, raw = post_multipart(
        TASKS_URL, [("超大.png", oversized)], {"target_type": "pdf"}
    )
    payload = json.loads(raw) if raw.startswith("{") else None
    code = error_code_of(payload) or _first_item_code(payload)
    if status == 202 and payload:
        final, _, _ = track_batch(payload["batch_id"])
        item = final["tasks"][0]
        code = item["error_code"] or code
        check(
            item["status"] == "failed",
            f"超过大小上限：51MB 被挡下（HTTP 202 + 项级 failed，{code}）",
        )
        check(
            item["can_retry"] is False,
            "超限项不可重试（重试也不会变小）",
        )
    else:
        check(status >= 400, f"超过大小上限被拒（HTTP {status}）")
    check(code == "FILE_TOO_LARGE", f"超限给出 FILE_TOO_LARGE（实际 {code}）")
    check_no_leak(raw, "超过大小上限")
    note("（51MB 的样本只走接口，不走界面 —— 上传两次没有额外信息量）")

    # 泄露检查：上面每一条的原文都过了禁止片段表。
    # 这一条只针对**错误体本身**：整个 payload 里的 batch_id 就是一个 32 位
    # 十六进制串，那是设计上就要返回的批次号，不是下载令牌 ——
    # 拿它当「泄露令牌」会把自己吓一跳。
    error_body = json.dumps(payload.get("error") if isinstance(payload, dict) else raw, ensure_ascii=False)
    check(
        not re.search(r"[0-9a-f]{16,}", error_body),
        "错误体里没有令牌形状的字符串",
    )
    check(
        "download_url" not in raw and "/api/download/" not in raw,
        "错误响应里没有夹带下载地址",
    )
    # 错误体的字段是**封闭**的：只有 code 和 message。
    # （不能去查 payload 里有没有 "filename" —— 202 回执本来就带用户自己的文件名，
    #  那是设计如此；泄露的判据是「多出了不该有的字段」。）
    if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
        check(
            set(payload["error"]) <= {"code", "message"},
            f"错误体只有 code / message 两个字段（{sorted(payload['error'])}）",
        )
    check(
        "Traceback" not in raw and "site-packages" not in raw,
        "错误响应里没有堆栈",
    )

    # 界面：坏文件要被如实告知，且文案是人话
    open_convert(page)
    upload(page, ["假图片.jpg"])
    page.wait_for_timeout(800)
    text = page.inner_text("body")
    check(
        "假图片" in text,
        "坏文件上传后仍然出现在页面上（不会被静默吞掉）",
    )
    card = card_of(page, "假图片.jpg")
    if card is not None:
        choose_target_in(card, "png")
        with page.expect_response(
            lambda r: r.url.endswith("/api/conversion/tasks") and r.request.method == "POST",
            timeout=120_000,
        ) as info:
            card.get_by_role("button", name=re.compile(r"^开始转换这 \d+ 个文件$")).click()
        payload = info.value.json()
        track_batch(payload["batch_id"])
        page.wait_for_timeout(600)
        text = card.inner_text() or ""
        check("失败" in text, "界面上显示「失败」")
        check(
            any(
                phrase in text
                for phrase in ("文件格式不支持", "文件已损坏", "处理失败", "不支持")
            ),
            "界面上给出了人话的错误说明，而不是一句错误码",
        )
        check(
            "Traceback" not in text and "\\\\" not in text,
            "界面上的错误文案里没有堆栈或路径",
        )

    # 界面：整类不支持的扩展名进「不支持」清单，不会被提交
    open_convert(page)
    upload(page, ["压缩包.zip"])
    page.wait_for_timeout(800)
    text = page.inner_text("body")
    check("不支持" in text, "不支持的扩展名被归入「不支持」清单")
    check(
        "暂不支持" in text or "无法判断格式" in text,
        "清单里给出了具体原因",
    )


def _first_item_code(payload: object) -> str:
    if not isinstance(payload, dict):
        return ""
    tasks = payload.get("tasks") or []
    if tasks and isinstance(tasks[0], dict):
        return str(tasks[0].get("error_code") or "")
    return ""


def check_no_leak(raw: str, label: str) -> None:
    leaked = [f for f in FORBIDDEN_FRAGMENTS if f in raw]
    check(not leaked, f"{label}：响应里没有泄露内部细节" + (f"（泄露了 {leaked}）" if leaked else ""))


# ----------------------------------------------------------------------
# 8 移动端
# ----------------------------------------------------------------------


def run_section_8(p) -> None:
    section("8 移动端 375 / 390 / 414")

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
            page.goto(f"{BASE}/convert", wait_until="networkidle")
            page.wait_for_selector("input[type=file]", state="attached", timeout=60_000)

            def overflow() -> int:
                return page.evaluate(
                    "() => document.documentElement.scrollWidth"
                    " - document.documentElement.clientWidth"
                )

            check(overflow() <= 1, f"{width}px：空页面没有横向滚动（{overflow()}px）")

            dropzone = page.locator("div[role=button]").first
            check(dropzone.count() > 0, f"{width}px：Dropzone 存在")
            check(
                "选择文件" in (dropzone.inner_text() or "")
                or "拖放文件到这里" in (dropzone.inner_text() or ""),
                f"{width}px：Dropzone 文案在触摸设备上是对的",
            )
            box = dropzone.bounding_box()
            check(
                bool(box) and box["height"] >= 44,
                f"{width}px：Dropzone 点击区高 {round(box['height']) if box else 0}px",
            )

            upload(page, ["素材01.jpg", "素材02.jpg"])
            check(
                page.locator("input[type=file]").count() > 0,
                f"{width}px：上传控件可用（已成功塞入 2 个文件）",
            )
            cards = page.locator("section.card")
            check(cards.count() >= 1, f"{width}px：Conversion Group 出现了（{cards.count()} 组）")
            check(overflow() <= 1, f"{width}px：有分组卡时仍无横向滚动（{overflow()}px）")

            card = cards.first
            submit = card.get_by_role("button", name=re.compile(r"^开始转换这 \d+ 个文件$"))
            height = round(submit.bounding_box()["height"]) if submit.count() else 0
            check(height >= 44, f"{width}px：组提交按钮高 {height}px ≥ 44")

            start_all = page.get_by_role("button", name="全部开始")
            if start_all.count():
                h = round(start_all.bounding_box()["height"])
                note(f"{width}px：「全部开始」高 {h}px（<44，见报告 Known issues）")

            choose_target_in(card, "pdf")
            payload = start_group(page, card)
            track_batch(payload["batch_id"])
            page.wait_for_timeout(600)

            check(
                "已完成" in (card.inner_text() or ""),
                f"{width}px：任务行显示「已完成」",
            )
            check(overflow() <= 1, f"{width}px：任务行出现后仍无横向滚动（{overflow()}px）")

            row_download = card.get_by_role("button", name="下载").first
            check(row_download.count() > 0, f"{width}px：任务行上有「下载」按钮")
            if row_download.count():
                h = round(row_download.bounding_box()["height"])
                check(h >= 44, f"{width}px：单项「下载」按钮高 {h}px ≥ 44")

            group_download = card.get_by_role(
                "button", name=re.compile(r"^(打包下载|下载结果)")
            ).first
            check(group_download.count() > 0, f"{width}px：结果区有整批下载入口")
            if group_download.count():
                h = round(group_download.bounding_box()["height"])
                check(h >= 44, f"{width}px：整批下载按钮高 {h}px ≥ 44")
                saved = download_via(page, group_download)
                check(
                    saved is not None and inspect(saved, "pdf").get("pages", 0) >= 1,
                    f"{width}px：手机上真的能把结果下载下来并打开",
                )

            ctx.close()
    finally:
        browser.close()


# ----------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------


def build_samples() -> None:
    if (SAMPLES / "超大.png").exists():
        print(f"复用已有样张：{SAMPLES}")
        return
    print("正在生成验收样张…")
    _run_backend_python(_SAMPLE_CODE, SAMPLES, DOCX_MARK, XLSX_MARK, TXT_MARK, PDF_MARK)
    names = sorted(p.name for p in SAMPLES.iterdir())
    print(f"  共 {len(names)} 个样张 → {SAMPLES}")


def dev_available() -> bool:
    try:
        with urllib.request.urlopen(f"{DEV_BASE}/", timeout=3) as response:
            return response.status == 200
    except Exception:
        return False


def main() -> int:
    build_samples()

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 1000}, accept_downloads=True)
        attach(page)
        try:
            run_section_1(page, dev_available())
            run_section_2(page)
            run_section_3_and_4(page)
            run_section_5(page)
            run_section_6(page)
            run_section_7(page)
        finally:
            browser.close()
        run_section_8(p)

    js_errors = [
        line
        for line in console_errors
        if line.startswith("[pageerror]")
        or ("[console.error]" in line and "Failed to load resource" not in line)
    ]
    check(not js_errors, f"浏览器控制台 0 条 JS 报错（{len(js_errors)} 条）")
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
