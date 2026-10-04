"""图片元数据查看（第十阶段 A §三十三–§三十六）。

三条主线，每条都对应规格里一句具体的话：

* **§三十四 的十一行** —— 顺序、标签、取值，以及最重要的一条：
  读不出来就是读不出来（``None``），不编一个数字；
* **§三十五/§三十六 的隐私** —— GPS 坐标与机身序列号**哪里都不出现**：
  不在响应里，不在结构化日志里，也不在转换产物之外的地方；
* **§三十三 的「不吹牛」** —— 「清除元数据」说清除了就真的没了，
  「保留」说保留了就真的还在。帮助文案里那句「只移除可安全剥离的 EXIF」
  是一条可以被证伪的声明，所以这里逐条验证它。

## 为什么 canary 要分两类

「不许出现在日志里」与「不许出现在响应里」是**两个不同的集合**：

* 相机型号、镜头、创建软件 —— 界面上**就是要显示**给用户看的（那是他
  自己的文件），所以它们出现在响应里是对的，出现在日志里才是泄露。
  这类 canary 用来测日志。
* 机身序列号、GPS 坐标 —— 连响应里都不该有。§三十四 明说 GPS 只报
  「有没有」，所以这类 canary 两边都要测。

把两类混成一句「什么都不许出现」，测出来的东西就说不清了。
"""

from __future__ import annotations

import io
import logging
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from compressors.metadata import (
    MAX_METADATA_CHARS,
    TIFF_STRUCTURAL_TAGS,
    ImageMetadata,
    _clean,
    read_metadata,
)
from compressors.pipeline import PipelineOptions
from config import settings
from conversion import registry
from conversion.options import METADATA_DEFAULT, METADATA_REMOVE
from services.conversion_types import ConversionOptions
from tests.conftest import (
    encodable_formats,
    image_files,
    requires_heic_encode,
    run_conversion,
    run_task,
)

ENDPOINT = "/api/image/metadata"

# ----------------------------------------------------------------------
# canary
# ----------------------------------------------------------------------

#: **连响应里都不许出现**的值。序列号是最典型的：它唯一地标识一台设备，
#: 而查看元数据这个功能对它的需求是零。
#:
#: 每个 canary 都必须与**别的 canary 以及任何会被显示的值**互不为子串。
#: 第一版把镜头型号写成 ``CANARYLENS0001MODEL``、镜头序列号写成
#: ``CANARYLENS0001`` —— 于是「响应里不许出现序列号」那条断言对着一个
#: 合法的型号值报红。**假阳性比漏测更坏**：它会让人去改一个没坏的东西。
SERIAL_CANARIES = ("CANARYBODYSERIAL", "CANARYLENSSERIAL")
#: GPS 子 IFD 里的一条**文本** tag（``GPSProcessingMethod``）。
#: 用文本而不是坐标做 canary，是因为坐标是浮点数，「没出现」很难证；
#: 一串独特的字母则一出现就能被抓到 —— 而它只要被抓到，就说明有人把
#: 整个 GPS IFD 端出来了（那坐标自然也一起出来了）。
GPS_TEXT_CANARY = "CANARYGPSPROC"

#: **响应里该有、日志里不许有**的值：都是用户自己的拍摄信息，
#: 界面上正是要显示它们。
LOGGABLE_CANARIES = (
    "CANARYMAKE",
    "CANARYMODEL",
    "CANARYSOFT",
    "CANARYLENSMODEL",
)


def build_exif_jpeg(width: int = 800, height: int = 600, *, mode: str = "RGB") -> bytes:
    """造一张带**完整拍摄信息**的 JPEG，含 GPS 与两个序列号。

    Pillow 写嵌套 IFD 的方式是 ``exif.get_ifd(指针)`` 拿到一个 dict 再往里塞
    —— 直接给 ``exif[0x8769]`` 赋一个 dict 是不生效的（实测）。
    """
    exif = Image.Exif()
    # IFD0。厂商与型号**刻意互不为前缀**：这样这份样张读到的是最朴素的
    # 拼接结果，而「型号里已经含厂商名就不重复写」那条规则由
    # ``test_a_model_that_already_contains_the_make_is_not_repeated``
    # 用另一个专门造的文件去测。两件事混在一份样张里，红了也说不清是谁坏。
    exif[0x010F] = "CANARYMAKE"
    exif[0x0110] = "CANARYMODEL"
    exif[0x0131] = "CANARYSOFT"
    exif[0x0132] = "2024:01:02 03:04:05"

    sub = exif.get_ifd(0x8769)
    sub[0x9003] = "2023:05:01 10:00:00"
    sub[0xA434] = "CANARYLENSMODEL"
    # §三十四 只要「相机与镜头」，序列号从来就不在展示清单里。
    # 把它们塞进来，是为了证明**没读**，而不是「文件里没有」。
    sub[0xA431] = SERIAL_CANARIES[0]
    sub[0xA435] = SERIAL_CANARIES[1]

    gps = exif.get_ifd(0x8825)
    gps[1] = "N"
    gps[2] = (51.0, 30.0, 0.0)
    gps[3] = "E"
    gps[4] = (0.0, 7.0, 0.0)
    gps[27] = GPS_TEXT_CANARY

    buffer = io.BytesIO()
    Image.new(mode, (width, height), (10, 20, 30)).save(
        buffer, format="JPEG", exif=exif.tobytes()
    )
    return buffer.getvalue()


def build_plain_png(width: int = 64, height: int = 32) -> bytes:
    """一张**什么拍摄信息都没有**的 PNG。"""
    buffer = io.BytesIO()
    Image.new("RGBA", (width, height), (1, 2, 3, 4)).save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture
def exif_jpeg() -> bytes:
    return build_exif_jpeg()


@pytest.fixture
def exif_jpeg_path(tmp_path: Path, exif_jpeg: bytes) -> Path:
    path = tmp_path / "photo.jpg"
    path.write_bytes(exif_jpeg)
    return path


def post_metadata(client: TestClient, name: str, data: bytes, mime: str = "image/jpeg"):
    return client.post(ENDPOINT, files={"file": (name, data, mime)})


def rows(body: dict) -> dict[str, dict]:
    return {item["key"]: item for item in body["fields"]}


