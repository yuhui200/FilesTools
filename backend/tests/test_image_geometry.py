"""裁剪 / 旋转 / 翻转的像素级验证，以及固定变换顺序（第十阶段 A §二十–§二十四）。

为什么这个文件里几乎每条断言都读像素，而不是只看 ``.size``：

尺寸对不上，图也可能整个是反的。一张 100×200 的图旋转 90° 得到 200×100 ——
断言尺寸只能证明「转过了」，证明不了「转对了方向」。把 90° 顺时针写成逆时针、
把水平翻转写成垂直翻转，尺寸断言**全都是绿的**。所以方向类断言一律取
四个象限的代表像素，用一张四角颜色互不相同的图去比对。

同理，变换顺序错了（先旋转后裁剪、先翻转后旋转）在尺寸上往往看不出来，
却会让用户拿到一张方向不对的图。顺序断言用「独立算一遍期望图再逐像素比」
的方式钉住，而不是抄一份实现里的中间尺寸。
"""

from __future__ import annotations

import io

import pytest
from PIL import Image, ImageOps

from compressors.cropper import CropRequest, compute_crop_box
from compressors.pipeline import PipelineOptions, run_pipeline
from compressors.resizer import ResizeRequest, compute_target_size
from utils.errors import ValidationError

# 四角颜色互不相同 —— 任何一个方向上的错误都会让象限签名变化。
TL = (255, 0, 0)        # 左上 红
TR = (0, 255, 0)        # 右上 绿
BL = (0, 0, 255)        # 左下 蓝
BR = (255, 255, 255)    # 右下 白


def quadrant_image(width: int = 200, height: int = 100) -> Image.Image:
    """造一张四象限图，四个角的颜色两两不同。"""
    img = Image.new("RGB", (width, height), TL)
    half_w, half_h = width // 2, height // 2
    img.paste(TR, (half_w, 0, width, half_h))
    img.paste(BL, (0, half_h, half_w, height))
    img.paste(BR, (half_w, half_h, width, height))
    return img


