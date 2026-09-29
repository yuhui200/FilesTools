"""统一转换中心的能力注册表 —— 纯数据 + 纯查询，**不做任何 I/O**。

这一层只回答一个问题：**服务器真的支持把 X 变成 Y 吗？**

它不碰文件、不碰网络，也不 import 任何转换实现（那样会与 services 层形成
循环依赖），所以能被单元测试直接喂，也能被 routers 安全地吐给前端。

「支持」的唯一依据是 **services/conversion_service.py 的 ``CONVERTERS``
表里真的有 ``converter_key`` 对应的那一格**，不是「理论上这个格式能转」。
矩阵里多写一格、用户点了却报错，比少写一格严重得多 —— 所以每加一格都必须
同时有实现和测试（``test_conversion_dispatch.py`` 里那条
``convert_file_plan() == ()`` 就是干这个的）。

## 第九阶段的结构：词汇表显式，能力事实派生

```
词汇表（显式）    SOURCE_TYPES / SOURCE_LABELS / EXTENSIONS_BY_SOURCE
                 SOURCE_GROUPS / SOURCE_REQUIREMENTS
                 TARGET_ORDER / TARGET_LABELS / EXTENSION_BY_TARGET / MEDIA_TYPE_BY_TARGET
                          ↓ 被条目表引用
能力规格（显式）  _TARGETS_BY_SOURCE_SPEC / _OPERATION_SPECS
                 —— 唯一一份「哪一对真的能转」的手写真相
                          ↓ _build_capabilities() / _build_operations() 生成
条目表            CAPABILITIES —— 生成结果，也是查询的唯一入口
                          ↓ 派生（纯计算）
派生视图          TARGETS_BY_SOURCE / TARGET_TYPES / EXTENSION_TO_SOURCE
                 CAPABILITY_BY_ID / CAPABILITY_BY_PAIR
```

为什么词汇表不派生：``.bmp`` 属于 ``bmp``、它叫「BMP 图片」、下载 MIME 是
``image/bmp`` —— 这**不是**能力事实，而是「bmp 是什么」。硬从条目里推，
就得在 bmp 的每一条目上重复一遍扩展名与 MIME，那是把一份真相抄成 42 份。

为什么能力事实必须派生：这就是 §四十二/§四十四 要的效果 ——
**加一种能转的组合，只改条目表一处**，``TARGETS_BY_SOURCE`` 与前端看到的
能力矩阵自动跟着变，分发代码一行不动。

词汇约定：图片目标沿用 ``/api/config`` 已有的线上写法 ``jpg`` / ``png`` / ``webp``
（不是编码层的 ``jpeg``）。两者之间由 ``compressors.encoder.normalize_format``
转换，并由 ``tests/test_conversion_registry.py`` 断言不会漂移。
"""

from __future__ import annotations

from pathlib import Path
from typing import NamedTuple

from conversion.capability import (
    CATEGORY_DOCUMENT,
    CATEGORY_IMAGE,
    CATEGORY_PDF,
    GROUP_IMAGE,
    GROUP_LABELS,
    GROUP_OFFICE,
    GROUP_PDF,
    GROUP_TEXT,
    OPERATION_CONVERSION,
    INPUT_FILE,
    INPUT_FILES,
    INPUT_UPLOAD,
    OPERATION_OPERATION,
    REQUIREMENT_BUILTIN,
    REQUIREMENT_DOCX,
    REQUIREMENT_HEIF_DECODE,
    REQUIREMENT_HEIF_ENCODE,
    REQUIREMENT_IMAGE,
    REQUIREMENT_MARKUP,
    REQUIREMENT_OFFICE,
    REQUIREMENT_PDF_TO_WORD,
    Capability,
    capability_id,
    operation_id,
)
from conversion.options import (
    IMAGE_CONVERT,
    IMAGE_TO_PDF,
    MARKUP_CONVERT,
    MARKUP_TO_PDF,
    OFFICE_TO_PDF,
    PDF_TO_DOCX,
    SVG_TO_PDF,
    TEXT_CONVERT,
    TEXT_TO_DOCX,
    TEXT_TO_PDF,
    operation_options,
    options_for,
)

# ----------------------------------------------------------------------
# 源类型
# ----------------------------------------------------------------------

SOURCE_JPG = "jpg"
SOURCE_PNG = "png"
SOURCE_WEBP = "webp"
SOURCE_BMP = "bmp"
SOURCE_GIF = "gif"
SOURCE_TIFF = "tiff"
SOURCE_PDF = "pdf"
SOURCE_DOC = "doc"
SOURCE_DOCX = "docx"
SOURCE_XLS = "xls"
SOURCE_XLSX = "xlsx"
SOURCE_PPT = "ppt"
SOURCE_PPTX = "pptx"
SOURCE_TXT = "txt"
SOURCE_HTML = "html"
SOURCE_MD = "md"
SOURCE_SVG = "svg"
#: 第十阶段 A §五–§八。``heic`` 是内部词汇（不是 ``heif``）：用户手里
#: 的文件叫 ``.heic``，界面上的标签也就该是 HEIC。``.heif`` 是同一种容器的
#: 另一个扩展名，收到同一个源里（理由同 .jpg / .jpeg）。
SOURCE_HEIC = "heic"

#: 全部源类型。**这份顺序是用户可见的**：``detect_source`` 在拒绝一个文件时
#: 会按它拼出「可转换的格式：JPG、PNG、…」那句提示。它**与
#: :data:`_MATRIX_SOURCE_ORDER` 不同**（那里 ``pdf`` 排最后）——两个顺序都是
#: 既有行为，各自被测试逐字钉住。顺手「统一」它们会同时改动一句错误提示
#: 和转换中心的卡片顺序，那不是重构，是改需求。
#:
#: 第九阶段新增的三种图片格式紧跟在 ``webp`` 后面：那句提示按「同类相邻」
#: 读起来才通顺，而且这张表的前半段与 :data:`_IMAGE_SOURCES` 保持同序。
#: HTML / Markdown 追加在末尾（新格式一律往后放，既有位置逐字不变）。
SOURCE_TYPES: tuple[str, ...] = (
    SOURCE_JPG,
    SOURCE_PNG,
    SOURCE_WEBP,
    SOURCE_BMP,
    SOURCE_GIF,
    SOURCE_TIFF,
    # SVG 紧跟图片那六个：它也是「一张图」，用户会把它和 JPG 放在一起想，
    # 而且它就排在 PDF 前面 —— 那句「可转换的格式：…」的前半段因此
    # 仍然是完整的一组图片格式。**它不在 ``_IMAGE_SOURCES`` 里**，
    # 见那里与 ``_SVG_SOURCES`` 的说明。
    SOURCE_SVG,
    # HEIC 排在 SVG 之后、PDF 之前：它是位图，用户会把它和 JPG 放在一起想，
    # 「可转换的格式」那句里紧跟着图片那一组读起来才通顺。
    # **既有位置一个都没动**（新格式一律往后放，这里只是插在图片组末尾）。
    SOURCE_HEIC,
    SOURCE_PDF,
    SOURCE_DOC,
    SOURCE_DOCX,
    SOURCE_XLS,
    SOURCE_XLSX,
    SOURCE_PPT,
    SOURCE_PPTX,
    SOURCE_TXT,
    SOURCE_HTML,
    SOURCE_MD,
)

#: 源类型 -> 界面显示名
SOURCE_LABELS: dict[str, str] = {
    SOURCE_JPG: "JPG 图片",
    SOURCE_PNG: "PNG 图片",
    SOURCE_WEBP: "WEBP 图片",
    SOURCE_BMP: "BMP 图片",
    SOURCE_GIF: "GIF 图片",
    SOURCE_TIFF: "TIFF 图片",
    SOURCE_SVG: "SVG 矢量图",
    SOURCE_HEIC: "HEIC 图片",
    SOURCE_PDF: "PDF 文档",
    SOURCE_DOC: "Word 97-2003 文档",
    SOURCE_DOCX: "Word 文档",
    SOURCE_XLS: "Excel 97-2003 表格",
    SOURCE_XLSX: "Excel 表格",
    SOURCE_PPT: "PowerPoint 97-2003 演示文稿",
    SOURCE_PPTX: "PowerPoint 演示文稿",
    SOURCE_TXT: "文本文件",
    SOURCE_HTML: "HTML 网页",
    SOURCE_MD: "Markdown 文档",
}

