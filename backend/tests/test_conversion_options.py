"""选项词表与纯校验（第九阶段 §九 / §十 / §二十二）。

这一层最怕的不是「校验写漏了」，而是**词表与实现悄悄分叉**：
界面摆出「A5」，实现只认 ``a4``；或者 schema 允许 50000 像素，
解析器一看到就拒。两类问题的表现都是「用户点了没反应」，
所以这里的测试分三组：

1. **与实现对账** —— 枚举值、数值边界逐条与 ``pdf.image_to_pdf`` /
   ``compressors.encoder`` / ``routers.params`` / ``routers.pdf_params``
   里那份**已经存在**的常量比。词表是手写的字面量（§十三 要求注册表零 I/O，
   不能 import 实现），所以必须由测试来保证两者一致。
2. **不变式** —— ``schema`` 只允许比服务端更窄：``min >= server_min`` 且
   ``max <= server_max``。这条保证了「界面不会摆出一个必然被拒的输入框」，
   同时资源上限仍然由既有解析器把关（§四十五）。
3. **纯校验的行为** —— 非法质量 / 非法宽高 / 零 / 负数 / 极端值 /
   NaN / 未知键，一个都不能放过。
"""

from __future__ import annotations

import math

import pytest

from compressors.encoder import OUTPUT_FORMATS
from config import settings
from conversion import options as opts
from conversion import registry
from conversion.capability import DYNAMIC_FONTS, OPTION_TYPES, OptionSpec
from pdf.image_to_pdf import FITS, MARGINS, ORIENTATIONS, PAGE_SIZES
from routers import params, pdf_params

# ----------------------------------------------------------------------
# 服务端的真实边界（不是抄 schema，是去实现里取）
# ----------------------------------------------------------------------

#: 选项键 -> 服务端解析器允许的区间。
#:
#: ``dpi.custom`` 是唯一一个「服务端边界也由 options.py 拥有」的键：
#: 图片 DPI 只是写进文件的密度信息，Pillow 本身没有上限，
#: 所以这个范围由 :mod:`conversion.options` 定义、解析器复用同一对常量，
#: 不存在第二份真相可言。
SERVER_BOUNDS: dict[str, tuple[float, float]] = {
    "quality": (1, 100),                                   # parse_quality_value
    "resize.width": (params.MIN_EDGE, params.MAX_EDGE),    # parse_edge
    "resize.height": (params.MIN_EDGE, params.MAX_EDGE),   # parse_edge
    "dpi.custom": (opts.MIN_DPI, opts.MAX_DPI),
    "page_size.width_mm": (pdf_params._MIN_CUSTOM_MM, pdf_params._MAX_CUSTOM_MM),
    "page_size.height_mm": (pdf_params._MIN_CUSTOM_MM, pdf_params._MAX_CUSTOM_MM),
    "font_size": (settings.TXT_MIN_FONT_SIZE, settings.TXT_MAX_FONT_SIZE),
    # ---- 第十阶段 A ----
    # 裁剪的四个几何键。下界由 ``compressors.cropper`` 直接判（x/y 非负、
    # 宽高大于 0）；上界没有常量可引用 —— 真正的上界是**那张图自己的尺寸**，
    # 由 ``compute_crop_box`` 对着实际宽高判越界。这里写 MAX_IMAGE_EDGE
    # 是因为源图本身已被流水线限制在这个边长内，裁剪框不可能比它更大。
    "crop.x": (0, settings.MAX_IMAGE_EDGE),
    "crop.y": (0, settings.MAX_IMAGE_EDGE),
    "crop.width": (1, settings.MAX_IMAGE_EDGE),
    "crop.height": (1, settings.MAX_IMAGE_EDGE),
    # 百分比缩放：上界是确实由服务端强制的一条策略（validate_payload 用它），
    # 不是抄一遍 schema 了事 —— ``compute_target_size`` 只判「大于 0」。
    "resize.percent": (opts.MIN_PERCENT, opts.MAX_PERCENT),
    # 自定义旋转角：359 是「小于一整圈」的最大整数度。360 等价于不转，
    # 由流水线的 ``% 360`` 归一，词表不接受它以免出现两个表达同一件事的值。
    "rotation.angle": (opts.MIN_ROTATION_ANGLE, opts.MAX_ROTATION_ANGLE),
    # 目标大小（§二十九）。服务端边界**本身就是** config 里的两个策略常量
    # （``parse_target_bytes`` 直接引用它们），schema 也引用同一对常量 ——
    # 所以这里的意义不是「对账两份手抄的真值」，而是钉住「谁都不许另写一对」。
    # 下界 5 KB 是「比这更小的图没有意义」，上界 200 MB 是「别拿它当下载加速器」。
    "target_bytes": (settings.MIN_TARGET_BYTES, settings.MAX_TARGET_BYTES),
}


