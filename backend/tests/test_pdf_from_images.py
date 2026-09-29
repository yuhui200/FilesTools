"""图片 → PDF 的端到端测试。

覆盖第三阶段 §17 要求的前三项：单张图片转 PDF、多张图片转 PDF、
JPG / PNG 分别转 PDF，以及排版参数与非法输入的处理。
"""

from __future__ import annotations

import io

import pymupdf
import pytest
from fastapi.testclient import TestClient

from tests.conftest import build_image_bytes, image_files

ENDPOINT = "/api/pdf/from-images"

# 允许 1 pt 的误差：自定义页面尺寸要经过毫米换算
TOLERANCE = 1.0

#: 页边距「中」的期望值。第九阶段 §十 把档位改成整毫米，所以这里按
#: **10 毫米**算，而不是去读 ``settings.PDF_MARGINS``：读配置的话，
#: 配置本身被改错（比如写成 12.7）测试也照样绿 —— 那就白测了。
MARGIN_MEDIUM_PT = 10 * 72 / 25.4


def post_images(client: TestClient, files, **options: str):
    return client.post(ENDPOINT, files=files, data=options)


def fetch_pdf(client: TestClient, url: str) -> pymupdf.Document:
    response = client.get(url)
    assert response.status_code == 200, response.text
    assert response.content.startswith(b"%PDF-")
    return pymupdf.open("pdf", response.content)


def page_sizes(doc: pymupdf.Document) -> list[tuple[float, float]]:
    return [(page.rect.width, page.rect.height) for page in doc]


# ----------------------------------------------------------------------
# 单张 / 多张
# ----------------------------------------------------------------------

def test_single_jpeg_becomes_one_page_pdf(client: TestClient) -> None:
    """单张 JPG 转成只有一页的 PDF，直接下载不打包。"""
    source = build_image_bytes(800, 600, "JPEG")
    response = post_images(client, image_files(("photo.jpg", source)))
    assert response.status_code == 200, response.text

    body = response.json()
    assert body["archived"] is False
    assert body["page_count"] == 1
    assert body["filename"] == "photo.pdf"
    assert body["files"][0]["page_count"] == 1

    with fetch_pdf(client, body["download_url"]) as doc:
        assert doc.page_count == 1
        assert len(doc[0].get_images(full=True)) == 1


def test_single_png_becomes_one_page_pdf(client: TestClient) -> None:
    """单张 PNG 同样能转成 PDF。"""
    source = build_image_bytes(600, 900, "PNG")
    response = post_images(client, image_files(("图 表.png", source)))
    assert response.status_code == 200, response.text

    body = response.json()
    assert body["filename"] == "图 表.pdf"
    with fetch_pdf(client, body["download_url"]) as doc:
        assert doc.page_count == 1
        assert len(doc[0].get_images(full=True)) == 1


def test_multiple_images_keep_upload_order(client: TestClient) -> None:
    """多张图片：一张一页，页序与上传顺序一致。"""
    files = image_files(
        ("1.jpg", build_image_bytes(400, 300, "JPEG")),
        ("2.jpg", build_image_bytes(300, 400, "JPEG")),
        ("3.png", build_image_bytes(500, 500, "PNG")),
    )
    response = post_images(client, files, page_size="a4", orientation="portrait")
    assert response.status_code == 200, response.text

    body = response.json()
    assert body["page_count"] == 3
    assert body["filename"] == "images.pdf"
    assert [item["filename"] for item in body["files"]] == ["images.pdf"]
    assert body["original_size"] > 0

    with fetch_pdf(client, body["download_url"]) as doc:
        assert doc.page_count == 3
        # 每页各有一张图，顺序即上传顺序
        for index in range(3):
            assert len(doc[index].get_images(full=True)) == 1


def test_upload_order_is_respected(client: TestClient) -> None:
    """页面顺序必须严格按上传顺序，而不是按大小或名字。"""
    tall = build_image_bytes(200, 800, "JPEG")
    wide = build_image_bytes(800, 200, "JPEG")
    response = post_images(
        client,
        image_files(("a.jpg", tall), ("b.jpg", wide)),
        page_size="auto",
    )
    assert response.status_code == 200, response.text

    with fetch_pdf(client, response.json()["download_url"]) as doc:
        first, second = page_sizes(doc)
        assert first[1] > first[0], "第一页应该是竖图"
        assert second[0] > second[1], "第二页应该是横图"


