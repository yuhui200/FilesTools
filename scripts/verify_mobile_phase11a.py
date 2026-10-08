"""第十一阶段 A 的真机验收脚本（真实 Expo Web + 真实后端 + 真实产物打开）。

第十一阶段 A 交付的是 FileTools 的**移动 App 基础框架**：一个 React Native /
Expo 客户端，四个底部 Tab，能力驱动的工具列表与参数表单，一次性令牌的结果
下载。后端一行没动 —— 移动端只是**又一个客户端**，文件的处理真相仍然只有
服务器那一份。这个脚本要证的正是这件事，而不是「界面能画出来」：

1. **能力目录零硬编码**（§九/§二十五）—— App 里的工具列表只能来自
   ``/api/conversion/capabilities``。源码里出现任何一条 ``xxx-to-yyy`` 形式
   的能力 id 字面量、任何一份手抄的格式清单，都算失配。这条守的是
   「后端加一种格式，App 不用改一行」。
2. **参数面板与提交同源**（§二十五）—— 界面上渲染出来的默认值，序列化成
   ``options`` 之后必须被**真服务器**逐条接受。这里对**每一条**声明了
   ``options_schema`` 的能力（本机 57 条）都真的 POST 一次，不是抽查。
   序列化用的是 App 自己的 ``buildPayload``（编译后由 node 执行），
   不是脚本里另写一份 —— 另写一份就只是在测脚本自己。
3. **错误码不漏配**（§十四）—— App 文案表必须覆盖 ``/api/config`` 给出的
   ``error_codes`` 全集；少一个，用户就会在某个真实错误上看到「处理失败」。
4. **三条真实链路**（§十五–§十八）—— JPG→PNG、DOCX→PDF（LibreOffice）、
   PDF→DOCX（OCR 扫描件）走**真界面**提交、轮询、**下载**，产物一律用
   ``PIL`` / ``PyMuPDF`` / ``python-docx`` **真打开**验证格式与内容。
   HTTP 200 不算数：转出一张坏图同样是 200。
5. **下载令牌只用一次**（§十七）—— 结果地址是一次性的（服务端发完就删）。
   界面上「下载」过一次之后必须变成「分享 / 打开」，并且这个地址**真的**
   再也取不到（脚本会亲自再取一次，必须 404/410）。
6. **用户看不到任何内部信息**（§十四/§三十六）—— 走过的每一个页面的
   渲染文本里，都不许出现堆栈、文件路径、内部模块名。
7. **窄屏与触控**（§二十三）—— 375 / 390 / 414 三档无横向溢出，
   所有可点控件实测高度 ≥ 44。
8. **取消与重试真的能用**（§四十九）—— 四份 DOCX 堆在 LibreOffice 的单飞锁
   后面，点「取消」之后**服务端的 cancelled 计数真的涨了**、整批落成
   ``cancelled``；再拿一份**真的会失败**的 DOCX（zip 结构完好、里面
   ``document.xml`` 被改坏，能穿过收件层、到 LibreOffice 才炸）走一遍
   「失败 → 重试 → 再失败」，服务端的 ``retry_count`` 真的涨到 1，然后
   **服务端与界面两边都不再给重试按钮**（手动重试上限是 1）。
9. **本机地址不许写进 App**（§四十九 的 ❌ 清单）—— ``src/`` 与 ``app/`` 里
   出现 ``localhost`` / ``127.0.0.1`` / ``10.0.2.2`` 的硬编码地址即为失败。
   地址只能从 ``EXPO_PUBLIC_API_BASE_URL`` 读（注释里教人怎么配不算）。

跑法（浏览器驱动用带 Playwright 的解释器，素材与产物校验用后端 venv）::

    cd backend && .venv/Scripts/python -m uvicorn main:app --host 127.0.0.1 --port 8011
    cd mobile  && npx expo start --web --port 8081
    python scripts/verify_mobile_phase11a.py

**后端要多放一个 CORS 来源**（Expo Web 在 8081，默认白名单里只有 5173）::

    FILETOOLS_CORS_ORIGINS=http://localhost:8081,http://127.0.0.1:8081

这个是环境变量，**不需要改后端代码**。

几处**踩过的坑**写在这里，免得下次又踩：

* **选文件不能直接对 `<input type=file>` 调 ``set_input_files``。**
  ``expo-document-picker`` 在 Web 上是自己造一个隐藏 input、再用
  ``dispatchEvent(new MouseEvent('click'))`` 去点它。直接塞 files 会让
  Chromium「弹一个文件选择器又立刻关掉」，input 上先冒出一个 ``cancel``，
  而 expo 的 cancel 监听器先一步 resolve 成 ``canceled``，后面真正的
  ``change`` 就被丢掉了 —— 表现是「点了选文件，什么也没发生，也不报错」。
  正确姿势是把**按钮那一下点击**包在 ``expect_file_chooser`` 里。
* Expo Web 的**滚动容器会裁剪** ``innerText``：判断某段文字在不在，
  要用 ``textContent``，不然会得出「界面上没有这句话」的错误结论。
* ``FILETOOLS_CORS_ORIGINS`` 是**逗号分隔**的，不是 JSON 数组。
  写成 JSON 会得到一个不匹配任何来源的单元素列表，预检直接 400。

可用环境变量覆盖：
    FILETOOLS_API_BASE      默认 http://127.0.0.1:8011
    FILETOOLS_APP_BASE      默认 http://localhost:8081
    FILETOOLS_BACKEND_PY    后端 venv 的 python（生成素材、打开产物）
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
MOBILE = ROOT / "mobile"
API = os.environ.get("FILETOOLS_API_BASE", "http://127.0.0.1:8011").rstrip("/")
APP = os.environ.get("FILETOOLS_APP_BASE", "http://localhost:8081").rstrip("/")

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

WORK = pathlib.Path(tempfile.mkdtemp(prefix="filetools-verify-p11a-"))
SAMPLES = WORK / "samples"
DOWNLOADS = WORK / "downloads"
REPORT = ROOT / "scripts" / "verify_mobile_phase11a_report.txt"

#: 渲染出来的文本里绝不能出现的东西（规格 §三十六）。断言打在**用户看得见的
#: 文本**上，不在源码上 —— 源码里有反斜杠（正则）是正常的。
#:
#: 注意**不含**服务器地址：「我的」页会如实显示当前连的是哪台服务器、以及在
#: 哪里改。这是个自托管的工具，使用者就是部署它的人，告诉他连的是哪儿是必要
#: 信息，不是泄漏。真正不能出现的是堆栈、文件系统路径、内部模块名。
FORBIDDEN_FRAGMENTS = (
    "Traceback",
    'File "',
    "/tmp/",
    "AppData",
    "site-packages",
    "soffice",
    "LibreOffice",
    "rapidocr",
    "onnxruntime",
    "pymupdf",
    "fitz",
    ".py'",
    '.py"',
    "backend/",
    "backend\\",
    "conversion_service",
    "task_queue",
)

#: 上面那张表里，有两条**只允许出现在「我的」页**：那一页是给部署这个 App 的人
#: 看的自检页，写着连的是哪台服务器、配置项叫什么名字。其余页面出现即为泄漏。
PROFILE_ONLY_FRAGMENTS = ("EXPO_PUBLIC_API_BASE_URL", ".env")

#: 源码里本来就不该出现的内部名字（出现即说明把服务端的东西抄进了客户端）
SOURCE_FORBIDDEN = (
    "soffice",
    "LibreOffice",
    "rapidocr",
    "onnxruntime",
    "pymupdf",
    "site-packages",
    "Traceback",
    "docx_writer",
)

#: `mobile/package.json` 里允许出现的依赖。多一个就是「擅自加了依赖」。
ALLOWED_DEPENDENCIES = {
    "expo",
    "expo-constants",
    "expo-document-picker",
    "expo-file-system",
    "expo-linking",
    "expo-router",
    "expo-sharing",
    "expo-status-bar",
    "react",
    "react-dom",
    "react-native",
    "react-native-safe-area-context",
    "react-native-screens",
    "react-native-web",
    "@react-native-async-storage/async-storage",
}

#: 明令不做的功能（§四十 与规格 §三）。搜的是界面文案里的词。
FORBIDDEN_FEATURES = (
    "会员",
    "支付",
    "登录",
    "注册",
    "订阅",
    "购买",
    "充值",
    "VIP",
    "VIP会员",
)

FORBIDDEN_FORMATS = ("MP3", "MP4", "WAV", "FLAC", "AVI", "MKV", "EPUB", "MOBI", "AZW3", "DWG", "DXF", "STL", "PSD", "CR2", "NEF", "ARW")

MOBILE_WIDTHS = (375, 390, 414)

results: list[tuple[bool, str]] = []
console_errors: list[str] = []
page_texts: dict[str, str] = {}
#: 不是断言、但必须原样进报告的事实（边界、口径、跳过的东西）。
#: 用 note 而不是硬塞一个 PASS —— 把「没做」写成「通过」就是放水。
notes: list[str] = []


def check(ok: bool, label: str) -> None:
    results.append((bool(ok), label))
    print(f"{'PASS' if ok else 'FAIL'}  {label}", flush=True)


def note(text: str) -> None:
    notes.append(text)
    print(f"NOTE  {text}", flush=True)


def section(title: str) -> None:
    print(f"\n=== {title} ===", flush=True)


# ----------------------------------------------------------------------
# 解释器与 HTTP
# ----------------------------------------------------------------------


def find_backend_python() -> pathlib.Path:
    candidates = [
        pathlib.Path(os.environ.get("FILETOOLS_BACKEND_PY", "")),
        ROOT / "backend" / ".venv" / "Scripts" / "python.exe",
        ROOT / "backend" / ".venv" / "bin" / "python",
        pathlib.Path(sys.executable),
    ]
    for item in candidates:
        if item and item.is_file():
            probe = subprocess.run(
                # 用 `pymupdf` 这个名字探测，不是 `fitz` —— 后者只是个会打
                # 弃用横幅的兼容别名，见 `inspect()` 的注释
                [str(item), "-c", "import PIL, pymupdf, docx"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            if probe.returncode == 0:
                return item
    raise SystemExit(
        "找不到能用 PIL / PyMuPDF / python-docx 的解释器。"
        "设 FILETOOLS_BACKEND_PY 指向 backend/.venv 里的 python。"
    )


def _run_backend_python(code: str, *args: object) -> str:
    proc = subprocess.run(
        [str(BACKEND_PYTHON), "-c", code, *[str(a) for a in args]],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        raise SystemExit(f"后端解释器执行失败：\n{proc.stdout}\n{proc.stderr}")
    return proc.stdout


def http(method: str, path: str, body: bytes | None = None, headers: dict | None = None):
    """发一次请求，返回 ``(状态码, 响应头, 响应体字节)``。不抛 4xx/5xx。"""
    url = path if path.startswith("http") else f"{API}{path}"
    request = urllib.request.Request(url, data=body, method=method)
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers or {}), error.read()


def get_json(path: str):
    status, _, raw = http("GET", path)
    if status != 200:
        raise SystemExit(f"GET {path} 返回 {status}，先把后端跑起来再验")
    return json.loads(raw.decode("utf-8"))


def post_multipart(path: str, fields: list[tuple[str, str]], files: list[tuple[str, pathlib.Path]]):
    """手搓 multipart —— 这里故意不用 requests（没装），也不引新依赖。"""
    boundary = "----filetools-verify-p11a"
    chunks: list[bytes] = []
    for name, value in fields:
        chunks.append(f"--{boundary}\r\n".encode())
        chunks.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        chunks.append(f"{value}\r\n".encode())
    # ⚠️ 循环变量**不能叫 path** —— 那会把上面那个 URL 参数就地覆盖掉
    for name, file_path in files:
        chunks.append(f"--{boundary}\r\n".encode())
        chunks.append(
            f'Content-Disposition: form-data; name="{name}"; filename="{file_path.name}"\r\n'.encode()
        )
        chunks.append(b"Content-Type: application/octet-stream\r\n\r\n")
        chunks.append(file_path.read_bytes())
        chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode())
    payload = b"".join(chunks)
    return http(
        "POST",
        path,
        body=payload,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )


# ----------------------------------------------------------------------
# 素材：一律用后端 venv 生成（它才有 Pillow / python-docx / PyMuPDF）
# ----------------------------------------------------------------------

_FIXTURE_CODE = r'''
import os, sys, zipfile

from PIL import Image, ImageDraw, ImageFont
import docx as docx_module

out = sys.argv[1]
os.makedirs(out, exist_ok=True)

# 1) 一张有辨识度的 JPG：四个角颜色不同，转出来一眼能看出方向对不对
img = Image.new("RGB", (480, 320), (250, 250, 250))
draw = ImageDraw.Draw(img)
draw.rectangle([0, 0, 119, 79], fill=(220, 30, 30))
draw.rectangle([360, 0, 479, 79], fill=(30, 160, 60))
draw.rectangle([0, 240, 119, 319], fill=(30, 60, 220))
draw.rectangle([360, 240, 479, 319], fill=(240, 200, 20))
img.save(os.path.join(out, "sample.jpg"), "JPEG", quality=92)

# 各源格式一份：给「每条能力的默认参数都提交一次」那一段用。
# 这 10 种要与服务端能力表里出现过的 source_type 对齐，缺一种就会有若干条
# 能力提交不了（脚本会如实报出来，不会假装通过）。
for ext, fmt in (
    ("png", "PNG"), ("webp", "WEBP"), ("bmp", "BMP"), ("gif", "GIF"), ("tiff", "TIFF"),
):
    Image.new("RGB", (64, 48), (90, 120, 200)).save(os.path.join(out, f"tiny.{ext}"), fmt)

# HEIC 要额外注册插件：Pillow 自己不认这个容器（macOS / iPhone 的默认拍照格式）
import pillow_heif
pillow_heif.register_heif_opener()
Image.new("RGB", (64, 48), (200, 90, 60)).save(os.path.join(out, "tiny.heic"), "HEIF")

# 2) 真 DOCX，带一个独一无二的句子，转成 PDF 之后要在 PDF 里找得到
document = docx_module.Document()
document.add_heading("FileTools 第十一阶段 A", level=1)
document.add_paragraph("TOKEN-DOCX-11A-7741")
document.add_paragraph("这一段用来证明 DOCX → PDF 走的是真的 LibreOffice 转换。")
document.save(os.path.join(out, "sample.docx"))

# 3) 扫描版 PDF：把文字**画成像素**再存成 PDF，没有文字层 —— 逼服务端走 OCR
page = Image.new("RGB", (1240, 1754), (255, 255, 255))
draw = ImageDraw.Draw(page)
font = None
for candidate in (r"C:\Windows\Fonts\arialbd.ttf", r"C:\Windows\Fonts\arial.ttf", r"C:\Windows\Fonts\DejaVuSans-Bold.ttf"):
    if os.path.exists(candidate):
        font = ImageFont.truetype(candidate, 96)
        break
if font is None:
    font = ImageFont.load_default()
draw.text((110, 260), "FILETOOLS OCR TEST", fill=(0, 0, 0), font=font)
draw.text((110, 420), "SCANNED PAGE 7741", fill=(0, 0, 0), font=font)
page.save(os.path.join(out, "scanned.pdf"), "PDF", resolution=150.0)

# 4) 纯文本家族
open(os.path.join(out, "sample.txt"), "w", encoding="utf-8").write(
    "FileTools 第十一阶段 A\nTOKEN-TXT-11A-7741\n纯文本转换的样张。\n"
)
open(os.path.join(out, "sample.md"), "w", encoding="utf-8").write(
    "# 第十一阶段 A\n\nTOKEN-MD-11A-7741\n\n- 能力驱动\n- 一次一个阶段\n"
)
open(os.path.join(out, "sample.html"), "w", encoding="utf-8").write(
    "<!doctype html><meta charset='utf-8'><h1>第十一阶段 A</h1>"
    "<p>TOKEN-HTML-11A-7741</p>"
)
open(os.path.join(out, "sample.svg"), "w", encoding="utf-8").write(
    "<svg xmlns='http://www.w3.org/2000/svg' width='200' height='120'>"
    "<rect width='200' height='120' fill='#4f46e5'/><circle cx='100' cy='60' r='40' fill='#fff'/>"
    "</svg>"
)

# 5) 取消那一段要一批**真任务**堆在队列里，所以来四份一模一样的 DOCX。
#    故意用四个不同的文件名：同名去重会把它们变成 sample(1).docx 之类，
#    那样虽然也是 4 项，但这条断言就不用再顺带证明去重规则了。
import shutil
for index in range(1, 5):
    shutil.copyfile(
        os.path.join(out, "sample.docx"), os.path.join(out, "queue%d.docx" % index)
    )

# 6) 重试那一段需要一个**真的会失败**的样本 —— 重试按钮只对 failed 项出现。
#
#    收件层挡得很严：截断一半的 JPEG 会被 verify_image 直接拒掉，连任务都建不出来。
#    但 office 的收件检查是**只看 zip 里条目在不在**，不读 entry 的内容，
#    所以「结构完好的 zip + 里面 document.xml 的字节被改坏」能穿过去，
#    到 LibreOffice 解压时才炸 —— 这才是真的「处理失败」，不是伪造的状态。
def _poison_ooxml(source, target_path):
    with zipfile.ZipFile(source) as archive:
        names = archive.namelist()
        payloads = {name: archive.read(name) for name in names}
    part = "word/document.xml"
    poisoned = bytearray(payloads[part])
    # 翻中间一个字节：zip 的 CRC 就对不上了，但 getinfo 照样能返回条目信息
    poisoned[len(poisoned) // 2] ^= 0xFF
    payloads[part] = bytes(poisoned)
    with zipfile.ZipFile(target_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in names:
            archive.writestr(name, payloads[name])


_poison_ooxml(os.path.join(out, "sample.docx"), os.path.join(out, "broken.docx"))

print("ok")
'''


def build_fixtures() -> None:
    SAMPLES.mkdir(parents=True, exist_ok=True)
    DOWNLOADS.mkdir(parents=True, exist_ok=True)
    _run_backend_python(_FIXTURE_CODE, SAMPLES)


def inspect(path: pathlib.Path, kind: str) -> dict:
    """用真的解码器打开产物，返回它的实际情况。

    ⚠️ **PyMuPDF 要用 `import pymupdf`，不能 `import fitz`。** 后者会往
    **stdout** 打一行 "warning: The `fitz` API is deprecated"，正好排在
    JSON 前面，`json.loads` 于是在第 1 个字符就炸 —— 而产物本身完全没问题。
    这是「诊断输出污染被诊断的输出」那一类坑，同一个教训在这台机器上已经
    栽过一次（CI 的 `pytest -rs` 顶掉 `-rfE`）。

    下面还额外做了兜底：只取 stdout 的**最后一行**，这样将来无论谁往
    stdout 打了什么横幅，都毒不到解析。
    """
    code = {
        "image": (
            "import sys, json; from PIL import Image;"
            "im = Image.open(sys.argv[1]); im.load();"
            "print(json.dumps({'format': im.format, 'size': list(im.size), 'mode': im.mode}))"
        ),
        "pdf": (
            "import sys, json, pymupdf;"
            "d = pymupdf.open(sys.argv[1]);"
            "print(json.dumps({'pages': d.page_count, 'text': ''.join(p.get_text() for p in d),"
            " 'rect': [round(d[0].rect.width,1), round(d[0].rect.height,1)]}))"
        ),
        "docx": (
            "import sys, json, docx;"
            "d = docx.Document(sys.argv[1]);"
            "print(json.dumps({'paragraphs': len(d.paragraphs),"
            " 'text': '\\n'.join(p.text for p in d.paragraphs)}))"
        ),
    }[kind]
    raw = _run_backend_python(code, path)
    lines = [line for line in raw.splitlines() if line.strip()]
    if not lines:
        raise SystemExit(f"打开 {path.name} 没有拿到任何输出（退出码却是 0）")
    return json.loads(lines[-1])


def build_mobile_driver() -> pathlib.Path:
    """把 App 自己的 `utils/options.ts` 与 `utils/errorMessages.ts` 编译出来。

    为什么非要编译：第二段要断言「**App 的**序列化结果服务端一条都不拒」。
    在脚本里用 Python 再写一份 buildPayload，测的就只是这份复制品 ——
    它跟 App 一起漂移的时候，脚本还是绿的。
    """
    out = WORK / "mobile-compiled"
    tsconfig = MOBILE / ".tsconfig.verify-11a.json"
    # **故意不 extends 应用那份 tsconfig**：它设的 `customConditions` 只有配
    # bundler/node16 才合法，而我们要的是 CJS emit，两者会直接冲突报错。
    # 这里显式列出编译纯逻辑需要的全部选项，不继承任何东西。
    #
    # `paths` 必须给：`utils/errorMessages.ts` 从 `@/services/api/client` 取
    # `CLIENT_CODES`。好在 `client.ts` 对 `@/types` 是 **type-only import**
    # （emit 时整个消失），所以产物里没有任何 `require('expo-...')`，
    # node 直接跑得起来。
    tsconfig.write_text(
        json.dumps(
            {
                "compilerOptions": {
                    "noEmit": False,
                    "module": "commonjs",
                    "moduleResolution": "node10",
                    "ignoreDeprecations": "6.0",
                    "target": "ES2022",
                    "lib": ["ES2022", "DOM"],
                    "rootDir": "src",
                    "outDir": str(out).replace("\\", "/"),
                    "baseUrl": ".",
                    "paths": {"@/*": ["src/*"]},
                    "esModuleInterop": True,
                    "declaration": False,
                    "sourceMap": False,
                    "skipLibCheck": True,
                    # `client.ts` 读了 `process.env`。`@types/node` 是模板自带的
                    # （devDependency 里那两条之外它本来就装着了，没有新增依赖），
                    # 显式点名只让它进来，免得把 react 那套环境类型也拖进来。
                    "types": ["node"],
                },
                "files": ["src/utils/options.ts", "src/utils/errorMessages.ts"],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    try:
        proc = subprocess.run(
            ["npx", "tsc", "-p", tsconfig.name],
            cwd=str(MOBILE),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=True,
        )
        if proc.returncode != 0:
            raise SystemExit(f"编译 App 的纯逻辑失败：\n{proc.stdout}\n{proc.stderr}")
    finally:
        tsconfig.unlink(missing_ok=True)

    # tsc **不会**改写 `paths` 别名：`errorMessages.ts` 里那句
    # `from '@/services/api/client'` 原样变成了 `require("@/services/api/client")`，
    # node 根本不认识 `@`。这里按每个产物文件相对于 outDir 的深度，把 `@/`
    # 换成正确的相对前缀（`utils/errorMessages.js` → `../`）。
    for emitted in out.rglob("*.js"):
        relative = os.path.relpath(out, emitted.parent).replace("\\", "/")
        prefix = "" if relative == "." else relative + "/"
        text = emitted.read_text(encoding="utf-8")
        if '"@/' in text:
            emitted.write_text(text.replace('"@/', f'"{prefix}'), encoding="utf-8")
    return out


_DRIVER_CODE = r'''
const fs = require('fs')
const path = require('path')

const [, , compiledDir, inputPath, outputPath] = process.argv
const options = require(path.join(compiledDir, 'utils', 'options.js'))
const messages = require(path.join(compiledDir, 'utils', 'errorMessages.js'))

const input = JSON.parse(fs.readFileSync(inputPath, 'utf8'))
const dynamicValues = { fonts: input.fonts || [] }

const payloads = {}
const rendered = {}
for (const capability of input.capabilities) {
  const schema = capability.options_schema || null
  const initial = options.initialValues(schema, dynamicValues)
  payloads[capability.id] = options.buildPayload(schema, initial, dynamicValues)
  rendered[capability.id] = options.visibleItems(schema, initial, dynamicValues).map((i) => i.key)
}

fs.writeFileSync(
  outputPath,
  JSON.stringify({
    known_error_codes: messages.KNOWN_ERROR_CODES,
    missing_error_codes: messages.missingErrorCodes(input.server_error_codes || []),
    payloads,
    rendered,
  }),
)
'''


def run_mobile_driver(compiled: pathlib.Path, capabilities: list[dict], config: dict) -> dict:
    driver = WORK / "driver.js"
    driver.write_text(_DRIVER_CODE, encoding="utf-8")
    payload_in = WORK / "driver-in.json"
    payload_out = WORK / "driver-out.json"
    payload_in.write_text(
        json.dumps(
            {
                "capabilities": capabilities,
                "fonts": config.get("txt_fonts", []),
                "server_error_codes": config.get("error_codes", []),
            }
        ),
        encoding="utf-8",
    )
    proc = subprocess.run(
        ["node", str(driver), str(compiled), str(payload_in), str(payload_out)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        raise SystemExit(f"跑 App 的纯逻辑失败：\n{proc.stdout}\n{proc.stderr}")
    return json.loads(payload_out.read_text(encoding="utf-8"))


# ----------------------------------------------------------------------
# 浏览器
# ----------------------------------------------------------------------


def attach(page: Page) -> None:
    def on_console(message) -> None:
        if message.type == "error":
            console_errors.append(message.text)

    page.on("console", on_console)
    page.on("pageerror", lambda error: console_errors.append(f"pageerror: {error}"))


def text_of(page: Page) -> str:
    """**不能用 innerText** —— Expo Web 的滚动容器会把视口外的文字裁掉，
    于是「界面上没有这句提示」这类结论会是假的。"""
    return page.evaluate("() => document.body.textContent || ''")


def snapshot(page: Page, name: str, *, profile: bool = False) -> None:
    text = text_of(page)
    page_texts[name] = text
    banned = list(FORBIDDEN_FRAGMENTS)
    if not profile:
        banned += list(PROFILE_ONLY_FRAGMENTS)
    hit = next((fragment for fragment in banned if fragment and fragment in text), None)
    check(hit is None, f"「{name}」页面没有泄漏堆栈 / 路径 / 内部模块名" + (f"（出现了 {hit!r}）" if hit else ""))


def pick_file(page: Page, trigger: str, path: pathlib.Path) -> None:
    """走真实用户路径选文件。

    直接对 input 调 ``set_input_files`` 是不行的 —— 见文件头的「踩过的坑」。
    """
    pattern = re.compile(re.escape(trigger))
    with page.expect_file_chooser(timeout=60_000) as chooser:
        page.get_by_role("button", name=pattern).first.click()
    chooser.value.set_files(str(path))
    page.wait_for_timeout(1200)


#: 工具页上一条工具占的高度大约是多少 —— 用来数「列表里有几条」。
#: 不去认 DOM 结构（那是实现细节，改一次布局就废），数的是**可点的行**。
COUNT_TOOL_ROWS = """() => Array.from(document.querySelectorAll('[role=button]'))
    .filter((el) => el.offsetParent !== null && /→/.test(el.textContent || '')).length"""

#: 工具页「能用了」的判据：搜索框在，且列表里已经有带箭头的工具行。
#: **不能拿 placeholder 去等** —— placeholder 是个属性，不进 `textContent`，
#: 按文本等它只会一直等到超时。
WAIT_TOOLS_READY = """() => {
  if (!document.querySelector('input[aria-label="搜索工具"]')) return false;
  return Array.from(document.querySelectorAll('[role=button]')).some(
    (el) => el.offsetParent !== null && /→/.test(el.textContent || ''));
}"""

#: 页面上占比最大的那个**不透明**底色。Expo Web 把主题底色刷在内层 div 上，
#: `document.body` 是透明的 —— 直接量 body 会得出「两个主题一样」的假结论。
DOMINANT_BACKGROUND = """() => {
  const counts = new Map();
  for (const el of document.querySelectorAll('div')) {
    const c = getComputedStyle(el).backgroundColor;
    if (!c || c === 'rgba(0, 0, 0, 0)' || c === 'transparent') continue;
    const area = el.getBoundingClientRect().width * el.getBoundingClientRect().height;
    counts.set(c, (counts.get(c) || 0) + area);
  }
  let best = null, bestArea = -1;
  for (const [color, area] of counts) if (area > bestArea) { best = color; bestArea = area; }
  return best;
}"""


def count_tool_rows(page: Page) -> int:
    return page.evaluate(COUNT_TOOL_ROWS)


#: 工具列表是虚拟列表，**一帧画不完** —— 刚进页面时可能只有十来行。
#: 数条数之前必须等到它稳定到该有的条数，否则量到的是个中间态
#: （第一版就是这么误判的：把 70 条量成了 10 条）。
COUNT_TOOL_ROWS_IS = """(n) => Array.from(document.querySelectorAll('[role=button]'))
    .filter((el) => el.offsetParent !== null && /→/.test(el.textContent || '')).length === n"""


def wait_tools_ready(page: Page, timeout: int = 180_000) -> None:
    """等到工具页可用（搜索框在、列表里至少有一行）。"""
    page.wait_for_function(WAIT_TOOLS_READY, timeout=timeout)


def wait_tool_count(page: Page, expected: int, timeout: int = 180_000) -> None:
    """等到列表**正好**是这么多条。数条数之前一定要先过这一关。"""
    page.wait_for_function(COUNT_TOOL_ROWS_IS, arg=expected, timeout=timeout)


def dominant_background(page: Page) -> str:
    return page.evaluate(DOMINANT_BACKGROUND)


def luminance(rgb: str) -> float:
    """粗略相对亮度，只用来判「哪个更暗」，不做色彩学精度。"""
    numbers = [int(n) for n in re.findall(r"\d+", rgb or "")[:3]]
    if len(numbers) < 3:
        return 0.0
    red, green, blue = numbers
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def wait_text(page: Page, needle: str, timeout: int = 300_000) -> None:
    page.wait_for_function(
        "(t) => (document.body.textContent || '').includes(t)", arg=needle, timeout=timeout
    )


def wait_settled(page: Page, timeout: int = 360_000) -> str:
    """等到整批定下来，返回最终那句标题。"""
    page.wait_for_function(
        """() => {
            const t = document.body.textContent || '';
            return t.includes('已完成') || t.includes('处理失败') || t.includes('已取消');
        }""",
        timeout=timeout,
    )
    text = text_of(page)
    for word in ("已完成", "处理失败", "已取消"):
        if word in text:
            return word
    return "未知"


def download_result(page: Page, name: str) -> pathlib.Path:
    """点「下载」，把浏览器真正落下来的文件收好。"""
    with page.expect_download(timeout=120_000) as download:
        page.get_by_role("button", name="下载").first.click()
    target = DOWNLOADS / name
    download.value.save_as(str(target))
    return target


# ----------------------------------------------------------------------
# A 段：反漂移（静态）
# ----------------------------------------------------------------------


BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)
#: 行注释，但**放过 `://`** —— 否则 `http://127.0.0.1` 会被从中间切掉，
#: 后面的代码就跟注释粘成一片，扫出来的东西全是假的。
LINE_COMMENT = re.compile(r"(?<!:)//[^\n]*")


def strip_comments(text: str) -> str:
    """去掉注释再扫。

    注释里写「例如 ``image.jpg-to-png``」是**说明**，不是把能力表抄进了客户端；
    不区分这两件事的检查只会在注释变动时乱叫，那样的检查留不住。
    """
    return LINE_COMMENT.sub("", BLOCK_COMMENT.sub("", text))


def read_source() -> dict[str, str]:
    """读 App 自己的源码，**只留代码**（注释已剥离）。"""
    files: dict[str, str] = {}
    for base in (MOBILE / "src", MOBILE / "app"):
        for path in sorted(base.rglob("*.ts*")):
            name = str(path.relative_to(MOBILE)).replace("\\", "/")
            files[name] = strip_comments(path.read_text(encoding="utf-8"))
    return files


def section_a(sources: dict[str, str], package: dict, capabilities: dict) -> None:
    section("A 反漂移：能力目录 / 依赖 / 平台差异的位置")

    # A1 能力 id 不许作为字面量出现。App 里能力 id 只应该是一个透传的字符串，
    #    写死一条就等于把服务器的矩阵抄了一份到客户端。
    id_literals: list[str] = []
    for name, text in sources.items():
        for match in re.finditer(r"""['"`]([a-z][a-z0-9]*\.[a-z0-9]+-to-[a-z0-9]+)['"`]""", text):
            id_literals.append(f"{name}: {match.group(1)}")
    check(
        not id_literals,
        f"App 源码里没有写死的能力 id 字面量（找到 {len(id_literals)} 处）"
        + (f"：{id_literals[:4]}" if id_literals else ""),
    )

    # A2 不许有手抄的格式矩阵
    matrix_words = ("TARGETS_BY_SOURCE", "SOURCE_TYPES", "EXTENSIONS_BY_SOURCE", "CAPABILITY_BY_ID")
    hits = [f"{name}: {word}" for name, text in sources.items() for word in matrix_words if word in text]
    check(not hits, f"App 里没有第二份能力矩阵（找到 {len(hits)} 处）" + (f"：{hits[:3]}" if hits else ""))

    # A3 参数面板里不许出现格式名 —— 它只认 schema
    form = sources.get("src/components/OptionForm.tsx", "")
    format_words = ("jpg", "jpeg", "png", "webp", "docx", "xlsx", "pptx", "tiff", "heic")
    in_form = [word for word in format_words if re.search(rf"""['"`]{word}['"`]""", form, re.I)]
    check(not in_form, f"参数面板不认格式名，只认 schema（找到 {in_form}）")

    # A4 没有移动端专用端点
    mobile_endpoints = [
        f"{name}"
        for name, text in sources.items()
        if "/api/mobile" in text or "mobile/convert" in text
    ]
    check(not mobile_endpoints, f"没有 /api/mobile 之类的第二条接口（{mobile_endpoints}）")

    # A5 EXPO_PUBLIC 只在一处、且必须是字面量成员表达式（babel 是字符串替换）
    env_hits = [name for name, text in sources.items() if "process.env.EXPO_PUBLIC" in text]
    check(env_hits == ["src/services/api/client.ts"], f"EXPO_PUBLIC_API_BASE_URL 只在 client.ts 读（{env_hits}）")
    client = sources.get("src/services/api/client.ts", "")
    # babel-preset-expo 做的是**纯文本替换**：`process.env.EXPO_PUBLIC_API_BASE_URL`
    # 会被换成一个字面量，而 `process.env[name]` 或解构都换不掉 —— 那样打包出来
    # 就是个 undefined，App 连不上服务器且看不出原因。这条守的就是这个。
    check(
        "process.env.EXPO_PUBLIC_API_BASE_URL" in client,
        "读法是 process.env.EXPO_PUBLIC_API_BASE_URL 这个成员表达式",
    )
    check(
        "process.env[" not in client and "} = process.env" not in client,
        "没有 process.env[...] / 解构 env 这种打包时替换不掉的读法",
    )

    # A5b 本机地址不许硬编码进 App（§四十九 的 ❌ 清单里有这一条）。
    #
    # 只在 `src/` 与 `app/` 里扫，且**先剥注释** —— 注释里写「真机上要填
    # http://10.0.2.2:8011」是在教人怎么配，不是在写死地址。
    # `.env.example` 不在此列：它就是给人抄的模板。
    LOCAL_ADDRESS = re.compile(r"https?://(?:localhost|127\.0\.0\.1|10\.0\.2\.2)[:/]")
    hardcoded = [
        f"{name}: {LOCAL_ADDRESS.search(text).group(0)}"
        for name, text in sources.items()
        if LOCAL_ADDRESS.search(text)
    ]
    check(
        not hardcoded,
        f"App 源码里没有硬编码的本机地址（找到 {hardcoded[:3]}）",
    )

    # A6 源码里不该出现服务端内部名字
    leaked = [f"{name}: {word}" for name, text in sources.items() for word in SOURCE_FORBIDDEN if word in text]
    check(not leaked, f"App 源码里没有服务端内部实现的名字（{leaked[:3]}）")

    # A7 依赖一个字都没多
    declared = set(package.get("dependencies", {}))
    extra = sorted(declared - ALLOWED_DEPENDENCIES)
    check(not extra, f"没有新增依赖（多出来的是 {extra}）")
    check(
        not set(package.get("devDependencies", {})) - {"@types/react", "typescript"},
        f"devDependencies 也只有模板自带的那两个（{sorted(package.get('devDependencies', {}))}）",
    )

    # A8 平台差异必须收在 services 层
    fs_users = [
        name
        for name, text in sources.items()
        if ("expo-file-system" in text or "expo-sharing" in text or "expo-document-picker" in text)
    ]
    offenders = [name for name in fs_users if not name.startswith("src/services/")]
    check(
        not offenders,
        f"expo-file-system / sharing / document-picker 只在 services/ 里用（越界的：{offenders}）",
    )

    # A9 触控目标
    tokens = sources.get("src/theme/tokens.ts", "")
    match = re.search(r"TOUCH_TARGET\s*=\s*(\d+)", tokens)
    check(
        match is not None and int(match.group(1)) >= 44,
        f"TOUCH_TARGET 是 {match.group(1) if match else '?'}（要求 ≥ 44）",
    )
    # 每个**自己**处理按压的组件都必须引用这个令牌。`ToolRow` 不在此列 ——
    # 它把按压整个交给了 `Card`（`<Card onPress=...>`），高度也由 Card 保证。
    # 按「组件名」一刀切会把这个正当的委托判成违规。
    pressable = ("Pressable", "TouchableOpacity", "TouchableHighlight")
    naked = [
        name
        for name, text in sources.items()
        if name.startswith("src/components/")
        and any(word in text for word in pressable)
        and "TOUCH_TARGET" not in text
    ]
    check(not naked, f"自己处理按压的组件都用了 TOUCH_TARGET 令牌（漏的：{naked}）")
    card = sources.get("src/components/Card.tsx", "")
    check(
        "Pressable" in card and "TOUCH_TARGET" in card,
        "Card 是那个统一给列表行兜住 44px 的组件（它自己按压、自己设最小高度）",
    )

    # A10 三档主题 + 跟随系统
    check(
        "export const LIGHT" in tokens and "export const DARK" in tokens,
        "深浅两套色板都在",
    )
    theme_provider = sources.get("src/theme/ThemeProvider.tsx", "")
    check("useColorScheme" in theme_provider, "「跟随系统」真的读了系统的明暗（useColorScheme）")

    # A11 轮询有界 + 卸载即停
    polling = sources.get("src/hooks/useBatchPolling.ts", "")
    # 上限必须是**一个写死的数**，不能是 Infinity / 依赖服务端给的字段
    max_poll = re.search(r"MAX_POLL_MS\s*=\s*([^\n]+)", polling or "")
    expression = max_poll.group(1).strip() if max_poll else ""
    check(
        bool(re.fullmatch(r"[0-9_\s*]+", expression)),
        f"轮询上限是一个写死的毫秒数（MAX_POLL_MS = {expression or '?'}），不是无穷",
    )
    # 30 * 60_000 = 30 分钟，与后端 task_ttl_seconds 同量级 —— 任务都过期了还在轮询
    # 就是白耗电
    minutes = re.fullmatch(r"(\d+)\s*\*\s*60_000", expression)
    check(
        minutes is not None and 1 <= int(minutes.group(1)) <= 60,
        f"轮询上限换算成分钟是 {minutes.group(1) if minutes else '?'} 分钟（要在 1–60 之间）",
    )
    # 卸载顺序：先立旗子再 abort。反过来的话 abort 触发的 rejection 会被
    # catch 到并记成一次真实的失败，用户会看到一条凭空的错误。
    stop_at = polling.find("stopped = true")
    abort_at = polling.find("abort()")
    check(
        stop_at != -1 and abort_at != -1 and stop_at < abort_at,
        f"卸载时先置 stopped 再 abort（stopped 在 {stop_at}，abort 在 {abort_at}）",
    )
    check("SETTLED_STATES" in polling, "终态即停（不是一直轮询下去）")

    # A12 历史只存元数据
    history = sources.get("src/services/history.ts", "")
    check(
        not any(word in history for word in ("base64", "arrayBuffer", "readAsStringAsync", "Blob")),
        "本地历史里没有文件内容（只有元数据）",
    )

    # A13 禁做功能一个都没有。
    #     「我的」页里有一句**否定句**：「这个 App 不含账号、登录、支付与会员功能」——
    #     它是在**声明没有**这些功能，不是有。先把这句原样摘掉再扫，同时单独断言
    #     这句必须在（否则把声明删掉就能让这条检查变绿，那是自己骗自己）。
    disclaimer = "这个 App 不含账号、登录、支付与会员功能"
    profile_source = sources.get("src/screens/ProfileScreen.tsx", "")
    check(disclaimer in profile_source, "「我的」页如实声明了不含账号 / 登录 / 支付 / 会员")
    scrubbed = {name: text.replace(disclaimer, "") for name, text in sources.items()}
    feature_hits = [
        f"{name}: {word}"
        for name, text in scrubbed.items()
        for word in FORBIDDEN_FEATURES
        if word in text
    ]
    check(not feature_hits, f"除那句声明外，没有账号 / 支付 / 会员相关的界面（{feature_hits[:3]}）")

    # A14 没让用户以为能转音视频 / 专业格式
    caps = capabilities["conversions"] + capabilities["operations"]
    advertised = {c["source_type"] for c in caps} | {c.get("target_type") for c in caps}
    sneaked = sorted(f for f in FORBIDDEN_FORMATS if f.lower() in {a.lower() for a in advertised})
    check(not sneaked, f"服务器能力表里没有被明令禁止的格式（{sneaked}）")

    # 边界如实记下来：移动端这一阶段只做 conversions 那 70 条，PDF 操作
    # （合并 / 拆分 / 压缩 / 抽页 / 删页 / 图片合成 PDF）在移动端**没有入口**。
    # 它们在 Web 端走各自的旧接口（第九阶段决策 B），不是这套 1→1 的批量模型。
    note(
        f"移动端工具列表 = 服务端 conversions 里 operation_type=conversion 的 "
        f"{sum(1 for c in capabilities['conversions'] if c['operation_type'] == 'conversion')} 条；"
        f"服务端另有 {len(capabilities['operations'])} 条 PDF 操作，本阶段移动端不提供入口。"
    )
    note(f"服务端能力总览：conversions {len(capabilities['conversions'])} 条，"
         f"operations {len(capabilities['operations'])} 条，"
         f"categories {[c['value'] for c in capabilities['categories']]}。")


