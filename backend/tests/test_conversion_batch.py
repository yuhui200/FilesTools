"""统一转换中心的批量行为（第七阶段 §五 / §六 / §十五 / §十六 / §三十八）。

批量是这一阶段真正的重点，所以这里不看「有没有 200」，而是逐条验规格里的硬要求：

* 单个失败不影响整批（§五）；
* 成功 1 个直接给文件、≥2 个才打包，**ZIP 用标准库真解压**验证数量与文件名；
* 每个下载令牌只拥有自己的子目录 —— 下载其中一个不能毁掉别人的结果；
* ZIP 里不能出现 ``../``（§三十七）；
* 取消是协作式的，**正在跑的项要如实显示 cancelling**（§十五）；
* 重试只针对失败项、每项最多一次，且不做自动重试（§十六）。
"""

from __future__ import annotations

import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from config import settings
from services import office_converter
from utils.errors import ErrorCode
from tests.conftest import (
    build_docx_bytes,
    build_image_bytes,
    build_labeled_pdf,
    conversion_task,
    image_files,
    office_files,
    pdf_files,
    run_conversion,
    submit_conversion,
    wait_conversion,
)

ENDPOINT = "/api/conversion/tasks"

#: 取消测试用的批次大小。必须大于 ``QUEUE_WORKERS``：
#: 只有「确实还在排队」的项才能立刻取消，这正是要验的东西。
CANCEL_BATCH_SIZE = settings.QUEUE_WORKERS + 1


def item_of(snapshot: dict, index: int) -> dict:
    return conversion_task(snapshot, index)


def fetch_zip(client: TestClient, url: str) -> zipfile.ZipFile:
    """下载并**真的解开**整批结果。"""
    response = client.get(url)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/zip"
    archive = zipfile.ZipFile(io.BytesIO(response.content))
    assert archive.testzip() is None, "ZIP 本身是坏的"
    return archive


# ----------------------------------------------------------------------
# 单个成功 vs 多个成功（§六）
# ----------------------------------------------------------------------

def test_single_result_is_not_wrapped_in_a_zip(client: TestClient) -> None:
    """只有一个结果时直接给那个文件 —— 单文件套一层 ZIP 只会让人多解压一次。"""
    snapshot = run_conversion(
        client,
        files=image_files(("photo.png", build_image_bytes(160, 120, "PNG"))),
        target_type="jpg",
    )

    assert snapshot["status"] == "completed"
    result = snapshot["result"]
    assert result["archived"] is False
    assert result["archive_filename"] is None
    assert result["items"][0]["filename"] == "photo.jpg"

    response = client.get(result["download_url"])
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/jpeg"
    with Image.open(io.BytesIO(response.content)) as image:
        assert image.format == "JPEG"


def test_two_files_are_archived_and_extractable(client: TestClient) -> None:
    png = build_image_bytes(200, 150, "PNG")
    snapshot = run_conversion(
        client,
        files=image_files(("a.png", png), ("b.png", png)),
        target_type="jpg",
    )

    assert snapshot["status"] == "completed"
    result = snapshot["result"]
    assert result["archived"] is True
    assert result["archive_filename"] == "filetools-converted.zip"

    with fetch_zip(client, result["download_url"]) as archive:
        assert sorted(archive.namelist()) == ["a.jpg", "b.jpg"]
        with Image.open(io.BytesIO(archive.read("a.jpg"))) as image:
            assert image.format == "JPEG"
            assert image.size == (200, 150)


def test_ten_files_all_succeed(client: TestClient) -> None:
    """十个文件：一个都不能少，ZIP 里要正好十个。"""
    count = 10
    files = tuple(
        (f"photo-{index}.png", build_image_bytes(80 + index, 60, "PNG"))
        for index in range(count)
    )
    snapshot = run_conversion(client, files=image_files(*files), target_type="jpg")

    assert snapshot["status"] == "completed"
    assert snapshot["completed"] == count
    assert snapshot["failed"] == 0
    assert snapshot["progress"] == 100.0
    assert [task["source_filename"] for task in snapshot["tasks"]] == [
        f"photo-{index}.png" for index in range(count)
    ]

    with fetch_zip(client, snapshot["result"]["download_url"]) as archive:
        assert sorted(archive.namelist()) == sorted(
            f"photo-{index}.jpg" for index in range(count)
        )


