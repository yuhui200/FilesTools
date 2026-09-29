"""第九阶段 9a 的真机验收：后端能力层，对着跑着的 :8011 走一遍。

单元测试验的是函数，这里验的是**服务**：能力矩阵接口、multipart 上传、
任务队列、worker 池、下载令牌、以及 `/api/pdf/*` 那一组操作接口。
中间隔着 FastAPI 的表单解析、队列、线程池与下载路由，任何一环接错都会
在这里现形，而不会在单元测试里现形。

**产物一律真打开**：图片用 ``PIL.Image.open`` 看格式 / 尺寸 / 像素 / EXIF，
PDF 用 PyMuPDF 看页数 / 页面尺寸 / 图片落点 / 页面文字，ZIP 用 ``zipfile``
逐个读进去。只看 HTTP 200 说明不了任何事 —— 转出一张损坏的图同样是 200。

覆盖 9a 的八块：

1. 能力矩阵接口（§十五/§十六，前端的唯一能力来源）
2. 新格式互转 + PNG→ICO（§七）
3. 图片选项真的生效（§九：质量 / 尺寸 / DPI / 旋转 / 元数据）
4. 多帧 GIF 与多页 TIFF 只取第一帧、并如实说明（决策 C）
5. 图片 → PDF 的排版几何（§十）
6. 六个 PDF 操作（决策 B）
7. 安全：伪造文件、越界参数、穿越文件名（§四十五/§四十六）
8. 所有失败信息都不许泄露内部细节（§三十四）
9. 混批下的队列与 worker 健康：不超订、不死锁、不卡死、不重复（§五十八）

跑法（必须用后端 venv，它才有 PIL / PyMuPDF / httpx）::

    cd backend && .venv/Scripts/python.exe ../scripts/verify_phase9a_live.py
"""

from __future__ import annotations

import io
import sys
import time
import zipfile

import httpx
import pymupdf
from docx import Document
from PIL import Image

BASE = "http://127.0.0.1:8011"
API = f"{BASE}/api/conversion"
PDF_API = f"{BASE}/api/pdf"
CAPABILITIES = f"{API}/capabilities"

#: 任务的终止状态。「完成」用的是 ``completed`` 而不是 ``succeeded`` ——
#: 这套词表由第八阶段的任务队列定义，验的时候按它的来。
TERMINAL = ("completed", "failed", "cancelled")

#: 毫米 -> PDF 点。页边距与自定义页面尺寸都要用它换算。
MM = 72.0 / 25.4

_failures: list[str] = []
_checks = 0


def check(ok: bool, what: str) -> None:
    global _checks
    _checks += 1
    print(("PASS  " if ok else "FAIL  ") + what, flush=True)
    if not ok:
        _failures.append(what)


def check_clean(message: str, label: str) -> None:
    """失败信息里不许出现异常栈 / 服务器路径 / 内部模块名（§三十四）。"""
    text = str(message)
    leaked = [
        token
        for token in ("Traceback", "File \"", ".py\", line", "site-packages",
                      "PIL", "pymupdf", "fitz", "docx", "libreoffice", "soffice",
                      "C:\\", "D:\\", "/tmp/", "/home/", "\\\\")
        if token in text
    ]
    check(not leaked, f"{label}：信息干净（泄露={leaked} 原文={text[:70]!r}）")


# ----------------------------------------------------------------------
# 造样本
# ----------------------------------------------------------------------

RED, GREEN, BLUE, YELLOW, BLACK = (
    (255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0), (0, 0, 0),
)


def encode(image: Image.Image, fmt: str, **kwargs) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, fmt, **kwargs)
    return buffer.getvalue()


def quadrants(width: int, height: int, fmt: str = "PNG", **kwargs) -> bytes:
    """四象限四色 + 左上角一个黑点。

    旋转与缩放都只能靠**像素位置**来判断方向对不对 —— 尺寸对而方向反
    是最容易漏的一种错，只看长宽是看不出来的。
    """
    image = Image.new("RGB", (width, height), (255, 255, 255))
    pixels = image.load()
    for y in range(height):
        for x in range(width):
            if x < width // 2 and y < height // 2:
                pixels[x, y] = RED
            elif x >= width // 2 and y < height // 2:
                pixels[x, y] = GREEN
            elif x < width // 2:
                pixels[x, y] = BLUE
            else:
                pixels[x, y] = YELLOW
    pixels[2, 2] = BLACK
    return encode(image, fmt, **kwargs)


def noisy(width: int, height: int, fmt: str = "PNG", **kwargs) -> bytes:
    """有噪点的图 —— 纯色图压缩率过高，比不出体积差异。"""
    base = Image.linear_gradient("L").resize((width, height)).convert("RGB")
    noise = Image.effect_noise((width, height), 60).convert("L")
    blended = Image.blend(base, Image.merge("RGB", (noise, noise, noise)), 0.4)
    return encode(blended, fmt, **kwargs)


def multiframe(fmt: str, frames: int, size: tuple[int, int] = (120, 80)) -> bytes:
    """货真价实的多帧 / 多页文件。"""
    images = [
        Image.new("RGB", size, (40 * index, 90, 200 - 40 * index))
        for index in range(frames)
    ]
    buffer = io.BytesIO()
    images[0].save(
        buffer, fmt, save_all=True, append_images=images[1:], duration=100, loop=0
    )
    return buffer.getvalue()


def labeled_pdf(pages: int, label: str = "") -> bytes:
    """每页写着不同文字的 PDF —— 页序只能靠内容才验得出来。

    每页写两行：一行 ASCII 标记（用于精确断言），一行中文（``china-s``
    是 MuPDF 自带的中文字体，能用 ``get_text()`` 原样读回来）。
    中文那行是必要的：真实用户的 PDF 是中文的，只测 ASCII 等于没测
    最容易出问题的那条路。
    """
    doc = pymupdf.open()
    try:
        for index in range(1, pages + 1):
            page = doc.new_page()
            page.insert_text((72, 100), f"{label}PAGE{index} MARK{index}", fontsize=18)
            page.insert_text((72, 140), f"第{index}页 中文内容 {label}",
                             fontsize=18, fontname="china-s")
        return doc.tobytes()
    finally:
        doc.close()


# ----------------------------------------------------------------------
# HTTP 小工具
# ----------------------------------------------------------------------

def convert(
    client: httpx.Client,
    filename: str,
    content: bytes,
    target: str,
    options: str | None = None,
    *,
    media_type: str = "application/octet-stream",
):
    """走统一转换队列，返回 ``(status_code, body)``。"""
    data = {"target_type": target}
    if options is not None:
        data["options"] = options
    response = client.post(
        f"{API}/tasks",
        files={"files": (filename, io.BytesIO(content), media_type)},
        data=data,
    )
    if response.status_code != 202:
        return response.status_code, response.json()
    batch_id = response.json()["batch_id"]
    deadline = time.time() + 180
    while time.time() < deadline:
        body = client.get(f"{API}/tasks/{batch_id}").json()
        if all(item["status"] in TERMINAL for item in body["tasks"]):
            return 200, body
        time.sleep(0.3)
    raise SystemExit(f"任务超时未结束：{batch_id}")


def produce(client: httpx.Client, filename: str, content: bytes, target: str,
            options: str | None = None, label: str = "") -> tuple[bytes, dict]:
    """转换并下载结果；失败直接终止 —— 这一步失败说明前置条件就不成立。"""
    status, body = convert(client, filename, content, target, options)
    if status != 200:
        raise SystemExit(f"[{label or filename}→{target}] 提交被拒 {status}: {body}")
    task = body["tasks"][0]
    if task["status"] != "completed":
        raise SystemExit(
            f"[{label or filename}→{target}] 任务失败："
            f"{task['error_code']} {task['error_message']}"
        )
    url = (task.get("result") or {}).get("download_url")
    if not url:
        raise SystemExit(f"[{label or filename}→{target}] 没有下载地址：{task}")
    response = client.get(f"{BASE}{url}")
    if response.status_code != 200:
        raise SystemExit(f"[{label or filename}→{target}] 下载失败 {response.status_code}")
    return response.content, task


def rejected(
    client: httpx.Client, filename: str, content: bytes, target: str,
    options: str | None, label: str,
) -> str:
    """提交一个应当被拒的请求，返回面向用户的那句话。

    校验发生在哪一层是实现自由 —— 表单层直接 400 比排进队列再失败更好，
    所以**两种都算被拒**。真正要钉死的是：要么当场拒绝，要么任务明确失败，
    绝不能「已完成」；而且无论哪种，用户都拿到一句能读懂的话。
    """
    status, body = convert(client, filename, content, target, options)
    if status != 200:
        message = (body.get("error") or {}).get("message", "")
        check(bool(message), f"{label}：提交被拒（HTTP {status}）且有面向用户的说明")
        return message
    task = body["tasks"][0]
    check(task["status"] == "failed", f"{label}：明确失败，不是「已完成」")
    check(task.get("result") is None, f"{label}：没有产物")
    return str(task["error_message"])