# ----------------------------------------------------------------------
# B 段：与真服务器对账
# ----------------------------------------------------------------------


def section_b(capabilities: dict, config: dict, driver: dict) -> None:
    section("B 与真服务器对账：错误码 + 每条能力的默认参数")

    server_codes = config.get("error_codes", [])
    check(
        len(driver["missing_error_codes"]) == 0,
        f"App 的文案覆盖了服务端全部 {len(server_codes)} 个错误码"
        + (f"，缺 {driver['missing_error_codes']}" if driver["missing_error_codes"] else ""),
    )
    check(
        len(driver["known_error_codes"]) >= len(server_codes),
        f"本地文案表 {len(driver['known_error_codes'])} 条 ≥ 服务端 {len(server_codes)} 条",
    )

    # 每一条声明了 options_schema 的能力，都用 App 自己算出来的默认参数
    # 真提交一次。这是「表单渲染出来的东西服务端一定收」的最强形式。
    # source_type → 样张。**每一种都要有**，缺一种就等于那几条能力没验到
    fixtures = {
        "jpg": SAMPLES / "sample.jpg",
        "png": SAMPLES / "tiny.png",
        "webp": SAMPLES / "tiny.webp",
        "bmp": SAMPLES / "tiny.bmp",
        "gif": SAMPLES / "tiny.gif",
        "tiff": SAMPLES / "tiny.tiff",
        "heic": SAMPLES / "tiny.heic",
        "txt": SAMPLES / "sample.txt",
        "md": SAMPLES / "sample.md",
        "html": SAMPLES / "sample.html",
        "svg": SAMPLES / "sample.svg",
    }

    with_schema = [c for c in capabilities["conversions"] if c.get("options_schema")]
    accepted = 0
    problems: list[str] = []
    for capability in with_schema:
        source = capability["source_type"]
        fixture = fixtures.get(source)
        if fixture is None or not fixture.exists():
            problems.append(f"{capability['id']}：没有 {source} 的样张，跳过了")
            continue
        payload = driver["payloads"].get(capability["id"], {})
        fields = [
            ("target_type", capability["target_type"]),
            ("capability_id", capability["id"]),
        ]
        if payload:
            fields.append(("options", json.dumps(payload, ensure_ascii=False)))
        status, _, raw = post_multipart("/api/conversion/tasks", fields, [("files", fixture)])
        if status != 202:
            problems.append(f"{capability['id']} → {status} {raw[:160].decode('utf-8', 'replace')}")
            continue
        accepted += 1
        # 立刻取消：这一段验的是**校验层收不收**，不是转换结果。任务真跑完也无妨
        batch = json.loads(raw.decode("utf-8"))
        http("POST", f"/api/conversion/tasks/{batch['batch_id']}/cancel")

    check(
        accepted == len(with_schema) and not problems,
        f"{len(with_schema)} 条带参数的能力，App 渲染出来的默认参数服务端全部接受"
        + (f"；出问题的：{problems[:3]}" if problems else ""),
    )

    # 反过来：服务端不认的键，App 也不该发出去（默认值里绝不能出现隐藏项）
    schema_by_id = {c["id"]: c for c in with_schema}
    leaked: list[str] = []
    # 只对**有 schema 的那 57 条**比对：另外 13 条本来就没有参数，
    # 拿它们去查 schema_by_id 只会 KeyError，不是发现了一个问题
    for capability_id, payload in driver["payloads"].items():
        spec = schema_by_id.get(capability_id)
        if spec is None:
            if payload:
                leaked.append(f"{capability_id}（没有 schema 却提交了 {sorted(payload)}）")
            continue
        spec_keys = {item["key"] for item in spec["options_schema"]["items"]}
        for key in payload:
            if key not in spec_keys:
                leaked.append(f"{capability_id}.{key}")
    check(not leaked, f"提交的键都在那条能力的 schema 里（越界的：{leaked[:3]}）")

    # 隐藏项必须真的被丢掉。`buildPayload` 会丢掉看不见的键，所以
    # **提交的键集必须是渲染出来的可见键集的子集**。多出来的那个键就是
    # 一个「用户根本没机会设置、却被我们替他填了个值」的参数。
    #
    # 服务端这一侧是**不检查**隐藏项的（它只认 schema 里有没有这个键），
    # 所以漏发隐藏键这件事只有在客户端才拦得住 —— 这条断言就长在客户端上。
    stray: list[str] = []
    hidden_but_sent: list[str] = []
    for capability in with_schema:
        payload = driver["payloads"].get(capability["id"], {})
        rendered = set(driver["rendered"].get(capability["id"], []))
        for key in payload:
            if key not in rendered:
                stray.append(f"{capability['id']}.{key}")
        for item in capability["options_schema"]["items"]:
            conditions = item.get("visible_when")
            if not conditions or item["key"] not in rendered:
                continue
            for dependency, expected in conditions.items():
                if str(payload.get(dependency)) != str(expected):
                    hidden_but_sent.append(
                        f"{capability['id']}.{item['key']}（{dependency}={payload.get(dependency)}）"
                    )
    check(
        not stray,
        f"提交的键都是界面上真的渲染出来的（多出来的：{stray[:3]}）",
    )
    check(
        not hidden_but_sent,
        f"visible_when 不成立的项一个都没被提交（越界的：{hidden_but_sent[:3]}）",
    )

    # 服务端自己也要能说出这些能力：矩阵里的每一行都能在 App 的搜索里找到归属分类
    check(
        all(c["category"] in {item["value"] for item in capabilities["categories"]} for c in capabilities["conversions"]),
        "每条能力的 category 都在服务端给的 categories 里（App 的分类标签完全来自这里）",
    )


