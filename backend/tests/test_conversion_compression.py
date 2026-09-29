"""统一压缩引擎（第十阶段 A §二十七–§三十二）。

这一组验的是**压缩本身说得对不对**，不是「HTTP 200 就算过」。三条主线：

1. **词表只有一份**（§二十七 / §二十九 / §五十二）：界面上那七档质量和
   五档目标大小全部由 ``config`` 里唯一一张表经 ``OptionSpec.presets``
   出网。所以这里既查「出网的是不是恰好那七个数、那五个数」，也查
   「有没有第二处自己写了一份」。少了这条，界面上就会冒出服务端不认的
   档位 —— 用户点了，得到一句「参数无效」。

2. **无损格式没有质量旋钮**（§三十）：PNG / BMP / GIF / TIFF / ICO 的
   schema 里**不该出现** ``quality``。摆一个点了没反应的滑杆是假话。
   但它们**都该有** ``target_bytes`` —— 目标大小是 PNG 唯一的压缩杠杆。

3. **报告必须是真数字**（§三十一 / §三十二）：``original_size`` /
   ``output_size`` / ``target_size`` / ``quality_used`` / ``target_reached``
   逐个与磁盘上、与下载回来的字节对账。最要紧的一条是
   ``target_reached`` **不许伪造**：搜到极限仍超标要如实报 ``False``，
   用户没提目标要报 ``None``（而不是假装 ``True`` 说「已达成」）。

## 为什么样张用纯噪声

``conftest.build_image_bytes`` 是渐变 + 椭圆 + 少量噪点，压得动。
「目标达不到」这种情况必须用**压不动**的图才能稳定复现：纯随机 RGB
在质量下限（25）加最大降采样（8 轮 × 0.85）之后仍然超过 5 KB。
换成平滑样张的话，5120 字节的目标随手就能达成，那条断言就永远走不到
``target_reached is False`` 的分支 —— 等于没测。
"""

from __future__ import annotations

import io
import random

import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageChops

from compressors import encoder, pipeline
from config import settings
from conversion import options as opts
from tests.conftest import image_files, run_conversion, submit_conversion

#: 五种无损 / 索引色目标：不许出现质量旋钮（§三十 字面）。
LOSSLESS_TARGETS = ("png", "bmp", "tiff", "gif", "ico")

#: 两种有损目标：质量旋钮只在这里。
LOSSY_TARGETS = ("jpg", "webp")

#: 能作为**上传源**的无损格式（ICO 只出不进，见 ``test_image_formats``）。
LOSSLESS_SOURCES = ("png", "bmp", "tiff", "gif")

#: 查某个目标的 schema 时该拿谁当源。
#:
#: 同格式之间**没有能力**（``image.png-to-png`` 不存在 —— 「转成它自己」
#: 不是一次转换），所以查 PNG 的 schema 得用 JPG 当源；ICO 只接受 PNG
#: （决策 F）。这张表把这两条事实写成数据，省得每条测试各记一遍。
SOURCE_FOR_TARGET = {
    "jpg": "png",
    "webp": "png",
    "png": "jpg",
    "bmp": "png",
    "tiff": "png",
    "gif": "png",
    "ico": "png",
}


def capability_id(target: str) -> str:
    return f"image.{SOURCE_FOR_TARGET[target]}-to-{target}"


def noisy_png(width: int = 1600, height: int = 1200, *, seed: int = 7) -> bytes:
    """纯随机 RGB 的 PNG。

    用 ``randbytes`` 而不是逐个 ``getrandbits``：5.7 MB 的样张差着一个
    数量级的时间，而这条路径每个测试模块都要走一次。
    """
    rng = random.Random(seed)
    image = Image.frombytes("RGB", (width, height), rng.randbytes(width * height * 3))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture(scope="module")
def uncompressible() -> bytes:
    """1600×1200 的纯噪声 PNG —— 「目标达不到」全靠它（见模块 docstring）。"""
    return noisy_png()


def item_of(snapshot: dict, index: int = 0) -> dict:
    task = next(t for t in snapshot["tasks"] if t["index"] == index)
    assert task["status"] == "completed", task
    return task["result"]


def download(client: TestClient, snapshot: dict, index: int = 0) -> bytes:
    result = item_of(snapshot, index)
    response = client.get(result["download_url"])
    assert response.status_code == 200, response.text
    return response.content


def convert(
    client: TestClient, source: bytes, *, name: str, target: str, **extra
):
    return run_conversion(
        client, files=image_files((name, source)), target_type=target, **extra
    )


