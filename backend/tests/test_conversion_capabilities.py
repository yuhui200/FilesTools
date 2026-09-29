"""能力矩阵端点（第七阶段 §七 / §二十八）。

``GET /api/conversion/capabilities`` 是前端渲染目标格式下拉框的唯一依据：
**界面上不能出现一个选了就报错的选项**（§十九）。所以这里既要验形状，
也要验「组件缺失时能力真的消失」，还要验那条容易被写错的规矩 ——
**OCR 不可用时 ``pdf → docx`` 必须保留**。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from config import settings
from compressors import heif
from conversion import capability, registry
from services import conversion_service
from services.office_converter import is_available as office_available
from services.pdf_to_docx import docx_available

ENDPOINT = "/api/conversion/capabilities"

OFFICE_SOURCES = (
    registry.SOURCE_DOC,
    registry.SOURCE_DOCX,
    registry.SOURCE_XLS,
    registry.SOURCE_XLSX,
    registry.SOURCE_PPT,
    registry.SOURCE_PPTX,
)
IMAGE_SOURCES = (
    registry.SOURCE_JPG,
    registry.SOURCE_PNG,
    registry.SOURCE_WEBP,
    registry.SOURCE_BMP,
    registry.SOURCE_GIF,
    registry.SOURCE_TIFF,
)
#: SVG 在界面上与那六种同栏，但在注册表里不属于 ``IMAGE_SOURCES``
#: （那份元组同时是「Pillow 能解码的格式」的名单）。
#: 见 ``test_conversion_registry.py`` 里同一处说明。
SVG_SOURCES = (registry.SOURCE_SVG,)
#: 「图片这一栏」全部源。只在「这一栏整体表现如何」的断言里用。
_IMAGE_GROUP_SOURCES = IMAGE_SOURCES + SVG_SOURCES


def capabilities(client: TestClient, params: dict | None = None) -> dict:
    response = client.get(ENDPOINT, params=params or {})
    assert response.status_code == 200, response.text
    return response.json()


# ----------------------------------------------------------------------
# 形状
# ----------------------------------------------------------------------

def test_capabilities_reports_the_whole_matrix(client: TestClient) -> None:
    """本机 LibreOffice 与 python-docx 都在，矩阵应当是完整的 11 行。"""
    assert office_available() and docx_available(), "本机应当具备全部转换组件"

    body = capabilities(client)

    assert body["office_available"] is True
    assert body["pdf_to_word_available"] is True
    assert set(body["matrix"]) == set(registry.SOURCE_TYPES)
    assert body["notes"] == []
    for source, targets in body["matrix"].items():
        assert targets, source
        assert set(targets) == set(registry.TARGETS_BY_SOURCE[source])


def test_groups_cover_every_source_exactly_once(client: TestClient) -> None:
    body = capabilities(client)

    groups = {group["key"]: group for group in body["groups"]}
    assert set(groups) == set(registry.GROUP_LABELS)
    assert [group["key"] for group in body["groups"]][0] is not None

    seen: list[str] = []
    for group in body["groups"]:
        assert group["label"] == registry.GROUP_LABELS[group["key"]]
        for source in group["sources"]:
            value = source["value"]
            assert registry.SOURCE_GROUPS[value] == group["key"]
            assert source["label"] == registry.SOURCE_LABELS[value]
            assert source["extensions"] == list(registry.EXTENSIONS_BY_SOURCE[value])
            assert source["targets"] == list(registry.TARGETS_BY_SOURCE[value])
            seen.append(value)
        # 这一组的目标是组内各源目标的并集，且按出现顺序去重
        union: list[str] = []
        for source in group["sources"]:
            for target in source["targets"]:
                if target not in union:
                    union.append(target)
        assert group["targets"] == union

    assert sorted(seen) == sorted(registry.SOURCE_TYPES)


def test_target_options_are_renderable(client: TestClient) -> None:
    """每个目标都要有中文名与结果扩展名，界面才不用自己写一张表。"""
    body = capabilities(client)

    options = {item["value"]: item for item in body["targets"]}
    assert set(options) == set(registry.TARGET_TYPES)
    for value, item in options.items():
        assert item["label"] == registry.TARGET_LABELS[value]
        assert item["extension"] == registry.EXTENSION_BY_TARGET[value]


def test_pdf_to_word_note_describes_ocr_honestly(client: TestClient) -> None:
    body = capabilities(client)

    assert body["ocr_available"] is True
    assert str(settings.PDF_TO_WORD_MAX_OCR_PAGES) in body["pdf_to_word_note"]


# ----------------------------------------------------------------------
# 组件缺失（§七：不可用的能力不能出现在选项里）
# ----------------------------------------------------------------------

def test_missing_libreoffice_removes_office_but_keeps_txt(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(conversion_service, "office_available", lambda: False)

    body = capabilities(client)

    assert body["office_available"] is False
    for source in OFFICE_SOURCES:
        assert source not in body["matrix"], source
    # TXT 走 PyMuPDF / python-docx / markup_render，一个都不经 LibreOffice
    assert body["matrix"][registry.SOURCE_TXT] == ["pdf", "docx", "html", "md"]
    assert body["matrix"][registry.SOURCE_PDF] == ["docx"]
    for source in IMAGE_SOURCES:
        assert source in body["matrix"]

    assert body["notes"], "缺组件时必须说明原因，不能悄悄少几项"
    assert any("未安装" in note for note in body["notes"])
    # 消失的源也不能出现在分组里（否则前端会渲染出一个空组）
    rendered = {
        source["value"] for group in body["groups"] for source in group["sources"]
    }
    assert not (rendered & set(OFFICE_SOURCES))


def test_missing_docx_component_removes_pdf_to_word(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(conversion_service, "docx_available", lambda: False)

    body = capabilities(client)

    assert body["pdf_to_word_available"] is False
    assert registry.SOURCE_PDF not in body["matrix"]
    # PDF 只是不能当源，仍然可以当目标
    assert ["pdf"] == body["matrix"][registry.SOURCE_DOCX]
    assert any("PDF 转 Word" in note for note in body["notes"])
    assert "docx" not in {item["value"] for item in body["targets"]}


def test_ocr_unavailable_keeps_pdf_to_word(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§七 的硬要求：OCR 不可用时 ``pdf → docx`` **仍然在矩阵里**。

    带文字层的 PDF 本来就能转，只是扫描页会转成空白。把整条能力藏掉
    是过度降级 —— 用户会以为这个网站根本不能把 PDF 转成 Word。
    """
    monkeypatch.setattr(conversion_service, "ocr_available", lambda: False)

    body = capabilities(client)

    assert body["ocr_available"] is False
    assert body["matrix"][registry.SOURCE_PDF] == ["docx"]
    assert "docx" in {item["value"] for item in body["targets"]}
    # 缺 OCR 不是「缺组件」，不该出现在能力缺失说明里
    assert body["notes"] == []
    # 但必须在提示里如实说明扫描件会怎样
    assert "文字层" in body["pdf_to_word_note"]
    assert "扫描" in body["pdf_to_word_note"]


