"""第十阶段 C（§十一–§十五）：图片炸弹（decompression bomb）三层防线。

Phase 10A 把防线做出来了 —— ``utils/validation.py`` 与 ``compressors/loader.py``
各有一处 ``except Image.DecompressionBombError`` —— 但当时**一个测试都没有**。
Phase 10A 报告 §18.2 / §24.13 如实记着这个缺口，本文件把它补上。

## 为什么是三层，不是一层

Pillow 的炸弹检查在 ``Image.open()`` 里按图片**声明的**尺寸算一次乘法，
然后分两档：

* ``像素 > 2 × MAX_IMAGE_PIXELS`` → 抛 ``DecompressionBombError``；
* ``MAX_IMAGE_PIXELS < 像素 <= 2 × MAX_IMAGE_PIXELS`` → **只发一条**
  ``DecompressionBombWarning``（``RuntimeWarning``，默认不打断流程）。

第二档不抛异常，所以「接住 Error」和「拦住 Warning 那一带」是两件独立的事，
各自要有一条测试 —— 只测前者会漏掉整整一半的炸弹区间，而且漏掉的那一半
更容易被忽略：它不报错、不中断，用户会拿到一个「成功」的结果。

两档给出的中文提示**刻意不同**，下面逐字断言，不写成「包含『过大』」：

* Error 档 → ``图片尺寸过大，超过服务器处理上限``
* Warning 档 → ``图片像素总量过大，超过服务器处理上限``

第三层是超长边（``MAX_IMAGE_EDGE``）：它走的是**安全等比缩减**而不是拒绝。
那是 Phase 10A 定下的取舍，本阶段只验证、不修改。

## 为什么不真造一张巨图（§十二）

100000 × 100000 的真图要几十 GB 内存，测试机上跑不了，而且会拖慢整轮回归。
这里用的是**最小可编码图片 + 改文件头**：先让 Pillow 写一张真的 8×8 PNG，
再把 IHDR 里的宽高字段改成天文数字，并**重算那一段的 CRC**。

重算 CRC 这一步不能省：不改的话 Pillow 会在 ``verify()`` 里以「CRC 校验失败」
报文件损坏，测到的就变成「损坏文件」那条路，而不是炸弹这条路。

于是文件只有 77 字节，而 Pillow 在 ``open()`` 阶段就按声明尺寸算出了炸弹 ——
测的是**真实的检查路径**（Pillow 真的读了文件头、真的做了那次乘法），不是打桩。
第一条测试专门钉住这个性质。
"""

from __future__ import annotations

import io
import math
import warnings
import zlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from compressors.loader import load_image
from config import settings
from tests.conftest import (
    conversion_task,
    image_files,
    run_task,
    submit_conversion,
    wait_conversion,
)
from tests.test_conversion_api import assert_clean
from utils import validation
from utils.errors import ErrorCode, ValidationError

# 下档的炸弹**必然**会让 Pillow 发一条 ``DecompressionBombWarning`` —— 那是
# 这整套防线所依赖的事实（见 test_pillow_raises_in_one_band_and_only_warns_in_the_other），
# 不是需要清理的噪音。默认在本文件里滤掉它：不滤的话，凡是碰到下档的用例
# 都会往 pytest 的 warnings summary 里塞一行，真正该被看见的新警告会被淹掉。
#
# 只影响本文件，而且「警告确实会发出来」仍有一条专门的用例在断言 ——
# 这里滤掉的是**汇总里的重复行**，不是那个事实本身。
pytestmark = pytest.mark.filterwarnings(
    "ignore::PIL.Image.DecompressionBombWarning"
)

#: 老的图片接口（Phase 9 之前的页面还在用，§六十九 明令不删）
LEGACY_CONVERT = "/api/image/convert"

#: 炸弹夹具的字节数上限。实测值是 77 字节。
#:
#: 这条上限的作用是**防止夹具退化成真图**：哪天有人图省事把这里改成
#: ``Image.new("RGB", (20000, 20000))``，测试会立刻变慢甚至 OOM ——
#: 这条断言会先一步拦下来（§十二：不要创建巨大的真实图片）。
_BOMB_FIXTURE_MAX_BYTES = 1024

#: 两档的名字。用名字而不是裸数字，参数化表才读得懂哪一行在测什么。
ERROR_BAND = "error"
WARNING_BAND = "warning"

#: 两档各自的中文文案，逐字 —— 见模块开头。
MESSAGE_BY_BAND = {
    ERROR_BAND: "图片尺寸过大，超过服务器处理上限",
    WARNING_BAND: "图片像素总量过大，超过服务器处理上限",
}


# ----------------------------------------------------------------------
# 夹具
# ----------------------------------------------------------------------

