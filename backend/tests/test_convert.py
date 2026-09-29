"""图片格式转换的端到端测试。

批量接口是异步的（提交 → 轮询），``run_task`` 把两步合成一次调用，
断言仍然针对最终结果。
"""

from __future__ import annotations

import io
import zipfile

from fastapi.testclient import TestClient
from PIL import Image

from config import settings
from tests.conftest import build_image_bytes, image_files, run_task

ENDPOINT = "/api/image/convert"

# 六种转换组合：(源格式, 源扩展名, 用户提交的目标格式, 内部格式名)
COMBINATIONS = [
    ("JPEG", "jpg", "png", "png"),
    ("JPEG", "jpg", "webp", "webp"),
    ("PNG", "png", "jpg", "jpeg"),
    ("PNG", "png", "webp", "webp"),
    ("WEBP", "webp", "jpg", "jpeg"),
    ("WEBP", "webp", "png", "png"),
]

# 目标格式 -> 期望的 Pillow 格式名
EXPECTED_PILLOW_FORMAT = {"jpeg": "JPEG", "png": "PNG", "webp": "WEBP"}


def post_convert(
    client: TestClient,
    images: list[tuple[str, bytes]],
    target_format: str,
    **extra: str,
):
    data = {"target_format": target_format, **extra}
    return run_task(client, ENDPOINT, files=image_files(*images), data=data)


def fetch(client: TestClient, url: str) -> bytes:
    response = client.get(url)
    assert response.status_code == 200, response.text
    return response.content


# ----------------------------------------------------------------------
# 基本转换
# ----------------------------------------------------------------------

def test_convert_single_file_returns_image_not_zip(client: TestClient) -> None:
    """单文件转换应直接返回图片，不套一层 ZIP。"""
    source = build_image_bytes(800, 600, "JPEG")
    response = post_convert(client, [("photo.jpg", source)], "png")
    assert response.status_code == 200, response.text

    body = response.json()
    assert body["archived"] is False
    assert body["archive_filename"] is None
    assert len(body["items"]) == 1

    item = body["items"][0]
    assert item["result"]["format"] == "png"
    assert item["result"]["filename"] == "photo.png"

    download = client.get(body["download_url"])
    assert download.status_code == 200
    assert download.headers["content-type"] == "image/png"
    assert Image.open(io.BytesIO(download.content)).format == "PNG"


def test_convert_all_six_combinations(client: TestClient) -> None:
    """JPG / PNG / WEBP 三者的全部 6 种互转组合都必须真实可用。

    用户提交 "jpg"，服务端内部统一成 "jpeg"，因此这里比对的是内部名。
    """
    for source_format, extension, target, internal in COMBINATIONS:
        source = build_image_bytes(640, 480, source_format)
        response = post_convert(client, [(f"image.{extension}", source)], target)
        assert response.status_code == 200, f"{source_format} -> {target}: {response.text}"

        body = response.json()
        assert body["items"][0]["result"]["format"] == internal

        raw = fetch(client, body["download_url"])
        decoded = Image.open(io.BytesIO(raw))
        assert decoded.format == EXPECTED_PILLOW_FORMAT[internal]
        # 尺寸不能因为转换而改变
        assert decoded.size == (640, 480)
        assert decoded.width > 0 and decoded.height > 0


def test_convert_preserves_transparency_to_png(client: TestClient) -> None:
    """带透明通道的图片转 PNG 必须保留透明通道。"""
    image = Image.new("RGBA", (200, 200), (255, 0, 0, 0))
    image.putpixel((10, 10), (0, 255, 0, 255))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    source = buffer.getvalue()

    response = post_convert(client, [("alpha.png", source)], "png")
    assert response.status_code == 200, response.text

    raw = fetch(client, response.json()["download_url"])
    decoded = Image.open(io.BytesIO(raw))
    assert decoded.mode == "RGBA"
    assert decoded.getpixel((100, 100))[3] == 0, "透明区域应保持透明"