# ----------------------------------------------------------------------
# C 段：三条真实链路
# ----------------------------------------------------------------------


def open_tool(page: Page, source: str, target: str) -> None:
    """搜索 → 点进那条工具的参数页。

    每一步都等**真的发生了**再走下一步，不用固定 sleep 猜时间：搜索框是
    onChange 即时过滤的，sleep 短了会点到还没过滤完的列表。
    """
    page.goto(f"{APP}/tools", wait_until="load", timeout=180_000)
    wait_tools_ready(page)
    page.get_by_label("搜索工具").fill(source)
    name = f"{source.upper()} → {target.upper()}"
    row = page.get_by_role("button", name=name).first
    row.wait_for(state="visible", timeout=60_000)
    row.click()
    page.wait_for_url(re.compile(r".*/convert/"), timeout=60_000)
    # 参数页要先把能力信息拉回来才画得出表单（「文件」那一栏在，说明画出来了）
    wait_text(page, "文件", timeout=60_000)


def section_c(page: Page, capabilities: dict) -> None:
    section("C 三条真实链路（真界面 → 真后端 → 真产物）")
    ids = {c["id"] for c in capabilities["conversions"]}

    # ---- C1 JPG → PNG ----
    check("image.jpg-to-png" in ids, "服务端有 image.jpg-to-png 这条能力")
    open_tool(page, "JPG", "PNG")
    pick_file(page, "选择文件", SAMPLES / "sample.jpg")
    page.get_by_role("button", name="开始转换").first.click()
    page.wait_for_url(re.compile(r".*/task/"), timeout=120_000)
    settled = wait_settled(page)
    check(settled == "已完成", f"C1 JPG→PNG 完成（界面显示「{settled}」）")
    text = text_of(page)
    check("sample.jpg" in text, "结果卡上是用户选的那个真文件名")

    png_path = download_result(page, "c1-result.png")
    check(png_path.stat().st_size > 0, f"C1 下载到的文件不是空的（{png_path.stat().st_size} 字节）")
    info = inspect(png_path, "image")
    check(info["format"] == "PNG", f"C1 产物真的是 PNG（解码器说 {info['format']}）")
    check(info["size"] == [480, 320], f"C1 产物尺寸与源一致（{info['size']}）")
    check("已保存到本机" in text_of(page), "C1 下载后按钮变成「已保存到本机」，没有第二次网络下载")

    # 一次性令牌：这个地址必须真的再也取不到。两路一起证 ——
    # 一路是直接打服务端（协议层），一路是**重新打开这个任务页**看界面
    # 是不是也如实说了「已经被下载过」（用户层）。只证前者的话，
    # 界面万一还挂着一个点了就 404 的按钮，用户照样被坑。
    batch_id = page.url.rsplit("/", 1)[-1]
    _, _, raw = http("GET", f"/api/conversion/tasks/{batch_id}")
    task_json = json.loads(raw.decode("utf-8"))
    url = (task_json.get("result") or {}).get("download_url")
    check(url is None, "下载之后，服务端快照里的整包地址已经撤下（不能再下第二次）")

    page.goto(f"{APP}/task/{batch_id}", wait_until="load", timeout=120_000)
    wait_text(page, "下载过", timeout=60_000)
    check(
        "结果已经被下载过或已过期" in text_of(page),
        "重新打开这个任务页，界面如实说「已经被下载过」，没有留一个点了就 404 的按钮",
    )
    snapshot(page, "任务页（已下载过）")
    page.goto(f"{APP}/tools", wait_until="load", timeout=120_000)

    # ---- C2 DOCX → PDF（LibreOffice）----
    check("document.docx-to-pdf" in ids, "服务端有 document.docx-to-pdf 这条能力")
    open_tool(page, "DOCX", "PDF")
    pick_file(page, "选择文件", SAMPLES / "sample.docx")
    page.get_by_role("button", name="开始转换").first.click()
    page.wait_for_url(re.compile(r".*/task/"), timeout=180_000)
    settled = wait_settled(page)
    check(settled == "已完成", f"C2 DOCX→PDF 完成（界面显示「{settled}」）")

    pdf_path = download_result(page, "c2-result.pdf")
    info = inspect(pdf_path, "pdf")
    check(info["pages"] >= 1, f"C2 产物真的是 PDF，{info['pages']} 页")
    flat = re.sub(r"\s+", "", info["text"])
    check("TOKEN-DOCX-11A-7741" in flat, "C2 PDF 里真的找得到 DOCX 里那句话（不是只转了个空壳）")

    # ---- C3 PDF → DOCX（OCR 扫描件）----
    check("pdf.pdf-to-docx" in ids, "服务端有 pdf.pdf-to-docx 这条能力")
    open_tool(page, "PDF", "DOCX")
    pick_file(page, "选择文件", SAMPLES / "scanned.pdf")
    page.get_by_role("button", name="开始转换").first.click()
    page.wait_for_url(re.compile(r".*/task/"), timeout=180_000)
    settled = wait_settled(page, timeout=600_000)
    check(settled == "已完成", f"C3 PDF→DOCX（OCR）完成（界面显示「{settled}」）")

    docx_path = download_result(page, "c3-result.docx")
    info = inspect(docx_path, "docx")
    flat = re.sub(r"\s+", "", info["text"]).upper()
    check(info["paragraphs"] >= 1, f"C3 产物真的是 docx，{info['paragraphs']} 段")
    check(
        "FILETOOLS" in flat and "7741" in flat,
        "C3 OCR 真的认出了扫描件上的字（找到 FILETOOLS 与 7741）",
    )
    snapshot(page, "结果页")


