"""统一转换中心的分发层（第七阶段）。

这一层**只做两件事**：判断一个文件到底是什么，以及把「源 → 目标」
翻译成对已有转换函数的一次调用。**转换逻辑一行都没有搬过来** ——
图片走 ``compressors``、图片转 PDF 走 ``pdf.image_to_pdf``、
Office/TXT 走 ``services.doc_service``、PDF 转 Word 走
``services.pdf_to_docx``，四个都是前六个阶段已经在线上跑着的东西。

## 为什么每个转换都写进一个「独占输出目录」

``convert_document`` 与 ``convert_pdf_to_docx`` 会在内部**自己登记下载令牌**
（``register_pdf_result``），而且会把传给它们的那个源文件**删掉**。
这两件事都是既有行为，改了就会动到前六个阶段的实现。

于是这里顺着它们来：把 ``out_dir``（本项独占的 ``out/<n>/``）当作它们的
工作目录传进去。它们登记出来的 Job 的 ``directory`` 正好等于这个独占目录，
而它们删掉的只是**自己那份副本**，我在 ``in/<n>.<ext>`` 留的原件不受影响 ——
重试因此仍然可用。代价是多一次磁盘拷贝与读取，这是刻意的取舍：
**多一次 I/O 远好过改坏一条已经验证过的转换链路。**
"""

from __future__ import annotations

import logging
import shutil
from collections.abc import Awaitable, Callable
from pathlib import Path

from fastapi import UploadFile

from compressors.encoder import extension_for as _encoder_extension_for
from compressors.encoder import normalize_format
from compressors.heif import heif_support
from compressors.pipeline import PipelineOptions, run_pipeline
from config import settings
from conversion import capability, registry
from conversion import options as option_vocab
from office.loader import (
    KIND_TEXT,
    UNSUPPORTED_MARKUP_MESSAGE,
    UNSUPPORTED_OFFICE_MESSAGE,
    OfficeInput,
    validate_markup_upload,
    validate_office_upload,
)
from office.txt_to_pdf import available_fonts
from pdf.image_to_pdf import ImageSource, build_pdf_from_images
from pdf.loader import validate_pdf_upload
from services import batch_service, pdf_to_docx
from services.batch_service import ResultEntry
from services.conversion_types import (
    ConversionOptions,
    ConversionOutput,
    ConversionRequest,
    DetectedSource,
)
from services.doc_service import convert_document
from services.intake import media_type_for, run_in_pool
from services.markup_service import (
    SOURCE_FORMAT_BY_TYPE,
    markup_convert,
    markup_to_pdf,
)
from services.ocr_service import is_available as ocr_available
from services.office_converter import (
    MISSING_COMPONENT_MESSAGE,
    is_available as office_available,
)
from services.pdf_to_docx import docx_available
from services.text_service import text_convert, text_to_docx
from utils.errors import (
    ConverterUnavailableError,
    ProcessingError,
    UnsupportedConversionError,
    UnsupportedTypeError,
    ValidationError,
)
from utils.files import new_token, sanitize_stem
from utils.validation import validate_image_upload, validate_svg_upload

logger = logging.getLogger(__name__)

__all__ = [
    # DTO 原样再导出（真正的定义在 ``services.conversion_types``）：
    # 既有的 ``from services.conversion_service import ConversionOptions``
    # 一行都不用改，搬迁对调用方不可见。
    "ConversionOptions",
    "ConversionOutput",
    "ConversionRequest",
    "DetectedSource",
    "CONVERTERS",
    "as_upload_file",
    "conversion_capabilities",
    "convert_file",
    "convert_file_plan",
    "detect_source",
]

#: 图片源类型 -> 内部格式名（`jpeg` / `png` / `webp` / …）。
#: 注册表对外用线上词汇 `jpg`，编码层用内部名 `jpeg`，这里是两者的桥。
#:
#: **``ico`` 有意不在这张表里**：ICO 不是输入格式（§七 只要求 PNG → ICO），
#: 所以 `.ico` 文件走不到图片这条分支，``detect_source`` 在扩展名那一步
#: 就会给出「暂不支持这种文件」。
#:
#: ``heic`` 写的是 ``heif``：Pillow 报的容器名是 ``HEIF``，
#: ``validate_image_upload`` 返回的也是它（见 ``compressors.heif.PILLOW_FORMAT``）。
#: 这条差异有实质后果，不是命名洁癖 —— 写错的表现是**漏登记**，
#: 于是一个 .heic 文件掉进下面 Office 那条 ``else`` 分支，
#: 用户收到「请上传 Word、Excel、PowerPoint 文档或 TXT 文本文件」，
#: 而它明明是一张能读的图片。第十阶段 A 的真机测试正是这样抓到它的。
_IMAGE_FORMAT_BY_SOURCE: dict[str, str] = {
    registry.SOURCE_JPG: "jpeg",
    registry.SOURCE_PNG: "png",
    registry.SOURCE_WEBP: "webp",
    registry.SOURCE_BMP: "bmp",
    registry.SOURCE_GIF: "gif",
    registry.SOURCE_TIFF: "tiff",
    registry.SOURCE_HEIC: "heif",
}