def test_convert_transparency_to_jpg_flattens_to_white(client: TestClient) -> None:
    """转 JPG 时透明区域应合成到白底，而不是变成黑色。"""
    image = Image.new("RGBA", (200, 200), (255, 0, 0, 0))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")

    response = post_convert(client, [("alpha.png", buffer.getvalue())], "jpg")
    assert response.status_code == 200, response.text

    raw = fetch(client, response.json()["download_url"])
    decoded = Image.open(io.BytesIO(raw))
    assert decoded.mode == "RGB"
    pixel = decoded.getpixel((100, 100))
    assert all(channel > 240 for channel in pixel), f"透明区域应为白色，实际 {pixel}"


def test_convert_same_format_is_allowed_and_explained(client: TestClient) -> None:
    """源格式与目标格式相同时不做无意义转换：原样返回，并说明原因。"""
    source = build_image_bytes(640, 480, "PNG")
    response = post_convert(client, [("photo.png", source)], "png")
    assert response.status_code == 200, response.text

    item = response.json()["items"][0]
    assert item["result"]["format"] == "png"
    assert item["untouched"] is True
    assert item["note"] and "已经是 PNG 格式" in item["note"]


def test_convert_same_format_returns_identical_bytes(client: TestClient) -> None:
    """原样返回意味着字节完全一致，不是重新编码一遍。"""
    source = build_image_bytes(320, 240, "JPEG")
    response = post_convert(client, [("photo.jpg", source)], "jpg")
    assert response.status_code == 200, response.text

    raw = fetch(client, response.json()["download_url"])
    assert raw == source


def test_mixed_batch_passes_matching_file_through_untouched(client: TestClient) -> None:
    """批量转换时，已经是目标格式的那张原样返回，其余正常转换。"""
    png = build_image_bytes(200, 150, "PNG")
    jpeg = build_image_bytes(200, 150, "JPEG")

    response = post_convert(client, [("a.png", png), ("b.jpg", jpeg)], "png")
    assert response.status_code == 200, response.text

    items = {item["original"]["filename"]: item for item in response.json()["items"]}
    assert items["a.png"]["untouched"] is True
    assert items["b.jpg"]["untouched"] is False
    assert items["b.jpg"]["result"]["format"] == "png"

    raw = fetch(client, response.json()["download_url"])
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        assert archive.read("a.png") == png


# ----------------------------------------------------------------------
# 质量与目标大小
# ----------------------------------------------------------------------

def test_convert_uses_default_quality_when_not_specified(client: TestClient) -> None:
    """不指定质量时用的是 ``settings.DEFAULT_QUALITY_VALUE``。

    第十阶段 A §二十七 把「推荐档」定死在 85，并明确要求它适用于**所有**路径 ——
    这个页面此前的默认是 80。所以这条断言从 80 改成 85，是**跟着规格改的精确值**，
    不是放宽：它仍然钉着一个具体数字，只是那个数字换了。
    （与 §十 把 PDF 页边距 36 磅改成 10 毫米同一性质，已写进验收报告。）
    """
    response = post_convert(
        client, [("photo.jpg", build_image_bytes(800, 600, "JPEG"))], "webp"
    )
    assert response.status_code == 200, response.text
    assert settings.DEFAULT_QUALITY_VALUE == 85
    assert response.json()["items"][0]["quality_used"] == settings.DEFAULT_QUALITY_VALUE


def test_convert_custom_quality_changes_size(client: TestClient) -> None:
    """自定义质量必须真正生效：质量越低文件越小。"""
    source = build_image_bytes(1200, 900, "JPEG")

    high = post_convert(client, [("photo.jpg", source)], "jpg", quality_value="95")
    low = post_convert(client, [("photo.jpg", source)], "jpg", quality_value="30")
    assert high.status_code == 200 and low.status_code == 200

    high_size = high.json()["items"][0]["result"]["size"]
    low_size = low.json()["items"][0]["result"]["size"]
    assert low_size < high_size, f"质量 30（{low_size}）应小于质量 95（{high_size}）"


def test_convert_respects_target_bytes(client: TestClient) -> None:
    """指定目标大小时，结果不能超过目标。"""
    source = build_image_bytes(1600, 1200, "JPEG")
    target = 60 * 1024

    response = post_convert(client, [("photo.jpg", source)], "jpg", target_bytes=str(target))
    assert response.status_code == 200, response.text

    item = response.json()["items"][0]
    assert item["result"]["size"] <= target
    assert item["target_met"] is True


