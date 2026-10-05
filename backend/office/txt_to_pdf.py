"""TXT → PDF 排版（纯 PyMuPDF，不经过 LibreOffice）。

LibreOffice 也能导入纯文本，但它导入 .txt 时没有字号、字体、页面大小、
页面方向这些控制项 —— 满足不了「TXT 排版四项可选」的要求，所以这一路
自己用 PyMuPDF 排。这也是全项目唯一不碰 LibreOffice 的文档转换。

**排版为什么不是「把整篇丢给 fill_textbox」**（这一段是实测换来的，别改回去）：

``TextWriter.fill_textbox`` 的返回协议是：放得下返回 ``[]``，放不下返回
``[(没排下的片段, 剩余高度), …]``，那些片段是**已经断好的行**。于是最自然的
写法「把返回的片段拼起来喂给下一页」有个隐蔽的坑 —— 拼接会丢掉所有换行，
第二页收到的是**一整条没有空白的长串**。而单次 ``fill_textbox`` 的耗时是
喂入字数的**三次方**（实测 5000 字 2.3s、10000 字 17.7s、20000 字 139.6s，
纯汉字无空格一样慢，与内容无关，只与这一次喂了多少字有关）：

    2 万字正文分 8 页：17.5 秒**全部**花在 fill_textbox 里，write_text 只占 0.15 秒

所以这里两条一起用：

1. **按段喂**，且每次调用喂的字数封顶 ``_CALL_CHAR_CAP``（见该常量的说明）；
2. **不把「剩下的全文」反复重排** —— 放不下时把片段用 ``\\n`` 拼回去
   （它们本来就是一行行的），续排的文本因此仍然带换行、不会退化成无空白长串。

实测结果：正常文档 20 万字 1.9 秒排完 86 页，改前同样的文本要几百秒；
``"A" × 20000`` 这类最坏形状从 140 秒降到 1.3 秒，且全文一字不差。

字体必须**按文件探测**（``available_fonts``）：PyMuPDF 的内置中文码
china-s / china-ss / china-t / japan / korea 实测全部解析成同一个
Droid Sans Fallback，拿它们冒充宋体、黑体就是个不生效的假控件。
探测不到任何系统字体时只提供内置字体，并如实说明。

``doc.subset_fonts()`` 是必须的，不是优化：一页中文不子集化 1.62 MB，
子集化后 9.9 KB（0.6%）。漏掉这一步产物会大到荒唐。
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

from config import settings
from utils.errors import ValidationError

__all__ = [
    "TxtBuildResult",
    "TxtFont",
    "TxtOptions",
    "available_fonts",
    "build_pdf_from_text",
    "find_font",
]

#: 单次 ``fill_textbox`` 允许喂入的最大字数。
#:
#: 实测耗时 ≈ 1.85e-11 × 字数的三次方，与内容无关。1500 字最坏约 0.06 秒；
#: 而一页（A4 / 11pt）大约装得下 2400 字，所以一个块必然整块落在同一页上，
#: 不会出现「同一段被从中间截断到下一页」的情况。
_CALL_CHAR_CAP = 1500

#: 找系统字体的目录。Windows 是 Fonts，Linux 发行版各不相同，
#: 所以 Linux 下递归扫这几个常见根目录，再深就不扫了（字体不会埋在深层）。
_FONT_DIRS_WIN = ("{WINDIR}/Fonts",)
_FONT_DIRS_POSIX = (
    "/usr/share/fonts",
    "/usr/local/share/fonts",
    "/usr/share/fonts/truetype",
    "~/.fonts",
    "~/.local/share/fonts",
)
#: Linux 下递归扫字体目录的深度上限，避免在异常目录结构上白耗时间
_FONT_SCAN_DEPTH = 4

PAGE_SIZE_LABELS: dict[str, str] = {"a4": "A4", "a5": "A5", "letter": "Letter"}
ORIENTATION_LABELS: dict[str, str] = {"portrait": "纵向", "landscape": "横向"}

TOO_LONG_MESSAGE = (
    f"这个文本太长了（超过 {settings.MAX_TXT_CHARS} 字），"
    "转换会排成几百页、等很久，请先拆分后再上传。"
)
TOO_MANY_PAGES_MESSAGE = (
    f"这个文本排出来超过 {settings.MAX_TXT_PAGES} 页，请先拆分后再上传。"
)


@dataclass(slots=True)
class TxtFont:
    """一个可用的排版字体。

    ``path`` 为 None 表示用的是 PyMuPDF 内置字体（不需要字体文件）。
    """

    key: str
    label: str
    path: Path | None


@dataclass(slots=True)
class TxtOptions:
    """TXT 排版选项，对应界面上的四项。

    ``font`` 的默认值必须**运行时探测**，不能写成 ``TXT_FALLBACK_FONT``：
    那个键只在「一个系统字体都没探测到」时才在可选列表里，本机装了宋体时
    直接拿它去排版会被 ``resolve_font`` 拒掉（"没有「china-s」这个字体"）。
    这里用 default_factory 取可用字体的第一项，与
    ``routers/pdf_params.parse_txt_options`` 的默认选择完全一致。
    """

    font: str = field(default_factory=lambda: available_fonts()[0].key)
    font_size: int = settings.TXT_DEFAULT_FONT_SIZE
    page_size: str = settings.TXT_PAGE_SIZES[0]
    orientation: str = settings.TXT_ORIENTATIONS[0]


@dataclass(slots=True)
class TxtBuildResult:
    """排版结果。"""

    data: bytes
    notes: list[str]
    page_count: int


# ----------------------------------------------------------------------
# 字体探测
# ----------------------------------------------------------------------

#: 只在第一次真正排版时扫一次字体目录。字体不会在服务运行期间凭空出现，
#: 而扫一次 Fonts 目录（本机 544 个文件）虽然不贵，也没必要每次都扫。
_font_cache: list[TxtFont] | None = None


def _font_dirs() -> list[Path]:
    """要搜索的字体目录（存在的才算）。"""
    if sys.platform == "win32":
        windir = os.environ.get("WINDIR", r"C:\Windows")
        raw = [item.format(WINDIR=windir) for item in _FONT_DIRS_WIN]
    else:
        raw = list(_FONT_DIRS_POSIX)

    dirs: list[Path] = []
    for item in raw:
        path = Path(item).expanduser()
        if path.is_dir():
            dirs.append(path)
    return dirs


def _scan_font_files() -> dict[str, Path]:
    """扫一遍字体目录，返回 {小写文件名: 路径}。

    按文件名索引而不是按字体内部名称：候选表里写的就是文件名，
    这样两边是同一套标识，不必再去解析字体表。
    """
    found: dict[str, Path] = {}
    for directory in _font_dirs():
        for path in _iter_font_files(directory, _FONT_SCAN_DEPTH):
            found.setdefault(path.name.lower(), path)
    return found


def _iter_font_files(directory: Path, depth: int):
    """遍历目录下的字体文件，深度受限。"""
    try:
        entries = list(os.scandir(directory))
    except OSError:
        return
    for entry in entries:
        try:
            if entry.is_dir(follow_symlinks=False):
                if depth > 0:
                    yield from _iter_font_files(Path(entry.path), depth - 1)
            elif entry.name.lower().endswith((".ttf", ".ttc", ".otf", ".otc")):
                yield Path(entry.path)
        except OSError:  # pragma: no cover - 扫描期间目录被删
            continue


def available_fonts() -> list[TxtFont]:
    """这台机器上真正能用的排版字体，按 ``settings.TXT_FONT_CANDIDATES`` 的顺序。

    一个系统字体都探测不到时，只返回内置字体一项 —— 界面上就只显示这一项，
    而不是列一堆选了也没效果的字体。
    """
    global _font_cache
    if _font_cache is not None:
        return list(_font_cache)

    files = _scan_font_files()
    fonts: list[TxtFont] = []
    for key, (label, candidates) in settings.TXT_FONT_CANDIDATES.items():
        for name in candidates:
            # 文件在、但 PyMuPDF 读不动 —— 跳过的**是这个文件**，不是这个字体键：
            # 还有下一个候选文件名可以试。为什么某个文件会被拦，见
            # settings.TXT_FONT_FILES_PYMUPDF_CANNOT_READ 上面那段实测记录。
            if name.lower() in settings.TXT_FONT_FILES_PYMUPDF_CANNOT_READ:
                continue
            path = files.get(name.lower())
            if path is not None:
                fonts.append(TxtFont(key=key, label=label, path=path))
                break

    if not fonts:
        fonts = [
            TxtFont(
                key=settings.TXT_FALLBACK_FONT,
                label=settings.TXT_FALLBACK_FONT_LABEL,
                path=None,
            )
        ]

    _font_cache = fonts
    return list(fonts)


def find_font(key: str) -> TxtFont:
    """按字体键取出它的说明，**不加载字体文件**。

    Word 那边要的是字体名（「宋体」），不是 PyMuPDF 的字体对象；
    为了拿一个名字去解析一遍 .ttc 是白花的力气。键不认识时的报错与
    :func:`resolve_font` 共用这一句 —— 两处各写一句迟早会不一样。
    """
    for item in available_fonts():
        if item.key == key:
            return item

    available = "、".join(item.label for item in available_fonts())
    raise ValidationError(f"没有「{key}」这个字体，可选的是：{available}。")


def resolve_font(key: str) -> tuple[pymupdf.Font, TxtFont]:
    """按字体键取出 PyMuPDF 字体对象与它的说明。

    键不认识时抛 :class:`ValidationError`，**不会悄悄换成别的字体** ——
    用户选了宋体却排成黑体，比直接报错更难发现。界面上的选项来自
    :func:`available_fonts`，正常情况下不会走到这里。
    """
    item = find_font(key)
    try:
        if item.path is None:
            return pymupdf.Font(settings.TXT_FALLBACK_FONT), item
        return pymupdf.Font(fontfile=str(item.path)), item
    except Exception as exc:  # pragma: no cover - 字体文件损坏
        raise ValidationError(
            f"无法加载字体「{item.label}」，请换一个字体再试。"
        ) from exc


# ----------------------------------------------------------------------
# 排版
# ----------------------------------------------------------------------

def build_pdf_from_text(text: str, options: TxtOptions) -> TxtBuildResult:
    """把纯文本排成一份 PDF，返回字节、页数与需要说明的事项。"""
    if len(text) > settings.MAX_TXT_CHARS:
        raise ValidationError(TOO_LONG_MESSAGE)

    font, font_info = resolve_font(options.font)
    font_size = max(
        settings.TXT_MIN_FONT_SIZE, min(settings.TXT_MAX_FONT_SIZE, int(options.font_size))
    )

    width, height = settings.PDF_PAGE_SIZES[options.page_size]
    if options.orientation == "landscape":
        width, height = height, width

    margin = settings.TXT_MARGIN_PT
    leading = font_size * settings.TXT_LINE_HEIGHT
    if height - margin * 2 < leading:
        # 页面太小、字号太大，一行都放不下。与其死循环，不如直接说清楚。
        raise ValidationError("当前页面大小配这个字号放不下一行，请调小字号或换更大的页面。")

    doc = pymupdf.open()
    try:
        pages = _layout_text(
            doc,
            text,
            font,
            font_size=font_size,
            width=width,
            height=height,
            margin=margin,
            leading=leading,
        )
        # 必须子集化：不子集化一页中文 1.62 MB，子集化后 9.9 KB
        doc.subset_fonts()
        data = doc.tobytes(deflate=True, garbage=4, clean=True)
    finally:
        doc.close()

    size_label = PAGE_SIZE_LABELS.get(options.page_size, options.page_size)
    direction = ORIENTATION_LABELS.get(options.orientation, options.orientation)
    # 写「字号 11」而不是「11 号」：中文排版里的「号」是另一套字号刻度
    # （初号 / 小初 / 一号…），说「11 号」会被读成那套刻度，与实际不符。
    notes = [
        f"已用「{font_info.label}」排版，字号 {font_size}，"
        f"页面 {size_label} {direction}，共 {pages} 页。"
    ]
    return TxtBuildResult(data=data, notes=notes, page_count=pages)


def _layout_text(
    doc: pymupdf.Document,
    text: str,
    font: pymupdf.Font,
    *,
    font_size: float,
    width: float,
    height: float,
    margin: float,
    leading: float,
) -> int:
    """把文本逐段排进 doc，返回页数。超出页数上限时抛 ValidationError。"""
    left, right, bottom = margin, width - margin, height - margin
    text_width = right - left
    max_pages = settings.MAX_TXT_PAGES

    page = doc.new_page(width=width, height=height)
    writer = pymupdf.TextWriter(page.rect)
    y = margin
    pages = 1

    def new_page() -> None:
        nonlocal page, writer, y, pages
        writer.write_text(page)
        if pages >= max_pages:
            # 换行极多的文件靠这里兜住：不能等排完几百页才说不行
            raise ValidationError(TOO_MANY_PAGES_MESSAGE)
        page = doc.new_page(width=width, height=height)
        writer = pymupdf.TextWriter(page.rect)
        y = margin
        pages += 1

    for paragraph in _split_lines(text):
        for piece in _split_long(paragraph):
            carry = piece
            while True:
                if y + leading > bottom:
                    new_page()
                    continue
                if not carry:
                    y += leading  # 空行：占一行高度
                    break

                rect = pymupdf.Rect(left, y, right, bottom)
                leftover = writer.fill_textbox(
                    rect,
                    carry,
                    font=font,
                    fontsize=font_size,
                    lineheight=settings.TXT_LINE_HEIGHT,
                )
                if not leftover:
                    y = writer.last_point.y + leading * 0.25
                    break

                rest = "\n".join(
                    item[0] if isinstance(item, (list, tuple)) else item for item in leftover
                )
                if len(rest) >= len(carry):
                    # 一整页都排不下、片段拼回来还没变短：说明这一段里有
                    # 单条比行宽还长的串。硬切一刀，保证每一轮都有进展。
                    head, tail = _hard_break(carry, font, font_size, text_width)
                    if not head:  # 一个字符都放不下，防死循环
                        head, tail = carry[:1], carry[1:]
                    writer.append(
                        (left, y + font_size), head, font=font, fontsize=font_size
                    )
                    rest = tail
                new_page()
                carry = rest

    writer.write_text(page)
    return pages


def _split_lines(text: str) -> list[str]:
    """把文本切成段落。

    统一换行符：Windows 的 ``\\r\\n`` 若原样留着，``\\r`` 会跟着每段末尾
    一起交给排版，成为看不见的杂字符。制表符展开成 4 个空格、
    换页符当成段落分隔 —— 都是纯文本里常见、而排版器不一定认的东西。
    """
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").replace("\f", "\n")
    return normalized.replace("\t", "    ").split("\n")


def _split_long(paragraph: str) -> list[str]:
    """把过长的段落切成不超过 ``_CALL_CHAR_CAP`` 的块。

    汉字之间本来就能断行，硬切一刀看不出来；英文尽量退到空格处切，
    退不到（整段没有空白）就按字数硬切。见模块说明里关于三次方耗时的实测。
    """
    if len(paragraph) <= _CALL_CHAR_CAP:
        return [paragraph]

    chunks: list[str] = []
    rest = paragraph
    while len(rest) > _CALL_CHAR_CAP:
        cut = rest.rfind(" ", 0, _CALL_CHAR_CAP + 1)
        if cut <= 0:
            cut = _CALL_CHAR_CAP
        chunks.append(rest[:cut])
        rest = rest[cut + 1 :] if rest[cut : cut + 1] == " " else rest[cut:]
    chunks.append(rest)
    return chunks


def _hard_break(
    run: str, font: pymupdf.Font, font_size: float, width: float
) -> tuple[str, str]:
    """把一条比行宽还长的串切成 (放得下的最长前缀, 剩下的)。

    二分找断点：每次只量一个候选前缀，log2(n) 次就能定位，
    比逐字加上去量快得多。
    """
    low, high = 1, len(run)
    while low < high:
        mid = (low + high + 1) // 2
        if font.text_length(run[:mid], fontsize=font_size) <= width:
            low = mid
        else:
            high = mid - 1
    return run[:low], run[low:]
