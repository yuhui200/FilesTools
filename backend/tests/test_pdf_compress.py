"""PDF 压缩的端到端测试。

对应第三阶段 §17 的「PDF 压缩」一项。

压缩最容易造假 —— 报一个好看的数字、或者把文件压小但内容糊掉。
所以这里除了看体积，还要**核对页面图片的真实像素**：
压到 150 DPI 就该是 150 DPI 的像素数，而不是随便报个数。
"""

from __future__ import annotations

import io
import tempfile
from pathlib import Path

import pymupdf
import pytest
from fastapi.testclient import TestClient

from tests.conftest import build_image_bytes, build_labeled_pdf

ENDPOINT = "/api/pdf/compress"

MB = 1024 * 1024


@pytest.fixture(scope="module")
def scan_bytes() -> bytes:
    """一份「扫描件」样式的 PDF：整页大图，压缩空间很大。

    这是 PDF 压缩最典型的场景，也是唯一能验证出压缩率差异的素材 ——
    纯文字 PDF 压不出东西（模块内共用，避免每个用例都重新生成）。
    """
    photo = build_image_bytes(2400, 1800, "JPEG")
    doc = pymupdf.open()
    try:
        for _ in range(3):
            page = doc.new_page(width=595, height=842)
            page.insert_image(pymupdf.Rect(0, 0, 595, 842), stream=photo)
        return doc.tobytes(deflate=True, garbage=4, clean=True)
    finally:
        doc.close()


def upload(client: TestClient, data: bytes, name: str = "scan.pdf") -> dict:
    response = client.post(
        "/api/pdf/upload",
        files={"file": (name, io.BytesIO(data), "application/pdf")},
    )
    assert response.status_code == 200, response.text
    return response.json()


def compress(client: TestClient, input_id: str, **form):
    payload = {"input_id": input_id, "level": "balanced", "target": "none"}
    payload.update({k: v for k, v in form.items() if v is not None})
    return client.post(ENDPOINT, data=payload)


def fetch(client: TestClient, result: dict) -> bytes:
    response = client.get(result["download_url"])
    assert response.status_code == 200, response.text
    return response.content


def first_image_size(data: bytes) -> tuple[int, int]:
    """取出第一页第一张嵌入图片的真实像素尺寸。"""
    with pymupdf.open("pdf", data) as doc:
        xref = doc[0].get_images(full=True)[0][0]
        info = doc.extract_image(xref)
        return info["width"], info["height"]


def first_image_dpi(data: bytes) -> float:
    """第一页第一张图的实际分辨率（像素 / 放置尺寸）。

    原图 2400x1800 铺在 595x446 pt 的区域上，就是 290 DPI。
    """
    with pymupdf.open("pdf", data) as doc:
        xref = doc[0].get_images(full=True)[0][0]
        width = doc.extract_image(xref)["width"]
        rect = doc[0].get_image_bbox(doc[0].get_images(full=True)[0])
        return width / (rect.width / 72)


# ----------------------------------------------------------------------
# 基本效果
# ----------------------------------------------------------------------

def test_compression_shrinks_scanned_pdf(client: TestClient, scan_bytes: bytes) -> None:
    info = upload(client, scan_bytes)
    result = compress(client, info["input_id"], level="balanced").json()

    assert result["original_size"] == len(scan_bytes)
    assert result["size"] < len(scan_bytes)
    assert result["saved_bytes"] == len(scan_bytes) - result["size"]
    assert result["saved_percent"] == round(result["saved_bytes"] / len(scan_bytes) * 100, 1)
    assert result["saved_percent"] > 20


def test_compressed_pdf_keeps_pages_and_content(client: TestClient, scan_bytes: bytes) -> None:
    """压小可以，页数和页面内容不能丢。"""
    info = upload(client, scan_bytes)
    result = compress(client, info["input_id"]).json()

    assert result["page_count"] == 3
    assert result["original_pages"] == 3

    with pymupdf.open("pdf", fetch(client, result)) as doc:
        assert doc.page_count == 3
        for index in range(doc.page_count):
            assert doc[index].get_images(full=True), f"第 {index + 1} 页的图片丢了"
            assert doc[index].get_pixmap().width > 0


def test_stronger_level_gives_smaller_file(client: TestClient, scan_bytes: bytes) -> None:
    """三档必须真的不一样，否则等级选择就是摆设。"""
    sizes = {}
    for level in ("light", "balanced", "strong"):
        info = upload(client, scan_bytes)
        sizes[level] = compress(client, info["input_id"], level=level).json()["size"]

    assert sizes["strong"] < sizes["balanced"] < sizes["light"]