def schema_of(client: TestClient, capability_id: str) -> dict[str, dict]:
    """某个能力出网的 ``options_schema``，按键索引。"""
    payload = client.get("/api/conversion/capabilities").json()
    entry = next(item for item in payload["conversions"] if item["id"] == capability_id)
    schema = entry["options_schema"]
    return {item["key"]: item for item in (schema["items"] if schema else [])}


# ----------------------------------------------------------------------
# 1. 质量档位（§二十七）
# ----------------------------------------------------------------------

def test_the_seven_quality_stops_are_published_in_order(client: TestClient) -> None:
    """七档质量按从高到低出网，标签与 ``config`` 那张表逐条相同。"""
    quality = schema_of(client, "image.png-to-jpg")["quality"]

    assert [preset["value"] for preset in quality["presets"]] == [
        str(stop) for stop in settings.QUALITY_PRESET_STOPS
    ]
    assert [preset["label"] for preset in quality["presets"]] == [
        settings.QUALITY_PRESET_LABELS[stop] for stop in settings.QUALITY_PRESET_STOPS
    ]
    assert [preset["value"] for preset in quality["presets"]] == [
        "100", "90", "85", "80", "70", "60", "50",
    ]


def test_the_default_quality_is_the_recommended_stop(client: TestClient) -> None:
    """§二十七 把默认质量定死在 85，而 85 必须是七档里的「推荐」那一档。"""
    quality = schema_of(client, "image.png-to-jpg")["quality"]

    assert quality["default"] == settings.DEFAULT_QUALITY_VALUE == 85
    assert ("85", "推荐") in [
        (preset["value"], preset["label"]) for preset in quality["presets"]
    ]


def test_both_lossy_targets_publish_the_same_quality_spec(client: TestClient) -> None:
    """JPG 与 WEBP 是同一个 spec 对象，不是两份各写一遍的常量表。

    两份的后果是可预见的：改了一处档位，另一个目标还是老样子。
    """
    assert schema_of(client, "image.png-to-jpg")["quality"] == schema_of(
        client, "image.png-to-webp"
    )["quality"]


def test_the_legacy_three_band_preset_is_a_different_vocabulary(client: TestClient) -> None:
    """``quality_preset``（高/均衡/强）与七档 ``quality`` 并存，不是同一件事。

    前者是「按目标大小搜索时该在哪个区间里找」，后者是用户直接指定的
    压缩程度。这条断言把两者的边界钉住：它们**不共用**取值域，
    所以谁都不能拿对方的值来提交。
    """
    assert set(settings.QUALITY_PRESETS) == {"high", "balanced", "strong"}
    assert not set(settings.QUALITY_PRESETS) & {
        str(stop) for stop in settings.QUALITY_PRESET_STOPS
    }


@pytest.mark.parametrize("stop", settings.QUALITY_PRESET_STOPS)
def test_every_published_quality_stop_is_accepted(
    client: TestClient, stop: int
) -> None:
    """七档里每一档都真的能提交、能跑完、且报告的正是那一档。"""
    source = noisy_png(640, 480, seed=stop)

    snapshot = convert(
        client, source, name="a.png", target="jpg", options=f'{{"quality": {stop}}}'
    )

    assert item_of(snapshot)["quality_used"] == stop


def test_higher_quality_means_a_bigger_file(client: TestClient) -> None:
    """质量档位要真的传下去：七档出来的体积必须随质量单调不减。

    「单调不减」而不是「严格递增」：85 / 80 在 4:2:0 区间里可能压出
    同样的大小，那是编码器的自由；反过来（高质量反而更小）就说明
    这个旋钮根本没接上。
    """
    source = noisy_png(640, 480, seed=11)
    sizes: list[tuple[int, int]] = []
    for stop in reversed(settings.QUALITY_PRESET_STOPS):  # 50 → 100
        snapshot = convert(
            client, source, name="a.png", target="jpg", options=f'{{"quality": {stop}}}'
        )
        result = item_of(snapshot)
        assert result["quality_used"] == stop
        sizes.append((stop, result["size"]))

    for (low_stop, low_size), (high_stop, high_size) in zip(sizes, sizes[1:]):
        assert low_size <= high_size, (
            f"质量 {low_stop}（{low_size} 字节）不该大于 "
            f"质量 {high_stop}（{high_size} 字节）"
        )


# ----------------------------------------------------------------------
# 2. 目标大小档位与边界（§二十九）
# ----------------------------------------------------------------------

