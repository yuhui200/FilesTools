"""SVG 走完整 HTTP 链路（第十阶段 A §九–§十五、§六十四–§六十八）。

``tests/test_svg_security.py`` 验的是净化器**自己**的判据。这里验的是
**另一件事**：那些判据在真实的上传-校验-排队-转换-下载链路上真的生效，
而且产物是真的能打开的图片 / PDF，不是「HTTP 200 就算过」。

三条主线：

    1. §九 字面：SVG → PNG / JPG / WEBP / PDF 四个目标都能出真东西；
    2. §二 的落点：SVG 渲染出来的是一张普通位图，所以几何选项
       （裁剪 / 缩放 / 旋转 / 翻转）**一行都不用改**就适用于它 —— 这里逐条验；
    3. §九–§十五 的边界：``file://`` 引用够不着本地文件，产物里
       **一个像素**都不会多出来。
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pymupdf
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from compressors import svg as svg_codec
from tests.conftest import conversion_task, image_files, run_conversion

SVG_NAMESPACE = "http://www.w3.org/2000/svg"

#: 两份颜色在半张图处对切 —— 方向错了一眼就能看出来（与几何测试同一手法）。
LEFT_RGB = (255, 0, 0)
RIGHT_RGB = (0, 0, 255)


def two_tone_svg(width: int = 120, height: int = 60, label: str = "Hi") -> bytes:
    half = width // 2
    return (
        f'<svg xmlns="{SVG_NAMESPACE}" width="{width}" height="{height}">'
        f'<rect x="0" y="0" width="{half}" height="{height}" fill="#ff0000"/>'
        f'<rect x="{half}" y="0" width="{width - half}" height="{height}" fill="#0000ff"/>'
        f'<text x="6" y="24" font-size="16" fill="#ffffff">{label}</text>'
        f"</svg>"
    ).encode("utf-8")


def _submit(client: TestClient, target: str, svg: bytes, *, name: str = "a.svg", **options):
    data = {}
    if options:
        data["options"] = json.dumps(options)
    return run_conversion(
        client, files=image_files((name, svg)), target_type=target, **data
    )


def _download(client: TestClient, snapshot: dict, index: int = 0) -> bytes:
    task = conversion_task(snapshot, index)
    assert task["status"] == "completed", task
    response = client.get(task["result"]["download_url"])
    assert response.status_code == 200, response.text
    return response.content


def _convert(client: TestClient, target: str, svg: bytes, **options) -> Image.Image:
    return Image.open(io.BytesIO(_download(client, _submit(client, target, svg, **options))))


def _notes(snapshot: dict, index: int = 0) -> list[str]:
    task = conversion_task(snapshot, index)
    assert task["status"] == "completed", task
    return task["result"].get("notes") or []


def _near(actual: tuple[int, int, int], expected: tuple[int, int, int], tolerance: int) -> bool:
    return all(abs(a - b) <= tolerance for a, b in zip(actual, expected))


# ----------------------------------------------------------------------
# 1. 能力矩阵：§九 字面的四个目标
# ----------------------------------------------------------------------

def test_capabilities_offer_exactly_the_four_svg_targets(client: TestClient) -> None:
    payload = client.get("/api/conversion/capabilities").json()
    assert payload["matrix"]["svg"] == ["jpg", "png", "webp", "pdf"]

    conversions = [item for item in payload["conversions"] if item["source_type"] == "svg"]
    assert {item["target_type"] for item in conversions} == {"png", "jpg", "webp", "pdf"}
    assert all(item["available"] for item in conversions)
    assert all(item["group"] == "image" for item in conversions)


def test_svg_raster_targets_share_the_image_option_schema(client: TestClient) -> None:
    """§二 在 API 层的证据：SVG→位图**不是**一套单独的选项集。

    它跟 JPG→PNG 拿到的是同一份 schema —— 所以裁剪/旋转/缩放那些控件
    对 SVG 自动可用，前端一行分支都不用加。
    """
    payload = client.get("/api/conversion/capabilities").json()
    by_id = {item["id"]: item for item in payload["conversions"]}

    def keys(capability_id: str) -> set[str]:
        schema = by_id[capability_id]["options_schema"]
        assert schema is not None
        return {item["key"] for item in schema["items"]}

    svg_keys = keys("image.svg-to-png")
    assert svg_keys == keys("image.jpg-to-png")
    assert {"crop.x", "rotation", "flip", "resize.mode"} <= svg_keys


def test_svg_to_pdf_offers_no_options(client: TestClient) -> None:
    """矢量导出没有可调参数 —— 与其发一个点了没反应的控件，不如什么都不发。"""
    payload = client.get("/api/conversion/capabilities").json()
    entry = next(item for item in payload["conversions"] if item["id"] == "image.svg-to-pdf")
    assert entry["options_schema"] is None


# ----------------------------------------------------------------------
# 2. 四个目标：真打开产物看
# ----------------------------------------------------------------------

def test_svg_to_png_is_a_real_png_at_the_declared_size(client: TestClient) -> None:
    image = _convert(client, "png", two_tone_svg())
    assert image.format == "PNG"
    assert image.size == (120, 60)


def test_svg_to_png_puts_the_pixels_where_the_svg_says(client: TestClient) -> None:
    """「文件存在」不算验过 —— 左右两半的颜色必须真的对得上。"""
    image = _convert(client, "png", two_tone_svg()).convert("RGB")
    assert _near(image.getpixel((10, 30)), LEFT_RGB, 2)
    assert _near(image.getpixel((110, 30)), RIGHT_RGB, 2)


@pytest.mark.parametrize(("target", "expected_format"), [("jpg", "JPEG"), ("webp", "WEBP")])
def test_lossy_targets_are_their_own_format(
    client: TestClient, target: str, expected_format: str
) -> None:
    image = _convert(client, target, two_tone_svg())
    assert image.format == expected_format
    assert image.size == (120, 60)

    rgb = image.convert("RGB")
    # 有损压缩，给一点余量；但「左边红右边蓝」这个事实不该被压没了
    assert _near(rgb.getpixel((10, 30)), LEFT_RGB, 40)
    assert _near(rgb.getpixel((110, 30)), RIGHT_RGB, 40)


def test_svg_to_pdf_stays_vector(client: TestClient) -> None:
    """§九 的 PDF 目标必须是**真矢量**，不是把位图贴进 PDF。

    三条判据缺一不可：文字能被抽出来、几何能被读成绘图指令、页面里没有图像对象。
    """
    data = _download(client, _submit(client, "pdf", two_tone_svg()))

    document = pymupdf.open(stream=data, filetype="pdf")
    try:
        assert document.page_count == 1
        page = document[0]
        assert (round(page.rect.width), round(page.rect.height)) == (120, 60)
        assert "Hi" in page.get_text()
        assert page.get_drawings(), "PDF 里没有任何矢量绘图指令"
        assert page.get_images() == [], "PDF 里混进了位图"
    finally:
        document.close()


def test_the_download_filename_carries_the_target_extension(client: TestClient) -> None:
    """``.svg`` 的源文件转成 PDF 后，下载名不能还叫 ``.svg``。"""
    snapshot = _submit(client, "pdf", two_tone_svg())
    task = conversion_task(snapshot, 0)
    assert task["result"]["filename"].endswith(".pdf")


# ----------------------------------------------------------------------
# 3. 净化结果如实到达用户
# ----------------------------------------------------------------------

def test_a_stripped_local_reference_reaches_neither_pixels_nor_notes(
    client: TestClient, tmp_path: Path
) -> None:
    """端到端版的「够不着本地文件」：一份品红色的本地图片被 ``file://`` 引用。

    产物里出现品红像素就说明服务器真的把文件读进去画了。
    """
    secret = tmp_path / "secret.png"
    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), (255, 0, 255)).save(buffer, format="PNG")
    secret.write_bytes(buffer.getvalue())

    svg = (
        f'<svg xmlns="{SVG_NAMESPACE}" width="80" height="80">'
        f'<rect width="80" height="80" fill="#ffffff"/>'
        f'<image x="0" y="0" width="80" height="80" href="{secret.as_uri()}"/>'
        f"</svg>"
    ).encode("utf-8")

    snapshot = _submit(client, "png", svg)
    image = Image.open(io.BytesIO(_download(client, snapshot))).convert("RGB")

    colors = {color for _, color in (image.getcolors(maxcolors=1 << 20) or [])}
    assert (255, 0, 255) not in colors, "本地文件被读进产物里了"
    assert svg_codec.EXTERNAL_NOTE in _notes(snapshot)


def test_a_script_tag_is_removed_and_reported(client: TestClient) -> None:
    svg = (
        f'<svg xmlns="{SVG_NAMESPACE}" width="40" height="40">'
        f'<script>alert("xss")</script>'
        f'<rect width="40" height="40" fill="#00ff00"/>'
        f"</svg>"
    ).encode("utf-8")

    snapshot = _submit(client, "png", svg)
    image = Image.open(io.BytesIO(_download(client, snapshot))).convert("RGB")

    assert svg_codec.SOCKET_NOTE in _notes(snapshot)
    # 脚本没了，图还在 —— 净化不等于毁掉文件
    assert _near(image.getpixel((20, 20)), (0, 255, 0), 2)


def test_a_clean_svg_reports_no_notes(client: TestClient) -> None:
    """没动过就说没动过。见谁都报一串「已移除」会让用户以为文件被削过。"""
    assert _notes(_submit(client, "png", two_tone_svg())) == []


def test_a_svg_without_a_declared_size_says_so(client: TestClient) -> None:
    """渲染器兜底的 612×792 是它自己编的 —— 用了就必须如实说。"""
    svg = f'<svg xmlns="{SVG_NAMESPACE}"><rect width="10" height="10"/></svg>'.encode("utf-8")
    snapshot = _submit(client, "png", svg)
    image = Image.open(io.BytesIO(_download(client, snapshot)))

    assert image.size == (612, 792)
    assert any("612" in note and "792" in note for note in _notes(snapshot))


# ----------------------------------------------------------------------
# 4. §二 的落点：几何选项对 SVG 一样有效
# ----------------------------------------------------------------------

def test_resize_applies_to_svg(client: TestClient) -> None:
    image = _convert(client, "png", two_tone_svg(), **{"resize.mode": "percent", "resize.percent": 50})
    assert image.size == (60, 30)


def test_crop_applies_to_svg(client: TestClient) -> None:
    """只留左半张 —— 结果应当整张都是红的。"""
    image = _convert(
        client, "png", two_tone_svg(),
        **{"crop.x": 0, "crop.y": 0, "crop.width": 60, "crop.height": 60},
    ).convert("RGB")
    assert image.size == (60, 60)
    assert _near(image.getpixel((5, 5)), LEFT_RGB, 2)
    assert _near(image.getpixel((55, 55)), LEFT_RGB, 2)


def test_rotation_applies_to_svg(client: TestClient) -> None:
    image = _convert(client, "png", two_tone_svg(), rotation="90")
    assert image.size == (60, 120)


def test_flip_really_mirrors_an_svg(client: TestClient) -> None:
    """翻转之后左边必须变成蓝的 —— 尺寸不变，所以只看尺寸是验不出来的。"""
    image = _convert(client, "png", two_tone_svg(), flip="horizontal").convert("RGB")
    assert image.size == (120, 60)
    assert _near(image.getpixel((10, 30)), RIGHT_RGB, 2)
    assert _near(image.getpixel((110, 30)), LEFT_RGB, 2)


def test_svg_never_takes_the_passthrough_path(client: TestClient) -> None:
    """§二十五/§二十六：快速返回路径的前提是「什么都没变」。

    SVG 的输出格式永远是**跨格式**的（``svg`` 不是任何一种输出格式），
    所以它结构上就不可能走那条路。这里把「没走」验成事实：
    产物是 PNG 而不是原样的 SVG 字节，且尺寸确实是 SVG 声明的那张。
    """
    source = two_tone_svg()
    data = _download(client, _submit(client, "png", source))

    assert data != source
    assert data.startswith(b"\x89PNG\r\n\x1a\n")
    assert Image.open(io.BytesIO(data)).size == (120, 60)


# ----------------------------------------------------------------------
# 5. 边界：判定阶段就出局，不占 worker，也不带 traceback
#
# 这里**不**断言 4xx。第九阶段定下的批量语义是：**一个坏文件不该拖垮一整批** ——
# 50 个文件里有一个是伪装成 SVG 的网页，用户要的是另外 49 个转好，
# 而不是整批 400。所以逐文件的类型判定失败表现为**该项失败**，
# 错误码与中文说明都在，且 ``can_retry=false``（内容不对，重试一百次也一样）。
# 判定发生在进队列**之前**（``tasks/conversion_tasks.py``），所以它同样不占 worker。
# 批次级别的错误（目标格式不认识、选项越界）才是 4xx。
# ----------------------------------------------------------------------

def _failed_task(client: TestClient, name: str, data: bytes) -> dict:
    snapshot = run_conversion(client, files=image_files((name, data)), target_type="png")
    task = conversion_task(snapshot, 0)
    assert task["status"] == "failed", task
    return task


@pytest.mark.parametrize(
    ("document", "code"),
    [
        (b"<!DOCTYPE html><html><body><p>hi</p></body></html>", "INVALID_REQUEST"),
        (b"<html><body><p>hi</p></body></html>", "INVALID_FILE_TYPE"),
        (b"<root><svg/></root>", "INVALID_FILE_TYPE"),
        (b'<svg width="10"><rect></svg>', "CORRUPTED_FILE"),
    ],
)
def test_html_renamed_to_svg_is_rejected(
    client: TestClient, document: bytes, code: str
) -> None:
    """§五十七：靠**内容**判定类型，不看扩展名。

    一份 HTML 改名成 ``.svg``，渲染器会照收不误（实测），所以「渲染器打得开」
    从来不是判据 —— 根元素检查才是。
    """
    task = _failed_task(client, "fake.svg", document)

    assert task["error_code"] == code
    assert task["error_message"]
    assert "Traceback" not in task["error_message"]
    assert task["can_retry"] is False
    assert task["result"] is None


def test_a_rejected_file_does_not_take_its_batch_down(client: TestClient) -> None:
    """一条坏的 + 一条好的：好的必须照常转出来。"""
    snapshot = run_conversion(
        client,
        files=image_files(
            ("fake.svg", b"<html><body><p>hi</p></body></html>"),
            ("good.svg", two_tone_svg()),
        ),
        target_type="png",
    )
    assert conversion_task(snapshot, 0)["status"] == "failed"
    assert conversion_task(snapshot, 1)["status"] == "completed"

    image = Image.open(io.BytesIO(_download(client, snapshot, 1)))
    assert image.size == (120, 60)


def test_an_svg_over_the_byte_limit_is_rejected(client: TestClient) -> None:
    from config import settings

    huge = (
        f'<svg xmlns="{SVG_NAMESPACE}" width="10" height="10"><!--'
        + "x" * (settings.MAX_SVG_BYTES + 4096)
        + "--></svg>"
    ).encode("utf-8")

    task = _failed_task(client, "huge.svg", huge)
    assert str(settings.MAX_SVG_BYTES // 1024) in task["error_message"]
    assert "Traceback" not in task["error_message"]


def test_an_oversized_svg_is_downscaled_and_says_so(client: TestClient) -> None:
    """位图那条路对超大图是缩放而不是拒绝，SVG 的栅格化跟着保持一致。"""
    svg = two_tone_svg(width=20000, height=100).decode()
    snapshot = _submit(client, "png", svg.encode("utf-8"))

    image = Image.open(io.BytesIO(_download(client, snapshot)))
    assert image.size == (12000, 60)
    assert any("12000" in note for note in _notes(snapshot))


def test_an_oversized_svg_cannot_be_exported_to_pdf(client: TestClient) -> None:
    """矢量没法「缩小到上限以内」，所以这条路上只能是拒绝 —— 但要拒绝得干净。"""
    svg = two_tone_svg(width=20000, height=100).decode()
    task = conversion_task(_submit(client, "pdf", svg.encode("utf-8")), 0)

    assert task["status"] == "failed"
    message = task.get("error_message") or ""
    assert "12000" in message
    assert "Traceback" not in message