def _every_spec() -> list[OptionSpec]:
    """全部家族、全部目标下的每一个选项（去重后）。"""
    seen: dict[str, OptionSpec] = {}
    for source in registry.SOURCE_TYPES:
        for target in registry.TARGETS_BY_SOURCE[source]:
            entry = registry.capability_for(source, target)
            assert entry is not None
            for spec in entry.options:
                seen[spec.key] = spec
    return list(seen.values())


# ----------------------------------------------------------------------
# 1. 与实现对账
# ----------------------------------------------------------------------

def test_page_size_enum_matches_the_implementation() -> None:
    """图片 → PDF 的页面大小就是 ``pdf.image_to_pdf.PAGE_SIZES``。"""
    spec = opts._IMAGE_TO_PDF_OPTIONS[0]
    assert spec.key == "page_size"
    assert tuple(value for value, _ in spec.values) == PAGE_SIZES


def test_orientation_enum_matches_the_implementation() -> None:
    spec = opts._IMAGE_TO_PDF_OPTIONS[3]
    assert spec.key == "orientation"
    assert tuple(value for value, _ in spec.values) == ORIENTATIONS


def test_fit_enum_matches_the_implementation() -> None:
    spec = opts._IMAGE_TO_PDF_OPTIONS[5]
    assert spec.key == "fit"
    assert tuple(value for value, _ in spec.values) == FITS


def test_margin_enum_matches_the_implementation() -> None:
    spec = opts._IMAGE_TO_PDF_OPTIONS[4]
    assert spec.key == "margin"
    assert tuple(value for value, _ in spec.values) == MARGINS


def test_margin_labels_show_the_real_millimetres() -> None:
    """边距标签里的毫米数必须等于 ``settings.PDF_MARGINS`` 的真实值。

    第九阶段把边距从 18/36/72 磅改成了 5/10/20 毫米（§十）。
    标签是用户唯一能看到的数量说明 —— 它还写着「约 6 mm」而实际给 20 mm，
    就是一个界面上的谎话。
    """
    spec = next(s for s in opts._IMAGE_TO_PDF_OPTIONS if s.key == "margin")
    for value, label in spec.values:
        if value == "none":
            assert label == "无"
            assert settings.PDF_MARGINS[value] == 0.0
            continue
        millimetres = settings.PDF_MARGINS[value] / (72.0 / 25.4)
        assert f"{millimetres:.0f} 毫米" == label, (value, label, millimetres)


def test_lossy_targets_match_the_encoder() -> None:
    """「有损格式」的判定必须与编码器认的输出格式对得上。

    ``quality`` 只对有损格式出现。集合写错的表现是：
    PNG 用户看到一个质量滑杆（拖了没反应），或者 JPG 用户没有滑杆。

    HEIC 从第十阶段 A 起在这一格里，理由不是「它看起来像有损格式」，
    而是**实测**：同一张 384×384 细节图，
    q10→2988 / q30→32324 / q85→173219 / q100→198164 字节，
    质量参数对输出大小有强影响，所以这个滑杆不是摆设。
    """
    assert opts._LOSSY_TARGETS <= set(registry.TARGET_TYPES)
    for target in opts._LOSSY_TARGETS:
        assert registry.EXTENSION_BY_TARGET[target] in {
            ".jpg", ".jpeg", ".webp", ".heic",
        }, target
    # JPEG / WEBP / HEIC 在编码层是三种格式，一个都不能漏
    assert {"jpeg", "webp", "heif"} <= set(OUTPUT_FORMATS)
    assert len(opts._LOSSY_TARGETS) == 3


