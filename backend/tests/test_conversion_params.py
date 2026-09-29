"""``options`` / ``capability_id`` 两个表单字段的五层校验链（第九阶段 §四十五）。

纯层（第 4 层）在 ``test_conversion_options.py`` 里已经逐条测过。这里测的是
**路由侧的四层** —— 语法、白名单、覆盖、绑定 —— 以及它们串起来之后的
端到端行为：真正转一张图，用 Pillow 打开结果验尺寸、验元数据。

三组断言：

1. **老客户端不受影响** —— 不传 ``options`` 时，解析出来的 DTO 与
   第七阶段**完全相同**（同一个对象、逐字段相等）；
2. **非法输入一律 400，且文案是中文** —— 不允许静默忽略，
   也不允许把 ``ValueError`` / ``JSONDecodeError`` 泄漏成 500；
3. **绑定层走的是既有解析器** —— 边界的真相只有一个：
   把 ``font_size`` 写成 999、把 ``page_size`` 写成不存在的值，
   报的是 ``routers/pdf_params`` 里那句话。
"""

from __future__ import annotations

import io
import json

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from compressors.resizer import ResizeRequest
from conversion import options as opts
from conversion import registry
from routers.conversion_params import (
    parse_capability_id,
    parse_options_json,
    resolve_options,
)
from services.conversion_service import ConversionOptions
from tests.conftest import (
    build_image_bytes,
    conversion_task,
    image_files,
    run_conversion,
    submit_conversion,
)
from utils.errors import ValidationError

ENDPOINT = "/api/conversion/tasks"


def base_options(**kwargs) -> ConversionOptions:
    """与路由层构造方式一致的一份基础 DTO。"""
    defaults = dict(
        quality_preset=None,
        quality_value=None,
        target_bytes=None,
        resize=None,
    )
    defaults.update(kwargs)
    return ConversionOptions(**defaults)


def resolve(
    payload: dict | str | None,
    *,
    target_type: str = "jpg",
    capability_id: str | None = None,
    base: ConversionOptions | None = None,
):
    raw = payload if isinstance(payload, str) or payload is None else json.dumps(payload)
    return resolve_options(
        raw_options=raw,
        raw_capability_id=capability_id,
        target_type=target_type,
        base=base if base is not None else base_options(),
    )


# ----------------------------------------------------------------------
# 第 1 层：语法
# ----------------------------------------------------------------------

@pytest.mark.parametrize("raw", [None, "", "   ", "\n\t "])
def test_blank_options_means_nothing_was_selected(raw: str | None) -> None:
    assert parse_options_json(raw) == {}


def test_valid_json_object_is_parsed() -> None:
    assert parse_options_json('{"quality": 70}') == {"quality": 70}


def test_oversized_payload_is_rejected() -> None:
    """体积按 UTF-8 字节算 —— 中文键名一个字符三字节。"""
    huge = "{" + ",".join(f'"k{i}": {i}' for i in range(2000)) + "}"
    assert len(huge.encode("utf-8")) > opts.MAX_OPTIONS_BYTES
    with pytest.raises(ValidationError):
        parse_options_json(huge)


@pytest.mark.parametrize(
    "raw",
    [
        "{",                       # 截断
        "{'quality': 70}",         # 单引号不是 JSON
        '{"quality": }',           # 缺值
        "not json at all",
    ],
)
def test_broken_json_is_rejected_with_a_readable_message(raw: str) -> None:
    with pytest.raises(ValidationError) as excinfo:
        parse_options_json(raw)
    assert "参数" in str(excinfo.value)


@pytest.mark.parametrize("raw", ["[1, 2]", '"hello"', "42", "null", "true"])
def test_non_object_json_is_rejected(raw: str) -> None:
    with pytest.raises(ValidationError) as excinfo:
        parse_options_json(raw)
    assert "格式" in str(excinfo.value)


def test_deeply_nested_json_does_not_crash_the_server() -> None:
    """8 KB 的 ``[[[[…`` 足以顶穿 json 的递归解析器 —— 必须是 400，不是 500。"""
    with pytest.raises(ValidationError):
        parse_options_json("[" * 4000)