# ----------------------------------------------------------------------
# D 段：界面行为
# ----------------------------------------------------------------------


def section_d(page: Page, capabilities: dict) -> None:
    section("D 界面行为：空态 / 主题 / 历史 / 窄屏 / 触控")

    # 工具页列的是 conversions 里 operation_type=conversion 的那些（与
    # `CapabilitiesProvider` 的过滤条件逐字一致）；PDF 操作是另一条路，不进这个列表
    def expected_rows(category: str | None = None) -> int:
        return sum(
            1
            for c in capabilities["conversions"]
            if c["operation_type"] == "conversion"
            and (category is None or c["category"] == category)
        )

    total_rows = expected_rows()

    # D1 工具页：搜不到东西时的空态是「说人话」，不是白屏
    page.goto(f"{APP}/tools", wait_until="load", timeout=120_000)
    wait_tools_ready(page)
    # 虚拟列表一帧画不完，**先等它涨到该有的条数**再拿它当基准
    wait_tool_count(page, total_rows)
    page.get_by_label("搜索工具").fill("zzzz-no-such-format")
    page.wait_for_timeout(900)
    text = text_of(page)
    check("没有匹配" in text, "搜不到时给的是空态文案（没有的话用户只看到一片白）")
    check("zzzz-no-such-format" in text, "空态里回显了用户搜的词")
    check(count_tool_rows(page) == 0, "搜到一个都不剩时列表真的空了，不是留着旧结果")
    snapshot(page, "工具页空态")
    page.get_by_label("搜索工具").fill("")
    wait_tool_count(page, total_rows)
    check(count_tool_rows(page) == total_rows, f"清空搜索后 {total_rows} 条工具都回来了")

    # D2 分类标签来自服务端，且真的在过滤（点一下列表必须变短）
    labels = [c["label"] for c in capabilities["categories"]]
    shortened = 0
    for category in capabilities["categories"]:
        page.get_by_role("radio", name=re.compile(re.escape(category["label"]))).first.click()
        expected = expected_rows(category["value"])
        wait_tool_count(page, expected)
        filtered = count_tool_rows(page)
        check(
            filtered == expected,
            f"点「{category['label']}」后列表正好是服务端那一类（{filtered} 条，服务端说 {expected} 条）",
        )
        shortened += 1 if filtered < total_rows else 0
    check(
        shortened == len(labels),
        f"服务端给的 {len(labels)} 个分类标签，{shortened} 个真的把列表筛短了",
    )
    page.get_by_role("radio", name=re.compile("全部")).first.click()
    wait_tool_count(page, total_rows)

    # D3 主题：深色真的把底色变暗了。
    #     **不能量 document.body** —— Expo Web 的底色挂在内层 div 上，
    #     body 是透明的。取的是页面上占比最大的那个不透明底色。
    page.goto(f"{APP}/profile", wait_until="load", timeout=120_000)
    wait_text(page, "外观", timeout=60_000)
    light_rgb = dominant_background(page)
    page.get_by_role("radio", name=re.compile("深色")).first.click()
    page.wait_for_timeout(1200)
    dark_rgb = dominant_background(page)
    check(light_rgb != dark_rgb, f"切到深色后底色真的换了（{light_rgb} → {dark_rgb}）")
    check(
        luminance(dark_rgb) < luminance(light_rgb),
        f"深色确实更暗（亮度 {luminance(light_rgb):.0f} → {luminance(dark_rgb):.0f}）",
    )
    page.get_by_role("radio", name=re.compile("浅色")).first.click()
    page.wait_for_timeout(1000)
    check(dominant_background(page) == light_rgb, "切回浅色能还原成原来那个底色")
    page.get_by_role("radio", name=re.compile("跟随系统")).first.click()
    page.wait_for_timeout(800)
    check(dominant_background(page) == light_rgb, "「跟随系统」在浅色系统下就是浅色（本机系统是浅色）")

    text = text_of(page)
    check("错误码文案与服务端一致，没有漏配。" in text, "「我的」页的自检说错误码文案齐了")
    check("不含账号、登录、支付与会员" in text, "「我的」页如实写明没有账号 / 支付 / 会员")
    snapshot(page, "我的", profile=True)

    # D4 历史：只存元数据，且真的记下来了
    page.goto(f"{APP}/history", wait_until="load", timeout=120_000)
    wait_text(page, "历史")
    page.wait_for_timeout(1200)
    text = text_of(page)
    check("sample" in text or "还没有" in text, "历史页要么列出了刚才那几次，要么如实说没有")
    stored = page.evaluate(
        "() => { try { return localStorage.getItem('filetools.history') } catch (e) { return null } }"
    )
    check(stored is not None, "历史真的写进了本机存储（AsyncStorage → localStorage）")
    if stored:
        entries = json.loads(stored)
        keys = set()
        for entry in entries:
            keys |= set(entry)
        check(
            keys <= {
                "batchId", "filename", "sourceType", "targetType", "capabilityId",
                "total", "status", "createdAt", "resultFilename", "resultExpired",
            },
            f"历史条目只有元数据字段（实际：{sorted(keys)}）",
        )
        check(len(stored) < 20_000, f"历史整包只有 {len(stored)} 字节 —— 没有把文件塞进去")
    snapshot(page, "历史")

    # D5 最近处理：刚跑完的三条要出现在首页
    page.goto(APP, wait_until="load", timeout=120_000)
    wait_text(page, "最近处理", timeout=120_000)
    page.wait_for_timeout(1500)
    home_text = text_of(page)
    check("sample" in home_text, "首页「最近处理」列出了刚才那三次")
    check("还没有处理过文件" not in home_text, "有记录了就不再显示空态")

    # D6 窄屏：三档都不能横向溢出
    for width in MOBILE_WIDTHS:
        page.set_viewport_size({"width": width, "height": 844})
        page.wait_for_timeout(800)
        # 每一页都等它**画完**再量。在骨架态量宽度等于没量 ——
        # 那时候溢出的那个元素还没渲染出来，量出来永远是 0。
        for route, marker in (
            ("/", "选一个工具开始"),
            ("/tools", None),
            ("/history", "历史"),
            ("/profile", "外观"),
        ):
            page.goto(f"{APP}{route}", wait_until="load", timeout=120_000)
            if marker is None:
                wait_tools_ready(page)
                wait_tool_count(page, expected_rows())
            else:
                wait_text(page, marker, timeout=120_000)
            page.wait_for_timeout(600)
            overflow = page.evaluate(
                "() => document.documentElement.scrollWidth - document.documentElement.clientWidth"
            )
            check(overflow <= 1, f"{width}px 下 {route} 没有横向溢出（多出 {overflow}px）")

    # D7 触控目标实测
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(f"{APP}/tools", wait_until="load", timeout=120_000)
    wait_tools_ready(page)
    wait_tool_count(page, expected_rows())
    heights = page.evaluate(
        """() => Array.from(document.querySelectorAll('[role=button],[role=radio],input'))
            .filter((el) => el.offsetParent !== null)
            .map((el) => ({ h: Math.round(el.getBoundingClientRect().height),
                            t: (el.textContent || el.getAttribute('aria-label') || el.tagName).slice(0, 20) }))"""
    )
    small = [item for item in heights if item["h"] < 44]
    check(
        not small,
        f"工具页上每个可点控件都 ≥44 高（量了 {len(heights)} 个，矮的：{small[:3]}）",
    )