def test_the_target_size_stops_are_published_as_raw_bytes(client: TestClient) -> None:
    """五档目标大小出网的是**原始字节数**，标签才是给人看的。

    值必须是字节：前端的输入框直接拿它当数值用，服务端解析器也按字节
    解释。若为了好看发 ``"500 KB"``，提交上来就得再翻译一次 ——
    多一层翻译就多一个写错的地方。
    """
    target = schema_of(client, "image.png-to-jpg")["target_bytes"]

    assert [preset["value"] for preset in target["presets"]] == [
        str(size) for size in settings.TARGET_SIZE_PRESET_BYTES
    ]
    assert [preset["value"] for preset in target["presets"]] == [
        "512000", "1048576", "2097152", "5242880", "10485760",
    ]
    assert [preset["label"] for preset in target["presets"]] == [
        "≤ 500 KB", "≤ 1 MB", "≤ 2 MB", "≤ 5 MB", "≤ 10 MB",
    ]
    # 第六档「自定义」不是预设里的一个值，而是「自己填」这件事本身 ——
    # 它的边界由 min/max 承担。
    assert target["min"] == settings.MIN_TARGET_BYTES == 5120
    assert target["max"] == settings.MAX_TARGET_BYTES == 200 * 1024 * 1024
    assert target["default"] is None


@pytest.mark.parametrize("size", settings.TARGET_SIZE_PRESET_BYTES)
def test_every_published_target_stop_is_accepted(client: TestClient, size: int) -> None:
    source = noisy_png(320, 240, seed=3)

    snapshot = convert(
        client, source, name="a.png", target="jpg", target_bytes=str(size)
    )

    assert item_of(snapshot)["target_size"] == size


def test_the_target_size_is_echoed_exactly(client: TestClient) -> None:
    """用户填的不是预设值时，报告的必须是**他填的那个数**，不许四舍五入。"""
    size = 300 * 1024  # 不在五档里
    snapshot = convert(
        client,
        noisy_png(640, 480, seed=5),
        name="a.png",
        target="jpg",
        target_bytes=str(size),
    )

    assert item_of(snapshot)["target_size"] == size


def test_zero_means_no_limit(client: TestClient) -> None:
    """``0`` 与留空等价 —— 这是既有三个页面用「不限制」这个选项值定下的约定。

    提交上来是 ``0``，服务端要理解成「没提目标」，而不是「目标 0 字节」
    （那是个谁也达不到的目标，会被如实报成 ``False``，白白吓用户一跳）。
    """
    snapshot = convert(
        client,
        noisy_png(320, 240, seed=13),
        name="a.png",
        target="jpg",
        target_bytes="0",
    )

    result = item_of(snapshot)
    assert result["target_size"] is None
    assert result["target_reached"] is None


@pytest.mark.parametrize(
    ("raw", "message"),
    (
        ("100", "目标大小不能小于 5 KB"),
        ("999999999999", "目标大小超出允许范围"),
        ("abc", "目标大小必须是数字"),
        ("-1", "目标大小不能小于 5 KB"),
    ),
)
def test_out_of_range_targets_are_rejected(
    client: TestClient, raw: str, message: str
) -> None:
    """整批级别的问题在提交时就 4xx，且是干净的中文，不带堆栈。"""
    response = submit_conversion(
        client,
        files=image_files(("a.png", noisy_png(64, 64, seed=17))),
        target_type="jpg",
        target_bytes=raw,
    )

    assert response.status_code == 400, response.text
    error = response.json()["error"]
    assert error["code"] == "INVALID_REQUEST"
    assert error["message"] == message


def test_the_json_option_and_the_form_field_reach_the_same_parser(
    client: TestClient,
) -> None:
    """同一个数走两条路（``options`` JSON / 扁平表单字段）必须得到同一个结果。

    §四十五 要求两条路都穿过**同一个**既有解析器；这里用「同一个字节数
    得到同一个 ``target_size``」来验它，而不是去数函数调用了几次。
    """
    source = noisy_png(320, 240, seed=19)
    size = 400 * 1024

    flat = convert(client, source, name="a.png", target="jpg", target_bytes=str(size))
    nested = convert(
        client, source, name="a.png", target="jpg", options=f'{{"target_bytes": {size}}}'
    )

    assert item_of(flat)["target_size"] == item_of(nested)["target_size"] == size


# ----------------------------------------------------------------------
# 3. 无损格式没有质量旋钮（§三十）
# ----------------------------------------------------------------------

