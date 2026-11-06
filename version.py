#!/usr/bin/env python3
"""控制台版本号 —— **唯一来源**。

发版时只改这里，别去 console.html 里手改（页面上的版本号由 `/api/status` 回填）。

用法
----
    .venv/bin/python version.py                 # 打印当前版本
    .venv/bin/python version.py --bump patch    # 1.5.0 → 1.5.1（修 bug）
    .venv/bin/python version.py --bump minor    # 1.5.0 → 1.6.0（加功能）
    .venv/bin/python version.py --bump major    # 1.5.0 → 2.0.0（大改）
    .venv/bin/python version.py --set 1.6.0     # 直接指定
    .venv/bin/python version.py --tag           # git tag v<当前版本>（不重复打）

约定（改动 → 版本号）
--------------------
| 改动性质 | 递增位 | 例 |
|---|---|---|
| 修 bug / 小调整 | patch | 1.5.0 → 1.5.1 |
| 新功能（如「每条段数」） | minor | 1.5.0 → 1.6.0 |
| 不兼容的大改 | major | 1.5.0 → 2.0.0 |
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys

__version__ = "1.11.0"

_HERE = os.path.dirname(os.path.abspath(__file__))
_VER_RE = re.compile(r'^__version__\s*=\s*"([^"]+)"', re.M)


def parse(v: str) -> tuple:
    parts = (v or "").strip().split(".")
    nums = []
    for p in parts[:3]:
        try:
            nums.append(int(p))
        except ValueError:
            nums.append(0)
    while len(nums) < 3:
        nums.append(0)
    return tuple(nums)


def fmt(t: tuple) -> str:
    return ".".join(str(n) for n in t)


def bump(part: str = "patch") -> str:
    """把当前版本按 part 递增，写回本文件，返回新版本号。"""
    path = os.path.join(_HERE, "version.py")
    with open(path, encoding="utf-8") as f:
        src = f.read()
    m = _VER_RE.search(src)
    if not m:
        raise SystemExit("version.py 里找不到 __version__")
    cur = m.group(1)
    major, minor, patch = parse(cur)
    if part == "major":
        major, minor, patch = major + 1, 0, 0
    elif part == "minor":
        minor, patch = minor + 1, 0
    else:
        patch += 1
    new = fmt((major, minor, patch))
    src = src[:m.start(1)] + new + src[m.end(1):]
    with open(path, "w", encoding="utf-8") as f:
        f.write(src)
    return new


def set_version(v: str) -> None:
    path = os.path.join(_HERE, "version.py")
    with open(path, encoding="utf-8") as f:
        src = f.read()
    m = _VER_RE.search(src)
    if not m:
        raise SystemExit("version.py 里找不到 __version__")
    src = src[:m.start(1)] + v.strip() + src[m.end(1):]
    with open(path, "w", encoding="utf-8") as f:
        f.write(src)


def git_tag() -> str:
    tag = "v" + __version__
    r = subprocess.run(["git", "tag", "-l", tag], cwd=_HERE,
                       capture_output=True, text=True)
    if r.stdout.strip():
        print(f"tag {tag} 已存在，跳过")
        return tag
    subprocess.run(["git", "tag", tag], cwd=_HERE, check=True)
    print(f"✅ 已打 tag {tag}")
    return tag


def main() -> None:
    ap = argparse.ArgumentParser(description="控制台版本号")
    ap.add_argument("--bump", choices=["patch", "minor", "major"])
    ap.add_argument("--set", dest="set_to")
    ap.add_argument("--tag", action="store_true")
    a = ap.parse_args()
    if a.set_to:
        set_version(a.set_to)
        print(f"版本已设为 v{a.set_to}")
        return
    if a.bump:
        new = bump(a.bump)
        print(f"v{__version__} → v{new}（{a.bump}）")
        return
    if a.tag:
        git_tag()
        return
    print(f"v{__version__}")


if __name__ == "__main__":
    sys.exit(main())
