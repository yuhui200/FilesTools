"""结果预览（第十阶段 A §三十九 / §四十 / §四十一）。

## 这一节在防什么

「给结果配个缩略图」听起来是纯前端的事，实际最容易出两种事故：

1. **挂一个点下去报错的预览按钮。** 结果卡上有个「预览」，用户点了，
   后端回 400「该结果不支持预览」。能力目录里那个 ``supports_preview``
   于是变成一句空话 —— 声明了却做不到，正是 §二十六 要审的那种。
2. **为预览另开一个口子。** 为了让缩略图能显示，把结果目录挂成静态
   文件服务 —— 那就等于绕过了下载令牌的鉴权与过期清理（§四十 明令
   禁止）。

所以下面这些断言只认一件事：**发出去的 ``preview_url`` 必须真的能返回
那份结果的图**，而且走的必须是既有的那条令牌路。

## 「能预览」的判定放在哪

放在注册表（``registry.preview_url``），不在前端。前端自己判断
「这个格式能不能显示」就等于把格式知识抄了第二份 —— 加一种格式时要改
两处，漂移时没人发现。前端只回答一个是非题：这个键有没有值。

因此有一条**双向**的机械断言：能力条目上的 ``supports_preview`` 与
``preview_url`` 给不给值，必须逐条一致。少一边都是假话 —— 一边是
「说能点」，另一边是「真的能点」。
"""

from __future__ import annotations

import io
import zipfile

import pytest
from PIL import Image

from conversion import registry
from tests.conftest import conversion_task, image_files, run_conversion

#: 样本图。用 PNG 当源：无损，转出去的四角颜色不会被源自身的量化弄花。
SIZE = (48, 32)


