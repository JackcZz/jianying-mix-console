#!/usr/bin/env python3
"""修复剪映草稿索引 root_meta_info.json。

背景（踩坑记录 P6/P2）：
  引擎模板 default2 由他人在 Windows 上制作，草稿内 draft_meta_info.json 残留了
  与本机无关的陈旧数据（Windows 路径 C:/Users/1/...、多年前的创建时间、draft_name
  是个 UUID）。当【剪映正在运行】时生成草稿，剪映会扫到新文件夹、按那份陈旧 meta
  往索引里塞一条「名字=文件夹名、时长0」的条目；我们的 register_draft 又正常追加
  一条 → 同一草稿出现 2 条条目。
  更糟的是：你 ⌘Q 退出剪映时，它会用内存里的索引【覆盖】文件 —— 于是我们那条正常
  条目被冲掉，只剩剪映扫描出的残缺条目（名字=文件夹名、时长0）。此时光「去重」不够，
  必须【重建】一条规范条目。

  ✅ 正确工作流：生成/注册前先 ⌘Q 完全退出剪映 → 生成 → 再打开剪映。

本脚本三个能力：
  1) 去重：同一草稿文件夹出现多条条目时，只保留最完整的一条；
  2) 改名：把某草稿的索引名改成期望值（--set-name）；
  3) 重建：删掉某草稿的现有条目，用 generator.register_draft 按草稿自身
     draft_info.json 的 name/duration 重建一条规范条目（--rebuild）。

用法:
  python fix_draft_index.py                                  # 只体检，不写入
  python fix_draft_index.py --apply                          # 去重并写回（自动备份）
  python fix_draft_index.py --apply --set-name <文件夹> "<名>"  # 去重 + 改名
  python fix_draft_index.py --apply --rebuild <文件夹>          # 去重 + 重建（推荐）

⚠ 运行前务必 ⌘Q 完全退出剪映，否则剪映退出时会覆盖本文件的修改。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DRAFT_DIR = os.path.expanduser(
    "~/Movies/JianyingPro/User Data/Projects/com.lveditor.draft")
IDX = os.path.join(DRAFT_DIR, "root_meta_info.json")


def _score(e: dict, folder_base: str) -> tuple:
    """越大越该保留：优先有人类可读名，其次时长>0，再其次新的创建时间。"""
    name = str(e.get("draft_name") or "")
    human = 0 if name in ("", folder_base) else 1
    has_dur = 1 if (e.get("tm_duration") or 0) > 0 else 0
    create = e.get("tm_draft_create") or 0
    return (human, has_dur, create)


def _write(idx: dict, store: list, backup: bool):
    if backup:
        bak = IDX + f".bak-{time.strftime('%Y%m%d-%H%M%S')}"
        shutil.copy2(IDX, bak)
        print(f"  备份: {bak}")
    idx["all_draft_store"] = store
    with open(IDX, "w", encoding="utf-8") as f:
        json.dump(idx, f, ensure_ascii=False, separators=(",", ":"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="实际写回（默认只体检）")
    ap.add_argument("--set-name", nargs=2, action="append", default=[],
                    metavar=("FOLDER", "NAME"), help="改某草稿的索引名（可重复）")
    ap.add_argument("--rebuild", action="append", default=[], metavar="FOLDER",
                    help="删掉该草稿现有条目并重建一条规范条目（读 draft_info.json）")
    args = ap.parse_args()

    if not os.path.isfile(IDX):
        raise SystemExit(f"找不到索引文件：{IDX}")
    idx = json.load(open(IDX, encoding="utf-8"))
    store = idx.get("all_draft_store", [])

    # 1) 去重（按 draft_fold_path 分组，保留最优）
    groups: dict[str, list[int]] = {}
    for i, e in enumerate(store):
        groups.setdefault(e.get("draft_fold_path") or "", []).append(i)
    dropped = []
    keep_idx = []
    for fp, idxs in groups.items():
        base = os.path.basename(fp)
        best = idxs[0] if len(idxs) == 1 else max(
            idxs, key=lambda i: _score(store[i], base))
        keep_idx.append(best)
        for i in idxs:
            if i != best:
                dropped.append(store[i])
    new_store = [store[i] for i in sorted(keep_idx)]

    # 2) 重建：先摘掉旧条目
    rebuild_set = set(args.rebuild)
    removed = [e for e in new_store
               if os.path.basename(e.get("draft_fold_path") or "") in rebuild_set]
    if rebuild_set:
        new_store = [e for e in new_store
                     if os.path.basename(e.get("draft_fold_path") or "")
                     not in rebuild_set]

    # 3) 改名
    name_map = {f: n for f, n in args.set_name}
    renamed = []
    for e in new_store:
        base = os.path.basename(e.get("draft_fold_path") or "")
        if base in name_map and e.get("draft_name") != name_map[base]:
            renamed.append((e.get("draft_name"), name_map[base]))
            e["draft_name"] = name_map[base]

    # 4) 报告
    print(f"索引条目 {len(store)} → {len(new_store)} | 唯一点位 {len(groups)}"
          f" | 去重 {len(dropped)} | 重建前摘除 {len(removed)} | 改名 {len(renamed)}")
    for e in dropped:
        print(f"  [去重] name={e.get('draft_name')!r} dur={e.get('tm_duration')}")
    for old, new in renamed:
        print(f"  [改名] {old!r} → {new!r}")

    if not args.apply:
        print("\n(体检模式，未写入。加 --apply 生效)")
        return

    need_write = bool(dropped or removed or renamed)
    if need_write:
        _write(idx, new_store, backup=True)

    # 5) 重建（register_draft 会自行追加）
    if rebuild_set:
        sys.path.insert(0, HERE)
        import generator as G  # noqa: E402
        for folder in args.rebuild:
            info_path = os.path.join(DRAFT_DIR, folder, "draft_info.json")
            if not os.path.isfile(info_path):
                print(f"  [跳过] 找不到 {info_path}")
                continue
            info = json.load(open(info_path, encoding="utf-8"))
            nm = info.get("name") or folder
            dur = int(info.get("duration") or 0)
            G.register_draft(folder, nm, dur)
            print(f"  [重建] {folder} → 名={nm!r} 时长={dur}us")

    # 6) 终态
    store2 = json.load(open(IDX, encoding="utf-8"))["all_draft_store"]
    print(f"\n终态：条目 {len(store2)} | 唯一点位 "
          f"{len({e.get('draft_fold_path') for e in store2})}")
    if not need_write and not rebuild_set:
        print("无需修改。")


if __name__ == "__main__":
    main()