def test_density_and_exif_sets_are_subset_of_known_targets() -> None:
    """裁剪表里只能出现**认识**的格式名（写错一个字母就静默失效）。

    这条以前要拿一份 ``PENDING_TARGETS`` 豁免来放行「词表领先于矩阵」的
    格式；第九阶段第 8 步把矩阵补全之后豁免已经删掉，于是这里比的是
    **真实的**目标集合 —— ``tif`` / ``tiff`` 拼错一个字母当场就红。
    """
    known = set(registry.TARGET_TYPES)
    assert opts._LOSSY_TARGETS <= known
    assert opts._DENSITY_TARGETS <= known
    assert opts._EXIF_TARGETS <= known
    # 新登记的四种格式一个都不能混进这三张裁剪表
    assert not (opts._LOSSY_TARGETS & {"bmp", "gif", "tiff", "ico"})
    assert not (opts._EXIF_TARGETS & {"gif", "bmp", "ico"})
    # WebP 容器里没有密度字段，Pillow 也不接受 dpi 参数 —— 不能把它算进去
    assert "webp" not in opts._DENSITY_TARGETS
    # BMP 的文件头必须有一个密度字段，Pillow 会写它自己的默认值（96），
    # 所以「保持原样」在那里是句做不到的话 —— 不给这个旋钮，见 encoder 说明
    assert "bmp" not in opts._DENSITY_TARGETS
    assert "ico" not in opts._DENSITY_TARGETS


def test_target_gates_only_reference_known_keys() -> None:
    """裁剪表里的键必须真的存在于某个家族的词表里（写错一个字母就静默失效）。"""
    declared = {
        spec.key for specs in opts._FAMILY_OPTIONS.values() for spec in specs
    }
    assert set(opts._TARGET_GATED) <= declared
    assert len(opts._TARGET_GATED) == 4, "新增/删除被裁剪的键时请一起更新这条"


def test_every_enum_has_string_values_and_a_resolvable_default() -> None:
    for spec in _every_spec():
        assert spec.type in OPTION_TYPES, spec.key
        assert spec.label, spec.key
        if spec.type != "enum":
            continue
        if spec.dynamic:
            # 运行期才知道值域的枚举（字体）此时就是个空壳，
            # 由 services 层在序列化时填上真实列表与默认值。
            assert spec.values == (), spec.key
            continue
        assert spec.values, spec.key
        for value, label in spec.values:
            assert isinstance(value, str) and value, spec.key
            assert label, spec.key
        assert spec.default is not None, f"{spec.key} 需要默认值"
        assert str(spec.default) in {v for v, _ in spec.values}, spec.key


def test_visible_when_only_points_at_sibling_keys() -> None:
    """``visible_when`` 依赖的键必须与它同族（否则界面上它永远不出现）。"""
    for specs in opts._FAMILY_OPTIONS.values():
        keys = {spec.key for spec in specs}
        for spec in specs:
            for dependency, expected in spec.visible_when:
                assert dependency in keys, (spec.key, dependency)
                parent = next(s for s in specs if s.key == dependency)
                assert parent.type == "enum", dependency
                assert str(expected) in {v for v, _ in parent.values}, dependency


def test_converter_keys_are_covered_by_the_vocabulary() -> None:
    """注册表里出现的每个 ``converter_key`` 都必须在选项词表里有定义。

    否则那个家族就悄悄没有选项了 —— 现在看不出来，等前端按 schema 渲染时
    会得到一个空面板，而用户以为「这个转换就是没得调」。
    """
    used = {entry.converter_key for entry in registry.CONVERSIONS}
    assert used <= set(opts.converter_keys()), used - set(opts.converter_keys())


# ----------------------------------------------------------------------
# 2. 不变式：schema 只允许比服务端更窄
# ----------------------------------------------------------------------

def test_schema_bounds_are_inside_the_server_bounds() -> None:
    """§四十五：schema 是体验，不是防线。

    每个带区间约束的选项都必须满足 ``schema.min >= server_min`` 且
    ``schema.max <= server_max``。反过来的话，界面会允许一个服务端必然
    拒绝的值 —— 用户按下按钮才发现，而且拿到的是一句他无法理解的报错。
    """
    checked = 0
    for spec in _every_spec():
        bounds = SERVER_BOUNDS.get(spec.key)
        if bounds is None:
            continue
        server_min, server_max = bounds
        assert spec.min is not None and spec.max is not None, spec.key
        assert spec.min >= server_min, (spec.key, spec.min, server_min)
        assert spec.max <= server_max, (spec.key, spec.max, server_max)
        checked += 1
    assert checked == len(SERVER_BOUNDS), "有选项没被检查到，说明键名对不上"


