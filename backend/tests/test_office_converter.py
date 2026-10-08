"""``services/office_converter.py` 的测试。

分两类：

* **不需要 LibreOffice 的**：产物复验、缺组件报错 —— monkeypatch 掉发现逻辑即可，
  在任何机器上都会跑；
* **需要真正转换的**：挂 ``requires_soffice``，没装 LibreOffice 的机器自动跳过。

真正转换的用例都断言「产出的 PDF 里能读出原文」，而不是只看有没有报错 ——
转换出一张白纸同样不会报错。
"""

from __future__ import annotations

import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from config import settings
from services import office_converter
from tests.conftest import (
    OFFICE_MARKER,
    build_docx_bytes,
    normalize_text,
    requires_soffice,
)
from utils.errors import (
    ConverterUnavailableError,
    CorruptedFileError,
    ErrorCode,
    ProcessingError,
)


@pytest.fixture(scope="module", autouse=True)
def clean_profile():
    """模块跑完后收掉长驻 profile 目录，别在系统临时目录里留东西。"""
    yield
    office_converter.shutdown_libreoffice()


def pdf_text(path: Path) -> str:
    """读出 PDF 里的全部文字（去空白，见 normalize_text 的说明）。"""
    import pymupdf

    with pymupdf.open(path) as doc:
        return normalize_text("".join(page.get_text() for page in doc))


# ----------------------------------------------------------------------
# 组件发现
# ----------------------------------------------------------------------

@requires_soffice
def test_finds_a_real_launcher() -> None:
    binary = office_converter.find_soffice()

    assert binary is not None
    assert binary.is_file()
    assert office_converter.is_available() is True


@requires_soffice
def test_prefers_the_com_launcher_on_windows() -> None:
    """``.com`` 与 ``.exe`` 是同一个启动器，但只有 ``.com`` 会阻塞。

    换成 ``.exe`` 会让「等它转完」这件事直接失效（进程立刻返回、产物还没写出来），
    所以这条要钉住。
    """
    import sys

    if sys.platform != "win32":
        pytest.skip("只在 Windows 上有 .com / .exe 之分")

    binary = office_converter.find_soffice()

    assert binary is not None
    if (binary.parent / "soffice.com").is_file():
        assert binary.name == "soffice.com"


def test_missing_component_raises_a_dedicated_error(monkeypatch) -> None:
    """缺组件必须是 CONVERTER_UNAVAILABLE，不能混进「处理失败」。

    用户看到「处理失败」会一直重传一份没问题的文档；
    这个错误码对应的建议是「联系管理员」。
    """
    monkeypatch.setattr(office_converter, "find_soffice", lambda: None)
    monkeypatch.setattr(office_converter, "_soffice_cache", None)

    assert office_converter.is_available() is False

    with pytest.raises(ConverterUnavailableError) as excinfo:
        office_converter.convert_word_to_pdf(Path("whatever.docx"), Path("."))

    assert excinfo.value.status_code == 503
    assert excinfo.value.code == ErrorCode.CONVERTER_UNAVAILABLE
    assert excinfo.value.message == "当前服务器缺少 Office 转换组件，请联系管理员。"


def test_explicit_path_that_does_not_exist_is_not_silently_replaced(monkeypatch) -> None:
    """显式配置了不存在的路径时判定为缺组件，不偷偷回退到自动查找。"""
    monkeypatch.setattr(office_converter, "_soffice_cache", None)
    monkeypatch.setattr(
        settings, "LIBREOFFICE_PATH", r"C:\definitely\not\here\soffice.com"
    )

    assert office_converter.find_soffice() is None


# ----------------------------------------------------------------------
# 内层超时必须严格小于外层
# ----------------------------------------------------------------------

def test_inner_timeout_is_strictly_smaller_than_outer(monkeypatch) -> None:
    """外层超时不会杀线程，只有内层先超时才能让锁正常释放。

    这条不改配置也必须成立，所以对着几个极端值都验一遍。
    """
    for outer in (30, 45, 60, 120, 180, 600):
        monkeypatch.setattr(settings, "OFFICE_CONVERT_TIMEOUT_SECONDS", outer)
        inner = office_converter._soffice_timeout()

        assert 0 < inner < outer, f"外层 {outer} 秒时内层算出了 {inner} 秒"


