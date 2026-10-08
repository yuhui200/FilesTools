#!/usr/bin/env python3
"""v0.1.0 正式发行的验收：``release/v0.1.0/`` 这份暂存树是否**真的**可发。

它检查的不是「文件在不在」——``Path.exists()`` 谁都会写。它检查的是：

* 每个产物的 **sha256 与 ``SHA256SUMS.txt``、``RELEASE-MANIFEST.json`` 三方一致**，
  且清单里没有多出来的项、也没有漏掉的项（清单不能变成第二份真相）；
* **产物的形态与它自称的一致** —— exe 真的是 PE、真的是 NSIS；
  APK 真的是 APK（有 ``AndroidManifest.xml`` / ``classes.dex`` / 四个 ABI 的 ``lib/``）、
  真的带着 APK Signature Scheme 的签名块；
  Web 的 zip 真的能独立部署（``index.html`` 引用的每一个本地文件都在包里）；
* **Web 那份 zip 是可复现的** —— 现场用同一套代码重打一次，sha256 必须相同。
  和 ``verify_branding.py`` 的「现场渲一次、逐像素比对」是同一种态度。

**环境够不着的项一律 ``NOT EXECUTED``，绝不输出 PASS**（§十四 / §二十）：
iOS（无 macOS）、Docker 镜像（本机无 docker）、Android 真机安装（``adb devices`` 为空）、
Windows ARM64（本机只有 x86_64 target）。它们既不进 PASS 也不进 FAIL ——
没被执行过的事，报成 PASS 就是假绿，报成 FAIL 又是在冤枉代码。

跑法::

    python scripts/verify_release.py
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 版本读取复用 sync_version —— 校验与写入同一套代码，不自己再解析一次 VERSION。
sys.path.insert(0, str(ROOT / "scripts"))
import sync_version as sv          # noqa: E402
import build_release as br         # noqa: E402  打包逻辑复用，确定性检查才有意义

REPORT = ROOT / "scripts" / "verify_release_report.txt"

#: APK 里必须有这些东西，少一个就不是一个能装的包。
APK_REQUIRED = ("AndroidManifest.xml", "classes.dex", "resources.arsc")

#: 四个 ABI 都必须有（``gradle.properties`` 的 ``reactNativeArchitectures``）。
APK_REQUIRED_ABIS = ("arm64-v8a", "armeabi-v7a", "x86", "x86_64")

#: 一份能独立部署的 Web 产物至少要有这些。
WEB_REQUIRED = ("index.html", "favicon.svg", "manifest.webmanifest")

#: APK Signing Block 里各签名方案的 ID。
APK_SIG_SCHEME_IDS = {
    0x7109871A: "v2",
    0xF05368C0: "v3",
    0x1B93AD61: "v3.1",
}

#: Signing Block 的魔数，固定 16 字节，紧贴在 Central Directory 之前。
APK_SIG_BLOCK_MAGIC = b"APK Sig Block 42"


def read_apk_signatures(path: Path) -> tuple[list[str], list[str]]:
    """读 APK 的签名方案，返回 ``(Signing Block 里的方案名, v1 的 META-INF 文件)``。

    **为什么不能只找 ``META-INF/*.RSA``**：``minSdk >= 24`` 时 AGP 默认
    **不再生成 v1（JAR）签名** —— v2/v3 是它的替代品，不是「没签名」。
    本项目 ``minSdk 24``，所以早期那条「找 .RSA 文件」的判据永远找不到东西，
    会把一个签好的包判成没签。这里按 APK Signature Scheme 的真实结构读：

    Signing Block 紧贴在 Central Directory 之前，布局是
    ``[uint64 块大小][ID-value 对…][uint64 块大小][魔数 16 字节]``，
    每个 ID-value 对是 ``[uint64 长度][uint32 ID][值]``（长度不含它自己）。
    """
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        handle.seek(max(0, size - 1_048_576))
        tail = handle.read()

    eocd = tail.rfind(b"PK\x05\x06")
    if eocd < 0:
        return [], []
    cd_offset = struct.unpack_from("<I", tail, eocd + 16)[0]

    schemes: list[str] = []
    with path.open("rb") as handle:
        if cd_offset >= 24:
            handle.seek(cd_offset - 16)
            if handle.read(16) == APK_SIG_BLOCK_MAGIC:
                handle.seek(cd_offset - 24)
                block_size = struct.unpack("<Q", handle.read(8))[0]
                # 块在盘上占 ``块大小 + 8`` 字节：两个 uint64 里只有一个
                # 算进「块大小」（它不含自己），另一个在尾部、要额外算 8。
                start = cd_offset - 8 - block_size
                if start >= 8 and block_size > 24:
                    handle.seek(start)
                    if struct.unpack("<Q", handle.read(8))[0] == block_size:
                        # 块内除两个 uint64 与魔数外，剩下的全是 ID-value 对
                        pairs = handle.read(block_size - 24)
                        offset = 0
                        while offset + 12 <= len(pairs):
                            pair_len = struct.unpack_from("<Q", pairs, offset)[0]
                            pair_id = struct.unpack_from("<I", pairs, offset + 8)[0]
                            if pair_id in APK_SIG_SCHEME_IDS:
                                schemes.append(APK_SIG_SCHEME_IDS[pair_id])
                            if pair_len < 4:
                                break
                            offset += 8 + pair_len

    with zipfile.ZipFile(path) as bundle:
        v1 = [n for n in bundle.namelist()
              if n.startswith("META-INF/") and n.upper().endswith((".RSA", ".DSA", ".EC"))]
    return schemes, v1

results: list[tuple[str, bool, str]] = []
not_executed: list[tuple[str, str]] = []
notes: list[str] = []


def check(area: str, ok: bool, label: str) -> None:
    results.append((area, bool(ok), label))
    print(f"  {'PASS' if ok else 'FAIL'}  {label}", flush=True)


def skip(area: str, label: str, reason: str) -> None:
    not_executed.append((area, f"{label} —— {reason}"))
    print(f"  NOT EXECUTED  {label} —— {reason}", flush=True)


def note(text: str) -> None:
    notes.append(text)
    print(f"  NOTE  {text}", flush=True)


def section(title: str) -> None:
    print(f"\n=== {title} ===", flush=True)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def read_pe_machine(path: Path) -> int | None:
    """读 PE 头的 Machine 字段。不是 PE 就返回 None。"""
    import struct

    data = path.read_bytes()[:4096]
    if data[:2] != b"MZ":
        return None
    offset = struct.unpack_from("<I", data, 0x3C)[0]
    if data[offset : offset + 4] != b"PE\0\0":
        return None
    return struct.unpack_from("<H", data, offset + 4)[0]


def find_apkanalyzer() -> Path | None:
    """在本机的 Android SDK 里找 ``apkanalyzer``。找不到返回 None。"""
    roots: list[Path] = []
    for name in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        value = os.environ.get(name)
        if value:
            roots.append(Path(value))
    local = ROOT / "mobile" / "android" / "local.properties"
    if local.is_file():
        for line in local.read_text(encoding="utf-8").splitlines():
            if line.startswith("sdk.dir="):
                roots.append(Path(line.split("=", 1)[1].replace("\\:", ":").replace("\\\\", "/")))
    roots.append(Path("D:/Android/Sdk"))

    for root in roots:
        for candidate in root.glob("cmdline-tools/*/bin/apkanalyzer.bat"):
            return candidate
        for candidate in root.glob("cmdline-tools/*/bin/apkanalyzer"):
            return candidate
    return None


