"""批量处理与 ZIP 打包的测试。

重点关注安全要求：文件数量限制、整批大小限制、ZIP 的一次性与清理。
批量接口是异步的（提交 → 轮询），``run_task`` 把这两步合成一次调用，
断言仍然针对最终结果。
"""

from __future__ import annotations

import io
import re
import zipfile
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image

from config import settings
from services.job_store import job_store
from tests.conftest import build_image_bytes, image_files, run_task

CONVERT = "/api/image/convert"
RESIZE = "/api/image/resize"


def post(client: TestClient, endpoint: str, images: list[tuple[str, bytes]], **fields: str):
    return run_task(client, endpoint, files=image_files(*images), data=fields)


def temp_dirs_under(path: Path) -> set[Path]:
    if not path.is_dir():
        return set()
    return {item for item in path.iterdir() if item.is_dir()}


# ----------------------------------------------------------------------
# 数量与大小限制
# ----------------------------------------------------------------------

def test_batch_rejects_more_than_max_files(client: TestClient) -> None:
    source = build_image_bytes(120, 120, "JPEG")
    images = [(f"img{index}.jpg", source) for index in range(settings.MAX_BATCH_FILES + 1)]

    response = post(client, CONVERT, images, target_format="png")
    assert response.status_code == 400
    assert str(settings.MAX_BATCH_FILES) in response.json()["error"]["message"]


def test_batch_allows_exactly_max_files(client: TestClient) -> None:
    source = build_image_bytes(120, 120, "JPEG")
    images = [(f"img{index}.jpg", source) for index in range(settings.MAX_BATCH_FILES)]

    response = post(client, CONVERT, images, target_format="png")
    assert response.status_code == 200, response.text
    assert len(response.json()["items"]) == settings.MAX_BATCH_FILES


def test_batch_rejects_oversized_single_file(client: TestClient, monkeypatch) -> None:
    """单张超过上传上限时，该文件失败但整批继续。"""
    monkeypatch.setattr(settings, "MAX_UPLOAD_BYTES", 40 * 1024)

    big = build_image_bytes(1200, 1200, "JPEG")
    assert len(big) > 40 * 1024, "测试图片需要大于上限"
    small = build_image_bytes(120, 120, "JPEG")

    response = post(
        client,
        CONVERT,
        [("small.jpg", small), ("big.jpg", big)],
        target_format="png",
    )
    assert response.status_code == 200, response.text

    body = response.json()
    assert len(body["items"]) == 1
    assert body["items"][0]["original"]["filename"] == "small.jpg"
    assert [failure["filename"] for failure in body["failures"]] == ["big.jpg"]
    assert body["failures"][0]["code"] == "FILE_TOO_LARGE"


def test_batch_rejects_oversized_total(client: TestClient, monkeypatch) -> None:
    """整批总大小超限时必须整体拒绝，而不是处理到一半才发现。"""
    source = build_image_bytes(240, 240, "JPEG")
    # 额度只够一张图：4 张的总量是额度的 4 倍。
    # 但 content-length 远小于中间件的 1 MB 余量，所以拦住它的是服务层的整批上限。
    monkeypatch.setattr(settings, "MAX_BATCH_TOTAL_BYTES", len(source) * 2)

    response = post(
        client,
        CONVERT,
        [(f"img{index}.jpg", source) for index in range(4)],
        target_format="png",
    )
    assert response.status_code == 400
    assert "分批" in response.json()["error"]["message"]


# ----------------------------------------------------------------------
# ZIP 内容
# ----------------------------------------------------------------------

def test_zip_contains_every_result_and_nothing_else(client: TestClient) -> None:
    images = [
        ("one.jpg", build_image_bytes(300, 200, "JPEG")),
        ("two.jpg", build_image_bytes(200, 300, "JPEG")),
    ]
    response = post(client, CONVERT, images, target_format="jpg", quality_value="85")
    assert response.status_code == 200

    body = response.json()
    download = client.get(body["download_url"])
    assert download.status_code == 200

    with zipfile.ZipFile(io.BytesIO(download.content)) as archive:
        names = archive.namelist()
        assert sorted(names) == ["one.jpg", "two.jpg"]
        # ZIP 里不能混进上传的原始文件或中间产物
        assert not any(name.endswith(".upload") for name in names)

        for name in names:
            with archive.open(name) as fh:
                assert Image.open(io.BytesIO(fh.read())).format == "JPEG"

    assert archive_filename_matches(body["download_url"], download)