# ----------------------------------------------------------------------
# §三十四：十一行
# ----------------------------------------------------------------------

#: §三十四 点名的十一项，**顺序就是规格里的顺序**。
EXPECTED_KEYS = (
    "format",
    "width",
    "height",
    "color_mode",
    "dpi",
    "exif_available",
    "camera",
    "lens",
    "captured_at",
    "gps",
    "software",
)


def test_the_viewer_publishes_exactly_the_eleven_declared_rows(
    client: TestClient, exif_jpeg: bytes
) -> None:
    """行数与顺序都必须与 §三十四 逐字一致。

    顺序写死在规格里，不是「随便排的」：用户从上往下读，先知道
    「这是什么图、多大」，再知道「是谁拍的」。接口按列表下发，
    界面就不必再抄一份顺序。
    """
    response = post_metadata(client, "photo.jpg", exif_jpeg)
    assert response.status_code == 200, response.text

    body = response.json()
    assert tuple(item["key"] for item in body["fields"]) == EXPECTED_KEYS
    assert all(item["label"] for item in body["fields"]), "每一行都要有中文标签"


def test_a_photo_with_full_exif_reads_every_row(
    client: TestClient, exif_jpeg: bytes
) -> None:
    """信息齐全的照片，十一行里一行都不该是「读不出来」。"""
    body = post_metadata(client, "photo.jpg", exif_jpeg).json()
    values = {key: item["value"] for key, item in rows(body).items()}

    assert values == {
        "format": "JPEG",
        "width": "800",
        "height": "600",
        "color_mode": "RGB",
        "dpi": "72 × 72",
        "exif_available": "有",
        # 厂商与型号都写全：这份样张的型号里不含厂商名（见 build_exif_jpeg）
        "camera": "CANARYMAKE CANARYMODEL",
        "lens": "CANARYLENSMODEL",
        # EXIF 的日期写成 ``2023:05:01``，展示时换成短横线
        "captured_at": "2023-05-01 10:00:00",
        "gps": "有",
        "software": "CANARYSOFT",
    }


def test_a_model_that_already_contains_the_make_is_not_repeated(
    client: TestClient,
) -> None:
    """型号本身就以厂商名开头时，只显示型号 —— 不写成「佳能 佳能 EOS R6」。

    相机厂商普遍在型号里带上自己的名字（``Canon EOS R6`` / ``NIKON Z 6``），
    所以直接拼接多半会读出重影。规则是**大小写无关的前缀比对**：EXIF 里
    ``Make`` 与 ``Model`` 的大小写并不统一，逐字节比会漏掉一半的真机。

    这份样张是专门为这条规则造的，而不是复用上面那份 —— 上面那份刻意让
    两者互不为前缀，两条规则各自有一个干净的证人。
    """
    exif = Image.Exif()
    exif[0x010F] = "CANON"
    exif[0x0110] = "Canon EOS R6"

    buffer = io.BytesIO()
    Image.new("RGB", (100, 80), (7, 8, 9)).save(buffer, format="JPEG", exif=exif.tobytes())

    body = post_metadata(client, "dedup.jpg", buffer.getvalue()).json()
    assert rows(body)["camera"]["value"] == "Canon EOS R6"


def test_a_make_without_a_model_is_still_shown(client: TestClient) -> None:
    """只有厂商、没有型号的文件照样报出厂商，不是整行留空。

    这是拼接规则的另一半：拼接只在**两者都有**的时候需要处理重复，
    缺一个就直接报剩下的那一个 —— 而不是「型号为空，那这行不显示了」。
    """
    exif = Image.Exif()
    exif[0x010F] = "CANARYMAKE"

    buffer = io.BytesIO()
    Image.new("RGB", (100, 80), (7, 8, 9)).save(buffer, format="JPEG", exif=exif.tobytes())

    body = post_metadata(client, "make-only.jpg", buffer.getvalue()).json()
    assert rows(body)["camera"]["value"] == "CANARYMAKE"


def test_a_file_without_any_metadata_says_so_instead_of_guessing(
    client: TestClient,
) -> None:
    """没有拍摄信息的图：该是「无」的报「无」，该是「读不出来」的留 ``None``。

    两者**不是同一件事**，界面上的文案也不同：

    * 「EXIF 信息：无」是一个**确定的答案** —— 问过了，文件里没有；
    * 「相机：无法读取」是**没有答案** —— 这个文件里根本没有这个概念。

    把它们合成一句「无」，用户就没法区分「这图是截图所以没相机信息」
    与「服务器读不出这张图的相机信息」。
    """
    body = post_metadata(client, "shot.png", build_plain_png(), "image/png").json()
    values = {key: item["value"] for key, item in rows(body).items()}

    assert values["format"] == "PNG"
    assert values["width"] == "64"
    assert values["height"] == "32"
    assert values["color_mode"] == "RGB + 透明"
    assert values["exif_available"] == "无"
    assert values["gps"] == "无"
    # 这四项在 PNG 里无处可读，保持 None 而不是编一个值
    assert values["camera"] is None
    assert values["lens"] is None
    assert values["captured_at"] is None
    assert values["software"] is None
    # 这张 PNG 没有 pHYs 块，密度读不出来
    assert values["dpi"] is None


def test_a_meaningless_density_is_reported_as_unreadable(tmp_path: Path) -> None:
    """JFIF 密度为 1 时报「读不出来」，而不是「1 × 1 DPI」。

    密度 0/1 在 JFIF 里的含义是「这个文件只声明了像素宽高比，没有真实
    密度」，不是「每英寸一个点」。报一个数字出来看起来像测量结果，
    其实是一个约定值 —— 那是这条规则存在的全部理由。

    前半段先证明**这个文件真的写着 (1, 1)**，否则这条测试可能只是因为
    文件里压根没有密度而通过的（假绿）。
    """
    path = tmp_path / "tiny-dpi.jpg"
    buffer = io.BytesIO()
    Image.new("RGB", (40, 30), (5, 5, 5)).save(buffer, format="JPEG", dpi=(1, 1))
    path.write_bytes(buffer.getvalue())

    with Image.open(path) as img:
        assert img.info.get("dpi") == (1, 1), "前提不成立：Pillow 没读到 (1, 1)"

    assert read_metadata(path).dpi is None


