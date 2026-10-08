#!/usr/bin/env python3
"""v0.1.0 正式发行：把**已经构建好的**产物收进 ``release/v0.1.0/``，并算出清单。

这个脚本**不构建任何东西** —— 那是 ``build_windows.py`` / ``npm run build`` /
``gradlew assembleRelease`` 的事。它只做三件事：

1. 把各平台的产物按固定布局收进 ``release/<版本>/``，Web 那份打成 zip；
2. 逐文件算 sha256，写 ``SHA256SUMS.txt``（``sha256sum -c`` 能直接校验的格式）；
3. 写 ``RELEASE-MANIFEST.json`` —— **产物清单的机器可读形态**，
   含每一项的字节数、sha256、**签名状态**与它没做到的地方。

两条刻意的设计：

* **zip 是确定性的。** 条目按名字排序、时间戳钉死在 ``ZIP_EPOCH``、
  压缩级别固定 —— 同一份 ``frontend/dist`` 打两次必须得到**逐字节相同**的 zip。
  这和 ``verify_branding.py`` 里「现场渲一次、断言逐像素相同」是同一种态度：
  产物要可复现，否则 sha256 这个数字就没有意义。

* **签名状态写进清单，不留给读者猜。** Android 那份是 ``assembleRelease`` 出来的，
  但 ``app/build.gradle`` 的 ``release`` 块里写的是 ``signingConfig signingConfigs.debug``
  —— 它是 **debug keystore 签的**，能装、能用，**不能上架**。清单里如实写 ``debug``。

跑法::

    python scripts/build_release.py            # 收产物 + 写清单
    python scripts/build_release.py --web-only # 只做 Web 那份（产物还没齐时）
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import pathlib
import shutil
import subprocess
import sys
import zipfile

ROOT = pathlib.Path(__file__).resolve().parent.parent

#: 版本真相源。发行目录名必须与它一致，不另抄一份。
VERSION_FILE = ROOT / "VERSION"

#: zip 条目里钉死的时间戳。1980-01-01 是 ZIP 格式的纪元下界。
ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)

PRODUCT = "FileTools"

#: 各平台产物：源路径 -> 收纳进发行目录的相对路径。
WEB_DIST = ROOT / "frontend" / "dist"
WINDOWS_INSTALLER = ROOT / "desktop" / "artifacts" / "FileTools-Setup-x64.exe"
#: Android APK 可能落在这两个地方之一，按顺序找：
#:
#: 1. 仓库里的常规位置；
#: 2. **纯 ASCII 的构建工作区** ``D:/filetools-build/mobile``。
#:
#: 第 2 条不是随手加的：AGP 会拒绝含非 ASCII 字符的项目路径
#: （``Your project path contains non-ASCII characters``），而本仓库住在
#: ``D:\\系统\\FileTools``。试过用 ``subst`` 映射一个盘符绕开 —— 不行：
#: React Native 的 codegen 会在 ``X:\\…`` 与 ``D:\\系统\\…`` 之间算相对路径，
#: 两个根不一致就直接抛 ``this and base files have different roots``。
#: 所以 Android 的构建**真的在纯 ASCII 路径下进行**（把 ``mobile/`` 复制过去），
#: 与 Rust 那边用 ``CARGO_TARGET_DIR`` 避开同一个坑是同一种做法。
ANDROID_APK_CANDIDATES = (
    ROOT / "mobile" / "android" / "app" / "build" / "outputs" / "apk" / "release"
    / "app-release.apk",
    pathlib.Path("D:/filetools-build/mobile/android/app/build/outputs/apk/release"
                 "/app-release.apk"),
)


def find_android_apk() -> pathlib.Path | None:
    for candidate in ANDROID_APK_CANDIDATES:
        if candidate.is_file():
            return candidate
    return None

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def read_version() -> str:
    version = VERSION_FILE.read_text(encoding="utf-8").strip()
    if not version:
        raise SystemExit(f"{VERSION_FILE} 是空的 —— 版本没有真相源，停止")
    return version


def sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def git(*args: str) -> str:
    try:
        done = subprocess.run(
            ["git", *args], cwd=str(ROOT), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""


def zip_tree(source: pathlib.Path, destination: pathlib.Path) -> None:
    """把 ``source`` 整棵树打成确定性 zip（路径相对 ``source``）。"""
    entries = sorted(p for p in source.rglob("*") if p.is_file())
    if not entries:
        raise SystemExit(f"{source} 里一个文件都没有，先跑 `npm run build`")

    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in entries:
            relative = path.relative_to(source).as_posix()
            info = zipfile.ZipInfo(relative, date_time=ZIP_EPOCH)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16          # 普通文件、权限固定
            archive.writestr(info, path.read_bytes())


def stage(source: pathlib.Path, destination: pathlib.Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)


def collect(version: str, web_only: bool) -> tuple[list[dict], list[dict]]:
    """收产物，返回 (收进来的, 没收进来的)。"""
    release = ROOT / "release" / f"v{version}"
    staged: list[dict] = []
    missing: list[dict] = []

    # ---- Web：静态站点，打 zip ----
    if not WEB_DIST.is_dir():
        missing.append({
            "platform": "web",
            "reason": "frontend/dist 不存在 —— 先在 frontend/ 里跑 `npm run build`",
        })
    else:
        target = release / "web" / f"{PRODUCT}-Web-{version}.zip"
        zip_tree(WEB_DIST, target)
        files = sum(1 for p in WEB_DIST.rglob("*") if p.is_file())
        staged.append({
            "platform": "web",
            "file": target.relative_to(release).as_posix(),
            "bytes": target.stat().st_size,
            "sha256": sha256(target),
            "kind": "静态站点（由后端托管，也可独立部署）",
            "signed": None,
            "detail": f"frontend/dist 的 {files} 个文件，确定性 zip（时间戳钉在 {ZIP_EPOCH[0]}）",
        })

    if web_only:
        return staged, missing

    # ---- Windows：NSIS 安装器 ----
    if not WINDOWS_INSTALLER.is_file():
        missing.append({
            "platform": "windows",
            "reason": f"{WINDOWS_INSTALLER.relative_to(ROOT).as_posix()} 不存在"
                      " —— 先跑 `python scripts/build_windows.py`",
        })
    else:
        target = release / "windows" / "FileTools-Setup-x64.exe"
        stage(WINDOWS_INSTALLER, target)
        # 与构建目录里的原始输出对账：复制不能改动任何一个字节
        bundle = pathlib.Path("D:/filetools-build/target/release/bundle/nsis")
        originals = sorted(bundle.glob("*-setup.exe")) if bundle.is_dir() else []
        detail = "NSIS 安装器，仅 x64"
        if originals:
            detail += (f"；与 Tauri 原始输出 {originals[0].name} 逐字节相同"
                       if sha256(originals[0]) == sha256(target)
                       else "；⚠️ 与 Tauri 原始输出不一致")
        staged.append({
            "platform": "windows",
            "file": target.relative_to(release).as_posix(),
            "bytes": target.stat().st_size,
            "sha256": sha256(target),
            "kind": "NSIS 安装包（currentUser，免 UAC）",
            "signed": "none",
            "detail": detail,
        })

    # ---- Android：release APK（debug 签名）----
    android_apk = find_android_apk()
    if android_apk is None:
        missing.append({
            "platform": "android",
            "reason": "找不到 app-release.apk —— 先在 mobile/android 里跑 "
                      "`gradlew assembleRelease`（项目路径含非 ASCII 时见 "
                      "ANDROID_APK_CANDIDATES 的注释：要到纯 ASCII 路径下构建）",
        })
    else:
        target = release / "android" / f"{PRODUCT}-{version}-release.apk"
        stage(android_apk, target)
        staged.append({
            "platform": "android",
            "file": target.relative_to(release).as_posix(),
            "bytes": target.stat().st_size,
            "sha256": sha256(target),
            "kind": "Android APK（含 4 个 ABI）",
            "signed": "debug",
            "detail": f"由 `gradlew assembleRelease` 产出（构建于 {android_apk.parent}"
                      "）；app/build.gradle 的 release 块用的是 signingConfigs.debug"
                      " —— **debug keystore 签名，可安装、不可上架**",
        })

    return staged, missing


def write_manifest(version: str, staged: list[dict], missing: list[dict]) -> pathlib.Path:
    release = ROOT / "release" / f"v{version}"
    manifest = {
        "product": PRODUCT,
        "version": version,
        "generated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "git": {
            "commit": git("rev-parse", "HEAD"),
            "branch": git("rev-parse", "--abbrev-ref", "HEAD"),
            "dirty": bool(git("status", "--porcelain")),
        },
        "artifacts": staged,
        "not_built": missing + [
            {"platform": "ios",
             "reason": "本机没有 macOS，没有 Xcode —— 一行都构建不了"},
            {"platform": "docker",
             "reason": "本机没有可用的 docker（开发机是 Windows）"},
        ],
    }
    path = release / "RELEASE-MANIFEST.json"
    path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return path


def write_sums(version: str, staged: list[dict]) -> pathlib.Path:
    """写标准的 ``sha256sum -c`` 格式（路径相对发行目录）。"""
    release = ROOT / "release" / f"v{version}"
    lines = [f"{item['sha256']}  {item['file']}" for item in staged]
    path = release / "SHA256SUMS.txt"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="收拢 v0.1.0 发行产物")
    parser.add_argument("--web-only", action="store_true",
                        help="只做 Web 那份（Windows/Android 产物还没好时）")
    args = parser.parse_args()

    version = read_version()
    release = ROOT / "release" / f"v{version}"
    print(f"仓库根：{ROOT}")
    print(f"版本：{version}（来自 VERSION）")
    print(f"发行目录：{release.relative_to(ROOT).as_posix()}")
    print("-" * 60, flush=True)

    staged, missing = collect(version, args.web_only)

    for item in staged:
        print(f"[收] {item['platform']:<8} {item['file']}")
        print(f"     {item['bytes']:,} 字节  sha256 {item['sha256']}")
        print(f"     {item['detail']}")
    for item in missing:
        print(f"[缺] {item['platform']:<8} {item['reason']}")

    if not staged:
        print("\n一个产物都没有，什么都没写。")
        return 1

    manifest = write_manifest(version, staged, missing)
    sums = write_sums(version, staged)

    print("-" * 60)
    print(f"清单：{manifest.relative_to(ROOT).as_posix()}")
    print(f"校验：{sums.relative_to(ROOT).as_posix()}（{len(staged)} 条）")
    if missing:
        print(f"\n{len(missing)} 个平台本轮没有产物，已如实写进清单的 not_built。")
    print("\n完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
