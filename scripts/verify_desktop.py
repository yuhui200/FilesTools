#!/usr/bin/env python3
"""第十一阶段 A 补充：Windows 桌面端的验收（§二十 的六项 + §六 的「能构建就真的构建」）。

跑法::

    python scripts/verify_desktop.py                 # 默认真的跑一遍完整构建
    python scripts/verify_desktop.py --skip-build    # 只查配置与产物，跳过构建

前提：``desktop/`` 已经 ``npm install`` 过；只需要 Pillow（自动交给
``backend/.venv`` 的解释器），**不需要后端进程**。

两条贯穿全篇的原则：

1. **断言不是 ``Path.exists()``。** 图标要真的用 Pillow 打开、真的报出它包含的
   那些边长；前端产物要真的把 ``index.html`` 里引用的每个文件去盘上找一遍。
2. **「能构建」的判断与环境缺项分得开。** 环境齐了就**真的构建**，并拿
   ``FileTools.exe`` 的 PE 头与安装器的 sha256 当证据；环境不齐就逐字打印
   ``Windows build not executed because current environment does not provide: …``，
   **既不判 PASS 也不判 FAIL** —— 把「没做」伪装成「做了」或者伪装成「失败」，
   都是在报告里撒谎。构建缺项的判定顺序见 ``probe_build_environment``。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

# 复用生成脚本里的后端解释器探测（Pillow 在那儿）—— 与 verify_branding.py 同一套
sys.path.insert(0, str(ROOT / "scripts"))
import generate_branding as gb  # noqa: E402

DESKTOP = ROOT / "desktop"
SRC_TAURI = DESKTOP / "src-tauri"
TAURI_CONF = SRC_TAURI / "tauri.conf.json"
CARGO_TOML = SRC_TAURI / "Cargo.toml"
WIN_ICONS = SRC_TAURI / "icons"
ARTIFACTS = DESKTOP / "artifacts"

MOBILE_APP_JSON = ROOT / "mobile" / "app.json"
FRONTEND_PACKAGE = ROOT / "frontend" / "package.json"
VERSION_FILE = ROOT / "VERSION"

REPORT = ROOT / "scripts" / "verify_desktop_report.txt"

#: 沿用 mobile/app.json 里两处逐字相同的那个 ID（§八「已有就沿用」）
EXPECTED_IDENTIFIER = "com.filetools.app"
EXPECTED_PRODUCT_NAME = "FileTools"
DEFAULT_TAURI_IDENTIFIER = "com.tauri.dev"

#: Windows 图标必须包含的边长（§十三：至少 16/24/32/48/64/128/256）
REQUIRED_ICO_SIZES = {16, 24, 32, 48, 64, 128, 256}

#: 构建期烤进产物里的后端地址。桌面端不提供服务器地址设置界面，所以它必须在。
BAKED_API_BASE = "http://127.0.0.1:8000"

#: §六 要求的最小产物名
INSTALLER_NAME = "FileTools-Setup-x64.exe"

IMAGE_FILE_MACHINE_AMD64 = 0x8664

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ----------------------------------------------------------------------
# 骨架（与 scripts/verify_*.py 一致）
# ----------------------------------------------------------------------

results: list[tuple[bool, str]] = []
notes: list[str] = []
skipped: list[str] = []


def check(ok: bool, label: str) -> None:
    results.append((bool(ok), label))
    print(f"{'PASS' if ok else 'FAIL'}  {label}", flush=True)


def note(text: str) -> None:
    notes.append(text)
    print(f"NOTE  {text}", flush=True)


def section(title: str) -> None:
    print(f"\n=== {title} ===", flush=True)


def sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def read_json(path: pathlib.Path):
    return json.loads(path.read_text(encoding="utf-8"))


# ----------------------------------------------------------------------
# 图标实测（交给 backend/.venv 的 Pillow）
# ----------------------------------------------------------------------

_INSPECT_CODE = r'''
import json, pathlib, sys
from PIL import Image

root = pathlib.Path(sys.argv[1])
report = {}
for raw in json.loads(sys.argv[2]):
    path = root / raw
    entry = {"exists": path.exists()}
    if entry["exists"]:
        entry["bytes"] = path.stat().st_size
        with Image.open(path) as image:
            entry["size"] = list(image.size)
            entry["mode"] = image.mode
            entry["format"] = image.format
            if image.format == "ICO":
                entry["ico_sizes"] = sorted(list(s) for s in image.info.get("sizes", []))
    report[raw] = entry
print(json.dumps(report))
'''

ICON_FILES = [
    "desktop/src-tauri/icons/icon.ico",
    "desktop/src-tauri/icons/icon-256.png",
]
INSPECTED: dict[str, dict] = {}


def inspect_icons() -> None:
    raw = gb._run_backend_python(_INSPECT_CODE, ROOT, json.dumps(ICON_FILES))
    INSPECTED.update(json.loads(raw))


def entry(path: str) -> dict:
    return INSPECTED.get(path, {"exists": False})


# ----------------------------------------------------------------------
# §一 tauri.conf.json
# ----------------------------------------------------------------------


def load_conf() -> dict | None:
    section("§一 tauri.conf.json")
    check(TAURI_CONF.is_file(), f"配置文件存在：{TAURI_CONF.relative_to(ROOT).as_posix()}")
    if not TAURI_CONF.is_file():
        return None
    try:
        conf = read_json(TAURI_CONF)
    except json.JSONDecodeError as exc:
        check(False, f"配置文件是合法 JSON（解析失败：{exc}）")
        return None
    check(True, "配置文件是合法 JSON")
    return conf


# ----------------------------------------------------------------------
# §二 产品名
# ----------------------------------------------------------------------


def section_product_name(conf: dict) -> None:
    section("§二 产品名")
    name = conf.get("productName")
    check(
        name == EXPECTED_PRODUCT_NAME,
        f'productName == "{EXPECTED_PRODUCT_NAME}"（实际 {name!r}）',
    )
    window_titles = [item.get("title") for item in conf.get("app", {}).get("windows", [])]
    check(
        bool(window_titles) and all(title == EXPECTED_PRODUCT_NAME for title in window_titles),
        f"每个窗口标题都是 {EXPECTED_PRODUCT_NAME}（实际 {window_titles}）",
    )


# ----------------------------------------------------------------------
# §三 应用 ID
# ----------------------------------------------------------------------


def section_identifier(conf: dict) -> None:
    section("§三 应用 ID（§八）")
    identifier = conf.get("identifier")
    check(bool(identifier), "identifier 存在")
    if not identifier:
        return

    check(
        identifier != DEFAULT_TAURI_IDENTIFIER,
        f"identifier 不是脚手架默认值 {DEFAULT_TAURI_IDENTIFIER}",
    )
    check(
        bool(re.fullmatch(r"[A-Za-z0-9\-]+(\.[A-Za-z0-9\-]+)+", identifier)),
        f"identifier 形态合法（{identifier}）",
    )
    check(
        identifier == EXPECTED_IDENTIFIER,
        f"identifier == {EXPECTED_IDENTIFIER}（实际 {identifier}）",
    )

    # §八 说「已有项目 ID 就沿用」：移动端那两处必须逐字相同，
    # 否则同一款应用在三个商店里是三个身份。
    if MOBILE_APP_JSON.is_file():
        app = read_json(MOBILE_APP_JSON)
        expo = app.get("expo", {})
        check(
            expo.get("ios", {}).get("bundleIdentifier") == identifier,
            f"mobile/app.json 的 ios.bundleIdentifier 与桌面端一致"
            f"（{expo.get('ios', {}).get('bundleIdentifier')!r}）",
        )
        check(
            expo.get("android", {}).get("package") == identifier,
            f"mobile/app.json 的 android.package 与桌面端一致"
            f"（{expo.get('android', {}).get('package')!r}）",
        )
    else:
        check(False, "mobile/app.json 存在（要拿它比应用 ID）")


# ----------------------------------------------------------------------
# §四 版本
# ----------------------------------------------------------------------


def resolve_version(conf: dict) -> str | None:
    """把 tauri.conf.json 的 version 解成一个 semver 字符串。

    Tauri 允许这里写**字面量**，也允许写一个指向 ``package.json`` 的路径。
    两种都要认 —— 本项目的选择是路径（版本只有一份真相源），
    但脚本不该假定一定是哪种形态，否则「改回字面量」会让这里静默失效。
    """
    raw = conf.get("version")
    if not isinstance(raw, str) or not raw:
        return None
    if raw.endswith(".json"):
        target = (SRC_TAURI / raw).resolve()
        if not target.is_file():
            return None
        try:
            return read_json(target).get("version")
        except json.JSONDecodeError:
            return None
    return raw


def section_version(conf: dict) -> None:
    section("§四 版本（§十七）")
    version = resolve_version(conf)
    check(
        bool(version) and bool(re.fullmatch(r"\d+\.\d+\.\d+", version or "")),
        f"配置能解出一个合法 semver（实际 {version!r}）",
    )

    if VERSION_FILE.is_file():
        expected = VERSION_FILE.read_text(encoding="utf-8").strip()
        check(
            version == expected,
            f"与仓库根 VERSION 一致（VERSION={expected!r}，桌面端={version!r}）",
        )
    else:
        check(False, "仓库根 VERSION 存在")

    check(
        conf.get("version") == "../../frontend/package.json",
        "版本指向 frontend/package.json（不是又抄了一份字面量）",
    )


# ----------------------------------------------------------------------
# §五 Windows 图标
# ----------------------------------------------------------------------


def section_icons(conf: dict) -> None:
    section("§五 Windows 图标（§十三）")
    icons = conf.get("bundle", {}).get("icon") or []
    check(bool(icons), f"bundle.icon 非空（{icons}）")

    ico_entries = [item for item in icons if str(item).lower().endswith(".ico")]
    check(bool(ico_entries), f"bundle.icon 里有 .ico（{ico_entries}）")

    for relative in icons:
        path = SRC_TAURI / relative
        check(
            path.is_file(),
            f"图标文件存在：{relative}"
            f"（{path.stat().st_size:,} 字节）" if path.is_file() else f"图标文件存在：{relative}",
        )

    # installerIcon 也要在盘上，否则 NSIS 会在构建的最后一步才报错
    installer_icon = conf.get("bundle", {}).get("windows", {}).get("nsis", {}).get("installerIcon")
    if installer_icon:
        check(
            (SRC_TAURI / installer_icon).is_file(),
            f"NSIS installerIcon 存在：{installer_icon}",
        )

    info = entry("desktop/src-tauri/icons/icon.ico")
    check(info.get("exists", False), "icon.ico 真的能被 Pillow 打开")
    if info.get("exists"):
        # Pillow 的 info["sizes"] 是 (宽, 高) 元组的集合，过一趟 JSON 就成了
        # 列表的列表 —— 列表不可哈希，直接 set() 会当场 TypeError。
        # 这里取每个元组的宽度：ICO 的每一档都是正方形。
        sizes = {
            int(item[0]) if isinstance(item, (list, tuple)) else int(item)
            for item in (info.get("ico_sizes") or [])
        }
        missing = sorted(REQUIRED_ICO_SIZES - sizes)
        check(
            not missing,
            f"icon.ico 包含全部要求的边长 {sorted(REQUIRED_ICO_SIZES)}"
            f"（缺 {missing}）" if missing else f"icon.ico 包含全部要求的边长 {sorted(REQUIRED_ICO_SIZES)}",
        )
        check(
            info.get("bytes", 0) > 0,
            f"icon.ico 不是空文件（{info.get('bytes', 0):,} 字节）",
        )

    png = entry("desktop/src-tauri/icons/icon-256.png")
    check(png.get("exists", False), "icon-256.png 存在")
    if png.get("exists"):
        check(
            png.get("size") == [256, 256],
            f"icon-256.png 尺寸正好 256×256（实际 {png.get('size')}）",
        )


# ----------------------------------------------------------------------
# §六 前端产物能被 Tauri 消费
# ----------------------------------------------------------------------


def section_frontend_dist(conf: dict) -> None:
    section("§六 前端产物（真产物，不是「文件在」）")
    raw = conf.get("build", {}).get("frontendDist")
    check(bool(raw), f"frontendDist 配了（{raw!r}）")
    if not raw:
        return

    if pathlib.PurePath(str(raw)).is_absolute():
        check(False, "frontendDist 是相对路径（绝对路径在 Windows 上已知是坏的，tauri#14005）")
        return
    check(True, "frontendDist 是相对路径（绝对路径在 Windows 上已知是坏的）")

    dist = (SRC_TAURI / raw).resolve()
    check(dist.is_dir(), f"产物目录存在：{dist}")

    index = dist / "index.html"
    check(index.is_file(), "产物里有 index.html")
    if not index.is_file():
        return

    html = index.read_text(encoding="utf-8")

    # 自定义协议下 `/assets/...` 会指到根上，白屏的典型成因
    references = re.findall(r'(?:src|href)="([^"]+)"', html)
    local = [item for item in references if not item.startswith(("http://", "https://", "data:", "#"))]
    check(bool(local), f"index.html 里有本地引用（{len(local)} 条）")

    absolute = [item for item in local if item.startswith("/")]
    check(
        not absolute,
        f"本地引用都是相对路径（§ 绝对路径会白屏，实际 {absolute}）"
        if absolute
        else "本地引用都是相对路径（相对路径才在自定义协议下加载得到）",
    )

    for item in local:
        resolved = (dist / item.lstrip("./")).resolve()
        check(resolved.is_file(), f"被引用的文件在盘上：{item}")

    # 烤进去的后端地址：证明 define 真的生效了，而不是走了空串相对路径
    scripts = sorted(dist.glob("assets/*.js"))
    check(bool(scripts), f"产物里有打包好的 JS（{len(scripts)} 个）")
    haystack = "\n".join(path.read_text(encoding="utf-8", errors="replace") for path in scripts)
    check(
        BAKED_API_BASE in haystack,
        f"打包产物里烤进了 {BAKED_API_BASE}（define 真的生效了）",
    )
    check(
        "VITE_API_BASE_URL" not in haystack,
        "产物里没有残留的 VITE_API_BASE_URL 占位符",
    )

    # 桌面端才有的那些东西：Web 产物里一个都不该有（反之亦然）
    for marker in ("save_result", "x-filetools-filename", "__TAURI__"):
        check(marker in haystack, f"桌面产物里有桌面专属标记 {marker}")

    css = sorted(dist.glob("assets/*.css"))
    check(bool(css), f"产物里有 CSS（{len(css)} 个）")


# ----------------------------------------------------------------------
# §七 构建环境判定
# ----------------------------------------------------------------------


def probe_cargo() -> str | None:
    """cargo 在，且 host 就是 x86_64-pc-windows-msvc。"""
    cargo = shutil.which("cargo")
    if cargo is None:
        return "Rust 工具链（cargo 不在 PATH 里）"
    rustc = shutil.which("rustc")
    if rustc is None:
        return "Rust 工具链（rustc 不在 PATH 里）"
    try:
        out = subprocess.run(
            [rustc, "-vV"], capture_output=True, text=True, timeout=60
        ).stdout
    except OSError:
        return "Rust 工具链（rustc 跑不起来）"
    host = ""
    for line in out.splitlines():
        if line.startswith("host:"):
            host = line.split(":", 1)[1].strip()
    if host != "x86_64-pc-windows-msvc":
        return f"Rust 的 host target（实际 {host or '解不出来'}）"
    return None


def vswhere_path() -> pathlib.Path | None:
    base = os.environ.get("ProgramFiles(x86)") or os.environ.get("ProgramFiles")
    if not base:
        return None
    candidate = pathlib.Path(base) / "Microsoft Visual Studio" / "Installer" / "vswhere.exe"
    return candidate if candidate.is_file() else None


def probe_msvc() -> str | None:
    """VC 生成工具（cl.exe 不在 PATH 是正常的，rustc 自己去 vswhere 找）。"""
    vswhere = vswhere_path()
    if vswhere is None:
        return "MSVC 生成工具（找不到 vswhere.exe）"
    try:
        out = subprocess.run(
            [
                str(vswhere),
                "-latest",
                "-products", "*",
                "-requires", "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
                "-property", "installationPath",
            ],
            capture_output=True,
            text=True,
            timeout=120,
        ).stdout.strip()
    except OSError:
        return "MSVC 生成工具（vswhere 跑不起来）"
    return None if out else "MSVC 生成工具（vswhere 没找到 VC.Tools.x86.x64 组件）"


def probe_rc() -> str | None:
    """Windows SDK 的资源编译器。Tauri 打 exe 时要嵌图标和版本信息。"""
    base = os.environ.get("ProgramFiles(x86)") or os.environ.get("ProgramFiles")
    if not base:
        return "Windows SDK 的 rc.exe（找不到 Program Files）"
    kits = pathlib.Path(base) / "Windows Kits" / "10" / "bin"
    if not kits.is_dir():
        return "Windows SDK 的 rc.exe（没有 Windows Kits/10/bin）"
    for candidate in sorted(kits.glob("*/x64/rc.exe"), reverse=True):
        if candidate.is_file():
            return None
    return "Windows SDK 的 rc.exe（Windows Kits/10/bin/*/x64 下没有）"


def probe_cli() -> str | None:
    cli = DESKTOP / "node_modules" / "@tauri-apps" / "cli"
    return None if cli.is_dir() else "@tauri-apps/cli（desktop/node_modules 里没有）"


def probe_build_environment() -> list[str]:
    """返回环境里**缺**的东西。空列表表示可以真的构建。"""
    missing: list[str] = []
    for probe in (probe_cargo, probe_msvc, probe_rc, probe_cli):
        problem = probe()
        if problem:
            missing.append(problem)
    return missing


def read_pe_machine(path: pathlib.Path) -> int | None:
    try:
        data = path.read_bytes()[:4096]
        if data[:2] != b"MZ":
            return None
        offset = int.from_bytes(data[0x3C:0x40], "little")
        if data[offset : offset + 4] != b"PE\0\0":
            return None
        return int.from_bytes(data[offset + 4 : offset + 6], "little")
    except (OSError, IndexError):
        return None


def section_build(skip: bool) -> None:
    section("§七 真实构建（§六 / §二十）")

    missing = probe_build_environment()
    if missing:
        message = (
            "Windows build not executed because current environment does not provide: "
            + "; ".join(missing)
        )
        note(message)
        skipped.append(message)
        print(f"\n{message}", flush=True)
        return
    note("构建环境齐备：cargo(host x86_64-pc-windows-msvc)、MSVC 生成工具、rc.exe、@tauri-apps/cli")

    if skip:
        message = "本次跳过了构建（--skip-build）：安装器是上一次跑出来的，不代表今天能构建"
        note(message)
        skipped.append(message)
        print(f"\n{message}", flush=True)
        return

    print("\n开始 tauri build（这一步要几分钟）……", flush=True)
    completed = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "build_windows.py")],
        cwd=ROOT,
    )
    check(completed.returncode == 0, f"scripts/build_windows.py 退出码 0（实际 {completed.returncode}）")
    if completed.returncode != 0:
        return

    target_dir = pathlib.Path(
        os.environ.get("FILETOOLS_CARGO_TARGET_DIR") or "D:/filetools-build/target"
    )
    exe = target_dir / "release" / "FileTools.exe"
    check(exe.is_file(), f"release/FileTools.exe 存在（{exe}）")
    if exe.is_file():
        machine = read_pe_machine(exe)
        check(
            machine == IMAGE_FILE_MACHINE_AMD64,
            f"FileTools.exe 是 x86-64 的 PE"
            f"（Machine=0x{machine:04X}）" if machine is not None
            else "FileTools.exe 的 PE 头读不出来",
        )
        note(f"release/FileTools.exe：{exe.stat().st_size:,} 字节，sha256={sha256(exe)}")

    installer = ARTIFACTS / INSTALLER_NAME
    check(installer.is_file(), f"安装器存在：desktop/artifacts/{INSTALLER_NAME}")
    if installer.is_file():
        note(f"{INSTALLER_NAME}：{installer.stat().st_size:,} 字节，sha256={sha256(installer)}")
        check(installer.stat().st_size > 0, "安装器不是空文件")

        # 与 bundle 目录里的源文件逐字节对照 —— 证明复制没有改动内容
        produced = sorted((target_dir / "release" / "bundle" / "nsis").glob("*-setup.exe"))
        if len(produced) == 1:
            check(
                sha256(produced[0]) == sha256(installer),
                f"与 Tauri 的原始产物 {produced[0].name} sha256 一致",
            )
            note(f"Tauri 原始产物名：{produced[0].name}（NSIS 输出名无配置项可改）")
        else:
            check(False, f"bundle/nsis 下的安装器数量应为 1（实际 {len(produced)}）")


# ----------------------------------------------------------------------
# 报告
# ----------------------------------------------------------------------


def write_report() -> tuple[int, int]:
    passed = sum(1 for ok, _ in results if ok)
    failed = [label for ok, label in results if not ok]
    lines = [
        "第十一阶段 A 补充（Windows 桌面端）验收报告",
        f"通过：{passed} / {len(results)}",
        "",
    ]
    if skipped:
        lines.append("⚠️ 未执行的部分（不是通过，也不是失败）：")
        lines.extend(f"  · {item}" for item in skipped)
        lines.append("")
    if notes:
        lines.append("如实记录（不是断言）：")
        lines.extend(f"  · {item}" for item in notes)
        lines.append("")
    if failed:
        lines.append("失败项：")
        lines.extend(f"  - {label}" for label in failed)
    else:
        lines.append("全部通过。")
    REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return passed, len(results)


def summary() -> int:
    passed, total = write_report()
    print(f"\n=== 汇总：{passed}/{total} 通过 ===", flush=True)
    print(f"报告写入 {REPORT.relative_to(ROOT).as_posix()}", flush=True)
    for item in skipped:
        print(f"\n⚠️  {item}", flush=True)
    failed = [label for ok, label in results if not ok]
    if failed:
        print("\n失败项：", flush=True)
        for label in failed:
            print(f"  - {label}", flush=True)
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Windows 桌面端验收")
    parser.add_argument(
        "--skip-build",
        action="store_true",
        help="跳过完整构建（报告里会显式记录「本次跳过了构建」，不会静默变绿）",
    )
    args = parser.parse_args()

    print(f"仓库根：{ROOT}")
    print(f"后端解释器（Pillow）：{gb.BACKEND_PYTHON}")

    inspect_icons()

    conf = load_conf()
    if conf is None:
        return summary()

    section_product_name(conf)
    section_identifier(conf)
    section_version(conf)
    section_icons(conf)

    # 顺序是有意的：构建会**重新生成** frontend/dist-desktop，所以「前端产物
    # 能被 Tauri 消费」那一段必须排在构建之后 —— 否则它查验的可能是上一次
    # 构建的陈旧产物，一份坏掉的今天照样能绿。
    section_build(args.skip_build)
    section_frontend_dist(conf)

    return summary()


if __name__ == "__main__":
    sys.exit(main())