#: 内部格式名 -> 源类型（校验层返回的是内部名，要转回注册表的词汇）
_IMAGE_SOURCE_BY_FORMAT: dict[str, str] = {
    "jpeg": registry.SOURCE_JPG,
    "png": registry.SOURCE_PNG,
    "webp": registry.SOURCE_WEBP,
    "bmp": registry.SOURCE_BMP,
    "gif": registry.SOURCE_GIF,
    "tiff": registry.SOURCE_TIFF,
    "heif": registry.SOURCE_HEIC,
}


# ----------------------------------------------------------------------
# 类型识别
# ----------------------------------------------------------------------

def detect_source(path: Path, filename: str) -> DetectedSource:
    """确认一个上传文件的真实类型，返回它的身份。

    做法是**扩展名分派 + 内容复核**，而不是单纯嗅探魔数：先按扩展名决定
    它「声称」是哪一类，再交给那一类**已有的**校验函数（``validate_image_upload``
    / ``validate_pdf_upload`` / ``validate_office_upload``）去验明正身。

    这三个函数本来就做的是内容校验（魔数、OOXML 内部结构、OLE2 流名、
    Pillow 真解码），所以：
    - ``.exe`` 改名成 ``.jpg`` → 被魔数挡下；
    - 普通压缩包改名成 ``.docx`` → ``[Content_Types].xml`` 缺失，被挡下；
    - ``.xlsx`` 改名成 ``.docx`` → 内部声明的类型对不上，被挡下。

    这也意味着 §三十七 的路径穿越在这里没有立足点：文件名从头到尾
    只用来取扩展名与展示，真正的路径都是服务端生成的随机名。

    不支持的类型抛 :class:`UnsupportedTypeError`（415，换文件），
    认得出类型但组合不在矩阵里由 :func:`convert_file` 抛
    :class:`UnsupportedConversionError`（400，换目标格式）——
    两者让用户做的事完全不同，所以是两个码。
    """
    extension = registry.normalize_extension(filename)
    source_type = registry.source_for_extension(filename)

    if extension is None or source_type is None:
        raise UnsupportedTypeError(
            "暂不支持这种文件，可转换的格式："
            + "、".join(
                ext.lstrip(".").upper()
                for source in registry.SOURCE_TYPES
                for ext in registry.EXTENSIONS_BY_SOURCE[source]
            )
        )

    size = path.stat().st_size if path.exists() else 0
    if size <= 0:
        raise ValidationError("上传的文件为空")

    if source_type == registry.SOURCE_SVG:
        # SVG 在图片组里，但校验走的是**另一条**路：它没有魔数、Pillow 也
        # 解不开，``validate_image_upload`` 对它只会报「暂不支持该文件格式」。
        # 这一条必须在 ``_IMAGE_FORMAT_BY_SOURCE`` 那个分支旁边独立存在，
        # 而不是混进去 —— 两条路的判据没有任何重叠。
        width, height = validate_svg_upload(path, filename)
        return DetectedSource(
            source_type=source_type,
            extension=extension,
            size=size,
            # 声明里读不出尺寸时是 ``(0, 0)``：结果为 0 表示「未知」，
            # 由界面显示成「—」。这里编一个数字出来比留空更糟。
            width=width or None,
            height=height or None,
        )

    if source_type in _IMAGE_FORMAT_BY_SOURCE:
        width, height, fmt = validate_image_upload(path, filename)
        # 校验层可能以 Pillow 的判断为准改写格式，这里跟着它走
        actual = _IMAGE_SOURCE_BY_FORMAT.get(fmt)
        if actual is None:  # pragma: no cover - 校验层已保证在白名单内
            raise UnsupportedTypeError("暂不支持这种图片格式。")
        return DetectedSource(
            source_type=actual,
            extension=extension,
            size=size,
            width=width,
            height=height,
        )

    if source_type == registry.SOURCE_PDF:
        page_count = validate_pdf_upload(path, filename)
        return DetectedSource(
            source_type=source_type,
            extension=extension,
            size=size,
            page_count=page_count,
        )

    # HTML / Markdown：与 TXT 一样没有魔数，靠「能不能当文本读出来」验明正身。
    # 长度上限也在这里卡住 —— 用户提交的那一刻就知道文件太长，
    # 不必等它排到队里才失败。
    if source_type in registry.MARKUP_SOURCES:
        source_format, _text = validate_markup_upload(path, filename)
        # 校验层按扩展名算出的格式，要与注册表给出的源类型对得上：
        # 两边白名单漂移时（例如注册表认了 .markdown 而解析器不认），
        # 宁可在这里明确报错，也不要拿一个错的源类型往下走。
        if SOURCE_FORMAT_BY_TYPE[source_type] != source_format:  # pragma: no cover
            raise UnsupportedTypeError(UNSUPPORTED_MARKUP_MESSAGE)  # pragma: no cover
        return DetectedSource(
            source_type=source_type,
            extension=extension,
            size=size,
        )

    # Office / TXT：结构校验能分辨「普通压缩包改名」与「xlsx 改名成 docx」
    info = validate_office_upload(path, filename)
    return DetectedSource(
        source_type=_office_source_type(info),
        extension=extension,
        size=size,
    )


