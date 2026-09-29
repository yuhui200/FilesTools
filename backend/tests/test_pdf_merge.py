"""PDF 合并的端到端测试。

对应第三阶段 §17 的「PDF 合并」一项，重点是**页序**：
合并的意义就在于顺序完全由用户决定，任何自动排序都是 bug。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pymupdf
import pytest
from fastapi.testclient import TestClient

from tests.conftest import build_labeled_pdf, build_pdf_bytes, pdf_files

ENDPOINT = "/api/pdf/merge"


def merge(client: TestClient, *items: tuple[str, bytes], field: str = "files"):
    return client.post(ENDPOINT, files=pdf_files(*items, field=field))


def download(client: TestClient, result: dict) -> pymupdf.Document:
    response = client.get(result["download_url"])
    assert response.status_code == 200, response.text
    assert response.content.startswith(b"%PDF-")
    return pymupdf.open("pdf", response.content)


def page_texts(doc: pymupdf.Document) -> list[str]:
    return [doc[index].get_text().strip() for index in range(doc.page_count)]


# ----------------------------------------------------------------------
# 页序
# ----------------------------------------------------------------------

def test_merge_keeps_upload_order(client: TestClient) -> None:
    """合并后的页序必须与上传顺序完全一致。"""
    response = merge(
        client,
        ("a.pdf", build_labeled_pdf("A", 2)),
        ("b.pdf", build_labeled_pdf("B", 3)),
        ("c.pdf", build_labeled_pdf("C", 1)),
    )
    assert response.status_code == 200, response.text

    result = response.json()
    assert result["page_count"] == 6

    with download(client, result) as doc:
        assert page_texts(doc) == ["A-1", "A-2", "B-1", "B-2", "B-3", "C-1"]


def test_reordering_changes_result(client: TestClient) -> None:
    """换个顺序传，页序就要跟着变 —— 说明顺序真的来自请求而不是别的。"""
    first = build_labeled_pdf("A", 1)
    second = build_labeled_pdf("B", 1)

    with download(client, merge(client, ("a.pdf", first), ("b.pdf", second)).json()) as doc:
        assert page_texts(doc) == ["A-1", "B-1"]

    with download(client, merge(client, ("b.pdf", second), ("a.pdf", first)).json()) as doc:
        assert page_texts(doc) == ["B-1", "A-1"]


def test_same_file_twice(client: TestClient) -> None:
    """同一份 PDF 传两次是合法用法（用户就是想重复几页）。"""
    data = build_labeled_pdf("A", 2)
    response = merge(client, ("a.pdf", data), ("a-again.pdf", data))
    assert response.status_code == 200, response.text

    result = response.json()
    assert result["page_count"] == 4
    with download(client, result) as doc:
        assert page_texts(doc) == ["A-1", "A-2", "A-1", "A-2"]


# ----------------------------------------------------------------------
# 结果信息
# ----------------------------------------------------------------------

def test_single_file_merge(client: TestClient) -> None:
    """只传一份也要能合并（等价于复制一份），不应报错。"""
    response = merge(client, ("only.pdf", build_pdf_bytes(3)))
    assert response.status_code == 200, response.text

    result = response.json()
    assert result["page_count"] == 3
    assert result["archived"] is False
    assert result["filename"] == "merged.pdf"
    assert result["media_type"] == "application/pdf"


def test_reports_total_pages_and_size(client: TestClient) -> None:
    """要能显示总页数与大小。"""
    a = build_pdf_bytes(2)
    b = build_pdf_bytes(3)
    result = merge(client, ("a.pdf", a), ("b.pdf", b)).json()

    assert result["page_count"] == 5
    assert result["original_size"] == len(a) + len(b)
    assert result["size"] > 0
    assert result["notes"] and "5 页" in result["notes"][0]
    assert result["files"][0]["page_count"] == 5


def test_result_is_one_time_download(client: TestClient) -> None:
    result = merge(client, ("a.pdf", build_pdf_bytes(1))).json()
    url = result["download_url"]
    assert client.get(url).status_code == 200
    assert client.get(url).status_code == 404


def test_pdf_links_survive_merge(client: TestClient) -> None:
    """目录/链接这类注释不该因为合并而丢失。"""
    source = pymupdf.open()
    page = source.new_page(width=400, height=400)
    page.insert_link(
        {
            "kind": pymupdf.LINK_URI,
            "from": pymupdf.Rect(50, 50, 200, 100),
            "uri": "https://example.com/",
        }
    )
    data = source.tobytes()
    source.close()

    result = merge(client, ("link.pdf", data)).json()
    with download(client, result) as doc:
        links = doc[0].get_links()
        assert len(links) == 1
        assert links[0]["uri"] == "https://example.com/"


# ----------------------------------------------------------------------
# 非法输入
# ----------------------------------------------------------------------

def test_broken_pdf_in_the_middle_is_rejected(client: TestClient) -> None:
    """中间夹一份损坏的 PDF，整批就失败 —— 缺页的结果比报错更难发现。"""
    before = set(Path(tempfile.gettempdir()).glob("filetools_*"))
    response = merge(
        client,
        ("a.pdf", build_pdf_bytes(2)),
        ("broken.pdf", b"%PDF-1.7\n" + b"\x00" * 200),
        ("c.pdf", build_pdf_bytes(1)),
    )
    assert response.status_code == 400
    assert "损坏" in response.json()["error"]["message"]

    # 失败后不留垃圾，也不会因为文件被占用而覆盖掉真正的错误
    assert set(Path(tempfile.gettempdir()).glob("filetools_*")) - before == set()


def test_non_pdf_is_rejected(client: TestClient) -> None:
    response = merge(
        client,
        ("a.pdf", build_pdf_bytes(1)),
        ("notes.txt", b"just some text"),
    )
    assert response.status_code == 415
    assert "PDF" in response.json()["error"]["message"]


def test_empty_file_is_rejected(client: TestClient) -> None:
    response = merge(client, ("a.pdf", build_pdf_bytes(1)), ("empty.pdf", b""))
    assert response.status_code == 400


def test_too_many_files_are_rejected(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from config import settings

    monkeypatch.setattr(settings, "MAX_BATCH_FILES", 2)
    response = merge(
        client,
        ("a.pdf", build_pdf_bytes(1)),
        ("b.pdf", build_pdf_bytes(1)),
        ("c.pdf", build_pdf_bytes(1)),
    )
    assert response.status_code == 400
    assert "2" in response.json()["error"]["message"]


def test_merged_page_limit(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """单份都不超标，合起来超标也要拒绝。"""
    from config import settings

    monkeypatch.setattr(settings, "MAX_PDF_PAGES", 5)
    response = merge(
        client,
        ("a.pdf", build_pdf_bytes(3)),
        ("b.pdf", build_pdf_bytes(3)),
    )
    assert response.status_code == 400
    message = response.json()["error"]["message"]
    assert "合并后共 6 页" in message
    assert "5 页" in message


def test_page_limit_inside_single_file(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """单份就超过上限时，提示应该指向那份文件本身。"""
    from config import settings

    monkeypatch.setattr(settings, "MAX_PDF_PAGES", 2)
    response = merge(client, ("big.pdf", build_pdf_bytes(4)))
    assert response.status_code == 400
    assert "该 PDF 有 4 页" in response.json()["error"]["message"]


def test_uploaded_sources_are_cleaned_up_after_download(client: TestClient) -> None:
    """下载后整个任务目录（含上传的来源文件）都要清掉。"""
    before = set(Path(tempfile.gettempdir()).glob("filetools_*"))
    result = merge(
        client,
        ("a.pdf", build_pdf_bytes(2)),
        ("b.pdf", build_pdf_bytes(2)),
    ).json()
    client.get(result["download_url"])

    assert set(Path(tempfile.gettempdir()).glob("filetools_*")) - before == set()


def test_merge_output_is_readable_by_pymupdf(client: TestClient) -> None:
    """合并结果必须是结构完好的 PDF，而不是拼起来的字节。"""
    result = merge(
        client,
        ("a.pdf", build_pdf_bytes(2)),
        ("b.pdf", build_pdf_bytes(3)),
    ).json()

    raw = client.get(result["download_url"]).content
    with pymupdf.open("pdf", raw) as doc:
        assert doc.page_count == 5
        assert doc.is_pdf
        assert not doc.needs_pass
        # 每一页都还能渲染
        for index in range(doc.page_count):
            assert doc[index].get_pixmap().width > 0


def test_merge_does_not_register_source_tokens(client: TestClient) -> None:
    """合并的来源文件不登记令牌：它们是过程文件，不该留下可下载的入口。"""
    from services.job_store import job_store

    before = job_store.count()
    result = merge(
        client,
        ("a.pdf", build_pdf_bytes(1)),
        ("b.pdf", build_pdf_bytes(1)),
    ).json()

    # 只多了 1 个结果任务，没有为两份来源各留一个
    assert job_store.count() == before + 1
    assert client.get(result["download_url"]).status_code == 200
