"""文档内容的中间表示（IR）。

读 PDF 的代码（``pdf/analyzer.py``）产出它，写 Word 的代码
（``office/docx_writer.py``）消费它。两边都只依赖这个叶子模块，
谁也不用 import 谁 —— 放在任何一侧都会让那一侧反过来依赖另一侧。

之所以要有这一层，而不是让「读」直接调「写」：读出来的东西必须能被
单元测试单独喂进去。构造一份 ``PageContent`` 不需要真 PDF，也不需要
真去解析 DOCX，于是「段落合并对不对」「标题级别判得对不对」都能单独测。

**这里刻意没有坐标。** python-docx 做不到把文字放到任意坐标上（那要手搓
文本框 XML），所以 IR 里只保留「能落进 Word 的东西」：段落顺序、标题级别、
粗体斜体字号、以及由 x 推断出来的对齐方式。
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = [
    "Block",
    "ImageBlock",
    "PageContent",
    "Paragraph",
    "TableBlock",
    "TextRun",
    "blocks_from_text",
    "PAGE_KIND_SCAN",
    "PAGE_KIND_TEXT",
    "PARA_KIND_BODY",
    "PARA_KIND_CODE",
    "PARA_KIND_LIST",
    "PARA_KIND_QUOTE",
    "PARA_KIND_RULE",
]

#: 这一页的文字是从 PDF 文字层直接抽出来的
PAGE_KIND_TEXT = "text"
#: 这一页没有可用的文字层，内容是靠 OCR 认出来的
PAGE_KIND_SCAN = "scan"

#: 段落种类。默认 ``PARA_KIND_BODY``，所以 PDF→Word 那条路（第七阶段之前
#: 就写好了，构造 ``Paragraph`` 时只给 runs/level/align）一个字都不用改。
PARA_KIND_BODY = "body"
#: 列表项（``marker`` 是项目符号或序号，``indent`` 是嵌套层级）
PARA_KIND_LIST = "list"
#: 引用块
PARA_KIND_QUOTE = "quote"
#: 代码块（等宽、不折行改写）
PARA_KIND_CODE = "code"
#: 分隔线。**没有文字**，是四个种类里唯一一个「空段落也合法」的 ——
#: 别处的空段落一律当成噪音丢掉，这里不行，它本身就是内容。
PARA_KIND_RULE = "rule"


@dataclass(slots=True)
class TextRun:
    """一段样式相同的连续文字。"""

    text: str
    bold: bool = False
    italic: bool = False
    #: 字号（磅）。None 表示「跟正文一样」，写 Word 时不设，让它继承样式。
    size_pt: float | None = None
    #: 原始字体名。只用于排查，不写进 DOCX —— 字体得在读者机器上存在，
    #: 服务端指定的名字到了别人机器上照样会被替换。
    font: str | None = None
    #: 超链接目标。None 表示不是链接。**只存已经过协议过滤的地址**
    #: （见 office/markup_parse.py），危险协议在解析阶段就被剥掉了，
    #: 渲染层拿到的一定是安全的。
    link: str | None = None
    #: 行内代码（``<code>`` / Markdown 的反引号）
    code: bool = False


@dataclass(slots=True)
class Paragraph:
    """一个段落。"""

    runs: list[TextRun] = field(default_factory=list)
    #: 标题级别 1–6；None 表示正文。
    #: （PDF 那条路只会产出 1–3，标记文档那条路能到 6；Word 的
    #: ``Heading N`` 样式本身就支持到 9，两条路都落得下。）
    level: int | None = None
    #: 'left' / 'center' / 'right'；None 表示不显式设置（Word 默认左对齐）
    align: str | None = None
    #: PARA_KIND_* 之一。默认正文 —— 既有构造全是关键字调用，不受影响。
    kind: str = PARA_KIND_BODY
    #: 列表项前缀，例如 ``"•"`` 或 ``"3."``。``kind == PARA_KIND_LIST`` 时才有值。
    marker: str | None = None
    #: 缩进层级，0 表示不缩进（嵌套列表靠它体现）
    indent: int = 0

    @property
    def text(self) -> str:
        return "".join(run.text for run in self.runs)


@dataclass(slots=True)
class ImageBlock:
    """一张要嵌进 Word 的图片。

    ``data`` 必须是 PNG / JPEG 这类 python-docx 认得的格式，
    转换在写之前就做完（PDF 里的图常常是 jpx / ccitt / jbig2）。
    """

    data: bytes
    #: 展示尺寸（磅）。**必须显式给**：不给的话 python-docx 按图片自带的
    #: DPI 算尺寸，一张标着 72 DPI 的扫描图会大到荒唐。
    width_pt: float
    height_pt: float
    #: 图片下方的一句说明，例如「以下文字由 OCR 识别」
    caption: str | None = None


@dataclass(slots=True)
class TableBlock:
    """一张识别出来的表格。

    单元格一律是字符串 —— 识别不可靠时调用方就不该生成这个块，
    而不是生成一个带空洞的表格让用户去修。
    """

    rows: list[list[str]] = field(default_factory=list)


Block = Paragraph | ImageBlock | TableBlock


def blocks_from_text(text: str) -> list[Block]:
    """纯文本 → 一串正文段落。

    这是**最退化的一种解析**：.txt 里没有任何标记，一行就是一个段落。
    之所以要有它，而不是让 TXT 那三个目标各自去切字符串：切法只该有一份，
    否则「空行算不算段落」「行尾的空白留不留」会在 DOCX / HTML / Markdown
    三个结果里各说各话。

    两条规矩，都是照着「文件里看见什么就是什么」定的：

    * **一行一个段落**，不把相邻行并成一段 —— .txt 里换行是作者敲下去的，
      不是排版器折出来的，合并等于替他改稿子；
    * **空行丢掉**。它在这里只是分隔符，而空段落进了 DOCX 是一段真空白、
      进了 HTML 是 ``<p></p>``，都不是用户想要的东西。
      （``PARA_KIND_RULE`` 是唯一一个空段落合法的种类，这里不产生它。）

    行尾的空白要一并去掉：Windows 记事本存出来的文件每行都以 ``\\r`` 结尾，
    留着会变成行尾一个看不见的空格。行首的缩进**保留** —— 那是作者
    用来表示层级的，砍掉就把内容改没了。
    """
    blocks: list[Block] = []
    for line in text.splitlines():
        stripped = line.rstrip()
        if not stripped.strip():
            continue
        blocks.append(Paragraph(runs=[TextRun(text=stripped)]))
    return blocks


@dataclass(slots=True)
class PageContent:
    """PDF 里的一页。"""

    blocks: list[Block] = field(default_factory=list)
    #: PAGE_KIND_TEXT 或 PAGE_KIND_SCAN
    kind: str = PAGE_KIND_TEXT

    @property
    def text(self) -> str:
        """这一页的全部文字，用于「有没有抽出东西」的判断。"""
        parts: list[str] = []
        for block in self.blocks:
            if isinstance(block, Paragraph):
                parts.append(block.text)
            elif isinstance(block, TableBlock):
                parts.extend("".join(row) for row in block.rows)
        return "".join(parts)
