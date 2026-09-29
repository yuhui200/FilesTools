"""第十阶段 A §七十四：20 张混合图片走真实队列，四条性质一条都不能少。

这个文件不测「某一个选项对不对」——那些在 ``test_image_geometry_api.py``、
``test_conversion_compression.py`` 里各自钉住了。这里测的是**批次跑起来之后
队列本身还正不正常**，四件事：

1. **不死锁** —— 二十项全部走到终态，不是「大部分到了就算了」；
2. **不丢件** —— 每一项都成功，且都能下载到结果；
3. **不重复** —— 二十项各自处理一次，没有哪一项被处理两次、也没有哪一项
   的结果其实是另一项的（每个源图带一个专属颜色，中心的那个像素就是它的
   身份证 —— 认错图会当场露馅）；
4. **不超订** —— 图片池同时最多 ``IMAGE_WORKERS`` 个在跑，且并发**真的
   发生过**（否则「没超订」是一句废话：串行跑当然不超订）。

## 为什么没有耗时断言

§七十四 要的是这四条性质，不是「跑得多快」。秒表判据在共享 CI 机器上不可靠
（第一次跑要付导入与磁盘缓存的代价，第二次不用），**拿它当验收线只会得到
一个会随机变红的测试**。所以这里只测量、不判分：耗时写进失败信息里方便排查，
断言里一个字都不提。这与第九阶段定下的原则一致。

## 为什么把池的计数也读进来

「没超订」如果只在处理器里数一个自己维护的计数器，那测的是**我自己的
计数器**，不是队列。所以这里读的是 ``GET /api/system/workers`` —— 第八阶段
就有的那个只读接口，它的 ``active`` 来自 worker 槽位的真实现状。顺带用
``handled`` 的增量证明这二十项**确实进了图片池**（§五十九：不许再建第二个
图片池；进了 default 池就说明分池没生效）。
"""

from __future__ import annotations

import io
import json
import time

from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from config import settings
from services.worker_pool import POOL_IMAGE
from tests.conftest import (
    CONVERSION_ENDPOINT,
    TASK_TIMEOUT_SECONDS,
    _CONVERSION_FINAL_STATES,
    conversion_task,
    image_files,
    submit_conversion,
)

#: 一批多少张 —— §七十四 点名的数字。
BATCH_SIZE = 20

#: 轮询间隔。取得比 ``wait_conversion`` 的 20ms 更密：并发峰值是一个**瞬时**
#: 量，采样太疏会把它漏掉，而漏掉之后 ``peak >= 2`` 会假红。
POLL_SECONDS = 0.005

#: 源格式循环。**全部是能转成 PNG 的格式** —— 能力矩阵里没有自转换，
#: PNG→PNG 不存在，所以 PNG 自己不能当这一批的源。
#:
#: HEIC 有意不在这个循环里：它依赖可选组件（``pillow-heif``），而本文件要
#: 保持「无条件可跑」。那条路已经在 ``tests/test_heic.py`` 里单独验过。
SOURCE_FORMATS = ("jpg", "webp", "bmp", "gif", "tiff", "svg")

#: ``PIL`` 的格式名。SVG 不走 Pillow，单独生成。
PILLOW_FORMAT = {
    "jpg": "JPEG",
    "webp": "WEBP",
    "bmp": "BMP",
    "gif": "GIF",
    "tiff": "TIFF",
}

#: 目标格式。选 PNG 有两个理由：对六个源格式**都存在**这条转换；而且它无损，
#: 「中心像素就是这张图的身份证」这句话才成立（JPEG/WEBP 会挪动颜色）。
TARGET = "png"

#: 这一批共用的处理参数。§四十四：同一批 = 同一目标 + 同一组参数。
#: ``w640`` 把每张图都缩到宽 640（预设允许放大，所以小图也会被拉到 640），
#: 于是「结果的宽高应该是什么」由**每一项自己的**源图宽高唯一决定。
OPTIONS = {"resize.mode": "w640", "metadata": "remove"}

#: 颜色容差。JPEG/WEBP 是有损的，纯色块也会被挪几个数；GIF 要走一次调色板
#: 量化。20 足够容下这些误差，而下面颜色的**最小间距是 50**，差着一个量级。
COLOUR_TOLERANCE = 20


