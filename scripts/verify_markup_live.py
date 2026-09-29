"""第九阶段第 9 步的真机验收：对着跑着的 :8011 走一遍统一的 HTTP 路径。

单元测试验的是函数，这里验的是**服务**：multipart 上传 → 队列 → worker →
下载令牌 → 真正打开产物。两端之间隔着 FastAPI 的表单解析、任务队列、
线程池和下载路由，任何一环接错都会在这里现形而不会在单元测试里现形。

产物一律**真打开**：PDF 用 PyMuPDF 抽文字数图片，HTML 用标准库解析器
数结构标签并检查有没有脚本。只看 HTTP 200 不算验过。
"""

from __future__ import annotations

import io
import sys
import time
from html.parser import HTMLParser

import fitz
import httpx

BASE = "http://127.0.0.1:8011"
API = f"{BASE}/api/conversion"

#: 任务的终止状态。「完成」用的是 ``completed`` 而不是 ``succeeded`` ——
#: 这套词表由第八阶段的任务队列定义，验的时候按它的来。
TERMINAL = ("completed", "failed", "cancelled")

_failures: list[str] = []
_checks = 0


def check(ok: bool, what: str) -> None:
    global _checks
    _checks += 1
    print(("PASS  " if ok else "FAIL  ") + what, flush=True)
    if not ok:
        _failures.append(what)


SAMPLE_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>季度报告</title>
<script>alert('我不该出现在结果里')</script>
<style>body{color:red}</style></head>
<body>
<h1>季度报告</h1>
<p>这是<strong>加粗</strong>与<em>斜体</em>，还有<code>inline_code()</code>。</p>
<h2>要点</h2>
<ul><li>第一条<ul><li>嵌套的条目</li></ul></li><li>第二条</li></ul>
<ol><li>步骤一</li><li>步骤二</li></ol>
<blockquote>引用第一行<br>引用第二行</blockquote>
<table><tr><th>项目</th><th>数值</th></tr>
<tr><td>收入</td><td>1234</td></tr></table>
<hr>
</body></html>
"""

SAMPLE_MARKDOWN = """# 发布说明

