"""第八阶段的真机验收脚本（Playwright + 真实浏览器 + 真实混合负载）。

第八阶段不加任何新格式，改的是**并发架构**：按资源分池、每池自己的并发上限、
超时兜底、心跳与看门狗、自动重试、结构化日志与指标。所以这个脚本要证的不是
「能不能转出文件」，而是三件在界面上根本看不出来的事：

1. **一种资源被拖垮时，其它种类照常工作** —— Office 批次堵在那里的时候，
   图片批次必须照样先跑完（B / C / J 段）；
2. **服务端诚实地说得出发生了什么** —— 超时是内层先触发还是兜底触发、
   取消是「已停止」还是「正在停」、指标里每一个数都对得上（D / E / G 段）；
3. **并发是真的** —— 靠 /api/system/workers 的实时采样算出各池的**峰值并发**，
   并用「单件耗时 × 件数 ≫ 这一路的墙钟」把并发算出来，
   而不是只报一个好看的秒数（J 段）。

分段：

    A 段  启动与池：五个池齐备、worker 编号形状、配置与上报一致、超时表自洽
    B 段  混合批次真机：10 图片 + 3 TXT + 5 Word 一次提交，采样各池并发，
          图片池必须出现 ≥2 同时活跃、Office 池始终 ≤1；产物真验
    C 段  不互相阻塞：Office 批次先提交，图片批次后提交，图片必须先跑完，
          且两个池**在同一时刻**都有项在处理
    D 段  超时组合（第二个后端实例）：池兜底是**下限**不是旋钮；
          内层先触发时用户拿到的是诚实的超时说法而不是笼统的 TASK_TIMEOUT；
          超时之后槽位真的释放、队列继续跑
    E 段  取消：排队项立刻取消，在跑项如实显示 cancelling，绝不假装已停止
    F 段  重试：手动重试的既有契约没变；自动重试的可观测表面（字段与计数）
    G 段  指标与开关：指标自洽、关掉之后 /openapi.json 里真的没有那两个路径
    H 段  无泄露：两个新接口的原文过一个禁止片段表
    I 段  移动端 375 / 390 / 414 + 十四个工具页回归
    J 段  基准：10 图片 + 5 Word + 5 PDF→Word 三路同时跑，
          总耗时 / 各路耗时 / 各池峰值并发 / 并发倍数的硬证据

用法：
    1. 先构建前端并启动后端（后端会顺带托管 frontend/dist）：
           cd frontend && npm run build
           cd backend && .venv\\Scripts\\python -m uvicorn main:app --port 8011
    2. python scripts/verify_phase8.py

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
import statistics
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

# Windows 控制台默认是 GBK，直接打印「✓」会抛 UnicodeEncodeError 把整轮跑挂掉
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

WORK = pathlib.Path(
    os.environ.get("FILETOOLS_WORK_DIR") or tempfile.mkdtemp(prefix="filetools-verify-p8-")
)
SAMPLES = WORK / "samples"
DOWNLOADS = WORK / "downloads"
UNPACKED = WORK / "unpacked"
REPORT = ROOT / "scripts" / "verify_phase8_report.txt"

ROUTE = "/convert"
MOBILE_WIDTHS = (375, 390, 414)

WORKERS_URL = "/api/system/workers"
METRICS_URL = "/api/system/metrics"
TASKS_URL = "/api/conversion/tasks"

#: 五个池，顺序也必须是这个 —— 验收脚本与运维都按位置读这张表
POOL_ORDER = ("image", "pdf", "office", "ocr", "default")

#: 第二个后端实例（超时那一段）。端口与主实例错开，临时目录也隔离 ——
#: 两个实例共用 TEMP_ROOT 的话，一个的清理线程会去删另一个正在用的目录。
TIMEOUT_PORT = 8012
TIMEOUT_BASE = f"http://127.0.0.1:{TIMEOUT_PORT}"
TIMEOUT_LOG = WORK / "timeout-backend.log"

#: 第三个后端实例（关掉运行状态接口）。验的是「路径整个不存在」，
#: 而这件事只有用另一个进程重新 import 一次才测得出来 ——
#: 路由是在 import main 那一刻按环境变量决定注不注册的。
OFF_PORT = 8013
OFF_BASE = f"http://127.0.0.1:{OFF_PORT}"
OFF_LOG = WORK / "off-backend.log"

#: 出错响应、运行状态响应里绝不能出现的东西（§四十三）。
#: 这一份与 phase7 逐字一致；/api/config 与两个新接口都单独空跑过一遍确认零命中。
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

#: 验收样张里的标记。刻意与 pytest 样张用不同的串。
DOCX_MARK = "ACCEPT8DOCX"
TXT_MARK = "ACCEPT8TXT"
PDF_MARK = "ACCEPT8PDF"

TERMINAL = ("completed", "failed", "cancelled")

#: 「这一项超时了」的诚实说法：内层自己的限额先触发时给的是这些码，
#: 而不是池兜底的 TASK_TIMEOUT。
HONEST_TIMEOUT_CODES = ("PDF_CONVERSION_TIMEOUT", "PROCESSING_TIMEOUT")

IMAGE_NAMES = [f"素材{i:02d}.{'png' if i % 2 else 'jpg'}" for i in range(1, 11)]
DOCX_NAMES = [f"文档{i:02d}.docx" for i in range(1, 6)]
TXT_NAMES = [f"说明{i:02d}.txt" for i in range(1, 4)]
PDF_NAMES = [f"报告{i:02d}.pdf" for i in range(1, 6)]
SCAN_NAME = "扫描件.pdf"


# ----------------------------------------------------------------------
# 样张：复用后端测试里的那套构造器，避免同一份排版规则在这里再抄一遍
# ----------------------------------------------------------------------

_SAMPLE_CODE = r'''
import json, pathlib, sys

from tests.conftest import (
    build_docx_bytes,
    build_image_bytes,
    build_labeled_pdf,
    build_scanned_pdf,
)

out = pathlib.Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)
docx_mark, txt_mark, pdf_mark = sys.argv[2:5]

# 十张图片：混着格式才看得出图片池的并发（同一张图太快，采样采不到）
for i in range(1, 11):
    fmt = "PNG" if i % 2 else "JPEG"
    ext = "png" if i % 2 else "jpg"
    (out / f"素材{i:02d}.{ext}").write_bytes(
        build_image_bytes(900 + i * 40, 700 + i * 30, fmt)
    )

# 五份 Word：走 LibreOffice，落在 office 池
for i in range(1, 6):
    (out / f"文档{i:02d}.docx").write_bytes(build_docx_bytes(f"{docx_mark}{i}", paragraphs=1))

# 三份 TXT：不碰 LibreOffice，落在 image 池（builtin 需求）
for i in range(1, 4):
    (out / f"说明{i:02d}.txt").write_text(
        f"验收用纯文本 {txt_mark}-{i}\n第二行：中英文混排 with English words 42。\n",
        encoding="utf-8",
    )

# 五份文字版 PDF：走 OCR 池的 PDF → Word 通路（有文字层，不真的跑 OCR）
for i in range(1, 6):
    (out / f"报告{i:02d}.pdf").write_bytes(build_labeled_pdf(f"{pdf_mark}{i}", pages=2))

# 一份扫描件：没有文字层，OCR 必须逐页真跑 —— D 段靠它撑出超时窗口
(out / "扫描件.pdf").write_bytes(build_scanned_pdf([f"{pdf_mark}-S{i}" for i in range(1, 6)]))

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


def note(label: str) -> None:
    """只记录事实、不做判定的一行（数字、耗时、峰值并发）。"""
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


def body(page: Page) -> str:
    return page.inner_text("body")


def wait_for_text(page: Page, needle: str, *, timeout: int = 60_000) -> bool:
    try:
        page.wait_for_selector(f"text={needle}", timeout=timeout)
        return True
    except Exception:
        return False


# ----------------------------------------------------------------------
# HTTP：直接打接口。浏览器看到的只是渲染后的文字，这里看的是原始响应体
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
    """原样取回一个响应：状态码、Content-Type、正文。

    泄露检查必须看**原文**，不能看反序列化再序列化的结果 ——
    那样会把「响应里到底有没有这个串」变成一个二手问题。
    """
    request = urllib.request.Request(f"{base}{url}")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
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


def post_tasks(
    files: list[tuple[str, bytes]],
    target_type: str,
    *,
    base: str = BASE,
) -> tuple[int, str]:
    """直接往 /api/conversion/tasks 发一次 multipart。"""
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
    parts.append(f"--{boundary}--\r\n".encode())

    request = urllib.request.Request(
        f"{base}{TASKS_URL}",
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


def submit(
    files: list[tuple[str, bytes]], target_type: str, *, base: str = BASE
) -> tuple[str, dict]:
    """提交一批，返回 (batch_id, 提交时的快照)。"""
    status, raw = post_tasks(files, target_type, base=base)
    if status != 202:
        raise AssertionError(f"提交失败（{status}）：{raw[:400]}")
    payload = json.loads(raw)
    return payload["batch_id"], payload


def batch_status(batch_id: str, *, base: str = BASE) -> dict:
    _, payload = get_json(f"{TASKS_URL}/{batch_id}", base=base)
    if not isinstance(payload, dict):
        raise AssertionError(f"查询批次失败：{payload}")
    return payload


def wait_batch(
    batch_id: str, *, timeout: float = 900.0, interval: float = 0.4, base: str = BASE
) -> dict:
    """轮询一批直到它定下来，返回最后一份快照。"""
    deadline = time.monotonic() + timeout
    snapshot: dict = {}
    while time.monotonic() < deadline:
        snapshot = batch_status(batch_id, base=base)
        if snapshot.get("status") in TERMINAL:
            return snapshot
        time.sleep(interval)
    raise AssertionError(f"等了 {timeout} 秒这一批还没定下来：{snapshot.get('status')}")


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
    _, payload = get_json(WORKERS_URL, base=base)
    if not isinstance(payload, dict):
        raise AssertionError(f"运行状态接口没返回 JSON：{payload}")
    return payload


def metrics_of(base: str = BASE) -> dict:
    _, payload = get_json(METRICS_URL, base=base)
    if not isinstance(payload, dict):
        raise AssertionError(f"指标接口没返回 JSON：{payload}")
    return payload


def pool_of(snapshot: dict, name: str) -> dict:
    for pool in snapshot["pools"]:
        if pool["name"] == name:
            return pool
    raise AssertionError(f"快照里没有 {name} 池：{[p['name'] for p in snapshot['pools']]}")


# ----------------------------------------------------------------------
# 并发采样器：把「并发真的发生了」变成可计算的事实，而不是一句感觉
# ----------------------------------------------------------------------


class PoolSampler(threading.Thread):
    """后台按固定间隔采 /api/system/workers，记下每个池的**峰值并发**。

    这是整份脚本里最重要的一个工具：没有它，「图片池并发是 2」就只是配置里
    写着的数字；有了它，那是**连续采样里真的看到过两次同时活跃**。
    """

    def __init__(
        self, configured: dict[str, int], *, base: str = BASE, interval: float = 0.15
    ) -> None:
        super().__init__(daemon=True)
        self.base = base
        self.interval = interval
        self.configured = configured
        self.peak_active: dict[str, int] = {}
        self.peak_queue: dict[str, int] = {}
        #: 同一个采样点里**同时**有项在处理的池有几个。这是「三种资源并行」
        #: 唯一说得清的证据 —— 各池的峰值是分别统计的，两个池都到过 1
        #: 完全可能是先后发生的。
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
                # 采样失败不该把整轮验收带下水：这只说明这一刻没读到，
                # 如实计一笔，继续采。
                self.failures += 1
                time.sleep(self.interval)
                continue
            self.samples += 1
            busy_pools = 0
            for pool in snapshot["pools"]:
                name = pool["name"]
                self.peak_active[name] = max(self.peak_active.get(name, 0), pool["active"])
                self.peak_queue[name] = max(
                    self.peak_queue.get(name, 0), pool["queue_size"]
                )
                if pool["active"]:
                    busy_pools += 1
                if pool["active"] > pool["configured_workers"]:
                    self.violations.append(
                        (name, pool["active"], pool["configured_workers"])
                    )
            self.peak_busy_pools = max(self.peak_busy_pools, busy_pools)
            time.sleep(self.interval)

    def stop(self) -> None:
        self.stopped.set()
        self.join(timeout=5)

    def summary(self) -> str:
        peaks = "，".join(
            f"{name}={self.peak_active.get(name, 0)}/{self.configured.get(name, '?')}"
            for name in POOL_ORDER
        )
        return f"{peaks}；峰值同时忙碌的池 {self.peak_busy_pools} 个"


def start_sampler(*, base: str = BASE, interval: float = 0.15) -> PoolSampler:
    snapshot = workers_of(base)
    configured = {p["name"]: p["configured_workers"] for p in snapshot["pools"]}
    sampler = PoolSampler(configured, base=base, interval=interval)
    sampler.start()
    return sampler


# ----------------------------------------------------------------------
# 第二个 / 第三个后端实例
# ----------------------------------------------------------------------


def start_backend(
    *,
    port: int,
    base: str,
    temp: pathlib.Path,
    log: pathlib.Path,
    env_extra: dict,
) -> subprocess.Popen:
    """另起一个后端实例，用一组不同的环境变量。

    为什么非要另起进程：池超时、超时组合、路由注不注册，全都是在
    **进程启动时**读一次环境变量定下来的。在同一个进程里 monkeypatch
    只是测试内部的假动作，验不到「换一组环境变量部署会怎样」。
    """
    temp.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update({"FILETOOLS_TEMP_ROOT": str(temp), "PYTHONIOENCODING": "utf-8"})
    env.update(env_extra)
    handle = open(log, "wb")
    process = subprocess.Popen(
        [
            str(BACKEND_PYTHON),
            "-m",
            "uvicorn",
            "main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=str(BACKEND),
        env=env,
        stdout=handle,
        stderr=handle,
    )
    for _ in range(120):
        try:
            with urllib.request.urlopen(f"{base}/api/health", timeout=1) as response:
                if response.status == 200:
                    return process
        except (urllib.error.URLError, OSError):
            time.sleep(0.5)
    process.terminate()
    raise SystemExit(f"后端 {base} 没能启动，看日志：{log}")


def stop_backend(process: subprocess.Popen) -> None:
    process.terminate()
    try:
        process.wait(timeout=30)
    except subprocess.TimeoutExpired:
        process.kill()


# ----------------------------------------------------------------------
# 页面操作
# ----------------------------------------------------------------------


def open_convert(page: Page, *, base: str = BASE) -> None:
    page.goto(f"{base}{ROUTE}", wait_until="networkidle")
    page.wait_for_selector("input[type=file]", state="attached", timeout=30_000)


def choose_target(page: Page, name: str, *, index: int = 0) -> None:
    """选一个格式。真正可点的是包着 ``sr-only`` radio 的那层 label。"""
    radio = page.get_by_role("radio", name=name, exact=False).nth(index)
    radio.evaluate("el => el.closest('label').click()")
    page.wait_for_timeout(200)
    check(radio.is_checked(), f"选中了「{name}」")


def unpack(archive_path: pathlib.Path) -> pathlib.Path:
    """真的解开一个 ZIP 到独立目录，返回目录。"""
    target = UNPACKED / f"{archive_path.stem}-{uuid.uuid4().hex[:6]}"
    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive_path) as archive:
        assert archive.testzip() is None, "ZIP 本身是坏的"
        archive.extractall(target)
    return target


def download_result(url: str, *, base: str = BASE) -> pathlib.Path | None:
    """按下载地址取回结果。"""
    if not url:
        return None
    DOWNLOADS.mkdir(parents=True, exist_ok=True)
    name = url.rstrip("/").rsplit("/", 1)[-1] or "result"
    target = DOWNLOADS / f"{uuid.uuid4().hex[:6]}-{name}"
    try:
        with urllib.request.urlopen(f"{base}{url}", timeout=300) as response:
            target.write_bytes(response.read())
    except urllib.error.HTTPError as exc:
        check(False, f"下载结果失败：HTTP {exc.code}")
        return None
    return target


# ----------------------------------------------------------------------
# A 段：启动与池
# ----------------------------------------------------------------------


def run_part_a() -> None:
    section("A 段：启动与池")

    config = config_of()
    snapshot = workers_of()

    names = [pool["name"] for pool in snapshot["pools"]]
    check(names == list(POOL_ORDER), f"五个池齐备且顺序固定（{names}）")
    check(snapshot["accepting"] is True, "队列处于接受新任务的状态")

    sizes = config.get("worker_pools") or {}
    check(
        sizes
        == {name: pool_of(snapshot, name)["configured_workers"] for name in POOL_ORDER},
        f"/api/config 的 worker_pools 与运行状态一致（{sizes}）",
    )

    ids: list[str] = []
    for name in POOL_ORDER:
        pool = pool_of(snapshot, name)
        check(
            pool["started_workers"] == pool["configured_workers"],
            f"{name} 池的 worker 全部就绪"
            f"（{pool['started_workers']}/{pool['configured_workers']}）",
        )
        for worker in pool["workers"]:
            ids.append(worker["worker_id"])
            if not re.fullmatch(r"worker-\d+", worker["worker_id"]):
                check(False, f"{name} 池的 worker 编号形状不对：{worker['worker_id']}")
                break
    check(len(ids) == sum(sizes.values()), f"每个 worker 都有编号（{len(ids)} 个）")
    check(len(ids) == len(set(ids)), "worker 编号在所有池之间不重号")
    check(
        snapshot["total_workers"] == sum(sizes.values()),
        f"合计 worker 数等于配置之和（{snapshot['total_workers']}）",
    )
    check(
        snapshot["total_active"] == 0 and snapshot["total_queue"] == 0,
        "刚启动时没有活跃项、队列是空的",
    )
    check(
        sum(pool["failed"] for pool in snapshot["pools"]) == 0,
        "刚启动时各池的失败计数都是 0",
    )

    # 超时表：生效值必须是「池兜底」与「池内既有操作」取 max。
    # 这是本阶段最容易搞错的一处 —— 池兜底若抢在操作自己的限额之前触发，
    # 用户拿到的就是一句笼统的「任务处理超时」，而不是诚实的「拆分超时」。
    rows = snapshot["timeouts"]
    check([row["pool"] for row in rows] == list(POOL_ORDER), "超时表也覆盖五个池")
    composed = True
    for row in rows:
        inner = list(row["existing_timeouts"].values())
        if row["effective_timeout_seconds"] != max([row["pool_timeout_seconds"], *inner]):
            composed = False
            check(False, f"{row['pool']} 池的生效超时不是 max(兜底, 既有操作)：{row}")
    check(composed, "每个池的生效超时 = max(池兜底, 池内既有操作超时)")
    check(
        config.get("pool_timeouts")
        == {row["pool"]: row["effective_timeout_seconds"] for row in rows},
        "/api/config 的 pool_timeouts 与运行状态一致",
    )
    for row in rows:
        inner = max(row["existing_timeouts"].values()) if row["existing_timeouts"] else 0
        note(
            f"{row['pool']:>7} 池：兜底 {row['pool_timeout_seconds']:>4}s，"
            f"池内既有 {inner:>4}s，生效 {row['effective_timeout_seconds']:>4}s"
        )

    check(config.get("system_api_enabled") is True, "运行状态接口默认开启")
    check(
        config.get("queue_workers") == sizes["default"],
        f"既有的 queue_workers 语义没变（{config.get('queue_workers')}）",
    )

    metrics = metrics_of()
    check(
        set(metrics)
        == {"uptime_seconds", "tasks", "retries", "workers", "duration_ms", "by_type"},
        "指标接口的顶层字段齐全",
    )
    check(
        metrics["tasks"]["total"]
        == metrics["tasks"]["completed"]
        + metrics["tasks"]["failed"]
        + metrics["tasks"]["cancelled"],
        "指标的合计 = 成功 + 失败 + 取消",
    )
    check(
        set(metrics["tasks"])
        == {"total", "started", "completed", "failed", "cancelled", "timeout", "lost"},
        "任务计数器的字段就是约定好的那七个",
    )


# ----------------------------------------------------------------------
# B 段：混合批次真机
# ----------------------------------------------------------------------


def run_part_b() -> None:
    section("B 段：混合批次真机（10 图片 + 3 TXT + 5 Word，一次提交）")

    names = IMAGE_NAMES + TXT_NAMES + DOCX_NAMES
    batch_id, submitted = submit(collect(*names), "pdf")
    check(submitted["total"] == len(names), f"整批 {len(names)} 个文件都收下了")
    check(len(submitted["tasks"]) == len(names), "提交回执里逐项列出了每一个文件")

    sampler = start_sampler()
    snapshot = wait_batch(batch_id, timeout=900.0)
    sampler.stop()

    check(snapshot["status"] == "completed", f"混合批次整体成功（{snapshot['status']}）")
    check(snapshot["completed"] == len(names), f"{len(names)} 个文件全部转换成功")
    check(snapshot["failed"] == 0, "没有文件失败")

    note(f"采样 {sampler.samples} 次：" + sampler.summary())
    image_peak = sampler.peak_active.get("image", 0)
    office_peak = sampler.peak_active.get("office", 0)
    check(
        image_peak >= 2,
        f"图片池真的出现过 ≥2 个项同时处理（峰值 {image_peak}）—— 并发生效的硬证据",
    )
    check(
        office_peak <= sampler.configured["office"],
        f"Office 池始终没超过配置上限（峰值 {office_peak}/{sampler.configured['office']}）",
    )
    check(
        not sampler.violations,
        f"任何时刻都没有池超过自己的配置上限（越界 {len(sampler.violations)} 次）",
    )
    check(sampler.failures == 0, f"采样期间运行状态接口一次都没读失败（{sampler.failures} 次）")

    # 产物真验：界面写着「转换完成」不算数，打开文件才算
    result = snapshot["result"]
    check(result is not None, "整批给出了结果摘要")
    if result is None:
        return
    check(len(result["items"]) == len(names), "结果里列出了每一个文件")
    archive = download_result(result["download_url"])
    # 下载地址是一次性令牌端点，路径上没有扩展名，所以这里按**内容**判是不是
    # 一个真 ZIP，而不是看文件名后缀 —— 后者只是在验我自己的下载函数。
    check(
        archive is not None and zipfile.is_zipfile(archive),
        f"{len(names)} 个结果打成了一个 ZIP（{archive.name if archive else '无'}）",
    )
    if archive is None:
        return

    folder = unpack(archive)
    produced = sorted(child for child in folder.rglob("*") if child.is_file())
    check(len(produced) == len(names), f"ZIP 里正好 {len(names)} 个文件（{len(produced)}）")
    check(
        all(".." not in part for path in produced for part in path.parts),
        "ZIP 里没有路径穿越（§三十七）",
    )

    texts: list[str] = []
    broken: list[str] = []
    for child in produced:
        info = inspect(child, "pdf")
        if info.get("pages", 0) >= 1 and info.get("size", 0) > 0:
            texts.append(squashed(info.get("text", "")))
        else:
            broken.append(f"{child.name}: {info}")
    check(
        not broken,
        f"{len(produced)} 份产物都是真的能打开的 PDF"
        + (f"（坏的：{broken}）" if broken else ""),
    )

    # 内容也对得上：写进源文件里的标记必须能在产物里读回来
    docx_hits = [text for text in texts if DOCX_MARK in text]
    check(len(docx_hits) == 5, f"5 份 Word 转出来的 PDF 里都读得到原文（{len(docx_hits)} 份）")
    txt_hits = [text for text in texts if TXT_MARK in text]
    check(len(txt_hits) == 3, f"3 份 TXT 转出来的 PDF 里都读得到原文（{len(txt_hits)} 份）")


# ----------------------------------------------------------------------
# C 段：一种资源被占住时，另一种照常工作
# ----------------------------------------------------------------------


def run_part_c() -> None:
    section("C 段：Office 被占住时图片照常跑（不互相阻塞）")

    office_id, _ = submit(collect(*DOCX_NAMES), "pdf")
    # 故意让 Office 批次先上路：如果队列还是第四阶段那条全局 FIFO，
    # 后提交的图片就得排在它后面
    time.sleep(0.3)
    image_names = IMAGE_NAMES[:3]
    image_id, _ = submit(collect(*image_names), "webp")

    sampler = start_sampler()
    started = time.monotonic()
    image_done = wait_batch(image_id, timeout=900.0)
    image_seconds = time.monotonic() - started
    office_done = wait_batch(office_id, timeout=900.0)
    office_seconds = time.monotonic() - started
    sampler.stop()

    check(image_done["status"] == "completed", "后提交的图片批次成功了")
    check(office_done["status"] == "completed", "先提交的 Office 批次也成功了")
    check(
        image_seconds < office_seconds,
        f"后提交的图片批次先跑完（图片 {image_seconds:.1f}s < Office {office_seconds:.1f}s）"
        " —— 没有队头阻塞",
    )
    note("各池峰值：" + sampler.summary())
    check(
        sampler.peak_busy_pools >= 2,
        f"两个池**在同一时刻**都有项在处理（峰值同时忙碌 {sampler.peak_busy_pools} 个池）"
        " —— 这是「并发」而不是「先后」",
    )
    check(
        sampler.peak_active.get("image", 0) >= 2,
        f"图片池峰值并发 {sampler.peak_active.get('image', 0)} ≥ 2",
    )
    check(not sampler.violations, "并发期间没有任何池越界")


# ----------------------------------------------------------------------
# D 段：超时组合（第二个后端实例）
# ----------------------------------------------------------------------


def run_part_d() -> None:
    section("D 段：超时组合与槽位释放（第二个后端实例）")

    # -- D1：池兜底是**下限**，不是可以往下拧紧的旋钮 --
    #
    # 把五个池的兜底全部调到 1 秒，生效超时**必须**仍然是各池内既有操作的
    # 最大值。如果实现写成「池超时说了算」，这里会全线塌成 1 秒 ——
    # 一次正常的 240 秒 PDF 拆分会被池超时抢先判死，用户拿到的是笼统的
    # 「任务处理超时」而不是诚实的「拆分超时，请减少页数」。
    process = start_backend(
        port=TIMEOUT_PORT,
        base=TIMEOUT_BASE,
        temp=WORK / "server-temp-d1",
        log=TIMEOUT_LOG,
        env_extra={
            "FILETOOLS_IMAGE_TIMEOUT": "1",
            "FILETOOLS_PDF_TIMEOUT": "1",
            "FILETOOLS_OFFICE_POOL_TIMEOUT": "1",
            "FILETOOLS_OCR_TIMEOUT": "1",
            "FILETOOLS_DEFAULT_ITEM_TIMEOUT": "1",
        },
    )
    try:
        rows = {row["pool"]: row for row in workers_of(TIMEOUT_BASE)["timeouts"]}
        check(
            all(row["pool_timeout_seconds"] == 1 for row in rows.values()),
            "五个池的兜底都被调成了 1 秒（换一组环境变量真的生效了）",
        )
        check(
            all(row["effective_timeout_seconds"] > 1 for row in rows.values()),
            "五个池的生效超时**都没有**跟着塌到 1 秒 —— 兜底是下限，不是旋钮",
        )
        check(
            all(
                row["effective_timeout_seconds"]
                == max([row["pool_timeout_seconds"], *row["existing_timeouts"].values()])
                for row in rows.values()
            ),
            "生效超时仍然逐个等于 max(兜底, 池内既有操作)",
        )
        for name in POOL_ORDER:
            inner = max(rows[name]["existing_timeouts"].values())
            note(
                f"{name:>7} 池：兜底 1s → 生效 "
                f"{rows[name]['effective_timeout_seconds']:.0f}s（池内既有 {inner}s）"
            )
    finally:
        stop_backend(process)

    # -- D2：内层先触发时，用户拿到的是诚实的说法，不是笼统的 TASK_TIMEOUT --
    #
    # 起一个新实例：OCR 池的兜底仍然是 300 秒（它被 PDF_TO_WORD 的 300s
    # 顶着），而 PDF → Word **自己的预算**被调到 1 秒。一份 5 页扫描件跑 OCR
    # 稳稳超过 1 秒，于是内层先触发 —— 而且是它自己体面地退出的
    # （每页开工前检查预算），不是被外层 wait_for 掐断的：后者只会留下一个
    # 攥着 OCR 锁继续跑的僵尸线程。
    process = start_backend(
        port=TIMEOUT_PORT,
        base=TIMEOUT_BASE,
        temp=WORK / "server-temp-d2",
        log=TIMEOUT_LOG,
        env_extra={
            "FILETOOLS_PDF_TO_WORD_WORKER_BUDGET": "1",
            "FILETOOLS_PDF_TO_WORD_TIMEOUT": "300",
        },
    )
    try:
        config = config_of(TIMEOUT_BASE)
        check(config.get("ocr_available") is True, "第二个实例的 OCR 组件可用")
        rows = {row["pool"]: row for row in workers_of(TIMEOUT_BASE)["timeouts"]}
        check(
            rows["ocr"]["pool_timeout_seconds"] == 300,
            f"OCR 池的兜底仍有 300 秒（{rows['ocr']['pool_timeout_seconds']}s）",
        )

        scan_id, _ = submit(collect(SCAN_NAME), "docx", base=TIMEOUT_BASE)
        failed = wait_batch(scan_id, timeout=600.0, base=TIMEOUT_BASE)
        item = failed["tasks"][0]
        check(
            failed["status"] == "failed",
            f"1 秒预算下扫描件转 Word 确实失败了（{item['status']}）",
        )
        check(
            item["error_code"] != "TASK_TIMEOUT",
            f"拿到的是内层诚实的说法而不是笼统的 TASK_TIMEOUT（{item['error_code']}）",
        )
        check(
            item["error_code"] in HONEST_TIMEOUT_CODES,
            f"错误码是内层超时那一类（{item['error_code']}）",
        )
        note(f"错误码 {item['error_code']}：{item['error_message']}")
        check(
            bool(item["error_message"]) and item["error_message"] != "任务处理超时，请稍后重试",
            "失败原因是给人看的具体说明，不是一句笼统的「任务处理超时」",
        )
        check(
            item["timeout_requested"] is False,
            "内层超时不该把池的兜底超时标记也打上（timeout_requested 仍是 False）",
        )

        # -- D3：超时之后槽位真的释放了，队列继续跑 --
        #
        # 不释放的话，OCR 池那一个槽位会被永远占着，后面所有 PDF → Word
        # 都会卡在排队里 —— 这是最难在界面上发现、也最要命的一种坏法。
        after = workers_of(TIMEOUT_BASE)
        check(after["total_active"] == 0, "失败之后没有项还赖在「处理中」")
        check(after["total_queue"] == 0, "队列被清空了，没有项永远排在那里")

        again_id, _ = submit(collect(IMAGE_NAMES[0]), "pdf", base=TIMEOUT_BASE)
        again = wait_batch(again_id, timeout=300.0, base=TIMEOUT_BASE)
        check(again["status"] == "completed", "超时之后同一个实例仍然能正常干活（槽位释放了）")
        check(
            workers_of(TIMEOUT_BASE)["total_active"] == 0,
            "第二次也没留下卡住的项",
        )
    finally:
        stop_backend(process)


# ----------------------------------------------------------------------
# E 段：取消的诚实性
# ----------------------------------------------------------------------


def run_part_e() -> None:
    section("E 段：取消（协作式，且如实显示）")

    count = config_of()["queue_workers"] + 1
    names = (DOCX_NAMES * 2)[:count]
    batch_id, submitted = submit(collect(*names), "pdf")

    # 先等**至少一项真的开始跑**再取消：Office 池只有 1 个槽位，
    # 于是「一项在跑 + 其余在排队」是这一刻的确定状态。
    # 提交完立刻取消会变成「几项都还没被认领」，那时没有 cancelling 可验 ——
    # 那是测运气，不是测行为。
    deadline = time.monotonic() + 120
    started = False
    while time.monotonic() < deadline:
        if batch_status(batch_id)["processing"] >= 1:
            started = True
            break
        time.sleep(0.2)
    check(started, "至少有一项真的进入了处理中")

    status, cancelled = post_json(f"{TASKS_URL}/{batch_id}/cancel")
    check(status == 200, f"取消请求被接受（HTTP {status}）")

    check(cancelled["cancelling"] is True, "整批被标记为「正在取消」")
    check(cancelled["queued"] == 0, "排队中的项立刻不再是排队状态")
    running = [task for task in cancelled["tasks"] if task["status"] == "cancelling"]
    check(
        bool(running),
        f"已经在跑的项如实显示「正在取消」（{len(running)} 项），没有假装已经停了（§十五）",
    )
    check(
        all(task["error_code"] is None for task in running),
        "「正在取消」不是一种失败，不该带错误码",
    )
    check(
        all(task["can_retry"] is False for task in cancelled["tasks"]),
        "已请求取消的批次里不再提供重试",
    )
    check(
        cancelled["queued"]
        + cancelled["processing"]
        + cancelled["completed"]
        + cancelled["failed"]
        + cancelled["cancelled"]
        == submitted["total"],
        "取消那一刻的五个计数加起来就是总数，没有项凭空消失",
    )

    snapshot = wait_batch(batch_id, timeout=900.0)
    check(snapshot["status"] == "cancelled", f"整批最终是已取消（{snapshot['status']}）")
    check(
        snapshot["cancelled"] + snapshot["completed"] == submitted["total"],
        f"没跑完的都被取消、跑完了的算完成，没有项停在中间"
        f"（取消 {snapshot['cancelled']} / 完成 {snapshot['completed']}）",
    )
    check(snapshot["failed"] == 0, "取消不算失败")
    check(snapshot["error"] is None, "取消是用户自己按的，整批不该报错")
    check(
        all(task["error_code"] is None for task in snapshot["tasks"]),
        "被取消的项不该带一个错误码 —— 那不是失败",
    )
    check(
        all(task["status"] == "cancelled" for task in snapshot["tasks"]),
        "每一项最终都定在了已取消",
    )

    after = workers_of()
    check(after["total_active"] == 0, "取消之后没有项卡在「处理中」")


# ----------------------------------------------------------------------
# F 段：重试
# ----------------------------------------------------------------------


def run_part_f() -> None:
    section("F 段：重试（手动契约不变 + 自动重试的可观测表面）")

    # 自动重试的真实触发需要一个**服务器侧的偶发失败**（WORKER_LOST /
    # TEMPORARY_IO_ERROR / TASK_TIMEOUT）。黑盒脚本制造不出这三样：
    # 前两个要么需要把内部锁攥在手里，要么需要一条永不返回的处理器；
    # 第三个需要处理器连自己的超时都不理。那不是这里的疏漏 ——
    # 那三样由 tests/test_worker_pool.py 的 43 条用假慢处理器与真实抛错
    # 处理器逐条验证。这里只验**对外可观测的表面**：字段在不在、口径对不对。
    metrics = metrics_of()
    check(
        "automatic" in metrics["retries"] and "manual" in metrics["retries"],
        "指标里自动重试与手动重试分开计（合成一个数就分不清是谁重试的）",
    )
    check(
        isinstance(metrics["retries"]["automatic"], int)
        and metrics["retries"]["automatic"] >= 0,
        f"自动重试计数是个真实的数（{metrics['retries']['automatic']}）",
    )
    check(
        "recycled" in metrics["workers"] and "watchdog_kicks" in metrics["workers"],
        f"worker 回收与看门狗分开计"
        f"（回收 {metrics['workers']['recycled']}，"
        f"踢了 {metrics['workers']['watchdog_kicks']} 次）",
    )
    snapshot = workers_of()
    check(
        sum(pool["lost"] for pool in snapshot["pools"]) == metrics["tasks"]["lost"],
        "各池的失联计数之和 = 指标里的失联总数",
    )

    # 手动重试的既有契约：成功项不可重试、不存在的批次 404、形状不对 400
    batch_id, _ = submit(collect(IMAGE_NAMES[0], IMAGE_NAMES[1]), "webp")
    done = wait_batch(batch_id, timeout=600.0)
    check(done["status"] == "completed", "用于验重试契约的批次先成功了")

    for task in done["tasks"]:
        check(task["can_retry"] is False, f"成功的项不给重试按钮（{task['task_id']}）")
        check(
            task["auto_retry_count"] == 0,
            f"成功的项不该有自动重试记录（{task['auto_retry_count']}）",
        )
        check(task["timeout_requested"] is False, "成功的项不该带着超时标记")
        status, payload = post_json(f"{TASKS_URL}/{task['task_id']}/retry")
        check(
            status == 409 and error_code_of(payload) == "TASK_NOT_RETRYABLE",
            f"重试一个成功的项被拒（HTTP {status} / {error_code_of(payload)}）",
        )
        check_no_leak(json.dumps(payload, ensure_ascii=False), "重试成功的项")

    status, payload = post_json(f"{TASKS_URL}/does-not-exist:0/retry")
    check(
        status == 404 and error_code_of(payload) == "TASK_NOT_FOUND",
        f"重试一个不存在的批次是 404（HTTP {status} / {error_code_of(payload)}）",
    )
    check_no_leak(json.dumps(payload, ensure_ascii=False), "重试不存在的批次")

    status, payload = post_json(f"{TASKS_URL}/not-a-task-id/retry")
    check(status == 400, f"形状不对的任务号被挡下（HTTP {status}）")
    check_no_leak(json.dumps(payload, ensure_ascii=False), "形状不对的任务号")


# ----------------------------------------------------------------------
# G 段：指标与开关
# ----------------------------------------------------------------------


def run_part_g() -> None:
    section("G 段：指标自洽与运行状态接口的开关")

    before = metrics_of()

    batch_id, _ = submit(collect(*IMAGE_NAMES[:5]), "webp")
    done = wait_batch(batch_id, timeout=600.0)
    check(done["status"] == "completed", "用于验指标的批次成功了")

    after = metrics_of()
    delta_completed = after["tasks"]["completed"] - before["tasks"]["completed"]
    check(delta_completed == 5, f"指标如实记下了这 5 个成功（+{delta_completed}）")
    check(
        after["tasks"]["total"]
        == after["tasks"]["completed"]
        + after["tasks"]["failed"]
        + after["tasks"]["cancelled"],
        "指标自洽：合计 = 成功 + 失败 + 取消",
    )
    check(
        after["tasks"]["timeout"] <= after["tasks"]["failed"]
        and after["tasks"]["lost"] <= after["tasks"]["failed"],
        "超时与失联是失败的子集，没有重复计数",
    )
    check(
        after["tasks"]["started"] >= before["tasks"]["started"] + 5,
        "「已开始」跟着动了，而且不少于成功数",
    )

    image_before = before["by_type"].get("image", {"count": 0})
    image_after = after["by_type"].get("image", {"count": 0})
    check(
        image_after["count"] >= image_before["count"] + 5,
        f"分类统计跟着动了，而且动的是图片池"
        f"（{image_before['count']} → {image_after['count']}）",
    )
    check(
        after["duration_ms"]["count"] >= before["duration_ms"]["count"] + 5,
        "成功的项都记了耗时（没有耗时的项不该把平均值往下拽）",
    )
    check(
        after["duration_ms"]["max"] is not None and after["duration_ms"]["min"] is not None,
        "耗时统计给出了真实的上下界"
        f"（min {after['duration_ms']['min']}ms / max {after['duration_ms']['max']}ms）",
    )
    check(after["uptime_seconds"] > 0, f"进程已运行 {after['uptime_seconds']}s")

    # 这两个接口是只读的：写方法必须被挡住
    for path in (WORKERS_URL, METRICS_URL):
        status, content_type, _ = get_raw(path)
        check(
            status == 200 and "application/json" in content_type,
            f"GET {path} 是 200 且是 JSON",
        )
        status, _ = get_json(path, method="POST")
        check(status == 405, f"POST {path} 被挡下（HTTP {status}）")

    # 开关：起第三个实例把接口整个关掉
    process = start_backend(
        port=OFF_PORT,
        base=OFF_BASE,
        temp=WORK / "server-temp-off",
        log=OFF_LOG,
        env_extra={"FILETOOLS_SYSTEM_API": "0"},
    )
    try:
        with urllib.request.urlopen(f"{OFF_BASE}/openapi.json", timeout=60) as response:
            paths = json.load(response)["paths"]
        check(
            not [path for path in paths if path.startswith("/api/system")],
            "关掉之后 /openapi.json 里真的没有这两个路径（路由根本没注册）",
        )
        check(
            "/api/health" in paths and TASKS_URL in paths,
            "关掉运行状态接口不影响别的接口",
        )
        for path in (WORKERS_URL, METRICS_URL):
            status, content_type, raw = get_raw(path, base=OFF_BASE)
            # 生产形态下后端顺带托管着前端，SPA 的兜底会把所有未匹配路径
            # 回落到 index.html。所以这里要断言的是**「API 面消失」**，
            # 而不是一个会随部署形态变化的数字（详见交付报告的已知限制）。
            check(
                "application/json" not in content_type,
                f"关掉之后 {path} 不再返回 JSON（HTTP {status} / {content_type}）",
            )
            check(
                "pools" not in raw and "by_type" not in raw and "uptime_seconds" not in raw,
                f"关掉之后 {path} 一个字段都不吐（{len(raw)} 字节）",
            )
        check(
            config_of(OFF_BASE).get("system_api_enabled") is False,
            "/api/config 如实上报开关是关的",
        )
    finally:
        stop_backend(process)


# ----------------------------------------------------------------------
# H 段：不泄露
# ----------------------------------------------------------------------


def check_no_leak(raw: str, label: str) -> None:
    leaked = [fragment for fragment in FORBIDDEN_FRAGMENTS if fragment in raw]
    check(not leaked, f"{label}：没有泄露内部细节" + (f"（泄露了 {leaked}）" if leaked else ""))


def run_part_h() -> None:
    section("H 段：不泄露（§四十三）")

    for path in (WORKERS_URL, METRICS_URL, "/api/config"):
        _, _, raw = get_raw(path)
        check_no_leak(raw, path)

    _, _, raw = get_raw(WORKERS_URL)

    # group_id 是 /api/tasks/{group_id} 的凭据，而那个接口会返回文件名 ——
    # 所以运行状态接口**不能**出现任何能关联到具体用户的东西
    token_shape = re.compile(r"[0-9a-f]{16,}")
    check(not token_shape.search(raw), "运行状态接口里没有令牌形状的字符串")
    flat = json.dumps(json.loads(raw), ensure_ascii=False)
    for word in ("filename", "group_id", "task_id", "source_filename", "download_url"):
        check(word not in flat, f"运行状态接口里没有 {word!r} 这个词")

    payload = json.loads(raw)
    expected = {
        "worker_id", "pool", "state", "busy_seconds",
        "handled", "failed", "recycles", "generation",
    }
    extra = [
        worker
        for pool in payload["pools"]
        for worker in pool["workers"]
        if set(worker) != expected
    ]
    check(not extra, "每个 worker 的字段就是约定好的那八个，没有多出来的")

    idle = [
        worker
        for pool in payload["pools"]
        for worker in pool["workers"]
        if worker["state"] == "idle"
    ]
    check(
        all(worker["busy_seconds"] is None for worker in idle),
        f"空闲的 worker 不说自己忙了多久（{len(idle)} 个空闲）",
    )


# ----------------------------------------------------------------------
# I 段：移动端 + 十四个工具页回归
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
                [
                    str(SAMPLES / IMAGE_NAMES[0]),
                    str(SAMPLES / DOCX_NAMES[0]),
                    str(SAMPLES / TXT_NAMES[0]),
                ],
            )
            page.wait_for_timeout(1500)
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


#: 每个分类列表页**自己那类**的工具入口。手抄自 ``config/tools.ts``，
#: 刻意不从被测页面反推 —— 从被测对象推出期望值等于没测。
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


def hrefs_on(page: Page) -> list[str]:
    """**主内容区**里所有 ``<a href>``。

    只看 ``main``：导航栏那个全站通用的「开始使用 → /image/compress」按钮
    在每一页都有，把它算进来就等于要求「/pdf 页面不许有页头」。
    要比的是这一页**列了什么**，不是站点外壳长什么样。
    """
    return page.eval_on_selector_all(
        "main a[href]", "els => els.map(el => el.getAttribute('href'))"
    )


def run_part_i_desktop(page: Page) -> None:
    section("I 段：十四个工具页回归")

    for route in PHASE_ROUTES:
        page.goto(BASE + route, wait_until="networkidle")
        page.wait_for_selector("input[type=file]", state="attached", timeout=30_000)
        check(page.locator("input[type=file]").count() > 0, f"{route} 仍能打开并接受文件")

    # 「这一页是不是本类工具的列表」**不能**用「页面里出没出现『统一转换中心』」来判。
    #
    # 第十阶段 A 把 ``/image`` 升级成 ``ImageTools.tsx`` 之后，它上面写着
    # 「入口都在统一转换中心」—— 这是**该说的话**，而旧的子串判据会把一句正确的
    # 文案判成错误。判据换成逐链接：自己那类的入口要在，别类的入口要不在。
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

    # /convert 是那个统一转换中心，这一条照旧
    page.goto(f"{BASE}/convert", wait_until="networkidle")
    check("统一转换中心" in body(page), "/convert 仍然是统一转换中心")

    # 真跑一次第一阶段的图片格式转换：页面能打开证明不了接口没被改坏
    page.goto(BASE + "/image/convert", wait_until="networkidle")
    page.wait_for_selector("input[type=file]", state="attached", timeout=30_000)
    page.set_input_files("input[type=file]", str(SAMPLES / IMAGE_NAMES[0]))
    page.wait_for_timeout(400)
    choose_target(page, "WEBP")
    page.get_by_role("button", name="开始转换").first.click()
    check(wait_for_text(page, "下载", timeout=300_000), "第一阶段的图片格式转换仍然跑得通")

    # 再跑一次第五阶段的 Word → PDF：统一中心复用的就是这条实现
    page.goto(BASE + "/doc/word", wait_until="networkidle")
    page.wait_for_selector("input[type=file]", state="attached", timeout=30_000)
    page.set_input_files("input[type=file]", str(SAMPLES / DOCX_NAMES[0]))
    page.wait_for_timeout(400)
    page.get_by_role("button", name="开始转换").first.click()
    check(wait_for_text(page, "下载", timeout=300_000), "第五阶段的 Word → PDF 仍然跑得通")


# ----------------------------------------------------------------------
# J 段：基准
# ----------------------------------------------------------------------


def run_part_j() -> None:
    section("J 段：基准（三条通路同时跑）")

    # 先量一件：单个图片转换要多久。并发有没有生效，全靠它当分母。
    single_seconds = _time_one(IMAGE_NAMES[0], "pdf")
    note(
        f"单件（图片 → PDF）耗时 {single_seconds:.2f}s"
        f"（{_BASELINE_ROUNDS} 次取中位数，间隔 {_BASELINE_INTERVAL}s）"
    )

    # 并发倍数：在**只有图片池在干活**的窗口里量，分子分母同条件。
    #
    # 这一路的目标格式用 WebP 而不是 PDF：图片池的活有相当一部分卡在
    # CPython 的 GIL 上（PNG 解码、JPEG 编码），两个线程并不能真的同时跑 ——
    # 单独量 Pillow 本身，PNG→JPEG 开两个线程只有 1.26 倍，PNG→WebP 有 1.89 倍。
    # 在那种**物理上就不并行**的负载上要求并发收益，考的是机器的 GIL，不是队列。
    ratio_target = "webp"
    ratio_single = _time_one(IMAGE_NAMES[0], ratio_target)
    ratio_batch = _quiet_batch(IMAGE_NAMES, ratio_target)
    ratio = ratio_single * len(IMAGE_NAMES) / ratio_batch
    note(
        f"单件（图片 → {ratio_target.upper()}）{ratio_single:.2f}s × {len(IMAGE_NAMES)} 件 "
        f"= {ratio_single * len(IMAGE_NAMES):.2f}s，实际墙钟 {ratio_batch:.2f}s "
        f"⇒ 并发倍数 {ratio:.2f}（只有图片池在跑）"
    )
    check(
        ratio > 1.4,
        f"并发倍数 {ratio:.2f} > 1.4 —— 图片池确实同时在跑不止一件，"
        "这个收益是算出来的，不是感觉出来的",
    )

    before = metrics_of()
    workers_before = workers_of()
    sampler = start_sampler(interval=0.1)
    outcomes: dict[str, dict] = {}
    elapsed: dict[str, float] = {}

    def run_one(key: str, names: list[str], target: str) -> None:
        started = time.monotonic()
        try:
            batch_id, _ = submit(collect(*names), target)
            outcomes[key] = wait_batch(batch_id, timeout=1800.0)
        except Exception as exc:  # noqa: BLE001 - 结果如实地记下来，不吞掉
            outcomes[key] = {"status": "error", "error": str(exc)}
        elapsed[key] = time.monotonic() - started

    # 三条通路落在三个**完全不同的池**上，这正是第八阶段要买的东西
    plan = (
        ("image", IMAGE_NAMES, "pdf"),
        ("office", DOCX_NAMES, "pdf"),
        ("ocr", PDF_NAMES, "docx"),
    )
    threads = [
        threading.Thread(target=run_one, args=(key, names, target), name=key)
        for key, names, target in plan
    ]
    started = time.monotonic()
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    total_seconds = time.monotonic() - started
    sampler.stop()
    after = metrics_of()
    workers_after = workers_of()

    for key, names, _ in plan:
        outcome = outcomes.get(key, {})
        check(
            outcome.get("status") == "completed",
            f"{len(names)} 件走 {key} 池这一路整体成功（{outcome.get('status')}）",
        )
        note(f"{key:>6} 池：{len(names)} 件用了 {elapsed.get(key, 0):.2f}s")

    note(f"三路同时提交，整轮墙钟 {total_seconds:.2f}s")
    note("各池峰值：" + sampler.summary())
    note(
        "各池峰值排队："
        + "，".join(f"{name}={sampler.peak_queue.get(name, 0)}" for name in POOL_ORDER)
    )
    check(sampler.samples > 0, f"基准期间采到了 {sampler.samples} 次运行状态")
    check(not sampler.violations, "基准期间没有任何池越界")
    check(
        sampler.peak_busy_pools >= 3,
        f"图片 / Office / OCR 三个池**在同一时刻**都有项在处理"
        f"（峰值同时忙碌 {sampler.peak_busy_pools} 个池）",
    )
    check(
        sampler.peak_active.get("image", 0) >= 2,
        f"图片池峰值并发 {sampler.peak_active.get('image', 0)} ≥ 2",
    )

    # ---- 并发的**硬证据**，而不是一句「跑得挺快」----
    #
    # 只报一个「总共花了 15 秒」说明不了任何事：串行跑也是 15 秒。
    # 这里用「单件耗时 × 件数」去比**这一路自己的墙钟** ——
    # 图片池 2 个槽位，10 件若真的并行，墙钟应当接近 5 件的耗时而不是 10 件。
    #
    # **两个数必须在同一个条件下量。** 这一轮三条腿是同时提交的，Office 那条
    # 走 LibreOffice、OCR 那条逐页串行，都会来抢 CPU；分母是空闲时量的、
    # 分子是满载时量的，比出来的就不是并发，而是机器当时有多忙 ——
    # 同一套代码同一台机器，空闲时量到 1.50，与 Office / OCR 争 CPU 时量到 1.06。
    # 所以倍数改用下面这个**单独的空闲窗口**来量，三条腿的并发证据由采样器
    # （峰值同时忙碌的池数、图片池峰值并发）与各 worker 的经手件数来给。
    image_seconds = elapsed.get("image", 0.0)
    check(image_seconds > 0, "图片池这一路的墙钟测到了")
    if image_seconds > 0:
        note(
            f"（同一条图片腿在满载时的读数是 "
            f"{single_seconds * len(IMAGE_NAMES) / image_seconds:.2f} —— "
            "分母量于空闲、分子量于满载，不参与判定）"
        )
        # 再补一条更硬的：这一批**真的分给了不止一个 worker**。
        # 「池子里配了两个 worker」是配置文件里的数字；各自经手了几件，
        # 才是这一轮真跑出来的事实。有了它，即使某台机器上单件的活快得
        # 测不出倍数（0.2s 的活被上传与轮询的固定开销盖住），
        # 「并发发生了」这件事仍然是被证明过的。
        before_handled = {
            w["worker_id"]: w["handled"]
            for w in pool_of(workers_before, "image")["workers"]
        }
        gained = [
            w["handled"] - before_handled.get(w["worker_id"], 0)
            for w in pool_of(workers_after, "image")["workers"]
        ]
        check(
            sum(1 for value in gained if value > 0) >= 2,
            f"图片池这一批真的分给了不止一个 worker（各自经手 {gained} 件）",
        )

    completed = after["tasks"]["completed"] - before["tasks"]["completed"]
    check(completed >= 20, f"这一轮一共完成 {completed} 件（10 图片 + 5 Word + 5 PDF）")


#: 量单件基准时的轮询间隔与次数。
#:
#: ``wait_batch`` 默认 0.4s —— 对一次跑几分钟的批量正合适，但拿来量
#: 「单件 0.15s」这个量级就是一块**走不准的秒表**：第一次查询要么正好撞上
#: 完成（0.15s），要么差一步、白等一整个 0.4s（0.43s）。同一个文件反复量，
#: 读数只在 0.16 / 0.43 两档之间跳。
#:
#: 这个抖动不是无关紧要的：分母抖高一档，并发倍数就跟着虚高（1.18 → 3.6）。
#: 第九阶段的全量回归里它正好落在低档，把一句真话报成了假失败。量的东西
#: 既没变、判据也没变，变的只是**把秒表换准** —— 细间隔轮询，取多次中位数。
_BASELINE_INTERVAL = 0.05
_BASELINE_ROUNDS = 3


def _time_one(name: str, target: str) -> float:
    """量单件的真实耗时：细间隔轮询 + 取中位数（理由见上一段注释）。"""
    samples: list[float] = []
    for _ in range(_BASELINE_ROUNDS):
        started = time.monotonic()
        batch_id, _ = submit(collect(name), target)
        outcome = wait_batch(batch_id, timeout=300.0, interval=_BASELINE_INTERVAL)
        check(outcome["status"] == "completed", f"基准用的单件（{name}）先跑通")
        samples.append(time.monotonic() - started)
    return statistics.median(samples)


def _quiet_batch(names: list[str], target: str) -> float:
    """把一批送进队列，量到全部落地为止的墙钟 —— 同样用细间隔轮询。

    用 0.4s 的轮询去量 1 秒出头的批量，等于给读数加上最多 0.4s 的量子化，
    量出来的并发倍数会跟着这个抖动上下跳。
    """
    started = time.monotonic()
    batch_id, _ = submit(collect(*names), target)
    outcome = wait_batch(batch_id, timeout=900.0, interval=_BASELINE_INTERVAL)
    check(
        outcome["status"] == "completed",
        f"基准用的 {len(names)} 件（→ {target.upper()}）整体成功（{outcome['status']}）",
    )
    return time.monotonic() - started


# ----------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------


def build_samples() -> None:
    if (SAMPLES / SCAN_NAME).exists() and (SAMPLES / IMAGE_NAMES[-1]).exists():
        print(f"复用已有样张：{SAMPLES}")
        return
    print("正在生成验收样张…")
    _run_backend_python(_SAMPLE_CODE, SAMPLES, DOCX_MARK, TXT_MARK, PDF_MARK)
    print(f"  样张目录：{SAMPLES}")


def main() -> int:
    build_samples()

    run_part_a()
    run_part_b()
    run_part_c()
    run_part_d()
    run_part_e()
    run_part_f()
    run_part_g()
    run_part_h()
    run_part_j()

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 1000}, accept_downloads=True)
        attach(page)
        try:
            run_part_i_desktop(page)
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
