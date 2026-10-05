"""TXT 排版层的单元测试（第五阶段）。

这一层**不需要 LibreOffice**：TXT 走 PyMuPDF 自己排版，所以这些用例
在任何机器上都应该跑得动，也正因为如此，它们必须把排版的正确性钉死 ——
没有转换器帮我们兜底。

四个排版选项不能是「假控件」（选了没效果），所以每一项都要验到**产物真的变了**：
字号变大 → 页数变多；换页面大小/方向 → 页面矩形真的变了；
换字体 → PDF 里嵌入的字体真的不一样。最后一条尤其重要：PyMuPDF 的内置
中文码实测全部指向同一个 Droid Sans Fallback，如果字体探测写错成内置码，
四项里就有了一项是假的，而界面看上去一切正常。

另外把「排版不能死循环、不能爆掉」这条钉住：单个超长无空白串在
``fill_textbox`` 里是三次方耗时（实测 2 万字 139 秒），所以必须封顶。

用例里一律用 ``default_font()`` 取本机真正探测到的第一个字体，
不写死 "song" —— 否则在没有宋体的机器（比如只装了 Noto 的 Linux）上会红。
"""

from __future__ import annotations

import time
from pathlib import Path

import pymupdf
import pytest

from config import settings
from office import txt_to_pdf
from office.txt_to_pdf import (
    TxtOptions,
    available_fonts,
    build_pdf_from_text,
    resolve_font,
)
from utils.errors import ValidationError

#: 一段有中文、英文、数字、标点的正文，够长到跨页
BODY = "第{i}段：中文排版测试，标点、English words 与数字 12345 混排。\n"


def default_font() -> str:
    """本机探测到的第一个字体键。"""
    return available_fonts()[0].key