def test_a_tiff_whose_metadata_was_stripped_says_no_exif(tmp_path: Path) -> None:
    """清过元数据的 TIFF，不能报「EXIF 信息：有」。

    TIFF 是这条规则唯一的例外：它的 0 号 IFD 里**必然**躺着一批描述像素
    排布的结构字段（宽、高、每样本位数、条带偏移…），而 Pillow 的
    ``getexif()`` 直接把整张 ``tag_v2`` 交出来 —— 于是 ``bool(exif)``
    恒为真，界面上就会在用户刚选了「清除元数据」之后告诉他「有 EXIF」。

    这条测试同时把两件事钉住：**结构 tag 不算 EXIF**，而**真的拍摄信息
    仍然算**（``keep`` 那一半），否则「都报无」也能过。
    """
    from compressors.pipeline import PipelineOptions, run_pipeline

    source = tmp_path / "cam.jpg"
    image = Image.new("RGB", (64, 48), (9, 9, 9))
    exif = image.getexif()
    exif[0x010F] = "Make"
    exif[0x0110] = "Model"
    image.save(source, format="JPEG", exif=exif, quality=90)

    kept = tmp_path / "kept.tiff"
    kept.write_bytes(
        run_pipeline(
            source,
            PipelineOptions(target_format="tiff", quality_value=85, metadata="keep"),
        ).data
    )
    assert read_metadata(kept).exif_available is True
    assert read_metadata(kept).camera == "Make Model"

    dropped = tmp_path / "dropped.tiff"
    dropped.write_bytes(
        run_pipeline(
            source,
            PipelineOptions(target_format="tiff", quality_value=85, metadata="remove"),
        ).data
    )
    assert read_metadata(dropped).exif_available is False
    # 前提：这个文件里**确实还有**一个非空的 0 号 IFD，只是里面全是
    # 结构字段 —— 否则这条测试可能只是因为 TIFF 压根没有 IFD 而通过。
    with Image.open(dropped) as check:
        tags = set(check.getexif())
    assert tags, "清过元数据的 TIFF 连结构 tag 都没有？前提不成立"
    assert 0x0110 not in tags and 0x010F not in tags


def test_the_top_level_keys_agree_with_the_rows(
    client: TestClient, exif_jpeg: bytes
) -> None:
    """``format`` / ``width`` / ``height`` 与 ``fields`` 里的同名行必须一致。

    它们来自**同一次读取的同一个对象**（``fields`` 是它的视图），
    所以今天是恒真的。留着这条断言是为了将来：哪一天有人图方便改成
    「顶层用校验层的值、行用读取器的值」，它就会红 —— 而那时响应里
    会同时存在两个可能对不上的尺寸。
    """
    body = post_metadata(client, "photo.jpg", exif_jpeg).json()
    table = rows(body)

    assert body["format"] == table["format"]["value"]
    assert str(body["width"]) == table["width"]["value"]
    assert str(body["height"]) == table["height"]["value"]
    assert body["filename"] == "photo.jpg"


def test_reading_never_needs_the_pixels(tmp_path: Path) -> None:
    """读取器只读文件头，不解码像素。

    这不是性能洁癖：``read_metadata`` 会被一个**同步**接口调用，
    而一张 12000×12000 的图解码一次要好几秒 —— 那是「点一下看信息，
    界面卡住」的来源。这里用一张截断的 JPEG 证明它没走解码那条路：
    文件只有前 2 KB，任何真正的解码都会失败。
    """
    full = build_exif_jpeg(1600, 1200)
    truncated = tmp_path / "half.jpg"
    truncated.write_bytes(full[:2048])

    metadata = read_metadata(truncated)

    assert metadata.width == 1600
    assert metadata.height == 1200
    assert metadata.camera == "CANARYMAKE CANARYMODEL"


# ----------------------------------------------------------------------
# §三十五 / §三十六：隐私
# ----------------------------------------------------------------------

def test_the_response_never_carries_coordinates_or_serials(
    client: TestClient, exif_jpeg: bytes
) -> None:
    """§三十四 的「GPS available」是**只报有没有**，不是「顺便给坐标」。

    序列号同理：它不在展示清单里，所以它一个字符都不该出现在响应体中。
    断言整段响应原文，而不是只看某个字段 —— 泄露可能发生在任何一个
    角落（错误信息、调试字段、将来新增的行），只看已知字段是抓不到的。
    """
    response = post_metadata(client, "photo.jpg", exif_jpeg)
    assert response.status_code == 200

    raw = response.text
    assert GPS_TEXT_CANARY not in raw, "GPS 子 IFD 的内容被端出来了"
    for serial in SERIAL_CANARIES:
        assert serial not in raw, f"机身/镜头序列号 {serial} 出现在响应里"
    # GPS 那一行的值只能是「有 / 无」，不能是任何别的东西
    assert rows(response.json())["gps"]["value"] in ("有", "无")


def test_metadata_never_reaches_the_structured_logs(
    client: TestClient, exif_jpeg: bytes, caplog: pytest.LogCaptureFixture
) -> None:
    """§三十五 / §三十六：结构化日志里不许出现用户元数据。

    测两件事，因为元数据有两条可能进日志的路：

    1. **查看**这一步 —— ``/api/image/metadata`` 自己；
    2. **转换**这一步 —— ``metadata=keep`` 时 EXIF 真的流经了整条流水线，
       那条路上的任务日志(``utils.logging.log_event``)才是更容易出事的地方。

    两边一起测，且用**整条记录的 repr** 当比对对象：只看 ``getMessage()``
    会漏掉塞进 ``record.args`` 或自定义字段的值。
    """
    canaries = LOGGABLE_CANARIES + SERIAL_CANARIES + (GPS_TEXT_CANARY,)

    with caplog.at_level(logging.DEBUG):
        assert post_metadata(client, "photo.jpg", exif_jpeg).status_code == 200
        snapshot = run_conversion(
            client,
            files=image_files(("photo.jpg", exif_jpeg)),
            target_type="png",
            options='{"metadata": "keep"}',
        )
        assert snapshot["status"] == "completed", snapshot

    haystack = "\n".join(
        [record.getMessage() for record in caplog.records]
        + [repr(record.__dict__) for record in caplog.records]
    )
    assert caplog.records, "一个日志记录都没抓到 —— 这条测试会假绿"
    for canary in canaries:
        assert canary not in haystack, f"{canary} 出现在结构化日志里"