def open_image(data: bytes) -> Image.Image:
    """真的把下载到的字节打开（不是只看长度）。"""
    image = Image.open(io.BytesIO(data))
    image.load()
    return image


def open_pdf(data: bytes) -> pymupdf.Document:
    if not data.startswith(b"%PDF-"):
        raise SystemExit(f"下载到的不是 PDF：{data[:16]!r}")
    return pymupdf.open("pdf", data)


def page_mark(doc: pymupdf.Document, index: int) -> str:
    """一页的文本，去掉所有空白。

    ``get_text()`` 的行序与空格随排版变化，比对「这一页是哪一页」时
    不该被它们干扰。
    """
    return "".join(doc[index].get_text().split())


def upload_pdf(client: httpx.Client, name: str = "源文件.pdf") -> str:
    response = client.post(
        f"{PDF_API}/upload",
        files={"file": (name, io.BytesIO(labeled_pdf(4)), "application/pdf")},
    )
    if response.status_code != 200:
        raise SystemExit(f"上传 PDF 失败 {response.status_code}: {response.text[:200]}")
    return response.json()["input_id"]


def download(client: httpx.Client, url: str) -> bytes:
    response = client.get(f"{BASE}{url}")
    if response.status_code != 200:
        raise SystemExit(f"下载失败 {response.status_code}: {response.text[:200]}")
    return response.content


# ----------------------------------------------------------------------
# 1. 能力矩阵接口（§十五 / §十六）
# ----------------------------------------------------------------------

def verify_capabilities(client: httpx.Client) -> None:
    print("\n=== 1. 能力矩阵接口 ===")
    body = client.get(CAPABILITIES).json()

    # 既有键一个不少（§五十九：前端已经在用）
    for key in ("matrix", "groups", "targets", "notes",
                "office_available", "pdf_to_word_available", "ocr_available"):
        check(key in body, f"既有键还在：{key}")
    for key in ("conversions", "operations", "categories", "formats"):
        check(key in body, f"新增键存在：{key}")

    conversions = body["conversions"]
    operations = body["operations"]
    # 70 = 第九阶段的 53 条 + 第十阶段 A §九 的 4 条 SVG + §五/§六 的 13 条 HEIC
    # （7 解码 heic→jpg/png/webp/bmp/gif/tiff/pdf + 6 编码 jpg/png/webp/bmp/gif/tiff→heic）。
    # 更新成**新的精确真值**，不是放宽：下面每一条仍然是逐项相等的比较。
    # HEIC 那 13 条要求这台机器真的装了 pillow-heif（本机装了），
    # 缺组件的机器上它们会正确地消失 —— 那时这个脚本该报的是
    # 「数量不对」而不是「悄悄少了几条」，这正是精确断言的意义。
    check(len(conversions) == 70, f"转换条目 {len(conversions)} 条（期望 70）")
    # 7 = 第九阶段的 6 个 PDF/合成操作 + 第十阶段 A §三十四 的图片元数据查看。
    # 同上：更新成新的精确真值，不是放宽。
    check(len(operations) == 7, f"操作条目 {len(operations)} 条（期望 7）")
    check(all(item["operation_type"] == "conversion" for item in conversions),
          "conversions[] 里全是 conversion")
    check(all(item["operation_type"] == "operation" for item in operations),
          "operations[] 里全是 operation")
    check([item["id"] for item in operations] == [
        "op.pdf-merge", "op.pdf-split", "op.pdf-compress",
        "op.pdf-extract-pages", "op.pdf-delete-pages", "op.image-images-to-pdf",
        "op.image-metadata",
    ], "七个操作的 ID 与顺序稳定（§四十三）")

    # ID 唯一且能往返（§四十三）
    ids = [item["id"] for item in conversions + operations]
    check(len(ids) == len(set(ids)), f"能力 ID 互不重复（实际 {len(ids)} 个）")
    check(all(item["id"] == f"{item['category']}.{item['source_type']}-to-"
                           f"{item['target_type']}" for item in conversions),
          "转换 ID 全部是 <类别>.<源>-to-<目标> 形式")
    check(all(item["id"] == f"op.{item['display_name']}" or item["id"].startswith("op.")
              for item in operations), "操作 ID 全部以 op. 开头")

    # 每个条目都必须有 JSON 契约里那 18 个键
    published = set(conversions[0]) | set(operations[0])
    for key in ("id", "source_type", "target_type", "operation_type", "display_name",
                "category", "group", "requires", "worker_pool", "available",
                "supports_batch", "supports_options", "supports_preview",
                "endpoint", "method", "tags", "note", "options_schema"):
        check(key in published, f"条目里有 {key}")
    check(not any("converter_key" in item for item in conversions + operations),
          "converter_key 不对外发布（内部实现细节）")

    # §十六：前端禁止硬编码能力矩阵 —— 接口必须能自己说清「什么能转什么」
    by_source: dict[str, set[str]] = {}
    for item in conversions:
        by_source.setdefault(item["source_type"], set()).add(item["target_type"])
    # 第十阶段 A 给 PNG 加上了 ``heic``（编码方向，要 libx265）。这是**新的
    # 精确真值**：仍然是逐元素相等的集合比较，不是 ``⊇`` 超集。
    check(by_source["png"] == {"jpg", "webp", "bmp", "gif", "tiff", "ico", "pdf", "heic"},
          f"PNG 的目标集合正确：{sorted(by_source['png'])}")
    check(by_source["ico"] if "ico" in by_source else True,
          "ICO 不是源格式（§七：只有 PNG→ICO）")
    check("ico" not in by_source, "ICO 没有作为源出现")
    for source, targets in by_source.items():
        check(source not in targets, f"{source} 的目标里没有它自己（不做同格式转换）")

    # 操作条目的端点必须真的存在
    served = {path: {method.upper() for method in ops}
              for path, ops in client.get(f"{BASE}/openapi.json").json()["paths"].items()}
    for item in operations:
        endpoint = item["endpoint"]
        check(endpoint in served, f"{item['id']} 的端点存在：{endpoint}")
        if endpoint in served:
            check(item["method"].upper() in served[endpoint],
                  f"{item['id']} 的方法正确：{item['method']} {endpoint}")

    # 过滤参数（§十五 的查询能力）
    only_pdf = client.get(CAPABILITIES, params={"category": "pdf"}).json()
    check(all(item["category"] == "pdf"
              for item in only_pdf["conversions"] + only_pdf["operations"]),
          "?category=pdf 只返回 PDF 类")
    only_op = client.get(CAPABILITIES, params={"operation_type": "operation"}).json()
    check(len(only_op["operations"]) == 7 and only_op["conversions"] == [],
          "?operation_type=operation 只返回操作")


