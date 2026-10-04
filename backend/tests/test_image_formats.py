"""第九阶段（§七 / §八）新增的四种图片格式：BMP / GIF / TIFF / ICO。

这些格式在架构上不是新东西 —— 它们走的是同一条
``decode → normalize → transform → optimize → encode`` 流水线，
共用同一个 ``_image_to_image`` 叶子（§八 明令禁止为每个组合写一个 converter）。
所以这一组测试要验的不是「有没有新代码」，而是三件容易悄悄错的事：

1. **真的能编出来，也真的能读回来** —— 每次都拿 Pillow 重新打开结果，
   看 ``format``、尺寸和像素，而不是只看「函数没抛错」（§五十五）。
2. **色彩模式没被静默毁掉** —— BMP 的 alpha 要合成到白底而不是丢成黑色，
   GIF 的透明像素要还是透明的，CMYK 转 TIFF 不该报错。
3. **校验层认识它们的文件头** —— 否则文件在能力 API 里可选、上传时却被拒，
   是最难查的一类不一致。

另有一条**词表对账**：``conversion.options`` 里那几张按目标裁剪的表
（哪些格式有质量、有 EXIF、有 DPI）与 ``compressors.encoder`` 里的
真实能力必须一致。两处写的是同一件事，漂移的表现是「界面摆了旋钮、
转出来却没反应」。
"""

from __future__ import annotations

import io
import zipfile

import pymupdf
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from compressors.encoder import (
    DENSITY_FORMATS,
    EXIF_FORMATS,
    OUTPUT_FORMATS,
    build_extra,
    encode_image,
    extension_for,
    normalize_format,
    supports_density,
    supports_exif,
)
from compressors.heif import heif_support
from compressors.loader import load_image
from compressors.pipeline import (
    DENSITY_DROPPED_NOTE,
    EXIF_DROPPED_NOTE,
    MULTIFRAME_NOTE,
    MULTIPAGE_TIFF_NOTE,
    PipelineOptions,
    run_pipeline,
)
from config import settings
from conversion import options as opts
from conversion import registry
from converters.image_converter import (
    LEGACY_TARGET_FORMATS,
    prepare_for_format,
)
from tests.conftest import (
    build_animated_gif,
    build_image_bytes,
    build_multipage_tiff,
    conversion_task,
    encodable_formats,
    image_files,
    run_conversion,
)
from utils.errors import ErrorCode, UnsupportedTypeError, ValidationError
from utils.validation import (
    _MAGIC_SIGNATURES,
    check_extension,
    sniff_format,
    validate_image_upload,
)

#: 走图片编码器的全部格式（编码层内部名）。
ALL_IMAGE_FORMATS = ("jpeg", "png", "webp", "bmp", "gif", "tiff", "ico")

#: 第九阶段新增的那四种。
NEW_FORMATS = ("bmp", "gif", "tiff", "ico")


def _sample(fmt: str, size: tuple[int, int] = (40, 24)) -> Image.Image:
    """一张有两种颜色的小图，方便断言像素。"""
    image = Image.new("RGB", size, (200, 40, 40))
    image.putpixel((0, 0), (10, 200, 240))
    return image


def _reopen(data: bytes) -> Image.Image:
    """真的把结果打开（不是只看字节数）。"""
    image = Image.open(io.BytesIO(data))
    image.load()
    return image


def _bytes_of(fmt: str) -> bytes:
    """把样例图真的编码成 ``fmt``（走 Pillow，不走被测代码）。"""
    buffer = io.BytesIO()
    _sample(fmt).save(buffer, format=fmt)
    return buffer.getvalue()


def _write(tmp_path, name: str, data: bytes):
    path = tmp_path / name
    path.write_bytes(data)
    return path


#: 目标类型 → 结果该被 Pillow 认成什么格式名。
#: ``heic`` 那一行写的是 ``HEIF`` —— Pillow 报的是容器名，不是扩展名。
#: 这条差异在 ``encoder.normalize_format`` 里已经踩过一次，这里再来一次
#: 很容易写成 ``HEIC`` 然后拿到一个「格式不对」的假失败。
_EXPECTED_PILLOW_FORMAT = {
    "jpg": "JPEG",
    "png": "PNG",
    "webp": "WEBP",
    "bmp": "BMP",
    "gif": "GIF",
    "tiff": "TIFF",
    "heic": "HEIF",
}


def _assert_real_output(client: TestClient, task: dict, target: str) -> None:
    """把一条已完成任务的结果**真的下载并打开**（§五十五：不能只看状态）。

    返回值不用，断言就是目的：状态为 completed 但内容是零字节、
    或者其实是另一种格式，只有打开才看得见。
    """
    response = client.get(task["result"]["download_url"])
    assert response.status_code == 200, (target, response.status_code)
    assert response.content, target
    if target == "pdf":
        with pymupdf.open(stream=response.content, filetype="pdf") as doc:
            assert doc.page_count == 1, target
            # 页面上真的有一张图，不是一页空白
            assert doc[0].get_images(), target
        return
    with Image.open(io.BytesIO(response.content)) as image:
        image.load()  # 光 open 不解码，坏文件要 load 才现形
        assert image.format == _EXPECTED_PILLOW_FORMAT[target], target
        assert image.size == (120, 80), target


# ----------------------------------------------------------------------
# 1. 编码器：每一种格式都能编出来、都能读回来
# ----------------------------------------------------------------------

@pytest.mark.parametrize("fmt", ALL_IMAGE_FORMATS)
def test_every_format_encodes_and_reopens(fmt: str) -> None:
    """七种格式逐一走一遍「编码 → 重新打开」，格式与尺寸都要对得上。"""
    source = _sample(fmt)
    prepared = prepare_for_format(source, fmt)
    data = encode_image(prepared, fmt, 85)

    back = _reopen(data)
    # Pillow 对 JPEG 报的是 ``JPEG``，其余都是格式名本身 —— 统一小写后逐字相等
    assert back.format.lower() == fmt, (fmt, back.format)
    # ICO 是容器：Pillow 会按自己的尺寸表等比缩略，所以只断言没被放大
    if fmt == "ico":
        assert max(back.size) <= 256
        assert back.size[0] <= source.width and back.size[1] <= source.height
    else:
        assert back.size == source.size, fmt


@pytest.mark.parametrize("fmt", ALL_IMAGE_FORMATS)
def test_every_format_keeps_the_picture(fmt: str) -> None:
    """像素不能被编没了 —— 主色必须还在。

    BMP / TIFF 是无损的，颜色要一模一样；JPEG / WEBP / GIF / ICO 有量化，
    给一个宽容但真实的容差（离原色太远就说明色彩模式被搞错了，
    例如 alpha 被丢成黑色）。
    """
    source = _sample(fmt)
    data = encode_image(prepare_for_format(source, fmt), fmt, 90)
    back = _reopen(data).convert("RGB")

    if fmt in ("bmp", "tiff"):
        assert back.getpixel((20, 12)) == (200, 40, 40)
    else:
        pixel = back.getpixel((20, 12))
        assert all(abs(a - b) <= 12 for a, b in zip(pixel, (200, 40, 40))), (fmt, pixel)