def test_everything_missing_still_serves_images_and_txt(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """可选组件全缺时服务仍然可用（图片、TXT、HTML/Markdown 只依赖必装库）。"""
    monkeypatch.setattr(conversion_service, "office_available", lambda: False)
    monkeypatch.setattr(conversion_service, "docx_available", lambda: False)
    monkeypatch.setattr(conversion_service, "ocr_available", lambda: False)
    # HEIC 组件从第十阶段 A 起也是**可选**的（``pillow-heif``），
    # 所以「全缺」这一格必须把它一起关掉 —— 否则这台装了 pillow-heif 的
    # 开发机会让 ``heic`` 留在矩阵里，而这条测试的名字就不再成立。
    # 探测结果由 ``heif_support()`` 提供，这里换成「编解码都没有」。
    monkeypatch.setattr(
        conversion_service,
        "heif_support",
        lambda: heif.HeifSupport(reason="测试：假装没装"),
    )

    body = capabilities(client)

    # HTML / Markdown 与 TXT 一样，靠自写的纯标准库子集解析，
    # 不碰 LibreOffice / python-docx / OCR，所以在这一格里必须留下来 ——
    # 这也正是把它们登记为 ``markup`` 需求（而不是复用某个组件）的意义。
    # SVG 同理：它靠 PyMuPDF 自带的 SVG 解析器，同样是必装依赖。
    #
    # **HEIC 是唯一会消失的图片源**：它要的 ``pillow-heif`` 是可选组件，
    # 与 Office / python-docx 同一类。减掉它不是放宽断言 ——
    # 恰恰是断言「缺组件时它必须消失」（§八：不要伪造成功）。
    assert set(body["matrix"]) == (set(_IMAGE_GROUP_SOURCES) - {registry.SOURCE_HEIC}) | {
        registry.SOURCE_TXT,
        registry.SOURCE_HTML,
        registry.SOURCE_MD,
    }
    # 图片那六种格式两两互转 + PNG → ICO + 都能转 PDF；
    # ``html`` / ``txt`` / ``md`` 来自文本这一支（MD→HTML、HTML→TXT、TXT→MD），
    # 同样不依赖任何可选组件。**``docx`` 不在其中** —— 两个能产出它的
    # 能力（PDF→DOCX、TXT→DOCX）都要 python-docx，而那正是这一格里
    # 被拿掉的那个组件。**``heic`` 同样不在** —— 那是 HEIC 编码器。
    assert {item["value"] for item in body["targets"]} == {
        "jpg", "png", "webp", "bmp", "gif", "tiff", "ico", "pdf",
        "html", "txt", "md",
    }
    # 三句：LibreOffice、python-docx、HEIC。编码器那**不算第四句**
    # （``unavailable_reasons`` 里的 ``elif``），连解码都没有时
    # 再说「不能转成 HEIC」没有信息量。
    assert len(body["notes"]) == 3


# ----------------------------------------------------------------------
# 错误码（§十七：新错误码必须能被前端发现）
# ----------------------------------------------------------------------

def test_new_error_codes_are_published(client: TestClient) -> None:
    """前端按 /api/config 的 error_codes 自检「有没有漏配文案」。"""
    config = client.get("/api/config").json()

    assert "UNSUPPORTED_CONVERSION" in config["error_codes"]
    assert "TASK_NOT_RETRYABLE" in config["error_codes"]


def test_capabilities_does_not_leak_internals(client: TestClient) -> None:
    import tempfile

    raw = str(capabilities(client))

    assert tempfile.gettempdir() not in raw
    assert "soffice" not in raw.lower()
    assert "Traceback" not in raw


# ----------------------------------------------------------------------
# 能力目录（第九阶段 §十四 / §十五 / §十六）
# ----------------------------------------------------------------------

#: §十四 点名的必备字段。少一个，前端就得回退到硬编码（§四十二 禁止）。
REQUIRED_ENTRY_KEYS = (
    "id",
    "source_type",
    "target_type",
    "operation_type",
    "display_name",
    "category",
    "group",
    "requires",
    "worker_pool",
    "available",
    "supports_batch",
    "supports_options",
    "supports_preview",
)


def test_new_catalog_keys_are_published(client: TestClient) -> None:
    body = capabilities(client)

    assert [item["value"] for item in body["categories"]] == ["image", "document", "pdf"]
    for item in body["categories"]:
        assert item["label"] == capability.CATEGORY_LABELS[item["value"]]
    assert body["formats"] and body["conversions"]
    # 第九阶段第 11 步：六个 PDF 工具登记为 operation 条目（决策 B）。
    # 它们**不进统一队列**，所以只出现在 operations[] 里，conversions[] 里没有。
    assert [item["id"] for item in body["operations"]] == [
        "op.pdf-merge",
        "op.pdf-split",
        "op.pdf-compress",
        "op.pdf-extract-pages",
        "op.pdf-delete-pages",
        "op.image-images-to-pdf",
        # 第十阶段 A §三十四
        "op.image-metadata",
    ]
    assert all(item["operation_type"] == "operation" for item in body["operations"])
    assert all(item["operation_type"] == "conversion" for item in body["conversions"])


def test_old_keys_are_byte_identical_without_filters(client: TestClient) -> None:
    """不带查询参数时，第七阶段的几个键必须与注册表逐字节一致。

    加了四个新键、又给旧键接上过滤器，最容易出的事就是顺手把旧键的形状
    也改了 —— 旧前端会当场白屏。
    """
    body = capabilities(client)

    assert body["matrix"] == {
        source: list(targets) for source, targets in registry.TARGETS_BY_SOURCE.items()
    }
    assert [group["key"] for group in body["groups"]] == [
        group
        for group in ("image", "office", "text", "pdf")
        if any(
            registry.SOURCE_GROUPS[source] == group
            for source in registry.TARGETS_BY_SOURCE
        )
    ]
    assert [item["value"] for item in body["targets"]] == list(registry.TARGET_TYPES)


def test_every_conversion_entry_has_the_required_fields(client: TestClient) -> None:
    for entry in capabilities(client)["conversions"]:
        for key in REQUIRED_ENTRY_KEYS:
            assert key in entry, (entry.get("id"), key)
        assert entry["operation_type"] == "conversion"
        assert registry.CAPABILITY_BY_ID[entry["id"]] is not None


def test_entries_never_leak_the_dispatch_key(client: TestClient) -> None:
    """``converter_key`` 是服务端的分发细节，不能出现在任何一条响应里。

    它直接对应一个 Python 函数名 —— 泄漏出去就成了一套没人敢改的隐性契约。
    """
    assert "converter_key" not in str(capabilities(client))


def test_ids_are_unique_and_reversible(client: TestClient) -> None:
    ids = [entry["id"] for entry in capabilities(client)["conversions"]]
    assert len(ids) == len(set(ids))
    for entry in capabilities(client)["conversions"]:
        assert capability.parse_capability_id(entry["id"]) == (
            entry["category"],
            entry["source_type"],
            entry["target_type"],
        )


def test_formats_carry_labels_extensions_and_mime(client: TestClient) -> None:
    body = capabilities(client)

    formats = {item["value"]: item for item in body["formats"]}
    assert set(formats) == set(registry.SOURCE_TYPES) | set(registry.TARGET_TYPES)
    for value, item in formats.items():
        assert item["label"] == registry.format_label(value)
        assert item["category"] == registry.format_category(value)
        assert item["extensions"] == list(registry.EXTENSIONS_BY_SOURCE.get(value, ()))
        assert item["media_type"] == registry.media_type_for_target(value)
        assert item["is_source"] is (value in registry.SOURCE_TYPES)
        assert item["is_target"] is (value in registry.TARGET_TYPES)
        # 扩展名与显示名不能是空的，否则界面只能显示一个裸标识符
        assert item["label"] and item["media_type"]


def test_options_schema_is_present_exactly_when_there_are_options(
    client: TestClient,
) -> None:
    """``supports_options`` 与 ``options_schema`` 必须同真同假。

    两者一旦分叉，界面就会渲染出一个空面板（说支持、却没有可渲染的东西），
    或者藏起一个真的存在的参数。
    """
    for entry in capabilities(client)["conversions"]:
        has_schema = entry["options_schema"] is not None
        assert has_schema is entry["supports_options"], entry["id"]
        if has_schema:
            assert entry["options_schema"]["version"] == 1
            assert entry["options_schema"]["items"]


def test_image_options_are_trimmed_per_target(client: TestClient) -> None:
    """质量只出现在有损格式上 —— 界面不摆点了没反应的旋钮（§九）。"""
    by_id = {entry["id"]: entry for entry in capabilities(client)["conversions"]}

    def keys(capability_id: str) -> set[str]:
        schema = by_id[capability_id]["options_schema"]
        return {item["key"] for item in schema["items"]} if schema else set()

    assert "quality" in keys("image.png-to-jpg")
    assert "quality" not in keys("image.jpg-to-png")
    assert "quality" not in keys("image.jpg-to-pdf")
    assert "page_size" in keys("image.jpg-to-pdf")
    assert keys("document.doc-to-pdf") == set()
    assert keys("pdf.pdf-to-docx") == set()


# ----------------------------------------------------------------------
# 查询参数（§十六）
# ----------------------------------------------------------------------

def test_filter_by_source_type(client: TestClient) -> None:
    body = capabilities(client, params={"source_type": "txt"})

    assert body["matrix"] == {"txt": ["pdf", "docx", "html", "md"]}
    assert [entry["id"] for entry in body["conversions"]] == [
        "document.txt-to-pdf",
        "document.txt-to-docx",
        "document.txt-to-html",
        "document.txt-to-md",
    ]
    assert {item["value"] for item in body["formats"]} == {"txt"}


def test_filter_by_target_type(client: TestClient) -> None:
    body = capabilities(client, params={"target_type": "docx"})

    assert body["matrix"] == {"pdf": ["docx"], "txt": ["docx"]}
    assert [entry["id"] for entry in body["conversions"]] == [
        "document.txt-to-docx",
        "pdf.pdf-to-docx",
    ]


def test_filter_by_category(client: TestClient) -> None:
    body = capabilities(client, params={"category": "pdf"})

    assert set(body["matrix"]) == {"pdf"}
    assert {entry["category"] for entry in body["conversions"]} == {"pdf"}


def test_operation_type_filter_empties_the_conversion_matrix(client: TestClient) -> None:
    """只看工具时，转换矩阵整个空掉 —— 那正是「这一问没有匹配」的诚实答案。"""
    body = capabilities(client, params={"operation_type": "operation"})

    assert body["matrix"] == {}
    assert body["groups"] == []
    assert body["conversions"] == []


def test_unknown_category_is_a_clear_error(client: TestClient) -> None:
    """``category`` 是服务端自己的闭集，取值写错属于客户端 bug。"""
    response = client.get(ENDPOINT, params={"category": "nope"})

    assert response.status_code == 400
    assert "category" in response.text


def test_unknown_source_type_is_just_an_empty_result(client: TestClient) -> None:
    """格式是数据驱动的（以后会变多），未知值只是「没有匹配」。"""
    response = client.get(ENDPOINT, params={"source_type": "exe"})

    assert response.status_code == 200
    assert response.json()["matrix"] == {}
    assert response.json()["conversions"] == []


def test_unavailable_conversions_are_listed_with_available_false(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**matrix 与 conversions 的一处有意差异**。

    没装 LibreOffice 时 Office 行从 ``matrix`` 整体消失（第七阶段的行为，
    前端的分组卡片靠它）；``conversions`` 仍然列出那条能力并如实标注
    ``available: false``，理由在 ``notes`` 里 —— 一个永远为 true 的
    ``available`` 字段是没有意义的（§十四）。
    """
    monkeypatch.setattr(conversion_service, "office_available", lambda: False)

    body = capabilities(client)

    assert "docx" not in body["matrix"]
    entry = next(
        item for item in body["conversions"] if item["id"] == "document.docx-to-pdf"
    )
    assert entry["available"] is False
    # 图片与 TXT 不受影响
    assert next(
        item for item in body["conversions"] if item["id"] == "image.jpg-to-png"
    )["available"] is True
    assert body["notes"]


def test_dynamic_enum_is_filled_by_the_services_layer(client: TestClient) -> None:
    """纯层放行的空壳枚举，到线上必须**真的有得选**。

    ``conversion.options`` 只说「字体由运行期决定」（它不该知道服务器装了
    什么），填充是 services 层的活。两边都不做的话，前端拿到的就是
    一个 ``type: "enum"`` 却 ``enum: []`` 的控件 —— 渲染出一个空的单选组，
    用户点不动，还以为是页面坏了。这条测试钉住接力的第二棒。
    """
    body = capabilities(client)
    entry = next(
        item for item in body["conversions"] if item["id"] == "document.txt-to-pdf"
    )
    font = next(item for item in entry["options_schema"]["items"] if item["key"] == "font")

    assert font["type"] == "enum"
    assert font["dynamic"] == "fonts"
    assert len(font["enum"]) >= 1, "线上枚举是空的，前端会渲染出一个没有选项的单选组"
    for choice in font["enum"]:
        assert choice["value"] and choice["label"]
    # 默认值必须落在可选项里，否则表单初始状态就是一个非法值
    assert font["default"] in {choice["value"] for choice in font["enum"]}
    assert font["default_label"] in {choice["label"] for choice in font["enum"]}


def test_dynamic_enum_never_leaks_a_path(client: TestClient) -> None:
    """字体选项只出 ``(键, 显示名)``。

    §三十四：返回给用户的东西里不能出现服务器路径。字体列表来自扫描本机
    字体目录，是**最容易**顺手把路径带出去的一处，所以单独钉一条。
    """
    raw = client.get(ENDPOINT).text
    for marker in ("C:", "\\\\", "/usr/", "fonts/", ".ttf", ".ttc", ".otf", "Fonts"):
        assert marker not in raw, f"能力响应的正文里出现了疑似路径的片段：{marker}"


def test_only_the_font_enum_is_resolved_at_runtime(client: TestClient) -> None:
    """需要运行期探测的**只有字体**，且只有「会排版文字的那四条」用它。

    TXT→DOCX/HTML/MD 与 HTML/MD→PDF 都要落字，所以都要字体；图片与 Office
    那几路一个动态枚举都没有。这条是防止 resolver 把不该动的东西动了
    （例如把 ``quality`` 的 10–100 换成一串字体名），也防止将来新增的
    排版能力漏了字体选项。真的多了第二个动态枚举时，就在这里显式加一行。
    """
    body = capabilities(client)
    dynamic = {
        (entry["id"], item["key"])
        for entry in body["conversions"] + body["operations"]
        for item in (entry["options_schema"] or {}).get("items", [])
        if item.get("dynamic")
    }
    assert dynamic == {
        ("document.txt-to-docx", "font"),
        ("document.txt-to-pdf", "font"),
        ("document.html-to-pdf", "font"),
        ("document.md-to-pdf", "font"),
    }, dynamic

    # 反过来：这四条之外，任何 schema 里都不该冒出 ``enum: []`` 的空控件
    empty = [
        (entry["id"], item["key"])
        for entry in body["conversions"] + body["operations"]
        for item in (entry["options_schema"] or {}).get("items", [])
        if item["type"] == "enum" and not item.get("enum")
    ]
    assert empty == [], f"这些枚举一个可选项都没有：{empty}"


def test_published_keys_cover_everything_the_entry_carries(client: TestClient) -> None:
    """**发布层的字段必须与条目的 ``to_json`` 逐字对齐**，一个都不能少。

    这条是真被咬过才写的：``Capability.to_json`` 加了 ``accepts`` /
    ``input_field``，而路由的响应模型 ``ConversionCapabilityItem`` 是个
    Pydantic 模型 —— 它**默默地**把没声明的键丢掉，接口照常 200，
    前端却永远拿不到那两个键（界面于是放行错的文件类型，还不报错）。
    两份字段表放在两个文件里，就是两份真相；这条测试把它们钉在一起。
    """
    from routers.conversion_schemas import ConversionCapabilityItem

    declared = set(ConversionCapabilityItem.model_fields)
    for entry in registry.CAPABILITIES:
        emitted = set(entry.to_json(available=True))
        assert emitted == declared, (entry.id, emitted ^ declared)
    # 反向也要能验：拿一条真的响应，确认字段没在中途被削掉
    for item in capabilities(client)["conversions"] + capabilities(client)["operations"]:
        assert set(item) == declared, (item.get("id"), set(item) ^ declared)
