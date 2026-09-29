"""EXIF 方向归一化（第十阶段 A §三十七 / §三十八）。

## 这一节在防什么

手机竖着拍一张照片，像素其实是**横**的；让它看起来是竖的，靠的是 EXIF 里
一个叫 ``Orientation`` 的标记（tag ``0x0112``）。几乎所有的麻烦都从这个
「像素与显示不是一回事」里长出来：

* 只读像素、不看标记 —— 用户拿到一张躺倒的图；
* 读了标记、把像素摆正了，却**没摘掉标记** —— 查看器再转一次，
  图躺倒 180°；
* 把标记当普通元数据一起「保留」下去 —— 同上；
* 直接把原文件字节透传出去（近路），而这一格本该摆正 —— 用户什么也没得到。

所以下面的断言**一条都不看 ``.size`` 就算了**：四角颜色互不相同，
摆正方向、镜像、转 180°，任何一种错法都会让象限签名变化。

## 四张样张为什么是 1 / 3 / 6 / 8

这是 §三十八 点名的四档，也是 EXIF 里真正会用到的四档：

====  ==========================  ==========================
值    含义                        摆正后的画面
====  ==========================  ==========================
1     正常（有些相机显式写 1）    原样
3     上下颠倒                    转 180°
6     顺时针转 90° 才正            宽高互换
8     逆时针转 90° 才正            宽高互换
====  ==========================  ==========================

2 / 4 / 5 / 7 是四种镜像，现实中极少出现（规格也没点名），这里不造 ——
造了也是在测自己想象的语义。

## 「摆正」的定义写在这里

源样张的**存储像素**永远是：左上红、右上绿、左下蓝、右下白（40×20 横图）。
于是每一档摆正之后的期望画面可以独立算出来，而不是抄一份实现里的中间结果：

* **1** —— 原样，40×20
* **3** —— 转 180°，40×20，四角整体对调
* **6** —— 顺时针 90°，20×40，左上=蓝、右上=红、左下=白、右下=绿
* **8** —— 逆时针 90°，20×40，左上=绿、右上=白、左下=红、右下=蓝
"""

from __future__ import annotations

import io

import pytest
from PIL import Image

from compressors.encoder import normalize_format
from compressors.pipeline import PipelineOptions, run_pipeline
from compressors.loader import EXIF_ORIENTATION_TAG
from tests.conftest import image_files, run_conversion

# 与 test_image_geometry.py 同源的四角配色：两两不同，方向错了就看得出来。
TL = (255, 0, 0)        # 左上 红
TR = (0, 255, 0)        # 右上 绿
BL = (0, 0, 255)        # 左下 蓝
BR = (255, 255, 255)    # 右下 白

#: 样张的**存储**尺寸。横图 —— 所以 6 / 8 两档摆正之后必须是 20×40，
#: 这条比任何像素断言都先暴露「转没转」。
STORED_SIZE = (40, 20)

#: 每一档：摆正后的尺寸，以及四角的期望颜色（左上、右上、左下、右下）。
#:
#: 手算的依据是「把存储画面按这一档转过来」：
#:
#: * ``3`` 转 180° —— 四个角整体对调；
#: * ``6`` 顺时针 90° —— 存储的左下角转到左上，依此类推；
#: * ``8`` 逆时针 90° —— 存储的右上角转到左上。
EXPECTED: dict[int, tuple[tuple[int, int], tuple[tuple[int, int, int], ...]]] = {
    1: (STORED_SIZE, (TL, TR, BL, BR)),
    3: (STORED_SIZE, (BR, BL, TR, TL)),
    6: ((20, 40), (BL, TL, BR, TR)),
    8: ((20, 40), (TR, BR, TL, BL)),
}

#: 会走「重新编码」这条路的全部图片目标（线上词汇）。HEIC 在内 ——
#: 它由可选组件提供，下面按可用性 skip。
#:
#: 交给 ``run_pipeline`` 之前一律过一遍 ``normalize_format``：流水线说的是
#: 内部词汇（``jpeg`` / ``heif``），线上说的是 ``jpg`` / ``heic``，
#: 转换服务里也做了同一件事。这里照做，免得测试悄悄用了一组
#: 用户根本发不出来的参数。
_ALL_TARGETS = ("png", "jpg", "webp", "bmp", "gif", "tiff", "heic")