def verify_option_gating(client: httpx.Client) -> None:
    """§九/§二十二：不该出现的旋钮不许出现，出现了的旋钮不许一提交就报错。

    逐条扫过**全部**图片转换，而不是抽查几个 —— 「某个组合忘了裁剪」
    正是这类配置驱动代码最容易出的错，抽查恰好会漏掉它。
    """
    print("\n=== 2. 选项按目标格式裁剪 ===")
    body = client.get(CAPABILITIES).json()
    schema = {item["id"]: item["options_schema"] for item in body["conversions"]}

    def keys(capability_id: str) -> list[str]:
        found = schema[capability_id]
        return [item["key"] for item in found["items"]] if found else []

    # 目标格式 -> 该不该有这个旋钮。三张表就是实现的对外投影。
    #
    # ``heic`` 的三格都是**量出来的**，不是照抄别的格式：
    # * ``quality`` 有 —— 效果实测明显（384×384 细节图：q10 约 3 KB、
    #   q85 约 173 KB、q100 约 198 KB）；
    # * ``dpi`` **没有** —— Pillow 收下 ``dpi=`` 不报错，回读
    #   ``info['dpi']`` 却是 ``None``，是纯装饰的旋钮；
    # * ``metadata`` 有 —— 保留方向实测能把 Make / Model 与 XMP 带过去。
    gating = {
        "quality": ({"jpg", "webp", "heic"}, {"png", "bmp", "gif", "tiff", "ico"}),
        "dpi": ({"jpg", "png", "tiff"}, {"webp", "bmp", "gif", "ico", "heic"}),
        "metadata": ({"jpg", "png", "webp", "tiff", "heic"}, {"bmp", "gif", "ico"}),
    }
    image_conversions = [
        item for item in body["conversions"]
        if item["category"] == "image" and item["target_type"] != "pdf"
    ]
    # 46 = 第九阶段的 31 条图片互转 + 第十阶段 A 的 3 条 SVG→位图
    # + §五/§六 的 12 条 HEIC 互转（6 条解码 heic→位图 + 6 条编码位图→heic）。
    # SVG 不在注册表的 ``IMAGE_SOURCES`` 里（那张表还有「Pillow 能解码」这重
    # 身份），但它在 ``category`` 上确实是图片 —— 也正是因此，这 3 条必须
    # 和其余图片一样逐条过下面这套旋钮裁剪检查，一条都不能豁免。
    check(len(image_conversions) == 46, f"图片互转共 {len(image_conversions)} 条（期望 46）")
    for item in image_conversions:
        target = item["target_type"]
        present = keys(item["id"])
        for key, (wanted, unwanted) in gating.items():
            if target in wanted:
                check(key in present, f"{item['id']} 有 {key}（{target.upper()} 支持）")
            elif target in unwanted:
                check(key not in present,
                      f"{item['id']} 不给 {key}（{target.upper()} 没有这个容器）")
        check("rotation" in present, f"{item['id']} 有旋转选项")
        check("resize.mode" in present, f"{item['id']} 有尺寸选项")

    # 图片 → PDF 的旋钮是排版那一组，不该混进图片编码的旋钮
    for item in body["conversions"]:
        if item["target_type"] != "pdf" or item["category"] != "image":
            continue
        present = keys(item["id"])
        if item["source_type"] == "svg":
            # §九–§十五：SVG→PDF 走 ``svg.to_pdf``（PyMuPDF ``convert_to_pdf``），
            # 导出的是**真正的矢量页**，页面尺寸就是 SVG 自己声明的那个 ——
            # 没有「排版」可言。给它挂上 page_size / margin 只会得到一组
            # 点了没反应的控件（第九阶段就定下：不发这种控件）。
            check(present == [],
                  f"{item['id']} 是矢量导出，不该有任何排版旋钮：{present}")
            continue
        check(present == ["page_size", "page_size.width_mm", "page_size.height_mm",
                          "orientation", "margin", "fit"],
              f"{item['id']} 的选项就是排版那一组：{present}")
        check("quality" not in present and "resize.mode" not in present,
              f"{item['id']} 没有混进图片编码旋钮")

    # 枚举值域与实现同源
    #
    # 下面两处期望值在第十阶段 A 扩容过，是**新的精确真值**，不是放宽：
    # ``resize.mode`` 增加了 §十九 点名的最大宽度档（640/1280/1920/2560）
    # 与百分比档（25/50/75/100% + 自定义），``rotation`` 增加了 ``custom``
    # （角度由 ``rotation.angle`` 给出）。断言强度一个字符没降 ——
    # 仍然是逐值相等的列表比较。
    resize = [item for item in schema["image.png-to-jpg"]["items"]
              if item["key"] == "resize.mode"][0]
    check([value["value"] for value in resize["enum"]] ==
          ["original", "small", "medium", "large",
           "w640", "w1280", "w1920", "w2560",
           "p25", "p50", "p75", "p100", "percent", "custom"],
          "尺寸档位与实现一致")
    rotation = [item for item in schema["image.png-to-jpg"]["items"]
                if item["key"] == "rotation"][0]
    check([value["value"] for value in rotation["enum"]] ==
          ["0", "90", "180", "270", "custom"],
          "旋转档位是 0/90/180/270/自定义 且值是字符串")
    margins = [item for item in schema["image.jpg-to-pdf"]["items"]
               if item["key"] == "margin"][0]
    check([value["value"] for value in margins["enum"]] ==
          ["none", "small", "medium", "large"],
          "页边距档位是 §十 的四档")

    # 有条件的旋钮必须真的挂在条件上，否则界面上会出现两个互斥的输入框
    for capability_id, key, condition in (
        ("image.png-to-jpg", "resize.width", {"resize.mode": "custom"}),
        ("image.png-to-jpg", "dpi.custom", {"dpi": "custom"}),
        ("image.jpg-to-pdf", "page_size.width_mm", {"page_size": "custom"}),
    ):
        spec = [item for item in schema[capability_id]["items"] if item["key"] == key][0]
        check(spec.get("visible_when") == condition,
              f"{capability_id} 的 {key} 只在 {condition} 下出现")


# ----------------------------------------------------------------------
# 3. 新格式互转（§七）
# ----------------------------------------------------------------------

def verify_new_formats(client: httpx.Client) -> None:
    print("\n=== 3. 新格式互转 + PNG→ICO ===")
    cases = [
        ("photo.png", encode(Image.new("RGB", (400, 200), (18, 52, 86)), "PNG"),
         "bmp", "BMP", ".bmp", "image/bmp"),
        ("photo.png", encode(Image.new("RGB", (400, 200), (18, 52, 86)), "PNG"),
         "gif", "GIF", ".gif", "image/gif"),
        ("photo.png", encode(Image.new("RGB", (400, 200), (18, 52, 86)), "PNG"),
         "tiff", "TIFF", ".tiff", "image/tiff"),
        ("photo.bmp", encode(Image.new("RGB", (400, 200), (200, 30, 30)), "BMP"),
         "png", "PNG", ".png", "image/png"),
        ("photo.gif", encode(Image.new("P", (400, 200)), "GIF"),
         "png", "PNG", ".png", "image/png"),
        ("photo.tiff", encode(Image.new("RGB", (400, 200), (30, 200, 30)), "TIFF"),
         "png", "PNG", ".png", "image/png"),
        ("photo.webp", encode(Image.new("RGB", (400, 200), (30, 30, 200)), "WEBP"),
         "tiff", "TIFF", ".tiff", "image/tiff"),
    ]
    for name, payload, target, pillow_format, extension, media_type in cases:
        data, task = produce(client, name, payload, target, label=f"{name}→{target}")
        result = task["result"]
        with open_image(data) as image:
            check(image.format == pillow_format,
                  f"{name}→{target}：真的打开了，格式 {image.format}")
            check(image.size == (400, 200), f"{name}→{target}：尺寸 {image.size}")
        check(result["filename"].endswith(extension),
              f"{name}→{target}：结果名 {result['filename']}")
        check(result["media_type"] == media_type,
              f"{name}→{target}：MIME 是 {result['media_type']}（不是 octet-stream）")
        check(task["conversion_id"] == f"image.{path_format(name)}-to-{target}",
              f"{name}→{target}：conversion_id={task['conversion_id']}")

    # PNG→ICO：容器里必须真的装着若干帧
    data, task = produce(
        client, "logo.png", encode(Image.new("RGB", (600, 500), (240, 240, 240)), "PNG"),
        "ico", label="logo.png→ico",
    )
    with open_image(data) as icon:
        frames = sorted(icon.info.get("sizes", []))
        check(icon.format == "ICO", f"PNG→ICO：真的是 ICO（{icon.format}）")
        check(len(frames) >= 3, f"PNG→ICO：装了 {len(frames)} 档尺寸 {frames}")
        check(max(frames)[0] <= 256, "PNG→ICO：最大帧不超过 256（ICO 的标准上限）")
        # 各帧是**等比缩略**的整数取整结果，不是精确同比例（16×13 与 24×20
        # 都是 600×500 的取整产物），所以比的是「都在源比例附近」而不是相等。
        source_ratio = 600 / 500
        drifted = [f for f in frames if abs(f[0] / f[1] - source_ratio) > 0.05]
        check(not drifted, f"PNG→ICO：每帧都贴近源图长宽比 1.20（偏离的：{drifted}）")
        check(abs((frames[-1][0] / frames[-1][1]) - source_ratio) < 0.02,
              f"PNG→ICO：最大帧保持源图长宽比（{frames[-1][0] / frames[-1][1]:.3f}）")
    check(task["result"]["filename"] == "logo.ico", "PNG→ICO：结果名 logo.ico")

    # 凑不出 16×16 的源必须明确失败，不能交出零帧空壳
    for size in ((10, 10), (17, 9), (15, 40)):
        payload = encode(Image.new("RGB", size, (9, 9, 9)), "PNG")
        message = rejected(client, f"tiny{size[0]}x{size[1]}.png", payload, "ico",
                           None, f"{size} 的 PNG→ICO")
        check("16" in message, f"{size} 的失败说明了 16 像素这个门槛：{message[:50]}")
        check_clean(message, f"{size} 的 PNG→ICO 失败信息")


def path_format(filename: str) -> str:
    """``photo.jpeg`` → ``jpeg``：conversion_id 用的是类型词汇，不是扩展名。"""
    return {"jpg": "jpg", "jpeg": "jpg", "tif": "tiff"}.get(
        filename.rsplit(".", 1)[-1].lower(), filename.rsplit(".", 1)[-1].lower()
    )