def _office_source_type(info: OfficeInput) -> str:
    """Office 文档的具体源类型。

    ``loader`` 只分到「word / excel / powerpoint / text」这一层，
    而注册表还要区分新旧格式（doc 与 docx、xls 与 xlsx）。
    文件内容本身分辨不出这一点 —— 新旧格式的差别不止在容器上，
    而 LibreOffice 也是**按扩展名**选导入过滤器的，所以这里按扩展名细分，
    与转换实际会走的那条路保持一致。
    """
    if info.extension in registry.EXTENSION_TO_SOURCE:
        return registry.EXTENSION_TO_SOURCE[info.extension]
    # loader 放行了、注册表却没有的扩展名：两边白名单漂移了。
    # 宁可在这里明确报错，也不要拿一个错的类型去查矩阵。  # pragma: no cover
    raise UnsupportedTypeError(UNSUPPORTED_OFFICE_MESSAGE)  # pragma: no cover


# ----------------------------------------------------------------------
# 适配器
# ----------------------------------------------------------------------

def as_upload_file(path: Path, filename: str) -> UploadFile:
    """把一个已经落盘的文件包成 :class:`UploadFile`。

    只为喂给 ``convert_document`` / ``convert_pdf_to_docx`` —— 它们收的是
    ``UploadFile`` 而不是 ``Path``。**这是适配，不是复制**：转换逻辑一行没搬。

    两者都会在结束时 ``await upload.close()`` 并删掉自己接收的那份副本，
    所以每次调用都要新开一个句柄、并且传进来的必须是**副本**而不是原件。
    """
    return UploadFile(file=path.open("rb"), filename=filename)


# ----------------------------------------------------------------------
# 转换分发
# ----------------------------------------------------------------------

#: 家族键 -> 叶子实现。**这就是全部的分发逻辑**（§四十四 配置驱动）。
#:
#: 一个键服务一整族条目：36 个「图片 → 另一种图片」的组合共用
#: ``image.convert`` 一格，这正是 §八「禁止为每个组合创建独立 converter」的落点。
#: 将来加一种格式 = 注册表加条目 + 这里不用动（同族）或加一格（新族），
#: **而不是**在 :func:`convert_file` 里再插一个 ``elif``。
#:
#: 键与 :mod:`conversion.options` 的家族常量是同一组字符串，
#: 条目上的 ``converter_key`` 就是从这里取的。一致性由
#: :func:`convert_file_plan` 与 ``test_converter_table_matches_the_registry`` 钉住。
#:
#: 表本身定义在四个叶子实现之后（见「家族分发表」一节）——
#: 它引用的是那些函数对象，写在前面就成了前向引用。


def convert_file_plan() -> tuple[str, ...]:
    """分发表与注册表之间的漂移清单；空元组表示两边严丝合缝。

    两个方向都要查：

    * 注册表登记了某个家族，而 :data:`CONVERTERS` 里没有实现 ——
      用户点下去会得到「转换器不可用」，而能力 API 却说它可用；
    * 反过来，实现写了却没有条目用它 —— 一段永远跑不到的死代码。

    返回字符串清单而不是抛异常：它是给测试与诊断用的，不是运行路径。
    """
    problems: list[str] = []
    used = {entry.converter_key for entry in registry.CONVERSIONS}

    for key in sorted(key for key in used if key):
        if key not in CONVERTERS:
            problems.append(f"注册表登记了家族 {key}，但 CONVERTERS 里没有对应实现")
    for key in sorted(CONVERTERS):
        if key not in used:
            problems.append(f"CONVERTERS 里的 {key} 没有任何条目在用")

    return tuple(problems)


