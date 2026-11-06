#!/usr/bin/env python3
"""清理剪映索引里「磁盘上已经没有文件夹」的死条目。

为什么需要它
------------
生成失败（例如 ffmpeg 报错）会在草稿目录留下**空壳草稿**（draft_content.json 约 4.3KB、
0 段、0 复合片段）。剪映扫描到这些文件夹后会在 root_meta_info.json 里补一条
「名字=文件夹名、时长 0」的条目；之后哪怕把文件夹删了，**条目仍留在索引里**，
剪映草稿列表里就会出现点不开的 0 秒项。

`fix_draft_index.py` 只管索引内部去重/改名/重建，**不处理这种孤儿**——本脚本补这一环。

用法
----
    python prune_draft_index.py                    # 体检：只列出会被删的条目
    python prune_draft_index.py --apply            # 真正写回（自动备份 .bak-时间戳）
    python prune_draft_index.py --apply --force    # 剪映在跑也强写（⚠ 不推荐）

⚠ 剪映运行时会把 root_meta_info.json 载入内存后回写，会**覆盖**你的修改
  → 默认拒绝在剪映运行时写回（这是 P2 类问题的根因）。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time

DRAFT_DIR = os.path.expanduser(
    "~/Movies/JianyingPro/User Data/Projects/com.lveditor.draft"
)
INDEX_NAME = "root_meta_info.json"
STORE_KEY = "all_draft_store"
# 剪映主二进制（排除残留的 TrayHelper / Helper 子进程）
CAPCUT_MAIN = "VideoFusion-macOS.app/Contents/MacOS/VideoFusion-macOS"


def capcut_running() -> bool:
    try:
        r = subprocess.run(["pgrep", "-f", CAPCUT_MAIN],
                           capture_output=True, text=True)
        return r.returncode == 0 and bool(r.stdout.strip())
    except Exception:
        return False


def entry_folder(e: dict, draft_dir: str) -> str:
    """索引条目对应的草稿文件夹绝对路径。"""
    fp = e.get("draft_fold_path")
    if fp:
        return os.path.expanduser(str(fp))
    name = str(e.get("draft_name") or "")
    return os.path.join(draft_dir, name) if name else ""


def main() -> int:
    ap = argparse.ArgumentParser(description="清理剪映索引里的死条目（文件夹已不存在）")
    ap.add_argument("--apply", action="store_true", help="实际写回（默认只体检）")
    ap.add_argument("--force", action="store_true",
                    help="剪映在运行时也写回（⚠ 会被剪映覆盖，不推荐）")
    ap.add_argument("--draft-dir", default=DRAFT_DIR, help="剪映草稿目录")
    ap.add_argument("--only-missing", action="store_true", default=True,
                    help="只处理文件夹不存在的条目（默认行为）")
    args = ap.parse_args()

    draft_dir = os.path.expanduser(args.draft_dir)
    idx_path = os.path.join(draft_dir, INDEX_NAME)
    if not os.path.isfile(idx_path):
        print(f"❌ 找不到索引文件：{idx_path}")
        return 1

    raw = open(idx_path, encoding="utf-8").read()
    idx = json.loads(raw)
    store = idx.get(STORE_KEY, [])
    if not isinstance(store, list):
        # 兼容个别版本把 store 放在嵌套结构里
        print("❌ 索引结构异常：all_draft_store 不是列表")
        return 1

    dead, alive = [], []
    for e in store:
        folder = entry_folder(e, draft_dir)
        (alive if folder and os.path.isdir(folder) else dead).append(e)

    print(f"索引文件：{idx_path}")
    print(f"条目总数：{len(store)}   有效：{len(alive)}   死条目：{len(dead)}")
    if dead:
        print("\n--- 将被移除的死条目 ---")
        for e in dead:
            folder = entry_folder(e, draft_dir) or "(空)"
            print(f"  {str(e.get('draft_name'))[:36]:<38} "
                  f"dur={e.get('tm_duration') or 0:<12} {folder}")
    else:
        print("\n✅ 没有死条目，索引干净。")

    if not args.apply:
        print("\n(体检模式，未写入。加 --apply 生效)")
        return 0

    if not dead:
        return 0

    if capcut_running() and not args.force:
        print("\n⚠ 剪映正在运行：它会把索引载入内存后回写，覆盖本次修改。")
        print("  请先 ⌘Q 完全退出剪映，再重跑；确实要现在写就加 --force。")
        return 2

    if not raw:
        print("❌ 索引内容为空，为安全起见中止。")
        return 1
    bak = idx_path + ".bak-" + time.strftime("%Y%m%d-%H%M%S")
    shutil.copy2(idx_path, bak)
    print(f"\n已备份 → {bak}")

    idx[STORE_KEY] = alive
    tmp = idx_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(idx, f, ensure_ascii=False, indent=2)
    os.replace(tmp, idx_path)
    print(f"✅ 已移除 {len(dead)} 条死条目，写回完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