# ----------------------------------------------------------------------
# 4. 图片选项真的生效（§九）
# ----------------------------------------------------------------------

def verify_rotation(client: httpx.Client) -> None:
    print("\n=== 4. 旋转（按像素位置判断方向）===")
    source = quadrants(800, 400, "JPEG", quality=100)
    # 顺时针旋转之后，四个象限的落点是确定的：
    # 0° 左上红；90° 左下蓝转到左上；180° 右下黄转到左上；270° 右上绿转到左上
    expected = {
        "0": ((800, 400), "左上", RED),
        "90": ((400, 800), "左上", BLUE),
        "180": ((800, 400), "左上", YELLOW),
        "270": ((400, 800), "左上", GREEN),
    }
    for degree, (size, corner, color) in expected.items():
        data, _ = produce(client, "quad.jpg", source, "png",
                          f'{{"rotation":"{degree}"}}', label=f"旋转 {degree}")
        with open_image(data) as image:
            check(image.size == size, f"旋转 {degree}：尺寸 {image.size} 期望 {size}")
            got = image.getpixel((3, 3))
            check(all(abs(a - b) <= 40 for a, b in zip(got, color)),
                  f"旋转 {degree}：{corner}角是 {got}，期望接近 {color}")

    # 90 与 270 不能是同一张图 —— 方向转反是最容易漏的错
    data90, _ = produce(client, "quad.jpg", source, "png", '{"rotation":"90"}')
    data270, _ = produce(client, "quad.jpg", source, "png", '{"rotation":"270"}')
    check(data90 != data270, "顺时针 90° 与 270° 的结果不同（没有转反）")

    message = rejected(client, "quad.jpg", source, "png", '{"rotation":"45"}',
                       "45° 旋转")
    check_clean(message, "非法旋转角度的失败信息")


def verify_resize(client: httpx.Client) -> None:
    print("\n=== 5. 尺寸（预设只缩不放）===")
    big = quadrants(3000, 1500, "JPEG", quality=100)
    for mode, expected in (("original", (3000, 1500)), ("small", (1024, 512)),
                          ("medium", (1600, 800)), ("large", (2560, 1280))):
        data, _ = produce(client, "big.jpg", big, "png", f'{{"resize.mode":"{mode}"}}')
        with open_image(data) as image:
            check(image.size == expected, f"{mode}：{image.size} 期望 {expected}")

    cases = [
        ('{"resize.mode":"custom","resize.width":500,"resize.height":500,"resize.keep_aspect":true}',
         (500, 250), "自定义 500×500 + 保持宽高比 → 500×250"),
        ('{"resize.mode":"custom","resize.width":500,"resize.height":500,"resize.keep_aspect":false}',
         (500, 500), "自定义 500×500 + 不保持 → 500×500（真的拉伸）"),
        ('{"resize.mode":"custom","resize.width":12000,"resize.height":12000}',
         (12000, 6000), "自定义 12000（上限）→ 12000×6000"),
    ]
    for options, expected, label in cases:
        data, _ = produce(client, "big.jpg", big, "png", options, label=label)
        with open_image(data) as image:
            check(image.size == expected, f"{label}：{image.size}")

    # 小图不会被拉大
    small = quadrants(300, 150, "JPEG", quality=100)
    data, _ = produce(client, "small.jpg", small, "png", '{"resize.mode":"small"}')
    with open_image(data) as image:
        check(image.size == (300, 150), f"小图 small 不被放大：{image.size}")

    for options, keyword in (
        ('{"resize.mode":"custom","resize.width":12001,"resize.height":100}', "12000"),
        ('{"resize.mode":"custom","resize.width":0,"resize.height":100}', "1"),
    ):
        message = rejected(client, "big.jpg", big, "png", options, f"越界尺寸 {keyword}")
        check(keyword in message, f"越界尺寸的失败点明了上限 {keyword}：{message[:50]}")
        check_clean(message, f"越界尺寸 {keyword} 的失败信息")


def verify_quality(client: httpx.Client) -> None:
    print("\n=== 6. 质量（有损格式才给，且单调）===")
    source = noisy(600, 400, "PNG")
    sizes = {}
    for quality in (10, 50, 85, 100):
        data, _ = produce(client, "noisy.png", source, "jpg", f'{{"quality":{quality}}}')
        sizes[quality] = len(data)
    check(sizes[10] < sizes[50] < sizes[85] < sizes[100],
          f"JPG 体积随质量单调上升：{sizes}")

    webp = {}
    for quality in (10, 100):
        data, _ = produce(client, "noisy.png", source, "webp", f'{{"quality":{quality}}}')
        webp[quality] = len(data)
    check(webp[10] < webp[100], f"WEBP 体积随质量单调上升：{webp}")

    # 质量只对有损格式有意义 —— 给无损格式必须被明确拒绝，而不是静默忽略。
    # 源换成一个 JPG：同格式不是能力，拿 PNG 源去撞 PNG 目标会撞在
    # 「不支持同格式转换」上，那样这条测试就测错了东西。
    lossless_probe = noisy(600, 400, "JPEG", quality=95)
    for target in ("png", "bmp", "gif", "tiff", "ico"):
        message = rejected(client, "q.jpg", lossless_probe, target, '{"quality":85}',
                           f"{target.upper()} 目标提交 quality")
        check_clean(message, f"{target.upper()} 的 quality 失败信息")

    for options, keyword in (('{"quality":9}', "10"), ('{"quality":101}', "100")):
        message = rejected(client, "noisy.png", source, "jpg", options,
                           f"越界质量 {keyword}")
        check(keyword in message, f"越界质量点明了边界 {keyword}：{message[:50]}")
        check_clean(message, f"越界质量 {keyword} 的失败信息")


def verify_dpi(client: httpx.Client) -> None:
    print("\n=== 7. DPI（写进去要读得回来）===")
    source = noisy(400, 200, "JPEG", quality=95)
    for target in ("png", "tiff"):
        data, _ = produce(client, "d.jpg", source, target, '{"dpi":"300"}')
        with open_image(data) as image:
            stored = image.info.get("dpi")
            check(stored is not None, f"{target.upper()} 真的写入了密度")
            check(stored is not None and abs(stored[0] - 300) <= 0.5,
                  f"{target.upper()} 的密度读回来是 {stored}（期望 300 上下）")

    for target in ("webp", "bmp", "gif", "ico"):
        message = rejected(client, "d.jpg", source, target, '{"dpi":"300"}',
                           f"{target.upper()} 目标提交 dpi")
        check_clean(message, f"{target.upper()} 的 dpi 失败信息")

    data, _ = produce(client, "d.jpg", source, "png",
                      '{"dpi":"custom","dpi.custom":150}')
    with open_image(data) as image:
        stored = image.info.get("dpi")
        check(stored is not None and abs(stored[0] - 150) <= 0.5,
              f"自定义 150 DPI 读回来是 {stored}")

    for value in (72, 96, 150, 300):
        data, _ = produce(client, "d.jpg", source, "tiff", f'{{"dpi":"{value}"}}')
        with open_image(data) as image:
            stored = image.info.get("dpi")
            check(stored is not None and abs(stored[0] - value) <= 0.5,
                  f"{value} DPI 档位读回来是 {stored}")

    message = rejected(client, "d.jpg", source, "png",
                       '{"dpi":"custom","dpi.custom":1201}', "自定义 DPI 超上限")
    check_clean(message, "自定义 DPI 超上限的失败信息")