def test_duplicate_names_do_not_overwrite_each_other(client: TestClient) -> None:
    """两个不同来源的同名文件转出来会同名，ZIP 里必须两份都在。"""
    snapshot = run_conversion(
        client,
        files=image_files(
            ("报告.png", build_image_bytes(120, 90, "PNG")),
            ("报告.png", build_image_bytes(240, 180, "PNG")),
        ),
        target_type="jpg",
    )

    assert snapshot["status"] == "completed"
    with fetch_zip(client, snapshot["result"]["download_url"]) as archive:
        names = sorted(archive.namelist())
        assert names == ["报告-2.jpg", "报告.jpg"]
        # 两份内容确实不同（否则就是「其中一个被覆盖了」）
        assert archive.read("报告.jpg") != archive.read("报告-2.jpg")


# ----------------------------------------------------------------------
# 单个失败不影响整批（§五）
# ----------------------------------------------------------------------

def test_mixed_batch_keeps_the_good_ones(client: TestClient) -> None:
    """一个批次里混进转不了的文件：它自己失败，同批其它文件照常出结果。"""
    png = build_image_bytes(120, 90, "PNG")
    snapshot = run_conversion(
        client,
        files=image_files(
            ("one.png", png),
            ("doc.pdf", build_labeled_pdf("p", 1)),  # PDF 转不了 JPG
            ("two.png", png),
        ),
        target_type="jpg",
    )

    assert snapshot["status"] == "completed", snapshot["error"]
    assert snapshot["completed"] == 2
    assert snapshot["failed"] == 1

    assert item_of(snapshot, 0)["status"] == "completed"
    assert item_of(snapshot, 1)["status"] == "failed"
    assert item_of(snapshot, 2)["status"] == "completed"
    assert item_of(snapshot, 1)["error_code"] == ErrorCode.UNSUPPORTED_CONVERSION
    # 源类型由服务端判定，不采信客户端的说法
    assert item_of(snapshot, 1)["source_type"] == "pdf"

    # ZIP 里只有能转的那两个
    with fetch_zip(client, snapshot["result"]["download_url"]) as archive:
        assert sorted(archive.namelist()) == ["one.jpg", "two.jpg"]


def test_one_batch_is_one_target_and_one_set_of_options(client: TestClient) -> None:
    """同一批 = 同一个目标 + 同一套参数（第十阶段 A §四十二–§四十四）。

    这不是实现细节，是**批量这个模型的边界**：一批结果要能并排比较、能
    一起下载，前提就是它们按同一个目标、同一套参数产出。源格式可以混
    （png / jpg / bmp 各来一个），目标和参数不行。

    结构上它是恒真的 —— 目标与参数各是**一个**表单字段，不在每个文件上。
    所以这条测试守的不是「今天能不能」，而是将来有人为了「每个文件单独
    设参数」把字段拆成数组时，这里会先红：那种改动会让结果卡上的对比
    失去意义，也会让「一批一个 ZIP」的命名与去重规则失去前提。
    """
    snapshot = run_conversion(
        client,
        files=image_files(
            ("甲.png", build_image_bytes(120, 90, "PNG")),
            ("乙.webp", build_image_bytes(120, 90, "WEBP")),
            ("丙.bmp", build_image_bytes(120, 90, "BMP")),
        ),
        target_type="jpg",
        options=json.dumps({"quality": 70}),
    )

    assert snapshot["status"] == "completed", snapshot["error"]
    assert snapshot["completed"] == 3

    tasks = [item_of(snapshot, index) for index in range(3)]
    # 目标只有一个值 —— 三项都是它
    assert {task["target_type"] for task in tasks} == {"jpg"}
    # 源格式确实不同，否则这条测试等于只跑了一次
    assert {task["source_type"] for task in tasks} == {"png", "webp", "bmp"}
    # 各自命中的能力按源分别对应，目标侧一致（ID 的尾巴都是 -to-jpg）
    assert {task["conversion_id"] for task in tasks} == {
        "image.png-to-jpg",
        "image.webp-to-jpg",
        "image.bmp-to-jpg",
    }
    # 参数逐项相同（不是一个有 dpi 一个没有）
    assert {json.dumps(task["options"], sort_keys=True) for task in tasks} == {
        json.dumps({"quality": 70}, sort_keys=True)
    }

    with fetch_zip(client, snapshot["result"]["download_url"]) as archive:
        assert sorted(archive.namelist()) == ["丙.jpg", "乙.jpg", "甲.jpg"]