async def convert_file(request: ConversionRequest) -> ConversionOutput:
    """把一份文件从 ``source_type`` 转成 ``target_type``。

    分发是**一次查表**：由「源 + 目标」找到注册表里那一条能力，
    再用它声明的 ``converter_key`` 找到实现。未登记的组合抛
    :class:`UnsupportedConversionError` —— **不是 500**：
    这是「这个组合我们不提供」，不是「服务器出错了」。
    """
    entry = registry.capability_for(request.source_type, request.target_type)
    if entry is None:
        raise UnsupportedConversionError(
            f"暂不支持「{registry.SOURCE_LABELS.get(request.source_type, request.source_type)}」"
            f"转换为「{registry.TARGET_LABELS.get(request.target_type, request.target_type)}」。"
            "请换一个目标格式，文件本身没有问题。"
        )

    leaf = CONVERTERS.get(entry.converter_key or "")
    if leaf is None:
        # 只有「注册表说能做、实现却没接上」才会走到这里 ——
        # 那是服务器少装/少写了东西，不是用户的输入有问题，
        # 所以是 503（换时间重试）而不是 400（换文件）。
        # convert_file_plan() 与它的测试就是为了让这里永远不成立。
        raise ConverterUnavailableError(  # pragma: no cover
            "这个转换暂时不可用，请稍后再试或换一个目标格式。"
        )

    request.out_dir.mkdir(parents=True, exist_ok=True)
    # 展示名统一在这里清洗：它会进 ZIP 的条目名，`../../evil.pdf`
    # 必须在这一步就变成 `evil`，不能等到打包时才想起来（§三十七）。
    stem = sanitize_stem(request.filename, fallback="converted")

    return await leaf(request, stem)


# ----------------------------------------------------------------------
# 四格实现
# ----------------------------------------------------------------------

async def _image_to_image(request: ConversionRequest, stem: str) -> ConversionOutput:
    """图片 → 另一种图片（``compressors.pipeline.run_pipeline``）。"""
    options = request.options
    fmt = normalize_format(request.target_type)
    if fmt is None:  # pragma: no cover - 注册表登记的目标都在 OUTPUT_FORMATS 里
        raise UnsupportedConversionError("暂不支持这个图片目标格式。")

    result = await run_in_pool(
        run_pipeline,
        request.source,
        PipelineOptions(
            target_format=fmt,
            quality_preset=options.quality_preset,
            quality_value=options.quality_value,
            target_bytes=options.target_bytes,
            crop=options.crop,
            resize=options.resize,
            rotation=options.rotation,
            rotation_expand=options.rotation_expand,
            flip=options.flip,
            dpi=options.dpi,
            metadata=options.metadata,
        ),
    )

    extension = _encoder_extension_for(result.format)
    path = request.out_dir / f"{new_token()}{extension}"
    path.write_bytes(result.data)

    filename = f"{stem}{extension}"
    # 走图片那套登记（与批量图片共用同一个 register_batch）：
    # 结果只有 1 个，它会直接给出文件而**不打包** —— 和专用页面的行为一致。
    job_id, url, _archive = batch_service.register_batch(
        request.out_dir,
        [ResultEntry(index=0, filename=filename, path=path, media_type=_media_type(result.format))],
        archive_stem=stem,
    )

    notes: list[str] = []
    if result.untouched:
        notes.append("已经是你选的格式，文件保持原样。")
    notes.extend(result.notes)

    # §三十一：把这次压缩的真实数字如实报出来，由前端并排显示。
    # ``target_reached`` 只在**用户确实要了目标大小**时才给结论 ——
    # 没要的时候报 None（见 ConversionOutput 的说明）。
    target_size = options.target_bytes

    return ConversionOutput(
        filename=filename,
        path=path,
        media_type=_media_type(result.format),
        size=len(result.data),
        job_id=job_id,
        download_url=url,
        width=result.width,
        height=result.height,
        notes=notes,
        original_size=_source_size(request.source),
        target_size=target_size,
        quality_used=result.quality_used,
        target_reached=result.target_met if target_size is not None else None,
    )


async def _image_to_pdf(request: ConversionRequest, stem: str) -> ConversionOutput:
    """图片 → PDF，**每张各出一份**（``pdf.image_to_pdf.build_pdf_from_images``）。

    不做「多张合成一份」：统一转换中心是按文件逐项处理的，
    想合并多张图应该去专门的「图片转 PDF」页面，那里能排序、能选版式。
    """
    # 尺寸要重新读一次：转换发生在提交之后的 worker 里，
    # 提交时读到的尺寸没有地方可以安全地留到这一刻（见模块开头的说明）。
    width, height, _fmt = validate_image_upload(request.source, request.filename)
    source = ImageSource(
        filename=request.filename,
        data=request.source.read_bytes(),
        width=width,
        height=height,
    )

    # 排版是纯计算，放线程池里跑，与专用页面走的是同一条路
    built = await run_in_pool(
        build_pdf_from_images, [source], request.options.layout
    )

    path = request.out_dir / f"{new_token()}.pdf"
    path.write_bytes(built.data)

    filename = f"{stem}.pdf"
    job_id, url, _archive = batch_service.register_batch(
        request.out_dir,
        [ResultEntry(index=0, filename=filename, path=path, media_type=registry.MEDIA_TYPE_BY_TARGET[registry.TARGET_PDF])],
        archive_stem=stem,
    )

    return ConversionOutput(
        filename=filename,
        path=path,
        media_type=registry.MEDIA_TYPE_BY_TARGET[registry.TARGET_PDF],
        size=len(built.data),
        job_id=job_id,
        download_url=url,
        width=width,
        height=height,
        notes=list(built.notes),
    )