def test_too_many_keys_are_rejected() -> None:
    payload = {f"k{i}": 1 for i in range(opts.MAX_OPTION_KEYS + 1)}
    with pytest.raises(ValidationError) as excinfo:
        parse_options_json(json.dumps(payload))
    assert "过多" in str(excinfo.value)


@pytest.mark.parametrize(
    "key",
    [
        "Quality",           # 大写
        "resize-Mode",       # 连字符
        "1quality",          # 数字开头
        ".quality",          # 点号开头
        "resize..width",     # 双点
        "resize.",           # 点号结尾
        "中文键",             # 非 ASCII
        "quality\nx",        # 控制字符（键名会进错误文案）
        "_quality",          # 下划线开头
    ],
)
def test_illegal_key_names_are_rejected(key: str) -> None:
    with pytest.raises(ValidationError) as excinfo:
        parse_options_json(json.dumps({key: 1}))
    assert "参数名" in str(excinfo.value)


def test_overlong_key_is_rejected() -> None:
    with pytest.raises(ValidationError):
        parse_options_json(json.dumps({"a" * (opts.MAX_OPTION_KEY_CHARS + 1): 1}))


# ----------------------------------------------------------------------
# 第 2 层：白名单
# ----------------------------------------------------------------------

def test_unknown_key_is_rejected() -> None:
    with pytest.raises(ValidationError) as excinfo:
        resolve({"totally.made.up": 1})
    assert "不适用于该转换" in str(excinfo.value)


def test_key_not_meaningful_for_the_target_is_rejected() -> None:
    """PNG 是无损格式，质量滑杆在那里没有意义 —— 传了要明确报错。"""
    with pytest.raises(ValidationError) as excinfo:
        resolve({"quality": 70}, target_type="png")
    assert "不适用于该转换" in str(excinfo.value)


def test_whitelist_is_the_union_across_sources() -> None:
    """提交时还不知道源格式，所以白名单是**并集**。

    同一批里混着图片和 TXT 时，两种家族各自的参数都必须被接受 ——
    否则先到的那条能力会把另一条的参数判成非法。
    """
    # 图片 → PDF 的键
    assert resolve({"fit": "fill"}, target_type="pdf").payload == {"fit": "fill"}
    # TXT → PDF 的键
    assert resolve({"font_size": 14}, target_type="pdf").payload == {"font_size": 14}
    # Markdown → PDF 的键
    assert resolve({"margin": "small"}, target_type="pdf").payload == {"margin": "small"}


def test_capability_id_narrows_the_whitelist() -> None:
    """声明了能力 ID，就用那一条能力自己的 schema 校验。

    ``page_size=auto`` 只对「图片 → PDF」成立；声明成 TXT 那条能力之后，
    它必须当场被拒 —— 这正是 capability_id 的价值：版本错配会立刻暴露，
    而不是被静默忽略。
    """
    ok = resolve(
        {"page_size": "auto"},
        target_type="pdf",
        capability_id="image.jpg-to-pdf",
    )
    assert ok.payload["page_size"] == "auto"

    with pytest.raises(ValidationError) as excinfo:
        resolve(
            {"page_size": "auto"},
            target_type="pdf",
            capability_id="document.txt-to-pdf",
        )
    assert "页面大小" in str(excinfo.value)


def test_capability_id_cannot_add_options() -> None:
    """收窄只会**减少**可用键，永远不可能凭空多给一个。"""
    with pytest.raises(ValidationError):
        resolve(
            {"font_size": 14},
            target_type="pdf",
            capability_id="image.jpg-to-pdf",
        )


def test_unknown_capability_id_is_rejected() -> None:
    with pytest.raises(ValidationError) as excinfo:
        parse_capability_id("image.jpg-to-nothing", target_type="jpg")
    assert "不存在" in str(excinfo.value)


@pytest.mark.parametrize(
    "value",
    ["op.pdf-merge", "op.pdf-split", "op.pdf-compress", "op.image-images-to-pdf"],
)
def test_operation_capability_is_rejected(value: str) -> None:
    """PDF 合并之类的工具不走统一队列，不能拿它们的 ID 来提交任务。"""
    with pytest.raises(ValidationError) as excinfo:
        parse_capability_id(value, target_type="pdf")
    assert "不存在" in str(excinfo.value)


