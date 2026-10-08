#!/usr/bin/env python3
"""把仓库根的 ``VERSION`` 同步到所有**只能存静态值**的配置文件。

## 为什么需要这个脚本

版本号的唯一真相源是仓库根的 ``VERSION``（一行文本）。但四个平台里，有几个
消费方**没有能力在构建期读它**：

* ``mobile/app.json`` —— Expo 的 ``expo.version`` 只认静态值；
* ``frontend`` / ``mobile`` 的 ``package.json`` 与 ``package-lock.json`` ——
  npm 的版本字段是文件里的字面量；
* ``mobile/src/types/index.ts`` —— 编译进 App 的 ``APP_VERSION`` 常量；
* ``desktop/src-tauri/Cargo.toml`` —— Cargo **要求** ``[package]`` 段必须有
  ``version``，没有「指向别处」这种写法。

（**能**读的两处就直接读了，不经过本脚本，免得白多一份副本：

* ``backend/config.py`` 在 import 期读 ``VERSION``（见那里的 ``_read_version``）；
* 桌面端的 ``tauri.conf.json`` 的 ``version`` 字段**指向** ``frontend/package.json``，
  不写字面量。）

> Cargo.toml 那一份是**纯形式**的：真正决定 ``FileTools.exe`` 版本的是
> ``tauri.conf.json``（它又指向 ``frontend/package.json``），Cargo 的 ``version``
> 不会进 PE 的版本资源、也不进安装包文件名。但「形式上存在、语义上没用」
> 恰恰是最容易漂移的那种副本 —— 所以照样同步、照样断言。

所以规则是：**改版本只改 ``VERSION``，然后跑一次这个脚本**。
``scripts/verify_branding.py`` 会断言上面每一处都等于 ``VERSION`` ——
忘了跑脚本不会静默漂移，会当场变红。

## 用法

    python scripts/sync_version.py            # 写入（幂等：值对了就一个字节都不动）
    python scripts/sync_version.py --check    # 只检查；有漂移则打印并退出码 1
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

#: 版本唯一真相源
VERSION_FILE = ROOT / "VERSION"

#: 合法的 ``MAJOR.MINOR.PATCH``（§十七 要求四平台统一成这个形状）
SEMVER = re.compile(r"^\d+\.\d+\.\d+$")

#: ``mobile/src/types/index.ts`` 里那一行。用行首锚定 + 单引号，
#: 避免误伤注释里出现的同名片段。
TS_APP_VERSION = re.compile(r"^(export const APP_VERSION = ')([^']*)(')", re.MULTILINE)

#: ``desktop/src-tauri/Cargo.toml`` 的 ``[package]`` **段本身**（到下一个段头为止）。
#: 必须先切出这一段再改里面的 version —— 依赖表里也全是 ``version = "2"``
#: （``tauri = { version = "2", features = [] }``），不在段落里限定就会改错行。
CARGO_PACKAGE_BLOCK = re.compile(r"^\[package\][^\n]*\n(?:(?!\[).*\n)*", re.MULTILINE)
#: 段落内的 version 行
CARGO_VERSION_LINE = re.compile(r'^(version\s*=\s*")([^"]*)(")', re.MULTILINE)

#: 桌面端 Cargo 清单
CARGO_TOML = ROOT / "desktop" / "src-tauri" / "Cargo.toml"


def read_source_version() -> str:
    value = VERSION_FILE.read_text(encoding="utf-8").strip()
    if not SEMVER.match(value):
        raise SystemExit(
            f"{VERSION_FILE} 的内容 {value!r} 不是 MAJOR.MINOR.PATCH 形式"
        )
    return value


def _json_edit(path: pathlib.Path, edits: dict[tuple[str, ...], object]) -> bool:
    """按「键路径 → 新值」改一个 JSON 文件；返回是否真的变了。

    用 ``json`` 往返而不是正则替换：键路径分层，正则会误伤依赖树里
    恰好同名的字段。重新序列化用 2 空格缩进 + 末尾换行，与 npm 的写法一致，
    所以值没变时产物与原文件**逐字节相同**（幂等由调用方先比较再写来保证）。
    """
    original = path.read_text(encoding="utf-8")
    data = json.loads(original)

    for keys, value in edits.items():
        node = data
        for key in keys[:-1]:
            node = node.setdefault(key, {})
        node[keys[-1]] = value

    updated = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    if updated == original:
        return False
    path.write_text(updated, encoding="utf-8")
    return True


def _text_edit(path: pathlib.Path, pattern: re.Pattern[str], replacement: str) -> bool:
    original = path.read_text(encoding="utf-8")
    updated, count = pattern.subn(replacement, original)
    if count != 1:
        raise SystemExit(f"{path} 里匹配到 {count} 处（期望恰好 1 处），拒绝改动")
    if updated == original:
        return False
    path.write_text(updated, encoding="utf-8")
    return True


def cargo_package_version() -> str | None:
    """读出 ``Cargo.toml`` 的 ``[package]`` 段的 version；读不到返回 ``None``。"""
    text = CARGO_TOML.read_text(encoding="utf-8")
    block = CARGO_PACKAGE_BLOCK.search(text)
    if block is None:
        return None
    found = CARGO_VERSION_LINE.search(block.group(0))
    return found.group(2) if found else None


def _cargo_set_version(version: str) -> bool:
    """改 ``Cargo.toml`` 的 ``[package].version``；返回是否真的变了。

    只在 ``[package]`` 段的切片上做替换，再拼回原文 —— 这样依赖表里
    那些同名的 ``version`` 不会被碰到。切不出段落或段内匹配数不是 1 时
    **拒绝改动并退出**，而不是猜一个位置下手。
    """
    text = CARGO_TOML.read_text(encoding="utf-8")
    block = CARGO_PACKAGE_BLOCK.search(text)
    if block is None:
        raise SystemExit(f"{CARGO_TOML} 里找不到 [package] 段，拒绝改动")

    updated_block, count = CARGO_VERSION_LINE.subn(
        lambda m: f"{m.group(1)}{version}{m.group(3)}", block.group(0)
    )
    if count != 1:
        raise SystemExit(
            f"{CARGO_TOML} 的 [package] 段里匹配到 {count} 处 version（期望 1 处），拒绝改动"
        )

    updated = text[: block.start()] + updated_block + text[block.end() :]
    if updated == text:
        return False
    CARGO_TOML.write_text(updated, encoding="utf-8")
    return True


def plan(version: str) -> list[tuple[pathlib.Path, dict[tuple[str, ...], object]]]:
    """列出「文件 → 要写的键路径」。写死成表，好一眼看出覆盖了哪几处。"""
    return [
        (ROOT / "frontend" / "package.json", {("version",): version}),
        (ROOT / "mobile" / "package.json", {("version",): version}),
        (ROOT / "mobile" / "app.json", {("expo", "version"): version}),
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="只报告漂移，不写文件；有漂移则退出码 1",
    )
    args = parser.parse_args()

    version = read_source_version()
    changed: list[str] = []
    drift: list[str] = []

    for path, edits in plan(version):
        rel = path.relative_to(ROOT).as_posix()
        if args.check:
            data = json.loads(path.read_text(encoding="utf-8"))
            for keys, want in edits.items():
                node = data
                for key in keys:
                    node = node.get(key, {}) if isinstance(node, dict) else {}
                got = node if isinstance(node, str) else None
                if got != want:
                    drift.append(f"{rel} {'.'.join(keys)} = {got!r}，应为 {want!r}")
            continue
        if _json_edit(path, edits):
            changed.append(rel)

    # 两份 lockfile：版本**和** name 都对齐到各自的 package.json。
    # mobile/package-lock.json 里的 name 是 "mobile"、version 是 "1.0.0"，
    # 与 package.json 的 "filetools-mobile" / 版本号都对不上 —— 那是手动
    # 改过 package.json 却没重新生成 lock 留下的。用 npm install 去修会连带
    # 动到依赖树（本阶段明令不加依赖），所以在这里按 package.json 对齐。
    for name in ("frontend", "mobile"):
        pkg_path = ROOT / name / "package.json"
        lock_path = ROOT / name / "package-lock.json"
        pkg = json.loads(pkg_path.read_text(encoding="utf-8"))
        edits: dict[tuple[str, ...], object] = {
            ("version",): version,
            ("packages", "", "version"): version,
        }
        # 顶层 name 只在 lockfile 里本来就有的时候才写：npm 的 lock 顶层
        # 一定有 name，但真缺了就补上，免得留下半个文件。
        edits[("name",)] = pkg["name"]
        edits[("packages", "", "name")] = pkg["name"]

        rel = lock_path.relative_to(ROOT).as_posix()
        if args.check:
            data = json.loads(lock_path.read_text(encoding="utf-8"))
            root_pkg = data.get("packages", {}).get("", {})
            for label, got, want in (
                ("version", data.get("version"), version),
                ("packages[''].version", root_pkg.get("version"), version),
                ("name", data.get("name"), pkg["name"]),
                ("packages[''].name", root_pkg.get("name"), pkg["name"]),
            ):
                if got != want:
                    drift.append(f"{rel} {label} = {got!r}，应为 {want!r}")
            continue
        if _json_edit(lock_path, edits):
            changed.append(rel)

    ts_path = ROOT / "mobile" / "src" / "types" / "index.ts"
    ts_rel = ts_path.relative_to(ROOT).as_posix()
    if args.check:
        found = TS_APP_VERSION.search(ts_path.read_text(encoding="utf-8"))
        if found is None:
            drift.append(f"{ts_rel} 里找不到 export const APP_VERSION")
        elif found.group(2) != version:
            drift.append(
                f"{ts_rel} APP_VERSION = {found.group(2)!r}，应为 {version!r}"
            )
    elif _text_edit(
        ts_path, TS_APP_VERSION, lambda m: f"{m.group(1)}{version}{m.group(3)}"
    ):
        changed.append(ts_rel)

    cargo_rel = CARGO_TOML.relative_to(ROOT).as_posix()
    if args.check:
        got = cargo_package_version()
        if got is None:
            drift.append(f"{cargo_rel} 的 [package] 段里找不到 version")
        elif got != version:
            drift.append(f"{cargo_rel} [package].version = {got!r}，应为 {version!r}")
    elif _cargo_set_version(version):
        changed.append(cargo_rel)

    if args.check:
        if drift:
            print(f"版本漂移（真相源 {VERSION_FILE.name} = {version}）：")
            for item in drift:
                print(f"  · {item}")
            return 1
        print(f"版本一致：全部落点 = {version}")
        return 0

    if changed:
        print(f"已同步到 {version}：")
        for item in changed:
            print(f"  · {item}")
    else:
        print(f"全部落点已经是 {version}，未改动任何文件")
    return 0


if __name__ == "__main__":
    sys.exit(main())
