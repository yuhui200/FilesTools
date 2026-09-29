"""第十阶段 A 的几何选项走完整 HTTP 链路（§二十–§二十四、§四十七）。

``tests/test_image_geometry.py`` 已经验过流水线本身的像素正确性。这里验的是
**另一件事**：那些参数能不能活着穿过路由的五层校验链。

这两件事必须分开测，因为「流水线是对的」和「参数到得了流水线」是两回事。
第九阶段最隐蔽的那个 bug 正是后者：``validate_payload`` 放行了新键，
``_bind`` 里那张硬编码的键名表却没跟上，参数被**静默丢掉** ——
校验全绿、转换成功、用户拿到一张没被裁过的图。所以这里的每条断言都是
「用 Pillow 打开结果，看它到底变没变」，而不是「看 options 有没有被回显」。

目标格式一律用 **BMP**（不是 PNG）：PNG→PNG 是同格式转换，第九阶段起就
不在能力矩阵里；而要逐像素比对翻转/裁剪的结果，目标格式必须是**无损**的，
所以也不能用 JPG/WEBP。BMP 同时满足这两条 —— 换个格式，且一个像素都不动。
"""

from __future__ import annotations

import io
import json

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from tests.conftest import (
    build_image_bytes,
    conversion_task,
    image_files,
    run_conversion,
)

#: 无损的**跨格式**目标 —— 见模块 docstring。
LOSSLESS_TARGET = "bmp"


def _download(client: TestClient, snapshot: dict, index: int = 0) -> bytes:
    task = conversion_task(snapshot, index)
    assert task["status"] == "completed", task
    response = client.get(task["result"]["download_url"])
    assert response.status_code == 200, response.text
    return response.content


def _quadrants(width: int, height: int) -> bytes:
    """四象限图：四个角颜色互不相同，方向错了一眼就能看出来。"""
    image = Image.new("RGB", (width, height), (255, 0, 0))
    half_w, half_h = width // 2, height // 2
    image.paste((0, 255, 0), (half_w, 0, width, half_h))
    image.paste((0, 0, 255), (0, half_h, half_w, height))
    image.paste((255, 255, 255), (half_w, half_h, width, height))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _submit(client: TestClient, options: dict, png: bytes | None = None):
    source = png if png is not None else _quadrants(200, 100)
    return run_conversion(
        client,
        files=image_files(("a.png", source)),
        target_type=LOSSLESS_TARGET,
        options=json.dumps(options),
    )


def _convert(client: TestClient, options: dict, png: bytes | None = None) -> Image.Image:
    snapshot = _submit(client, options, png)
    return Image.open(io.BytesIO(_download(client, snapshot))).convert("RGB")


def _fail(client: TestClient, options: dict, png: bytes | None = None) -> dict:
    """提交一个**应当失败**的转换，返回那条任务的错误信息。"""
    task = conversion_task(_submit(client, options, png), 0)
    assert task["status"] == "failed", task
    return task


def _post_raw(client: TestClient, options: dict):
    """直接 POST，用来验提交期就被挡下的输入。"""
    return client.post(
        "/api/conversion/tasks",
        files=image_files(("a.png", _quadrants(200, 100))),
        data={
            "target_type": LOSSLESS_TARGET,
            "options": json.dumps(options),
        },
    )


def _corners(img: Image.Image) -> tuple:
    w, h = img.size
    return (
        img.getpixel((2, 2)),
        img.getpixel((w - 3, 2)),
        img.getpixel((2, h - 3)),
        img.getpixel((w - 3, h - 3)),
    )


# ----------------------------------------------------------------------
# 裁剪
# ----------------------------------------------------------------------

def test_crop_option_really_crops_the_output(client: TestClient) -> None:
    img = _convert(
        client,
        {"crop.x": 100, "crop.y": 50, "crop.width": 100, "crop.height": 50},
    )
    assert img.size == (100, 50)
    # 右下角那一块是白的 —— 证明切的是对的位置，不只是对的尺寸
    assert img.getpixel((50, 25)) == (255, 255, 255)


