"""``office/loader.py` 的内容校验测试。

这一层不碰 LibreOffice，所以永远会跑。测的是三件事：
扩展名白名单、容器魔数、**容器内部结构是否真的属于所声称的类型**。

最后一条是重点。只看魔数的话，一个改了扩展名的安装包或普通 zip
会一路走到转换器，用户看到的是「文件转换失败」；
分层判断之后，用户看到的是「这个格式不支持」，两个提示指向的动作不一样。
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from office.loader import (
    BROKEN_OFFICE_MESSAGE,
    BROKEN_TEXT_MESSAGE,
    MISMATCH_MESSAGE,
    UNSUPPORTED_OFFICE_MESSAGE,
    count_excel_sheets,
    count_powerpoint_slides,
    decode_text,
    detect_kind,
    looks_like_ole2,
    looks_like_ooxml,
    validate_office_upload,
)
from tests.conftest import (
    build_docx_bytes,
    build_ole2_bytes,
    build_pptx_bytes,
    build_xlsx_bytes,
    zip_bytes,
)
from utils.errors import (
    CorruptedFileError,
    ErrorCode,
    UnsupportedTypeError,
    ValidationError,
)


def write(tmp_path: Path, name: str, data: bytes) -> Path:
    """按给定文件名落盘，返回路径。"""
    target = tmp_path / name
    target.write_bytes(data)
    return target


def validate(tmp_path: Path, name: str, data: bytes):
    return validate_office_upload(write(tmp_path, name, data), name)


# ----------------------------------------------------------------------
# 正常文件
# ----------------------------------------------------------------------

@pytest.mark.parametrize(
    ("name", "builder", "kind"),
    [
        ("report.docx", build_docx_bytes, "word"),
        ("book.xlsx", build_xlsx_bytes, "excel"),
        ("deck.pptx", build_pptx_bytes, "powerpoint"),
    ],
)
def test_accepts_well_formed_ooxml(tmp_path: Path, name, builder, kind) -> None:
    result = validate(tmp_path, name, builder())

    assert result.kind == kind
    assert result.filename == name
    assert result.extension == f".{name.rsplit('.', 1)[-1]}"
    assert result.size > 0
    assert result.kind_label  # 界面上要显示的中文名，不能是空的


def test_accepts_plain_text(tmp_path: Path) -> None:
    result = validate(tmp_path, "notes.txt", "第一行\n第二行\n".encode("utf-8"))

    assert result.kind == "text"
    assert result.kind_label == "文本文件"


def test_extension_matching_is_case_insensitive(tmp_path: Path) -> None:
    result = validate(tmp_path, "REPORT.DOCX", build_docx_bytes())

    assert result.kind == "word"
    assert result.extension == ".docx"


@pytest.mark.parametrize("ext", [".doc", ".xls", ".ppt"])
def test_accepts_legacy_ole2_with_the_right_stream(tmp_path: Path, ext) -> None:
    from tests.conftest import OLE2_STREAM_BY_EXT

    result = validate(tmp_path, f"old{ext}", build_ole2_bytes(OLE2_STREAM_BY_EXT[ext.strip(".")]))

    assert result.extension == ext


def test_excel_also_accepts_the_old_book_stream(tmp_path: Path) -> None:
    """Excel 5.0/95 用的是 Book 流名，只认 Workbook 会误杀老文件。"""
    result = validate(tmp_path, "old.xls", build_ole2_bytes("Book"))

    assert result.kind == "excel"


# ----------------------------------------------------------------------
# 扩展名不在白名单内
# ----------------------------------------------------------------------

@pytest.mark.parametrize("name", ["macro.docm", "data.csv", "page.rtf", "archive.zip", "noext"])
def test_rejects_extensions_outside_whitelist(tmp_path: Path, name: str) -> None:
    with pytest.raises(UnsupportedTypeError) as excinfo:
        validate(tmp_path, name, build_docx_bytes())

    assert excinfo.value.code == ErrorCode.INVALID_FILE_TYPE
    assert excinfo.value.message == UNSUPPORTED_OFFICE_MESSAGE


def test_detect_kind_returns_none_for_unknown(tmp_path: Path) -> None:
    assert detect_kind("a.csv") is None
    assert detect_kind("a.docx") == "word"


# ----------------------------------------------------------------------
# 容器与内容不符（改名文件）
# ----------------------------------------------------------------------

def test_rejects_executable_renamed_to_docx(tmp_path: Path) -> None:
    """MZ 开头的可执行文件改名成 .docx —— 连 zip 头都不是。"""
    with pytest.raises(UnsupportedTypeError) as excinfo:
        validate(tmp_path, "virus.docx", b"MZ\x90\x00" + b"\x00" * 4096)

    assert excinfo.value.code == ErrorCode.INVALID_FILE_TYPE
    assert excinfo.value.message == MISMATCH_MESSAGE


def test_rejects_xlsx_renamed_to_docx(tmp_path: Path) -> None:
    """真正的 Excel 文件改名成 .docx。

    这是最要紧的一种：它的 zip 头、[Content_Types].xml 都完全正常，
    只有「里面声明的是表格而不是文档」这一点不对劲。
    """
    with pytest.raises(UnsupportedTypeError) as excinfo:
        validate(tmp_path, "fake.docx", build_xlsx_bytes())

    assert excinfo.value.message == MISMATCH_MESSAGE


def test_rejects_docx_renamed_to_pptx(tmp_path: Path) -> None:
    with pytest.raises(UnsupportedTypeError) as excinfo:
        validate(tmp_path, "fake.pptx", build_docx_bytes())

    assert excinfo.value.message == MISMATCH_MESSAGE


def test_rejects_plain_zip_renamed_to_docx(tmp_path: Path) -> None:
    """普通压缩包改名：是个合法 zip，但没有 [Content_Types].xml。"""
    payload = zip_bytes({"a.txt": "hello", "b/c.txt": "world"})

    with pytest.raises(UnsupportedTypeError) as excinfo:
        validate(tmp_path, "bundle.docx", payload)

    assert excinfo.value.message == MISMATCH_MESSAGE


def test_rejects_non_ole2_renamed_to_doc(tmp_path: Path) -> None:
    """旧格式分支同样要看魔数：zip 改名成 .doc 不接受。"""
    with pytest.raises(UnsupportedTypeError) as excinfo:
        validate(tmp_path, "fake.doc", build_docx_bytes())

    assert excinfo.value.message == MISMATCH_MESSAGE


def test_rejects_ole2_without_the_expected_stream(tmp_path: Path) -> None:
    """OLE2 头是对的，但里面没有 Word 的流 —— 多半是改名成 .doc 的安装包。

    不做这一步的话它会一路走到 LibreOffice，
    用户看到「文件转换失败」而不是「格式不支持」，白等十几秒。
    """
    with pytest.raises(UnsupportedTypeError) as excinfo:
        validate(tmp_path, "installer.doc", build_ole2_bytes(None))

    assert excinfo.value.message == MISMATCH_MESSAGE


def test_rejects_word_ole2_renamed_to_xls(tmp_path: Path) -> None:
    """Word 的流名不该让 .xls 通过。"""
    with pytest.raises(UnsupportedTypeError):
        validate(tmp_path, "fake.xls", build_ole2_bytes("WordDocument"))


# ----------------------------------------------------------------------
# 内容损坏
# ----------------------------------------------------------------------

def test_rejects_truncated_docx(tmp_path: Path) -> None:
    """下载中断的半截 zip。"""
    whole = build_docx_bytes()

    with pytest.raises(CorruptedFileError) as excinfo:
        validate(tmp_path, "half.docx", whole[: len(whole) // 2])

    assert excinfo.value.code == ErrorCode.CORRUPTED_FILE
    assert excinfo.value.message == BROKEN_OFFICE_MESSAGE


def test_rejects_docx_missing_its_main_part(tmp_path: Path) -> None:
    """声明自己是 Word 文档，正文部件却不在包里。

    和「Excel 改名」不是一回事：这个要报「文件损坏」，
    因为用户该做的是重新拿一份文件，而不是换个格式。
    """
    parts = {
        "[Content_Types].xml": (
            '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org'
            '/package/2006/content-types"><Override PartName="/word/document.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.'
            'wordprocessingml.document.main+xml"/></Types>'
        ),
        "docProps/app.xml": "<Properties/>",
    }

    with pytest.raises(CorruptedFileError) as excinfo:
        validate(tmp_path, "broken.docx", zip_bytes(parts))

    assert excinfo.value.message == BROKEN_OFFICE_MESSAGE


def test_rejects_empty_file(tmp_path: Path) -> None:
    with pytest.raises(ValidationError) as excinfo:
        validate(tmp_path, "empty.docx", b"")

    assert excinfo.value.code == ErrorCode.INVALID_REQUEST
    assert "空" in excinfo.value.message


# ----------------------------------------------------------------------
# 文本文件
# ----------------------------------------------------------------------

@pytest.mark.parametrize(
    ("encoding", "text"),
    [
        ("utf-8", "纯中文内容，第一行\n第二行\n"),
        ("utf-8-sig", "带 BOM 的 UTF-8 内容\n"),
        ("gbk", "国标编码的中文内容\n"),
        ("utf-16", "UTF-16 编码的中文内容\n"),
    ],
)
def test_decodes_common_encodings(tmp_path: Path, encoding: str, text: str) -> None:
    path = write(tmp_path, "notes.txt", text.encode(encoding))

    decoded, used = decode_text(path)

    assert decoded == text
    assert used  # 实际用的是哪种编码，note 里要写清楚


def test_prefers_utf8_over_utf16(tmp_path: Path) -> None:
    """utf-16 没有 BOM 时几乎能「解开」任何字节，排在前面会把 gbk 解成乱码。"""
    path = write(tmp_path, "notes.txt", "中文内容\n".encode("gbk"))

    decoded, used = decode_text(path)

    assert used == "gbk"
    assert decoded == "中文内容\n"


def test_rejects_binary_renamed_to_txt(tmp_path: Path) -> None:
    """可执行文件改名成 .txt：头部有 NUL 又没有 UTF-16 BOM。"""
    with pytest.raises(CorruptedFileError) as excinfo:
        validate(tmp_path, "fake.txt", b"MZ\x90\x00" + b"\x00" * 500)

    assert excinfo.value.message == BROKEN_TEXT_MESSAGE


def test_rejects_binary_with_control_characters(tmp_path: Path) -> None:
    """能解码成功、也没有 NUL，但控制字符占了绝大多数 —— 还是二进制。"""
    payload = b"\x01\x02\x03\x04\x05\x06\x07" * 600

    with pytest.raises(CorruptedFileError) as excinfo:
        validate(tmp_path, "fake.txt", payload)

    assert excinfo.value.message == BROKEN_TEXT_MESSAGE


def test_rejects_whitespace_only_txt(tmp_path: Path) -> None:
    """空内容没有可转换的东西，早点说比转出一张白纸好。"""
    with pytest.raises(ValidationError) as excinfo:
        validate(tmp_path, "blank.txt", "   \n\n\t  ".encode("utf-8"))

    assert "内容" in excinfo.value.message


def test_accepts_utf16_with_bom_despite_nul_bytes(tmp_path: Path) -> None:
    """UTF-16 正文里到处是 NUL，但 BOM 说明它是文本而不是二进制。"""
    result = validate(tmp_path, "notes.txt", "中文 UTF-16 内容\n".encode("utf-16"))

    assert result.kind == "text"


# ----------------------------------------------------------------------
# 魔数辅助函数
# ----------------------------------------------------------------------

def test_magic_helpers(tmp_path: Path) -> None:
    docx = write(tmp_path, "a.docx", build_docx_bytes())
    ole = write(tmp_path, "a.doc", build_ole2_bytes("WordDocument"))
    text = write(tmp_path, "a.txt", b"hello")

    assert looks_like_ooxml(docx) is True
    assert looks_like_ole2(docx) is False
    assert looks_like_ole2(ole) is True
    assert looks_like_ooxml(ole) is False
    assert looks_like_ooxml(text) is False
    assert looks_like_ole2(text) is False


def test_magic_helpers_survive_missing_file(tmp_path: Path) -> None:
    """读不到文件时返回 False，不要把 OSError 漏给上层。"""
    missing = tmp_path / "nope.docx"

    assert looks_like_ooxml(missing) is False
    assert looks_like_ole2(missing) is False


def test_empty_zip_magic_is_recognised(tmp_path: Path) -> None:
    """空 zip 的头是 PK\\x05\\x06，PyMuPDF 之外的工具也会产出这种文件。"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w"):
        pass
    empty = write(tmp_path, "empty.xlsx", buffer.getvalue())

    assert looks_like_ooxml(empty) is True


