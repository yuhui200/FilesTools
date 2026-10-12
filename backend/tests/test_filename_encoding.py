"""上传文件名编码的兼容层（本轮 §五）。

## 这条缺陷长什么样

Android 上传「中文样张.jpg」，结果叫 ``E4_B8_AD_E6_96_87_E6_A0_B7_E5_BC_A0.jpg``
—— 字节、尺寸、像素全对，**只有名字是乱的**。

根因是 ``expo/fetch`` 拼 multipart 时对文件名做了一次
``encodeURIComponent``（``mobile/node_modules/expo/src/winter/fetch/convertFormData.ts``
的 ``encodeFilename``，**无条件**，没有开关）。服务端这边**一个字节都不解码**，
``%E4%B8%AD`` 原样就是 ``%E4%B8%AD``，随后被 ``utils/files.py::sanitize_stem``
把白名单外的 ``%`` 换成 ``_``。

## 修法与这里的判据

服务端**不猜**：``a%20b.png`` 既可能是文件真叫这个名字（浏览器发原文），
也可能是「``a b.png`` 被编码了」（原生发的），从字符串上分不出来。
所以改成**客户端显式声明**（multipart 里多带一个
``filename_encoding=uri-component``），服务端只在看到它时才解一次。

于是这一份测试真正要钉的是**一句话**：

    同一个文件名，无论从哪条客户端路径上来，服务端看到的和产出的都必须一样。

下面第一组用例就是把「原生形态」与「Web 形态」两条请求各跑一遍，
逐字段比对（``source_filename``、结果名、下载下来的真实字节）。
这比「断言某条路径上名字是某个字面量」更强 —— 它同时挡住了
「解错」和「没解」两个方向。
"""

from __future__ import annotations

import importlib
import io
import pkgutil
import typing
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient
from PIL import Image

import routers
from routers.uploads import (
    FILENAME_ENCODING_FIELD,
    URI_COMPONENT,
    decode_uri_component_filename,
)
from tests.conftest import build_image_bytes, image_files, run_conversion

ENDPOINT = "/api/conversion/tasks"

#: 原生客户端在表单里带的那个「我编码过了」声明
NATIVE_DECLARATION = {FILENAME_ENCODING_FIELD: URI_COMPONENT}

#: §五 点名要覆盖的文件名。空格、括号、emoji、纯 ASCII、已带扩展名都在里面。
NAMES = [
    "测试图片.png",
    "中文 文件名.png",
    "report(最终版).png",
    "photo.png",
    "pic_🎉.png",
    "100%25.png",
]


def native_wire(name: str) -> str:
    """原生客户端在 ``filename=`` 里实际发出去的那串东西。

    与 ``expo/fetch`` 的 ``encodeFilename`` 同式：``encodeURIComponent``
    再加上把 ``/`` 换成 ``_``。
    """
    return quote(name.replace("/", "_"), safe="")


def web_files(name: str, data: bytes):
    """浏览器的形态：``filename=`` 里是**原始 UTF-8**，且不带任何声明。"""
    return image_files((name, data))


def native_files(name: str, data: bytes):
    """原生端的形态：``filename=`` 里是编码过的串。"""
    return image_files((native_wire(name), data))


def only_task(snapshot: dict) -> dict:
    assert len(snapshot["tasks"]) == 1, snapshot
    return snapshot["tasks"][0]


def fetch_image(client: TestClient, url: str) -> Image.Image:
    response = client.get(url)
    assert response.status_code == 200, response.text
    return Image.open(io.BytesIO(response.content))


# ----------------------------------------------------------------------
# 一、两条客户端路径必须产出同一份结果
# ----------------------------------------------------------------------