def test_crop_ratio_adjusts_the_height(client: TestClient) -> None:
    img = _convert(
        client,
        {"crop.ratio": "1:1", "crop.x": 0, "crop.y": 0,
         "crop.width": 100, "crop.height": 40},
    )
    assert img.size == (100, 100)


def test_crop_custom_ratio_is_parsed(client: TestClient) -> None:
    img = _convert(
        client,
        {"crop.ratio": "custom", "crop.custom_ratio": "2:1",
         "crop.x": 0, "crop.y": 0, "crop.width": 100, "crop.height": 10},
    )
    assert img.size == (100, 50)


@pytest.mark.parametrize(
    "options",
    [
        {"crop.x": -1, "crop.y": 0, "crop.width": 10, "crop.height": 10},
        {"crop.x": 0, "crop.y": -1, "crop.width": 10, "crop.height": 10},
        {"crop.x": 0, "crop.y": 0, "crop.width": 0, "crop.height": 10},
        {"crop.x": 0, "crop.y": 0, "crop.width": 10, "crop.height": 0},
    ],
    ids=["x<0", "y<0", "width=0", "height=0"],
)
def test_non_positive_crop_is_rejected_at_submit_time(
    client: TestClient, options: dict
) -> None:
    """§二十一：非正数与负数在**提交期**就该被挡下（选项边界层）。

    这四种输入不需要知道原图尺寸就能判断非法，所以它们不该等到任务跑起来
    才失败 —— 那会白白占一个 worker，用户还要多等一轮轮询。
    """
    response = _post_raw(client, options)
    assert response.status_code == 400, response.text
    body = response.json()
    assert "error" in body or "detail" in body
    # 不允许把 Python 异常泄漏成 500 或 traceback
    assert "Traceback" not in response.text


def test_crop_out_of_range_is_rejected_with_the_real_dimensions(
    client: TestClient,
) -> None:
    """越界要看原图才知道，所以只能在流水线里拒绝 —— 但必须**如实**拒绝。

    拒绝时把原图尺寸说出来，用户才知道该改成多少。消息里既要有真实的
    200×100，也要有他填的那个框 —— 只说「参数错误」等于没说。
    """
    task = _fail(
        client, {"crop.x": 150, "crop.y": 0, "crop.width": 100, "crop.height": 10}
    )
    message = task["error_message"]
    assert "200" in message and "100" in message, message
    assert "150" in message, message


# ----------------------------------------------------------------------
# 旋转 / 翻转
# ----------------------------------------------------------------------

def test_custom_rotation_angle(client: TestClient) -> None:
    """§二十三：90 的整数倍之外的自定义角度。"""
    img = _convert(client, {"rotation": "custom", "rotation.angle": 45},
                   png=_quadrants(200, 200))
    # 45° 默认扩大画布装下整张图，所以两边都变大
    assert img.width > 200 and img.height > 200


def test_custom_rotation_without_expand_keeps_the_canvas(client: TestClient) -> None:
    img = _convert(
        client,
        {"rotation": "custom", "rotation.angle": 45, "rotation.expand": False},
        png=_quadrants(200, 200),
    )
    assert img.size == (200, 200)


def test_custom_rotation_without_an_angle_is_rejected(client: TestClient) -> None:
    """选了「自定义角度」却没填角度 —— 不能悄悄当成 0° 放过去。

    这条在**提交期**就被挡下：缺角度不需要原图就能判断，没必要占一个 worker。
    """
    response = _post_raw(client, {"rotation": "custom"})
    assert response.status_code == 400, response.text
    assert "角度" in response.text


def test_flip_horizontal_really_mirrors(client: TestClient) -> None:
    img = _convert(client, {"flip": "horizontal"})
    assert _corners(img) == (
        (0, 255, 0), (255, 0, 0),
        (255, 255, 255), (0, 0, 255),
    )


def test_flip_vertical_really_mirrors(client: TestClient) -> None:
    img = _convert(client, {"flip": "vertical"})
    assert _corners(img) == (
        (0, 0, 255), (255, 255, 255),
        (255, 0, 0), (0, 255, 0),
    )