@pytest.mark.parametrize("target", LOSSLESS_TARGETS)
def test_lossless_targets_have_no_quality_control(
    client: TestClient, target: str
) -> None:
    """PNG / BMP / GIF / TIFF / ICO 的 schema 里不许出现 ``quality``。"""
    source = SOURCE_FOR_TARGET[target]
    keys = schema_of(client, capability_id(target))

    assert "quality" not in keys, f"{target} 不该有质量旋钮"
    assert "quality" not in opts.applicable_keys(
        converter_key=opts.IMAGE_CONVERT, source_type=source, target_type=target
    )


@pytest.mark.parametrize("target", LOSSY_TARGETS)
def test_lossy_targets_keep_the_quality_control(client: TestClient, target: str) -> None:
    assert "quality" in schema_of(client, capability_id(target))


@pytest.mark.parametrize("target", (*LOSSY_TARGETS, *LOSSLESS_TARGETS))
def test_every_target_can_limit_the_size(client: TestClient, target: str) -> None:
    """目标大小**每个目标都有** —— 无损格式的质量杠杆就是它。"""
    assert "target_bytes" in schema_of(client, capability_id(target))


@pytest.mark.parametrize("source_format", LOSSLESS_SOURCES)
def test_a_lossless_source_still_reports_a_real_quality(
    client: TestClient, source_format: str
) -> None:
    """源是无损格式、目标是 JPG 时，``quality_used`` 仍然是真用过的那个数。"""
    buffer = io.BytesIO()
    Image.frombytes(
        "RGB", (320, 240), random.Random(23).randbytes(320 * 240 * 3)
    ).save(buffer, format=source_format.upper())

    snapshot = convert(
        client,
        buffer.getvalue(),
        name=f"a.{'tif' if source_format == 'tiff' else source_format}",
        target="jpg",
        options='{"quality": 70}',
    )

    assert item_of(snapshot)["quality_used"] == 70


# ----------------------------------------------------------------------
# 4. 报告必须是真数字（§三十一 / §三十二）
# ----------------------------------------------------------------------

def test_no_target_reports_none_not_a_fake_true(client: TestClient) -> None:
    """用户没提目标大小的时候，``target_reached`` 必须是 ``None``。

    报 ``True`` 的害处不是「多说了一句话」：结果卡会显示「已达成目标大小」，
    而用户根本没设过目标，于是他以为服务器替他把文件压到了某个标准。
    """
    source = noisy_png(640, 480, seed=29)
    snapshot = convert(client, source, name="a.png", target="jpg")

    result = item_of(snapshot)
    assert result["target_size"] is None
    assert result["target_reached"] is None
    assert result["quality_used"] == settings.DEFAULT_QUALITY_VALUE
    assert result["size"] == result["output_size"] == len(download(client, snapshot))


def test_original_size_is_the_uploaded_file(client: TestClient) -> None:
    """``original_size`` 是**用户上传的那份**，不是中间产物，也不是输出。"""
    source = noisy_png(640, 480, seed=31)
    snapshot = convert(client, source, name="a.png", target="jpg")

    result = item_of(snapshot)
    assert result["original_size"] == len(source)
    assert result["output_size"] != result["original_size"]


def test_a_reachable_target_is_reported_as_reached(client: TestClient) -> None:
    """达得到就报 ``True``，而且**文件真的在目标以内** —— 不是嘴上说的。"""
    source = noisy_png(1600, 1200, seed=37)
    target = 500 * 1024
    snapshot = convert(
        client, source, name="a.png", target="jpg", target_bytes=str(target)
    )

    result = item_of(snapshot)
    assert result["target_reached"] is True
    assert result["size"] <= target
    assert result["size"] == len(download(client, snapshot))
    assert pipeline.TARGET_UNREACHABLE_NOTE not in result["notes"]


def test_an_unreachable_target_is_reported_as_unreached(
    client: TestClient, uncompressible: bytes
) -> None:
    """§三十二 的核心：达不到就报 ``False``，并且说清楚。

    这条最容易写成假话 —— 搜索确实尽力了、文件也确实变小了，
    顺手报个 ``True`` 很自然。但那是一句用户会据以做决定的假话：
    他以为文件已经能发邮件了，实际还差一截。
    """
    target = settings.MIN_TARGET_BYTES  # 5120：允许的最小目标
    snapshot = convert(
        client, uncompressible, name="n.png", target="jpg", target_bytes=str(target)
    )

    result = item_of(snapshot)
    assert result["target_reached"] is False
    assert result["size"] > target
    assert result["size"] == len(download(client, snapshot))
    assert pipeline.TARGET_UNREACHABLE_NOTE in result["notes"]
    # 达不到目标是「尽力了但没做到」，不是「转换失败」：产物有效、可下载。
    assert result["width"] > 0 and result["height"] > 0