def test_every_registered_operation_id_is_rejected() -> None:
    """登记进注册表的每一条 operation 都不接受作为提交参数。

    operation 条目现在还是一条都没有（第九阶段第 11 步才登记），
    所以这条断言此刻是空转的 —— 它守的是「登记之后别忘了我」。
    """
    for entry in registry.OPERATIONS:
        with pytest.raises(ValidationError):
            parse_capability_id(entry.id, target_type=entry.target_type)


def test_capability_id_must_match_the_target() -> None:
    with pytest.raises(ValidationError) as excinfo:
        parse_capability_id("image.jpg-to-png", target_type="jpg")
    assert "不一致" in str(excinfo.value)


def test_blank_capability_id_is_accepted() -> None:
    for raw in (None, "", "   "):
        assert parse_capability_id(raw, target_type="jpg") == ""


# ----------------------------------------------------------------------
# 第 3 层：覆盖
# ----------------------------------------------------------------------

def test_json_overrides_the_flat_fields() -> None:
    """JSON 里的尺寸整组取代扁平字段解析出来的那个 ResizeRequest。"""
    base = base_options(resize=ResizeRequest(width=800, height=600))
    parsed = resolve({"resize.mode": "small"}, base=base)
    assert parsed.options.resize == ResizeRequest(max_edge=1024)


def test_json_quality_overrides_the_flat_quality() -> None:
    base = base_options(quality_value=60)
    parsed = resolve({"quality": 92}, base=base)
    assert parsed.options.quality_value == 92


def test_flat_fields_survive_when_json_is_silent() -> None:
    base = base_options(
        quality_value=60, target_bytes=200_000, resize=ResizeRequest(width=800)
    )
    parsed = resolve({"rotation": "90"}, base=base)
    assert parsed.options.quality_value == 60
    assert parsed.options.target_bytes == 200_000
    assert parsed.options.resize == ResizeRequest(width=800)
    assert parsed.options.rotation == 90


def test_layout_keys_do_not_clobber_untouched_fields() -> None:
    parsed = resolve({"page_size": "a4", "orientation": "landscape"}, target_type="pdf")
    assert parsed.options.layout.page_size == "a4"
    assert parsed.options.layout.orientation == "landscape"
    assert parsed.options.layout.fit == "contain"
    assert parsed.options.layout.margin == "none"


# ----------------------------------------------------------------------
# 第 4 层：类型与边界（纯层，这里只验它真的被串上了）
# ----------------------------------------------------------------------

@pytest.mark.parametrize(
    "payload",
    [
        {"quality": 9},          # 低于 §九 的下限 10
        {"quality": 101},
        {"resize.width": 0},
        {"resize.width": -100},
        {"resize.height": 0},
        {"rotation": "45"},      # 不是 90 的整数倍
        {"dpi": "300dpi"},
    ],
)
def test_out_of_range_values_are_rejected(payload: dict) -> None:
    with pytest.raises(ValidationError):
        resolve(payload, target_type="jpg")


def test_non_finite_numbers_are_rejected() -> None:
    """JSON 允许 NaN / Infinity 字面量，它们会让后面每一次比较都返回 False。"""
    with pytest.raises(ValidationError):
        resolve('{"page_size": "custom", "page_size.width_mm": NaN}', target_type="pdf")


def test_hidden_option_is_rejected() -> None:
    """宽高只在「自定义」档下有意义，档位不对时必须报错而不是静默忽略。"""
    with pytest.raises(ValidationError) as excinfo:
        resolve({"resize.mode": "small", "resize.width": 800})
    assert "不能使用" in str(excinfo.value)


# ----------------------------------------------------------------------
# 第 5 层：绑定（逐字复用既有解析器）
# ----------------------------------------------------------------------

def test_preset_resize_binds_to_max_edge() -> None:
    for mode, edge in opts.RESIZE_MAX_EDGE_BY_MODE.items():
        parsed = resolve({"resize.mode": mode})
        assert parsed.options.resize == ResizeRequest(max_edge=edge)


def test_original_resize_clears_the_flat_resize() -> None:
    base = base_options(resize=ResizeRequest(width=800))
    assert resolve({"resize.mode": "original"}, base=base).options.resize is None


def test_custom_resize_needs_at_least_one_edge() -> None:
    with pytest.raises(ValidationError) as excinfo:
        resolve({"resize.mode": "custom"})
    assert "宽度或高度" in str(excinfo.value)