def quadrants(img: Image.Image) -> tuple[tuple[int, ...], ...]:
    """取四个象限中心的像素 —— 图像的「方向签名」。"""
    w, h = img.size
    return (
        img.getpixel((w // 4, h // 4)),          # 左上
        img.getpixel((w * 3 // 4, h // 4)),      # 右上
        img.getpixel((w // 4, h * 3 // 4)),      # 左下
        img.getpixel((w * 3 // 4, h * 3 // 4)),  # 右下
    )


def save(img: Image.Image, path, fmt: str = "PNG") -> None:
    img.save(path, format=fmt)


def opened(result) -> Image.Image:
    """把流水线产出的字节真正解码回来 —— 不看字节数，看像素。"""
    return Image.open(io.BytesIO(result.data)).convert("RGB")


def colours(img: Image.Image) -> set[tuple[int, ...]]:
    """图上出现过的全部颜色。

    用 ``getcolors`` 而不是 ``getdata``：后者在 Pillow 12 已标记废弃，
    会在 Pillow 14 移除 —— 测试不该给自己埋一个两年后的红。
    """
    return {colour for _, colour in img.getcolors(maxcolors=img.width * img.height)}


# ----------------------------------------------------------------------
# 翻转：方向必须真的对，不能只检查文件存在
# ----------------------------------------------------------------------

def test_flip_horizontal_mirrors_left_and_right(tmp_path):
    source = tmp_path / "q.png"
    save(quadrant_image(), source)

    result = run_pipeline(source, PipelineOptions(target_format="png", flip="horizontal"))
    assert quadrants(opened(result)) == (TR, TL, BR, BL)


def test_flip_vertical_mirrors_top_and_bottom(tmp_path):
    source = tmp_path / "q.png"
    save(quadrant_image(), source)

    result = run_pipeline(source, PipelineOptions(target_format="png", flip="vertical"))
    assert quadrants(opened(result)) == (BL, BR, TL, TR)


def test_flip_both_equals_rotate_180(tmp_path):
    source = tmp_path / "q.png"
    save(quadrant_image(), source)

    result = run_pipeline(source, PipelineOptions(target_format="png", flip="both"))
    assert quadrants(opened(result)) == (BR, BL, TR, TL)


# ----------------------------------------------------------------------
# 旋转：90/180/270 与任意角度
# ----------------------------------------------------------------------

@pytest.mark.parametrize(
    "angle,expected",
    [
        (90, (BL, TL, BR, TR)),    # 顺时针：左下转到左上
        (180, (BR, BL, TR, TL)),
        (270, (TR, BR, TL, BL)),
    ],
)
def test_rotation_direction_is_clockwise(tmp_path, angle, expected):
    source = tmp_path / "q.png"
    save(quadrant_image(), source)

    result = run_pipeline(source, PipelineOptions(target_format="png", rotation=angle))
    assert quadrants(opened(result)) == expected


def test_rotation_90_swaps_dimensions(tmp_path):
    source = tmp_path / "q.png"
    save(quadrant_image(200, 100), source)

    result = run_pipeline(source, PipelineOptions(target_format="png", rotation=90))
    assert (result.width, result.height) == (100, 200)


def test_rotation_90_is_lossless_pixel_rearrangement(tmp_path):
    """90 的整数倍不该重采样 —— 每个像素值都必须原样出现。"""
    source = tmp_path / "q.png"
    save(quadrant_image(), source)

    result = run_pipeline(source, PipelineOptions(target_format="png", rotation=90))
    assert colours(opened(result)) == colours(quadrant_image())


def test_arbitrary_angle_expands_canvas(tmp_path):
    """45° 必须放大画布装下整张图，而不是把四个角切掉。"""
    source = tmp_path / "q.png"
    save(quadrant_image(200, 200), source)

    result = run_pipeline(
        source, PipelineOptions(target_format="png", rotation=45, rotation_expand=True)
    )
    assert result.width > 200 and result.height > 200


def test_arbitrary_angle_without_expand_keeps_canvas(tmp_path):
    source = tmp_path / "q.png"
    save(quadrant_image(200, 200), source)

    result = run_pipeline(
        source, PipelineOptions(target_format="png", rotation=45, rotation_expand=False)
    )
    assert (result.width, result.height) == (200, 200)


# ----------------------------------------------------------------------
# 裁剪：几何与越界
# ----------------------------------------------------------------------

def test_crop_extracts_the_requested_region(tmp_path):
    """切右下角那一块，拿到的必须是白 —— 不是「尺寸对了」就算数。"""
    source = tmp_path / "q.png"
    save(quadrant_image(200, 100), source)

    result = run_pipeline(
        source,
        PipelineOptions(target_format="png", crop=CropRequest(x=100, y=50, width=100, height=50)),
    )
    assert (result.width, result.height) == (100, 50)
    assert colours(opened(result)) == {BR}


def test_crop_is_lossless(tmp_path):
    source = tmp_path / "q.png"
    save(quadrant_image(200, 100), source)

    result = run_pipeline(
        source,
        PipelineOptions(target_format="png", crop=CropRequest(x=0, y=0, width=100, height=50)),
    )
    assert colours(opened(result)) == {TL}


@pytest.mark.parametrize(
    "request_",
    [
        CropRequest(x=-1, y=0, width=10, height=10),
        CropRequest(x=0, y=-1, width=10, height=10),
        CropRequest(x=0, y=0, width=0, height=10),
        CropRequest(x=0, y=0, width=10, height=0),
        CropRequest(x=0, y=0, width=-5, height=10),
        CropRequest(x=0, y=0, width=10, height=-5),
    ],
)
def test_crop_rejects_non_positive_and_negative(request_):
    with pytest.raises(ValidationError):
        compute_crop_box(200, 100, request_)


@pytest.mark.parametrize(
    "request_",
    [
        CropRequest(x=150, y=0, width=100, height=10),   # 右边越界
        CropRequest(x=0, y=80, width=10, height=100),    # 下边越界
        CropRequest(x=200, y=0, width=10, height=10),    # 起点就在外面
    ],
)
def test_crop_rejects_out_of_range(request_):
    with pytest.raises(ValidationError):
        compute_crop_box(200, 100, request_)


def test_crop_rejection_message_states_the_real_dimensions():
    """越界时必须把原图尺寸说出来 —— 用户才知道该改成多少。"""
    with pytest.raises(ValidationError) as excinfo:
        compute_crop_box(200, 100, CropRequest(x=150, y=0, width=100, height=10))
    assert "200" in str(excinfo.value) and "100" in str(excinfo.value)


def test_crop_ratio_adjusts_height_and_says_so():
    """选了 1:1 但高度填错 —— 服务端按比例纠正，并如实说明改动。"""
    box = compute_crop_box(400, 400, CropRequest(x=0, y=0, width=200, height=150, ratio="1:1"))
    assert (box.width, box.height) == (200, 200)
    assert box.note is not None and "200" in box.note


def test_crop_ratio_leaves_correct_height_untouched():
    box = compute_crop_box(400, 400, CropRequest(x=0, y=0, width=200, height=200, ratio="1:1"))
    assert (box.width, box.height) == (200, 200)
    assert box.note is None


def test_crop_16_9_ratio():
    box = compute_crop_box(1920, 1080, CropRequest(x=0, y=0, width=1600, height=1, ratio="16:9"))
    assert box.height == 900


# ----------------------------------------------------------------------
# 固定顺序：decode → crop → resize → rotate → flip → encode（§二十二）
# ----------------------------------------------------------------------

def test_crop_happens_before_resize(tmp_path):
    """先裁后缩 —— 缩放只花在留下的像素上。

    这张 400×200 的图裁掉下半只剩 400×100；若顺序反过来先缩到宽 200
    会得到 200×100，再裁剪时 400 宽的框根本放不下（直接报错）。
    所以「成功且是 200×50」本身就证明了裁剪在前。
    """
    source = tmp_path / "q.png"
    save(quadrant_image(400, 200), source)

    result = run_pipeline(
        source,
        PipelineOptions(
            target_format="png",
            crop=CropRequest(x=0, y=0, width=400, height=100),
            resize=ResizeRequest(width=200),
        ),
    )
    assert (result.width, result.height) == (200, 50)


def test_resize_happens_before_rotate(tmp_path):
    """先缩后转 —— 长边上限说的必须是最终那张图的长边。

    400×200 先缩到宽 200（得到 200×100）再顺时针转 90°，结果是 100×200。
    若先转再缩：转完是 200×400，再缩到宽 200 仍是 200×400 —— 两个答案
    不同，所以这条断言能真正分辨顺序。
    """
    source = tmp_path / "q.png"
    save(quadrant_image(400, 200), source)

    result = run_pipeline(
        source,
        PipelineOptions(
            target_format="png",
            resize=ResizeRequest(width=200),
            rotation=90,
        ),
    )
    assert (result.width, result.height) == (100, 200)


def test_rotate_happens_before_flip(tmp_path):
    """先转后翻 —— 反过来做，水平翻转会变成垂直翻转。

    期望图由测试自己独立算一遍：先 transpose 再 mirror。与流水线结果逐像素比，
    而不是抄一份中间尺寸。"""
    source = tmp_path / "q.png"
    save(quadrant_image(200, 100), source)

    result = run_pipeline(
        source, PipelineOptions(target_format="png", rotation=90, flip="horizontal")
    )
    expected = ImageOps.mirror(quadrant_image(200, 100).transpose(Image.Transpose.ROTATE_270))
    assert opened(result).tobytes() == expected.tobytes()


def test_full_order_matches_independent_computation(tmp_path):
    """四个变换一起上，与测试独立算出的结果逐像素相等。"""
    source = tmp_path / "q.png"
    save(quadrant_image(400, 200), source)

    result = run_pipeline(
        source,
        PipelineOptions(
            target_format="png",
            crop=CropRequest(x=0, y=0, width=400, height=100),
            resize=ResizeRequest(width=200),
            rotation=90,
            flip="vertical",
        ),
    )

    expected = quadrant_image(400, 200).crop((0, 0, 400, 100)).resize((200, 50), Image.Resampling.LANCZOS)
    expected = expected.transpose(Image.Transpose.ROTATE_270)
    expected = ImageOps.flip(expected)
    assert (result.width, result.height) == expected.size
    assert opened(result).tobytes() == expected.convert("RGB").tobytes()


# ----------------------------------------------------------------------
# 尺寸：fit / fill / stretch 与百分比（§十六–§十九）
# ----------------------------------------------------------------------

def test_resize_fit_letterboxes_without_cropping(tmp_path):
    """fit（默认）：整张图放进框内，四角一个不少。"""
    source = tmp_path / "q.png"
    save(quadrant_image(400, 200), source)

    result = run_pipeline(
        source,
        PipelineOptions(
            target_format="png",
            resize=ResizeRequest(width=100, height=100, fit="fit"),
        ),
    )
    assert (result.width, result.height) == (100, 50)
    assert quadrants(opened(result)) == (TL, TR, BL, BR)


def test_resize_fill_covers_and_centre_crops(tmp_path):
    """fill：缩放到盖住整个框再居中裁剪 —— 结果是正方形，且上下被切掉。

    400×200 放进 100×100 的框：先缩到 200×100（盖住），再居中切出
    中间那条 100×100。切掉的是左右，所以四个象限都还在，
    但左右各少了一半 —— 用「尺寸对 + 内容不变形」来区分它与 stretch。
    """
    source = tmp_path / "q.png"
    save(quadrant_image(400, 200), source)

    result = run_pipeline(
        source,
        PipelineOptions(
            target_format="png",
            resize=ResizeRequest(width=100, height=100, fit="fill"),
        ),
    )
    assert (result.width, result.height) == (100, 100)
    assert quadrants(opened(result)) == (TL, TR, BL, BR)


def test_resize_stretch_distorts_and_differs_from_fill(tmp_path):
    """stretch 尺寸与 fill 相同，但内容不同 —— 只断言尺寸区分不了这两者。

    四象限图被横向压扁一半后，每个象限的颜色仍然各占一格，
    所以象限签名一样；真正的区别在**边界位置**。这里用一张左黑右白的图：
    fill 保比例裁剪后仍是对半分，stretch 横向压缩后也是对半分 ——
    两者在这张图上恰好相同。所以换一张**左右渐变**的图，
    比较中间那一列的像素值。
    """
    source = tmp_path / "grad.png"
    # 单调递增的红通道斜坡（0→255，**不取模** —— 取模会在 256 处绕回 0，
    # 让「中间不该出现 0」这条断言失去意义）。
    gradient = Image.new("RGB", (400, 200))
    for x in range(400):
        ramp = round(x * 255 / 399)
        for y in range(200):
            gradient.putpixel((x, y), (ramp, 0, 0))
    gradient.save(source)

    fill = run_pipeline(
        source,
        PipelineOptions(
            target_format="png", resize=ResizeRequest(width=100, height=100, fit="fill")
        ),
    )
    stretch = run_pipeline(
        source,
        PipelineOptions(
            target_format="png",
            resize=ResizeRequest(width=100, height=100, keep_aspect=False, fit="stretch"),
        ),
    )
    assert (fill.width, fill.height) == (100, 100)
    assert (stretch.width, stretch.height) == (100, 100)

    fill_img, stretch_img = opened(fill), opened(stretch)
    fill_row = [fill_img.getpixel((x, 50))[0] for x in range(100)]
    stretch_row = [stretch_img.getpixel((x, 50))[0] for x in range(100)]

    # fill 只取了原图中间那条（x 100→300，红通道 64→191），
    # stretch 用了整张图的 0→399（红通道 0→255）。
    # 所以关键区别是**跨度**：fill 看到的是被裁窄的一段，stretch 看到整条。
    # 不断言具体端点值 —— LANCZOS 重采样会让端点差一两个单位，
    # 断言 `min == 0` 会变成一条依赖重采样细节的脆测试。
    fill_span = max(fill_row) - min(fill_row)
    stretch_span = max(stretch_row) - min(stretch_row)
    assert fill_row != stretch_row
    assert fill_span < stretch_span, "fill 裁掉了两端，跨过的色阶必然更窄"
    assert fill_span > 0, "fill 不该把图压成一块纯色"


def test_resize_percent_scales_both_edges(tmp_path):
    source = tmp_path / "q.png"
    save(quadrant_image(400, 200), source)

    result = run_pipeline(
        source, PipelineOptions(target_format="png", resize=ResizeRequest(percent=25))
    )
    assert (result.width, result.height) == (100, 50)
    assert quadrants(opened(result)) == (TL, TR, BL, BR)


def test_resize_percent_can_enlarge(tmp_path):
    """百分比是为数不多**允许放大**的入口 —— 预设档只缩不放，
    但用户明确说「放大到 200%」时就该放大。"""
    source = tmp_path / "q.png"
    save(quadrant_image(100, 50), source)

    result = run_pipeline(
        source, PipelineOptions(target_format="png", resize=ResizeRequest(percent=200))
    )
    assert (result.width, result.height) == (200, 100)


@pytest.mark.parametrize("percent", [0, -10])
def test_resize_rejects_non_positive_percent(percent):
    with pytest.raises(ValidationError):
        compute_target_size(400, 200, ResizeRequest(percent=percent))


def test_resize_rejects_preset_combined_with_percent():
    with pytest.raises(ValidationError):
        compute_target_size(400, 200, ResizeRequest(max_edge=1024, percent=50))


def test_resize_rejects_percent_combined_with_width():
    with pytest.raises(ValidationError):
        compute_target_size(400, 200, ResizeRequest(width=100, percent=50))


# ----------------------------------------------------------------------
# 快速路径：新选项绝不能被静默吞掉（§二十五 / §二十六）
# ----------------------------------------------------------------------

@pytest.mark.parametrize(
    "options",
    [
        {"crop": CropRequest(x=0, y=0, width=100, height=50)},
        {"flip": "horizontal"},
        {"rotation": 90},
        {"rotation": 45},
    ],
    ids=["crop", "flip", "rotate90", "rotate45"],
)
def test_geometry_options_never_take_the_passthrough(tmp_path, options):
    """目标格式与源格式相同、也没提别的要求时，只要动了几何就必须真的重编码。

    这是第九阶段那个静默 bug 的同型：近路照旧返回原文件字节，
    界面显示成功，图却一动不动。
    """
    source = tmp_path / "q.png"
    save(quadrant_image(200, 100), source)

    result = run_pipeline(source, PipelineOptions(target_format="png", **options))

    assert result.untouched is False
    # 真的变了 —— 不是「没走远路但碰巧尺寸相同」
    assert opened(result).tobytes() != quadrant_image(200, 100).tobytes()


def test_no_options_still_takes_the_passthrough(tmp_path):
    """反向守卫：什么都没要求时，近路必须还在。

    否则这条「不要无意义重编码」的既有优化就被上面的测试顺手删掉了，
    而所有几何断言依然全绿。"""
    source = tmp_path / "q.png"
    save(quadrant_image(200, 100), source)

    result = run_pipeline(source, PipelineOptions(target_format="png"))

    assert result.untouched is True
    assert result.data == source.read_bytes()
