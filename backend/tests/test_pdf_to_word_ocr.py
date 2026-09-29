"""OCR 引擎层（第六阶段 A）。

分两半：

* **不依赖 OCR 栈的那半**（可用性探测、错误翻译、引擎缓存、锁）永远会跑；
* **需要真引擎的那半**挂 :data:`tests.conftest.requires_ocr`，
  没装组件的机器上跳过，不会红成一片。

断言的字符串都是**实测认得准**的（纯中文、纯英文、纯数字）。
中英混排、带标点的长串只经 ``normalize_ocr_text`` 之后断言 ——
不这么做的话，断言会因为「OCR 在两个词之间少放了一个空格」变红，
看起来像 bug，其实和正确性无关。
"""

from __future__ import annotations

import io

import pymupdf
import pytest

from services import ocr_service
from services.ocr_service import OcrLine, order_boxes, recognize
from tests.conftest import (
    build_scanned_pdf,
    normalize_ocr_text,
    requires_ocr,
)


def scanned_png(label: str, *, dpi: int = 200) -> bytes:
    """把一行大字渲染成 PDF 再按指定 DPI 转成 PNG —— 和真实管线一致。"""
    data = build_scanned_pdf([label])
    document = pymupdf.open(stream=data, filetype="pdf")
    try:
        return document[0].get_pixmap(dpi=dpi).tobytes("png")
    finally:
        document.close()


def blank_png(*, width: int = 800, height: int = 600) -> bytes:
    from PIL import Image

    stream = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(stream, format="PNG")
    return stream.getvalue()


# ----------------------------------------------------------------------
# 不依赖引擎的部分
# ----------------------------------------------------------------------


def test_component_availability_is_a_boolean() -> None:
    assert isinstance(ocr_service.is_available(), bool)


def test_disabling_via_env_reports_unavailable(monkeypatch) -> None:
    """``FILETOOLS_OCR_DISABLED=1`` 要能在装了 OCR 的机器上模拟「没装」。

    验收脚本靠它验证降级提示和「文字版 PDF 仍然可用」这两件事 ——
    没有这个开关就得真的去卸包才能测。
    """
    monkeypatch.setattr(ocr_service.settings, "OCR_DISABLED", True)
    assert ocr_service.is_available() is False


def test_available_languages_are_reported_as_advertised() -> None:
    """内置模型覆盖的字符集，只读、不作为请求参数。"""
    assert ocr_service.available_languages() == ["chi_sim", "eng"]


def test_recognising_without_the_component_reports_unavailable(monkeypatch) -> None:
    """组件没装时给 OCR_UNAVAILABLE（503），**不是 500**。

    文件本身没毛病，重传多少次都一样 —— 用户的下一步是找管理员。
    """
    from utils.errors import OcrUnavailableError

    monkeypatch.setattr(ocr_service, "is_available", lambda: False)
    with pytest.raises(OcrUnavailableError) as caught:
        recognize(blank_png(), dpi=200)
    assert "未安装 OCR 组件" in caught.value.message


def test_a_broken_engine_is_reported_as_unavailable(monkeypatch) -> None:
    """引擎构造失败算「组件不可用」，不算「这一页识别失败」。

    模型文件缺失、onnxruntime 装坏了都是**服务器的问题**，
    和用户传了什么无关，该走 503。
    """
    import sys
    import types

    from utils.errors import OcrUnavailableError

    def explode():
        raise RuntimeError("模型文件缺失")

    fake = types.ModuleType("rapidocr_onnxruntime")
    fake.RapidOCR = explode  # type: ignore[attr-defined]

    ocr_service.reset_engine()
    monkeypatch.setitem(sys.modules, "rapidocr_onnxruntime", fake)
    monkeypatch.setattr(ocr_service, "is_available", lambda: True)
    try:
        with pytest.raises(OcrUnavailableError):
            recognize(blank_png(), dpi=200)
    finally:
        ocr_service.reset_engine()


def test_a_broken_image_is_reported_as_a_failed_recognition(monkeypatch) -> None:
    """图本身是坏的 → OCR_FAILED，而不是把库的异常直接抛成 500。

    喂进去的不是图片时，解码那一步就会失败；这条路径必须在
    引擎被调用之前就把异常翻译掉。
    """
    from utils.errors import OcrFailedError

    monkeypatch.setattr(ocr_service, "is_available", lambda: True)
    monkeypatch.setattr(ocr_service, "_load_engine", lambda: (lambda image: None))
    with pytest.raises(OcrFailedError) as caught:
        recognize(b"this is not an image", dpi=200)
    assert "Traceback" not in caught.value.message
    assert "site-packages" not in caught.value.message