def test_the_unreachable_case_still_reports_honest_numbers(
    client: TestClient, uncompressible: bytes
) -> None:
    """达不到目标的那个结果里，每一个数都要是真的。"""
    target = settings.MIN_TARGET_BYTES
    snapshot = convert(
        client, uncompressible, name="n.png", target="jpg", target_bytes=str(target)
    )

    result = item_of(snapshot)
    assert result["original_size"] == len(uncompressible)
    assert result["target_size"] == target
    assert result["size"] == result["output_size"]
    # 搜到极限仍未达标 → 停在质量下限，绝不会低于它
    assert result["quality_used"] == pipeline.MIN_SEARCH_QUALITY
    # 也真的降过尺寸（allow_downscale 在没指定宽高时成立）
    assert result["width"] < 1600 and result["height"] < 1200


def test_a_roomy_target_never_pushes_the_quality_up(client: TestClient) -> None:
    """目标给得再宽，质量也停在默认那一档 —— 搜索只负责**不超标**。

    反过来做（把预算花光、质量往 100 顶）看起来很「贴心」，实际是在
    用户没要求的时候替他放大文件：他要的是「不超过 10 MB」，
    不是「尽量占满 10 MB」。
    """
    source = noisy_png(320, 240, seed=41)  # 远小于 10 MB
    snapshot = convert(
        client, source, name="a.png", target="jpg", target_bytes=str(10 * 1024**2)
    )

    result = item_of(snapshot)
    assert result["target_reached"] is True
    assert result["quality_used"] == settings.DEFAULT_QUALITY_VALUE
    assert result["size"] <= 10 * 1024**2


# ----------------------------------------------------------------------
# 5. 搜索是有界的（§二十七 / §二十九）
# ----------------------------------------------------------------------

def test_an_explicit_quality_is_the_search_ceiling(client: TestClient) -> None:
    """用户指定质量后，搜索只允许**往下**走去找目标大小，不会超过它。"""
    source = noisy_png(1600, 1200, seed=43)

    capped = item_of(
        convert(
            client,
            source,
            name="a.png",
            target="jpg",
            target_bytes=str(5 * 1024**2),
            options='{"quality": 60}',
        )
    )
    assert capped["quality_used"] == 60
    # 目标宽裕时就是用户要的那一档，不会被搜索擅自抬高
    assert capped["size"] <= 5 * 1024**2

    squeezed = item_of(
        convert(
            client,
            source,
            name="a.png",
            target="jpg",
            target_bytes=str(settings.MIN_TARGET_BYTES),
            options='{"quality": 60}',
        )
    )
    assert squeezed["quality_used"] < 60
    assert squeezed["quality_used"] == pipeline.MIN_SEARCH_QUALITY


def test_the_search_never_goes_below_the_quality_floor(
    client: TestClient, uncompressible: bytes
) -> None:
    """再难达标也不会把质量压到下限以下 —— 宁可缩尺寸也不把画质压烂。"""
    snapshot = convert(
        client,
        uncompressible,
        name="n.png",
        target="jpg",
        target_bytes=str(settings.MIN_TARGET_BYTES),
    )

    assert item_of(snapshot)["quality_used"] >= pipeline.MIN_SEARCH_QUALITY


def test_the_search_is_bounded_by_the_configured_rounds() -> None:
    """轮次上限与降采样系数取自 ``config``，不是散在实现里的魔数。"""
    assert settings.MAX_DOWNSCALE_ROUNDS == 8
    assert 0 < settings.DOWNSCALE_FACTOR < 1
    assert encoder.MAX_SEARCH_STEPS >= 1


# ----------------------------------------------------------------------
# 6. 高清 JPEG 的编码回归（本阶段发现的既有缺陷）
# ----------------------------------------------------------------------

def _max_deviation(source: Image.Image, data: bytes) -> int:
    """编码再解码之后，与源图的最大像素偏差（0–255）。"""
    with Image.open(io.BytesIO(data)) as back:
        diff = ImageChops.difference(source, back.convert("RGB"))
    return max(channel[1] for channel in diff.getextrema())