@pytest.mark.parametrize("name", NAMES)
def test_native_and_web_agree_on_every_name(client: TestClient, name: str) -> None:
    """同一个文件名，原生形态与 Web 形态的结果必须**逐字段相同**。

    原始图片的字节也一并比对：只要结果名对了但内容被换了（例如两张图搞混），
    这条也会红。
    """
    source = build_image_bytes(120, 90, "PNG")

    web = run_conversion(client, files=web_files(name, source), target_type="jpg")
    native = run_conversion(
        client,
        files=native_files(name, source),
        target_type="jpg",
        **NATIVE_DECLARATION,
    )

    assert web["status"] == native["status"] == "completed", (web, native)

    web_task, native_task = only_task(web), only_task(native)
    assert native_task["source_filename"] == web_task["source_filename"], (
        "服务端看到的源文件名不一致 —— 原生那条多半没被解回来"
    )
    # 顺带钉死「解回来的就是用户选的那个名字」（emoji 会被 sanitize 削掉，
    # 所以这一条只对不含被清洗字符的名字成立）
    if name == "测试图片.png":
        assert native_task["source_filename"] == name

    web_item, native_item = web["result"]["items"][0], native["result"]["items"][0]
    assert native_item["filename"] == web_item["filename"], "结果文件名不一致"

    with fetch_image(client, web["result"]["download_url"]) as web_image:
        assert web_image.format == "JPEG"
        assert web_image.size == (120, 90)
    with fetch_image(client, native["result"]["download_url"]) as native_image:
        assert native_image.format == "JPEG"
        assert native_image.size == (120, 90)


def test_the_decoded_name_is_what_the_user_picked(client: TestClient) -> None:
    """不绕弯的一次：原生形态上来，「中文 文件名.png」就该原样被看到。

    这一条同时确认空格**没有**被解成别的东西、括号没被吃掉、
    扩展名还在（``Path(...).stem`` 认得出 ``.png``）。
    """
    name = "中文 文件名.png"
    snapshot = run_conversion(
        client,
        files=native_files(name, build_image_bytes(80, 60, "PNG")),
        target_type="jpg",
        **NATIVE_DECLARATION,
    )
    assert snapshot["status"] == "completed", snapshot
    assert only_task(snapshot)["source_filename"] == name
    assert snapshot["result"]["items"][0]["filename"] == "中文 文件名.jpg"


def test_unicode_outside_the_safe_charset_is_cleaned_the_same_way_both_sides(
    client: TestClient,
) -> None:
    """emoji 会被 ``sanitize_stem`` 的白名单洗掉 —— 但**两边洗成一样**。

    这条防的是「以为把 emoji 也修好了」。emoji 不在
    ``_UNSAFE_CHARS`` 的允许集里（``\\w`` / 汉字 / 少数标点），
    所以它本来就会被替换掉，这与编码无关。要保证的是：
    Web 与原生**结果一致**，而不是「emoji 能留下」。
    """
    assert decode_uri_component_filename(native_wire("pic_🎉.png")) == "pic_🎉.png"

    source = build_image_bytes(80, 60, "PNG")
    web = run_conversion(client, files=web_files("pic_🎉.png", source), target_type="jpg")
    native = run_conversion(
        client,
        files=native_files("pic_🎉.png", source),
        target_type="jpg",
        **NATIVE_DECLARATION,
    )
    assert only_task(web)["source_filename"] == only_task(native)["source_filename"]
    assert web["result"]["items"][0]["filename"] == native["result"]["items"][0]["filename"]


def test_an_unsupported_format_keeps_the_decoded_name(client: TestClient) -> None:
    """不支持的文件格式：失败项里报出来的名字也要是解回来的那个。

    失败路径上一样要显示对的名字 —— 用户看到「『测试图片.png』格式不支持」
    才知道是哪一份出的问题。
    """
    name = "测试图片.xyz"
    response = client.post(
        ENDPOINT,
        files=native_files(name, b"not a real document" * 8),
        data={"target_type": "jpg", **NATIVE_DECLARATION},
    )
    assert response.status_code == 202, response.text

    from tests.conftest import wait_conversion

    snapshot = wait_conversion(client, response.json()["batch_id"])
    assert snapshot["status"] == "failed", snapshot
    task = only_task(snapshot)
    assert task["status"] == "failed"
    assert task["source_filename"] == name, "失败项上的源文件名没有解回来"


# ----------------------------------------------------------------------
# 二、声明是必需的：不发声明就一个字节都不许动
# ----------------------------------------------------------------------