def archive_filename_matches(_url: str, download) -> bool:
    """下载响应必须带上可读的 ZIP 文件名。"""
    disposition = download.headers.get("content-disposition", "")
    return "converted_images" in disposition


def test_zip_names_are_flat_no_path_traversal(client: TestClient) -> None:
    """恶意文件名不能变成 ZIP 里的目录穿越路径。"""
    source = build_image_bytes(200, 200, "JPEG")
    images = [
        ("../../evil.jpg", source),
        ("..\\..\\windows.jpg", source),
        ("normal.jpg", source),
    ]
    response = post(client, CONVERT, images, target_format="png")
    assert response.status_code == 200, response.text

    download = client.get(response.json()["download_url"])
    with zipfile.ZipFile(io.BytesIO(download.content)) as archive:
        for info in archive.infolist():
            assert "/" not in info.filename
            assert "\\" not in info.filename
            assert not info.filename.startswith(".")


def test_resize_zip_uses_resized_names(client: TestClient) -> None:
    images = [
        ("a.png", build_image_bytes(400, 400, "PNG")),
        ("b.png", build_image_bytes(400, 400, "PNG")),
    ]
    response = post(client, RESIZE, images, width="200")
    assert response.status_code == 200

    body = response.json()
    assert body["archive_filename"] == "resized_images.zip"

    download = client.get(body["download_url"])
    with zipfile.ZipFile(io.BytesIO(download.content)) as archive:
        assert sorted(archive.namelist()) == ["a_resized.png", "b_resized.png"]


# ----------------------------------------------------------------------
# 一次性与清理
# ----------------------------------------------------------------------

def test_zip_download_is_one_time(client: TestClient) -> None:
    """ZIP 下载后令牌立即失效，不能重复下载。"""
    images = [
        ("a.jpg", build_image_bytes(300, 200, "JPEG")),
        ("b.jpg", build_image_bytes(300, 200, "JPEG")),
    ]
    body = post(client, CONVERT, images, target_format="png").json()

    assert client.get(body["download_url"]).status_code == 200
    assert client.get(body["download_url"]).status_code == 404


def test_preview_stops_working_after_download(client: TestClient) -> None:
    """下载后临时文件被删除，预览也应立即失效。"""
    images = [
        ("a.jpg", build_image_bytes(300, 200, "JPEG")),
        ("b.jpg", build_image_bytes(300, 200, "JPEG")),
    ]
    body = post(client, CONVERT, images, target_format="png").json()
    preview_url = body["items"][0]["preview_url"]

    assert client.get(preview_url).status_code == 200
    assert client.get(body["download_url"]).status_code == 200
    assert client.get(preview_url).status_code == 404


# ----------------------------------------------------------------------
# 预览的能力边界（第十阶段 C §五）
#
# 「这张结果能不能预览」原先按 MIME 前缀 ``image/*`` 判断，于是 TIFF 与 HEIC
# 也被算成能预览：响应里发出一个 ``preview_url``，接口也真的返回 200，
# 可浏览器渲染不了这两种格式 —— 用户看到的是结果卡上一张破图。
# 判据现在改成 ``registry.INLINE_PREVIEW_MEDIA_TYPES``（那张表同时也是统一
# 转换中心「发不发 preview_url」用的表），下面四条把这个边界钉住：
# 不能渲染的不发地址、能渲染的照发且真的能打开、直接访问不发地址的那张
# 得到一个干净的业务错误、以及两边用的是同一张表。
# ----------------------------------------------------------------------

