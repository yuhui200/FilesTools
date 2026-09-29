"""第十阶段 A §五–§八：HEIC / HEIF。

这一组测的不是「HEIC 能不能转」—— 那件事在 ``test_image_formats.py``
里逐格真跑过了。这里测的是**它在各种组件状态下的行为是否诚实**，
因为 HEIC 与前面七种格式有一个根本区别：

    Pillow 12 本体**没有**任何 HEIC 代码。它的可用性取决于一个可选包
    （``pillow-heif``）在那台机器上装没装、装的那个版本带了解码器还是
    连编码器一起带。

于是「能转」与「不能转」之间多出两个中间态，而 §六 / §八 对它们的
要求只有一句话：**不要伪装**。所以这里的测试围着三件事：

1. **探测必须真的探测**（``_probe``）—— 尤其是「只有解码器」那一格。
   如果解码能力是拿自己的编码能力试出来的，一台只有 libde265 的服务器
   会被报成「解码也不行」，那条「只发布解码方向」的规矩就永远走不到。
   为此 ``compressors.heif`` 里内嵌了一份 454 字节的真 HEIC 样张。
2. **缺组件要说真话**（``validate_image_upload``）—— 不能退回那句
   「暂不支持该文件格式」，因为那是假话：HEIC 在支持范围里。
3. **文件头识别要经得起伪装**（``utils.validation``）—— HEIF 没有固定
   前缀，盒子大小可变，判据必须落在 offset 4 的 ``ftyp`` 与后面的
   brand 上，且**不能**把 AVIF 收进来（brand 是 ``avif``）。
"""

from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from compressors import heif
from compressors.heif import (
    PILLOW_FORMAT,
    HeifSupport,
    heif_support,
)
from conversion import registry
from services import conversion_service
from utils.errors import UnsupportedTypeError
from utils.validation import (
    HEIC_COMPONENT_MISSING_MESSAGE,
    _MAGIC_FOLLOWUPS,
    _MAGIC_SIGNATURES,
    sniff_format,
    validate_image_upload,
)
from tests.conftest import build_image_bytes

#: 这台机器上到底有没有编解码器。下面凡是**真跑**的用例都要看它 ——
#: 没有组件的机器上这些用例该 skip 而不是 fail（§八十二 要求
#: 「可用性回退 + 测试」，回退路径本身由后面的 monkeypatch 用例覆盖）。
_SUPPORT = heif_support()

_needs_decode = pytest.mark.skipif(
    not _SUPPORT.decode, reason=f"没有 HEIC 解码器：{_SUPPORT.reason}"
)
_needs_encode = pytest.mark.skipif(
    not _SUPPORT.encode, reason=f"没有 HEIC 编码器：{_SUPPORT.reason}"
)


def _heic_bytes() -> bytes:
    """一份真的 HEIC（走 Pillow，不走被测代码）。"""
    return build_image_bytes(64, 48, "HEIC")


def _write(tmp_path, name: str, data: bytes):
    path = tmp_path / name
    path.write_bytes(data)
    return path


# ----------------------------------------------------------------------
# 1. 探测：四个状态，一个都不能少
# ----------------------------------------------------------------------

def test_the_probe_reports_a_real_package_version() -> None:
    """探测结果里的版本号来自真实的包，不是写死的字符串。

    版本号是要给人看的（排查「为什么这台机器转不了 HEIC」时第一句话），
    所以它必须真的读出来 —— 写死一个 ``"1.8.0"`` 在升级之后就变成假话。
    """
    if not _SUPPORT.any:
        pytest.skip(f"没有 HEIC 组件：{_SUPPORT.reason}")

    import pillow_heif

    assert _SUPPORT.package_version == pillow_heif.__version__
    assert _SUPPORT.package_version  # 非空
    assert _SUPPORT.libheif_version, "libheif 版本探测不到"


