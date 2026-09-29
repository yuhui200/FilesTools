"""Office / 文本文件的内容校验。

和图片、PDF 一样是三层，只是「第三层」换成了各自容器的真实结构：

1. 扩展名是否在白名单内；
2. 容器魔数是否对得上（OOXML 是 zip，旧版 Office 是 OLE2 复合文档）；
3. **容器内部结构是否真的属于所声称的类型** —— 光看 `PK` 挡不住
   一个改名成 .docx 的普通 zip，光看 OLE2 头也挡不住改名成 .doc 的 .msi。

所有判断都只读文件头部和有界的片段：`namelist()` 不会解压任何东西，
读 `[Content_Types].xml` 也限了长度，所以压缩炸弹伤不到这里。

调用方必须通过 :func:`validate_office_upload` 一个入口进来，
不要在别处另写一套 —— 页数、大小之类的限制都在这里卡住，
新加功能时不会漏掉某一条安全约束。
"""

from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass
from pathlib import Path

from config import settings
from office.markup_parse import (
    check_length as check_markup_length,
)
from office.markup_parse import (
    markup_source_for_extension,
)
from utils.errors import (
    CorruptedFileError,
    UnsupportedTypeError,
    ValidationError,
)

__all__ = [
    "BROKEN_OFFICE_MESSAGE",
    "BROKEN_TEXT_MESSAGE",
    "MISMATCH_MESSAGE",
    "UNSUPPORTED_MARKUP_MESSAGE",
    "UNSUPPORTED_OFFICE_MESSAGE",
    "KIND_EXCEL",
    "KIND_POWERPOINT",
    "KIND_TEXT",
    "KIND_WORD",
    "OfficeInput",
    "count_excel_sheets",
    "count_powerpoint_slides",
    "decode_text",
    "detect_kind",
    "looks_like_ole2",
    "looks_like_ooxml",
    "validate_office_upload",
]

# 面向用户的统一文案。转换组件缺失那一句在 office/libreoffice.py 里。
BROKEN_OFFICE_MESSAGE = "无法读取该 Office 文件，请检查文件是否损坏。"
BROKEN_TEXT_MESSAGE = "无法读取该文本文件，请检查文件编码后重试。"
UNSUPPORTED_OFFICE_MESSAGE = "暂不支持该文件格式，请上传 Word、Excel、PowerPoint 文档或 TXT 文本文件。"
#: 专用页面上传标记文档时的提示。与上面那句分开写：两个入口接受的格式
#: 本来就不一样，共用一句只会让其中一边说假话。
UNSUPPORTED_MARKUP_MESSAGE = "暂不支持该文件格式，请上传 HTML 或 Markdown 文件。"
MISMATCH_MESSAGE = "文件内容与扩展名不一致，已拒绝处理。"

KIND_WORD = "word"
KIND_EXCEL = "excel"
KIND_POWERPOINT = "powerpoint"
KIND_TEXT = "text"

#: 文档类别 -> 中文名（提示文案与界面标题共用一份）
KIND_LABELS: dict[str, str] = {
    KIND_WORD: "Word 文档",
    KIND_EXCEL: "Excel 表格",
    KIND_POWERPOINT: "PowerPoint 演示文稿",
    KIND_TEXT: "文本文件",
}

# 扩展名 -> 类别。这里是「声明」，真实类型还要靠下面的结构校验确认。
_EXT_KINDS: dict[str, str] = {
    ".docx": KIND_WORD,
    ".doc": KIND_WORD,
    ".xlsx": KIND_EXCEL,
    ".xls": KIND_EXCEL,
    ".pptx": KIND_POWERPOINT,
    ".ppt": KIND_POWERPOINT,
    ".txt": KIND_TEXT,
}

_ZIP_MAGICS = (b"PK\x03\x04", b"PK\x05\x06")  # 普通 zip / 空 zip
_OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_CONTENT_TYPES = "[Content_Types].xml"
# 只读这么长的 [Content_Types].xml：真实文件通常 1~2 KB，
# 限长是为了让「zip 里塞一个 1 GB 的 XML」这种构造伤不到我们。
_CONTENT_TYPES_LIMIT = 64 * 1024

