#!/usr/bin/env python3
"""第十一阶段 A 补充：Windows 桌面端的**真实构建**（§六 / §二十）。

这个脚本只做一件事：把 ``FileTools.exe`` 和 ``FileTools-Setup-x64.exe``
真的造出来，并留下能复核的证据链。它**不**负责判断「该不该构建」——
那是 ``scripts/verify_desktop.py`` 的事；这里的假设是环境齐了、现在就造。

跑法::

    python scripts/build_windows.py

    FILETOOLS_CARGO_TARGET_DIR   覆盖 cargo 的产物目录（默认见下）

三件事值得写下来，它们都是踩过的坑而不是风格偏好：

1. **``CARGO_TARGET_DIR`` 指到纯 ASCII 路径。** 仓库自己住在
   ``D:\\系统\\FileTools``（含中文），而 NSIS 的 ``makensis`` 对非 ASCII 路径
   历史上有问题；C 盘又只剩十几个 GB。所以默认落到
   ``D:/filetools-build/target``。用**环境变量**而不是
   ``desktop/.cargo/config.toml`` 里的 ``target-dir``：cargo 两者都认，但 Tauri CLI
   找 bundle 目录时可能仍去看 ``src-tauri/target``，于是出现「构建成功却找不到
   安装器」这种自相矛盾的结果。环境变量是两边都认的那一个。

2. **安装器是复制出来的，不是配置出来的。** Tauri 的 NSIS 产物名固定是
   ``FileTools_<版本>_x64-setup.exe``，**没有**改名配置项。所以这里复制成
   ``desktop/artifacts/FileTools-Setup-x64.exe``，并把两边的 sha256 一起打出来 ——
   复制过的文件必须能证明它和被复制的那份逐字节相同。

3. **``npx`` 必须写成解析后的 ``npx.cmd``。** Windows 上 ``subprocess`` 走的是
   ``CreateProcess``，它只在名字没有扩展名时补 ``.exe``，**不认 ``PATHEXT``**。
   直接 ``subprocess.run(["npx", ...])`` 会抛 ``FileNotFoundError``，
   而报错信息是「系统找不到指定的文件」—— 看起来像「没装 Node」，
   实际上 Node 好好的。用 ``shutil.which`` 解出真实路径再执行。
"""

from __future__ import annotations

import hashlib
import os
import pathlib
import shutil
import struct
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
DESKTOP = ROOT / "desktop"
SRC_TAURI = DESKTOP / "src-tauri"

#: 版本真相源（与 ``build_release.py`` 读的是同一个文件）。
VERSION_FILE = ROOT / "VERSION"

#: cargo 的产物目录。**纯 ASCII**，理由见模块开头第 1 条。
DEFAULT_TARGET_DIR = "D:/filetools-build/target"

#: 复制出来的安装器名字。§六 要求的最小产物名就是这个。
INSTALLER_NAME = "FileTools-Setup-x64.exe"

#: PE 头里的 Machine 字段值。
IMAGE_FILE_MACHINE_AMD64 = 0x8664
IMAGE_FILE_MACHINE_I386 = 0x014C
IMAGE_FILE_MACHINE_ARM64 = 0xAA64
MACHINE_NAMES = {
    IMAGE_FILE_MACHINE_AMD64: "x86-64",
    IMAGE_FILE_MACHINE_I386: "x86",
    IMAGE_FILE_MACHINE_ARM64: "ARM64",
}

#: Optional Header 的 Magic：PE32 是 0x10B，PE32+（64 位）是 0x20B。
PE32_MAGIC = 0x10B
PE32_PLUS_MAGIC = 0x20B

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def read_version() -> str:
    version = VERSION_FILE.read_text(encoding="utf-8").strip()
    if not version:
        raise SystemExit(f"{VERSION_FILE} 是空的 —— 版本没有真相源，停止")
    return version


def target_dir() -> pathlib.Path:
    return pathlib.Path(
        os.environ.get("FILETOOLS_CARGO_TARGET_DIR") or DEFAULT_TARGET_DIR
    )


def read_pe(path: pathlib.Path) -> dict:
    """读 PE 头。

    只解析到 Machine 与 Optional Header 的 Magic 为止 —— 这足够回答
    「它是不是一个 64 位的 Windows 可执行文件」，而不必拖进来一个
    反汇编库（本阶段不新增任何依赖）。
    """
    data = path.read_bytes()[:4096]
    if data[:2] != b"MZ":
        raise ValueError("不是 PE 文件（开头不是 MZ）")

    offset = struct.unpack_from("<I", data, 0x3C)[0]
    if data[offset : offset + 4] != b"PE\0\0":
        raise ValueError("PE 签名不对")

    machine = struct.unpack_from("<H", data, offset + 4)[0]
    magic = struct.unpack_from("<H", data, offset + 24)[0]

    return {
        "machine": machine,
        "machine_name": MACHINE_NAMES.get(machine, f"未知(0x{machine:04X})"),
        "magic": magic,
        "pe32_plus": magic == PE32_PLUS_MAGIC,
    }