def test_the_decode_probe_does_not_go_through_our_own_encoder() -> None:
    """解码能力必须由**内嵌的样张**测出来，不能拿自己的编码结果去试。

    这条测试是防一个具体的设计错误。第一版的 ``_probe()`` 是这样写的：
    编码一张小图 → 把编出来的字节读回来 → 读得回来就算解码也支持。
    在一台既有 libde265 又有 libx265 的机器上它是对的；
    在一台**只有 libde265** 的机器上，编码那一步先失败，于是
    ``decode`` 被报成 ``False`` —— 而这正是 §六 点名的那种部署，
    它本该「只发布解码方向」。

    所以判据是：内嵌样张能被解出来，且它是一份**固化的** base64 常量，
    不是运行时生成的 —— 生成就意味着又绕回编码器了。
    """
    sample = heif._HEIF_PROBE_SAMPLE
    # ISOBMFF 结构：前四字节是盒子大小（变长），``ftyp`` 必须在 offset 4
    assert sample[4:8] == b"ftyp", "内嵌样张不是 ISOBMFF 容器"
    assert sample[8:12] == b"heic", "内嵌样张的 brand 不是 heic"

    if not _SUPPORT.decode:
        pytest.skip(f"没有解码器：{_SUPPORT.reason}")
    with Image.open(io.BytesIO(sample)) as img:
        img.load()
        assert img.format == PILLOW_FORMAT


def test_a_decode_only_build_reports_decode_true_and_encode_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """只有解码器时，探测结果必须是 ``decode=True, encode=False``。

    ``libde265``（解码）与 ``libx265``（编码）是两个独立的库，前者体积
    0.9 MB、后者 22 MB，所以「只装解码」是真实存在的部署选择。
    这一格存在的意义就是让 §六「只发布解码方向」有落点。
    """
    monkeypatch.setattr(heif, "_probe", lambda: HeifSupport(
        decode=True, encode=False, package_version="1.8.0", libheif_version="1.23.4",
        reason="服务器上的 HEIC 组件不支持编码，其它格式转 HEIC 暂时不可用。",
    ))
    monkeypatch.setattr(heif, "_cached", None)

    support = heif.heif_support()
    assert support.decode is True
    assert support.encode is False
    assert support.any is True
    assert "不支持编码" in support.reason


def test_an_encode_only_build_is_reported_honestly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """只有编码器：``decode=False``。能写不能读，同样要说清楚。"""
    monkeypatch.setattr(heif, "_probe", lambda: HeifSupport(
        decode=False, encode=True, reason="服务器上的 HEIC 组件不支持解码，HEIC 图片暂时无法转换。",
    ))
    monkeypatch.setattr(heif, "_cached", None)

    support = heif.heif_support()
    assert support.decode is False
    assert support.encode is True
    assert "不支持解码" in support.reason


def test_a_missing_package_is_not_an_error() -> None:
    """包不在时 ``register()`` 只是安静地跳过，不抛异常。

    ``pillow-heif`` 是**可选**依赖：没装它，服务必须照常起来，
    只是 HEIC 那十三格不出现在能力矩阵里（§六）。
    """
    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(heif, "_cached", None)
        monkeypatch.setattr(
            heif, "_probe", lambda: HeifSupport(reason="未安装 pillow-heif")
        )
        support = heif.heif_support()
        assert support.any is False
        assert "pillow-heif" in support.reason
        # register() 是幂等的，重复调用不会炸
        heif.register()
        heif.register()
    finally:
        monkeypatch.undo()


def test_the_support_result_is_cached() -> None:
    """探测结果缓存起来 —— 它要开一次真实的编解码，不便宜。"""
    if not _SUPPORT.any:
        pytest.skip(f"没有 HEIC 组件：{_SUPPORT.reason}")
    first = heif_support()
    second = heif_support()
    assert first is second, "两次探测返回了不同的对象，缓存没生效"


# ----------------------------------------------------------------------
# 2. 文件头：盒子结构 + brand，且不能把 AVIF 收进来
# ----------------------------------------------------------------------