def body_text(chars: int = 6_000) -> str:
    return (BODY * (chars // 30 + 1))[:chars]


def open_result(result: txt_to_pdf.TxtBuildResult) -> pymupdf.Document:
    return pymupdf.open(stream=result.data, filetype="pdf")


def read_text(result: txt_to_pdf.TxtBuildResult) -> str:
    with open_result(result) as doc:
        return "".join(page.get_text() for page in doc)


def stripped(text: str) -> str:
    """去掉全部空白再比对。

    PDF 取文字时每个软换行都会带出一个空白，带空白比对会得到假失败。
    去空白比对能抓住真正要防的错：少字、多字、丢一段。
    """
    return "".join(text.split())


# ----------------------------------------------------------------------
# 字体探测
# ----------------------------------------------------------------------

def test_available_fonts_are_usable() -> None:
    """探测到的字体必须真的能加载 —— 列出名字却加载不了等于没这个选项。"""
    fonts = available_fonts()
    assert fonts, "本机至少应该有内置字体可用"

    for item in fonts:
        assert item.key and item.label
        font = (
            pymupdf.Font(item.key) if item.path is None else pymupdf.Font(fontfile=str(item.path))
        )
        assert font.name, f"{item.label} 加载后没有字体名"

    keys = [item.key for item in fonts]
    assert len(keys) == len(set(keys)), f"字体键重复：{keys}"


def test_font_files_actually_exist() -> None:
    """报出来的路径必须存在，否则排版时才失败就太晚了。"""
    for item in available_fonts():
        if item.path is not None:
            assert item.path.is_file(), f"{item.label} 指向的文件不存在：{item.path}"


def test_font_keys_are_ascii() -> None:
    """字体键要当表单值传，必须是 ASCII；中文显示名放在 label 里。"""
    for item in available_fonts():
        assert item.key.isascii(), f"字体键不是 ASCII：{item.key!r}"


def test_fonts_pymupdf_cannot_read_are_skipped_not_offered(monkeypatch) -> None:
    """MuPDF 读不动的字体文件不许被列出来 —— 列出来就是个静默的坏选项。

    起因是 2026-10-05 的 CI：Ubuntu 装了 fonts-noto-cjk 之后，Linux 上
    ``available_fonts()[0]`` 变成 NotoSansCJK-Regular.ttc，于是
    TXT→PDF / Markdown→PDF 两条路一起坏 —— 一页中文 13.7 MB（子集化静默
    失效），抽出来的字被换成 U+00A0 / U+2011，引用块整段消失。
    本机没有那个文件，所以用桩把它摆进来。

    ``test_available_fonts_are_usable`` 拦不住这个：那个文件**能**加载、
    也有字体名，只是排出来的东西是坏的。所以这里要单独钉一条。
    """
    blocked = settings.TXT_FONT_FILES_PYMUPDF_CANNOT_READ
    assert blocked, "拦截表是空的？那 NotoSansCJK 那个坑就原样回来了"

    # 候选表里 Noto 的第一个文件名，正是实测读不动的那个。它要是被改掉了，
    # 这条断言会红 —— 提醒改的人回去重新实测一遍，别照抄结论。
    ttc, otf = settings.TXT_FONT_CANDIDATES["noto"][1][:2]
    assert ttc.lower() in blocked, f"Noto 的首选文件名 {ttc!r} 不在拦截表里"

    monkeypatch.setattr(txt_to_pdf, "_font_cache", None)
    monkeypatch.setattr(
        txt_to_pdf, "_scan_font_files", lambda: {ttc.lower(): Path("/nowhere") / ttc}
    )
    fonts = available_fonts()
    assert [item.key for item in fonts] == [settings.TXT_FALLBACK_FONT], (
        f"读不动的字体被列出来了，或者没落到内置字体上：{[i.key for i in fonts]}"
    )

    # 拦的是**那个文件**，不是「noto」这个字体键：只有单体 .otf 时仍该可选。
    monkeypatch.setattr(txt_to_pdf, "_font_cache", None)
    monkeypatch.setattr(
        txt_to_pdf, "_scan_font_files", lambda: {otf.lower(): Path("/nowhere") / otf}
    )
    assert [item.key for item in available_fonts()] == ["noto"]


def test_falls_back_to_builtin_when_no_system_font_is_found(monkeypatch) -> None:
    """一个系统字体都探测不到时，只给内置字体一项，而且还能真的排出版来。"""
    monkeypatch.setattr(txt_to_pdf, "_font_cache", None)
    monkeypatch.setattr(txt_to_pdf, "_scan_font_files", lambda: {})

    fonts = available_fonts()
    assert len(fonts) == 1
    assert fonts[0].key == settings.TXT_FALLBACK_FONT
    assert fonts[0].label == settings.TXT_FALLBACK_FONT_LABEL
    assert fonts[0].path is None

    # 内置字体这条路要真的能排出版，不能只是列在那里
    result = build_pdf_from_text("内置字体也要能排中文。", TxtOptions(font=fonts[0].key))
    assert result.page_count == 1
    assert "内置字体也要能排中文" in stripped(read_text(result))


def test_unknown_font_is_rejected_not_silently_replaced() -> None:
    """不认识的字体要报错，不能悄悄换成别的 —— 选了宋体得到黑体更难发现。"""
    with pytest.raises(ValidationError) as excinfo:
        resolve_font("不存在的字体")
    assert "可选的是" in str(excinfo.value)


# ----------------------------------------------------------------------
# 排版基本正确性
# ----------------------------------------------------------------------

def test_produces_a_readable_pdf_with_the_full_text() -> None:
    text = body_text(6_000)
    result = build_pdf_from_text(text, TxtOptions(font=default_font()))

    assert result.data.startswith(b"%PDF")
    assert result.page_count >= 2, "6000 字在 A4 上排不下两页说明排版没生效"

    with open_result(result) as doc:
        assert doc.page_count == result.page_count
    assert stripped(text) == stripped(read_text(result)), "排出来的文字和原文对不上"


def test_notes_describe_what_was_actually_done() -> None:
    chosen = available_fonts()[0]
    result = build_pdf_from_text(
        body_text(3_000),
        TxtOptions(font=chosen.key, font_size=12, page_size="a4", orientation="landscape"),
    )
    note = " ".join(result.notes)

    assert chosen.label in note
    assert "字号 12" in note
    assert "A4" in note
    assert "横向" in note
    assert f"{result.page_count} 页" in note


def test_bigger_font_makes_more_pages() -> None:
    """字号必须真的生效。"""
    text = body_text(4_000)
    key = default_font()
    small = build_pdf_from_text(text, TxtOptions(font=key, font_size=8))
    large = build_pdf_from_text(text, TxtOptions(font=key, font_size=24))

    assert large.page_count > small.page_count, "字号调大页数没变，说明字号没生效"


def test_page_size_and_orientation_change_the_page_rect() -> None:
    """页面大小与方向必须真的生效 —— 验页面矩形，不验界面上的选中状态。"""
    key = default_font()
    text = body_text(2_000)

    def first_page(**kwargs) -> pymupdf.Rect:
        result = build_pdf_from_text(text, TxtOptions(font=key, **kwargs))
        with open_result(result) as doc:
            return doc[0].rect

    a4 = first_page(page_size="a4", orientation="portrait")
    a5 = first_page(page_size="a5", orientation="portrait")
    landscape = first_page(page_size="a4", orientation="landscape")

    assert (a4.width, a4.height) == pytest.approx(settings.PDF_PAGE_SIZES["a4"])
    assert (a5.width, a5.height) == pytest.approx(settings.PDF_PAGE_SIZES["a5"])
    assert landscape.width > landscape.height, "横向页面的宽应该大于高"
    assert landscape.width == pytest.approx(a4.height), "横向就是把纵向转 90 度"
    assert landscape.height == pytest.approx(a4.width)


def test_font_choice_changes_the_embedded_font() -> None:
    """换字体要换掉 PDF 里真正嵌入的字体。

    这是「字体不是假控件」的硬证据：如果哪天有人把字体探测改回 PyMuPDF 的
    内置中文码（china-s / china-ss …），它们全部解析成同一个 Droid Sans
    Fallback，这个断言就会失败。
    """
    fonts = [item for item in available_fonts() if item.path is not None]
    if len(fonts) < 2:
        pytest.skip("本机只探测到一个系统字体，无法比较")

    names = []
    for item in fonts[:2]:
        result = build_pdf_from_text("字体对比测试。", TxtOptions(font=item.key))
        with open_result(result) as doc:
            used = doc.get_page_fonts(0)
        assert used, f"{item.label} 没有嵌入任何字体"
        names.append(used[0][3])

    assert names[0] != names[1], f"两种字体嵌进去是同一个：{names}"


def test_subset_fonts_keeps_the_file_small() -> None:
    """必须子集化：不子集化一页中文 1.62 MB，子集化后只有十几 KB。"""
    result = build_pdf_from_text(body_text(1_000), TxtOptions(font=default_font()))
    assert len(result.data) < 60 * 1024, (
        f"产物 {len(result.data) / 1024:.0f} KB，多半是漏了 doc.subset_fonts()"
    )


# ----------------------------------------------------------------------
# 危险输入：不能死循环、不能爆掉
# ----------------------------------------------------------------------

def test_a_single_unbreakable_run_finishes_quickly() -> None:
    """整篇没有空白、也没有换行的超长串是这里最坏的形状。

    实测（见 office/txt_to_pdf.py 的模块说明）：单次 fill_textbox 的耗时是
    喂入字数的三次方，2 万字要 139 秒。封顶之后应该是一两秒的事。
    这里给 30 秒的上限 —— 不是精确的性能断言，而是「别退化成几分钟」的护栏。
    """
    text = "A" * 20_000
    start = time.monotonic()
    result = build_pdf_from_text(text, TxtOptions(font=default_font()))
    elapsed = time.monotonic() - start

    assert elapsed < 30, f"2 万字无空白串用了 {elapsed:.1f} 秒，封顶逻辑失效了"
    assert stripped(text) == stripped(read_text(result)), "超长串排出来少字了"


def test_pure_chinese_without_spaces_finishes_quickly() -> None:
    """纯汉字没有空格，也是同一类危险形状（实测同样慢）。"""
    text = "中文排版测试标点符号也要一起排进去看看会怎么样" * 400
    start = time.monotonic()
    result = build_pdf_from_text(text, TxtOptions(font=default_font()))
    elapsed = time.monotonic() - start

    assert elapsed < 30, f"纯汉字长段用了 {elapsed:.1f} 秒"
    assert stripped(text) == stripped(read_text(result))


def test_a_long_run_inside_normal_text_is_handled() -> None:
    """正文里夹一条长串：既不能丢字，也不能把前后两段挤掉。"""
    text = "正常的一段。\n" + "B" * 30_000 + "\n正常的一段。"
    result = build_pdf_from_text(text, TxtOptions(font=default_font()))

    assert stripped(read_text(result)) == stripped(text)
    assert result.page_count > 3


# ----------------------------------------------------------------------
# 边界与上限
# ----------------------------------------------------------------------

def test_empty_text_produces_one_page_and_does_not_crash() -> None:
    """空文本排一页空白，不报错。（接口层另有「没有任何内容」的拒绝，见 loader。）"""
    result = build_pdf_from_text("", TxtOptions(font=default_font()))
    assert result.page_count == 1


def test_blank_lines_push_text_down() -> None:
    """空行要真的占高度，而不是被吃掉。"""
    key = default_font()

    def last_line_bottom(text: str) -> float:
        result = build_pdf_from_text(text, TxtOptions(font=key))
        with open_result(result) as doc:
            words = doc[0].get_text("words")
        assert words, "页面上一个字都没有"
        return max(word[3] for word in words)

    tight = last_line_bottom("第一段\n第二段\n")
    loose = last_line_bottom("第一段\n\n\n第二段\n")

    assert loose > tight + 10, "加了两个空行，第二段却没有往下移"


def test_windows_line_endings_are_normalized() -> None:
    """Windows 的 \\r\\n 要处理掉，不能把 \\r 当正文排进去。"""
    result = build_pdf_from_text(
        "第一行\r\n第二行\r\n", TxtOptions(font=default_font())
    )
    got = read_text(result)

    assert "第一行" in got and "第二行" in got
    assert "\r" not in got, "\\r 被排进 PDF 了"


def test_tabs_are_expanded_not_dropped() -> None:
    """制表符展开成空格，内容不能丢。"""
    result = build_pdf_from_text("甲\t乙\n", TxtOptions(font=default_font()))
    assert stripped(read_text(result)) == "甲乙"


def test_too_much_text_is_rejected(monkeypatch) -> None:
    monkeypatch.setattr(settings, "MAX_TXT_CHARS", 100)
    with pytest.raises(ValidationError) as excinfo:
        build_pdf_from_text("字" * 101, TxtOptions(font=default_font()))
    assert "太长" in str(excinfo.value)


def test_too_many_pages_is_rejected(monkeypatch) -> None:
    """换行极多的文件靠页数上限兜住，而且要**在排完之前**就停下来。"""
    monkeypatch.setattr(settings, "MAX_TXT_PAGES", 3)
    # 文本要真的排得超过 3 页，否则测的是「没超上限」，什么也没验到
    with pytest.raises(ValidationError) as excinfo:
        build_pdf_from_text(body_text(20_000), TxtOptions(font=default_font()))
    assert "页" in str(excinfo.value)


def test_a_page_too_small_for_the_font_is_rejected(monkeypatch) -> None:
    """页面小到一行都放不下时要明确报错，不能死循环。"""
    monkeypatch.setitem(settings.PDF_PAGE_SIZES, "a5", (100.0, 60.0))
    with pytest.raises(ValidationError) as excinfo:
        build_pdf_from_text(
            "内容", TxtOptions(font=default_font(), page_size="a5", font_size=32)
        )
    assert "放不下一行" in str(excinfo.value)


def test_font_size_out_of_range_is_clamped() -> None:
    """接口层已经卡过范围，这里再夹一次，防止有人绕过接口直接调用。"""
    key = default_font()
    tiny = build_pdf_from_text("内容", TxtOptions(font=key, font_size=1))
    assert f"字号 {settings.TXT_MIN_FONT_SIZE}" in " ".join(tiny.notes)

    huge = build_pdf_from_text("内容", TxtOptions(font=key, font_size=999))
    assert f"字号 {settings.TXT_MAX_FONT_SIZE}" in " ".join(huge.notes)