def test_custom_resize_accepts_a_single_edge() -> None:
    """§九 允许只填一边：另一边按比例推导。"""
    parsed = resolve({"resize.mode": "custom", "resize.width": 640})
    assert parsed.options.resize == ResizeRequest(
        width=640, height=None, keep_aspect=True
    )


def test_custom_resize_keeps_the_aspect_switch() -> None:
    parsed = resolve(
        {
            "resize.mode": "custom",
            "resize.width": 640,
            "resize.height": 480,
            "resize.keep_aspect": False,
        }
    )
    assert parsed.options.resize == ResizeRequest(
        width=640, height=480, keep_aspect=False
    )


def test_custom_resize_without_mode_implies_custom() -> None:
    """只给了宽高、没给档位 —— 意图只能是自定义。"""
    parsed = resolve({"resize.width": 640})
    assert parsed.options.resize == ResizeRequest(width=640, height=None)


def test_dpi_presets_bind_to_integers() -> None:
    for value in opts.DPI_PRESETS:
        assert resolve({"dpi": str(value)}).options.dpi == value


def test_dpi_original_and_custom() -> None:
    assert resolve({"dpi": opts.DPI_ORIGINAL}).options.dpi == opts.DPI_ORIGINAL
    assert resolve({"dpi": "custom", "dpi.custom": 600}).options.dpi == 600


def test_custom_dpi_needs_its_value() -> None:
    with pytest.raises(ValidationError) as excinfo:
        resolve({"dpi": "custom"})
    assert "DPI" in str(excinfo.value)


def test_custom_dpi_alone_implies_custom() -> None:
    assert resolve({"dpi.custom": 600}).options.dpi == 600


def test_dpi_is_not_offered_for_formats_without_a_density_field() -> None:
    with pytest.raises(ValidationError):
        resolve({"dpi": "300"}, target_type="gif")


def test_rotation_and_metadata_bind() -> None:
    parsed = resolve({"rotation": "270", "metadata": "remove"})
    assert parsed.options.rotation == 270
    assert parsed.options.metadata == opts.METADATA_REMOVE


def test_options_json_only_overrides_the_keys_it_mentions() -> None:
    """只提到 ``rotation`` 的一份 JSON，只改 rotation；其余照统一中心的默认走。

    这条原先断言的是「不传 metadata 就丢弃 EXIF（``remove``）」，那是
    **第十阶段 C §三 之前**的语义，现在已经被有意改掉了：统一转换中心
    的默认值是 ``conversion.options.METADATA_DEFAULT``（``keep``），而它
    必须与前端在「元数据」那一项上显示的选中项**是同一个值** —— 否则
    界面显示「保留」、接口却按「清除」处理，不报错，只是说了假话。

    两条路各自的默认值现在各有测试钉着，不在这一层重复：

    * 统一中心不传 ``options`` → 保留：``test_image_metadata.py``
      的 ``test_the_unified_fallback_keeps_the_location``；
    * 老页面（压缩 / 尺寸调整 / 旧转换页）→ 仍然清除 EXIF：
      同一文件的 ``test_legacy_pages_still_drop_the_location``。

    所以这里钉的是**绑定层的合并语义**：JSON 只覆盖它明确提到的键。
    那个默认值本身写成 ``METADATA_DEFAULT`` 而不是某个字面量 —— 这条
    测试不该在默认值被有意调整时跟着红，它管的是「谁覆盖了谁」。
    """
    base = base_options()
    parsed = resolve({"rotation": "90"}, base=base).options
    assert parsed.rotation == 90
    assert parsed.metadata == opts.METADATA_DEFAULT
    # 没提到的键一个都不许被顺手改掉：与传进去的那份基础 DTO 逐字段相等
    assert parsed.quality_preset == base.quality_preset
    assert parsed.quality_value == base.quality_value
    assert parsed.target_bytes == base.target_bytes
    assert parsed.resize == base.resize
    assert parsed.dpi == base.dpi
    assert parsed.crop == base.crop