# ----------------------------------------------------------------------
# 页面大小与方向
# ----------------------------------------------------------------------

@pytest.mark.parametrize(
    ("size", "expected"),
    [("a4", (595.276, 841.890)), ("a5", (419.528, 595.276)), ("letter", (612.0, 792.0))],
)
def test_fixed_page_sizes(client: TestClient, size: str, expected: tuple[float, float]) -> None:
    """A4 / A5 / Letter 的页面尺寸要精确匹配。"""
    response = post_images(
        client,
        image_files(("p.jpg", build_image_bytes(400, 400, "JPEG"))),
        page_size=size,
        orientation="portrait",
    )
    assert response.status_code == 200, response.text

    with fetch_pdf(client, response.json()["download_url"]) as doc:
        width, height = page_sizes(doc)[0]
        assert width == pytest.approx(expected[0], abs=TOLERANCE)
        assert height == pytest.approx(expected[1], abs=TOLERANCE)


def test_auto_page_size_matches_image(client: TestClient) -> None:
    """页面大小选自动时，页面尺寸等于图片尺寸（不含边距）。"""
    response = post_images(
        client,
        image_files(("p.jpg", build_image_bytes(640, 480, "JPEG"))),
        page_size="auto",
    )
    assert response.status_code == 200, response.text

    with fetch_pdf(client, response.json()["download_url"]) as doc:
        assert page_sizes(doc)[0] == pytest.approx((640.0, 480.0), abs=TOLERANCE)


def test_auto_orientation_follows_each_image(client: TestClient) -> None:
    """方向选自动时，每页各自按图片的长宽比决定横竖。"""
    files = image_files(
        ("横.jpg", build_image_bytes(900, 500, "JPEG")),
        ("竖.jpg", build_image_bytes(500, 900, "JPEG")),
    )
    response = post_images(client, files, page_size="a4", orientation="auto")
    assert response.status_code == 200, response.text

    with fetch_pdf(client, response.json()["download_url"]) as doc:
        sizes = page_sizes(doc)
        assert sizes[0][0] > sizes[0][1], "第一页应为横向"
        assert sizes[1][1] > sizes[1][0], "第二页应为纵向"


def test_fixed_orientation_overrides_image(client: TestClient) -> None:
    """显式指定方向时，横图也必须放进纵向页面。"""
    response = post_images(
        client,
        image_files(("横.jpg", build_image_bytes(900, 500, "JPEG"))),
        page_size="a4",
        orientation="portrait",
    )
    assert response.status_code == 200, response.text

    with fetch_pdf(client, response.json()["download_url"]) as doc:
        width, height = page_sizes(doc)[0]
        assert height > width


def test_custom_page_size_in_millimeters(client: TestClient) -> None:
    """自定义尺寸按毫米输入，换算成点后写入 PDF。"""
    response = post_images(
        client,
        image_files(("p.jpg", build_image_bytes(300, 300, "JPEG"))),
        page_size="custom",
        orientation="portrait",
        custom_width_mm="100",
        custom_height_mm="150",
    )
    assert response.status_code == 200, response.text

    with fetch_pdf(client, response.json()["download_url"]) as doc:
        width, height = page_sizes(doc)[0]
        # 100 mm = 283.46 pt，150 mm = 425.20 pt
        assert width == pytest.approx(283.46, abs=TOLERANCE)
        assert height == pytest.approx(425.20, abs=TOLERANCE)


# ----------------------------------------------------------------------
# 边距与适应方式
# ----------------------------------------------------------------------

