"""页面范围的解析与选择。

「PDF 转图片」「拆分」「删除页面」「提取页面」四个功能都要面对同一个问题：
用户写下的 ``1-3,5,8`` 到底指哪些页。这份解析只实现一次，
四个功能的报错文案和边界行为才能完全一致。

约定：

- 页码对用户一律是 **1 开始**（第 1 页是第一页），内部统一转成 0 开始的索引；
- 支持 ``1-3``、``1,3,5``、``2-6``，也支持混写 ``1-3,7,10-12``；
- 分隔符兼容中英文逗号、分号与空格 —— 用户从别处粘贴过来的范围常常是中文标点；
- 结果**去重并按页码升序**排列：页序由文档决定，不按用户输入的顺序重排，
  否则「提取」出来的 PDF 页序会跟着输入顺序变，很难解释；
- 上限之外、区间颠倒、空输入都会给出具体的中文提示，而不是笼统的「参数错误」。
"""

from __future__ import annotations

import re

from utils.errors import PdfPageNotFoundError, ValidationError

__all__ = [
    "ALL_PAGES",
    "parse_page_range",
    "parse_page_sequence",
    "parse_range_groups",
    "format_page_list",
    "page_labels",
    "part_labels",
]

# 「全部页面」的关键字，前端下拉框直接传这个值
ALL_PAGES = "all"

# 一个区间：1-3；单独一页：5
_RANGE_RE = re.compile(r"^(\d+)\s*[-–—~]\s*(\d+)$")
_SINGLE_RE = re.compile(r"^\d+$")

# 用户可能用到的分隔符
_SEPARATORS = re.compile(r"[,，;；\s]+")

# 拆分「按范围」时的分段符。**逗号刻意不在其中**：
# 段内的逗号表示同一组里的多页（1-3,7 是一组），分段只能用换行或下面这些符号。
_CHUNK_SEPARATORS = re.compile(r"[;；\n\r|｜/]+")

_FORMAT_HINT = "页面范围格式不正确，示例：1-3、1,3,5、2-6，或填 all 表示全部页面"


def parse_page_range(raw: str | None, page_count: int) -> list[int]:
    """把用户写的范围解析成 0 开始的页码列表。

    ``page_count`` 是文档总页数，用来判断越界。
    """
    if page_count <= 0:
        raise ValidationError("该 PDF 没有任何页面，无法处理")

    text = (raw or "").strip()
    if text == "" or text.lower() == ALL_PAGES:
        return list(range(page_count))

    pages: set[int] = set()
    for token in _SEPARATORS.split(text):
        if not token:
            continue
        pages.update(_parse_token(token, page_count))

    if not pages:
        raise ValidationError("请至少选择一页")

    return sorted(pages)


def parse_page_sequence(raw: str | None, page_count: int) -> list[int]:
    """把用户写的范围解析成**按书写顺序**排列的页码列表（0 开始）。

    与 :func:`parse_page_range` 只差一件事：这里不排序、不去重。

    「提取页面」「自定义页面拆分」的输出页序由用户决定（第四阶段 §11：
    支持拖拽排序，最终输出必须按照新顺序），所以这两处必须保留顺序 ——
    用户把第 5 页拖到最前面，结果的第一页就得是原来的第 5 页。
    重复写同一页也是允许的（``3,3,1`` 会得到三页），
    这比悄悄丢掉一次重复更符合「所见即所得」。

    排序、去重仍然是删除页面、转图片、拆分等场景的正确行为，
    它们继续用 :func:`parse_page_range`。
    """
    if page_count <= 0:
        raise ValidationError("该 PDF 没有任何页面，无法处理")

    text = (raw or "").strip()
    if text == "" or text.lower() == ALL_PAGES:
        return list(range(page_count))

    pages: list[int] = []
    for token in _SEPARATORS.split(text):
        if not token:
            continue
        match = _RANGE_RE.match(token)
        if match:
            start, end = int(match.group(1)), int(match.group(2))
            if start > end:
                raise ValidationError(f"页码范围 {token} 不正确，起始页不能大于结束页")
            _check_bounds(start, page_count, token)
            _check_bounds(end, page_count, token)
            pages.extend(range(start - 1, end))
            continue

        if _SINGLE_RE.match(token):
            number = int(token)
            _check_bounds(number, page_count, token)
            pages.append(number - 1)
            continue

        raise ValidationError(_FORMAT_HINT)

    if not pages:
        raise ValidationError("请至少选择一页")

    return pages