def test_the_log_whitelist_still_drops_unknown_fields() -> None:
    """隐私靠的是 ``log_event`` 的**白名单**，不是调用点的自觉。

    这条是上面那条测试的地基：即使有人往 ``log_event`` 里塞了元数据，
    只要字段名不在白名单里就会被丢掉。地基塌了，上面那条测试迟早有一天
    会因为「刚好没人这么写」而失效。
    """
    from utils.logging import _ALLOWED_FIELDS, log_event

    for forbidden in ("camera", "gps", "exif", "serial", "software", "metadata"):
        assert forbidden not in _ALLOWED_FIELDS, forbidden

    # 现场试一次：塞进去也写不出来
    import logging as logging_module

    records: list[str] = []

    class _Capture(logging_module.Handler):
        def emit(self, record: logging_module.LogRecord) -> None:
            records.append(self.format(record))

    logger = logging_module.getLogger("filetools.tasks")
    handler = _Capture()
    logger.addHandler(handler)
    previous = logger.level
    logger.setLevel(logging_module.INFO)
    try:
        log_event(
            "task_started",
            task_id="t:0",
            camera=LOGGABLE_CANARIES[0],
            gps=GPS_TEXT_CANARY,
            **{"exif.serial": SERIAL_CANARIES[0]},
        )
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)

    written = "\n".join(records)
    assert "task_id=t:0" in written, "白名单里的字段反而没写出来"
    for canary in LOGGABLE_CANARIES + SERIAL_CANARIES + (GPS_TEXT_CANARY,):
        assert canary not in written


# ----------------------------------------------------------------------
# §三十三：清除 / 保留，说到做到
# ----------------------------------------------------------------------

def gps_ifd_of(data: bytes) -> dict:
    """读一张图里的 GPS 子 IFD。空字典 = 没有位置信息。"""
    with Image.open(io.BytesIO(data)) as img:
        return dict(img.getexif().get_ifd(0x8825))


def download(client: TestClient, snapshot: dict, index: int = 0) -> bytes:
    for task in snapshot["tasks"]:
        if task["index"] == index:
            response = client.get(task["result"]["download_url"])
            assert response.status_code == 200, response.text
            return response.content
    raise AssertionError(f"任务里没有第 {index} 项：{snapshot}")


def test_remove_really_removes_the_location(client: TestClient, exif_jpeg: bytes) -> None:
    """「清除」之后 GPS 必须真的没了 —— 帮助文案就是这么向用户承诺的。

    这条同时证明了那句承诺**不是空话**：源文件确实带 GPS（前半段先验），
    清除之后确实不带（后半段）。
    """
    assert gps_ifd_of(exif_jpeg), "前提不成立：样张里本来就没有 GPS"

    snapshot = run_conversion(
        client,
        files=image_files(("photo.jpg", exif_jpeg)),
        target_type="png",
        options='{"metadata": "remove"}',
    )
    assert snapshot["status"] == "completed", snapshot

    result = download(client, snapshot)
    assert gps_ifd_of(result) == {}
    with Image.open(io.BytesIO(result)) as img:
        assert dict(img.getexif()) == {}, "清除后不该还剩任何 EXIF"


def test_keep_really_keeps_the_location(client: TestClient, exif_jpeg: bytes) -> None:
    """「保留」之后拍摄信息还在 —— 否则那个选项就是个摆设。

    与上一条是**一对**：只有两条都在，「元数据：保留 / 清除」才是一句
    有内容的话。少了任何一条，这个开关都可能是「反正都一样」。
    """
    snapshot = run_conversion(
        client,
        files=image_files(("photo.jpg", exif_jpeg)),
        target_type="png",
        options='{"metadata": "keep"}',
    )
    assert snapshot["status"] == "completed", snapshot

    result = download(client, snapshot)
    assert gps_ifd_of(result), "选「保留」却把 GPS 丢了"
    with Image.open(io.BytesIO(result)) as img:
        exif = img.getexif()
        assert exif.get(0x010F) == "CANARYMAKE"
        # 方向标记由 ``load_image`` 摘掉（像素已经摆正了），别的都还在
        assert exif.get(0x0110) == "CANARYMODEL"


def test_the_help_text_promises_exactly_what_is_delivered(client: TestClient) -> None:
    """§三十三：不许宣称「100% 清除」，除非真的测过。

    取的是**前端真正渲染的那段文字**（走 HTTP 拿 schema），不是源码里的
    常量 —— 界面上写的是什么，这条测试管的就是什么。

    措辞必须与上面两条测试覆盖到的范围对得上：它说的是「只移除可安全
    剥离的拍摄信息（EXIF），不做像素级擦除」，而不是「彻底删除所有信息」。
    这句话是可以被证伪的，上面两条测试正是那条证伪线。
    """
    body = client.get("/api/conversion/capabilities").json()
    entry = next(item for item in body["conversions"] if item["id"] == "image.jpg-to-png")
    spec = next(
        item for item in entry["options_schema"]["items"] if item["key"] == "metadata"
    )

    help_text = spec["help"]
    assert "EXIF" in help_text
    assert "不做像素级擦除" in help_text
    for overclaim in ("彻底", "100%", "全部删除", "安全删除"):
        assert overclaim not in help_text, overclaim


# ----------------------------------------------------------------------
# 默认值只有一个来源（第十阶段 C §三）
# ----------------------------------------------------------------------