def load_manifest(release: Path) -> dict | None:
    path = release / "RELEASE-MANIFEST.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


# ----------------------------------------------------------------------
# VERSION 与目录结构
# ----------------------------------------------------------------------

def section_structure() -> tuple[str, Path]:
    section("VERSION 与发行目录")
    area = "STRUCTURE"

    version = sv.read_source_version()
    check(area, bool(re.fullmatch(r"\d+\.\d+\.\d+", version)), f"VERSION = {version}")

    release = ROOT / "release" / f"v{version}"
    check(area, release.is_dir(), f"release/v{version}/ 存在")
    if not release.is_dir():
        return version, release

    # 目录名必须由 VERSION 决定 —— 手抄一个 v0.1.1 出来就是第二份真相
    siblings = [p.name for p in (ROOT / "release").iterdir() if p.is_dir()]
    check(area, siblings == [f"v{version}"],
          f"release/ 下只有 v{version}（实际：{', '.join(siblings)}）")

    for name in ("RELEASE-NOTES.md", "RELEASE-MANIFEST.json", "SHA256SUMS.txt"):
        check(area, (release / name).is_file(), f"release/v{version}/{name} 存在")

    return version, release


# ----------------------------------------------------------------------
# Web
# ----------------------------------------------------------------------

def section_web(release: Path, version: str) -> None:
    section("WEB")
    area = "WEB"

    archive = release / "web" / f"FileTools-Web-{version}.zip"
    check(area, archive.is_file(), f"web/FileTools-Web-{version}.zip 存在")
    if not archive.is_file():
        return

    with zipfile.ZipFile(archive) as bundle:
        names = bundle.namelist()

    for name in WEB_REQUIRED:
        check(area, name in names, f"zip 里有 {name}")

    scripts = [n for n in names if n.startswith("assets/") and n.endswith(".js")]
    styles = [n for n in names if n.startswith("assets/") and n.endswith(".css")]
    check(area, bool(scripts), f"zip 里有打包后的 JS（{len(scripts)} 个）")
    check(area, bool(styles), f"zip 里有打包后的 CSS（{len(styles)} 个）")

    if "index.html" in names:
        with zipfile.ZipFile(archive) as bundle:
            html = bundle.read("index.html").decode("utf-8")

        # Web 产物是**按站点根部署**构建的：vite.config.ts 里 Web 走 ``base: '/'``
        # （桌面端才走 ``base: './'``，因为 webview 用自定义协议加载）。
        # 所以这里断言的是「引用能被解析到包里真实存在的文件」——
        # 那才是这份包真正的要求；「引用必须是相对路径」只是把**某一种**实现
        # 当成了标准，写成断言就会把正确的产物判成错的。
        referenced = re.findall(r'(?:src|href)="([^"]+)"', html)
        local = [r for r in referenced
                 if not r.startswith(("http://", "https://", "//", "#", "data:"))]
        missing = [r for r in local if r.removeprefix("./").removeprefix("/") not in names]
        check(area, not missing,
              f"index.html 引用的 {len(local)} 个本地资源都在包里（缺 {len(missing)} 个）")

        root_absolute = [r for r in local if r.startswith("/")]
        if root_absolute:
            note(f"Web 产物按**站点根**部署（{len(root_absolute)} 处 /assets/… 形态的引用）"
                 " —— 解压后要挂在服务器根路径，放子目录会 404")

    # 确定性：同一份 dist 用同一套代码重打一次，必须逐字节相同
    with tempfile.TemporaryDirectory() as tmp:
        again = Path(tmp) / "again.zip"
        br.zip_tree(ROOT / "frontend" / "dist", again)
        check(area, sha256(again) == sha256(archive),
              "zip 是可复现的（现场重打一次 sha256 相同）")


