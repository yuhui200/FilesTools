"""PDF 页面删除与页面提取的端到端测试。

对应第三阶段 §17 的「删除页面」「提取页面」两项。
两个功能共用同一套重建逻辑，所以放在一个文件里成对测试 ——
「删掉第 2 页」和「提取其余页」必须得到完全一样的结果，
否则用户会发现两个入口给的 PDF 不一样。
"""

from __future__ import annotations

import io
import tempfile
from pathlib import Path

import pymupdf
import pytest
from fastapi.testclient import TestClient

from tests.conftest import build_labeled_pdf

DELETE = "/api/pdf/delete-pages"
EXTRACT = "/api/pdf/extract-pages"


def upload(client: TestClient, data: bytes, name: str = "report.pdf") -> dict:
    response = client.post(
        "/api/pdf/upload",
        files={"file": (name, io.BytesIO(data), "application/pdf")},
    )
    assert response.status_code == 200, response.text
    return response.json()


def call(client: TestClient, endpoint: str, input_id: str, pages: str):
    return client.post(endpoint, data={"input_id": input_id, "pages": pages})


def fetch(client: TestClient, result: dict) -> bytes:
    response = client.get(result["download_url"])
    assert response.status_code == 200, response.text
    return response.content


def page_texts(data: bytes) -> list[str]:
    with pymupdf.open("pdf", data) as doc:
        return [doc[index].get_text().strip() for index in range(doc.page_count)]


# ----------------------------------------------------------------------
# 页面删除
# ----------------------------------------------------------------------

def test_delete_single_page(client: TestClient) -> None:
    info = upload(client, build_labeled_pdf("A", 5))
    result = call(client, DELETE, info["input_id"], "2").json()

    assert result["page_count"] == 4
    assert result["original_pages"] == 5
    assert result["archived"] is False
    assert page_texts(fetch(client, result)) == ["A-1", "A-3", "A-4", "A-5"]


def test_delete_multiple_pages_and_ranges(client: TestClient) -> None:
    info = upload(client, build_labeled_pdf("A", 8))
    result = call(client, DELETE, info["input_id"], "1,3,6-8").json()

    assert result["page_count"] == 3
    assert page_texts(fetch(client, result)) == ["A-2", "A-4", "A-5"]


def test_delete_first_and_last_page(client: TestClient) -> None:
    """首尾两页是最容易写错边界的两个位置。"""
    info = upload(client, build_labeled_pdf("A", 4))
    result = call(client, DELETE, info["input_id"], "1,4").json()

    assert page_texts(fetch(client, result)) == ["A-2", "A-3"]


def test_delete_reports_pages_in_note(client: TestClient) -> None:
    info = upload(client, build_labeled_pdf("A", 5))
    result = call(client, DELETE, info["input_id"], "2,4").json()

    note = result["notes"][0]
    assert "已删除 2 页" in note
    assert "2,4" in note
    assert "保留 3 页" in note


def test_delete_keeps_original_stem(client: TestClient) -> None:
    info = upload(client, build_labeled_pdf("A", 3), name="季度报告.pdf")
    result = call(client, DELETE, info["input_id"], "1").json()

    assert result["filename"] == "季度报告_edited.pdf"


def test_delete_all_pages_is_rejected(client: TestClient) -> None:
    """全删掉会生成一份 0 页的 PDF，后续功能拿到它会全线报错。"""
    info = upload(client, build_labeled_pdf("A", 3))
    response = call(client, DELETE, info["input_id"], "all")

    assert response.status_code == 400
    assert "至少保留一页" in response.json()["error"]["message"]


def test_delete_exceeding_page_count_is_rejected(client: TestClient) -> None:
    info = upload(client, build_labeled_pdf("A", 3))
    response = call(client, DELETE, info["input_id"], "1-99")

    assert response.status_code == 400
    assert "第 99 页不存在" in response.json()["error"]["message"]