def test_the_schema_default_and_the_server_fallback_are_one_value(
    client: TestClient,
) -> None:
    """界面显示的默认值，与「调用方不传 options」时的兜底，必须是同一个。

    第十阶段 C §三 之前这两处**各写了一份字面量，而且不相等**：schema 说
    「保留」，``ConversionOptions.metadata`` 的兜底却是 ``remove``。
    后果是不传 ``options`` 的调用方拿到一张被清掉 EXIF 的图，而界面上
    「元数据」那一项显示的是「保留」—— 不报错、不失败，只是说了假话。

    这条测试走 HTTP 取**前端真正收到的那份 schema**，再与服务端的兜底
    对照。两处各写一份字面量的话，它会红。
    """
    body = client.get("/api/conversion/capabilities").json()
    entry = next(item for item in body["conversions"] if item["id"] == "image.jpg-to-png")
    spec = next(
        item for item in entry["options_schema"]["items"] if item["key"] == "metadata"
    )

    assert spec["default"] == METADATA_DEFAULT
    assert ConversionOptions().metadata == METADATA_DEFAULT
    # 默认值必须是**可选值之一**，否则界面会渲染出一个「选中项不在列表里」
    # 的下拉框，而用户改不动它
    assert spec["default"] in {value["value"] for value in spec["enum"]}


def test_the_unified_fallback_keeps_the_location(
    client: TestClient, exif_jpeg: bytes
) -> None:
    """不传 ``options`` 的统一转换，按界面上显示的那个默认走 —— 保留。

    与下面那条是**一对**：统一中心默认保留，老页面默认清除。两条路不一样
    是**有意的**（§三），所以各钉一条，谁改了哪一边都能立刻看出来。
    """
    snapshot = run_conversion(
        client, files=image_files(("photo.jpg", exif_jpeg)), target_type="png"
    )
    assert snapshot["status"] == "completed", snapshot

    assert gps_ifd_of(download(client, snapshot)), (
        "统一转换中心不传 options 时应按界面默认「保留」处理"
    )


def test_legacy_pages_still_drop_the_location(
    client: TestClient, exif_jpeg: bytes
) -> None:
    """老页面（压缩 / 尺寸调整 / 旧转换页）没有元数据选项，默认**仍然丢弃**。

    这是第一阶段定下的隐私取舍，第十阶段 C §三 明确保留：老页面不给用户
    选择，就不把拍摄信息（含 GPS）带进结果。统一中心把选择权交给用户，
    是另一回事。

    这条测试守的是 ``PipelineOptions.metadata`` 那个默认值。把它顺手改成
    ``keep``（理由通常是「和统一中心统一一下」）不会有别的测试变红，
    但三个老页面会开始把用户的位置信息带进结果 —— 所以这里从**接口**
    验一遍，而不是只断言那个常量。
    """
    assert PipelineOptions().metadata == METADATA_REMOVE
    assert gps_ifd_of(exif_jpeg), "前提不成立：样张里本来就没有 GPS"

    outcome = run_task(
        client,
        "/api/image/convert",
        files=image_files(("photo.jpg", exif_jpeg)),
        data={"target_format": "png"},
    )
    assert outcome.status_code == 200, outcome.text

    response = client.get(outcome.json()["download_url"])
    assert response.status_code == 200, response.text
    assert gps_ifd_of(response.content) == {}, "老页面把拍摄地点带进结果了"
    with Image.open(io.BytesIO(response.content)) as img:
        assert dict(img.getexif()) == {}


# ----------------------------------------------------------------------
# XMP（第十阶段 C §四）：EXIF 之外的第二套元数据
# ----------------------------------------------------------------------

#: 一份**真的** XMP 包：``<?xpacket>`` 头 + ``x:xmpmeta``。刻意做成一眼能认
#: 出来的标记，免得断言只能靠长度比对 —— 长度相等的两段不同字节会让
#: 「保留住了」变成假绿。
_XMP_CANARY = (
    b'<?xpacket begin="\xef\xbb\xbf"?>'
    b'<x:xmpmeta xmlns:x="adobe:ns:meta/">'
    b"<rdf:RDF><rdf:Description>FILETOOLS_XMP_CANARY</rdf:Description></rdf:RDF>"
    b"</x:xmpmeta><?xpacket end=\"w\"?>"
)

_PNG_XMP_KEY = "XML:com.adobe.xmp"


def xmp_of(data: bytes) -> bytes:
    """读一张图里的 XMP，没有就返回空字节。"""
    with Image.open(io.BytesIO(data)) as img:
        for key in ("xmp", _PNG_XMP_KEY):
            value = img.info.get(key)
            if isinstance(value, bytes) and value:
                return value
            if isinstance(value, str) and value:
                return value.encode("latin-1", errors="replace")
    return b""


def xmp_png(width: int = 40, height: int = 30) -> bytes:
    """一张带 XMP 但没有 EXIF 的 PNG。

    **故意用 PNG 当载体**：它的 XMP 存在 iTXt 文本块里（读回来是 ``str``），
    与 JPEG / TIFF 的二进制通道不是一回事。载体要是选 JPEG，就测不出
    「键名与类型都对不上」这一类问题 —— 而那正是这条链路最容易漏的地方。
    """
    from PIL import PngImagePlugin

    meta = PngImagePlugin.PngInfo()
    meta.add_text(_PNG_XMP_KEY, _XMP_CANARY.decode("latin-1"))
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (12, 34, 56)).save(
        buffer, format="PNG", pnginfo=meta
    )
    return buffer.getvalue()


def test_the_xmp_sample_really_carries_xmp() -> None:
    """先证明样张里真有 XMP —— 否则下面每条「保住了」都无从谈起。"""
    assert xmp_of(xmp_png()) == _XMP_CANARY


@pytest.mark.parametrize(
    "target",
    [
        "jpeg",
        "png",
        "webp",
        "tiff",
        # 只有装得起 HEVC 编码器的机器才写得出 HEIC；缺了就只跳过这一格，
        # 上面四种照测（见 conftest 的 requires_heic_encode）。
        pytest.param("heif", marks=requires_heic_encode),
    ],
)
def test_xmp_survives_every_target_that_can_carry_it(target: str) -> None:
    """「保留」在每一种装得下 XMP 的格式上都必须真的留住它。

    这里**不逐格式写断言**，而是拿 ``supports_xmp`` 的答案去对账：
    表说是就真能往返、说否就真没有。所以 :data:`XMP_FORMATS` 那张表
    不是「我们希望的」，是「实测的」—— 谁把某个格式加进去而 Pillow
    其实静默丢弃，这条会红（``xmp=`` 静默失效是这张表存在的全部理由）。
    """
    from compressors.encoder import supports_xmp
    from compressors.pipeline import run_pipeline

    assert supports_xmp(target), f"{target} 应该在这张表里，用例选错了"

    with tempfile.TemporaryDirectory() as raw:
        source = Path(raw) / "xmp.png"
        source.write_bytes(xmp_png())
        result = run_pipeline(
            source,
            PipelineOptions(target_format=target, quality_value=95, metadata="keep"),
        )

    assert xmp_of(result.data) == _XMP_CANARY, (
        f"{target}：声明支持 XMP，结果里却读不到（或读到了别的字节）"
    )
    assert not [note for note in result.notes if "XMP" in note], result.notes