#: 源类型 -> 归入哪一组。分组只影响界面排版与「不可用时该不该解释」，
#: 不参与任何转换决策。
#: （``GROUP_*`` 常量从 :mod:`conversion.capability` 引入并在此再导出。）
SOURCE_GROUPS: dict[str, str] = {
    SOURCE_JPG: GROUP_IMAGE,
    SOURCE_PNG: GROUP_IMAGE,
    SOURCE_WEBP: GROUP_IMAGE,
    SOURCE_BMP: GROUP_IMAGE,
    SOURCE_GIF: GROUP_IMAGE,
    SOURCE_TIFF: GROUP_IMAGE,
    # SVG 与位图同属图片那一栏：用户想的是「图转图」，不是「矢量文本转位图」。
    # 界面上把它单独摆一栏，只会让人找不到自己的文件在哪。
    SOURCE_SVG: GROUP_IMAGE,
    # HEIC 与位图同属图片那一栏 —— 它本来就是位图，只是编码方式不同。
    SOURCE_HEIC: GROUP_IMAGE,
    SOURCE_DOC: GROUP_OFFICE,
    SOURCE_DOCX: GROUP_OFFICE,
    SOURCE_XLS: GROUP_OFFICE,
    SOURCE_XLSX: GROUP_OFFICE,
    SOURCE_PPT: GROUP_OFFICE,
    SOURCE_PPTX: GROUP_OFFICE,
    SOURCE_TXT: GROUP_TEXT,
    # HTML / Markdown 与 TXT 同属「文本」一栏：界面上不该出现三栏文本，
    # 它们要的也是同一种资源（进程内排版，见 _WORKER_POOL_BY_GROUP）。
    SOURCE_HTML: GROUP_TEXT,
    SOURCE_MD: GROUP_TEXT,
    SOURCE_PDF: GROUP_PDF,
}

# ----------------------------------------------------------------------
# 扩展名
# ----------------------------------------------------------------------

#: 源类型 -> 认哪些扩展名（都含点、小写）。
#: ``.jpeg`` 与 ``.jpg`` 归到同一个源类型：它们是同一种文件，
#: 拆成两组只会让用户看到两个「一模一样却要分别设置」的组。
EXTENSIONS_BY_SOURCE: dict[str, tuple[str, ...]] = {
    SOURCE_JPG: (".jpg", ".jpeg"),
    SOURCE_PNG: (".png",),
    SOURCE_WEBP: (".webp",),
    SOURCE_BMP: (".bmp",),
    SOURCE_GIF: (".gif",),
    # .tif 与 .tiff 是同一种文件的两个扩展名，理由同 .jpg / .jpeg
    SOURCE_TIFF: (".tif", ".tiff"),
    # ``.svgz``（gzip 过的 SVG）有意不收，理由见 ``settings.ALLOWED_SVG_EXTENSIONS``。
    SOURCE_SVG: (".svg",),
    # .heic 与 .heif 是同一种容器的两个扩展名，理由同 .jpg / .jpeg。
    SOURCE_HEIC: (".heic", ".heif"),
    SOURCE_PDF: (".pdf",),
    SOURCE_DOC: (".doc",),
    SOURCE_DOCX: (".docx",),
    SOURCE_XLS: (".xls",),
    SOURCE_XLSX: (".xlsx",),
    SOURCE_PPT: (".ppt",),
    SOURCE_PPTX: (".pptx",),
    SOURCE_TXT: (".txt",),
    # ``.htm`` 与 ``.html`` 是同一种文件，``.markdown`` 与 ``.md`` 也是 ——
    # 理由同 .jpg / .jpeg。拆成两组只会让用户看到两个一模一样的组。
    SOURCE_HTML: (".html", ".htm"),
    SOURCE_MD: (".md", ".markdown"),
}

#: 扩展名 -> 源类型（由上面反向生成，避免两张表各写一遍）
EXTENSION_TO_SOURCE: dict[str, str] = {
    ext: source
    for source, extensions in EXTENSIONS_BY_SOURCE.items()
    for ext in extensions
}

# ----------------------------------------------------------------------
# 组件要求（「要哪个组件才能转」，由 services 层的探测结果裁决）
# ----------------------------------------------------------------------

#: 源类型 -> 需要服务器具备哪个组件才能转。
#: ``builtin`` 表示只靠 PyMuPDF/Pillow 这类必装依赖，不额外要求任何东西。
#:
#: TXT 单独归为 ``builtin`` 是有事实依据的：``doc_service._convert_text``
#: 走 PyMuPDF 排版，**不经过 LibreOffice**。把它错误地绑在 office 上，
#: 会让一台没装 LibreOffice 的服务器白白失去一项本来能用的能力。
SOURCE_REQUIREMENTS: dict[str, str] = {
    SOURCE_JPG: REQUIREMENT_IMAGE,
    SOURCE_PNG: REQUIREMENT_IMAGE,
    SOURCE_WEBP: REQUIREMENT_IMAGE,
    SOURCE_BMP: REQUIREMENT_IMAGE,
    SOURCE_GIF: REQUIREMENT_IMAGE,
    SOURCE_TIFF: REQUIREMENT_IMAGE,
    # SVG 也是 ``builtin``，但理由与位图**不同**，值得写下来：
    # 位图那条路靠 Pillow，SVG 这条路靠 PyMuPDF 自带的 SVG 解析器 ——
    # 两者都是必装依赖，所以都永远可用。它**不是**「因为没探测所以标 builtin」：
    # 这里没有可选的解码器，没有「装了才有」的组件，也就没有可降级的空间。
    # 第十阶段 A §五–§八 那套「动态探测、不许假设可用」是给 HEIC 准备的 ——
    # 那里真的存在「只有解码没有编码」的中间态，SVG 这里不存在。
    SOURCE_SVG: REQUIREMENT_BUILTIN,
    # HEIC **不能**跟着图片那六个写 ``REQUIREMENT_IMAGE``。位图那条路靠 Pillow
    # 这个必装依赖，而 Pillow 12 自己一行 HEIC 代码都没有 —— HEIC 的全部能力
    # 来自可选的 ``pillow-heif``。写 ``image`` 就等于宣称「装了服务就能转
    # HEIC」，那是 §五–§八 明令禁止的假设。
    SOURCE_HEIC: REQUIREMENT_HEIF_DECODE,
    SOURCE_DOC: REQUIREMENT_OFFICE,
    SOURCE_DOCX: REQUIREMENT_OFFICE,
    SOURCE_XLS: REQUIREMENT_OFFICE,
    SOURCE_XLSX: REQUIREMENT_OFFICE,
    SOURCE_PPT: REQUIREMENT_OFFICE,
    SOURCE_PPTX: REQUIREMENT_OFFICE,
    SOURCE_TXT: REQUIREMENT_BUILTIN,
    # Markdown / HTML 子集的解析与渲染是自写的纯标准库代码，不依赖任何
    # 外部组件，所以与 TXT 一样永远可用（见 ``_availability``）。
    SOURCE_HTML: REQUIREMENT_MARKUP,
    SOURCE_MD: REQUIREMENT_MARKUP,
    SOURCE_PDF: REQUIREMENT_PDF_TO_WORD,
}

# ----------------------------------------------------------------------
# 目标类型
# ----------------------------------------------------------------------

TARGET_JPG = "jpg"
TARGET_PNG = "png"
TARGET_WEBP = "webp"
TARGET_BMP = "bmp"
TARGET_GIF = "gif"
TARGET_TIFF = "tiff"
TARGET_ICO = "ico"
#: 第十阶段 A §五–§八。既是源也是目标：HEIC 既能被读进来（手机照片），
#: 也能被写出去（省空间的现代格式）。
TARGET_HEIC = "heic"
TARGET_PDF = "pdf"
TARGET_DOCX = "docx"
TARGET_HTML = "html"
TARGET_TXT = "txt"
TARGET_MD = "md"

#: **全部**图片格式的规范顺序。派生图片源的目标列表时按它走，
#: 所以「加一种图片格式」只需要往这里加一项，42 个条目的目标顺序
#: 与矩阵全部自动跟上（新格式一律插在 ``pdf`` **之前**）。
_IMAGE_TARGET_ORDER: tuple[str, ...] = (
    TARGET_JPG,
    TARGET_PNG,
    TARGET_WEBP,
    TARGET_BMP,
    TARGET_GIF,
    TARGET_TIFF,
    TARGET_ICO,
    # HEIC 追加在最后（新格式一律往后放）。它一进来就有两个方向：
    # 作为**目标**，六个位图源都多出「转成 HEIC」一格；作为**源**，
    # ``_image_targets`` 会自动给它上面那六个加 ``pdf``。
    TARGET_HEIC,
)

#: 目标类型的全序。``pdf`` 与 ``docx`` 压在最后，保证新图片格式
#: 不会把 ``pdf`` 挤到中间 —— ``matrix["jpg"] == ["png", "webp", "pdf"]``
#: 这类既有断言因此只需要**追加**新格式，不用重排。
#:
#: 文档类目标（html / txt / md）再追加在 ``docx`` 之后，理由相同：新目标一律
#: 往后排，既有目标的位置逐字不变。
#:
#: ``md`` 是第 10 步才进来的 —— 到这一步 ``text.to_docx`` 与 ``text.convert``
#: 落地，TXT 能产出它，词汇表里不再有够不着的洞。
#: ``TARGET_ORDER`` 与 ``TARGET_TYPES`` 的差集由
#: ``test_every_target_in_the_vocabulary_is_reachable`` 钉住必须为空。
TARGET_ORDER: tuple[str, ...] = _IMAGE_TARGET_ORDER + (
    TARGET_PDF,
    TARGET_DOCX,
    TARGET_HTML,
    TARGET_TXT,
    TARGET_MD,
)

