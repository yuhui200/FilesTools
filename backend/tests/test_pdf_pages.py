"""页面范围解析的单元测试。

四个 PDF 功能共用这一份解析，所以边界要在这里钉死。
"""

from __future__ import annotations

import pytest

from pdf.pages import format_page_list, page_labels, parse_page_range
from utils.errors import ValidationError


# ----------------------------------------------------------------------
# 正常解析
# ----------------------------------------------------------------------

@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("all", [0, 1, 2, 3, 4]),
        ("ALL", [0, 1, 2, 3, 4]),
        ("", [0, 1, 2, 3, 4]),
        (None, [0, 1, 2, 3, 4]),
        ("1-3", [0, 1, 2]),
        ("2-5", [1, 2, 3, 4]),
        ("1,3,5", [0, 2, 4]),
        ("1-3,5", [0, 1, 2, 4]),
        ("1-2,4-5", [0, 1, 3, 4]),
        ("5", [4]),
        ("1-5", [0, 1, 2, 3, 4]),
    ],
)
def test_parse_valid_ranges(text: str | None, expected: list[int]) -> None:
    assert parse_page_range(text, 5) == expected


def test_parse_whole_document_with_six_pages() -> None:
    """需求里举的例子 2-6，前提是文档有 6 页。"""
    assert parse_page_range("2-6", 6) == [1, 2, 3, 4, 5]


@pytest.mark.parametrize("text", ["1，3，5", "1;3;5", "1 3 5", " 1 , 3 , 5 ", "1、3、5".replace("、", ",")])
def test_parse_accepts_common_separators(text: str) -> None:
    """用户从中英文输入法里粘来的分隔符都要能认。"""
    assert parse_page_range(text, 5) == [0, 2, 4]


@pytest.mark.parametrize("text", ["1-2；4", "1-2;4", "1-2 4"])
def test_parse_accepts_separators_between_ranges(text: str) -> None:
    assert parse_page_range(text, 5) == [0, 1, 3]


@pytest.mark.parametrize("text", ["1–3", "1—3", "1~3"])
def test_parse_accepts_unicode_dashes(text: str) -> None:
    """中文输入法打出来的是全角连接号，不能当成格式错误。"""
    assert parse_page_range(text, 5) == [0, 1, 2]


def test_parse_dedupes_and_sorts() -> None:
    """重复的页码只算一次，并且始终按页码升序 —— 页序由文档决定。"""
    assert parse_page_range("5,1,3,3,1", 5) == [0, 2, 4]
    assert parse_page_range("3-4,1,2", 5) == [0, 1, 2, 3]


# ----------------------------------------------------------------------
# 非法输入
# ----------------------------------------------------------------------

@pytest.mark.parametrize(
    ("text", "keyword"),
    [
        ("abc", "格式不正确"),
        ("1-", "格式不正确"),
        ("-3", "格式不正确"),
        ("1.5", "格式不正确"),
        ("1--3", "格式不正确"),
        ("1,2,x", "格式不正确"),
    ],
)
def test_parse_rejects_garbage(text: str, keyword: str) -> None:
    with pytest.raises(ValidationError) as excinfo:
        parse_page_range(text, 5)
    assert keyword in excinfo.value.message


@pytest.mark.parametrize("text", ["9", "1-9", "0", "0-3", "1,9"])
def test_parse_rejects_out_of_range(text: str) -> None:
    with pytest.raises(ValidationError) as excinfo:
        parse_page_range(text, 5)
    message = excinfo.value.message
    assert "共 5 页" in message or "从 1 开始" in message


def test_parse_rejects_reversed_range() -> None:
    with pytest.raises(ValidationError) as excinfo:
        parse_page_range("5-2", 5)
    assert "起始页不能大于结束页" in excinfo.value.message


def test_parse_rejects_empty_document() -> None:
    with pytest.raises(ValidationError) as excinfo:
        parse_page_range("all", 0)
    assert "没有任何页面" in excinfo.value.message


def test_parse_rejects_only_separators() -> None:
    with pytest.raises(ValidationError) as excinfo:
        parse_page_range(",,,", 5)
    assert "至少选择一页" in excinfo.value.message


# ----------------------------------------------------------------------
# 展示
# ----------------------------------------------------------------------

@pytest.mark.parametrize(
    ("pages", "expected"),
    [
        ([0, 1, 2], "1-3"),
        ([0, 2, 4], "1,3,5"),
        ([0, 1, 2, 4, 5], "1-3,5-6"),
        ([3], "4"),
        ([], ""),
        ([2, 1, 0], "1-3"),
    ],
)
def test_format_page_list(pages: list[int], expected: str) -> None:
    assert format_page_list(pages) == expected


@pytest.mark.parametrize(
    ("pages", "expected"),
    [
        ([0], ["01"]),
        ([0, 9], ["01", "10"]),
        ([0, 99, 100], ["001", "100", "101"]),
    ],
)
def test_page_labels_zero_pad_for_sorting(pages: list[int], expected: list[str]) -> None:
    """文件名要补零，否则 ZIP 里 page-10 会排在 page-2 前面。"""
    assert page_labels(pages) == expected
