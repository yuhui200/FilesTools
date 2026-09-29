"""图片压缩接口的端到端测试。

第四阶段起 ``/api/image/compress`` 是批量 + 异步接口：
提交后返回任务号，处理在后台队列里逐个文件进行。
这里用 ``run_task`` 把「提交 + 轮询」合成一次调用，
断言仍然针对最终的处理结果（与同步接口的响应体一致）。
"""

from __future__ import annotations

import io
from urllib.parse import unquote

from fastapi.testclient import TestClient
from PIL import Image

from config import settings
from tests.conftest import build_image_bytes, image_files, run_task, submit_task

ENDPOINT = "/api/image/compress"


def post_image(
    client: TestClient,
    data: bytes,
    filename: str = "photo.jpg",
    *,
    quality: str | None = None,
    target_bytes: int | None = None,
):
    """上传一张图片并等到处理结束。"""
    form: dict[str, str] = {}
    if quality is not None:
        form["quality"] = quality
    if target_bytes is not None:
        form["target_bytes"] = str(target_bytes)
    return run_task(client, ENDPOINT, files=image_files((filename, data)), data=form)


# ----------------------------------------------------------------------
# 基础连通性
# ----------------------------------------------------------------------

def test_health(client: TestClient) -> None:
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_public_config(client: TestClient) -> None:
    payload = client.get("/api/config").json()
    assert payload["max_upload_bytes"] == settings.MAX_UPLOAD_BYTES
    assert ".jpg" in payload["allowed_image_extensions"]


# ----------------------------------------------------------------------
# 任务提交
# ----------------------------------------------------------------------

def test_submit_returns_task_and_status_url(client: TestClient, jpeg_bytes: bytes) -> None:
    """提交后立刻拿到任务号，而不是等处理完。"""
    response = submit_task(
        client, ENDPOINT, files=image_files(("photo.jpg", jpeg_bytes)), data={"quality": "balanced"}
    )
    assert response.status_code == 202, response.text

    body = response.json()
    assert len(body["group_id"]) == 32
    assert body["tool"] == "image.compress"
    assert body["label"] == "图片压缩"
    assert body["total"] == 1
    assert body["status_url"] == f"/api/tasks/{body['group_id']}"

    # 轮询接口必须能查到它
    snapshot = client.get(body["status_url"])
    assert snapshot.status_code == 200
    assert snapshot.json()["total"] == 1


def test_unknown_task_returns_404(client: TestClient) -> None:
    response = client.get("/api/tasks/deadbeefdeadbeefdeadbeefdeadbeef")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "TASK_NOT_FOUND"


# ----------------------------------------------------------------------
# 按质量档位压缩
# ----------------------------------------------------------------------

def test_compress_jpeg_without_target(client: TestClient, jpeg_bytes: bytes) -> None:
    response = post_image(client, jpeg_bytes, quality="balanced")
    assert response.status_code == 200, response.text

    body = response.json()
    item = body["items"][0]
    assert item["original"]["size"] == len(jpeg_bytes)
    assert item["result"]["size"] < item["original"]["size"]
    assert item["saved_percent"] > 0
    assert body["download_url"] == f"/api/download/{body['job_id']}"
    assert body["target_bytes"] is None
    assert item["target_met"] is True


# ----------------------------------------------------------------------
# 按目标大小压缩 —— MVP 的核心场景
# ----------------------------------------------------------------------

def test_compress_to_target_size(client: TestClient, jpeg_bytes: bytes) -> None:
    target = 300 * 1024
    response = post_image(client, jpeg_bytes, quality="balanced", target_bytes=target)
    assert response.status_code == 200, response.text

    body = response.json()
    item = body["items"][0]
    assert body["target_bytes"] == target
    assert item["target_met"] is True
    assert item["result"]["size"] <= target, "结果必须不超过目标大小"
    # 「尽可能接近目标」：不应比目标小太多
    assert item["result"]["size"] > target * 0.5


def test_target_smaller_than_original_returns_original(
    client: TestClient, jpeg_bytes: bytes
) -> None:
    """原文件已小于目标时，不应做任何有损处理。"""
    target = len(jpeg_bytes) + 1024 * 1024
    response = post_image(client, jpeg_bytes, quality="balanced", target_bytes=target)
    assert response.status_code == 200

    item = response.json()["items"][0]
    assert item["untouched"] is True
    assert item["result"]["size"] == len(jpeg_bytes)
    assert item["note"] is not None