# ----------------------------------------------------------------------
# 产物复验
# ----------------------------------------------------------------------

def test_verify_output_rejects_a_non_pdf(tmp_path: Path) -> None:
    """LibreOffice 偶尔会「成功」退出却写出一份不是 PDF 的东西。

    这一步是「不生成损坏文件」的落地方式：宁可报错，也不把坏文件发给用户。
    """
    fake = tmp_path / "not-really.pdf"
    fake.write_bytes(b"<html><body>Error</body></html>")

    with pytest.raises(CorruptedFileError) as excinfo:
        office_converter._verify_output(fake)

    assert excinfo.value.code == ErrorCode.CORRUPTED_FILE
    assert excinfo.value.message == "转换结果无法读取，请重新上传文件。"


def test_verify_output_rejects_an_empty_file(tmp_path: Path) -> None:
    empty = tmp_path / "empty.pdf"
    empty.write_bytes(b"")

    with pytest.raises(CorruptedFileError):
        office_converter._verify_output(empty)


def test_verify_output_accepts_a_real_pdf(tmp_path: Path) -> None:
    import pymupdf

    path = tmp_path / "ok.pdf"
    doc = pymupdf.open()
    doc.new_page()
    doc.new_page()
    doc.save(path)
    doc.close()

    assert office_converter._verify_output(path) == 2


def test_collect_output_ignores_empty_files(tmp_path: Path) -> None:
    """产出目录里有个 0 字节的 pdf 时，不能把它当成结果。"""
    (tmp_path / "abc.pdf").write_bytes(b"")

    assert office_converter._collect_output(tmp_path, "abc") is None


def test_collect_output_falls_back_to_the_only_pdf(tmp_path: Path) -> None:
    """源文件主名和产出名对不上时，目录里唯一的 PDF 就是结果。"""
    import pymupdf

    other = tmp_path / "renamed-by-libreoffice.pdf"
    doc = pymupdf.open()
    doc.new_page()
    doc.save(other)
    doc.close()

    assert office_converter._collect_output(tmp_path, "expected-stem") == other


def test_collect_output_gives_up_when_ambiguous(tmp_path: Path) -> None:
    import pymupdf

    for name in ("a.pdf", "b.pdf"):
        doc = pymupdf.open()
        doc.new_page()
        doc.save(tmp_path / name)
        doc.close()

    assert office_converter._collect_output(tmp_path, "missing") is None


# ----------------------------------------------------------------------
# 真正转换
# ----------------------------------------------------------------------

@requires_soffice
def test_converts_word_to_pdf(tmp_path: Path) -> None:
    source = tmp_path / "report.docx"
    source.write_bytes(build_docx_bytes())

    output = office_converter.convert_word_to_pdf(source, tmp_path)

    assert output.is_file()
    assert output.suffix == ".pdf"
    assert output.stat().st_size > 0
    # 产物用随机名，不带用户文件名（§十八）
    assert "report" not in output.name
    # 正文真的搬过去了，不是一张白纸
    assert OFFICE_MARKER in pdf_text(output)


@requires_soffice
def test_output_is_a_readable_pdf(tmp_path: Path) -> None:
    source = tmp_path / "report.docx"
    source.write_bytes(build_docx_bytes())

    output = office_converter.convert_word_to_pdf(source, tmp_path)

    import pymupdf

    with pymupdf.open(output) as doc:
        assert doc.page_count >= 1


@requires_soffice
def test_converts_a_document_with_a_non_ascii_name(tmp_path: Path) -> None:
    """中文文件名要能转 —— LibreOffice 在非 ASCII 路径上的表现单独验一次。"""
    source = tmp_path / "季度报告 2026.docx"
    source.write_bytes(build_docx_bytes())

    output = office_converter.convert_word_to_pdf(source, tmp_path)

    assert OFFICE_MARKER in pdf_text(output)