@pytest.mark.parametrize(
    ("level", "cap"),
    [("light", 200), ("balanced", 150), ("strong", 96)],
)
def test_level_caps_image_resolution(
    client: TestClient, scan_bytes: bytes, level: str, cap: int
) -> None:
    """每个等级都真的把图片分辨率压到了档位以内 —— 不是只写进提示文案里。

    原图是 290 DPI，所以这条断言是有意义的：没有压缩的话它必然超标。
    """
    info = upload(client, scan_bytes)
    result = compress(client, info["input_id"], level=level, target=None).json()

    actual = first_image_dpi(fetch(client, result))
    assert actual <= cap, f"{level} 档压缩后仍有 {actual:.0f} DPI 的图片"


def test_strong_level_actually_shrinks_pixels(client: TestClient, scan_bytes: bytes) -> None:
    """高压缩档必须真的减少像素，而不只是降质量。"""
    info = upload(client, scan_bytes)
    result = compress(client, info["input_id"], level="strong", target=None).json()

    width, _height = first_image_size(fetch(client, result))
    assert width < first_image_size(scan_bytes)[0]


def test_note_describes_level_and_settings(client: TestClient, scan_bytes: bytes) -> None:
    info = upload(client, scan_bytes)
    result = compress(client, info["input_id"], level="light").json()

    note = result["notes"][0]
    assert "轻度" in note
    assert "200 DPI" in note
    assert "质量 85" in note


def test_output_name_keeps_original_stem(client: TestClient, scan_bytes: bytes) -> None:
    info = upload(client, scan_bytes, name="合同扫描件.pdf")
    result = compress(client, info["input_id"]).json()

    assert result["filename"] == "合同扫描件_compressed.pdf"
    assert result["archived"] is False


# ----------------------------------------------------------------------
# 目标最大文件大小
# ----------------------------------------------------------------------

def test_target_already_met(client: TestClient, scan_bytes: bytes) -> None:
    """原文件本来就不到 20 MB，选 20 MB 应当一次就达标。"""
    info = upload(client, scan_bytes)
    result = compress(client, info["input_id"], target="20mb").json()

    assert result["size"] <= 20 * MB
    assert any("已达到目标大小" in note for note in result["notes"])


def test_target_pushes_compression_harder(client: TestClient, scan_bytes: bytes) -> None:
    """设了目标就要往死里压，结果必须比同等级的普通压缩更小。"""
    info = upload(client, scan_bytes)
    plain = compress(client, info["input_id"], level="light", target="none").json()

    info = upload(client, scan_bytes)
    targeted = compress(
        client, info["input_id"], level="light", target="custom", target_mb="0.2"
    ).json()

    assert targeted["size"] < plain["size"]
    assert targeted["size"] <= 0.2 * MB
    assert any("已达到目标大小" in note for note in targeted["notes"])


def test_custom_target_is_used(client: TestClient, scan_bytes: bytes) -> None:
    info = upload(client, scan_bytes)
    result = compress(
        client, info["input_id"], target="custom", target_mb="0.5"
    ).json()

    assert result["size"] <= 0.5 * MB


def test_unreachable_target_is_reported_honestly(scan_bytes: bytes, tmp_path: Path) -> None:
    """达不到目标时必须如实说明，而不是假装达标。

    这里直接调压缩函数并只给一档机会，构造出「压不到目标」的情况 ——
    走接口的话任何真实文件都能压到几百 KB，反而测不到这条分支。
    """
    from pdf.compressor import compress_pdf

    source = tmp_path / "scan.pdf"
    source.write_bytes(scan_bytes)

    result = compress_pdf(source, level="light", target_bytes=1024, max_attempts=1)

    assert result.reached_target is False
    assert any("仍未达到" in note for note in result.notes)
    assert result.size > 1024


# ----------------------------------------------------------------------
# 参数校验
# ----------------------------------------------------------------------

def test_invalid_level_is_rejected(client: TestClient, scan_bytes: bytes) -> None:
    info = upload(client, scan_bytes)
    response = compress(client, info["input_id"], level="extreme")

    assert response.status_code == 400
    message = response.json()["error"]["message"]
    assert "轻度" in message and "高压缩" in message


def test_invalid_target_is_rejected(client: TestClient, scan_bytes: bytes) -> None:
    info = upload(client, scan_bytes)
    response = compress(client, info["input_id"], target="1gb")

    assert response.status_code == 400
    assert "目标大小参数无效" in response.json()["error"]["message"]