这是**加粗**与*斜体*，还有 `inline_code()`，以及 [链接](https://example.com)。

## 要点

- 第一条
  - 嵌套的条目
- 第二条

1. 步骤一
2. 步骤二

> 引用第一行
> 引用第二行

| 项目 | 数值 |
| --- | --- |
| 收入 | 1234 |

---

最后一段。
"""


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


def submit(client: httpx.Client, filename: str, content: bytes,
           target_type: str, extra: dict | None = None) -> str:
    files = {"files": (filename, io.BytesIO(content), "application/octet-stream")}
    data = {"target_type": target_type}
    if extra:
        data.update(extra)
    resp = client.post(f"{API}/tasks", files=files, data=data)
    if resp.status_code != 202:
        raise SystemExit(f"提交失败 {resp.status_code}: {resp.text[:400]}")
    body = resp.json()
    return body["batch_id"]


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
    batch_id = submit(client, filename, content, target, extra)
    body = wait_done(client, batch_id)
    task = body["tasks"][0]
    if task["status"] != "completed":
        raise SystemExit(f"[{label}] 任务失败：{task.get('error_message') or task}")
    resp = download(client, task)
    print(f"      {label}: {len(resp.content)} 字节, "
          f"Content-Type={resp.headers.get('content-type')}, "
          f"Disposition={resp.headers.get('content-disposition')}")
    return resp.content, resp


# ----------------------------------------------------------------------

def main() -> int:
    with httpx.Client(timeout=120.0) as client:
        # ---- 1. HTML → PDF ----
        pdf, _ = run(client, "报告.html", SAMPLE_HTML.encode(), "pdf", "HTML→PDF")
        doc = fitz.open(stream=pdf, filetype="pdf")
        try:
            text = "".join(page.get_text() for page in doc)
            check(doc.page_count >= 1, f"HTML→PDF 真的产出了 {doc.page_count} 页")
            for fragment in ("季度报告", "加粗", "斜体", "inline_code()",
                             "第一条", "嵌套的条目", "步骤二", "引用第二行",
                             "收入", "1234"):
                check(fragment in text, f"HTML→PDF 的正文里有「{fragment}」")
            check("我不该出现在结果里" not in text, "HTML→PDF 没有把 <script> 内容排进去")
            check(len(pdf) < 400_000, f"HTML→PDF 的字体是子集（{len(pdf)} 字节）")
        finally:
            doc.close()

        # ---- 2. Markdown → PDF ----
        pdf, _ = run(client, "说明.md", SAMPLE_MARKDOWN.encode(), "pdf", "MD→PDF")
        doc = fitz.open(stream=pdf, filetype="pdf")
        try:
            text = "".join(page.get_text() for page in doc)
            check(doc.page_count >= 1, f"MD→PDF 真的产出了 {doc.page_count} 页")
            for fragment in ("发布说明", "要点", "加粗", "斜体", "inline_code()",
                             "嵌套的条目", "步骤二", "引用第二行", "1234"):
                check(fragment in text, f"MD→PDF 的正文里有「{fragment}」")
        finally:
            doc.close()

        # ---- 3. Markdown → HTML ----
        html, resp = run(client, "说明.md", SAMPLE_MARKDOWN.encode(), "html",
                         "MD→HTML")
        check(resp.headers.get("content-type", "").startswith("text/html"),
              "MD→HTML 的 Content-Type 是 text/html")
        check("attachment" in (resp.headers.get("content-disposition") or ""),
              "产出 HTML 带 Content-Disposition: attachment（否则就是存储型 XSS）")
        parser = _TagCounter()
        parser.feed(html.decode("utf-8"))
        check(parser.counts.get("h1", 0) == 1, f"MD→HTML 有 1 个 h1（{parser.counts.get('h1', 0)}）")
        check(parser.counts.get("h2", 0) == 1, f"MD→HTML 有 1 个 h2（{parser.counts.get('h2', 0)}）")
        check(parser.counts.get("table", 0) == 1, "MD→HTML 把表格转成了 table")
        check(parser.counts.get("blockquote", 0) == 1, "MD→HTML 把引用转成了 blockquote")
        check(parser.counts.get("hr", 0) == 1, "MD→HTML 把分隔线转成了 hr")
        check(parser.counts.get("script", 0) == 0, "MD→HTML 的产物里没有 script")
        check("加粗" in parser.text and "1234" in parser.text, "MD→HTML 的正文完整")

        # ---- 4. HTML → TXT ----
        text_bytes, resp = run(client, "报告.html", SAMPLE_HTML.encode(), "txt",
                               "HTML→TXT")
        check(resp.headers.get("content-type", "").startswith("text/plain"),
              "HTML→TXT 的 Content-Type 是 text/plain")
        text = text_bytes.decode("utf-8")
        check("季度报告" in text and "1234" in text, "HTML→TXT 的正文完整")
        check("<h1>" not in text and "<p>" not in text, "HTML→TXT 真的把标签剥掉了")
        check("我不该出现在结果里" not in text, "HTML→TXT 没有把 <script> 内容留下")
        check(text.count("\n") >= 5, f"HTML→TXT 保留了换行结构（{text.count(chr(10))} 个）")

        # ---- 5. 多文件的每一种判定 ----
        batch_id = submit(client, "报告.html", SAMPLE_HTML.encode(), "pdf")
        body = wait_done(client, batch_id)
        check(len(body["tasks"]) == 1, "一次提交一个文件就只有一个任务")

        # ---- 6. 拒绝：伪造扩展名 ----
        resp = client.post(
            f"{API}/tasks",
            files={"files": ("假的.html", io.BytesIO(b"\x00\x01\x02\x03\xff\xfe"),
                             "text/html")},
            data={"target_type": "pdf"},
        )
        check(resp.status_code == 202, "坏文件也让整批受理（不整批 500）")
        body = wait_done(client, resp.json()["batch_id"])
        check(body["tasks"][0]["status"] == "failed",
              "伪造的 HTML 被内容校验拦下")
        message = str(body["tasks"][0].get("error_message") or "")
        check("Traceback" not in message and "\\" not in message
              and "/" not in message,
              f"失败信息里没有异常栈与路径（{message[:60]}）")

        # ---- 7. 长度上限绕不过去 ----
        huge = ("<p>很长</p>" * 60_000).encode()
        resp = client.post(
            f"{API}/tasks",
            files={"files": ("超长.html", io.BytesIO(huge), "text/html")},
            data={"target_type": "pdf"},
        )
        body = wait_done(client, resp.json()["batch_id"])
        check(body["tasks"][0]["status"] == "failed", "超长 HTML 被长度上限拦下")
        message = str(body["tasks"][0].get("error_message") or "")
        check("Traceback" not in message and "backend" not in message.lower(),
              f"超长的失败信息同样干净（{message[:60]}）")

    print()
    print(f"通过 {_checks - len(_failures)} 项，失败 {len(_failures)} 项")
    for item in _failures:
        print(f"  失败：{item}")
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main())
