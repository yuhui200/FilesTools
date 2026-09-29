"""OCR 组件（第六阶段 A）。

这一层存在的理由和 ``office_converter.py`` 一样：把「用哪个引擎、怎么调」
关在一个模块里。改引擎、换模型、调参数都只动这里，工作流那边不受影响。

引擎选的是 **rapidocr-onnxruntime**：纯 pip 安装、自带中英文模型、
不依赖系统里另外装 Tesseract。规格里说「优先评估 Tesseract」——
本机（以及绝大多数 Windows 服务器）根本没有 Tesseract，装它还要额外走
系统级安装程序；rapidocr 是 ``pip install`` 就能跑的那个，所以先用它。
换引擎只需要改这个文件里的 ``_load_engine``。

**可用性探测和引擎本身分开**：``is_available()`` 只做一次 ``find_spec``，
不导入 onnxruntime、更不构造引擎 —— 前端在打开首页时就会问一次
``/api/config``，为了回答这个问题去加载一个几百 MB 的推理运行时是不合适的。
"""

from __future__ import annotations

import importlib.util
import io
import logging
import threading
from dataclasses import dataclass

from config import settings
from utils.errors import OcrFailedError, OcrUnavailableError

__all__ = [
    "MISSING_OCR_MESSAGE",
    "OcrLine",
    "available_languages",
    "is_available",
    "order_boxes",
    "recognize",
    "reset_cache",
    "reset_engine",
]

logger = logging.getLogger(__name__)

#: 面向用户的统一文案。OCR 组件没装时用户的下一步是找管理员，
#: 不是重新上传文件 —— 所以这不能复用「处理失败」那类文案。
MISSING_OCR_MESSAGE = "当前服务器未安装 OCR 组件，暂时无法处理扫描 PDF。"

#: 识别本身失败（组件在、但这一页认不出来）。
FAILED_OCR_MESSAGE = "这一页的 OCR 识别没有成功，请稍后重试。"

#: 等不到 OCR 引擎。单飞锁是**有上限的等待**，不是无限排队。
BUSY_OCR_MESSAGE = "服务器正在处理别的扫描件，请稍后再试。"

#: 引擎依赖的包名。rapidocr 会连带拉起 onnxruntime 和 numpy。
_REQUIRED_MODULES = ("rapidocr_onnxruntime", "onnxruntime", "numpy")

#: 同一行里两个识别框之间空出「框高 × 这个倍数」就补一个空格。
#: 中文的字与字之间几乎挨着（间距接近 0），而中英混排、分列的数字之间
#: 会空出半个字高 —— 不补空格的话 ``OCR 2026`` 会粘成 ``OCR2026``，
#: 英文单词也会连成一片。
LINE_GAP_FACTOR = 0.25

#: 只缓存**成功**结果，这样服务器上新装了 OCR 组件不必重启服务
#: （和 ``office_converter`` 缓存 soffice 路径同理）。
_available: bool | None = None
_PROBE_LOCK = threading.Lock()

#: 引擎是重量级的（构造要几百毫秒、常驻几百 MB），全进程只留一个。
_ENGINE_LOCK = threading.Lock()
_engine = None

#: **OCR 是单飞的。** onnxruntime 的会话能吃满所有核心，几个扫描件同时进来
#: 只会互相拖慢、把内存顶爆，总耗时反而更长。和 soffice 那个锁同一个理由，
#: 也用同样的**有上限等待** —— 拿不到就报错，不无限排队：
#: 被取消却还活着的线程会一直攥着锁，无上限的等待会把线程池耗光。
_OCR_LOCK = threading.Lock()


def is_available() -> bool:
    """服务器上能不能做 OCR。前端在上传之前就会问这个。

    ``FILETOOLS_OCR_DISABLED=1`` 会强制返回否 —— 给验收脚本用：
    在一台装了 OCR 的机器上模拟「服务器没装 OCR 组件」，
    验证降级提示和「文字版 PDF 仍然可用」这两件事。
    """
    global _available
    if settings.OCR_DISABLED:
        return False
    with _PROBE_LOCK:
        if _available is None:
            _available = all(
                importlib.util.find_spec(name) is not None for name in _REQUIRED_MODULES
            )
            if not _available:
                logger.info("未安装 OCR 组件 %s，扫描 PDF 将不可用", _REQUIRED_MODULES)
        return _available


def available_languages() -> list[str]:
    """内置 OCR 模型覆盖的字符集。

    **这不是可切换的语言包。** rapidocr 是单一内置中英文模型，
    构造函数没有 ``lang`` 参数，把这里的值传回去不会有任何效果。
    它只表示「模型认得出这些字符集」，供前端如实展示。
    """
    return list(settings.OCR_LANGUAGES)


def reset_cache() -> None:
    """清掉可用性缓存。测试和验收脚本用。"""
    global _available
    with _PROBE_LOCK:
        _available = None


def reset_engine() -> None:
    """丢掉已构造的引擎。测试用。

    引擎构造失败那条路径只有在**没有**缓存引擎时才走得到，
    而它恰恰是最值得测的一条（模型文件缺失、onnxruntime 装坏了）——
    没有这个入口就只能去动全局变量。
    """
    global _engine
    with _ENGINE_LOCK:
        _engine = None