def test_custom_target_requires_number(client: TestClient, scan_bytes: bytes) -> None:
    info = upload(client, scan_bytes)
    response = compress(client, info["input_id"], target="custom")

    assert response.status_code == 400
    assert "请填写目标大小" in response.json()["error"]["message"]


def test_custom_target_rejects_non_number(client: TestClient, scan_bytes: bytes) -> None:
    info = upload(client, scan_bytes)
    response = compress(client, info["input_id"], target="custom", target_mb="小一点")

    assert response.status_code == 400
    assert "必须是数字" in response.json()["error"]["message"]


def test_custom_target_rejects_out_of_range(client: TestClient, scan_bytes: bytes) -> None:
    info = upload(client, scan_bytes)
    response = compress(client, info["input_id"], target="custom", target_mb="1000")

    assert response.status_code == 400
    assert "MB 之间" in response.json()["error"]["message"]


def test_default_level_is_balanced(client: TestClient, scan_bytes: bytes) -> None:
    info = upload(client, scan_bytes)
    response = client.post(ENDPOINT, data={"input_id": info["input_id"]})

    assert response.status_code == 200, response.text
    assert "平衡" in response.json()["notes"][0]


def test_unknown_input_is_rejected(client: TestClient) -> None:
    response = compress(client, "a" * 32)
    assert response.status_code == 404
    assert "重新上传" in response.json()["error"]["message"]


# ----------------------------------------------------------------------
# 不能压坏、不能压大
# ----------------------------------------------------------------------

def test_text_only_pdf_keeps_original(client: TestClient) -> None:
    """没有图片可压的 PDF 不做无谓的重编码，也不该给出更大的文件。"""
    info = upload(client, build_labeled_pdf("A", 3))
    result = compress(client, info["input_id"], level="strong").json()

    assert result["saved_bytes"] >= 0
    assert result["saved_percent"] >= 0
    assert any("保留了原文件" in note for note in result["notes"])

    # 保留下来的就是原文件本身，内容一字不差
    with pymupdf.open("pdf", fetch(client, result)) as doc:
        assert [doc[i].get_text().strip() for i in range(doc.page_count)] == [
            "A-1",
            "A-2",
            "A-3",
        ]


def test_never_returns_a_bigger_file(client: TestClient, scan_bytes: bytes) -> None:
    for level in ("light", "balanced", "strong"):
        info = upload(client, scan_bytes)
        result = compress(client, info["input_id"], level=level).json()
        assert result["size"] <= result["original_size"]


def test_compressed_pdf_is_structurally_valid(client: TestClient, scan_bytes: bytes) -> None:
    info = upload(client, scan_bytes)
    result = compress(client, info["input_id"], level="strong").json()

    raw = fetch(client, result)
    with pymupdf.open("pdf", raw) as doc:
        assert doc.is_pdf
        assert not doc.needs_pass
        assert doc.page_count == 3


def test_result_is_one_time_download(client: TestClient, scan_bytes: bytes) -> None:
    info = upload(client, scan_bytes)
    result = compress(client, info["input_id"]).json()

    url = result["download_url"]
    assert client.get(url).status_code == 200
    assert client.get(url).status_code == 404


def test_input_survives_so_user_can_try_another_level(client: TestClient, scan_bytes: bytes) -> None:
    info = upload(client, scan_bytes)
    fetch(client, compress(client, info["input_id"], level="light").json())

    again = compress(client, info["input_id"], level="strong")
    assert again.status_code == 200, again.text


def test_temp_dir_is_cleaned_up_after_download(client: TestClient, scan_bytes: bytes) -> None:
    before = set(Path(tempfile.gettempdir()).glob("filetools_*"))
    info = upload(client, scan_bytes)
    uploaded = set(Path(tempfile.gettempdir()).glob("filetools_*")) - before

    fetch(client, compress(client, info["input_id"]).json())

    assert set(Path(tempfile.gettempdir()).glob("filetools_*")) - before == uploaded


def test_compressing_an_already_compressed_file_is_safe(client: TestClient, scan_bytes: bytes) -> None:
    """把压缩结果再压一次不该报错，也不该变大（用户会这么试）。"""
    info = upload(client, scan_bytes)
    first = compress(client, info["input_id"], level="strong").json()
    raw = fetch(client, first)

    again = upload(client, raw, name="compressed.pdf")
    second = compress(client, again["input_id"], level="strong").json()

    assert second["size"] <= len(raw)