async def _svg_to_pdf(request: ConversionRequest, stem: str) -> ConversionOutput:
    """SVG → PDF，**保留矢量与真实文字**（``compressors.svg.svg_to_pdf``）。

    与 :func:`_image_to_pdf` 是两条不同的路，不是同一件事的两种写法：
    那条路先把位图栅格化再贴进 PDF 页面，这条路用 PyMuPDF 的
    ``convert_to_pdf`` 导出**真正的矢量页** —— 图元还是图元、文字还能
    被选中和搜索（实测 ``get_text()`` 抽得出来）。用户把一张 SVG 转 PDF，
    要的正是后者；给它一张糊掉的位图，能力 API 上却写着同一个「PDF」，
    那就是在瞒着他降级。

    所以 ``options.layout`` 在这里**一个字段都不用**：页面尺寸就是 SVG
    自己声明的那个尺寸，不需要（也不该）由用户再选一次 A4 或页边距。
    这条能力在注册表里因此挂着空选项集，见 ``options.SVG_TO_PDF``。

    净化在 ``svg.svg_to_pdf`` 之前必须跑：渲染器**绝不能**拿到未净化的
    字节。上传校验时已经净化过一遍，但那一遍的产物没有被保存下来
    （任务队列里流转的是源文件路径），而**这一遍是不能省的那一遍**。
    代价是一次重复的 XML 解析，换来的是「渲染器只见过干净字节」这条
    不依赖调用顺序的性质。
    """
    # 延迟导入的理由同 ``compressors.loader.decode_source``：让纯位图的路径
    # 不在 import 期背上 MuPDF。
    from compressors import svg as svg_codec

    data = svg_codec.read_svg(request.source)
    clean, notes = svg_codec.sanitize_svg(data)

    # 矢量导出是纯 CPU 计算，放线程池里跑，与其它转换走同一条规矩
    pdf_bytes = await run_in_pool(svg_codec.svg_to_pdf, clean)

    path = request.out_dir / f"{new_token()}.pdf"
    path.write_bytes(pdf_bytes)

    filename = f"{stem}.pdf"
    media_type = registry.MEDIA_TYPE_BY_TARGET[registry.TARGET_PDF]
    job_id, url, _archive = batch_service.register_batch(
        request.out_dir,
        [ResultEntry(index=0, filename=filename, path=path, media_type=media_type)],
        archive_stem=stem,
    )

    return ConversionOutput(
        filename=filename,
        path=path,
        media_type=media_type,
        size=len(pdf_bytes),
        job_id=job_id,
        download_url=url,
        # SVG 导出后就是一页 —— 这是 ``convert_to_pdf`` 的实际行为
        # （实测 ``page_count == 1``），不是猜的。
        page_count=1,
        notes=notes,
    )


async def _office_to_pdf(request: ConversionRequest, stem: str) -> ConversionOutput:
    """Office / TXT → PDF（``services.doc_service.convert_document``）。

    ``convert_document`` 会删掉传给它的源文件，所以先把它要的那份副本
    拷进本次的输出目录 —— ``in/`` 里的原件必须留着，重试还要用。
    """
    extension = Path(request.filename).suffix.lower()
    working = request.out_dir / f"{new_token()}{extension}"
    try:
        shutil.copyfile(request.source, working)
    except OSError as exc:  # pragma: no cover - 磁盘异常
        raise ProcessingError("读取文件失败，请重新上传") from exc

    try:
        info = validate_office_upload(working, request.filename)
    except Exception:
        working.unlink(missing_ok=True)
        raise

    if info.kind != KIND_TEXT and not office_available():
        # 与 receive_office 同一道闸门、同一句提示：文件没问题，
        # 是服务器缺组件，用户重传多少次都一样。
        working.unlink(missing_ok=True)
        raise ConverterUnavailableError(MISSING_COMPONENT_MESSAGE)

    options = request.options
    result = await convert_document(
        info,
        working,
        request.out_dir,
        # TXT 不看目标大小：纯文本排出来的 PDF 谈体积没有意义，
        # 与专用页面的取舍保持一致（doc_service 内部也是这么分的）。
        target_bytes=None if info.kind == KIND_TEXT else options.target_bytes,
        txt_options=options.txt_options,
    )

    job = result.job
    return ConversionOutput(
        filename=job.filename,
        path=job.path,
        media_type=job.media_type,
        size=job.size,
        job_id=job.job_id,
        download_url=f"/api/download/{job.job_id}",
        page_count=result.page_count,
        notes=list(result.notes),
    )


