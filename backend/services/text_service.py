"""TXT 家族的叶子实现（第九阶段第 10 步）。

这个模块是 ``CONVERTERS`` 表里 ``text.to_docx`` 与 ``text.convert``
两格的落点。它自己不含解析或排版逻辑：纯文本 → IR 在
:func:`office.document_ir.blocks_from_text`，IR → Word 在
:mod:`office.docx_writer`，IR → HTML/Markdown 在 :mod:`office.markup_render`。
这里只做四件事：解码、拆段、丢进线程池、落盘登记。

**``text.to_pdf`` 不在这里。** TXT→PDF 从第七阶段起就走
``services.doc_service``（PyMuPDF 排版、逐行分页、限页数），那条路的行为
已经被既有测试与专用页面钉住。这里再写一份「文本转 PDF」只会得到两个
不一样的 PDF，所以 ``CONVERTERS[TEXT_TO_PDF]`` 仍然指着老地方。

两个目标共用一次解码与一次拆段：同一份 .txt 转成 DOCX 与转成 HTML，
段落划分必须一模一样，各拆各的迟早会不一样。
"""

from __future__ import annotations

import logging

from config import settings
from conversion import registry
from office.document_ir import PAGE_KIND_TEXT, PageContent, blocks_from_text
from office.loader import decode_text
from office.txt_to_pdf import TOO_LONG_MESSAGE, TxtOptions, find_font
from services.conversion_types import (
    ConversionOptions,
    ConversionOutput,
    ConversionRequest,
)
from services.intake import run_in_pool
from services.markup_service import Rendered, register_output, render_blocks
from utils.errors import ValidationError

logger = logging.getLogger(__name__)

__all__ = [
    "text_convert",
    "text_to_docx",
]

#: 「纯文本没有标记」这句说明，DOCX 与 HTML/Markdown 都要带。
#:
#: 必须说：用户拿到的结果里一个标题、一个列表都没有，得知道原因是
#: 「源文件里本来就没有这些东西」，而不是我们漏转了。
#: **诚实的方向只有一个** —— 不能为了结果好看就凭空造结构。
NO_MARKUP_NOTE = "纯文本里没有标题、列表等标记，因此结果只有正文段落。"


def _docx_builder():
    """懒加载 DOCX 写入层。

    **不能放在模块顶层 import。** 少了 python-docx 的话，
    ``import office.docx_writer`` 会直接抛 ImportError，而
    ``services.conversion_service`` 是路由层模块级 import 的 ——
    整个应用都起不来，「缺组件时其它功能照常可用」就成了空话。
    ``services/pdf_to_docx.py`` 的 ``_writer()`` 出于同一个理由这么做，
    这里照它的样子来。
    """
    from office.docx_writer import build_docx

    return build_docx


def _font_label(options: ConversionOptions) -> str:
    """这次转换该用哪个字体名写进 Word。

    ``TxtOptions.font`` 是**字体键**（``song``），而 DOCX 里要写的是
    字体名（``宋体``）—— Word 拿这个名字去系统里找字体，给它键是找不到的。
    ``options.txt_options`` 为 None 时用 :class:`TxtOptions` 的默认值，
    与路由层 `parse_txt_options` 缺省时选的是同一个字体。

    这也是 ``TEXT_TO_DOCX`` 唯一的一项选项：``office.docx_writer``
    目前只设置 Normal 样式的东亚字体，**字号没有实现路径**，
    发一个点了没反应的控件比不发更糟。
    """
    txt = options.txt_options or TxtOptions()
    return find_font(txt.font).label


# ----------------------------------------------------------------------
# 纯计算（跑在线程池里）
# ----------------------------------------------------------------------

def _blocks(text: str):
    """解码后的文本 → 段落列表，顺便卡长度上限。

    上限与文案都取自 ``office.txt_to_pdf``：同一个源文件不该在这里放行、
    在 TXT→PDF 那里被拒。
    """
    if len(text) > settings.MAX_TXT_CHARS:
        raise ValidationError(TOO_LONG_MESSAGE)
    return blocks_from_text(text)


def _to_docx(text: str, label: str) -> Rendered:
    """文本 → DOCX。

    ``page_count`` 留空：``.txt`` 里没有分页，DOCX 的页是 Word 打开时
    按纸张与字号算出来的，我们**不知道**会是几页 —— 与其填一个估的
    数字，不如不填（§十二：不制造虚假进度）。
    """
    blocks = _blocks(text)
    if not blocks:
        raise ValidationError("这个文本文件里没有可转换的内容。")
    build = _docx_builder()(
        [PageContent(blocks=blocks, kind=PAGE_KIND_TEXT)],
        east_asian_font=label,
    )
    return Rendered(
        data=build.data,
        notes=[f"已用「{label}」写入 Word 文档，共 {len(blocks)} 段。", NO_MARKUP_NOTE],
    )


def _to_text_target(text: str, target_type: str) -> Rendered:
    """文本 → HTML / Markdown。

    目标不认识时由 :func:`services.markup_service.render_blocks` 抛错 ——
    这里不再查一次表，否则「哪些目标是合法的」就有了第二份答案。
    """
    blocks = _blocks(text)
    if not blocks:
        raise ValidationError("这个文本文件里没有可转换的内容。")
    rendered = render_blocks(blocks, target_type)
    rendered.notes = [*rendered.notes, NO_MARKUP_NOTE]
    return rendered


# ----------------------------------------------------------------------
# 两个叶子
# ----------------------------------------------------------------------

async def text_to_docx(request: ConversionRequest, stem: str) -> ConversionOutput:
    """TXT → DOCX（``office.docx_writer``，**不进 LibreOffice**）。"""
    text, _encoding = decode_text(request.source)
    label = _font_label(request.options)
    rendered = await run_in_pool(_to_docx, text, label)
    return register_output(request, stem, registry.TARGET_DOCX, rendered)


async def text_convert(request: ConversionRequest, stem: str) -> ConversionOutput:
    """TXT → HTML / Markdown（``office.markup_render``）。"""
    text, _encoding = decode_text(request.source)
    rendered = await run_in_pool(_to_text_target, text, request.target_type)
    return register_output(request, stem, request.target_type, rendered)
