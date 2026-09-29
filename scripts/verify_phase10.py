"""第十阶段（A + C）的封版验收脚本 —— 真机、真实接口、真实产物。

这是**第十阶段 A 与第十阶段 C 合并后的最终封版入口**。它不重跑 pytest
（那是 ``pytest -o addopts= -q`` 的事），只挑「机器跑一遍就能证明这套系统
此刻是好的」的那些高价值不变量。所以这里有几条是别的脚本已经证过的
—— 封版脚本本来就该自己把最关键的那几颗钉子再敲一遍，而不是转述
「另一个脚本说它是好的」。

十一段内容，对应第十阶段 C §十三–§十五、§二十四–§二十六 与 §六十一
点名的风险：

A 炸弹防线（§十三–§十五）—— **本阶段新增的东西，重点在这里**。三档都在：
  夹具本身是否是「字节极小、声明的画布极大」（否则测的就不是炸弹）、
  上档与下档在**真实接口**上各自给出哪一句中文、旧接口有没有绕过去、
  合法的大图还能不能转（防线不能变成能力削减），以及**浏览器**里用户
  真的看得到那句话。
B 元数据（§二十四·3 / §二十六）—— 统一接口默认「保留」、旧页面默认
  「清除」这条既有差异必须**是现在这个样子**，而且 XMP 在装得下的格式上
  真的往返、在装不下的格式上**明说装不下**、存进 TIFF 的拍摄信息不再丢。
C 能力目录（§五十六）—— ``matrix`` 与 ``conversions[]`` 必须互相对得上；
  缺组件时不得宣传；registry 仍然零 I/O、不 import 任何实现库。
D 兜底配置（§五）—— ``FALLBACK_CONFIG`` 与线上 ``/api/config`` **机械对账**。
  这条是上一版真正咬到人的地方：兜底值停在 80、服务端按 85 处理，
  不报错，只是同一张图在两个页面得到两份结果。
E 预览边界（§五）—— 预览与下载必须**是同一份字节**；浏览器渲染不了的
  格式不发地址、硬敲那个地址得到干净的业务错误。这一条在第十阶段 C
  修过一个真缺陷（旧接口按 ``image/*`` 前缀放行，TIFF/HEIC 挂破图）。
F 压缩与临时文件（§十一/§十二）—— 同名来源进 ZIP 不互相覆盖；
  跑完一轮不残留临时目录。
G 安全 —— 恶意 SVG 走**净化后继续**那条既有契约（不改成拒绝）；
  假扩展名被内容校验拦下；对外文本里不许出现服务器内部细节。
H 前端一致性 —— ``/image`` 列出的源格式、首页 FAQ 说的格式，
  都必须与**此刻**的能力目录逐项一致；旧工具页仍然活着。
I 移动端 —— 375 / 390 / 414 三档无横向溢出。
J 几何与体积（§六十一·4–9、§六十七）—— resize / crop / rotate / flip /
  压缩质量 / 目标体积 / EXIF 方向，每一条都**落到像素或字节上**：
  红块转到哪个角、宽高换没换、质量 90 是不是真比 50 大、「有没有达标」
  与实测一不一致。只报「文件存在」在这里一律不算通过。
K 汇总 —— 控制台零错误也是一条断言。

跑法（浏览器驱动用带 Playwright 的解释器，素材与校验用后端 venv）::

    cd frontend && npm run build
    cd backend && .venv/Scripts/python -m uvicorn main:app --host 127.0.0.1 --port 8011
    python scripts/verify_phase10.py

**不要用 Vite 开发服务器（5173）跑本脚本** —— 它把 ``/api`` 代理到别处，
版本不一致时会报出一批根本不存在的「回归」。

几处**踩过的坑**，写在这里免得下次又踩：

* 统一转换接口是**异步**的：``POST /api/conversion/tasks`` 返回 **202** 与
  ``batch_id``，终态要去 ``GET /api/conversion/tasks/{batch_id}`` 轮询。
  旧图片接口是同步的，整批失败时直接抛 **400**，响应体是
  ``{"error": {"code", "message"}}`` —— 两个形状不一样，别混。
* 两档炸弹的边长**从配置算**（``isqrt(MAX_IMAGE_PIXELS) + 1`` 与
  ``isqrt(2 * MAX_IMAGE_PIXELS) + 1``），不抄 8945 / 12650：上限是
  ``FILETOOLS_MAX_IMAGE_PIXELS`` 可配的，写死等于换个上限就测了个寂寞。
* JPEG / WEBP 不可能逐位还原，比颜色一律带容差；比**字节**只在不经过
  再次编码的那一处用（预览 vs 下载）。
* ``target_format`` 没有自转换（jpg → jpg 是 ``UNSUPPORTED_CONVERSION``）。
  下面每一对 源→目标 都先问过能力目录。

可用环境变量覆盖：
    FILETOOLS_WEB_BASE      默认 http://127.0.0.1:8011（直接打后端托管的 dist）
    FILETOOLS_BACKEND_PY    后端 venv 的 python，用于生成素材、打开产物
"""

from __future__ import annotations

import ast
import io
import json
import math
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

# Windows 控制台默认是 GBK，直接打印一个装饰符号就会抛 UnicodeEncodeError
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

WORK = pathlib.Path(tempfile.mkdtemp(prefix="filetools-verify-p10-"))
SAMPLES = WORK / "samples"
DOWNLOADS = WORK / "downloads"
REPORT = ROOT / "scripts" / "verify_phase10_report.txt"

ROUTE = "/convert"
IMAGE_TOOLS_ROUTE = "/image"
MOBILE_WIDTHS = (375, 390, 414)
CAPABILITIES_URL = "/api/conversion/capabilities"
CONFIG_URL = "/api/config"
UNIFIED_SUBMIT = "/api/conversion/tasks"
LEGACY_CONVERT = "/api/image/convert"

#: 首页 FAQ 里那条「答案由能力目录现场生成」的问题（Home.tsx 的 FORMAT_QUESTION）。
#: 一字不差 —— 少了问号就定位不到那一块。
FORMAT_FAQ_QUESTION = "支持哪些文件格式？"

