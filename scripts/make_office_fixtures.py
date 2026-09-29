"""生成第五阶段测试要用的旧格式 Office 样张（.doc / .xls / .ppt）。

为什么要单独写一个脚本：**旧格式样张必须是真的 Office 文件**。
手写 OLE2 容器里的 Word 二进制流不现实，而网上下载的样张没法进版本库、
也没法解释它到底是什么。这里的办法是：

    手写极简 flat XML（.fodt / .fods / .fodp，都是一个纯文本 XML 文件）
      → 用**本机 LibreOffice** 转成 .doc / .xls / .ppt

这样样张是 LibreOffice 自己写出来的真文件，生成过程可复现、可审查，
文件也小（每个几 KB），适合提交进版本库。

跑一次即可，产物放在 ``backend/tests/fixtures/``：

    backend\\.venv\\Scripts\\python scripts\\make_office_fixtures.py

需要本机装了 LibreOffice；没装的话脚本会明确报错退出，不会留下半个文件。

生成的文件同样用于 README 里「旧格式支持到什么程度」的说明：
本脚本只证明**往返样张**能转，不代表真实的 Word 97 文件一定没问题。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
FIXTURES = BACKEND / "tests" / "fixtures"

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# 复用后端自己的 LibreOffice 发现逻辑（含 FILETOOLS_LIBREOFFICE_PATH），
# 免得这里再抄一份安装路径的猜测。
sys.path.insert(0, str(BACKEND))

from services.office_converter import find_soffice  # noqa: E402

#: 每种旧格式的标记文字。测试会断言它**出现在转出来的 PDF 里** ——
#: 只断言「页数 ≥ 1」是不够的：LibreOffice 对打不开的文件也可能给出一页空白。
#:
#: 比对时要**去掉所有空白**再比：LibreOffice 会把拉丁字母和汉字拆成两个文本段，
#: 抽出来的文字里就会出现「LEGACY-PPT- 标记文字」这样凭空多一个空格的情况。
#: 这跟内容对不对无关，只是抽取时的分段，所以比对前先抹掉空白。
MARKERS = {
    "doc": "LEGACY-DOC-标记文字",
    "xls": "LEGACY-XLS-标记文字",
    "ppt": "LEGACY-PPT-标记文字",
}

_NS = (
    'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
    'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
    'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" '
    'xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0" '
    'xmlns:svg="urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0" '
    'xmlns:style="urn:oasis:names:tc:opendocument:xmlns:style:1.0" '
    'xmlns:fo="urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0"'
)

FODT = f"""<?xml version="1.0" encoding="UTF-8"?>
<office:document {_NS} office:version="1.3"
 office:mimetype="application/vnd.oasis.opendocument.text">
 <office:body><office:text>
  <text:p>{MARKERS["doc"]}</text:p>
  <text:p>第二段：用于确认多段文字都会被转出来。</text:p>
 </office:text></office:body>
</office:document>
"""

#: 列宽必须显式写死。默认列宽放不下标记文字，右边又是非空单元格，
#: LibreOffice 会在**导出时就裁掉**放不下的部分 —— 抽出来的 PDF 文字里
#: 只剩「LEGACY-XLS」，后面半截是真的丢了。这不是抽取问题，是内容没了。
FODS = f"""<?xml version="1.0" encoding="UTF-8"?>
<office:document {_NS} office:version="1.3"
 office:mimetype="application/vnd.oasis.opendocument.spreadsheet">
 <office:automatic-styles>
  <style:style style:name="co-wide" style:family="table-column">
   <style:table-column-properties style:column-width="12cm"/>
  </style:style>
 </office:automatic-styles>
 <office:body><office:spreadsheet>
  <table:table table:name="旧表一">
   <table:table-column table:style-name="co-wide"/>
   <table:table-row>
    <table:table-cell office:value-type="string"><text:p>{MARKERS["xls"]}</text:p></table:table-cell>
   </table:table-row>
   <table:table-row>
    <table:table-cell office:value-type="float" office:value="42"><text:p>42</text:p></table:table-cell>
   </table:table-row>
  </table:table>
 </office:spreadsheet></office:body>
</office:document>
"""

FODP = f"""<?xml version="1.0" encoding="UTF-8"?>
<office:document {_NS} office:version="1.3"
 office:mimetype="application/vnd.oasis.opendocument.presentation">
 <office:body><office:presentation>
  <draw:page draw:name="第一页">
   <draw:frame svg:width="24cm" svg:height="4cm" svg:x="2cm" svg:y="3cm">
    <draw:text-box><text:p>{MARKERS["ppt"]}</text:p></draw:text-box>
   </draw:frame>
  </draw:page>
 </office:presentation></office:body>
</office:document>
"""

#: flat XML 源文件 -> (目标扩展名, LibreOffice 过滤器名)
JOBS: dict[str, tuple[str, str]] = {
    "sample.fodt": ("doc", "MS Word 97"),
    "sample.fods": ("xls", "MS Excel 97"),
    "sample.fodp": ("ppt", "MS PowerPoint 97"),
}

SOURCES = {"sample.fodt": FODT, "sample.fods": FODS, "sample.fodp": FODP}


def main() -> int:
    soffice = find_soffice()
    if soffice is None:
        print("找不到 LibreOffice，无法生成旧格式样张。", file=sys.stderr)
        print("装好后重跑本脚本；没有样张的相关用例会自动跳过。", file=sys.stderr)
        return 1

    FIXTURES.mkdir(parents=True, exist_ok=True)
    profile = Path(tempfile.mkdtemp(prefix="make-fixtures-profile-"))
    work = Path(tempfile.mkdtemp(prefix="make-fixtures-src-"))
    try:
        for name, content in SOURCES.items():
            (work / name).write_text(content, encoding="utf-8")

        for name, (extension, filter_name) in JOBS.items():
            command = [
                str(soffice),
                "--headless",
                "--norestore",
                f"-env:UserInstallation={profile.as_uri()}",
                "--convert-to",
                f"{extension}:{filter_name}",
                "--outdir",
                str(work),
                str(work / name),
            ]
            proc = subprocess.run(command, capture_output=True, timeout=180)
            produced = work / f"{Path(name).stem}.{extension}"
            if not produced.is_file():
                print(
                    f"转换失败：{name} -> {extension}\n"
                    f"stdout: {proc.stdout.decode('utf-8', 'replace')}\n"
                    f"stderr: {proc.stderr.decode('utf-8', 'replace')}",
                    file=sys.stderr,
                )
                return 1

            header = produced.read_bytes()[:8]
            if header != b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
                # OLE2 容器头。不是它，说明 LibreOffice 其实给了 OOXML，
                # 那这批「旧格式样张」就是假的，宁可报错也不要写出误导人的文件。
                print(f"{produced.name} 不是 OLE2 旧格式（头 {header!r}）", file=sys.stderr)
                return 1

            target = FIXTURES / produced.name
            shutil.copyfile(produced, target)
            print(f"已生成 {target.relative_to(ROOT)}（{target.stat().st_size} 字节）")

        print("\n标记文字（测试会断言它们出现在转出来的 PDF 里）：")
        for extension, marker in MARKERS.items():
            print(f"  .{extension}  {marker}")
        return 0
    finally:
        shutil.rmtree(profile, ignore_errors=True)
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