@dataclass(slots=True, frozen=True)
class OcrLine:
    """识别出来的一行文字。

    ``confidence`` 是这一行各识别框的平均置信度（0–1）。它只用来在结果说明里
    如实提醒「有几页识别得不太有把握」，不参与对错判断 ——
    0.75 的置信度也可能完全正确，0.95 也可能错一个字。
    """

    text: str
    confidence: float


def recognize(image: bytes, *, dpi: int) -> list[OcrLine]:
    """把一页的渲染图认成若干行文字，按**阅读顺序**返回。

    ``image`` 是 PNG 字节。返回空列表表示这一页没认出任何文字
    （空白页、或者整页都是图）—— 这不是错误，是事实。

    **阅读顺序必须自己排。** 引擎返回的顺序既不是阅读顺序、也不保证稳定，
    天真拼接会得到 ``FileTPDFoolstoWord`` 这种结果：字都认对了，顺序是乱的。
    见 :func:`order_boxes`。
    """
    if not is_available():
        raise OcrUnavailableError(MISSING_OCR_MESSAGE)

    engine = _load_engine()
    if not _OCR_LOCK.acquire(timeout=settings.PDF_TO_WORD_OCR_LOCK_WAIT_SECONDS):
        logger.warning("等待 OCR 引擎超过 %s 秒", settings.PDF_TO_WORD_OCR_LOCK_WAIT_SECONDS)
        raise OcrFailedError(BUSY_OCR_MESSAGE)
    try:
        boxes = _detect(engine, image)
    finally:
        _OCR_LOCK.release()

    return order_boxes(boxes)


def _load_engine():
    """构造（并缓存）OCR 引擎。

    构造失败按「组件不可用」处理而不是「这一页识别失败」：模型文件缺失、
    onnxruntime 装坏了都属于**服务器的问题**，用户重传多少次都一样，
    该走 503 让 TA 去找管理员。
    """
    global _engine
    with _ENGINE_LOCK:
        if _engine is None:
            try:
                from rapidocr_onnxruntime import RapidOCR
            except Exception as exc:  # noqa: BLE001 - 装坏了也算不可用
                logger.error("OCR 引擎导入失败", exc_info=True)
                raise OcrUnavailableError(MISSING_OCR_MESSAGE) from exc
            try:
                _engine = RapidOCR()
            except Exception as exc:  # noqa: BLE001 - 模型缺失 / 运行时故障
                logger.error("OCR 引擎初始化失败", exc_info=True)
                raise OcrUnavailableError(MISSING_OCR_MESSAGE) from exc
            logger.info("OCR 引擎已就绪")
        return _engine


def _detect(engine, image: bytes) -> list:
    """跑一次识别，返回原始的识别框列表。

    引擎的 stdout / stderr 只进日志，绝不跟着响应回给前端（§十三）——
    它里面会带模型路径和服务器信息。
    """
    try:
        import numpy
        from PIL import Image

        # 交给引擎的是 ndarray 而不是 PNG 字节：省掉引擎内部的一次解码，
        # 也让「图是坏的」这件事在这一步就暴露成明确的异常。
        picture = numpy.array(Image.open(io.BytesIO(image)).convert("RGB"))
        output = engine(picture)
    except Exception as exc:  # noqa: BLE001 - 识别失败不该是 500
        logger.warning("OCR 识别失败", exc_info=True)
        raise OcrFailedError(FAILED_OCR_MESSAGE) from exc

    # rapidocr 返回 (结果, 耗时)；没有文字时结果是 None
    boxes = output[0] if isinstance(output, tuple) else output
    return list(boxes or [])


def order_boxes(boxes: list) -> list[OcrLine]:
    """把识别框排成阅读顺序：先上后下，同一行先左后右。

    每个框是 ``[四点坐标, 文字, 置信度]``。同一行的框在垂直方向上总有几个
    像素的抖动，所以先用**平均框高的一半**做容差把它们归到同一行，再在行内按
    左边坐标排序 —— 直接按 y 排会把同一行拆成好几行。
    """
    entries = []
    for item in boxes or []:
        quad, text, *rest = item
        text = (text or "").strip()
        if not text:
            continue
        xs = [float(point[0]) for point in quad]
        ys = [float(point[1]) for point in quad]
        confidence = float(rest[0]) if rest and rest[0] is not None else 0.0
        entries.append((min(ys), max(ys), min(xs), max(xs), text, confidence))

    if not entries:
        return []

    heights = [bottom - top for top, bottom, *_ in entries]
    tolerance = max(sum(heights) / len(heights) / 2, 1.0)

    rows: list[list[tuple]] = []
    for entry in sorted(entries, key=lambda item: item[0]):
        for row in rows:
            if abs(entry[0] - row[0][0]) <= tolerance:
                row.append(entry)
                break
        else:
            rows.append([entry])

    return [
        _join_row(sorted(row, key=lambda item: item[2]))
        for row in sorted(rows, key=lambda group: min(item[0] for item in group))
    ]


def _join_row(row: list[tuple]) -> OcrLine:
    """把同一行的几个框拼成一行文字，该断的地方补空格。"""
    parts: list[str] = []
    previous_right = None
    for top, bottom, left, right, text, _confidence in row:
        if previous_right is not None:
            gap = left - previous_right
            if gap > max((bottom - top) * LINE_GAP_FACTOR, 1.0):
                parts.append(" ")
        parts.append(text)
        previous_right = right

    confidences = [entry[5] for entry in row]
    return OcrLine(
        text="".join(parts).strip(),
        confidence=sum(confidences) / len(confidences),
    )