# ----------------------------------------------------------------------
# 数工作表
# ----------------------------------------------------------------------

def test_counts_visible_sheets(tmp_path: Path) -> None:
    book = write(tmp_path, "book.xlsx", build_xlsx_bytes())

    assert count_excel_sheets(book) == (2, 0)


def test_counts_hidden_sheets_separately(tmp_path: Path) -> None:
    """隐藏表要单独报 —— 它们不会被导出，混进「可见」里数字就是错的。"""
    book = write(tmp_path, "book.xlsx", build_xlsx_bytes(hidden=2))

    assert count_excel_sheets(book) == (2, 2)


def test_sheet_count_returns_none_for_legacy_xls(tmp_path: Path) -> None:
    """旧版 .xls 是 OLE2 二进制，数不出来要返回 None 而不是 0。

    返回 0 会让上层说出「表格里有 0 个工作表」这种明显错误的话；
    None 让上层改用不报数字的说法。
    """
    legacy = write(tmp_path, "old.xls", build_ole2_bytes("Workbook"))

    assert count_excel_sheets(legacy) is None


def test_sheet_count_returns_none_for_a_broken_zip(tmp_path: Path) -> None:
    broken = write(tmp_path, "broken.xlsx", b"PK\x03\x04" + b"\x00" * 128)

    assert count_excel_sheets(broken) is None


