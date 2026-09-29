"""PDF 操作链路里的文件名安全（第九阶段第 12 步补的安全回归）。

背景：图片/统一转换那条路上的穿越文件名早就有测试钉着
（``test_conversion_api.test_path_traversal_in_filename_is_neutralised``、
``test_batch.test_zip_names_are_flat_no_path_traversal``），
但 **``/api/pdf/*`` 这一组操作从来没有被这么测过** —— 上传名会一路被带到
结果文件名、ZIP 名、ZIP 包内条目名和 ``Content-Disposition`` 头上。
这里把整条链路逐段钉死，任何一段哪天开始信任浏览器给的名字都会红。

两件事分开看：

* **落盘与下载用的名字必须清洗** —— 这是安全要求，本文件主体；
* **上传回执里的 ``filename`` 原样返回** —— 只用于界面展示，令牌是随机
  ``input_id``，服务器落盘用的是 ``<随机 token>.upload``，所以它既不是路径
  也不是下载名。这是第三阶段就定下的响应形状（§五十九 要求保持兼容），
  这里如实钉住，免得日后有人「顺手清洗一下」却不知道动了对外契约。
"""

from __future__ import annotations

import io
import tempfile
import zipfile
from pathlib import PurePosixPath, PureWindowsPath

import pytest
from fastapi.testclient import TestClient

from tests.conftest import build_labeled_pdf

#: 各种想往外跑的写法。Windows 与 POSIX 的分隔符都要覆盖：
#: 服务器跑在 Windows 上，但 ``Path`` 在两种写法下的行为必须一样安全。
EVIL_NAMES = (
    "../../evil.pdf",
    "..\\..\\windows.pdf",
    "....//....//evil.pdf",
    "..%2f..%2fevil.pdf",
    "C:\\Windows\\Temp\\evil.pdf",
    "/etc/passwd.pdf",
)


def _assert_safe(name: str, where: str) -> None:
    """名字必须是**单个**安全成分：不跨目录、不指向上级、不是隐藏名。

    刻意**不**断言「不含 ``..`` 子串」—— ``%`` 会被清洗成 ``_``，
    于是 ``..%2f..%2fevil.pdf`` 变成 ``2f.._2fevil_parts.zip``，
    中间那两个点看着像上跳，其实只是普通字符：危险的从来不是 ``..``
    这个子串，而是它**作为一个路径成分**出现。所以这里按成分判。
    """
    assert name, f"{where} 是空的"
    assert "/" not in name, f"{where} 含路径分隔符：{name!r}"
    assert "\\" not in name, f"{where} 含反斜杠：{name!r}"
    assert name not in (".", ".."), f"{where} 就是上级目录：{name!r}"
    assert not name.startswith("."), f"{where} 是隐藏名：{name!r}"
    assert tempfile.gettempdir() not in name, f"{where} 泄露了临时目录：{name!r}"


def _download_name(client: TestClient, result: dict) -> str:
    """下载响应头里真正会变成用户文件名的那一段。"""
    response = client.get(result["download_url"])
    assert response.status_code == 200, response.text
    return response.headers.get("content-disposition", "")


def _receipt_candidates(evil: str) -> set[str]:
    """上传回执里**合规**的文件名写法。

    multipart 解析器（以及它背后的 ``email`` 参数解析）对 ``filename``
    参数有一条约定：取基名。于是同一个 ``C:\\Windows\\Temp\\evil.pdf``
    可能以原样送到应用层，也可能在解析时就被砍成 ``evil.pdf``。
    这不是本项目的清洗逻辑，两种都属于既有行为，所以两种都接受 ——
    真正要钉死的是「它不会变成路径或下载名」，见下面各条断言。
    """
    return {evil, PureWindowsPath(evil).name, PurePosixPath(evil).name}