@requires_soffice
def test_corrupt_document_reports_the_corrupt_message(tmp_path: Path) -> None:
    """容器齐全但正文 XML 是坏的 —— 这是「文件损坏」，不是「格式不支持」。"""
    from tests.conftest import zip_bytes

    parts = {
        "[Content_Types].xml": (
            '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org'
            '/package/2006/content-types"><Override PartName="/word/document.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.'
            'wordprocessingml.document.main+xml"/></Types>'
        ),
        "word/document.xml": "<w:document>不是合法的 XML <<< 没有闭合",
    }
    source = tmp_path / "broken.docx"
    source.write_bytes(zip_bytes(parts))

    with pytest.raises(CorruptedFileError) as excinfo:
        office_converter.convert_word_to_pdf(source, tmp_path)

    assert excinfo.value.code == ErrorCode.CORRUPTED_FILE
    assert excinfo.value.message == "无法读取该 Office 文件，请检查文件是否损坏。"


# ----------------------------------------------------------------------
# 损坏文件的归类：**不能只押 soffice 的退出码**
#
# 同一个损坏的 .docx，本机（Windows）上 soffice 退出码是 1 ——
# 上面那条 requires_soffice 的用例就是靠这个过的。但 CI 的 Linux runner 上
# 退出码是 **0**，于是同一个文件被判成「转换失败」→ 422
# 「请尝试重新上传文件」，而正确的话术是不该重传的「文件已损坏」。
#
# 下面这三条**不依赖 LibreOffice**（把 soffice 换成假实现），所以在本机与
# CI 上跑的是同一段逻辑；它们钉住的正是上面那条缝。
# ----------------------------------------------------------------------

def _fake_soffice(
    monkeypatch: pytest.MonkeyPatch, *, returncode: int, stderr: str = ""
) -> None:
    """把 soffice 换成「跑完了、但什么也没产出」的假实现。

    ``returncode=0, 无产物`` 就是 Linux runner 上真实发生过的那种返回。
    """
    monkeypatch.setattr(office_converter, "_require_soffice", lambda: Path("soffice"))

    def fake_run(binary: Path, source: Path, outdir: Path) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(
            args=["soffice"], returncode=returncode, stdout="", stderr=stderr
        )

    monkeypatch.setattr(office_converter, "_run_soffice", fake_run)


def _broken_docx(tmp_path: Path) -> Path:
    """容器完整、正文 XML 是坏的 —— 与上面那条 requires_soffice 用例同一份样张。"""
    from tests.conftest import zip_bytes

    parts = {
        "[Content_Types].xml": (
            '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org'
            '/package/2006/content-types"><Override PartName="/word/document.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.'
            'wordprocessingml.document.main+xml"/></Types>'
        ),
        "word/document.xml": "<w:document>不是合法的 XML <<< 没有闭合",
    }
    source = tmp_path / "broken.docx"
    source.write_bytes(zip_bytes(parts))
    return source