#: 目标类型 -> 界面显示名
TARGET_LABELS: dict[str, str] = {
    TARGET_JPG: "JPG",
    TARGET_PNG: "PNG",
    TARGET_WEBP: "WEBP",
    TARGET_BMP: "BMP",
    TARGET_GIF: "GIF",
    TARGET_TIFF: "TIFF",
    TARGET_ICO: "ICO",
    TARGET_HEIC: "HEIC",
    TARGET_PDF: "PDF",
    TARGET_DOCX: "Word (DOCX)",
    TARGET_HTML: "HTML 网页",
    # 与 SOURCE_LABELS 的「文本文件」有意不同名：一个是「这是一份 txt」，
    # 另一个是「把别的东西转成纯文本」。用户在目标下拉里看到的是后者。
    TARGET_TXT: "纯文本 (TXT)",
    # 与 SOURCE_LABELS 的「Markdown 文档」同理不同名。写全 ``(MD)``
    # 是因为下拉里 ``Markdown`` 与 ``HTML`` 并排时，两个词都长，
    # 补上扩展名用户扫一眼就知道结果是什么文件。
    TARGET_MD: "Markdown (MD)",
}

#: 目标类型 -> 结果文件扩展名
#:
#: ``tiff`` 选了 ``.tiff`` 而不是更短的 ``.tif``：两者都被认，但结果用
#: 四字母的那个 —— Windows 的「文件类型」列与多数图片查看器都按它显示，
#: 而输入侧两个扩展名都收（见 ``EXTENSIONS_BY_SOURCE``）。
EXTENSION_BY_TARGET: dict[str, str] = {
    TARGET_JPG: ".jpg",
    TARGET_PNG: ".png",
    TARGET_WEBP: ".webp",
    TARGET_BMP: ".bmp",
    TARGET_GIF: ".gif",
    TARGET_TIFF: ".tiff",
    TARGET_ICO: ".ico",
    TARGET_HEIC: ".heic",
    TARGET_PDF: ".pdf",
    TARGET_DOCX: ".docx",
    TARGET_HTML: ".html",
    TARGET_TXT: ".txt",
    TARGET_MD: ".md",
}

#: 目标类型 -> 下载时的 MIME。
#: 这几个值与 ``services/intake.py`` / ``services/pdf_service.py`` 里的常量
#: 必须一致；unit test 会断言它们相等，改了一边另一边就会红。
MEDIA_TYPE_BY_TARGET: dict[str, str] = {
    TARGET_JPG: "image/jpeg",
    TARGET_PNG: "image/png",
    TARGET_WEBP: "image/webp",
    TARGET_BMP: "image/bmp",
    TARGET_GIF: "image/gif",
    TARGET_TIFF: "image/tiff",
    TARGET_ICO: "image/x-icon",
    TARGET_HEIC: "image/heic",
    TARGET_PDF: "application/pdf",
    TARGET_DOCX: (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    ),
    # ``text/html`` 是安全的：下载接口带 ``Content-Disposition: attachment``
    # （``routers/download.py`` 传了 filename），浏览器只会存盘、不会渲染，
    # 所以用户自己写的脚本没有机会在我们的域上执行。**这一点不能想当然** ——
    # 一旦有人去掉那个 filename，这里就变成一发存储型 XSS。
    TARGET_HTML: "text/html",
    TARGET_TXT: "text/plain",
    # ``text/markdown`` 是 RFC 7763 注册的类型。给 ``text/plain`` 也不算错，
    # 但下载到本地的 .md 会被系统按纯文本关联到记事本，而 ``text/markdown``
    # 能让认出它的编辑器（VS Code、Typora 之类）接手。两者的
    # ``Content-Disposition`` 都是 attachment，安全性质完全一样。
    TARGET_MD: "text/markdown",
}

#: 源类型 -> 客户端**可能为它声明的全部 MIME**，用于和
#: ``settings.ALLOWED_IMAGE_MIMES`` 双向对账
#: （``test_image_mimes_match_the_upload_whitelist_exactly``）。
#:
#: 为什么不直接用 :data:`MEDIA_TYPE_BY_TARGET`：那张表回答的是
#: 「我们**产出**这种文件时该报什么 MIME」，一个目标只有一个答案；
#: 这张表回答的是「用户**传上来**时可能报什么」，而一个源可以有好几个 ——
#: HEIC 正是如此：``.heic`` 报 ``image/heic``、``.heif`` 报 ``image/heif``，
#: 两种扩展名我们都收（见 ``EXTENSIONS_BY_SOURCE``），所以两种 MIME
#: 都得在上传白名单里。用产出的那张表去对账，会得出「``image/heif``
#: 是名单里没人认识的类型」这个错误结论 —— 它认识，它对应的
#: ``.heif`` 就在 ``EXTENSIONS_BY_SOURCE`` 里。
#:
#: 缺省取 :data:`MEDIA_TYPE_BY_TARGET` 的同名项（绝大多数源与目标一一对应），
#: 只有 HEIC 在这里显式多列一个。**不派生**，因为「多出来的那个 MIME」
#: 是件需要人读一遍才知道对不对的事。
SOURCE_MEDIA_TYPES: dict[str, tuple[str, ...]] = {
    source: (MEDIA_TYPE_BY_TARGET[source],)
    for source in SOURCE_TYPES
    if source in MEDIA_TYPE_BY_TARGET
}
SOURCE_MEDIA_TYPES[SOURCE_HEIC] = ("image/heic", "image/heif")

# ----------------------------------------------------------------------
# 条目表：唯一一份「哪一对真的能转」的真相
# ----------------------------------------------------------------------

#: 能力矩阵里源类型的排列顺序：图片 → Office → 文本 → PDF。
#: **这决定首页与转换中心里分组卡片的先后、以及组内源格式的先后**，
#: 是用户可见的（``conversion_capabilities`` 按它遍历生成 ``groups``）。
#: 与 :data:`SOURCE_TYPES` 的差异见那里的说明。
_MATRIX_SOURCE_ORDER: tuple[str, ...] = (
    SOURCE_JPG,
    SOURCE_PNG,
    SOURCE_WEBP,
    SOURCE_BMP,
    SOURCE_GIF,
    SOURCE_TIFF,
    SOURCE_SVG,
    SOURCE_HEIC,
    SOURCE_DOC,
    SOURCE_DOCX,
    SOURCE_XLS,
    SOURCE_XLSX,
    SOURCE_PPT,
    SOURCE_PPTX,
    SOURCE_TXT,
    SOURCE_HTML,
    SOURCE_MD,
    SOURCE_PDF,
)

#: 走 :data:`_IMAGE_TARGET_ORDER` 那套图片流水线的源格式。
#: 它们两两互转（**不含同格式**），并且都能转 PDF。
#:
#: ``svg`` **不在其中**，这一点容易看错，所以写清楚：这张表的另一重身份是
#: 「Pillow 能解码的格式」—— ``test_image_extensions_agree_with_settings``
#: 拿它去和 ``settings.ALLOWED_IMAGE_EXTENSIONS`` 逐项对账，
#: ``validate_image_upload`` / ``sniff_format`` 的魔数表也按它维护。
#: SVG 没有魔数、Pillow 解不开、校验走的是另一条
#: （``validate_svg_upload``），把它并进来会让「图片格式」这个概念失去边界，
#: 也会让那条对账测试被迫放宽。它的目标另在 :data:`_SVG_SOURCES` 里声明 ——
#: **两处都是显式的**，宁可多一行表，不要一个含义混杂的集合。
_IMAGE_SOURCES: tuple[str, ...] = (
    SOURCE_JPG,
    SOURCE_PNG,
    SOURCE_WEBP,
    SOURCE_BMP,
    SOURCE_GIF,
    SOURCE_TIFF,
    # HEIC 在这张表里的资格需要说明，因为它与上面六个**不同类**：
    # 上面六个是「Pillow 装了就能解」，HEIC 是「Pillow 加上可选的
    # ``pillow-heif`` 才能解」。它仍然属于这里，因为这张表回答的是
    # **「这是不是一条走 Pillow 的位图路径」**（校验、魔数、流水线都按它走），
    # 而 HEIC 确实是 —— 它与 SVG 那条矢量路径是两回事，SVG 才不在表里。
    # 「现在到底能不能用」是另一个问题，由 ``SOURCE_REQUIREMENTS`` 上的
    # ``REQUIREMENT_HEIF_DECODE`` 回答，两者别混。
    SOURCE_HEIC,
)

