"""HEIC / HEIF 支持：注册 + 运行期探测（第十阶段 A §五–§八）。

## 为什么单独一个模块

Pillow 12 本身**一行 HEIC 代码都没有** —— 实测 ``Image.OPEN`` / ``Image.SAVE``
里没有 HEIF，``registered_extensions()`` 里也没有 ``.heic``，喂一个真实的
``ftypheic`` 头会得到 ``UnidentifiedImageError``。HEIC 的全部能力来自
``pillow-heif`` 这个可选包，它在导入时把自己挂进 Pillow 的插件表。

于是这里有两件必须分开的事：

* **注册**（:func:`register`）—— 一次性副作用，必须**在任何一次 ``Image.open``
  之前**跑完，否则 Pillow 认不出 HEIC。它是幂等的、无条件的（包不在就什么都不做）。
* **探测**（:func:`heif_support`）—— 「这台机器现在到底能解码吗、能编码吗」。
  它**不是**「包在不在」的同义词，见下。

## 为什么探测要真的编一次码，而不是 ``find_spec``

``pdf_to_docx.docx_available`` 用 ``find_spec`` 就够了：python-docx 装上就是
装上了，没有中间态。**HEIC 有中间态**，而且 §六 点名要求处理它：

    pillow-heif 把 libheif（容器解析）与编解码器分开打包。
    libde265 是 HEVC **解码**器，libx265 是 HEVC **编码**器。
    一个只带 libde265 的构建能 ``HEIC → JPG``，**不能** ``JPG → HEIC``。

所以「包在不在」回答不了「能不能编码」。这里改成**真的做一次**：编一张 2×2
再读回来。代价是微秒级（实测整轮 < 5 ms），换来的是 §六 要的那个区分 ——
只有解码时就只发布解码方向，绝不把一个点下去必然失败的 ``JPG → HEIC``
摆在界面上（§三十六「不要伪装成 encode 可用」）。

探测结果**缓存**：编码器不会在进程运行期间消失，而首页每次刷新都会问一遍。
"""

from __future__ import annotations

import base64
import importlib.util
import io
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

#: 探测用的位图尺寸。1×1 在某些编码器的边缘分支上会走不同路径，
#: 2×2 能确认它真的跑完了完整的编码流程，又便宜到可以忽略。
_PROBE_SIZE = (2, 2)

#: Pillow 给 HEIC 的格式名。**不是** ``HEIC`` —— ``pillow-heif`` 注册的
#: 是 ``HEIF``（容器名），实测 ``img.format == "HEIF"``。写成 ``HEIC``
#: 会让 ``normalize_format`` 之后与魔数表里的键对不上。
PILLOW_FORMAT = "HEIF"

#: 编码层内部名（``compressors.encoder`` 那一套用的是小写词汇）。
FORMAT_NAME = "heif"

#: 一张 8×8 的**真实 HEIC 样张**，base64 固化在这里。
#:
#: 为什么生产代码里要存一个二进制样张 —— 因为「能不能解码」**必须独立于
#: 「能不能编码」去测**。最初的写法是「编一张再读回来」，那有一个要命的
#: 盲区：只带 libde265（HEVC 解码器）而不带 libx265（编码器）的构建
#: 编不出东西，于是探测会把 ``decode`` 也报成 False —— 而它其实解得开。
#: §六 恰恰点名要处理这个中间态（「只有解码时只发布解码方向」），
#: 一个测不出解码的探测等于把那条规矩架空。
#:
#: 有了这张样张，两个方向各自独立可测：解码 = 读这张，编码 = 自己写一张。
#:
#: 重新生成的办法（换 pillow-heif 大版本后如果这张读不动了）::
#:
#:     from PIL import Image; import io, pillow_heif, base64
#:     pillow_heif.register_heif_opener()
#:     b = io.BytesIO()
#:     Image.new("RGB", (8, 8), (200, 30, 40)).save(b, format="HEIF", quality=1)
#:     print(base64.b64encode(b.getvalue()).decode())
#:
#: 它**只服务于探测**，不是任何测试的替身 —— 测试的样张都是现场生成的。
_HEIF_PROBE_SAMPLE = base64.b64decode(
    "AAAAHGZ0eXBoZWljAAAAAG1pZjFoZWljbWlhZgAAAXxtZXRhAAAAAAAAACFoZGxyAAAAAAAAAABwaWN0"
    "AAAAAAAAAAAAAAAAAAAAACJpbG9jAAAAAERAAAEAAQAAAAABoAABAAAAAAAAACYAAAAjaWluZgAAAAAA"
    "AQAAABVpbmZlAgAAAAABAABodmMxAAAAAA5waXRtAAAAAAABAAAA/GlwcnAAAADcaXBjbwAAAHVodmND"
    "AQNwAAAAAAAAAAAAHvAA/P34+AAADwNgAAEAGEABDAH//wNwAAADAJAAAAMAAAMAHroCQGEAAQApQgEB"
    "A3AAAAMAkAAAAwAAAwAeoCCBBZbqrprm4CGgwIAAAAyAAAADAIRiAAEABkQBwXPBiQAAABNjb2xybmNs"
    "eAABAA0ABoAAAAAUaXNwZQAAAAAAAABAAAAAQAAAAChjbGFwAAAACAAAAAEAAAAIAAAAAf///8gAAAAC"
    "////yAAAAAIAAAAQcGl4aQAAAAADCAgIAAAAGGlwbWEAAAAAAAAAAQABBYECAwWEAAAALm1kYXQAAAAi"
    "KAGvBRITTOD6ulI7n90h4MBlH7PlQKzvFvyglVoQkfFbQA=="
)


