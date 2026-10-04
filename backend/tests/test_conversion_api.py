"""统一转换中心的单文件全链路（第七阶段 §二十八 / §三十八 / §四十二）。

规格在这里的要求很具体：**测试不能只检查 HTTP 200**。
每一个分支都要把产物打开来看：

* DOCX → 标准库解包 + ``word/document.xml`` + 文字真的在里面；
* PDF → PyMuPDF 打开，页数 ≥ 1；
* 图片 → Pillow 验 ``format`` / ``width`` / ``height``。

外加两条安全要求：改名的文件不能靠扩展名蒙混过关（§三十七），
任何错误响应里都不能出现堆栈或服务器路径（§四十三）。
"""

from __future__ import annotations

import io
import tempfile
import zipfile

import pymupdf
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from config import settings
from conversion import registry
from utils.errors import ErrorCode
from tests.conftest import (
    build_docx_bytes,
    build_image_bytes,
    build_labeled_pdf,
    build_pptx_bytes,
    build_text_pdf,
    build_xlsx_bytes,
    conversion_task,
    docx_pages,
    image_files,
    office_files,
    pdf_files,
    requires_ocr,
    requires_soffice,
    run_conversion,
    submit_conversion,
    wait_conversion,
)

ENDPOINT = "/api/conversion/tasks"

#: 出错时绝不能出现在响应里的东西（与第五、六阶段同一份清单）
FORBIDDEN_FRAGMENTS = (
    "Traceback",
    'File "',
    "\\",
    "/tmp/",
    "AppData",
    "site-packages",
    "filetools_",
    "rapidocr",
    "onnxruntime",
    "soffice",
)


def assert_clean(snapshot: dict) -> None:
    """整份快照里不能出现内部细节 —— 失败原因也是给用户看的。"""
    raw = str(snapshot)
    for fragment in FORBIDDEN_FRAGMENTS:
        assert fragment not in raw, f"响应里泄露了 {fragment!r}"


def item_of(snapshot: dict, index: int = 0) -> dict:
    return conversion_task(snapshot, index)


# ----------------------------------------------------------------------
# 提交响应（§二十八）
# ----------------------------------------------------------------------

def test_submit_returns_task_ids(client: TestClient) -> None:
    png = build_image_bytes(120, 90, "PNG")
    response = submit_conversion(
        client, files=image_files(("a.png", png), ("b.png", png)), target_type="jpg"
    )

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["total"] == 2
    assert [task["index"] for task in body["tasks"]] == [0, 1]
    assert [task["filename"] for task in body["tasks"]] == ["a.png", "b.png"]
    assert [task["status"] for task in body["tasks"]] == ["queued", "queued"]
    # task_id 必须能定位到「哪一批的哪一项」，重试接口只靠它
    batch_id = body["batch_id"]
    assert [task["task_id"] for task in body["tasks"]] == [
        f"{batch_id}:0",
        f"{batch_id}:1",
    ]
    assert body["status_url"] == f"{ENDPOINT}/{batch_id}"
    assert body["progress_url"] == f"{ENDPOINT}/{batch_id}/progress"
    assert body["cancel_url"] == f"{ENDPOINT}/{batch_id}/cancel"

    wait_conversion(client, batch_id)


def test_unknown_batch_is_404(client: TestClient) -> None:
    response = client.get(f"{ENDPOINT}/does-not-exist")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == ErrorCode.TASK_NOT_FOUND


def test_lightweight_progress_matches_the_full_snapshot(client: TestClient) -> None:
    png = build_image_bytes(120, 90, "PNG")
    response = submit_conversion(
        client, files=image_files(("a.png", png)), target_type="jpg"
    )
    batch_id = response.json()["batch_id"]
    snapshot = wait_conversion(client, batch_id)

    progress = client.get(f"{ENDPOINT}/{batch_id}/progress")
    assert progress.status_code == 200
    body = progress.json()

    assert body["batch_id"] == batch_id
    assert body["status"] == snapshot["status"]
    assert body["progress"] == snapshot["progress"]
    for key in ("total", "queued", "processing", "completed", "failed", "cancelled"):
        assert body[key] == snapshot[key], key