#: 走 ``compressors.svg`` 那条**矢量解码**路径的源格式。
#:
#: 单列一张表而不是塞进 ``_IMAGE_SOURCES``，理由见那里的说明。
#: 它的目标是**显式枚举**的，就按 §九 字面那四个：PNG / JPG / WEBP / PDF。
#: 位图那六个源是通过 ``_IMAGE_TARGET_ORDER`` 派生出全部目标的 ——
#: SVG 不跟着派生的原因很实在：**它多出一条 ``svg.to_pdf`` 家族**
#: （矢量导出，不是把位图贴进 PDF），而 bmp / gif / tiff 三格
#: 要么没人会从矢量图转过去，要么需要额外的量化处理。
#: 每多一格都是对用户的一个承诺，要配测试、配界面、配文档 —— 不做。
#: 将来要放开，只改这一行（``_image_targets`` 那套派生逻辑原样可用）。
_SVG_SOURCES: tuple[str, ...] = (SOURCE_SVG,)

#: SVG 的全部目标，**顺序按 §九 字面**（PNG / JPG / WEBP / PDF）。
#: 不用 ``_IMAGE_TARGET_ORDER`` 派生，见 :data:`_SVG_SOURCES` 的说明。
_SVG_TARGETS: tuple[str, ...] = (
    TARGET_PNG,
    TARGET_JPG,
    TARGET_WEBP,
    TARGET_PDF,
)


#: 只走 LibreOffice 的那六个 Office 格式；目标固定只有 PDF。
_OFFICE_SOURCES: tuple[str, ...] = (
    SOURCE_DOC,
    SOURCE_DOCX,
    SOURCE_XLS,
    SOURCE_XLSX,
    SOURCE_PPT,
    SOURCE_PPTX,
)

#: 唯一能转成 ICO 的源（§七 字面 + 决策 F）。
#:
#: ICO 是图标容器，它存的是一份或多份**小尺寸位图**。从一张有损压缩
#: 或索引色的图（jpg / gif）转过去，只会得到一张糊掉的图标；从
#: webp / bmp / tiff 转过去则没有任何用户会需要。放开这一格要在界面上
#: 多摆五个选项去换一个没人用的组合 —— 所以**只有 PNG**。
#: 架构上它仍然是一条普通的注册表条目，将来要放开只改这一行。
_ICO_SOURCE: str = SOURCE_PNG

#: 走 ``markup_parse`` / ``markup_render`` 那套自写子集的源格式。
#: 公开的：``conversion_service.detect_source`` 要用它决定该把文件交给
#: 哪个校验函数，而「哪些源属于 markup」只有这一处定义。
MARKUP_SOURCES: tuple[str, ...] = (SOURCE_HTML, SOURCE_MD)


def _image_targets(source: str) -> tuple[str, ...]:
    """一种图片源能转成的全部目标，按 :data:`_IMAGE_TARGET_ORDER` 排列。

    ``+ (TARGET_PDF,)`` 在末尾：PDF 不是图片格式，但每种图片都能转过去，
    而 ``TARGET_ORDER`` 把 ``pdf`` 压在图片格式之后，所以这里追加正好
    与那个全序一致。

    ICO 那一格由 :data:`_ICO_SOURCE` 单独把关，见那里的说明。
    """
    return tuple(
        target
        for target in _IMAGE_TARGET_ORDER
        if target != source and (target != TARGET_ICO or source == _ICO_SOURCE)
    ) + (TARGET_PDF,)


#: 逐格覆盖 :data:`SOURCE_REQUIREMENTS` 的例外表。
#:
#: ``requires`` 说的本来是「这个源格式需要什么」—— 绝大多数格子跟着源走，
#: 所以它挂在源上而不是抄进每一条条目里。两个例外：
#:
#: * **TXT → DOCX** 写出来的是一份 DOCX，要 python-docx，而 TXT 的另外三个
#:   目标（pdf / html / md）一个外部组件都不碰；
#: * **任意源 → HEIC** 要的是 HEVC **编码器**，而 HEIC 作为源那一侧要的是
#:   **解码器**（见 ``SOURCE_REQUIREMENTS[SOURCE_HEIC]``）。
#:
#: 两种一刀切都是错的：一律按源标成 ``builtin``，缺 python-docx 的服务器上
#: 会多出一个点下去必然失败的按钮；一律标成 ``docx``，又会把本来能用的
#: 三个目标一起藏掉。所以只在这几格上覆盖，其余照旧。
#:
#: **位置是有讲究的**：这张表是本模块里唯一同时用到「源」与「目标」两套
#: 词汇的常量（``(source, TARGET_HEIC)``），所以它必须排在上面那些
#: 源集合（``_IMAGE_SOURCES`` 等）与目标常量**之后**。第一版把它留在
#: 目标常量后面、源集合前面，模块一 import 就 ``NameError`` ——
#: 字典推导式在定义时求值，不是等到查表时才求值。
_REQUIRES_OVERRIDES: dict[tuple[str, str], str] = {
    (SOURCE_TXT, TARGET_DOCX): REQUIREMENT_DOCX,
    # 「转成 HEIC」由 ``_IMAGE_SOURCES`` 派生而不是手抄七行：将来加一种位图
    # 格式时，漏抄一行的表现是「新格式能转 HEIC」这个按钮在缺编码器的机器上
    # 照样亮着 —— 而那种 bug 不会有任何测试主动发现。
    **{
        (source, TARGET_HEIC): REQUIREMENT_HEIF_ENCODE
        for source in _IMAGE_SOURCES
        # HEIC → HEIC 不存在（同格式不转，见 ``_image_targets``），
        # 留一条够不着的规则只会让后来的人猜它是不是有用。
        if source != SOURCE_HEIC
    },
}


def _requires_for(source: str, target: str) -> str:
    """这一格需要什么组件。缺省跟着源走，例外见 :data:`_REQUIRES_OVERRIDES`。"""
    return _REQUIRES_OVERRIDES.get((source, target), SOURCE_REQUIREMENTS[source])


#: 源类型 -> 该源**在图矩阵里**的目标，按 :data:`TARGET_ORDER` 排列。
#: 这是条目生成的输入；生成完就没人再用它，公开视图是派生的
#: :data:`TARGETS_BY_SOURCE`。
#:
#: HTML 与 Markdown 的目标**照 §六 字面来**：HTML→PDF/TXT、MD→HTML/PDF/TXT。
#: 特别地**没有** HTML→Markdown —— 规格里没点这一条，而它能做（同一个叶子
#: 加一行就有了）恰恰是「先不做」的理由：每加一格都是对用户的一个承诺，
#: 要配测试、配界面、配文档，不能顺手送。加的时候只改这一行。
#:
#: TXT 的四个目标同样照 §六：TXT→PDF/DOCX/HTML/MD。``pdf`` 排在最前
#: （第七阶段就有的那一格，位置不动），后三个按 ``TARGET_ORDER`` 追加。
_TARGETS_BY_SOURCE_SPEC: dict[str, tuple[str, ...]] = {
    **{source: _image_targets(source) for source in _IMAGE_SOURCES},
    # SVG 的目标是显式枚举的，见 ``_SVG_SOURCES`` 的说明
    **{source: _SVG_TARGETS for source in _SVG_SOURCES},
    **{source: (TARGET_PDF,) for source in _OFFICE_SOURCES},
    SOURCE_TXT: (TARGET_PDF, TARGET_DOCX, TARGET_HTML, TARGET_MD),
    SOURCE_HTML: (TARGET_PDF, TARGET_TXT),
    SOURCE_MD: (TARGET_PDF, TARGET_HTML, TARGET_TXT),
    SOURCE_PDF: (TARGET_DOCX,),
}


def _display_name(source: str, target: str) -> str:
    """转换的紧凑名，如 ``JPG → PNG``。

    用箭头而不是「JPG 图片 转 PDF」：界面上一行里已经同时显示源文件与
    目标格式的选择，这里再重复一遍长名字只会把卡片撑爆。它也是个稳定标识，
    日志与报告里引用同一格能力时不必再拼一次。
    """
    return f"{source.upper()} → {target.upper()}"


#: 分组 -> 资源池。与 ``services/worker_pool.py`` 的 ``_REQUIREMENT_POOLS``
#: 是同一个答案的两种表达，测试会断言两者一致。
#:
#: 注意 ``text`` 归 **image** 池：TXT 走 PyMuPDF 排版，全程不碰 LibreOffice，
#: 和图片一样属于「进程内 CPU 计算」。放进 office 池的话，一批 50 个 txt
#: 会在 ``OFFICE_WORKERS=1`` 上把所有 Office 转换堵在身后 —— 那正是
#: 第八阶段要消灭的队头阻塞。
_WORKER_POOL_BY_GROUP: dict[str, str] = {
    GROUP_IMAGE: "image",
    GROUP_OFFICE: "office",
    GROUP_TEXT: "image",
    GROUP_PDF: "ocr",
}