def test_delete_empty_selection_is_rejected(client: TestClient) -> None:
    info = upload(client, build_labeled_pdf("A", 3))
    response = call(client, DELETE, info["input_id"], "  ")

    assert response.status_code == 400
    assert "选择要删除的页面" in response.json()["error"]["message"]


def test_delete_is_case_insensitive_for_all(client: TestClient) -> None:
    info = upload(client, build_labeled_pdf("A", 2))
    response = call(client, DELETE, info["input_id"], "ALL")
    assert response.status_code == 400
    assert "至少保留一页" in response.json()["error"]["message"]


# ----------------------------------------------------------------------
# 页面提取
# ----------------------------------------------------------------------

def test_extract_pages(client: TestClient) -> None:
    info = upload(client, build_labeled_pdf("A", 8))
    result = call(client, EXTRACT, info["input_id"], "1,3,5,8").json()

    assert result["page_count"] == 4
    assert result["filename"] == "selected_pages.pdf"
    assert result["archived"] is False
    assert page_texts(fetch(client, result)) == ["A-1", "A-3", "A-5", "A-8"]


def test_extract_single_page(client: TestClient) -> None:
    info = upload(client, build_labeled_pdf("A", 4))
    result = call(client, EXTRACT, info["input_id"], "3").json()

    assert result["page_count"] == 1
    assert page_texts(fetch(client, result)) == ["A-3"]


def test_extract_keeps_written_order(client: TestClient) -> None:
    """提取按**书写顺序**输出（第四阶段 §11：可以拖动排序）。

    删除仍然是升序的（见上面的删除用例）—— 只有「提取」的页序是用户
    在界面上排的，所以这里必须原样保留，重复的页面也要保留。
    """
    info = upload(client, build_labeled_pdf("A", 6))
    result = call(client, EXTRACT, info["input_id"], "5,2,4").json()

    assert page_texts(fetch(client, result)) == ["A-5", "A-2", "A-4"]


def test_extract_keeps_duplicates(client: TestClient) -> None:
    """同一页写两次就得到两页 —— 拖动排序后这是用户明确的选择。"""
    info = upload(client, build_labeled_pdf("A", 3))
    result = call(client, EXTRACT, info["input_id"], "3,1,3").json()

    assert result["page_count"] == 3
    assert page_texts(fetch(client, result)) == ["A-3", "A-1", "A-3"]


def test_extract_all_pages(client: TestClient) -> None:
    info = upload(client, build_labeled_pdf("A", 3))
    result = call(client, EXTRACT, info["input_id"], "all").json()

    assert page_texts(fetch(client, result)) == ["A-1", "A-2", "A-3"]


def test_extract_reports_pages_in_note(client: TestClient) -> None:
    info = upload(client, build_labeled_pdf("A", 5))
    result = call(client, EXTRACT, info["input_id"], "1-2,5").json()

    assert "已提取 3 页" in result["notes"][0]
    assert "1-2,5" in result["notes"][0]


def test_extract_empty_selection_is_rejected(client: TestClient) -> None:
    info = upload(client, build_labeled_pdf("A", 3))
    response = call(client, EXTRACT, info["input_id"], "")

    assert response.status_code == 400
    assert "选择要提取的页面" in response.json()["error"]["message"]


def test_extract_out_of_bounds_is_rejected(client: TestClient) -> None:
    info = upload(client, build_labeled_pdf("A", 3))
    response = call(client, EXTRACT, info["input_id"], "0")

    assert response.status_code == 400
    assert "页码从 1 开始" in response.json()["error"]["message"]


# ----------------------------------------------------------------------
# 两个功能的关系
# ----------------------------------------------------------------------

def test_delete_and_extract_are_complementary(client: TestClient) -> None:
    """删除第 2 页 与 提取除第 2 页以外的页，结果必须一模一样。"""
    source = build_labeled_pdf("A", 5)

    deleted = fetch(client, call(client, DELETE, upload(client, source)["input_id"], "2").json())
    extracted = fetch(
        client,
        call(client, EXTRACT, upload(client, source)["input_id"], "1,3,4,5").json(),
    )

    assert page_texts(deleted) == page_texts(extracted) == ["A-1", "A-3", "A-4", "A-5"]