def test_every_numeric_option_is_declared_in_the_bounds_table() -> None:
    """数值型选项不能在边界表之外 —— 那就等于没人验过它的区间。"""
    numeric = {spec.key for spec in _every_spec() if spec.type in ("integer", "number")}
    assert numeric == set(SERVER_BOUNDS), numeric ^ set(SERVER_BOUNDS)


def test_server_bounds_themselves_are_usable() -> None:
    """边界表里的区间必须是真的能用的区间（不是抄错方向的空区间）。"""
    for key, (low, high) in SERVER_BOUNDS.items():
        assert low < high, key
        assert math.isfinite(low) and math.isfinite(high), key


# ----------------------------------------------------------------------
# 3. 按目标格式裁剪
# ----------------------------------------------------------------------

def _keys(source: str, target: str) -> set[str]:
    return set(
        opts.applicable_keys(
            converter_key=registry.capability_for(source, target).converter_key,
            source_type=source,
            target_type=target,
        )
    )


def test_quality_appears_exactly_for_lossy_targets() -> None:
    """同格式不转换（jpg → jpg 不是一条能力），所以源只取非目标的那个。"""
    for target in ("jpg", "png", "webp"):
        source = next(s for s in ("jpg", "png", "webp") if s != target)
        keys = _keys(source, target)
        assert ("quality" in keys) is (target in opts._LOSSY_TARGETS), target


def test_dpi_and_metadata_are_trimmed_per_target() -> None:
    assert "dpi" in _keys("png", "jpg")
    assert "dpi" not in _keys("jpg", "webp")
    assert "metadata" in _keys("png", "webp")
    assert "metadata" in _keys("jpg", "png")


def test_dpi_custom_travels_with_its_parent() -> None:
    """``dpi.custom`` 与 ``dpi`` 必须同进同退。

    只留子项：界面上一个永远不显示的输入框。只留父项：用户选了「自定义」
    却没有地方填数字。
    """
    for source in ("jpg", "png", "webp"):
        for target in ("jpg", "png", "webp"):
            if source == target:
                continue
            keys = _keys(source, target)
            assert ("dpi.custom" in keys) == ("dpi" in keys), (source, target)


def test_image_to_pdf_gets_the_layout_options() -> None:
    assert _keys("jpg", "pdf") == {
        "page_size",
        "page_size.width_mm",
        "page_size.height_mm",
        "orientation",
        "margin",
        "fit",
    }


def test_txt_to_pdf_gets_the_txt_options() -> None:
    assert _keys("txt", "pdf") == {"font", "font_size", "page_size", "orientation"}


def test_office_and_pdf_to_word_declare_no_options() -> None:
    """没有可调参数时就是没有 —— 不摆一个空面板。

    字号的例子最能说明这条：``office/docx_writer`` 目前只设置字体、
    没有字号路径，所以 TXT → DOCX 只登记 ``font``。
    """
    assert _keys("docx", "pdf") == set()
    assert _keys("pdf", "docx") == set()
    assert opts._TXT_DOCX_OPTIONS and {s.key for s in opts._TXT_DOCX_OPTIONS} == {"font"}


# ----------------------------------------------------------------------
# 4. 纯校验
# ----------------------------------------------------------------------

def _validate(payload: dict, source: str = "jpg", target: str = "png") -> dict:
    entry = registry.capability_for(source, target)
    assert entry is not None
    return opts.validate_payload(payload, entry.options)


def test_valid_payload_is_normalised() -> None:
    """数字进来是数字，字符串枚举出去还是字符串。"""
    result = _validate(
        {
            "resize.mode": "custom",
            "resize.width": "800",
            "resize.height": 600,
            "resize.keep_aspect": "false",
            "dpi": "300",
            "rotation": 90,
            "metadata": "remove",
        }
    )
    assert result == {
        "resize.mode": "custom",
        "resize.width": 800,
        "resize.height": 600,
        "resize.keep_aspect": False,
        "dpi": "300",
        "rotation": "90",
        "metadata": "remove",
    }
    assert isinstance(result["resize.width"], int)
    assert isinstance(result["resize.keep_aspect"], bool)