# ----------------------------------------------------------------------
# E 段：任务生命周期 —— 取消与重试（§四十九 的「Cancel 可用 / Retry 可用」）
# ----------------------------------------------------------------------


#: 服务端整批状态 -> 界面上那句标题。取自 ``src/utils/labels.ts`` 的
#: ``STATUS_LABELS``：界面照抄服务端的结论，不许自己另算一套
#: （那正是「界面说成功、其实失败了」的来源）。
STATUS_LABELS = {
    "queued": "等待中",
    "processing": "处理中",
    "completed": "已完成",
    "failed": "失败",
    "cancelled": "已取消",
}
TERMINAL_STATUSES = ("completed", "failed", "cancelled")


def batch_snapshot(page: Page) -> dict:
    """自己打接口拿快照。界面上的说法与它必须对得上，**裁判是服务端**。"""
    batch_id = page.url.rstrip("/").rsplit("/", 1)[-1]
    return get_json(f"/api/conversion/tasks/{batch_id}")


def wait_batch_settled(page: Page, timeout: int = 600_000) -> dict:
    """轮询服务端直到整批进终态。

    不用固定 sleep 猜时间：LibreOffice 那一项跑多久取决于机器，
    猜短了会拿到中间态、猜长了白等。轮询到为止，上限兜底。
    """
    waited = 0
    snapshot = batch_snapshot(page)
    while snapshot.get("status") not in TERMINAL_STATUSES and waited < timeout:
        page.wait_for_timeout(1000)
        waited += 1000
        snapshot = batch_snapshot(page)
    return snapshot