def verify_metadata(client: httpx.Client) -> None:
    print("\n=== 8. 元数据（保留 / 清除，如实说明）===")
    image = Image.new("RGB", (200, 100), (10, 20, 30))
    exif = image.getexif()
    exif[0x010F] = "FileToolsMake"
    exif[0x0110] = "FileToolsModel"
    exif[0x0132] = "2020:01:02 03:04:05"
    blob = exif.tobytes()
    jpeg = encode(image, "JPEG", exif=blob)
    png = encode(image, "PNG", exif=blob)
    with open_image(jpeg) as probe:
        check(len(dict(probe.getexif())) >= 3, "样本本身带着 EXIF")

    # 同格式不是能力，所以每个目标都换一个源：JPG 目标用 PNG 源，
    # 其余三个目标用 JPG 源。
    wanted = (0x010F, 0x0110, 0x0132)
    sources = {
        "jpg": ("meta.png", png),
        "png": ("meta.jpg", jpeg),
        "webp": ("meta.jpg", jpeg),
        "tiff": ("meta.jpg", jpeg),
    }
    for target, (filename, source) in sources.items():
        data, _ = produce(client, filename, source, target, '{"metadata":"keep"}')
        with open_image(data) as kept:
            found = {k: v for k, v in dict(kept.getexif()).items() if k in wanted}
            check(len(found) >= 2, f"{target.upper()} keep：EXIF 还在 {list(found)}")

        data, _ = produce(client, filename, source, target, '{"metadata":"remove"}')
        with open_image(data) as stripped:
            found = {k: v for k, v in dict(stripped.getexif()).items() if k in wanted}
            check(not found, f"{target.upper()} remove：EXIF 已清空（残留 {list(found)}）")

    # TIFF 的结构性标签：清除元数据不该把文件结构一起弄坏
    data, _ = produce(client, "meta.jpg", jpeg, "tiff", '{"metadata":"remove"}')
    with open_image(data) as stripped:
        check(stripped.format == "TIFF" and stripped.size == (200, 100),
              "TIFF remove 之后仍然是可打开、尺寸正确的 TIFF")
        check(300 not in dict(stripped.tag_v2), "TIFF remove：拍摄信息标签已移除")

    for target in ("bmp", "gif", "ico"):
        message = rejected(client, "meta.jpg", jpeg, target, '{"metadata":"keep"}',
                           f"{target.upper()} 目标提交 metadata")
        check_clean(message, f"{target.upper()} 的 metadata 失败信息")

    message = rejected(client, "meta.jpg", jpeg, "png", '{"metadata":"nonsense"}',
                       "非法元数据取值")
    check_clean(message, "非法元数据的失败信息")


# ----------------------------------------------------------------------
# 9. 多帧 / 多页（决策 C）
# ----------------------------------------------------------------------

def verify_multiframe(client: httpx.Client) -> None:
    print("\n=== 9. 多帧 GIF / 多页 TIFF 只取第一帧 ===")
    gif = multiframe("GIF", 3)
    tiff = multiframe("TIFF", 3)

    # 图片 → 图片
    for name, payload, target, keyword in (
        ("anim.gif", gif, "png", "动图"),
        ("anim.gif", gif, "jpg", "动图"),
        ("scan.tiff", tiff, "png", "页"),
    ):
        data, task = produce(client, name, payload, target)
        with open_image(data) as image:
            check(getattr(image, "n_frames", 1) == 1,
                  f"{name}→{target}：结果只有 {getattr(image, 'n_frames', 1)} 帧")
            # 三帧的颜色是 (0,90,200) / (40,90,160) / (80,90,120)。
            # 判据取**第一帧那一组**，不是「随便哪一帧」—— 只有精确到
            # 颜色才能证明取的是第一帧而不是最后一帧。
            got = image.convert("RGB").getpixel((5, 5))
            check(all(abs(a - b) <= 30 for a, b in zip(got, (0, 90, 200))),
                  f"{name}→{target}：取的是第一帧 {got}，期望约 (0, 90, 200)")
        notes = task["result"].get("notes") or []
        check(any("3" in note and keyword in note for note in notes),
              f"{name}→{target}：如实说明「共 3」：{notes}")
        for note in notes:
            check_clean(note, f"{name}→{target} 的说明")

    # 图片 → PDF（此前这里漏了说明，9a 验收时补上）
    for name, payload, keyword in (("anim.gif", gif, "动图"), ("scan.tiff", tiff, "页")):
        data, task = produce(client, name, payload, "pdf")
        with open_pdf(data) as doc:
            check(doc.page_count == 1, f"{name}→pdf：只有 1 页（实际 {doc.page_count}）")
        notes = task["result"].get("notes") or []
        check(any("3" in note and keyword in note for note in notes),
              f"{name}→pdf：如实说明只转了第一帧：{notes}")
        for note in notes:
            check_clean(note, f"{name}→pdf 的说明")

    # 单帧文件绝不能收到「只转了第一帧」这句假话
    data, task = produce(client, "one.png",
                         encode(Image.new("RGB", (120, 80), (10, 200, 10)), "PNG"), "pdf")
    notes = task["result"].get("notes") or []
    check(not any("帧" in note or "第一页" in note for note in notes),
          f"单帧 PNG→pdf 没有多帧提示：{notes}")

    # 多图合成一份 PDF 时汇总成一句，但帧数照样列出来
    response = client.post(
        f"{PDF_API}/from-images",
        files=[
            ("files", ("a.gif", io.BytesIO(gif), "image/gif")),
            ("files", ("b.png", io.BytesIO(encode(Image.new("RGB", (200, 100)), "PNG")),
                       "image/png")),
            ("files", ("c.tiff", io.BytesIO(multiframe("TIFF", 4, (100, 60))),
                       "image/tiff")),
        ],
        data={"page_size": "a4"},
    )
    check(response.status_code == 200, f"多图合成 PDF 成功 {response.status_code}")
    notes = response.json()["notes"]
    check(len([n for n in notes if "多帧" in n]) == 1,
          f"多个多帧源汇总成一句：{notes}")
    check(any("GIF 3 帧" in note and "TIFF 4 页" in note for note in notes),
          f"汇总里列了每个文件的帧数：{notes}")


# ----------------------------------------------------------------------
# 10. 图片 → PDF 的排版几何（§十）
# ----------------------------------------------------------------------

def verify_image_to_pdf(client: httpx.Client) -> None:
    print("\n=== 10. 图片→PDF 的排版几何 ===")
    source = encode(Image.new("RGB", (800, 400), (240, 240, 240)), "PNG")

    def build(options: str, label: str):
        data, task = produce(client, "p.png", source, "pdf", options, label=label)
        doc = open_pdf(data)
        page = doc[0]
        images = page.get_images()
        rects = page.get_image_rects(images[0][0]) if images else []
        return doc, page, (rects[0] if rects else None), task

    def inside_content_box(page, rect, margin_mm: float) -> bool:
        """图片是否整个落在版心内（页边距之内）。

        断言的是**不变式**而不是「左边距刚好等于多少」：等比缩放之后，
        受限的那条边贴住版心，另一条边居中留白，左内缩因此大于页边距。
        这才是「页边距生效」真正的意思。
        """
        inset = margin_mm * MM
        return (rect.x0 >= inset - 0.5 and rect.y0 >= inset - 0.5
                and rect.x1 <= page.rect.width - inset + 0.5
                and rect.y1 <= page.rect.height - inset + 0.5)

    def touches_content_box(page, rect, margin_mm: float) -> bool:
        """至少有一条边正好贴住版心 —— 证明是按版心缩放的，不是缩得更小。"""
        inset = margin_mm * MM
        return (abs(rect.x0 - inset) < 0.5 or abs(rect.y0 - inset) < 0.5
                or abs(rect.x1 - (page.rect.width - inset)) < 0.5
                or abs(rect.y1 - (page.rect.height - inset)) < 0.5)

    doc, page, rect, _ = build(
        '{"page_size":"a4","orientation":"portrait","margin":"none","fit":"contain"}',
        "a4 纵 margin none",
    )
    check((round(page.rect.width, 2), round(page.rect.height, 2)) == (595.28, 841.89),
          f"A4 纵向页面 {tuple(round(v, 2) for v in page.rect)}")
    check(rect is not None and abs(rect.width - 595.28) < 1,
          f"无页边距时图片占满宽度：{None if rect is None else round(rect.width, 2)}")
    doc.close()

    doc, page, rect, _ = build(
        '{"page_size":"a4","orientation":"landscape","margin":"large","fit":"contain"}',
        "a4 横 margin large",
    )
    check((round(page.rect.width, 2), round(page.rect.height, 2)) == (841.89, 595.28),
          f"A4 横向页面 {tuple(round(v, 2) for v in page.rect)}")
    # 页边距「大」= 20 毫米 = 56.69 磅
    check(rect is not None and inside_content_box(page, rect, 20),
          f"页边距大 = 20mm：图片在版心内 {None if rect is None else tuple(round(v, 2) for v in rect)}")
    check(rect is not None and touches_content_box(page, rect, 20),
          "页边距大：图片正好缩到版心，没有缩得更小")
    check(rect is not None and abs(rect.y0 - (page.rect.height - rect.height) / 2) < 1,
          "等比缩放后在版心里垂直居中")
    doc.close()

    doc, page, rect, _ = build(
        '{"page_size":"custom","page_size.width_mm":"100","page_size.height_mm":"50",'
        '"margin":"small","fit":"contain"}',
        "custom 100x50mm margin small",
    )
    check((round(page.rect.width, 2), round(page.rect.height, 2)) ==
          (round(100 * MM, 2), round(50 * MM, 2)),
          f"自定义 100×50 毫米 → {tuple(round(v, 2) for v in page.rect)}")
    check(rect is not None and inside_content_box(page, rect, 5),
          f"页边距小 = 5mm：图片在版心内 {None if rect is None else tuple(round(v, 2) for v in rect)}")
    check(rect is not None and touches_content_box(page, rect, 5),
          "页边距小：图片正好缩到版心")
    # 5mm 与 20mm 必须真的不一样 —— 档位映射错了这里就会相等
    check(rect is not None and rect.y0 > 0 and abs(rect.y0 - 5 * MM) < 0.5,
          f"页边距小 = 5mm → 上内缩 {None if rect is None else round(rect.y0, 2)} 磅（= 5 毫米）")
    doc.close()

    doc, page, rect, task = build(
        '{"page_size":"a5","margin":"none","fit":"fill"}', "a5 fill")
    check(page.rect.width > page.rect.height, "A5 + 横图 → 页面自动转成横向")
    check(rect is not None and (rect.x0 < 0 or rect.y0 < 0),
          "「填充页面」会溢出页面并如实说明")
    check(any("裁剪" in note for note in (task["result"].get("notes") or [])),
          "填充页面的说明出现在结果里")
    doc.close()

    doc, page, rect, _ = build(
        '{"page_size":"auto","margin":"none","fit":"original"}', "auto original")
    check((round(page.rect.width, 2), round(page.rect.height, 2)) == (800.0, 400.0),
          f"自动页面 = 图片像素尺寸 {tuple(round(v, 2) for v in page.rect)}")
    doc.close()

    doc, page, rect, _ = build(
        '{"page_size":"auto","margin":"medium","fit":"contain"}', "auto margin medium")
    check((round(page.rect.width, 2), round(page.rect.height, 2)) ==
          (round(800 + 2 * 10 * MM, 2), round(400 + 2 * 10 * MM, 2)),
          f"自动页面 + 中页边距（10mm）= {tuple(round(v, 2) for v in page.rect)}")
    doc.close()

    for size, expected in (("a4", (595.28, 841.89)), ("a5", (419.53, 595.28)),
                           ("letter", (612.0, 792.0))):
        status, body = convert(
            client, "p.png", encode(Image.new("RGB", (400, 600), (200, 200, 200)), "PNG"),
            "pdf", f'{{"page_size":"{size}","orientation":"portrait"}}',
        )
        task = body["tasks"][0]
        check(task["status"] == "completed", f"page_size={size} 转换成功")
        doc = open_pdf(download(client, task["result"]["download_url"]))
        got = (round(doc[0].rect.width, 2), round(doc[0].rect.height, 2))
        check(got == expected, f"page_size={size} → {got} 期望 {expected}")
        doc.close()

    for options, keyword in (
        ('{"page_size":"custom","page_size.width_mm":"99999","page_size.height_mm":"100"}',
         "毫米"),
        ('{"page_size":"a3"}', "页面"),
        ('{"margin":"huge"}', "页边距"),
        ('{"fit":"stretch"}', "适应"),
    ):
        message = rejected(client, "p.png", source, "pdf", options, f"非法排版参数 {keyword}")
        check_clean(message, f"非法排版参数 {keyword} 的失败信息")


