"""PDF 拆分的端到端测试。

对应第三阶段 §17 的「PDF 拆分」一项，覆盖 §7 的三种方式：
每页一个 PDF、按范围拆分、自定义页面。

拆分和合并一样，**页序与分组必须和用户写的一模一样**，
所以这里几乎每个用例都在核对「结果里有哪几页」。
"""

from __future__ import annotations

import io
import tempfile
import zipfile
from pathlib import Path

import pymupdf
import pytest
from fastapi.testclient import TestClient

from tests.conftest import build_labeled_pdf, build_pdf_bytes

ENDPOINT = "/api/pdf/split"


def upload(client: TestClient, data: bytes, name: str = "report.pdf") -> dict:
    response = client.post(
        "/api/pdf/upload",
        files={"file": (name, io.BytesIO(data), "application/pdf")},
    )
    assert response.status_code == 200, response.text
    return response.json()


def split(client: TestClient, input_id: str, *, mode: str, pages: str | None = None):
    payload: dict = {"input_id": input_id, "mode": mode}
    if pages is not None:
        payload["pages"] = pages
    return client.post(ENDPOINT, data=payload)


def fetch(client: TestClient, result: dict) -> bytes:
    response = client.get(result["download_url"])
    assert response.status_code == 200, response.text
    return response.content


def pdf_page_texts(data: bytes) -> list[str]:
    with pymupdf.open("pdf", data) as doc:
        return [doc[index].get_text().strip() for index in range(doc.page_count)]


def zip_page_texts(data: bytes) -> dict[str, list[str]]:
    """把 ZIP 里每一份 PDF 的每页文字解出来，键是包内文件名。"""
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return {
            name: pdf_page_texts(archive.read(name))
            for name in archive.namelist()
        }


# ----------------------------------------------------------------------
# 每页一个 PDF
# ----------------------------------------------------------------------

def test_every_page_becomes_one_pdf(client: TestClient) -> None:
    source = build_labeled_pdf("A", 3)
    result = split(client, upload(client, source)["input_id"], mode="every").json()

    assert result["archived"] is True
    assert result["page_count"] == 3
    assert result["original_pages"] == 3
    assert [item["filename"] for item in result["files"]] == [
        "part-01.pdf",
        "part-02.pdf",
        "part-03.pdf",
    ]
    assert zip_page_texts(fetch(client, result)) == {
        "part-01.pdf": ["A-1"],
        "part-02.pdf": ["A-2"],
        "part-03.pdf": ["A-3"],
    }


def test_every_page_with_single_page_document(client: TestClient) -> None:
    """只有一页时不该套 ZIP —— 多解压一次没有任何意义。"""
    result = split(client, upload(client, build_labeled_pdf("A", 1))["input_id"], mode="every").json()

    assert result["archived"] is False
    assert result["filename"] == "part-01.pdf"
    assert pdf_page_texts(fetch(client, result)) == ["A-1"]


def test_every_page_archive_name_follows_source(client: TestClient) -> None:
    """压缩包名字带上原文件名，下载一堆 parts 时才分得清是哪个文档。"""
    info = upload(client, build_labeled_pdf("A", 2), name="季度报告.pdf")
    result = split(client, info["input_id"], mode="every").json()

    assert result["archive_filename"] == "季度报告_parts.zip"


# ----------------------------------------------------------------------
# 按范围拆分
# ----------------------------------------------------------------------

def test_ranges_split_each_line_into_one_file(client: TestClient) -> None:
    source = build_labeled_pdf("A", 6)
    result = split(
        client,
        upload(client, source)["input_id"],
        mode="ranges",
        pages="1-2\n3-4\n5-6",
    ).json()

    assert result["page_count"] == 6
    assert [item["filename"] for item in result["files"]] == [
        "part-01.pdf",
        "part-02.pdf",
        "part-03.pdf",
    ]
    assert [item["page_count"] for item in result["files"]] == [2, 2, 2]
    assert zip_page_texts(fetch(client, result)) == {
        "part-01.pdf": ["A-1", "A-2"],
        "part-02.pdf": ["A-3", "A-4"],
        "part-03.pdf": ["A-5", "A-6"],
    }


def test_ranges_accept_slash_separator(client: TestClient) -> None:
    """§7 的示例写作 1-5/6-10/11-20，斜杠也要当分段符。"""
    result = split(
        client,
        upload(client, build_labeled_pdf("A", 4))["input_id"],
        mode="ranges",
        pages="1-2/3-4",
    ).json()

    assert [item["page_count"] for item in result["files"]] == [2, 2]
    assert zip_page_texts(fetch(client, result)) == {
        "part-01.pdf": ["A-1", "A-2"],
        "part-02.pdf": ["A-3", "A-4"],
    }