def _band_sides() -> tuple[int, int]:
    """从**配置**算出两档各自需要多大边长，返回 ``(warning 档, error 档)``。

    刻意不硬编码 8945 / 12650：上限是 ``FILETOOLS_MAX_IMAGE_PIXELS``
    可配的，写死的话换个上限整个文件就测了个寂寞（见
    ``test_the_pixel_limit_comes_from_settings``）。
    """
    limit = settings.MAX_IMAGE_PIXELS
    # isqrt 向下取整，+1 之后平方一定**大于**目标，正好落进要测的那一档
    return math.isqrt(limit) + 1, math.isqrt(2 * limit) + 1


def _png_declaring(width: int, height: int) -> bytes:
    """一张**真实**的 PNG，但 IHDR 里声明成 ``width × height``。

    做法见模块开头的「为什么不真造一张巨图」。改完宽高必须重算 IHDR 的 CRC
    （覆盖 ``IHDR`` 四个字节 + 13 字节数据，即文件偏移 12..29，CRC 存在 29..33），
    否则 Pillow 判 CRC 失败、报「文件损坏」，那就测错了东西。
    """
    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), (10, 20, 30)).save(buffer, format="PNG")
    data = bytearray(buffer.getvalue())

    # 下面的偏移都是按 PNG 规范算死的。先确认这张图确实是 Pillow 刚写出来的
    # 标准 PNG，免得哪天 Pillow 改了写法、这里悄悄改错字段却没人发现。
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "Pillow 写出来的不是 PNG？"
    assert data[12:16] == b"IHDR", "第一个 chunk 不是 IHDR？"

    data[16:20] = width.to_bytes(4, "big")
    data[20:24] = height.to_bytes(4, "big")
    data[29:33] = zlib.crc32(bytes(data[12:29])).to_bytes(4, "big")
    return bytes(data)


def _bomb(band: str) -> bytes:
    """造一张落在指定档位里的炸弹图。"""
    warning_side, error_side = _band_sides()
    side = error_side if band == ERROR_BAND else warning_side
    return _png_declaring(side, side)


def _write(tmp_path: Path, name: str, data: bytes) -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


# ----------------------------------------------------------------------
# 夹具本身
# ----------------------------------------------------------------------

def test_bomb_fixture_is_tiny_but_declares_a_huge_canvas() -> None:
    """夹具的性质：字节数极小，声明的像素数极大。

    这条守的是**测试自己**。它保证下面几条测的是「Pillow 按声明尺寸拦截」，
    而不是「测试机真的分配了几十 GB 内存」—— 也就是 §十二 那条规矩的
    可执行版本。
    """
    for band in (WARNING_BAND, ERROR_BAND):
        data = _bomb(band)
        assert len(data) < _BOMB_FIXTURE_MAX_BYTES, f"{band} 档的夹具太大了"

    # 文件头仍然是一张**真**的 PNG，所以校验层会把它当图片收下、
    # 一路走到炸弹检查，而不是在嗅探那一步就被当成别的类型挡掉。
    assert _bomb(ERROR_BAND)[:8] == b"\x89PNG\r\n\x1a\n"


def test_pillow_raises_in_one_band_and_only_warns_in_the_other(tmp_path: Path) -> None:
    """把「为什么需要那条显式像素检查」钉成可执行的事实。

    Pillow 只在上档抛异常；下档发一条 ``DecompressionBombWarning`` 就继续
    往下走了。这正是 ``verify_image`` 里那三行
    ``if width * height > settings.MAX_IMAGE_PIXELS`` 的存在理由 ——
    没有它，下档的炸弹会被静默放行。

    这条测的是 Pillow 本身的行为，不是我们的代码。写在这里是因为它是
    我们那三行的**前提**：哪天 Pillow 改成两档都抛，这条会红，
    提醒我们那三行已经不是唯一的防线了（那时应该重新审视，而不是
    直接删掉这条测试）。
    """
    Image.MAX_IMAGE_PIXELS = settings.MAX_IMAGE_PIXELS

    warning_path = _write(tmp_path, "warning.png", _bomb(WARNING_BAND))
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with Image.open(warning_path) as image:
            assert image.size == (image.width, image.height)
    assert any(
        issubclass(record.category, Image.DecompressionBombWarning)
        for record in caught
    ), "Pillow 在下档没有发出 DecompressionBombWarning —— 前提变了"

    error_path = _write(tmp_path, "error.png", _bomb(ERROR_BAND))
    with pytest.raises(Image.DecompressionBombError):
        Image.open(error_path)


# ----------------------------------------------------------------------
# 第一、二层：verify_image（上传校验走的就是它）
# ----------------------------------------------------------------------

