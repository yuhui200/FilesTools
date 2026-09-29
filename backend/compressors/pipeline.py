"""图片处理主流程。

第一阶段的「压缩」、第二阶段的「格式转换」和「尺寸调整」，本质上都是同一条流水线：

    解码 → 旋转 → 改尺寸 → 改色彩模式 → 编码（必要时按目标大小优化）→ 兜底

三个功能只是给这条流水线传不同的参数，因此共用同一个实现，
避免同一套「按目标大小搜索质量」的逻辑被复制三遍。

## 第九阶段新增的三个参数默认「什么都不做」

``rotation`` / ``dpi`` / ``metadata`` 的默认值一律是**保持第七阶段的行为**：

* ``rotation=0``（不旋转）
* ``dpi=None``（不写入密度信息 —— 与第一阶段以来一致）
* ``metadata="remove"``（不带元数据 —— 与第一阶段以来一致，见 ``loader`` 的说明）

于是压缩 / 格式转换 / 尺寸调整三个页面的调用方一行都不用改，结果逐字节不变；
只有统一转换中心会显式传入这三个值（§九 的选项面板）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image

from compressors.encoder import (
    LOSSLESS_PNG_QUALITY,
    OUTPUT_FORMATS,
    TARGET_UNREACHABLE_NOTE,
    build_extra,
    encode_image,
    encode_under_target,
    read_exif,
    read_xmp,
    supports_density,
    supports_exif,
    supports_xmp,
)
from compressors.cropper import CropRequest, compute_crop_box
from compressors.loader import (
    crop_image,
    decode_source,
    flip_image,
    resize_exact,
    rotate_arbitrary,
    source_dimensions,
    source_frame_count,
)
from compressors.resizer import (
    ResizeRequest,
    centre_crop_box,
    compute_target_size,
)
from config import settings
from conversion.options import DPI_ORIGINAL, METADATA_KEEP, METADATA_REMOVE
from converters.image_converter import is_same_format, prepare_for_format, same_format_note
from utils.errors import ProcessingError, ValidationError

# 按目标大小搜索质量时的下限。低于这个值画质损失过大，宁可缩尺寸。
MIN_SEARCH_QUALITY = 25

#: ``fill`` 是「缩放到盖住整个框，再居中裁剪」的两步操作，见 ``run_pipeline``。
FILL = "fill"

# 重新编码后体积没有变小（甚至变大）时的说明
NO_GAIN_NOTE = "重新压缩未能减小体积，已保留原文件。"
# 原文件本来就达标时的说明
ALREADY_SMALL_NOTE = "原文件已小于目标大小，未做任何压缩，直接返回原文件。"

#: 目标格式装不下元数据 / 密度时的如实说明（§九：不允许伪造）。
EXIF_DROPPED_NOTE = "目标格式不支持携带元数据，拍摄信息未能保留。"
DENSITY_DROPPED_NOTE = "目标格式不支持写入分辨率，该项未生效。"
#: XMP 没能保留时的如实说明（第十阶段 C §四）。
#:
#: 与 EXIF 那条**分开写**：两者是同一份文件里两套独立的元数据，用户可能
#: 只关心其中一套（XMP 里常有编辑历史与版权信息，EXIF 里是拍摄参数）。
#: 合成一句「元数据没保住」会让人以为两样都丢了，而实际情况常常是
#: 「EXIF 保住了、XMP 没保住」（GIF 目标），或者反过来。
XMP_DROPPED_NOTE = "目标格式不支持携带 XMP 元数据，该项未能保留。"

#: 多帧 GIF / 多页 TIFF 的如实说明。
#:
#: 第九阶段只处理第一帧（流水线拿到的就是第 0 帧，后面的帧不解码、不输出）。
#: 这件事**必须说**：用户看到「转换成功」，拿到的却只是第一帧，
#: 而文件里明明还有几十帧 —— 不说就是伪造了一个完整的转换结果。
#: 措辞按容器分开：GIF 说「帧」、TIFF 说「页」，这是用户在自己文件里看到的说法。
MULTIFRAME_NOTE = "该 GIF 是动图，共 {count} 帧，本次只转换了第一帧。"
MULTIPAGE_TIFF_NOTE = "该 TIFF 含 {count} 页，本次只转换了第一页。"


def frame_note(source_format: str, frames: int) -> str:
    """多帧源文件的说明文案。"""
    if source_format == "tiff":
        return MULTIPAGE_TIFF_NOTE.format(count=frames)
    return MULTIFRAME_NOTE.format(count=frames)


@dataclass(slots=True)
class PipelineOptions:
    """一次图片处理的全部参数。

    - ``target_format``：输出格式，None 表示保持原格式
    - ``quality_preset``：质量档位（压缩功能使用）
    - ``quality_value``：显式质量 1-100（格式转换 / 尺寸调整使用）
    - ``target_bytes``：目标大小上限，None 表示不限制
    - ``crop``：裁剪参数，None 表示不裁剪
    - ``resize``：尺寸调整参数，None 表示保持原尺寸
    - ``rotation``：顺时针旋转角度。0 / 90 / 180 / 270 走无损重排，
      其余角度按第十阶段 A §二十三 重采样
    - ``rotation_expand``：旋转后画布是否放大到装下整张图（仅非 90 倍数时有意义）
    - ``flip``：翻转方向，见 ``loader.FLIP_MODES``
    - ``dpi``：``None`` 不写入；``"original"`` 沿用源文件的密度；正整数写入它
    - ``metadata``：``"keep"`` 带上源文件的 EXIF，``"remove"`` 不带
    """

    target_format: str | None = None
    quality_preset: str | None = None
    quality_value: int | None = None
    target_bytes: int | None = None
    crop: CropRequest | None = None
    resize: ResizeRequest | None = None
    rotation: float = 0
    rotation_expand: bool = True
    flip: str = "none"
    dpi: int | str | None = None
    #: ``"keep"`` 带上源文件的 EXIF，``"remove"`` 不带。
    #:
    #: **默认 ``remove`` 是给老页面留的**（压缩 / 尺寸调整 / 旧转换页）：
    #: 那三个页面不给用户元数据选项，第一阶段起就靠这个默认丢掉拍摄信息
    #: （含 GPS），既减小体积也不把用户的位置带进结果。这个默认**不要动**。
    #:
    #: 统一转换中心**必须显式传** ``metadata=``：它把选择权交给了用户，
    #: 界面上「元数据」那一项默认选的是「保留」（``conversion.options``
    #: 的 ``METADATA_DEFAULT``）。哪条新路径忘了传，用户选了「保留」却
    #: 拿到一张没有 EXIF 的图 —— 第十阶段 C §三 把这个坑显式记在这里。
    metadata: str = METADATA_REMOVE


@dataclass(slots=True)
class PipelineResult:
    """处理结果。字段与第一阶段的 CompressResult 保持一致。"""

    data: bytes
    width: int
    height: int
    format: str            # jpeg / png / webp
    quality_used: int | None
    scale: float           # 相对原图的缩放比例（含尺寸调整与自动缩放）
    target_bytes: int | None
    target_met: bool       # 是否满足目标大小（未指定目标时为 True）
    untouched: bool        # 是否原样返回了原文件（未重新编码）
    #: 全部说明。可能同时有两条（例如既重压了又丢掉了元数据），
    #: 所以是列表而不是一句话。
    notes: list[str] = field(default_factory=list)

    @property
    def note(self) -> str | None:
        """合并成一句话给只需要一段文字的调用方（压缩 / 尺寸调整页面）。"""
        return " ".join(self.notes) or None


def run_pipeline(source: Path, options: PipelineOptions) -> PipelineResult:
    """按参数处理图片，返回编码后的字节与元信息。"""
    try:
        original_size = source.stat().st_size
    except OSError as exc:
        raise ValidationError("读取上传文件失败") from exc

    if options.target_bytes is not None and options.target_bytes <= 0:
        raise ValidationError("目标大小必须大于 0")

    img, source_format, decode_notes = decode_source(source)
    out_format = _resolve_format(options.target_format, source_format, source)
    format_changed = not is_same_format(source_format, out_format)
    # 解码时的说明排在**最前面**：它是关于「我们把你的文件读成了什么」的，
    # 后面那些是关于「我们拿它做了什么」的，顺序反过来读着别扭。
    notes: list[str] = list(decode_notes)

    # 元数据在**任何变换之前**取好：``load_image`` 已经按 EXIF 方向把像素摆正
    # 并摘掉方向标记（不会与下面的旋转叠加），剩下的拍摄信息在这里一次性取出，
    # 之后无论怎么缩放都不受影响。
    exif, dpi, xmp = _resolve_media_info(img, options, out_format, notes)

    # 变换顺序是**固定**的（第十阶段 A §二十二）：
    #     decode → crop → resize → rotate → flip → optimize → encode
    #
    # 顺序不是随手排的，每一处相邻关系都有理由：
    # * crop 在 resize 前 —— 先切掉不要的部分再缩放，缩放只花在真正要留的
    #   像素上；反过来则要先缩放整张图，白算一遍，还会让裁剪坐标变成
    #   「缩放后的坐标」，与用户照原图量的数字对不上；
    # * resize 在 rotate 前 —— 旋转非 90 倍数会放大画布，先旋转会让
    #   「长边 1920」变成缩放一张带透明角的更大的图，最终边长不是 1920；
    # * rotate 在 flip 前 —— 反过来做，水平翻转会变成垂直翻转。
    #
    # 这条顺序由 test_image_geometry.py 逐条钉住：谁把它改回去，
    # 对应的像素断言就会红。
    #
    # 裁剪：坐标按**用户手上那份原图**量。``load_image`` 可能已经把超大图
    # 缩到 MAX_IMAGE_EDGE，那样坐标空间就变了 —— 这种时候必须说，
    # 否则用户只会看到一句「超出图片范围」，却不知道范围为什么变小了。
    if options.crop is not None:
        declared = source_dimensions(source)
        if declared is not None and declared != (img.width, img.height):
            notes.append(
                f"原图 {declared[0]}×{declared[1]} 像素超过服务器单边上限"
                f"（{settings.MAX_IMAGE_EDGE} 像素），已先等比缩小到 "
                f"{img.width}×{img.height} 像素再裁剪，裁剪坐标按缩小后的尺寸计算。"
            )
        box = compute_crop_box(img.width, img.height, options.crop)
        if box.note:
            notes.append(box.note)
        img = crop_image(img, (box.x, box.y, box.width, box.height))

    # 尺寸调整：只做一次重采样，后续的质量搜索在此基础上进行
    scale = 1.0
    if options.resize is not None:
        target_width, target_height = compute_target_size(img.width, img.height, options.resize)
        if (target_width, target_height) != img.size:
            scale = target_width / img.width
            img = resize_exact(img, target_width, target_height)
        # ``fill`` 是两步：先缩到「盖住」整个框，再居中切掉溢出的部分。
        # 单独一步是必须的 —— 缩放保比例、裁剪定尺寸，合成一步就成了拉伸。
        if options.resize.fit == FILL and options.resize.width and options.resize.height:
            box = centre_crop_box(img.size, (options.resize.width, options.resize.height))
            if box != (0, 0, img.width, img.height):
                img = crop_image(img, box)

    # 旋转：90 的整数倍走无损的 transpose，其余角度才重采样
    rotated = options.rotation % 360
    if rotated:
        img = rotate_arbitrary(img, rotated, expand=options.rotation_expand)

    if options.flip not in ("", "none"):
        img = flip_image(img, options.flip)

    # 色彩模式必须与输出格式匹配，否则 Pillow 会保存失败或产生错误结果
    img = prepare_for_format(img, out_format)

    # 用户什么都没要求：换格式、改尺寸、裁剪、改体积、改质量、旋转、翻转、
    # 写元数据、改密度 —— 一样都没有：这是一次没有意义的「转换」。
    # 直接返回原文件字节，不做重新编码 —— 对已经是目标格式的图片重新编码一遍，
    # 用户什么也得不到，画质和体积却可能变差。批量处理中混入的这类文件也走这条路径。
    if (
        _passthrough_ok(options, format_changed)
        and options.target_bytes is None
        and options.quality_value is None
        and options.quality_preset is None
    ):
        return PipelineResult(
            data=source.read_bytes(),
            width=img.width,
            height=img.height,
            format=out_format,
            quality_used=None,
            scale=1.0,
            target_bytes=None,
            target_met=True,
            untouched=True,
            notes=[same_format_note(source_format, out_format)],
        )

    # 用户没有要求改变格式与尺寸，而原文件本来就达标：原样返回，不做有损处理。
    # 同上：改过像素或要求写元数据的时候，这条近路也不成立。
    if (
        _passthrough_ok(options, format_changed)
        and options.target_bytes is not None
        and original_size <= options.target_bytes
    ):
        return PipelineResult(
            data=source.read_bytes(),
            width=img.width,
            height=img.height,
            format=out_format,
            quality_used=None,
            scale=1.0,
            target_bytes=options.target_bytes,
            target_met=True,
            untouched=True,
            notes=[ALREADY_SMALL_NOTE],
        )

    # 到这里才是真的重新编码。上面那三条近路都是**原样返回原文件**字节，
    # 多帧文件的所有帧一个不少，所以「只转换了第一帧」这句话只能挂在这里 ——
    # 提前挂上去会让「什么都没做、原文件退回」的情况也收到一句假提醒。
    frames = source_frame_count(source)
    if frames > 1:
        notes.append(frame_note(source_format, frames))

    result = _encode(img, out_format, options, scale, exif=exif, dpi=dpi, xmp=xmp)
    result.notes = notes + result.notes

    # 兜底：用户只要求压缩（没改格式、没改尺寸）时，重编码反而变大就保留原文件。
    # 格式转换和尺寸调整不能走这条兜底 —— 那会把用户明确要求的结果吞掉。
    # 旋转、翻转、裁剪、写入元数据或密度同样不能：用户要的是那张改过的图，
    # 「原文件更小」不构成把改动丢掉的理由。
    if (
        _passthrough_ok(options, format_changed)
        and len(result.data) >= original_size
    ):
        result.data = source.read_bytes()
        result.width = img.width
        result.height = img.height
        result.quality_used = None
        result.scale = 1.0
        result.untouched = True
        result.notes = [NO_GAIN_NOTE]

    return result


def _passthrough_ok(options: PipelineOptions, format_changed: bool) -> bool:
    """现在能不能走「原样返回原文件」的近路（§二十五 / §二十六）。

    「可以」的条件是**像素、格式与元数据都没被动过**：没换格式、没改尺寸、
    没裁剪、没旋转、没翻转、没改密度、没要求保留元数据。

    第九阶段这项判断在三条近路里各写了一遍（当时只有旋转/密度/元数据
    三项）。第十阶段 A 又加了裁剪与翻转 —— 再摊下去就是三份各写十行、
    **迟早漏掉一行**的同一份条件。漏掉的后果是固定的：用户勾了那个新选项，
    界面显示转换成功，图却一动不动。所以收成一份真相，三条近路都调它。

    质量与目标体积**有意不在这里**：三条近路对它们的判断各不相同
    （第一条要求「都没给」，第二条要求「给了目标且原文件已达标」，
    第三条压根不看）。把它们并进来会**改变既有行为** —— 例如
    「目标 1MB + 质量 50，原文件 800KB」，今天返回原文件并说明已达标，
    并进来之后会变成重新压一遍。所以它们留在各自的调用点。
    """
    return (
        not format_changed
        and options.resize is None
        and options.crop is None
        and not options.rotation % 360
        and options.flip in ("", "none")
        and options.dpi is None
        and options.metadata != METADATA_KEEP
    )


def _resolve_media_info(
    img: Image.Image,
    options: PipelineOptions,
    out_format: str,
    notes: list[str],
) -> tuple[bytes | None, tuple[int, int] | None, bytes | None]:
    """算出这次编码要带上的 EXIF / DPI / XMP，并把「带不上」如实记进 ``notes``。

    ``decode_source`` 已经摘掉了方向标记，所以这里原样透传的 EXIF 是安全的：
    查看器不会再自作主张地转一次。SVG 那条路渲染出来的图没有 EXIF，
    于是 ``metadata="keep"`` 在这里什么也不做 —— 那不是「丢掉了用户的设置」，
    而是本来就没有可保留的东西，所以也**不记 note**（记了反而是假话）。

    XMP 走同一套规矩（第十阶段 C §四）：源文件里没有就什么都不做、不记说明；
    有而目标格式装不下，就**明说装不下**。在此之前 XMP 是**静默**丢掉的 ——
    ``metadata`` 那一项写着「保留」，用户在结果文件里却找不到自己的版权信息，
    没有任何一句话告诉他为什么。
    """
    exif: bytes | None = None
    xmp: bytes | None = None
    if options.metadata == METADATA_KEEP:
        raw = read_exif(img)
        if raw:
            if supports_exif(out_format):
                exif = raw
            else:
                notes.append(EXIF_DROPPED_NOTE)

        found = read_xmp(img)
        if found:
            if supports_xmp(out_format):
                xmp = found
            else:
                notes.append(XMP_DROPPED_NOTE)

    dpi: tuple[int, int] | None = None
    if options.dpi is not None:
        if options.dpi == DPI_ORIGINAL:
            # 沿用源文件的密度。源文件里读不到就什么都不写 ——
            # 这不是「丢掉了用户的设置」，而是本来就没有可沿用的值。
            source_dpi = img.info.get("dpi")
            if (
                isinstance(source_dpi, (tuple, list))
                and len(source_dpi) == 2
                and all(
                    isinstance(item, (int, float)) and not isinstance(item, bool) and item > 0
                    for item in source_dpi
                )
            ):
                dpi = (int(round(source_dpi[0])), int(round(source_dpi[1])))
        elif isinstance(options.dpi, int) and not isinstance(options.dpi, bool):
            if options.dpi <= 0:
                raise ValidationError("分辨率必须是大于 0 的整数")
            dpi = (options.dpi, options.dpi)
        else:
            raise ValidationError("分辨率参数无效，可选值：保持原样、72、96、150、300 或自定义")

        if dpi is not None and not supports_density(out_format):
            notes.append(DENSITY_DROPPED_NOTE)
            dpi = None

    return exif, dpi, xmp


# ----------------------------------------------------------------------
# 内部步骤
# ----------------------------------------------------------------------

def _resolve_format(target_format: str | None, source_format: str, source: Path) -> str:
    """确定输出格式，并确认它能被编码。"""
    if target_format is not None:
        if target_format not in OUTPUT_FORMATS:
            raise ProcessingError(f"不支持输出格式：{target_format.upper()}")
        return target_format

    fmt = source_format if source_format in OUTPUT_FORMATS else ""
    if not fmt:
        # 文件头没能给出格式时，按原扩展名兜底
        suffix = source.suffix.lower().lstrip(".")
        fmt = "jpeg" if suffix in ("jpg", "jpeg") else suffix
    if fmt not in OUTPUT_FORMATS:
        raise ProcessingError(f"不支持输出格式：{(fmt or '未知').upper()}")
    return fmt


def _resolve_quality(options: PipelineOptions) -> tuple[int, int, int]:
    """算出 (无目标时使用的质量, 搜索下限, 搜索上限)。"""
    if options.quality_value is not None:
        high = max(1, min(100, int(options.quality_value)))
        return high, min(MIN_SEARCH_QUALITY, high), high

    if options.quality_preset is not None:
        preset = options.quality_preset
        if preset not in settings.QUALITY_PRESETS:
            raise ValidationError(f"未知的压缩质量档位：{preset}")
        low, high = settings.QUALITY_PRESETS[preset]
        base = settings.QUALITY_PRESET_POINTS.get(preset, settings.DEFAULT_QUALITY_VALUE)
        return base, low, high

    # 格式转换 / 尺寸调整：既没有档位也没有指定质量时，使用默认质量 80
    base = max(1, min(100, settings.DEFAULT_QUALITY_VALUE))
    return base, min(MIN_SEARCH_QUALITY, base), base


def _encode(
    img: Image.Image,
    out_format: str,
    options: PipelineOptions,
    scale: float,
    *,
    exif: bytes | None = None,
    dpi: tuple[int, int] | None = None,
    xmp: bytes | None = None,
) -> PipelineResult:
    base_quality, low, high = _resolve_quality(options)
    extra = build_extra(out_format, exif=exif, dpi=dpi, xmp=xmp)

    # PNG 是无损格式，encoder 用「调色板颜色数」近似 quality，属于有损量化。
    # 用户既没指定质量、也没提出体积要求时，不做这种量化 ——
    # 那等于凭空降低画质去换用户根本没要的收益。
    # 指定了目标大小时保留量化，因为 PNG 只有这一个质量杠杆。
    if (
        out_format == "png"
        and options.target_bytes is None
        and options.quality_value is None
        and options.quality_preset is None
    ):
        base_quality = LOSSLESS_PNG_QUALITY

    if options.target_bytes is None:
        data = encode_image(img, out_format, base_quality, extra=extra)
        return PipelineResult(
            data=data,
            width=img.width,
            height=img.height,
            format=out_format,
            quality_used=base_quality,
            scale=round(scale, 4),
            target_bytes=None,
            target_met=True,
            untouched=False,
        )

    # 用户明确指定了尺寸时不能再擅自缩小 —— 那会违背用户输入的宽高
    fit = encode_under_target(
        img,
        out_format,
        options.target_bytes,
        low,
        high,
        allow_downscale=options.resize is None,
        extra=extra,
    )
    return PipelineResult(
        data=fit.data,
        width=fit.image.width,
        height=fit.image.height,
        format=out_format,
        quality_used=fit.quality,
        scale=round(scale * fit.scale, 4),
        target_bytes=options.target_bytes,
        target_met=fit.target_met,
        untouched=False,
        notes=[] if fit.target_met else [TARGET_UNREACHABLE_NOTE],
    )