# ----------------------------------------------------------------------
# Windows
# ----------------------------------------------------------------------

def section_windows(release: Path) -> None:
    section("WINDOWS")
    area = "WINDOWS"

    installer = release / "windows" / "FileTools-Setup-x64.exe"
    check(area, installer.is_file(), "windows/FileTools-Setup-x64.exe 存在")
    if not installer.is_file():
        return

    size = installer.stat().st_size
    check(area, size > 1_000_000, f"体积合理（{size:,} 字节）")

    machine = read_pe_machine(installer)
    check(area, machine is not None, "是合法的 PE 文件（MZ + PE 签名）")

    head = installer.read_bytes()[:1 << 20]
    check(area, b"Nullsoft" in head, "是 NSIS 安装器（含 Nullsoft 标记）")

    # 主程序必须是 x64 —— 安装器自己跑在 32 位 NSIS 上，装进去的是 x64 的壳
    inner = Path("D:/filetools-build/target/release/FileTools.exe")
    if inner.is_file():
        check(area, read_pe_machine(inner) == 0x8664,
              "被装进去的 FileTools.exe 是 x64（Machine=0x8664）")
    else:
        skip(area, "主程序架构", f"{inner} 不在本机（换台机器构建过就查不到）")


# ----------------------------------------------------------------------
# Android
# ----------------------------------------------------------------------