@pytest.mark.parametrize("band", [ERROR_BAND, WARNING_BAND])
def test_verify_image_rejects_both_bands(tmp_path: Path, band: str) -> None:
    """两档都要翻成**业务错误**，各自的文案逐字对上。

    上档靠接住 Pillow 的异常，下档靠那条显式比较 —— 但对外必须是同一种
    东西：一个 ``ValidationError``（400 / ``INVALID_REQUEST``），
    用户看到一句中文，而不是 Python 异常名。
    """
    path = _write(tmp_path, "bomb.png", _bomb(band))

    with pytest.raises(ValidationError) as caught:
        validation.verify_image(path)

    assert caught.value.message == MESSAGE_BY_BAND[band]
    assert caught.value.code == ErrorCode.INVALID_REQUEST
    assert caught.value.status_code == 400
    # 炸弹不是「文件损坏」—— 那条路会引导用户重新导出一遍再失败一次
    assert type(caught.value) is ValidationError


def test_validate_image_upload_rejects_the_bomb(tmp_path: Path) -> None:
    """完整上传校验那条路（扩展名 → 魔数 → 解码）也要拦住炸弹。

    ``verify_image`` 只是其中一层；这一条走完 ``validate_image_upload``
    的前两层，确认炸弹不是「碰巧在别处被挡下」才没进来。
    """
    path = _write(tmp_path, "bomb.png", _bomb(ERROR_BAND))

    with pytest.raises(ValidationError) as caught:
        validation.validate_image_upload(path, "bomb.png")

    assert caught.value.message == MESSAGE_BY_BAND[ERROR_BAND]


# ----------------------------------------------------------------------
# worker 那条路：load_image
# ----------------------------------------------------------------------

def test_load_image_rejects_the_bomb(tmp_path: Path) -> None:
    """真正解码的那一层（``compressors/loader.py``）同样翻成业务错误。

    统一转换中心的 worker 走的是这条；它和上传校验是**两处**独立的
    ``except``，所以要分别测 —— 只测一处的话，另一处的 except 被删掉
    也不会有测试变红，用户会收到一个 500 加一段 traceback。
    """
    path = _write(tmp_path, "bomb.png", _bomb(ERROR_BAND))

    with pytest.raises(ValidationError) as caught:
        load_image(path)

    assert caught.value.message == MESSAGE_BY_BAND[ERROR_BAND]
    assert caught.value.code == ErrorCode.INVALID_REQUEST


# ----------------------------------------------------------------------
# 第三层：超长边是安全缩减，不是拒绝
# ----------------------------------------------------------------------

def test_oversized_edge_is_shrunk_not_rejected(tmp_path: Path) -> None:
    """长边超过 ``MAX_IMAGE_EDGE`` 的图照样能转，只是被等比缩小。

    这是 Phase 10A 的取舍：拒绝一张 12001 像素宽的正常照片对用户毫无道理，
    缩到 12000 再处理，结果仍然是他要的那张图。这里同时钉住两件事 ——
    校验层**放行**（像素数远没到上限），解码层**缩小**。

    用真实的图而不是改过文件头的夹具：这一层必须真的能解码，
    否则测的就不是缩减，而是「读不出来」。
    """
    edge = settings.MAX_IMAGE_EDGE
    width, height = edge + 1, 100
    # 12001 × 100 = 120 万像素，纯色 PNG 只有几 KB —— 不是「巨大的真实图片」
    image = Image.new("RGB", (width, height), (200, 30, 40))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    path = _write(tmp_path, "long.png", buffer.getvalue())

    # 校验层只管像素总量与损坏，不因为长边超限就拒绝
    assert validation.validate_image_upload(path, "long.png") == (width, height, "png")

    loaded, fmt = load_image(path)
    assert fmt == "png"
    assert max(loaded.size) == edge
    # 等比：长边压到上限，短边按同一比例走（100 × 12000/12001 四舍五入仍是 100）
    assert loaded.size == (edge, height)


# ----------------------------------------------------------------------
# 上限来自配置，不是硬编码
# ----------------------------------------------------------------------