def test_a_target_that_cannot_carry_xmp_says_so(tmp_path: Path) -> None:
    """BMP / GIF / ICO 装不下 XMP —— 那就**明说**，不许静默丢掉。

    这三个格式的容器里没有放 XMP 的地方，实测三种写法全是静默丢弃
    （不抛错、也不写进去）。静默是最坏的一种：用户选了「保留」，
    拿到结果、看到「转换成功」，而版权信息已经没了，没有任何一句话
    告诉他发生了什么。
    """
    from compressors.pipeline import XMP_DROPPED_NOTE, run_pipeline

    source = tmp_path / "xmp.png"
    source.write_bytes(xmp_png())

    for target in ("bmp", "gif", "ico"):
        result = run_pipeline(
            source,
            PipelineOptions(target_format=target, quality_value=95, metadata="keep"),
        )
        assert xmp_of(result.data) == b"", (
            f"{target}：居然写进去了 XMP，那这条测试的前提（装不下）是错的"
        )
        assert XMP_DROPPED_NOTE in result.notes, (target, result.notes)


def test_remove_drops_the_xmp_in_every_format(tmp_path: Path) -> None:
    """选了「清除」，XMP 就必须真的不在 —— 含源格式自己。

    **每个本机能写出来的格式都要过一遍**，包括那些本来就不支持 XMP 的：
    它们的答案应当是同一个（没有），而「碰巧没有」与「真的删掉了」在这里
    看起来一样，所以正向对照由上一条测试给出（源文件确实有 XMP）。
    """
    from compressors.pipeline import run_pipeline

    source = tmp_path / "xmp.png"
    source.write_bytes(xmp_png())

    for target in encodable_formats():
        result = run_pipeline(
            source,
            PipelineOptions(target_format=target, quality_value=95, metadata="remove"),
        )
        assert xmp_of(result.data) == b"", f"{target}：清了元数据，XMP 却还在"


def test_a_tiff_source_keeps_its_exif_at_last(
    client: TestClient, tmp_path: Path
) -> None:
    """TIFF 源文件选「保留」，拍摄信息必须真的留得下来。

    **这条测试盯的是一个曾经完全走不到的分支**：TIFF 的拍摄信息只在
    ``getexif()`` 上，``info`` 里没有 ``exif`` 这个键；而 ``load_image``
    里的 ``exif_transpose`` 会返回一个新对象、把 ``getexif()`` 清空
    （实测：源文件没有方向标记时也照样是新对象）。于是流水线那句
    ``img.info.get("exif")`` 对 TIFF 源**永远**是 ``None``：
    「保留元数据」在 TIFF 上等于什么都没保留，不报错、不提示。

    从**接口**验（而不是直接调 ``read_exif``），因为缺陷横跨
    解码 → 流水线两层，只测其中一层都可能漏。
    """
    exif = Image.Exif()
    exif[0x0110] = "CanaryModel"
    exif[0x010F] = "CanaryMake"
    sub = exif.get_ifd(0x8769)
    sub[0x9003] = "2026:09:29 12:00:00"
    gps = exif.get_ifd(0x8825)
    gps[1] = "N"
    gps[2] = (31.0, 12.0, 0.0)

    source = tmp_path / "shot.tif"
    # 传**字节**而不是 Exif 对象：Pillow 的 TIFF 写入器拿到对象时只搬 0 号
    # IFD 的顶层标签，子 IFD 会被丢掉（实测）。传字节才会整包写进去。
    Image.new("RGB", (32, 24), (7, 8, 9)).save(
        source, format="TIFF", exif=exif.tobytes()
    )

    # 前提：样张自己两样都带上了。少了这一步，下面「都没丢」可能只是
    # 因为本来就没有 —— 一条永远为真的断言没有价值。
    with Image.open(source) as check:
        assert check.getexif().get(0x0110) == "CanaryModel"
        assert check.getexif().get_ifd(0x8769).get(0x9003) == "2026:09:29 12:00:00"
        assert check.getexif().get_ifd(0x8825).get(1) == "N"

    snapshot = run_conversion(
        client,
        files=image_files(("shot.tif", source.read_bytes())),
        target_type="png",
        options='{"metadata": "keep"}',
    )
    assert snapshot["status"] == "completed", snapshot
    result = download(client, snapshot)

    with Image.open(io.BytesIO(result)) as img:
        got = img.getexif()
    assert got.get(0x0110) == "CanaryModel", "TIFF 源的相机型号没保住"
    assert got.get(0x010F) == "CanaryMake", "TIFF 源的厂商没保住"
    assert got.get_ifd(0x8769).get(0x9003) == "2026:09:29 12:00:00", (
        "TIFF 源里 Exif 子 IFD 的拍摄时间没保住"
    )
    assert got.get_ifd(0x8825).get(1) == "N", "TIFF 源里的 GPS 没保住"
    # 结构字段是**像素怎么排布**，不是拍摄信息。273（StripOffsets）这种
    # 「像素在文件第几个字节」的数字只对源文件成立，带进结果就是噪声，
    # 严重时会让别的查看器去一个不存在的位置找像素。
    for structural in sorted(TIFF_STRUCTURAL_TAGS):
        assert structural not in got, f"结构字段 {structural} 被当成拍摄信息带出来了"