#: OOXML 类别 -> (入口部件, Content_Types 里必须出现的内容类型关键字)
_OOXML_PARTS: dict[str, tuple[str, str]] = {
    KIND_WORD: ("word/document.xml", "wordprocessingml"),
    KIND_EXCEL: ("xl/workbook.xml", "spreadsheetml"),
    KIND_POWERPOINT: ("ppt/presentation.xml", "presentationml"),
}

#: 旧版 Office 类别 -> 复合文档里必须存在的流名（OLE2 目录项以 UTF-16LE 存储）
_OLE2_STREAMS: dict[str, tuple[str, ...]] = {
    # Word 的正文流
    KIND_WORD: ("WordDocument",),
    # Excel 8.0 起是 Workbook，Excel 5.0/95 用的是 Book —— 只认前者会误杀老文件
    KIND_EXCEL: ("Workbook", "Book"),
    KIND_POWERPOINT: ("PowerPoint Document",),
}

# 解码尝试顺序。utf-16 放最后：没有 BOM 时它能把任意字节对「成功」解成
# 一片汉字，排前面会把 gbk 的文件解码成乱码。
_ENCODINGS = ("utf-8-sig", "utf-8", "gbk", "utf-16")


@dataclass(slots=True)
class OfficeInput:
    """校验通过的文档。"""

    kind: str
    filename: str
    size: int
    extension: str

    @property
    def kind_label(self) -> str:
        return KIND_LABELS.get(self.kind, "文档")


def detect_kind(original_filename: str) -> str | None:
    """按扩展名判断文档类别；不在白名单内返回 None。"""
    return _EXT_KINDS.get(Path(original_filename).suffix.lower())


def looks_like_ooxml(path: Path) -> bool:
    """按文件头粗判是不是 OOXML（zip 容器）。"""
    try:
        with path.open("rb") as handle:
            head = handle.read(4)
    except OSError:
        return False
    return any(head.startswith(magic) for magic in _ZIP_MAGICS)


def looks_like_ole2(path: Path) -> bool:
    """按文件头粗判是不是旧版 Office（OLE2 复合文档）。"""
    try:
        with path.open("rb") as handle:
            head = handle.read(8)
    except OSError:
        return False
    return head == _OLE2_MAGIC


def _check_ooxml(path: Path, kind: str) -> None:
    """确认这个 zip 的内部结构确实属于所声称的 Office 类型。

    三种失败要分清楚，因为用户该做的事完全不同：

    * 包里没有 ``[Content_Types].xml`` —— OOXML 规定它是必需的，
      没有就说明这不是 Office 文档（普通压缩包改了扩展名）→ 格式不支持；
    * 有 ``[Content_Types].xml`` 但里面没有声明这种类型 ——
      典型的 ``.xlsx`` 改名成 ``.docx`` → 格式不一致；
    * 声明了这种类型、主部件却不在包里 —— 文件被改坏了 → 内容损坏。
    """
    entry, marker = _OOXML_PARTS[kind]

    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            if _CONTENT_TYPES not in names:
                raise UnsupportedTypeError(MISMATCH_MESSAGE)
            try:
                with archive.open(_CONTENT_TYPES) as handle:
                    declared = handle.read(_CONTENT_TYPES_LIMIT).decode(
                        "utf-8", errors="replace"
                    )
            except (KeyError, OSError, zipfile.BadZipFile) as exc:
                raise CorruptedFileError(BROKEN_OFFICE_MESSAGE) from exc
    except zipfile.BadZipFile as exc:
        # 头四字节像 zip，实际结构坏了（下载中断、被截断）
        raise CorruptedFileError(BROKEN_OFFICE_MESSAGE) from exc

    if marker not in declared:
        raise UnsupportedTypeError(MISMATCH_MESSAGE)

    if entry not in names:
        # 声明自己是 Word 文档，正文部件却丢了 —— 这是坏文件，不是别的格式
        raise CorruptedFileError(BROKEN_OFFICE_MESSAGE)


def _check_ole2(path: Path, kind: str) -> None:
    """确认这个 OLE2 容器里有该 Office 类型必有的流。

    OLE2 的头是通用的，.doc / .xls / .ppt / .msi 长得一模一样，
    所以必须往下看目录项：每种类型都有自己必有的流名。
    不做这一步的话，一个改名成 .doc 的安装包会一路走到转换器，
    用户看到的是「文件转换失败」，而不是「格式不支持」。
    """
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise CorruptedFileError(BROKEN_OFFICE_MESSAGE) from exc

    for stream in _OLE2_STREAMS[kind]:
        if stream.encode("utf-16-le") in data:
            return
    raise UnsupportedTypeError(MISMATCH_MESSAGE)


