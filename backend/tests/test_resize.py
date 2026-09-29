"""图片尺寸调整的端到端测试。

覆盖：指定宽/高、保持宽高比例的自动联动、常用尺寸预设、目标大小控制、批量处理。
批量接口是异步的（提交 → 轮询），``run_task`` 把两步合成一次调用。
"""

from __future__ import annotations

import io
import zipfile

from fastapi.testclient import TestClient
from PIL import Image

from config import settings
from tests.conftest import build_image_bytes, image_files, run_task

ENDPOINT = "/api/image/resize"

# 与前端「常用尺寸」预设一致的一组尺寸
PRESET_SIZES = [
    (1080, 1080),   # 社交媒体 · 方图
    (1080, 1350),   # 社交媒体 · 竖图
    (1080, 1920),   # 社交媒体 · 全屏竖版
    (1920, 1080),   # 视频 · 横版 1080p
    (1280, 720),    # 视频 · 720p
]


def post_resize(
    client: TestClient,
    images: list[tuple[str, bytes]],
    **fields: str,
):
    return run_task(client, ENDPOINT, files=image_files(*images), data=fields)


def result_image(client: TestClient, body: dict) -> Image.Image:
    response = client.get(body["download_url"])
    assert response.status_code == 200, response.text
    return Image.open(io.BytesIO(response.content))


# ----------------------------------------------------------------------
# 基本尺寸调整
# ----------------------------------------------------------------------

def test_resize_by_width_derives_height(client: TestClient) -> None:
    """4032 × 3024 的图，只填宽度 1920 → 高度应自动变成 1440。"""
    source = build_image_bytes(4032, 3024, "JPEG")
    response = post_resize(client, [("photo.jpg", source)], width="1920")
    assert response.status_code == 200, response.text

    item = response.json()["items"][0]
    assert item["result"]["width"] == 1920
    assert item["result"]["height"] == 1440

    image = result_image(client, response.json())
    assert image.size == (1920, 1440)


def test_resize_by_height_derives_width(client: TestClient) -> None:
    source = build_image_bytes(4000, 3000, "JPEG")
    response = post_resize(client, [("photo.jpg", source)], height="600")
    assert response.status_code == 200, response.text

    item = response.json()["items"][0]
    assert item["result"]["height"] == 600
    assert item["result"]["width"] == 800


def test_resize_without_keep_aspect_uses_exact_value(client: TestClient) -> None:
    """不保持比例时，只填的宽度生效，高度维持原值。"""
    source = build_image_bytes(1600, 1200, "JPEG")
    response = post_resize(
        client, [("photo.jpg", source)], width="800", keep_aspect="false"
    )
    assert response.status_code == 200, response.text

    item = response.json()["items"][0]
    assert item["result"]["width"] == 800
    assert item["result"]["height"] == 1200


def test_resize_both_edges_without_keep_aspect_is_exact(client: TestClient) -> None:
    """不保持比例且两边都填 → 精确拉伸到指定尺寸。"""
    source = build_image_bytes(1600, 1200, "JPEG")
    response = post_resize(
        client,
        [("photo.jpg", source)],
        width="640",
        height="640",
        keep_aspect="false",
    )
    assert response.status_code == 200, response.text

    item = response.json()["items"][0]
    assert item["result"]["width"] == 640
    assert item["result"]["height"] == 640


def test_resize_both_edges_with_keep_aspect_fits_inside_box(client: TestClient) -> None:
    """保持比例且两边都填 → 等比缩放到正好放进框内，不裁剪、不变形。"""
    source = build_image_bytes(1600, 1200, "JPEG")
    response = post_resize(
        client,
        [("photo.jpg", source)],
        width="1000",
        height="1000",
        keep_aspect="true",
    )
    assert response.status_code == 200, response.text

    item = response.json()["items"][0]
    # 4:3 的图画进 1000×1000 的框 → 1000×750
    assert item["result"]["width"] == 1000
    assert item["result"]["height"] == 750
    # 不能超出用户给的框
    assert item["result"]["width"] <= 1000
    assert item["result"]["height"] <= 1000