def test_a_tiff_source_still_loses_everything_when_removing(
    client: TestClient, tmp_path: Path
) -> None:
    """同一份 TIFF 源，选「清除」就必须两样都没有。

    与上一条成对：只有「保留」那条的话，「清除」可能只是因为**根本
    没有东西可清**而看起来是对的 —— 那正是这条路上原来的状态。
    """
    exif = Image.Exif()
    exif[0x0110] = "CanaryModel"
    exif[700] = _XMP_CANARY
    source = tmp_path / "shot.tif"
    Image.new("RGB", (32, 24), (7, 8, 9)).save(source, format="TIFF", exif=exif)

    assert xmp_of(source.read_bytes()) == _XMP_CANARY, "前提不成立：样张里没有 XMP"

    snapshot = run_conversion(
        client,
        files=image_files(("shot.tif", source.read_bytes())),
        target_type="png",
        options='{"metadata": "remove"}',
    )
    assert snapshot["status"] == "completed", snapshot
    result = download(client, snapshot)

    with Image.open(io.BytesIO(result)) as img:
        assert dict(img.getexif()) == {}, "选了「清除」，TIFF 源的拍摄信息还在"
    assert xmp_of(result) == b"", "选了「清除」，XMP 还在"


def test_xmp_and_exif_travel_together_into_tiff(tmp_path: Path) -> None:
    """TIFF 上 XMP 与 EXIF **共用同一次写入**，不能一个挤掉另一个。

    TIFF 装 XMP 的唯一可行通道是把 700 号标签并进 EXIF 字节
    （``tiffinfo`` 那条路会把同时传的 ``exif=`` 整个挤掉）。既然是
    「重新打包 EXIF」，就必须证明打包之后原来的拍摄信息还在 ——
    否则这次改动是拿 EXIF 换了 XMP，用户损失一样大。
    """
    from compressors.pipeline import run_pipeline

    # 源文件**只用 exif= 这一个通道**写：TIFF 的 ``tiffinfo=`` 会把同时传的
    # ``exif=`` 整个挤掉（实测），两个一起传造出来的样张自己就没有拍摄信息，
    # 那样这条测试失败的原因就与被测代码无关了。真实相机写的 TIFF 也正是
    # 把 XMP 当作 IFD 里的 700 号标签、与其余拍摄信息放在同一个 IFD 里。
    exif = Image.Exif()
    exif[0x0110] = "CanaryModel"
    exif[0x010F] = "CanaryMake"
    exif[700] = _XMP_CANARY

    source = tmp_path / "both.tif"
    Image.new("RGB", (32, 24), (1, 2, 3)).save(source, format="TIFF", exif=exif)

    # 前提：样张自己两样都带上了。没有这一步，下面「都没丢」可能只是
    # 因为本来就没有，而一条永远为真的断言是没有价值的。
    assert xmp_of(source.read_bytes()) == _XMP_CANARY
    with Image.open(source) as check:
        assert check.getexif().get(0x0110) == "CanaryModel"

    result = run_pipeline(
        source, PipelineOptions(target_format="tiff", quality_value=95, metadata="keep")
    )

    assert xmp_of(result.data) == _XMP_CANARY, "TIFF 的结果里 XMP 不见了"
    with Image.open(io.BytesIO(result.data)) as back:
        got = back.getexif()
    assert got.get(0x0110) == "CanaryModel", "为了保住 XMP 把相机型号弄丢了"
    assert got.get(0x010F) == "CanaryMake", "为了保住 XMP 把厂商弄丢了"


def test_the_declared_xmp_formats_match_reality() -> None:
    """``XMP_FORMATS`` 与「Pillow 到底写不写得进去」必须逐格相等。

    这条是**机械对账**，不依赖任何样张：对每个输出格式，用同一张带 XMP
    的图片直接调编码器，看回读结果。表里写支持而实际写不进去（或反之）
    都算失败 —— 前者是假能力，后者让用户白白丢掉 XMP。

    单独成条的理由：上面那些用例是「按表选格式」，这一条是「按事实验表」。
    两者合起来，表既不可能漏也不可能多。
    """
    from compressors.encoder import (
        build_extra,
        encode_image,
        supports_xmp,
    )

    work = Image.open(io.BytesIO(xmp_png())).convert("RGB")
    for target in encodable_formats():
        extra = build_extra(target, exif=None, dpi=None, xmp=_XMP_CANARY)
        data = encode_image(work, target, 95, extra=extra)
        survived = xmp_of(data) == _XMP_CANARY
        assert survived is supports_xmp(target), (
            f"{target}：表里说 {'支持' if supports_xmp(target) else '不支持'}，"
            f"实测{'保住了' if survived else '没保住'}"
        )


def test_not_passing_xmp_writes_none_in_every_format() -> None:
    """``build_extra`` 没拿到 XMP 时，任何格式都不许凭空写一段进去。

    与 HEIC 那条「插件自己去图片对象上抓元数据」是同一类风险的另一半：
    保留要真的保留，不保留也要真的不保留。这里**故意**把 XMP 挂在待编码
    的图片对象上（插件「自己去抓」时看的就是那里），再按「没有 XMP」构造
    参数 —— 结果里不该出现它。
    """
    from compressors.encoder import build_extra, encode_image

    work = Image.open(io.BytesIO(xmp_png())).convert("RGB")
    work.info["xmp"] = _XMP_CANARY
    work.info[_PNG_XMP_KEY] = _XMP_CANARY.decode("latin-1")

    for target in encodable_formats():
        data = encode_image(
            work, target, 95, extra=build_extra(target, exif=None, dpi=None)
        )
        assert xmp_of(data) == b"", f"{target}：没给 XMP，结果里却有一段"


# ----------------------------------------------------------------------
# 读取器本身的边界
# ----------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw",
    [
        None,
        (1, 2, 3),          # 元组：Pillow 会给出这种形状的 tag，不是字符串内容
        b"\x00\x00\x00",    # 解出来全是 NUL
        "   \t\n  ",        # 全是空白
        "",
    ],
)
def test_unusable_values_become_none(raw: object) -> None:
    """读不出内容就返回 ``None``，绝不返回空串或 ``repr``。

    返回 ``repr((1, 2, 3))`` 会把一个内部结构当成「用户的信息」展示出去；
    返回空串则会让界面显示一个空白的格子 —— 两者都比 ``None`` 糟。
    """
    assert _clean(raw) is None


