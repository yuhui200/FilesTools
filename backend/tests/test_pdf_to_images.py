"""PDF → 图片的端到端测试。

对应第三阶段 §17 的「单页 PDF 转图片」「多页 PDF 转图片」「批量下载 ZIP」
「损坏 PDF」「非法文件格式」几项。

第四阶段起这个接口也是异步任务：提交后返回任务号，处理在后台队列里进行，
``run_task`` 把「提交 + 轮询」合成一次调用。
"""

from __future__ import annotations

import io
import zipfile

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from tests.conftest import build_pdf_bytes, pdf_files, run_task, submit_task, wait_for_task

UPLOAD = "/api/pdf/upload"
TO_IMAGES = "/api/pdf/to-images"


def upload(client: TestClient, name: str, data: bytes) -> dict:
    response = client.post(UPLOAD, files=pdf_files((name, data), field="file"))
    assert response.status_code == 200, response.text
    return response.json()


def to_images(client: TestClient, input_id: str | list[str], **options: str):
    """提交转图片任务并等到结束。"""
    ids = [input_id] if isinstance(input_id, str) else input_id
    return run_task(client, TO_IMAGES, data={"input_ids": ids, **options})


def download_zip(client: TestClient, url: str) -> zipfile.ZipFile:
    response = client.get(url)
    assert response.status_code == 200, response.text
    return zipfile.ZipFile(io.BytesIO(response.content))


def download_image(client: TestClient, url: str) -> Image.Image:
    response = client.get(url)
    assert response.status_code == 200, response.text
    return Image.open(io.BytesIO(response.content))


# ----------------------------------------------------------------------
# 上传
# ----------------------------------------------------------------------

def test_upload_reports_name_size_and_pages(client: TestClient) -> None:
    """上传后要能拿到文件名、大小和页数，前端才有的显示。"""
    data = build_pdf_bytes(4)
    body = upload(client, "季度 报告.pdf", data)

    assert body["filename"] == "季度 报告.pdf"
    assert body["size"] == len(data)
    assert body["page_count"] == 4
    assert body["input_id"]
    assert body["thumbnail_base"] == f"/api/pdf/input/{body['input_id']}/thumb"


def test_upload_rejects_non_pdf(client: TestClient) -> None:
    """Word 文档改名成 .pdf 也要被认出来。"""
    response = client.post(
        UPLOAD,
        files=[("file", ("fake.pdf", io.BytesIO(b"PK\x03\x04 not a pdf"), "application/pdf"))],
    )
    assert response.status_code == 415
    assert "PDF" in response.json()["error"]["message"]


def test_upload_rejects_wrong_extension(client: TestClient) -> None:
    response = client.post(
        UPLOAD,
        files=[("file", ("report.txt", io.BytesIO(build_pdf_bytes(1)), "text/plain"))],
    )
    assert response.status_code == 415


def test_upload_rejects_broken_pdf(client: TestClient) -> None:
    """文件头正确但内容损坏，要提示「损坏」而不是「格式不支持」。"""
    broken = b"%PDF-1.7\n" + b"\x00" * 200
    response = client.post(
        UPLOAD, files=[("file", ("broken.pdf", io.BytesIO(broken), "application/pdf"))]
    )
    assert response.status_code == 400
    assert "损坏" in response.json()["error"]["message"]


def test_upload_rejects_empty_file(client: TestClient) -> None:
    response = client.post(
        UPLOAD, files=[("file", ("empty.pdf", io.BytesIO(b""), "application/pdf"))]
    )
    assert response.status_code == 400


def test_upload_is_not_downloadable(client: TestClient) -> None:
    """输入令牌不是下载令牌，不能拿它取回原文件。"""
    body = upload(client, "a.pdf", build_pdf_bytes(1))
    assert client.get(f"/api/download/{body['input_id']}").status_code == 404


# ----------------------------------------------------------------------
# 缩略图
# ----------------------------------------------------------------------

def test_thumbnail_is_png_and_small(client: TestClient) -> None:
    """缩略图要真的能渲染出来，而且比整页渲染小得多。"""
    body = upload(client, "a.pdf", build_pdf_bytes(3))
    response = client.get(f"{body['thumbnail_base']}/0")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"

    image = Image.open(io.BytesIO(response.content))
    assert image.format == "PNG"
    assert image.width == 220  # PDF_THUMBNAIL_WIDTH