#: 分组 -> 分发用的实现键（``services.conversion_service.CONVERTERS`` 的键）。
#: 一个键服务一整族条目：图片互转共用一个 ``image.convert``，
#: 这正是 §八「禁止为每个组合创建独立 converter」的落点。
#:
#: **图片组与文本组不在表里**：两者都按目标再分家族
#: （互转 / 转 PDF / 换个写法），见 :func:`_converter_key`。
#: 写在这里会变成一句需要被特例覆盖的谎话。
_CONVERTER_KEY_BY_GROUP: dict[str, str] = {
    GROUP_OFFICE: OFFICE_TO_PDF,
    GROUP_PDF: PDF_TO_DOCX,
}


def _converter_key(source: str, target: str) -> str:
    """这一格该走哪个家族实现。

    「转 PDF」与「换个写法」是两条不同的流水线（前者要排版，后者只是
    换一种表示），所以图片组按目标分成两支。文本组同样按目标分，
    但分法**不同**，因为 TXT 的四个目标各有一个最合适的引擎：

    - ``pdf`` → ``text.to_pdf``（PyMuPDF 排版，第七阶段就在走的路）
    - ``docx`` → ``text.to_docx``（``office.docx_writer``，不进 LibreOffice）
    - ``html`` / ``md`` → ``text.convert``（``office.markup_render``）

    HTML / Markdown 源则是另一回事：它们的 IR 要先用 ``markup_parse``
    解析出来，所以走 ``markup.*`` 两个家族，与 TXT 那三支不共用。

    SVG 必须在 ``group == GROUP_IMAGE`` **之前**判断，而且**不能**跟着
    图片组那条规则走：它的 ``pdf`` 目标不是「把位图贴进 PDF」，而是
    ``svg.to_pdf``（PyMuPDF ``convert_to_pdf``，产出真正的矢量页，
    文字可选可搜）。按图片组那条规则分派过去，用户拿到的会是一张
    栅格化后又贴进 PDF 的图 —— 矢量信息全丢，而能力 API 上写的是同一个
    "PDF"。栅格那三个目标（png/jpg/webp）确实与位图共用 ``image.convert``：
    渲染出来的本来就是一张普通的 ``PIL.Image``，后面每一步都一样。
    """
    if source in _SVG_SOURCES:
        return SVG_TO_PDF if target == TARGET_PDF else IMAGE_CONVERT
    group = SOURCE_GROUPS[source]
    if group == GROUP_IMAGE:
        return IMAGE_TO_PDF if target == TARGET_PDF else IMAGE_CONVERT
    if source in MARKUP_SOURCES:
        return MARKUP_TO_PDF if target == TARGET_PDF else MARKUP_CONVERT
    if group == GROUP_TEXT:
        return _TEXT_CONVERTER_BY_TARGET.get(target, TEXT_TO_PDF)
    return _CONVERTER_KEY_BY_GROUP[group]


#: 文本组按目标分派。缺省是 ``text.to_pdf`` —— 表里查不到时不静默换一个
#: 引擎，而是回到这一组最老的那条路（``get`` 的默认值），
#: 由 ``test_every_target_in_the_vocabulary_is_reachable`` 保证不会真发生。
_TEXT_CONVERTER_BY_TARGET: dict[str, str] = {
    TARGET_PDF: TEXT_TO_PDF,
    TARGET_DOCX: TEXT_TO_DOCX,
    TARGET_HTML: TEXT_CONVERT,
    TARGET_MD: TEXT_CONVERT,
}

#: 结果能**直接内联给浏览器看**的目标格式 —— 也就是 ``preview_url``
#: 发得出去的那几种。``docx`` 不在其中：没有浏览器能渲染它，给一个
#: 「预览」按钮只会变成下载。
#:
#: 新格式里只放 ``bmp`` 与 ``gif``：Chromium / Firefox / Safari 都拿
#: ``<img>`` 直接渲染它们。``tiff`` 与 ``ico`` **不放** —— TIFF 没有任何
#: 主流浏览器支持，ICO 的支持是不一致的（Chrome 能显示、Safari 不能）。
#: ``supports_preview`` 说错的方向只有一种代价：给用户一个点了显示
#: 破图的按钮。宁可保守。
#:
#: **``pdf`` 原先在这张表里，第十阶段 A 把它拿掉了。** 拿掉的理由不是
#: 「浏览器显示不了 PDF」（它能），而是「这条路走不通」：预览地址指向
#: ``/api/preview/{job_id}/{index}``，那个端点只发 ``image/*``，对 PDF
#: 回一句「该结果不支持预览」。留着 ``pdf`` 就等于让结果卡挂一个点下去
#: 报错的缩略图 —— 正是上面那句「宁可保守」要防的。
#:
#: PDF 结果并非没有出口：它和别的结果一样带 ``download_url``，点了就
#: 下载／在新标签打开，浏览器自己会渲染。所以少的是「内联缩略图」，
#: 不是「看不到结果」。这条差异如实写进验收报告的已知限制。
#:
#: 这张表与 :data:`INLINE_PREVIEW_MEDIA_TYPES` 是同一个答案的两种写法，
#: 由 ``tests/test_conversion_preview.py`` 逐项对账。
_PREVIEWABLE_TARGETS: frozenset[str] = frozenset(
    {TARGET_JPG, TARGET_PNG, TARGET_WEBP, TARGET_BMP, TARGET_GIF}
)

#: 分组 -> 能力类别（进 ID 前缀）。
#: ``text`` 与 ``office`` 都是 ``document`` —— 类别回答「这是什么能力」，
#: 分组回答「界面上分到哪一栏」，两者有意分开。
_CATEGORY_BY_GROUP: dict[str, str] = {
    GROUP_IMAGE: CATEGORY_IMAGE,
    GROUP_OFFICE: CATEGORY_DOCUMENT,
    GROUP_TEXT: CATEGORY_DOCUMENT,
    GROUP_PDF: CATEGORY_PDF,
}

#: 标签词表。目前只有一个：``recommended``（§二十二 的 Recommended 一栏）。
#: 词表放在注册表里而不是前端常量里，理由与整个矩阵一样 ——
#: 「哪几格是常用的」是能力数据，不是界面文案。
TAG_RECOMMENDED = "recommended"
TAGS: tuple[str, ...] = (TAG_RECOMMENDED,)

#: 分组 -> 该族目标的**推荐优先级**。「推荐」这一栏是给用户的第一屏，
#: 只能放最常用的几个，所以这里是一条有次序的偏好，不是全集。
#:
#: 规则（:func:`_tags_for` 实现，测试钉住）：**按这张表取，去掉源自身的
#: 格式，最多留三个**。于是每种图片源得到「换一种常见格式 + 转 PDF」，
#: TXT 得到「PDF / DOCX / HTML」，Office 与 PDF 源只有 PDF / DOCX 一条 ——
#: 它们本来也只有这些目标。
#:
#: 不按「同类别」推导：图片源的目标**全部**同属图片，那样推出来 Recommended
#: 会等于 Other formats，等于没分栏。
_RECOMMENDED_PRIORITY: dict[str, tuple[str, ...]] = {
    GROUP_IMAGE: (TARGET_PNG, TARGET_JPG, TARGET_PDF, TARGET_WEBP),
    GROUP_OFFICE: (TARGET_PDF,),
    GROUP_TEXT: (TARGET_PDF, TARGET_DOCX, TARGET_HTML, TARGET_MD),
    GROUP_PDF: (TARGET_DOCX,),
}

#: 推荐栏最多放几个。三个正好一行，再多第一屏就与「全部格式」没区别了。
_RECOMMENDED_LIMIT = 3


def _tags_for(source: str, target: str) -> tuple[str, ...]:
    """这一格的标签。见 :data:`_RECOMMENDED_PRIORITY` 上的规则。"""
    priority = _RECOMMENDED_PRIORITY[SOURCE_GROUPS[source]]
    recommended = tuple(t for t in priority if t != source)[:_RECOMMENDED_LIMIT]
    return (TAG_RECOMMENDED,) if target in recommended else ()


#: 源格式自己的**如实说明**，挂在该源的每一条能力上。
#:
#: 目前只有「多帧」这一件事（决策 C）：GIF 与 TIFF 都可以装多帧，
#: 而转换只处理第一帧。这句话必须出现在用户看得见的地方 ——
#: 参数面板与结果卡都读 ``note``，所以挂在这里就够了，
#: 不必在界面上再写一遍（写一遍就意味着加一种多帧格式时要改前端）。
_SOURCE_NOTES: dict[str, str] = {
    SOURCE_GIF: "GIF 可以是多帧动画，这里只转换第一帧，其余帧会被忽略。",
    SOURCE_TIFF: "TIFF 可以是多页文件，这里只转换第一页，其余页会被忽略。",
}