def test_the_engine_is_constructed_only_once() -> None:
    """引擎是重量级的（构造几百毫秒、常驻几百 MB），全进程只留一个。

    每页都新建一个的话，30 页的扫描件要构造 30 次引擎，
    内存和时间都白花。
    """
    if not ocr_service.is_available():
        pytest.skip("本机没有 OCR 组件")

    ocr_service.reset_engine()
    try:
        first = ocr_service._load_engine()
        assert ocr_service._load_engine() is first
    finally:
        ocr_service.reset_engine()


def test_the_engine_can_be_called_when_it_is_already_loaded() -> None:
    """引擎已就绪时不再重复构造 —— 这是上面那条的另一面。"""
    if not ocr_service.is_available():
        pytest.skip("本机没有 OCR 组件")

    ocr_service._load_engine()
    assert ocr_service._load_engine() is not None


# ----------------------------------------------------------------------
# 需要真引擎
# ----------------------------------------------------------------------


@requires_ocr
def test_recognises_chinese() -> None:
    """中文认不出来这个方案对本项目就没有意义（用户要的正是扫描件里的中文）。"""
    lines = recognize(scanned_png("文件转换测试系统"), dpi=200)
    assert normalize_ocr_text("文件转换测试系统") in normalize_ocr_text(
        "".join(line.text for line in lines)
    )


@requires_ocr
def test_recognises_english() -> None:
    """英文整句要认对，单词之间的空格也要在 —— 英文不能去掉空白比对。"""
    lines = recognize(scanned_png("FileTools PDF to Word"), dpi=200)
    assert lines
    assert normalize_ocr_text("FileTools PDF to Word") == normalize_ocr_text(lines[0].text)


@requires_ocr
def test_recognises_digits() -> None:
    """数字认不准的话，合同金额、订单号就没法用。"""
    lines = recognize(scanned_png("INVOICE No. 88123"), dpi=200)
    text = normalize_ocr_text("".join(line.text for line in lines))
    assert "88123" in text


@requires_ocr
def test_a_blank_page_yields_no_lines() -> None:
    """空白页认不出东西是**事实**，不是错误。

    真实扫描件里空白页不少（分隔页、背面）。把它当失败的话，
    一份 20 页的扫描件只要夹一张空白页就整份失败。
    """
    assert recognize(blank_png(), dpi=200) == []


@requires_ocr
def test_lines_come_back_with_a_confidence() -> None:
    """置信度要落在 0–1 之间，用来如实提示识别把握。"""
    lines = recognize(scanned_png("采购合同"), dpi=200)
    assert lines
    for line in lines:
        assert 0.0 <= line.confidence <= 1.0
        assert isinstance(line, OcrLine)


@requires_ocr
def test_reading_order_is_rebuilt_for_a_real_page() -> None:
    """真实页面上的阅读顺序也要对。

    喂合成框能验排序算法，但只有真引擎能验「引擎确实不按阅读顺序返回」——
    如果哪天引擎改成按顺序返回了，这条还在，说明我们仍然是对的；
    而万一重排被误删，喂合成框的那几条也会立刻红。
    """
    lines = recognize(scanned_png("订单号 20260927"), dpi=200)
    joined = normalize_ocr_text("".join(line.text for line in lines))
    assert joined.startswith("订单号")
    assert "20260927" in joined


@requires_ocr
def test_a_higher_dpi_does_not_break_recognition() -> None:
    """重试用的高 DPI 也要认得出来 —— 低 DPI 认失败时要退到这条路。"""
    lines = recognize(scanned_png("文件转换测试系统", dpi=300), dpi=300)
    assert normalize_ocr_text("文件转换测试系统") in normalize_ocr_text(
        "".join(line.text for line in lines)
    )


@requires_ocr
def test_order_boxes_agrees_with_the_live_engine() -> None:
    """把引擎的真实输出喂给排序函数，结果要和 :func:`recognize` 一致。

    防止「``recognize`` 里偷偷做了别的处理」这类漂移。
    """
    png = scanned_png("FileTools PDF to Word")
    engine = ocr_service._load_engine()
    import numpy
    from PIL import Image

    raw = engine(numpy.array(Image.open(io.BytesIO(png)).convert("RGB")))[0]

    assert order_boxes(raw) == recognize(png, dpi=200)