def _colour(index: int) -> tuple[int, int, int]:
    """第 ``index`` 张图的专属颜色。

    在 (0/50/100/150/200) × (0/50/100/150) 这个粗网格上取点：任意两张图
    至少在一个通道上差 50，而容差只有 20 —— **认错图不可能发生**。
    """
    return ((index % 5) * 50, (index // 5) * 50, 128)


def _size(index: int) -> tuple[int, int]:
    """第 ``index`` 张图的宽高。

    ``(200+10i) / (100+6i)`` 严格递减，所以二十张图的**宽高比两两不同** ——
    缩到宽 640 之后，高度也两两不同。这是颜色之外的第二重身份。
    """
    return 200 + index * 10, 100 + index * 6


def _block_box(width: int, height: int) -> tuple[int, int, int, int]:
    """居中纯色块的 ``(左, 上, 宽, 高)``。占每一维的一半，块心即图心。"""
    block_w = max(2, width // 2)
    block_h = max(2, height // 2)
    return (width - block_w) // 2, (height - block_h) // 2, block_w, block_h


def _raster(index: int, fmt: str) -> bytes:
    """一张位图源图：噪点底 + 居中的专属纯色块。

    噪点是**为了让它慢一点**：纯色图的 PNG 编码快得几乎测不到并发，
    二十张加起来还不够一次采样。噪点只铺在块外面，块心因此是精确的。
    """
    width, height = _size(index)
    image = Image.new("RGB", (width, height), (245, 245, 245))

    if fmt != "gif":
        # GIF 不铺噪点：它要走调色板量化，抖动的噪点会把纯色块也搅浑。
        noise = Image.effect_noise((width, height), 48).convert("L")
        image = Image.blend(image, Image.merge("RGB", (noise, noise, noise)), 0.5)

    left, top, block_w, block_h = _block_box(width, height)
    ImageDraw.Draw(image).rectangle(
        [left, top, left + block_w - 1, top + block_h - 1], fill=_colour(index)
    )

    buffer = io.BytesIO()
    image.save(buffer, format=PILLOW_FORMAT[fmt])
    return buffer.getvalue()


def _svg(index: int) -> bytes:
    """一张 SVG 源图：和位图同样的构图，但走矢量。

    根元素显式声明 ``width``/``height``，渲染时用户单位与像素 1:1
    （见 ``compressors/svg.py`` 的模块文档），所以尺寸是可预期的。
    """
    width, height = _size(index)
    left, top, block_w, block_h = _block_box(width, height)
    red, green, blue = _colour(index)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}"'
        f' viewBox="0 0 {width} {height}">'
        f'<rect width="{width}" height="{height}" fill="#f5f5f5"/>'
        f'<rect x="{left}" y="{top}" width="{block_w}" height="{block_h}"'
        f' fill="#{red:02x}{green:02x}{blue:02x}"/>'
        "</svg>"
    ).encode("utf-8")


def _sources() -> list[tuple[int, str, bytes]]:
    """二十份 ``(序号, 扩展名, 内容)``，格式轮着来。"""
    items = []
    for index in range(BATCH_SIZE):
        fmt = SOURCE_FORMATS[index % len(SOURCE_FORMATS)]
        data = _svg(index) if fmt == "svg" else _raster(index, fmt)
        items.append((index, fmt, data))
    return items


def _pool(client: TestClient, name: str) -> dict:
    """读一次真实池快照。找不到那个池就是硬错误，不是「跳过」。"""
    response = client.get("/api/system/workers")
    assert response.status_code == 200, response.text
    for pool in response.json()["pools"]:
        if pool["name"] == name:
            return pool
    raise AssertionError(f"没有名为 {name} 的池：{response.json()}")


def _close(actual: tuple[int, ...], expected: tuple[int, ...]) -> bool:
    return all(abs(a - b) <= COLOUR_TOLERANCE for a, b in zip(actual, expected))


def test_twenty_mixed_images_cross_the_real_queue_intact(client: TestClient) -> None:
    """§七十四：20 张混合图片 —— 不死锁、不丢件、不重复、不超订。"""

    items = _sources()
    assert len(items) == BATCH_SIZE
    # 这一批真的"混合"：源格式不止一种。否则「混合」二字是空话。
    assert len({fmt for _, fmt, _ in items}) == len(SOURCE_FORMATS)

    limit = max(1, settings.IMAGE_WORKERS)

    before = _pool(client, POOL_IMAGE)

    response = submit_conversion(
        client,
        files=image_files(*((f"{index}.{fmt}", data) for index, fmt, data in items)),
        target_type=TARGET,
        options=json.dumps(OPTIONS),
    )
    assert response.status_code == 202, response.text
    batch_id = response.json()["batch_id"]

    # ---- 边等边采样：峰值并发只能在跑的时候看到 ----------------------
    peak = 0
    started = time.monotonic()
    deadline = started + TASK_TIMEOUT_SECONDS

    while True:
        snapshot = client.get(f"{CONVERSION_ENDPOINT}/{batch_id}").json()
        peak = max(peak, _pool(client, POOL_IMAGE)["active"])
        if snapshot["status"] in _CONVERSION_FINAL_STATES:
            break
        elapsed = time.monotonic() - started
        assert time.monotonic() < deadline, (
            f"批次提交后 {elapsed:.1f} 秒仍未进入终态，队列疑似死锁：{snapshot}"
        )
        time.sleep(POLL_SECONDS)

    elapsed = time.monotonic() - started

    # ---- 一、不死锁、不丢件：二十项全部成功 --------------------------
    statuses = {task["index"]: task["status"] for task in snapshot["tasks"]}
    assert sorted(statuses) == list(range(BATCH_SIZE)), (
        f"结果里的序号不是 0..{BATCH_SIZE - 1}，有项丢了或重了：{sorted(statuses)}"
    )
    failed = {index: task for index, task in statuses.items() if task != "completed"}
    assert not failed, (
        f"{len(failed)}/{BATCH_SIZE} 项没成功（耗时 {elapsed:.1f} 秒）："
        f"{ {i: conversion_task(snapshot, i).get('error') for i in failed} }"
    )

    # ---- 二、不重复：每一项的结果都对得上它自己的源图 ----------------
    outputs: dict[int, bytes] = {}
    for index, fmt, _ in items:
        task = conversion_task(snapshot, index)
        result = task["result"]
        download = client.get(result["download_url"])
        assert download.status_code == 200, download.text
        outputs[index] = download.content

        with Image.open(io.BytesIO(download.content)) as image:
            image.load()
            assert image.format == "PNG", f"第 {index} 项（源 {fmt}）不是 PNG"
            measured = (image.width, image.height)
            centre = image.convert("RGB").getpixel((image.width // 2, image.height // 2))

        # 服务端说它产出了多大 —— 但那只是一句话，下面用 Pillow 量出来的
        # 尺寸去对。「打开结果看它到底是什么」才是验收（§六十四–§七十八）。
        assert (result["width"], result["height"]) == measured, (
            f"第 {index} 项：结果里写的是 {result['width']}×{result['height']}，"
            f"打开却是 {measured[0]}×{measured[1]}"
        )

        # 颜色：这张图的身份证。混了、串了、拿错了，都在这里露馅。
        assert _close(centre, _colour(index)), (
            f"第 {index} 项（源 {fmt}）的中心像素是 {centre}，"
            f"应该是 {_colour(index)} —— 这一项拿到的可能不是它自己的结果"
        )

        # 几何：宽被 w640 拉到 640，高按**它自己的**宽高比走。
        source_width, source_height = _size(index)
        assert image.width == 640, f"第 {index} 项宽度不是 640：{image.width}"
        expected_height = source_height * 640 / source_width
        assert abs(image.height - expected_height) <= 1, (
            f"第 {index} 项高度 {image.height} 与它自己的宽高比"
            f"（{source_width}×{source_height} → 期望 {expected_height:.1f}）对不上"
        )

    # 二十份结果两两不同。颜色本来就各不相同，出现相同的内容只可能是
    # 「同一份文件被发给了多项」—— 那正是重复处理的样子。
    assert len(set(outputs.values())) == BATCH_SIZE, "有两项拿到了完全相同的结果文件"

    # ---- 三、不超订：并发上限是池的，且并发真的发生过 ----------------
    assert peak <= limit, f"图片池同时跑了 {peak} 个，超过上限 {limit}（超订）"
    if limit > 1:
        assert peak >= 2, (
            f"整批跑完最大并发只有 {peak}，说明二十项是串行跑的 —— "
            f"那样「没超订」是一句废话，分池也毫无意义"
        )

    # ---- 四、这一批确实走的是图片池 --------------------------------
    after = _pool(client, POOL_IMAGE)
    assert after["handled"] - before["handled"] == BATCH_SIZE, (
        f"图片池只处理了 {after['handled'] - before['handled']} 项，"
        f"应该是 {BATCH_SIZE} —— 有任务跑到别的池里去了"
    )