def test_output_formats_cover_the_whole_registry_vocabulary() -> None:
    """注册表登记的目标，编码层必须都认识 —— 两侧用的是同一组名字。"""
    for target in registry.TARGET_TYPES:
        internal = normalize_format(target)
        if internal is None:
            continue  # pdf / docx 不走图片编码器
        assert internal in OUTPUT_FORMATS, target


def test_new_format_names_normalize_from_their_aliases() -> None:
    """``tif`` / ``.tiff`` / ``TIF`` 都要归到同一个内部名。"""
    assert normalize_format("tif") == "tiff"
    assert normalize_format(".TIFF") == "tiff"
    assert normalize_format(" TJPG ") is None  # 空白与未知值一样是被拒的
    for fmt in NEW_FORMATS:
        assert normalize_format(fmt.upper()) == fmt
        assert normalize_format(f".{fmt}") == fmt


def test_extensions_match_the_encoder() -> None:
    assert extension_for("bmp") == ".bmp"
    assert extension_for("gif") == ".gif"
    assert extension_for("tiff") == ".tiff"
    assert extension_for("ico") == ".ico"


# ----------------------------------------------------------------------
# 2. 色彩模式：不能被静默毁掉
# ----------------------------------------------------------------------

def test_bmp_flattens_alpha_instead_of_dropping_it() -> None:
    """BMP 存不住 alpha。丢掉通道会变黑，合成到白底才是用户预期的结果。

    半透明的红像素 → 白底合成 → 偏粉的实色；如果实现是「直接丢 alpha」，
    得到的是纯红（看起来「对」但其实没合成）或者黑（明显错）。
    这里断言它**确实被合成过**：红通道满、绿蓝通道被白底抬高。
    """
    rgba = Image.new("RGBA", (8, 8), (255, 0, 0, 128))
    prepared = prepare_for_format(rgba, "bmp")
    assert prepared.mode == "RGB", "BMP 的结果不该带 alpha 通道"

    back = _reopen(encode_image(prepared, "bmp", 85)).convert("RGB")
    red, green, blue = back.getpixel((4, 4))
    assert red == 255
    assert green > 100 and blue > 100, (red, green, blue)  # 白底抬高了它们
    assert abs(green - blue) <= 2, "白底合成后绿蓝应当相同"


def test_jpeg_and_bmp_flatten_the_same_way() -> None:
    """同一种输入，JPG 与 BMP 的合成结果必须一致 —— 两条分支不能各做一套。"""
    rgba = Image.new("RGBA", (8, 8), (10, 90, 200, 96))
    jpeg_pixel = _reopen(
        encode_image(prepare_for_format(rgba, "jpeg"), "jpeg", 95)
    ).convert("RGB").getpixel((4, 4))
    bmp_pixel = _reopen(
        encode_image(prepare_for_format(rgba, "bmp"), "bmp", 95)
    ).convert("RGB").getpixel((4, 4))
    assert all(abs(a - b) <= 6 for a, b in zip(jpeg_pixel, bmp_pixel)), (
        jpeg_pixel,
        bmp_pixel,
    )


def test_gif_keeps_fully_transparent_pixels_transparent() -> None:
    """GIF 只有 1 位透明度，但「完全透明」必须真的是透明的。

    实现上交给 Pillow 自己量化（实测手写调色板反而会把半透明像素
    整个抹掉）。这条钉住的是**结果**：透明处透明的、不透明处颜色不变。
    """
    rgba = Image.new("RGBA", (8, 8), (0, 0, 255, 0))
    rgba.putpixel((0, 0), (255, 0, 0, 255))

    back = _reopen(encode_image(prepare_for_format(rgba, "gif"), "gif", 85)).convert("RGBA")
    assert back.getpixel((0, 0)) == (255, 0, 0, 255), "不透明像素必须原样保留"
    assert back.getpixel((4, 4))[3] == 0, "完全透明的像素必须还是透明的"


def test_tiff_keeps_the_wide_colour_modes() -> None:
    """TIFF 的原生模式（CMYK / I;16）不该被降级成 RGB。"""
    for mode, value in (("CMYK", (0, 128, 128, 0)), ("I;16", 4000)):
        source = Image.new(mode, (8, 8), value)
        prepared = prepare_for_format(source, "tiff")
        assert prepared.mode == mode, mode
        back = _reopen(encode_image(prepared, "tiff", 85))
        assert back.mode == mode, (mode, back.mode)


def test_bmp_handles_the_modes_it_declares() -> None:
    """BMP 声明的四种模式都要真的存得下去。"""
    for mode, value in (("1", 1), ("L", 128), ("RGB", (10, 20, 30))):
        source = Image.new(mode, (8, 8), value)
        prepared = prepare_for_format(source, "bmp")
        assert prepared.mode == mode, mode
        assert _reopen(encode_image(prepared, "bmp", 85)).mode == mode, mode

    # 调色板图（来自 GIF 的那种）必须原样保留调色板，而不是被转成 RGB
    palette_source = _reopen(
        encode_image(prepare_for_format(_sample("gif"), "gif"), "gif", 85)
    )
    assert palette_source.mode == "P"
    assert prepare_for_format(palette_source, "bmp").mode == "P"


def test_ico_is_always_rgb_or_rgba() -> None:
    """ICO 的每一帧最终由 BMP 写入器存盘，只认 RGB / RGBA。

    没有 alpha 的模式降到 RGB（不凭空造一个 alpha 通道），
    有 alpha 的才升到 RGBA —— 升上去会多一层半透明，用户看不出来；
    降下来则会把透明区域压成实色，那才是真的丢东西。
    """
    assert prepare_for_format(Image.new("RGBA", (8, 8), (0, 0, 0, 0)), "ico").mode == "RGBA"
    for mode in ("CMYK", "P", "L", "RGB", "1", "I;16"):
        assert prepare_for_format(Image.new(mode, (8, 8)), "ico").mode == "RGB", mode


def test_ico_keeps_the_aspect_ratio() -> None:
    """ICO 不能被补成正方形 —— 那会破坏用户图片的长宽比。

    Pillow 的 ICO 写入器会对每一档做等比缩略（``thumbnail``），
    所以 400×200 的源得到的是 128×64，而不是 128×128。
    """
    wide = Image.new("RGBA", (400, 200), (0, 128, 255, 255))
    back = _reopen(encode_image(prepare_for_format(wide, "ico"), "ico", 85))
    assert back.size[0] == 2 * back.size[1], back.size
    assert max(back.size) <= 256