def _quadrant_image() -> Image.Image:
    """存储画面：左上红、右上绿、左下蓝、右下白。"""
    img = Image.new("RGB", STORED_SIZE, TL)
    half_w, half_h = STORED_SIZE[0] // 2, STORED_SIZE[1] // 2
    img.paste(TR, (half_w, 0, STORED_SIZE[0], half_h))
    img.paste(BL, (0, half_h, half_w, STORED_SIZE[1]))
    img.paste(BR, (half_w, half_h, STORED_SIZE[0], STORED_SIZE[1]))
    return img


def oriented_jpeg(orientation: int) -> bytes:
    """一份带指定 EXIF 方向的 JPEG 样张。"""
    img = _quadrant_image()
    exif = img.getexif()
    exif[EXIF_ORIENTATION_TAG] = orientation
    buffer = io.BytesIO()
    # 质量拉满、4:4:4 采样：下面比的是**具体颜色**，色度抽样会把
    # 象限边界糊成过渡色，让 ±8 的容差都不够用。
    img.save(buffer, format="JPEG", exif=exif, quality=100, subsampling=0)
    return buffer.getvalue()


def quadrants(img: Image.Image) -> tuple[tuple[int, ...], ...]:
    """取四个象限中心的像素 —— 图像的「方向签名」。"""
    w, h = img.size
    return (
        img.getpixel((w // 4, h // 4)),
        img.getpixel((w * 3 // 4, h // 4)),
        img.getpixel((w // 4, h * 3 // 4)),
        img.getpixel((w * 3 // 4, h * 3 // 4)),
    )


def close(got: tuple[int, ...], want: tuple[int, ...], tolerance: int = 12) -> bool:
    """有损格式（JPEG / WEBP / HEIC）不可能逐位还原，按容差比。

    容差给 12 而不是 2：象限中心是**纯色块的中心**，离边界有半个象限远，
    量化误差到不了这里；但不同版本的编码器在色度上会有几个灰阶的出入，
    写死 ±2 会变成一个随库版本飘的假失败。
    """
    return all(abs(a - b) <= tolerance for a, b in zip(got, want))


def _decoded(data: bytes) -> Image.Image:
    with Image.open(io.BytesIO(data)) as img:
        img.load()
        return img.convert("RGB")


def _orientation_of(data: bytes) -> int | None:
    """结果文件里**还留着**的方向标记（没有就是 ``None``）。"""
    with Image.open(io.BytesIO(data)) as img:
        return img.getexif().get(EXIF_ORIENTATION_TAG)


# ----------------------------------------------------------------------
# 1. 样张自己的前提：四档真的写进去了
# ----------------------------------------------------------------------

@pytest.mark.parametrize("orientation", sorted(EXPECTED))
def test_the_fixture_really_carries_the_tag(orientation: int) -> None:
    """先证明样张里真的写着这一档，否则下面整组测试可能都是空转。

    Pillow 会把方向上**去重**：写 1 与不写，读回来都是 1 或 ``None``。
    所以第 1 档只断言「读回来是 1 或 None」——这两种都表示「不需要转」，
    对被测代码是同一件事。
    """
    data = oriented_jpeg(orientation)
    with Image.open(io.BytesIO(data)) as img:
        stored = img.getexif().get(EXIF_ORIENTATION_TAG)
        assert img.size == STORED_SIZE, "存储像素必须是横的 40×20"
    if orientation == 1:
        assert stored in (None, 1), stored
    else:
        assert stored == orientation, f"样张没带上方向 {orientation}：{stored!r}"


# ----------------------------------------------------------------------
# 2. 流水线：摆正，并摘掉标记
# ----------------------------------------------------------------------

@pytest.mark.parametrize("orientation", sorted(EXPECTED))
@pytest.mark.parametrize("target", _ALL_TARGETS)
def test_every_orientation_is_normalised_for_every_target(
    tmp_path, orientation: int, target: str
) -> None:
    """四档方向 × 七个目标：画面摆正，且结果里不再留下方向标记。

    **两条断言缺一不可**：

    * 画面摆正 —— 用户打开结果看到的就是正的；
    * 标记摘掉 —— 否则查看器会再转一次，图变成躺倒 180°。这一条尤其
      重要，因为它在「只打开结果看一眼」的验证里**看不出来**（很多
      查看器不认 EXIF 方向，Pillow 的 ``convert("RGB")`` 也不认）。

    第三档断言 ``untouched`` 为假是**选项性**的：这里的源与目标格式不同
    （jpg → png/webp/...），近路本来就该关着。它守的是将来有人把近路的
    条件写宽。
    """
    if target == "heic":
        from compressors.heif import heif_support

        support = heif_support()
        if not (support.decode and support.encode):
            pytest.skip(f"没有 HEIC 编解码器：{support.reason}")

    source = tmp_path / f"o{orientation}.jpg"
    source.write_bytes(oriented_jpeg(orientation))

    result = run_pipeline(
        source,
        PipelineOptions(
            target_format=normalize_format(target), quality_value=95
        ),
    )

    want_size, want_quadrants = EXPECTED[orientation]
    img = _decoded(result.data)
    assert img.size == want_size, f"方向 {orientation} → {target}：尺寸 {img.size}"
    for got, want in zip(quadrants(img), want_quadrants):
        assert close(got, want), (
            f"方向 {orientation} → {target}：象限签名 {quadrants(img)} 期望 {want_quadrants}"
        )
    assert _orientation_of(result.data) in (None, 1), (
        f"方向 {orientation} → {target}：结果里还留着方向标记，查看器会再转一次"
    )
    assert result.untouched is False


@pytest.mark.parametrize("orientation", sorted(EXPECTED))
def test_keeping_metadata_does_not_put_the_orientation_tag_back(
    tmp_path, orientation: int
) -> None:
    """``metadata="keep"`` 保留**其余**拍摄信息，但方向标记不许回来。

    ``load_image`` 用 ``exif_transpose`` 摆正像素时会顺手把标记摘掉；
    之后 ``metadata="keep"`` 原样透传的那份 EXIF 必须是**摘过之后**的。
    如果谁改成「先把原始 EXIF 存起来、最后再写回去」，标记就会回来，
    用户拿到一张躺倒 180° 的图 —— 而这条路上 ``keep`` 看起来还很正常。
    """
    source = tmp_path / f"keep-{orientation}.jpg"
    data = oriented_jpeg(orientation)
    # 再塞一项**应当被保留**的拍摄信息，证明 keep 不是「什么都没做」
    img = Image.open(io.BytesIO(data))
    exif = img.getexif()
    exif[0x0110] = "OrientationCanary"
    buffer = io.BytesIO()
    img.save(buffer, format="JPEG", exif=exif, quality=100, subsampling=0)
    source.write_bytes(buffer.getvalue())

    result = run_pipeline(
        source,
        PipelineOptions(target_format="png", quality_value=95, metadata="keep"),
    )

    with Image.open(io.BytesIO(result.data)) as out:
        got = out.getexif()
    assert got.get(EXIF_ORIENTATION_TAG) in (None, 1), "方向标记被写回去了"
    assert got.get(0x0110) == "OrientationCanary", "keep 没把其余拍摄信息带过来"


# ----------------------------------------------------------------------
# 3. HTTP：从转换中心走一遍，用户真的拿到摆正的图
# ----------------------------------------------------------------------

@pytest.mark.parametrize("orientation", sorted(EXPECTED))
def test_the_conversion_api_returns_an_upright_image(client, orientation: int) -> None:
    """端到端一遍：上传带方向的 JPEG，转 PNG，下载回来验像素。

    走的是**真实接口**（``/api/conversion/tasks`` → 下载），而不是直接调
    流水线 —— §三十八 要的是「用户拿到的东西是对的」，中间任何一层
    （上传校验、任务参数、结果登记、下载）把方向弄丢都该在这里红。
    """
    payload = run_conversion(
        client,
        files=image_files(("手机照片.jpg", oriented_jpeg(orientation))),
        target_type="png",
    )
    url = payload["result"]["download_url"]
    response = client.get(url)
    assert response.status_code == 200, response.text

    want_size, want_quadrants = EXPECTED[orientation]
    # 下面这几行同时钉住 §四十五 的「一条结果就直接下载」：这个地址给回来的
    # 必须是一张图本身，不是一个装着图的 ZIP —— 能被 ``Image.open`` 打开
    # 就已经排除了后者。
    img = _decoded(response.content)
    assert img.size == want_size, f"方向 {orientation}：尺寸 {img.size}"
    for got, want in zip(quadrants(img), want_quadrants):
        assert close(got, want), f"方向 {orientation}：{quadrants(img)} 期望 {want_quadrants}"
    assert _orientation_of(response.content) in (None, 1)
