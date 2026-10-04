"""测试公共夹具。"""

from __future__ import annotations

import io
import time
from html import unescape
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw, ImageFont

from main import app
from utils.errors import status_for_code

# 批量任务在后台队列里跑，测试必须等它跑完。
# 上限放得比真实处理宽很多：正常的单文件任务是毫秒级的，
# 一旦真的挂住，这里会直接失败而不是把整个测试卡死。
TASK_TIMEOUT_SECONDS = 60.0

# 终态：到了这两个状态就不会再变
_TASK_FINAL_STATES = ("done", "failed")


@pytest.fixture(scope="session")
def client() -> TestClient:
    with TestClient(app) as c:
        yield c


class TaskOutcome:
    """批量任务的最终结果，形状与「同步接口的响应」一致。

    第四阶段把批量接口改成了「提交 → 轮询」，但绝大多数测试关心的是
    「这次处理的结果是什么」。这个类把两步合成一步，让测试里
    ``response.status_code`` / ``response.json()`` 的写法保持不变：

    - 整批成功：状态码是 200，响应体就是原同步接口的响应体；
    - 整批失败：状态码由错误码还原（例如 CORRUPTED_FILE → 400），
      响应体是 ``{"error": {"code", "message"}}``，与同步接口的报错完全一致。
    """

    __slots__ = ("status_code", "payload", "snapshot")

    def __init__(self, status_code: int, payload: dict, snapshot: dict | None = None) -> None:
        self.status_code = status_code
        self.payload = payload
        self.snapshot = snapshot

    @property
    def text(self) -> str:
        return str(self.payload)

    def json(self) -> dict:
        return self.payload


def submit_task(client: TestClient, endpoint: str, *, files=None, data=None):
    """提交批量任务，返回接口的原始响应（202 + 任务号）。"""
    return client.post(endpoint, files=files, data=data)


def wait_for_task(client: TestClient, group_id: str, *, timeout: float = TASK_TIMEOUT_SECONDS) -> dict:
    """轮询任务直到它进入终态。"""
    deadline = time.monotonic() + timeout
    while True:
        response = client.get(f"/api/tasks/{group_id}")
        assert response.status_code == 200, response.text
        snapshot = response.json()
        if snapshot["state"] in _TASK_FINAL_STATES:
            return snapshot
        if time.monotonic() > deadline:
            raise AssertionError(f"任务在 {timeout} 秒内没有完成：{snapshot}")
        time.sleep(0.02)


def run_task(client: TestClient, endpoint: str, *, files=None, data=None) -> TaskOutcome:
    """提交批量任务并等到结束，返回与同步接口等价的响应。"""
    response = submit_task(client, endpoint, files=files, data=data)
    if response.status_code != 202:
        # 提交阶段就被拒（参数不合法、文件太多、整批超限）
        return TaskOutcome(response.status_code, response.json())

    group = response.json()
    snapshot = wait_for_task(client, group["group_id"])
    if snapshot["state"] == "done":
        return TaskOutcome(200, snapshot["result"] or {}, snapshot)

    error = snapshot["error"] or {
        "code": "PROCESSING_FAILED",
        "message": "处理失败",
    }
    return TaskOutcome(status_for_code(error["code"]), {"error": error}, snapshot)


# ----------------------------------------------------------------------
# 统一转换中心（第七阶段）
#
# 转换中心**自己的**一套状态词汇（规格 §八：queued / processing /
# completed / failed / cancelled，外加协作式取消的 cancelling），与上面
# ``/api/tasks`` 的 ``state`` 不是同一套。所以轮询要另写两个薄薄的 helper，
# 而不是硬把上面那两个改造成能同时认两套词汇 —— 那样两边都会变难读。
# ----------------------------------------------------------------------

CONVERSION_ENDPOINT = "/api/conversion/tasks"

#: 转换批次的终态。比 ``/api/tasks`` 多一个 ``cancelled``：
#: 用户取消掉的批次同样是「定下来了」，不能一直轮询下去。
_CONVERSION_FINAL_STATES = ("completed", "failed", "cancelled")


def submit_conversion(client: TestClient, *, files, target_type: str, **data):
    """提交一批转换，返回接口的原始响应（202 + batch_id）。"""
    return client.post(
        CONVERSION_ENDPOINT, files=files, data={"target_type": target_type, **data}
    )


def wait_conversion(
    client: TestClient, batch_id: str, *, timeout: float = TASK_TIMEOUT_SECONDS
) -> dict:
    """轮询转换批次直到它进入终态。"""
    deadline = time.monotonic() + timeout
    while True:
        response = client.get(f"{CONVERSION_ENDPOINT}/{batch_id}")
        assert response.status_code == 200, response.text
        snapshot = response.json()
        if snapshot["status"] in _CONVERSION_FINAL_STATES:
            return snapshot
        if time.monotonic() > deadline:
            raise AssertionError(f"转换任务在 {timeout} 秒内没有结束：{snapshot}")
        time.sleep(0.02)


def run_conversion(client: TestClient, *, files, target_type: str, **data) -> dict:
    """提交并等到结束，返回终态快照。"""
    response = submit_conversion(client, files=files, target_type=target_type, **data)
    assert response.status_code == 202, response.text
    return wait_conversion(client, response.json()["batch_id"])


def conversion_task(snapshot: dict, index: int) -> dict:
    """按序号取某一项的状态。"""
    for task in snapshot["tasks"]:
        if task["index"] == index:
            return task
    raise AssertionError(f"任务里没有第 {index} 项：{snapshot}")