# ----------------------------------------------------------------------
# 四个分支：产物必须真的能打开
# ----------------------------------------------------------------------

@requires_ocr
def test_pdf_to_docx_produces_a_real_document(client: TestClient) -> None:
    """PDF → Word：DOCX 必须真的能打开，而且文字真的在里面。

    挂 ``requires_ocr`` 是因为 ``build_labeled_pdf`` 每页只写一个短标签
    （``one-1`` ＝ 5 个字符），低于 ``PDF_TO_WORD_MIN_TEXT_CHARS``（默认 16），
    ``detect_page_kind`` 会**如实**把这一页判成扫描页，于是真的走 OCR。
    这是既有覆盖，不是新加的依赖 —— 文字层那条路径由
    ``test_pdf_to_word_api.py`` 里用长文本的用例负责。
    """
    pdf = build_labeled_pdf("one", 2)
    snapshot = run_conversion(
        client,
        files=pdf_files(("two-pages.pdf", pdf), field="files"),
        target_type="docx",
    )

    assert snapshot["status"] == "completed"
    task = item_of(snapshot)
    assert task["status"] == "completed"
    assert task["source_type"] == registry.SOURCE_PDF
    assert task["target_type"] == registry.SOURCE_DOCX
    assert task["result"]["filename"] == "two-pages.docx"
    assert task["result"]["media_type"].endswith("wordprocessingml.document")

    response = client.get(task["result"]["download_url"])
    assert response.status_code == 200
    data = response.content

    # 第一层：只用标准库确认它是一份完好的 DOCX
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        assert archive.testzip() is None
        names = set(archive.namelist())
        for part in ("[Content_Types].xml", "_rels/.rels", "word/document.xml"):
            assert part in names, f"DOCX 缺少必需条目 {part}"
        assert "<w:document" in archive.read("word/document.xml").decode("utf-8")

    # 第二层：文字真的转过去了
    pages = docx_pages(data)
    assert len(pages) == 2
    assert "one-1" in pages[0]
    assert "one-2" in pages[1]

    # 下载是一次性的
    assert client.get(task["result"]["download_url"]).status_code == 404


@requires_soffice
def test_docx_to_pdf_produces_a_real_pdf(client: TestClient) -> None:
    snapshot = run_conversion(
        client,
        files=office_files(("report.docx", build_docx_bytes("HELLO")), field="files"),
        target_type="pdf",
    )

    assert snapshot["status"] == "completed"
    task = item_of(snapshot)
    assert task["source_type"] == registry.SOURCE_DOCX
    assert task["result"]["filename"] == "report.pdf"
    assert task["result"]["media_type"] == "application/pdf"
    assert task["result"]["page_count"] >= 1

    response = client.get(task["result"]["download_url"])
    assert response.status_code == 200
    assert response.content.startswith(b"%PDF")
    with pymupdf.open(stream=response.content, filetype="pdf") as doc:
        assert doc.page_count >= 1
        assert "HELLO" in "".join(page.get_text() for page in doc)


@requires_soffice
def test_xlsx_and_pptx_to_pdf(client: TestClient) -> None:
    """Excel 与 PowerPoint 走的是同一条 LibreOffice 链路，但输入校验不同。"""
    xlsx = run_conversion(
        client,
        files=office_files(("sheet.xlsx", build_xlsx_bytes("CELL")), field="files"),
        target_type="pdf",
    )
    assert xlsx["status"] == "completed"
    assert item_of(xlsx)["result"]["page_count"] >= 1

    pptx = run_conversion(
        client,
        files=office_files(("deck.pptx", build_pptx_bytes("SLIDE")), field="files"),
        target_type="pdf",
    )
    assert pptx["status"] == "completed"
    assert item_of(pptx)["result"]["page_count"] >= 1