def test_the_quality_above_ninety_does_not_break_on_a_hard_picture() -> None:
    """质量 ≥ 90 会让 ``_subsampling_for`` 选 4:4:4，而本机这一版
    Pillow / libjpeg 在 4:4:4 上带 ``optimize`` 或 ``progressive`` 时，
    遇到压不动的大图会在第二次扫描时报
    ``OSError: broken data stream when writing image file``。

    这是**第一阶段就存在**的缺陷，第十阶段 A 把「最高 / 100」摆到界面上
    之后才会被真实用户大量踩到（在此之前 90 以上只能靠手填数字）。
    修法是先按最优参数编、挂了退回基线，所以这条测试要钉住两件事：
    编码**不再抛异常**，以及退回之后**色度仍然是 4:4:4**。

    色度那一半用「比 4:2:0 好得多」来判，不写死一个绝对阈值：
    量化误差随质量变化（实测噪声样张 90 档 52、95 档 25、100 档 4），
    写死一个数只会在换样张时变成假失败；而 4:2:0 的偏差在同一张图上
    三档都是 255，差距是压倒性的。
    """
    rng = random.Random(53)
    image = Image.frombytes("RGB", (1600, 1200), rng.randbytes(1600 * 1200 * 3))

    for quality in (90, 95, 100):
        data = encoder.encode_image(image, "jpeg", quality)
        with Image.open(io.BytesIO(data)) as back:
            assert back.format == "JPEG"
            assert back.size == (1600, 1200)

        chroma_kept = io.BytesIO()
        image.save(
            chroma_kept,
            format="JPEG",
            quality=quality,
            subsampling=0,
            optimize=False,
            progressive=False,
        )
        chroma_dropped = io.BytesIO()
        image.save(
            chroma_dropped, format="JPEG", quality=quality, subsampling=2
        )

        deviation = _max_deviation(image, data)
        assert deviation <= _max_deviation(image, chroma_kept.getvalue()), quality
        assert deviation < _max_deviation(image, chroma_dropped.getvalue()), quality


def test_a_broken_encoder_still_raises_for_the_other_subsamplings() -> None:
    """退回逻辑只对 4:4:4 生效：别的组合一旦报错就是真错，必须原样抛出。

    吞掉它们的后果是一个「成功」的空文件 —— 比失败糟糕得多。
    """
    calls: list[dict] = []

    class _Boom:
        mode = "RGB"
        width = height = 8

        def save(self, buffer, format=None, **params):  # noqa: A002
            calls.append(params)
            raise OSError("boom")

    with pytest.raises(OSError):
        encoder._encode_jpeg(_Boom(), 60)  # type: ignore[arg-type]

    assert len(calls) == 1, "4:2:0 不该被重试"
    assert calls[0]["subsampling"] != 0


def test_the_fallback_drops_only_the_second_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    """退回时丢掉的只有 ``optimize`` / ``progressive``，色度采样不动。

    丢了采样方式就等于「修好了报错、悄悄降了画质」，那不是修。
    """
    seen: list[dict] = []
    real = encoder._save_jpeg

    def spy(img, params):  # type: ignore[no-untyped-def]
        seen.append(dict(params))
        if len(seen) == 1:
            raise OSError("broken data stream when writing image file")
        return real(img, params)

    monkeypatch.setattr(encoder, "_save_jpeg", spy)

    rng = random.Random(59)
    image = Image.frombytes("RGB", (64, 64), rng.randbytes(64 * 64 * 3))
    data = encoder._encode_jpeg(image, 95)

    assert len(seen) == 2
    assert seen[0]["subsampling"] == seen[1]["subsampling"] == 0
    assert seen[0]["optimize"] is True and seen[0]["progressive"] is True
    assert seen[1]["optimize"] is False and seen[1]["progressive"] is False
    with Image.open(io.BytesIO(data)) as back:
        assert back.size == (64, 64)


def test_the_hard_picture_also_survives_the_whole_pipeline(client: TestClient) -> None:
    """同样的图走完整 HTTP 链路也只能成功 —— 这才是用户看到的那条路。"""
    source = noisy_png(1600, 1200, seed=61)

    snapshot = convert(
        client, source, name="n.png", target="jpg", options='{"quality": 100}'
    )

    result = item_of(snapshot)
    assert result["quality_used"] == 100
    with Image.open(io.BytesIO(download(client, snapshot))) as back:
        assert back.format == "JPEG"
        assert back.size == (1600, 1200)