def test_broken_file_is_rejected_at_submit_and_not_retryable(
    client: TestClient,
) -> None:
    """坏文件在**提交时**就被识破，不是排到队才失败。

    这也是「重试」这条规矩的另一半（§十六）：重试只对**服务器侧的偶发问题**
    有意义。一个本身就损坏的文件，重试一百次也是同样的结果 —— 它在提交定性
    阶段就被记成失败，源文件一并丢掉，``can_retry`` 因此是 False。
    """
    snapshot = run_conversion(
        client,
        files=image_files(
            ("good.png", build_image_bytes(100, 80, "PNG")),
            ("broken.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 200),
        ),
        target_type="jpg",
    )

    assert snapshot["status"] == "completed"
    broken = item_of(snapshot, 1)
    assert broken["status"] == "failed"
    assert broken["error_code"] in (
        ErrorCode.CORRUPTED_FILE,
        ErrorCode.INVALID_FILE_TYPE,
    )
    assert broken["error_message"], "失败的文件必须给出原因"
    assert broken["can_retry"] is False, "文件本身不行，重试没有意义"
    assert snapshot["result"]["items"][0]["filename"] == "good.jpg"

    retry = client.post(f"{ENDPOINT}/{broken['task_id']}/retry")
    assert retry.status_code == 409


def test_all_failed_batch_reports_the_reason(client: TestClient) -> None:
    """全部失败：整批标记失败，并给出第一个失败原因。"""
    snapshot = run_conversion(
        client,
        files=pdf_files(
            ("one.pdf", build_labeled_pdf("p", 1)),
            ("two.pdf", build_labeled_pdf("q", 1)),
        ),
        target_type="jpg",
    )

    assert snapshot["status"] == "failed"
    assert snapshot["failed"] == 2
    assert snapshot["completed"] == 0
    assert snapshot["result"] is None
    assert snapshot["error"]["code"] == ErrorCode.UNSUPPORTED_CONVERSION
    assert snapshot["error"]["message"]


# ----------------------------------------------------------------------
# 每个下载令牌只拥有自己的子目录（§六）
# ----------------------------------------------------------------------

def test_downloading_one_item_keeps_the_others_alive(client: TestClient) -> None:
    """下载是「用后即删」的，但删的只能是它自己那一份。

    这是「每个令牌独占 out/<n>/」这条设计的直接后果：如果所有结果共用一个
    目录，下载任意一个都会把别人的结果一起删掉，用户点第二个就报 404。
    """
    png = build_image_bytes(140, 100, "PNG")
    snapshot = run_conversion(
        client,
        files=image_files(("a.png", png), ("b.png", png), ("c.png", png)),
        target_type="jpg",
    )
    assert snapshot["status"] == "completed"

    first = item_of(snapshot, 0)["result"]["download_url"]
    assert client.get(first).status_code == 200
    assert client.get(first).status_code == 404, "同一个令牌只能下载一次"

    for index in (1, 2):
        url = item_of(snapshot, index)["result"]["download_url"]
        assert client.get(url).status_code == 200, f"第 {index} 项被连累了"

    # 别人下载过之后，整批 ZIP 仍然完好（它自己那份已经拷进包里了）
    with fetch_zip(client, snapshot["result"]["download_url"]) as archive:
        assert sorted(archive.namelist()) == ["a.jpg", "b.jpg", "c.jpg"]


def test_zip_never_contains_parent_paths(client: TestClient) -> None:
    """§三十七：ZIP 成员名不能带 ``../``，解压时不能落到上级目录。"""
    png = build_image_bytes(90, 70, "PNG")
    snapshot = run_conversion(
        client,
        files=image_files(("../../evil.png", png), ("../escape.png", png)),
        target_type="jpg",
    )

    assert snapshot["status"] == "completed"
    with fetch_zip(client, snapshot["result"]["download_url"]) as archive:
        names = archive.namelist()
        assert names, "ZIP 不该是空的"
        for name in names:
            assert "/" not in name and "\\" not in name, name
            assert ".." not in name, name
        assert sorted(names) == ["escape.jpg", "evil.jpg"]


# ----------------------------------------------------------------------
# 取消（§十五：协作式，且如实显示）
# ----------------------------------------------------------------------

def test_cancel_stops_queued_items_and_is_honest_about_running_ones(
    client: TestClient,
) -> None:
    """取消必须**如实**：排队的立刻停，正在跑的显示「正在取消」。

    正在跑的那个转的是 LibreOffice 里的线程，杀不掉；假装它已经停了
    就是撒谎（§十五）。这里同时验两件事：排队项真的被取消，
    在跑项的状态是 ``cancelling`` 而不是 ``cancelled``。
    """
    files = tuple(
        (f"doc-{index}.docx", build_docx_bytes(f"DOC{index}"))
        for index in range(CANCEL_BATCH_SIZE)
    )
    response = submit_conversion(
        client, files=office_files(*files, field="files"), target_type="pdf"
    )
    assert response.status_code == 202
    batch_id = response.json()["batch_id"]

    # 提交完立刻取消：此时至少有 CANCEL_BATCH_SIZE - QUEUE_WORKERS 项还在排队
    cancelled = client.post(f"{ENDPOINT}/{batch_id}/cancel")
    assert cancelled.status_code == 200
    body = cancelled.json()

    assert body["cancelling"] is True
    assert body["completed"] == 0
    assert body["cancelled"] >= 1, "还在排队的项必须立刻取消"
    assert body["queued"] == 0, "取消之后不该还有项在排队"

    running = [task for task in body["tasks"] if task["status"] == "cancelling"]
    assert running, "已经在跑的项必须如实显示 cancelling，不能假装已经停了"
    for task in running:
        assert task["status"] != "cancelled"

    snapshot = wait_conversion(client, batch_id, timeout=120.0)

    assert snapshot["status"] == "cancelled", snapshot["error"]
    assert snapshot["completed"] == 0
    assert snapshot["cancelled"] == CANCEL_BATCH_SIZE
    assert snapshot["failed"] == 0
    assert snapshot["result"] is None
    # 取消是用户自己按的，不该报成「服务器出错」
    assert snapshot["error"] is None
    # 已经在跑的那个跑完之后结果被丢弃，状态改判为已取消
    for task in snapshot["tasks"]:
        assert task["status"] == "cancelled"
        assert task["error_code"] is None
        assert task["result"] is None


def test_cancel_is_idempotent_and_unknown_batch_is_404(client: TestClient) -> None:
    png = build_image_bytes(60, 60, "PNG")
    response = submit_conversion(
        client, files=image_files(("a.png", png)), target_type="jpg"
    )
    batch_id = response.json()["batch_id"]
    wait_conversion(client, batch_id)

    # 已经收尾的批次：原样返回，不报错也不改状态
    again = client.post(f"{ENDPOINT}/{batch_id}/cancel")
    assert again.status_code == 200
    assert again.json()["status"] == "completed"

    assert client.post(f"{ENDPOINT}/nope/cancel").status_code == 404


def test_cancelled_item_cannot_be_retried(client: TestClient) -> None:
    """用户自己不要的东西，不能靠重试又转起来。"""
    files = tuple(
        (f"doc-{index}.docx", build_docx_bytes(f"DOC{index}"))
        for index in range(CANCEL_BATCH_SIZE)
    )
    response = submit_conversion(
        client, files=office_files(*files, field="files"), target_type="pdf"
    )
    batch_id = response.json()["batch_id"]
    assert client.post(f"{ENDPOINT}/{batch_id}/cancel").status_code == 200
    snapshot = wait_conversion(client, batch_id, timeout=120.0)
    assert snapshot["status"] == "cancelled"

    task_id = item_of(snapshot, 0)["task_id"]
    retry = client.post(f"{ENDPOINT}/{task_id}/retry")
    assert retry.status_code == 409
    assert retry.json()["error"]["code"] == ErrorCode.TASK_NOT_RETRYABLE


# ----------------------------------------------------------------------
# 重试（§十六：只重试失败项，每项最多一次）
# ----------------------------------------------------------------------

def test_retry_only_accepts_failed_items(client: TestClient) -> None:
    png = build_image_bytes(80, 60, "PNG")
    snapshot = run_conversion(
        client,
        files=image_files(("a.png", png), ("b.png", png)),
        target_type="jpg",
    )
    assert snapshot["status"] == "completed"

    for index in (0, 1):
        task = item_of(snapshot, index)
        assert task["can_retry"] is False, "成功的项不该显示重试按钮"
        response = client.post(f"{ENDPOINT}/{task['task_id']}/retry")
        assert response.status_code == 409
        assert response.json()["error"]["code"] == ErrorCode.TASK_NOT_RETRYABLE


def test_retry_rejects_malformed_task_id(client: TestClient) -> None:
    for bad in ("nonsense", ":3", "abc:", "abc:x", "abc:-1"):
        response = client.post(f"{ENDPOINT}/{bad}/retry")
        assert response.status_code in (400, 404), bad
        assert response.json()["error"]["code"] in (
            ErrorCode.INVALID_REQUEST,
            ErrorCode.TASK_NOT_FOUND,
        )


def test_retry_after_a_transient_failure_succeeds(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """重试的正经用途：服务器侧的偶发问题（这里是转换组件被占用）。

    故意**不**给转换函数打桩：把 ``OFFICE_LOCK_WAIT_SECONDS`` 调到 1 秒并
    自己握住转换锁，失败走的是真实路径 —— ``ConverterUnavailableError``
    由真实的锁等待超时抛出，接着真的重试一次、真的转出 PDF。
    """
    monkeypatch.setattr(settings, "OFFICE_LOCK_WAIT_SECONDS", 1)
    lock = office_converter._LAUNCHER_LOCK
    assert lock.acquire(timeout=5), "拿不到转换锁，说明有别的转换在跑"
    try:
        snapshot = run_conversion(
            client,
            files=office_files(("report.docx", build_docx_bytes("RETRY")), field="files"),
            target_type="pdf",
        )
    finally:
        lock.release()

    assert snapshot["status"] == "failed"
    task = item_of(snapshot, 0)
    assert task["status"] == "failed"
    assert task["error_code"] == ErrorCode.PROCESSING_FAILED
    # 是「组件忙」这个可预期的失败，不是兜底的「处理失败，请重试」
    assert "转换" in task["error_message"]
    assert task["can_retry"] is True
    assert task["retry_count"] == 0
    assert snapshot["error"]["code"] == ErrorCode.PROCESSING_FAILED

    # 锁已经放开，重试必须真的转成功
    retried = client.post(f"{ENDPOINT}/{task['task_id']}/retry")
    assert retried.status_code == 200, retried.text
    assert retried.json()["cancelling"] is False

    final = wait_conversion(client, snapshot["batch_id"], timeout=120.0)
    assert final["status"] == "completed", final["error"]
    done = item_of(final, 0)
    assert done["status"] == "completed"
    assert done["retry_count"] == 1
    assert done["error_code"] is None
    assert done["result"]["filename"] == "report.pdf"

    # 结果真的是能打开的 PDF
    response = client.get(done["result"]["download_url"])
    assert response.status_code == 200
    assert response.content.startswith(b"%PDF")

    # 每项最多重试一次：再点就 409（上限在服务端，不靠前端自觉）
    assert done["can_retry"] is False
    again = client.post(f"{ENDPOINT}/{done['task_id']}/retry")
    assert again.status_code == 409
    assert again.json()["error"]["code"] == ErrorCode.TASK_NOT_RETRYABLE


def test_retry_rebuilds_the_archive_and_spares_the_single_result(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """重试会改变成功集合，旧 ZIP 必须作废重建，但**别人那份单文件结果不能碰**。

    这一批里：图片转 PDF 成功（不碰 LibreOffice），文档转 PDF 因为锁被占住而
    失败。只有一个成功 → 整批入口就是那张图片自己的令牌（还没有 ZIP）。
    放开锁重试文档之后有两个成功了 → 重新打 ZIP，而那张图片的令牌必须
    仍然能下载 —— 这正是 ``retry()`` 只作废 ``archive_job_id`` 的原因。
    """
    monkeypatch.setattr(settings, "OFFICE_LOCK_WAIT_SECONDS", 1)
    lock = office_converter._LAUNCHER_LOCK
    assert lock.acquire(timeout=5)
    try:
        snapshot = run_conversion(
            client,
            files=[
                *image_files(("cover.png", build_image_bytes(150, 110, "PNG"))),
                *office_files(("report.docx", build_docx_bytes("REBUILD")), field="files"),
            ],
            target_type="pdf",
        )
    finally:
        lock.release()

    assert snapshot["status"] == "completed"
    assert snapshot["completed"] == 1
    assert snapshot["failed"] == 1
    assert snapshot["result"]["archived"] is False

    image = item_of(snapshot, 0)
    document = item_of(snapshot, 1)
    assert image["status"] == "completed"
    assert image["source_filename"] == "cover.png"
    assert document["error_code"] == ErrorCode.PROCESSING_FAILED
    assert document["can_retry"] is True
    single_url = image["result"]["download_url"]

    retried = client.post(f"{ENDPOINT}/{document['task_id']}/retry")
    assert retried.status_code == 200, retried.text

    final = wait_conversion(client, snapshot["batch_id"], timeout=120.0)
    assert final["status"] == "completed", final["error"]
    assert final["completed"] == 2
    assert final["result"]["archived"] is True
    assert final["result"]["archive_filename"] == "filetools-converted.zip"

    with fetch_zip(client, final["result"]["download_url"]) as archive:
        assert sorted(archive.namelist()) == ["cover.pdf", "report.pdf"]

    # 重试没有连累那张图片：它自己的令牌仍然能用
    assert client.get(single_url).status_code == 200, "重试把别人那份结果删掉了"
    assert item_of(final, 0)["result"]["download_url"] == single_url