def pick_files(page: Page, trigger: str, paths: list[pathlib.Path]) -> None:
    """一次选多个文件 —— ``set_files`` 收列表就等于真人按住 Ctrl 多选。"""
    pattern = re.compile(re.escape(trigger))
    with page.expect_file_chooser(timeout=60_000) as chooser:
        page.get_by_role("button", name=pattern).first.click()
    chooser.value.set_files([str(item) for item in paths])
    page.wait_for_timeout(1200)


def section_e(page: Page) -> None:
    section("E 任务生命周期：取消与重试（真点按钮、真改服务端状态）")

    # ---- E1 取消 ---------------------------------------------------------
    #
    # 为什么拿 DOCX→PDF 做这个：它走 LibreOffice 的**单飞锁**，同一时刻只有
    # 一个转换在跑，每个十秒上下。四份文件必然堆出「还在排队」的积压，取消
    # 才有东西可取消。换成图片的话几个 worker 一起上，任务说不定已经跑完，
    # 那这条断言就变成碰运气 —— 那是放水。
    open_tool(page, "DOCX", "PDF")
    pick_files(page, "选择文件", [SAMPLES / f"queue{index}.docx" for index in range(1, 5)])
    page.get_by_role("button", name="开始转换").first.click()
    page.wait_for_url(re.compile(r".*/task/"), timeout=180_000)

    submitted = batch_snapshot(page)
    check(
        len(submitted.get("tasks", [])) == 4,
        f"E1 一次提交 4 个文件，服务端确实建了 4 项（实际 {len(submitted.get('tasks', []))} 项）",
    )

    # 立刻取消。等久了任务自己跑完，验的就不是「取消」了。
    # 页面先是「正在获取任务状态…」的骨架态（那时还没有按钮），等它画出来。
    wait_text(page, "取消", timeout=60_000)
    cancel = page.get_by_role("button", name="取消")
    check(cancel.count() > 0, "E1 处理中的任务页上有「取消」按钮")
    if cancel.count() == 0:
        snapshot(page, "本该有取消按钮的任务页")
        return
    cancel.first.click()

    # 界面必须**如实说「已经请求取消」**：排队中的会立刻停，正在跑的那个
    # 停不下来。这一句是产品对用户的承诺，不能省成一个「已取消」了事。
    wait_text(page, "已经请求取消", timeout=120_000)
    check(
        "已经请求取消" in text_of(page),
        "E1 点取消后，界面如实说「还在排队的会立刻停，正在处理的这个要跑完才能停」",
    )

    after = wait_batch_settled(page)
    cancelled = after.get("cancelled", 0)
    check(cancelled >= 1, f"E1 取消真的落到了服务端（cancelled={cancelled}）")
    check(
        after.get("status") == "cancelled",
        f"E1 整批最终状态是 cancelled（服务端说 {after.get('status')}）",
    )

    expected = STATUS_LABELS.get(after.get("status", ""), "?")
    page.wait_for_function(
        "(t) => (document.body.textContent || '').includes(t)",
        arg=expected,
        timeout=120_000,
    )
    shown = text_of(page)
    check(
        expected in shown,
        f"E1 界面结论与服务端一致（服务端 {after.get('status')}，界面出现「{expected}」）",
    )
    # 整批报销的任务**不该**再挂着「下载」按钮 —— 一个点了只会 404 的按钮
    # 比没有按钮更糟。
    check(
        page.get_by_role("button", name="下载").count() == 0,
        "E1 取消掉的批次没有留下任何「下载」按钮",
    )
    note(
        f"E1 取消后服务端计数：completed={after.get('completed')} / failed={after.get('failed')} / "
        f"cancelled={cancelled} / total={after.get('total')}。正在跑的那一项停不下来是"
        "**设计如此**（取消是协作式的），界面也如实这么写，没有假装它被中断了。"
    )
    snapshot(page, "取消后的任务页")

    # ---- E2 重试 ---------------------------------------------------------
    #
    # 重试按钮只对 failed 项出现，所以需要一个**真的失败过**的样本。
    # 收件层挡得住截断的图片（``verify_image`` 直接拒），但 office 的收件
    # 检查**只看 zip 里条目在不在、不读条目内容** —— 于是「zip 结构完好、
    # 里面 document.xml 被改坏」能穿过去，到 LibreOffice 解压才炸。
    # 这才是真的「处理失败」，不是伪造出来的状态。
    open_tool(page, "DOCX", "PDF")
    pick_file(page, "选择文件", SAMPLES / "broken.docx")
    page.get_by_role("button", name="开始转换").first.click()
    page.wait_for_url(re.compile(r".*/task/"), timeout=180_000)

    failed = wait_batch_settled(page)
    check(
        failed.get("status") == "failed",
        f"E2 坏文件真的失败了（服务端说 {failed.get('status')}），不是假装成功",
    )
    retryable = [item for item in failed.get("tasks", []) if item.get("can_retry")]
    check(
        bool(retryable),
        "E2 服务端把这个失败项标成可重试"
        f"（can_retry={[item.get('can_retry') for item in failed.get('tasks', [])]}）",
    )
    page.wait_for_function(
        "(t) => (document.body.textContent || '').includes(t)",
        arg=STATUS_LABELS["failed"],
        timeout=120_000,
    )
    check(
        STATUS_LABELS["failed"] in text_of(page),
        f"E2 界面上如实显示「{STATUS_LABELS['failed']}」",
    )
    snapshot(page, "失败后的任务页")

    retry = page.get_by_role("button", name="重试")
    have_retry = retry.count() > 0
    check(have_retry, "E2 界面上出现了「重试」按钮")
    if not have_retry:
        # 没有按钮就没什么可点的了。**如实记下失败并收工**，不去点一个
        # 不存在的元素 —— 那只会抛异常，把后面本该跑到的断言一起吞掉。
        snapshot(page, "本该有重试按钮的任务页")
        return

    retry.first.click()
    # 重试必须**真的把这一项排回队列**，不是界面自己换个样子。判据是
    # 「失败」这句从界面上消失 —— 它只会在这一项重新入队时消失
    # （``retryTask`` 之后 ``polling.refresh()`` 立刻拉一次新快照）。
    page.wait_for_function(
        "(t) => !(document.body.textContent || '').includes(t)",
        arg=STATUS_LABELS["failed"],
        timeout=180_000,
    )

    again = wait_batch_settled(page)
    check(
        again.get("status") == "failed",
        f"E2 重试之后又真跑了一次并再次失败（服务端说 {again.get('status')}）",
    )
    used = [item.get("retry_count", 0) for item in again.get("tasks", [])]
    check(max(used or [0]) >= 1, f"E2 服务端记下了这次手动重试（retry_count={used}）")

    # ``MAX_ITEM_RETRIES = 1``：手动重试的机会用完了，就不该再挂一个
    # 点了只会报「这一项已经重试过」的按钮。「不留无效按钮」是硬要求，
    # 这条专门守它 —— 服务端与界面**两边都要**不再允许。
    holding = [item for item in again.get("tasks", []) if item.get("can_retry")]
    check(not holding, "E2 服务端不再允许重试这一项（手动重试次数已用完）")
    page.wait_for_timeout(1500)
    check(
        page.get_by_role("button", name="重试").count() == 0,
        "E2 界面上没有残留的「重试」按钮（不留无效按钮）",
    )
    snapshot(page, "重试之后的任务页")


