"""PDF 操作条目（第九阶段第 11 步，决策 B）。

六个工具（合并 / 拆分 / 压缩 / 删页 / 提取页 / 多图合成 PDF）在注册表里
登记为 ``operation_type="operation"``，**不进统一队列** —— 执行仍然走
各自原来的 ``/api/pdf/*`` 接口。统一中心只负责入口与参数 UI。

于是这里有一条隐性契约：**条目上的 ``options_schema`` 与那个端点的
Form 字段是同一件事的两种写法**。它极其容易腐烂（改端点的人不会想到
还有一张 schema 在别处），所以这份测试用 ``inspect.signature`` 把它
机械地钉死：多一个旋钮、少一个字段、改一个枚举取值，都会当场红。
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil

import pytest
from fastapi.testclient import TestClient

import routers
from config import settings
from conversion import capability, options, registry
from main import app
from pdf import image_to_pdf
from pdf.compressor import LEVEL_LABELS, TARGET_PRESETS_MB
from pdf.splitter import SPLIT_MODES, SPLIT_MODE_LABELS

#: 端点上**不是用户可选项**的参数：``files`` / ``file`` 是文件本身，
#: ``input_id`` 是上传接口发的令牌。三个都不该出现在参数面板里。
#: 这份名单是白的：下面 ``test_excluded_parameters_really_exist`` 会证明
#: 名单里的每一个名字都真的出现在某个端点的签名里 —— 否则它就是在
#: 悄悄掩盖一个已经改名的字段。
NOT_USER_OPTIONS = ("files", "file", "input_id")

EXPECTED_IDS = (
    "op.pdf-merge",
    "op.pdf-split",
    "op.pdf-compress",
    "op.pdf-extract-pages",
    "op.pdf-delete-pages",
    "op.image-images-to-pdf",
    # 第十阶段 A §三十四：只读的元数据查看器。它是**第一条不产出文件的
    # 工具**，也是第一条例外的 category/group（图片，不是 PDF）。
    "op.image-metadata",
)


def _declared_routes() -> dict[str, object]:
    """``{路径: APIRoute}``，取自 ``routers`` 包里每个模块自己的路由表。

    **为什么不遍历 ``app.routes``**：这个 FastAPI 版本（0.141）把
    ``include_router`` 变成了惰性的 ``_IncludedRouter``，``app.routes``
    里不再有那些具体路径，只剩一个占位对象。要么用一个下划线开头的私有
    属性去钻，要么换个公开的口径 —— 这里选后者。

    于是「这条路径真的对外提供」由下面 ``_served_paths()``（OpenAPI，
    也就是前端真正看到的那份契约）证明，而「这个函数的签名是什么」由
    各模块自己的 ``router.routes`` 给出。两个事实分别有各自的证据，
    没有一个是靠猜的。
    """
    table: dict[str, object] = {}
    for info in pkgutil.iter_modules(routers.__path__):
        module = importlib.import_module(f"routers.{info.name}")
        for route in getattr(getattr(module, "router", None), "routes", ()):
            path = getattr(route, "path", None)
            if path:
                table[path] = route
    return table


def _served_paths() -> dict[str, set[str]]:
    """应用真正对外提供的 ``{路径: {方法}}``，取自 OpenAPI。"""
    return {
        path: {method.upper() for method in operations}
        for path, operations in app.openapi()["paths"].items()
    }


ROUTES = _declared_routes()
SERVED = _served_paths()


def route_for(endpoint: str):
    """按路径找到端点函数所属的 APIRoute。找不到就直接失败 —— 那正是要守的。"""
    assert endpoint in SERVED, f"应用没有对外提供 {endpoint}"
    assert endpoint in ROUTES, f"没有任何 router 声明了 {endpoint}"
    return ROUTES[endpoint]


#: FastAPI 用 ``PydanticUndefined`` 表示「这个字段没有默认值」。
#: 不 import 它的原因很简单：它在 pydantic 的内部命名空间里，
#: 拿 ``inspect.Parameter.empty`` 当替身更稳，语义也一样。
_NO_DEFAULT = inspect.Parameter.empty


def declared_default(route, key: str):
    """端点表单字段的**真正**默认值，没有默认值时返回 ``inspect.Parameter.empty``。

    签名里看到的是 ``Form("")`` 这个包装对象，值在它的 ``.default`` 上。
    直接拿包装对象跟 ``""`` 比会永远不相等 —— 那不是「有默认值」的意思，
    所以要把这一层剥掉再看。
    """
    parameter = inspect.signature(route.endpoint).parameters[key]
    default = parameter.default
    if hasattr(default, "default"):  # fastapi.params.Param / File 包装
        default = default.default
    name = type(default).__name__
    if name in ("PydanticUndefinedType", "_PydanticUndefined"):
        return _NO_DEFAULT
    return default


def specs_of(entry: capability.Capability) -> dict[str, capability.OptionSpec]:
    return {spec.key: spec for spec in entry.options}


def values_of(spec: capability.OptionSpec) -> tuple[str, ...]:
    return tuple(value for value, _ in spec.values)


# ----------------------------------------------------------------------
# A. 条目本身的形状
# ----------------------------------------------------------------------

def test_exactly_the_declared_tools_are_registered() -> None:
    """不多不少，就是 :data:`EXPECTED_IDS` 这一串。

    函数名里的数字**有意去掉了**：第九阶段它是「六个」，第十阶段 A 加了
    元数据查看器就成了七个。写死数字的测试名每加一条工具就要改一次，
    而改名字这件事本身不产生任何验证价值 —— 下面这条断言才是。
    """
    assert tuple(entry.id for entry in registry.OPERATIONS) == EXPECTED_IDS


def test_operations_are_not_conversions() -> None:
    for entry in registry.OPERATIONS:
        assert entry.operation_type == capability.OPERATION_OPERATION, entry.id
        assert entry.converter_key is None, entry.id
        assert entry.endpoint, entry.id
        assert entry.method == "POST", entry.id


def test_operations_stay_out_of_the_conversion_matrix() -> None:
    """操作没有「这个源能转成哪些目标」的含义，不能污染矩阵与配对表。"""
    assert not any(entry.is_operation for entry in registry.CONVERSIONS)
    assert len(registry.CONVERSIONS) + len(registry.OPERATIONS) == len(
        registry.CAPABILITIES
    )
    operation_ids = {entry.id for entry in registry.OPERATIONS}
    assert not (operation_ids & {id_ for id_, _ in registry.CAPABILITY_BY_PAIR.items()})


def test_operations_are_always_available() -> None:
    """六个工具只靠必装依赖，缺任何可选组件都不该影响它们。"""
    for entry in registry.OPERATIONS:
        assert entry.requires == capability.REQUIREMENT_BUILTIN, entry.id
        assert registry.capability_snapshot(
            entry,
            office_ok=False,
            pdf_to_word_ok=False,
            heif_decode_ok=False,
            heif_encode_ok=False,
        ), entry.id


def test_operations_declare_their_own_metadata() -> None:
    """操作条目的 ``group`` / ``category`` 是手写的，池子由 ``group`` 推出。

    转换条目的 ``group`` 由源格式词汇推、池子由 ``group`` 推；操作条目
    没有源格式可推，所以 ``group`` 与 ``category`` 是**有意选的**
    （六个工具都产出一份 PDF、都挂在 ``/api/pdf`` 下，界面上一律归
    「PDF」栏 —— 用户找「合并 PDF」不会先去「图片」栏里翻）。

    但「池子由 ``group`` 推」这条规则**两边一样**，所以这里断言的是规则
    本身，不是抄一遍结果。PDF 那几个工具都经 PyMuPDF 处理 PDF，落 ``ocr``
    池不算错；而且它们根本不进队列，这个值是声明，不是路由依据。

    第十阶段 A 起 ``category``/``group`` **不再恒等于 PDF**（元数据查看器
    归「图片」栏），所以这一条改成断言「分类与分组在词汇表里、且池子
    确实由 group 推出来」—— 规则没变，只是不再假装结果只有一个值。
    """
    from conversion.registry import _WORKER_POOL_BY_GROUP
    from services.worker_pool import _REQUIREMENT_POOLS

    for entry in registry.OPERATIONS:
        assert entry.category in capability.CATEGORIES, entry.id
        assert entry.group in capability.GROUPS, entry.id
        assert entry.worker_pool == _WORKER_POOL_BY_GROUP[entry.group], entry.id
        # requires 仍然是「要哪个组件」，因此必须落在词汇表里
        assert entry.requires in _REQUIREMENT_POOLS, entry.id
        # 操作是多进多出 / 页面级操作，没有「一批同目标」的含义（决策 B）
        assert entry.supports_batch is False, entry.id
        assert entry.display_name, entry.id


def test_image_to_pdf_tool_carries_the_one_page_per_image_note() -> None:
    """§十 的「每张图片一页」是**说明**，不是控件。

    那个端点没有这个参数，「每图一页」结构上恒真 —— 发一个点了没反应的
    开关比不发更糟。所以它必须出现在 ``note`` 里被用户看到。
    """
    entry = registry.CAPABILITY_BY_ID["op.image-images-to-pdf"]

    assert entry.note and "每张图片一页" in entry.note
    assert "first" not in entry.note.lower()
    assert "只取第一帧" in entry.note  # 决策 C：多帧文件如实说明


# ----------------------------------------------------------------------
# B. 端点契约（这一组是这份文件存在的理由）
# ----------------------------------------------------------------------

def test_every_endpoint_really_exists() -> None:
    """条目上写的 ``(endpoint, method)`` 必须真的由应用提供。

    两个证据都要：OpenAPI 里能看到（前端拿得到），且某个 router 真的
    声明了它（不是靠别处巧合凑出来的路径）。
    """
    for entry in registry.OPERATIONS:
        route = route_for(entry.endpoint)
        assert entry.method in SERVED[entry.endpoint], entry.id
        assert entry.method in set(getattr(route, "methods", ()) or ()), entry.id


def test_options_match_the_endpoint_form_fields_exactly() -> None:
    """``options_schema`` 的键 == 端点 Form 字段 - 非用户可选项。

    **相等**，不是包含：少一个字段，参数面板就少一个控件（用户改不了）；
    多一个旋钮，那个控件点了没反应。两种都是用户能看见的谎。
    """
    for entry in registry.OPERATIONS:
        route = route_for(entry.endpoint)
        declared = set(inspect.signature(route.endpoint).parameters)
        expected = declared - set(NOT_USER_OPTIONS)

        assert set(specs_of(entry)) == expected, entry.id


def test_excluded_parameters_really_exist() -> None:
    """白名单里的名字必须真的在某个端点上 —— 否则它只是掩盖了一个改名。"""
    declared: set[str] = set()
    for entry in registry.OPERATIONS:
        route = route_for(entry.endpoint)
        declared |= set(inspect.signature(route.endpoint).parameters)

    for name in NOT_USER_OPTIONS:
        assert name in declared, name
    # 每个端点都恰好只有这两类被排除掉，没有第三个
    for entry in registry.OPERATIONS:
        route = route_for(entry.endpoint)
        declared = set(inspect.signature(route.endpoint).parameters)
        assert declared & set(NOT_USER_OPTIONS) <= set(NOT_USER_OPTIONS)


def test_required_options_have_no_default_in_the_endpoint() -> None:
    """``required`` 与端点默认值必须对得上，两个方向都要。

    端点上这些字段是 ``Form(...)``，所以「有没有默认值」是可读的：

    * 标了 ``required=True`` 的键，端点**不能自己给一个值**（``Form("")``
      / ``Form(None)`` 是「没给」的意思，合格；``Form("1-1")`` 就意味着
      用户不填也会拿到一个默认结果，界面上的星号是假的）；
    * 标了 ``required=False`` 的键，端点**必须有默认值** —— 否则那个可选
      控件留空时 FastAPI 直接 422，用户看到的是一句「请求参数不完整」，
      而界面明明说这一项可以不填。
    """
    for entry in registry.OPERATIONS:
        route = route_for(entry.endpoint)
        for spec in entry.options:
            default = declared_default(route, spec.key)
            if spec.required:
                assert default in ("", None), (entry.id, spec.key, default)
            else:
                assert default is not _NO_DEFAULT, (entry.id, spec.key)


def test_required_options_are_backed_by_a_real_rejection() -> None:
    """``required=True`` 不能只是界面上的一颗星 —— 服务端必须真的拒绝空值。

    「页面提取」「页面删除」的端点上 ``pages`` 是 ``Form("")``，所以
    FastAPI 不会 422；拒绝发生在解析层。不填就点按钮，得到的会是一份
    和原文件一样的 PDF，用户以为功能没生效 —— 所以那一层必须拦住。
    """
    from routers import pdf_params
    from utils.errors import ValidationError

    for raw in ("", "   ", None):
        with pytest.raises(ValidationError):
            pdf_params.parse_required_page_range(raw, "请填写要提取的页码")

    # 非必填的那一支（拆分 / 每页一个 PDF）反过来必须放行
    assert pdf_params.parse_split_value("", "every") is None


def test_option_keys_never_appear_twice_in_one_entry() -> None:
    for entry in registry.OPERATIONS:
        keys = [spec.key for spec in entry.options]
        assert len(keys) == len(set(keys)), entry.id


# ----------------------------------------------------------------------
# C. 枚举取值与真实词表对账
# ----------------------------------------------------------------------

def test_split_mode_matches_the_splitter() -> None:
    entry = registry.CAPABILITY_BY_ID["op.pdf-split"]
    spec = specs_of(entry)["mode"]

    assert values_of(spec) == SPLIT_MODES
    assert dict(spec.values) == SPLIT_MODE_LABELS
    assert spec.default == SPLIT_MODES[0]


def test_compress_level_matches_the_compressor() -> None:
    entry = registry.CAPABILITY_BY_ID["op.pdf-compress"]
    spec = specs_of(entry)["level"]

    assert values_of(spec) == tuple(settings.PDF_COMPRESS_LEVELS)
    assert dict(spec.values) == {
        name: LEVEL_LABELS[name] for name in settings.PDF_COMPRESS_LEVELS
    }
    assert spec.default == settings.DEFAULT_PDF_COMPRESS_LEVEL


def test_compress_target_matches_the_compressor_presets() -> None:
    entry = registry.CAPABILITY_BY_ID["op.pdf-compress"]
    spec = specs_of(entry)["target"]

    assert values_of(spec) == tuple(TARGET_PRESETS_MB) + ("custom",)
    assert spec.default == "none"


def test_compress_target_mb_bounds_come_from_settings() -> None:
    entry = registry.CAPABILITY_BY_ID["op.pdf-compress"]
    spec = specs_of(entry)["target_mb"]

    from routers import pdf_params

    assert (spec.min, spec.max) == (
        settings.PDF_MIN_TARGET_MB,
        settings.PDF_MAX_TARGET_MB,
    )
    # 解析器用的是同一对数（第九阶段把它从两份合成了一份）
    assert (pdf_params._MIN_TARGET_MB, pdf_params._MAX_TARGET_MB) == (
        spec.min,
        spec.max,
    )
    assert spec.visible_when == (("target", "custom"),)


def test_images_to_pdf_layout_matches_the_engine() -> None:
    """页面大小 / 方向 / 适应方式 / 页边距四项与排版引擎的词表逐字一致。"""
    entry = registry.CAPABILITY_BY_ID["op.image-images-to-pdf"]
    specs = specs_of(entry)

    assert values_of(specs["page_size"]) == image_to_pdf.PAGE_SIZES
    assert values_of(specs["orientation"]) == image_to_pdf.ORIENTATIONS
    assert values_of(specs["fit"]) == image_to_pdf.FITS
    assert values_of(specs["margin"]) == image_to_pdf.MARGINS


def test_images_to_pdf_custom_mm_bounds_come_from_settings() -> None:
    entry = registry.CAPABILITY_BY_ID["op.image-images-to-pdf"]
    specs = specs_of(entry)

    for key in ("custom_width_mm", "custom_height_mm"):
        assert specs[key].min == settings.PDF_MIN_CUSTOM_MM, key
        assert specs[key].max == settings.PDF_MAX_CUSTOM_MM, key
        assert specs[key].visible_when == (("page_size", "custom"),)

    # 引擎自己的上下界与这里是同一对数
    assert (image_to_pdf._MIN_CUSTOM_MM, image_to_pdf._MAX_CUSTOM_MM) == (
        settings.PDF_MIN_CUSTOM_MM,
        settings.PDF_MAX_CUSTOM_MM,
    )


def test_layout_keys_are_shared_with_the_conversion_family() -> None:
    """四项排版选项**复用**同一批 spec 对象，不是各抄一份。

    同一个概念抄两遍，一遍改了另一遍忘改，用户就会看到界面上写着
    「10 毫米」而结果按 12.7 毫米排版。
    """
    entry = registry.CAPABILITY_BY_ID["op.image-images-to-pdf"]
    conversion = registry.CAPABILITY_BY_PAIR[(registry.SOURCE_PNG, registry.TARGET_PDF)]

    operation_specs = specs_of(entry)
    for spec in conversion.options:
        if spec.key in operation_specs:
            assert operation_specs[spec.key] is spec, spec.key


def test_operation_option_table_covers_every_operation() -> None:
    """参数表与条目表必须一一对应（注册表用的是硬下标，漏登记会炸）。"""
    assert set(options.operation_ids()) == {entry.id for entry in registry.OPERATIONS}


# ----------------------------------------------------------------------
# D. API
# ----------------------------------------------------------------------

ENDPOINT = "/api/conversion/capabilities"


def capabilities(client: TestClient, params: dict | None = None) -> dict:
    response = client.get(ENDPOINT, params=params or {})
    assert response.status_code == 200, response.text
    return response.json()


def test_operations_are_published_with_their_endpoints(client: TestClient) -> None:
    body = capabilities(client)
    published = {item["id"]: item for item in body["operations"]}

    assert tuple(published) == EXPECTED_IDS
    for entry in registry.OPERATIONS:
        item = published[entry.id]
        assert item["operation_type"] == "operation"
        assert item["endpoint"] == entry.endpoint
        assert item["method"] == "POST"
        assert item["available"] is True
        # 「有没有参数」与「参数长什么样」必须一致：有 schema 才允许
        # 界面渲染面板，没有就别渲染一个空壳。
        assert item["supports_options"] is bool(entry.options)
        assert (item["options_schema"] is not None) is bool(entry.options)


def test_operations_never_leak_a_converter_key(client: TestClient) -> None:
    """``converter_key`` 是服务端分发细节（直指一个函数名），不出网。

    操作条目本来就没有它，但这条守的是**将来**：哪天有人给 operation
    也填一个 converter_key（那意味着它偷偷进了统一队列，与决策 B 相悖），
    这里会当场红。
    """
    published = capabilities(client)["operations"]

    for item in published:
        assert "converter_key" not in item, item["id"]
    assert "converter_key" not in str(published)
    assert all(entry.converter_key is None for entry in registry.OPERATIONS)


def test_operation_filters_split_the_catalog(client: TestClient) -> None:
    only_tools = capabilities(client, params={"operation_type": "operation"})
    assert [item["id"] for item in only_tools["operations"]] == list(EXPECTED_IDS)
    assert only_tools["conversions"] == []
    assert only_tools["matrix"] == {}

    only_conversions = capabilities(client, params={"operation_type": "conversion"})
    assert only_conversions["operations"] == []
    assert only_conversions["conversions"]


def test_operation_options_are_not_accepted_by_the_task_endpoint(
    client: TestClient,
) -> None:
    """``level`` / ``target`` 是那个端点的 Form 字段，不是 ``/tasks`` 的选项。

    两套参数不能互相串门：``/tasks`` 的白名单取自**转换**条目的并集
    （``options.union_specs`` 明确不收 operation），所以这里必须 400。

    **为什么键名长得不一样不是 bug**：操作面板的键名**照抄端点 Form 字段**
    （``custom_width_mm``），因为那个面板提交给的就是那个端点；
    ``/tasks`` 的白名单用的是点号键（``page_size.width_mm``），因为它要
    穿越五层校验链再落到既有解析器。同一个概念在两条提交路径上各有一个
    入口名，这正是 §二十二「参数面板由 schema 驱动」的代价 —— 而契约测试
    保证每一个名字都真的被某个端点接受，不会有一个名字两边都不认。
    """
    response = client.post(
        "/api/conversion/tasks",
        files={"files": ("a.png", b"\x89PNG\r\n\x1a\n", "image/png")},
        data={"target_type": "pdf", "options": '{"level":"strong"}'},
    )

    assert response.status_code == 400, response.text
    assert "level" in response.text
    # 而且不许把服务端内部的东西抖出来
    assert "Traceback" not in response.text


@pytest.mark.parametrize("key", ["mode", "pages", "target_mb", "custom_width_mm"])
def test_every_operation_only_key_is_rejected_by_the_task_endpoint(
    client: TestClient, key: str
) -> None:
    response = client.post(
        "/api/conversion/tasks",
        files={"files": ("a.png", b"\x89PNG\r\n\x1a\n", "image/png")},
        data={"target_type": "pdf", "options": f'{{"{key}":"1"}}'},
    )

    assert response.status_code == 400, (key, response.text)


@pytest.mark.parametrize(
    ("target_type", "payload"),
    [("pdf", '{"page_size":"a4"}'), ("png", '{"resize.mode":"small"}')],
)
def test_conversion_options_are_still_accepted_by_the_task_endpoint(
    client: TestClient, target_type: str, payload: str
) -> None:
    """上面那两条拒绝的**对照**：合法的转换选项照样进得去。

    没有这一条，一个「不管传什么都 400」的 bug 也能让那两条测试全绿 ——
    那时它们守的就不是白名单，而是「这个接口坏了」。
    """
    response = client.post(
        "/api/conversion/tasks",
        files={"files": ("a.png", b"\x89PNG\r\n\x1a\n", "image/png")},
        data={"target_type": target_type, "options": payload},
    )

    assert response.status_code == 202, (target_type, payload, response.text)


# ----------------------------------------------------------------------
# 输入怎么送（第九阶段 9b）：条目说的必须与端点真的收的对上
# ----------------------------------------------------------------------


def test_declared_input_field_really_exists_on_the_endpoint() -> None:
    """``input_field`` 是**端点上真实的 Form 字段名**，不是自造的词。

    统一中心照着它决定「直接传文件」还是「先换一个上传令牌」——
    写错了不会报错，只会让那一个工具在界面上永远调用失败。所以这里
    拿 ``inspect.signature`` 对账：声明的那个名字必须真的在签名里。
    """
    for entry in registry.OPERATIONS:
        assert entry.input_field in capability.INPUT_KINDS, entry.id
        route = route_for(entry.endpoint)
        declared = set(inspect.signature(route.endpoint).parameters)
        assert entry.input_field in declared, entry.id


def test_input_field_matches_the_shape_of_the_endpoint() -> None:
    """**两个方向都对**：收 ``files`` 的端点不该再要令牌，反之亦然。

    只验「名字存在」是不够的 —— 一个端点同时有 ``files`` 与 ``input_id``
    时（今天没有，但将来可能有），上面那条会两个都放行。
    """
    for entry in registry.OPERATIONS:
        route = route_for(entry.endpoint)
        declared = set(inspect.signature(route.endpoint).parameters)
        # 每个端点上「文件/令牌」这一类字段恰好只有一个
        handles = declared & set(capability.INPUT_KINDS)
        assert handles == {entry.input_field}, (entry.id, handles)


def test_accepts_is_declared_for_every_operation() -> None:
    """六个工具都要说清楚收什么文件，而且必须是合法的源类型。

    没有这一条，界面只能靠猜（猜错的结果是让用户选一份 PDF 去「图片合成
    PDF」）。也不是随便一串字符串：每个都要在 ``SOURCE_TYPES`` 里，
    否则前端拿它去比对 capabilities 的格式清单时一个都对不上。
    """
    for entry in registry.OPERATIONS:
        assert entry.accepts, entry.id
        for source in entry.accepts:
            assert source in registry.SOURCE_TYPES, (entry.id, source)


#: 收图片的工具 —— **恰好这两个，一个不多一个不少**。
#:
#: 第九阶段这条测试叫 ``test_only_the_composer_accepts_images``，断言
#: 「只有图片合成 PDF 收图片」。第十阶段 A 加了只读的元数据查看器，
#: 名字里的 ``only`` 就不成立了。改法是**把新的精确集合写出来**，
#: 不是把断言放宽成「至少包含」—— 「谁还能收图片」正是这条测试要守的东西，
#: 放宽等于把它守的那个洞打开。
_IMAGE_ACCEPTING_OPERATIONS = ("op.image-images-to-pdf", "op.image-metadata")


def test_only_these_tools_accept_images() -> None:
    """收图片的工具就是 :data:`_IMAGE_ACCEPTING_OPERATIONS` 这两个。

    其余每一个都只收 PDF。多出一个收图片的工具，界面就会在「图片」栏里
    给用户一个他没期待过的入口，而这条断言当场变红。
    """
    by_id = {entry.id: entry for entry in registry.OPERATIONS}
    for tool_id in _IMAGE_ACCEPTING_OPERATIONS:
        assert by_id[tool_id].accepts == registry._IMAGE_SOURCES, tool_id

    image_sources = set(registry._IMAGE_SOURCES)
    for entry in registry.OPERATIONS:
        if entry.id in _IMAGE_ACCEPTING_OPERATIONS:
            continue
        assert entry.accepts == (registry.SOURCE_PDF,), entry.id
        assert not (set(entry.accepts) & image_sources), entry.id


def test_conversions_do_not_carry_operation_only_fields() -> None:
    """``accepts`` / ``input_field`` 是操作的字段，转换条目上必须是空的。

    转换的输入由 ``(source_type, target_type)`` 唯一确定，再填一份
    ``accepts`` 就是第二份真相（§二十三 的同一条道理）。
    """
    for entry in registry.CONVERSIONS:
        assert entry.accepts == (), entry.id
        assert entry.input_field is None, entry.id