def _parse_token(token: str, page_count: int) -> set[int]:
    match = _RANGE_RE.match(token)
    if match:
        start, end = int(match.group(1)), int(match.group(2))
        if start > end:
            raise ValidationError(f"页码范围 {token} 不正确，起始页不能大于结束页")
        _check_bounds(start, page_count, token)
        _check_bounds(end, page_count, token)
        return set(range(start - 1, end))

    if _SINGLE_RE.match(token):
        number = int(token)
        _check_bounds(number, page_count, token)
        return {number - 1}

    raise ValidationError(_FORMAT_HINT)


def _check_bounds(number: int, page_count: int, token: str) -> None:
    if number < 1:
        raise PdfPageNotFoundError(f"页码 {token} 不存在，页码从 1 开始")
    if number > page_count:
        raise PdfPageNotFoundError(
            f"第 {number} 页不存在，该 PDF 共 {page_count} 页"
        )


def parse_range_groups(raw: str | None, page_count: int) -> list[list[int]]:
    """把「一段一个范围」的文本解析成多组页码，用于「按范围拆分」。

    分段用换行、分号、竖线或斜杠 —— 前端的做法是一行一个范围，
    所以最主要的输入就是换行分隔。**逗号不是分段符**：段内的逗号
    表示同一组有多页（``1-3,7`` 会拆成同一份 PDF 的第 1、2、3、7 页）。

    每一段单独交给 :func:`parse_page_range`，因此报错文案与其它功能完全一致，
    用户不会遇到「同样是 1-3，在这里能填、在那里报错」。
    """
    if page_count <= 0:
        raise ValidationError("该 PDF 没有任何页面，无法处理")

    text = (raw or "").strip()
    if not text:
        raise ValidationError("请至少填写一个页面范围，例如 1-5")

    groups: list[list[int]] = []
    for chunk in _CHUNK_SEPARATORS.split(text):
        chunk = chunk.strip()
        if not chunk:
            continue
        groups.append(parse_page_range(chunk, page_count))

    if not groups:
        raise ValidationError("请至少填写一个页面范围，例如 1-5")

    return groups


def format_page_list(pages: list[int]) -> str:
    """把 0 开始的页码列表压缩成 ``1-3,5`` 这样的展示文案。"""
    if not pages:
        return ""

    ordered = sorted(set(pages))
    parts: list[str] = []
    start = previous = ordered[0]

    for page in ordered[1:]:
        if page == previous + 1:
            previous = page
            continue
        parts.append(_format_span(start, previous))
        start = previous = page
    parts.append(_format_span(start, previous))
    return ",".join(parts)


def _format_span(start: int, end: int) -> str:
    if start == end:
        return str(start + 1)
    return f"{start + 1}-{end + 1}"


def page_labels(pages: list[int]) -> list[str]:
    """给结果文件名用的页码标签：第 1 页 -> 01。"""
    width = max(2, len(str(max(pages) + 1))) if pages else 2
    return [str(page + 1).zfill(width) for page in pages]


def part_labels(count: int) -> list[str]:
    """给 ``part-01.pdf`` 这类文件名用的编号，宽度随份数自适应。

    9 份以内补到两位（part-01），超过 99 份补到三位（part-001），
    这样在文件管理器里按名称排序就是按页码排序。
    """
    if count <= 0:
        return []
    width = max(2, len(str(count)))
    return [str(number).zfill(width) for number in range(1, count + 1)]