# ----------------------------------------------------------------------
# 11. 六个 PDF 操作（决策 B）
# ----------------------------------------------------------------------

def verify_pdf_operations(client: httpx.Client) -> None:
    print("\n=== 11. 六个 PDF 操作 ===")
    source = labeled_pdf(4)
    input_id = upload_pdf(client, "汇报.pdf")

    def call(path: str, files=None, data=None) -> tuple[int, dict]:
        payload = dict(data or {})
        if files is None:
            payload.setdefault("input_id", input_id)
        response = client.post(f"{PDF_API}/{path}", files=files, data=payload)
        return response.status_code, response.json()

    # 合并：2 份 × 4 页 → 8 页，页序必须原样
    status, body = call("merge", files=[
        ("files", ("第一份.pdf", io.BytesIO(labeled_pdf(4, "A")), "application/pdf")),
        ("files", ("第二份.pdf", io.BytesIO(labeled_pdf(4, "B")), "application/pdf")),
    ])
    check(status == 200, f"合并成功 {status}")
    check(body["filename"] == "merged.pdf", f"合并结果名 {body['filename']}")
    doc = open_pdf(download(client, body["download_url"]))
    texts = [doc[index].get_text() for index in range(doc.page_count)]
    check(doc.page_count == 8, f"合并后 8 页（实际 {doc.page_count}）")
    marks = ["".join(t.split()) for t in texts]
    check(all(f"APAGE{n}" in m for n, m in zip(range(1, 5), marks[:4]))
          and all(f"BPAGE{n}" in m for n, m in zip(range(1, 5), marks[4:])),
          f"合并页序正确：{[t[:12] for t in texts]}")
    check(all(f"第{n}页" in t for n, t in zip(list(range(1, 5)) * 2, texts)),
          "合并后每一页的中文内容都原样保留")
    doc.close()

    # 拆分（每页一个）→ ZIP，逐个读进去
    status, body = call("split", data={"mode": "every"})
    check(status == 200 and body["archived"] is True, f"每页拆分返回 ZIP {status}")
    check(body["archive_filename"] == "汇报_parts.zip",
          f"ZIP 名来自源文件名：{body['archive_filename']}")
    check([item["page_count"] for item in body["files"]] == [1, 1, 1, 1],
          f"四份各 1 页：{[i['page_count'] for i in body['files']]}")
    with zipfile.ZipFile(io.BytesIO(download(client, body["download_url"]))) as archive:
        names = sorted(archive.namelist())
        check(names == ["part-01.pdf", "part-02.pdf", "part-03.pdf", "part-04.pdf"],
              f"ZIP 内文件名平铺有序：{names}")
        inner = [pymupdf.open("pdf", archive.read(name)) for name in names]
        check(all(part.page_count == 1 for part in inner),
              f"每份都是 1 页：{[p.page_count for p in inner]}")
        marks = [page_mark(part, 0) for part in inner]
        check(all(f"PAGE{n}MARK{n}" in mark for n, mark in zip(range(1, 5), marks))
              and all(f"第{n}页" in mark for n, mark in zip(range(1, 5), marks)),
              f"拆分后四份内容依次是第 1~4 页：{marks}")
        for part in inner:
            part.close()

    # 拆分（自定义页）
    status, body = call("split", data={"mode": "selected", "pages": "1,3"})
    doc = open_pdf(download(client, body["download_url"]))
    check(doc.page_count == 2, f"自定义拆分 1,3 → 2 页（实际 {doc.page_count}）")
    check(page_mark(doc, 0).startswith("PAGE1MARK1")
          and page_mark(doc, 1).startswith("PAGE3MARK3"), "自定义拆分的页序正确")
    doc.close()

    # 提取页面
    status, body = call("extract-pages", data={"pages": "1,3"})
    doc = open_pdf(download(client, body["download_url"]))
    check(doc.page_count == 2, f"提取 1,3 → 2 页（实际 {doc.page_count}）")
    check(page_mark(doc, 0).startswith("PAGE1MARK1")
          and page_mark(doc, 1).startswith("PAGE3MARK3"), "提取的页序正确")
    doc.close()

    # 删除页面
    status, body = call("delete-pages", data={"pages": "2"})
    doc = open_pdf(download(client, body["download_url"]))
    check(doc.page_count == 3, f"删掉第 2 页后剩 3 页（实际 {doc.page_count}）")
    remaining = [page_mark(doc, i)[:9] for i in range(doc.page_count)]
    check(remaining == ["PAGE1MARK", "PAGE3MARK", "PAGE4MARK"],
          f"剩下的页正确：{remaining}")
    doc.close()

    # 压缩
    status, body = call("compress", data={"level": "strong"})
    check(status == 200, f"压缩成功 {status}")
    check(body["original_pages"] == 4, f"压缩前 4 页：{body['original_pages']}")
    doc = open_pdf(download(client, body["download_url"]))
    check(doc.page_count == 4, f"压缩后还是 4 页（实际 {doc.page_count}）")
    check([page_mark(doc, i)[:9] for i in range(4)] ==
          ["PAGE1MARK", "PAGE2MARK", "PAGE3MARK", "PAGE4MARK"],
          "压缩没有动页面内容")
    check(sum("第" in page_mark(doc, i) for i in range(4)) == 4,
          "压缩后中文内容也都在")
    doc.close()
    check(body["original_size"] and body["size"] and body["size"] <= body["original_size"],
          f"体积 {body['original_size']} → {body['size']}（{body['saved_percent']}%）")

    # 图片合成 PDF
    status, body = call("from-images", files=[
        ("files", ("一.png", io.BytesIO(encode(Image.new("RGB", (400, 300)), "PNG")),
                   "image/png")),
        ("files", ("二.jpg", io.BytesIO(encode(Image.new("RGB", (300, 500)), "JPEG")),
                   "image/jpeg")),
        ("files", ("三.bmp", io.BytesIO(encode(Image.new("RGB", (200, 100)), "BMP")),
                   "image/bmp")),
    ], data={"page_size": "a4", "orientation": "portrait", "margin": "medium"})
    check(status == 200, f"图片合成 PDF 成功 {status}")
    doc = open_pdf(download(client, body["download_url"]))
    check(doc.page_count == 3, f"三张图 → 3 页（实际 {doc.page_count}）")
    check(all(len(doc[i].get_images()) == 1 for i in range(3)), "每页各放一张图")
    check((round(doc[0].rect.width, 2), round(doc[0].rect.height, 2)) == (595.28, 841.89),
          f"页面是 A4 纵向 {tuple(round(v, 2) for v in doc[0].rect)}")
    doc.close()
    # 多张图统一叫 images.pdf，单张才沿用原文件名（``pdf_tools._output_name``）
    check(body["filename"] == "images.pdf", f"多张图的结果名：{body['filename']}")

    status, single = call("from-images", files=[
        ("files", ("单张图.png", io.BytesIO(encode(Image.new("RGB", (400, 300)), "PNG")),
                   "image/png")),
    ], data={"page_size": "a4"})
    check(status == 200 and single["filename"] == "单张图.pdf",
          f"单张图沿用原文件名：{single.get('filename')}")

    # 六个端点都真的存在（能力矩阵说的和实际提供的一致）
    served = client.get(f"{BASE}/openapi.json").json()["paths"]
    for path in ("/api/pdf/merge", "/api/pdf/split", "/api/pdf/compress",
                 "/api/pdf/extract-pages", "/api/pdf/delete-pages",
                 "/api/pdf/from-images"):
        check(path in served, f"{path} 真的对外提供")