@dataclass(frozen=True, slots=True)
class HeifSupport:
    """这台机器现在真的具备哪些 HEIC 能力。

    ``decode`` 与 ``encode`` **是两个独立的事实**，不是一个开关的两面：
    只带 libde265 的构建是 ``decode=True, encode=False``。把它们合成一个
    ``available`` 会让 §六 那条规矩无从落地。
    """

    decode: bool = False
    encode: bool = False
    #: ``pillow-heif`` 的版本；包没装是 ``None``。
    package_version: str | None = None
    #: 底层 ``libheif`` 的版本；探测失败是 ``None``。
    libheif_version: str | None = None
    #: 不可用的原因（给 ``unavailable_reasons`` 用的一句话）；全可用是 ``None``。
    reason: str | None = None

    @property
    def any(self) -> bool:
        return self.decode or self.encode


#: 缓存。``None`` 表示还没探测过 —— 用 ``None`` 而不是一个 bool 哨兵，
#: 是因为探测结果本身可能是「都不可用」，那也是一个有效结果。
_cached: HeifSupport | None = None

#: 注册只做一次。``pillow_heif.register_heif_opener()`` 本身是幂等的，
#: 但重复调用会重复往 Pillow 插件表里塞，没必要。
_registered = False


def register() -> None:
    """把 HEIC 插件挂进 Pillow。**幂等**，没有包时静默跳过。

    静默是**对的**：HEIC 是可选的，一台没装 ``pillow-heif`` 的服务器
    应该在 JPG/PNG 上一切照旧，而不是启动就报错。缺了什么由
    :func:`heif_support` 如实回答，界面据此隐藏 HEIC。
    """
    global _registered
    if _registered:
        return
    _registered = True

    if importlib.util.find_spec("pillow_heif") is None:
        # 连包都没有：不 import，也不记 WARNING —— 这是一个受支持的部署形态，
        # 不是异常。真正需要解释的时候由 ``heif_support().reason`` 说。
        return

    try:
        import pillow_heif

        pillow_heif.register_heif_opener()
    except Exception:  # pragma: no cover - 装坏了 / DLL 缺失
        # 装是装了但加载不起来（缺 VC 运行库、wheel 与 ABI 不匹配……）。
        # **不能让它冒泡**：这条路径在包导入期，抛出去整个应用都起不来。
        # 记一条 WARNING 并降级成「没有 HEIC」——这正是 §八十二 要的
        # availability fallback。
        logger.warning("pillow-heif 已安装但无法加载，HEIC 功能将不可用", exc_info=True)


def _probe() -> HeifSupport:
    """两个方向各测各的，互不牵连。

    * **解码** = 读那张固化的真实样张；
    * **编码** = 自己写一张 2×2 出来。

    最初的写法用「编一张再读回来」同时回答两个问题，那会把
    「能解码但不能编码」的构建误报成「两个都不行」。分开测之后，
    ``decode=True, encode=False`` 是一个真能被观测到的结果。
    """
    if importlib.util.find_spec("pillow_heif") is None:
        return HeifSupport(
            reason="服务器未安装 HEIC 组件（pillow-heif），HEIC / HEIF 图片暂时无法处理。"
        )

    try:
        import pillow_heif
        from PIL import Image
    except Exception:
        return HeifSupport(
            reason="服务器上的 HEIC 组件无法加载，HEIC / HEIF 图片暂时无法处理。"
        )

    register()

    try:
        version = pillow_heif.__version__
    except Exception:  # pragma: no cover
        version = None
    try:
        libheif = pillow_heif.libheif_version()
    except Exception:  # pragma: no cover
        libheif = None

    # ---- 解码：读那张真实样张 ----
    try:
        with Image.open(io.BytesIO(_HEIF_PROBE_SAMPLE)) as image:
            image.load()
        decode_ok = True
    except Exception:
        # 没有 HEVC 解码器（缺 libde265）就是走到这里。
        decode_ok = False

    # ---- 编码：自己写一张 ----
    try:
        buffer = io.BytesIO()
        Image.new("RGB", _PROBE_SIZE, (1, 2, 3)).save(buffer, format=PILLOW_FORMAT)
        encode_ok = bool(buffer.getvalue())
    except Exception:
        # 没有 HEVC 编码器（缺 libx265）就是走到这里。
        encode_ok = False

    if decode_ok and encode_ok:
        return HeifSupport(
            decode=True,
            encode=True,
            package_version=version,
            libheif_version=libheif,
        )

    if decode_ok:
        # §六 的那个中间态：能解不能编。发布解码方向，**不发布编码方向**。
        return HeifSupport(
            decode=True,
            encode=False,
            package_version=version,
            libheif_version=libheif,
            reason="服务器上的 HEIC 组件不支持编码，其它格式转 HEIC 暂时不可用。",
        )

    if encode_ok:
        # 理论上不该出现（能编码必然自带解码能力），但真出现了就如实报 ——
        # 与其断言「不可能」，不如让它在界面上表现为「只有编码方向」。
        return HeifSupport(
            decode=False,
            encode=True,
            package_version=version,
            libheif_version=libheif,
            reason="服务器上的 HEIC 组件无法解码，HEIC / HEIF 图片暂时无法处理。",
        )

    return HeifSupport(
        decode=False,
        encode=False,
        package_version=version,
        libheif_version=libheif,
        reason="服务器上的 HEIC 组件不可用，HEIC / HEIF 图片暂时无法处理。",
    )


def heif_support() -> HeifSupport:
    """这台机器现在真的具备哪些 HEIC 能力。结果缓存。"""
    global _cached
    if _cached is None:
        _cached = _probe()
    return _cached