def test_sniff_format_recognises_a_real_heic(tmp_path) -> None:
    if not _SUPPORT.decode:
        pytest.skip(f"没有解码器：{_SUPPORT.reason}")
    path = _write(tmp_path, "a.heic", _heic_bytes())
    assert sniff_format(path) == "heif"


def test_heif_has_no_fixed_prefix_so_it_uses_a_followup() -> None:
    """HEIF 的判据不能写成固定前缀。

    ISOBMFF 的盒子大小在**前四个字节**，随文件而变；``ftyp`` 永远在
    offset 4。写成 ``startswith(b"....ftypheic")`` 这种形式会在
    任何大小 ≠ 24 的文件上失配 —— 而那是绝大多数真实文件。
    所以 ``_MAGIC_SIGNATURES["heif"]`` 是**空列表**（表示「没有固定前缀」），
    真正的判断在 ``_MAGIC_FOLLOWUPS["heif"]`` 里。
    """
    assert _MAGIC_SIGNATURES["heif"] == [], "HEIF 不该有固定前缀"
    assert "heif" in _MAGIC_FOLLOWUPS


def test_a_real_heic_is_not_rejected_by_a_changed_box_size(tmp_path) -> None:
    """把盒子大小改掉（模拟另一种封装器产出的文件），仍然要认得出来。"""
    if not _SUPPORT.decode:
        pytest.skip(f"没有解码器：{_SUPPORT.reason}")
    data = bytearray(_heic_bytes())
    # 前四字节是盒子大小，改成一个明显不同的值；ftyp 与 brand 不动。
    data[0:4] = (9999).to_bytes(4, "big")
    assert sniff_format(_write(tmp_path, "b.heic", bytes(data))) == "heif"


def test_avif_is_not_accepted_as_heic(tmp_path) -> None:
    """AVIF 与 HEIC 是同一个容器家族，但 brand 不同 —— 不能混为一谈。

    这台机器上 Pillow 能写 AVIF（实测），所以样张是真的 AVIF。
    收下它会让一个「明明不支持」的格式通过校验，然后在解码那一步
    抛一个用户看不懂的错。能力矩阵里没有 AVIF，上传口就不该放它进来。
    """
    buffer = io.BytesIO()
    try:
        Image.new("RGB", (16, 16), (200, 30, 30)).save(buffer, format="AVIF")
    except Exception:  # pragma: no cover - 这台机器没 AVIF 编码器时
        pytest.skip("这台机器写不出 AVIF 样张")
    data = buffer.getvalue()
    assert data[4:8] == b"ftyp" and data[8:12] == b"avif", "样张不是 AVIF"

    path = _write(tmp_path, "fake.heic", data)
    with pytest.raises(UnsupportedTypeError):
        validate_image_upload(path, "fake.heic")


def test_other_images_renamed_to_heic_are_rejected(tmp_path) -> None:
    """JPEG / PNG 改名成 .heic：内容与扩展名对不上，必须拒（§四十五）。"""
    for fmt in ("JPEG", "PNG"):
        path = _write(tmp_path, f"fake_{fmt}.heic", build_image_bytes(32, 32, fmt))
        with pytest.raises(UnsupportedTypeError):
            validate_image_upload(path, f"fake_{fmt}.heic")


@_needs_decode
def test_a_real_heic_renamed_to_jpg_is_rejected(tmp_path) -> None:
    """反过来也一样：真 HEIC 改名成 .jpg 不能蒙混过关。"""
    path = _write(tmp_path, "photo.jpg", _heic_bytes())
    with pytest.raises(UnsupportedTypeError):
        validate_image_upload(path, "photo.jpg")


# ----------------------------------------------------------------------
# 3. 缺组件时说的是真话，不是「暂不支持这种格式」
# ----------------------------------------------------------------------