def test_thumbnail_pages_differ(client: TestClient) -> None:
    """不同页的缩略图内容必须不同，否则等于没渲染。"""
    body = upload(client, "a.pdf", build_pdf_bytes(3))
    first = client.get(f"{body['thumbnail_base']}/0").content
    second = client.get(f"{body['thumbnail_base']}/2").content
    assert first != second


def test_thumbnail_out_of_range(client: TestClient) -> None:
    body = upload(client, "a.pdf", build_pdf_bytes(3))
    response = client.get(f"{body['thumbnail_base']}/7")
    assert response.status_code == 400
    assert "共 3 页" in response.json()["error"]["message"]


def test_thumbnail_unknown_input(client: TestClient) -> None:
    response = client.get("/api/pdf/input/deadbeef/thumb/0")
    assert response.status_code == 404


# ----------------------------------------------------------------------
# 转图片：单页 / 多页
# ----------------------------------------------------------------------

def test_single_page_pdf_returns_one_image(client: TestClient) -> None:
    """单页 PDF 只导出一张图，不套 ZIP。"""
    body = upload(client, "one.pdf", build_pdf_bytes(1))
    response = to_images(client, body["input_id"], target_format="png", pages="all")
    assert response.status_code == 200, response.text

    result = response.json()
    assert result["archived"] is False
    assert result["filename"] == "page-01.png"
    assert len(result["files"]) == 1

    image = download_image(client, result["download_url"])
    assert image.format == "PNG"


def test_multi_page_pdf_is_zipped(client: TestClient) -> None:
    """多页 PDF 导出的图片自动打包成 pdf_pages.zip。"""
    body = upload(client, "many.pdf", build_pdf_bytes(5))
    response = to_images(client, body["input_id"], target_format="png", pages="all")
    assert response.status_code == 200, response.text

    result = response.json()
    assert result["archived"] is True
    assert result["filename"] == "pdf_pages.zip"
    # files 描述的是 ZIP 里的内容，而不是 ZIP 本身
    assert [item["filename"] for item in result["files"]] == [
        f"page-{n:02d}.png" for n in range(1, 6)
    ]

    archive = download_zip(client, result["download_url"])
    assert archive.namelist() == [f"page-{n:02d}.png" for n in range(1, 6)]
    assert archive.testzip() is None


def test_zip_contains_valid_images(client: TestClient) -> None:
    """ZIP 里的每一张都必须是能打开的图片。"""
    body = upload(client, "many.pdf", build_pdf_bytes(3))
    result = to_images(client, body["input_id"], target_format="jpg", pages="all").json()
    archive = download_zip(client, result["download_url"])

    for name in archive.namelist():
        with Image.open(io.BytesIO(archive.read(name))) as image:
            assert image.format == "JPEG"
            assert image.width > 100


def test_thirty_page_pdf_batch(client: TestClient) -> None:
    """§11 的批量场景：30 页 PDF 一次导出成 ZIP。"""
    body = upload(client, "document.pdf", build_pdf_bytes(30))
    assert body["page_count"] == 30

    response = to_images(
        client, body["input_id"], target_format="jpg", pages="all", quality="60"
    )
    assert response.status_code == 200, response.text

    result = response.json()
    assert result["archived"] is True
    assert result["original_pages"] == 30

    archive = download_zip(client, result["download_url"])
    names = archive.namelist()
    assert len(names) == 30
    # 补零后字典序 = 页序，解压出来不会乱
    assert names == sorted(names)
    assert names[0] == "page-01.jpg"
    assert names[-1] == "page-30.jpg"
    assert archive.testzip() is None


# ----------------------------------------------------------------------
# 转图片：格式与质量
# ----------------------------------------------------------------------

@pytest.mark.parametrize(
    ("fmt", "pillow_format", "extension"),
    [("jpg", "JPEG", ".jpg"), ("png", "PNG", ".png"), ("webp", "WEBP", ".webp")],
)
def test_export_formats(
    client: TestClient, fmt: str, pillow_format: str, extension: str
) -> None:
    body = upload(client, "a.pdf", build_pdf_bytes(1))
    result = to_images(client, body["input_id"], target_format=fmt, pages="1").json()
    assert result["filename"] == f"page-01{extension}"

    image = download_image(client, result["download_url"])
    assert image.format == pillow_format


