"""上传文件名编码的兼容层。

## 为什么需要它

multipart 的 ``Content-Disposition`` 里那个 ``filename=`` 参数，
各家客户端填的东西**不一样**（实测，不是推断）：

- 浏览器填的是**原始 UTF-8 字节** —— ``中文样张.png`` 到服务端还是
  ``中文样张.png``；
- Expo SDK 57 的原生 ``fetch``（``expo/fetch``）会把文件名过一遍
  ``encodeURIComponent``（``expo/src/winter/fetch/convertFormData.ts``
  的 ``encodeFilename``，**无条件**，没有开关可关）。

服务端这边**一个字节都不解码**：``%E4%B8%AD`` 原样就是 ``%E4%B8%AD``。
于是同一个「中文样张.png」，Web 上传得到 ``中文样张.jpg``，
Android 上传得到 ``E4_B8_AD_E6_96_87_E6_A0_B7_E5_BC_A0.jpg`` ——
``utils/files.py::sanitize_stem`` 把不在白名单里的 ``%`` 换成了 ``_``，
再被 ``strip(" ._")`` 削掉首尾的 ``_``。结果字节、尺寸、像素**全对**，
**只有名字是乱的**。

## 为什么不猜

**只有客户端自己知道它编码了没有**，服务端从字符串上看不出来：
``a%20b.png`` 既可能是「文件真的叫 ``a%20b.png``」（浏览器发原文），
也可能是「文件叫 ``a b.png``，被编码了」（原生发的）。
任何「看着像 percent-encoding 就解一下」的启发式都会**解错前者**，
而字面文件名一样是合法文件名，不能被弄坏。

所以这里不猜：**客户端显式声明，服务端才解**。声明方式是在 multipart 里
多带一个普通文本字段 ``filename_encoding=uri-component``。
（服务端本来就会忽略没声明的字段，所以对老客户端零影响。）

## 三种客户端各自的行为

=================  ======================  ==============  ==================
客户端             发的内容                 声明            服务端做什么
=================  ======================  ==============  ==================
浏览器             原始 UTF-8              不发            什么都不做
原生（本轮修复后）  ``encodeURIComponent``   发              解一次
=================  ======================  ==============  ==================

**不会重复解码** —— 解码只由发起编码的那一端触发，Web 端（以及任何
不发这个声明的客户端）永远走不到解码那一步。

**不会误解码** —— 声明了却发原始 UTF-8 时，``unquote`` 对不含合法
``%XX`` 转义的名字是恒等；转义非法或不是合法 UTF-8 时**原样保留**，
交给 ``sanitize_stem`` 照旧清洗。

⚠️ **残留风险，如实记下**：如果将来 expo 修好了编码器（改发原始 UTF-8）
却仍然发这个声明，那么一个**字面**含 ``%XX`` 的名字（例如 ``100%25.png``）
会被多解一次。这个声明与「文件名已编码」这个事实是绑在一起的，
两边必须一起改。``tests/test_filename_encoding.py`` 把「原生端必须发、
Web 端必须不发」钉死了；真到 expo 改的那天，改这里也改客户端。

## 挂在哪

以 ``APIRouter(dependencies=[...])`` 挂在**收文件的整个 router** 上，
而不是逐个端点写一遍 —— 端点会越加越多，漏一个就是一个静默的错名。
``tests/test_filename_encoding.py`` 里有一条机械对账，扫全部路由，
凡是收 ``UploadFile`` 的都必须带上这个依赖。
"""

from __future__ import annotations

from urllib.parse import unquote

from fastapi import Request
from starlette.datastructures import UploadFile as StarletteUploadFile

#: 客户端用来说明「本请求里的 filename 是 URI 组件编码过的」的字段名。
#: 与 ``mobile/src/services/api/filenameEncoding.ts`` 里的字面量必须逐字相同，
#: 由 ``tests/test_filename_encoding.py`` 两侧对账。
FILENAME_ENCODING_FIELD = "filename_encoding"

#: 目前唯一认得的编码值。认不出来的值一律**不解**（宁可保留原样，
#: 也不要按一个不认识的约定去动用户的文件名）。
URI_COMPONENT = "uri-component"


def decode_uri_component_filename(filename: str) -> str:
    """把一个 ``encodeURIComponent`` 过的文件名解回原文。

    解不动就**原样返回**，绝不抛异常、绝不给出半个字符：

    - ``%ZZ`` 这种不是合法转义的，``unquote`` 本来就原样留着；
    - ``%E4%B8`` 这种截断的多字节序列会解码失败 —— 这时整串原样保留，
      宁可名字丑一点，也不要出现替换字符 ``\\ufffd``。
    """
    try:
        return unquote(filename, encoding="utf-8", errors="strict")
    except UnicodeDecodeError:
        return filename


def decode_declared_filenames(uploads, declared: str | None) -> None:
    """按声明就地解开每个上传文件的名字。

    纯函数形态（不碰请求对象），方便直接测边界。
    """
    if declared != URI_COMPONENT:
        return
    for upload in uploads:
        name = getattr(upload, "filename", None)
        if name:
            upload.filename = decode_uri_component_filename(name)


async def declare_upload_filenames(request: Request) -> None:
    """router 级依赖：客户端声明了文件名编码时，就地解回来。

    为什么能改到端点收到的那个对象：Starlette 把解析结果缓存在
    ``request._form`` 上，FastAPI 解析 body 又比解依赖更早，
    所以这里 ``await request.form()`` 拿到的是**同一个** ``FormData``，
    改它里面的 ``UploadFile.filename`` 就是端点随后读到的值。
    （``tests/test_filename_encoding.py`` 用真实端点钉住了这条，
    FastAPI 哪天换了次序会直接红。）

    非 multipart 请求直接返回：这个依赖挂在整个 router 上，
    同一个 router 里还有 JSON 端点。
    """
    if not request.headers.get("content-type", "").startswith("multipart/form-data"):
        return
    form = await request.form()

    declared = form.get(FILENAME_ENCODING_FIELD)
    if declared != URI_COMPONENT:
        return

    # 判 Starlette 那个 ``UploadFile``，而不是 ``fastapi.UploadFile``：
    # multipart 解析器建的是前者，后者只是它的子类，``isinstance`` 判不出来。
    decode_declared_filenames(
        [value for value in form.values() if isinstance(value, StarletteUploadFile)],
        declared,
    )