def test_a_format_the_browser_cannot_render_gets_no_preview_url(client: TestClient) -> None:
    """TIFF 结果不给预览地址 —— 给了就是一个必定显示成破图的缩略图。

    尺寸调整接口不换格式（``PipelineOptions`` 里没有 ``target_format``），
    所以传一张 TIFF 进去，出来的还是 TIFF。
    """
    response = post(client, RESIZE, [("scan.tiff", build_image_bytes(300, 200, "TIFF"))], width="150")
    assert response.status_code == 200, response.text

    item = response.json()["items"][0]
    assert item["result"]["format"].lower() in ("tiff", "tif")
    assert item["preview_url"] is None


def test_a_renderable_format_still_gets_a_working_preview_url(client: TestClient) -> None:
    """能被浏览器渲染的格式照旧给地址，而且那个地址真的打得开。

    这条是上一条的反面：修边界不能顺手把能预览的也一起砍掉（§六十九）。
    """
    response = post(client, CONVERT, [("photo.jpg", build_image_bytes(300, 200, "JPEG"))],
                    target_format="png")
    assert response.status_code == 200, response.text

    item = response.json()["items"][0]
    assert item["result"]["format"].lower() == "png"
    assert item["preview_url"] is not None

    preview = client.get(item["preview_url"])
    assert preview.status_code == 200, preview.text
    assert Image.open(io.BytesIO(preview.content)).format == "PNG"


def test_hitting_the_preview_of_a_tiff_is_a_clean_business_error(client: TestClient) -> None:
    """硬敲一个不支持预览的地址，得到的是业务错误而不是 200 破图。

    状态码跟着这条路由既有的风格走（``ProcessingError`` → 422）：
    「这张结果没法预览」与「这张结果已经过期」是同一类失败 ——
    请求本身没问题，是它指向的那个东西给不出内容。
    """
    body = post(client, RESIZE, [("scan.tiff", build_image_bytes(300, 200, "TIFF"))],
                width="150").json()

    response = client.get(f"/api/preview/{body['job_id']}/0")
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["message"] == "该结果不支持预览"
    assert error["code"] == "PROCESSING_FAILED"


def test_both_sides_of_the_preview_boundary_use_the_same_table() -> None:
    """发地址的那边与放行的那边必须是同一张表。

    任一边被单独改动，结果都是「发了地址但访问报错」或者「能访问但没发地址」。
    这里机械地对账一次，免得将来有人只改一处。
    """
    import routers.download as download_module
    import routers.schemas as schemas_module
    from conversion.registry import INLINE_PREVIEW_MEDIA_TYPES
    from routers.schemas import ItemResponse
    from services.intake import media_type_for

    # 两个模块引用的是同一个对象，不是各抄一份内容相同的字面量。
    assert schemas_module.INLINE_PREVIEW_MEDIA_TYPES is download_module.INLINE_PREVIEW_MEDIA_TYPES
    assert schemas_module.INLINE_PREVIEW_MEDIA_TYPES is INLINE_PREVIEW_MEDIA_TYPES

    # 而「哪些格式能预览」与这张表确实对得上：能渲染的格式的 MIME 在里面，
    # 渲染不了的（TIFF / HEIC / ICO）不在里面。
    for fmt in ("jpeg", "png", "webp", "bmp", "gif"):
        assert media_type_for(fmt) in INLINE_PREVIEW_MEDIA_TYPES, fmt
    for fmt in ("tiff", "heif", "ico"):
        assert media_type_for(fmt) not in INLINE_PREVIEW_MEDIA_TYPES, fmt

    # 字段本身是可空的：类型上就允许 null，前端才不必猜。
    assert ItemResponse.model_fields["preview_url"].annotation == (str | None)


