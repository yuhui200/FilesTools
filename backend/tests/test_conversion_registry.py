"""转换注册表（第七阶段 §1）。

这一层是纯函数，所以测试也不需要数据库、不需要 HTTP、不需要转换组件。
关心的只有一件事：**注册表说的「支持」与代码里真的有的实现是否一致**。

矩阵里多写一格（用户点了却报错）比少写一格严重得多，所以这里对
「每一格都必须有实现」的检查是逐格做的，不是抽查。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from compressors.encoder import OUTPUT_FORMATS, extension_for, normalize_format
from config import settings
from conversion import capability, options as option_vocab, registry
from office import markup_parse, markup_render
from services import markup_service
from services.intake import media_type_for
from services.pdf_service import DOCX_MEDIA_TYPE, PDF_MEDIA_TYPE

#: 走 ``compressors.encoder`` 的目标格式。``pdf`` / ``docx`` 不在其中 ——
#: 它们各有自己的写入器（PyMuPDF / python-docx），不经图片编码器。
IMAGE_TARGETS = ("jpg", "png", "webp", "bmp", "gif", "tiff", "ico", "heic")

IMAGE_SOURCES = (
    registry.SOURCE_JPG,
    registry.SOURCE_PNG,
    registry.SOURCE_WEBP,
    registry.SOURCE_BMP,
    registry.SOURCE_GIF,
    registry.SOURCE_TIFF,
    # HEIC 从第十阶段 A 起在这份元组里。它符合这份元组的身份
    # （「Pillow 能解码的格式」，见 ``SVG_SOURCES`` 的说明）：``pillow-heif``
    # 是 Pillow 的一个**解码器插件**，装上之后 ``Image.open`` 就能读它，
    # 与 JPEG 走的是同一条 Pillow 路径。它是不是**现在**可用由组件探测决定，
    # 那是另一件事 —— 能力矩阵那边管，这里管的是格式身份。
    registry.SOURCE_HEIC,
)

#: SVG 在**用户眼里**也是图片（界面上与那六种同栏），但在注册表里
#: 它**不属于** ``IMAGE_SOURCES``：那份元组的另一重身份是
#: 「Pillow 能解码的格式」，被 ``test_image_extensions_agree_with_settings``
#: 拿去和 ``settings.ALLOWED_IMAGE_EXTENSIONS`` 逐项对账。
#: 所以这里也分开写 —— 凡是「图片这一栏全都得留下」的断言，
#: 要把两份并起来（见下面的 ``_IMAGE_GROUP_SOURCES``）。
SVG_SOURCES = (registry.SOURCE_SVG,)
OFFICE_SOURCES = (
    registry.SOURCE_DOC,
    registry.SOURCE_DOCX,
    registry.SOURCE_XLS,
    registry.SOURCE_XLSX,
    registry.SOURCE_PPT,
    registry.SOURCE_PPTX,
)

#: 「图片这一栏」的全部源：Pillow 那六种 + SVG。
#: 只在「这一栏整体表现如何」的断言里用（可用的池子、缺组件时留没留下），
#: **不用**在扩展名/MIME 那种逐项对账上 —— 那里必须分开，理由见 ``SVG_SOURCES``。
_IMAGE_GROUP_SOURCES = IMAGE_SOURCES + SVG_SOURCES

#: 每一个「源 → 目标」格子对应的实现函数所在模块。
#: 这份表是**测试自己的**，故意与 registry 分开写：从 registry 推导出来的
#: 期望值是没有意义的（改错了也照样通过）。
#:
#: 第九阶段（§七）把图片那六行从三种格式扩到七种目标。``ico`` 只出现在
#: ``png`` 那一行 —— 决策 F 只要求 PNG → ICO。
#:
#: 第十阶段 A（§五 / §六）再加 HEIC：每种位图源多一个 ``heic`` 目标
#: （编码），HEIC 自己作为源有七个目标（解码）但**没有** ``heic``
#: —— 同格式不转，见 ``test_no_self_conversion``。``heic`` 排在 ``ico``
#: 之后、``pdf`` 之前，跟着 ``_IMAGE_TARGET_ORDER`` 的追加位置走。
EXPECTED_TARGETS = {
    registry.SOURCE_JPG: ("png", "webp", "bmp", "gif", "tiff", "heic", "pdf"),
    registry.SOURCE_PNG: ("jpg", "webp", "bmp", "gif", "tiff", "ico", "heic", "pdf"),
    registry.SOURCE_WEBP: ("jpg", "png", "bmp", "gif", "tiff", "heic", "pdf"),
    registry.SOURCE_BMP: ("jpg", "png", "webp", "gif", "tiff", "heic", "pdf"),
    registry.SOURCE_GIF: ("jpg", "png", "webp", "bmp", "tiff", "heic", "pdf"),
    registry.SOURCE_TIFF: ("jpg", "png", "webp", "bmp", "gif", "heic", "pdf"),
    # HEIC 作为源：七个目标，**不含 heic 自己**（同格式不转）。
    registry.SOURCE_HEIC: ("jpg", "png", "webp", "bmp", "gif", "tiff", "pdf"),
    # 第十阶段 A §九 字面：SVG→PNG/JPG/WEBP/PDF。**顺序按规格那句写的来**，
    # 不是按 ``_IMAGE_TARGET_ORDER`` 派生 —— 它不跟着位图那套走，
    # 理由见 ``registry._SVG_SOURCES``。
    registry.SOURCE_SVG: ("png", "jpg", "webp", "pdf"),
    registry.SOURCE_DOC: ("pdf",),
    registry.SOURCE_DOCX: ("pdf",),
    registry.SOURCE_XLS: ("pdf",),
    registry.SOURCE_XLSX: ("pdf",),
    registry.SOURCE_PPT: ("pdf",),
    registry.SOURCE_PPTX: ("pdf",),
    registry.SOURCE_TXT: ("pdf", "docx", "html", "md"),
    # §六 字面：HTML→PDF/TXT、MD→HTML/PDF/TXT。**HTML→Markdown 不在其中** ——
    # 规格没点这一条，能实现不等于该实现。
    registry.SOURCE_HTML: ("pdf", "txt"),
    registry.SOURCE_MD: ("html", "pdf", "txt"),
    registry.SOURCE_PDF: ("docx",),
}


# ----------------------------------------------------------------------
# 表的完整性
# ----------------------------------------------------------------------

def test_every_source_has_a_full_row() -> None:
    """每一种源都要有目标、扩展名、显示名、分组、组件要求。"""
    for source in registry.SOURCE_TYPES:
        assert registry.TARGETS_BY_SOURCE.get(source), f"{source} 没有可选目标"
        assert registry.EXTENSIONS_BY_SOURCE.get(source), f"{source} 没有扩展名"
        assert registry.SOURCE_LABELS.get(source), f"{source} 没有显示名"
        assert registry.SOURCE_GROUPS.get(source) in registry.GROUP_LABELS
        assert registry.SOURCE_REQUIREMENTS.get(source)


def test_matrix_matches_the_real_implementations() -> None:
    """矩阵的每一格都要与实现一一对应（对照表写在本文件顶部）。"""
    assert set(registry.TARGETS_BY_SOURCE) == set(EXPECTED_TARGETS)
    for source, targets in EXPECTED_TARGETS.items():
        assert set(registry.TARGETS_BY_SOURCE[source]) == set(targets), source


def test_every_target_is_usable() -> None:
    """出现过的目标都必须有显示名、扩展名、MIME。"""
    used = {t for targets in registry.TARGETS_BY_SOURCE.values() for t in targets}
    assert used == set(registry.TARGET_TYPES)
    for target in used:
        assert registry.TARGET_LABELS[target]
        assert registry.EXTENSION_BY_TARGET[target].startswith(".")
        assert registry.MEDIA_TYPE_BY_TARGET[target]


def test_no_self_conversion() -> None:
    """同格式转换不列：那是一次无意义的重新编码，不是「换个格式」。"""
    for source in registry.SOURCE_TYPES:
        assert not registry.supports(source, source), source


def test_extensions_are_unique_across_sources() -> None:
    """一个扩展名只能属于一种源，否则分派会出现两种解释。"""
    seen: dict[str, str] = {}
    for source, extensions in registry.EXTENSIONS_BY_SOURCE.items():
        for ext in extensions:
            assert ext == ext.lower() and ext.startswith(".")
            assert ext not in seen, f"{ext} 同时属于 {seen[ext]} 与 {source}"
            seen[ext] = source
    assert registry.EXTENSION_TO_SOURCE == seen


# ----------------------------------------------------------------------
# 与既有配置的一致性（改了任何一边，这里就会红）
# ----------------------------------------------------------------------

def test_image_extensions_agree_with_settings() -> None:
    """图片那几行的扩展名必须与 ``settings.ALLOWED_IMAGE_EXTENSIONS`` 一致。

    这条是**双向**的，两个方向都真的会出事：

    * 注册表多了、白名单少了 → 能力 API 说「支持 BMP」，用户传上来却
      在 ``check_extension`` 就被拒；
    * 白名单多了、注册表少了 → 文件通过了上传校验，走到 ``detect_source``
      却找不到源类型，报一句自相矛盾的「暂不支持这种文件」。

    ``.ico`` 因此**不在**这份集合里：它是目标格式，不是输入格式（§七）。
    """
    declared = {
        ext for source in IMAGE_SOURCES for ext in registry.EXTENSIONS_BY_SOURCE[source]
    }
    assert declared == {ext.lower() for ext in settings.ALLOWED_IMAGE_EXTENSIONS}
    assert ".ico" not in declared


def test_office_extensions_agree_with_settings() -> None:
    """Office 六类必须与各自的 ``ALLOWED_*_EXTENSIONS`` 一致。

    对不上的后果很具体：文件在转换中心的上传白名单里通过了，
    却在真正调用 ``doc_service`` 时被第二道校验拒掉。
    """
    pairs = (
        (settings.ALLOWED_WORD_EXTENSIONS, (registry.SOURCE_DOC, registry.SOURCE_DOCX)),
        (settings.ALLOWED_EXCEL_EXTENSIONS, (registry.SOURCE_XLS, registry.SOURCE_XLSX)),
        (
            settings.ALLOWED_POWERPOINT_EXTENSIONS,
            (registry.SOURCE_PPT, registry.SOURCE_PPTX),
        ),
    )
    for allowed, sources in pairs:
        declared = {
            ext for source in sources for ext in registry.EXTENSIONS_BY_SOURCE[source]
        }
        assert declared == {ext.lower() for ext in allowed}, sources


def test_pdf_and_text_extensions_agree_with_settings() -> None:
    assert set(registry.EXTENSIONS_BY_SOURCE[registry.SOURCE_PDF]) == {
        ext.lower() for ext in settings.ALLOWED_PDF_EXTENSIONS
    }
    assert set(registry.EXTENSIONS_BY_SOURCE[registry.SOURCE_TXT]) == {
        ext.lower() for ext in settings.ALLOWED_TEXT_EXTENSIONS
    }


def test_markup_extensions_agree_with_settings_and_the_parser() -> None:
    """HTML / Markdown 的扩展名在**三处**出现过，必须逐字相等。

    注册表这份是手写的，``settings.ALLOWED_MARKUP_EXTENSIONS`` 是配置，
    ``markup_parse.allowed_extensions()`` 是解析器自己的视图。三者漂移的
    后果不是「少一个格式」这么轻：用户在转换中心能选中一个 ``.md`` 文件，
    提交后却在校验层被拒 —— 报的还是「暂不支持这种文件」。
    """
    declared = {
        ext
        for source in registry.MARKUP_SOURCES
        for ext in registry.EXTENSIONS_BY_SOURCE[source]
    }
    assert declared == {ext.lower() for ext in settings.ALLOWED_MARKUP_EXTENSIONS}
    assert declared == markup_parse.allowed_extensions()


def test_markup_sources_map_to_parse_formats() -> None:
    """每个 markup 源都要有解析格式，且格式本身解析器认得。

    两边的词汇不同（注册表说 ``md``，解析器说 ``markdown``），靠
    ``markup_service.SOURCE_FORMAT_BY_TYPE`` 连起来。漏一格的后果是
    那个格式的用户全部拿到 500，而不是一句看得懂的提示。
    """
    for source in registry.MARKUP_SOURCES:
        parse_format = markup_service.SOURCE_FORMAT_BY_TYPE.get(source)
        assert parse_format in (markup_parse.SOURCE_HTML, markup_parse.SOURCE_MARKDOWN), source
        # 解析器要真的认这个格式，而不是只在常量表里有个名字
        assert markup_parse.parse_markup("<p>x</p>" if parse_format == markup_parse.SOURCE_HTML
                                        else "x\n", parse_format).blocks is not None


def test_markup_targets_map_to_render_targets() -> None:
    """markup 源能产出的每个文本目标都要有渲染格式，且渲染器真的认它。

    写成「产出 ⇒ 有映射」而不是「产出 == 某个固定集合」：第 10 步会为
    TXT 加出 Markdown 目标，那时这条不需要改，而漏配一格映射仍然会红。
    """
    produced = {
        target
        for source in registry.MARKUP_SOURCES
        for target in registry.TARGETS_BY_SOURCE[source]
    }
    # PDF 走排版器（``markup.to_pdf``），不经过 ``markup_render``
    assert produced - {registry.TARGET_PDF}
    for target in produced - {registry.TARGET_PDF}:
        render_target = markup_service.MARKUP_RENDER_TARGETS.get(target)
        assert render_target is not None, target
        # 渲染器不认的目标会抛 ValueError —— 这里让它真的渲染一次
        markup_render.render_markup([], render_target)

    # 反向：映射里的每个键都必须是注册表认得的目标词汇，
    # 不能是一个谁也不认识的字符串
    for target in markup_service.MARKUP_RENDER_TARGETS:
        assert target in registry.TARGET_ORDER, target


def test_markup_sources_have_a_converter_for_every_target() -> None:
    """markup 的每一格都要落到一个真实的家族实现上。

    注册表说「能转」而 ``CONVERTERS`` 里没有对应叶子，用户点下去只会
    拿到 503。逐格查，不抽查。
    """
    expected = {
        registry.TARGET_PDF: option_vocab.MARKUP_TO_PDF,
        registry.TARGET_HTML: option_vocab.MARKUP_CONVERT,
        registry.TARGET_TXT: option_vocab.MARKUP_CONVERT,
    }
    for source in registry.MARKUP_SOURCES:
        for target in registry.TARGETS_BY_SOURCE[source]:
            entry = registry.capability_for(source, target)
            assert entry is not None, (source, target)
            assert entry.converter_key == expected[target], (source, target)


def test_media_types_agree_with_the_existing_constants() -> None:
    """MIME 不能在转换中心里另起一套写法。"""
    assert registry.MEDIA_TYPE_BY_TARGET[registry.TARGET_PDF] == PDF_MEDIA_TYPE
    assert registry.MEDIA_TYPE_BY_TARGET[registry.TARGET_DOCX] == DOCX_MEDIA_TYPE
    # 图片的线上写法是 jpg（tiff 那边是 tif/tiff 两个扩展名），
    # 编码层的写法是 jpeg / tiff，两者必须能互相对上
    for source, internal in (
        (registry.SOURCE_JPG, "jpeg"),
        (registry.SOURCE_PNG, "png"),
        (registry.SOURCE_WEBP, "webp"),
        (registry.SOURCE_BMP, "bmp"),
        (registry.SOURCE_GIF, "gif"),
        (registry.SOURCE_TIFF, "tiff"),
    ):
        assert registry.MEDIA_TYPE_BY_TARGET[source] == media_type_for(internal)


def test_every_target_has_a_real_media_type() -> None:
    """**注册表里出现过的每个目标**都要有非 ``octet-stream`` 的 MIME。

    漏一格的后果很具体：下载响应头退化成 ``application/octet-stream``，
    浏览器把它当未知二进制，预览与「在新标签页打开」都失效 ——
    而转换本身是成功的，所以这种问题只会在用户点下载时才暴露。

    从 ``registry.TARGET_TYPES`` 遍历而不是手抄一份清单：将来加格式时
    忘了补 MIME，这条会当场红。
    """
    for target in registry.TARGET_TYPES:
        media_type = registry.media_type_for_target(target)
        assert media_type != "application/octet-stream", target
        # 图片与 Office 结果走 image/ 与 application/，文本结果走 text/ ——
        # 三类都是真 MIME，判据是「有类型/子类型」，不是某几个前缀。
        assert re.fullmatch(r"[a-z]+/[a-z0-9.+-]+", media_type), (target, media_type)


def test_image_mime_agrees_with_intake() -> None:
    """同一个格式，注册表与 ``services.intake`` 必须给出同一个 MIME。

    结果文件的下载走 ``intake.media_type_for``（按编码层内部名），
    能力 API 报给前端的是 ``registry`` 那份（按线上词汇）。两份不一致
    时界面会显示一个与真实响应头不同的类型 —— 用户看不出来，
    只有浏览器行为会悄悄不同。
    """
    for target in IMAGE_TARGETS:
        internal = normalize_format(target)
        assert internal is not None, target
        assert media_type_for(internal) == registry.media_type_for_target(target), target


def test_image_mimes_match_the_upload_whitelist_exactly() -> None:
    """图片**源**可能被声明的 MIME 必须与 ``settings.ALLOWED_IMAGE_MIMES`` 逐项相等。

    这是 :func:`test_image_extensions_agree_with_settings` 的 MIME 版，
    同样双向：少了会让一个能读的格式在上传那一步被拒，多了会让
    上传白名单里出现一个没人认识的类型。

    比的是 ``SOURCE_MEDIA_TYPES`` 而不是 ``MEDIA_TYPE_BY_TARGET``：
    后者是「我们产出时报什么」，一个目标一个答案；前者是「用户可能声明
    什么」。HEIC 是两者唯一分岔的地方（``.heic`` 报 ``image/heic``、
    ``.heif`` 报 ``image/heif``），理由写在 ``SOURCE_MEDIA_TYPES`` 上。
    拿产出的表来比会误判 ``image/heif`` 是野类型。

    ``ico`` 不参与 —— 它只出不进（§七 只要求 PNG → ICO），所以既不在
    扩展名白名单里，也不该在 MIME 白名单里。两处保持一致，
    由下面那句断言钉住。
    """
    declared = {
        mime for source in IMAGE_SOURCES for mime in registry.SOURCE_MEDIA_TYPES[source]
    }
    assert declared == {mime.lower() for mime in settings.ALLOWED_IMAGE_MIMES}

    upload_extensions = {ext.lower() for ext in settings.ALLOWED_IMAGE_EXTENSIONS}
    assert registry.EXTENSION_BY_TARGET[registry.TARGET_ICO] not in upload_extensions, (
        "ICO 是目标格式，不是输入格式 —— 它出现在上传白名单里就会误导用户"
    )
    assert registry.MEDIA_TYPE_BY_TARGET[registry.TARGET_ICO] not in settings.ALLOWED_IMAGE_MIMES


def test_every_image_target_has_an_encoder_and_an_extension() -> None:
    """图片目标必须真的能被 ``compressors.encoder`` 编出来。

    矩阵里多写一格「能转 TGA」而编码器里没有它，用户点下去会在 worker
    里抛 ``不支持输出格式`` —— 这正是「注册表只登记有实现的组合」那条
    规矩在编码层的对应物。

    最后一行是反向检查：矩阵里出现的目标要么是图片（走编码器），
    要么是这份清单里点名的文档写入器。新加一个格式却忘了登记进
    ``IMAGE_TARGETS``，这条会提醒。
    """
    assert set(IMAGE_TARGETS) <= set(registry.TARGET_TYPES)
    for target in IMAGE_TARGETS:
        internal = normalize_format(target)
        assert internal is not None, target
        assert internal in OUTPUT_FORMATS, target
        assert registry.EXTENSION_BY_TARGET[target] == extension_for(internal), target

    assert set(registry.TARGET_TYPES) - set(IMAGE_TARGETS) == {
        registry.TARGET_PDF,   # PyMuPDF 排版 / 图片合成
        registry.TARGET_DOCX,  # python-docx
        registry.TARGET_HTML,  # office.markup_render
        registry.TARGET_TXT,   # office.markup_render
        registry.TARGET_MD,    # office.markup_render
    }


def test_every_image_source_is_dispatchable_by_detect_source() -> None:
    """每一个图片源都要在 ``detect_source`` 的两张桥接表里。

    ``_IMAGE_FORMAT_BY_SOURCE`` 决定「这个源走不走图片那条校验」，
    ``_IMAGE_SOURCE_BY_FORMAT`` 把校验层的内部名转回注册表词汇。
    漏登记一个的表现**不是报错，而是报错报错了**：``detect_source``
    会掉到最后的 Office 分支，用户上传一张 .heic 收到
    「请上传 Word、Excel、PowerPoint 文档或 TXT 文本文件」。

    第十阶段 A 的 HEIC 就是这样漏了一整天 —— registry 的矩阵、
    能力 API、编码器、校验层全都对了，只有这张表没跟上，
    于是真机跑 ``heic → bmp`` 才把它抓出来。这条测试让下一次
    「加一种图片格式」在单元测试阶段就红，而不是等真机。
    """
    from services.conversion_service import (
        _IMAGE_FORMAT_BY_SOURCE,
        _IMAGE_SOURCE_BY_FORMAT,
    )

    assert set(_IMAGE_FORMAT_BY_SOURCE) == set(registry._IMAGE_SOURCES)
    # 两张表必须互为逆映射：``a → b`` 与 ``b → a`` 同时成立，
    # 任何一处写错（``heic`` 配 ``heic`` 而不是 ``heif``）都会当场红。
    assert {
        fmt: source for source, fmt in _IMAGE_FORMAT_BY_SOURCE.items()
    } == dict(_IMAGE_SOURCE_BY_FORMAT)


def test_every_image_source_format_name_is_known_to_the_encoder() -> None:
    """桥接表里的内部名必须是编码器认识的名字。

    ``normalize_format`` 是把线上词汇转成内部名的那个函数，
    桥接表绕过了它直接写内部名 —— 所以这里补一次对账：
    写错一个字母（``heif`` 写成 ``heic``）在 ``detect_source`` 里
    表现为「校验层返回了一个我不认识的格式」，而校验层其实是对的。
    """
    from services.conversion_service import _IMAGE_FORMAT_BY_SOURCE

    for source, internal in _IMAGE_FORMAT_BY_SOURCE.items():
        assert normalize_format(source) == internal, source


def test_every_target_in_the_vocabulary_is_reachable() -> None:
    """``TARGET_ORDER`` 里的每个目标都要真的有一格能产出它。

    词汇表比实现在前一步是很自然的写法（先想好 ``TARGET_MD`` 再实现
    TXT→MD），但**不能停在那个状态**：一个谁也产不出的目标会出现在
    目标下拉里、出现在 :data:`registry.TARGET_TYPES` 的推导里，
    只是没有任何一条路径通向它。这条断言把「先写词汇表、后补实现」
    这件事变成一个必须当场收尾的动作。
    """
    declared = set(registry.TARGET_ORDER)
    reachable = set(registry.TARGET_TYPES)
    assert declared - reachable == set(), (
        f"这些目标在词汇表里但没有任何一格能产出：{sorted(declared - reachable)}"
    )
    assert reachable - declared == set()


# ----------------------------------------------------------------------
# 查询函数
# ----------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw, expected",
    [
        ("photo.PNG", ".png"),
        ("photo.jpeg", ".jpeg"),
        ("archive.tar.gz", ".gz"),
        ("no-extension", None),
        ("", None),
        (None, None),
    ],
)
def test_normalize_extension(raw: str | None, expected: str | None) -> None:
    # 用 == 而不是 is：normalize_extension 里的 .lower() 每次都会新建一个
    # 字符串对象，is 比的是对象身份
    assert registry.normalize_extension(raw) == expected


def test_trailing_dot_name_is_unsupported() -> None:
    """``报告.`` 这种名字必须落到「不支持」，而不是被当成某种格式。

    不断言 ``normalize_extension`` 的具体返回：Python 3.14 把
    ``PurePath("x.").suffix`` 从 ``""`` 改成了 ``"."``，钉死它会让测试
    跟着解释器版本走。真正要保证的是**结果**：它不匹配任何已知格式。
    """
    assert registry.source_for_extension("报告.") is None
    assert registry.targets_for(registry.source_for_extension("报告.")) == ()


def test_source_for_extension() -> None:
    assert registry.source_for_extension("report.DOCX") == registry.SOURCE_DOCX
    assert registry.source_for_extension("a.jpeg") == registry.SOURCE_JPG
    assert registry.source_for_extension("evil.exe") is None
    assert registry.source_for_extension("no-extension") is None
    assert registry.source_for_extension(None) is None


def test_unknown_types_never_raise() -> None:
    """未知输入一律返回空/False，不抛错 —— 调用方靠它给出「不支持」提示。"""
    assert registry.targets_for(None) == ()
    assert registry.targets_for("") == ()
    assert registry.targets_for("exe") == ()
    assert registry.supports(None, "pdf") is False
    assert registry.supports("png", None) is False
    assert registry.supports("png", "exe") is False
    assert registry.supports("exe", "pdf") is False


def test_supports_matches_the_matrix() -> None:
    for source in registry.SOURCE_TYPES:
        for target in registry.TARGET_TYPES:
            expected = target in EXPECTED_TARGETS[source]
            assert registry.supports(source, target) is expected, (source, target)


def test_media_type_for_unknown_target_is_binary() -> None:
    assert registry.media_type_for_target("exe") == "application/octet-stream"


# ----------------------------------------------------------------------
# 能力矩阵（§七）
# ----------------------------------------------------------------------

def test_matrix_is_complete_when_everything_is_installed() -> None:
    matrix = registry.available_matrix(
        office_ok=True, pdf_to_word_ok=True, heif_decode_ok=True, heif_encode_ok=True
    )
    assert matrix == registry.TARGETS_BY_SOURCE
    assert (
        registry.unavailable_reasons(
            office_ok=True, pdf_to_word_ok=True, heif_decode_ok=True, heif_encode_ok=True
        )
        == []
    )


def test_missing_libreoffice_hides_office_but_keeps_txt() -> None:
    """没装 LibreOffice：Office 六类消失，但 TXT 与图片必须还在。

    TXT 走 PyMuPDF 排版、完全不经 LibreOffice，把它一起藏掉
    等于在一台本来能转 TXT 的服务器上凭空少一个功能。

    第九阶段之后 TXT 有四个目标，**一个都不能少**：``docx`` 走
    ``python-docx``、``html`` / ``md`` 走 ``markup_render``，
    三条新路同样不碰 LibreOffice。这条断言写的是精确的元组 ——
    少一个（例如把 TXT 也一起藏了）会红，多一个也会红。

    HEIC 那两个标志这里给 True：本条只考 LibreOffice，让 HEIC 保持可用
    才能证明「缺的是 Office 组件」而不是「什么都缺」。
    """
    matrix = registry.available_matrix(
        office_ok=False, pdf_to_word_ok=True, heif_decode_ok=True, heif_encode_ok=True
    )

    for source in OFFICE_SOURCES:
        assert source not in matrix, source
    for source in IMAGE_SOURCES + (registry.SOURCE_TXT, registry.SOURCE_PDF):
        assert source in matrix, source
    assert matrix[registry.SOURCE_TXT] == ("pdf", "docx", "html", "md")

    reasons = registry.unavailable_reasons(
        office_ok=False, pdf_to_word_ok=True, heif_decode_ok=True, heif_encode_ok=True
    )
    assert len(reasons) == 1
    assert "TXT" in reasons[0] and "图片" in reasons[0]


def test_missing_pdf_to_word_only_hides_pdf() -> None:
    matrix = registry.available_matrix(
        office_ok=True, pdf_to_word_ok=False, heif_decode_ok=True, heif_encode_ok=True
    )

    assert registry.SOURCE_PDF not in matrix
    assert registry.SOURCE_DOCX in matrix
    assert registry.SOURCE_TXT in matrix
    reasons = registry.unavailable_reasons(
        office_ok=True, pdf_to_word_ok=False, heif_decode_ok=True, heif_encode_ok=True
    )
    assert len(reasons) == 1
    assert "PDF 转 Word" in reasons[0]


def test_matrix_without_any_optional_component() -> None:
    """五个可选组件**全缺**时矩阵的精确形状。

    这是 HEIC 那条规矩（§六）最容易被写错的一格，所以说清楚：
    ``heif_decode_ok=False`` 让 ``heic`` **整行消失** —— 读都读不进来，
    更谈不上转成别的；``heif_encode_ok`` 这时是 False 还是 True 都无所谓，
    因为四行里已经没有哪一行还能多出一个 ``heic`` 目标
    （HEIC 自己不在 ``_IMAGE_TARGET_ORDER`` 的源里，它不能转成自己）。
    """
    matrix = registry.available_matrix(
        office_ok=False, pdf_to_word_ok=False, heif_decode_ok=False, heif_encode_ok=False
    )

    # HTML / Markdown 与 TXT 一样不依赖任何外部组件（自写的纯标准库子集），
    # 所以在这一格里全部留下 —— 这正是把它们登记为 ``markup`` 需求的目的。
    # SVG 同理：它要的 PyMuPDF 是必装依赖（``REQUIREMENT_BUILTIN``），
    # 三个可选组件全缺也照样能用。
    #
    # HEIC 是**唯一**会被这一格删掉的图片源，因为它要的 ``pillow-heif``
    # 是可选组件。所以期望集合要显式减去它 —— 减这一下不是放宽，
    # 恰恰是断言「它必须消失」。
    assert set(matrix) == (set(_IMAGE_GROUP_SOURCES) - {registry.SOURCE_HEIC}) | {
        registry.SOURCE_TXT,
        registry.SOURCE_HTML,
        registry.SOURCE_MD,
    }
    assert registry.SOURCE_HEIC not in matrix
    # 三句：LibreOffice、python-docx、HEIC 解码器各一句。
    # 编码器那**不算第四句** —— 连解码都没有时再说「不能转成 HEIC」是废话，
    # ``unavailable_reasons`` 里那个 ``elif`` 就是为这个写的。
    reasons = registry.unavailable_reasons(
        office_ok=False, pdf_to_word_ok=False, heif_decode_ok=False, heif_encode_ok=False
    )
    assert len(reasons) == 3


def test_a_decode_only_server_still_offers_heic_as_a_source() -> None:
    """§六 的核心一格：只有解码器（libde265）没有问题。

    ``heif_decode_ok=True, heif_encode_ok=False`` 必须得到
    「HEIC → 七种格式**照常**、其余格式**都不出现** ``heic`` 目标」。
    这一格是加两个标志（而不是一个 ``heif_ok``）的全部理由：
    合成一个开关时，这种真实存在的部署只能二选一 —— 要么把能用的解码
    一起藏掉，要么摆出一个点下去必然失败的「转成 HEIC」。
    """
    matrix = registry.available_matrix(
        office_ok=True, pdf_to_word_ok=True, heif_decode_ok=True, heif_encode_ok=False
    )

    assert matrix[registry.SOURCE_HEIC] == EXPECTED_TARGETS[registry.SOURCE_HEIC]
    for source in IMAGE_SOURCES:
        if source == registry.SOURCE_HEIC:
            continue
        assert registry.TARGET_HEIC not in matrix[source], source
        assert matrix[source] == tuple(
            target
            for target in EXPECTED_TARGETS[source]
            if target != registry.TARGET_HEIC
        ), source

    reasons = registry.unavailable_reasons(
        office_ok=True, pdf_to_word_ok=True, heif_decode_ok=True, heif_encode_ok=False
    )
    assert len(reasons) == 1
    assert "转成 HEIC" in reasons[0]


def test_an_encode_only_server_offers_no_heic_source_but_keeps_the_target() -> None:
    """反过来的一格：只有编码器。HEIC 作为**源**必须消失。

    这台机器上有 libx265 但没有 libde265 —— 用户手里那份 .heic
    一个像素都读不出来。此时把 ``heic`` 留在矩阵的源里，
    等于让用户传一个必然在 ``Image.open`` 上炸掉的文件。
    """
    matrix = registry.available_matrix(
        office_ok=True, pdf_to_word_ok=True, heif_decode_ok=False, heif_encode_ok=True
    )

    assert registry.SOURCE_HEIC not in matrix
    assert registry.TARGET_HEIC in matrix[registry.SOURCE_JPG]

    reasons = registry.unavailable_reasons(
        office_ok=True, pdf_to_word_ok=True, heif_decode_ok=False, heif_encode_ok=True
    )
    assert len(reasons) == 1
    assert "无法转换" in reasons[0]


def test_ocr_never_removes_the_pdf_to_word_capability() -> None:
    """§七 的硬要求：OCR 不可用时 ``pdf → docx`` 仍然保留。

    所以 OCR 根本不参与 ``available_matrix`` —— 它只影响一句提示。
    这里用签名把这件事钉住：多出一个 ocr 参数就说明有人改错了方向。

    第十阶段 A 加了 ``heif_decode_ok`` / ``heif_encode_ok`` 两个参数，
    它们**通过**这条签名检查，因为 HEIC 确实改变了「哪些格子存在」
    （见上面两条测试）。``ocr_ok`` 加不得，差别在这里：
    能力有没有是**注册表**的事，OCR 只影响做得好不好。
    """
    import inspect

    parameters = inspect.signature(registry.available_matrix).parameters
    assert set(parameters) == {
        "office_ok",
        "pdf_to_word_ok",
        "heif_decode_ok",
        "heif_encode_ok",
    }

    matrix = registry.available_matrix(
        office_ok=True, pdf_to_word_ok=True, heif_decode_ok=True, heif_encode_ok=True
    )
    assert registry.SOURCE_PDF in matrix


def test_ocr_note_follows_the_configured_page_limit() -> None:
    """提示语里的页数上限来自配置，不是写死的。"""
    assert "7" in registry.ocr_note(ocr_ok=True, max_pages=7)
    assert "30" not in registry.ocr_note(ocr_ok=True, max_pages=7)

    without_ocr = registry.ocr_note(ocr_ok=False, max_pages=7)
    assert "文字层" in without_ocr
    assert "7" not in without_ocr


# ----------------------------------------------------------------------
# 条目表（第九阶段 §十三 / §十四 / §四十三）
# ----------------------------------------------------------------------

def test_every_capability_is_hashable() -> None:
    """条目必须整体可哈希。

    这一条同时是「没人往条目里塞 ``list`` / ``dict``」的机械保证：
    字段一旦变成可变容器，``hash()`` 当场就抛 ``TypeError``，
    而可变字段正是「某个调用方改了一个共享条目」的入口。
    """
    for entry in registry.CAPABILITIES:
        assert isinstance(hash(entry), int), entry.id


def test_capability_ids_are_unique() -> None:
    assert len(registry.CAPABILITY_BY_ID) == len(registry.CAPABILITIES)


def test_every_id_round_trips() -> None:
    """ID 必须能从自身无损反推出 ``(类别, 源, 目标)``。

    这条把「ID 是给人看的名字」变成「ID 是同一事实的另一种视图」——
    手写一个与内容对不上的 ID（``image.png-to-jpg`` 挂在 jpg→png 上）
    会当场失败。
    """
    for entry in registry.CAPABILITIES:
        if not entry.is_conversion:
            continue
        assert capability.parse_capability_id(entry.id) == (
            entry.category,
            entry.source_type,
            entry.target_type,
        ), entry.id


def test_capability_id_shape() -> None:
    """形状守卫：防止有人手写 ID 时把 ``-to-`` 打成 ``_to_`` 之类。"""
    pattern = re.compile(r"[a-z]+\.[a-z0-9]+-to-[a-z0-9]+")
    for entry in registry.CAPABILITIES:
        if entry.is_conversion:
            assert pattern.fullmatch(entry.id), entry.id


def test_conversion_pairs_are_unique() -> None:
    """一对 ``(源, 目标)`` 只能有一条 conversion —— 两条就得靠顺序决定谁生效。"""
    pairs = [
        (entry.source_type, entry.target_type)
        for entry in registry.CAPABILITIES
        if entry.is_conversion
    ]
    assert len(set(pairs)) == len(pairs)


def test_operations_never_collide_with_conversions() -> None:
    """操作类的 ID 与转换类的 ID 不共用命名空间。

    比的是**转换**的 ID，不是 ``CAPABILITY_BY_ID`` —— 后者现在两边都装，
    拿它来比等于让操作 ID 与自己做交集，永远为空、永远通过。
    """
    operation_ids = {entry.id for entry in registry.OPERATIONS}
    conversion_ids = {entry.id for entry in registry.CONVERSIONS}

    assert operation_ids and conversion_ids
    assert not (operation_ids & conversion_ids)
    assert operation_ids | conversion_ids == set(registry.CAPABILITY_BY_ID)


def test_matrix_rows_follow_the_canonical_target_order() -> None:
    """每一行的目标顺序都必须服从 ``TARGET_ORDER``。

    顺序是用户可见的（目标格式单选按钮的排列），也是既有断言
    ``matrix["jpg"] == ["png", "webp", "pdf"]`` 之所以钉得住的原因。
    从条目派生之后，顺序不能再靠「碰巧写对了」。
    """
    positions = {target: index for index, target in enumerate(registry.TARGET_ORDER)}
    for source, targets in registry.TARGETS_BY_SOURCE.items():
        assert list(targets) == sorted(targets, key=positions.__getitem__), source


def test_source_types_order_is_pinned() -> None:
    """``SOURCE_TYPES`` 的**确切顺序**是用户可见的。

    ``detect_source`` 拒绝一个文件时用它拼「可转换的格式：JPG、PNG、…」
    那句提示，所以顺序不能变。

    第九阶段把三种新图片格式插在 ``webp`` 之后 —— 与它们同组，
    提示语读起来才是「同类相邻」而不是把图片拆成两段。
    HTML / Markdown 追加在 ``txt`` 之后：新格式一律往后放，
    既有的十四个位置逐字不变。

    第十阶段 A 把 SVG 插在 ``tiff`` 与 ``pdf`` **之间**：它是图片，
    提示语里跟在那六个后面读起来才通顺；而它同样是**插入**不是重排 ——
    既有的十六个位置一个都没动。

    HEIC 同样插在 ``svg`` 之后、``pdf`` 之前（``_MATRIX_SOURCE_ORDER``
    与这里一致）：它是位图，紧挨着 SVG 那一栏读起来最顺。
    **它又是「插入」不是「追加」** —— 追加到末尾会让提示语写成
    「…、TXT、HTML、Markdown、HEIC」，把一种图片排到文本后面。
    """
    assert registry.SOURCE_TYPES == (
        "jpg", "png", "webp", "bmp", "gif", "tiff", "svg", "heic", "pdf",
        "doc", "docx", "xls", "xlsx", "ppt", "pptx", "txt",
        "html", "md",
    )


def test_matrix_source_order_is_pinned() -> None:
    """``TARGETS_BY_SOURCE`` 的**键顺序**同样用户可见 —— 它决定
    ``/api/conversion/capabilities`` 里 ``groups`` 的先后与组内源格式的先后。

    它与 :func:`test_source_types_order_is_pinned` 的顺序**故意不同**
    （这里 ``pdf`` 排最后，那里排第七）。这是第七阶段就存在的差异，
    两个顺序都被保留下来，而不是顺手统一 —— 统一会同时改动一句错误提示
    和转换中心的卡片排列。

    HTML / Markdown 排在这里的 ``pdf`` **之前**（它们属于「文本」一栏，
    与 ``txt`` 相邻），而在那句错误提示里排在 ``pdf`` 之后。两处不同不是
    疏漏：一处是提示语的朗读顺序，一处是卡片的排列顺序。

    SVG 与上面那句错误提示里一样，紧跟 ``tiff``（同属「图片」那一栏）。
    HEIC 也跟在那句错误提示里一样，排在 ``svg`` 之后、Office 之前。
    """
    assert list(registry.TARGETS_BY_SOURCE) == [
        "jpg", "png", "webp", "bmp", "gif", "tiff", "svg", "heic",
        "doc", "docx", "xls", "xlsx", "ppt", "pptx", "txt",
        "html", "md",
        "pdf",
    ]


def test_matrix_is_derived_not_hand_written() -> None:
    """矩阵必须等于从条目表重算出来的结果。

    这条防的是「派生视图被悄悄换回手抄表」：手抄表与条目表一旦分叉，
    ``capability_for()`` 说能转、矩阵说不能转，而两边都有测试各自通过。
    """
    recomputed: dict[str, list[str]] = {}
    positions = {target: index for index, target in enumerate(registry.TARGET_ORDER)}
    for entry in registry.CAPABILITIES:
        if not entry.is_conversion:
            continue
        bucket = recomputed.setdefault(entry.source_type, [])
        if entry.target_type not in bucket:
            bucket.append(entry.target_type)
    assert registry.TARGETS_BY_SOURCE == {
        source: tuple(sorted(targets, key=positions.__getitem__))
        for source, targets in recomputed.items()
    }


def test_entry_declarations_agree_with_the_vocabulary() -> None:
    """条目上的 ``group`` / ``requires`` / ``worker_pool`` 必须与词汇表一致。

    它们由 :func:`registry._build_capabilities` 从词汇表取，所以正常情况下
    不可能不一致 —— 这条断言守的是「将来有人为了某一条条目手写覆盖一个值」。
    资源池那一项尤其重要：池错了，任务就会排到错误的并发额度上。

    ``requires`` 比对的是 :func:`registry._requires_for`，不是
    ``SOURCE_REQUIREMENTS``：第九阶段第 10 步起，**格子**（源 + 目标）
    才决定要什么组件 —— TXT→DOCX 要 python-docx，TXT 的另外三个目标不要。
    这里跟着走那一个入口，例外表就只有一份真相。

    只遍历 :data:`registry.CONVERSIONS`：操作条目（``op.*``）的那三项是
    手写的（它们没有源格式词汇可推），由
    ``test_conversion_operations.py::test_operations_declare_their_own_metadata``
    逐条钉住。
    """
    from services.worker_pool import _REQUIREMENT_POOLS

    for entry in registry.CONVERSIONS:
        assert entry.group == registry.SOURCE_GROUPS[entry.source_type], entry.id
        assert entry.requires == registry._requires_for(
            entry.source_type, entry.target_type
        ), entry.id
        assert entry.worker_pool == _REQUIREMENT_POOLS[entry.requires], entry.id
        assert entry.category in capability.CATEGORIES, entry.id


def test_importing_registry_does_not_pull_any_implementation() -> None:
    """§十三：注册表必须保持**零 I/O**，import 它不得拉起任何转换实现。

    在子进程里做，避免被本次 session 已经 import 过的模块干扰
    （同进程里 ``fitz`` 早就被别的测试导进来了，断言会永远通过）。
    """
    backend_dir = Path(__file__).resolve().parents[1]
    code = (
        "import sys; import conversion.registry;"
        "bad = [m for m in ('fitz', 'PIL', 'docx', 'openpyxl', 'pptx') if m in sys.modules];"
        "assert not bad, bad"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=backend_dir,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_one_source_never_spans_two_pools() -> None:
    """同一个源的所有条目必须落在同一个池里。

    ``services/worker_pool.pool_for`` 按**源格式**取池，所以一个源若同时
    声明了两个池，取到哪个就成了字典顺序的函数 —— 而字典顺序是没人
    会去维护的东西。真的出现这种需求（比如「pdf → docx 走 OCR，
    pdf → png 不走」），正确的做法是给 ``pool_for`` 加一条按目标区分的
    路径，而不是让这张表默默二义。
    """
    pools: dict[str, set[str]] = {}
    for entry in registry.CONVERSIONS:
        pools.setdefault(entry.source_type, set()).add(entry.worker_pool)

    for source, declared in pools.items():
        assert len(declared) == 1, f"{source} 同时声明了 {sorted(declared)} 两个池"
    # 派生的那张表必须与逐条目的答案完全一致（不能少一个源、也不能多）
    assert registry.POOL_BY_SOURCE == {
        source: next(iter(declared)) for source, declared in pools.items()
    }
    assert set(registry.POOL_BY_SOURCE) == set(registry.SOURCE_TYPES)


def test_omitting_a_worker_pool_is_not_a_silent_default() -> None:
    """``worker_pool`` 是**必填**字段，不能靠 dataclass 默认值悄悄补一个。

    它没有默认值，正是为了让「新加一条能力时忘了写池」在构造那一刻就
    抛 ``TypeError`` —— 而不是让一批活默默排进某个池，等到线上变慢
    才有人发现。
    """
    import dataclasses

    field = next(
        f for f in dataclasses.fields(capability.Capability) if f.name == "worker_pool"
    )
    assert field.default is dataclasses.MISSING
    assert field.default_factory is dataclasses.MISSING


# ----------------------------------------------------------------------
# 家族分发表（第九阶段 §八 / §四十四）
# ----------------------------------------------------------------------

def test_converter_table_matches_the_registry() -> None:
    """注册表声明的家族与 ``CONVERTERS`` 里的实现**一一对应**。

    两条漂移都很难在运行期被发现，所以在这里一次性钉住：

    * 注册表登记了某个家族而实现缺席 —— 能力 API 说「能做」，
      用户点下去却得到「转换器不可用」，是最难查的一类 bug；
    * 实现写了却没有条目用它 —— 一段永远跑不到的死代码。
    """
    from services.conversion_service import convert_file_plan

    assert convert_file_plan() == ()


def test_converter_keys_are_the_option_family_vocabulary() -> None:
    """分发表的键必须落在选项词表的家族集合里。

    两处用的是同一组字符串常量（``conversion.options`` 的家族标识）；
    手抄一份字面量的话，``image.convert`` 写成 ``image.converts``
    会让那个家族的所有条目**悄悄没有选项**。
    """
    from services.conversion_service import CONVERTERS

    vocabulary = set(option_vocab.converter_keys())
    assert set(CONVERTERS) <= vocabulary, set(CONVERTERS) - vocabulary


# ----------------------------------------------------------------------
# 推荐标签（第九阶段 §二十二）
#
# 「Recommended / Other formats」两级选择器要有个数据来源。它可以由前端
# 按某条规则自己算（比如「目标与源同类别」），也可以由注册表直接说。
# 这里选后者：规则在前端算的话，图片源会推出「所有图片目标都推荐」，
# 分栏等于没分；而且「哪几格常用」是能力数据，改它不该动界面代码。
# ----------------------------------------------------------------------

def test_every_source_recommends_at_least_one_target() -> None:
    """每种源至少有一格推荐。

    否则用户在「Recommended」那一栏下面看到一片空白，
    会以为这个格式没得转。
    """
    empty = sorted(
        source
        for source in registry.SOURCE_TYPES
        if not any(
            registry.TAG_RECOMMENDED in entry.tags
            for entry in registry.CONVERSIONS
            if entry.source_type == source
        )
    )
    assert empty == []


def test_recommended_targets_are_real_targets() -> None:
    """推荐只能推荐**真的存在**的那一格。

    ``_RECOMMENDED_PRIORITY`` 是一张手写的偏好表，与
    ``_TARGETS_BY_SOURCE_SPEC`` 是两份数据；这里断言前者永远是后者的子集，
    否则界面上会出现一个点了报「不支持」的按钮。
    """
    for entry in registry.CONVERSIONS:
        if registry.TAG_RECOMMENDED in entry.tags:
            assert entry.target_type in registry.TARGETS_BY_SOURCE[entry.source_type]


def test_recommended_never_includes_the_source_itself() -> None:
    """同格式不是能力（见 ``CAPABILITY_BY_PAIR``），当然也不能被推荐。"""
    for entry in registry.CONVERSIONS:
        if registry.TAG_RECOMMENDED in entry.tags:
            assert entry.target_type != entry.source_type, entry.id


def test_recommended_is_a_strict_subset_of_all_targets() -> None:
    """每种源都要有**没**被推荐的格式，否则「Other formats」是空的。"""
    for source in registry.SOURCE_TYPES:
        targets = registry.TARGETS_BY_SOURCE[source]
        recommended = {
            entry.target_type
            for entry in registry.CONVERSIONS
            if entry.source_type == source and registry.TAG_RECOMMENDED in entry.tags
        }
        if len(targets) > 1:
            assert recommended < set(targets), source


def test_recommended_is_capped_at_three() -> None:
    """推荐栏最多三个 —— 多了第一屏就与「全部格式」没区别。"""
    for source in registry.SOURCE_TYPES:
        recommended = [
            entry
            for entry in registry.CONVERSIONS
            if entry.source_type == source and registry.TAG_RECOMMENDED in entry.tags
        ]
        assert len(recommended) <= registry._RECOMMENDED_LIMIT, source


def test_tags_come_from_the_vocabulary() -> None:
    """条目上的标签只能是词表里的词。

    前端按标签分栏，一个拼错的标签（``"recomended"``）不会报错，
    只会让那一格从推荐栏里静静消失。
    """
    for entry in registry.CAPABILITIES:
        assert set(entry.tags) <= set(registry.TAGS), (entry.id, entry.tags)


def test_image_sources_recommend_a_format_and_pdf() -> None:
    """图片源的第一屏是「换一种常见格式」加「转 PDF」。

    逐格钉死真值而不是断言形状：``_RECOMMENDED_PRIORITY`` 改一个字母，
    用户的第一屏就变了，那是要被看见的改动。
    """
    expected = {
        "jpg": ("png", "pdf", "webp"),
        "png": ("jpg", "pdf", "webp"),
        "webp": ("png", "jpg", "pdf"),
        "bmp": ("png", "jpg", "pdf"),
        "gif": ("png", "jpg", "pdf"),
        "tiff": ("png", "jpg", "pdf"),
    }
    actual = {
        source: tuple(
            entry.target_type
            for entry in registry.CONVERSIONS
            if entry.source_type == source and registry.TAG_RECOMMENDED in entry.tags
        )
        for source in IMAGE_SOURCES
    }
    for source, targets in expected.items():
        # 推荐次序由 ``_RECOMMENDED_PRIORITY`` 决定，这里只对比集合与个数
        assert set(actual[source]) == set(targets), (source, actual[source])
        assert len(actual[source]) == len(targets), (source, actual[source])


def test_document_sources_recommend_their_flagship_target() -> None:
    """文档族的推荐就是它最该去的地方。"""
    expected = {
        "txt": {"pdf", "docx", "html"},
        "html": {"pdf"},
        "md": {"pdf", "html"},
        "pdf": {"docx"},
        "doc": {"pdf"},
        "docx": {"pdf"},
        "xls": {"pdf"},
        "xlsx": {"pdf"},
        "ppt": {"pdf"},
        "pptx": {"pdf"},
    }
    for source, targets in expected.items():
        actual = {
            entry.target_type
            for entry in registry.CONVERSIONS
            if entry.source_type == source and registry.TAG_RECOMMENDED in entry.tags
        }
        assert actual == targets, (source, actual)


def test_capability_payload_carries_the_tags() -> None:
    """标签必须真的出网 —— 前端只看得到 JSON。"""
    from services.conversion_service import conversion_capabilities

    payload = conversion_capabilities()
    tagged = [item["id"] for item in payload["conversions"] if item["tags"]]
    assert tagged, "一个带标签的条目都没有"
    assert set(tagged) <= {
        entry.id for entry in registry.CONVERSIONS
    }
    for item in payload["conversions"]:
        assert isinstance(item["tags"], list)


def test_multiframe_sources_say_so_on_every_capability() -> None:
    """GIF / TIFF 可以装多帧，而转换只处理第一帧（决策 C）——**必须说出来**。

    这不是界面文案，是能力的事实：挂在这两个源的**每一条**能力上，
    参数面板与结果卡都读它。漏掉任何一条，那条路径上的用户就会以为
    自己拿到的动图 / 多页文件是完整的。
    """
    multiframe = {"gif", "tiff"}
    for entry in registry.CONVERSIONS:
        if entry.source_type in multiframe:
            assert entry.note, entry.id
            assert "第一帧" in entry.note or "第一页" in entry.note, (entry.id, entry.note)
        else:
            assert entry.note is None, (entry.id, entry.note)


def test_single_frame_sources_carry_no_note() -> None:
    """反过来也要验：**不能**给 JPG / PNG 这类单帧源挂一句多余的话。

    一句话挂在所有源上就成了没人读的噪音，而噪音会让真正要紧的那条
    （GIF / TIFF）也被忽略。
    """
    assert set(registry._SOURCE_NOTES) == {"gif", "tiff"}, registry._SOURCE_NOTES
