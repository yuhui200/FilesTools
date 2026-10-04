"""文档转换接口的测试（第五阶段）。

重点是**真的转换成功**：断言下载到的 PDF 里能读出原文，
而不是只看接口返回 200 —— 转出一张白纸同样不报错。

另外把两条安全约定钉住：
* 缺组件时必须在**落盘之前**就拒绝，不为一个注定失败的请求写 50 MB 进磁盘；
* 出错响应里不能出现堆栈、服务器路径、临时目录名（§十三）。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from config import settings
from services import office_converter
from tests.conftest import (
    HIDDEN_SHEET_MARKER,
    HIDDEN_SLIDE_MARKER,
    OFFICE_MARKER,
    build_docx_bytes,
    build_pptx_bytes,
    build_xlsx_bytes,
    download_text,
    office_files,
    open_downloaded_pdf,
    requires_soffice,
    zip_bytes,
)

WORD = "/api/office/word-to-pdf"
EXCEL = "/api/office/excel-to-pdf"
POWERPOINT = "/api/office/powerpoint-to-pdf"
TXT = "/api/office/txt-to-pdf"

#: TXT 样张里的标记，用来断言「下载到的 PDF 里真的能读出原文」
TXT_MARKER = "TXT-测试标记-42"


def build_txt_bytes(chars: int = 3_000, *, encoding: str = "utf-8") -> bytes:
    """一份够长到跨页的纯文本，用指定编码编出来。"""
    body = "第{i}段：这是一份用于测试的纯文本文件。\n"
    text = TXT_MARKER + "\n" + (body * (chars // 20 + 1))[:chars]
    return text.encode(encoding)

#: 出错时绝不能出现在响应里的东西
FORBIDDEN_FRAGMENTS = (
    "Traceback",
    "File \"",
    "\\",                      # Windows 路径分隔符
    "/tmp/",
    "AppData",
    "site-packages",
    "filetools_",
    "office-converter-profile",
)


def temp_dirs() -> set[Path]:
    """系统临时目录下本服务的临时目录。"""
    root = Path(settings.TEMP_ROOT or tempfile.gettempdir())
    if not root.is_dir():
        return set()
    return {item for item in root.iterdir() if item.name.startswith("filetools_")}


def assert_clean_error(response, *, status: int, code: str, contains: str) -> dict:
    """统一的错误响应断言：状态码、错误码、文案，以及不泄露内部细节。"""
    assert response.status_code == status
    body = response.json()
    error = body["error"]
    assert error["code"] == code
    assert contains in error["message"]

    raw = response.text
    for fragment in FORBIDDEN_FRAGMENTS:
        assert fragment not in raw, f"错误响应里泄露了 {fragment!r}：{raw[:300]}"
    return error


# ----------------------------------------------------------------------
# 正常转换
# ----------------------------------------------------------------------

@requires_soffice
def test_converts_word_and_delivers_a_readable_pdf(client: TestClient) -> None:
    response = client.post(
        WORD, files=office_files(("季度报告.docx", build_docx_bytes()))
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["filename"] == "季度报告.pdf"
    assert payload["media_type"] == "application/pdf"
    assert payload["archived"] is False
    assert payload["page_count"] >= 1
    # original_size 是上传的 Word 文件大小，前端要拿它和结果并排显示
    assert payload["original_size"] == len(build_docx_bytes())
    assert any("页" in note for note in payload["notes"])

    # 真正的验收：PDF 里能读出原文
    assert OFFICE_MARKER in download_text(client, payload)


@requires_soffice
def test_result_is_cleaned_up_after_download(client: TestClient) -> None:
    """下载后结果目录要删掉（§十四：不留用户文件）。"""
    response = client.post(WORD, files=office_files(("report.docx", build_docx_bytes())))
    payload = response.json()

    assert client.get(payload["download_url"]).status_code == 200
    # 令牌是一次性的
    assert client.get(payload["download_url"]).status_code == 404


@requires_soffice
def test_successful_conversion_leaves_no_source_document(client: TestClient) -> None:
    """转换成功后，服务器上不该留下用户的原始文档（§十二）。

    转换一完成原件就没用了，结果目录还要留到用户下载或超时 ——
    没必要让用户的文档在旁边多躺这段时间。
    """
    response = client.post(WORD, files=office_files(("secret.docx", build_docx_bytes())))
    assert response.status_code == 200

    from services.job_store import job_store

    # 只看**这一次**转换的结果目录。登记簿是全局共享的内存表，扫全部目录
    # 会把别的测试留下的结果当成残留 —— 第七阶段之后 ``.docx`` 本身就是
    # 一种合法的**结果**格式（PDF 转 Word 的输出），那样扫必然误报。
    job = job_store.get(response.json()["download_url"].rsplit("/", 1)[-1])
    assert job is not None
    leftovers = [item.name for item in job.directory.iterdir() if item.suffix == ".docx"]
    assert leftovers == [], f"原始文档没有被清掉：{leftovers}"

    # 结果本身要还在（只是原件没了，不是整个目录被误删）
    assert job.path.exists()
    assert client.get(response.json()["download_url"]).status_code == 200


# ----------------------------------------------------------------------
# Excel：多个工作表
# ----------------------------------------------------------------------

@requires_soffice
def test_converts_excel_and_exports_every_worksheet(client: TestClient) -> None:
    """两张工作表都要进同一份 PDF，并且如实报出张数。"""
    response = client.post(
        EXCEL, files=office_files(("季度表格.xlsx", build_xlsx_bytes()))
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["filename"] == "季度表格.pdf"
    assert payload["page_count"] >= 2, "两张工作表至少要出两页"

    text = download_text(client, payload)
    assert OFFICE_MARKER in text
    assert "第二张表的内容" in text

    notes = " ".join(payload["notes"])
    assert "2 个工作表" in notes
    assert "隐藏" not in notes


@requires_soffice
def test_hidden_worksheet_is_not_exported(client: TestClient) -> None:
    """隐藏的工作表不导出 —— 说明里既报张数，也点明隐藏表的下场。

    实测（见 office/loader.py::count_excel_sheets 的注释）：两张可见 + 一张隐藏，
    LibreOffice 只出 2 页，隐藏表的内容不在 PDF 里。这句话要是说错了，
    用户会以为隐藏表也跟着转了 —— 而隐藏表里往往正是他不想给人看的东西。
    """
    response = client.post(
        EXCEL, files=office_files(("表格.xlsx", build_xlsx_bytes(hidden=1)))
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["page_count"] == 2

    notes = " ".join(payload["notes"])
    assert "2 个工作表" in notes
    assert "1 个隐藏的工作表" in notes

    text = download_text(client, payload)
    assert OFFICE_MARKER in text
    assert HIDDEN_SHEET_MARKER not in text, "隐藏工作表的内容不该出现在 PDF 里"


@requires_soffice
def test_excel_target_size_is_reported_honestly(client: TestClient) -> None:
    """「最大文件大小」对 Excel 同样生效，且说明如实。

    样张只有两个单元格，排出来必定远小于 500 KB，所以这里可以写无条件断言 ——
    换成「达到了就断言 A、没达到就断言 B」的写法，两个分支永远只走一个。
    """
    response = client.post(
        EXCEL,
        files=office_files(("表格.xlsx", build_xlsx_bytes())),
        data={"target": "500kb"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["size"] <= 500 * 1024
    assert "没有再做压缩" in " ".join(payload["notes"])


def test_word_uploaded_to_the_excel_endpoint_is_rejected(client: TestClient) -> None:
    """反向也要拦住：Word 传到 Excel 页面同样是「你传错了地方」。"""
    response = client.post(EXCEL, files=office_files(("报告.docx", build_docx_bytes())))

    error = assert_clean_error(
        response, status=415, code="INVALID_FILE_TYPE", contains="只能转换"
    )
    assert "Excel" in error["message"]
    assert "Word" in error["message"]


# ----------------------------------------------------------------------
# PowerPoint：一页幻灯片一页 PDF
# ----------------------------------------------------------------------

@requires_soffice
def test_converts_powerpoint_one_page_per_slide(client: TestClient) -> None:
    """三页幻灯片 → 三页 PDF，且每页的内容都在。"""
    response = client.post(
        POWERPOINT,
        files=office_files(("产品发布.pptx", build_pptx_bytes(slides=3))),
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["filename"] == "产品发布.pdf"
    assert payload["page_count"] == 3

    notes = " ".join(payload["notes"])
    assert "3 页幻灯片" in notes
    assert "已全部排进" in notes
    assert "隐藏" not in notes, "一页隐藏的都没有，就别提隐藏这回事"

    text = download_text(client, payload)
    assert OFFICE_MARKER in text
    assert "第2页幻灯片的内容" in text
    assert "第3页幻灯片的内容" in text


@requires_soffice
def test_hidden_slide_is_not_exported(client: TestClient) -> None:
    """隐藏幻灯片不导出，并且两个数字都要如实报出来。

    实测（见 office/loader.py::count_powerpoint_slides 的注释）：
    两张可见 + 一张隐藏 → 2 页，隐藏页的内容不在 PDF 里。
    """
    response = client.post(
        POWERPOINT,
        files=office_files(("发布.pptx", build_pptx_bytes(slides=2, hidden=1))),
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["page_count"] == 2

    notes = " ".join(payload["notes"])
    assert "3 页幻灯片" in notes
    assert "这份 PDF 有 2 页" in notes

    text = download_text(client, payload)
    assert OFFICE_MARKER in text
    assert HIDDEN_SLIDE_MARKER not in text, "隐藏幻灯片的内容不该出现在 PDF 里"


def test_powerpoint_uploaded_to_the_word_endpoint_is_rejected(
    client: TestClient,
) -> None:
    response = client.post(WORD, files=office_files(("deck.pptx", build_pptx_bytes())))

    error = assert_clean_error(
        response, status=415, code="INVALID_FILE_TYPE", contains="只能转换"
    )
    assert "PowerPoint" in error["message"]


# ----------------------------------------------------------------------
# 上传错文件
# ----------------------------------------------------------------------

def test_excel_uploaded_to_the_word_endpoint_is_rejected(client: TestClient) -> None:
    """传错格式时要说清「你传错了地方」，而不是笼统的「格式不支持」。"""
    response = client.post(WORD, files=office_files(("表格.xlsx", build_xlsx_bytes())))

    error = assert_clean_error(
        response, status=415, code="INVALID_FILE_TYPE", contains="只能转换"
    )
    assert "Word" in error["message"]
    assert "Excel" in error["message"]


@requires_soffice
def test_renamed_executable_is_rejected(client: TestClient) -> None:
    """挂在 LibreOffice 上：下面那 4 条同理。

    缺组件时 ``receive_office`` 在**落盘之前**就抛 503（``doc_service.py``
    里那句 ``if kind != KIND_TEXT and not is_available()``），于是内容嗅探与
    参数校验根本没机会跑起来 —— 那个顺序是有意的，由
    :func:`test_missing_component_is_reported_without_writing_to_disk` 钉着：
    服务器都用不了这个功能了，就不该把用户的文件先写到磁盘上再告诉他。
    代价是这几条「坏输入该被拒」的用例在没有组件的机器上问不到答案，
    只能跳过；它们的校验逻辑由上面那批单元测试继续覆盖。
    """
    response = client.post(
        WORD, files=office_files(("invoice.docx", b"MZ\x90\x00" + b"\x00" * 2048))
    )

    assert_clean_error(
        response, status=415, code="INVALID_FILE_TYPE", contains="与扩展名不一致"
    )


def test_unsupported_extension_is_rejected(client: TestClient) -> None:
    response = client.post(WORD, files=office_files(("notes.rtf", b"{\\rtf1 hello}")))

    assert_clean_error(
        response, status=415, code="INVALID_FILE_TYPE", contains="暂不支持该文件格式"
    )


@requires_soffice
def test_corrupt_document_reports_the_corrupt_message(client: TestClient) -> None:
    """容器齐全但正文 XML 坏了 —— 用户该做的是重新拿一份文件。"""
    parts = {
        "[Content_Types].xml": (
            '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org'
            '/package/2006/content-types"><Override PartName="/word/document.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.'
            'wordprocessingml.document.main+xml"/></Types>'
        ),
        "word/document.xml": "<w:document>没有闭合的标签 <<<",
    }
    response = client.post(WORD, files=office_files(("broken.docx", zip_bytes(parts))))

    assert_clean_error(
        response,
        status=400,
        code="CORRUPTED_FILE",
        contains="无法读取该 Office 文件，请检查文件是否损坏。",
    )


@requires_soffice
def test_empty_upload_is_rejected(client: TestClient) -> None:
    response = client.post(WORD, files=office_files(("empty.docx", b"")))

    assert response.status_code in (400, 415)
    assert "error" in response.json()


def test_missing_file_field_is_rejected(client: TestClient) -> None:
    """不带文件就提交：由参数校验或请求体限制拦下，不能进到处理逻辑。"""
    response = client.post(WORD)

    assert response.status_code in (400, 422)
    assert "error" in response.json()


# ----------------------------------------------------------------------
# 缺组件
# ----------------------------------------------------------------------

def test_missing_component_is_reported_without_writing_to_disk(
    client: TestClient, monkeypatch
) -> None:
    """缺组件要 503 + 专门的话术，并且在**落盘之前**就拒绝。

    这时候用户的文件没有任何问题，重传多少次都一样 ——
    所以既不能报「处理失败」，也不该为它白写一次磁盘。
    """
    monkeypatch.setattr(office_converter, "find_soffice", lambda: None)
    monkeypatch.setattr(office_converter, "_soffice_cache", None)

    before = temp_dirs()
    response = client.post(
        WORD, files=office_files(("report.docx", build_docx_bytes() * 200))
    )
    after = temp_dirs()

    assert_clean_error(
        response,
        status=503,
        code="CONVERTER_UNAVAILABLE",
        contains="当前服务器缺少 Office 转换组件，请联系管理员。",
    )
    assert after == before, "缺组件时不该留下临时目录"


# ----------------------------------------------------------------------
# 最大文件大小
# ----------------------------------------------------------------------

@requires_soffice
def test_small_result_skips_compression(client: TestClient) -> None:
    """最常见的情形：一份普通文档转出来只有几十 KB，远低于目标。

    这时不该做无谓的压缩（重编码只会让文字变糊），并说明为什么没压。
    """
    response = client.post(
        WORD,
        files=office_files(("report.docx", build_docx_bytes())),
        data={"target": "10mb"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["size"] < 10 * 1024 * 1024
    assert "没有再做压缩" in " ".join(payload["notes"])


@requires_soffice
def test_big_document_under_target_is_not_compressed(client: TestClient) -> None:
    """文档排出来就有几百 KB、仍低于目标时，不做压缩，并说明原因。

    和上面 ``test_small_result_skips_compression`` 的区别是样张真的很大
    （20 页 / 600 KB 上下）—— 证明「不压缩」不是因为样张小得看不出来。
    """
    response = client.post(
        WORD,
        files=office_files(("长篇报告.docx", build_docx_bytes(paragraphs=800))),
        data={"target": "5mb"},
    )

    assert response.status_code == 200
    payload = response.json()
    notes = " ".join(payload["notes"])

    assert payload["size"] > 100 * 1024, "样张排出来太小，证明不了什么"
    assert payload["size"] < 5 * 1024 * 1024
    assert "没有再做压缩" in notes, "结果本来就没超目标，不该假装压过"


@requires_soffice
def test_unreachable_target_is_reported_honestly(client: TestClient) -> None:
    """压不到目标时必须如实说明 —— 这条走的是**真实**的未达标路径。

    样张是 800 段纯文字：文字 PDF 的体积在字体与文字流上，压缩器只能重编码
    页面图片，对它基本无能为力（实测 643 KB 压完 631 KB）。目标取自定义的
    下限 0.1 MB，差距有 6 倍，所以这里可以写**无条件**断言。

    为什么不是「达到了就说达标、没达到就说没达标」那种写法：那样两个分支永远
    只走一个，等于什么也没验。样例若哪天小到 100 KB 以下，这条分支就没被走到，
    用例应当红掉提醒换更大的样张，而不是默默通过。
    """
    response = client.post(
        WORD,
        files=office_files(("长篇报告.docx", build_docx_bytes(paragraphs=800))),
        data={"target": "custom", "target_mb": "0.1"},
    )

    assert response.status_code == 200
    payload = response.json()
    notes = " ".join(payload["notes"])

    assert payload["size"] > 100 * 1024, "样张排出来的 PDF 太小，没走到未达标那条分支"
    assert "仍未达到" in notes
    assert "已达到目标大小" not in notes, "没达到目标却说了达标"
    # 说明里要给出真实数值，不能只说一句「没达到」让人无从判断
    assert "102.40 KB" in notes


# 「最大文件大小」参数的两条校验也挂在 LibreOffice 上：
# 目标大小是 LibreOffice 转完之后再压的（TXT 那条链路根本不看它），
# 所以整个接口在没有组件的机器上都是 503。见 test_renamed_executable 的说明。
@requires_soffice
def test_invalid_target_preset_is_rejected(client: TestClient) -> None:
    """档位不在白名单里要明确报错，不能悄悄退回「不限制」。"""
    response = client.post(
        WORD,
        files=office_files(("report.docx", build_docx_bytes())),
        data={"target": "1gb"},
    )

    assert_clean_error(
        response, status=400, code="INVALID_REQUEST", contains="目标大小参数无效"
    )


@requires_soffice
def test_custom_target_requires_a_number(client: TestClient) -> None:
    response = client.post(
        WORD,
        files=office_files(("report.docx", build_docx_bytes())),
        data={"target": "custom"},
    )

    assert_clean_error(
        response, status=400, code="INVALID_REQUEST", contains="请填写目标大小"
    )


# ----------------------------------------------------------------------
# TXT：排版四项 + 不依赖 LibreOffice
#
# 这一节**不挂 requires_soffice**：TXT 走 PyMuPDF 自己排版，
# 服务器没装 LibreOffice 也应该能用。这正是下面 test_txt_works_without_... 验的事。
# ----------------------------------------------------------------------

def test_converts_txt_and_reports_the_layout(client: TestClient) -> None:
    """纯文本排成 PDF，并且把实际用的字体、字号、页面写进说明。"""
    from office.txt_to_pdf import available_fonts

    chosen = available_fonts()[0]
    response = client.post(
        TXT,
        files=office_files(("会议记录.txt", build_txt_bytes())),
        data={
            "font": chosen.key,
            "font_size": "12",
            "page_size": "a4",
            "orientation": "portrait",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["filename"] == "会议记录.pdf"
    assert payload["media_type"] == "application/pdf"
    assert payload["page_count"] >= 1
    assert payload["original_size"] == len(build_txt_bytes())

    notes = " ".join(payload["notes"])
    assert chosen.label in notes
    assert "字号 12" in notes
    assert "A4" in notes
    assert "纵向" in notes
    assert f"{payload['page_count']} 页" in notes

    # 真正的验收：下载到的 PDF 里能读出原文（不是看界面说成功）
    assert TXT_MARKER in download_text(client, payload)


def test_txt_layout_options_change_the_downloaded_pdf(client: TestClient) -> None:
    """四个选项都要真的改变产物 —— 验下载到的 PDF，不验界面上选了哪个。"""

    def convert(**data) -> dict:
        response = client.post(
            TXT, files=office_files(("文本.txt", build_txt_bytes(4_000))), data=data
        )
        assert response.status_code == 200
        return response.json()

    portrait = convert(page_size="a4", orientation="portrait", font_size="11")
    landscape = convert(page_size="a4", orientation="landscape", font_size="11")
    small = convert(font_size="8")
    large = convert(font_size="24")

    with open_downloaded_pdf(client, portrait) as doc:
        a4 = doc[0].rect
    with open_downloaded_pdf(client, landscape) as doc:
        wide = doc[0].rect

    assert a4.height > a4.width, "纵向页面的高应该大于宽"
    assert wide.width > wide.height, "横向页面的宽应该大于高"
    assert wide.width == pytest.approx(a4.height), "横向就是把纵向转 90 度"

    assert large["page_count"] > small["page_count"], "字号调大页数没变，字号没生效"


def test_txt_font_choice_changes_the_embedded_font(client: TestClient) -> None:
    """换字体要换掉 PDF 里真正嵌入的字体（字体不是假控件）。"""
    from office.txt_to_pdf import available_fonts

    fonts = [item for item in available_fonts() if item.path is not None]
    if len(fonts) < 2:
        pytest.skip("本机只探测到一个系统字体，无法比较")

    names = []
    for item in fonts[:2]:
        response = client.post(
            TXT,
            files=office_files(("文本.txt", "字体对比。".encode("utf-8"))),
            data={"font": item.key},
        )
        assert response.status_code == 200
        with open_downloaded_pdf(client, response.json()) as doc:
            used = doc.get_page_fonts(0)
        assert used, f"{item.label} 没有嵌入任何字体"
        names.append(used[0][3])

    assert names[0] != names[1], f"两种字体嵌进去是同一个：{names}"


def test_txt_works_without_libreoffice(client: TestClient, monkeypatch) -> None:
    """没装 LibreOffice 时，TXT 照样能用，Office 三种照旧报「缺少组件」。

    TXT 不需要转换组件，这个区别必须在接口层成立：如果哪天有人把
    「缺组件」的检查挪到 receive_office 外面，TXT 会跟着一起坏掉，
    而用户完全没有办法绕过。
    """
    monkeypatch.setattr(office_converter, "find_soffice", lambda: None)
    monkeypatch.setattr(office_converter, "_soffice_cache", None)

    txt = client.post(TXT, files=office_files(("笔记.txt", build_txt_bytes(500))))
    assert txt.status_code == 200
    assert TXT_MARKER in download_text(client, txt.json())

    word = client.post(WORD, files=office_files(("报告.docx", build_docx_bytes())))
    assert_clean_error(
        word,
        status=503,
        code="CONVERTER_UNAVAILABLE",
        contains="当前服务器缺少 Office 转换组件，请联系管理员。",
    )


def test_txt_ignores_the_target_size(client: TestClient) -> None:
    """TXT 不接受「最大文件大小」：产物大小由字号和页面决定，压它没有意义。

    传了也不该产生任何压缩动作 —— 说明里不能出现压缩相关的字样。
    """
    response = client.post(
        TXT,
        files=office_files(("笔记.txt", build_txt_bytes(1_000))),
        data={"target": "500kb"},
    )

    assert response.status_code == 200
    notes = " ".join(response.json()["notes"])
    assert "压缩" not in notes
    assert "目标大小" not in notes


def test_gbk_encoded_txt_is_decoded_correctly(client: TestClient) -> None:
    """GBK 编码的 txt 很常见（Windows 记事本另存的默认编码），要能读出来。"""
    response = client.post(
        TXT, files=office_files(("gbk.txt", build_txt_bytes(500, encoding="gbk")))
    )

    assert response.status_code == 200
    # 编码猜错的话这里会是一堆乱码，标记就对不上了
    assert TXT_MARKER in download_text(client, response.json())


def test_docx_uploaded_to_the_txt_endpoint_is_rejected(client: TestClient) -> None:
    """Word 传到 TXT 页面也要说清「你传错了地方」。"""
    response = client.post(TXT, files=office_files(("报告.docx", build_docx_bytes())))

    error = assert_clean_error(
        response, status=415, code="INVALID_FILE_TYPE", contains="只能转换"
    )
    assert "文本文件" in error["message"]


def test_binary_file_renamed_to_txt_is_rejected(client: TestClient) -> None:
    """可执行文件改名成 .txt 要拦下，不能拿去排版。"""
    response = client.post(
        TXT, files=office_files(("readme.txt", b"MZ\x90\x00" + b"\x00" * 2048))
    )

    assert_clean_error(
        response,
        status=400,
        code="CORRUPTED_FILE",
        contains="无法读取该文本文件",
    )


def test_whitespace_only_txt_is_rejected(client: TestClient) -> None:
    """只有空白的文件没有任何可转换的内容，要说清楚而不是排出一张白纸。"""
    response = client.post(TXT, files=office_files(("blank.txt", b"   \n\n\t\n")))

    assert_clean_error(
        response, status=400, code="INVALID_REQUEST", contains="没有任何可转换的内容"
    )


def test_invalid_txt_font_is_rejected(client: TestClient) -> None:
    """字体不在探测到的列表里要报错，不能悄悄换成别的字体。"""
    response = client.post(
        TXT,
        files=office_files(("笔记.txt", build_txt_bytes(200))),
        data={"font": "comic-sans"},
    )

    assert_clean_error(
        response, status=400, code="INVALID_REQUEST", contains="字体参数无效"
    )


def test_txt_font_size_out_of_range_is_rejected(client: TestClient) -> None:
    response = client.post(
        TXT,
        files=office_files(("笔记.txt", build_txt_bytes(200))),
        data={"font_size": "999"},
    )

    assert_clean_error(
        response, status=400, code="INVALID_REQUEST", contains="字体大小需要在"
    )


def test_invalid_txt_page_size_is_rejected(client: TestClient) -> None:
    response = client.post(
        TXT,
        files=office_files(("笔记.txt", build_txt_bytes(200))),
        data={"page_size": "a3"},
    )

    assert_clean_error(
        response, status=400, code="INVALID_REQUEST", contains="页面大小参数无效"
    )


def test_txt_defaults_are_used_when_options_are_omitted(client: TestClient) -> None:
    """一个选项都不传也要能转，并且用配置里的默认值。"""
    response = client.post(TXT, files=office_files(("笔记.txt", build_txt_bytes(500))))

    assert response.status_code == 200
    notes = " ".join(response.json()["notes"])
    assert f"字号 {settings.TXT_DEFAULT_FONT_SIZE}" in notes
    assert "A4" in notes
    assert "纵向" in notes