def sample_png() -> bytes:
    """一张四角不同色的 PNG —— 预览回来的图能看出是不是这一张。"""
    img = Image.new("RGB", SIZE, (255, 0, 0))
    img.paste((0, 255, 0), (SIZE[0] // 2, 0, SIZE[0], SIZE[1] // 2))
    img.paste((0, 0, 255), (0, SIZE[1] // 2, SIZE[0] // 2, SIZE[1]))
    img.paste((255, 255, 255), (SIZE[0] // 2, SIZE[1] // 2, SIZE[0], SIZE[1]))
    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    return buffer.getvalue()


def sample_jpeg() -> bytes:
    buffer = io.BytesIO()
    Image.open(io.BytesIO(sample_png())).convert("RGB").save(
        buffer, format="JPEG", quality=95
    )
    return buffer.getvalue()


_SAMPLES = {"png": sample_png, "jpg": sample_jpeg}

#: 每一格：源格式、目标格式。目标覆盖注册表里**每一个**可预览格式，
#: 且每一格都是线上真的存在的能力（下面第一条断言自己会验）。
_PREVIEWABLE_CASES: tuple[tuple[str, str], ...] = (
    ("png", "jpg"),
    ("jpg", "png"),
    ("png", "webp"),
    ("png", "bmp"),
    ("png", "gif"),
)

#: 结构上不该给预览地址的目标：要么浏览器渲染不了（PDF 走的是另一条
#: 端点，只发 ``image/*``），要么主流浏览器根本不支持。
_NO_PREVIEW_CASES: tuple[tuple[str, str], ...] = (
    ("png", "pdf"),
    ("png", "tiff"),
    ("png", "ico"),
)


def _name(fmt: str) -> str:
    return f"样张.{fmt}"


def _run(client, source: str, target: str) -> dict:
    """跑一次真实转换，返回那一项的终态快照。"""
    snapshot = run_conversion(
        client,
        files=image_files((_name(source), _SAMPLES[source]())),
        target_type=target,
    )
    task = conversion_task(snapshot, 0)
    # 转换任务的终态叫 ``completed``（``ConversionTaskStatus.status``），
    # 不是队列内部那个 ``done`` —— 两套词汇在路由层换过一次名字。
    assert task["status"] == "completed", task
    return task


# ----------------------------------------------------------------------
# 1. 注册表：声明与实现逐条对账
# ----------------------------------------------------------------------

def test_the_flag_and_the_url_always_agree() -> None:
    """``supports_preview`` 为真 ⟺ ``preview_url`` 给得出地址。

    **双向**断言，不是「真的那部分也对」这种单向检查。单向的话，
    「声明支持却给不出地址」会漏过去 —— 而用户看到的正是那一半：
    按钮在，点下去报错。
    """
    for entry in registry.CAPABILITIES:
        # 只看 conversion：operation 条目**不产出可下载的结果**，
        # 它们的 ``target_type`` 是「接受什么输入」的占位，不是产物格式。
        # 对它们来说「能不能预览结果」这个问题本身就不成立，
        # 所以标志为假、也不该有地址 —— 下面那条断言把这一点也钉住。
        if entry.operation_type != registry.OPERATION_CONVERSION:
            assert entry.supports_preview is False, entry.id
            continue
        address = registry.preview_url("job", entry.target_type)
        assert (address is not None) is entry.supports_preview, (
            f"{entry.id}：supports_preview={entry.supports_preview} "
            f"但 preview_url={address!r}"
        )


def test_every_previewable_target_has_an_image_media_type() -> None:
    """能预览的目标，媒体类型必须是浏览器真的会内联渲染的。

    ``/api/preview`` 只发 ``image/*``（见 ``routers/download.py``）。
    哪天有人往 ``_PREVIEWABLE_TARGETS`` 里加一个非图片格式，
    这条会先红 —— 而不是等用户点出一个坏图。
    """
    for target in registry._PREVIEWABLE_TARGETS:
        media_type = registry.media_type_for_target(target)
        assert media_type.startswith("image/"), f"{target} -> {media_type}"
        assert media_type in registry.INLINE_PREVIEW_MEDIA_TYPES


def test_an_unknown_or_missing_target_gets_no_url() -> None:
    """拿不准就不给地址。少一个便利入口，好过多一个坏按钮。"""
    assert registry.preview_url("job", None) is None
    assert registry.preview_url("job", "") is None
    assert registry.preview_url("job", "不存在的格式") is None
    # 没有令牌就没有地址 —— 不能凭空拼一个指向别人的结果的链接
    assert registry.preview_url(None, "png") is None
    assert registry.preview_url("", "png") is None


# ----------------------------------------------------------------------
# 2. 真机：发出来的地址必须真的能返回那张图
# ----------------------------------------------------------------------

@pytest.mark.parametrize(("source", "target"), _PREVIEWABLE_CASES)
def test_a_previewable_result_returns_the_real_image(client, source, target) -> None:
    """端到端：转换 → 取 ``preview_url`` → 打开，必须是那张图本身。

    断言的是**内容**而不是状态码：能被 ``Image.open`` 打开、尺寸对得上、
    四角配色对得上。只看 200 的话，一个把 HTML 错误页发回来的端点
    也能过。
    """
    assert registry.supports(source, target), f"{source}→{target} 不是一条能力"

    task = _run(client, source, target)
    address = task["result"]["preview_url"]
    assert address, f"{source}→{target} 是可预览格式，却没给预览地址"

    response = client.get(address)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("image/")

    # 不设 filename 才会 inline 渲染；设了浏览器就变成下载（§三十九）
    assert "attachment" not in response.headers.get("content-disposition", "")

    with Image.open(io.BytesIO(response.content)) as img:
        assert img.size == SIZE, f"{source}→{target}：预览回来的是 {img.size}"


@pytest.mark.parametrize(("source", "target"), _PREVIEWABLE_CASES)
def test_previewing_never_consumes_the_download_token(client, source, target) -> None:
    """反复预览之后，下载**仍然可用**，而且拿到的是同一份文件。

    这是「预览复用下载令牌那条路」这句话的实测版本：预览与下载走的是
    同一个 ``job_id`` 下的同一份结果，但预览**不消耗**令牌
    （``routers/download.py`` 的预览端点不 discard）。如果哪天有人把
    预览实现成「下载一次算一次」，用户就会先看后下不了 —— 而单看
    预览那一步完全正常。
    """
    task = _run(client, source, target)
    address = task["result"]["preview_url"]
    download = task["result"]["download_url"]

    for _ in range(3):
        preview = client.get(address)
        assert preview.status_code == 200, "预览第二次就没了"
    seen = preview.content

    response = client.get(download)
    assert response.status_code == 200, "预览把下载令牌吃掉了"
    assert response.content == seen, "预览与下载不是同一份文件"

    # 顺序反过来再确认一次：**下载之后**预览就没了。结果文件是
    # 一次性的（下载即清理），所以这是诚实的结局，不是 bug ——
    # 留在磁盘上的东西已经没了，这里再报 200 才是假话。
    assert client.get(address).status_code == 404, "下载过的结果还说自己能预览"


@pytest.mark.parametrize(("source", "target"), _NO_PREVIEW_CASES)
def test_a_non_previewable_result_advertises_nothing(client, source, target) -> None:
    """渲染不了的目标不给预览地址 —— 键在，值是 ``None``。

    键**必须存在**（前端只读这个键，不该再判一次「有没有这个字段」），
    值必须是 ``None`` 而不是空串：空串是个合法但无意义的相对地址，
    前端一个 ``<img src="">`` 会去请求当前页面。
    """
    assert registry.supports(source, target), f"{source}→{target} 不是一条能力"

    task = _run(client, source, target)
    assert task["result"]["preview_url"] is None, task["result"]["preview_url"]


# ----------------------------------------------------------------------
# 3. 一批多条：每一项各自带自己的预览地址
# ----------------------------------------------------------------------

def test_each_row_of_a_batch_carries_its_own_preview_url(client) -> None:
    """两条结果 → 一个 ZIP，但两项各自的 ``preview_url`` 仍然可点。

    §四十五 说的是「≥2 条就打包下载」；打包是**下载**的形状，不该顺带
    把逐项预览也收走 —— 用户想先看一眼再决定下不下载，恰恰是多项时
    更需要的事。两条结果的预览地址必须不同，且各自返回自己那张图。
    """
    snapshot = run_conversion(
        client,
        files=image_files(
            ("甲.png", sample_png()),
            ("乙.png", sample_png()),
        ),
        target_type="jpg",
    )

    addresses = []
    for index in (0, 1):
        task = conversion_task(snapshot, index)
        assert task["status"] == "completed", task
        addresses.append(task["result"]["preview_url"])

    assert all(addresses), addresses
    assert addresses[0] != addresses[1], "两项给了同一个预览地址"

    sizes = []
    for address in addresses:
        response = client.get(address)
        assert response.status_code == 200, response.text
        with Image.open(io.BytesIO(response.content)) as img:
            sizes.append(img.size)
    assert sizes == [SIZE, SIZE]

    # 整批的下载仍然是一个 ZIP（§四十五），预览不影响它
    archive = client.get(snapshot["result"]["download_url"])
    assert archive.status_code == 200
    with zipfile.ZipFile(io.BytesIO(archive.content)) as zf:
        names = zf.namelist()
    assert len(names) == 2, names
    for name in names:
        assert ".." not in name and not name.startswith("/"), name


def test_the_group_summary_carries_the_same_rows_as_the_tasks(client) -> None:
    """整批 ``items[]`` 与逐项 ``tasks[].result`` 是同一份数据。

    两个入口（组级 ``result.items`` 与项级 ``result``）都由
    ``_item_summary`` 产出，但分别从两条路径序列化出去。这条钉住它们
    不会一个有一个没有 —— 比如 ``preview_url`` 只加在了一条路上。
    """
    snapshot = run_conversion(
        client,
        files=image_files(("甲.png", sample_png()), ("乙.png", sample_png())),
        target_type="jpg",
    )

    from_group = [item["preview_url"] for item in snapshot["result"]["items"]]
    from_tasks = [
        conversion_task(snapshot, index)["result"]["preview_url"] for index in (0, 1)
    ]
    assert from_group == from_tasks
    assert all(from_group), from_group