def decode_text(path: Path) -> tuple[str, str]:
    """按常见编码依次尝试解码，返回 (文本, 实际用的编码)。

    校验和排版共用这一个函数，避免出现「校验说能解、排版却解不开」的裂缝。
    """
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise CorruptedFileError(BROKEN_TEXT_MESSAGE) from exc

    if b"\x00" in raw[:4096] and not raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        # 有 NUL 又没有 UTF-16 的 BOM：这是二进制文件，不是文本
        raise CorruptedFileError(BROKEN_TEXT_MESSAGE)

    for encoding in _ENCODINGS:
        try:
            text = raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
        return text, encoding

    raise CorruptedFileError(BROKEN_TEXT_MESSAGE)


def _assert_plain_text(text: str) -> None:
    """确认解出来的东西真的是一份文本文档。

    文本文件没有魔数，只能靠「能不能当文本读出来」加上这两条判断。
    控制字符（换行、制表符除外）占比过高说明是二进制被改了扩展名 ——
    这一步是给 .exe 改名成 .txt 这类文件兜底的：它们常常能碰巧解码成功。
    """
    if not text.strip():
        raise ValidationError("这个文件没有任何可转换的内容")

    sample = text[:4096]
    if sample:
        control = sum(
            1 for char in sample if ord(char) < 32 and char not in "\r\n\t\f\v"
        )
        if control / len(sample) > 0.05:
            raise CorruptedFileError(BROKEN_TEXT_MESSAGE)


def _check_text(path: Path) -> None:
    text, _ = decode_text(path)
    _assert_plain_text(text)


def validate_markup_upload(path: Path, original_filename: str) -> tuple[str, str]:
    """校验一份上传的 HTML / Markdown，返回 ``(源格式, 文本)``。

    与 Office 那条路是同一套三层思路，只是第三层换成了「能不能当文本读」：

    1. 扩展名在 ``settings.ALLOWED_MARKUP_EXTENSIONS`` 里；
    2. 文件非空、能按常见编码解出文字（``decode_text`` 会挡下含 NUL 的二进制）；
    3. 解出来的内容确实像文本（控制字符占比），且没有超长。

    长度在这里就卡住，用户提交的那一刻就能得到反馈，不必等排到队里
    才失败。解析层还会再查一次同样的界限 —— 那是为了堵住「绕过这个
    入口直接调解析器」的可能，两处共用 ``markup_parse`` 里那一份文案。
    """
    extension = Path(original_filename).suffix.lower()
    source_format = markup_source_for_extension(extension)
    if source_format is None:
        raise UnsupportedTypeError(UNSUPPORTED_MARKUP_MESSAGE)

    size = path.stat().st_size if path.exists() else 0
    if size <= 0:
        raise ValidationError("上传的文件为空")

    text, _encoding = decode_text(path)
    _assert_plain_text(text)
    check_markup_length(text)
    return source_format, text


def validate_office_upload(path: Path, original_filename: str) -> OfficeInput:
    """完整校验一个上传的 Word / Excel / PowerPoint / TXT，返回它的信息。"""
    extension = Path(original_filename).suffix.lower()
    kind = _EXT_KINDS.get(extension)

    if kind is None:
        raise UnsupportedTypeError(UNSUPPORTED_OFFICE_MESSAGE)

    size = path.stat().st_size if path.exists() else 0
    if size <= 0:
        raise ValidationError("上传的文件为空")

    if kind == KIND_TEXT:
        _check_text(path)
    elif extension in {".docx", ".xlsx", ".pptx"}:
        # 新版 OOXML：必须是 zip
        if not looks_like_ooxml(path):
            raise UnsupportedTypeError(MISMATCH_MESSAGE)
        _check_ooxml(path, kind)
    else:
        # 旧版 .doc / .xls / .ppt：必须是 OLE2 复合文档
        if not looks_like_ole2(path):
            raise UnsupportedTypeError(MISMATCH_MESSAGE)
        _check_ole2(path, kind)

    return OfficeInput(
        kind=kind,
        filename=Path(original_filename).name,
        size=size,
        extension=extension,
    )