# ----------------------------------------------------------------------
# 汇总
# ----------------------------------------------------------------------


def write_report() -> tuple[int, int]:
    passed = sum(1 for ok, _ in results if ok)
    failed = [label for ok, label in results if not ok]
    lines = [
        "第十一阶段 A（移动 App 基础框架）真机验收报告",
        f"App：{APP}",
        f"后端：{API}",
        f"通过：{passed} / {len(results)}",
        "",
    ]
    if notes:
        lines.append("如实记录（不是断言）：")
        lines.extend(f"  · {item}" for item in notes)
        lines.append("")
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


def preflight() -> tuple[dict, dict]:
    """先把两件必需品确认在跑，免得跑到一半才发现连不上。"""
    status, _, _ = http("GET", "/api/health")
    if status != 200:
        raise SystemExit(f"后端 {API} 没起来（/api/health → {status}）")
    status, _, raw = http("GET", f"{APP}/")
    if status != 200:
        raise SystemExit(f"Expo Web {APP} 没起来（→ {status}）")
    return get_json("/api/conversion/capabilities"), get_json("/api/config")


def main() -> int:
    global BACKEND_PYTHON

    capabilities, config = preflight()
    print(f"后端能力表：{len(capabilities['conversions'])} 条 conversion，"
          f"{len(capabilities['operations'])} 条 operation", flush=True)

    BACKEND_PYTHON = find_backend_python()
    build_fixtures()
    compiled = build_mobile_driver()
    driver = run_mobile_driver(compiled, capabilities["conversions"], config)

    sources = read_source()
    package = json.loads((MOBILE / "package.json").read_text(encoding="utf-8"))

    section_a(sources, package, capabilities)
    section_b(capabilities, config, driver)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(
            viewport={"width": 390, "height": 844},
            is_mobile=True,
            has_touch=True,
            accept_downloads=True,
        )
        page = context.new_page()
        attach(page)
        page.goto(APP, wait_until="load", timeout=180_000)
        # 首页要等它真的把能力表拉回来：首屏在拿到 /capabilities 之前是骨架态
        wait_text(page, "选一个工具开始", timeout=180_000)
        page.wait_for_timeout(1500)
        snapshot(page, "首页")
        section_c(page, capabilities)
        section_d(page, capabilities)
        section_e(page)
        browser.close()

    return summary()


if __name__ == "__main__":
    sys.exit(main())