def test_margin_shrinks_content_area(client: TestClient) -> None:
    """页边距应当体现在图片的实际位置上。"""
    source = build_image_bytes(400, 400, "JPEG")

    def image_rect(margin: str) -> tuple[float, float, float, float]:
        response = post_images(
            client,
            image_files(("p.jpg", source)),
            page_size="a4",
            orientation="portrait",
            margin=margin,
        )
        assert response.status_code == 200, response.text
        with fetch_pdf(client, response.json()["download_url"]) as doc:
            # 页面里唯一一张图片的位置
            bbox = doc[0].get_image_bbox(doc[0].get_images(full=True)[0])
            return (bbox.x0, bbox.y0, bbox.x1, bbox.y1)

    none_rect = image_rect("none")
    large_rect = image_rect("large")

    assert none_rect[0] == pytest.approx(0.0, abs=TOLERANCE)
    # 第九阶段 §十：边距档位改成整毫米，large = 20 毫米 = 56.69 磅
    assert large_rect[0] == pytest.approx(20 * 72 / 25.4, abs=TOLERANCE)
    assert large_rect[2] < none_rect[2], "边距越大，图片越窄"


def test_contain_keeps_image_inside_content_box(client: TestClient) -> None:
    """保持比例：图片完整落在版心内，不会超出页面。"""
    response = post_images(
        client,
        image_files(("p.jpg", build_image_bytes(1200, 300, "JPEG"))),
        page_size="a4",
        orientation="portrait",
        fit="contain",
        margin="medium",
    )
    assert response.status_code == 200, response.text

    with fetch_pdf(client, response.json()["download_url"]) as doc:
        page = doc[0]
        bbox = page.get_image_bbox(page.get_images(full=True)[0])
        assert bbox.x0 >= MARGIN_MEDIUM_PT - TOLERANCE
        assert bbox.y0 >= MARGIN_MEDIUM_PT - TOLERANCE
        assert bbox.x1 <= page.rect.width - MARGIN_MEDIUM_PT + TOLERANCE
        assert bbox.y1 <= page.rect.height - MARGIN_MEDIUM_PT + TOLERANCE
        # 比例保持：宽高比与 1200:300 一致
        assert (bbox.x1 - bbox.x0) / (bbox.y1 - bbox.y0) == pytest.approx(4.0, abs=0.02)