def test_custom_page_size_needs_both_millimetres() -> None:
    """报的是 ``routers.pdf_params.parse_layout`` 里那句话。"""
    with pytest.raises(ValidationError) as excinfo:
        resolve({"page_size": "custom"}, target_type="pdf")
    assert "宽度和高度" in str(excinfo.value)

    parsed = resolve(
        {
            "page_size": "custom",
            "page_size.width_mm": 100,
            "page_size.height_mm": 150,
        },
        target_type="pdf",
    )
    assert parsed.options.layout.page_size == "custom"
    assert parsed.options.layout.custom_width_mm == 100
    assert parsed.options.layout.custom_height_mm == 150


def test_margin_and_fit_bind_through_the_pdf_parser() -> None:
    parsed = resolve({"margin": "large", "fit": "original"}, target_type="pdf")
    assert parsed.options.layout.margin == "large"
    assert parsed.options.layout.fit == "original"


def test_txt_options_bind_through_the_txt_parser() -> None:
    parsed = resolve({"font_size": 14, "page_size": "a5"}, target_type="pdf")
    assert parsed.options.txt_options is not None
    assert parsed.options.txt_options.font_size == 14
    assert parsed.options.txt_options.page_size == "a5"


def test_font_size_beyond_the_server_bound_is_rejected_by_the_pure_layer() -> None:
    from config import settings

    with pytest.raises(ValidationError) as excinfo:
        resolve({"font_size": settings.TXT_MAX_FONT_SIZE + 1}, target_type="pdf")
    assert "字号" in str(excinfo.value)


def test_unknown_font_is_rejected_by_the_binding_layer() -> None:
    """字体是**动态枚举**：纯层只挡住空值与超长，真正的白名单在绑定层
    （``parse_txt_options`` 拿 ``available_fonts()`` 现算的键做校验）。"""
    with pytest.raises(ValidationError) as excinfo:
        resolve({"font": "NotInstalledFont-42"}, target_type="pdf")
    assert "字体" in str(excinfo.value)


def test_image_only_page_size_does_not_touch_the_txt_defaults() -> None:
    """``page_size=auto`` 是图片专有的取值，不该把 TXT 那一侧也重建一遍。"""
    parsed = resolve({"page_size": "auto"}, target_type="pdf")
    assert parsed.options.layout.page_size == "auto"
    assert parsed.options.txt_options is None


def test_images_are_unaffected_by_the_txt_family_defaults() -> None:
    parsed = resolve({"font_size": 14}, target_type="pdf")
    assert parsed.options.layout.page_size == "auto"
    assert parsed.options.txt_options.font_size == 14


# ----------------------------------------------------------------------
# 短路：老客户端
# ----------------------------------------------------------------------

def test_no_options_returns_the_same_object() -> None:
    """不传 ``options`` 时返回的就是传进来的那个 base —— 一个字段都不动。"""
    base = base_options(quality_value=70, resize=ResizeRequest(width=800))
    parsed = resolve(None, base=base)
    assert parsed.options is base
    assert parsed.payload == {}
    assert parsed.capability_id == ""


def test_empty_options_object_is_also_a_short_circuit() -> None:
    base = base_options()
    assert resolve({}, base=base).options is base


def test_capability_id_alone_does_not_touch_the_options() -> None:
    base = base_options(quality_value=70)
    parsed = resolve(None, capability_id="image.png-to-jpg", target_type="jpg", base=base)
    assert parsed.capability_id == "image.png-to-jpg"
    assert parsed.options is base


# ----------------------------------------------------------------------
# 端到端：真的转一张图，用 Pillow 打开结果
# ----------------------------------------------------------------------

def _download(client: TestClient, snapshot: dict, index: int = 0) -> bytes:
    task = conversion_task(snapshot, index)
    assert task["status"] == "completed", task
    response = client.get(task["result"]["download_url"])
    assert response.status_code == 200, response.text
    return response.content


def test_rotation_option_really_rotates_the_output(client: TestClient) -> None:
    png = build_image_bytes(120, 60, "PNG")
    snapshot = run_conversion(
        client,
        files=image_files(("a.png", png)),
        target_type="jpg",
        options=json.dumps({"rotation": "90"}),
    )
    data = _download(client, snapshot)
    with Image.open(io.BytesIO(data)) as img:
        assert img.format == "JPEG"
        # 120×60 顺时针 90° → 60×120
        assert (img.width, img.height) == (60, 120)