def test_ico_from_a_square_png_reaches_the_largest_frame() -> None:
    """方图能拿到 256×256 那一档（ICO 的标准上限）。"""
    square = Image.new("RGBA", (256, 256), (0, 128, 255, 255))
    back = _reopen(encode_image(prepare_for_format(square, "ico"), "ico", 85))
    assert back.size == (256, 256)


def test_ico_is_never_a_zero_frame_shell() -> None:
    """短边不足 16 像素的图必须**明确拒绝**，不能交出零帧空壳。

    这是真机验收获抓到的 bug：Pillow 的 ICO 写入器遇到「一档都够不着」
    的源图时不抛异常，只写出 6 个字节的 ``00 00 01 00 00 00``（ICONDIR
    里帧数为 0）就返回。此前那条路上任务状态是「已完成」、体积 6 字节，
    用户拿到的是一个打不开的 .ico —— 虚假成功比报错糟得多。
    """
    from compressors.encoder import ICO_MIN_EDGE

    assert ICO_MIN_EDGE == 16
    # 短边小于 16 的一律拒绝 —— 注意 (40, 15) 这种「长边够、短边不够」
    # 的也要拒绝，ICONDIR 是按短边判的
    for size in ((1, 1), (10, 10), (15, 15), (15, 40), (17, 9), (16, 15)):
        with pytest.raises(ValidationError):
            encode_image(prepare_for_format(_sample("png", size), "ico"), "ico", 85)

    # 短边刚好 16 就能出图，而且真的能打开。
    # 注意帧尺寸：ICO 的尺寸表按**短边**选档，再对每一档做等比缩略，
    # 所以 16×40 的源得到的是 6×16 —— 长边才是 16，短边可以更小。
    for size in ((16, 16), (16, 40), (17, 17)):
        data = encode_image(prepare_for_format(_sample("png", size), "ico"), "ico", 85)
        assert len(data) > 6, (size, len(data))
        back = _reopen(data)
        assert back.format == "ICO", (size, back.format)
        assert max(back.size) == 16, (size, back.size)
        # 长宽比没有给补成正方形
        assert back.size[0] / back.size[1] == pytest.approx(size[0] / size[1], rel=0.2), size