def test_txt_to_pdf_does_not_need_libreoffice(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TXT 走 PyMuPDF 排版。把它绑死在 LibreOffice 上会白白少一个功能。"""
    from services import conversion_service

    monkeypatch.setattr(conversion_service, "office_available", lambda: False)

    snapshot = run_conversion(
        client,
        files=office_files(("notes.txt", "第一行\n第二行\n".encode()), field="files"),
        target_type="pdf",
    )

    assert snapshot["status"] == "completed", snapshot["error"]
    task = item_of(snapshot)
    assert task["source_type"] == registry.SOURCE_TXT

    response = client.get(task["result"]["download_url"])
    with pymupdf.open(stream=response.content, filetype="pdf") as doc:
        assert doc.page_count >= 1
        text = "".join(page.get_text() for page in doc)
    assert "第一行" in text
    assert "第二行" in text


def test_png_to_webp_and_jpg_produce_real_images(client: TestClient) -> None:
    png = build_image_bytes(320, 240, "PNG")

    for target, expected_format in (("webp", "WEBP"), ("jpg", "JPEG")):
        snapshot = run_conversion(
            client, files=image_files(("photo.png", png)), target_type=target
        )
        assert snapshot["status"] == "completed", snapshot["error"]
        task = item_of(snapshot)
        assert task["source_type"] == registry.SOURCE_PNG
        assert task["result"]["filename"] == f"photo.{target}"

        response = client.get(task["result"]["download_url"])
        assert response.status_code == 200
        with Image.open(io.BytesIO(response.content)) as image:
            assert image.format == expected_format
            assert image.size == (320, 240)
            image.verify()

        # 结果摘要里的宽高也得是真的（前端直接展示这两个数）
        assert task["result"]["width"] == 320
        assert task["result"]["height"] == 240


def test_image_to_pdf_makes_one_pdf_per_image(client: TestClient) -> None:
    """图片 → PDF 的语义是「每张各出一份」，不是一个批次合成一本。"""
    snapshot = run_conversion(
        client,
        files=image_files(
            ("a.png", build_image_bytes(200, 150, "PNG")),
            ("b.png", build_image_bytes(300, 100, "PNG")),
        ),
        target_type="pdf",
    )

    assert snapshot["status"] == "completed"
    assert snapshot["completed"] == 2
    assert [task["result"]["filename"] for task in snapshot["tasks"]] == [
        "a.pdf",
        "b.pdf",
    ]

    # 两张图尺寸不同 → 两份 PDF 的页面尺寸也必须各随其图
    sizes = []
    for index in (0, 1):
        url = item_of(snapshot, index)["result"]["download_url"]
        with pymupdf.open(stream=client.get(url).content, filetype="pdf") as doc:
            assert doc.page_count == 1
            sizes.append(tuple(round(value) for value in doc[0].rect[2:]))
    assert sizes[0] != sizes[1], "两份 PDF 的页面尺寸不该一样，说明真的各转各的"


# ----------------------------------------------------------------------
# 参数真的生效（不是收了不用）
# ----------------------------------------------------------------------

def test_resize_option_is_applied(client: TestClient) -> None:
    snapshot = run_conversion(
        client,
        files=image_files(("photo.png", build_image_bytes(400, 300, "PNG"))),
        target_type="jpg",
        width="100",
    )

    task = item_of(snapshot)
    assert snapshot["status"] == "completed", snapshot["error"]
    assert task["result"]["width"] == 100
    # 只给了一条边，默认保持比例
    assert task["result"]["height"] == 75

    response = client.get(task["result"]["download_url"])
    with Image.open(io.BytesIO(response.content)) as image:
        assert image.size == (100, 75)


def test_quality_option_is_applied(client: TestClient) -> None:
    """质量档位要真的传下去：高质量档的文件必须比高压缩档大。"""
    png = build_image_bytes(600, 400, "PNG", noisy=True)
    sizes = {}
    for preset in ("strong", "high"):
        snapshot = run_conversion(
            client,
            files=image_files(("photo.png", png)),
            target_type="jpg",
            quality_preset=preset,
        )
        assert snapshot["status"] == "completed", snapshot["error"]
        task = item_of(snapshot)
        sizes[preset] = task["result"]["size"]
        assert client.get(task["result"]["download_url"]).status_code == 200

    assert sizes["high"] > sizes["strong"], sizes


def test_blank_options_are_not_an_error(client: TestClient) -> None:
    """前端留空的可选参数不能变成 400（空串与「没传」等价）。"""
    response = submit_conversion(
        client,
        files=image_files(("photo.png", build_image_bytes(80, 60, "PNG"))),
        target_type="jpg",
        quality_preset="",
        quality_value="",
        target_bytes="",
        width="",
        height="",
        keep_aspect="",
    )
    assert response.status_code == 202, response.text
    snapshot = wait_conversion(client, response.json()["batch_id"])
    assert snapshot["status"] == "completed"


def test_bad_quality_preset_is_rejected(client: TestClient) -> None:
    response = submit_conversion(
        client,
        files=image_files(("photo.png", build_image_bytes(80, 60, "PNG"))),
        target_type="jpg",
        quality_preset="ultra",
    )
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == ErrorCode.INVALID_REQUEST
    assert "high" in error["message"] and "balanced" in error["message"]


# ----------------------------------------------------------------------
# 进度：真实就给，没有就是 null（§八 / §十二）
# ----------------------------------------------------------------------

def test_no_fake_percentage_for_image_conversion(client: TestClient) -> None:
    """图片转换没有可比对的刻度，进度必须是 null，不能编一个 50% 出来。"""
    snapshot = run_conversion(
        client,
        files=image_files(("photo.png", build_image_bytes(200, 200, "PNG"))),
        target_type="jpg",
    )
    task = item_of(snapshot)

    assert task["progress"] is None
    assert task["stage"] is None
    assert task["page"] is None
    assert task["page_count"] is None


@requires_ocr
def test_progress_id_reports_real_pages(client: TestClient) -> None:
    """带上进度 id 时，PDF → Word 的真实页码要能被读到。

    短标签 PDF 会被判成扫描页（见 ``test_pdf_to_docx_produces_a_real_document``），
    所以这条要 OCR —— 而「真实页码」这件事恰恰只有扫描页那条路才报得出来。
    """
    snapshot = run_conversion(
        client,
        files=pdf_files(("doc.pdf", build_labeled_pdf("p", 2)), field="files"),
        target_type="docx",
        progress_ids=["conversionprogress01"],
    )
    task = item_of(snapshot)

    assert snapshot["status"] == "completed", snapshot["error"]
    # 转换已经结束，进度表可能已被清理；但只要还在，就必须是真页码
    if task["progress"] is not None:
        assert task["page_count"] == 2
        assert 0 < task["page"] <= 2
        assert 0 < task["progress"] <= 100


@requires_ocr
def test_malformed_progress_id_is_ignored(client: TestClient) -> None:
    """形状不合法的进度 id 当作没传，不能因此报参数错误（与第六阶段一致）。"""
    snapshot = run_conversion(
        client,
        files=pdf_files(("doc.pdf", build_labeled_pdf("p", 1)), field="files"),
        target_type="docx",
        progress_ids=["../../etc/passwd"],
    )

    assert snapshot["status"] == "completed", snapshot["error"]
    assert item_of(snapshot)["progress"] is None


# ----------------------------------------------------------------------
# 不支持与改名（§三十七）
# ----------------------------------------------------------------------

def test_unknown_target_is_rejected_at_submit(client: TestClient) -> None:
    """目标格式不在册：整批直接拒绝，不必等到排队。"""
    response = submit_conversion(
        client,
        files=image_files(("a.png", build_image_bytes(80, 60, "PNG"))),
        target_type="pptx",
    )

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == ErrorCode.UNSUPPORTED_CONVERSION
    for fragment in FORBIDDEN_FRAGMENTS:
        assert fragment not in response.text


def test_pdf_to_powerpoint_is_rejected(client: TestClient) -> None:
    """§四十四 明令不做 PDF → PPT。前端不该给出这个选项，后端也必须挡住。"""
    response = submit_conversion(
        client,
        files=pdf_files(("doc.pdf", build_labeled_pdf("p", 1)), field="files"),
        target_type="pptx",
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == ErrorCode.UNSUPPORTED_CONVERSION


def test_unsupported_combination_fails_only_that_item(client: TestClient) -> None:
    """目标格式本身合法，但这个源转不过去 —— 记在这一项头上，说清原因。"""
    snapshot = run_conversion(
        client,
        files=pdf_files(("doc.pdf", build_labeled_pdf("p", 1)), field="files"),
        target_type="pdf",
    )

    assert snapshot["status"] == "failed"
    task = item_of(snapshot)
    assert task["status"] == "failed"
    assert task["error_code"] == ErrorCode.UNSUPPORTED_CONVERSION
    assert "PDF" in task["error_message"]
    # 这种失败重试一百次也是同样结果 → 不可重试（§十六）
    assert task["can_retry"] is False
    assert_clean(snapshot)


def test_renamed_file_is_rejected(client: TestClient) -> None:
    """把 PNG 改名成 .docx 上传：必须按真实内容识破，不能靠扩展名放行。"""
    snapshot = run_conversion(
        client,
        files=office_files(
            ("fake.docx", build_image_bytes(120, 90, "PNG")), field="files"
        ),
        target_type="pdf",
    )

    assert snapshot["status"] == "failed"
    task = item_of(snapshot)
    assert task["status"] == "failed"
    assert task["error_code"] in (
        ErrorCode.INVALID_FILE_TYPE,
        ErrorCode.CORRUPTED_FILE,
    )
    assert task["error_message"]
    assert_clean(snapshot)


def test_executable_renamed_to_pdf_is_rejected(client: TestClient) -> None:
    payload = b"MZ" + b"\x00" * 4096
    snapshot = run_conversion(
        client,
        files=pdf_files(("invoice.pdf", payload), field="files"),
        target_type="docx",
    )

    assert snapshot["status"] == "failed"
    task = item_of(snapshot)
    assert task["error_code"] in (
        ErrorCode.INVALID_FILE_TYPE,
        ErrorCode.CORRUPTED_FILE,
    )
    assert "MZ" not in task["error_message"]
    assert_clean(snapshot)


def test_unsupported_extension_is_rejected_by_upload(client: TestClient) -> None:
    """上传白名单是第一道闸。

    注意它**不会让整个请求失败**（§五）：这一项记为失败，其余文件照常处理。
    提示语要把能转的格式列全 —— 转换中心的白名单是各种源格式的并集，
    不能像单格式工具那样只说一种。
    """
    snapshot = run_conversion(
        client,
        files=image_files(("virus.exe", b"MZ" + b"\x00" * 64)),
        target_type="jpg",
    )

    assert snapshot["status"] == "failed"
    task = item_of(snapshot)
    assert task["status"] == "failed"
    assert task["error_code"] == ErrorCode.INVALID_FILE_TYPE
    assert "JPG" in task["error_message"] and "PDF" in task["error_message"]
    assert task["can_retry"] is False
    assert_clean(snapshot)


def test_empty_upload_is_rejected(client: TestClient) -> None:
    response = client.post(ENDPOINT, files=[], data={"target_type": "jpg"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == ErrorCode.INVALID_REQUEST


def test_too_many_files_are_rejected(client: TestClient) -> None:
    png = build_image_bytes(40, 40, "PNG")
    names = tuple((f"{index}.png", png) for index in range(settings.MAX_BATCH_FILES + 1))

    response = submit_conversion(client, files=image_files(*names), target_type="jpg")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == ErrorCode.INVALID_REQUEST
    assert str(settings.MAX_BATCH_FILES) in response.json()["error"]["message"]


# ----------------------------------------------------------------------
# 能力 ID（第九阶段 §二十三）
# ----------------------------------------------------------------------

def test_every_item_reports_the_capability_it_hit(client: TestClient) -> None:
    """每一项都要带上命中的能力 ID，且它与源/目标必须自洽。

    ``conversion_id`` 是**展示用**的（结果卡上那句「JPG → PNG」靠它），
    所以它错了不会转错文件 —— 正因如此，没有断言就没人会发现它错了。
    这里把它与 ``registry.CAPABILITY_BY_ID`` 对账：ID 必须真的在册，
    而且反推出来的 (源, 目标) 必须与这一项真实判定的格式一致。
    """
    from conversion import capability

    snapshot = run_conversion(
        client,
        files=image_files(("a.png", build_image_bytes(64, 48, "PNG"))),
        target_type="jpg",
    )

    assert snapshot["status"] == "completed", snapshot["error"]
    task = item_of(snapshot)
    assert task["conversion_id"] == "image.png-to-jpg"
    entry = registry.CAPABILITY_BY_ID[task["conversion_id"]]
    assert (entry.source_type, entry.target_type) == (
        task["source_type"],
        task["target_type"],
    )
    assert capability.parse_capability_id(task["conversion_id"]) == (
        entry.category,
        entry.source_type,
        entry.target_type,
    )


def test_rejected_item_has_no_capability_id(client: TestClient) -> None:
    """没命中任何能力的那一项，ID 是**空串**，不是一个编出来的名字。

    空串的意思是「这一项从来没有匹配上一条能力」；给它填一个最接近的 ID
    会让界面显示一条它其实没走成的转换路径。
    """
    snapshot = run_conversion(
        client,
        files=pdf_files(("doc.pdf", build_labeled_pdf("p", 1)), field="files"),
        target_type="pdf",
    )

    assert snapshot["status"] == "failed"
    task = item_of(snapshot)
    assert task["error_code"] == ErrorCode.UNSUPPORTED_CONVERSION
    assert task["conversion_id"] == ""


def test_capability_id_is_empty_when_upload_never_got_that_far(
    client: TestClient,
) -> None:
    """上传阶段就被拒的文件连类型都没定，自然也没有能力 ID。"""
    snapshot = run_conversion(
        client,
        files=image_files(("virus.exe", b"MZ" + b"\x00" * 64)),
        target_type="jpg",
    )

    assert snapshot["status"] == "failed"
    task = item_of(snapshot)
    assert task["error_code"] == ErrorCode.INVALID_FILE_TYPE
    assert task["conversion_id"] == ""
    assert task["source_type"] == ""


def test_options_are_not_recorded_for_a_batch_that_never_sent_any(
    client: TestClient,
) -> None:
    """没传 ``options`` 时字段是 ``None``，而不是一个空的 ``{}``。

    两者的区别是给界面看的：「没有记录」与「记录了一次空参数」不是一回事，
    而前端要据此决定显不显示那一栏。
    """
    snapshot = run_conversion(
        client,
        files=image_files(("a.png", build_image_bytes(64, 48, "PNG"))),
        target_type="jpg",
    )

    task = item_of(snapshot)
    assert task["options"] is None


# ----------------------------------------------------------------------
# 文件名清洗（§三十七：``../../evil.pdf`` 不能影响服务器目录）
# ----------------------------------------------------------------------

def test_path_traversal_in_filename_is_neutralised(client: TestClient) -> None:
    snapshot = run_conversion(
        client,
        files=image_files(("../../evil.png", build_image_bytes(60, 60, "PNG"))),
        target_type="jpg",
    )

    assert snapshot["status"] == "completed", snapshot["error"]
    task = item_of(snapshot)
    name = task["result"]["filename"]
    assert "/" not in name and "\\" not in name and ".." not in name
    assert name == "evil.jpg"

    response = client.get(task["result"]["download_url"])
    assert response.status_code == 200
    disposition = response.headers.get("content-disposition", "")
    assert ".." not in disposition
    assert tempfile.gettempdir() not in disposition
