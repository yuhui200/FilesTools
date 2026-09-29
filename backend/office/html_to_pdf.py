"""HTML / Markdown → PDF（PyMuPDF ``Story`` + ``DocumentWriter``）。

**这一路为什么不直接吃用户上传的 HTML**：``Story`` 是个真的 HTML/CSS
排版器，它会去解析 ``<img src>``、``@font-face``、``@import``、CSS 的
``url()``。用户写的 ``file:///etc/passwd`` 就是一次本地文件读取，
``http://内网地址`` 就是一次服务端发起的请求。所以进到这个模块的内容
**一定是 :mod:`office.markup_parse` 解析出来、:mod:`office.markup_render`
重新拼出来的 HTML** —— 里面有我们自己写的标签、常量 CSS、以及 ``data:``
图片，没有一个字符来自用户的属性。这个模块不提供「直接排版一段 HTML」
的入口，就是为了让上面那句话在类型层面也成立。

字体和 TXT→PDF 共用一套探测（``available_fonts`` / ``resolve_font``）：
PyMuPDF 内置的 china-s 等中文码实测都指向同一个 Droid Sans Fallback，
拿它们冒充宋体、黑体是个不生效的假控件。

``subset_fonts()`` **必须调用，不是优化**：实测一页中文不子集化
18.3 MB，子集化后 13 KB（0.07%）。``DocumentWriter`` 写出来的是成品字节流，
所以这里要重新 ``open`` 一次再子集化 —— 直接返回 writer 的产物会大到荒唐。
"""

from __future__ import annotations

import io

import pymupdf

from config import settings
from office import markup_render
from office.document_ir import Block
from office.txt_to_pdf import (
    ORIENTATION_LABELS,
    PAGE_SIZE_LABELS,
    TxtBuildResult,
    TxtOptions,
    resolve_font,
)
from utils.errors import ValidationError

__all__ = [
    "STORY_FONT_FAMILY",
    "STYLE_SUBSET_NOTE",
    "build_pdf_from_blocks",
]

#: 注入 ``@font-face`` 时用的字体族名。用固定的 ASCII 名而不是字体自己的
#: 名字：CSS 里的族名要拼进样式表，用它自己的名字（可能是中文、可能带空格）
#: 只会平白多一层转义。
STORY_FONT_FAMILY = "filetools-body"

#: 如实说明保留了什么。用户拿到的 PDF 与浏览器里看到的不会一模一样，
#: 与其让他自己发现，不如写在结果说明里。
STYLE_SUBSET_NOTE = (
    "已按标题、段落、列表、引用、代码块、表格与分隔线重新排版；"
    "网页里的字体、颜色、分栏等版式没有保留。"
)

TOO_MANY_PAGES_MESSAGE = (
    f"这份文档排出来超过 {settings.MAX_TXT_PAGES} 页，请先拆分后再上传。"
)


def build_pdf_from_blocks(
    blocks: list[Block], options: TxtOptions | None = None
) -> TxtBuildResult:
    """把 IR 排成一份 PDF。

    选项复用 :class:`office.txt_to_pdf.TxtOptions`：字体、字号、纸张、方向
    这四项在两处是同一件事，各写一份迟早会有一边漏改。
    """
    options = options or TxtOptions()
    _font, font_info = resolve_font(options.font)
    font_size = max(
        settings.TXT_MIN_FONT_SIZE,
        min(settings.TXT_MAX_FONT_SIZE, int(options.font_size)),
    )

    width, height = settings.PDF_PAGE_SIZES[options.page_size]
    if options.orientation == "landscape":
        width, height = height, width

    margin = settings.TXT_MARGIN_PT
    if height - margin * 2 <= font_size:
        raise ValidationError("当前页面大小配这个字号放不下一行，请调小字号或换更大的页面。")

    archive = None
    extra_css = ""
    if font_info.path is None:
        # 一个系统字体都没探测到：让 Story 用它自带的回退（内置中文码）。
        # 不写 @font-face 是诚实的做法 —— 写了也没对应的字体文件可用。
        font_stack = markup_render.DEFAULT_FONT_STACK
    else:
        # **只把这一个字体文件放进 archive**，不放目录：archive 能取到什么
        # 完全由这里决定，加一个目录进去就等于把那个目录开放给了排版引擎。
        archive = pymupdf.Archive()
        archive.add(str(font_info.path), font_info.path.name)
        extra_css = (
            f'@font-face {{ font-family: "{STORY_FONT_FAMILY}"; '
            f'src: url("{font_info.path.name}"); }}'
        )
        font_stack = f'"{STORY_FONT_FAMILY}", {markup_render.DEFAULT_FONT_STACK}'

    css = markup_render.build_css(
        font_stack=font_stack, font_size_pt=font_size, extra_css=extra_css
    )
    html = markup_render.render_html(blocks)

    page_rect = pymupdf.Rect(0, 0, width, height)
    where = page_rect + (margin, margin, -margin, -margin)

    story = pymupdf.Story(html=html, user_css=css, archive=archive)
    data, pages = _layout(story, page_rect, where)

    doc = pymupdf.open(stream=data, filetype="pdf")
    try:
        doc.subset_fonts()
        final = doc.tobytes(deflate=True, garbage=4, clean=True)
        pages = doc.page_count
    finally:
        doc.close()

    size_label = PAGE_SIZE_LABELS.get(options.page_size, options.page_size)
    direction = ORIENTATION_LABELS.get(options.orientation, options.orientation)
    notes = [
        f"已用「{font_info.label}」排版，字号 {font_size}，"
        f"页面 {size_label} {direction}，共 {pages} 页。",
        STYLE_SUBSET_NOTE,
    ]
    return TxtBuildResult(data=final, notes=notes, page_count=pages)


def _layout(
    story: pymupdf.Story, page_rect: pymupdf.Rect, where: pymupdf.Rect
) -> tuple[bytes, int]:
    """把 story 排到若干页上，返回 (PDF 字节, 页数)。"""
    buffer = io.BytesIO()
    writer = pymupdf.DocumentWriter(buffer)
    pages = 0
    more = 1
    try:
        while more:
            pages += 1
            if pages > settings.MAX_TXT_PAGES:
                # 换行极多的文档靠这里兜住：不能等排完几百页才说不行
                raise ValidationError(TOO_MANY_PAGES_MESSAGE)
            device = writer.begin_page(page_rect)
            more, _ = story.place(where)
            story.draw(device)
            writer.end_page()
    finally:
        writer.close()
    return buffer.getvalue(), pages