def test_resize_preset_really_shrinks_the_output(client: TestClient) -> None:
    png = build_image_bytes(3000, 1500, "PNG")
    snapshot = run_conversion(
        client,
        files=image_files(("a.png", png)),
        target_type="jpg",
        options=json.dumps({"resize.mode": "small"}),
    )
    data = _download(client, snapshot)
    with Image.open(io.BytesIO(data)) as img:
        assert max(img.width, img.height) == 1024
        assert img.width / img.height == pytest.approx(2.0, rel=0.01)


def test_resize_preset_does_not_enlarge_small_images(client: TestClient) -> None:
    """§九：「不要强行改变原始尺寸」—— 预设档只缩不放。"""
    png = build_image_bytes(120, 60, "PNG")
    snapshot = run_conversion(
        client,
        files=image_files(("a.png", png)),
        target_type="jpg",
        options=json.dumps({"resize.mode": "large"}),
    )
    data = _download(client, snapshot)
    with Image.open(io.BytesIO(data)) as img:
        assert (img.width, img.height) == (120, 60)


def test_custom_resize_really_resizes(client: TestClient) -> None:
    png = build_image_bytes(400, 200, "PNG")
    snapshot = run_conversion(
        client,
        files=image_files(("a.png", png)),
        target_type="jpg",
        options=json.dumps({"resize.mode": "custom", "resize.width": 200}),
    )
    data = _download(client, snapshot)
    with Image.open(io.BytesIO(data)) as img:
        assert (img.width, img.height) == (200, 100)


def test_metadata_keep_carries_the_exif_through(client: TestClient) -> None:
    """「保留元数据」是真的保留：EXIF 容器里有拍摄信息。

    这一段历史很具体：``load_image`` 从第一阶段起就把 EXIF 丢掉了
    （见 ``compressors.loader`` 的说明），统一转换中心把选择权交回用户，
    所以这里必须验到字节层面 —— 不能只看 HTTP 200。
    """
    source = Image.new("RGB", (80, 40), (200, 30, 30))
    exif = Image.Exif()
    exif[0x010F] = "FileTools"      # Make
    exif[0x0110] = "TestCamera"     # Model
    buffer = io.BytesIO()
    source.save(buffer, format="JPEG", exif=exif.tobytes(), quality=95)
    jpeg = buffer.getvalue()

    snapshot = run_conversion(
        client,
        files=image_files(("a.jpg", jpeg), field="files"),
        target_type="png",
        options=json.dumps({"metadata": "keep"}),
    )
    data = _download(client, snapshot)
    with Image.open(io.BytesIO(data)) as img:
        assert img.format == "PNG"
        assert img.info.get("exif")
        carried = Image.Exif()
        carried.load(img.info["exif"])
        assert carried.get(0x010F) == "FileTools"
        assert carried.get(0x0110) == "TestCamera"


def test_metadata_remove_drops_the_exif(client: TestClient) -> None:
    source = Image.new("RGB", (80, 40), (30, 30, 200))
    exif = Image.Exif()
    exif[0x010F] = "FileTools"
    buffer = io.BytesIO()
    source.save(buffer, format="JPEG", exif=exif.tobytes(), quality=95)

    snapshot = run_conversion(
        client,
        files=image_files(("a.jpg", buffer.getvalue())),
        target_type="png",
        options=json.dumps({"metadata": "remove"}),
    )
    data = _download(client, snapshot)
    with Image.open(io.BytesIO(data)) as img:
        assert not img.info.get("exif")


def test_dpi_option_writes_the_density(client: TestClient) -> None:
    png = build_image_bytes(120, 60, "PNG")
    snapshot = run_conversion(
        client,
        files=image_files(("a.png", png)),
        target_type="jpg",
        options=json.dumps({"dpi": "300"}),
    )
    data = _download(client, snapshot)
    with Image.open(io.BytesIO(data)) as img:
        assert img.info.get("dpi") == (300, 300)


def test_options_reach_the_task_item_for_display(client: TestClient) -> None:
    """收敛后的参数写进 ``TaskItem.options``，供结果页回显（§二十三）。"""
    png = build_image_bytes(120, 60, "PNG")
    payload = {"rotation": "90", "resize.mode": "small"}
    snapshot = run_conversion(
        client,
        files=image_files(("a.png", png)),
        target_type="jpg",
        options=json.dumps(payload),
    )
    task = conversion_task(snapshot, 0)
    assert task["conversion_id"] == "image.png-to-jpg"
    assert task["options"] == {"rotation": "90", "resize.mode": "small"}