def test_convert_rejects_invalid_format(client: TestClient) -> None:
    source = build_image_bytes(400, 300, "JPEG")
    response = post_convert(client, [("photo.jpg", source)], "gif")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_REQUEST"


def test_convert_rejects_invalid_quality(client: TestClient) -> None:
    source = build_image_bytes(400, 300, "JPEG")
    response = post_convert(client, [("photo.jpg", source)], "png", quality_value="0")
    assert response.status_code == 400

    response = post_convert(client, [("photo.jpg", source)], "png", quality_value="101")
    assert response.status_code == 400


# ----------------------------------------------------------------------
# 批量
# ----------------------------------------------------------------------

def test_convert_multiple_files_returns_zip(client: TestClient) -> None:
    """多个文件的转换结果必须打包成 ZIP，且内容都能解码。"""
    images = [
        ("a.jpg", build_image_bytes(400, 300, "JPEG")),
        ("b.jpg", build_image_bytes(320, 240, "JPEG")),
        ("c.jpg", build_image_bytes(360, 360, "JPEG")),
    ]
    response = post_convert(client, images, "png")
    assert response.status_code == 200, response.text

    body = response.json()
    assert body["archived"] is True
    assert body["archive_filename"] == "converted_images.zip"
    assert len(body["items"]) == 3

    download = client.get(body["download_url"])
    assert download.status_code == 200
    assert download.headers["content-type"] == "application/zip"

    with zipfile.ZipFile(io.BytesIO(download.content)) as archive:
        names = sorted(archive.namelist())
        assert names == ["a.png", "b.png", "c.png"]
        for name in names:
            with archive.open(name) as fh:
                assert Image.open(io.BytesIO(fh.read())).format == "PNG"


def test_convert_batch_preserves_upload_order(client: TestClient) -> None:
    """结果顺序必须与上传顺序一致。"""
    images = [
        ("first.jpg", build_image_bytes(300, 200, "JPEG")),
        ("second.jpg", build_image_bytes(300, 200, "JPEG")),
        ("third.jpg", build_image_bytes(300, 200, "JPEG")),
    ]
    response = post_convert(client, images, "png")
    assert response.status_code == 200

    items = response.json()["items"]
    assert [item["index"] for item in items] == [0, 1, 2]
    assert [item["original"]["filename"] for item in items] == [
        "first.jpg",
        "second.jpg",
        "third.jpg",
    ]


def test_convert_batch_deduplicates_names(client: TestClient) -> None:
    """同名文件在 ZIP 里不能互相覆盖。"""
    source = build_image_bytes(300, 200, "JPEG")
    response = post_convert(client, [("same.jpg", source), ("same.jpg", source)], "png")
    assert response.status_code == 200

    download = client.get(response.json()["download_url"])
    with zipfile.ZipFile(io.BytesIO(download.content)) as archive:
        assert sorted(archive.namelist()) == ["same-2.png", "same.png"]


def test_convert_batch_skips_broken_file_but_keeps_others(client: TestClient) -> None:
    """单张图片损坏不应导致整批失败。"""
    images = [
        ("good.jpg", build_image_bytes(400, 300, "JPEG")),
        ("broken.jpg", b"\xff\xd8\xff" + b"\x00" * 500),
    ]
    response = post_convert(client, images, "png")
    assert response.status_code == 200, response.text

    body = response.json()
    assert len(body["items"]) == 1
    assert len(body["failures"]) == 1
    assert body["failures"][0]["filename"] == "broken.jpg"
    assert body["failures"][0]["message"]


def test_convert_all_files_broken_returns_error(client: TestClient) -> None:
    """整批都失败时必须报错，而不是返回空结果。"""
    images = [
        ("bad1.jpg", b"not an image at all" * 20),
        ("bad2.jpg", b"still not an image" * 20),
    ]
    response = post_convert(client, images, "png")
    assert response.status_code in (400, 415)
    assert response.json()["error"]["message"]


def test_convert_rejects_too_many_files(client: TestClient) -> None:
    """超过单批数量上限必须拒绝。"""
    source = build_image_bytes(120, 120, "JPEG")
    images = [
        (f"img{index}.jpg", source) for index in range(settings.MAX_BATCH_FILES + 1)
    ]
    response = post_convert(client, images, "png")
    assert response.status_code == 400
    assert str(settings.MAX_BATCH_FILES) in response.json()["error"]["message"]