def _build_capabilities() -> tuple[Capability, ...]:
    """把 :data:`_TARGETS_BY_SOURCE_SPEC` 展开成正式条目表。

    ``group`` / ``requires`` / ``worker_pool`` / ``converter_key`` / ``options``
    全部从词汇表取，**不在条目上手写** —— 它们是「这个源格式是什么」的属性
    （jpg 属于图片组、要 Pillow、排 image 池），与具体转成什么无关。
    手写几十遍迟早会有一遍写错。

    ``options`` 取自 :func:`conversion.options.options_for`，那里已经按目标格式
    裁剪过（质量只对有损格式、DPI 只对能携带密度的格式）。所以
    ``test_capability_options_are_consistent_with_the_family`` 能直接断言
    「条目上的选项 == 该家族在该目标下的选项」，而不是再抄一份。
    """
    entries: list[Capability] = []
    for source in _MATRIX_SOURCE_ORDER:
        group = SOURCE_GROUPS[source]
        category = _CATEGORY_BY_GROUP[group]
        for target in _TARGETS_BY_SOURCE_SPEC[source]:
            converter_key = _converter_key(source, target)
            entries.append(
                Capability(
                    id=capability_id(source, target, category=category),
                    source_type=source,
                    target_type=target,
                    operation_type=OPERATION_CONVERSION,
                    display_name=_display_name(source, target),
                    category=category,
                    group=group,
                    requires=_requires_for(source, target),
                    worker_pool=_WORKER_POOL_BY_GROUP[group],
                    converter_key=converter_key,
                    supports_preview=target in _PREVIEWABLE_TARGETS,
                    options=options_for(
                        converter_key=converter_key,
                        source_type=source,
                        target_type=target,
                    ),
                    tags=_tags_for(source, target),
                    note=_SOURCE_NOTES.get(source),
                )
            )
    return tuple(entries)


# ----------------------------------------------------------------------
# 操作条目（第九阶段第 11 步，决策 B）
#
# 这一组是**多进多出 / 页面级**的工具：合并、拆分、压缩、删页、提取页、
# 多图合成一份 PDF。它们与转换类条目共用一份能力目录（§十二 要求 registry
# 同时支持 ``conversion`` 与 ``operation`` 两种 operation_type），
# 但**不进统一任务队列** —— 「把 3 份 PDF 合成 1 份」与「一份进一份出」
# 在结构上不是一回事，硬塞进批量模型只会两边都别扭。
#
# 所以每个条目登记的是 ``endpoint`` + ``method``：统一中心负责**入口与
# 参数 UI**（参数从 ``options_schema`` 来），提交仍然打到原来那个接口上。
# 两条机械守卫保证这张表不会腐烂（``tests/test_conversion_operations.py``）：
#
# 1. ``(endpoint, method)`` 真的在 ``app.routes`` 里；
# 2. ``options_schema`` 的键与端点 Form 字段**逐字相等**（只有
#    ``files`` / ``input_id`` 两类除外 —— 它们是服务端发的令牌或文件本身，
#    不是用户可选的旋钮）。
# ----------------------------------------------------------------------

#: 操作条目的**默认**类别与分组，也是六个 PDF 工具实际用的值。
#: 它们都产出一份 PDF、也都挂在 ``/api/pdf`` 下，所以在界面上一律归到
#: 「PDF」这一栏 —— 用户找「合并 PDF」时不会先去「图片」栏里翻。
#: 第十阶段 A 的图片元数据查看器是**第一条例外**，它自己声明 image。
_OPERATION_GROUP = GROUP_PDF
_OPERATION_CATEGORY = CATEGORY_PDF
#: 操作条目的组件需求：PyMuPDF / Pillow 都是必装依赖，所以永远可用。
#: 它们不走队列，``worker_pool`` 因此只是**声明**：写的是「这份活该算什么资源」
#: （PDF 归 ``ocr`` 池、图片归 ``image`` 池），不会有人拿它去排任务。
_OPERATION_REQUIREMENT = REQUIREMENT_BUILTIN


class _OperationSpec(NamedTuple):
    """一个工具条目的手写声明。

    用名字而不是位置元组：字段里有两个是**元组/字符串**，
    顺序写错是静默串位（``accepts`` 与 ``note`` 的位置搞反，界面上会拿
    「多帧只取第一帧」当放行清单，而且不报错）。

    ``category`` / ``group`` / ``source_type`` / ``target_type`` 有默认值，
    所以六个 PDF 工具那六条**一个字都不用改**。默认值存在的理由是历史：
    第九阶段登记它们时这四项是硬编码在 :func:`_build_operations` 里的
    （六个工具都产出一份 PDF、都挂在 ``/api/pdf`` 下）。第十阶段 A 要再加
    一个**图片**工具，硬编码就挡路了 —— 与其在下面写 if，不如让每一条
    自己声明。默认值不是「PDF 更正确」，只是「不改既有六条」。
    """

    name: str
    display_name: str
    endpoint: str
    #: 文件怎么交给端点（:data:`~conversion.capability.INPUT_KINDS`）。
    input_field: str
    #: 接受哪些源类型的文件。
    accepts: tuple[str, ...]
    note: str | None = None
    category: str = _OPERATION_CATEGORY
    group: str = _OPERATION_GROUP
    source_type: str = SOURCE_PDF
    target_type: str = TARGET_PDF

#: 六个工具。``(操作名, 显示名, 端点, 输入怎么送, 收哪些源类型, 说明)``。
#: 说明里写的必须是**界面上要说给用户听的事实**，尤其是
#: ``op.image-images-to-pdf`` 那条「每张图片一页」—— 见下面 :data:`OPERATIONS`
#: 上的注释。
_OPERATION_SPECS: tuple[_OperationSpec, ...] = (
    _OperationSpec(
        "pdf-merge",
        "PDF 合并",
        "/api/pdf/merge",
        INPUT_FILES,
        (SOURCE_PDF,),
    ),
    _OperationSpec(
        "pdf-split",
        "PDF 拆分",
        "/api/pdf/split",
        INPUT_UPLOAD,
        (SOURCE_PDF,),
    ),
    _OperationSpec(
        "pdf-compress",
        "PDF 压缩",
        "/api/pdf/compress",
        INPUT_UPLOAD,
        (SOURCE_PDF,),
    ),
    _OperationSpec(
        "pdf-extract-pages",
        "PDF 页面提取",
        "/api/pdf/extract-pages",
        INPUT_UPLOAD,
        (SOURCE_PDF,),
    ),
    _OperationSpec(
        "pdf-delete-pages",
        "PDF 页面删除",
        "/api/pdf/delete-pages",
        INPUT_UPLOAD,
        (SOURCE_PDF,),
    ),
    _OperationSpec(
        "image-images-to-pdf",
        "图片合成 PDF",
        "/api/pdf/from-images",
        INPUT_FILES,
        # **七种图片**，不是 ``source_type`` 那一个 ``pdf``：这个工具收的是
        # 图片。``source_type`` 只说得了一个标量（§十四 的契约），
        # 放行清单在这里，界面上因此不会出现「选了 PDF 去合成图片」。
        _IMAGE_SOURCES,
        # §十 的「One image per page（默认 true）」在这里落地。
        # **不登记成一个选项**：那个端点根本没有这个参数，多图合成时
        # 「每图一页」结构上恒真、无从选择。发一个点了没反应的开关，
        # 比不发更糟 —— 所以它是一句说明，不是控件。
        "多张图片按上传顺序合成一份 PDF，每张图片一页；多帧文件只取第一帧。",
    ),
    _OperationSpec(
        "image-metadata",
        "查看图片元数据",
        "/api/image/metadata",
        INPUT_FILE,
        # 七种位图，**不含 SVG**（:data:`_IMAGE_SOURCES` 本来就不含它）。
        # 这不是偷懒：SVG 没有 EXIF、没有密度、没有色彩模式，§三十四 那十一行
        # 里有三行对它结构上不适用，剩下几行的答案早就印在转换结果卡上了。
        # 放它进来只会让「图片元数据」这个工具多一条自己都说不清的支路。
        _IMAGE_SOURCES,
        # 说明里必须写清**这是一次只读的查看**：它不产出任何文件。
        # 界面上如果照着别的工具给它配一个「下载结果」按钮，用户会等一个
        # 永远不会出现的东西。§三十四 要的是一个 viewer，不是一条转换。
        "读取图片自带的拍摄信息（相机、镜头、时间等）；只查看，不修改文件、"
        "不产生新文件。GPS 只告诉你有没有，不给坐标。",
        category=CATEGORY_IMAGE,
        group=GROUP_IMAGE,
        # 「拿图片做点事，还你一份图片信息」。这两个键对 operation 只是
        # 契约要求的占位（§十四），真正表达「收哪些文件」的是 ``accepts``。
        source_type=SOURCE_JPG,
        target_type=TARGET_JPG,
    ),
)