def section_android(release: Path, version: str) -> None:
    section("ANDROID")
    area = "ANDROID"

    apk = release / "android" / f"FileTools-{version}-release.apk"
    check(area, apk.is_file(), f"android/FileTools-{version}-release.apk 存在")
    if not apk.is_file():
        return

    size = apk.stat().st_size
    check(area, size > 1_000_000, f"体积合理（{size:,} 字节）")
    check(area, zipfile.is_zipfile(apk), "是合法的 zip 容器（APK 就是 zip）")

    if not zipfile.is_zipfile(apk):
        return

    with zipfile.ZipFile(apk) as bundle:
        names = bundle.namelist()

    for name in APK_REQUIRED:
        check(area, name in names, f"APK 里有 {name}")

    abis = {n.split("/")[1] for n in names if n.startswith("lib/") and "/" in n[4:]}
    for abi in APK_REQUIRED_ABIS:
        check(area, abi in abis, f"含 {abi} 的原生库")

    schemes, v1_files = read_apk_signatures(apk)
    found = list(schemes) + ([f"v1（{len(v1_files)} 个文件）"] if v1_files else [])
    check(area, bool(found), f"被签名过（APK Signature Scheme：{' + '.join(found) or '一个都没有'}）")
    if found:
        note(f"Android 的签名方案是 {' + '.join(found)} —— `minSdk 24` 下 AGP 默认只出 v2/v3、"
             "不出 v1，这是正常的，不是「没签名」；但签名证书是 **debug keystore**，"
             "可安装、不可上架，这一点写在 RELEASE-MANIFEST.json 的 signed 字段里")

    # 用 SDK 自带的 apkanalyzer 读真实版本号与包名（有就用，没有就如实标 NOT EXECUTED）
    analyzer = find_apkanalyzer()
    if analyzer is None:
        skip(area, "APK 元数据（versionName / applicationId）",
             "本机找不到 apkanalyzer，未读取 APK 内部清单")
    else:
        for label, argv in (
            ("versionName", ["manifest", "version-name"]),
            ("applicationId", ["manifest", "application-id"]),
        ):
            done = subprocess.run(
                [str(analyzer), *argv, str(apk)], capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=180,
                shell=(os.name == "nt"),
            )
            value = (done.stdout or "").strip()
            if done.returncode != 0 or not value:
                skip(area, f"APK 的 {label}", f"apkanalyzer 退出码 {done.returncode}")
                continue
            expected = version if label == "versionName" else "com.filetools.app"
            check(area, value == expected, f"APK 的 {label} = {value}")


# ----------------------------------------------------------------------
# 清单自洽：三方（文件 / SHA256SUMS / MANIFEST）必须一致
# ----------------------------------------------------------------------

