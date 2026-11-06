#!/usr/bin/env python3
"""统一混剪入口：按项目名切换配置，两个项目完全隔离。

用法:
  python gen_project.py niubian 30 --title_offset 0 --video_speed 1.05
  python gen_project.py yuza    20 --title_offset 120 --speed 1.0

每个项目有独立的:
  - 素材池(pool_dirs) 与 BGM(bgm_dirs，可多个文件夹)
  - 标题库(titles)
  - 花字/字体收藏池(huazi_file / font_file)
  - 跨批次避重历史(history_path)
互不串用，所以鱼杂和牛鞭永远不会混。
"""
import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
import generator as g  # noqa: E402

PROJECTS = {
    "niubian": os.path.expanduser(
        "~/Desktop/03 品牌项目/红太阳牛鞭项目/config/niubian_spec.json"),
    "yuza": os.path.join(ROOT, "projects", "yuza", "spec.json"),
    # 0911杭州鱼杂（8 个子文件夹类别 + 固定镜头配方，片内绝不同意思相邻）
    "yuza0911": os.path.join(ROOT, "projects", "yuza0911", "spec.json"),
    # 0911-跃杨鱼杂（另一家店，9 类；独立历史文件，不与杭州批混）
    "yuza0911yy": os.path.join(ROOT, "projects", "yuza0911yy", "spec.json"),
}


def walk(d):
    d = os.path.expanduser(d)
    return [os.path.abspath(os.path.join(r, f))
            for r, _, fs in os.walk(d)
            for f in sorted(fs) if f.lower().endswith(g.VIDEO_EXTS)]


def bgm_dirs_of(spec):
    """取 BGM 文件夹列表（优先 bgm_dirs，兼容旧的单个 bgm_dir）。"""
    dirs = spec.get("bgm_dirs")
    if not dirs:
        single = spec.get("bgm_dir")
        dirs = [single] if single else []
    if isinstance(dirs, str):
        dirs = [dirs]
    return [os.path.expanduser(d) for d in dirs if d]


def main():
    ap = argparse.ArgumentParser(description="按项目生成混剪草稿")
    ap.add_argument("project", choices=list(PROJECTS), help="项目名")
    ap.add_argument("count", type=int, help="生成条数")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--title_offset", type=int, default=0)
    ap.add_argument("--video_speed", type=float, default=1.0,
                    help="画面倍速(默认1.0)；BGM 不变")
    ap.add_argument("--speed", type=float, default=1.0,
                    help="整条倍速(含BGM)，默认1.0")
    ap.add_argument("--start_jitter", type=float, default=0.0,
                    help="随机起始点(秒，0=关)。起点在 [0, 该值] 内随机后移，"
                         "同一素材复用取不同片段、打散查重指纹")
    ap.add_argument("--name", default=None)
    ap.add_argument("--ignore_history", action="store_true")
    ap.add_argument("--dry_run", action="store_true",
                    help="只打印选片方案（含体检报告），不生成草稿")
    args = ap.parse_args()

    spec = json.load(open(PROJECTS[args.project], encoding="utf-8"))
    pool = []
    for d in spec["pool_dirs"]:
        pool += walk(d)
    if not pool:
        sys.exit(f"[错误] 素材池为空，检查 pool_dirs: {spec['pool_dirs']}")

    seed = args.seed if args.seed is not None \
        else (abs(hash(args.project)) + args.title_offset * 7 + 1) % 100000

    _rand = (spec.get("rule_mode") or "recipe") == "random"

    full = {
        "name": args.name or
        f"{spec.get('name_prefix', '混剪')}_{args.title_offset + 1}-{args.title_offset + args.count}",
        "pool": pool,
        # 素材根目录：用于判断"文件直接躺在根目录"，从而退回按文件名前缀分
        # 类别（子文件夹则直接用文件夹名）
        "pool_roots": [os.path.expanduser(d) for d in spec["pool_dirs"]],
        "bgm_dirs": bgm_dirs_of(spec),
        "titles": os.path.expanduser(spec["titles"]),
        "font_file": os.path.expanduser(spec["font_file"]),
        "huazi_file": os.path.expanduser(spec["huazi_file"]),
        "history_path": os.path.expanduser(spec["history_path"]),
        "count": args.count,
        "slots": spec.get("slots"),          # None = 按配方长度算
        # 镜头配方：每段固定类别（类内随机换文件），片内绝不同意思相邻
        # rule_mode=random → 忽略配方，只按段数随机（配方仍留在 spec 里）
        "shot_recipe": None if _rand else spec.get("shot_recipe"),
        "shot_recipes": None if _rand else spec.get("shot_recipes"),
        "slot_groups": None if _rand else spec.get("slot_groups"),
        "beat_sync": spec.get("beat_sync", True),
        "shuffle_slots": spec.get("shuffle_slots", False),
        "clip_jitter": spec.get("clip_jitter", 0.0),
        "bgm_jitter": spec.get("bgm_jitter", 0.0),
        "start_jitter": args.start_jitter if args.start_jitter != 0.0
                        else spec.get("start_jitter", 0.0),
        # 排他类别：相邻两条不能都含（如「端锅-上桌」要求隔条出现）
        "exclusive_adjacent": spec.get("exclusive_adjacent"),
        "shot_dur": spec.get("shot_dur", 2.0),
        "min_diff": spec.get("min_diff", 2),
        "seed": seed,
        "title_offset": args.title_offset,
        "video_speed": args.video_speed,
        "speed": args.speed,
        "ignore_history": args.ignore_history,
        "dry_run": args.dry_run,
    }
    # 店面选用规则（文案 title_filter / BGM bgm_filter）——白名单漏传会静默「用全部」
    full.update({k: spec[k] for k in g.SPEC_PASSTHROUGH_KEYS if k in spec})
    try:
        res = g.generate_remix(full)
    except g.GenError as e:                 # 素材池 / 配方 / 段数配置问题 → 干净退出报错
        sys.exit(f"{e}")
    if res.get("dry_run"):
        print("(dry-run) 未生成草稿。")
        return
    print("draft_id:", res["draft_id"], "| name:", res["name"],
          "| count:", res["count"])
    print("总时长(秒):", round(res["duration_us"] / 1e6, 1))


if __name__ == "__main__":
    main()