def test_without_the_declaration_the_name_is_left_exactly_as_sent(
    client: TestClient,
) -> None:
    """没有声明时，服务端**什么都不做** —— 这是 Web 端不被误伤的全部依据。

    浏览器发的 ``filename`` 就是文件名的原始字节。旧客户端（0.1.0/0.1.1
    的 APK）也不发这个声明，它们的行为必须**逐字节不变**。
    """
    wire = native_wire("测试图片.png")
    snapshot = run_conversion(
        client,
        files=image_files((wire, build_image_bytes(80, 60, "PNG"))),
        target_type="jpg",
    )
    assert snapshot["status"] == "completed", snapshot
    assert only_task(snapshot)["source_filename"] == wire, "没有声明却动了文件名"


def test_a_literal_percent_name_is_not_decoded(client: TestClient) -> None:
    """字面叫 ``a%20b.png`` 的文件（浏览器发原文）**不许**被解成 ``a b.png``。

    这正是「不猜」的理由：光看字符串分不出「真叫这个名字」和「被编码了」，
    所以只能由客户端声明。这条是那个决定的守卫。
    """
    literal = "a%20b.png"
    snapshot = run_conversion(
        client,
        files=image_files((literal, build_image_bytes(80, 60, "PNG"))),
        target_type="jpg",
    )
    assert only_task(snapshot)["source_filename"] == literal
    # 它会照旧被 sanitize_stem 洗掉 %，但**不是**被解成空格
    assert snapshot["result"]["items"][0]["filename"] == "a_20b.jpg"


def test_a_declaration_that_says_nothing_we_know_is_ignored(client: TestClient) -> None:
    """声明了一个我们不认识的编码值 → 一律不解（宁可原样，也不按不认识的约定动）。"""
    wire = native_wire("测试图片.png")
    snapshot = run_conversion(
        client,
        files=image_files((wire, build_image_bytes(80, 60, "PNG"))),
        target_type="jpg",
        **{FILENAME_ENCODING_FIELD: "base64"},
    )
    assert only_task(snapshot)["source_filename"] == wire


def test_declaring_while_sending_raw_utf8_is_a_no_op(client: TestClient) -> None:
    """声明了、但发的是原始 UTF-8 → 结果不变（``unquote`` 对非 ASCII 是恒等）。

    这是「万一将来 expo 修了编码器、声明却还在发」的降级路径：
    名字不会因此坏掉。
    """
    name = "中文 文件名.png"
    snapshot = run_conversion(
        client,
        files=image_files((name, build_image_bytes(80, 60, "PNG"))),
        target_type="jpg",
        **NATIVE_DECLARATION,
    )
    assert only_task(snapshot)["source_filename"] == name


# ----------------------------------------------------------------------
# 三、解不动的时候要原样留着，不许弄出半个字符
# ----------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw",
    [
        "100%ZZ.png",       # 不是合法转义
        "%E4%B8.png",       # 截断的多字节序列：解出来不是合法 UTF-8
        "%FF%FE.png",       # 合法转义但字节序列不是 UTF-8
        "50% 完成.png",      # 孤零零一个百分号
    ],
)
def test_undecodable_names_are_returned_untouched(raw: str) -> None:
    """解不动就原样返回，**绝不**吐替换字符 U+FFFD。

    半个字符比一个丑名字糟得多：用户拿着 ``\\ufffd\\ufffd.png``
    既认不出是哪个文件，也没法拿去搜。
    """
    decoded = decode_uri_component_filename(raw)
    assert decoded == raw, f"{raw!r} 被解成了 {decoded!r}"
    assert "�" not in decoded


def test_the_round_trip_is_exact_for_every_character_class() -> None:
    """``encodeURIComponent`` 与这里的解码是严格互逆的 —— 逐个字符类过一遍。

    只要这条成立，「原生端解回来的名字 == 用户选的名字」就是**推导出来的**，
    不是靠几个样例碰运气。
    """
    samples = [
        "photo.png",
        "测试图片.png",
        "中文 文件名.jpg",
        "report(最终版).docx",
        "50% 完成.png",
        "a+b&c=d.png",
        "引号'与\"双引号.png",
        "换行\n制表\t.png",
        "pic_🎉.png",
        "日本語のファイル.pdf",
        "한국어 파일.pdf",
    ]
    for name in samples:
        assert decode_uri_component_filename(native_wire(name)) == name, name