def test_the_pixel_limit_comes_from_settings(tmp_path: Path, monkeypatch) -> None:
    """上限必须真的从 ``settings`` 读（§四十五：schema 是体验，防线在上限里）。

    把上限临时压到 10 000 像素：一张 200×200（40 000 像素）的图在默认
    8000 万的上限下毫无问题，压过之后必须被拦。谁要是哪天把
    ``80_000_000`` 写死进比较里，这条会红。
    """
    path = _write(tmp_path, "over.png", _png_declaring(200, 200))

    # 对照：默认上限下这张图完全正常 —— 待会儿拦下它的原因是上限，
    # 不是这张图本身有问题。这句必须放在压上限**之前**。
    assert validation.verify_image(path) == (200, 200, "png")

    monkeypatch.setattr(settings, "MAX_IMAGE_PIXELS", 10_000)
    # ``Image.MAX_IMAGE_PIXELS`` 是 Pillow 的模块级全局量。``verify_image``
    # 每次调用都会拿 settings 重设它，所以生产路径上不会漂；这里一起改
    # 只是为了让用例内部自洽。monkeypatch 会把两者都还原。
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 10_000)

    with pytest.raises(ValidationError) as caught:
        validation.verify_image(path)

    assert caught.value.message == MESSAGE_BY_BAND[ERROR_BAND]


# ----------------------------------------------------------------------
# 真实接口（§十四：不要只测函数）
# ----------------------------------------------------------------------

@pytest.mark.parametrize("band", [ERROR_BAND, WARNING_BAND])
def test_unified_api_reports_a_business_error(
    client: TestClient, band: str
) -> None:
    """一张炸弹图走完整条统一转换接口，用户看到的是中文业务错误。

    §十四 点名要验的五件事一起断言：请求收得下（202）、批次失败、
    这一项的错误码是业务码、文案逐字是那句中文、而且整份快照里没有
    内部细节。最后一项复用转换中心自己那份「不许泄露」清单
    （``test_conversion_api.FORBIDDEN_FRAGMENTS``）—— 同一件事只该有
    一处定义，这里不另抄一份。

    目标格式随便给一个合法的：炸弹在 ``detect_source`` 那一步就被挡下，
    根本走不到「这个源能不能转成那个目标」的查表。
    """
    response = submit_conversion(
        client,
        files=image_files(("bomb.png", _bomb(band))),
        target_type="jpg",
    )
    assert response.status_code == 202, response.text

    snapshot = wait_conversion(client, response.json()["batch_id"])
    assert snapshot["status"] == "failed"

    task = conversion_task(snapshot, 0)
    assert task["status"] == "failed"
    assert task["error_code"] == ErrorCode.INVALID_REQUEST
    assert task["error_message"] == MESSAGE_BY_BAND[band]
    # 没有产物，也没有「重试一下就会好」的假希望（§十六）
    assert task["result"] is None
    assert task["can_retry"] is False

    assert_clean(snapshot)


def test_warning_band_never_reaches_the_decoder(client: TestClient) -> None:
    """下档炸弹在真实接口上报的是「像素总量过大」，不是「文件损坏」。

    这条测的是**顺序**，不是某一句话：提交时的 ``detect_source`` 先跑
    ``validate_image_upload``，worker 里的 ``load_image`` 根本轮不到。
    顺序反过来的话，Pillow 会在 ``load()`` 时先以「图片文件被截断」
    失败 —— 用户会以为自己传了一份坏文件，去重新导出一遍然后再失败一次。

    换句话说：下档那 77 字节的夹具**确实**是一份截断文件，能给出正确的
    那句提示，全靠校验跑在解码前面。
    """
    response = submit_conversion(
        client,
        files=image_files(("bomb.png", _bomb(WARNING_BAND))),
        target_type="jpg",
    )
    assert response.status_code == 202, response.text

    snapshot = wait_conversion(client, response.json()["batch_id"])
    task = conversion_task(snapshot, 0)
    assert task["error_message"] == MESSAGE_BY_BAND[WARNING_BAND]
    assert "损坏" not in task["error_message"]


def test_legacy_api_does_not_bypass_the_bomb_check(client: TestClient) -> None:
    """老的 ``/api/image/convert`` 被同一道防线拦住，并给出真正的 HTTP 状态码。

    统一接口是异步的（202 + 轮询），所以它的「状态码」是任务终态；
    旧的图片接口在整批都失败时会把错误同步抛出，于是这里能断言一个
    真的 **400**，以及 ``{"error": {"code", "message"}}`` 这个既有形状。

    Phase 10C 不许删旧接口（§六十九），但旧接口**必须**和统一中心用同一套
    校验 —— 否则「哪条路能塞炸弹」就成了一个只有攻击者知道的分叉。
    这条同时是 §六十九 里「旧接口不能绕过新校验」那项审计的证据。
    """
    outcome = run_task(
        client,
        LEGACY_CONVERT,
        files=image_files(("bomb.png", _bomb(ERROR_BAND))),
        data={"target_format": "jpg"},
    )

    assert outcome.status_code == 400
    body = outcome.json()
    assert body["error"]["code"] == ErrorCode.INVALID_REQUEST
    assert body["error"]["message"] == MESSAGE_BY_BAND[ERROR_BAND]
    assert outcome.snapshot is not None
    assert_clean(outcome.snapshot)