async def _pdf_to_word(request: ConversionRequest, stem: str) -> ConversionOutput:
    """PDF → Word（``services.pdf_to_docx.convert_pdf_to_docx``）。

    ``progress_id`` 透传进去，OCR 期间能读到**真实的页号与总页数**（§十二）；
    不传的话这一项就没有百分比可显示，界面上只报阶段，不编数字。
    """
    result = await pdf_to_docx.convert_pdf_to_docx(
        as_upload_file(request.source, request.filename),
        request.out_dir,
        progress_id=request.progress_id,
    )

    job = result.job
    return ConversionOutput(
        filename=job.filename,
        path=job.path,
        media_type=job.media_type,
        size=job.size,
        job_id=job.job_id,
        download_url=f"/api/download/{job.job_id}",
        page_count=result.page_count,
        notes=list(result.notes),
    )


# ----------------------------------------------------------------------
# 家族分发表
# ----------------------------------------------------------------------

#: 一次转换的叶子实现：``(请求, 已清洗的文件名主干) -> 结果``。
#:
#: 叶子**不收** ``Capability`` 条目：今天四个实现要用的一切都在
#: ``ConversionRequest`` 与 ``ConversionOptions`` 里，多传一个参数只会
#: 变成一个没人读的形参。将来哪一个家族真的需要条目上的信息
#: （例如按 ``entry.target_type`` 选编码器）时再加 —— 那时它是**被用到的**，
#: 而不是今天的占位。
ConverterLeaf = Callable[[ConversionRequest, str], Awaitable[ConversionOutput]]

CONVERTERS: dict[str, ConverterLeaf] = {
    # 36 格「图片 → 另一种图片」共用一格（§八）
    option_vocab.IMAGE_CONVERT: _image_to_image,
    option_vocab.IMAGE_TO_PDF: _image_to_pdf,
    # SVG → PNG/JPG/WEBP **共用上面那格 ``image.convert``**：渲染出来的
    # 就是一张普通的 PIL 图，后面每一步都一样（见 ``_converter_key``）。
    # 只有 SVG → PDF 另起一格 —— 它导出的是矢量页，那条路与「把位图贴进
    # PDF」在实现和目标上都不是一回事。
    option_vocab.SVG_TO_PDF: _svg_to_pdf,
    # Office 与 **TXT→PDF** 走同一条 ``doc_service.convert_document`` 链路
    # （它内部按 ``info.kind`` 分流），所以两格指向同一个实现。
    # TXT→PDF 从第七阶段起就在这条路上，行为被既有测试与专用页面钉着，
    # 第九阶段不搬它 —— 只把 TXT 的另外三个目标接上新叶子。
    option_vocab.OFFICE_TO_PDF: _office_to_pdf,
    option_vocab.TEXT_TO_PDF: _office_to_pdf,
    # TXT 的另外三个目标：不碰 LibreOffice，直接写 DOCX / 渲染成文本标记。
    option_vocab.TEXT_TO_DOCX: text_to_docx,
    option_vocab.TEXT_CONVERT: text_convert,
    option_vocab.PDF_TO_DOCX: _pdf_to_word,
    # HTML / Markdown 家族走自己的两个叶子：解析与渲染都在
    # ``office.markup_*`` 里，与 Office 那条 LibreOffice 链路毫无关系。
    option_vocab.MARKUP_TO_PDF: markup_to_pdf,
    option_vocab.MARKUP_CONVERT: markup_convert,
}


def _source_size(path: Path) -> int | None:
    """源文件的字节数，读不到就 ``None``（§三十一）。

    读不到不是错误：这是一条**展示用**的数字，压缩本身已经成功了。
    为了一句「原来多大」把一次成功的转换判成失败，本末倒置。
    """
    try:
        return path.stat().st_size
    except OSError:  # pragma: no cover - 转换刚读过这个文件
        return None


def _media_type(fmt: str) -> str:
    """内部图片格式名 -> 下载 MIME。

    直接用 ``intake.media_type_for`` —— 图片结果的 MIME 只有这一个出处，
    在这里另写一份映射迟早会和它漂移。
    """
    return media_type_for(fmt)


# ----------------------------------------------------------------------
# 能力矩阵
# ----------------------------------------------------------------------

#: ``category`` / ``operation_type`` 是服务端自己的闭集词汇，取值写错属于
#: 客户端 bug，明确报错比默默返回空数组好排查。
#: ``source_type`` / ``target_type`` 是**数据驱动**的（格式会变多），
#: 所以未知值只是「没有匹配」，返回空结果而不是 400。
_FILTER_ENUMS: dict[str, tuple[str, ...]] = {
    "category": capability.CATEGORIES,
    "operation_type": capability.OPERATION_TYPES,
}


def _matches(
    entry: capability.Capability,
    *,
    source_type: str | None,
    target_type: str | None,
    category: str | None,
    operation_type: str | None,
) -> bool:
    if source_type and entry.source_type != source_type:
        return False
    if target_type and entry.target_type != target_type:
        return False
    if category and entry.category != category:
        return False
    if operation_type and entry.operation_type != operation_type:
        return False
    return True


