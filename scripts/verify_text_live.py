"""第九阶段第 10 步的真机验收：TXT → DOCX / HTML / Markdown 走一遍真实 HTTP。

与 :mod:`scripts.verify_markup_live` 同一条路子，验的是**服务**而不是函数：
multipart 上传 → 表单解析 → 五层 options 校验链 → 任务队列 → worker →
下载令牌 → 真正打开产物。中间隔着 FastAPI、队列和线程池，任何一环接错
都会在这里现形，而不会在单元测试里现形。

产物一律**真打开**：

* DOCX —— ``python-docx`` 读段落 + ``zipfile`` 读 ``word/styles.xml`` 对字体
* HTML —— 标准库 ``html.parser`` 数结构标签、检查脚本有没有被注入
* MD   —— 拿**服务端自己的解析器**再解析一遍，比对是否回到原文
* PDF  —— PyMuPDF 打开，读页数、页面尺寸与正文

用后端虚拟环境跑（需要 ``docx`` / ``fitz`` / ``httpx``）::

    backend/.venv/Scripts/python.exe scripts/verify_text_live.py
"""

from __future__ import annotations

import io
import sys
import time
import zipfile
from html.parser import HTMLParser
from pathlib import Path

import fitz
import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from office import markup_parse  # noqa: E402  （要在 sys.path 设好之后）

BASE = "http://127.0.0.1:8011"
API = f"{BASE}/api/conversion"

#: 任务的终止状态。「完成」用的是 ``completed`` 而不是 ``succeeded`` ——
#: 这套词表由第八阶段的任务队列定义，验的时候按它的来。
TERMINAL = ("completed", "failed", "cancelled")

DOCX_MIME = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)

_failures: list[str] = []
_checks = 0


def check(ok: bool, what: str) -> None:
    global _checks
    _checks += 1
    print(("PASS  " if ok else "FAIL  ") + what, flush=True)
    if not ok:
        _failures.append(what)


#: 纯文本样本。刻意混进四种「长得像 Markdown 却不是 Markdown」的行，
#: 以及一行 ``---``：转成 .md 时它们必须还是正文，不能变成标题 / 列表 / 分隔线。
SAMPLE_TEXT = "\n".join(
    [
        "季度总结",
        "本季度收入 1234 万元，同比增长 12%。",
        "  这一行前面有两个空格，表示它是上一行的补充说明。",
        "",
        "# 这不是标题",
        "- 这不是列表项",
        "1. 这也不是有序列表",
        "> 这不是引用",
        "---",
        "C# 与 F# 都是语言",
    ]
)

#: 行首缩进那一行在 Markdown 里留不住（Markdown 段落本来就不保留前导空白），
#: 往返比对时按去掉缩进的样子期待。
_MD_EXPECTED = [
    line.strip() if line.startswith("  ") else line
    for line in SAMPLE_TEXT.split("\n")
    if line.strip()
]