@pytest.mark.parametrize("fmt", ["JPG", "JPEG", "PNG", "WebP", "png"])
def test_format_names_are_case_insensitive(client: TestClient, fmt: str) -> None:
    body = upload(client, "a.pdf", build_pdf_bytes(1))
    response = to_images(client, body["input_id"], target_format=fmt, pages="1")
    assert response.status_code == 200, response.text


def test_unsupported_format_is_rejected(client: TestClient) -> None:
    body = upload(client, "a.pdf", build_pdf_bytes(1))
    response = to_images(client, body["input_id"], target_format="gif", pages="1")
    assert response.status_code == 400
    assert "JPG" in response.json()["error"]["message"]


def test_quality_changes_file_size(client: TestClient) -> None:
    """质量参数必须真的改变输出体积。"""
    body = upload(client, "a.pdf", build_pdf_bytes(1))

    def size(quality: str) -> int:
        result = to_images(
            client, body["input_id"], target_format="jpg", pages="1", quality=quality
        ).json()
        return result["size"]

    assert size("20") < size("95")


def test_quality_is_ignored_for_png(client: TestClient) -> None:
    """PNG 是无损格式：前端不显示质量选项，后端也不因为质量报错。"""
    body = upload(client, "a.pdf", build_pdf_bytes(1))
    response = to_images(
        client, body["input_id"], target_format="png", pages="1", quality="10"
    )
    assert response.status_code == 200, response.text


@pytest.mark.parametrize("quality", ["0", "101", "abc"])
def test_invalid_quality_is_rejected_for_lossy_formats(
    client: TestClient, quality: str
) -> None:
    body = upload(client, "a.pdf", build_pdf_bytes(1))
    response = to_images(
        client, body["input_id"], target_format="jpg", pages="1", quality=quality
    )
    assert response.status_code == 400
    assert "质量" in response.json()["error"]["message"]


@pytest.mark.parametrize(
    ("preset", "dpi"),
    [("standard", 96), ("high", 150), ("ultra", 300)],
)
def test_resolution_presets(client: TestClient, preset: str, dpi: int) -> None:
    """清晰度档位要真的改变像素尺寸。"""
    body = upload(client, "a.pdf", build_pdf_bytes(1))
    result = to_images(
        client, body["input_id"], target_format="png", pages="1", resolution=preset
    ).json()

    image = download_image(client, result["download_url"])
    expected_width = round(595.276 * dpi / 72)
    assert image.width == pytest.approx(expected_width, abs=1)


def test_invalid_resolution_is_rejected(client: TestClient) -> None:
    body = upload(client, "a.pdf", build_pdf_bytes(1))
    response = to_images(client, body["input_id"], pages="1", resolution="999")
    assert response.status_code == 400
    assert "DPI" in response.json()["error"]["message"]


# ----------------------------------------------------------------------
# 页面范围
# ----------------------------------------------------------------------

@pytest.mark.parametrize(
    ("pages", "expected"),
    [
        ("1-3", ["page-01.png", "page-02.png", "page-03.png"]),
        ("1,3,5", ["page-01.png", "page-03.png", "page-05.png"]),
        ("2-6", ["page-02.png", "page-03.png", "page-04.png", "page-05.png", "page-06.png"]),
        ("all", [f"page-{n:02d}.png" for n in range(1, 7)]),
        ("", [f"page-{n:02d}.png" for n in range(1, 7)]),
    ],
)
def test_page_ranges(client: TestClient, pages: str, expected: list[str]) -> None:
    """§4 的四种范围写法都要选出正确的页。"""
    body = upload(client, "six.pdf", build_pdf_bytes(6))
    result = to_images(
        client, body["input_id"], target_format="png", pages=pages
    ).json()

    if len(expected) == 1:
        assert result["filename"] == expected[0]
    else:
        archive = download_zip(client, result["download_url"])
        assert archive.namelist() == expected


def test_selected_pages_render_their_own_content(client: TestClient) -> None:
    """选第 3 页导出的图，必须真的是第 3 页，而不是第一页。"""
    body = upload(client, "six.pdf", build_pdf_bytes(6))

    def render(page: str) -> bytes:
        result = to_images(client, body["input_id"], target_format="png", pages=page).json()
        return client.get(result["download_url"]).content

    assert render("1") != render("3")
    assert render("3") == render("3")