def test_every_image_target_encoder_produces_a_openable_file() -> None:
    """每一种本机能写出来的目标格式都要产出**真的打得开**的文件，不能只看字节数。

    §五十五~§五十七 的 Real Output Validation 在单元层的最小版本：
    编码器是「最后一道工序」，它悄悄产出空壳的话，前面所有校验都白做。
    有损格式（JPG / WEBP）只能比个大概，无损格式必须逐像素相等。

    遍历 ``encodable_formats()`` 而不是 ``OUTPUT_FORMATS``：后者是
    「编码器认识它们」，其中 ``heif`` 要 ``pillow-heif`` 带 HEVC 编码器
    才写得出来。缺席时只把它一项去掉 —— 其余七种格式的回归一条都不能少，
    不整条跳过。
    """
    lossy = {"jpeg", "webp"}
    for fmt in encodable_formats():
        prepared = prepare_for_format(_sample("png", (40, 24)), fmt)
        data = encode_image(prepared, fmt, 85)
        assert data, fmt
        back = _reopen(data)

        if fmt == "ico":
            # ICO 是**多分辨率容器**，里面装的不是原尺寸那一张：
            # 最大的帧由尺寸表里不超过源图短边的那一档决定（40×24 → 24 档 → 24×14）
            assert max(back.size) == 24, back.size
            assert back.size[0] / back.size[1] == pytest.approx(40 / 24, rel=0.2), back.size
        else:
            assert back.size == (40, 24), (fmt, back.size)

        # 像素也真的写进去了。取图中心那块**平坦**区域比：单看左上角
        # 那一个异色像素在有损格式上会被 8×8 的 DCT 块糊掉，
        # 那是 JPEG 的固有行为，不是「没写进去」。
        center = back.convert("RGB").getpixel((back.size[0] // 2, back.size[1] // 2))
        if fmt in lossy:
            assert all(abs(a - b) <= 20 for a, b in zip(center, (200, 40, 40))), (fmt, center)
        else:
            assert center == (200, 40, 40), (fmt, center)


# ----------------------------------------------------------------------
# 3. 元数据 / 密度：只在该给的时候给
# ----------------------------------------------------------------------

def test_build_extra_only_passes_what_the_format_accepts() -> None:
    """``build_extra`` 是「哪些格式真的收得下 EXIF / DPI」的唯一出口。

    给不支持它的格式塞进去不会报错，而是**静默丢掉** —— 比报错更糟，
    调用方会以为写进去了。所以这一格必须在传之前就拦住。
    """
    exif = b"Exif\x00\x00fake"
    dpi = (300, 300)

    assert build_extra("jpeg", exif=exif, dpi=dpi) == {"exif": exif, "dpi": dpi}
    assert build_extra("tiff", exif=exif, dpi=dpi) == {"exif": exif, "dpi": dpi}
    assert build_extra("png", exif=exif, dpi=dpi) == {"exif": exif, "dpi": dpi}
    # WebP 收 EXIF 但没有密度字段
    assert build_extra("webp", exif=exif, dpi=dpi) == {"exif": exif}
    # GIF / BMP / ICO 两样都不收
    for fmt in ("gif", "bmp", "ico"):
        assert build_extra(fmt, exif=exif, dpi=dpi) == {}, fmt


def test_dpi_really_lands_in_the_file_for_the_formats_that_claim_it() -> None:
    """声明支持密度的格式，写进去必须真的能读回来（§九：不许伪造）。

    这里不能用精确相等：PNG 的密度存在 pHYs 块里，单位是**每米**像素数，
    300 dpi 换算、取整、再换算回来是 299.9994。差的是浮点换算，
    不是「没写进去」—— 所以给一个紧到能认出「写错了」、松到容得下换算的容差。
    """
    source = _sample("png")
    for fmt in DENSITY_FORMATS:
        prepared = prepare_for_format(source, fmt)
        extra = build_extra(fmt, exif=None, dpi=(300, 300))
        back = _reopen(encode_image(prepared, fmt, 85, extra=extra))
        stored = back.info.get("dpi")
        assert stored is not None, f"{fmt} 声明支持密度却没有写进文件"
        assert stored[0] == pytest.approx(300, abs=0.5), (fmt, stored)
        assert stored[1] == pytest.approx(300, abs=0.5), (fmt, stored)


def test_dpi_is_writeable_and_readable_for_every_value_the_ui_offers() -> None:
    """§九 的 72 / 96 / 150 / 300 四档都要真的写得进去、读得回来。

    只测 300 的话，「换算系数写反了」这类错误在 300 上恰好看起来正常
    （因为 300 dpi 的每米像素数是一个整数）。
    """
    source = _sample("png")
    for value in (72, 96, 150, 300):
        prepared = prepare_for_format(source, "tiff")
        extra = build_extra("tiff", exif=None, dpi=(value, value))
        back = _reopen(encode_image(prepared, "tiff", 85, extra=extra))
        assert back.info["dpi"][0] == pytest.approx(value, abs=0.5), value


def test_encoder_capability_tables_agree_with_the_option_vocabulary() -> None:
    """编码器的「支不支持 EXIF / DPI」与选项词表的裁剪表必须一致。

    两处是同一件事的两种写法：一边按编码层内部名（``jpeg``），
    一边按线上词汇（``jpg``）。漂移的后果是一个界面上真实存在的旋钮，
    转过去却被 ``build_extra`` 静默丢掉。归一化之后必须逐项相等。
    """
    assert {normalize_format(t) for t in opts._EXIF_TARGETS} == set(EXIF_FORMATS)
    assert {normalize_format(t) for t in opts._DENSITY_TARGETS} == set(DENSITY_FORMATS)
    # 有损格式那边没有对应的编码器常量，只能钉住它确实只覆盖这三种。
    # ``heic`` 归一化后是 ``heif``（Pillow 认的是容器名，见
    # ``encoder.normalize_format``），所以这里写 ``heif`` 而不是 ``heic``。
    assert {normalize_format(t) for t in opts._LOSSY_TARGETS} == {
        "jpeg", "webp", "heif",
    }


def test_dpi_help_text_names_every_format_on_the_correct_side() -> None:
    """``dpi`` 的界面说明必须把七个格式一个不漏地站对边。

    这条测试是被真机抓出来的 bug 逼出来的：说明里原本写着
    「GIF / BMP / ICO 不支持」，漏了 WEBP —— 而 WEBP 目标提交 ``dpi``
    确实会 400。用户看不到旋钮，说明却也说不清为什么，两边都不对。

    断言方式是把说明里的两组名字还原成编码器内部名，再和
    ``DENSITY_FORMATS`` 及其补集比**集合相等** —— 多写一个、
    少写一个、写错边都会红。
    """
    ok = {normalize_format(label) for label in opts._DENSITY_OK_LABELS}
    no = {normalize_format(label) for label in opts._DENSITY_NO_LABELS}

    assert ok == set(DENSITY_FORMATS), (ok, DENSITY_FORMATS)
    assert no == set(OUTPUT_FORMATS) - set(DENSITY_FORMATS), no
    assert not (ok & no), ok & no
    # 七个格式必须都被点到名，一个都不能漏
    assert ok | no == set(OUTPUT_FORMATS), ok | no

    text = opts._DPI.help
    assert text, "dpi 没有帮助说明"
    for label in (*opts._DENSITY_OK_LABELS, *opts._DENSITY_NO_LABELS):
        assert label in text, (label, text)


def test_supports_helpers_match_the_tables() -> None:
    for fmt in ALL_IMAGE_FORMATS:
        assert supports_exif(fmt) is (fmt in EXIF_FORMATS), fmt
        assert supports_density(fmt) is (fmt in DENSITY_FORMATS), fmt


# ----------------------------------------------------------------------
# 4. 校验层：文件头、扩展名、以及「假扩展名」仍然被拦
# ----------------------------------------------------------------------

@pytest.mark.parametrize(
    "fmt, name",
    [
        ("BMP", "sample.bmp"),
        ("GIF", "sample.gif"),
        ("TIFF", "sample.tiff"),
        ("TIFF", "sample.tif"),
    ],
)
def test_new_formats_pass_the_whole_upload_validation(tmp_path, fmt, name) -> None:
    """新格式走一遍完整的三层校验：扩展名 → 文件头 → Pillow 真解码。"""
    data = _sample(fmt).convert("RGB") if fmt != "GIF" else _sample("GIF")
    buffer = io.BytesIO()
    data.save(buffer, format=fmt if fmt != "GIF" else "GIF")

    path = _write(tmp_path, name, buffer.getvalue())
    width, height, detected = validate_image_upload(path, name)
    assert (width, height) == (40, 24)
    assert detected == fmt.lower()


def test_sniff_format_knows_the_new_signatures(tmp_path) -> None:
    """文件头这一层必须认识新格式 —— 它先于 Pillow 运行。"""
    assert set(_MAGIC_SIGNATURES) >= {"bmp", "gif", "tiff"}

    for fmt, name, internal in (
        ("BMP", "a.bmp", "bmp"),
        ("GIF", "a.gif", "gif"),
        ("TIFF", "a.tiff", "tiff"),
    ):
        path = _write(tmp_path, name, _bytes_of(fmt))
        assert sniff_format(path) == internal, fmt


def test_sniff_format_reads_both_tiff_byte_orders(tmp_path) -> None:
    """TIFF 有大端与小端两种字节序，两个签名都要认。"""
    little = _write(tmp_path, "le.tiff", _bytes_of("TIFF"))
    assert sniff_format(little) == "tiff"
    # ``MM\x00*``：把字节序标记直接改掉，不需要真的生成一份大端 TIFF
    big_endian = b"MM\x00*" + little.read_bytes()[4:]
    assert sniff_format(_write(tmp_path, "be.tiff", big_endian)) == "tiff"


def test_ico_is_not_an_upload_format(tmp_path) -> None:
    """§七 只有 PNG → ICO：ICO 只出不进。

    让它进得来会立刻产生两处自相矛盾 —— 能力 API 里没有「ICO 能转成
    什么」，上传页面却收它。
    """
    assert ".ico" not in {ext.lower() for ext in settings.ALLOWED_IMAGE_EXTENSIONS}

    ico = io.BytesIO()
    _sample("ico").save(ico, format="ICO")
    path = _write(tmp_path, "icon.ico", ico.getvalue())
    with pytest.raises(UnsupportedTypeError):
        check_extension("icon.ico")
    with pytest.raises(UnsupportedTypeError):
        validate_image_upload(path, "icon.ico")
    assert registry.source_for_extension("icon.ico") is None


def test_a_renamed_file_is_still_rejected(tmp_path) -> None:
    """扩容不改安全策略：内容与扩展名对不上照样拒（§四十五）。"""
    png_bytes = _bytes_of("PNG")

    # PNG 的内容、.gif 的名字
    path = _write(tmp_path, "fake.gif", png_bytes)
    with pytest.raises(UnsupportedTypeError):
        validate_image_upload(path, "fake.gif")

    # 可执行文件的头、.bmp 的名字（BMP 的签名只有两个字节 ``BM``，
    # 最容易出现「只看签名就放行」的漏洞，所以这条必须验）
    exe = _write(tmp_path, "evil.bmp", b"MZ\x90\x00" + b"\x00" * 64)
    with pytest.raises(UnsupportedTypeError):
        validate_image_upload(exe, "evil.bmp")

    # 一个 ICO 改名成 .png：文件头认不出来，必须拒
    ico = io.BytesIO()
    _sample("ico").save(ico, format="ICO")
    disguised = _write(tmp_path, "fake.png", ico.getvalue())
    with pytest.raises(UnsupportedTypeError):
        validate_image_upload(disguised, "fake.png")


def test_extension_whitelist_covers_every_new_source() -> None:
    """新格式的扩展名必须与注册表的图片源逐项对上。"""
    for source in (
        registry.SOURCE_BMP,
        registry.SOURCE_GIF,
        registry.SOURCE_TIFF,
    ):
        for ext in registry.EXTENSIONS_BY_SOURCE[source]:
            assert ext in {item.lower() for item in settings.ALLOWED_IMAGE_EXTENSIONS}


# ----------------------------------------------------------------------
# 5. 完整流水线：新格式走的是同一条
# ----------------------------------------------------------------------

@pytest.mark.parametrize("target", NEW_FORMATS)
def test_pipeline_produces_the_new_formats(tmp_path, target: str) -> None:
    """``run_pipeline`` 是统一转换中心真正调用的那一层。"""
    source = _write(tmp_path, "in.png", _bytes_of("PNG"))

    result = run_pipeline(
        source,
        PipelineOptions(target_format=target, metadata=opts.METADATA_REMOVE),
    )

    assert result.format == target
    assert extension_for(result.format) == registry.EXTENSION_BY_TARGET[target]
    back = _reopen(result.data)
    assert back.format.lower() == target


def test_pipeline_rotation_still_applies_to_a_new_format(tmp_path) -> None:
    """扩格式没有绕开第九阶段补的那三个快速返回路径守卫。

    旋转在新格式上同样必须生效 —— 否则用户勾了「旋转 90°」，
    结果卡显示成功而图一动不动。
    """
    # 已经是 BMP，目标也是 BMP，且没有任何其它参数：这正是那条近路
    source = _write(tmp_path, "in.bmp", _bytes_of("BMP"))

    untouched = run_pipeline(source, PipelineOptions(target_format="bmp"))
    assert untouched.untouched is True
    assert untouched.notes and "已经是" in untouched.notes[0]

    rotated = run_pipeline(
        source, PipelineOptions(target_format="bmp", rotation=90)
    )
    assert rotated.untouched is False, "旋转必须让那条近路失效"
    assert (rotated.width, rotated.height) == (24, 40)


def _jpeg_with_exif() -> bytes:
    """一张带真实拍摄信息的 JPEG —— 用户从相机/手机拿到的就是这种。"""
    import io as _io

    exif = Image.Exif()
    exif[0x010F] = "FileTools"      # Make
    exif[0x0110] = "Format Prober"  # Model
    buffer = _io.BytesIO()
    _sample("jpeg").save(buffer, format="JPEG", exif=exif.tobytes(), quality=95)
    return buffer.getvalue()


def test_pipeline_metadata_and_dpi_are_honest_on_a_new_format(tmp_path) -> None:
    """要了元数据而格式装不下时，必须**如实说明**，不能装作写进去了。

    走统一转换中心时这种组合被 schema 提前挡掉（GIF 根本不显示
    「保留元数据」），所以这条路只可能被直接调用的代码走到 ——
    但那正是它存在的意义：它是最后一道「不许伪造」的防线。

    注意源图必须是**真的带 EXIF** 的 JPEG：拿一张没有拍摄信息的图来测，
    这条「如实说明」的分支根本不会被触发，测试会绿得毫无意义。
    """
    source = _write(tmp_path, "shot.jpg", _jpeg_with_exif())

    # 先确认源图真的带着 EXIF，否则下面的断言证明不了任何事
    assert load_image(source)[0].info.get("exif")

    result = run_pipeline(
        source,
        PipelineOptions(
            target_format="gif",
            metadata=opts.METADATA_KEEP,
            dpi=300,
        ),
    )

    assert EXIF_DROPPED_NOTE in result.notes
    assert DENSITY_DROPPED_NOTE in result.notes
    back = _reopen(result.data)
    assert back.format == "GIF"
    # 说明归说明，图本身必须转出来
    assert back.size == (40, 24)


@pytest.mark.parametrize("target", ["tiff", "png", "webp", "jpeg"])
def test_every_format_that_claims_exif_really_carries_it(tmp_path, target: str) -> None:
    """声明支持 EXIF 的格式，拍摄信息必须真的读得回来 —— 而不是「没报错」。

    读的位置因容器而异：PNG/WebP/JPEG 走 ``info["exif"]`` 这个独立块，
    TIFF 则把 EXIF 直接写成 TIFF 标签目录本身。所以这里统一用
    ``getexif()`` —— 它是任何容器都能走的标准入口，
    不会因为「换了个容器没读到」而误判成「元数据丢了」。
    """
    source = _write(tmp_path, "shot.jpg", _jpeg_with_exif())

    result = run_pipeline(
        source,
        PipelineOptions(target_format=target, metadata=opts.METADATA_KEEP),
    )

    assert EXIF_DROPPED_NOTE not in result.notes
    stored = _reopen(result.data).getexif()
    assert stored.get(0x010F) == "FileTools", (target, dict(stored))
    assert stored.get(0x0110) == "Format Prober", (target, dict(stored))


def test_a_format_that_cannot_carry_density_says_nothing_about_exif(tmp_path) -> None:
    """只丢掉密度时不该顺带说元数据的事 —— 一条提示对应一件事。"""
    source = _write(tmp_path, "shot.png", _bytes_of("PNG"))

    result = run_pipeline(
        source,
        PipelineOptions(target_format="gif", dpi=300),
    )

    assert DENSITY_DROPPED_NOTE in result.notes
    assert EXIF_DROPPED_NOTE not in result.notes, "没要求保留元数据，就不该出现这条"


# ----------------------------------------------------------------------
# 6. 多帧文件：只转第一帧这件事必须说出来
# ----------------------------------------------------------------------



def test_multiframe_sources_are_reported_honestly(tmp_path) -> None:
    """§九 的「如实说明」也适用于帧数：只转第一帧就必须讲明。

    用户看到「转换成功」、拿到一张 30×20 的静图，而原文件是 60 帧的动图 ——
    不说，就是伪造了一个完整的转换结果。
    """
    animated = _write(tmp_path, "anim.gif", build_animated_gif(3))
    multipage = _write(tmp_path, "doc.tiff", build_multipage_tiff(4))

    gif_result = run_pipeline(animated, PipelineOptions(target_format="png"))
    assert MULTIFRAME_NOTE.format(count=3) in gif_result.notes

    tiff_result = run_pipeline(multipage, PipelineOptions(target_format="jpeg"))
    assert MULTIPAGE_TIFF_NOTE.format(count=4) in tiff_result.notes
    # 措辞按容器分：GIF 是「帧」，TIFF 是「页」
    assert not any("页" in note for note in gif_result.notes)
    assert not any("帧" in note for note in tiff_result.notes)


def test_single_frame_sources_get_no_such_note(tmp_path) -> None:
    """单帧文件不能被安上「只转了第一帧」——那也是一句假话。"""
    plain = _write(tmp_path, "still.gif", _bytes_of("GIF"))
    result = run_pipeline(plain, PipelineOptions(target_format="png"))
    assert not any("第一帧" in note or "第一页" in note for note in result.notes)


def test_untouched_returns_carry_no_frame_note(tmp_path) -> None:
    """前三秒的近路是**原样退回原文件** —— 所有帧都在里面。

    这条最容易被写错：把说明挂在函数开头，于是「你的 GIF 已经是 GIF、
    原样退回」也收到一句「只转换了第一帧」，而文件其实一帧没少。
    """
    animated = _write(tmp_path, "anim.gif", build_animated_gif(3))

    result = run_pipeline(animated, PipelineOptions(target_format="gif"))
    assert result.untouched is True
    # 退回的是**原始字节**，帧一帧没少
    assert result.data == animated.read_bytes()
    assert not any("第一帧" in note for note in result.notes), result.notes


def test_multiframe_note_reaches_the_api_result(client: TestClient) -> None:
    """说明必须真的走到结果里 —— 参数面板与结果卡读的就是这个字段。"""
    snapshot = run_conversion(
        client,
        files=image_files(("anim.gif", build_animated_gif(3))),
        target_type="png",
    )

    assert snapshot["status"] == "completed"
    task = conversion_task(snapshot, 0)
    assert task["status"] == "completed"
    assert MULTIFRAME_NOTE.format(count=3) in task["result"]["notes"]
    # 整批摘要里也有一份（只有一个结果时前端读的是它）
    assert MULTIFRAME_NOTE.format(count=3) in snapshot["result"]["notes"]

    response = client.get(task["result"]["download_url"])
    with Image.open(io.BytesIO(response.content)) as image:
        assert image.format == "PNG"
        assert image.size == (30, 20)  # 第一帧的尺寸，不是全部帧叠起来


# ----------------------------------------------------------------------
# 7. HTTP 端到端：新格式真的能转，结果真的能打开
# ----------------------------------------------------------------------

#: 扩展名 → Pillow 认的格式名。``.jpg`` 写成 ``JPG`` 会被 Pillow 拒绝
#: （它只认 ``JPEG``），所以样张生成这一层也得有个映射，不能直接 upper()。
PILLOW_FORMAT = {
    "jpg": "JPEG",
    "jpeg": "JPEG",
    "png": "PNG",
    "webp": "WEBP",
    "bmp": "BMP",
    "gif": "GIF",
    "tif": "TIFF",
    "tiff": "TIFF",
}

#: (源文件名, 目标, 结果扩展名, 结果的 Pillow 格式, 源尺寸)
HTTP_CASES = [
    ("photo.bmp", "webp", ".webp", "WEBP", (200, 120)),
    ("photo.gif", "png", ".png", "PNG", (200, 120)),
    ("photo.tiff", "jpg", ".jpg", "JPEG", (200, 120)),
    ("photo.png", "ico", ".ico", "ICO", (200, 120)),
    ("photo.jpg", "bmp", ".bmp", "BMP", (200, 120)),
    ("photo.webp", "tiff", ".tiff", "TIFF", (200, 120)),
]


def test_png_to_ico_refuses_a_picture_that_cannot_make_a_single_frame(
    client: TestClient,
) -> None:
    """短边不足 16 像素的 PNG 转 ICO 必须**失败**，不能交出零帧空壳。

    真机验收获抓到的 bug：这种情况此前任务是「已完成」、产物 6 字节，
    用户下载到一个打不开的 .ico。这条测试从 HTTP 层盯住它 ——
    既要有明确的错误码与说明，也不许留下任何 result。
    """
    for size in ((10, 10), (17, 9), (15, 40)):
        png = build_image_bytes(size[0], size[1], "PNG")
        snapshot = run_conversion(
            client, files=image_files((f"tiny-{size[0]}x{size[1]}.png", png)),
            target_type="ico",
        )
        task = conversion_task(snapshot, 0)
        assert task["status"] == "failed", (size, task)
        assert task["result"] is None, (size, task["result"])
        assert task["error_code"] == ErrorCode.INVALID_REQUEST, (size, task["error_code"])

        message = str(task["error_message"])
        assert "16" in message, (size, message)
        # 面向用户的文案里不许出现这些
        for leak in ("Traceback", "PIL", "FileTools", "\\", "C:"):
            assert leak not in message, (size, leak, message)


@pytest.mark.parametrize("name, target, extension, pillow_format, size", HTTP_CASES)
def test_new_formats_convert_over_http(
    client: TestClient, name: str, target: str, extension: str, pillow_format: str, size
) -> None:
    """每一种新格式都走一遍真接口，并**真的打开下载到的文件**（§五十五）。

    只看 HTTP 200 说明不了任何事：转出一张损坏的图同样是 200。
    """
    payload = build_image_bytes(size[0], size[1], PILLOW_FORMAT[name.rsplit(".", 1)[-1]])

    snapshot = run_conversion(
        client, files=image_files((name, payload)), target_type=target
    )
    assert snapshot["status"] == "completed", snapshot
    task = conversion_task(snapshot, 0)
    assert task["status"] == "completed", task

    # 下载文件名要跟着目标格式走，不能还是源扩展名
    assert task["result"]["filename"] == f"photo{extension}"

    response = client.get(task["result"]["download_url"])
    assert response.status_code == 200
    assert response.headers["content-type"] != "application/octet-stream", (
        "新格式的 MIME 没接上，浏览器会把它当未知文件下载"
    )

    with Image.open(io.BytesIO(response.content)) as image:
        assert image.format == pillow_format
        if pillow_format == "ICO":
            # ICO 是容器：等比缩略，所以断言「没被放大、长宽比还在」
            assert max(image.size) <= 256
            assert image.size[0] <= size[0] and image.size[1] <= size[1]
        else:
            assert image.size == size


def test_a_batch_of_new_formats_comes_back_as_a_valid_zip(client: TestClient) -> None:
    """四种新源格式混在一批里：ZIP 要真的能解开，每个成员都要真的是图。"""
    sources = (
        ("a.bmp", build_image_bytes(120, 80, "BMP")),
        ("b.gif", build_image_bytes(140, 90, "GIF")),
        ("c.tiff", build_image_bytes(160, 100, "TIFF")),
        ("d.jpg", build_image_bytes(180, 110, "JPEG")),
    )

    snapshot = run_conversion(client, files=image_files(*sources), target_type="png")
    assert snapshot["status"] == "completed"
    assert snapshot["completed"] == 4

    response = client.get(snapshot["result"]["download_url"])
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert archive.testzip() is None
        assert sorted(archive.namelist()) == ["a.png", "b.png", "c.png", "d.png"]
        for member in sorted(archive.namelist()):
            with Image.open(io.BytesIO(archive.read(member))) as image:
                assert image.format == "PNG", member


def test_a_renamed_file_is_rejected_over_http(client: TestClient) -> None:
    """扩容不改安全策略：假扩展名在**真接口**上也必须被内容校验拦下。"""
    snapshot = run_conversion(
        client,
        files=image_files(("fake.bmp", build_image_bytes(120, 80, "PNG"))),
        target_type="jpg",
    )

    assert snapshot["status"] == "failed"
    task = conversion_task(snapshot, 0)
    assert task["status"] == "failed"
    assert task["error_code"] in (ErrorCode.INVALID_FILE_TYPE, ErrorCode.CORRUPTED_FILE)
    assert task["error_message"]
    # 提示语里不能出现服务器路径或内部模块名
    assert ":\\" not in task["error_message"]
    assert "PIL" not in task["error_message"]


def test_ico_cannot_be_uploaded_over_http(client: TestClient) -> None:
    """ICO 只出不进（§七）：上传要在第一道闸就被拒。"""
    icon = io.BytesIO()
    _sample("ico").save(icon, format="ICO")

    snapshot = run_conversion(
        client,
        files=image_files(("favicon.ico", icon.getvalue())),
        target_type="png",
    )

    assert snapshot["status"] == "failed"
    task = conversion_task(snapshot, 0)
    assert task["status"] == "failed"
    assert task["error_code"] in (ErrorCode.INVALID_FILE_TYPE, ErrorCode.CORRUPTED_FILE)


def test_every_new_pair_in_the_matrix_really_converts(client: TestClient) -> None:
    """矩阵里登记的新组合必须**真的都能转出来** —— 一条都不能是「登记了但转不了」。

    这是「配置驱动」最容易翻车的地方：registry 里加了一行，分发层却不认识，
    用户点了得到一个「服务器缺少组件」的报错。逐条真跑一遍是唯一能保证的方法 ——
    只提交不看结果的话，失败会藏在任务状态里照样通过。

    第十阶段 A 加进来的 ``heic`` 目标需要 ``pillow-heif``，所以那一半
    单独放在 :func:`test_every_heic_pair_really_converts` 里并带 skip 条件 ——
    这台机器上有编解码器不代表每一台都有，把可选组件塞进这条必过的断言里
    会让整个测试套件变成「只有装了 pillow-heif 才跑得绿」。
    """
    #: 新源格式 → 它应该能转到的全部目标（与 registry 逐项一致）
    expected = {
        "bmp": {"jpg", "png", "webp", "gif", "tiff", "heic", "pdf"},
        "gif": {"jpg", "png", "webp", "bmp", "tiff", "heic", "pdf"},
        "tiff": {"jpg", "png", "webp", "bmp", "gif", "heic", "pdf"},
    }
    #: 这一批不需要任何可选组件（解码走 Pillow 本体），所以照旧全跑。
    component_free = {"jpg", "png", "webp", "bmp", "gif", "tiff", "pdf"}
    checked = 0
    for source, targets in expected.items():
        assert set(registry.TARGETS_BY_SOURCE[source]) == targets, source
        payload = build_image_bytes(120, 80, source.upper())
        for target in sorted(targets & component_free):
            snapshot = run_conversion(
                client,
                files=image_files((f"photo.{source}", payload)),
                target_type=target,
            )
            task = conversion_task(snapshot, 0)
            assert task["status"] == "completed", (source, target, task["error_message"])
            assert task["conversion_id"] == f"image.{source}-to-{target}"
            checked += 1
    assert checked == 18, "新矩阵的行数变了，请一起更新这张表"

    # ICO 只有 PNG 这一个来源（§七 字面）
    png = build_image_bytes(120, 80, "PNG")
    snapshot = run_conversion(
        client, files=image_files(("photo.png", png)), target_type="ico"
    )
    task = conversion_task(snapshot, 0)
    assert task["status"] == "completed"
    assert task["conversion_id"] == "image.png-to-ico"

    # 反过来：别的源转 ICO 不是一条能力。
    #
    # 注意它**不是提交时被拒**的：提交时服务器还不知道每个文件的真实类型
    # （§十七 用后端判定，不看扩展名），所以这一项会照常入队、
    # 在识别出源类型之后被判为不支持 —— 这是既有的逐项失败模型（§五），
    # 不是「悄悄转出个坏文件」。
    snapshot = run_conversion(
        client,
        files=image_files(("photo.jpg", build_image_bytes(120, 80, "JPEG"))),
        target_type="ico",
    )
    assert snapshot["status"] == "failed"
    task = conversion_task(snapshot, 0)
    assert task["error_code"] == ErrorCode.UNSUPPORTED_CONVERSION
    assert "ICO" in task["error_message"]
    assert task["result"] is None


#: 这台机器上 HEIC 编解码器到底有没有。两个方向分开判断 ——
#: 只有解码器（libde265）的部署能读不能写，那是 §六 点名的真实状态。
_HEIF = heif_support()


@pytest.mark.skipif(
    not (_HEIF.decode and _HEIF.encode),
    reason=f"服务器没有 HEIC 编解码组件：{_HEIF.reason}",
)
def test_every_heic_pair_really_converts(client: TestClient) -> None:
    """HEIC 的十三条格子逐条真跑：七条解码（HEIC → 别的）+ 六条编码。

    与上一条同样的道理，只是这一批要 ``pillow-heif``，所以带 skip 条件。
    **skip 不是放水**：没有编解码器的机器本来就该看不到这些格子
    （见 ``registry.available_matrix``），拿它当失败反而是错的。
    真正要防的是「装了组件、格子也发布了、点下去却转不出来」——
    那种情况在这台机器上必须被这条测试抓住。

    每条都**真的打开结果**验证，不只看任务状态：状态是 completed
    但文件是空的那种 bug，只有打开才看得见。
    """
    #: HEIC 作为源：七个目标（§五 解码方向）
    decode_targets = {"jpg", "png", "webp", "bmp", "gif", "tiff", "pdf"}
    assert set(registry.TARGETS_BY_SOURCE[registry.SOURCE_HEIC]) == decode_targets

    heic_payload = build_image_bytes(120, 80, "HEIC")
    for target in sorted(decode_targets):
        snapshot = run_conversion(
            client,
            files=image_files(("photo.heic", heic_payload)),
            target_type=target,
        )
        task = conversion_task(snapshot, 0)
        assert task["status"] == "completed", (target, task["error_message"])
        assert task["conversion_id"] == f"image.heic-to-{target}"
        _assert_real_output(client, task, target)

    #: 其余六种位图 → HEIC（§六 编码方向）。``svg`` 不在此列 ——
    #: 它的目标是 §九 字面那四个，不含 HEIC（见 ``registry._SVG_SOURCES``）。
    encode_sources = {"jpg", "png", "webp", "bmp", "gif", "tiff"}
    for source in sorted(encode_sources):
        assert registry.TARGET_HEIC in registry.TARGETS_BY_SOURCE[source], source
        payload = build_image_bytes(120, 80, source.upper())
        snapshot = run_conversion(
            client,
            files=image_files((f"photo.{source}", payload)),
            target_type="heic",
        )
        task = conversion_task(snapshot, 0)
        assert task["status"] == "completed", (source, task["error_message"])
        assert task["conversion_id"] == f"image.{source}-to-heic"
        _assert_real_output(client, task, "heic")


# ----------------------------------------------------------------------
# 7b. 每一种图片源都能转成 PDF（真跑，不只是断言矩阵里有那一格）
# ----------------------------------------------------------------------

#: 每一种图片源转 PDF 时，结果里该不该出现「重新编码」那句说明。
#:
#: 这张表是**实测**的结果，与 ``image_to_pdf._EMBEDDABLE_FORMATS`` 是
#: 同一件事的两种说法：MuPDF 嵌得进去的直通（无说明），嵌不进去的转码。
_PDF_TRANSCODE_EXPECTATION = {
    "jpg": False,
    "png": False,
    "bmp": False,
    "gif": False,
    "tiff": False,
    "webp": True,   # PyMuPDF 认不出 WEBP
    "heic": True,   # PyMuPDF 认不出 HEIF
}


def test_every_image_source_converts_to_pdf(client: TestClient) -> None:
    """七种图片源**逐个真转一份 PDF**。

    这条测试的由来是一个已经上线的 bug：``webp → pdf`` 从第九阶段起
    就在能力矩阵里、在界面按钮上，点下去却永远是
    「无法把 xxx.webp 放入 PDF」—— PyMuPDF 的 ``insert_image``
    根本不认 WEBP，而当时没有任何测试真的跑过这一格。
    只断言「矩阵里有 webp → pdf」是查不出这件事的，
    矩阵那一格本来就是对的，错的是它下面没有能跑通的实现。

    所以这里逐个真跑，并且**真的用 PyMuPDF 打开结果**，
    确认页面上确实有一张图（而不是一页空白 PDF）。
    """
    from pdf.image_to_pdf import _EMBEDDABLE_FORMATS

    for source, expect_transcode in _PDF_TRANSCODE_EXPECTATION.items():
        if source == "heic" and not (_HEIF.decode and _HEIF.encode):
            continue  # 没有编解码器就产不出这张样张，跳过（见上面的 skip 说明）
        assert "pdf" in registry.TARGETS_BY_SOURCE[source], source

        payload = build_image_bytes(120, 80, source.upper())
        snapshot = run_conversion(
            client,
            files=image_files((f"photo.{source}", payload)),
            target_type="pdf",
        )
        task = conversion_task(snapshot, 0)
        assert task["status"] == "completed", (source, task["error_message"])
        assert task["conversion_id"] == f"image.{source}-to-pdf"
        _assert_real_output(client, task, "pdf")

        notes = task["result"]["notes"]
        transcoded = any("重新编码" in note for note in notes)
        assert transcoded is expect_transcode, (source, notes)
        # 这份期望必须与编码层那张表同源：源类型 → Pillow 容器名 →
        # 在不在 ``_EMBEDDABLE_FORMATS`` 里。写错一边（例如把 webp 标成
        # 不需要转码）会当场红，而不是等真机。
        container = (normalize_format(source) or source).upper()
        assert expect_transcode is (container not in _EMBEDDABLE_FORMATS), source


def test_the_embeddable_format_table_is_still_true() -> None:
    """``_EMBEDDABLE_FORMATS`` 这张表要**当场重测**，不能靠注释里的旧结论。

    它是「PyMuPDF 能嵌哪些容器」的一份快照。MuPDF 升级后支持了 WEBP 的话，
    这张表就变得过于保守 —— 还能跑，但每一张 WEBP 都会白掉一代画质。
    反过来（表里写了其实不支持的格式）后果严重得多：用户拿到
    「无法放入 PDF」。所以这里逐格式真的 ``insert_image`` 一次。

    ``_EMBEDDABLE_FORMATS`` 里写死的每一个都必须真的能嵌进去；
    表外被点名的两个（WEBP / HEIF）必须真的嵌不进去。两头都钉住，
    表既不能少写也不能多写。
    """
    from pdf.image_to_pdf import _EMBEDDABLE_FORMATS

    image = Image.new("RGB", (60, 40), (10, 120, 200))
    other_sources = {"WEBP"}
    if _HEIF.decode:
        other_sources.add("HEIF")

    for container in sorted(_EMBEDDABLE_FORMATS | other_sources):
        buffer = io.BytesIO()
        image.save(buffer, format=container)
        doc = pymupdf.open()
        try:
            page = doc.new_page(width=100, height=100)
            if container in _EMBEDDABLE_FORMATS:
                page.insert_image(
                    pymupdf.Rect(0, 0, 50, 50), stream=buffer.getvalue()
                )
            else:
                with pytest.raises(Exception):
                    page.insert_image(
                        pymupdf.Rect(0, 0, 50, 50), stream=buffer.getvalue()
                    )
        finally:
            doc.close()


# ----------------------------------------------------------------------
# 8. 旧接口的向后兼容
# ----------------------------------------------------------------------

def test_legacy_convert_endpoint_keeps_its_three_targets() -> None:
    """``/api/convert`` 的目标仍然只有三种。

    编码层现在认识七种格式，但旧接口的白名单**没有跟着放宽**：
    旧前端的目标下拉里只有那三个，多出来的格式对它没有意义，
    悄悄放开只会让既有脚本拿到与预期不同的结果。
    """
    assert LEGACY_TARGET_FORMATS == ("jpeg", "png", "webp")
    assert set(LEGACY_TARGET_FORMATS) <= set(OUTPUT_FORMATS)


def test_legacy_upload_whitelist_widened_with_the_new_formats() -> None:
    """反过来，旧接口的**上传**白名单确实放宽了 —— 这是一处有意的扩张。

    扩容前前后后都说清楚：用户拿一张 BMP 去「格式压缩」页面，
    以前得到「暂不支持该文件格式」，现在能正常压缩并转成 PNG。
    这不是行为倒退，但确实是一项既有页面上的变化，报告里如实列出。
    """
    for ext in (".bmp", ".gif", ".tif", ".tiff"):
        assert ext in settings.ALLOWED_IMAGE_EXTENSIONS
    # 旧接口的三种目标一个都没少
    for ext in (".jpg", ".jpeg", ".png", ".webp"):
        assert ext in settings.ALLOWED_IMAGE_EXTENSIONS