class _TagCounter(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.counts: dict[str, int] = {}
        self.text_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        self.counts[tag] = self.counts.get(tag, 0) + 1

    def handle_data(self, data: str) -> None:
        self.text_parts.append(data)

    @property
    def text(self) -> str:
        return "".join(self.text_parts)


# ----------------------------------------------------------------------
# HTTP
# ----------------------------------------------------------------------

def submit(client: httpx.Client, filename: str, content: bytes, target: str,
           extra: dict | None = None) -> httpx.Response:
    files = {"files": (filename, io.BytesIO(content), "application/octet-stream")}
    data = {"target_type": target}
    if extra:
        data.update(extra)
    return client.post(f"{API}/tasks", files=files, data=data)


def wait_done(client: httpx.Client, batch_id: str, seconds: float = 90.0) -> dict:
    deadline = time.time() + seconds
    while time.time() < deadline:
        body = client.get(f"{API}/tasks/{batch_id}").json()
        tasks = body["tasks"]
        if tasks and all(t["status"] in TERMINAL for t in tasks):
            return body
        time.sleep(0.4)
    raise SystemExit(f"任务超时未结束：{batch_id}")


def download(client: httpx.Client, task: dict) -> httpx.Response:
    """按任务自己给出的下载令牌取结果 —— 不自己拼路径。"""
    url = (task.get("result") or {}).get("download_url")
    if not url:
        raise SystemExit(f"任务没有下载地址：{task}")
    resp = client.get(f"{BASE}{url}")
    if resp.status_code != 200:
        raise SystemExit(f"下载失败 {resp.status_code}: {resp.text[:300]}")
    return resp


def run(client: httpx.Client, filename: str, content: bytes, target: str,
        label: str, extra: dict | None = None) -> tuple[bytes, httpx.Response]:
    resp = submit(client, filename, content, target, extra)
    if resp.status_code != 202:
        raise SystemExit(f"[{label}] 提交失败 {resp.status_code}: {resp.text[:300]}")
    body = wait_done(client, resp.json()["batch_id"])
    task = body["tasks"][0]
    if task["status"] != "completed":
        raise SystemExit(f"[{label}] 任务失败：{task.get('error_message') or task}")
    got = download(client, task)
    print(f"      {label}: {len(got.content)} 字节, "
          f"Content-Type={got.headers.get('content-type')}, "
          f"Disposition={got.headers.get('content-disposition')}")
    return got.content, got


def expect_failure(client: httpx.Client, filename: str, content: bytes,
                   target: str, label: str, extra: dict | None = None) -> tuple[int, str]:
    """提交一份注定失败的输入，返回 ``(HTTP 状态码, 用户能看到的那句话)``。"""
    resp = submit(client, filename, content, target, extra)
    if resp.status_code == 202:
        body = wait_done(client, resp.json()["batch_id"])
        task = body["tasks"][0]
        return 202, str(task.get("error_message") or "")
    return resp.status_code, resp.text


def _clean(message: str) -> bool:
    """用户能看到的话里不许有异常栈、绝对路径、模块名。"""
    lowered = message.lower()
    return not any(
        bad in message or bad in lowered
        for bad in ("Traceback", "backend", "site-packages", "File \"", ".py")
    ) and "\\" not in message and "/" not in message


# ----------------------------------------------------------------------

def main() -> int:
    from docx import Document

    with httpx.Client(timeout=120.0) as client:

        # ---- 1. TXT → DOCX（真的用 python-docx 打开）----
        raw, resp = run(client, "季度总结.txt", SAMPLE_TEXT.encode(), "docx",
                        "TXT→DOCX", {"options": '{"font":"kai"}'})
        check(resp.headers.get("content-type") == DOCX_MIME,
              f"TXT→DOCX 的 Content-Type 是 Word 的 MIME（{resp.headers.get('content-type')}）")
        check("attachment" in (resp.headers.get("content-disposition") or ""),
              "TXT→DOCX 带 Content-Disposition: attachment")
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            names = set(archive.namelist())
            styles = archive.read("word/styles.xml").decode("utf-8")
        check("word/document.xml" in names and "[Content_Types].xml" in names,
              "产物是一个合法的 OOXML 包")

        path = Path("verify_text_live.docx").resolve()
        path.write_bytes(raw)
        try:
            document = Document(str(path))
            texts = [p.text for p in document.paragraphs if p.text]
        finally:
            path.unlink(missing_ok=True)
        check(texts == [line for line in SAMPLE_TEXT.split("\n") if line.strip()],
              f"用 python-docx 打开后段落逐字相符（{texts}）")
        check("eastAsia" in styles and "楷体" in styles,
              "选的字体（楷体）真的写进了 styles.xml，不是收下不用")

        # ---- 2. TXT → HTML ----
        raw, resp = run(client, "季度总结.txt", SAMPLE_TEXT.encode(), "html",
                        "TXT→HTML")
        check(resp.headers.get("content-type", "").startswith("text/html"),
              "TXT→HTML 的 Content-Type 是 text/html")
        check("attachment" in (resp.headers.get("content-disposition") or ""),
              "产出 HTML 带 attachment（否则就是一个存储型 XSS 页面）")
        html = raw.decode("utf-8")
        check(html.startswith("<!doctype html>"), "TXT→HTML 产出的是一份完整网页")
        parser = _TagCounter()
        parser.feed(html)
        check(parser.counts.get("p", 0) == 9,
              f"十行（去掉一个空行）变成了 9 个 <p>（{parser.counts.get('p', 0)}）")
        for tag in ("h1", "h2", "ul", "ol", "li", "table", "blockquote", "hr"):
            check(parser.counts.get(tag, 0) == 0, f"TXT→HTML 里没有凭空多出的 <{tag}>")
        check("C# 与 F# 都是语言" in parser.text, "正文完整（含句中的 #）")

        # ---- 3. TXT → Markdown，再拿服务端自己的解析器解析回来 ----
        raw, resp = run(client, "季度总结.txt", SAMPLE_TEXT.encode(), "md",
                        "TXT→MD")
        check(resp.headers.get("content-type", "").startswith("text/markdown"),
              "TXT→MD 的 Content-Type 是 text/markdown")
        md = raw.decode("utf-8")
        reparsed = markup_parse.parse_markdown(md)
        check([b.runs[0].text for b in reparsed.blocks if b.runs] == _MD_EXPECTED,
              "把 .md 再解析一遍，正文逐行回到原文（标题/列表/分隔线都没有凭空出现）")
        check(all(b.kind == markup_parse.PARA_KIND_BODY for b in reparsed.blocks),
              "解析回来的每一块都是正文段落")
        check("\\# 这不是标题" in md, "行首的 # 在 .md 里被转义了")

        # ---- 4. 注入：.txt 里的 <script> 只能是文字 ----
        payload = "<script>alert('x')</script>\n<b>加粗标签也只是文字</b>"
        raw, _ = run(client, "注入.txt", payload.encode(), "html", "注入→HTML")
        injected = _TagCounter()
        injected.feed(raw.decode("utf-8"))
        check(injected.counts.get("script", 0) == 0, "注入的 <script> 没有变成真元素")
        check(injected.counts.get("b", 0) == 0, "注入的 <b> 没有变成真元素")
        check("<script>alert('x')</script>" in injected.text,
              "它作为**文字**原样保留，没有被悄悄吞掉")

        # ---- 5. TXT → PDF：选项真的生效（A5 横向 + 楷体）----
        raw, _ = run(client, "季度总结.txt", SAMPLE_TEXT.encode(), "pdf",
                     "TXT→PDF(A5横向)",
                     {"options": '{"font":"kai","page_size":"a5","orientation":"landscape"}'})
        doc = fitz.open(stream=raw, filetype="pdf")
        try:
            page = doc[0]
            width, height = page.rect.width, page.rect.height
            check(doc.page_count >= 1, f"TXT→PDF 真的产出了 {doc.page_count} 页")
            check(abs(width - 595.28) < 3 and abs(height - 419.53) < 3,
                  f"选 A5 横向后页面是 {width:.1f}×{height:.1f} 磅（148×210mm 横放）")
            text = "".join(p.get_text() for p in doc)
            check("季度总结" in text and "1234" in text, "TXT→PDF 的正文读得出来")
        finally:
            doc.close()

        # ---- 6. 参数绕不过限制：不在这一格 schema 里的键当场 400 ----
        status, message = expect_failure(
            client, "季度总结.txt", SAMPLE_TEXT.encode(), "pdf", "越权参数",
            {"options": '{"quality":"100"}', "capability_id": "document.txt-to-pdf"},
        )
        check(status == 400, f"TXT→PDF 不接受 quality（HTTP {status}）")
        check(_clean(message), f"拒绝的话里没有内部信息（{message[:60]}）")

        # ``page_size=custom`` 是图片那一族的取值，TXT 能力上必须被拒
        status, message = expect_failure(
            client, "季度总结.txt", SAMPLE_TEXT.encode(), "pdf", "错族的取值",
            {"options": '{"page_size":"custom"}', "capability_id": "document.txt-to-pdf"},
        )
        check(status == 400, f"TXT 不认 page_size=custom（HTTP {status}）")

        # ---- 7. 伪造扩展名：内容是二进制垃圾的 .txt ----
        status, message = expect_failure(
            client, "假的.txt", b"\x00\x01\x02\x03\xff\xfe\x00", "docx", "假 TXT→DOCX"
        )
        check(status == 202, "坏文件也让整批受理（不整批 500）")
        check("Traceback" not in message and _clean(message),
              f"伪造的 .txt 被内容校验拦下，话很干净（{message[:60]}）")

        # ---- 8. 长度上限：绕不过去 ----
        huge = ("字" * 500_001).encode()
        status, message = expect_failure(client, "超长.txt", huge, "html", "超长 TXT")
        check("太长" in message or "拆分" in message,
              f"超长 TXT 被长度上限拦下（{message[:60]}）")
        check(_clean(message), "超长的失败信息同样干净")

    print()
    print(f"通过 {_checks - len(_failures)} 项，失败 {len(_failures)} 项")
    for item in _failures:
        print(f"  失败：{item}")
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main())