# ----------------------------------------------------------------------
# 四、机械对账：漏一个 router 就是一个静默的错名
# ----------------------------------------------------------------------

def _route_hints(route) -> dict:
    try:
        return typing.get_type_hints(route.endpoint)
    except Exception:  # pragma: no cover - 取不到注解时按「不收文件」处理
        return {}


def _is_upload(annotation) -> bool:
    from fastapi import UploadFile

    if annotation is UploadFile:
        return True
    return any(_is_upload(arg) for arg in typing.get_args(annotation))


def _routers() -> list[tuple[str, typing.Any]]:
    found = []
    for info in pkgutil.iter_modules(routers.__path__):
        module = importlib.import_module(f"routers.{info.name}")
        router = getattr(module, "router", None)
        if router is not None:
            found.append((info.name, router))
    return found


def test_every_router_that_receives_files_declares_the_encoding() -> None:
    """凡是收 ``UploadFile`` 的 router，都必须挂上归一化依赖。

    单点挂载的全部价值就在这条上：漏一个 router，那条路上的文件名就会
    静默地错下去，而且**只有真机上传中文名才看得出来**。
    这里扫的是 ``routers/`` 包里**所有**模块，将来新加一个 router
    不收文件就没事、收了文件没挂就立刻红。
    """
    from routers.uploads import declare_upload_filenames

    checked = 0
    for name, router in _routers():
        receives = any(
            _is_upload(annotation)
            for route in router.routes
            for annotation in _route_hints(route).values()
        )
        declared = any(
            getattr(dependency.dependency, "__name__", "") == declare_upload_filenames.__name__
            for dependency in router.dependencies
        )
        if receives:
            checked += 1
            assert declared, f"routers/{name}.py 收了文件却没挂 declare_upload_filenames"

    # 别因为一个都没扫到而「通过」
    assert checked >= 7, f"只扫到 {checked} 个收文件的 router，扫描逻辑本身可能坏了"


def test_both_sides_spell_the_contract_identically() -> None:
    """客户端与这里的字段名/取值必须**逐字相同**。

    两边各写一份字面量的后果是：改了这边忘了那边，而症状是
    「文件名又变回一串十六进制」—— 看上去像回归，实际是拼写漂移。
    这条做一次机械对账，从 TS 源码里把字面量抠出来比。
    """
    from pathlib import Path

    ts = Path(__file__).resolve().parents[2] / "mobile" / "src" / "services" / "api" / "filenameEncoding.ts"
    assert ts.is_file(), f"找不到客户端的约定文件：{ts}"
    source = ts.read_text(encoding="utf-8")

    assert f"'{FILENAME_ENCODING_FIELD}'" in source, "字段名两边不一致"
    assert f"'{URI_COMPONENT}'" in source, "编码取值两边不一致"

    # 判据本身：Web 不许声明，原生必须声明
    assert "return platformOS !== 'web'" in source, (
        "declaresUriEncodedFilenames 的判据变了 —— Web 端一旦声明，"
        "字面叫 a%20b.png 的文件就会被解错"
    )


def test_the_client_actually_sends_the_declaration_when_uploading() -> None:
    """上传那条路径上真的用了这个判据。

    上一支只保证约定本身没漂；这一支保证它被**用上了** ——
    否则约定再对，``buildForm`` 不发也是白搭。
    """
    from pathlib import Path

    ts = Path(__file__).resolve().parents[2] / "mobile" / "src" / "services" / "api" / "conversion.ts"
    source = ts.read_text(encoding="utf-8")

    assert "declaresUriEncodedFilenames(Platform.OS)" in source, (
        "buildForm 里没有按平台决定要不要声明"
    )
    # 用**标识符**而不是字面量拼进表单 —— 拼字面量的话，
    # 上面那条「两边拼写一致」的对账就管不到这里了。
    assert "form.append(FILENAME_ENCODING_FIELD, URI_COMPONENT)" in source, (
        "buildForm 里没有真的把声明写进表单"
    )