def test_flip_both_equals_a_180_rotation(client: TestClient) -> None:
    """水平 + 垂直 = 旋转 180°。两条不同的代码路径，同一张图。"""
    both = _convert(client, {"flip": "both"})
    turned = _convert(client, {"rotation": "180"})
    # 比字节，不比 getdata() —— 后者在 Pillow 12 已废弃（14 移除）
    assert both.tobytes() == turned.tobytes()


def test_flip_rejects_an_unknown_direction(client: TestClient) -> None:
    response = _post_raw(client, {"flip": "diagonal"})
    assert response.status_code == 400, response.text


# ----------------------------------------------------------------------
# 尺寸：新预设档、百分比、fit / fill / stretch
# ----------------------------------------------------------------------

def test_max_width_preset_limits_the_width(client: TestClient) -> None:
    """§十九 的最大宽度档。3000 宽 → 640 宽，高按比例。"""
    source = build_image_bytes(3000, 1500, "PNG")
    img = _convert(client, {"resize.mode": "w640"}, png=source)
    assert img.size == (640, 320)


def test_max_width_preset_does_enlarge(client: TestClient) -> None:
    """「最大宽度」档与 small/medium/large 不同 —— 用户点名了 1920，
    800 宽的图就该变成 1920 宽，而不是被「只缩不放」的规矩挡回去。"""
    source = _quadrants(800, 400)
    img = _convert(client, {"resize.mode": "w1920"}, png=source)
    assert img.size == (1920, 960)


def test_percentage_preset(client: TestClient) -> None:
    img = _convert(client, {"resize.mode": "p50"})
    assert img.size == (100, 50)


def test_custom_percentage(client: TestClient) -> None:
    img = _convert(client, {"resize.mode": "percent", "resize.percent": 25})
    assert img.size == (50, 25)


def test_resize_fit_and_fill_differ(client: TestClient) -> None:
    fit = _convert(client, {"resize.mode": "custom", "resize.width": 100,
                            "resize.height": 100, "resize.fit": "fit"})
    fill = _convert(client, {"resize.mode": "custom", "resize.width": 100,
                             "resize.height": 100, "resize.fit": "fill"})
    assert fit.size == (100, 50)
    assert fill.size == (100, 100)


def test_resize_stretch_distorts(client: TestClient) -> None:
    img = _convert(client, {"resize.mode": "custom", "resize.width": 100,
                            "resize.height": 100, "resize.fit": "stretch"})
    assert img.size == (100, 100)


@pytest.mark.parametrize("percent", [0, -5])
def test_invalid_percentage_is_rejected(client: TestClient, percent: int) -> None:
    response = _post_raw(
        client, {"resize.mode": "percent", "resize.percent": percent}
    )
    assert response.status_code == 400, response.text


# ----------------------------------------------------------------------
# 组合与顺序
# ----------------------------------------------------------------------

def test_crop_resize_rotate_flip_together(client: TestClient) -> None:
    """四个变换一起提交，顺序必须是 decode → crop → resize → rotate → flip。

    期望尺寸是手算出来的，不是从实现里抄的：200×100 裁成 200×50，
    缩到 100×25，转 90° 换成 25×100，再垂直翻转仍是 25×100。
    只要顺序被换过一对，这个数字就对不上。
    """
    img = _convert(
        client,
        {
            "crop.x": 0, "crop.y": 0, "crop.width": 200, "crop.height": 50,
            "resize.mode": "custom", "resize.width": 100, "resize.height": 25,
            "rotation": "90",
            "flip": "vertical",
        },
    )
    assert img.size == (25, 100)


def test_geometry_options_are_not_dropped_by_the_binder(client: TestClient) -> None:
    """绑定层漏键的回归守卫（第九阶段那个静默 bug 的同型）。

    只给一个裁剪参数，结果**必须**与不给它时不同。如果 ``_bind`` 忘记了
    ``crop.*``，这里会拿到一张 200×100 的原图 —— 而所有校验依然全绿。
    """
    plain = _convert(client, {})
    cropped = _convert(
        client, {"crop.x": 0, "crop.y": 0, "crop.width": 50, "crop.height": 50}
    )
    assert plain.size == (200, 100)
    assert cropped.size == (50, 50)
