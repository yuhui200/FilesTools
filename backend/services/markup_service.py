"""HTML / Markdown 家族的叶子实现（第九阶段第 9 步）。

这个模块是 ``CONVERTERS`` 表里 ``markup.to_pdf`` 与 ``markup.convert``
两格的落点。它自己**不含任何解析或排版逻辑** —— 那是
:mod:`office.markup_parse` / :mod:`office.markup_render` /
:mod:`office.html_to_pdf` 三个模块的事，这里只做四件事：

1. 按 ``source_type`` 决定源格式、按 ``target_type`` 决定目标格式；
2. 把纯计算的部分丢进线程池（与图片、TXT 走同一条路，见
   :func:`services.intake.run_in_pool`）；
3. 落盘、登记下载令牌（复用 :mod:`services.batch_service`，**不新建下载系统**）；
4. 把「解析时丢了什么」与「渲染时丢了什么」两句说明合起来交给用户。

**产出 HTML 是安全的**：``routers/download.py`` 给下载响应带了
``Content-Disposition: attachment``，浏览器只会存盘不会渲染，所以用户自己
写的脚本没有机会在我们的域上跑起来。这一点在
``registry.MEDIA_TYPE_BY_TARGET`` 那里也记了一笔。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from conversion import registry
from office import markup_parse, markup_render
from office.document_ir import Block
from office.html_to_pdf import build_pdf_from_blocks
from office.loader import decode_text
from office.txt_to_pdf import TxtBuildResult, TxtOptions
from services.batch_service import ResultEntry, register_batch
from services.conversion_types import ConversionOutput, ConversionRequest
from services.intake import run_in_pool
from utils.errors import UnsupportedConversionError
from utils.files import new_token

logger = logging.getLogger(__name__)

__all__ = [
    "MARKUP_RENDER_TARGETS",
    "SOURCE_FORMAT_BY_TYPE",
    "Rendered",
    "markup_convert",
    "markup_to_pdf",
    "register_output",
    "render_blocks",
]

#: 注册表的源词汇 -> 解析器的源格式。两边用的是各自的词汇
#: （``"md"`` / ``"markdown"``），这里显式写出这层对应关系，
#: 由 ``test_every_markup_source_has_a_parse_format`` 保证没有遗漏 ——
#: 隐式地按字符串相等去猜，只会在 ``md`` 这一格上悄悄错掉。
SOURCE_FORMAT_BY_TYPE: dict[str, str] = {
    registry.SOURCE_HTML: markup_parse.SOURCE_HTML,
    registry.SOURCE_MD: markup_parse.SOURCE_MARKDOWN,
}

#: 注册表的目标词汇 -> 渲染器的目标格式。
#:
#: 名字里的 ``MARKUP`` 指的是**引擎**（``office.markup_render``），
#: 不是源格式：TXT 家族产出 HTML / Markdown 时也查这张表
#: （见 :func:`services.text_service.text_convert`）。两个家族共用一份，
#: 是因为它们共用同一个渲染器 —— 各留一份「``md`` 该渲染成什么」的答案，
#: 迟早会有一边改另一边没改。
MARKUP_RENDER_TARGETS: dict[str, str] = {
    registry.TARGET_HTML: markup_render.OUTPUT_HTML,
    registry.TARGET_TXT: markup_render.OUTPUT_TEXT,
    registry.TARGET_MD: markup_render.OUTPUT_MARKDOWN,
}

#: HTML / Markdown 结果的落盘编码。三种目标都是文本，一律 UTF-8 ——
#: 产出的 HTML 头部也声明了 utf-8，两者必须一致。
_OUTPUT_ENCODING = "utf-8"


@dataclass(slots=True)
class Rendered:
    """一次渲染的产物。"""

    data: bytes
    notes: list[str]
    page_count: int | None = None


def _parse_format(request: ConversionRequest) -> str:
    source_format = SOURCE_FORMAT_BY_TYPE.get(request.source_type)
    if source_format is None:  # pragma: no cover - 注册表只登记了这两种源
        raise UnsupportedConversionError("暂不支持这种文档格式。")
    return source_format


def render_blocks(blocks: list[Block], target_type: str) -> Rendered:
    """IR → HTML / Markdown / 纯文本。

    公开出来是因为 TXT 家族也要用（``text.convert``）：它先把纯文本拆成
    IR，再走这里。**「怎么渲染」只该有一份实现** —— 否则 TXT→HTML 与
    MD→HTML 会在两次改动之后长得不一样。

    ``target_type`` 是注册表的词汇；查不到就抛 :class:`UnsupportedConversionError`，
    而不是回退到某个默认格式 —— 一个不认识的目标格式静默变成纯文本，
    是用户最难发现的一类错。
    """
    render_target = MARKUP_RENDER_TARGETS.get(target_type)
    if render_target is None:  # pragma: no cover - 注册表只登记了这三种目标
        raise UnsupportedConversionError("暂不支持这个目标格式。")

    body = markup_render.render_markup(blocks, render_target)
    # 「丢了什么」在前、「怎么排的」在后：丢图片是用户最需要立刻知道的事。
    notes = list(markup_render.notes_for_target(blocks, render_target))
    return Rendered(data=body.encode(_OUTPUT_ENCODING), notes=notes)


# ----------------------------------------------------------------------
# 纯计算（跑在线程池里）
# ----------------------------------------------------------------------

def _to_text_target(
    text: str, source_format: str, target_type: str
) -> Rendered:
    """解析 → 渲染成 HTML / Markdown / 纯文本。

    解析带来的说明（图片被丢掉、标签不认识）排在渲染的说明前面：
    它们是「你的源文件里有东西没跟过来」，比「结果是怎么排的」更要紧。
    """
    parsed = markup_parse.parse_markup(text, source_format)
    rendered = render_blocks(parsed.blocks, target_type)
    rendered.notes = [*parsed.notes, *rendered.notes]
    return rendered


def _to_pdf(text: str, source_format: str, options: TxtOptions | None) -> Rendered:
    """解析 → 排成 PDF。"""
    parsed = markup_parse.parse_markup(text, source_format)
    built: TxtBuildResult = build_pdf_from_blocks(parsed.blocks, options)
    return Rendered(
        data=built.data,
        # 「有 3 张外链图片没转」这类话必须排在「共 2 页」前面：
        # 它是用户拿到结果第一眼要确认的东西。
        notes=[*parsed.notes, *built.notes],
        page_count=built.page_count,
    )


# ----------------------------------------------------------------------
# 两个叶子
# ----------------------------------------------------------------------

async def markup_to_pdf(
    request: ConversionRequest, stem: str
) -> ConversionOutput:
    """HTML / Markdown → PDF（``office.html_to_pdf``）。"""
    text, _encoding = decode_text(request.source)
    rendered = await run_in_pool(
        _to_pdf,
        text,
        _parse_format(request),
        # 字体 / 字号 / 纸张 / 方向四项与 TXT→PDF 是同一套选项，
        # 走同一个 ``TxtOptions``，理由见 ``html_to_pdf`` 的说明。
        request.options.txt_options,
    )
    return register_output(request, stem, registry.TARGET_PDF, rendered)


async def markup_convert(
    request: ConversionRequest, stem: str
) -> ConversionOutput:
    """HTML / Markdown → HTML / Markdown / 纯文本（``office.markup_render``）。"""
    text, _encoding = decode_text(request.source)
    rendered = await run_in_pool(
        _to_text_target,
        text,
        _parse_format(request),
        request.target_type,
    )
    return register_output(request, stem, request.target_type, rendered)


def register_output(
    request: ConversionRequest, stem: str, target_type: str, rendered: Rendered
) -> ConversionOutput:
    """落盘 + 登记下载令牌。所有「产出文本文件」的收尾都走这里。

    TXT 家族也用（``text.convert`` / ``text.to_docx``）—— 它们的落盘方式、
    扩展名来源、下载令牌一模一样，没有第二份可写。
    """
    extension = registry.EXTENSION_BY_TARGET[target_type]
    media_type = registry.media_type_for_target(target_type)

    path = request.out_dir / f"{new_token()}{extension}"
    path.write_bytes(rendered.data)

    filename = f"{stem}{extension}"
    # 复用图片那套登记：结果只有 1 个时它直接给出文件而**不打包**，
    # 与专用页面的行为一致（**不新建下载系统**）。
    job_id, url, _archive = register_batch(
        request.out_dir,
        [
            ResultEntry(
                index=0, filename=filename, path=path, media_type=media_type
            )
        ],
        archive_stem=stem,
    )

    return ConversionOutput(
        filename=filename,
        path=path,
        media_type=media_type,
        size=len(rendered.data),
        job_id=job_id,
        download_url=url,
        page_count=rendered.page_count,
        notes=list(rendered.notes),
    )