def _upload(client: TestClient, data: bytes, name: str) -> dict:
    response = client.post(
        "/api/pdf/upload",
        files={"file": (name, io.BytesIO(data), "application/pdf")},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _check_result_names(client: TestClient, result: dict) -> None:
    """结果文件名、ZIP 名、包内条目名、下载头 —— 一处都不许漏。"""
    _assert_safe(result["filename"], "结果文件名")
    if result.get("archive_filename"):
        _assert_safe(result["archive_filename"], "ZIP 文件名")
    for item in result["files"]:
        _assert_safe(item["filename"], "结果文件列表里的文件名")

    disposition = _download_name(client, result)
    assert "\\" not in disposition, disposition
    assert tempfile.gettempdir() not in disposition, disposition
    # 头里出现「文件名带路径」的写法即视为失败：``filename="a/b"`` 或 ``filename="../x"``
    lowered = disposition.lower()
    assert 'filename="../' not in lowered, disposition
    assert 'filename="..\\' not in lowered, disposition
    assert "filename*=utf-8''.." not in lowered, disposition


@pytest.mark.parametrize("evil", EVIL_NAMES)
def test_upload_receipt_echoes_the_name_but_nothing_downstream_trusts_it(
    client: TestClient, evil: str
) -> None:
    """回执原样回显（展示用），而结果名一律是清洗过的安全名。"""
    uploaded = _upload(client, build_labeled_pdf("报告", 3), evil)
    # 回执：原样返回（第三阶段的响应形状，不改）。多分支断言是必要的 ——
    # multipart 解析器按「文件名取基名」的惯例会先砍掉目录部分，
    # 所以 ``C:\Windows\Temp\evil.pdf`` 到应用层时已经是 ``evil.pdf`` 了，
    # 而 ``../../evil.pdf`` 是原样送到的。两种都合规，重要的是下面那条。
    assert uploaded["filename"] in _receipt_candidates(evil), uploaded["filename"]
    # 令牌是随机的，跟文件名无关，也不含路径成分
    _assert_safe(uploaded["input_id"], "input_id")

    input_id = uploaded["input_id"]

    split = client.post("/api/pdf/split", data={"input_id": input_id, "mode": "every"})
    assert split.status_code == 200, split.text
    _check_result_names(client, split.json())


@pytest.mark.parametrize("name", [".", "..", "...", "无扩展名", "evil.pdf.txt"])
def test_names_that_cannot_be_pdf_are_rejected_before_anything_else(
    client: TestClient, name: str
) -> None:
    """扩展名白名单在最前面 —— ``.`` / ``..`` 这类名字根本进不来。

    注意这不是「文件名清洗」的功劳，而是**扩展名校验**先拒了它们：
    一个连 ``.pdf`` 结尾都不是的文件，没有理由再去猜它是不是 PDF。
    """
    response = client.post(
        "/api/pdf/upload",
        files={"file": (name, io.BytesIO(build_labeled_pdf("报告", 2)), "application/pdf")},
    )
    assert response.status_code == 415, (name, response.text)
    body = response.json()["error"]
    assert body["code"] == "INVALID_FILE_TYPE"
    assert "/" not in body["message"] and "\\" not in body["message"]
    assert tempfile.gettempdir() not in body["message"]


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        ("split", {"mode": "selected", "pages": "1,3"}),
        ("extract-pages", {"pages": "1,3"}),
        ("delete-pages", {"pages": "2"}),
        ("compress", {"level": "light"}),
    ],
)
def test_pdf_operations_never_return_a_traversing_name(
    client: TestClient, path: str, payload: dict
) -> None:
    """四个「上传一次、反复操作」的接口都不把原始名带进结果名。"""
    uploaded = _upload(client, build_labeled_pdf("报告", 4), "../../evil.pdf")

    response = client.post(f"/api/pdf/{path}", data={"input_id": uploaded["input_id"], **payload})
    assert response.status_code == 200, response.text
    _check_result_names(client, response.json())


def test_split_zip_entries_are_flat(client: TestClient) -> None:
    """ZIP 包内条目名必须平铺 —— 解压时不能跑到目标目录外面去。"""
    uploaded = _upload(client, build_labeled_pdf("报告", 3), "../../evil.pdf")
    response = client.post("/api/pdf/split", data={"input_id": uploaded["input_id"], "mode": "every"})
    assert response.status_code == 200, response.text

    body = response.json()
    assert body["archived"] is True
    assert body["archive_filename"] == "evil_parts.zip"

    download = client.get(body["download_url"])
    with zipfile.ZipFile(io.BytesIO(download.content)) as archive:
        names = archive.namelist()
    assert names, "ZIP 是空的"
    for name in names:
        _assert_safe(name, "ZIP 包内条目名")


def test_merge_ignores_the_uploaded_names(client: TestClient) -> None:
    """合并结果是固定名 —— 输入名连碰都碰不到它。"""
    pdf = build_labeled_pdf("报告", 2)
    response = client.post(
        "/api/pdf/merge",
        files=[
            ("files", ("../../一.pdf", io.BytesIO(pdf), "application/pdf")),
            ("files", ("..\\..\\二.pdf", io.BytesIO(pdf), "application/pdf")),
        ],
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["filename"] == "merged.pdf"
    _check_result_names(client, body)


def test_from_images_sanitizes_every_uploaded_name(client: TestClient) -> None:
    """图片合成 PDF 用的是第一张图的名字，同样要清洗。"""
    from tests.conftest import build_image_bytes

    response = client.post(
        "/api/pdf/from-images",
        files=[
            ("files", ("../../evil.png", io.BytesIO(build_image_bytes(80, 60, "PNG")), "image/png")),
            ("files", ("..\\..\\evil2.png", io.BytesIO(build_image_bytes(80, 60, "PNG")), "image/png")),
        ],
        data={"page_size": "a4"},
    )
    assert response.status_code == 200, response.text
    _check_result_names(client, response.json())


def test_absolute_path_name_cannot_reach_outside(client: TestClient) -> None:
    """绝对路径写法同样被清洗 —— 不能写到系统目录去。"""
    uploaded = _upload(client, build_labeled_pdf("报告", 2), "C:\\Windows\\Temp\\evil.pdf")
    response = client.post(
        "/api/pdf/compress", data={"input_id": uploaded["input_id"], "level": "light"}
    )
    assert response.status_code == 200, response.text

    body = response.json()
    _check_result_names(client, body)
    assert body["filename"].endswith(".pdf")
    assert ":" not in body["filename"], body["filename"]