def test_broken_ooxml_is_corrupt_even_when_soffice_exits_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**CI 上那条红的回归用例。**

    Linux 上 soffice 对同一个损坏文件返回 0 且不产出任何东西。这种返回
    必须仍然判成「文件损坏」（400 / CORRUPTED_FILE），而不是
    「转换失败」（422 / PROCESSING_FAILED）—— 用户该做的是重新拿一份文件，
    重传多少次都一样。

    判据不是退出码，而是源文件自己的结构：``word/document.xml`` 不是良构 XML。
    """
    _fake_soffice(monkeypatch, returncode=0)
    source = _broken_docx(tmp_path)

    with pytest.raises(CorruptedFileError) as excinfo:
        office_converter.convert_word_to_pdf(source, tmp_path)

    assert excinfo.value.code == ErrorCode.CORRUPTED_FILE
    assert excinfo.value.message == "无法读取该 Office 文件，请检查文件是否损坏。"


def test_unloadable_stderr_is_corrupt_even_when_soffice_exits_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """第二条信号：stderr 里明说读不了源文件。

    用旧版 .doc（OLE2 二进制）—— 它没有 XML 主部件可判，所以这条走的
    确实只有「退出码 + stderr」两条信号里的第二条。
    """
    _fake_soffice(
        monkeypatch,
        returncode=0,
        stderr="Error: source file could not be loaded\n",
    )
    source = tmp_path / "legacy.doc"
    source.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 2048)

    with pytest.raises(CorruptedFileError) as excinfo:
        office_converter.convert_word_to_pdf(source, tmp_path)

    assert excinfo.value.code == ErrorCode.CORRUPTED_FILE


def test_a_sound_document_that_produced_nothing_stays_a_processing_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """反向护栏：**不许把「转换失败」一律改判成「文件损坏」**。

    一份结构完全正常的 .docx，soffice 退出码 0、没报错、却没有产出 ——
    这仍然是「转换失败」（422，可以重试），不是「文件损坏」。
    放宽成一律 400 会让用户以为自己手上的文件坏了，去重新导出一份，
    而真正该做的是稍后重试。
    """
    _fake_soffice(monkeypatch, returncode=0)
    source = tmp_path / "fine.docx"
    source.write_bytes(build_docx_bytes())

    with pytest.raises(ProcessingError) as excinfo:
        office_converter.convert_word_to_pdf(source, tmp_path)

    assert excinfo.value.code == ErrorCode.PROCESSING_FAILED
    assert excinfo.value.message == office_converter.CONVERT_FAILED_MESSAGE


@requires_soffice
def test_concurrent_conversions_all_succeed(tmp_path: Path) -> None:
    """两个转换同时发起都必须成功。

    这正是「共用一份 profile 时第二个会静默失败」那个坑 ——
    exits=1、没有输出、没有日志，用户只看到「转换失败」。
    单飞锁就是为了这个。
    """
    sources = []
    for index in range(3):
        path = tmp_path / f"doc{index}.docx"
        path.write_bytes(build_docx_bytes(f"{OFFICE_MARKER}-{index}"))
        sources.append((index, path))

    results: dict[int, Path] = {}
    errors: list[BaseException] = []

    def worker(index: int, path: Path) -> None:
        try:
            results[index] = office_converter.convert_word_to_pdf(path, tmp_path)
        except BaseException as exc:  # noqa: BLE001 - 测试要把任何异常都收集起来
            errors.append(exc)

    with ThreadPoolExecutor(max_workers=3) as pool:
        for index, path in sources:
            pool.submit(worker, index, path)

    assert errors == [], f"并发转换出错：{errors}"
    assert len(results) == 3
    # 三份产物互不覆盖
    assert len({str(path) for path in results.values()}) == 3
    # 每一份都和自己源文件对得上（同名输入进同一目录会互相覆盖，这里也要挡住）
    for index, _ in sources:
        assert f"{OFFICE_MARKER}-{index}" in pdf_text(results[index])


@requires_soffice
def test_conversion_leaves_no_output_directory_behind(tmp_path: Path) -> None:
    """每次转换用的临时输出目录要收干净，否则临时目录会越堆越多。"""
    source = tmp_path / "report.docx"
    source.write_bytes(build_docx_bytes())

    before = {item.name for item in tmp_path.iterdir()}
    office_converter.convert_word_to_pdf(source, tmp_path)
    after = {item.name for item in tmp_path.iterdir()}

    assert not [name for name in after - before if name.startswith("office-out-")]


def test_lock_wait_has_an_upper_bound(monkeypatch) -> None:
    """锁必须有等待上限。

    被 ``asyncio.wait_for`` 取消的线程还活着，如果它无限期地等锁，
    全局线程池会被这些僵尸线程耗光，图片和 PDF 工具跟着一起卡住。
    """
    assert settings.OFFICE_LOCK_WAIT_SECONDS > 0

    held = threading.Event()
    release = threading.Event()

    def hog() -> None:
        office_converter._LAUNCHER_LOCK.acquire()
        held.set()
        release.wait(timeout=10)
        office_converter._LAUNCHER_LOCK.release()

    monkeypatch.setattr(settings, "OFFICE_LOCK_WAIT_SECONDS", 1)
    monkeypatch.setattr(
        office_converter, "find_soffice", lambda: Path(__file__)
    )

    thread = threading.Thread(target=hog)
    thread.start()
    try:
        assert held.wait(timeout=5)
        source = Path(__file__).parent / "conftest.py"

        with pytest.raises(ProcessingError) as excinfo:
            office_converter.convert_word_to_pdf(source, Path("."))

        assert "稍后重试" in excinfo.value.message
    finally:
        release.set()
        thread.join(timeout=10)
