"""旧格式 Office（.doc / .xls / .ppt）转换的测试（第五阶段第 9 步）。

为什么单独一个文件：这三种格式的验证方式和 OOXML 不一样。OOXML 样张是
手写 zip 拼出来的，改一行就能造一个新样张；旧格式是 OLE2 二进制容器，
手写不出来，只能拿 ``scripts/make_office_fixtures.py`` 让 LibreOffice
**自己写一份真的**出来，再把那份真文件当样张。

所以这里验的是**往返**：LibreOffice 写出来的 .doc，再交给 LibreOffice 转回 PDF。
这能证明「旧格式这条路是通的」，但**证明不了**「用户手上任意一份 Word 97 文件
都能转」—— 真实的旧文件里可能有宏、有嵌入对象、有 OLE 链接。
这一点写在 README 里，不在这里假装覆盖到了。

样张不存在时逐条 skip：样张是生成物，缺了不该让别的测试跟着红。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.conftest import (
    LEGACY_MARKERS,
    download_text,
    legacy_sample,
    office_files,
    requires_soffice,
)

#: (扩展名, 接口路径片段, 上传时的文件名, 这份样张属于哪个类别)
LEGACY_CASES = [
    ("doc", "word-to-pdf", "老文档.doc", "Word"),
    ("xls", "excel-to-pdf", "老表格.xls", "Excel"),
    ("ppt", "powerpoint-to-pdf", "老幻灯片.ppt", "PowerPoint"),
]

_IDS = [case[0] for case in LEGACY_CASES]


def _sample(extension: str):
    path = legacy_sample(extension)
    if not path.is_file():
        pytest.skip(
            f"缺少 tests/fixtures/sample.{extension}"
            "（跑 scripts/make_office_fixtures.py 生成）"
        )
    return path


@requires_soffice
@pytest.mark.parametrize(("extension", "endpoint", "filename", "label"), LEGACY_CASES, ids=_IDS)
def test_legacy_format_converts_and_keeps_its_text(
    client: TestClient, extension: str, endpoint: str, filename: str, label: str
) -> None:
    """旧格式要真的转出内容来，不能只是一页白纸。

    只断言 ``page_count >= 1`` 是不够的：LibreOffice 碰到打不开的文件时
    同样会吐出一页空白 PDF，接口也照样返回 200。所以这里读的是
    **下载到的 PDF 里的实际文字**。
    """
    sample = _sample(extension)
    response = client.post(
        f"/api/office/{endpoint}", files=office_files((filename, sample.read_bytes()))
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["filename"] == filename.rsplit(".", 1)[0] + ".pdf"
    assert payload["page_count"] >= 1
    # original_size 是上传的那份 .doc/.xls/.ppt 的大小
    assert payload["original_size"] == sample.stat().st_size
    assert any("页" in note for note in payload["notes"])

    assert LEGACY_MARKERS[extension] in download_text(client, payload), (
        f"{label} 旧格式转出来的 PDF 里找不到样张正文，可能是转出了一页白纸"
    )


@requires_soffice
def test_legacy_doc_uploaded_to_the_excel_endpoint_is_rejected(client: TestClient) -> None:
    """旧格式之间改名也要拦住。

    OOXML 能靠 zip 内部结构分辨，OLE2 的头三种格式一模一样 ——
    所以 loader 看的是**容器里的流名**（Word 是 WordDocument、
    Excel 是 Workbook、PowerPoint 是 PowerPoint Document）。
    否则一份 .doc 改名叫 .xls 会一路走到转换器，
    用户看到的是「文件转换失败」，而不是「你传错了地方」。
    """
    sample = _sample("doc")
    response = client.post(
        "/api/office/excel-to-pdf",
        files=office_files(("冒名表格.xls", sample.read_bytes())),
    )

    assert response.status_code == 415
    error = response.json()["error"]
    assert error["code"] == "INVALID_FILE_TYPE"
    assert "与扩展名不一致" in error["message"]