def test_null_means_not_provided() -> None:
    """前端把没填的输入框原样序列化成 null —— 那不该是一次失败。"""
    assert _validate({"resize.width": None}, source="jpg", target="png") == {}
    assert _validate({"quality": None}, source="png", target="jpg") == {}


def test_empty_payload_is_valid() -> None:
    assert _validate({}) == {}


@pytest.mark.parametrize("value", [9, 101, 0, -5, "abc", 88.5, "", [], {}])
def test_invalid_quality_is_rejected(value: object) -> None:
    """§五十一：非法质量必须被拒绝，而不是悄悄换成默认值。"""
    with pytest.raises(opts.OptionError):
        _validate({"quality": value}, source="png", target="jpg")


@pytest.mark.parametrize("value", [0, -1, -999, 12001, 10**9, 1.5, "宽", []])
def test_invalid_dimensions_are_rejected(value: object) -> None:
    """零 / 负数 / 极端值 / 非整数 —— 一个都不能变成一张图。"""
    with pytest.raises(opts.OptionError):
        _validate({"resize.mode": "custom", "resize.width": value})


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), "nan", "inf"])
def test_non_finite_numbers_are_rejected(value: object) -> None:
    """NaN / Infinity 会让后面每一次比较都返回 False，一路溜到实现层。"""
    with pytest.raises(opts.OptionError):
        _validate({"resize.mode": "custom", "resize.width": value})


def test_bool_is_not_an_integer() -> None:
    """Python 里 ``True == 1``，不特判的话 ``"resize.width": true`` 会变成 1 像素。"""
    with pytest.raises(opts.OptionError):
        _validate({"resize.mode": "custom", "resize.width": True})


def test_unknown_key_is_rejected() -> None:
    with pytest.raises(opts.OptionError) as excinfo:
        _validate({"quality_hack": 100})
    assert "quality_hack" in str(excinfo.value)


def test_option_not_applicable_to_the_target_is_rejected() -> None:
    """PNG 是无损格式，质量对它没有意义 —— 提交了也要明确拒绝。"""
    with pytest.raises(opts.OptionError):
        _validate({"quality": 90}, source="jpg", target="png")


def test_invalid_enum_value_lists_the_alternatives() -> None:
    with pytest.raises(opts.OptionError) as excinfo:
        _validate({"rotation": "45"})
    message = str(excinfo.value)
    assert "旋转" in message
    assert "顺时针 90°" in message


def test_hidden_option_is_rejected_instead_of_silently_ignored() -> None:
    """「保持原尺寸」却提交了宽度：静默忽略会让用户以为尺寸生效了。"""
    with pytest.raises(opts.OptionError):
        _validate({"resize.mode": "original", "resize.width": 800})
    # 选了自定义就该通过
    assert _validate({"resize.mode": "custom", "resize.width": 800})["resize.width"] == 800


def test_too_many_keys_are_rejected() -> None:
    payload = {f"k{index}": 1 for index in range(opts.MAX_OPTION_KEYS + 1)}
    with pytest.raises(opts.OptionError):
        opts.validate_payload(payload, ())


def test_overlong_key_is_rejected() -> None:
    with pytest.raises(opts.OptionError):
        opts.validate_payload({"x" * (opts.MAX_OPTION_KEY_CHARS + 1): 1}, ())


def test_dynamic_enum_defers_to_the_binding_layer() -> None:
    """字体是唯一一个运行期才知道值域的枚举。

    纯层放行（它**不装作**知道服务器装了哪些字体），真正的白名单在绑定层 ——
    值最终会交给 ``parse_txt_options``，那里拿 ``available_fonts()`` 现算的
    键做校验。这条测试把「谁负责哪一段」钉住：如果哪天纯层开始自己
    维护一张字体表，它就会与真实安装的字体分叉。
    """
    font = next(s for s in opts._TXT_PDF_OPTIONS if s.key == "font")
    assert font.dynamic == DYNAMIC_FONTS
    assert font.values == ()
    assert _validate({"font": "song"}, source="txt", target="pdf") == {"font": "song"}
    with pytest.raises(opts.OptionError):
        _validate({"font": ""}, source="txt", target="pdf")


def test_option_error_is_a_plain_value_error() -> None:
    """这一层不依赖 Web 框架、不依赖 utils.errors —— 才能单独测。"""
    assert issubclass(opts.OptionError, ValueError)
    assert opts.OptionError.__module__ == "conversion.options"