def verify_operation_errors(client: httpx.Client) -> None:
    print("\n=== 12. PDF 操作的错误处理 ===")
    input_id = upload_pdf(client, "汇报.pdf")

    cases = [
        ("delete-pages", {"pages": ""}, "空页码"),
        ("delete-pages", {"pages": "   "}, "空白页码"),
        ("extract-pages", {"pages": ""}, "空页码范围"),
        ("extract-pages", {"pages": "99-100"}, "超出页数"),
        ("split", {"mode": "selected", "pages": "abc"}, "页码不是数字"),
        ("split", {"mode": "nonsense"}, "拆分方式非法"),
        ("compress", {"level": " insane"}, "压缩等级非法"),
        ("compress", {"target": "custom", "target_mb": "0.0001"}, "目标体积过小"),
    ]
    for path, data, label in cases:
        payload = {"input_id": input_id, **data}
        response = client.post(f"{PDF_API}/{path}", data=payload)
        body = response.json()
        check(response.status_code >= 400, f"{label} 被拒（{response.status_code}）")
        message = (body.get("error") or {}).get("message", "")
        check(bool(message), f"{label} 有面向用户的说明：{message[:50]}")
        check_clean(message, f"{label} 的错误信息")

    response = client.post(f"{PDF_API}/delete-pages",
                           data={"input_id": "不存在的令牌", "pages": "1"})
    body = response.json()
    check(response.status_code == 404, f"伪造 input_id 返回 404（{response.status_code}）")
    check_clean((body.get("error") or {}).get("message", ""), "伪造 input_id 的错误信息")


# ----------------------------------------------------------------------
# 13. 安全（§四十五 / §四十六）
# ----------------------------------------------------------------------

def verify_security(client: httpx.Client) -> None:
    print("\n=== 13. 安全 ===")

    # 假 PDF
    response = client.post(f"{PDF_API}/merge", files=[
        ("files", ("假的.pdf", io.BytesIO("这不是 PDF，只是一串普通字节".encode("utf-8")),
                   "application/pdf")),
    ])
    check(response.status_code >= 400, f"假 PDF 被内容校验拦下（{response.status_code}）")
    message = str((response.json().get("error") or {}).get("message", ""))
    check(bool(message), f"假 PDF 有面向用户的说明：{message[:60]}")
    check_clean(message, "假 PDF 的错误信息")

    # 假扩展名：内容与扩展名不一致
    png = encode(Image.new("RGB", (200, 100), (1, 2, 3)), "PNG")
    message = rejected(client, "伪装.jpg", png, "webp", None, "假扩展名")
    check_clean(message, "假扩展名的失败信息")

    # 极小 GIF（结构不完整）
    tiny = b"GIF89a\x01\x00\x01\x00\x00\x00\x00;"
    message = rejected(client, "tiny.gif", tiny, "png", None, "残缺 GIF")
    check_clean(message, "残缺 GIF 的失败信息")

    # 穿越文件名：落盘与下载用的名字必须清洗干净
    source = labeled_pdf(3)
    for evil in ("../../evil.pdf", "..\\..\\windows.pdf", "C:\\Windows\\Temp\\evil.pdf",
                 "....//....//evil.pdf"):
        response = client.post(f"{PDF_API}/upload",
                               files={"file": (evil, io.BytesIO(source), "application/pdf")})
        check(response.status_code == 200, f"穿越名 {evil!r} 被受理（{response.status_code}）")
        token = response.json()["input_id"]
        check("/" not in token and "\\" not in token, f"令牌里没有路径成分：{token}")

        response = client.post(f"{PDF_API}/split",
                               data={"input_id": token, "mode": "every"})
        body = response.json()
        names = [body.get("filename"), body.get("archive_filename") or ""]
        names += [item["filename"] for item in body.get("files", [])]
        for name in names:
            check(name and "/" not in name and "\\" not in name
                  and not name.startswith(".") and ":" not in name,
                  f"结果名被清洗：{evil!r} → {name!r}")
        with zipfile.ZipFile(io.BytesIO(download(client, body["download_url"]))) as archive:
            for info in archive.infolist():
                check("/" not in info.filename and "\\" not in info.filename
                      and not info.filename.startswith("."),
                      f"ZIP 内条目平铺：{info.filename}")

    # 统一队列里的穿越名
    status, body = convert(client, "../../evil.png", png, "jpg")
    task = body["tasks"][0]
    check(task["status"] == "completed", f"统一队列的穿越名任务完成：{task['status']}")
    name = task["result"]["filename"]
    check("/" not in name and "\\" not in name and ".." not in name,
          f"统一队列的结果名被清洗：{name!r}")
    response = client.get(f"{BASE}{task['result']['download_url']}")
    disposition = response.headers.get("content-disposition", "")
    check(".." not in disposition and "\\" not in disposition,
          f"下载头里没有上跳：{disposition}")

    # options 绕不过资源上限
    oversized = encode(Image.new("RGB", (6000, 4000)), "PNG")
    message = rejected(
        client, "huge.png", oversized, "jpg",
        '{"resize.mode":"custom","resize.width":999999,"resize.height":999999}',
        "options 里的越界尺寸",
    )
    check_clean(message, "options 越界尺寸的失败信息")

    # 未知选项键
    message = rejected(client, "x.png", png, "jpg", '{"nonsense":1}', "未知选项键")
    check_clean(message, "未知选项键的失败信息")

    # 超大 options（超过 8KB）。
    # ``convert`` 的第二个返回值在提交被拒时是错误响应体，所以这里
    # 不能直接当任务快照用 —— 只取状态码与那句话。
    huge_options = '{"' + "a" * 9000 + '":1}'
    response = client.post(
        f"{API}/tasks",
        files={"files": ("x.png", io.BytesIO(png), "application/octet-stream")},
        data={"target_type": "jpg", "options": huge_options},
    )
    check(response.status_code == 400, f"超大 options 被拒（HTTP {response.status_code}）")
    check_clean(
        str((response.json().get("error") or {}).get("message", "")),
        "超大 options 的错误信息",
    )

    # 界面上会展示的每一句说明（能力矩阵的 notes、操作条目的 note）都不许
    # 泄露内部细节 —— 它们和错误信息一样会直接出现在用户眼前。
    capabilities = client.get(CAPABILITIES).json()
    for note in capabilities.get("notes") or []:
        text = note if isinstance(note, str) else str(note.get("message", note))
        check_clean(text, "能力矩阵里的说明")
    for item in capabilities["conversions"] + capabilities["operations"]:
        check_clean(item["display_name"], f"{item['id']} 的显示名")
        if item.get("note"):
            check_clean(item["note"], f"{item['id']} 的说明")


# ----------------------------------------------------------------------
# 14. 队列与 worker 健康（§五十八）
# ----------------------------------------------------------------------

def workers(client: httpx.Client) -> dict:
    return client.get(f"{BASE}/api/system/workers").json()


def metrics(client: httpx.Client) -> dict:
    return client.get(f"{BASE}/api/system/metrics").json()