#: 数部件内容时最多读这么长。真实文件只有几 KB，
#: 限长是为了让「zip 里塞一个 1 GB 的 XML」这种构造伤不到我们。
_OOXML_PART_LIMIT = 8 * 1024 * 1024

#: Excel 的工作簿部件，列出全部工作表。取自 ``_OOXML_PARTS``，
#: 免得同一份部件名在两处各写一遍、改一处漏一处。
_WORKBOOK_PART = _OOXML_PARTS[KIND_EXCEL][0]

#: ``<sheet ...>`` 起始标签。XML 里属性值中的 ``<`` 必须写成 ``&lt;``，
#: 所以良构文档里不会出现「属性值里混进一个 <sheet」的误判。
_SHEET_TAG = re.compile(rb"<sheet\b[^>]*>")

#: PowerPoint 的演示文稿部件，列出全部幻灯片（同上）
_PRESENTATION_PART = _OOXML_PARTS[KIND_POWERPOINT][0]

#: ``<p:sldId ...>`` 起始标签，一个就是一页幻灯片
_SLIDE_TAG = re.compile(rb"<p:sldId\b[^>]*>")


def _count_tags(path: Path, part: str, tag: re.Pattern[bytes]) -> int | None:
    """打开一个 OOXML 部件，数其中某种起始标签有几个；读不出来返回 None。

    用「限长读字节 + 正则」而不是 XML 解析器：这里只要一个数字，
    引入解析器等于白白多一个 XML 攻击面（实体展开、深度嵌套），
    而数错了顶多让说明里的数字不准，不会造成危害。
    """
    try:
        with zipfile.ZipFile(path) as archive:
            with archive.open(part) as handle:
                raw = handle.read(_OOXML_PART_LIMIT)
    except (KeyError, OSError, zipfile.BadZipFile):
        return None
    return len(tag.findall(raw))


def count_excel_sheets(path: Path) -> tuple[int, int] | None:
    """数一数工作簿里有几个工作表，返回 ``(可见, 隐藏)``；数不出来返回 None。

    只有 OOXML（.xlsx）数得出来。旧版 .xls 是 OLE2 复合文档，要数就得自己
    解析二进制格式 —— 代价远大于这一句说明的价值，所以返回 None，调用方
    改用不报数字的说法。

    **隐藏的工作表不计入「可见」**：实测 LibreOffice 转换时会跳过隐藏工作表
    （三张都可见 → 3 页；两张可见 + 一张隐藏 → 2 页，隐藏表的内容不在 PDF 里）。
    把隐藏表算进去，「有 N 个工作表」就和实际导出的对不上了。
    """
    try:
        with zipfile.ZipFile(path) as archive:
            with archive.open(_WORKBOOK_PART) as handle:
                raw = handle.read(_OOXML_PART_LIMIT)
    except (KeyError, OSError, zipfile.BadZipFile):
        return None

    visible = hidden = 0
    for tag in _SHEET_TAG.findall(raw):
        # state 缺省即可见；hidden 与 veryHidden 都算隐藏
        if b"state=" in tag:
            hidden += 1
        else:
            visible += 1

    if visible + hidden == 0:
        return None
    return visible, hidden


def count_powerpoint_slides(path: Path) -> int | None:
    """数一数演示文稿里有几页幻灯片；数不出来返回 None。

    只数 ``ppt/presentation.xml`` 的 ``<p:sldIdLst>``，**不分隐藏与否** ——
    隐藏标记写在各自的幻灯片部件上，要不要为它多开 N 次 zip 见下面的说明。
    旧版 .ppt 数不出来，返回 None。

    「哪几页是隐藏的」这里不查，因为页数本身已经回答了：实测隐藏幻灯片
    不会被导出（两张可见 + 一张隐藏 → 2 页），所以
    「总数 − 导出页数」就是没导出的页数，不必再去翻每个部件。
    """
    return _count_tags(path, _PRESENTATION_PART, _SLIDE_TAG)


def allowed_extensions() -> set[str]:
    """全部允许的文档扩展名（配置项分散在几个 set 里，这里合起来给校验和前端用）。"""
    return (
        set(settings.ALLOWED_WORD_EXTENSIONS)
        | set(settings.ALLOWED_EXCEL_EXTENSIONS)
        | set(settings.ALLOWED_POWERPOINT_EXTENSIONS)
        | set(settings.ALLOWED_TEXT_EXTENSIONS)
    )