def test_ranges_keep_pages_ascending_inside_one_file(client: TestClient) -> None:
    """一段里的逗号表示同一份文件的多页，页序按页码升序。"""
    result = split(
        client,
        upload(client, build_labeled_pdf("A", 5))["input_id"],
        mode="ranges",
        pages="3,1,5",
    ).json()

    assert result["archived"] is False
    assert result["filename"] == "part-01.pdf"
    assert pdf_page_texts(fetch(client, result)) == ["A-1", "A-3", "A-5"]


def test_ranges_allow_unsorted_and_overlapping(client: TestClient) -> None:
    """范围可以乱序、可以重叠 —— 用户在细调拆法时常常这么写。"""
    result = split(
        client,
        upload(client, build_labeled_pdf("A", 4))["input_id"],
        mode="ranges",
        pages="3-4\n1-2",
    ).json()

    assert zip_page_texts(fetch(client, result)) == {
        "part-01.pdf": ["A-3", "A-4"],
        "part-02.pdf": ["A-1", "A-2"],
    }

    overlapped = split(
        client,
        upload(client, build_labeled_pdf("B", 3))["input_id"],
        mode="ranges",
        pages="1-2\n2-3",
    ).json()
    assert overlapped["page_count"] == 4
    assert any("重叠" in note for note in overlapped["notes"])


def test_ranges_report_uncovered_pages(client: TestClient) -> None:
    """没被任何范围覆盖的页不会被静默吞掉，要在结果里说清楚。"""
    result = split(
        client,
        upload(client, build_labeled_pdf("A", 6))["input_id"],
        mode="ranges",
        pages="1-2\n5-6",
    ).json()

    assert result["page_count"] == 4
    assert any("第 3-4 页" in note for note in result["notes"])


def test_ranges_full_coverage_has_no_warning(client: TestClient) -> None:
    result = split(
        client,
        upload(client, build_labeled_pdf("A", 4))["input_id"],
        mode="ranges",
        pages="1-2\n3-4",
    ).json()

    assert [note for note in result["notes"] if "不在任何范围" in note] == []


def test_ranges_reject_empty_input(client: TestClient) -> None:
    response = split(
        client,
        upload(client, build_labeled_pdf("A", 3))["input_id"],
        mode="ranges",
        pages="   ",
    )
    assert response.status_code == 400
    assert "范围" in response.json()["error"]["message"]


def test_ranges_reject_bad_token(client: TestClient) -> None:
    response = split(
        client,
        upload(client, build_labeled_pdf("A", 3))["input_id"],
        mode="ranges",
        pages="1-2\nabc",
    )
    assert response.status_code == 400
    assert "1-3" in response.json()["error"]["message"]


def test_ranges_reject_reversed_range(client: TestClient) -> None:
    response = split(
        client,
        upload(client, build_labeled_pdf("A", 5))["input_id"],
        mode="ranges",
        pages="1-2\n5-2",
    )
    assert response.status_code == 400
    assert "起始页不能大于结束页" in response.json()["error"]["message"]


def test_ranges_reject_out_of_bounds(client: TestClient) -> None:
    response = split(
        client,
        upload(client, build_labeled_pdf("A", 3))["input_id"],
        mode="ranges",
        pages="1-2\n1-99",
    )
    assert response.status_code == 400
    assert "第 99 页不存在" in response.json()["error"]["message"]