# ----------------------------------------------------------------------
# 预览
# ----------------------------------------------------------------------

def test_preview_does_not_consume_download_token(client: TestClient) -> None:
    """预览是只读的，看完预览仍然可以下载。"""
    source = build_image_bytes(400, 300, "JPEG")
    response = post_convert(client, [("photo.jpg", source)], "png")
    body = response.json()

    preview = client.get(body["items"][0]["preview_url"])
    assert preview.status_code == 200
    assert preview.headers["content-type"] == "image/png"
    assert Image.open(io.BytesIO(preview.content)).format == "PNG"

    # 预览两次都不应消耗令牌
    assert client.get(body["items"][0]["preview_url"]).status_code == 200
    assert client.get(body["download_url"]).status_code == 200


def test_preview_of_batch_item(client: TestClient) -> None:
    images = [
        ("a.jpg", build_image_bytes(400, 300, "JPEG")),
        ("b.jpg", build_image_bytes(300, 400, "JPEG")),
    ]
    response = post_convert(client, images, "webp")
    body = response.json()

    first = client.get(body["items"][0]["preview_url"])
    second = client.get(body["items"][1]["preview_url"])
    assert first.status_code == 200 and second.status_code == 200

    assert Image.open(io.BytesIO(first.content)).size == (400, 300)
    assert Image.open(io.BytesIO(second.content)).size == (300, 400)


def test_preview_unknown_job_returns_404(client: TestClient) -> None:
    assert client.get("/api/preview/deadbeef/0").status_code == 404


# ----------------------------------------------------------------------
# PNG 的无损行为
# ----------------------------------------------------------------------

def count_distinct_colors(data: bytes) -> int:
    """统计图片里的不同颜色数，用来判断 PNG 有没有被调色板量化。"""
    with Image.open(io.BytesIO(data)) as img:
        return len(img.convert("RGB").getcolors(maxcolors=1 << 24) or [])


def test_convert_to_png_without_quality_is_lossless(client: TestClient) -> None:
    """未指定质量时转 PNG 必须无损 —— 不能悄悄做调色板量化。"""
    source = build_image_bytes(640, 480, "JPEG", noisy=True)
    response = post_convert(client, [("photo.jpg", source)], "png")
    assert response.status_code == 200, response.text

    body = response.json()
    data = fetch(client, body["items"][0]["preview_url"])

    with Image.open(io.BytesIO(data)) as img:
        assert img.format == "PNG"
        assert img.mode == "RGB"

    # 调色板量化会把颜色压到 256 色以内；照片类图片远不止这个数
    assert count_distinct_colors(data) > 256


def test_convert_to_png_with_explicit_quality_quantizes(client: TestClient) -> None:
    """显式指定了质量时才允许量化，用来换取更小的体积。"""
    source = build_image_bytes(640, 480, "JPEG", noisy=True)

    lossless = post_convert(client, [("photo.jpg", source)], "png").json()
    quantized = post_convert(
        client, [("photo.jpg", source)], "png", quality_value="40"
    ).json()

    quantized_data = fetch(client, quantized["items"][0]["preview_url"])
    assert count_distinct_colors(quantized_data) <= 256
    assert quantized["items"][0]["result"]["size"] < lossless["items"][0]["result"]["size"]


def test_png_roundtrip_without_quality_keeps_transparency(client: TestClient) -> None:
    """PNG → PNG 且未指定质量时，透明通道和颜色都不能被改动。"""
    with Image.open(io.BytesIO(build_image_bytes(320, 240, "PNG"))) as img:
        rgba = img.convert("RGBA")
        rgba.putalpha(128)
        buffer = io.BytesIO()
        rgba.save(buffer, format="PNG")
    source = buffer.getvalue()

    response = post_convert(client, [("logo.png", source)], "png")
    assert response.status_code == 200, response.text

    data = fetch(client, response.json()["items"][0]["preview_url"])
    with Image.open(io.BytesIO(data)) as img:
        assert img.format == "PNG"
        assert "A" in img.mode