def test_edited_pdf_can_be_edited_again(client: TestClient) -> None:
    """删完再删是常见用法：先删掉封面，再从结果里删掉附录。"""
    info = upload(client, build_labeled_pdf("A", 6))
    first = call(client, DELETE, info["input_id"], "1").json()
    assert page_texts(fetch(client, first)) == ["A-2", "A-3", "A-4", "A-5", "A-6"]

    second = call(client, DELETE, info["input_id"], "1,2").json()
    assert page_texts(fetch(client, second)) == ["A-3", "A-4", "A-5", "A-6"]


# ----------------------------------------------------------------------
# 结果文件
# ----------------------------------------------------------------------

def test_edited_pdf_keeps_images(client: TestClient) -> None:
    """重建后的页面要保留图片资源，不能只剩文字。"""
    from tests.conftest import build_pdf_bytes

    info = upload(client, build_pdf_bytes(3))
    result = call(client, EXTRACT, info["input_id"], "1,3").json()

    with pymupdf.open("pdf", fetch(client, result)) as doc:
        assert doc.page_count == 2
        for index in range(doc.page_count):
            assert doc[index].get_images(full=True)


def test_edited_pdf_is_valid(client: TestClient) -> None:
    info = upload(client, build_labeled_pdf("A", 4))
    result = call(client, DELETE, info["input_id"], "2").json()

    with pymupdf.open("pdf", fetch(client, result)) as doc:
        assert doc.is_pdf
        assert not doc.needs_pass
        for index in range(doc.page_count):
            assert doc[index].get_pixmap().width > 0


def test_result_is_one_time_download(client: TestClient) -> None:
    info = upload(client, build_labeled_pdf("A", 3))
    result = call(client, DELETE, info["input_id"], "1").json()

    url = result["download_url"]
    assert client.get(url).status_code == 200
    assert client.get(url).status_code == 404


def test_input_survives_after_download(client: TestClient) -> None:
    """下载结果不该把上传的文件删掉，用户还要继续删别的页。"""
    info = upload(client, build_labeled_pdf("A", 4))
    fetch(client, call(client, DELETE, info["input_id"], "1").json())

    again = call(client, DELETE, info["input_id"], "4")
    assert again.status_code == 200, again.text
    assert page_texts(fetch(client, again.json())) == ["A-1", "A-2", "A-3"]


def test_temp_dir_is_cleaned_up_after_download(client: TestClient) -> None:
    before = set(Path(tempfile.gettempdir()).glob("filetools_*"))
    info = upload(client, build_labeled_pdf("A", 3))
    uploaded = set(Path(tempfile.gettempdir()).glob("filetools_*")) - before
    assert len(uploaded) == 1

    fetch(client, call(client, DELETE, info["input_id"], "1").json())

    assert set(Path(tempfile.gettempdir()).glob("filetools_*")) - before == uploaded


def test_unknown_input_is_rejected(client: TestClient) -> None:
    response = call(client, DELETE, "f" * 32, "1")
    assert response.status_code == 404
    assert "重新上传" in response.json()["error"]["message"]


def test_result_token_cannot_be_used_as_input(client: TestClient) -> None:
    """结果令牌不是输入令牌，不能被拿来当作下一次编辑的源文件。"""
    info = upload(client, build_labeled_pdf("A", 3))
    result = call(client, DELETE, info["input_id"], "1").json()

    response = call(client, DELETE, result["job_id"], "1")
    assert response.status_code == 404


@pytest.mark.parametrize("pages", ["2", "1-2", "3,1"])
def test_delete_pages_parametrized(client: TestClient, pages: str) -> None:
    info = upload(client, build_labeled_pdf("A", 4))
    response = call(client, DELETE, info["input_id"], pages)
    assert response.status_code == 200, response.text
    assert response.json()["page_count"] < 4