def test_sheet_count_returns_none_when_workbook_part_is_missing(tmp_path: Path) -> None:
    """有 [Content_Types].xml 却没有工作簿部件：数不出来，但也不能抛异常。"""
    parts = {
        "[Content_Types].xml": (
            '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org'
            '/package/2006/content-types"/>'
        )
    }
    book = write(tmp_path, "book.xlsx", zip_bytes(parts))

    assert count_excel_sheets(book) is None


# ----------------------------------------------------------------------
# 数幻灯片
# ----------------------------------------------------------------------

def test_counts_slides(tmp_path: Path) -> None:
    deck = write(tmp_path, "deck.pptx", build_pptx_bytes(slides=3))

    assert count_powerpoint_slides(deck) == 3


def test_counts_hidden_slides_in_the_total(tmp_path: Path) -> None:
    """这里数的是**总页数**，隐藏与否不在这层区分。

    差额由「总数 − 导出页数」得到，所以总数必须把隐藏页也算进去，
    否则算出来的差额就少了一页。
    """
    deck = write(tmp_path, "deck.pptx", build_pptx_bytes(slides=2, hidden=1))

    assert count_powerpoint_slides(deck) == 3


def test_slide_count_returns_none_for_legacy_ppt(tmp_path: Path) -> None:
    legacy = write(tmp_path, "old.ppt", build_ole2_bytes("PowerPoint Document"))

    assert count_powerpoint_slides(legacy) is None


def test_slide_count_returns_none_for_a_broken_zip(tmp_path: Path) -> None:
    broken = write(tmp_path, "broken.pptx", b"PK\x03\x04" + b"\x00" * 128)

    assert count_powerpoint_slides(broken) is None


# ----------------------------------------------------------------------
# 校验结果的登记信息
# ----------------------------------------------------------------------

def test_filename_is_stripped_of_directories(tmp_path: Path) -> None:
    """上传文件名里带路径时要只留文件名（防路径穿越，§十八）。"""
    result = validate_office_upload(
        write(tmp_path, "a.docx", build_docx_bytes()), "../../etc/passwd.docx"
    )

    assert result.filename == "passwd.docx"
    assert "/" not in result.filename
    assert "\\" not in result.filename