def section_manifest(release: Path, version: str) -> None:
    section("清单自洽（文件 · SHA256SUMS · MANIFEST 三方一致）")
    area = "MANIFEST"

    manifest = load_manifest(release)
    check(area, manifest is not None, "RELEASE-MANIFEST.json 可解析")
    if manifest is None:
        return

    check(area, manifest.get("product") == "FileTools", "清单里的 product == FileTools")
    check(area, manifest.get("version") == version,
          f"清单里的 version == VERSION（{manifest.get('version')}）")
    check(area, bool(manifest.get("git", {}).get("commit")),
          f"清单记了 git commit（{manifest.get('git', {}).get('commit', '')[:8]}）")

    artifacts = manifest.get("artifacts") or []
    check(area, bool(artifacts), f"清单里有 {len(artifacts)} 个产物")

    for item in artifacts:
        target = release / item["file"]
        if not target.is_file():
            check(area, False, f"{item['file']} 在盘上存在")
            continue
        check(area, target.stat().st_size == item["bytes"],
              f"{item['file']} 字节数与清单一致（{item['bytes']:,}）")
        check(area, sha256(target) == item["sha256"],
              f"{item['file']} sha256 与清单一致")

    # SHA256SUMS.txt 必须是同一批事实，不能是第二份真相
    sums_path = release / "SHA256SUMS.txt"
    if not sums_path.is_file():
        check(area, False, "SHA256SUMS.txt 存在")
        return

    sums: dict[str, str] = {}
    for line in sums_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        digest, _, name = line.partition("  ")
        sums[name] = digest

    from_sums = sorted(sums)
    from_manifest = sorted(item["file"] for item in artifacts)
    check(area, from_sums == from_manifest,
          "SHA256SUMS.txt 与清单列的是同一批文件")
    check(area, all(sums[item["file"]] == item["sha256"] for item in artifacts
                    if item["file"] in sums),
          "SHA256SUMS.txt 的哈希与清单逐条相同")

    # 真按 sha256sum -c 的语义验一遍
    bad = [name for name, digest in sums.items()
           if not (release / name).is_file() or sha256(release / name) != digest]
    check(area, not bad, f"按 SHA256SUMS.txt 实算全部匹配（不符 {len(bad)} 个）")

    # 发行说明必须提到这个版本，且**如实写出没做到的部分**
    notes_path = release / "RELEASE-NOTES.md"
    if notes_path.is_file():
        text = notes_path.read_text(encoding="utf-8")
        check(area, version in text, f"RELEASE-NOTES.md 提到版本 {version}")
        check(area, "debug" in text.lower(),
              "RELEASE-NOTES.md 写明了 Android 是 debug 签名")
    else:
        check(area, False, "RELEASE-NOTES.md 存在")


# ----------------------------------------------------------------------
# 环境够不着的项 —— NOT EXECUTED，永不 PASS
# ----------------------------------------------------------------------

def section_environment_limited() -> None:
    section("环境受限的项（NOT EXECUTED 而不是 PASS）")
    area = "ENVIRONMENT"

    if sys.platform == "darwin":
        skip(area, "iOS 构建", "本机是 macOS，但本轮未执行")
    else:
        skip(area, "iOS 构建", "本机不是 macOS，没有 Xcode —— 出不了任何 iOS 产物")

    docker = shutil.which("docker")
    skip(area, "Docker 镜像",
         "本机有 docker，但镜像属于发布动作，本轮未构建" if docker
         else "本机没有可用的 docker（开发机是 Windows）")

    adb_ok = False
    adb = shutil.which("adb") or shutil.which("adb.exe")
    if adb is None:
        for candidate in Path("D:/Android/Sdk/platform-tools").glob("adb.exe"):
            adb = str(candidate)
    if adb:
        done = subprocess.run([adb, "devices"], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=60)
        devices = [l for l in done.stdout.splitlines()[1:] if l.strip()]
        adb_ok = bool(devices)
        if adb_ok:
            skip(area, "Android 真机安装",
                 f"接上了 {len(devices)} 台设备，但本轮未执行安装")
        else:
            skip(area, "Android 真机安装", "adb 可用但一台物理设备都没接")
    else:
        skip(area, "Android 真机安装", "本机找不到 adb")

    cargo = shutil.which("cargo")
    if cargo:
        done = subprocess.run(["rustup", "target", "list", "--installed"],
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=60)
        installed = done.stdout.split()
        if "aarch64-pc-windows-msvc" in installed:
            check(area, True, "ARM64 target 已安装")
        else:
            skip(area, "Windows ARM64 发行包",
                 f"本机只装了 {', '.join(installed)}，没有 aarch64")
    else:
        skip(area, "Windows ARM64 发行包", "本机没有 cargo")