def test_resize_preset_sizes(client: TestClient) -> None:
    """常用尺寸预设实际提交的就是一组宽高，必须都能得到精确结果。"""
    source = build_image_bytes(2400, 1800, "JPEG")

    for width, height in PRESET_SIZES:
        response = post_resize(
            client,
            [("photo.jpg", source)],
            width=str(width),
            height=str(height),
            keep_aspect="false",
        )
        assert response.status_code == 200, f"{width}x{height}: {response.text}"

        item = response.json()["items"][0]
        assert (item["result"]["width"], item["result"]["height"]) == (width, height)


def test_resize_preset_with_keep_aspect_keeps_ratio(client: TestClient) -> None:
    """预设尺寸配合「保持宽高比例」时，结果不超出预设框且比例不变。"""
    source = build_image_bytes(4000, 3000, "JPEG")  # 4:3
    for width, height in PRESET_SIZES:
        response = post_resize(
            client,
            [("photo.jpg", source)],
            width=str(width),
            height=str(height),
            keep_aspect="true",
        )
        assert response.status_code == 200

        item = response.json()["items"][0]
        assert item["result"]["width"] <= width
        assert item["result"]["height"] <= height
        ratio = item["result"]["width"] / item["result"]["height"]
        assert abs(ratio - 4 / 3) < 0.01, f"比例被改变：{ratio}"


def test_resize_keeps_format_and_is_decodable(client: TestClient) -> None:
    """尺寸调整不应改变格式。"""
    for source_format, extension, expected in (
        ("JPEG", "jpg", "JPEG"),
        ("PNG", "png", "PNG"),
        ("WEBP", "webp", "WEBP"),
    ):
        source = build_image_bytes(1200, 900, source_format)
        response = post_resize(client, [(f"photo.{extension}", source)], width="600")
        assert response.status_code == 200, response.text

        body = response.json()
        assert body["items"][0]["result"]["format"] == (
            "jpeg" if expected == "JPEG" else expected.lower()
        )

        image = result_image(client, body)
        assert image.format == expected
        assert image.size == (600, 450)


def test_resize_filename_keeps_extension_with_suffix(client: TestClient) -> None:
    source = build_image_bytes(800, 600, "JPEG")
    response = post_resize(client, [("my photo.jpg", source)], width="400")
    assert response.status_code == 200

    filename = response.json()["items"][0]["result"]["filename"]
    assert filename == "my photo_resized.jpg"


# ----------------------------------------------------------------------
# 目标大小控制
# ----------------------------------------------------------------------

def test_resize_with_target_bytes(client: TestClient) -> None:
    """先调尺寸、再自动优化质量，使结果不超过目标大小。"""
    source = build_image_bytes(1600, 1200, "JPEG")
    target = 80 * 1024

    response = post_resize(
        client, [("photo.jpg", source)], width="800", target_bytes=str(target)
    )
    assert response.status_code == 200, response.text

    item = response.json()["items"][0]
    assert item["result"]["size"] <= target
    assert item["target_met"] is True


def test_resize_with_target_never_changes_requested_size(client: TestClient) -> None:
    """用户明确的宽高必须被尊重 —— 不能为了达标而偷偷缩小尺寸。"""
    source = build_image_bytes(1600, 1200, "JPEG")
    target = 5 * 1024  # 小到几乎不可能达成

    response = post_resize(
        client, [("photo.jpg", source)], width="1200", target_bytes=str(target)
    )
    assert response.status_code == 200, response.text

    item = response.json()["items"][0]
    assert item["result"]["width"] == 1200
    assert item["result"]["height"] == 900
    # 达不到目标时如实说明，而不是报错或偷偷改尺寸
    assert item["target_met"] is False
    assert item["note"]


def test_resize_upscales_when_user_asks(client: TestClient) -> None:
    """用户明确要求放大时按用户要求执行，不做隐式限制。"""
    source = build_image_bytes(400, 300, "JPEG")
    response = post_resize(client, [("small.jpg", source)], width="800")
    assert response.status_code == 200, response.text

    item = response.json()["items"][0]
    assert item["result"]["width"] == 800
    assert item["result"]["height"] == 600


# ----------------------------------------------------------------------
# 批量
# ----------------------------------------------------------------------