def _build_operations() -> tuple[Capability, ...]:
    """把 :data:`_OPERATION_SPECS` 展开成正式条目。"""
    entries: list[Capability] = []
    for spec in _OPERATION_SPECS:
        operation = operation_id(spec.name)
        entries.append(
            Capability(
                id=operation,
                # 操作没有「一对源与目标」的含义，但契约要求这两个键存在
                # （§十四）。填的是**这份活处理什么、产出什么**：
                # 六个 PDF 工具都是「拿 PDF 做点事，还你一份 PDF」。
                # 「图片合成 PDF」收图片这件事不在这里表达 —— 那是
                # ``accepts`` 的活（见 :class:`~conversion.capability.Capability`）。
                source_type=spec.source_type,
                target_type=spec.target_type,
                operation_type=OPERATION_OPERATION,
                display_name=spec.display_name,
                category=spec.category,
                group=spec.group,
                requires=_OPERATION_REQUIREMENT,
                # 池子由 ``group`` 推，与转换条目同一条规则 —— 不另写一份。
                worker_pool=_WORKER_POOL_BY_GROUP[spec.group],
                endpoint=spec.endpoint,
                # 操作是「一次作业处理好几个输入」，不是「N 个各自独立的
                # 任务」—— 那正是 ``supports_batch`` 在转换条目里的含义。
                supports_batch=False,
                options=operation_options(operation),
                note=spec.note,
                accepts=spec.accepts,
                input_field=spec.input_field,
            )
        )
    return tuple(entries)


#: 条目表。**它是生成的，不是手写的** —— 「哪一对能转」的手写真相在
#: :data:`_TARGETS_BY_SOURCE_SPEC`（与 :data:`_OPERATION_SPECS`），
#: 这里由循环把它们铺成条目，而不是抄一遍：抄写正是上一版矩阵漂移的根源。
#:
#: 之所以要写清楚这一句：把生成结果当成手写真相，下次加一格时就会有人
#: 直接往这里塞一条，而 ``_TARGETS_BY_SOURCE_SPEC`` 那份没跟着改 ——
#: 于是又有了两份真相，正是本模块开头花了一整段要消灭的东西。
CAPABILITIES: tuple[Capability, ...] = _build_capabilities() + _build_operations()

#: ID -> 条目（含 operation，将来登记了就有）
CAPABILITY_BY_ID: dict[str, Capability] = {entry.id: entry for entry in CAPABILITIES}

#: ``(源, 目标)`` -> 条目。只收 conversion：operation 没有「一对一」的含义。
CAPABILITY_BY_PAIR: dict[tuple[str, str], Capability] = {
    (entry.source_type, entry.target_type): entry
    for entry in CAPABILITIES
    if entry.operation_type == OPERATION_CONVERSION
}

#: 只按操作类型切一刀。前端的两级选择器（转换 / 工具）分别吃这两个。
#: 生命周期内不变，所以在 import 期算一次。
CONVERSIONS: tuple[Capability, ...] = tuple(
    entry for entry in CAPABILITIES if entry.operation_type == OPERATION_CONVERSION
)
OPERATIONS: tuple[Capability, ...] = tuple(
    entry for entry in CAPABILITIES if entry.operation_type == OPERATION_OPERATION
)

#: 系统认识的**全部**格式，源在前、只在目标侧出现的格式补在后面。
#: 用 ``_MATRIX_SOURCE_ORDER`` 而不是 ``SOURCE_TYPES``：这份顺序是给界面用的，
#: 与 :data:`SOURCE_TYPES` 那句错误提示不是同一件事（见那里的说明）。
FORMAT_TYPES: tuple[str, ...] = _MATRIX_SOURCE_ORDER + tuple(
    target for target in TARGET_ORDER if target not in _MATRIX_SOURCE_ORDER
)


def _derive_pools() -> dict[str, str]:
    """源类型 -> 条目声明的资源池。

    同一个源的所有条目必然同池（``worker_pool`` 由 ``group`` 决定，而
    ``group`` 由 ``source_type`` 决定），所以「先到先得」不会随机 ——
    但这一点由 ``test_one_source_never_spans_two_pools`` 钉住，
    不靠这段注释。
    """
    pools: dict[str, str] = {}
    for entry in CONVERSIONS:
        pools.setdefault(entry.source_type, entry.worker_pool)
    return pools


#: 源类型 -> 该跑哪个资源池。``services/worker_pool.pool_for`` 的**首选**依据：
#: 池是「这个源格式要用哪种资源」的正式登记处，登记在条目上比散在
#: worker_pool 的一张映射表里更贴近事实。
POOL_BY_SOURCE: dict[str, str] = _derive_pools()


def pool_for_source(source_type: str | None) -> str | None:
    """注册表为这个源格式声明的资源池；不认识就返回 ``None``。

    返回 ``None`` 与返回 ``"default"`` 是两件事：前者是「我不知道」，
    调用方该继续走自己的回退路径；后者是「我知道，就用收容池」。
    把两者混成一个值，回退路径就永远轮不到了。
    """
    if not source_type:
        return None
    return POOL_BY_SOURCE.get(source_type)

# ----------------------------------------------------------------------
# 派生视图
# ----------------------------------------------------------------------

def _derive_targets() -> dict[str, tuple[str, ...]]:
    """从条目表推出矩阵：源 -> 可选目标（去重且保序）。

    顺序按 :data:`TARGET_ORDER` 排，所以条目表里怎么写都不影响对外的
    稳定顺序 —— 断言里那些 ``["png", "webp", "pdf"]`` 是钉得住的。
    """
    collected: dict[str, list[str]] = {}
    for entry in CAPABILITIES:
        if entry.operation_type != OPERATION_CONVERSION:
            continue  # 操作类不进矩阵：它没有「这个源能转成哪些目标」的含义
        bucket = collected.setdefault(entry.source_type, [])
        if entry.target_type not in bucket:
            bucket.append(entry.target_type)
    return {
        source: tuple(sorted(targets, key=TARGET_ORDER.index))
        for source, targets in collected.items()
    }


#: 源类型 -> 可选目标。
#: **不列同格式转换**（jpg → jpg 之类）：那是一次无意义的重新编码，
#: 用户要的是「换个格式」，不是「把文件重压一遍」。
TARGETS_BY_SOURCE: dict[str, tuple[str, ...]] = _derive_targets()

#: 真正被用到过的目标，按 :data:`TARGET_ORDER` 排列。
#: 从矩阵派生而不是从 ``TARGET_LABELS`` 派生：登记了但没人能转到的目标
#: 不该出现在前端的目标下拉里。
TARGET_TYPES: tuple[str, ...] = tuple(
    target
    for target in TARGET_ORDER
    if any(target in targets for targets in TARGETS_BY_SOURCE.values())
)


# ----------------------------------------------------------------------
# 查询
# ----------------------------------------------------------------------

def normalize_extension(name: str | None) -> str | None:
    """取小写扩展名（含点）。取不到或不是字符串就返回 None。"""
    if not name:
        return None
    suffix = Path(str(name)).suffix.lower()
    return suffix or None


def source_for_extension(name: str | None) -> str | None:
    """按扩展名判断源类型；不在表里返回 None（调用方据此报「不支持」）。"""
    ext = normalize_extension(name)
    return EXTENSION_TO_SOURCE.get(ext) if ext else None


def targets_for(source: str | None) -> tuple[str, ...]:
    """某个源类型可选的目标；未知源返回空元组（不抛错）。"""
    if not source:
        return ()
    return TARGETS_BY_SOURCE.get(source, ())


def capability_for(source_type: str | None, target_type: str | None) -> Capability | None:
    """这一对组合对应的条目；不支持则 None。

    :func:`supports` 的升级版：调用方想知道的不只是「能不能」，
    还有「该走哪个池、有哪些选项、ID 叫什么」。
    """
    if not source_type or not target_type:
        return None
    return CAPABILITY_BY_PAIR.get((source_type, target_type))


def supports(source: str | None, target: str | None) -> bool:
    """这一对组合是否真的能转。任何一边不认识都返回 False，不抛错。"""
    return capability_for(source, target) is not None


def media_type_for_target(target: str) -> str:
    """目标格式对应的下载 MIME；未知目标兜底为二进制流。"""
    return MEDIA_TYPE_BY_TARGET.get(target, "application/octet-stream")


#: 可以内联预览的 MIME 集合 —— :data:`_PREVIEWABLE_TARGETS` 换一种写法。
#: 派生而不是手写，所以两张表不可能各自漂移。
INLINE_PREVIEW_MEDIA_TYPES: frozenset[str] = frozenset(
    media_type_for_target(target) for target in _PREVIEWABLE_TARGETS
)