def test_impossible_target_falls_back_gracefully(
    client: TestClient, jpeg_bytes: bytes, monkeypatch
) -> None:
    """目标小到无法达成时，返回能做到的最小结果并说明原因，而不是报错。

    把缩小轮次设为 0，模拟「降质量已经到极限、也不允许再缩尺寸」的情况。
    """
    monkeypatch.setattr(settings, "MAX_DOWNSCALE_ROUNDS", 0)

    target = 5 * 1024  # 5 KB，1600x1200 的图单靠降质量到不了
    response = post_image(client, jpeg_bytes, quality="strong", target_bytes=target)
    assert response.status_code == 200

    item = response.json()["items"][0]
    assert item["target_met"] is False
    assert item["note"]
    assert item["result"]["size"] < item["original"]["size"]


def test_aggressive_target_allows_downscaling(client: TestClient, jpeg_bytes: bytes) -> None:
    """目标很小时，允许通过缩小尺寸达成（用户已授权自动调整尺寸）。"""
    target = 5 * 1024
    response = post_image(client, jpeg_bytes, quality="strong", target_bytes=target)
    assert response.status_code == 200

    item = response.json()["items"][0]
    assert item["target_met"] is True
    assert item["result"]["size"] <= target
    assert item["scale"] < 1.0, "应当缩小了尺寸"
    assert item["result"]["width"] < item["original"]["width"]


def test_quality_presets_produce_different_sizes(client: TestClient, jpeg_bytes: bytes) -> None:
    high = post_image(client, jpeg_bytes, quality="high").json()["items"][0]
    strong = post_image(client, jpeg_bytes, quality="strong").json()["items"][0]
    assert strong["result"]["size"] < high["result"]["size"]


# ----------------------------------------------------------------------
# 各输入格式
# ----------------------------------------------------------------------

def test_compress_png(client: TestClient, png_bytes: bytes) -> None:
    response = post_image(client, png_bytes, filename="shot.png", quality="balanced")
    assert response.status_code == 200, response.text
    assert response.json()["items"][0]["result"]["format"] == "png"


def test_compress_png_to_target(client: TestClient, png_bytes: bytes) -> None:
    target = 120 * 1024
    response = post_image(
        client, png_bytes, filename="shot.png", quality="strong", target_bytes=target
    )
    assert response.status_code == 200, response.text
    item = response.json()["items"][0]
    assert item["result"]["size"] <= target
    assert item["target_met"] is True


def test_compress_webp(client: TestClient, webp_bytes: bytes) -> None:
    response = post_image(client, webp_bytes, filename="pic.webp", quality="balanced")
    assert response.status_code == 200, response.text
    assert response.json()["items"][0]["result"]["format"] == "webp"


def test_result_is_decodable_image(client: TestClient, jpeg_bytes: bytes) -> None:
    """下载回来的必须是能正常解码的图片。"""
    body = post_image(client, jpeg_bytes, quality="balanced", target_bytes=200 * 1024).json()
    download = client.get(body["download_url"])
    assert download.status_code == 200

    with Image.open(io.BytesIO(download.content)) as img:
        img.load()
        assert img.format == "JPEG"
        assert img.width > 0 and img.height > 0


# ----------------------------------------------------------------------
# 下载行为
# ----------------------------------------------------------------------

def test_download_is_single_use(client: TestClient, jpeg_bytes: bytes) -> None:
    body = post_image(client, jpeg_bytes, quality="balanced").json()
    url = body["download_url"]

    assert client.get(url).status_code == 200
    # 下载后临时文件已删除，令牌失效
    assert client.get(url).status_code == 404


def test_download_unknown_token(client: TestClient) -> None:
    response = client.get("/api/download/deadbeefdeadbeefdeadbeefdeadbeef")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "JOB_NOT_FOUND"


def disposition_filename(header: str) -> str:
    """从 Content-Disposition 里取出文件名，兼容 RFC 5987 的 filename* 形式。"""
    if "filename*=utf-8''" in header:
        return unquote(header.split("filename*=utf-8''", 1)[1].strip())
    return header.split('filename="', 1)[1].rstrip('"')