def test_fill_covers_whole_content_box(client: TestClient) -> None:
    """填充页面：图片按比例放大到盖住整个版心，多出来的部分落在页面外被裁掉。"""
    response = post_images(
        client,
        image_files(("p.jpg", build_image_bytes(1200, 300, "JPEG"))),
        page_size="a4",
        orientation="portrait",
        fit="fill",
        margin="medium",
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert any("裁剪" in note for note in body["notes"])

    with fetch_pdf(client, body["download_url"]) as doc:
        page = doc[0]
        bbox = page.get_image_bbox(page.get_images(full=True)[0])
        page_width, page_height = page.rect.width, page.rect.height

    left, top = MARGIN_MEDIUM_PT, MARGIN_MEDIUM_PT
    right, bottom = page_width - MARGIN_MEDIUM_PT, page_height - MARGIN_MEDIUM_PT

    # 完全盖住版心：四条边都不小于版心（超出的部分会被页面裁掉，这是预期行为）
    assert bbox.x0 <= left + TOLERANCE
    assert bbox.y0 <= top + TOLERANCE
    assert bbox.x1 >= right - TOLERANCE
    assert bbox.y1 >= bottom - TOLERANCE
    # 比例仍然是 1200:300，没有被拉伸变形
    assert (bbox.x1 - bbox.x0) / (bbox.y1 - bbox.y0) == pytest.approx(4.0, abs=0.02)
    # A4 竖版装不下 4:1 的横图，所以高度铺满、宽度溢出
    assert (bbox.y1 - bbox.y0) == pytest.approx(bottom - top, abs=TOLERANCE)


def test_original_fit_places_the_image_at_its_pixel_size(client: TestClient) -> None:
    """「原始大小」：1 像素 = 1 磅，不缩放、居中放置（第九阶段 §十）。

    与「自动页面尺寸」用同一套换算，所以「自动 + 原始大小」得到的页面
    正好贴合图片。放在固定纸张上时超出的部分会被裁掉 —— 这一点必须
    在 notes 里如实说明，而不是让用户以为图片被缩没了。
    """
    response = post_images(
        client,
        image_files(("p.jpg", build_image_bytes(1200, 300, "JPEG"))),
        page_size="a4",
        orientation="portrait",
        fit="original",
        margin="none",
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert any("原始大小" in note and "裁剪" in note for note in body["notes"])

    with fetch_pdf(client, body["download_url"]) as doc:
        page = doc[0]
        bbox = page.get_image_bbox(page.get_images(full=True)[0])
        page_rect = page.rect
        assert bbox.width == pytest.approx(1200.0, abs=TOLERANCE)
        assert bbox.height == pytest.approx(300.0, abs=TOLERANCE)
        # 居中：左右余量相等（页面装不下，两边都是负的溢出量）
        assert bbox.x0 - page_rect.x0 == pytest.approx(page_rect.x1 - bbox.x1, abs=TOLERANCE)


def test_original_fit_on_an_auto_page_fits_exactly(client: TestClient) -> None:
    """自动页面 + 原始大小：页面就是图片本身，一个像素也不缩放。"""
    response = post_images(
        client,
        image_files(("p.jpg", build_image_bytes(640, 480, "JPEG"))),
        fit="original",
    )
    assert response.status_code == 200, response.text

    with fetch_pdf(client, response.json()["download_url"]) as doc:
        page = doc[0]
        assert page.rect.width == pytest.approx(640.0, abs=TOLERANCE)
        assert page.rect.height == pytest.approx(480.0, abs=TOLERANCE)
        bbox = page.get_image_bbox(page.get_images(full=True)[0])
        assert bbox.width == pytest.approx(640.0, abs=TOLERANCE)
        assert bbox.height == pytest.approx(480.0, abs=TOLERANCE)


# ----------------------------------------------------------------------
# 非法输入
# ----------------------------------------------------------------------

@pytest.mark.parametrize(
    ("options", "keyword"),
    [
        ({"page_size": "a3"}, "页面大小"),
        ({"fit": "stretch"}, "适应方式"),
        ({"margin": "huge"}, "页边距"),
        ({"orientation": "diagonal"}, "页面方向"),
    ],
)
def test_invalid_options_are_rejected(client: TestClient, options: dict, keyword: str) -> None:
    """非法枚举值必须报错，并指出是哪个参数。"""
    response = post_images(
        client,
        image_files(("p.jpg", build_image_bytes(200, 200, "JPEG"))),
        **options,
    )
    assert response.status_code == 400
    message = response.json()["error"]["message"]
    assert keyword in message


@pytest.mark.parametrize(
    ("options", "keyword"),
    [
        ({"page_size": "custom"}, "宽度"),
        ({"page_size": "custom", "custom_width_mm": "100"}, "高度"),
        (
            {"page_size": "custom", "custom_width_mm": "5", "custom_height_mm": "100"},
            "毫米",
        ),
        (
            {"page_size": "custom", "custom_width_mm": "abc", "custom_height_mm": "100"},
            "数字",
        ),
    ],
)
def test_invalid_custom_size(client: TestClient, options: dict, keyword: str) -> None:
    """自定义尺寸缺参数或超范围时要给出具体原因。"""
    response = post_images(
        client,
        image_files(("p.jpg", build_image_bytes(200, 200, "JPEG"))),
        **options,
    )
    assert response.status_code == 400
    assert keyword in response.json()["error"]["message"]


def test_non_image_upload_is_rejected(client: TestClient) -> None:
    """不是图片的文件（哪怕扩展名是 .jpg）必须被拒绝。"""
    response = post_images(client, image_files(("fake.jpg", b"this is not an image")))
    assert response.status_code == 415
    assert "JPG" in response.json()["error"]["message"]


def test_pdf_uploaded_as_image_is_rejected(client: TestClient) -> None:
    """把 PDF 改名成 .jpg 传进来也不能通过。"""
    response = post_images(client, image_files(("fake.jpg", b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")))
    assert response.status_code == 415


def test_one_bad_file_fails_whole_request(client: TestClient) -> None:
    """少一张的 PDF 是错的，因此单张失败要整体报错，而不是跳过继续。"""
    files = image_files(
        ("good.jpg", build_image_bytes(300, 300, "JPEG")),
        ("bad.jpg", b"not an image"),
    )
    response = post_images(client, files)
    assert response.status_code == 415
    assert response.json()["error"]["message"]


def test_empty_upload_is_rejected(client: TestClient) -> None:
    """空文件要明确报错。"""
    response = post_images(client, image_files(("empty.jpg", b"")))
    assert response.status_code == 400


def test_too_many_files_are_rejected(client: TestClient) -> None:
    """超过单次数量上限时明确拒绝。"""
    from config import settings

    files = image_files(
        *[
            (f"{index}.jpg", build_image_bytes(120, 120, "JPEG"))
            for index in range(settings.MAX_BATCH_FILES + 1)
        ]
    )
    response = post_images(client, files)
    assert response.status_code == 400
    assert str(settings.MAX_BATCH_FILES) in response.json()["error"]["message"]


# ----------------------------------------------------------------------
# 结果文件
# ----------------------------------------------------------------------

def test_result_is_one_time_download(client: TestClient) -> None:
    """结果令牌只能下载一次，第二次应提示已过期。"""
    response = post_images(
        client,
        image_files(("p.jpg", build_image_bytes(300, 300, "JPEG"))),
    )
    assert response.status_code == 200, response.text
    url = response.json()["download_url"]

    assert client.get(url).status_code == 200
    assert client.get(url).status_code == 404


def test_jpeg_is_embedded_without_recompression(client: TestClient) -> None:
    """JPEG 直通：嵌入的图片流与上传的原文件逐字节相同，画质不受损。"""
    source = build_image_bytes(1600, 1200, "JPEG")
    response = post_images(client, image_files(("p.jpg", source)))
    assert response.status_code == 200, response.text

    downloaded = client.get(response.json()["download_url"]).content
    with pymupdf.open("pdf", downloaded) as doc:
        xref = doc[0].get_images(full=True)[0][0]
        embedded = doc.xref_stream_raw(xref)
        assert doc.xref_get_key(xref, "Filter") == ("name", "/DCTDecode")

    assert embedded == source


def test_uploaded_images_are_not_left_on_disk(client: TestClient) -> None:
    """处理完成后，上传的原图不应还留在临时目录里。"""
    import tempfile
    from pathlib import Path

    before = set(Path(tempfile.gettempdir()).glob("filetools_*"))
    response = post_images(
        client,
        image_files(("p.jpg", build_image_bytes(300, 300, "JPEG"))),
    )
    assert response.status_code == 200, response.text
    # 下载后任务目录被清掉
    client.get(response.json()["download_url"])

    leftovers = set(Path(tempfile.gettempdir()).glob("filetools_*")) - before
    assert leftovers == set()


def test_uploaded_file_is_removed_when_validation_fails(client: TestClient) -> None:
    """校验失败时，半成品上传文件也要清掉，不能留在临时目录。"""
    import tempfile
    from pathlib import Path

    before = set(Path(tempfile.gettempdir()).glob("filetools_*"))
    response = post_images(client, image_files(("bad.jpg", b"not an image at all")))
    assert response.status_code == 415

    leftovers = set(Path(tempfile.gettempdir()).glob("filetools_*")) - before
    assert leftovers == set()


def test_response_shape_is_stable(client: TestClient) -> None:
    """响应字段齐全，前端只写一套解析逻辑。"""
    response = post_images(
        client,
        image_files(("p.jpg", build_image_bytes(300, 300, "JPEG"))),
    )
    body = response.json()
    for key in (
        "job_id",
        "download_url",
        "filename",
        "size",
        "media_type",
        "archived",
        "page_count",
        "original_size",
        "original_pages",
        "saved_bytes",
        "saved_percent",
        "files",
        "notes",
    ):
        assert key in body, key
    assert body["media_type"] == "application/pdf"
    assert body["original_pages"] is None


def test_exif_rotated_image_is_reported(client: TestClient) -> None:
    """带 EXIF 旋转信息的图片会被摆正，并在结果里说明。"""
    from PIL import Image

    image = Image.new("RGB", (400, 200), (200, 30, 30))
    exif = image.getexif()
    exif[0x0112] = 6  # 顺时针旋转 90 度
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", exif=exif)

    response = post_images(
        client,
        image_files(("phone.jpg", buffer.getvalue())),
        page_size="auto",
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert any("旋转" in note for note in body["notes"])

    with fetch_pdf(client, body["download_url"]) as doc:
        width, height = page_sizes(doc)[0]
        # 400x200 转正后变成 200x400
        assert (width, height) == pytest.approx((200.0, 400.0), abs=TOLERANCE)


# ----------------------------------------------------------------------
# 多帧 / 多页源文件（第九阶段决策 C：只取第一帧，但要如实说明）
# ----------------------------------------------------------------------

def build_multiframe_bytes(fmt: str, frames: int, width: int = 120, height: int = 80) -> bytes:
    """造一个货真价实的多帧 / 多页文件 —— GIF 用 ``save_all``，TIFF 同理。"""
    from PIL import Image

    images = [
        Image.new("RGB", (width, height), (30 * index, 90, 200 - 30 * index))
        for index in range(frames)
    ]
    buffer = io.BytesIO()
    images[0].save(buffer, fmt, save_all=True, append_images=images[1:], duration=100, loop=0)
    return buffer.getvalue()


@pytest.mark.parametrize(
    ("fmt", "extension", "keyword"),
    [("GIF", "gif", "动图"), ("TIFF", "tiff", "含")],
)
def test_multiframe_source_is_reported(
    client: TestClient, fmt: str, extension: str, keyword: str
) -> None:
    """多帧 GIF / 多页 TIFF 只放进第一帧，并在 notes 里说明还有多少内容没转。"""
    source = build_multiframe_bytes(fmt, 3)
    response = post_images(client, image_files((f"multi.{extension}", source)))
    assert response.status_code == 200, response.text

    body = response.json()
    assert body["page_count"] == 1
    assert any("3" in note and keyword in note for note in body["notes"]), body["notes"]

    with fetch_pdf(client, body["download_url"]) as doc:
        assert doc.page_count == 1


def test_single_frame_source_is_not_reported(client: TestClient) -> None:
    """单帧文件绝不能收到「只转换了第一帧」这句假话。"""
    response = post_images(client, image_files(("one.gif", build_multiframe_bytes("GIF", 1))))
    assert response.status_code == 200, response.text

    body = response.json()
    assert body["page_count"] == 1
    assert not any("帧" in note or "第一页" in note for note in body["notes"]), body["notes"]


def test_several_multiframe_sources_are_summarised(client: TestClient) -> None:
    """一批里有多个多帧源时汇总成一句，但每个文件的帧数照样列出来。"""
    response = post_images(
        client,
        image_files(
            ("a.gif", build_multiframe_bytes("GIF", 3)),
            ("b.png", build_image_bytes(200, 100, "PNG")),
            ("c.tiff", build_multiframe_bytes("TIFF", 4, 100, 60)),
        ),
    )
    assert response.status_code == 200, response.text

    body = response.json()
    assert body["page_count"] == 3
    notes = [note for note in body["notes"] if "多帧" in note]
    assert len(notes) == 1, body["notes"]
    assert "2" in notes[0] and "GIF 3 帧" in notes[0] and "TIFF 4 页" in notes[0], notes[0]


def test_frame_note_wording_matches_the_image_pipeline() -> None:
    """图片 → PDF 与图片 → 图片两条路的措辞必须逐字一致。

    两边共用 ``compressors.pipeline.frame_note``；这条测试盯住的是
    「不许有人哪天在其中一条路上手抄一份自己的文案」。
    """
    from compressors.pipeline import frame_note
    from pdf.image_to_pdf import _multi_frame_notes

    assert _multi_frame_notes([("gif", 3)]) == [frame_note("gif", 3)]
    assert _multi_frame_notes([("tiff", 5)]) == [frame_note("tiff", 5)]
    assert _multi_frame_notes([]) == []