def _font_choices() -> tuple[tuple[str, str], ...]:
    """服务器上真的装了的排版字体，``(键, 显示名)``。

    **路径一个字节都不出网**：``TxtFont`` 带着 ``path``，界面要的只是
    「有哪几个能选」与「叫什么」，给路径既没用又是内部信息（§三十四）。
    """
    return tuple((font.key, font.label) for font in available_fonts())


#: ``dynamic`` 标记 -> 现算值域的函数。加一个运行期枚举就在这里加一行，
#: 纯层（``conversion/options.py``）仍然不装作知道服务器上有什么。
_DYNAMIC_CHOICES: dict[str, Callable[[], tuple[tuple[str, str], ...]]] = {
    capability.DYNAMIC_FONTS: _font_choices,
}


def _resolve_dynamic_options(item: dict) -> dict:
    """把 schema 里运行期才知道值域的枚举填成真实列表。

    ``OptionSpec`` 上的 ``font`` 是个**空壳**（``enum`` 缺失、
    ``dynamic="fonts"``）：探测要扫字体目录，是 I/O，不能放在注册表那层
    （§十三 要求 registry 零 I/O）。所以由这里 —— services 层 —— 在
    序列化时补上，这正是 ``Capability.option_schema`` 的 docstring 承诺的事。

    填的顺序也有讲究：**第一项进 ``default``**，并与 ``default_label`` 一起
    给出，前端因此不必自己猜「没选时用的是哪一个」。
    """
    schema = item.get("options_schema")
    if not schema:
        return item

    for spec in schema.get("items", ()):
        source = spec.get("dynamic")
        if not source:
            continue
        resolver = _DYNAMIC_CHOICES.get(source)
        choices = resolver() if resolver is not None else ()
        if not choices:
            # 一项都探测不到：**不留一个空壳枚举**。界面看到 ``dynamic``
            # 却看不到 ``enum``，会如实说「暂时读不到可选值」，
            # 而不是渲染一个空下拉框让用户以为网站坏了。
            continue
        spec["enum"] = [{"value": value, "label": label} for value, label in choices]
        spec["default"] = choices[0][0]
        spec["default_label"] = choices[0][1]
    return item


def _format_catalog(*, source_type: str | None, target_type: str | None) -> list[dict]:
    """``formats[]``：系统认识的格式清单。

    界面靠它拿显示名、扩展名与 MIME，**不需要在前端再抄一份格式表**
    （§十六 / §四十二：能力矩阵禁止硬编码在前端）。
    """
    used_as_source = {
        entry.source_type for entry in registry.CONVERSIONS
    }
    used_as_target = {entry.target_type for entry in registry.CONVERSIONS}

    items: list[dict] = []
    for value in registry.FORMAT_TYPES:
        if source_type and value != source_type:
            continue
        if target_type and value != target_type:
            continue
        category = registry.format_category(value)
        if category is None:  # pragma: no cover - FORMAT_TYPES 都来自条目表
            continue
        items.append(
            {
                "value": value,
                "label": registry.format_label(value),
                "category": category,
                "extensions": list(registry.EXTENSIONS_BY_SOURCE.get(value, ())),
                "media_type": registry.media_type_for_target(value),
                "is_source": value in used_as_source,
                "is_target": value in used_as_target,
            }
        )
    return items