def test_ranges_reject_too_many_parts(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """份数上限：范围可以互相重叠，所以份数不受页数约束，必须单独卡住。"""
    from config import settings

    monkeypatch.setattr(settings, "MAX_PDF_PAGES", 2)
    response = split(
        client,
        upload(client, build_labeled_pdf("A", 2))["input_id"],
        mode="ranges",
        pages="1\n2\n1",
    )
    assert response.status_code == 400
    assert "最多拆出 2 个文件" in response.json()["error"]["message"]


def test_part_number_width_grows_with_count(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """份数上百时编号补到三位，按文件名排序才不会乱。"""
    from config import settings

    monkeypatch.setattr(settings, "MAX_PDF_PAGES", 120)
    result = split(
        client,
        upload(client, build_labeled_pdf("A", 100))["input_id"],
        mode="every",
    ).json()

    names = [item["filename"] for item in result["files"]]
    assert len(names) == 100
    assert names[0] == "part-001.pdf"
    assert names[99] == "part-100.pdf"


# ----------------------------------------------------------------------
# 自定义页面
# ----------------------------------------------------------------------

def test_selected_pages_merge_into_one_pdf(client: TestClient) -> None:
    result = split(
        client,
        upload(client, build_labeled_pdf("A", 8))["input_id"],
        mode="selected",
        pages="1,3,5,8",
    ).json()

    assert result["archived"] is False
    assert result["filename"] == "selected-pages.pdf"
    assert result["page_count"] == 4
    assert pdf_page_texts(fetch(client, result)) == ["A-1", "A-3", "A-5", "A-8"]


def test_selected_pages_accept_range_notation(client: TestClient) -> None:
    """和「转图片」用同一个解析器，所以 1-3 在这里同样有效。"""
    result = split(
        client,
        upload(client, build_labeled_pdf("A", 5))["input_id"],
        mode="selected",
        pages="2-4",
    ).json()

    assert pdf_page_texts(fetch(client, result)) == ["A-2", "A-3", "A-4"]


def test_selected_pages_all(client: TestClient) -> None:
    result = split(
        client,
        upload(client, build_labeled_pdf("A", 3))["input_id"],
        mode="selected",
        pages="all",
    ).json()

    assert result["page_count"] == 3
    assert pdf_page_texts(fetch(client, result)) == ["A-1", "A-2", "A-3"]


def test_selected_pages_reject_empty(client: TestClient) -> None:
    response = split(
        client,
        upload(client, build_labeled_pdf("A", 3))["input_id"],
        mode="selected",
        pages="",
    )
    assert response.status_code == 400
    assert "页码" in response.json()["error"]["message"]


def test_selected_pages_reject_out_of_bounds(client: TestClient) -> None:
    response = split(
        client,
        upload(client, build_labeled_pdf("A", 3))["input_id"],
        mode="selected",
        pages="9",
    )
    assert response.status_code == 400
    assert "第 9 页不存在" in response.json()["error"]["message"]


# ----------------------------------------------------------------------
# 参数与结果处理
# ----------------------------------------------------------------------

def test_invalid_mode_is_rejected(client: TestClient) -> None:
    response = split(
        client,
        upload(client, build_labeled_pdf("A", 2))["input_id"],
        mode="chapters",
    )
    assert response.status_code == 400
    message = response.json()["error"]["message"]
    assert "每页一个 PDF" in message
    assert "按范围拆分" in message


def test_every_mode_ignores_leftover_pages_text(client: TestClient) -> None:
    """前端切换方式时不必清空输入框，残留的范围不该影响结果。"""
    result = split(
        client,
        upload(client, build_labeled_pdf("A", 2))["input_id"],
        mode="every",
        pages="1-2",
    ).json()

    assert [item["filename"] for item in result["files"]] == ["part-01.pdf", "part-02.pdf"]


def test_expired_input_is_rejected(client: TestClient) -> None:
    response = split(client, "0" * 32, mode="every")
    assert response.status_code == 404
    assert "重新上传" in response.json()["error"]["message"]


def test_result_is_one_time_download(client: TestClient) -> None:
    result = split(
        client,
        upload(client, build_labeled_pdf("A", 3))["input_id"],
        mode="every",
    ).json()

    url = result["download_url"]
    assert client.get(url).status_code == 200
    assert client.get(url).status_code == 404


def test_input_survives_after_download_so_user_can_split_again(client: TestClient) -> None:
    """下载拆分结果不该把上传的原始文件一起删掉 —— 用户往往要换种拆法再试。"""
    info = upload(client, build_labeled_pdf("A", 4))
    first = split(client, info["input_id"], mode="every").json()
    fetch(client, first)

    second = split(client, info["input_id"], mode="ranges", pages="1-2\n3-4")
    assert second.status_code == 200, second.text
    assert zip_page_texts(fetch(client, second.json())) == {
        "part-01.pdf": ["A-1", "A-2"],
        "part-02.pdf": ["A-3", "A-4"],
    }


def test_temp_dirs_are_cleaned_up_after_download(client: TestClient) -> None:
    """下载后只应剩下上传的输入目录，结果目录必须已经删掉。"""
    before = set(Path(tempfile.gettempdir()).glob("filetools_*"))
    info = upload(client, build_labeled_pdf("A", 3))
    uploaded = set(Path(tempfile.gettempdir()).glob("filetools_*")) - before
    assert len(uploaded) == 1

    result = split(client, info["input_id"], mode="every").json()
    fetch(client, result)

    # 上传目录（1 个）留着继续用，本次拆分新建的目录已经清理
    assert set(Path(tempfile.gettempdir()).glob("filetools_*")) - before == uploaded


def test_split_result_pdfs_are_valid(client: TestClient) -> None:
    result = split(
        client,
        upload(client, build_labeled_pdf("A", 3))["input_id"],
        mode="ranges",
        pages="1-3",
    ).json()

    raw = fetch(client, result)
    with pymupdf.open("pdf", raw) as doc:
        assert doc.is_pdf
        assert not doc.needs_pass
        for index in range(doc.page_count):
            assert doc[index].get_pixmap().width > 0


def test_split_preserves_page_images(client: TestClient) -> None:
    """拆分后的页面要保留图片资源，而不是只剩文字。"""
    result = split(
        client,
        upload(client, build_pdf_bytes(2))["input_id"],
        mode="every",
    ).json()

    with zipfile.ZipFile(io.BytesIO(fetch(client, result))) as archive:
        for name in archive.namelist():
            with pymupdf.open("pdf", archive.read(name)) as doc:
                assert doc[0].get_images(full=True), f"{name} 丢了图片"