#: 对外文本里绝不能出现的东西（与 phase8 / phase9 / phase10a 同一张表）。
#: 对外文本里**一个都不许出现**的片段（§六十八 点名的那些，逐条落到这里）。
#:
#: * ``\\`` 一条就把 Windows 绝对路径整类挡掉（``..\\`` 也随之被挡）；
#: * 单个 ``..`` 太宽 —— 文件名里两个点很正常，所以只挡 ``../``
#:   这一种真正能穿越的写法；
#: * 四个 URL / 协议前缀（``http://`` / ``https://`` / ``file://`` /
#:   ``javascript:``）在**当前**的对外响应里一个都不出现（``download_url``
#:   与 ``preview_url`` 发的都是站内相对路径，不是绝对地址），所以能这么钉。
#:   哪天某个接口真的需要回一个外链，这里会因为「命中」而红 —— 那时候要
#:   改的是这份清单与它的理由，不是悄悄把断言删掉。
FORBIDDEN_FRAGMENTS = (
    "Traceback",
    'File "',
    "\\",
    "/tmp/",
    "AppData",
    "site-packages",
    "filetools_",
    "http://",
    "https://",
    "file://",
    "javascript:",
    "../",
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

#: 炸弹两档的名字与文案。文案**逐字**取自 ``tests/test_image_bomb.py``
#: 的 ``MESSAGE_BY_BAND`` —— 那份是 pytest 的真相，这里不另抄一份说法。
ERROR_BAND = "error"
WARNING_BAND = "warning"
MESSAGE_BY_BAND = {
    ERROR_BAND: "图片尺寸过大，超过服务器处理上限",
    WARNING_BAND: "图片像素总量过大，超过服务器处理上限",
}

#: ``FALLBACK_CONFIG`` 里**有意**与线上不同的键，以及各自的理由。
#:
#: 这张表是这条机械对账的闸门：D 段要求「除了这几个，其余每个键都必须与
#: 线上逐值相等」。将来再出现一处漂移，要么改前端、要么在这里写明理由
#: —— 两条路都得有人做一次决定，不会像上一版那样悄悄停在 80。
FALLBACK_PLACEHOLDERS = {
    # 字体是服务端**运行期探测本机**得到的，离线时无从得知。
    # 连不上后端时也排不了版，给一个「内置字体」比给一份本机可能
    # 根本没有的字体名单更诚实。
    "txt_fonts": "运行期探测，离线时无法得知",
    # 这三个都是「服务器装没装组件」的探测结果。离线时不能假装知道
    # 一个不知道的事实 —— 一律按可用渲染，真缺组件时由服务端返回
    # 503 和那句中文提示。
    "doc_conversion_available": "组件可用性由服务端探测，离线时不可知",
    "pdf_to_word_available": "组件可用性由服务端探测，离线时不可知",
    "ocr_available": "组件可用性由服务端探测，离线时不可知",
}

results: list[tuple[bool, str]] = []
console_errors: list[str] = []

#: 本轮跑过的每一个对外响应体，G 段统一过一遍「不许泄露」清单。
_seen_bodies: list[tuple[str, str]] = []

#: 当前段名，由 :func:`section` 维护。每一条检查点都记下自己属于哪一段，
#: 于是「A 段 26/26、E 段 12/12」这种数字是**数出来的**，不是报告作者
#: 事后回忆着写上去的 —— §六十五 要的是同一趟跑出来的数。
_current_section = "（未分段）"
_section_results: dict[str, list[bool]] = {}


def check(ok: bool, label: str) -> None:
    """记一条断言。

    **第一个参数必须是 ``bool``，不是「长得像布尔的东西」**：接口里的
    ``"true"``/``"false"`` 字符串、非空字典、非零整数都是真值，直接塞进来
    会得到一条**永远通过**的断言 —— 那是这个脚本最不该有的东西（§七十五
    「不要为了测试通过而降低断言」）。所以这里当场拦下，报出调用行号，
    而不是悄悄 ``bool()`` 一下把它变成一个假的绿灯。
    """
    if not isinstance(ok, bool):
        raise TypeError(
            f"check() 的第一个参数必须是 bool，收到 {type(ok).__name__}：{ok!r}"
            f"（{label}）"
        )
    results.append((ok, label))
    _section_results.setdefault(_current_section, []).append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {label}", flush=True)


def section(title: str) -> None:
    global _current_section
    _current_section = title.split("（")[0].split(" ")[0]
    _section_results.setdefault(_current_section, [])
    print(f"\n=== {title} ===", flush=True)


# ======================================================================
# 素材：一律用后端 venv 生成（它才有 Pillow / PyMuPDF）
# ======================================================================

#: 炸弹夹具的字节数上限（与 ``tests/test_image_bomb.py`` 同一条约束）。
#:
#: 作用是**防止夹具退化成真图**：哪天有人图省事把这里改成造一张
#: 20000×20000 的真图，整个脚本会变慢甚至 OOM，这条断言先一步拦下来。
_BOMB_FIXTURE_MAX_BYTES = 1024

_FIXTURE_CODE = r'''
import json, math, os, sys, zlib

from PIL import Image, ImageDraw, PngImagePlugin

out = sys.argv[1]
os.makedirs(out, exist_ok=True)

#: 与后端 config.settings.MAX_IMAGE_PIXELS 同源。**从后端读**，
#: 不在这里抄一个数字 —— 抄了就等于测的是抄来的那个上限。
sys.path.insert(0, os.path.abspath("."))
from config import settings

#: XMP 的标记串做成一眼能认出来的，免得断言只能靠长度比对 ——
#: 长度相等的两段不同字节会让「保留住了」变成假绿。
XMP = (
    b'<?xpacket begin="\xef\xbb\xbf"?>'
    b'<x:xmpmeta xmlns:x="adobe:ns:meta/">'
    b"<rdf:RDF><rdf:Description>FILETOOLS_XMP_CANARY</rdf:Description></rdf:RDF>"
    b'</x:xmpmeta><?xpacket end="w"?>'
)
PNG_XMP_KEY = "XML:com.adobe.xmp"


def png_declaring(width, height):
    """一张**真实**的 PNG，但 IHDR 里声明成 width x height。

    做法与 tests/test_image_bomb.py 逐字一致。改完宽高必须重算 IHDR 的
    CRC（覆盖文件偏移 12..29，CRC 存在 29..33），否则 Pillow 判 CRC 失败、
    报「文件损坏」—— 那就测错了东西。
    """
    import io
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (10, 20, 30)).save(buf, format="PNG")
    data = bytearray(buf.getvalue())
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "Pillow 写出来的不是 PNG？"
    assert data[12:16] == b"IHDR", "第一个 chunk 不是 IHDR？"
    data[16:20] = width.to_bytes(4, "big")
    data[20:24] = height.to_bytes(4, "big")
    data[29:33] = zlib.crc32(bytes(data[12:29])).to_bytes(4, "big")
    return bytes(data)


# 两档的边长**从配置算**：isqrt 向下取整，+1 之后平方一定大于目标，
# 正好落进要测的那一档。
limit = settings.MAX_IMAGE_PIXELS
warning_side = math.isqrt(limit) + 1
error_side = math.isqrt(2 * limit) + 1
for name, side in (("warning.png", warning_side), ("error.png", error_side)):
    with open(os.path.join(out, name), "wb") as fh:
        fh.write(png_declaring(side, side))


# 合法的大图：长边 6000 < MAX_IMAGE_EDGE，像素 6000x1000 = 600 万，
# 远低于上限。它必须**转得成** —— 炸弹防线不能变成「谁大拦谁」。
Image.new("RGB", (6000, 1000), (30, 120, 200)).save(
    os.path.join(out, "legal_big.png"), format="PNG"
)


# 带 XMP 的 PNG：源里有 XMP 才能验「保留 / 丢不掉」。
# **故意用 PNG 当载体** —— 它的 XMP 存在 iTXt 文本块里（读回来是 str），
# 与 JPEG / TIFF 的二进制通道不是一回事，载体选错了就测不出键名/类型
# 对不上的那类问题。
meta = PngImagePlugin.PngInfo()
meta.add_text(PNG_XMP_KEY, XMP.decode("latin-1"))
Image.new("RGB", (120, 90), (12, 34, 56)).save(
    os.path.join(out, "xmp.png"), format="PNG", pnginfo=meta
)

# 同一份 XMP 再存一份 **JPEG**。**目标格式里没有自转换**（png 转不了 png），
# 所以要验「XMP 能不能进 PNG」就必须换一个源 —— 拿 png 源去转 png 会得到
# UNSUPPORTED_CONVERSION，那是脚本写错了，不是系统坏了。
Image.new("RGB", (120, 90), (12, 34, 56)).save(
    os.path.join(out, "xmp.jpg"), format="JPEG", quality=95, xmp=XMP
)


def tagged():
    exif = Image.Exif()
    exif[0x010F] = "FileToolsCamera"     # Make
    exif[0x0110] = "FT-10C"              # Model
    return exif


# 带 EXIF 的 TIFF 源：第十阶段 C 之前 TIFF 的拍摄信息**整段丢掉**
# （Pillow 不把它放进 info["exif"]，只从 getexif() 暴露）。
Image.new("RGB", (140, 100), (200, 90, 40)).save(
    os.path.join(out, "tiff_exif.tiff"), format="TIFF", exif=tagged()
)


# 恶意 SVG：脚本 + 本地文件引用 + foreignObject + 外部图片。
# 契约是**净化后继续**（不是拒绝），所以它必须转得成。
# 里面那三个标记串用来验证产物里一个都没留下。
evil = """<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink"
     width="200" height="150" viewBox="0 0 200 150">
  <script>fetch('http://127.0.0.1:9/FILETOOLS_EVIL_SCRIPT')</script>
  <foreignObject width="100" height="100">
    <body xmlns="http://www.w3.org/1999/xhtml">FILETOOLS_EVIL_FOREIGN</body>
  </foreignObject>
  <image x="0" y="0" width="100" height="100"
         xlink:href="file:///etc/passwd" href="http://127.0.0.1:9/FILETOOLS_EVIL_REMOTE"/>
  <rect x="0" y="0" width="200" height="150" fill="#3366cc"/>
</svg>
"""
with open(os.path.join(out, "evil.svg"), "w", encoding="utf-8") as fh:
    fh.write(evil)


# 两份**同名**的 PDF：验证 ZIP 内的成员不会互相覆盖。
# PyMuPDF 在 venv 里，用它造真 PDF（不是改名的文本 —— 内容校验会拦下那个）。
#
# 用 ``import pymupdf`` 而不是 ``import fitz``：后者会往 **stdout** 打一行
# 弃用提示，把下面那句 JSON 顶到第二行。产物本身没问题，但父进程按整段
# stdout 解析就会炸 —— 这是真踩过的坑，不是假想。
import pymupdf
for index, pages in ((1, 1), (2, 2)):
    doc = pymupdf.open()
    for page_no in range(pages):
        page = doc.new_page(width=200, height=200)
        page.insert_text((40, 100), f"P{index}-{page_no + 1}", fontsize=18)
    doc.save(os.path.join(out, f"report_{index}.pdf"))
    doc.close()


# 假扩展名：**内容是货真价实的 PDF**，名字却叫 .png。
#
# 这里必须是真 PDF，不能手写一段 ``%PDF-1.4 ... %%EOF`` 的壳：那种东西
# 连 PyMuPDF 都打不开，拿它当反证（「同一份字节换个名字当 PDF 传就该被收下」）
# 会得到一个 400，反证自然不成立 —— 分不清「被内容校验拦下」和「这压根不是
# PDF」。直接用上面那份真 PDF 的字节。
with open(os.path.join(out, "report_1.pdf"), "rb") as fh:
    real_pdf = fh.read()
with open(os.path.join(out, "fake.png"), "wb") as fh:
    fh.write(real_pdf)


# 几何素材：**只有左上角**有一块红，其余全白。
# 旋转 / 翻转 / 裁剪都会把它挪到别的角上 —— 「转了吗、往哪转的」一眼看
# 得出来，比只比尺寸强得多（§六十七：不能只检查「文件存在」）。
# 块是 1/4 见方（50 × 37），下面的采样点取 12%，稳稳在块内；取象限中心
# (25%, 25%) 则恰好压在块边缘那一像素上，差一像素就翻。
canvas = Image.new("RGB", (200, 150), (255, 255, 255))
ImageDraw.Draw(canvas).rectangle([0, 0, 50, 37], fill=(200, 30, 30))
canvas.save(os.path.join(out, "corner.png"), format="PNG")


# EXIF 方向 6（要顺时针转 90° 才正）：存储像素是 40×20 的**横**图，
# 四角颜色互不相同 —— 摆正之后必须是 20×40，且颜色按方向 6 重排成
# 左上=蓝 右上=红 左下=白 右下=绿（与 tests/test_image_orientation.py
# 的 EXPECTED[6] 同源）。质量拉满 + 4:4:4：下面比的是**具体颜色**，
# 色度抽样会把象限边界糊成过渡色。
stored = Image.new("RGB", (40, 20), (255, 0, 0))
stored.paste((0, 255, 0), (20, 0, 40, 10))
stored.paste((0, 0, 255), (0, 10, 20, 20))
stored.paste((255, 255, 255), (20, 10, 40, 20))
oriented_exif = stored.getexif()
oriented_exif[0x0112] = 6
stored.save(os.path.join(out, "oriented.jpg"), format="JPEG", quality=100,
            subsampling=0, exif=oriented_exif)


# 噪点图：按目标体积压缩要**真搜得动**。纯色图一压就到极限，搜不出
# 「质量在变、必要时还在缩尺寸」的过程 —— 那样测出来的「达标」是假的。
grain = Image.effect_noise((1600, 1000), 48).convert("L")
Image.merge("RGB", (grain, grain, grain)).save(os.path.join(out, "grain.png"), format="PNG")


#: 带这个前缀的那一行才是给父进程看的。**不按整段 stdout 解析** ——
#: 任何一个库往 stdout 打一行提示都会把 JSON 顶下去（PyMuPDF 的
#: ``import fitz`` 就这么干过）。前缀让「多出来的那几行」变得无害，
#: 同时又不像「取最后一行」那样会把真正的输出错误地吞掉。
JSON_LINE = "FILETOOLS_FIXTURE_JSON "
print(JSON_LINE + json.dumps({
    "warning_side": warning_side,
    "error_side": error_side,
    "max_image_pixels": limit,
    "max_image_edge": settings.MAX_IMAGE_EDGE,
}))
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

#: 炸弹两档的中间结果，由 :func:`build_fixtures` 填。
BOMB_META: dict = {}


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


#: 只认带前缀的那一行，见 ``_FIXTURE_CODE`` 里的 ``JSON_LINE``。
JSON_LINE = "FILETOOLS_FIXTURE_JSON "


def _json_line(stdout: str) -> dict:
    """从子进程的 stdout 里取出带前缀的那一行 JSON。"""
    for line in stdout.splitlines():
        if line.startswith(JSON_LINE):
            return json.loads(line[len(JSON_LINE):])
    raise SystemExit(f"子进程没有打印结果行：\n{stdout}")


def build_fixtures() -> None:
    try:
        BOMB_META.update(_json_line(_run_backend_python(_FIXTURE_CODE, SAMPLES)))
    except RuntimeError as exc:
        raise SystemExit(f"生成测试素材失败：\n{exc}") from exc


# ----------------------------------------------------------------------
# 打开产物。**这才是「真的转出来了」的证据**
# ----------------------------------------------------------------------

#: 一次读出「是什么格式 / 多大 / 有没有 EXIF / 有没有 XMP」。
#:
#: XMP 那两个键都要看：PNG 的插件放在 iTXt 的 ``XML:com.adobe.xmp`` 下
#: （回读是 ``str``），其余格式统一放 ``xmp``（回读是 ``bytes``）。
#: 只看一个键，「PNG 源 → 任何目标」这条路上 XMP 会被当成不存在。
_INSPECT = r'''
import json, sys
from PIL import Image

XMP_KEYS = ("xmp", "XML:com.adobe.xmp")


def read(fmt_value):
    if isinstance(fmt_value, bytes) and fmt_value:
        return fmt_value.decode("latin-1", errors="replace")
    if isinstance(fmt_value, str):
        return fmt_value
    return ""


with Image.open(sys.argv[1]) as img:
    xmp = ""
    for key in XMP_KEYS:
        xmp = read(img.info.get(key))
        if xmp:
            break
    exif = img.getexif()
    print(json.dumps({
        "format": img.format,
        "width": img.size[0],
        "height": img.size[1],
        "xmp": xmp,
        "has_exif": len(exif) > 0,
        "exif_make": str(exif.get(0x010F) or ""),
        "exif_model": str(exif.get(0x0110) or ""),
    }))
'''


#: 只问「这个文件**声明**了多大的画布」，不碰像素。
#:
#: 不能用 :data:`_INSPECT`：它要 ``getexif()``，而 Pillow 的
#: ``getexif()`` 会先 ``load()`` —— 对着一张炸弹图就是真的去解码
#: 1.6 亿像素，于是拿到「图片文件被截断」而不是它声明的尺寸。
#: ``Image.open`` 是惰性的，``.size`` 直接来自文件头，所以这条探针
#: 既便宜又正是要测的那个事实（§十三：夹具得真的是「字节小、画布大」）。
#:
#: 顺手把 ``DecompressionBombWarning`` 滤掉：**下档炸弹必然会让 Pillow
#: 发这条警告**，那是整套防线依赖的事实，不是需要清理的噪音。
_DECLARED_SIZE = r'''
import json, sys, warnings

warnings.simplefilter("ignore")

from PIL import Image

with Image.open(sys.argv[1]) as img:
    print(json.dumps({"format": img.format, "width": img.size[0], "height": img.size[1]}))
'''


def inspect_file(path: pathlib.Path) -> dict:
    return json.loads(_run_backend_python(_INSPECT, path))


def declared_size(data: bytes) -> dict:
    """读文件头里声明的尺寸，不解码像素。"""
    handle, name = tempfile.mkstemp(suffix=".png", dir=str(WORK))
    os.close(handle)
    path = pathlib.Path(name)
    path.write_bytes(data)
    try:
        return json.loads(_run_backend_python(_DECLARED_SIZE, path))
    finally:
        path.unlink(missing_ok=True)


def inspect_bytes(data: bytes) -> dict:
    """同一段代码，但走字节 —— 省掉一次落盘。"""
    handle, name = tempfile.mkstemp(suffix=".bin", dir=str(WORK))
    os.close(handle)
    path = pathlib.Path(name)
    path.write_bytes(data)
    try:
        return inspect_file(path)
    finally:
        path.unlink(missing_ok=True)


#: 「几何类选项到底有没有落到像素上」需要的是**颜色**，不是尺寸。
#: ``_INSPECT`` 读的是格式/尺寸/XMP/EXIF，回答不了「红块转到哪个角了」。
#:
#: 采样点取 12% / 88% 而不是象限中心 25% / 75%：``corner.png`` 的红块占
#: 左上 1/4（0–50 × 0–37），12% 落在块内；象限中心恰好压在块边缘那一
#: 个像素上，差一像素就会翻。``oriented.jpg`` 摆正后是 20×40，12% / 88%
#: 也分别落在左右两列、上下两行之内。
_PIXELS = r'''
import json, sys
from PIL import Image

with Image.open(sys.argv[1]) as img:
    width, height = img.size
    rgb = img.convert("RGB")
    exif = img.getexif()
    print(json.dumps({
        "format": img.format,
        "width": width,
        "height": height,
        "corners": [
            list(rgb.getpixel((round(width * 0.12), round(height * 0.12)))),
            list(rgb.getpixel((round(width * 0.88), round(height * 0.12)))),
            list(rgb.getpixel((round(width * 0.12), round(height * 0.88)))),
            list(rgb.getpixel((round(width * 0.88), round(height * 0.88)))),
        ],
        "exif_orientation": exif.get(0x0112),
    }))
'''


def inspect_pixels(data: bytes) -> dict:
    """打开产物的**像素**：四角颜色 + EXIF 方向标记。

    ``data`` 为空时回一份**空壳**而不是抛异常：转换失败时调用方已经写了
    一条失败的检查点，再抛异常只会把后面所有检查点一起带走 —— 一轮跑完
    只能看见第一个错，那是最难查的一种失败。空壳里 ``corners`` 是四个
    ``None``，下面每一条断言都会各自如实报错。
    """
    if not data:
        return {"format": None, "width": 0, "height": 0,
                "corners": [None, None, None, None], "exif_orientation": None}
    handle, name = tempfile.mkstemp(suffix=".bin", dir=str(WORK))
    os.close(handle)
    path = pathlib.Path(name)
    path.write_bytes(data)
    try:
        return json.loads(_run_backend_python(_PIXELS, path))
    finally:
        path.unlink(missing_ok=True)


#: 四角颜色的期望值。与 ``verify_phase10a.py`` / ``tests/test_image_orientation.py``
#: 同源 —— 三处说的是同一件事，改一处就得三处一起改。
RED, GREEN, BLUE, WHITE = (255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 255)
CORNER_RED, CORNER_WHITE = (200, 30, 30), (255, 255, 255)


def close(pixel: list[int] | None, want: tuple[int, int, int], tolerance: int = 12) -> bool:
    """有损格式（JPEG / WEBP）不可能逐位还原，按容差比。

    容差 12 而不是 2：采样点在纯色块内部，离边界很远，量化误差到不了这里，
    但不同版本的编码器在色度上会有几个灰阶的出入，写死 ±2 会变成一个随
    库版本飘的假失败。
    """
    if pixel is None:  # 产物是空的（转换就失败了），比不了
        return False
    return all(abs(a - b) <= tolerance for a, b in zip(pixel, want))


# ======================================================================
# HTTP：极简 multipart。**不引第三方库** —— 驱动脚本跑在系统 Python 上，
# 那里没有 httpx/requests（见脚本开头「解释器分工」）。
# ======================================================================

def _multipart(
    fields: list[tuple[str, str]],
    files: list[tuple[str, str, bytes, str]],
) -> tuple[bytes, str]:
    boundary = "----filetoolsverify10" + uuid.uuid4().hex
    body = bytearray()
    for name, value in fields:
        body += (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n"
        ).encode("utf-8")
    for name, filename, content, mime in files:
        body += (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
            f"Content-Type: {mime}\r\n\r\n"
        ).encode("utf-8")
        body += content
        body += b"\r\n"
    body += f"--{boundary}--\r\n".encode("utf-8")
    return bytes(body), f"multipart/form-data; boundary={boundary}"


def request(
    path: str,
    *,
    method: str = "GET",
    fields: list[tuple[str, str]] | None = None,
    files: list[tuple[str, str, bytes, str]] | None = None,
    timeout: int = 300,
) -> tuple[int, dict | bytes, str]:
    """发一个请求，返回 ``(状态码, 解析后的正文或原始字节, content-type)``。

    4xx/5xx **不抛异常** —— 这个脚本有相当一部分断言就是「它必须报错」，
    而且报错时的那句中文正是要逐字比对的东西。
    """
    url = f"{BASE}{path}"
    headers = {"Accept": "*/*"}
    data = None
    if fields is not None or files is not None:
        data, content_type = _multipart(fields or [], files or [])
        headers["Content-Type"] = content_type
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read()
            content_type = response.headers.get("content-type", "")
            status = response.status
    except urllib.error.HTTPError as error:
        raw = error.read()
        content_type = error.headers.get("content-type", "")
        status = error.code

    if "json" in content_type:
        try:
            return status, json.loads(raw), content_type
        except json.JSONDecodeError:
            pass
    return status, raw, content_type


def get_json(path: str) -> dict:
    status, body, _ = request(path)
    if status != 200 or not isinstance(body, dict):
        raise SystemExit(f"GET {path} 失败：{status} {body!r}")
    return body


def record(label: str, body: object) -> None:
    """把一份对外响应收进 G 段的「不许泄露」检查。"""
    if isinstance(body, bytes):
        _seen_bodies.append((label, body.decode("utf-8", errors="replace")))
    else:
        _seen_bodies.append((label, json.dumps(body, ensure_ascii=False)))


# ----------------------------------------------------------------------
# 统一转换接口：提交（202）→ 轮询 → 终态快照
# ----------------------------------------------------------------------

def submit_unified(
    *,
    filename: str,
    content: bytes,
    target: str,
    mime: str = "application/octet-stream",
    extra: list[tuple[str, str]] | None = None,
    label: str | None = None,
) -> dict:
    """提交一个文件到统一转换接口，等到终态，返回快照。"""
    fields = [("target_type", target), *(extra or [])]
    status, body, _ = request(
        UNIFIED_SUBMIT,
        method="POST",
        fields=fields,
        files=[("files", filename, content, mime)],
    )
    record(label or f"POST {UNIFIED_SUBMIT} ({filename} → {target})", body)
    if status != 202:
        raise SystemExit(f"提交 {filename} 没拿到 202：{status} {body!r}")
    assert isinstance(body, dict)
    return wait_batch(body["batch_id"])


def wait_batch(batch_id: str, timeout: float = 300.0) -> dict:
    """轮询到整批终态。"""
    deadline = time.monotonic() + timeout
    while True:
        snapshot = get_json(f"{UNIFIED_SUBMIT}/{batch_id}")
        if snapshot["status"] in ("completed", "failed", "cancelled"):
            return snapshot
        if time.monotonic() > deadline:
            raise SystemExit(f"批次 {batch_id} 超时未结束：{snapshot['status']}")
        time.sleep(0.3)


# ----------------------------------------------------------------------
# 旧接口（第四–八阶段那批）：提交 → 202 → ``GET /api/tasks/{group_id}``
#
# **旧接口也是异步的。** 「整批失败时同步抛 400」这句话只对 pytest 的
# ``run_task`` 成立 —— 那是它把终态**翻译**回一个状态码（见
# ``tests/conftest.py`` 的 ``status_for_code``），HTTP 上它同样是 202。
# 这里如实按异步验：202 收下、终态是 failed、错误码与文案在
# ``snapshot["error"]`` 里。别把测试替身的形状当成线上协议。
# ----------------------------------------------------------------------

def wait_group(group_id: str, timeout: float = 300.0) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        snapshot = get_json(f"/api/tasks/{group_id}")
        if snapshot["state"] in ("done", "failed", "cancelled"):
            return snapshot
        if time.monotonic() > deadline:
            raise SystemExit(f"任务组 {group_id} 超时未结束：{snapshot['state']}")
        time.sleep(0.3)


def submit_legacy(
    path: str,
    *,
    fields: list[tuple[str, str]] | None = None,
    files: list[tuple[str, str, bytes, str]] | None = None,
    label: str,
) -> tuple[int, dict, dict]:
    """提交一个旧接口任务，返回 ``(提交状态码, 终态快照, 提交响应体)``。"""
    status, created, _ = request(path, method="POST", fields=fields, files=files)
    record(label, created)
    if status != 202 or not isinstance(created, dict):
        return status, {}, created if isinstance(created, dict) else {}
    return status, wait_group(created["group_id"]), created


def task_of(snapshot: dict, index: int = 0) -> dict:
    return next(item for item in snapshot["tasks"] if item["index"] == index)


def fetch_result(snapshot: dict, index: int = 0) -> tuple[bytes, str, dict]:
    """下载某一项的结果，返回 ``(字节, content-type, 该项的 result 段)``。"""
    task = task_of(snapshot, index)
    if task["status"] != "completed" or not task["result"]:
        raise SystemExit(f"第 {index} 项没成功：{task['status']} {task['error_message']!r}")
    url = task["result"]["download_url"]
    status, body, content_type = request(url)
    if status != 200 or not isinstance(body, bytes):
        raise SystemExit(f"下载 {url} 失败：{status} {body!r}")
    return body, content_type, task["result"]


# ----------------------------------------------------------------------
# 能力目录 / 配置
# ----------------------------------------------------------------------

class Catalogue:
    """能力目录的薄包装 —— 所有「源 → 目标」都从这里查，没有一处手写。"""

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

    def pairs(self) -> set[tuple[str, str]]:
        return {
            (source, target)
            for source, targets in self.data["matrix"].items()
            for target in targets
        }

    def spec(self, conversion_id: str, key: str) -> dict:
        for item in self.entry[conversion_id]["options_schema"]["items"]:
            if item["key"] == key:
                return item
        raise KeyError(f"{conversion_id} 没有参数 {key}")


def fallback_config() -> dict:
    """把 ``FALLBACK_CONFIG`` 从 TS 字面量里**机械地**取出来。

    不引 TypeScript 解析器（那是新依赖），也不人肉抄一遍（那就不是对账了）。
    做法是把这段**纯字面量**翻译成 Python 字面量再 ``literal_eval``：

    1. 去掉 ``//`` 行注释；
    2. 只取 ``export const FALLBACK_CONFIG ... = {`` 到行首 ``}`` 之间的正文；
    3. 把 ``50 * 1024 * 1024`` 这类算术先算成数字；
    4. 把字符串**先摘出去**（换成占位符），再给裸键名加引号，
       然后把字符串放回去 —— 顺序不能反：嵌在数组里的对象长这样
       ``[{ value: 'china-s', label: '内置字体' }]``，键名前面**没有换行**，
       按行首匹配会漏掉它；而全局匹配又可能碰到字符串里的冒号。
       摘出去再全局匹配，两边都躲开了。
    5. ``true``/``false`` → ``True``/``False``；``ast.literal_eval``。

    只在**这份对象是纯字面量**时成立。哪天有人往里塞一个函数调用，
    这里会抛异常而不是悄悄跳过 —— 那正是想要的行为：兜底配置一旦变成
    算出来的，D 段的对账就必须重新设计，不能装作没看见。
    """
    source = (ROOT / "frontend" / "src" / "hooks" / "useServerConfig.ts").read_text(
        encoding="utf-8"
    )
    source = re.sub(r"//[^\n]*", "", source)
    match = re.search(
        r"export const FALLBACK_CONFIG[^=]*=\s*\{(?P<body>.*?)\n\}",
        source,
        re.DOTALL,
    )
    if match is None:
        raise SystemExit("没在 useServerConfig.ts 里找到 FALLBACK_CONFIG 字面量")
    body = match.group("body")

    def _product(found: re.Match[str]) -> str:
        return str(eval(found.group(0)))  # noqa: S307 - 只由本文件的字面量喂进来

    body = re.sub(r"\d+(?:\s*\*\s*\d+)+", _product, body)

    # 把单引号字符串摘出去（占位符里不含引号、冒号、逗号、花括号，
    # 所以接下来的两步都碰不到它们）。
    literals: list[str] = []

    def _stash(found: re.Match[str]) -> str:
        literals.append(found.group(0))
        return f"@@{len(literals) - 1}@@"

    body = re.sub(r"'(?:[^'\\\n]|\\.)*'", _stash, body)
    body = re.sub(r"\btrue\b", "True", body)
    body = re.sub(r"\bfalse\b", "False", body)
    body = re.sub(r"([A-Za-z_][A-Za-z0-9_]*)\s*:", r'"\1":', body)
    for index, literal in enumerate(literals):
        body = body.replace(f"@@{index}@@", literal)
    return ast.literal_eval("{" + body + "}")


# ======================================================================
# 浏览器小工具（与 phase9 / phase10a 同一套，减少「各有一套点法」的漂移）
# ======================================================================

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


def upload_raw(page: Page, name: str, content: bytes, mime: str) -> None:
    page.locator("input[type=file]").first.set_input_files(
        [{"name": name, "mimeType": mime, "buffer": content}]
    )
    page.wait_for_timeout(500)


def pick_target(page: Page, value: str) -> None:
    """选目标格式。单选框是 sr-only 的，真实用户点的是外层 label。

    ``name`` 是 ``conversion-target-<groupId>``，组 id 由前端生成，
    所以按前缀匹配 —— 不去猜一个测试根本不掌握的值。
    """
    label = page.locator(
        f"label:has(input[name^='conversion-target-'][value='{value}'])"
    ).first
    label.evaluate("el => el.scrollIntoView({block: 'center'})")
    page.wait_for_timeout(120)
    label.click()
    page.wait_for_timeout(250)


def group_card(page: Page, filename: str):
    return page.locator("section.card").filter(has_text=filename).first


def wait_group_settled(page: Page, filename: str, timeout: int = 240_000) -> str:
    """等这一组跑完 —— 成功或失败都算跑完。

    成功时卡片上会出现下载按钮，失败时会出现那句错误说明。
    **两种都要等**：这个脚本里有一组素材的**预期就是失败**（炸弹图），
    只等下载按钮会在那里空等到超时。
    """
    card = group_card(page, filename)
    card.locator(
        "button, p, span",
        has_text=re.compile("下载结果|打包下载|图片尺寸过大|图片像素总量过大|失败"),
    ).first.wait_for(timeout=timeout)
    page.wait_for_timeout(400)
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


# ======================================================================
# A 段：炸弹防线（§十三–§十五）
# ======================================================================

def section_a(page: Page) -> None:
    section("A 炸弹防线（真实接口 + 浏览器）")

    error_bomb = (SAMPLES / "error.png").read_bytes()
    warning_bomb = (SAMPLES / "warning.png").read_bytes()

    # A1 夹具本身 —— 先证明测的是炸弹，不是一张真的大图。
    check(
        max(len(error_bomb), len(warning_bomb)) <= _BOMB_FIXTURE_MAX_BYTES,
        f"炸弹夹具只有 {len(error_bomb)} / {len(warning_bomb)} 字节"
        f"（上限 {_BOMB_FIXTURE_MAX_BYTES}，防止夹具退化成真图）",
    )
    declared = declared_size(error_bomb)
    check(
        declared["width"] * declared["height"] >= 2 * BOMB_META["max_image_pixels"],
        f"上档夹具声明的画布是 {declared['width']}×{declared['height']}"
        f"（{declared['width'] * declared['height']} 像素），"
        f"像素上限 {BOMB_META['max_image_pixels']}",
    )
    warning_declared = declared_size(warning_bomb)
    check(
        BOMB_META["max_image_pixels"]
        < warning_declared["width"] * warning_declared["height"]
        < 2 * BOMB_META["max_image_pixels"],
        f"下档夹具声明 {warning_declared['width']}×{warning_declared['height']}"
        f"（{warning_declared['width'] * warning_declared['height']} 像素），"
        "正好落在「超过上限、不到两倍」那一档",
    )

    # A2 / A3 两档都走完整条统一接口 —— §十四 点名要验的就是「真实接口」。
    for band, bomb in ((ERROR_BAND, error_bomb), (WARNING_BAND, warning_bomb)):
        snapshot = submit_unified(
            filename="bomb.png",
            content=bomb,
            target="jpg",
            mime="image/png",
            label=f"POST {UNIFIED_SUBMIT} (炸弹 · {band} 档)",
        )
        task = task_of(snapshot)
        check(
            snapshot["status"] == "failed" and task["status"] == "failed",
            f"{band} 档炸弹让这一批失败（批次 {snapshot['status']}）",
        )
        check(
            task["error_code"] == "INVALID_REQUEST",
            f"{band} 档给出业务错误码，不是 500（{task['error_code']}）",
        )
        check(
            task["error_message"] == MESSAGE_BY_BAND[band],
            f"{band} 档文案逐字：{task['error_message']!r}",
        )
        check(
            task["result"] is None and task["can_retry"] is False,
            f"{band} 档没有产物、也不给「重试一下就会好」的假希望",
        )

    # 下档那条**必须**报「像素总量过大」，不能报「文件损坏」。
    # 它测的是**顺序**：提交时的校验跑在 worker 解码之前。顺序反过来的话，
    # Pillow 会在 load() 时先以「图片文件被截断」失败 —— 用户会以为自己
    # 传了一份坏文件，重新导出一遍然后再失败一次。
    warning_task = task_of(
        submit_unified(
            filename="bomb.png", content=warning_bomb, target="jpg", mime="image/png"
        )
    )
    check(
        "损坏" not in (warning_task["error_message"] or ""),
        "下档报的是「像素总量过大」，不是「文件损坏」（校验确实跑在解码前面）",
    )

    # A4 旧接口不许绕过 —— §六十九 说旧接口不能删，但它必须与新接口
    #    共用同一套校验，否则「哪条路能塞炸弹」就成了只有攻击者知道的分叉。
    status, snapshot, _created = submit_legacy(
        LEGACY_CONVERT,
        fields=[("target_format", "jpg")],
        files=[("files", "bomb.png", error_bomb, "image/png")],
        label=f"POST {LEGACY_CONVERT} (炸弹)",
    )
    check(status == 202, f"旧 /api/image/convert 收下这一批（202，拿到 {status}）")
    error = snapshot.get("error") or {}
    check(
        snapshot.get("state") == "failed",
        f"旧接口这一批的终态是 failed（拿到 {snapshot.get('state')!r}）",
    )
    check(
        error.get("message") == MESSAGE_BY_BAND[ERROR_BAND],
        f"旧接口给出**同一句话**：{error.get('message')!r}",
    )
    check(
        error.get("code") == "INVALID_REQUEST",
        f"旧接口的错误码也是业务码（{error.get('code')!r}）",
    )

    # A5 合法的大图必须还转得成 —— 防线不能变成「谁大拦谁」（§六十九）。
    big = (SAMPLES / "legal_big.png").read_bytes()
    snapshot = submit_unified(
        filename="legal_big.png", content=big, target="jpg", mime="image/png"
    )
    big_task = task_of(snapshot)
    check(
        big_task["status"] == "completed",
        f"6000×1000 的合法大图照常转换（{big_task['status']} "
        f"{big_task['error_message']!r}）",
    )
    if big_task["status"] == "completed":
        data, _content_type, result = fetch_result(snapshot)
        opened = inspect_bytes(data)
        check(
            opened["format"] == "JPEG" and opened["width"] == 6000,
            f"它的产物是一张真的 6000 宽的 JPEG（{opened['format']} "
            f"{opened['width']}×{opened['height']}）",
        )

    # A6 **浏览器**里用户真的看得到那句话（§十五 至少一条浏览器检查）。
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(f"{BASE}{ROUTE}", wait_until="networkidle")
    upload_raw(page, "bomb.png", error_bomb, "image/png")
    pick_target(page, "jpg")
    page.locator("button", has_text=re.compile("^开始转换这")).first.click()
    text = wait_group_settled(page, "bomb.png")
    check(
        MESSAGE_BY_BAND[ERROR_BAND] in text,
        "浏览器里这条错误是可读的中文，与接口逐字一致",
    )
    for fragment in ("Traceback", "site-packages", "PIL", "\\"):
        check(fragment not in text, f"炸弹的错误说明里没有 {fragment!r}")
    # 失败项不得给出下载按钮 —— 没有产物就没有下载
    check(
        group_card(page, "bomb.png")
        .locator("button", has_text=re.compile("下载结果|打包下载"))
        .count()
        == 0,
        "失败的那一组没有下载按钮",
    )


# ======================================================================
# B 段：元数据（§二十四·3 / §二十六）
# ======================================================================

def section_b() -> None:
    section("B 元数据：默认值、XMP 往返、TIFF 拍摄信息")

    catalogue = Catalogue(get_json(CAPABILITIES_URL))

    # B1 统一接口与旧页面的默认**必须**是现在这个样子。
    #    「统一保留、旧页面清除」这条差异是第三阶段就定下的兼容决定，
    #    第十阶段 C 把它显式对账过；这里再钉一次，免得哪天有人「顺手统一」。
    xmp_png = (SAMPLES / "xmp.png").read_bytes()

    snapshot = submit_unified(
        filename="xmp.png", content=xmp_png, target="jpg", mime="image/png"
    )
    data, _ct, _result = fetch_result(snapshot)
    kept = inspect_bytes(data)
    check(
        "FILETOOLS_XMP_CANARY" in kept["xmp"],
        "统一接口默认**保留** XMP（第十阶段 C 对账后的既定行为）",
    )

    status, snapshot, _created = submit_legacy(
        LEGACY_CONVERT,
        fields=[("target_format", "jpg")],
        files=[("files", "xmp.png", xmp_png, "image/png")],
        label=f"POST {LEGACY_CONVERT} (xmp.png)",
    )
    legacy_result = snapshot.get("result") or {}
    check(
        snapshot.get("state") == "done" and bool(legacy_result.get("items")),
        f"旧 /api/image/convert 对带 XMP 的 PNG 仍然成功（{snapshot.get('state')!r}）",
    )
    if legacy_result.get("items"):
        url = legacy_result["items"][0]["preview_url"] or legacy_result["download_url"]
        _s, raw, _ct = request(url)
        if isinstance(raw, bytes):
            dropped = inspect_bytes(raw)
            check(
                "FILETOOLS_XMP_CANARY" not in dropped["xmp"],
                "旧页面默认**清除**元数据 —— 这条差异是有意的，不是漏改",
            )

    # B2 XMP 在每一种「装得下」的目标格式上真的往返。
    #    ``XMP_FORMATS`` 用的是**编码层的内部名**（jpeg），线上词汇是 ``jpg``
    #    —— 两者不是一回事，能力目录里只有后者。所以逐个先在目录里确认，
    #    不在就跳过并如实记一条：拿内部名当线上名去提交会得到
    #    ``UNSUPPORTED_CONVERSION``，那是脚本写错了，不是系统坏了。
    xmp_jpg = (SAMPLES / "xmp.jpg").read_bytes()
    for source_name, source_bytes, source_mime, target in (
        ("xmp.jpg", xmp_jpg, "image/jpeg", "png"),
        ("xmp.png", xmp_png, "image/png", "jpg"),
        ("xmp.png", xmp_png, "image/png", "webp"),
        ("xmp.png", xmp_png, "image/png", "tiff"),
    ):
        source_type = "jpg" if source_mime == "image/jpeg" else "png"
        if not catalogue.has(source_type, target):
            check(False, f"能力目录里没有 {source_type} → {target}，这条检查点写错了")
            continue
        snapshot = submit_unified(
            filename=source_name, content=source_bytes, target=target, mime=source_mime
        )
        task = task_of(snapshot)
        if task["status"] != "completed":
            check(False, f"XMP → {target}：转换没成功（{task['error_message']!r}）")
            continue
        data, _ct, _result = fetch_result(snapshot)
        got = inspect_bytes(data)
        check(
            "FILETOOLS_XMP_CANARY" in got["xmp"],
            f"XMP → {target}：保留住了（拿到 {got['xmp'][:40]!r}…）",
        )

    # B3 装不下的格式**必须明说**，不许静默丢掉。
    #    静默是最坏的一种：用户选了「保留」，看到「转换成功」，
    #    而版权信息已经没了，没有任何一句话告诉他为什么。
    for target in ("bmp", "gif", "ico"):
        if not catalogue.has("png", target):
            continue
        snapshot = submit_unified(
            filename="xmp.png", content=xmp_png, target=target, mime="image/png"
        )
        task = task_of(snapshot)
        if task["status"] != "completed":
            check(False, f"XMP → {target}：转换没成功（{task['error_message']!r}）")
            continue
        data, _ct, result = fetch_result(snapshot)
        got = inspect_bytes(data)
        notes = result.get("notes") or []
        check(
            "FILETOOLS_XMP_CANARY" not in got["xmp"],
            f"XMP → {target}：确实装不下（前提成立）",
        )
        # 断在 ``notes`` 这一个字段上，不是整段 JSON —— 整段匹配的话，
        # 随便哪个字段里出现「XMP」两个字都会让这条变成假绿。
        check(
            any("XMP" in note and "未能保留" in note for note in notes),
            f"XMP → {target}：装不下这件事被**明说**了（notes={notes}）",
        )

    # B4 清除就得真的清干净 —— 含源格式自己（自转换不存在，见上面 B2）。
    for source_name, source_bytes, source_mime, target in (
        ("xmp.jpg", xmp_jpg, "image/jpeg", "png"),
        ("xmp.png", xmp_png, "image/png", "jpg"),
        ("xmp.png", xmp_png, "image/png", "tiff"),
    ):
        snapshot = submit_unified(
            filename=source_name,
            content=source_bytes,
            target=target,
            mime=source_mime,
            extra=[("options", json.dumps({"metadata": "remove"}))],
        )
        task = task_of(snapshot)
        if task["status"] != "completed":
            check(
                False,
                f"metadata=remove：{source_name} → {target} 没成功"
                f"（{task['error_message']!r}）",
            )
            continue
        data, _ct, _result = fetch_result(snapshot)
        got = inspect_bytes(data)
        check(
            "FILETOOLS_XMP_CANARY" not in got["xmp"],
            f"metadata=remove：{source_name} → {target} 的 XMP 真的没了",
        )

    # B5 **TIFF 源**的拍摄信息。第十阶段 C 之前它整段丢掉 —— Pillow 不把
    #    TIFF 的 IFD 放进 info["exif"]，只从 getexif() 暴露，于是
    #    「保留」在 TIFF 源上等于什么都没做。
    tiff_source = (SAMPLES / "tiff_exif.tiff").read_bytes()
    source_truth = inspect_bytes(tiff_source)
    check(
        source_truth["exif_model"] == "FT-10C",
        f"前提成立：TIFF 源里真的有拍摄信息（Model={source_truth['exif_model']!r}）",
    )

    snapshot = submit_unified(
        filename="tiff_exif.tiff", content=tiff_source, target="jpg", mime="image/tiff"
    )
    task = task_of(snapshot)
    if task["status"] != "completed":
        check(False, f"TIFF 源 → JPEG 没成功（{task['error_message']!r}）")
    else:
        data, _ct, _result = fetch_result(snapshot)
        got = inspect_bytes(data)
        check(
            got["exif_model"] == "FT-10C",
            f"TIFF 源的拍摄信息在「保留」下真的带过去了（Model={got['exif_model']!r}）",
        )

    # 反过来：选了「清除」，TIFF 源的拍摄信息也必须没。
    snapshot = submit_unified(
        filename="tiff_exif.tiff",
        content=tiff_source,
        target="jpg",
        mime="image/tiff",
        extra=[("options", json.dumps({"metadata": "remove"}))],
    )
    task = task_of(snapshot)
    if task["status"] != "completed":
        check(False, f"TIFF 源 + metadata=remove 没成功（{task['error_message']!r}）")
    else:
        data, _ct, _result = fetch_result(snapshot)
        got = inspect_bytes(data)
        check(
            got["exif_model"] == "" and not got["has_exif"],
            f"TIFF 源 + metadata=remove：拍摄信息真的没了（Model={got['exif_model']!r}）",
        )

    # B6 schema 的默认值与服务端兜底是**同一个值**。
    #    两处各写一份，用户不动滑杆时得到的就会是两份不同的结果。
    #    id 由能力目录现场拼（``image.<source>-to-<target>``），不在脚本里
    #    手抄一个字符串 —— 抄错了，``spec()`` 会抛 KeyError，于是这条检查
    #    点会静默消失，而那正是最容易被忽略的一种失败。
    for target in ("jpg", "webp"):
        conversion_id = f"image.png-to-{target}"
        if conversion_id not in catalogue.entry:
            check(False, f"能力目录里没有 {conversion_id}，这条检查点写错了")
            continue
        default = catalogue.spec(conversion_id, "metadata")["default"]
        check(
            default == "keep",
            f"{conversion_id} 的 metadata 默认是 keep（{default!r}）",
        )


# ======================================================================
# C 段：能力目录（§五十六）
# ======================================================================

def section_c(catalogue: Catalogue) -> None:
    section("C 能力目录：矩阵与条目互相对得上、registry 零 I/O")

    matrix_pairs = catalogue.pairs()
    #: 1→1 的转换条目。``operation`` 那批（PDF 合并 / 拆分 / 压缩…）不在
    #: 矩阵里 —— 矩阵只回答「源能转成什么目标」，多进多出的页面级操作
    #: 结构上不是这个形状（第九阶段决策 B）。
    conversions = [
        item
        for item in catalogue.data["conversions"]
        if item["operation_type"] == "conversion"
    ]
    entry_pairs = {(item["source_type"], item["target_type"]) for item in conversions}

    missing = matrix_pairs - entry_pairs
    check(
        not missing,
        f"矩阵里的每一格都有对应条目（缺 {len(missing)} 格：{sorted(missing)[:3]}）",
    )

    # ``available`` 是**算出来的**，不是存下来的：此刻列在矩阵里的一对，
    # 它的条目就必须说可用；反过来不在矩阵里的，条目必须说不可用。
    # 这一条同时管住了两个方向 —— 既不许「矩阵有、条目说不可用」，
    # 也不许「条目说可用、矩阵里却没有」，后者正是「宣传了做不到的事」。
    wrong = [
        item["id"]
        for item in conversions
        if item["available"] != ((item["source_type"], item["target_type"]) in matrix_pairs)
    ]
    check(not wrong, f"available 与矩阵逐格一致（不一致 {len(wrong)} 条：{wrong[:3]}）")

    # 矩阵里留下的源，必须恰好是「至少有一条可用转换」的那些源。
    # 过滤是**逐格**做的，一个目标都不剩的源才整行去掉；整行藏掉会让
    # 本来能用的目标跟着消失，留一个空行则会让前端渲染出一张点不动的卡片。
    live_sources = set(catalogue.data["matrix"])
    expected_sources = {item["source_type"] for item in conversions if item["available"]}
    check(
        live_sources == expected_sources,
        f"矩阵里的源恰好是「至少有一条可用转换」的那些（差 "
        f"{sorted(live_sources ^ expected_sources)}）",
    )

    # 缺组件时不得宣传，但也**不许悄悄藏起来**：只要有转换此刻不可用，
    # ``notes`` 里就必须给出原因。空 notes 配着缺失的能力，用户只会
    # 以为网站坏了。
    unavailable = [item for item in conversions if not item["available"]]
    notes = catalogue.data.get("notes") or []
    check(
        bool(unavailable) == bool(notes),
        f"有 {len(unavailable)} 条转换不可用时 notes 恰好给出原因"
        f"（notes={notes}）",
    )

    # registry 必须零 I/O、不 import 任何实现库（§十三）。
    # 这是那个模块能被 router / tasks / 前端目录三方安全共用的前提。
    probe = _run_backend_python(
        r'''
import json, sys
sys.path.insert(0, ".")
import conversion.registry as registry
forbidden = [name for name in ("fitz", "pymupdf", "PIL", "docx", "openpyxl",
                               "pptx", "pikepdf", "rapidocr_onnxruntime",
                               "onnxruntime")
             if name in sys.modules]
print(json.dumps({
    "forbidden": forbidden,
    "capabilities": len(registry.CAPABILITIES),
    "sources": sorted(registry.TARGETS_BY_SOURCE),
    "preview_mimes": sorted(registry.INLINE_PREVIEW_MEDIA_TYPES),
}))
'''
    )
    loaded = json.loads(probe)
    check(
        not loaded["forbidden"],
        f"registry 导入后 sys.modules 里没有实现库（加载到 {loaded['forbidden']}）",
    )
    # 进程内的源表比线上矩阵**多**，这是对的：矩阵是「此刻真的能做」，
    # 源表是「系统认识哪些格式」。差的那些正是每一格都不可用的源
    # （这台机器上就是六个 Office 格式，没装 LibreOffice）。
    # 反过来就不行 —— 线上了而进程内不认识，那是凭空冒出来的能力。
    unregistered = sorted(live_sources - set(loaded["sources"]))
    check(
        not unregistered,
        f"矩阵里的每一种源，进程内的表都认识（凭空冒出来的 {unregistered}）",
    )
    vanished = sorted(set(loaded["sources"]) - live_sources)
    expected_vanished = sorted(
        {item["source_type"] for item in conversions if not item["available"]}
    )
    check(
        vanished == expected_vanished,
        f"矩阵里缺席的源恰好是「每一格都不可用」的那些"
        f"（缺席 {vanished} / 应当缺席 {expected_vanished}）",
    )

    # 预览那张表与「哪些目标真的能内联渲染」逐项相等。
    previewable = {
        item["target_type"]
        for item in catalogue.data["conversions"]
        if item.get("supports_preview")
    }
    expected = {"jpg", "png", "webp", "bmp", "gif"}
    check(
        previewable == expected,
        f"能预览的目标恰好是 {sorted(expected)}（实际 {sorted(previewable)}）",
    )
    check(
        set(loaded["preview_mimes"]) == {
            "image/jpeg", "image/png", "image/webp", "image/bmp", "image/gif"
        },
        f"放行的 MIME 集恰好是那五种（实际 {sorted(loaded['preview_mimes'])}）",
    )

    # formats[] 里每一种格式的扩展名都必须能在上传白名单里找到 —— 否则
    # 界面上写着「认识 HEIC」，用户传一个 .heic 却被扩展名白名单拦下。
    #
    # **SVG 是唯一一个例外，而且是有意的。** 它走统一转换中心那条按**内容**
    # 判定的路（第十阶段 A §九–§十五），而 ``allowed_image_extensions``
    # 是第四阶段那批旧图片接口用的**扩展名**白名单。旧接口对 SVG
    # 是 fail-closed 的：宁可拒掉，也不让一份活动内容从「旧的那条路」
    # 绕进图片流水线。所以这里**不放过它，而是把它钉住** ——
    # 例外集合必须恰好是 ``{"svg"}``：将来再有人把某个格式挪出白名单，
    # 这条会红，而不是像刚才那样静默通过。
    config = get_json(CONFIG_URL)
    allowed = {item.lower() for item in config["allowed_image_extensions"]}
    image_formats = [
        item for item in catalogue.data["formats"] if item["category"] == "image"
    ]
    # 扩展名要声明 —— 但**只对能当输入的格式**要求。
    # ``ico`` 是只有 PNG→ICO 这一条的**目标**格式（决策 F：架构上仍是一条
    # registry 条目，将来放开不用改前端），它根本不是一种源，声明扩展名
    # 反而会把 ``.ico`` 映射成一种可上传的源。所以这里断言的是：
    # 没有扩展名的格式，一个都不能出现在矩阵的源里。
    no_extension = {item["value"] for item in image_formats if not item["extensions"]}
    check(
        not (no_extension & live_sources),
        f"没有声明扩展名的格式都不是可上传的源（{sorted(no_extension)} 与源的交集："
        f"{sorted(no_extension & live_sources)}）",
    )
    sources_without_extensions = [
        source
        for source in live_sources
        if catalogue.category.get(source) == "image"
        and not next(
            (item["extensions"] for item in image_formats if item["value"] == source), None
        )
    ]
    check(
        not sources_without_extensions,
        f"每一种可上传的图片源都声明了扩展名（缺 {sources_without_extensions}）",
    )
    outside = {
        item["value"]
        for item in image_formats
        if any(ext.lower() not in allowed for ext in item["extensions"])
    }
    check(
        outside == {"svg"},
        f"扩展名不在旧接口白名单里的图片格式恰好只有 svg（实际 {sorted(outside)}）",
    )
    check(
        ".svg" not in allowed,
        "旧图片接口的扩展名白名单里**没有** .svg（活动内容不走那条路，fail-closed）",
    )


# ======================================================================
# D 段：兜底配置与线上配置机械对账（§五）
# ======================================================================

def section_d() -> None:
    section("D FALLBACK_CONFIG 与线上 /api/config 机械对账")

    fallback = fallback_config()
    live = get_json(CONFIG_URL)

    keys = sorted(fallback)
    check(len(keys) >= 30, f"从 useServerConfig.ts 里解析出 {len(keys)} 个兜底键")

    unknown = [key for key in keys if key not in live and key not in FALLBACK_PLACEHOLDERS]
    check(
        not unknown,
        f"兜底配置里没有线上不存在的键（多出 {unknown}）",
    )

    mismatched: list[tuple[str, object, object]] = []
    compared = 0
    for key in keys:
        if key in FALLBACK_PLACEHOLDERS:
            continue
        if key not in live:
            continue
        compared += 1
        if fallback[key] != live[key]:
            mismatched.append((key, fallback[key], live[key]))

    check(
        not mismatched,
        f"{compared} 个兜底值与线上逐值相等"
        + (f"（不一致 {mismatched}）" if mismatched else ""),
    )
    check(
        compared >= 30,
        f"真的比了 {compared} 个键（不是空跑一遍就报绿）",
    )

    # 有意不同的那几个也必须**真的有理由**，不是一张越来越长的免检名单。
    for key, reason in sorted(FALLBACK_PLACEHOLDERS.items()):
        check(key in fallback, f"免检键 {key} 确实在兜底配置里（理由：{reason}）")

    # 线上缺的那几个键里，一个都不许是「类型不对」——
    # 兜底值的形状必须与线上一致，否则前端拿到哪一份都会崩。
    for key in ("txt_font_size", "allowed_image_extensions", "worker_pools"):
        if key in fallback and key in live:
            check(
                type(fallback[key]) is type(live[key]),
                f"{key} 的兜底类型与线上一致（{type(fallback[key]).__name__}）",
            )
        elif key in live:
            check(True, f"{key} 只在线上有（前端按可选处理）")


# ======================================================================
# E 段：预览边界（§五）
# ======================================================================

def section_e() -> None:
    section("E 预览边界：发了地址就一定打得开，打不开就不发地址")

    # E1 能渲染的格式：地址要发，而且**与下载下来的是同一份字节**。
    #    预览走的就是下载那条路（同一个登记簿、同一份文件），
    #    这条断言是「没有第二套预览系统」的直接证据。
    #
    #    目标是 jpg 而不是 png：**自转换不存在**（png 转不了 png），
    #    拿 png 源去转 png 会得到 UNSUPPORTED_CONVERSION。
    png = (SAMPLES / "legal_big.png").read_bytes()
    snapshot = submit_unified(
        filename="legal_big.png", content=png, target="jpg", mime="image/png"
    )
    #    **顺序是语义的一部分**：``GET /api/download/{job_id}`` 是一次性的
    #    （响应发完就删文件、注销令牌），所以下载只能发生在最后。先下载
    #    再要预览，拿到的是 404 —— 那不是缺陷，是「一次性」在正常工作。
    #    这里按真实用户的顺序走：先看预览（可以反复看），最后下载一次。
    result = task_of(snapshot)["result"]
    check(
        result.get("preview_url") is not None,
        f"JPEG 结果拿到了 preview_url（{result.get('preview_url')!r}）",
    )

    preview_body = b""
    if result.get("preview_url"):
        preview_status, preview_body, preview_type = request(result["preview_url"])
        check(
            preview_status == 200 and isinstance(preview_body, bytes),
            f"那个地址真的打得开（{preview_status}）",
        )
        check(
            preview_type.startswith("image/"),
            f"预览的 Content-Type 是图片（{preview_type}）",
        )
        # 预览不吃令牌：同一个地址可以看第二次。
        again_status, again_body, _t = request(result["preview_url"])
        check(
            again_status == 200 and again_body == preview_body,
            f"预览可以反复看（第二次 {again_status}，字节相同 "
            f"{again_body == preview_body}）",
        )

        # **现在**才下载，而且只下载这一次。
        data, _ct, _downloaded = fetch_result(snapshot)
        check(
            data == preview_body,
            "预览与下载是**同一份字节**（预览没有第二条渲染管道）",
        )

        # 下载之后预览随之失效 —— 临时文件真的被删了，不是留着一份专门
        # 给预览用的副本。这是「没有第二套预览系统」的又一处直接证据。
        #
        # **先等半秒再问**：删除发生在下载响应的 BackgroundTask 里，
        # 客户端收到最后一个字节时它可能还没开始跑。不睡这一下，这条断言
        # 就在 200（还没删）与 404（删完了）之间随机翻 —— 与 F2 段同一个
        # 理由。这半秒同时也让过了删除与预览之间那条极窄的竞态
        # （``resolve_item_path`` 判完 ``is_file()``、Starlette 才去
        # ``os.stat``，中间文件被删掉 → 500）；那条竞态本身没有修，
        # 成因与实测到的频次如实记在最终报告的「仍然存在的限制」里。
        time.sleep(0.5)
        gone_status, _gone, _t2 = request(result["preview_url"])
        check(
            gone_status == 404,
            f"下载之后预览跟着失效（{gone_status}）—— 预览读的就是那份被删掉的文件",
        )

    # E2 渲染不了的格式：不发地址，而且硬敲那个地址得到干净的业务错误。
    #
    #    走 png → tiff 而不是 tiff → tiff：**自转换不存在**（能力目录里没有
    #    ``image.*-to-同一格式``，提交它会得到 UNSUPPORTED_CONVERSION）。
    #    目标格式是 tiff 就够 —— 预览边界看的是**结果的格式**，不是来源。
    tiff = (SAMPLES / "tiff_exif.tiff").read_bytes()
    snapshot = submit_unified(
        filename="xmp.png", content=(SAMPLES / "xmp.png").read_bytes(),
        target="tiff", mime="image/png",
    )
    task = task_of(snapshot)
    check(
        task["status"] == "completed",
        f"png → tiff 转得成（{task['status']} {task['error_message']!r}）",
    )
    if task["status"] == "completed":
        result = task["result"]
        check(
            result.get("preview_url") is None,
            f"TIFF 结果**不发** preview_url（浏览器渲染不了它，发了就是破图）："
            f"{result.get('preview_url')!r}",
        )
        status, error_body, _ = request(f"{UNIFIED_SUBMIT}/{snapshot['batch_id']}")
        record("GET 批次状态 (TIFF)", error_body)
        index = task["index"]
        hit, hit_body, _ = request(f"/api/preview/{snapshot['batch_id']}/{index}")
        record("GET /api/preview/TIFF", hit_body)
        check(
            hit != 200,
            f"硬敲那个预览地址不是 200（拿到 {hit}，正文 {hit_body!r}）",
        )
        # 结果**确实**是 TIFF —— 不发预览地址的理由是「这张结果浏览器渲染
        # 不了」，所以得先证明它真的是那种格式。统一接口的结果段里没有
        # ``format`` 字段（格式由 ``media_type`` / 文件名表达），所以这里
        # 不看字段，直接打开字节：扩展名和 MIME 都可能被写错，字节不会。
        data, _ct, _r = fetch_result(snapshot)
        opened = inspect_bytes(data)
        check(
            opened["format"] == "TIFF",
            f"下载回来的字节真的是 TIFF（{opened['format']} {opened['width']}×"
            f"{opened['height']}）",
        )

    # E3 旧接口同一条边界。第十阶段 C 修的正是这里：旧接口原先按
    #    ``image/*`` 前缀放行，把 TIFF 与 HEIC 也放进来了。
    status, snapshot, _created = submit_legacy(
        "/api/image/resize",
        fields=[("width", "150")],
        files=[("files", "scan.tiff", tiff, "image/tiff")],
        label="POST /api/image/resize (TIFF)",
    )
    legacy_result = snapshot.get("result") or {}
    check(
        snapshot.get("state") == "done" and bool(legacy_result.get("items")),
        f"旧接口能把 TIFF 缩到 150 宽（{snapshot.get('state')!r} "
        f"{(snapshot.get('error') or {}).get('message')!r}）",
    )
    if legacy_result.get("items"):
        item = legacy_result["items"][0]
        check(
            item["result"]["format"].lower() in ("tiff", "tif"),
            f"它出来的还是 TIFF（{item['result']['format']!r}）",
        )
        check(
            item["preview_url"] is None,
            f"旧接口对 TIFF 也不发 preview_url（{item['preview_url']!r}）",
        )
        # 而能渲染的格式照旧发 —— 修边界不能顺手砍掉能用的东西（§六十九）。
        status2, snapshot2, _created2 = submit_legacy(
            LEGACY_CONVERT,
            fields=[("target_format", "png")],
            files=[("files", "a.jpg", (SAMPLES / "xmp.jpg").read_bytes(), "image/jpeg")],
            label=f"POST {LEGACY_CONVERT} (png)",
        )
        legacy_png = snapshot2.get("result") or {}
        if snapshot2.get("state") == "done" and legacy_png.get("items"):
            url = legacy_png["items"][0]["preview_url"]
            check(
                url is not None and request(url)[0] == 200,
                f"能渲染的格式在旧接口上照旧拿到可用的 preview_url（{url!r}）",
            )
        else:
            check(False, f"旧接口 PNG 转换失败（{status2} {snapshot2.get('error')!r}）")


# ======================================================================
# F 段：ZIP 与临时文件（§十一 / §十二）
# ======================================================================

_TEMP_ROOT_CODE = r'''
import json, os, sys
sys.path.insert(0, ".")
from config import settings
root = settings.TEMP_ROOT or None
if root is None:
    import tempfile
    root = tempfile.gettempdir()
root = os.path.abspath(root)
dirs = sorted(
    os.path.join(root, name) for name in os.listdir(root)
    if os.path.isdir(os.path.join(root, name))
)
print(json.dumps({"root": root, "dirs": dirs}))
'''


def temp_snapshot() -> dict:
    return json.loads(_run_backend_python(_TEMP_ROOT_CODE))


def section_f() -> None:
    section("F ZIP 去重与临时目录（同名来源不互相覆盖、跑完不留垃圾）")

    before = temp_snapshot()

    # F1 两份**同名**的 PDF。用户从两个文件夹各拖一份 ``报告.pdf`` 进来是常事：
    #    前缀原先直接取来源文件名，两份都得到 ``报告-``，加上页面名也一样，
    #    于是 ZIP 里出现两个同名成员 —— ``zipfile`` 不报错，解压时后写的
    #    覆盖前一份，用户少拿到一份，界面上却显示两份都成功。
    uploads: list[str] = []
    for index in (1, 2):
        source = (SAMPLES / f"report_{index}.pdf").read_bytes()
        status, body, _ = request(
            "/api/pdf/upload",
            method="POST",
            files=[("file", "报告.pdf", source, "application/pdf")],
        )
        if status != 200 or not isinstance(body, dict):
            check(False, f"上传第 {index} 份同名 PDF 失败（{status} {body!r}）")
            return
        uploads.append(body["input_id"])

    fields = [("input_ids", uploads[0]), ("input_ids", uploads[1]),
              ("target_format", "png"), ("pages", "all")]
    status, created, _ = request("/api/pdf/to-images", method="POST", fields=fields)
    record("POST /api/pdf/to-images (两份同名 PDF)", created)
    if status != 202 or not isinstance(created, dict):
        check(False, f"提交 PDF 转图片失败（{status} {created!r}）")
        return

    snapshot = wait_group(created["group_id"])
    result = snapshot.get("result") or {}
    names = [item["filename"] for item in (result.get("files") or [])]
    check(len(names) == 3, f"三页都在（拿到 {names}）")
    check(
        len(set(names)) == len(names),
        f"ZIP 里没有同名成员（{names}）",
    )
    download_url = result.get("download_url")
    if download_url:
        status, raw, _ = request(download_url)
        if status == 200 and isinstance(raw, bytes):
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                members = archive.namelist()
                check(members == names, f"ZIP 的成员就是那三个（{members}）")
                check(archive.testzip() is None, "ZIP 本身是完好的")
                check(
                    len(set(members)) == len(members),
                    "解压后不会互相覆盖",
                )
        else:
            check(False, f"下载 PDF 结果的 ZIP 失败（{status}）")

    # F2 跑完一轮不留临时目录。**每个用户文件都不该在服务器上留下来**，
    #    这条是「处理完就删」这条承诺的直接证据。
    #
    #    但**上传的 PDF 是例外**，而且是有意的：``POST /api/pdf/upload`` 先
    #    把文件存下来，用户看完缩略图再决定导出哪些页 —— 一次上传多次操作。
    #    那两份上传目录要留到 30 分钟 TTL 到期，不是泄漏。所以这条断言分成
    #    两半：新增的目录只允许是**输入存储**（里面就一份 PDF，别无他物），
    #    而且任务目录一个都不许剩 —— 后半句另外用一次纯图片任务单独验。
    time.sleep(0.5)
    after = temp_snapshot()
    leaked = sorted(set(after["dirs"]) - set(before["dirs"]))

    input_dirs = [path for path in leaked if _is_pdf_input_dir(path)]
    leftovers = [path for path in leaked if path not in input_dirs]
    check(
        len(input_dirs) == 2,
        f"新增的两个临时目录正是那两份上传的 PDF（输入存储，30 分钟 TTL）："
        f"{[os.path.basename(p) for p in input_dirs]}",
    )
    check(
        not leftovers,
        f"旧接口的任务目录下载后**整个**消失，剩下的只有输入存储"
        f"（多出 {[os.path.basename(p) for p in leftovers]}）",
    )

    # 而**任务目录**要按两条路各自的契约分开验。两条路径的清理粒度**本来就
    #    不同**，而且是写在代码里的有意设计（tasks/conversion_tasks.py 的
    #    模块注释「目录布局」那一节），不是随手写成的：
    #
    #    * 旧接口（上面那批 PDF 转图片走的就是它）：下载即 ``remove_dir(
    #      job.directory)``，整棵任务目录当场消失；
    #    * 统一接口：每个结果**独占一个子目录**（``out/<n>/``），下载只删
    #      自己那一个 —— 若共用目录，下载任意一个就会把别人的结果一起删掉。
    #      批次根目录与 ``in/`` 里的原件**留着**，因为重试（§十六）要用它，
    #      由 ``TASK_TTL_SECONDS``（30 分钟）与孤儿清理兜底。
    #
    #    所以「一条断言管两条路」是错的：对统一接口下「根目录立刻消失」这个
    #    要求，要么删掉重试能力，要么把用户的原件提前销毁。这里按各自的真
    #    契约分别断言，一寸都不放松。
    before_task = temp_snapshot()
    snapshot = submit_unified(
        filename="legal_big.png", content=(SAMPLES / "legal_big.png").read_bytes(),
        target="jpg", mime="image/png",
    )
    during = temp_snapshot()
    created = sorted(set(during["dirs"]) - set(before_task["dirs"]))
    check(
        len(created) == 1,
        f"处理期间建了一个批次根目录（{created}）",
    )
    if created:
        root = created[0]
        layout = _tree(root)
        check(
            set(layout) == {"in", "out"},
            f"批次根目录里只有 in/ 与 out/（实际 {sorted(layout)}）",
        )
        check(
            len(layout.get("out", [])) == 1,
            f"结果独占一个子目录 out/<n>/（实际 {layout.get('out')}）",
        )

        download_url = task_of(snapshot)["result"]["download_url"]
        check(request(download_url)[0] == 200, "下载这个结果（只下这一次）")
        check(
            request(download_url)[0] == 404,
            "同一个令牌再下一次就 404 了 —— 结果确实是一次性的",
        )
        time.sleep(0.5)
        layout = _tree(root)
        check(
            os.path.isdir(root),
            "批次根目录还在（**有意留着**：重试要用 in/ 里的原件）",
        )
        check(
            layout.get("out") == [],
            f"结果子目录被整个删掉了（out/ 现在是空的：{layout.get('out')}）",
        )
        check(
            len(layout.get("in", [])) == 1,
            f"上传的原件还在 in/ 里，留给重试（{layout.get('in')}）",
        )


def _tree(root: str) -> dict[str, list[str]]:
    """一层目录树：``{子目录名: [文件名, ...]}``（只走两层，够描述这个布局）。"""
    layout: dict[str, list[str]] = {}
    try:
        entries = sorted(os.listdir(root))
    except OSError:
        return layout
    for entry in entries:
        full = os.path.join(root, entry)
        if os.path.isdir(full):
            try:
                layout[entry] = sorted(os.listdir(full))
            except OSError:
                layout[entry] = []
        else:
            layout.setdefault(".", []).append(entry)
    return layout


def _is_pdf_input_dir(path: str) -> bool:
    """这个临时目录是不是「上传的 PDF 存下来的那一份」。

    判据看**内容**（里面正好一个文件，而且以 ``%PDF`` 开头），不看名字 ——
    目录名是随机 token，靠名字猜就等于把实现细节抄进断言。
    """
    try:
        entries = [entry for entry in os.listdir(path)]
    except OSError:
        return False
    if len(entries) != 1:
        return False
    target = os.path.join(path, entries[0])
    try:
        if not os.path.isfile(target):
            return False
        with open(target, "rb") as handle:
            return handle.read(4) == b"%PDF"
    except OSError:
        return False


# ======================================================================
# G 段：安全
# ======================================================================

def section_g(catalogue: Catalogue) -> None:
    section("G 安全：SVG 净化后继续、假扩展名被拦下、对外文本不泄露内部")

    # G1 恶意 SVG 的契约是**净化后继续**，不是拒绝（§三十二 明令不许改）。
    #    「改安全了」与「改成拒绝」是两回事：后者是一次能力削减。
    evil = (SAMPLES / "evil.svg").read_bytes()
    if catalogue.has("svg", "png"):
        snapshot = submit_unified(
            filename="evil.svg", content=evil, target="png", mime="image/svg+xml"
        )
        task = task_of(snapshot)
        check(
            task["status"] == "completed",
            f"恶意 SVG 仍然**转换成功**（净化后继续，不是拒绝）：{task['status']} "
            f"{task['error_message']!r}",
        )
        if task["status"] == "completed":
            data, content_type, result = fetch_result(snapshot)
            opened = inspect_bytes(data)
            check(
                opened["format"] == "PNG" and opened["width"] > 0,
                f"产物是一张真的 PNG（{opened['format']} "
                f"{opened['width']}×{opened['height']}）",
            )
            blob = json.dumps(result, ensure_ascii=False)
            for fragment in ("FILETOOLS_EVIL", "<script", "foreignObject", "/etc/passwd"):
                check(
                    fragment not in blob,
                    f"结果里没有 {fragment!r}（危险内容没有跟着走）",
                )
            # 净化动过的地方要**如实记进 notes**，用户能知道自己的文件被动过。
            check(
                bool(result.get("note") or result.get("notes")),
                f"被动过的地方如实说明了（{result.get('note')!r}）",
            )
    else:
        check(False, "能力目录里没有 svg → png，无法验证 SVG 那条契约")

    # G2 假扩展名：内容是 PDF、名字叫 .png。判定必须**按内容**，
    #    不采信客户端给的分组（§十七 / §五）。
    #
    #    ``.png`` 本来就在白名单里，所以拦下它的不可能是扩展名 ——
    #    只能是内容。这一点下面用同一份字节反过来证明：换个名字当 PDF 传，
    #    服务器就收。
    status, snapshot, _created = submit_legacy(
        LEGACY_CONVERT,
        fields=[("target_format", "jpg")],
        files=[("files", "fake.png", (SAMPLES / "fake.png").read_bytes(), "image/png")],
        label=f"POST {LEGACY_CONVERT} (假扩展名)",
    )
    error = snapshot.get("error") or {}
    check(
        snapshot.get("state") == "failed"
        and error.get("code") == "INVALID_FILE_TYPE",
        f"假扩展名被内容校验拦下（{snapshot.get('state')!r} {error.get('code')!r}）",
    )
    check(
        "pdf" not in (error.get("message") or "").lower()
        or "不是" in (error.get("message") or ""),
        f"那句提示没有把服务器内部的判定过程讲出去（{error.get('message')!r}）",
    )

    # 同一份字节，名字改成 .pdf —— 服务器收下它。两件事合起来才是
    # 「按内容判定」：拒的是**名实不符**，不是这些字节本身。
    status, ok_body, _ct = request(
        "/api/pdf/upload",
        method="POST",
        files=[("file", "document.pdf", (SAMPLES / "fake.png").read_bytes(), "application/pdf")],
    )
    check(
        # ``bool(...)`` 不是装饰：``A and B and C`` 在 Python 里返回的是**最后
        # 一个真值本身**，这里是 ``input_id`` 那串十六进制。它落到 ``check``
        # 里照样算真，但「算真」和「是 True」在汇总时不是一回事（``sum``
        # 会当场撞上 int + str）。断言强度一个字没变，只是把值收敛成布尔。
        bool(status == 200 and isinstance(ok_body, dict) and ok_body.get("input_id")),
        f"同一份字节换个名字当 PDF 传就被收下（{status}）—— 拦的是名实不符，不是这些字节",
    )

    # G3 旧工具页仍然活着（§六十九：能力只增不减）。
    for path in ("/", ROUTE, IMAGE_TOOLS_ROUTE, "/pdf", "/word"):
        status, _body, _ct = request(path)
        check(status == 200, f"{path} 仍然返回 200（拿到 {status}）")


# ======================================================================
# H 段：前端一致性（服务端目录是唯一真相）
# ======================================================================

def section_h(page: Page, catalogue: Catalogue) -> None:
    section("H 前端一致性：/image 与首页 FAQ 都跟着能力目录走")

    expected_sources = set(catalogue.image_sources)

    page.goto(f"{BASE}{IMAGE_TOOLS_ROUTE}", wait_until="networkidle")
    page.wait_for_timeout(700)
    matrix = page.locator("section[aria-labelledby='image-matrix-heading']")
    rows = matrix.locator("li.flex.flex-col")
    listed = {
        rows.nth(index)
        .locator("p")
        .first.inner_text()
        .strip()
        for index in range(rows.count())
    }
    expected_labels = {catalogue.label.get(value, value) for value in expected_sources}
    check(
        listed == expected_labels,
        f"/image 列出的源格式与矩阵逐个一致（页面 {sorted(listed)} / 目录 {sorted(expected_labels)}）",
    )

    # 首页 FAQ 那一句是**现场生成**的。它必须提到此刻真的能转的格式，
    # 而且一个都不能多 —— 写死了就是在替服务器承诺一件它做不到的事。
    #
    # **必须按元素取文本，不能拿整页的 inner_text**：这条 FAQ 是 ``<details>``
    # 里的一段，默认收着；收着的节点不算「渲染出来的文本」，整页 inner_text
    # 里根本没有它。之前那版就是这么写的，结果拿到的是一份**不含 FAQ 的**
    # 文本 —— 于是「缺 7 个格式」全是假警报（漏掉的正是 FAQ 里的那 7 个）。
    # 换成 ``text_content()``（取 DOM 文本，不看可见性），范围收进这一个
    # 元素，断言的才是那句话本身。
    page.goto(BASE, wait_until="networkidle")
    page.wait_for_timeout(700)
    faq = page.locator("details").filter(has_text=FORMAT_FAQ_QUESTION).first
    answer = (faq.locator("p").first.text_content() or "").strip()
    check(
        bool(answer),
        f"首页定位到了「{FORMAT_FAQ_QUESTION}」那一条的答案（{answer[:40]!r}）",
    )
    missing = sorted(label for label in expected_labels if label not in answer)
    check(
        not missing,
        f"FAQ 那句答案提到了每一种此刻可用的图片格式（缺 {missing}；答案开头 "
        f"{answer[:60]!r}）",
    )

    # 反向：这一句里一个**不可用**的图片格式名都不许出现。
    unavailable = [
        item["label"]
        for item in catalogue.data["formats"]
        if item["category"] == "image"
        and item["value"] not in expected_sources
    ]
    overclaimed = sorted(label for label in unavailable if label and label in answer)
    check(
        not overclaimed,
        f"FAQ 那句答案没有宣传此刻不可用的格式（多说了 {overclaimed}）",
    )

    # §四十 明令不实现的那些，首页也不许假装可用。这一条按**整页**查 ——
    # 宣传可以出现在任何地方，不限于 FAQ。
    home = body(page)
    for name in ("MP4", "MP3", "EPUB", "DWG", "PSD", "RAW"):
        check(name not in home, f"首页没有宣传 §四十 明令不实现的 {name}")


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
            check(overflow <= 1, f"{width}px 下 {name} 无横向溢出（溢出 {overflow}px)")

    # 按钮要够大 —— 手指点得到。44px 是 §五十六 写下的下界。
    # 三个页面都查：宣传口径里「手机上能用」说的是整个站，不是某一个页面。
    page.set_viewport_size({"width": 375, "height": 844})
    for route in (BASE, f"{BASE}{IMAGE_TOOLS_ROUTE}", f"{BASE}{ROUTE}"):
        page.goto(route, wait_until="networkidle")
        page.wait_for_timeout(600)
        name = route[len(BASE):] or "/"
        small = []
        for index in range(page.locator("button").count()):
            button = page.locator("button").nth(index)
            if not button.is_visible():
                continue
            box = button.bounding_box()
            if box is None or box["height"] >= 44:
                continue
            # 报出**是哪一个**：只有高度和空字符串的话，下一个人得自己
            # 满页面找。aria-label 优先（图标按钮没有文字）。
            who = (
                button.get_attribute("aria-label")
                or button.inner_text().strip()[:16]
                or (button.get_attribute("class") or "")[:40]
            )
            small.append((who, round(box["height"])))
        check(
            not small,
            f"375px 下 {name} 的每个可见按钮都 ≥44px 高（偏小的 {small[:4]}）",
        )


# ======================================================================
# J 段：几何与体积（§六十一·4–9、§六十七）
# ======================================================================

def _options(values: dict) -> list[tuple[str, str]]:
    """把一份选项字典包成统一接口的 ``options`` 表单字段。

    空字典回**空表单**，不是 ``"{}"``：对照组要的是「这个请求压根没提过
    任何选项」，而不是「提了一个空对象」—— 后者会走一遍校验链，
    和「用户没选任何东西」不是同一件事。
    """
    if not values:
        return []
    return [("options", json.dumps(values, ensure_ascii=False))]


def section_j(catalogue: Catalogue) -> None:
    section("J 几何与体积：选项真的落到像素与字节上")

    corner = (SAMPLES / "corner.png").read_bytes()

    # 目标一律用 BMP：下面比的是**具体颜色**，有损格式的振铃会把纯色块
    # 边缘糊成过渡色。源用 PNG —— 目标格式里没有自转换（png 转不了 png）。
    def convert(values: dict, filename: str = "corner.png", target: str = "bmp",
                content: bytes | None = None, mime: str = "image/png") -> tuple[bytes, dict]:
        snapshot = submit_unified(
            filename=filename,
            content=content if content is not None else corner,
            target=target,
            mime=mime,
            extra=_options(values),
        )
        task = task_of(snapshot)
        check(
            task["status"] == "completed",
            f"{filename} → {target} {values or '（无选项）'} 转得成"
            f"（{task['status']} {task['error_message']!r}）",
        )
        if task["status"] != "completed":
            # 不往下走：调用方已经拿到一条失败，再让 fetch_result 抛
            # SystemExit 会把这一趟剩下的检查点全带走。
            return b"", {}
        data, _ct, result = fetch_result(snapshot)
        return data, result

    # J1 对照组：不做任何处理时红块留在左上。
    #    **没有这一条，下面「翻转生效了」可能只是原图本来就长这样。**
    plain, _r = convert({})
    info = inspect_pixels(plain)
    check(
        (info["width"], info["height"]) == (200, 150),
        f"不处理时尺寸不变（{info['width']}×{info['height']}）",
    )
    check(
        close(info["corners"][0], CORNER_RED) and close(info["corners"][1], CORNER_WHITE),
        f"不处理时红块留在左上（四角 {info['corners']}）",
    )

    # J2 旋转 90°：宽高互换，红块从左上转到右上。
    data, _r = convert({"rotation": "90"})
    info = inspect_pixels(data)
    check(
        (info["width"], info["height"]) == (150, 200),
        f"旋转 90° 后宽高互换（{info['width']}×{info['height']}）",
    )
    check(
        close(info["corners"][0], CORNER_WHITE) and close(info["corners"][1], CORNER_RED),
        f"顺时针 90°：红块从左上转到右上（四角 {info['corners']}）",
    )

    # J3 翻转：水平把红块挪到右上，垂直挪到左下。
    horizontal, _r = convert({"flip": "horizontal"})
    info = inspect_pixels(horizontal)
    check(
        (info["width"], info["height"]) == (200, 150),
        f"水平翻转不改变尺寸（{info['width']}×{info['height']}）",
    )
    check(
        close(info["corners"][0], CORNER_WHITE) and close(info["corners"][1], CORNER_RED),
        f"水平翻转：红块到右上（四角 {info['corners']}）",
    )
    vertical, _r = convert({"flip": "vertical"})
    info = inspect_pixels(vertical)
    check(
        close(info["corners"][0], CORNER_WHITE) and close(info["corners"][2], CORNER_RED),
        f"垂直翻转：红块到左下（四角 {info['corners']}）",
    )
    # 两种翻转必须产出不同的图 —— 否则上面两条可能测的是同一件事。
    check(horizontal != vertical, "水平翻转与垂直翻转产出的不是同一张图")

    # J4 裁剪：几何真的变了。
    data, _r = convert({"crop.width": "200", "crop.height": "100",
                        "crop.x": "0", "crop.y": "0"})
    info = inspect_pixels(data)
    check(
        (info["width"], info["height"]) == (200, 100),
        f"裁剪产出 200×100（实际 {info['width']}×{info['height']}）",
    )
    check(
        close(info["corners"][0], CORNER_RED),
        f"从左上角裁出来的那块仍带红（四角 {info['corners']}）",
    )

    # J5 缩放：「宽 640」允许放大，所以 200 宽的小图会被**拉**到 640；
    #    「长边 2560」只缩不放，同一张图原样出来。两条一起才说明
    #    「预设」不是一个点不动的装饰。
    data, _r = convert({"resize.mode": "w640"})
    info = inspect_pixels(data)
    check(
        (info["width"], info["height"]) == (640, 480),
        f"预设「宽 640」把小图放大到 640×480（实际 {info['width']}×{info['height']}）",
    )
    data, _r = convert({"resize.mode": "large"})
    info = inspect_pixels(data)
    check(
        (info["width"], info["height"]) == (200, 150),
        f"预设「长边 2560」不放大（仍是 {info['width']}×{info['height']}）",
    )

    # J6 压缩质量：同一张噪点图，90 必须比 50 大。
    grain = (SAMPLES / "grain.png").read_bytes()
    high, _r = convert({"quality": "90"}, filename="grain.png", target="jpg",
                       content=grain)
    low, _r = convert({"quality": "50"}, filename="grain.png", target="jpg",
                      content=grain)
    check(
        len(high) > len(low),
        f"质量 90 的产物比 50 大（{len(high)} > {len(low)}）",
    )

    # J7 目标体积：**报的必须与量出来的一致**（§三十一/§三十二）。
    #    两种情况都合格，唯独「量出来没达标、结果里却说达标」不合格。
    reachable = 100 * 1024
    data, result = convert({"target_bytes": str(reachable)}, filename="grain.png",
                           target="jpg", content=grain)
    measured = len(data)
    check(
        result.get("target_size") == reachable,
        f"结果里如实写着目标体积（{result.get('target_size')} vs {reachable}）",
    )
    check(
        bool(result.get("target_reached")) == (measured <= reachable),
        f"「有没有达标」与实测一致（报 {result.get('target_reached')}，"
        f"实测 {measured} vs 目标 {reachable}）",
    )
    check(measured <= reachable, f"100 KB 这个目标真的够得着（{measured} ≤ {reachable}）")

    # 极限目标：用 schema 里的下界，而不是自己编一个更小的数（那会直接 400）。
    floor = int(catalogue.spec("image.png-to-jpg", "target_bytes")["min"])
    data, result = convert({"target_bytes": str(floor)}, filename="grain.png",
                           target="jpg", content=grain)
    tight = len(data)
    check(
        bool(result.get("target_reached")) == (tight <= floor),
        f"下界目标下「有没有达标」也与实测一致（报 {result.get('target_reached')}，"
        f"实测 {tight} vs 目标 {floor}）",
    )
    check(tight < measured, f"目标更紧时产物更小（{tight} < {measured}）")

    # J8 EXIF 方向：存储的是 40×20 横图 + 方向 6，摆正后必须是 20×40，
    #    四角配色也要按方向 6 重排 —— 只比尺寸证明不了「摆正了」。
    oriented = (SAMPLES / "oriented.jpg").read_bytes()
    data, _r = convert({}, filename="oriented.jpg", target="png", content=oriented,
                       mime="image/jpeg")
    info = inspect_pixels(data)
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
# G3 的收尾：整轮跑过的响应体统一过一遍「不许泄露」
# ======================================================================

def check_no_leaks() -> None:
    section("G3 对外文本里没有服务器内部细节")

    for label, text in _seen_bodies:
        hit = [fragment for fragment in FORBIDDEN_FRAGMENTS if fragment in text]
        check(not hit, f"{label} 的响应里没有内部细节（命中 {hit}）")


# ======================================================================
# 汇总
# ======================================================================

def write_report() -> tuple[int, int]:
    """把结果写成报告文件，返回 ``(通过, 总数)``。

    返回计数是为了让**屏幕上那个数**和**文件里那个数**出自同一处：
    打印自己再数一遍，两者就有机会不一样。
    """
    passed = sum(1 for ok, _ in results if ok)
    failed = [label for ok, label in results if not ok]
    lines = [
        "第十阶段（A + C）封版验收报告",
        f"地址：{BASE}",
        f"通过：{passed} / {len(results)}",
        "",
        "分段：",
    ]
    for name, section_ok in _section_results.items():
        lines.append(f"  {name} 段：{sum(section_ok)} / {len(section_ok)}")
    lines += [
        "",
        "炸弹两档（从配置算出来的画布边长）：",
        f"  下档 warning：{BOMB_META.get('warning_side')} × {BOMB_META.get('warning_side')}",
        f"  上档 error  ：{BOMB_META.get('error_side')} × {BOMB_META.get('error_side')}",
        f"  像素上限    ：{BOMB_META.get('max_image_pixels')}",
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
    # 控制台那一条**必须**先入列 —— 它也是一条断言。放在打印之后的话，
    # 屏幕上那个数会与写进文件的数不一样：同一趟跑出两个数，
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
    catalogue = Catalogue(get_json(CAPABILITIES_URL))

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        page = context.new_page()
        attach(page)

        try:
            section_a(page)
            section_b()
            section_c(catalogue)
            section_d()
            section_e()
            section_f()
            section_g(catalogue)
            section_h(page, catalogue)
            section_i(page)
            section_j(catalogue)
            # 放在**最后**：这一段扫的是整轮跑过的每一个响应体，
            # 越靠后覆盖到的接口越多（J 段那十几个转换请求也在内）。
            check_no_leaks()
        finally:
            context.close()
            browser.close()

    return summary()


if __name__ == "__main__":
    raise SystemExit(main())