def test_resize_batch_returns_zip(client: TestClient) -> None:
    images = [
        ("a.jpg", build_image_bytes(800, 600, "JPEG")),
        ("b.jpg", build_image_bytes(1000, 500, "JPEG")),
    ]
    response = post_resize(client, images, width="400")
    assert response.status_code == 200, response.text

    body = response.json()
    assert body["archived"] is True
    assert body["archive_filename"] == "resized_images.zip"

    download = client.get(body["download_url"])
    assert download.status_code == 200

    with zipfile.ZipFile(io.BytesIO(download.content)) as archive:
        assert sorted(archive.namelist()) == ["a_resized.jpg", "b_resized.jpg"]
        for name in archive.namelist():
            with archive.open(name) as fh:
                image = Image.open(io.BytesIO(fh.read()))
                # 保持比例：两张图都按宽度 400 等比缩放
                assert image.width == 400


def test_resize_batch_keep_aspect_uses_each_image_ratio(client: TestClient) -> None:
    """批量保持比例时，每张图按各自的比例推导另一边。"""
    images = [
        ("wide.jpg", build_image_bytes(1600, 800, "JPEG")),   # 2:1
        ("tall.jpg", build_image_bytes(800, 1600, "JPEG")),   # 1:2
    ]
    response = post_resize(client, images, width="400")
    assert response.status_code == 200

    items = response.json()["items"]
    assert (items[0]["result"]["width"], items[0]["result"]["height"]) == (400, 200)
    assert (items[1]["result"]["width"], items[1]["result"]["height"]) == (400, 800)


def test_resize_allows_max_batch_files(client: TestClient) -> None:
    """正好 20 张（上限）必须能处理。"""
    source = build_image_bytes(200, 200, "JPEG")
    images = [(f"img{index}.jpg", source) for index in range(20)]

    response = post_resize(client, images, width="100")
    assert response.status_code == 200, response.text

    body = response.json()
    assert len(body["items"]) == 20
    assert body["failures"] == []


def test_resize_rejects_too_many_files(client: TestClient) -> None:
    source = build_image_bytes(200, 200, "JPEG")
    images = [
        (f"img{index}.jpg", source) for index in range(settings.MAX_BATCH_FILES + 1)
    ]

    response = post_resize(client, images, width="100")
    assert response.status_code == 400
    assert str(settings.MAX_BATCH_FILES) in response.json()["error"]["message"]


# ----------------------------------------------------------------------
# 参数校验
# ----------------------------------------------------------------------

def test_resize_requires_at_least_one_edge(client: TestClient) -> None:
    source = build_image_bytes(400, 300, "JPEG")
    response = post_resize(client, [("photo.jpg", source)])
    assert response.status_code == 400
    assert "宽度" in response.json()["error"]["message"]


def test_resize_rejects_invalid_edges(client: TestClient) -> None:
    source = build_image_bytes(400, 300, "JPEG")

    assert post_resize(client, [("p.jpg", source)], width="0").status_code == 400
    assert post_resize(client, [("p.jpg", source)], width="-10").status_code == 400
    assert post_resize(client, [("p.jpg", source)], width="abc").status_code == 400
    assert post_resize(client, [("p.jpg", source)], width="99999").status_code == 400


def test_resize_rejects_broken_image(client: TestClient) -> None:
    response = post_resize(client, [("bad.jpg", b"not an image" * 30)], width="100")
    assert response.status_code in (400, 415)
    assert response.json()["error"]["message"]


def test_resize_png_without_quality_stays_lossless(client: TestClient) -> None:
    """尺寸调整页不发送质量参数，PNG 结果必须保持无损而不是被量化。"""
    with Image.open(io.BytesIO(build_image_bytes(800, 600, "PNG"))) as img:
        buffer = io.BytesIO()
        img.convert("RGB").save(buffer, format="PNG")
    source = buffer.getvalue()

    response = post_resize(client, [("photo.png", source)], width="400", keep_aspect="true")
    assert response.status_code == 200, response.text

    data = client.get(response.json()["download_url"]).content
    with Image.open(io.BytesIO(data)) as img:
        assert img.format == "PNG"
        assert img.size == (400, 300)
        colors = len(img.convert("RGB").getcolors(maxcolors=1 << 24) or [])
    assert colors > 256
