"""第六阶段可行性探针：本机能不能真的跑通 OCR。

**这不是产品代码**，是一次性的环境探测脚本，用来在动手写第六阶段之前回答问题：

1. ``rapidocr-onnxruntime``（纯 pip 安装、自带中英文模型）在 Python 3.14 +
   onnxruntime 1.30 上**真的能跑**吗？还是只是「装得上、跑不起来」？
2. 中文、英文、数字分别认不认得出来？认不出中文这个方案对本项目就没意义
   （用户要的正是扫描件里的中文）。
3. 一页 200 DPI 的真实 PDF 渲染图要跑多久？这决定超时、页数上限与要不要限流。
4. **识别框的顺序**：第一版探针天真地把引擎返回的框按顺序拼起来，得到
   ``FileTPDFoolstoWord``、``1234. 556`` 这种结果 —— 字符认对了，拼接顺序错了。
   所以这一版把每个框的坐标打出来，并按「先上后下、同一行先左后右」重排后再比。
   这直接决定 OCR 层要不要自己写一个 reading-order 排序（答案是：要）。

跑法（用探针自己的 venv，别污染 backend/.venv）：

    "$PROBE/Scripts/python.exe" scripts/probe_ocr.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

#: 探针要认的内容。中文是重点：只认英文的 OCR 对本项目没用。
CASES = [
    ("纯中文", "文件转换测试系统"),
    ("纯英文", "FileTools PDF to Word"),
    ("中英混排", "扫描件识别测试 OCR 2026"),
    ("数字", "订单号 20260927 金额 1234.56"),
]

#: 真实 PDF 页要认的内容。多行 + 中英混排 + 一个数字，接近真实文档的样子。
PDF_LINES = [
    "采购合同",
    "Contract No. HT-2026-0927",
    "甲方：北京示例科技有限公司",
    "乙方：Shanghai Sample Trading Co., Ltd.",
    "合同金额：人民币 1234.56 元",
]

FONT = Path("C:/Windows/Fonts/simsun.ttc")
FALLBACK_FONTS = [Path("C:/Windows/Fonts/simhei.ttf"), Path("C:/Windows/Fonts/msyh.ttc")]


def _font_path() -> Path:
    path = next((p for p in [FONT, *FALLBACK_FONTS] if p.is_file()), None)
    if path is None:
        raise SystemExit("找不到任何中文字体，无法构造探针图片")
    return path


def render_line(text: str, *, size: int = 48):
    """把一段文字画成白底黑字的图片，模拟扫描页。

    用真实中文字体渲染 —— 拿 PIL 的默认位图字体画中文只会画成一片方框，
    那样测出来的是「字体不对」而不是「OCR 不行」。
    """
    from PIL import Image, ImageDraw, ImageFont

    font = ImageFont.truetype(str(_font_path()), size)
    probe = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    box = probe.textbbox((0, 0), text, font=font)
    image = Image.new("RGB", (box[2] - box[0] + 80, box[3] - box[1] + 80), "white")
    ImageDraw.Draw(image).text((40 - box[0], 40 - box[1]), text, font=font, fill="black")
    return image


def build_real_pdf(path: Path, *, dpi: int = 200) -> tuple[float, float]:
    """做一份「像真文档」的 PDF 并渲染成 PNG，返回 (构造秒, 渲染秒)。

    探针图片再干净也骗人：真实输入是 PDF 渲染出来的位图，带抗锯齿、带
    行间距、带多种字号。所以这里走一遍**真实管线**：写字 → 存 PDF →
    按 200 DPI 渲染 → 交给 OCR。测出来的耗时和准确率才有参考价值。
    """
    import pymupdf

    document = pymupdf.open()
    page = document.new_page()  # 默认 A4
    y = 90.0
    for index, line in enumerate(PDF_LINES):
        size = 22 if index == 0 else 13  # 标题大一点，模拟真实的字号差异
        page.insert_text((72, y), line, fontname="china-s", fontsize=size)
        y += 34 if index == 0 else 26

    started = time.perf_counter()
    pdf_bytes = document.tobytes()
    build_seconds = time.perf_counter() - started

    rendered = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    single = pymupdf.open()
    single.insert_pdf(rendered)
    started = time.perf_counter()
    pixmap = single[0].get_pixmap(dpi=dpi)
    render_seconds = time.perf_counter() - started
    pixmap.save(str(path))
    print(f"  真实 PDF 页已渲染：{path.name} {pixmap.width}×{pixmap.height} px @ {dpi} DPI")
    return build_seconds, render_seconds


def join_boxes(boxes: list, *, sorted_order: bool) -> str:
    """把识别框拼成文字。

    rapidocr 每个框是 ``[四点坐标, 文字, 置信度]`` 三元组。

    ``sorted_order=False`` 就是第一版探针的天真做法：按引擎返回顺序直接拼。
    ``True`` 则先按「先上后下、同一行先左后右」重排 —— 同一条线上 y 差几个像素
    很常见，所以先用行高的一半做容差把框归到同一行，再在行内按 x 排。
    """
    if not sorted_order:
        return "".join(item[1] for item in boxes)

    def center(box):
        xs = [point[0] for point in box]
        ys = [point[1] for point in box]
        return sum(xs) / len(xs), sum(ys) / len(ys)

    entries = []
    for box, text, *_rest in boxes:
        xs = [point[0] for point in box]
        ys = [point[1] for point in box]
        entries.append((min(ys), max(ys), min(xs), center(box), text))

    if not entries:
        return ""
    # 行高的一半当容差：同一行的几个框在垂直方向上总有几像素的抖动
    heights = [high - low for low, high, _, _, _ in entries]
    tolerance = max(sum(heights) / len(heights) / 2, 1.0)

    lines: list[list] = []
    for entry in sorted(entries, key=lambda item: item[0]):
        placed = False
        for line in lines:
            # 与这一行已有框的垂直中心差在容差内，就算同一行
            if abs(entry[0] - line[0][0]) <= tolerance:
                line.append(entry)
                placed = True
                break
        if not placed:
            lines.append([entry])

    ordered: list[str] = []
    for line in sorted(lines, key=lambda group: min(item[0] for item in group)):
        ordered.extend(text for _, _, _, _, text in sorted(line, key=lambda item: item[2]))
    return "".join(ordered)


def normalize(text: str) -> str:
    """比对前去掉所有空白：OCR 会在中英文之间插空格，跟认没认对无关。"""
    return "".join(text.split())


def run_case(engine, label: str, image) -> tuple[bool, float]:
    import numpy

    started = time.perf_counter()
    boxes, _ = engine(numpy.array(image))
    elapsed = time.perf_counter() - started
    boxes = boxes or []
    return boxes, elapsed


def main() -> int:
    print("=== 探针 1：导入 ===")
    try:
        import numpy
        import onnxruntime
        import rapidocr_onnxruntime as rr
    except Exception as exc:  # noqa: BLE001 - 探针就是要抓住一切失败
        print(f"导入失败：{type(exc).__name__}: {exc}")
        return 1
    print(f"  rapidocr-onnxruntime OK / numpy {numpy.__version__} / onnxruntime {onnxruntime.__version__}")

    print("\n=== 探针 2：构造引擎 ===")
    started = time.perf_counter()
    engine = rr.RapidOCR()
    print(f"  引擎就绪，耗时 {time.perf_counter() - started:.2f} 秒")

    print("\n=== 探针 3：合成图 —— 比较「引擎返回顺序」与「重排后的阅读顺序」 ===")
    failures = 0
    timings: list[float] = []
    for label, text in CASES:
        boxes, elapsed = run_case(engine, label, render_line(text))
        timings.append(elapsed)
        raw = join_boxes(boxes, sorted_order=False)
        ordered = join_boxes(boxes, sorted_order=True)
        hit = normalize(text) in normalize(ordered)
        if not hit:
            failures += 1
        print(f"  [{'OK ' if hit else 'MISS'}] {label}  （{len(boxes)} 个框，{elapsed:.2f} 秒）")
        print(f"        期望：{text}")
        print(f"        返回顺序：{raw!r}")
        print(f"        重排之后：{ordered!r}")

    print("\n=== 探针 4：真实 PDF 渲染图（这才是真正的输入）===")
    png = Path(__file__).resolve().parent / "_probe_page.png"
    build_seconds, render_seconds = build_real_pdf(png)
    print(f"  构造 {build_seconds:.3f} 秒 / 渲染 {render_seconds:.3f} 秒")

    from PIL import Image

    image = Image.open(png)
    boxes, elapsed = run_case(engine, "pdf", image)
    ordered = join_boxes(boxes, sorted_order=True)
    print(f"  OCR 耗时 {elapsed:.2f} 秒（{len(boxes)} 个框）")
    print(f"  重排之后：{ordered!r}")
    for line in PDF_LINES:
        found = normalize(line) in normalize(ordered)
        if not found:
            failures += 1
        print(f"    [{'OK ' if found else 'MISS'}] {line}")

    print("\n=== 结论 ===")
    if timings:
        print(f"  小图最快 {min(timings):.2f} 秒 / 最慢 {max(timings):.2f} 秒")
    print(f"  真实 A4 页 @200DPI：OCR {elapsed:.2f} 秒 + 渲染 {render_seconds:.2f} 秒")
    if failures == 0:
        print("  PASS：中英文与数字全部认对，OCR 方案在本机可用")
        return 0
    print(f"  FAIL：{failures} 处没认对，方案需要重新评估")
    return 1


if __name__ == "__main__":
    sys.exit(main())