def test_missing_codec_says_so_instead_of_unsupported_format(monkeypatch, tmp_path) -> None:
    """缺组件时的文案必须是**组件**那句，不能退回「暂不支持该文件格式」。

    两句的差别是用户能做的事：前者是「换个时间/换个服务器再试」，
    后者是「这个格式没戏，另存为别的格式吧」。对着一个我们明明
    支持、只是这台机器缺组件的格式说后者，就是把用户往错的方向赶。
    """
    if not _SUPPORT.decode:
        pytest.skip("这台机器本来就没有解码器，两条路径分不开")
    data = _heic_bytes()

    # 先确认正常情况下它是能过的
    validate_image_upload(_write(tmp_path, "ok.heic", data), "ok.heic")

    monkeypatch.setattr(
        conversion_service, "heif_support", lambda: HeifSupport(reason="测试")
    )
    monkeypatch.setattr(heif, "heif_support", lambda: HeifSupport(reason="测试"))
    monkeypatch.setattr(heif, "_cached", HeifSupport(reason="测试"))

    path = _write(tmp_path, "photo.heic", data)
    with pytest.raises(UnsupportedTypeError) as excinfo:
        validate_image_upload(path, "photo.heic")
    assert str(excinfo.value) == HEIC_COMPONENT_MISSING_MESSAGE
    assert "HEIC" in str(excinfo.value)
    # 「暂不支持」那句**不能**出现在这里
    assert "暂不支持该文件格式" not in str(excinfo.value)


def test_heic_extensions_stay_whitelisted_without_the_codec() -> None:
    """没有编解码器时，``.heic`` 也**留在**扩展名白名单里。

    摘掉它的话，用户传 .heic 会在 ``check_extension`` 那一步就拿到
    「暂不支持该文件格式」—— 一句假话。留在白名单里，它才能走到
    最后一层、拿到那句真话（见 ``config.ALLOWED_IMAGE_EXTENSIONS`` 的说明）。
    """
    from config import settings

    assert ".heic" in settings.ALLOWED_IMAGE_EXTENSIONS
    assert ".heif" in settings.ALLOWED_IMAGE_EXTENSIONS


# ----------------------------------------------------------------------
# 4. 能力矩阵跟着探测结果走（端到端，走 HTTP）
# ----------------------------------------------------------------------