def test_download_filename_is_readable(client: TestClient, jpeg_bytes: bytes) -> None:
    body = post_image(client, jpeg_bytes, filename="my photo.jpg", quality="balanced").json()
    assert body["items"][0]["result"]["filename"] == "my photo_compressed.jpg"

    download = client.get(body["download_url"])
    assert disposition_filename(download.headers["content-disposition"]) == "my photo_compressed.jpg"


def test_download_chinese_filename(client: TestClient, jpeg_bytes: bytes) -> None:
    """中文文件名不能乱码，也不能被当成路径。"""
    body = post_image(client, jpeg_bytes, filename="风景照片.jpg", quality="balanced").json()
    assert body["items"][0]["result"]["filename"] == "风景照片_compressed.jpg"

    download = client.get(body["download_url"])
    assert disposition_filename(download.headers["content-disposition"]) == "风景照片_compressed.jpg"


def test_malicious_filename_is_sanitized(client: TestClient, jpeg_bytes: bytes) -> None:
    """带路径穿越的文件名会被清洗掉，不会影响落盘位置。"""
    body = post_image(
        client, jpeg_bytes, filename="../../..\\windows\\evil.jpg", quality="balanced"
    ).json()
    name = body["items"][0]["result"]["filename"]
    assert "/" not in name and "\\" not in name
    assert name.endswith("_compressed.jpg")


# ----------------------------------------------------------------------
# 安全校验
# ----------------------------------------------------------------------

def test_reject_text_file_disguised_as_jpg(client: TestClient) -> None:
    response = post_image(client, b"this is definitely not an image" * 20, "evil.jpg")
    assert response.status_code == 415


def test_reject_extension_content_mismatch(client: TestClient, png_bytes: bytes) -> None:
    """PNG 内容配上 .jpg 扩展名 —— 必须拒绝。"""
    response = post_image(client, png_bytes, "fake.jpg")
    assert response.status_code == 415


def test_reject_unsupported_extension(client: TestClient, jpeg_bytes: bytes) -> None:
    response = post_image(client, jpeg_bytes, "archive.zip")
    assert response.status_code == 415


def test_reject_empty_file(client: TestClient) -> None:
    response = post_image(client, b"", "empty.jpg")
    assert response.status_code == 400


def test_reject_truncated_jpeg(client: TestClient, jpeg_bytes: bytes) -> None:
    """文件头正确但内容被截断 —— 必须拒绝。"""
    response = post_image(client, jpeg_bytes[:200], "broken.jpg")
    assert response.status_code in (400, 415, 422)


def test_reject_invalid_quality(client: TestClient, jpeg_bytes: bytes) -> None:
    response = post_image(client, jpeg_bytes, quality="ultra")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_REQUEST"


def test_reject_target_too_small(client: TestClient, jpeg_bytes: bytes) -> None:
    response = post_image(client, jpeg_bytes, target_bytes=100)
    assert response.status_code == 400


def test_reject_oversized_upload(client: TestClient) -> None:
    """超过上传上限的文件必须被拒绝。"""
    oversize = b"\xff\xd8\xff" + b"\x00" * (settings.MAX_UPLOAD_BYTES + 1024)
    response = post_image(client, oversize, "huge.jpg")
    assert response.status_code == 413


def test_empty_target_means_no_limit(client: TestClient, jpeg_bytes: bytes) -> None:
    response = run_task(
        client,
        ENDPOINT,
        files=image_files(("photo.jpg", jpeg_bytes)),
        data={"quality": "balanced", "target_bytes": ""},
    )
    assert response.status_code == 200
    assert response.json()["target_bytes"] is None


# ----------------------------------------------------------------------
# 压缩器单元测试
# ----------------------------------------------------------------------

def test_max_edge_is_clamped() -> None:
    """超大图片会被等比缩到最大边长以内。"""
    from compressors.image_compressor import CompressOptions, compress_image
    from utils.files import create_temp_dir, remove_dir

    work = create_temp_dir()
    try:
        big_w, big_h = 3000, 1500
        data = build_image_bytes(big_w, big_h, "JPEG")
        source = work / "big.jpg"
        source.write_bytes(data)

        result = compress_image(source, CompressOptions(quality_preset="balanced"))
        assert max(result.width, result.height) <= settings.MAX_IMAGE_EDGE
        # 等比缩放，宽高比不变
        assert abs(result.width / result.height - big_w / big_h) < 0.01
    finally:
        remove_dir(work)