def docx_bytes(paragraphs: list[str]) -> bytes:
    """一份真的 .docx（python-docx 写的），给 office 池用。"""
    document = Document()
    for line in paragraphs:
        document.add_paragraph(line)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def verify_queue_health(client: httpx.Client) -> None:
    """一次真的混批，验 §五十八 的四条：不超订、不死锁、不卡死、不重复。

    这一节关心的是**队列的形状**，不是产物长什么样（产物由 1~13 节逐条验过）。
    所以样本刻意做成多个池子混在一起：10 张图（image 池）、5 份 PDF 转 Word
    （**ocr 池**，它 requires ``pdf_to_word``，走的是 OCR 那条资源隔离）、
    5 份 Office→PDF（office 池，LibreOffice 串行锁）——
    单池的批量测不出跨池争抢，而超订与死锁恰恰只在混批里现形。

    每个任务落在哪个池子不在这里手抄，而是提交时带上 ``capability_id``、
    判定时回能力目录里读 ``worker_pool``（见 ``pool_of``）。手抄一张
    「PDF 就该进 pdf 池」的表会在第一次运行时就报假失败。

    **这一节必须独占后端**：它比的是「投进去多少、池子经手多少」的差值，
    此时若有别的脚本（比如 verify_phase9.py）也在提交任务，差值必然对不上，
    报出来的会是别人的任务。跑之前先确认没有别的验收在跑。
    """
    print("\n=== 14. 队列与 worker 健康 ===")

    before_workers = workers(client)
    before_metrics = metrics(client)

    pools: dict[str, dict] = {item["name"]: item for item in before_workers["pools"]}
    configured = {name: item["configured_workers"] for name, item in pools.items()}
    handled_before = {name: item["handled"] for name, item in pools.items()}

    # 每批活落在哪个池子，**由服务端的能力目录说了算**，不在这里手抄一份。
    # 手抄会当场错：``pdf → docx`` 走的是 ``ocr`` 池而不是 ``pdf`` 池
    # （它 requires pdf_to_word，要过 OCR 那条资源隔离），照着「PDF 就该进
    # pdf 池」的直觉写，测出来的会是脚本自己的假设。
    catalog = {entry["id"]: entry for entry in client.get(CAPABILITIES).json()["conversions"]}

    def pool_of(capability_id: str) -> str:
        entry = catalog.get(capability_id)
        if entry is None:
            raise SystemExit(f"能力目录里没有 {capability_id}，这一节无法判定池子")
        return entry["worker_pool"]

    office_job = docx_bytes([f"批量验收第 {i} 段" for i in range(1, 6)])
    # (能力 id, 文件名, 内容, 目标格式)
    plan = (
        [("image.png-to-jpg", f"batch{i}.png", noisy(320, 240, "PNG"), "jpg") for i in range(10)]
        + [("pdf.pdf-to-docx", f"batch{i}.pdf", labeled_pdf(2, f"B{i}"), "docx") for i in range(5)]
        + [("document.docx-to-pdf", f"batch{i}.docx", office_job, "pdf") for i in range(5)]
    )

    submitted: dict[str, str] = {}  # batch_id -> 文件名
    for capability_id, filename, content, target in plan:
        response = client.post(
            f"{API}/tasks",
            files={"files": (filename, io.BytesIO(content), "application/octet-stream")},
            data={"target_type": target, "capability_id": capability_id},
        )
        if response.status_code != 202:
            check(False, f"混批提交 {filename}→{target} 被拒（{response.status_code}）")
            return
        submitted[response.json()["batch_id"]] = filename

    total = len(submitted)
    check(total == len(plan), f"混批 {len(plan)} 个任务（图 / PDF / Office）全部受理")

    # 每个池子这一批应该经手多少，按目录里声明的池子数出来
    expected_per_pool: dict[str, int] = {}
    for capability_id, _, _, _ in plan:
        pool = pool_of(capability_id)
        expected_per_pool[pool] = expected_per_pool.get(pool, 0) + 1
    check(len(expected_per_pool) >= 2,
          f"这一批真的散在多个池子里（{'、'.join(sorted(expected_per_pool))}）")

    # --- 一边等一边采样：超订与死锁只有在**跑的过程中**才看得见
    peak_active: dict[str, int] = {name: 0 for name in configured}
    peak_queue: dict[str, int] = {name: 0 for name in configured}
    over_subscribed: list[str] = []
    samples = 0
    deadline = time.time() + 600
    while time.time() < deadline:
        snapshot = workers(client)
        samples += 1
        for item in snapshot["pools"]:
            name = item["name"]
            peak_active[name] = max(peak_active.get(name, 0), item["active"])
            peak_queue[name] = max(peak_queue.get(name, 0), item["queue_size"])
            if item["active"] > item["configured_workers"]:
                over_subscribed.append(
                    f"{name}: {item['active']}/{item['configured_workers']}"
                )
        pending = [
            batch for batch in submitted
            if not all(
                task["status"] in TERMINAL
                for task in client.get(f"{API}/tasks/{batch}").json()["tasks"]
            )
        ]
        if not pending:
            break
        time.sleep(0.3)

    check(not over_subscribed,
          f"没有 worker 超订（采样 {samples} 次，各池峰值 "
          f"{ {name: peak_active[name] for name in sorted(expected_per_pool)} }）")
    check(all(item["started_workers"] == item["configured_workers"]
              for item in workers(client)["pools"]),
          "每个池子的 worker 都起齐了（没有静默少起）")
    check(samples > 3, f"等待期间真的采到了中间态（{samples} 次采样）")

    # --- 全部落地：没有卡死、没有死锁
    final: dict[str, dict] = {}
    for batch in submitted:
        body = client.get(f"{API}/tasks/{batch}").json()
        final[batch] = body["tasks"][0]
    stuck = [batch for batch, task in final.items() if task["status"] not in TERMINAL]
    check(not stuck, f"{total} 个任务全部落地，没有卡在半路（卡住 {len(stuck)} 个）")
    done = [batch for batch, task in final.items() if task["status"] == "completed"]
    check(len(done) == total,
          f"{total} 个任务全部成功（成功 {len(done)}，失败 "
          f"{[ (submitted[b], final[b].get('error_message')) for b in final if final[b]['status'] != 'completed' ]}）")

    # 队列必须排空 —— 排在 0 不下来的就是死锁
    drained = workers(client)
    check(all(item["queue_size"] == 0 and item["active"] == 0 for item in drained["pools"]),
          "跑完之后每个池子的队列与在跑数都回到 0")
    check(drained["total_queue"] == 0, f"总队列长度为 0（{drained['total_queue']}）")

    # --- 不重复处理：每个池子经手的任务数正好等于投进去的数量
    after = workers(client)["pools"]
    for item in after:
        name = item["name"]
        if name not in expected_per_pool:
            continue
        delta = item["handled"] - handled_before.get(name, 0)
        check(delta == expected_per_pool[name],
              f"{name} 池一共经手 {delta} 个任务，正好等于投进去的 "
              f"{expected_per_pool[name]} 个（多出来就是重复处理）")
        check(item["lost"] == 0, f"{name} 池没有丢过任务（lost={item['lost']}）")

    # 每个任务各自拿到产物，且产物名互不相同 —— 同一份结果发两次也算重复
    names = [task["result"]["filename"] for task in final.values() if task.get("result")]
    check(len(names) == len(set(names)),
          f"{total} 个任务各自拿到互不相同的产物名（重复 {total - len(set(names))} 个）")

    after_metrics = metrics(client)
    check(after_metrics["tasks"]["timeout"] == before_metrics["tasks"]["timeout"],
          "这一批没有任务超时")
    check(after_metrics["workers"]["watchdog_kicks"]
          == before_metrics["workers"]["watchdog_kicks"],
          "看门狗没有出手（没有 worker 卡死）")
    check(after_metrics["workers"]["recycled"] == before_metrics["workers"]["recycled"],
          "没有 worker 被回收重建")
    check(after_metrics["tasks"]["total"] - before_metrics["tasks"]["total"] == total,
          f"指标里新增的任务数正好是 {total}（多了就是重复计数）")


def main() -> int:
    with httpx.Client(timeout=240) as client:
        try:
            client.get(f"{BASE}/api/health").raise_for_status()
        except Exception as exc:
            print(f"后端没有在 {BASE} 上跑：{exc}")
            return 2

        verify_capabilities(client)
        verify_option_gating(client)
        verify_new_formats(client)
        verify_rotation(client)
        verify_resize(client)
        verify_quality(client)
        verify_dpi(client)
        verify_metadata(client)
        verify_multiframe(client)
        verify_image_to_pdf(client)
        verify_pdf_operations(client)
        verify_operation_errors(client)
        verify_security(client)
        verify_queue_health(client)

    print()
    print(f"通过 {_checks - len(_failures)} 项，失败 {len(_failures)} 项")
    for item in _failures:
        print(f"  失败：{item}")
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main())