def test_capabilities_hide_every_heic_row_without_the_codec(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """没组件时 HEIC 相关的十三格**一个都不能是可用的**。

    两个方向都要查：``heic`` 不能作为**源**（读不了），
    也不能出现在任何一行的**目标**里（写不了）。

    ``conversions[]`` 与 ``matrix`` 的契约不同，这一点容易写错测试：
    ``matrix`` 是把不可用的行**去掉**，而 ``conversions[]`` 是
    全都列出来、每一条带一个 ``available`` 布尔 —— 前端要靠它把
    按钮置灰而不是让按钮消失（消失会让用户以为这个功能不存在，
    置灰配上 ``notes`` 里那句话才说得清「为什么」）。
    所以这里断言的不是「条目不在」，而是「没有一条是 available」。
    """
    monkeypatch.setattr(
        conversion_service, "heif_support", lambda: HeifSupport(reason="测试")
    )

    body = client.get("/api/conversion/capabilities").json()
    assert registry.SOURCE_HEIC not in body["matrix"]
    for source, targets in body["matrix"].items():
        assert registry.TARGET_HEIC not in targets, source
    assert registry.TARGET_HEIC not in {item["value"] for item in body["targets"]}

    heic_entries = [
        entry
        for entry in body["conversions"]
        if entry["source_type"] == registry.SOURCE_HEIC
        or entry["target_type"] == registry.TARGET_HEIC
    ]
    # 条目本身还在（前端要能显示成「不可用」），但一条都不能可用
    assert heic_entries, "HEIC 条目整个消失了，前端的置灰逻辑就没得可置"
    assert not [entry for entry in heic_entries if entry["available"]]

    # 而且要说清楚为什么（§六：不能悄悄藏起来）
    assert any("HEIC" in note for note in body["notes"])


@_needs_decode
@_needs_encode
def test_capabilities_publish_thirteen_available_heic_entries_when_available(
    client: TestClient,
) -> None:
    """组件齐全时，HEIC 相关的条目正好十三条可用：七解码 + 六编码。"""
    body = client.get("/api/conversion/capabilities").json()
    entries = [
        entry
        for entry in body["conversions"]
        if entry["source_type"] == registry.SOURCE_HEIC
        or entry["target_type"] == registry.TARGET_HEIC
    ]
    assert len(entries) == 13
    assert all(entry["available"] for entry in entries)

    decode = [e for e in entries if e["source_type"] == registry.SOURCE_HEIC]
    encode = [e for e in entries if e["target_type"] == registry.TARGET_HEIC]
    assert len(decode) == 7
    assert len(encode) == 6
    # heic → heic 不存在（同格式不转）
    assert not [e for e in entries if e["id"] == "image.heic-to-heic"]


# ----------------------------------------------------------------------
# 5. 真的转得动（往返，结果用 Pillow 打开）
# ----------------------------------------------------------------------

@_needs_encode
def test_encode_then_decode_round_trip() -> None:
    """写一份 HEIC 再读回来：尺寸与画面内容都对得上。

    直接调 ``encoder`` 这一层，不走 HTTP —— 端到端那条在
    ``test_image_formats.py`` 里，这条盯的是编码器自己的行为
    （``PILLOW_FORMAT`` 用的是 ``HEIF``，不是 ``HEIC``）。

    **断言写得有分寸**：HEVC 是有损的，而且对小图特别不客气 ——
    一张 48×32 的图上，一个孤立的异色像素会被涂抹开好几个像素。
    所以这里不逐像素比，而是验两件**真的有信息量**的事：
    大面积的底色要还原得准（±8），那个亮斑要比底色明显更亮。
    写死「原值 ±2」会在换一个 libx265 版本时变成假失败。
    """
    from compressors.encoder import encode_image
    from converters.image_converter import prepare_for_format

    base = (210, 90, 40)
    original = Image.new("RGB", (48, 32), base)
    # 画一块**够大**的亮斑（4×4），不用单个像素 —— 小到 1 像素的
    # 特征在 48×32 上会被量化掉，那时测试失败的原因就不是被测代码了。
    for x in range(20, 24):
        for y in range(12, 16):
            original.putpixel((x, y), (0, 255, 0))

    data = encode_image(prepare_for_format(original, "heif"), "heif", 95)
    assert data[4:8] == b"ftyp"

    with Image.open(io.BytesIO(data)) as back:
        back.load()
        assert back.format == PILLOW_FORMAT
        assert back.size == (48, 32)

        rgb = back.convert("RGB")
        corner = rgb.getpixel((2, 2))
        for channel, expected in zip(corner, base):
            assert abs(channel - expected) <= 8, (corner, base)
        # 亮斑要在，而且明显比底色绿
        spot = rgb.getpixel((21, 13))
        assert spot[1] > corner[1] + 60, (spot, corner)


@_needs_encode
def test_alpha_survives_a_heic_round_trip() -> None:
    """透明要留住 —— HEIC 支持 alpha（实测），所以它走的是保留通道那一支。

    ``prepare_for_format`` 里 HEIC 与 webp / gif / ico 同一类；
    如果谁把它挪进 JPEG / BMP 那一支（合成到白底），这条会红。
    """
    from compressors.encoder import encode_image
    from converters.image_converter import prepare_for_format

    original = Image.new("RGBA", (32, 32), (255, 0, 0, 0))
    original.putpixel((5, 5), (0, 0, 255, 255))

    data = encode_image(prepare_for_format(original, "heif"), "heif", 95)
    with Image.open(io.BytesIO(data)) as back:
        back.load()
        assert back.mode in ("RGBA", "LA"), back.mode
        alpha = back.convert("RGBA").getchannel("A")
        assert alpha.getpixel((0, 0)) < 40, "本来是透明的像素变不透明了"
        assert alpha.getpixel((5, 5)) > 200, "本来不透明的像素变透明了"


@_needs_encode
def test_dpi_is_not_advertised_for_heic() -> None:
    """HEIC 不给 DPI 旋钮。

    Pillow 收下 ``dpi=`` 却不生效（实测：不报错，读回来 ``info['dpi']``
    是 ``None``）。**「不报错但不生效」比报错更糟** —— 用户以为设上了。
    所以 ``DENSITY_FORMATS`` 里没有它，界面上也就不该有那个控件。
    """
    from compressors.encoder import DENSITY_FORMATS, OUTPUT_FORMATS

    assert "heif" in OUTPUT_FORMATS
    assert "heif" not in DENSITY_FORMATS


# ----------------------------------------------------------------------
# 6. 清除元数据必须真的清掉
# ----------------------------------------------------------------------

#: 一份带 ``tiff:Model="LeakCam"`` 的 XMP —— 内容是什么不重要，
#: 只要它整段可辨认，好断言「漏没漏」。
_XMP_PROBE = (
    '<x:xmpmeta xmlns:x="adobe:ns:meta/">'
    '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
    '<rdf:Description tiff:Model="LeakCam" xmlns:tiff="http://ns.adobe.com/tiff/1.0/"/>'
    "</rdf:RDF></x:xmpmeta>"
)


def _png_carrying_metadata() -> bytes:
    """一张 **EXIF 与 XMP 都带**的 PNG。

    用 PNG 而不是 JPEG 是有讲究的：Pillow 把 PNG 的 XMP 放在
    ``XML:com.adobe.xmp`` 这个键上（不是 ``xmp``），而 pillow-heif
    两个键都认。这条路径正是「用户以为清干净了、其实整段 XMP 进了结果文件」
    最容易漏掉的那一条。
    """
    from PIL import PngImagePlugin

    image = Image.new("RGB", (64, 48), (10, 120, 200))
    chunk = PngImagePlugin.PngInfo()
    chunk.add_itxt("XML:com.adobe.xmp", _XMP_PROBE, zip=False)
    exif = image.getexif()
    exif[0x010F] = "LeakMake"
    exif[0x0110] = "LeakModel"
    buffer = io.BytesIO()
    image.save(buffer, "PNG", pnginfo=chunk, exif=exif.tobytes())
    return buffer.getvalue()


def _carried_metadata(path) -> tuple[str | None, bytes]:
    """回读结果文件里**跟着走**的拍摄信息：(相机型号, XMP 原始字节)。"""
    with Image.open(path) as image:
        model = image.getexif().get(0x0110)
        xmp = image.info.get("xmp") or image.info.get("XML:com.adobe.xmp") or b""
    return model, xmp


@_needs_encode
def test_heic_metadata_remove_really_removes(tmp_path) -> None:
    """选了「清除元数据」，HEIC 结果里就必须真的没有元数据。

    **这条测试是有来历的**：pillow-heif 的 Pillow 插件在
    ``_pil_encode_image`` 里**无条件**执行
    ``info["exif"] = _exif_from_pillow(img)`` 与
    ``info["xmp"] = _xmp_from_pillow(img)`` —— 它自己去图片对象上抓。
    于是「不传 ``exif=``」在别的格式上等于「不写」，在 HEIC 上却等于
    「照抄源文件」：相机型号与整段 XMP（可能含 GPS）静默进了结果文件，
    而 ``notes`` 里一个字都没有。

    这比抛错难发现得多，也正是 §三十三 说的「不要宣传清除，除非测过」。
    修法在 ``compressors.encoder.build_extra``：对这类格式**显式**给一个
    空 EXIF 并把 ``xmp`` 置 ``None``。

    断言同时覆盖 ``keep`` —— 否则「清掉了」可能只是因为整条链路压根
    不带元数据，那这条测试就是假绿。
    """
    from compressors.pipeline import PipelineOptions, run_pipeline

    source = _write(tmp_path, "carrier.png", _png_carrying_metadata())

    kept = run_pipeline(
        source, PipelineOptions(target_format="heif", quality_value=85, metadata="keep")
    )
    kept_path = _write(tmp_path, "kept.heic", kept.data)
    model, xmp = _carried_metadata(kept_path)
    assert model == "LeakModel", "keep 没保住元数据，下面的 remove 断言就不算数"
    assert xmp, "keep 没保住 XMP，下面的 remove 断言就不算数"

    dropped = run_pipeline(
        source,
        PipelineOptions(target_format="heif", quality_value=85, metadata="remove"),
    )
    dropped_path = _write(tmp_path, "dropped.heic", dropped.data)
    model, xmp = _carried_metadata(dropped_path)
    assert model is None, f"清了元数据，相机型号却还在：{model!r}"
    assert xmp == b"", f"清了元数据，XMP 却还在（{len(xmp)} 字节）"


def test_not_passing_exif_means_not_writing_it_in_every_format(tmp_path) -> None:
    """「不传 ``exif=``」在**每一种**输出格式上都必须等于「不写 EXIF」。

    不针对 HEIC 写死：这条是通用的不变式，逐格式实测。将来 Pillow 或某个
    插件改了行为（又多一个自己去图片对象上抓元数据的格式），这里会红，
    而不是等用户在报告里发现自己的相机型号被写进了输出。

    做法是把元数据**直接挂在待编码的图片对象上**，再让 ``build_extra``
    按「没有 EXIF」去构造参数 —— 这正是插件「自己去抓」时看的地方。
    """
    from compressors.encoder import (
        OUTPUT_FORMATS,
        build_extra,
        encode_image,
        supports_exif,
    )
    from converters.image_converter import prepare_for_format

    carrier = Image.open(io.BytesIO(_png_carrying_metadata()))
    carrier.load()
    raw_exif = carrier.info.get("exif")
    assert raw_exif, "样张自己没带上 EXIF，下面整轮都没意义"

    for target in OUTPUT_FORMATS:
        work = prepare_for_format(carrier, target)
        # 模拟「图片对象上带着元数据」：两条抓取路径都铺上
        work.info["exif"] = raw_exif
        work.info["xmp"] = _XMP_PROBE
        work.info["XML:com.adobe.xmp"] = _XMP_PROBE

        # **正向对照**：先证明这一格式真的写得进 EXIF。没有这一步，
        # 下面「写不进去」的断言可能只是因为这条链路压根不带元数据 ——
        # 那就是假绿（尤其是 bmp / gif / ico，它们本来就带不了）。
        if supports_exif(target):
            written = encode_image(
                work, target, 90, extra=build_extra(target, exif=raw_exif, dpi=None)
            )
            written_path = _write(tmp_path, f"probe.{target}", written)
            probe_model, _ = _carried_metadata(written_path)
            assert probe_model == "LeakModel", (
                f"{target}：显式给了 EXIF 都没写进去，下面那条断言证明不了什么"
            )

        data = encode_image(
            work, target, 90, extra=build_extra(target, exif=None, dpi=None)
        )
        path = _write(tmp_path, f"plain.{target}", data)
        model, xmp = _carried_metadata(path)
        assert model is None, f"{target}：没给 EXIF，结果里却有相机型号 {model!r}"
        assert xmp == b"", f"{target}：没给 XMP，结果里却有 {len(xmp)} 字节 XMP"



# ----------------------------------------------------------------------
# 6. 编码失败时，用户看到的那句话里不许有第三方库的原文
#    （第十阶段 C §十：用户输出与技术细节分离）
# ----------------------------------------------------------------------

#: 用户可见文本里**绝不允许**出现的东西。这份清单与
#: ``test_conversion_api.FORBIDDEN_FRAGMENTS`` 是同一个思路，但**故意单独写**：
#: 那边管的是整条 HTTP 响应（含快照），这边管的是编码器抛出的一句话，
#: 两者将来要加的词不一定相同（比如这里的 ``.py`` 更针对异常文本）。
_FORBIDDEN_IN_MESSAGE = (
    "C:\\",
    "D:\\",
    "/home/",
    "AppData",
    "site-packages",
    "Traceback",
    ".py",
    "filetools_",
    "OSError",
    "ValueError",
    "libheif",
    "pillow_heif",
)


def test_a_third_party_error_never_reaches_the_user(tmp_path) -> None:
    """编码器抛什么，用户都不该看到原文 —— 只看到一句中文。

    **为什么要注入一个假的异常**：实测今天 libheif 失败时给的是
    ``ValueError: Invalid parameter value`` 这种干净文本，但「今天干净」
    不是一条能长期依赖的性质 —— 它取决于第三方库当前版本的措辞，
    换一个版本就可能变成带临时目录的 ``OSError``。所以这里**故意**造一个
    带服务器路径的 ``OSError``，验的是「不管对方抛什么，这道闸都拦得住」，
    而不是「今天恰好没漏」。

    三种异常都过一遍：``OSError``（带路径，最坏情况）、``ValueError``
    （今天的真实形态）、``RuntimeError``（没见过的第三种）。
    ``KeyError`` 不在其中 —— 它走的是上面那条「缺组件」的专用分支，
    由下一条测试覆盖。
    """
    from compressors.encoder import _encode_heif
    from utils.errors import ProcessingError

    #: 一个「最坏情况」的第三方异常：带服务器绝对路径的 OSError。
    #: 用原始字符串写，免得反斜杠在源码里先被 Python 自己解析一遍。
    leaky = {
        OSError: OSError(
            "[Errno 22] 无法写入 "
            r"C:\Users\Administrator\AppData\Local\Temp\filetools_ab12cd\out.heic"
        ),
        ValueError: ValueError("Invalid parameter value"),
        RuntimeError: RuntimeError("libheif: heif_context_write failed"),
    }

    image = Image.new("RGB", (16, 16), (1, 2, 3))
    for kind, error in leaky.items():
        original_save = Image.Image.save

        def failing_save(self, fp, *args, _error=error, **kwargs):
            raise _error

        Image.Image.save = failing_save
        try:
            with pytest.raises(ProcessingError) as caught:
                _encode_heif(image, 85)
        finally:
            Image.Image.save = original_save

        message = caught.value.message
        assert message, f"{kind.__name__}：兜底消息是空的，用户会看到一个空白错误"
        for fragment in _FORBIDDEN_IN_MESSAGE:
            assert fragment not in message, (
                f"{kind.__name__} 的原文漏进了用户可见文本：{fragment!r} 出现在 {message!r}"
            )
        # 异常链要完整保留 —— 拦下来是给用户看的，不是把线索销毁掉。
        assert caught.value.__cause__ is error, (
            f"{kind.__name__}：技术细节必须留在 __cause__ 里，日志与排查还要用"
        )


def test_the_missing_component_message_is_not_the_generic_one() -> None:
    """没有编码器时给的是「缺组件」，不是「这张图编码不了」。

    两者的**下一步动作完全不同**：缺组件要找管理员装包（重传、换格式都没用），
    编码失败要换一种目标格式。给错了，用户会一直对着一个没问题的文件折腾。

    ``KeyError`` 是 Pillow 在 ``Image.SAVE`` 表里找不到 ``HEIF`` 时的真实行为，
    这里直接把它造出来，不依赖这台机器上装没装 ``pillow-heif``。
    """
    from compressors.encoder import _encode_heif
    from utils.errors import ProcessingError

    original_save = Image.Image.save

    def missing_format(self, fp, *args, **kwargs):
        raise KeyError("HEIF")

    Image.Image.save = missing_format
    try:
        with pytest.raises(ProcessingError) as caught:
            _encode_heif(Image.new("RGB", (16, 16)), 85)
    finally:
        Image.Image.save = original_save

    assert "组件" in caught.value.message, caught.value.message
    assert caught.value.__cause__ is not None
    for fragment in _FORBIDDEN_IN_MESSAGE:
        assert fragment not in caught.value.message, caught.value.message