def test_the_supported_formats_sentence_matches_the_whitelist() -> None:
    """「只支持 … 格式的图片」这句话里点的格式，必须与白名单**完全一致**。

    这句话原先手抄成「只支持 JPG / PNG / WEBP 格式的图片」，白名单却早就
    扩到十种：用户传一个合法的 ``.tiff`` 被拒时，会被告知一个与他无关的
    更小集合。现在两边同源，这条测试把「同源」钉死 —— 双向相等，
    多一种少一种都算漂移。
    """
    from tasks.image_tasks import INVALID_TYPE_MESSAGE

    mentioned = set(re.findall(r"[A-Z0-9]+", INVALID_TYPE_MESSAGE))
    allowed = {extension.lstrip(".").upper() for extension in settings.ALLOWED_IMAGE_EXTENSIONS}

    assert mentioned == allowed, (sorted(mentioned), sorted(allowed))
    # 顺带确认这句话不是空壳
    assert INVALID_TYPE_MESSAGE.startswith("只支持 ") and INVALID_TYPE_MESSAGE.endswith("格式的图片")


def test_batch_temp_dirs_are_removed_after_download(client: TestClient) -> None:
    """处理完成后临时目录必须被删除，不留任何用户文件。"""
    before = temp_dirs_under(Path(settings.TEMP_ROOT) if settings.TEMP_ROOT else _system_temp())

    images = [
        ("a.jpg", build_image_bytes(300, 200, "JPEG")),
        ("b.jpg", build_image_bytes(300, 200, "JPEG")),
    ]
    body = post(client, CONVERT, images, target_format="png").json()

    during = temp_dirs_under(Path(settings.TEMP_ROOT) if settings.TEMP_ROOT else _system_temp())
    assert len(during) > len(before), "处理期间应存在任务临时目录"

    assert client.get(body["download_url"]).status_code == 200

    after = temp_dirs_under(Path(settings.TEMP_ROOT) if settings.TEMP_ROOT else _system_temp())
    assert len(after) == len(before), f"下载后仍残留临时目录：{after - before}"


def test_all_broken_batch_leaves_no_temp_dir(client: TestClient) -> None:
    """整批失败时也不能残留临时目录。"""
    temp_root = Path(settings.TEMP_ROOT) if settings.TEMP_ROOT else _system_temp()
    before = temp_dirs_under(temp_root)

    response = post(
        client,
        CONVERT,
        [("bad1.jpg", b"nope" * 40), ("bad2.jpg", b"nope" * 40)],
        target_format="png",
    )
    assert response.status_code in (400, 415)

    after = temp_dirs_under(temp_root)
    assert len(after) == len(before), f"失败后残留临时目录：{after - before}"


def test_job_store_does_not_grow_on_failure(client: TestClient) -> None:
    """失败的请求不应在登记簿里留下条目。"""
    before = job_store.count()
    post(client, CONVERT, [("bad.jpg", b"nope" * 40)], target_format="png")
    assert job_store.count() == before


def _system_temp() -> Path:
    import tempfile

    return Path(tempfile.gettempdir())


# ----------------------------------------------------------------------
# 公开配置
# ----------------------------------------------------------------------

def test_public_config_exposes_batch_limits(client: TestClient) -> None:
    """前端需要知道批量上限，避免两边写死不一致。"""
    config = client.get("/api/config").json()

    assert config["max_batch_files"] == settings.MAX_BATCH_FILES
    assert config["max_batch_total_bytes"] == settings.MAX_BATCH_TOTAL_BYTES
    assert config["default_quality_value"] == settings.DEFAULT_QUALITY_VALUE
    assert config["output_formats"] == ["jpg", "png", "webp"]


def test_preview_rejects_invalid_index(client: TestClient) -> None:
    """不存在的序号返回 422；序号不是整数时由统一的参数校验拦下（400）。"""
    images = [("a.jpg", build_image_bytes(200, 200, "JPEG"))] * 2
    body = post(client, CONVERT, images, target_format="png").json()

    assert client.get(f"/api/preview/{body['job_id']}/99").status_code == 422
    assert client.get(f"/api/preview/{body['job_id']}/-1").status_code == 422

    malformed = client.get(f"/api/preview/{body['job_id']}/abc")
    assert malformed.status_code == 400
    assert malformed.json()["error"]["code"] == "INVALID_REQUEST"
