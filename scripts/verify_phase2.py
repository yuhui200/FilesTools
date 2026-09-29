"""第二阶段的真机验收脚本（Playwright + 真实浏览器）。

覆盖：格式转换（含同格式提示、PNG 无损、质量设置）、尺寸调整（等比联动、
常用尺寸、目标大小）、批量处理与 ZIP 下载、图片预览、错误提示。

用法：
    1. 先启动后端（8000）和前端（5173）
    2. 安装依赖：pip install playwright && playwright install chromium
    3. python scripts/verify_phase2.py

可用环境变量覆盖默认地址：
    FILETOOLS_WEB_BASE   默认 http://localhost:5173
    FILETOOLS_BACKEND_PY 后端 venv 的 python，用于校验下载到的图片
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import tempfile

from playwright.sync_api import Page, expect, sync_playwright

ROOT = pathlib.Path(__file__).resolve().parent.parent
BASE = os.environ.get("FILETOOLS_WEB_BASE", "http://localhost:5173")

WORK = pathlib.Path(tempfile.mkdtemp(prefix="filetools-verify-"))
IMAGES = WORK / "images"
DOWNLOADS = WORK / "downloads"
REPORT = WORK / "report.txt"

_SIZES_CODE = (
    "import io,sys,zipfile;"
    "from PIL import Image;"
    "z=zipfile.ZipFile(sys.argv[1]);"
    "print(';'.join(n+':'+Image.open(io.BytesIO(z.read(n))).format+':'"
    "+str(Image.open(io.BytesIO(z.read(n))).size) for n in z.namelist()))"
)

# 生成测试图片的脚本：一张有渐变、图形和噪点的「类照片」图片，
# 纯色图压缩率过高，无法验证按目标大小压缩的效果。
_FIXTURE_CODE = r'''
import io, os, sys
from PIL import Image, ImageDraw, ImageFilter

out = sys.argv[1]
os.makedirs(out, exist_ok=True)

def photo(w, h):
    img = Image.linear_gradient("L").resize((w, h)).convert("RGB")
    draw = ImageDraw.Draw(img)
    for i in range(8):
        draw.ellipse(
            [i * w // 10, h // 5, i * w // 10 + w // 6, h // 5 + w // 6],
            fill=(200, 80, 40),
        )
    return img.filter(ImageFilter.GaussianBlur(1.2))

def save(img, name, **kwargs):
    img.save(os.path.join(out, name), **kwargs)

save(photo(4032, 3024), "big.jpg", quality=92)      # 4:3，与文档示例一致
save(photo(1600, 1200), "photo.jpg", quality=90)
save(photo(1600, 1200), "photo.png")
save(photo(1600, 1200), "photo.webp", quality=90)
save(photo(800, 600), "a.jpg", quality=88)          # 4:3
save(photo(600, 900), "b.jpg", quality=88)          # 2:3
save(photo(1200, 700), "c.png")

# 带透明通道的 PNG
logo = Image.new("RGBA", (600, 400), (0, 0, 0, 0))
ImageDraw.Draw(logo).ellipse([50, 50, 550, 350], fill=(30, 120, 220, 255))
save(logo, "logo.png")

# 真 GIF。第九阶段 §七 起它是**受支持**的输入格式（背景：这一条以前叫
# 「格式不支持」，现在前端白名单与后端编码器都收它了）
photo(120, 120).convert("P").save(os.path.join(out, "anim.gif"), format="GIF")

# 仍然不受支持的图片扩展名。ICO 在第九阶段只是**输出**格式
# （§七 只有 PNG→ICO），任何页面都不收它当输入 —— 拿它验「本地拦下」这一条
photo(64, 64).save(os.path.join(out, "icon.ico"), format="ICO")

# 头部合法、数据被截断的 JPEG：属于「文件损坏」
buf = io.BytesIO()
photo(800, 600).save(buf, format="JPEG", quality=90)
with open(os.path.join(out, "truncated.jpg"), "wb") as fh:
    fh.write(buf.getvalue()[:900])

# 头部合法、数据被破坏的 PNG
buf = io.BytesIO()
photo(600, 400).save(buf, format="PNG")
data = bytearray(buf.getvalue())
for i in range(200, min(len(data), 4000)):
    data[i] = 0
with open(os.path.join(out, "corrupt.png"), "wb") as fh:
    fh.write(bytes(data))

# 扩展名合法、内容完全不是图片
with open(os.path.join(out, "broken.jpg"), "wb") as fh:
    fh.write(b"this is definitely not a jpeg" * 40)

# 不支持的扩展名
with open(os.path.join(out, "notes.txt"), "w") as fh:
    fh.write("hello")
'''


def find_backend_python() -> pathlib.Path:
    """后端 venv 的 python —— 用它生成测试图片、校验下载结果。"""
    override = os.environ.get("FILETOOLS_BACKEND_PY")
    if override:
        return pathlib.Path(override)
    for candidate in (
        ROOT / "backend" / ".venv" / "Scripts" / "python.exe",
        ROOT / "backend" / ".venv" / "bin" / "python",
    ):
        if candidate.exists():
            return candidate
    return pathlib.Path(sys.executable)


BACKEND_PYTHON = find_backend_python()


def _run_backend_python(code: str, *args: object) -> str:
    """用后端的解释器跑一段代码，返回它的 stdout。

    ``errors="replace"`` 与 ``PYTHONIOENCODING=utf-8`` 都是必须的：子进程的 stdout
    是管道，这时 Python 按 Windows 本地代码页（GBK）写字节，父进程按 utf-8 读，
    只要子进程打印一个汉字就 UnicodeDecodeError —— 而且是在 subprocess 自己的
    **读取线程**里炸。线程一死，``communicate()`` 拿回的就是 ``stdout=None``，
    于是真正的报错会被一起吃掉，在这一层看起来是莫名其妙的 TypeError/AttributeError。
    见 verify_phase5.py 里同名函数上的同一段说明。
    """
    out = subprocess.run(
        [str(BACKEND_PYTHON), "-c", code, *[str(a) for a in args]],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=dict(os.environ, PYTHONIOENCODING="utf-8"),
    )
    if out.returncode != 0:
        raise RuntimeError(out.stderr or out.stdout)
    if out.stdout is None:
        raise RuntimeError(f"子进程没有可读的 stdout。stderr：{out.stderr!r}")
    return out.stdout


def build_fixtures() -> None:
    IMAGES.mkdir(parents=True, exist_ok=True)
    try:
        _run_backend_python(_FIXTURE_CODE, IMAGES)
    except RuntimeError as exc:
        raise SystemExit(f"生成测试图片失败：\n{exc}") from exc


def zip_image_info(zip_path: pathlib.Path) -> list[tuple[str, str, tuple[int, int]]]:
    """用后端的 Pillow 打开 ZIP 里的每张图片，返回 (名称, 格式, 尺寸)。"""
    out = _run_backend_python(_SIZES_CODE, zip_path)
    entries: list[tuple[str, str, tuple[int, int]]] = []
    for part in out.strip().split(";"):
        name, fmt, size = part.split(":")
        width, height = size.strip("()").split(",")
        entries.append((name, fmt, (int(width), int(height))))
    return entries

results: list[tuple[bool, str]] = []
console_errors: list[str] = []


def check(ok: bool, label: str) -> None:
    results.append((ok, label))
    print(f"{'PASS' if ok else 'FAIL'}  {label}")


def attach(page: Page) -> None:
    page.on(
        "console",
        lambda msg: console_errors.append(f"[console.{msg.type}] {msg.text}")
        if msg.type == "error"
        else None,
    )
    page.on("pageerror", lambda err: console_errors.append(f"[pageerror] {err}"))


def upload(page: Page, *names: str) -> None:
    """把文件塞进隐藏的 file input。"""
    page.set_input_files("input[type=file]", [str(IMAGES / n) for n in names])
    page.wait_for_timeout(400)


def pick(page: Page, field: str, value: str) -> None:
    """选中一个单选项。

    单选框是 sr-only 的，真实用户点的是外层 label，所以这里也点 label；
    先把它滚到视口中间，避免被吸顶导航挡住。
    """
    label = page.locator(f"label:has(input[name='{field}'][value='{value}'])")
    label.evaluate("el => el.scrollIntoView({block: 'center'})")
    page.wait_for_timeout(150)
    label.click()
    page.wait_for_timeout(250)


def run(page: Page) -> None:
    # ---------------------------------------------------------------- 首页
    page.goto(BASE, wait_until="networkidle")
    check(
        page.locator("a[href='/image/convert']").count() > 0,
        "首页有格式转换入口",
    )
    check(
        page.locator("a[href='/image/resize']").count() > 0,
        "首页有尺寸调整入口",
    )

    # ------------------------------------------------- 格式转换：同格式提示
    page.goto(f"{BASE}/image/convert", wait_until="networkidle")
    upload(page, "photo.jpg")

    pick(page, "target-format", "jpg")
    body = page.inner_text("body")
    check("已经是 JPG 格式" in body, "选择与原图相同的格式时给出提示")
    check(
        page.get_by_role("button", name="开始转换").is_disabled(),
        "全部文件同格式时禁用转换按钮",
    )

    # --------------------------------------------- 格式转换：PNG 不显示质量
    pick(page, "target-format", "png")
    body = page.inner_text("body")
    check("PNG 是无损格式" in body, "目标为 PNG 时说明不做有损压缩")
    check(
        page.get_by_role("radio", name="高质量").count() == 0,
        "目标为 PNG 时不显示 JPG 那样的质量设置",
    )

    # ---------------------------------------------------- 格式转换：单文件
    page.get_by_role("button", name="开始转换").click()
    expect(page.get_by_text("文件处理完成")).to_be_visible(timeout=60_000)
    body = page.inner_text("body")
    check("原图" in body and "处理后" in body, "结果页展示处理前后对比")
    check("PNG" in body, "结果页展示目标格式")

    with page.expect_download() as info:
        page.get_by_role("button", name="下载文件").click()
    download = info.value
    DOWNLOADS.mkdir(parents=True, exist_ok=True)
    target = DOWNLOADS / download.suggested_filename
    download.save_as(target)
    check(
        target.exists() and target.stat().st_size > 0,
        f"下载转换结果（{download.suggested_filename}）",
    )
    check(
        target.suffix == ".png" and target.stat().st_size > 20_000,
        "转换后的 PNG 体积合理（未被量化压缩）",
    )

    # ------------------------------------------------ 格式转换：质量设置可见
    page.get_by_role("button", name="重新处理").click()
    page.wait_for_timeout(300)
    pick(page, "target-format", "webp")
    check(
        page.get_by_role("radio", name="高质量").count() > 0,
        "目标为 WEBP 时显示质量设置",
    )
    pick(page, "quality-choice", "custom")
    check(
        page.locator("input[type=range]").count() > 0,
        "自定义质量显示 1-100 滑块",
    )
    expect(page.locator("input[type=range]")).to_have_attribute("max", "100")

    # ------------------------------------------------------- 批量 + ZIP 下载
    page.goto(f"{BASE}/image/convert", wait_until="networkidle")
    upload(page, "a.jpg", "b.jpg", "c.png")
    check("已选 3 个文件" in page.inner_text("body"), "批量上传 3 个文件")
    pick(page, "target-format", "webp")
    page.get_by_role("button", name="开始转换").click()
    expect(page.get_by_text("3 个文件处理完成")).to_be_visible(timeout=120_000)

    with page.expect_download() as info:
        page.get_by_role("button", name="下载全部（3 个文件）").click()
    download = info.value
    zip_path = DOWNLOADS / download.suggested_filename
    download.save_as(zip_path)
    check(
        download.suggested_filename == "converted_images.zip",
        f"批量结果打包为 ZIP（{download.suggested_filename}）",
    )
    import zipfile

    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
    check(len(names) == 3, f"ZIP 内含 3 个文件：{names}")
    check(
        all(n.endswith(".webp") for n in names),
        "ZIP 内文件均为目标格式",
    )

    # ------------------------------------------------------------ 尺寸调整
    page.goto(f"{BASE}/image/resize", wait_until="networkidle")
    upload(page, "big.jpg")
    page.wait_for_timeout(600)
    body = page.inner_text("body")
    check("4032 × 3024 px" in body, "显示原始尺寸 4032 × 3024 px")

    # 等比联动：输入宽度 1920，高度应自动变成 1440
    width_input = page.get_by_label("宽度", exact=False).first
    width_input.fill("1920")
    page.wait_for_timeout(250)
    height_value = page.locator("input[type=number]").nth(1).input_value()
    check(height_value == "1440", f"保持比例时宽度 1920 自动得出高度 {height_value}")

    # 常用尺寸预设
    page.get_by_role("button", name="1080 × 1080").click()
    page.wait_for_timeout(250)
    check(
        page.locator("input[type=number]").first.input_value() == "1080"
        and page.locator("input[type=number]").nth(1).input_value() == "1080",
        "点击常用尺寸后自动填入宽高",
    )
    check(
        "1080 × 810 px" in page.inner_text("body"),
        "保持比例时提示实际输出尺寸",
    )

    # 目标大小 + 开始调整
    pick(page, "target-size", "2mb")
    page.get_by_role("button", name="开始调整").click()
    expect(page.get_by_text("文件处理完成")).to_be_visible(timeout=120_000)
    body = page.inner_text("body")
    check("1080 × 810" in body, "结果尺寸与预期一致")

    with page.expect_download() as info:
        page.get_by_role("button", name="下载文件").click()
    download = info.value
    resized = DOWNLOADS / download.suggested_filename
    download.save_as(resized)
    check(download.suggested_filename == "big_resized.jpg", f"结果文件名 {download.suggested_filename}")
    check(resized.stat().st_size <= 2 * 1024 * 1024, "结果不超过目标大小 2 MB")

    # ------------------------------------------------------- 批量尺寸调整
    page.goto(f"{BASE}/image/resize", wait_until="networkidle")
    upload(page, "a.jpg", "b.jpg")
    page.wait_for_timeout(500)
    # 批量时不预填尺寸，两个框都应为空
    check(
        page.locator("input[type=number]").first.input_value() == ""
        and page.locator("input[type=number]").nth(1).input_value() == "",
        "批量上传后不预填尺寸（等待用户指定一边）",
    )
    check(
        "请填写宽度或高度" in page.inner_text("body"),
        "未填写尺寸时给出中性引导而不是报错",
    )
    check(
        page.get_by_role("button", name="批量调整").is_disabled(),
        "未填写尺寸时禁用批量调整按钮",
    )

    # 只填宽度：保持比例时另一边应留空，让每张图按自身比例缩放
    page.get_by_label("宽度", exact=False).first.fill("400")
    page.wait_for_timeout(250)
    check(
        page.locator("input[type=number]").nth(1).input_value() == "",
        "批量时只填宽度不会给高度套上第一张图片的比例",
    )

    page.get_by_role("button", name="批量调整").click()
    expect(page.get_by_text("2 个文件处理完成")).to_be_visible(timeout=120_000)
    with page.expect_download() as info:
        page.get_by_role("button", name="下载全部（2 个文件）").click()
    download = info.value
    zip_path = DOWNLOADS / download.suggested_filename
    download.save_as(zip_path)
    check(
        download.suggested_filename == "resized_images.zip",
        f"批量尺寸调整打包为 ZIP（{download.suggested_filename}）",
    )
    # 各图按各自的比例缩放：a.jpg 是 4:3 -> 400×300，b.jpg 是 2:3 -> 400×600
    info = zip_image_info(zip_path)
    check(
        [name for name, _, _ in info] == ["a_resized.jpg", "b_resized.jpg"],
        f"ZIP 内文件名与顺序：{[n for n, _, _ in info]}",
    )
    check(
        all(size[0] == 400 for _, _, size in info),
        f"每张图片宽度均为 400：{[(n, s) for n, _, s in info]}",
    )
    heights = {name: size[1] for name, _, size in info}
    check(
        heights == {"a_resized.jpg": 300, "b_resized.jpg": 600},
        f"每张图片按各自的原始比例计算高度：{heights}",
    )

    # ---------------------------------------------------------- 错误提示
    # 1) 文件损坏：头部合法、数据被截断
    page.goto(f"{BASE}/image/convert", wait_until="networkidle")
    upload(page, "truncated.jpg")
    pick(page, "target-format", "png")
    page.get_by_role("button", name="开始转换").click()
    page.wait_for_timeout(4000)
    body = page.inner_text("body")
    check("无法读取该图片，请检查文件是否损坏" in body, "损坏的图片给出友好提示")

    page.goto(f"{BASE}/image/convert", wait_until="networkidle")
    upload(page, "corrupt.png")
    # 原图已经是 PNG，同格式转换会被禁用，这里换个目标格式
    pick(page, "target-format", "webp")
    page.get_by_role("button", name="开始转换").click()
    page.wait_for_timeout(4000)
    check(
        "无法读取该图片，请检查文件是否损坏" in page.inner_text("body"),
        "损坏的 PNG 给出友好提示",
    )

    # 2) GIF 现在是受支持的输入（第九阶段 §七 扩了图片家族）。
    #    这一条以前断言的是「GIF 不支持」，那条前提已经作废 ——
    #    换成同样精确的新事实：它真的被收下、真的出现在待处理清单里。
    page.goto(f"{BASE}/image/convert", wait_until="networkidle")
    page.set_input_files("input[type=file]", str(IMAGES / "anim.gif"))
    page.wait_for_timeout(500)
    body = page.inner_text("body")
    check("anim.gif" in body and "1 个文件" in body, "GIF 现在被当作正常输入收下")

    # 3) 仍然不受支持的格式：客户端本地就拦下来，不浪费一次上传
    page.goto(f"{BASE}/image/convert", wait_until="networkidle")
    page.set_input_files("input[type=file]", str(IMAGES / "icon.ico"))
    page.wait_for_timeout(500)
    body = page.inner_text("body")
    check(
        "暂不支持该文件格式" in body and "JPG" in body and "WEBP" in body,
        "不支持的 ICO 给出友好提示",
    )
    # 旧版这里是 `"已选" not in body` —— 那句话在页面上根本不存在，
    # 于是这条断言恒真。改成看文件名有没有真的落进清单。
    check("icon.ico" not in body, "不支持的格式不会被加入待处理列表")

    page.goto(f"{BASE}/image/convert", wait_until="networkidle")
    page.set_input_files("input[type=file]", str(IMAGES / "notes.txt"))
    page.wait_for_timeout(500)
    check(
        "暂不支持该文件格式" in page.inner_text("body"),
        "扩展名不在白名单时给出友好提示",
    )

    # ------------------------------------------------------ 移动端与溢出
    page.set_viewport_size({"width": 390, "height": 844})
    for route in ("/", "/image", "/image/convert", "/image/resize", "/image/compress", "/pdf"):
        page.goto(BASE + route, wait_until="networkidle")
        overflow = page.evaluate(
            "() => document.documentElement.scrollWidth - document.documentElement.clientWidth"
        )
        check(overflow <= 1, f"移动端 {route} 无横向溢出（溢出 {overflow}px）")

    page.set_viewport_size({"width": 1440, "height": 900})
    for route in ("/", "/image/convert", "/image/resize"):
        page.goto(BASE + route, wait_until="networkidle")
        overflow = page.evaluate(
            "() => document.documentElement.scrollWidth - document.documentElement.clientWidth"
        )
        check(overflow <= 1, f"桌面端 {route} 无横向溢出（溢出 {overflow}px）")

    # 第一阶段未被破坏
    page.goto(f"{BASE}/image/compress", wait_until="networkidle")
    upload(page, "photo.jpg")
    check("photo.jpg" in page.inner_text("body"), "第一阶段的压缩页仍可上传")
    page.get_by_role("button", name="开始压缩").click()
    expect(page.get_by_text("文件处理完成")).to_be_visible(timeout=60_000)
    check(True, "第一阶段的压缩流程仍然可用")


def main() -> int:
    build_fixtures()
    DOWNLOADS.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()
        attach(page)
        try:
            run(page)
        finally:
            browser.close()

    # 浏览器会把「服务端返回 4xx」也记成 console error，这是刻意拒绝坏文件的正常现象，
    # 应用侧的友好提示已由上面的用例单独断言，这里只关心真正的脚本报错。
    ignored = ("Failed to load resource",)
    real_errors = [e for e in console_errors if not any(x in e for x in ignored)]
    rejected = [e for e in console_errors if any(x in e for x in ignored)]

    print("\n--- 控制台报错 ---")
    for err in real_errors or ["（无）"]:
        print(err)
    print(f"（服务端正常拒绝坏文件产生的 4xx 记录 {len(rejected)} 条，已忽略）")
    check(not real_errors, "浏览器控制台无 JS 报错")

    failed = [label for ok, label in results if not ok]
    lines = [f"{'PASS' if ok else 'FAIL'}  {label}" for ok, label in results]
    lines.append("")
    lines.append(f"共 {len(results)} 项，通过 {len(results) - len(failed)} 项")
    lines.append(f"服务端拒绝坏文件的 4xx 记录：{len(rejected)} 条（正常）")
    if real_errors:
        lines.append("控制台 JS 报错：")
        lines.extend("  " + err for err in real_errors)
    if failed:
        lines.append("未通过：")
        lines.extend("  - " + label for label in failed)
    else:
        lines.append("全部通过")
    REPORT.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n共 {len(results)} 项，通过 {len(results) - len(failed)} 项")
    print(f"详细报告：{REPORT}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