def conversion_capabilities(
    *,
    source_type: str | None = None,
    target_type: str | None = None,
    category: str | None = None,
    operation_type: str | None = None,
) -> dict:
    """服务器**当前真的**能做的转换（§七），以及可查询的能力目录（§十五/§十六）。

    三个可用性各查各的、互不牵连：

    * ``office_ok`` 只看 LibreOffice —— 关掉它，Office 六类消失，
      **TXT 仍在**（TXT 走 PyMuPDF 排版，压根不碰 soffice）；
    * ``pdf_to_word_ok`` 只看 python-docx，与 LibreOffice 无关；
    * ``ocr_ok`` **不参与能力裁剪** —— OCR 不可用时 ``pdf → docx`` 照样保留，
      只是扫描件会转成空白，这件事用一条提示说明就够了。
      把整条能力藏掉是过度降级：带文字层的 PDF 本来就能转。

    ## 过滤参数（§十六）

    ``?source_type=&target_type=&category=&operation_type=`` 一旦给了值，
    **整个响应一起收窄**（``matrix`` / ``groups`` / ``targets`` 也一样），
    这样 ``?target_type=png`` 拿到的是「能转成 PNG 的一切」这一份自洽的视图，
    而不是一个宽矩阵配一个窄清单。不传参数时，几个旧键与第七阶段逐字节相同。

    ## ``matrix`` 与 ``conversions`` 的一处**有意**差异

    ``matrix`` 只列**这台服务器现在真的能做**的格子（第七阶段以来就是如此，
    前端的分组卡片靠它）。``conversions`` 则把**全部**已登记的格子都列出来，
    用 ``available`` 字段如实标注每一格现在能不能用（§十四 要求条目带
    ``available``）—— 一个永远为 ``true`` 的字段是没有意义的。
    没有 LibreOffice 时，前者的 Office 行整体消失，后者仍会给出
    ``document.docx-to-pdf`` 且 ``available: false``，理由在 ``notes`` 里。
    """
    for name, value in (("category", category), ("operation_type", operation_type)):
        if value is not None and value not in _FILTER_ENUMS[name]:
            allowed = "、".join(_FILTER_ENUMS[name])
            raise ValidationError(f"{name} 参数无效，可选值：{allowed}")

    office_ok = office_available()
    pdf_to_word_ok = docx_available()
    ocr_ok = ocr_available()
    # HEIC 的两个方向**分开探测**（§六）：只带解码器的构建要如实表现为
    # 「能读进来、不能写出去」，而不是一刀切成「HEIC 不可用」。
    heif = heif_support()

    available = registry.available_matrix(
        office_ok=office_ok,
        pdf_to_word_ok=pdf_to_word_ok,
        heif_decode_ok=heif.decode,
        heif_encode_ok=heif.encode,
    )

    def is_available(entry: capability.Capability) -> bool:
        return registry.capability_snapshot(
            entry,
            office_ok=office_ok,
            pdf_to_word_ok=pdf_to_word_ok,
            heif_decode_ok=heif.decode,
            heif_encode_ok=heif.encode,
        )

    filters = {
        "source_type": source_type,
        "target_type": target_type,
        "category": category,
        "operation_type": operation_type,
    }

    # 可用性先过一遍：不可用的格子不进矩阵（与第七阶段一致）
    narrowed = {
        source: tuple(
            target
            for target in targets
            if (source_type is None or source == source_type)
            and (target_type is None or target == target_type)
            and (
                category is None
                or registry.format_category(source) == category
            )
        )
        for source, targets in available.items()
    }
    narrowed = {source: targets for source, targets in narrowed.items() if targets}
    if operation_type == capability.OPERATION_OPERATION:
        # 只看工具时，转换矩阵整个空掉 —— 那正是「这一问没有匹配」的诚实答案
        narrowed = {}

    # 按组整理，前端照着渲染分组卡片，不需要自己维护一份源类型清单
    groups: dict[str, dict] = {}
    for source, targets in narrowed.items():
        group = registry.SOURCE_GROUPS[source]
        entry = groups.setdefault(
            group,
            {
                "key": group,
                "label": registry.GROUP_LABELS[group],
                "sources": [],
                "targets": [],   # 这一组里出现过的全部目标（去重、有序）
            },
        )
        entry["sources"].append(
            {
                "value": source,
                "label": registry.SOURCE_LABELS[source],
                "extensions": list(registry.EXTENSIONS_BY_SOURCE[source]),
                "targets": list(targets),
            }
        )
        for target in targets:
            if target not in entry["targets"]:
                entry["targets"].append(target)

    return {
        # ---- 第七阶段的旧键：名字与语义一字未改 ----
        "matrix": {source: list(targets) for source, targets in narrowed.items()},
        "groups": list(groups.values()),
        "targets": [
            {
                "value": target,
                "label": registry.TARGET_LABELS[target],
                "extension": registry.EXTENSION_BY_TARGET[target],
            }
            for target in registry.TARGET_TYPES
            if any(target in targets for targets in narrowed.values())
        ],
        "office_available": office_ok,
        "pdf_to_word_available": pdf_to_word_ok,
        "ocr_available": ocr_ok,
        # 缺组件时**必须说明**：能力被悄悄藏起来，用户只会以为网站坏了
        "notes": registry.unavailable_reasons(
            office_ok=office_ok,
            pdf_to_word_ok=pdf_to_word_ok,
            heif_decode_ok=heif.decode,
            heif_encode_ok=heif.encode,
        ),
        "pdf_to_word_note": registry.ocr_note(
            ocr_ok=ocr_ok, max_pages=settings.PDF_TO_WORD_MAX_OCR_PAGES
        ),
        # ---- 第九阶段新增 ----
        "categories": [
            {"value": value, "label": capability.CATEGORY_LABELS[value]}
            for value in capability.CATEGORIES
        ],
        "formats": _format_catalog(source_type=source_type, target_type=target_type),
        "conversions": [
            _resolve_dynamic_options(entry.to_json(available=is_available(entry)))
            for entry in registry.CONVERSIONS
            if _matches(entry, **filters)
        ],
        "operations": [
            _resolve_dynamic_options(entry.to_json(available=is_available(entry)))
            for entry in registry.OPERATIONS
            if _matches(entry, **filters)
        ],
    }