def run_tauri_build() -> None:
    npx = shutil.which("npx")
    if npx is None:
        raise RuntimeError("PATH 里找不到 npx（Node.js 没装或没进 PATH）")

    env = dict(os.environ)
    env["CARGO_TARGET_DIR"] = str(target_dir())
    env["PYTHONIOENCODING"] = "utf-8"

    command = [npx, "tauri", "build"]
    print(f"工作目录：{DESKTOP}")
    print(f"CARGO_TARGET_DIR={env['CARGO_TARGET_DIR']}")
    print(f"命令：{' '.join(command)}")
    print("-" * 60, flush=True)

    # 构建输出直接流到终端：这一步动辄几分钟，用户该看见它活着
    completed = subprocess.run(command, cwd=DESKTOP, env=env)
    print("-" * 60, flush=True)

    if completed.returncode != 0:
        raise RuntimeError(f"tauri build 退出码 {completed.returncode}")


def main() -> int:
    release = target_dir() / "release"
    artifacts = DESKTOP / "artifacts"
    version = read_version()

    print(f"仓库根：{ROOT}")
    print(f"版本：{version}（来自 VERSION）")
    print(f"cargo 产物目录：{target_dir()}")

    if not (DESKTOP / "node_modules" / "@tauri-apps" / "cli").is_dir():
        print("desktop/node_modules/@tauri-apps/cli 不存在，先在 desktop/ 里 npm install")
        return 2

    run_tauri_build()

    # ---- 1. 主程序：存在、是 64 位 Windows 可执行文件 ----
    exe = release / "FileTools.exe"
    if not exe.is_file():
        print(f"\n构建结束但没找到主程序：{exe}")
        return 1

    try:
        pe = read_pe(exe)
    except ValueError as exc:
        print(f"\n主程序不是合法的 PE 文件：{exc}")
        return 1

    print(f"\n主程序：{exe}")
    print(f"  大小：{exe.stat().st_size:,} 字节")
    print(f"  架构：{pe['machine_name']}"
          f"（Machine=0x{pe['machine']:04X}, Magic=0x{pe['magic']:04X}, "
          f"{'PE32+' if pe['pe32_plus'] else 'PE32'}）")
    print(f"  sha256：{sha256(exe)}")

    if pe["machine"] != IMAGE_FILE_MACHINE_AMD64:
        print(f"\n主程序不是 x64（实际 {pe['machine_name']}）—— §六 只出 x64")
        return 1

    # ---- 2. NSIS 产物：找到它，复制成固定名字 ----
    #
    # **按版本号点名，不按「目录里只有一个」认。** Tauri 把版本写进输出文件名
    # （``FileTools_<版本>_x64-setup.exe``，且没有改名配置项可改），而构建目录是
    # 一轮轮攒下来的：打完 0.1.0，0.1.1 再打一次，那边就并排躺着两个。
    #
    # 早先这里是 ``glob("*-setup.exe")`` 加一句「多于一个就报错」—— 那个判据在
    # 第一轮是对的（确实只有一个），第二轮就变成了「因为上次的产物还没删，所以
    # 这次不干活」。它不是变松了，是**判据选错了维度**：这里要回答的问题是
    # 「本版的那个在哪」，不是「目录里是不是恰好只有一个」。
    bundle = release / "bundle" / "nsis"
    present = sorted(bundle.glob("*-setup.exe")) if bundle.is_dir() else []
    source = bundle / f"FileTools_{version}_x64-setup.exe"
    if not source.is_file():
        print(f"\n没找到本版（{version}）的 NSIS 安装器：{source}")
        if present:
            print(f"  {bundle} 里现有的是：")
            for path in present:
                print(f"  - {path.name}")
        return 1
    stale = [path.name for path in present if path != source]
    if stale:
        print(f"\n注：{bundle} 里还有别的版本的安装器（{', '.join(stale)}）——"
              f" 这一步只按 VERSION={version} 取 {source.name}，它们不影响结果。")

    artifacts.mkdir(parents=True, exist_ok=True)
    destination = artifacts / INSTALLER_NAME
    shutil.copyfile(source, destination)

    source_digest = sha256(source)
    destination_digest = sha256(destination)

    print(f"\n安装器（Tauri 的原始输出，名字无配置项可改）：")
    print(f"  {source.name}")
    print(f"  大小：{source.stat().st_size:,} 字节")
    print(f"  sha256：{source_digest}")
    print(f"\n安装器（复制成 §六 要求的名字）：")
    print(f"  {destination}")
    print(f"  大小：{destination.stat().st_size:,} 字节")
    print(f"  sha256：{destination_digest}")

    if source_digest != destination_digest:
        print("\n复制出来的文件与源文件 sha256 不一致")
        return 1
    print("\n两份 sha256 一致 —— 复制没有改动任何一个字节。")

    # 安装器本身也应当是 PE32（NSIS 在 32 位上跑，装上的是 x64 的主程序）
    try:
        installer_pe = read_pe(destination)
        print(f"安装器架构：{installer_pe['machine_name']}"
              f"（{'PE32+' if installer_pe['pe32_plus'] else 'PE32'}）")
    except ValueError as exc:
        print(f"安装器不是合法的 PE 文件：{exc}")
        return 1

    print("\n构建完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