@pytest.mark.parametrize(
    ("pages", "keyword"),
    [
        ("99", "共 3 页"),
        ("0", "从 1 开始"),
        ("abc", "格式不正确"),
        ("3-1", "起始页不能大于结束页"),
        ("1,99", "共 3 页"),
    ],
)
def test_invalid_page_ranges(client: TestClient, pages: str, keyword: str) -> None:
    body = upload(client, "three.pdf", build_pdf_bytes(3))
    response = to_images(client, body["input_id"], pages=pages)
    assert response.status_code == 400
    assert keyword in response.json()["error"]["message"]


def test_export_page_limit(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """超过单次导出上限时明确拒绝，而不是硬跑到超时。"""
    from config import settings

    body = upload(client, "six.pdf", build_pdf_bytes(6))
    monkeypatch.setattr(settings, "PDF_EXPORT_MAX_PAGES", 3)

    response = to_images(client, body["input_id"], pages="all")
    assert response.status_code == 400
    assert "最多导出 3 页" in response.json()["error"]["message"]

    # 上限之内仍然正常
    assert to_images(client, body["input_id"], pages="1-3").status_code == 200


# ----------------------------------------------------------------------
# 令牌与清理
# ----------------------------------------------------------------------

def test_result_is_one_time_download(client: TestClient) -> None:
    body = upload(client, "a.pdf", build_pdf_bytes(1))
    url = to_images(client, body["input_id"], pages="1").json()["download_url"]

    assert client.get(url).status_code == 200
    assert client.get(url).status_code == 404


def test_input_survives_multiple_operations(client: TestClient) -> None:
    """同一份 PDF 可以连续做多个操作，不必每一步都重传。"""
    body = upload(client, "a.pdf", build_pdf_bytes(4))
    for pages in ("1", "2", "3-4"):
        response = to_images(client, body["input_id"], pages=pages)
        assert response.status_code == 200, response.text


def test_preview_url_works_for_images(client: TestClient) -> None:
    """图片结果的预览地址要能直接用，且不消耗下载令牌。"""
    body = upload(client, "a.pdf", build_pdf_bytes(2))
    result = to_images(client, body["input_id"], target_format="png", pages="all").json()

    preview = result["files"][0]["preview_url"]
    assert preview
    assert client.get(preview).status_code == 200
    # 预览之后仍然可以下载
    assert client.get(result["download_url"]).status_code == 200


def test_result_files_are_cleaned_up_after_download(client: TestClient) -> None:
    """下载后结果目录要立刻清掉；输入文件保留，因为用户可能接着做别的操作。"""
    import tempfile
    from pathlib import Path

    body = upload(client, "a.pdf", build_pdf_bytes(2))
    before = set(Path(tempfile.gettempdir()).glob("filetools_*"))

    result = to_images(client, body["input_id"], pages="all").json()
    client.get(result["download_url"])

    leftovers = set(Path(tempfile.gettempdir()).glob("filetools_*")) - before
    assert leftovers == set()

    # 输入还在，同一个 input_id 可以继续用
    assert to_images(client, body["input_id"], pages="1").status_code == 200


def test_response_shape_is_stable(client: TestClient) -> None:
    body = upload(client, "a.pdf", build_pdf_bytes(1))
    result = to_images(client, body["input_id"], pages="1").json()
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
        assert key in result, key
    assert result["original_pages"] == 1
    assert result["original_size"] > 0


# ----------------------------------------------------------------------
# 批量（第四阶段 §2）
# ----------------------------------------------------------------------

def test_multiple_pdfs_in_one_task(client: TestClient) -> None:
    """一次提交多份 PDF：各份的结果合在一个 ZIP 里，页序不乱。"""
    first = upload(client, "报告A.pdf", build_pdf_bytes(1))
    second = upload(client, "报告B.pdf", build_pdf_bytes(2))

    response = submit_task(
        client,
        TO_IMAGES,
        data={
            "input_ids": [first["input_id"], second["input_id"]],
            "target_format": "png",
            "pages": "all",
        },
    )
    assert response.status_code == 202
    assert response.json()["total"] == 2

    snapshot = wait_for_task(client, response.json()["group_id"])
    assert snapshot["state"] == "done"
    assert snapshot["completed"] == 2

    result = snapshot["result"]
    assert result["archived"] is True
    assert result["archive_filename"] == "pdf_pages.zip"
    # 多份 PDF 时文件名带来源前缀，否则 ZIP 里的 page-01 会互相覆盖
    assert [item["filename"] for item in result["files"]] == [
        "报告A-page-01.png",
        "报告B-page-01.png",
        "报告B-page-02.png",
    ]

    archive = download_zip(client, result["download_url"])
    assert archive.namelist() == [item["filename"] for item in result["files"]]
    assert archive.testzip() is None

    # 每份 PDF 的处理结果都要单独可见
    assert [task["result"]["pages"] for task in snapshot["tasks"]] == [1, 2]


def test_two_pdfs_with_the_same_name_do_not_overwrite_each_other(
    client: TestClient,
) -> None:
    """两份同名的 PDF：ZIP 内不能出现同名成员，否则后写的静默覆盖前一份。

    用户从两个文件夹各拖一份 ``报告.pdf`` 进来是常事。前缀原先直接取来源
    文件名，两份都得到 ``报告-``，加上页面名也一样 —— ZIP 里就有了两个
    ``报告-page-01.png``。``zipfile`` 写重复成员**不报错**，解压时后一份
    覆盖前一份：用户少拿到一份，界面上却显示两份都成功（第十阶段 C §十二）。

    前缀现在走的是 ``services.batch_service.unique_name``，也就是图片那条路
    早就在用的那条规则。这条测试钉住的是「两份都在、名字互不相同」。
    """
    first = upload(client, "报告.pdf", build_pdf_bytes(1))
    second = upload(client, "报告.pdf", build_pdf_bytes(2))

    response = submit_task(
        client,
        TO_IMAGES,
        data={
            "input_ids": [first["input_id"], second["input_id"]],
            "target_format": "png",
            "pages": "all",
        },
    )
    assert response.status_code == 202

    snapshot = wait_for_task(client, response.json()["group_id"])
    assert snapshot["state"] == "done"
    assert snapshot["completed"] == 2

    result = snapshot["result"]
    names = [item["filename"] for item in result["files"]]

    # 三页都在：一份 1 页 + 一份 2 页。少了就说明有成员被覆盖掉了。
    assert len(names) == 3
    assert len(set(names)) == len(names), f"ZIP 内出现同名成员：{names}"
    assert names == ["报告-page-01.png", "报告-2-page-01.png", "报告-2-page-02.png"]

    archive = download_zip(client, result["download_url"])
    assert archive.namelist() == names
    assert archive.testzip() is None
    # 每个成员都得是**真实可打开**的图片，而不是覆盖后剩下的空壳
    for name in archive.namelist():
        image = Image.open(io.BytesIO(archive.read(name)))
        assert image.format == "PNG"


def test_one_invalid_input_does_not_break_the_batch(client: TestClient) -> None:
    """其中一份 PDF 的凭据已失效时，另一份照常导出。"""
    good = upload(client, "good.pdf", build_pdf_bytes(2))

    response = submit_task(
        client,
        TO_IMAGES,
        data={
            "input_ids": ["deadbeefdeadbeefdeadbeefdeadbeef", good["input_id"]],
            "target_format": "png",
            "pages": "all",
        },
    )
    snapshot = wait_for_task(client, response.json()["group_id"])

    assert snapshot["state"] == "done"
    assert snapshot["completed"] == 1
    assert snapshot["failed"] == 1

    failed = next(task for task in snapshot["tasks"] if task["state"] == "failed")
    assert failed["error_code"] == "JOB_NOT_FOUND"
    assert "重新上传" in failed["error_message"]

    # 前缀按「提交了几份」决定，而不是「成功了几份」：
    # 提交多份时一律带来源前缀，用户看到的命名规则才是稳定的
    archive = download_zip(client, snapshot["result"]["download_url"])
    assert archive.namelist() == ["good-page-01.png", "good-page-02.png"]


def test_batch_rejects_too_many_pdfs(client: TestClient) -> None:
    """超过单批数量上限必须拒绝。"""
    from config import settings

    body = upload(client, "a.pdf", build_pdf_bytes(1))
    response = client.post(
        TO_IMAGES,
        data={
            "input_ids": [body["input_id"]] * (settings.MAX_BATCH_FILES + 1),
            "pages": "1",
        },
    )
    assert response.status_code == 400
    assert str(settings.MAX_BATCH_FILES) in response.json()["error"]["message"]