def test_legacy_submission_leaves_options_empty(client: TestClient) -> None:
    """老前端（或旧测试）不传 options 时，回显字段保持为空 —— 不伪造。"""
    png = build_image_bytes(120, 60, "PNG")
    snapshot = run_conversion(
        client, files=image_files(("a.png", png)), target_type="jpg"
    )
    task = conversion_task(snapshot, 0)
    assert task["conversion_id"] == "image.png-to-jpg"
    assert task["options"] is None


def test_flat_fields_still_work_alongside_options(client: TestClient) -> None:
    """扁平字段与 options 同时出现：JSON 赢，其余照旧。"""
    png = build_image_bytes(600, 300, "PNG")
    snapshot = run_conversion(
        client,
        files=image_files(("a.png", png)),
        target_type="jpg",
        width="300",
        options=json.dumps({"resize.mode": "custom", "resize.width": 150}),
    )
    data = _download(client, snapshot)
    with Image.open(io.BytesIO(data)) as img:
        assert (img.width, img.height) == (150, 75)


# ----------------------------------------------------------------------
# 端到端：非法输入不能变成 500，也不能泄漏内部信息
# ----------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw_options",
    [
        "{",                                  # 坏 JSON
        "[1,2,3]",                            # 不是对象
        '{"nope": 1}',                        # 未知键
        '{"quality": 5}',                     # 越界
        '{"resize.mode": "custom"}',          # 缺宽高
        '{"resize.width": 800}',              # 缺 resize.mode 也无所谓，但会被接受…见下
        '{"dpi": "custom"}',                  # 缺自定义值
        '{"rotation": "45"}',                 # 非法旋转角
    ],
)
def test_invalid_options_are_rejected_with_a_clean_message(
    client: TestClient, raw_options: str
) -> None:
    png = build_image_bytes(120, 60, "PNG")
    response = submit_conversion(
        client,
        files=image_files(("a.png", png)),
        target_type="jpg",
        options=raw_options,
    )
    if raw_options == '{"resize.width": 800}':
        # 只给宽高是合法的（等价于自定义档），不该被拒
        assert response.status_code == 202, response.text
        return

    assert response.status_code == 400, response.text
    body = response.json()
    message = body["error"]["message"]
    assert message and not message.startswith("Traceback")
    for fragment in ("Traceback", "File \"", "\\", "site-packages", "conversion_params"):
        assert fragment not in json.dumps(body, ensure_ascii=False)


def test_invalid_capability_id_is_a_400(client: TestClient) -> None:
    png = build_image_bytes(120, 60, "PNG")
    response = submit_conversion(
        client,
        files=image_files(("a.png", png)),
        target_type="jpg",
        capability_id="image.png-to-nothing",
    )
    assert response.status_code == 400, response.text
    assert "不存在" in response.json()["error"]["message"]


@pytest.mark.parametrize(
    "payload",
    [
        {"resize.mode": "custom", "resize.width": 100_000},        # 远超 MAX_IMAGE_EDGE
        {"resize.mode": "custom", "resize.height": 999_999},
        {"quality": 100_000},
        {"font_size": 9999},
        {
            "page_size": "custom",
            "page_size.width_mm": 99_999,
            "page_size.height_mm": 99_999,
        },
    ],
)
def test_options_cannot_bypass_the_resource_limits(
    client: TestClient, payload: dict
) -> None:
    """§四十五：``options`` 不是绕过资源上限的旁路。

    每一个数值键的 schema 都被不变式钉在服务端解析器的区间**之内**
    （``test_schema_bounds_are_inside_the_server_bounds``），
    所以越界的值在到达实现之前就被拒了 —— 这里从 HTTP 层确认这件事：
    得到的是 400 与一句中文，不是 500，也不是一个悄悄放行的巨大尺寸。
    """
    png = build_image_bytes(120, 60, "PNG")
    response = submit_conversion(
        client,
        files=image_files(("a.png", png)),
        target_type="pdf",
        options=json.dumps(payload),
    )
    assert response.status_code == 400, response.text
    assert response.json()["error"]["message"]