def build_image_bytes(
    width: int = 1600,
    height: int = 1200,
    fmt: str = "JPEG",
    *,
    noisy: bool = True,
    color: tuple[int, int, int] = (60, 120, 220),
) -> bytes:
    """生成一张有渐变、图形和噪点的测试图片。

    纯色图压缩率过高，无法验证「按目标大小压缩」的逻辑；
    加入渐变和噪点后，体积和真实照片更接近。
    """
    image = Image.linear_gradient("L").resize((width, height)).convert("RGB")

    if noisy:
        noise = Image.effect_noise((width, height), 48).convert("L")
        image = Image.blend(image, Image.merge("RGB", (noise, noise, noise)), 0.35)

    draw = ImageDraw.Draw(image)
    for i in range(6):
        offset = i * (width // 8)
        draw.ellipse(
            [offset, height // 4, offset + width // 5, height // 4 + width // 5],
            fill=color,
        )

    buffer = io.BytesIO()
    save_kwargs: dict = {}
    pillow_format = _pillow_format_name(fmt)
    if pillow_format in _LOSSY_FOR_SAMPLE:
        save_kwargs["quality"] = 95
    image.save(buffer, format=pillow_format, **save_kwargs)
    return buffer.getvalue()


#: 扩展名 / 我们的词汇 -> Pillow 认的格式名。
#:
#: Pillow 只认 ``JPEG``（不认 ``JPG``）、``TIFF``（不认 ``TIF``），
#: 而 HEIC 的容器名是 ``HEIF``。调用方按源类型拼出来的名字常常是前者，
#: 所以这张小表在**造样张**这一层把它们对上 —— 不这么做的话，
#: ``build_image_bytes(..., "JPG")`` 会抛 ``KeyError: 'JPG'``，
#: 而那个报错看起来像是 Pillow 缺了个编码器。
_PILLOW_FORMAT_ALIASES = {
    "JPG": "JPEG",
    "TIF": "TIFF",
    "HEIC": "HEIF",
}

#: 造样张时要显式给 quality 的有损格式：默认质量偏低，
#: 「按目标大小压缩」一类的测试会被过低的起点带偏。
_LOSSY_FOR_SAMPLE = ("JPEG", "WEBP", "HEIF")


def _pillow_format_name(fmt: str) -> str:
    name = fmt.upper()
    return _PILLOW_FORMAT_ALIASES.get(name, name)


#: 造动图/多页样张时的用色，按顺序循环取 —— 帧数可以随便要多少
ANIMATION_COLORS = ((255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0), (0, 255, 255))


def build_animated_gif(frames: int = 3, size: tuple[int, int] = (30, 20)) -> bytes:
    """一张**真的动图**（多帧 GIF）。

    第九阶段只转换第一帧（决策 C），所以「多帧」这件事必须有真样张才验得了 ——
    单帧 GIF 走的是另一条路，测不出那句「只转换了第一帧」的说明。
    """
    images = [
        Image.new("RGB", size, ANIMATION_COLORS[index % len(ANIMATION_COLORS)])
        for index in range(max(2, frames))
    ]
    buffer = io.BytesIO()
    images[0].save(
        buffer, format="GIF", save_all=True, append_images=images[1:], duration=100, loop=0
    )
    return buffer.getvalue()


def build_multipage_tiff(pages: int = 3, size: tuple[int, int] = (30, 20)) -> bytes:
    """一份**多页** TIFF。理由同 :func:`build_animated_gif`。"""
    images = [
        Image.new("RGB", size, ANIMATION_COLORS[index % len(ANIMATION_COLORS)])
        for index in range(max(2, pages))
    ]
    buffer = io.BytesIO()
    images[0].save(buffer, format="TIFF", save_all=True, append_images=images[1:])
    return buffer.getvalue()


@pytest.fixture
def jpeg_bytes() -> bytes:
    return build_image_bytes(1600, 1200, "JPEG")


@pytest.fixture
def png_bytes() -> bytes:
    return build_image_bytes(900, 700, "PNG")


@pytest.fixture
def webp_bytes() -> bytes:
    return build_image_bytes(1200, 900, "WEBP")


def build_pdf_bytes(
    pages: int = 3,
    *,
    width: float = 595.0,
    height: float = 842.0,
    with_image: bool = True,
) -> bytes:
    """生成一份真实的测试 PDF。

    每一页都放一张图片 —— 空的 PDF 页压缩起来没有任何内容可压，
    无法用来验证压缩、转图片这些功能。
    """
    import pymupdf

    doc = pymupdf.open()
    try:
        photo = build_image_bytes(640, 480, "JPEG")
        for index in range(pages):
            page = doc.new_page(width=width, height=height)
            if with_image:
                page.insert_image(
                    pymupdf.Rect(60, 60, width - 60, height - 60),
                    stream=photo,
                    keep_proportion=True,
                )
            page.insert_text((72, 40), f"Page {index + 1}", fontsize=14)
        return doc.tobytes(deflate=True, garbage=4, clean=True)
    finally:
        doc.close()


@pytest.fixture
def pdf_bytes() -> bytes:
    return build_pdf_bytes(3)


def build_labeled_pdf(label: str, pages: int, *, size: float = 400.0) -> bytes:
    """生成一份每页都写着「label-N」的 PDF。

    合并、拆分、删页这些功能都要验证**页序**，
    而页序只能靠每页内容不同才看得出来。
    """
    import pymupdf

    doc = pymupdf.open()
    try:
        for index in range(pages):
            page = doc.new_page(width=size, height=size)
            page.insert_text((60, 100), f"{label}-{index + 1}", fontsize=24)
        return doc.tobytes(deflate=True, garbage=4, clean=True)
    finally:
        doc.close()


def pdf_files(*items: tuple[str, bytes], field: str = "files") -> list[tuple[str, tuple]]:
    """把 (文件名, 内容) 转成 TestClient 需要的上传结构。"""
    return [(field, (name, io.BytesIO(data), "application/pdf")) for name, data in items]


def image_files(*items: tuple[str, bytes], field: str = "files") -> list[tuple[str, tuple]]:
    """图片上传结构，MIME 按扩展名推断。"""
    mapping = {
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "png": "image/png",
        "webp": "image/webp",
        "bmp": "image/bmp",
        "gif": "image/gif",
        "tif": "image/tiff",
        "tiff": "image/tiff",
        "svg": "image/svg+xml",
    }
    return [
        (
            field,
            (
                name,
                io.BytesIO(data),
                mapping.get(name.rsplit(".", 1)[-1].lower(), "image/jpeg"),
            ),
        )
        for name, data in items
    ]


# ----------------------------------------------------------------------
# 文档转换（第五阶段）
# ----------------------------------------------------------------------

#: 所有 Office 样张正文里都带这个标记，用来确认「转换出来的 PDF 里真有原文」。
#: 不要改成纯数字或英文单词：中文能顺带验证编码没被搞坏。
OFFICE_MARKER = "MARKER-测试标记-42"

#: 隐藏工作表里的标记。转换时隐藏表不导出，所以这个串**不该**出现在 PDF 里。
HIDDEN_SHEET_MARKER = "HIDDEN-隐藏表"

#: 隐藏幻灯片里的标记。隐藏幻灯片导不导出要用真样张问清楚（见 build_pptx_bytes）。
HIDDEN_SLIDE_MARKER = "HIDDEN-隐藏页"


def normalize_text(text: str) -> str:
    """去掉所有空白，用于比对 PDF 里抽出来的文字。

    LibreOffice 导出 PDF 时会按自己的断行规则插空格，
    实测同一句话在 docx 里是 `MARKER-测试标记-42`、在 pptx 里却抽成
    `MARKER- 测试标记-42`。带空格直接比对会得到假失败。
    """
    return "".join(text.split())


def zip_bytes(parts: dict[str, str]) -> bytes:
    """把若干文本部件打成一个 zip（手写 OOXML 样张的底座）。"""
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, text in parts.items():
            archive.writestr(name, text.encode("utf-8"))
    return buffer.getvalue()


# OOXML 里那几个命名空间，样张要手写 XML，抽出来免得每处都抄一遍
_NS_CT = "http://schemas.openxmlformats.org/package/2006/content-types"
_NS_REL = "http://schemas.openxmlformats.org/package/2006/relationships"


def build_docx_bytes(marker: str = OFFICE_MARKER, *, paragraphs: int = 1) -> bytes:
    """一份最小但**结构完整**的 .docx（LibreOffice 能打开并转出真 PDF）。

    ``paragraphs`` 大于 1 时排出来的 PDF 会有几十页、几百 KB ——
    「最大文件大小」压不到目标那条分支需要一个**排得够大**的样张才测得到
    （纯文字的 PDF 几乎压不动，见 tests/test_office_api.py 里的用例）。
    """
    body = "".join(
        f"<w:p><w:r><w:t>{marker} 第{i}段：中英文混排 with English words "
        f"and numbers {i * 13}。</w:t></w:r></w:p>"
        for i in range(1, max(1, paragraphs) + 1)
    )
    return zip_bytes(
        {
            "[Content_Types].xml": f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="{_NS_CT}">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>""",
            "_rels/.rels": f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="{_NS_REL}">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>""",
            "word/document.xml": f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
<w:body>{body}</w:body>
</w:document>""",
        }
    )


def build_xlsx_bytes(marker: str = OFFICE_MARKER, *, hidden: int = 0) -> bytes:
    """一份**两个工作表**的 .xlsx，可选再加 N 张隐藏表。

    特意做成两张表：LibreOffice 会把全部工作表导出进同一个 PDF，
    「已导出 N 个工作表」这句说明得有样张才验得了。

    ``hidden`` 加的表带 ``state="hidden"``。实测它们**不会**被导出，
    所以样张里塞的是 :data:`HIDDEN_SHEET_MARKER` —— 断言它在 PDF 里
    找不到，才说明「隐藏表不导出」这句话是真的。
    """
    #: 每项是 (表名, A1 单元格内容, 是否隐藏)
    entries: list[tuple[str, str, bool]] = [
        ("第一张表", marker, False),
        ("第二张表", "第二张表的内容", False),
    ]
    entries.extend(
        (f"隐藏表{index + 1}", f"{HIDDEN_SHEET_MARKER}-{index + 1}", True)
        for index in range(hidden)
    )

    sheet = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>%s</t></is></c></row></sheetData>
</worksheet>"""

    overrides = "\n".join(
        f'<Override PartName="/xl/worksheets/sheet{index + 1}.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.'
        'spreadsheetml.worksheet+xml"/>'
        for index in range(len(entries))
    )
    sheet_tags = "\n".join(
        f'<sheet name="{name}" sheetId="{index + 1}" r:id="rId{index + 1}"'
        + (' state="hidden"/>' if is_hidden else "/>")
        for index, (name, _, is_hidden) in enumerate(entries)
    )
    rels = "\n".join(
        f'<Relationship Id="rId{index + 1}" Type="http://schemas.openxmlformats.org'
        '/officeDocument/2006/relationships/worksheet" '
        f'Target="worksheets/sheet{index + 1}.xml"/>'
        for index in range(len(entries))
    )

    parts = {
        "[Content_Types].xml": f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="{_NS_CT}">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
{overrides}
</Types>""",
        "_rels/.rels": f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="{_NS_REL}">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>""",
        "xl/workbook.xml": f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
<sheets>
{sheet_tags}
</sheets>
</workbook>""",
        "xl/_rels/workbook.xml.rels": f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="{_NS_REL}">
{rels}
</Relationships>""",
    }
    for index, (_, text, _) in enumerate(entries):
        parts[f"xl/worksheets/sheet{index + 1}.xml"] = sheet % text
    return zip_bytes(parts)


_PPT_SLIDE = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:sld xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
 xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"%s>
<p:cSld><p:spTree>
<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>
<p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/>
<a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>
<p:sp>
<p:nvSpPr><p:cNvPr id="2" name="Title 1"/><p:cNvSpPr><a:spLocks noGrp="1"/></p:cNvSpPr>
<p:nvPr><p:ph type="ctrTitle"/></p:nvPr></p:nvSpPr>
<p:spPr><a:xfrm><a:off x="1143000" y="2286000"/><a:ext cx="6858000" cy="1143000"/></a:xfrm></p:spPr>
<p:txBody><a:bodyPr/><a:lstStyle/>
<a:p><a:r><a:rPr lang="zh-CN" dirty="0"/><a:t>%s</a:t></a:r></a:p></p:txBody>
</p:sp>
</p:spTree></p:cSld>
<p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr>
</p:sld>"""

_PPT_LAYOUT = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:sldLayout xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
 xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" type="title">
<p:cSld name="Title"><p:spTree>
<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>
<p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/>
<a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>
</p:spTree></p:cSld>
<p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr>
</p:sldLayout>"""


def build_pptx_bytes(
    marker: str = OFFICE_MARKER, *, slides: int = 1, hidden: int = 0
) -> bytes:
    """一份最小但**依赖完整**的 .pptx，可选多张幻灯片与隐藏幻灯片。

    Impress 要求 slide 挂在 layout 上、layout 挂在 master 上、
    master 和 presentation 各自要引到 theme，少一环就打不开，
    所以这里看着啰嗦，但每一段都是必需的。

    ``hidden`` 加的幻灯片在 ``<p:sld>`` 上带 ``show="0"`` —— 这是 OOXML 里
    「隐藏幻灯片」的表达方式。它导不导出要用真样张问清楚，
    不能凭印象写进给用户看的说明里。
    """
    #: (幻灯片正文, 是否隐藏)
    entries: list[tuple[str, bool]] = [
        (marker if index == 0 else f"第{index + 1}页幻灯片的内容", False)
        for index in range(slides)
    ]
    entries.extend(
        (f"{HIDDEN_SLIDE_MARKER}-{index + 1}", True) for index in range(hidden)
    )

    slide_overrides = "\n".join(
        f'<Override PartName="/ppt/slides/slide{index + 1}.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.'
        'presentationml.slide+xml"/>'
        for index in range(len(entries))
    )
    sld_ids = "\n".join(
        f'<p:sldId id="{256 + index}" r:id="rId{index + 2}"/>'
        for index in range(len(entries))
    )
    slide_rels = "\n".join(
        f'<Relationship Id="rId{index + 2}" Type="http://schemas.openxmlformats.org'
        '/officeDocument/2006/relationships/slide" '
        f'Target="slides/slide{index + 1}.xml"/>'
        for index in range(len(entries))
    )
    # 主题的 rId 排在全部幻灯片之后
    theme_rid = len(entries) + 2

    parts = {
        "[Content_Types].xml": f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="{_NS_CT}">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/ppt/presentation.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/>
{slide_overrides}
<Override PartName="/ppt/slideLayouts/slideLayout1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideLayout+xml"/>
<Override PartName="/ppt/slideMasters/slideMaster1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideMaster+xml"/>
<Override PartName="/ppt/theme/theme1.xml" ContentType="application/vnd.openxmlformats-officedocument.theme+xml"/>
</Types>""",
            "_rels/.rels": f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="{_NS_REL}">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="ppt/presentation.xml"/>
</Relationships>""",
            "ppt/presentation.xml": f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:presentation xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
 xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
<p:sldMasterIdLst><p:sldMasterId id="2147483648" r:id="rId1"/></p:sldMasterIdLst>
<p:sldIdLst>
{sld_ids}
</p:sldIdLst>
<p:sldSz cx="9144000" cy="6858000"/><p:notesSz cx="6858000" cy="9144000"/>
</p:presentation>""",
            "ppt/_rels/presentation.xml.rels": f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="{_NS_REL}">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideMaster" Target="slideMasters/slideMaster1.xml"/>
{slide_rels}
<Relationship Id="rId{theme_rid}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/theme" Target="theme/theme1.xml"/>
</Relationships>""",
            "ppt/slideLayouts/slideLayout1.xml": _PPT_LAYOUT,
            "ppt/slideLayouts/_rels/slideLayout1.xml.rels": f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="{_NS_REL}">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideMaster" Target="../slideMasters/slideMaster1.xml"/>
</Relationships>""",
            "ppt/slideMasters/slideMaster1.xml": """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:sldMaster xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
 xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
<p:cSld><p:spTree>
<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>
<p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/>
<a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>
</p:spTree></p:cSld>
<p:clrMap bg1="lt1" tx1="dk1" bg2="lt2" tx2="dk2" accent1="accent1"
 accent2="accent2" accent3="accent3" accent4="accent4" accent5="accent5" accent6="accent6"
 hlink="hlink" folHlink="folHlink"/>
<p:sldLayoutIdLst><p:sldLayoutId id="2147483649" r:id="rId1"/></p:sldLayoutIdLst>
<p:txStyles><p:titleStyle/><p:bodyStyle/><p:otherStyle/></p:txStyles>
</p:sldMaster>""",
            "ppt/slideMasters/_rels/slideMaster1.xml.rels": f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="{_NS_REL}">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout" Target="../slideLayouts/slideLayout1.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/theme" Target="../theme/theme1.xml"/>
</Relationships>""",
            "ppt/theme/theme1.xml": """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<a:theme xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" name="Office">
<a:themeElements>
<a:clrScheme name="Office"><a:dk1><a:sysClr val="windowText" lastClr="000000"/></a:dk1>
<a:lt1><a:sysClr val="window" lastClr="FFFFFF"/></a:lt1>
<a:dk2><a:srgbClr val="44546A"/></a:dk2><a:lt2><a:srgbClr val="E7E6E6"/></a:lt2>
<a:accent1><a:srgbClr val="4472C4"/></a:accent1><a:accent2><a:srgbClr val="ED7D31"/></a:accent2>
<a:accent3><a:srgbClr val="A5A5A5"/></a:accent3><a:accent4><a:srgbClr val="FFC000"/></a:accent4>
<a:accent5><a:srgbClr val="5B9BD5"/></a:accent5><a:accent6><a:srgbClr val="70AD47"/></a:accent6>
<a:hlink><a:srgbClr val="0563C1"/></a:hlink><a:folHlink><a:srgbClr val="954F72"/></a:folHlink>
</a:clrScheme>
<a:fontScheme name="Office"><a:majorFont><a:latin typeface="Calibri Light"/><a:ea typeface=""/><a:cs typeface=""/></a:majorFont>
<a:minorFont><a:latin typeface="Calibri"/><a:ea typeface=""/><a:cs typeface=""/></a:minorFont></a:fontScheme>
<a:fmtScheme name="Office">
<a:fillStyleLst><a:solidFill><a:schemeClr val="phClr"/></a:solidFill>
<a:solidFill><a:schemeClr val="phClr"/></a:solidFill>
<a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:fillStyleLst>
<a:lnStyleLst><a:ln><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:ln>
<a:ln><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:ln>
<a:ln><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:ln></a:lnStyleLst>
<a:effectStyleLst><a:effectStyle><a:effectLst/></a:effectStyle>
<a:effectStyle><a:effectLst/></a:effectStyle><a:effectStyle><a:effectLst/></a:effectStyle></a:effectStyleLst>
<a:bgFillStyleLst><a:solidFill><a:schemeClr val="phClr"/></a:solidFill>
<a:solidFill><a:schemeClr val="phClr"/></a:solidFill>
<a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:bgFillStyleLst>
</a:fmtScheme>
</a:themeElements>
</a:theme>""",
    }

    for index, (text, is_hidden) in enumerate(entries):
        # show="0" 就是「隐藏幻灯片」
        parts[f"ppt/slides/slide{index + 1}.xml"] = _PPT_SLIDE % (
            ' show="0"' if is_hidden else "",
            text,
        )
        parts[f"ppt/slides/_rels/slide{index + 1}.xml.rels"] = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="{_NS_REL}">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout" Target="../slideLayouts/slideLayout1.xml"/>
</Relationships>"""

    return zip_bytes(parts)


#: 旧格式各类别在 OLE2 目录项里必有的流名（和 office/loader.py 的假设一致）
OLE2_STREAM_BY_EXT = {
    "doc": "WordDocument",
    "xls": "Workbook",
    "ppt": "PowerPoint Document",
}


def build_ole2_bytes(stream: str | None = None) -> bytes:
    """造一个 OLE2 头的字节串。

    OLE2 是真二进制格式，手写不出完整文件（也测不出真转换），
    所以这里只用来测**校验层**：给它一个 OLE2 头 + 指定流名，
    看 loader 认不认。真正能转换的旧格式样张由
    `scripts/make_office_fixtures.py` 用 LibreOffice 生成。
    """
    payload = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 512
    if stream is not None:
        payload += stream.encode("utf-16-le") + b"\x00" * 64
    return payload


#: 文件扩展名 -> MIME（上传时后端不校验 MIME，但 TestClient 需要给一个）
_OFFICE_MIMES = {
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "doc": "application/msword",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "xls": "application/vnd.ms-excel",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "ppt": "application/vnd.ms-powerpoint",
    "txt": "text/plain",
}


def office_files(*items: tuple[str, bytes], field: str = "file") -> list[tuple[str, tuple]]:
    """文档上传结构。

    默认字段名是 ``file``（单数）：文档转换是一发式接口，一次只收一份文件。
    """
    return [
        (
            field,
            (
                name,
                io.BytesIO(data),
                _OFFICE_MIMES.get(name.rsplit(".", 1)[-1].lower(), "application/octet-stream"),
            ),
        )
        for name, data in items
    ]


def open_downloaded_pdf(client: TestClient, payload: dict):
    """下载结果并打开成 PDF（调用方负责 close）。

    「转换成功了吗」只能用产物本身回答：接口返回 200 也可能是转出了一张白纸。
    """
    import pymupdf

    response = client.get(payload["download_url"])
    assert response.status_code == 200
    return pymupdf.open(stream=response.content, filetype="pdf")


def download_text(client: TestClient, payload: dict) -> str:
    """下载结果并读出 PDF 正文（已去掉空白，见 :func:`normalize_text`）。"""
    with open_downloaded_pdf(client, payload) as doc:
        return normalize_text("".join(page.get_text() for page in doc))


# ----------------------------------------------------------------------
# 旧格式样张（.doc / .xls / .ppt）
#
# 这些不是手写的，是 scripts/make_office_fixtures.py 用本机 LibreOffice
# 从 flat XML 转出来的**真文件**（OLE2 容器）。跑一次生成，产物进版本库。
# ----------------------------------------------------------------------

FIXTURES_DIR = Path(__file__).parent / "fixtures"

#: 旧格式样张里的标记文字。
#:
#: 这里和 make_office_fixtures.py 各写了一份 —— 看着像重复，但**抄错不会静默通过**：
#: tests/test_office_legacy.py 会去转换出来的 PDF 里找它，两边对不上就是红。
#: 所以别为了「消重」把其中一份改成从另一份推导，那会把校验也一起消掉。
LEGACY_MARKERS = {
    "doc": "LEGACY-DOC-标记文字",
    "xls": "LEGACY-XLS-标记文字",
    "ppt": "LEGACY-PPT-标记文字",
}


def legacy_sample(extension: str) -> Path:
    """旧格式样张的路径。文件不存在时由调用方 skip。"""
    return FIXTURES_DIR / f"sample.{extension}"


def soffice_available() -> bool:
    """本机能不能真的调起 LibreOffice。

    校验层的测试不需要它，凡是要真正转换的测试都挂这个条件，
    免得在没装 LibreOffice 的机器上红成一片。

    这里刻意不 import 转换模块以外的东西，并且吞掉 ImportError：
    ``office_converter`` 是分步实现的，缺了它不该让**其它**测试文件
    整个收集失败（conftest 是全局的，导入期抛异常会牵连全部用例）。
    """
    try:
        from services import office_converter
    except ImportError:
        return False

    return office_converter.find_soffice() is not None


#: 需要真正转换的测试统一挂这个标记
requires_soffice = pytest.mark.skipif(
    not soffice_available(), reason="本机没有 LibreOffice，跳过真实转换测试"
)


def ocr_available() -> bool:
    """本机能不能真的跑 OCR。

    和 :func:`soffice_available` 同一个套路：不 import 引擎、也不构造它
    （那要几百毫秒到一秒），只问可用性探测 —— 探测本身就是 ``find_spec``。
    ``ImportError`` 照样吞掉，conftest 是全局的，导入期抛异常会牵连
    全部用例收集失败。
    """
    try:
        from services import ocr_service
    except ImportError:
        return False

    return ocr_service.is_available()


#: 需要真正跑 OCR 的测试统一挂这个标记
requires_ocr = pytest.mark.skipif(
    not ocr_available(), reason="本机没有 OCR 组件，跳过真实识别测试"
)


def docx_available() -> bool:
    """本机能不能 ``PDF → Word``（要 python-docx）。

    和上面两个同一个套路：只问可用性探测，不 import 引擎的其它部分，
    ``ImportError`` 吞掉。它单独成一条是因为 ``pdf → docx`` 的可用性
    **与 LibreOffice 无关**、也与 OCR 无关（§七 明令 OCR 不可用时这条
    能力必须保留），是个独立的维度。
    """
    try:
        from services import pdf_to_docx
    except ImportError:
        return False

    return pdf_to_docx.docx_available()


#: 需要 ``PDF → Word`` 真能跑起来的测试挂这个标记
requires_docx = pytest.mark.skipif(
    not docx_available(), reason="本机没有 python-docx，跳过 PDF 转 Word 测试"
)


def heif_encode_available() -> bool:
    """本机能不能**写出** HEIC（要带 libx265 的 pillow-heif）。

    ``heif_support()`` 会真编一张 2×2 再读回来（见 ``compressors/heif.py``
    的模块说明），结果在进程内缓存，所以这里反复调用没有代价。
    """
    try:
        from compressors.heif import heif_support
    except ImportError:
        return False

    return heif_support().encode


#: 需要真的编码出 HEIC 的测试挂这个标记。
#: **只在这个方向上挂** —— 只有解码器的构建照样能 ``HEIC → JPG``，
#: 那半边由 ``compressors.heif.heif_support().decode`` 单独回答。
requires_heic_encode = pytest.mark.skipif(
    not heif_encode_available(), reason="本机没有 HEIC 编码器，跳过写出 HEIC 的测试"
)


def full_matrix_available() -> bool:
    """四种可选组件是不是全都在。

    只有齐备时 ``/api/conversion/capabilities`` 的矩阵才恰好等于
    ``registry`` 的全表 —— 少任何一样，响应都会**正确地**少几行/几格。
    所以「本机应当具备全部转换组件」那类断言必须挂在这个条件上，
    否则它测的是「这台机器装了什么」，不是「代码对不对」。
    """
    if not (soffice_available() and docx_available() and heif_encode_available()):
        return False

    try:
        from compressors.heif import heif_support
    except ImportError:
        return False

    return heif_support().decode


def encodable_formats() -> tuple[str, ...]:
    """本机**真能写出来**的目标格式，按 ``OUTPUT_FORMATS`` 的原顺序。

    ``OUTPUT_FORMATS`` 回答的是「编码器认识它们」；这里面唯一可能有中间态
    的是 ``heif``（要 ``pillow-heif`` 带 HEVC 编码器）。缺席时把它从遍历里
    去掉，**而不是跳过整条用例** —— 其余七种格式的回归一条都不能少，
    而「表里有 heif」这件事由 ``test_output_formats_cover_the_whole_registry_vocabulary``
    单独钉着，不会因为这里过滤了就没人管。
    """
    from compressors.encoder import OUTPUT_FORMATS

    if heif_encode_available():
        return OUTPUT_FORMATS

    return tuple(fmt for fmt in OUTPUT_FORMATS if fmt != "heif")


def normalize_ocr_text(text: str) -> str:
    """比对 OCR 结果前先归一化。

    OCR 会在中英文之间插空格、把半角标点认成全角、把数字和字母的间距
    认歪。这些都不代表「认错了字」，所以比对前统一走 NFKC 再抹掉空白。
    不这么做的话，断言会因为一堆与正确性无关的差异变红。
    """
    import unicodedata

    return "".join(unicodedata.normalize("NFKC", text).split())


# ----------------------------------------------------------------------
# PDF → Word（第六阶段 A）
# ----------------------------------------------------------------------


#: 样张里「一行 = 一段」时用的行距（磅）。
#:
#: **不能直接用紧凑的单倍行距。** 每一行都是一段的话，行与行之间必须有
#: 一段能看出来的空白，抽取器才能把它们判成不同的段落 —— 行距均匀的
#: 一排文字，到底是「三段」还是「一段自动换行成三行」，纸面上是没有
#: 区别的（见 :func:`build_wrapped_pdf` 的反例）。真实的合同、清单、
#: 表单也都留了这个空档，所以这里照着真实文档排。
PARAGRAPH_PITCH = 30.0

#: 文字页判定阈值是 16 个字符（``PDF_TO_WORD_MIN_TEXT_CHARS``），
#: 少于这个数会被当成扫描页送去 OCR。测试样张里那些短标题必须配上一条
#: 够长的正文，否则测的就不是「文字版 PDF 能不能转」，而是「这页被判成什么」。
#: 它同时也把正文字号钉在 12 磅上，标题级别才不会因为样本太少而抖。
TEXT_PDF_FILLER = "本页正文内容用于测试，长度足以让这一页被判定为文字页。"


def build_text_pdf(
    *pages: "list[str] | tuple[str, ...]", filler: str = TEXT_PDF_FILLER
) -> bytes:
    """按「每页一组文字行」生成一份**有文字层**的 PDF。

    每个参数是一页，每个元素是页内的一行，**每行自成一段**。
    第一行按标题大小排（20 磅），其余按正文大小（12 磅）——
    标题级别、字号还原这些都要靠它来验。行距见 :data:`PARAGRAPH_PITCH`。

    用 PyMuPDF 的内置中文字体（``china-s``）而不是默认的西文字体：
    中文和英文都得能写进去，否则测的是「字体里有没有这个字」，
    而不是「能不能把文字抽出来」。

    ``filler`` 会自动补在每页末尾，见 :data:`TEXT_PDF_FILLER` 的说明；
    不需要时传空字符串。
    """
    import pymupdf

    if not pages:
        raise ValueError("至少要给一页内容")

    doc = pymupdf.open()
    try:
        for lines in pages:
            page = doc.new_page()
            content = list(lines)
            if filler:
                content.append(filler)
            y = 90.0
            for index, text in enumerate(content):
                size = 20.0 if index == 0 else 12.0
                page.insert_text((72, y), text, fontname="china-s", fontsize=size)
                y += PARAGRAPH_PITCH
        return doc.tobytes(deflate=True, garbage=4, clean=True)
    finally:
        doc.close()


def build_wrapped_pdf(
    paragraph: str,
    *,
    fontsize: float = 12.0,
    leading: float = 17.7,
    fontname: str = "helv",
    width: float = 451.0,
) -> bytes:
    """一段**自动换行**的正文：单倍行距、每行都排满。

    这是 :data:`PARAGRAPH_PITCH` 的反例，用来钉住另一半行为：
    行距紧凑、上一行排到右边界的连续文字是**一段**，不是每行一段。
    排成一行一段的话，用户打开 Word 会看到一首诗，改都没法改。

    行距用 ``insert_textbox`` 的默认单倍行距（12 磅字约 17.7 磅），
    刻意不去动它 —— 真实正文就是这个行距。
    """
    import pymupdf

    doc = pymupdf.open()
    try:
        page = doc.new_page()
        rect = pymupdf.Rect(72, 90, 72 + width, 90 + leading * 20)
        leftover = page.insert_textbox(
            rect, paragraph, fontsize=fontsize, fontname=fontname
        )
        if leftover < 0:
            raise ValueError("样张没排下，段落要短一点")
        return doc.tobytes(deflate=True, garbage=4, clean=True)
    finally:
        doc.close()


#: 英文样张用的字体。**必须用 base-14 的 Helvetica，不能沿用 ``china-s``**：
#: 那是个 CJK 字体，里面的拉丁字母也按全角前进 —— 一行 44 个字符在 12 磅下
#: 要占 528 磅，从 x=72 排出去直接顶过页面右边界（595），被 PyMuPDF 截掉半句。
#: 那样测的是「样张有没有被裁掉」，而不是「英文能不能抽出来」。
LATIN_FONT = "helv"
ENGLISH_TITLE = "Quarterly Report"
ENGLISH_FILLER = "The revenue grew by 18 percent compared with the same period."


def build_english_pdf(
    *pages: "list[str] | tuple[str, ...]", filler: str = ENGLISH_FILLER
) -> bytes:
    """按「每页一组文字行」生成一份**英文**有文字层的 PDF。

    和 :func:`build_text_pdf` 同一套排版规则（首行 20 磅标题、其余 12 磅正文），
    只是换成拉丁字体与拉丁字宽。
    """
    import pymupdf

    if not pages:
        raise ValueError("至少要给一页内容")

    doc = pymupdf.open()
    try:
        for lines in pages:
            page = doc.new_page()
            content = list(lines)
            if filler:
                content.append(filler)
            y = 90.0
            for index, text in enumerate(content):
                size = 20.0 if index == 0 else 12.0
                page.insert_text((72, y), text, fontname=LATIN_FONT, fontsize=size)
                y += PARAGRAPH_PITCH
        return doc.tobytes(deflate=True, garbage=4, clean=True)
    finally:
        doc.close()


def build_inline_pdf(
    segments: "list[tuple[str, str]]",
    *,
    fontsize: float = 12.0,
    x0: float = 72.0,
    y: float = 120.0,
) -> bytes:
    """把若干「文字 + 字体名」**排在同一行**上，生成一页 PDF。

    位置是用 ``get_text_length`` 按真实字宽累加出来的，不是估的：
    间距算错的话，同一行会被判成两栏或者被断成两个段落，
    测出来的就成了「间距算得准不准」而不是「行内混排样式保不保得住」。

    ``fontname`` 用 base-14 的 ``helv`` / ``hebo`` / ``heit`` / ``hebi``
    分别表示普通 / 粗体 / 斜体 / 粗斜体。
    """
    import pymupdf

    if not segments:
        raise ValueError("至少要给一段文字")

    doc = pymupdf.open()
    try:
        page = doc.new_page()
        x = x0
        for text, fontname in segments:
            page.insert_text((x, y), text, fontname=fontname, fontsize=fontsize)
            x += pymupdf.get_text_length(text, fontname=fontname, fontsize=fontsize)
        return doc.tobytes(deflate=True, garbage=4, clean=True)
    finally:
        doc.close()


def build_scanned_pdf(labels: "list[str]", *, dpi: int = 200) -> bytes:
    """生成一份**没有文字层**的 PDF：整页就是一张画着大字的位图。

    这是扫描件的模型：``page.get_text()`` 拿回来是空字符串，
    只能靠 OCR。用真实中文字体渲染，别用 PIL 的默认位图字体
    （那个画中文只会画成一片方框，测出来的是「字体不对」而不是「OCR 不行」）。
    """
    import pymupdf

    font = ImageFont.truetype(str(scan_font_path()), 72)
    doc = pymupdf.open()
    try:
        for label in labels:
            probe = ImageDraw.Draw(Image.new("RGB", (10, 10)))
            box = probe.textbbox((0, 0), label, font=font)
            canvas = Image.new(
                "RGB", (box[2] - box[0] + 160, box[3] - box[1] + 160), "white"
            )
            ImageDraw.Draw(canvas).text(
                (80 - box[0], 80 - box[1]), label, font=font, fill="black"
            )
            stream = io.BytesIO()
            canvas.save(stream, format="PNG")

            page = doc.new_page()
            page.insert_image(page.rect, stream=stream.getvalue())
        return doc.tobytes(deflate=True, garbage=4, clean=True)
    finally:
        doc.close()


def build_mixed_pdf(specs: "list[tuple[str, object]]") -> bytes:
    """按顺序拼一份「有的页是文字、有的页是图」的 PDF。

    ``specs`` 里每一项是 ``("text", [行, ...])`` 或 ``("scan", "图上写的大字")``。
    真实的 PDF 大量是这种形态：封面、盖章页、扫描附件是图，正文是字。
    整份判成扫描件会把本来清清楚楚的正文也送去 OCR，所以逐页判定必须能测。
    """
    import pymupdf

    if not specs:
        raise ValueError("至少要给一页")

    font = ImageFont.truetype(str(scan_font_path()), 72)
    doc = pymupdf.open()
    try:
        for kind, payload in specs:
            page = doc.new_page()
            if kind == "scan":
                label = str(payload)
                probe = ImageDraw.Draw(Image.new("RGB", (10, 10)))
                box = probe.textbbox((0, 0), label, font=font)
                canvas = Image.new(
                    "RGB", (box[2] - box[0] + 160, box[3] - box[1] + 160), "white"
                )
                ImageDraw.Draw(canvas).text(
                    (80 - box[0], 80 - box[1]), label, font=font, fill="black"
                )
                stream = io.BytesIO()
                canvas.save(stream, format="PNG")
                # 只插图、不插文字：这一页因此没有文字层
                page.insert_image(page.rect, stream=stream.getvalue())
            elif kind == "text":
                y = 90.0
                for index, text in enumerate(list(payload)):  # type: ignore[arg-type]
                    size = 20.0 if index == 0 else 12.0
                    page.insert_text((72, y), text, fontname="china-s", fontsize=size)
                    y += PARAGRAPH_PITCH
            else:
                raise ValueError(f"不认识的页类型：{kind!r}")
        return doc.tobytes(deflate=True, garbage=4, clean=True)
    finally:
        doc.close()


def build_empty_pdf() -> bytes:
    """一份**一页都没有**的 PDF。

    手写字节而不是用 PyMuPDF 造：它能打开 0 页的文档，却**存不出** 0 页的文档
    （``ValueError: cannot save with zero pages``）。真实的空 PDF 就是这么来的 ——
    导出工具出错时留下的一个没有 Kids 的页面树。
    """
    return (
        b"%PDF-1.4\n"
        b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Kids[]/Count 0>>endobj\n"
        b"trailer<</Root 1 0 R>>\n"
        b"%%EOF\n"
    )


def scan_font_path() -> Path:
    """本机的中文字体。找不到就跳过相关测试（而不是画出一堆方框）。"""
    for candidate in (
        Path("C:/Windows/Fonts/simsun.ttc"),
        Path("C:/Windows/Fonts/simhei.ttf"),
        Path("C:/Windows/Fonts/msyh.ttc"),
    ):
        if candidate.is_file():
            return candidate
    pytest.skip("本机没有中文字体，无法构造扫描件样张")


#: DOCX 里必须存在的条目。缺任何一个，Word 都会直接拒绝打开。
DOCX_REQUIRED_PARTS = ("[Content_Types].xml", "_rels/.rels", "word/document.xml")


def download_docx(client: TestClient, payload: dict) -> bytes:
    """按响应里的 download_url 把 DOCX 取回来。"""
    response = client.get(payload["download_url"])
    assert response.status_code == 200
    return response.content


def docx_pages(data: bytes) -> list[str]:
    """按分页符把 DOCX 切成「页」，返回每页的文字。

    **只用标准库**，刻意不 import python-docx：这一层要抓的恰恰是
    「python-docx 自己读得回来、Word 却读不回来」那类故障
    （比如关系表写错、document.xml 是半截）。用同一个库去验自己，
    等于让被告当法官。

    DOCX 里没有「页」这个东西，分页符是页序唯一的真实表示，
    所以页序断言也只能建在它上面。
    """
    import re
    import xml.etree.ElementTree as ET
    import zipfile

    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        assert archive.testzip() is None, "DOCX 压缩包本身是坏的"
        names = set(archive.namelist())
        for part in DOCX_REQUIRED_PARTS:
            assert part in names, f"DOCX 缺少必需条目 {part}"
        xml = archive.read("word/document.xml").decode("utf-8")

    # 解析一遍：能解析才说明不是半截文件
    ET.fromstring(xml)

    segments = re.split(r'<w:br w:type="page"/>', xml)
    return [
        "".join(unescape(chunk) for chunk in re.findall(r"<w:t[^>]*>([^<]*)</w:t>", segment))
        for segment in segments
    ]


def docx_media(data: bytes) -> list[str]:
    """DOCX 里内嵌的图片条目名，按压缩包里的顺序。

    「图片不丢失」是这一阶段的硬要求之一，而图片有没有真的进去只能从
    压缩包看 —— 段落里看不见它，接口返回 200 更说明不了什么。
    """
    import zipfile

    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return [name for name in archive.namelist() if name.startswith("word/media/")]


def docx_runs(data: bytes):
    """用 python-docx 回读，返回 [(段落文字, 样式名, [(run 文字, 粗体, 斜体, 字号磅)])]。

    ``None`` 表示「没显式设过」——继承样式，这在 DOCX 里是正常的表达方式。
    """
    from docx import Document

    document = Document(io.BytesIO(data))
    result = []
    for paragraph in document.paragraphs:
        result.append(
            (
                paragraph.text,
                paragraph.style.name,
                [
                    (
                        run.text,
                        run.bold,
                        run.italic,
                        run.font.size.pt if run.font.size else None,
                    )
                    for run in paragraph.runs
                ],
            )
        )
    return result


def pdf_to_word_files(name: str, data: bytes) -> list[tuple[str, tuple]]:
    """PDF → Word 接口的表单文件参数。

    传 ``BytesIO`` 而不是裸 ``bytes``，和 :func:`office_files` 保持一致：
    裸 bytes 配三元组时 httpx 不会把它编成文件部件，接口那边会收到一个
    名叫 ``file`` 的普通表单字段，然后以「参数不完整」400 掉 ——
    报错离原因很远，排查起来很费劲。
    """
    return [("file", (name, io.BytesIO(data), "application/pdf"))]


def pdf_lines(data: bytes) -> list[str]:
    """源 PDF 里每一行的文字，按页序拼起来。"""
    import pymupdf

    doc = pymupdf.open(stream=data, filetype="pdf")
    try:
        return [line for page in doc for line in page.get_text().splitlines()]
    finally:
        doc.close()


def squash(text: str) -> str:
    """去掉所有空白。

    比对中文时用它：DOCX 会在段落之间插换行、在不同样式的 run 之间插空格，
    这些都不算「丢了字」。去掉空白之后源文件必须是产物的一段子串 ——
    这样任何一个字消失都会被抓到，而排版差异不会误报。
    """
    return "".join(text.split())


def collapse(text: str) -> str:
    """把连续空白压成一个空格。

    比对英文时用它：单词之间必须有空格，但换行位置不保证一致。
    """
    return " ".join(text.split())