#: 预览地址的形状。与 ``routers/download.py`` 里那条路由**同一条**；
#: 索引恒为 ``0``，因为一次转换只产出一份文件，该项的令牌就指向它
#: （``tasks/conversion_tasks.py`` 每项注册一个单项 job）。
_PREVIEW_URL_TEMPLATE = "/api/preview/{job_id}/0"


def preview_url(job_id: str | None, target_type: str | None) -> str | None:
    """一份结果的内联预览地址；预览不了就是 ``None``。

    **拿不准就给 ``None``。** 这个函数唯一的职责是回答「结果卡上能不能
    挂一个缩略图」，答案错了用户就会点出一个坏掉的图；而给 ``None``
    最坏也只是少一个便利入口（结果照样在 ``download_url`` 上）。

    第十阶段 A §三十九–§四十一：预览**复用下载令牌那条路**
    （``/api/preview/{job_id}/{index}``，由 ``job_store`` 解析、由
    结果自身的令牌授权），**没有**新开一个绕过权限的静态文件接口。
    """
    if not job_id or target_type not in _PREVIEWABLE_TARGETS:
        return None
    return _PREVIEW_URL_TEMPLATE.format(job_id=job_id)


def format_category(value: str) -> str | None:
    """一个格式属于哪一类（``image`` / ``document`` / ``pdf``）。

    先看它作为**源**时的分组；只在目标侧出现的格式（将来会有 ``ico``）
    没有分组，就取第一个能产出它的条目所属的类别 —— 「谁会产出它」
    比「它像什么」更接近事实。
    """
    group = SOURCE_GROUPS.get(value)
    if group is not None:
        return _CATEGORY_BY_GROUP[group]
    for entry in CONVERSIONS:
        if entry.target_type == value:
            return entry.category
    return None


def format_label(value: str) -> str:
    """格式的界面显示名；源词汇表和目标词汇表里取到哪个用哪个。"""
    return SOURCE_LABELS.get(value) or TARGET_LABELS.get(value, value.upper())


def _availability(
    *,
    office_ok: bool,
    pdf_to_word_ok: bool,
    heif_decode_ok: bool,
    heif_encode_ok: bool,
) -> dict[str, bool]:
    """组件需求 -> 这台服务器现在满不满足。

    ``markup`` 永远为真：Markdown/HTML 子集的解析与渲染是自写的纯标准库
    代码，不依赖任何外部组件（§六 的「零新依赖」决策）。

    ``docx`` 与 ``pdf_to_word`` 读的是**同一个探测结果**（python-docx 在不在）：
    它们是同一个库，只是缺失说明要分开说（见 ``REQUIREMENT_DOCX``）。

    ``heif_decode`` / ``heif_encode`` 读的是 ``compressors.heif.heif_support()``
    那一次**真实往返探测**的结果，**不是**「pillow-heif 装没装」——
    这个区别是 §六 的全部要点，见 :data:`REQUIREMENT_HEIF_DECODE`。
    """
    return {
        REQUIREMENT_BUILTIN: True,
        REQUIREMENT_IMAGE: True,  # Pillow 是硬依赖，装了服务就能转
        REQUIREMENT_MARKUP: True,
        REQUIREMENT_OFFICE: office_ok,
        REQUIREMENT_PDF_TO_WORD: pdf_to_word_ok,
        REQUIREMENT_DOCX: pdf_to_word_ok,
        REQUIREMENT_HEIF_DECODE: heif_decode_ok,
        REQUIREMENT_HEIF_ENCODE: heif_encode_ok,
    }


def capability_snapshot(
    entry: Capability,
    *,
    office_ok: bool,
    pdf_to_word_ok: bool,
    heif_decode_ok: bool,
    heif_encode_ok: bool,
) -> bool:
    """这一格能力现在能不能用。**纯函数**，探测结果由参数传入。"""
    return _availability(
        office_ok=office_ok,
        pdf_to_word_ok=pdf_to_word_ok,
        heif_decode_ok=heif_decode_ok,
        heif_encode_ok=heif_encode_ok,
    )[entry.requires]


def available_matrix(
    *,
    office_ok: bool,
    pdf_to_word_ok: bool,
    heif_decode_ok: bool,
    heif_encode_ok: bool,
) -> dict[str, tuple[str, ...]]:
    """按服务器当前**真实**具备的组件过滤后的能力矩阵。

    ``office_ok=False``（没装 LibreOffice）→ 六种 Office 源整体消失；
    ``pdf_to_word_ok=False``（缺 python-docx）→ ``pdf`` 消失，
    且 TXT 少一个 ``docx`` 目标（其余三个留着）；
    **TXT 在任何一种情况下都保留** —— 它不依赖 LibreOffice。

    ``heif_decode_ok=False`` → ``heic`` 整行消失（读都读不进来）；
    ``heif_encode_ok=False`` → 其余各行少一个 ``heic`` 目标（能读不能写）。
    两者**各管各的**：只有解码器的服务器照样能 ``HEIC → JPG``。

    过滤**逐格**做（不是逐源）：TXT 有四个目标，缺 python-docx 只影响其中
    写着 DOCX 的那一格。整行藏掉会让 pdf / html / md 三个本来能用的目标
    跟着消失。过滤后一个目标都不剩的源才整行去掉 —— 矩阵里留一个空行，
    前端会渲染出一张点不动的源格式卡片。

    可用性由参数传入而不是在这里现探测，是为了让这个函数保持纯函数：
    探测（找 soffice、import docx、试编一张 HEIC）在 services 层做，
    测试可以随便喂组合。

    **签名不得再增加参数**：``pdf → docx`` 与 OCR 是两件独立的事，
    OCR 不可用时它照样保留（见 :func:`ocr_note`）。多一个 ``ocr_ok``
    参数就是在把两件事重新绑回一起，
    ``test_ocr_never_removes_the_pdf_to_word_capability``
    用 ``inspect.signature`` 把这一点钉住了。
    （HEIC 那两个是有实质区别的**新维度**，不是把 OCR 塞进来，
    所以它们加得，``ocr_ok`` 加不得。）
    """
    available = _availability(
        office_ok=office_ok,
        pdf_to_word_ok=pdf_to_word_ok,
        heif_decode_ok=heif_decode_ok,
        heif_encode_ok=heif_encode_ok,
    )
    matrix: dict[str, tuple[str, ...]] = {}
    for source, targets in TARGETS_BY_SOURCE.items():
        if not available[SOURCE_REQUIREMENTS[source]]:
            continue
        kept = tuple(
            target for target in targets if available[_requires_for(source, target)]
        )
        if kept:
            matrix[source] = kept
    return matrix


def unavailable_reasons(
    *,
    office_ok: bool,
    pdf_to_word_ok: bool,
    heif_decode_ok: bool,
    heif_encode_ok: bool,
) -> list[str]:
    """把「哪些能力因为缺组件而没出现」写成给用户看的话。

    能力被悄悄藏起来、界面上什么都不说，用户只会以为网站坏了。
    """
    reasons: list[str] = []
    if not office_ok:
        reasons.append(
            "服务器未安装文档转换组件，Word / Excel / PowerPoint 转 PDF 暂时不可用；"
            "TXT 与图片转换不受影响。"
        )
    if not pdf_to_word_ok:
        reasons.append(
            "服务器缺少 Word 文档组件，PDF 转 Word 与 TXT 转 Word 暂时不可用。"
        )
    # HEIC 的两句话**分开写**，因为它们是两件事，用户能做的事也不同：
    # 「读不了」要换个格式再传，「写不了」只是不能选 HEIC 当目标。
    # 合成一句「HEIC 不可用」会让只缺编码器的服务器白挨一句「读不了」。
    if not heif_decode_ok:
        reasons.append(
            "服务器未安装 HEIC 组件，iPhone / 相机拍摄的 HEIC、HEIF 图片暂时无法转换。"
        )
    elif not heif_encode_ok:
        # ``elif``：连解码都没有时，说「不能转成 HEIC」是废话，
        # 而且会让一个缺组件的部署多挨一句没有信息量的话。
        reasons.append("服务器上的 HEIC 组件不支持编码，暂时无法把图片转成 HEIC。")
    return reasons


def ocr_note(*, ocr_ok: bool, max_pages: int) -> str:
    """PDF → Word 的 OCR 提示。

    §七 明确要求：**OCR 不可用时 ``pdf → docx`` 仍然保留** ——
    带文字层的 PDF 照样能转，只是扫描页会失败。所以这是一条提示，
    不是一道能力开关；把它写成「能力消失」是过度降级。

    ``max_pages`` 由调用方传 ``settings.PDF_TO_WORD_MAX_OCR_PAGES``：
    这个数字在配置里改过，提示语必须跟着变，不能在这里再写死一份。
    """
    if ocr_ok:
        return f"扫描版 PDF 会自动识别文字（每份最多 {max_pages} 页），转换时间较长。"
    return (
        "服务器未启用文字识别，带文字层的 PDF 可以正常转换；"
        "纯扫描件会转出空白内容。"
    )