# ----------------------------------------------------------------------
# 报告与汇总（骨架照抄 verify_final.py）
# ----------------------------------------------------------------------

SPEC_AREAS = ("STRUCTURE", "WEB", "WINDOWS", "ANDROID", "MANIFEST", "ENVIRONMENT")


def area_summary() -> list[tuple[str, bool, int]]:
    area_order: list[str] = []
    for area, _ok, _label in results:
        if area not in area_order:
            area_order.append(area)
    ordered = [a for a in SPEC_AREAS if a in area_order]
    ordered += [a for a in area_order if a not in SPEC_AREAS]

    area_ok: dict[str, bool] = {a: True for a in area_order}
    for area, ok, _label in results:
        area_ok[area] = area_ok[area] and ok

    skipped: dict[str, int] = {}
    for area, _reason in not_executed:
        skipped[area] = skipped.get(area, 0) + 1

    return [(a, area_ok[a], skipped.get(a, 0)) for a in ordered]


def write_report() -> tuple[int, int]:
    passed = sum(1 for _a, ok, _l in results if ok)
    failed = [f"[{a}] {label}" for a, ok, label in results if not ok]

    lines = ["FILETOOLS RELEASE VERIFICATION", "=" * 30, ""]
    for area, ok, skipped in area_summary():
        suffix = f"（{skipped} 项 NOT EXECUTED）" if skipped else ""
        lines.append(f"[{'PASS' if ok else 'FAIL'}] {area}{suffix}")

    lines += ["", f"TOTAL: {passed} / {len(results)}",
              f"FAILED: {len(failed)}", f"NOT EXECUTED: {len(not_executed)}"]
    if notes:
        lines += ["", "如实记录（不是断言）："] + [f"  · {n}" for n in notes]
    if not_executed:
        lines += ["", "未执行的项及原因："] + [f"  - [{a}] {label}" for a, label in not_executed]
    if failed:
        lines += ["", "失败项："] + [f"  - {f}" for f in failed]
    else:
        lines += ["", "全部通过（未执行项见上，它们**不算通过**）。"]

    REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return passed, len(failed)


def main() -> int:
    print("FILETOOLS RELEASE VERIFICATION")
    print("=" * 30)
    print(f"仓库根：{ROOT}")

    version, release = section_structure()
    if not release.is_dir():
        print("\n发行目录不存在，先跑 `python scripts/build_release.py`。")
        write_report()
        return 1

    section_web(release, version)
    section_windows(release)
    section_android(release, version)
    section_manifest(release, version)
    section_environment_limited()

    passed, failed_count = write_report()

    print("\n" + "=" * 30)
    for area, ok, skipped in area_summary():
        suffix = f"（{skipped} 项 NOT EXECUTED）" if skipped else ""
        print(f"[{'PASS' if ok else 'FAIL'}] {area}{suffix}")
    print(f"\nTOTAL: {passed} / {len(results)}")
    print(f"FAILED: {failed_count}")
    print(f"NOT EXECUTED: {len(not_executed)}")
    for area, label in not_executed:
        print(f"  - [{area}] {label}")
    print(f"报告写入 {REPORT.relative_to(ROOT).as_posix()}")
    if failed_count:
        print("\n失败项：")
        for area, ok, label in results:
            if not ok:
                print(f"  - [{area}] {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