def test_control_characters_are_stripped_and_whitespace_collapsed() -> None:
    """EXIF 字符串常带 NUL 填充；直接下发会让响应体里出现 ``\\u0000``。"""
    assert _clean("Canon\x00\x00 EOS\n\tR6  ") == "Canon EOS R6"


def test_overlong_values_are_truncated_with_a_marker() -> None:
    """超长字段截断，并**留下一个看得见的省略号**。

    没有省略号的话，界面会把一段被砍掉的文字当成完整内容显示 ——
    用户以为型号就叫这个名字。
    """
    cleaned = _clean("x" * 500)
    assert cleaned is not None
    assert len(cleaned) == MAX_METADATA_CHARS + 1
    assert cleaned.endswith("…")


def test_read_metadata_returns_the_dataclass_not_a_dict(
    exif_jpeg_path: Path,
) -> None:
    """读取器返回带类型的对象；行列表是它的**视图**。

    行列表里的宽度是字符串 ``"800"``（给界面显示用），具名字段是整数
    ``800``。两者都在，但只有一个是真相 —— 调用方要算数就用整数那个，
    不必把展示用的字符串解析回去。
    """
    metadata = read_metadata(exif_jpeg_path)

    assert isinstance(metadata, ImageMetadata)
    assert metadata.width == 800
    assert metadata.height == 600
    assert metadata.get("width") is not None
    assert metadata.get("width").value == "800"
    assert metadata.get("nonexistent") is None


# ----------------------------------------------------------------------
# 上传通道：复用既有的校验，不为这个接口另写一套
# ----------------------------------------------------------------------

def test_a_corrupt_file_is_rejected_with_a_clean_message(
    client: TestClient,
) -> None:
    """假扩展名 / 坏内容走既有的三层校验，拿到的是一句干净的中文。"""
    response = post_metadata(client, "fake.jpg", b"definitely not a jpeg" * 20)

    assert response.status_code == 415
    body = response.json()
    assert body["error"]["code"] == "INVALID_FILE_TYPE"
    assert "暂不支持" in body["error"]["message"]
    # 不许漏出内部细节
    assert "Traceback" not in response.text
    assert "PIL" not in response.text


def test_svg_is_not_claimed_by_this_tool(client: TestClient) -> None:
    """SVG 被明确拒掉 —— 而不是收下来再悄悄给一份残缺的元数据。

    §三十四 那十一行里有三行（EXIF / 密度 / 色彩模式）对矢量图结构上
    不适用。收下它只会让「图片元数据」多一条自己都说不清的支路，
    所以这条能力在注册表里的 ``accepts`` 也不含 SVG（见下面那条契约测试）。
    """
    svg = b"<svg xmlns='http://www.w3.org/2000/svg' width='10' height='10'></svg>"
    response = post_metadata(client, "icon.svg", svg, "image/svg+xml")

    assert response.status_code == 415
    assert response.json()["error"]["code"] == "INVALID_FILE_TYPE"


def test_an_empty_file_is_rejected(client: TestClient) -> None:
    response = post_metadata(client, "empty.png", b"", "image/png")
    assert response.status_code in (400, 415)
    assert response.json()["error"]["message"]


def test_the_upload_directory_is_always_cleaned_up(
    client: TestClient, exif_jpeg: bytes
) -> None:
    """查看类接口**没有产物要留**，所以成功与失败都必须清干净。

    别的接口成功时目录会被登记进 job_store（结果是给用户下载的），
    这个不会 —— 它只产出一段 JSON。要是照抄那份 ``except`` 清理，
    每看一次元数据就会在临时目录里留下一份完整的用户原图。
    """
    root = Path(settings.TEMP_ROOT or tempfile.gettempdir())

    def leftovers() -> set[str]:
        if not root.is_dir():
            return set()
        return {item.name for item in root.iterdir() if item.name.startswith("filetools_")}

    before = leftovers()
    assert post_metadata(client, "photo.jpg", exif_jpeg).status_code == 200
    assert post_metadata(client, "bad.jpg", b"nope" * 100).status_code == 415

    assert leftovers() == before, "查看元数据留下了临时目录"


# ----------------------------------------------------------------------
# 注册表契约
# ----------------------------------------------------------------------

def test_the_tool_is_registered_as_a_read_only_operation() -> None:
    """它是 operation（有自己的端点、不进统一队列），不是 conversion。"""
    entry = registry.CAPABILITY_BY_ID["op.image-metadata"]

    assert entry.operation_type == "operation"
    assert entry.converter_key is None
    assert entry.endpoint == ENDPOINT
    assert entry.method == "POST"
    # 只读：不产出一份「一批同目标」的结果，也没有可下载的产物
    assert entry.supports_batch is False
    assert entry.supports_preview is False
    assert entry.options == (), "这个工具一个参数都没有 —— 也不该有"
    assert entry.note and "只查看" in entry.note, "说明里要写明它只读、不产文件"


def test_the_tool_is_discoverable_in_the_image_group(client: TestClient) -> None:
    """界面从能力目录里发现它，而不是自己硬编码一个入口（§五十二）。"""
    body = client.get("/api/conversion/capabilities").json()
    published = {item["id"]: item for item in body["operations"]}

    item = published["op.image-metadata"]
    assert item["category"] == "image"
    assert item["group"] == "image"
    assert item["available"] is True
    assert item["input_field"] == "file"
    assert item["options_schema"] is None


def test_the_declared_accepts_match_what_the_endpoint_really_takes() -> None:
    """``accepts`` 必须**恰好**是这个端点真的收得下的那些格式。

    这是一条防漂移的断言：``utils.validation`` 的图片白名单加了新格式、
    或者删掉一个旧格式时，``accepts`` 不跟着改，界面就会放行一个
    端点会拒的文件（或反过来，藏起一个其实能用的格式）。
    两边都由同一批常量派生，所以这里比的是两个集合，不是手抄的结果。
    """
    entry = registry.CAPABILITY_BY_ID["op.image-metadata"]
    accepted = {
        registry.EXTENSION_TO_SOURCE[ext] for ext in settings.ALLOWED_IMAGE_EXTENSIONS
    }

    assert set(entry.accepts) == accepted
    # 矢量图不在其中 —— 上面那条 415 测试是这条断言的运行时证据
    assert registry.SOURCE_SVG not in entry.accepts
