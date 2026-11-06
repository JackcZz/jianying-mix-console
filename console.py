"""
视频混剪 · 项目管理控制台（Web）

左侧悬浮菜单切换「店面」（每个店面 = 一个素材文件夹 + 一套剪辑规则），
右侧按当前规则走完整链路：混剪选片 → BGM 卡点 → 标题 → 拼接成剪映草稿。

运行：
    .venv/bin/python console.py
然后浏览器打开 http://127.0.0.1:8766

⚠ 正式生成前必须先 ⌘Q 完全退出剪映（剪映运行时生成会产生重复索引条目）。
"""
from __future__ import annotations

import contextlib
import glob
import io
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
import urllib.error
import urllib.request
from urllib.parse import quote

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel
from typing import Optional

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import generator as g            # noqa: E402
import gen_project as gp         # noqa: E402
import titlelib as T             # noqa: E402
import binpaths                  # noqa: E402  定位 ffmpeg/ffprobe（.app 窄 PATH 下必需）
import version as VER            # noqa: E402  版本号唯一来源（version.py）

app = FastAPI(title="视频混剪控制台")

# ---------------------------------------------------------------------------
# 店面注册表：key → 显示名 + spec 路径。
#   内置店面写死在 BUILTIN_STORES；
#   用户自己加的店面存在 projects/_custom_stores.json（重启后仍在）。
# ---------------------------------------------------------------------------
BUILTIN_STORES: list[dict] = [
    {"key": "yuza0911yy", "label": "跃杨鱼杂",
     "desc": "0911-跃杨鱼杂 · 开头夹菜 / 上桌隔条",
     "spec": gp.PROJECTS["yuza0911yy"]},
    {"key": "yuza0911", "label": "杭州鱼杂",
     "desc": "0911 杭州鱼杂 · 固定镜头配方",
     "spec": gp.PROJECTS["yuza0911"]},
    {"key": "niubian", "label": "黄山牛鞭汤",
     "desc": "出锅 + 人多 · 不卡点",
     "spec": gp.PROJECTS["niubian"]},
    {"key": "yuza", "label": "鱼杂（早期）",
     "desc": "0917/0918 · 无配方随机",
     "spec": gp.PROJECTS["yuza"]},
]

CUSTOM_FILE = os.path.join(HERE, "projects", "_custom_stores.json")
_BUILTIN_KEYS = {s["key"] for s in BUILTIN_STORES}

# ---------------------------------------------------------------------------
# 花字库 / 字体池
#   favorite_huazi.json = 「启用池」：生成标题时从中随机挑 1 个花字。
#   favorite_fonts.json = 字体池（花字与字体叠加使用）。
#   两者都支持填「名称」或「ID」；保存时统一归一化成 ID 写回。
# ---------------------------------------------------------------------------
HUAZI_JSON = os.path.join(HERE, "assets", "huazi.json")
HUAZI_CAT_JSON = os.path.join(HERE, "assets", "huazi_categories.json")
FAV_HUAZI_JSON = os.path.join(HERE, "assets", "favorite_huazi.json")
FAV_FONTS_JSON = os.path.join(HERE, "assets", "favorite_fonts.json")
HUAZI_PREVIEW_DIR = os.path.join(HERE, "assets", "huazi_library", "preview")
JY_EFFECT_CACHE = os.path.expanduser(
    "~/Movies/JianyingPro/User Data/Cache/artistEffect")

# 默认文案库：新店面没单独配 titles 时用它
DEFAULT_TITLES = os.path.join(HERE, "assets", "titles.txt")

# 默认 BGM 曲库来自「模板店面」——即新建店铺时选的那个（界面上提示「留空 = 沿用模板曲库」）。
# 新店会记下自己用的 template；没记的（老店 / 内置店）回退到下面这个默认模板。
DEFAULT_BGM_TEMPLATE = "yuza0911yy"        # 跃杨鱼杂

STORES: list[dict] = list(BUILTIN_STORES)
BY_KEY: dict[str, dict] = {s["key"]: s for s in STORES}


def _load_custom_stores() -> list:
    if not os.path.isfile(CUSTOM_FILE):
        return []
    try:
        rows = json.load(open(CUSTOM_FILE, encoding="utf-8"))
        return rows if isinstance(rows, list) else []
    except Exception:
        return []


def _save_custom_stores(rows: list) -> None:
    os.makedirs(os.path.dirname(CUSTOM_FILE), exist_ok=True)
    tmp = CUSTOM_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CUSTOM_FILE)


def _refresh_stores() -> None:
    """内置 + 自定义 合并，重建 STORES / BY_KEY。"""
    global STORES, BY_KEY
    rows = [dict(s) for s in BUILTIN_STORES]
    seen = {s["key"] for s in rows}
    for s in _load_custom_stores():
        k = s.get("key")
        if k and k not in seen:
            seen.add(k)
            rows.append(s)
    STORES = rows
    BY_KEY = {s["key"]: s for s in STORES}


_refresh_stores()

# 后台生成任务
TASKS: dict[str, dict] = {}
_TASKS_LOCK = threading.Lock()

# 「关页面 → 退出程序」的待退出倒计时（None = 不退）。
# 为什么要倒计时而不是立刻退：浏览器**刷新页面**也会触发 beforeunload，
# 立刻退的话「按一下 F5 就把控制台关了」。改成：关页面 → 登记一个 12 秒倒计时；
# 新页面加载时会打 /api/keepalive 把它取消（刷新场景）；真关了页面就没人取消，
# 12 秒后自动退出（关页面场景）。显式点「关闭程序」按钮则用硬退（0.6 秒）。
_SHUTDOWN: dict = {"deadline": None}


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def _walk(d: str) -> list:
    d = os.path.expanduser(d)
    return [os.path.abspath(os.path.join(r, f))
            for r, _, fs in os.walk(d)
            for f in sorted(fs) if f.lower().endswith(g.VIDEO_EXTS)]


def _walk_audio(d: str) -> list:
    """递归扫描一个目录下的音频文件（BGM 曲库用）。"""
    d = os.path.expanduser(d)
    if not os.path.isdir(d):
        return []
    return [os.path.abspath(os.path.join(r, f))
            for r, _, fs in os.walk(d)
            for f in sorted(fs) if f.lower().endswith(g.AUDIO_EXTS)]


def _bgm_dirs_of(spec: dict) -> list:
    """取店面的 BGM 文件夹列表（优先 bgm_dirs，兼容旧的单个 bgm_dir）。"""
    dirs = spec.get("bgm_dirs")
    if not dirs:
        single = spec.get("bgm_dir")
        dirs = [single] if single else []
    if isinstance(dirs, str):
        dirs = [dirs]
    out, seen = [], set()
    for d in dirs:
        if not d:
            continue
        p = os.path.expanduser(d)
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def _load_spec(key: str) -> dict:
    return json.load(open(BY_KEY[key]["spec"], encoding="utf-8"))


# ---------------------------------------------------------------------------
# 花字库 / 字体池 工具
# ---------------------------------------------------------------------------
def _read_json(path: str, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _as_str_list(raw) -> list:
    if isinstance(raw, dict):
        raw = raw.get("ids", raw.get("names", raw.get("fonts", [])))
    if not isinstance(raw, list):
        return []
    return [str(x).strip() for x in raw if str(x).strip()]


def _fav_huazi_raw() -> list:
    return _as_str_list(_read_json(FAV_HUAZI_JSON, []))


def _fav_fonts_raw() -> list:
    return _as_str_list(_read_json(FAV_FONTS_JSON, []))


def _normalize_fav(raw: list, rows: list) -> list:
    """把启用池里的「名称或 ID」统一解析成 ID（去重、保序）。

    填名称时：先精确匹配 title，再不中就按「标题包含关键词」模糊匹配
    （与 generator._load_favorite_ids 口径一致）。
    """
    by_id = {r["id"]: r for r in rows}
    by_title = {}
    for r in rows:
        by_title.setdefault(r["title"], r["id"])
    out, seen = [], set()
    for x in raw:
        cand = [x] if x in by_id else (
            [by_title[x]] if x in by_title
            else [r["id"] for r in rows if x in r["title"]])
        for c in cand:
            if c not in seen:
                seen.add(c)
                out.append(c)
    return out


def _backup_file(path: str) -> str:
    if not os.path.isfile(path):
        return ""
    b = f"{path}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
    shutil.copy2(path, b)
    return os.path.basename(b)


def _write_json_atomic(path: str, obj) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    backup = _backup_file(path)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return backup


_HUAZI_ROWS_CACHE: dict = {"key": None, "rows": None}


def _huazi_rows() -> list:
    """全量花字 + 分类 + 是否启用 + 是否有本地预览图（按文件 mtime 缓存）。"""
    try:
        sig = (os.path.getmtime(HUAZI_JSON), os.path.getmtime(FAV_HUAZI_JSON))
    except Exception:
        sig = None
    if sig is not None and _HUAZI_ROWS_CACHE["key"] == sig:
        return _HUAZI_ROWS_CACHE["rows"]

    all_h = _read_json(HUAZI_JSON, [])
    cats = _read_json(HUAZI_CAT_JSON, {})
    id2cat: dict[str, str] = {}
    for name, ids in (cats.get("categories") or {}).items():
        for i in ids:
            id2cat.setdefault(str(i), name)
    for i in (cats.get("other") or []):
        id2cat.setdefault(str(i), "其他")

    seen, rows = set(), []
    for e in all_h:
        i = str(e.get("id") or "")
        if not i or i in seen:
            continue
        seen.add(i)
        rows.append({
            "id": i,
            "title": e.get("title") or "(无标题)",
            "vip": bool(e.get("is_vip")),
            "cat": id2cat.get(i, "未分类"),
            "fav": False,
        })

    fav_ids = set(_normalize_fav(_fav_huazi_raw(), rows))
    for r in rows:
        r["fav"] = r["id"] in fav_ids

    if sig is not None:
        _HUAZI_ROWS_CACHE["key"] = sig
        _HUAZI_ROWS_CACHE["rows"] = rows
    return rows


def _huazi_preview_path(eid: str):
    """花字预览图：先项目内抽好的，再回退剪映缓存里的 cover_icon.png。"""
    p = os.path.join(HUAZI_PREVIEW_DIR, eid + ".png")
    if os.path.isfile(p):
        return p
    d = os.path.join(JY_EFFECT_CACHE, eid)
    if os.path.isdir(d):
        hits = sorted(glob.glob(os.path.join(d, "*", "cover_icon.png")),
                      key=os.path.getsize, reverse=True)
        if hits:
            return hits[0]
    return None


def _huazi_preview_index() -> set:
    """一次性扫出「有预览图」的花字 id 集合。

    两个来源合并：
      1. 项目内已抽好的 huazi_library/preview/<id>.png（离线可用）
      2. 剪映缓存 Cache/artistEffect/<id>/*/cover_icon.png（实时）
    第 2 条意味着：用户在剪映里点开新花字后，这里**不用重跑脚本**就能识别到，
    刷新页面即可看到新图。
    """
    out = set()
    if os.path.isdir(HUAZI_PREVIEW_DIR):
        for f in os.listdir(HUAZI_PREVIEW_DIR):
            if f.lower().endswith(".png"):
                out.add(f[:-4])
    if os.path.isdir(JY_EFFECT_CACHE):
        for d in os.listdir(JY_EFFECT_CACHE):
            if d in out:
                continue
            p = os.path.join(JY_EFFECT_CACHE, d)
            if os.path.isdir(p) and glob.glob(
                    os.path.join(p, "*", "cover_icon.png")):
                out.add(d)
    return out


def _capcut_running() -> bool:
    """剪映主程序是否在运行。

    用主二进制路径精确匹配（.app/Contents/MacOS/ 下），排除残留的
    TrayHelper / Helper 子进程——剪映退出后常残留一个托盘 Helper 进程，
    用 `pgrep VideoFusion` 会误判成「运行中」。
    """
    try:
        r = subprocess.run(
            ["pgrep", "-f",
             "VideoFusion-macOS.app/Contents/MacOS/VideoFusion-macOS"],
            capture_output=True, text=True)
        return r.returncode == 0 and bool(r.stdout.strip())
    except Exception:
        return False


def _index_status() -> dict:
    idx_path = os.path.join(g.JIANYING_DRAFT_DIR, "root_meta_info.json")
    if not os.path.isfile(idx_path):
        return {"count": 0, "unique": 0}
    try:
        idx = json.load(open(idx_path, encoding="utf-8"))
        store = idx.get("all_draft_store", [])
        ids = [e.get("draft_id") for e in store]
        return {"count": len(store), "unique": len(set(ids))}
    except Exception:
        return {"count": -1, "unique": -1}


def _pool_and_categories(key: str):
    spec = _load_spec(key)
    pool: list = []
    for d in spec.get("pool_dirs", []):
        pool += _walk(d)
    roots = [os.path.expanduser(d) for d in spec.get("pool_dirs", [])]
    cats = g.group_by_category(pool, roots) if pool else {}
    history = g.load_history(os.path.expanduser(spec["history_path"]))
    return spec, pool, roots, cats, history


def _slots_info(spec: dict) -> dict:
    """「每条段数」的有效值，供前端展示。

    - spec.slots 留空 / 0 / "auto" → 按配方长度算（slots_auto=True）：
      各配方长度一致时 slots = 该长度；不一致时 slots = None（每条长度不同）。
      没有配方时回退 6（generator 的默认）。
    - spec.slots 填了数字 → 以它为准（slots_auto=False），配方按它截断/补位。
    """
    want = g.normalize_slots(spec.get("slots"))
    recipes = spec.get("shot_recipes") or (
        [spec["shot_recipe"]] if spec.get("shot_recipe") else None)
    lens = [len(r) for r in recipes] if recipes else []
    if want:
        return {"slots": want, "slots_auto": False, "recipe_lens": lens}
    if lens:
        return {"slots": lens[0] if len(set(lens)) == 1 else None,
                "slots_auto": True, "recipe_lens": lens}
    return {"slots": 6, "slots_auto": True, "recipe_lens": []}


def _is_random_mode(spec: dict) -> bool:
    """rule_mode == "random" → 本次出片忽略配方（配方本身仍留在 spec 里，随时切回来）。"""
    return (spec.get("rule_mode") or "recipe") == "random"


def _build_full(key: str, count: int, seed: int, title_offset: int,
                video_speed: float, speed: float, dry_run: bool,
                name: Optional[str] = None, ignore_history: bool = False) -> dict:
    """按 gen_project.main() 的口径组装 generate_remix 的入参。"""
    spec, pool, roots, _, _ = _pool_and_categories(key)
    _rand = _is_random_mode(spec)
    full = {
        "name": name or (f"{spec.get('name_prefix', '混剪')}_"
                         f"{title_offset + 1}-{title_offset + count}"),
        "pool": pool,
        "pool_roots": roots,
        "bgm_dirs": _bgm_dirs_of(spec),
        "titles": os.path.expanduser(spec["titles"]),
        "font_file": os.path.expanduser(spec["font_file"]),
        "huazi_file": os.path.expanduser(spec["huazi_file"]),
        "history_path": os.path.expanduser(spec["history_path"]),
        "count": count,
        "slots": spec.get("slots"),          # None = 按配方长度算（随机模式下默认 6 段）
        # rule_mode=random 时把配方置空：只按段数随机，配方原样保留在 spec 里
        "shot_recipe": None if _rand else spec.get("shot_recipe"),
        "shot_recipes": None if _rand else spec.get("shot_recipes"),
        "slot_groups": None if _rand else spec.get("slot_groups"),
        "beat_sync": spec.get("beat_sync", True),
        "shuffle_slots": spec.get("shuffle_slots", False),
        "clip_jitter": spec.get("clip_jitter", 0.0),
        "bgm_jitter": spec.get("bgm_jitter", 0.0),
        "start_jitter": spec.get("start_jitter", 0.0),
        "exclusive_adjacent": spec.get("exclusive_adjacent"),
        "shot_dur": spec.get("shot_dur", 2.0),
        "min_diff": spec.get("min_diff", 2),
        "seed": seed,
        "title_offset": title_offset,
        "video_speed": video_speed,
        "speed": speed,
        "ignore_history": ignore_history,
        "dry_run": dry_run,
    }
    # 店面选用规则（文案 title_filter / BGM bgm_filter）——白名单漏传会静默「用全部」
    full.update({k: spec[k] for k in g.SPEC_PASSTHROUGH_KEYS if k in spec})
    return full


# ---------------------------------------------------------------------------
# 请求体
# ---------------------------------------------------------------------------
class RunReq(BaseModel):
    count: int = 30
    seed: Optional[int] = None
    title_offset: Optional[int] = None
    video_speed: float = 1.0
    speed: float = 1.0
    name: Optional[str] = None
    ignore_history: bool = False
    force: bool = False           # True = 跳过剪映运行检查，强制生成


class RegisterReq(BaseModel):
    folder: str
    name: str = ""


class FixReq(BaseModel):
    action: str = "dedup"          # dedup | rebuild
    folder: Optional[str] = None   # rebuild 时必填


class SpecSaveReq(BaseModel):
    fields: dict                   # 要合并进 spec.json 的字段；值为 null 表示删除该字段


class CreateStoreReq(BaseModel):
    name: str                      # 店面显示名，如「张江鱼杂」
    key: Optional[str] = None      # 标识（留空自动生成 custom1/custom2…）
    template: str = "yuza0911yy"   # 复制哪个店面的规则（默认跃杨鱼杂）
    desc: str = ""
    pool_dirs: list = []
    bgm_dirs: list = []
    bgm_group: str = "默认"        # 新店默认标签；之后在 BGM 库页可改


# ---------------------------------------------------------------------------
# 只读接口
# ---------------------------------------------------------------------------
@app.get("/api/status")
def api_status():
    return {
        "version": VER.__version__,
        "capcut_running": _capcut_running(),
        "draft_dir": g.JIANYING_DRAFT_DIR,
        "index": _index_status(),
        # 有木有「关页面 → 待退出」的倒计时（新页面加载会取消，见 /api/keepalive）
        "shutdown_pending": _SHUTDOWN["deadline"] is not None,
    }


@app.get("/api/stores")
def api_stores():
    out = []
    for s in STORES:
        spec, pool, _, cats, history = _pool_and_categories(s["key"])
        out.append({
            "key": s["key"],
            "label": s["label"],
            "desc": s["desc"],
            "name_prefix": spec.get("name_prefix"),
            "pool_count": len(pool),
            "categories": [{"name": k, "count": len(v)}
                           for k, v in sorted(cats.items())],
            "history_count": len(history),
            "next_offset": len(history),
            **_slots_info(spec),
            "has_recipe": bool(spec.get("shot_recipes")
                               or spec.get("shot_recipe")),
            "has_slot_groups": bool(spec.get("slot_groups")),
            "beat_sync": spec.get("beat_sync", True),
            "exclusive_adjacent": spec.get("exclusive_adjacent") or [],
            "bgm_dirs": _bgm_dirs_of(spec),
            "bgm_count": sum(len(_walk_audio(d)) for d in _bgm_dirs_of(spec)),
            "builtin": s["key"] in _BUILTIN_KEYS,
        })
    return {"stores": out,
            "templates": [{"key": s["key"], "label": s["label"],
                           "desc": s["desc"]} for s in STORES]}


def _next_key(prefix: str = "custom") -> str:
    n = 1
    while f"{prefix}{n}" in BY_KEY:
        n += 1
    return f"{prefix}{n}"


@app.post("/api/stores")
def api_create_store(req: CreateStoreReq):
    """新建店面：按模板店面的规则复制一份 spec，改名称 / 素材池 / BGM / 历史文件。

    新店面立刻出现在左侧栏，规则已按模板预填（默认模板 = 跃杨鱼杂）。
    """
    name = (req.name or "").strip()
    if not name:
        raise HTTPException(400, "店面名不能为空")

    key = (req.key or "").strip() or _next_key()
    if not all(c.isalnum() or c in "_-" for c in key):
        raise HTTPException(400, "标识只能用字母 / 数字 / 下划线 / 连字符")
    if key in BY_KEY:
        raise HTTPException(400, f"标识已被占用：{key}")

    tpl = BY_KEY.get(req.template)
    if not tpl:
        raise HTTPException(400, f"模板店面不存在：{req.template}")
    if not os.path.isfile(tpl["spec"]):
        raise HTTPException(400, f"模板配置不存在：{tpl['spec']}")

    tpl_spec = json.load(open(tpl["spec"], encoding="utf-8"))
    spec = dict(tpl_spec)
    spec["name_prefix"] = name
    spec["bgm_group"] = (req.bgm_group or "默认").strip() or "默认"
    spec["pool_dirs"] = [d for d in (req.pool_dirs or []) if d]

    bgm = [d for d in (req.bgm_dirs or []) if d]
    if bgm:
        spec["bgm_dirs"] = bgm
        spec.pop("bgm_dir", None)

    # 独立历史文件：跨批次去重不跟模板店串
    spec["history_path"] = os.path.join(HERE, f"produced_history_{key}.jsonl")

    # 文案库：新店面一律先用共享默认库（assets/titles.txt），之后再单独改写
    spec["titles"] = _to_tilde(DEFAULT_TITLES)

    d = os.path.join(HERE, "projects", key)
    os.makedirs(d, exist_ok=True)
    spec_path = os.path.join(d, "spec.json")
    if os.path.exists(spec_path):
        raise HTTPException(400, f"配置已存在：{spec_path}")
    with open(spec_path, "w", encoding="utf-8") as f:
        json.dump(spec, f, ensure_ascii=False, indent=2)
        f.write("\n")

    rows = _load_custom_stores()
    rows.append({
        "key": key,
        "label": name,
        "desc": req.desc or f"自定义 · 规则模板：{tpl['label']}",
        "spec": spec_path,
        "template": req.template,      # 记下模板 → BGM 区据此显示「默认曲库」
    })
    _save_custom_stores(rows)
    _refresh_stores()
    return {"ok": True, "key": key, "label": name, "spec": spec_path}


@app.delete("/api/stores/{key}")
def api_delete_store(key: str):
    """删除自定义店面（只取消注册，不动已产出的历史文件）。内置店面不可删。"""
    if key not in BY_KEY:
        raise HTTPException(404, "未知店面")
    if key in _BUILTIN_KEYS:
        raise HTTPException(400, "内置店面不能删除")
    rows = [r for r in _load_custom_stores() if r.get("key") != key]
    _save_custom_stores(rows)
    _refresh_stores()
    return {"ok": True, "key": key}


@app.get("/api/stores/{key}")
def api_store_detail(key: str):
    if key not in BY_KEY:
        raise HTTPException(404, "未知店面")
    s = BY_KEY[key]
    spec, pool, _, cats, history = _pool_and_categories(key)
    recent = []
    for h in history[-10:]:
        recent.append({
            "name": h.get("name"),
            "combo": h.get("combo"),
            "title": h.get("title"),
        })
    return {
        "key": key,
        "label": s["label"],
        "desc": s["desc"],
        "builtin": key in _BUILTIN_KEYS,
        "spec": {
            "name_prefix": spec.get("name_prefix"),
            "pool_dirs": spec.get("pool_dirs"),
            "bgm_dirs": [{"path": d, "exists": os.path.isdir(d),
                          "count": len(_walk_audio(d))}
                         for d in _bgm_dirs_of(spec)],
            "titles": spec.get("titles"),
            "title_filter": T.normalize_filter(spec.get("title_filter")),
            "title_filter_is_default": T.is_default_filter(spec.get("title_filter")),
            "history_path": spec.get("history_path"),
            **_slots_info(spec),
            "shot_dur": spec.get("shot_dur", 2.0),
            "min_diff": spec.get("min_diff", 2),
            "beat_sync": spec.get("beat_sync", True),
            "rule_mode": spec.get("rule_mode") or "recipe",   # recipe=按配方 / auto=自动排序 / random=只按段数随机
            "auto_opening": spec.get("auto_opening"),
            "auto_ending": spec.get("auto_ending"),
            "shot_recipe": spec.get("shot_recipe"),
            "shot_recipes": spec.get("shot_recipes"),
            "slot_groups": spec.get("slot_groups"),
            "exclusive_adjacent": spec.get("exclusive_adjacent") or [],
            "clip_jitter": spec.get("clip_jitter", 0.0),
            "bgm_jitter": spec.get("bgm_jitter", 0.0),
            "start_jitter": spec.get("start_jitter", 0.0),
        },
        "pool_count": len(pool),
        # 素材文件夹 → 子文件夹（= 类别/小项目）清单，供前端项目卡悬停展示
        "pool_tree": [_pool_tree(d) for d in (spec.get("pool_dirs") or [])],
        "categories": [{"name": k, "count": len(v)}
                       for k, v in sorted(cats.items())],
        "history_count": len(history),
        "next_offset": len(history),
        "recent_history": recent,
    }


@app.get("/api/pick_folder")
def api_pick_folder(prompt: str = "选择素材文件夹"):
    """弹出 macOS 原生「选择文件夹」对话框，返回绝对路径。

    浏览器出于安全限制拿不到本地绝对路径（拖拽也只给文件名不给路径），
    所以用 osascript 弹系统选择器，把用户选的文件夹路径填回控制台。
    prompt 可自定义对话框标题（如「选择 BGM 文件夹」）。
    """
    safe = (prompt or "选择素材文件夹").replace("\\", "").replace('"', "") \
                                           .replace("'", "")[:60]
    try:
        r = subprocess.run(
            ["osascript", "-e",
             f'POSIX path of (choose folder with prompt "{safe}")'],
            capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        raise HTTPException(500, "选择超时")
    except Exception as e:
        raise HTTPException(500, f"无法弹出选择器：{e}")
    path = (r.stdout or "").strip()
    return {"path": path, "cancelled": not path}


@app.get("/api/bgm_scan")
def api_bgm_scan(path: str):
    """扫描一个 BGM 文件夹：返回音频文件数 + 文件名列表（递归子目录）。

    与 generator 的 BGM 池口径一致（AUDIO_EXTS：mp3/wav/m4a/aac/flac）。
    """
    p = os.path.expanduser(path.strip())
    if not os.path.isdir(p):
        return {"path": p, "exists": False, "audio_count": 0, "files": []}
    files = _walk_audio(p)
    return {"path": p, "exists": True, "audio_count": len(files),
            "files": [os.path.relpath(x, p) for x in files]}


@app.get("/api/scan_folder")
def api_scan_folder(path: str):
    """扫描一个素材文件夹：识别文件地址（绝对路径）、文件名、以及「意思类别」。

    类别 = 文件所在子文件夹名；文件直接躺在根目录时退回「文件名去结尾数字」。
    这与 generator.category_of / group_by_category 口径一致。
    """
    p = os.path.expanduser(path.strip())
    if not os.path.isdir(p):
        return {"path": p, "exists": False, "video_count": 0, "categories": []}
    files = [os.path.abspath(os.path.join(r, f))
             for r, _, fs in os.walk(p)
             for f in sorted(fs) if f.lower().endswith(g.VIDEO_EXTS)]
    cats = g.group_by_category(files, [p])
    out = [{"name": k, "count": len(v),
            "files": [os.path.basename(x) for x in v]}
           for k, v in sorted(cats.items())]
    return {"path": p, "exists": True, "video_count": len(files),
            "categories": out}


# ---------------------------------------------------------------------------
# 「拖进来自动识别路径」
#   ⚠ 浏览器出于安全限制**拿不到本地绝对路径**（拖拽也只给文件名），
#     所以这里的做法是：拿到拖入项的名字（文件夹名 / 文件名 + 大小），
#     在本机常见根目录与**已配置素材夹的父目录**下按名字搜出来，再让用户确认。
#     已有「📁 浏览选择」（osascript 系统选择器）拿的是真路径，两条路并存。
# ---------------------------------------------------------------------------
class DropReq(BaseModel):
    names: list = []               # 拖入项的名字（文件夹名或文件名）
    sizes: list = []               # 与 names 一一对应；文件夹给 null
    limit: int = 8                 # 每个名字最多返回几个候选


_DROP_CACHE: dict = {}
# 明显不可能放素材、又特别大的目录，直接跳过（不跳会拖慢整盘搜索）
_DROP_SKIP = {"Library", "node_modules", ".git", ".venv", "__pycache__",
              ".Trash", "Applications", "System", ".cache", ".local",
              ".npm", ".workbuddy", "Caches", "Containers"}


def _drop_search_roots() -> list:
    """搜索起点：先已配置素材/BGM 文件夹的父目录（命中率最高），再常见根目录。

    **按「具体 → 宽泛」排序**：具体的（如 `…/ai处理好的鱼杂素材`）先走，
    走宽泛的（如 `~/Desktop`）时剪掉已被具体起点覆盖的子树，
    否则同一棵子树会被走好几遍，既慢又让同一个路径重复命中。
    """
    cands = []
    for k in list(BY_KEY):
        try:
            spec = _load_spec(k)
        except Exception:
            continue
        for d in (spec.get("pool_dirs") or []) + _bgm_dirs_of(spec):
            p = os.path.expanduser(d).rstrip("/")
            if os.path.isdir(p):
                cands.append(os.path.dirname(p))
    cands += [os.path.expanduser(x) for x in
              ("~/Desktop", "~/Documents", "~/Movies", "~/Downloads", "~/")]
    out, seen = [], set()
    for c in cands:
        if not c:
            continue
        c = os.path.abspath(c)
        if c not in seen and os.path.isdir(c):
            seen.add(c)
            out.append(c)
    out.sort(key=lambda p: -p.count(os.sep))        # 具体优先
    return out


def _count_media(dirpath: str, cap: int = 3000) -> dict:
    """粗略统计目录里的视频/音频数量（有上限，避免把大盘扫描成卡顿）。"""
    v = a = n = 0
    for r, ds, fs in os.walk(dirpath):
        ds[:] = [d for d in ds if not d.startswith(".")]
        for f in fs:
            n += 1
            low = f.lower()
            if low.endswith(g.VIDEO_EXTS):
                v += 1
            elif low.endswith(g.AUDIO_EXTS):
                a += 1
        if n >= cap:
            break
    return {"video_count": v, "audio_count": a, "scanned": n, "capped": n >= cap}


def _find_dropped(name: str, size=None, limit: int = 8,
                  max_sec: float = 6.0) -> list:
    """按名字在本机找文件夹 / 文件，返回候选列表（path/kind/…，已去重）。"""
    name = (name or "").strip()
    if not name or name.startswith("."):
        return []
    ck = (name, None if size is None else int(size))
    if ck in _DROP_CACHE:
        return _DROP_CACHE[ck]

    roots = _drop_search_roots()
    covered = set(roots)                     # 已被更具体起点覆盖的子树，别再走
    hits, seen, t0 = [], set(), time.time()

    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            if time.time() - t0 > max_sec or len(hits) >= limit:
                break
            keep = []
            for d in dirnames:
                if d.startswith(".") or d in _DROP_SKIP:
                    continue
                full = os.path.join(dirpath, d)
                if full in covered and full != root:
                    continue                     # 这棵子树有更具体的起点在走，跳过
                keep.append(d)
            dirnames[:] = keep
            if os.path.basename(dirpath) == name:
                if dirpath not in seen:
                    seen.add(dirpath)
                    hits.append({"path": dirpath, "kind": "dir"})
                continue
            if size is not None and name in filenames:
                fp = os.path.join(dirpath, name)
                if fp in seen:
                    continue
                try:
                    if abs(os.path.getsize(fp) - int(size)) <= 1024:
                        seen.add(fp)
                        hits.append({"path": fp, "kind": "file"})
                except OSError:
                    pass
        if len(hits) >= limit or time.time() - t0 > max_sec:
            break

    for h in hits:                                  # 目录补上素材数量，方便辨认
        if h["kind"] == "dir":
            h.update(_count_media(h["path"]))
    _DROP_CACHE[ck] = hits
    return hits


@app.post("/api/locate_dropped")
def api_locate_dropped(req: DropReq):
    """把「拖入的文件夹/文件名字」在本机定位成绝对路径。

    返回每个拖入项的候选列表（含目录里的视频/音频数量，便于用户挑对那一个）。
    前端规则：唯一命中就直接填入；多个命中就让用户点选；0 个就提示改用「浏览选择」。
    """
    names = [str(x) for x in (req.names or [])][:20]
    out = []
    for i, n in enumerate(names):
        sz = req.sizes[i] if i < len(req.sizes) else None
        try:
            sz = int(sz) if sz not in (None, "", "null") else None
        except (TypeError, ValueError):
            sz = None
        hits = _find_dropped(n, sz, limit=max(1, min(int(req.limit or 8), 20)))
        # 目录优先（拖文件夹是主用法）
        hits.sort(key=lambda h: (h["kind"] != "dir", h.get("video_count", 0) * -1))
        out.append({"name": n, "hits": hits})
    return {"items": out, "roots": _drop_search_roots()}


# ---------------------------------------------------------------------------
# 素材文件夹 → 子项目树（前端「项目卡」悬停展示用）
# ---------------------------------------------------------------------------
def _pool_tree(dirpath: str) -> dict:
    """{path, exists, total, subs:[{name,count}]}，只算媒体文件，跳过点目录。"""
    p = os.path.expanduser(dirpath)
    if not os.path.isdir(p):
        return {"path": p, "exists": False, "total": 0, "subs": []}
    subs, total, direct = [], 0, 0
    try:
        names = sorted(os.listdir(p))
    except OSError:
        return {"path": p, "exists": True, "total": 0, "subs": []}
    exts = g.VIDEO_EXTS + g.AUDIO_EXTS
    for n in names:
        if n.startswith("."):
            continue
        full = os.path.join(p, n)
        if os.path.isdir(full):
            c = 0
            for _r, ds, fs in os.walk(full):
                ds[:] = [x for x in ds if not x.startswith(".")]
                c += sum(1 for f in fs if f.lower().endswith(exts))
            subs.append({"name": n, "count": c})
            total += c
        elif n.lower().endswith(exts):
            direct += 1
    if direct:
        subs.insert(0, {"name": "（直接放在根目录）", "count": direct})
        total += direct
    subs.sort(key=lambda x: -x["count"])
    return {"path": p, "exists": True, "total": total, "subs": subs}


# ---------------------------------------------------------------------------
# 历史版本：列出 / 回退（每次保存都自动留 .bak-时间戳）
# ---------------------------------------------------------------------------
_SPEC_KEY_LABELS = {"pool_dirs": "素材池", "bgm_dirs": "BGM 库", "bgm_dir": "BGM 库",
                    "shot_recipes": "配方", "shot_recipe": "配方",
                    "slot_groups": "结构配方", "slots": "每条段数",
                    "exclusive_adjacent": "排他类别", "beat_sync": "卡点模式",
                    "title_filter": "文案选用", "bgm_filter": "BGM 选用"}


class RestoreReq(BaseModel):
    name: str


@app.get("/api/stores/{key}/backups")
def api_store_backups(key: str):
    if key not in BY_KEY:
        raise HTTPException(404, "未知店面")
    path = BY_KEY[key]["spec"]
    folder, base = os.path.dirname(path), os.path.basename(path)
    try:
        with open(path, encoding="utf-8") as f:
            cur = json.load(f)
    except Exception:
        cur = {}
    out = []
    for n in sorted(glob.glob(os.path.join(folder, base + ".bak-*")), reverse=True)[:10]:
        try:
            with open(n, encoding="utf-8") as f:
                old = json.load(f)
        except Exception:
            continue
        changed = []
        for k in _SPEC_KEY_LABELS:
            if json.dumps(old.get(k), ensure_ascii=False, sort_keys=True) != \
               json.dumps(cur.get(k), ensure_ascii=False, sort_keys=True):
                lb = _SPEC_KEY_LABELS[k]
                if lb not in changed:
                    changed.append(lb)
        out.append({"name": os.path.basename(n),
                    "size": os.path.getsize(n),
                    "changed": changed})
    return {"ok": True, "backups": out}


@app.post("/api/stores/{key}/restore")
def api_store_restore(key: str, req: RestoreReq):
    if key not in BY_KEY:
        raise HTTPException(404, "未知店面")
    path = BY_KEY[key]["spec"]
    folder, base = os.path.dirname(path), os.path.basename(path)
    name = os.path.basename(req.name or "")           # 防路径穿越
    if not name.startswith(base + ".bak-"):
        raise HTTPException(400, "不是该店面的备份文件")
    src = os.path.join(folder, name)
    if not os.path.isfile(src):
        raise HTTPException(404, "备份不存在")
    backup = path + ".bak-" + time.strftime("%Y%m%d-%H%M%S")
    shutil.copy2(path, backup)                        # 先留一份「回退前的现在」
    shutil.copy2(src, path)
    return {"ok": True, "backup": os.path.basename(backup)}


# ---------------------------------------------------------------------------
# AI 自动部署规则（DeepSeek）
#   Key 只存本机 projects/_ai.conf（0o600，已 gitignore），不随代码走。
# ---------------------------------------------------------------------------
_AI_CONF = os.path.join(HERE, "projects", "_ai.conf")

_AI_SYS = """你是「剪映批量混剪」的镜头规则助手。按用户要求改写镜头配方（shot_recipes）。
可用类别（只能从这里选，名字必须完全一致）：
{cats}

当前配方：{cur}
每条段数：{slots}

规则要点：
- 配方是「按拍摄/上菜顺序排列的段」，每段只放一个类别；
- 同一配方内允许某个类别出现两次（成片会取不同文件）；
- 每套配方 2~12 段；可以给 1 套或多套（多套按条轮换，能降低相邻重复）。
- 类别名若形如「项目名/子文件夹」，表示只取那个项目下的这个夹（跨项目重名时才有前缀）。
- 也可以用特殊标签："*" = 整个素材池随机。
- 某一段想要「从多个夹里任意挑一个」时，这一段写成数组：["夹菜","浇汤"]。

只输出 JSON，不要多余文字，格式：
{{"shot_recipes": [["类别A","类别B"], ...], "slots": 数字或 null, "note": "一句话说明改了什么"}}"""


class AiConfReq(BaseModel):
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    model: Optional[str] = None


class AiRuleReq(BaseModel):
    prompt: str


def _ai_conf() -> dict:
    try:
        with open(_AI_CONF, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


@app.get("/api/ai/config")
def api_ai_config():
    c = _ai_conf()
    k = (c.get("api_key") or "").strip()
    hint = (k[:6] + "…" + k[-4:]) if len(k) > 12 else ("已配置" if k else "")
    return {"ok": True, "has_key": bool(k), "key_hint": hint,
            "base_url": c.get("base_url") or "https://api.deepseek.com",
            "model": c.get("model") or "deepseek-chat"}


@app.post("/api/ai/config")
def api_ai_config_save(req: AiConfReq):
    c = _ai_conf()
    if req.api_key is not None:
        c["api_key"] = req.api_key.strip()
    if req.base_url:
        c["base_url"] = req.base_url.strip()
    if req.model:
        c["model"] = req.model.strip()
    os.makedirs(os.path.dirname(_AI_CONF), exist_ok=True)
    with open(_AI_CONF, "w", encoding="utf-8") as f:
        json.dump(c, f, ensure_ascii=False, indent=2)
    try:
        os.chmod(_AI_CONF, 0o600)
    except OSError:
        pass
    return {"ok": True, "has_key": bool((c.get("api_key") or "").strip())}


@app.post("/api/stores/{key}/ai_rules")
def api_ai_rules(key: str, req: AiRuleReq):
    """让 DeepSeek 按自然语言要求直接改写配方，并**立即写盘**（旧版留在 .bak 可回退）。"""
    if key not in BY_KEY:
        raise HTTPException(404, "未知店面")
    conf = _ai_conf()
    api_key = (conf.get("api_key") or "").strip()
    if not api_key:
        raise HTTPException(400, "还没填 DeepSeek API Key")
    if not (req.prompt or "").strip():
        raise HTTPException(400, "先写一句要求，比如「按上菜顺序排一套 6 段配方」")

    spec, _pool, _roots, cats, _hist = _pool_and_categories(key)
    cur = spec.get("shot_recipes") or (
        [spec["shot_recipe"]] if spec.get("shot_recipe") else [])
    sysmsg = _AI_SYS.format(cats=json.dumps(sorted(cats), ensure_ascii=False),
                            cur=json.dumps(cur, ensure_ascii=False),
                            slots=spec.get("slots"))
    payload = json.dumps({
        "model": conf.get("model") or "deepseek-chat",
        "messages": [{"role": "system", "content": sysmsg},
                     {"role": "user", "content": req.prompt.strip()}],
        "response_format": {"type": "json_object"},
        "temperature": 0.2,
    }).encode("utf-8")
    base = (conf.get("base_url") or "https://api.deepseek.com").rstrip("/")
    rq = urllib.request.Request(base + "/chat/completions", data=payload,
                                headers={"Content-Type": "application/json",
                                         "Authorization": "Bearer " + api_key})
    try:
        with urllib.request.urlopen(rq, timeout=90) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise HTTPException(502, f"DeepSeek 返回 {e.code}：{e.read().decode(errors='replace')[:300]}")
    except Exception as e:
        raise HTTPException(502, f"调用 DeepSeek 失败：{e}")
    try:
        plan = json.loads(data["choices"][0]["message"]["content"])
    except Exception:
        raise HTTPException(502, "DeepSeek 返回的不是合法 JSON，再试一次")

    recs = plan.get("shot_recipes") or []
    # 允许：完整键 / 裸名（跨项目重名时取并集）/ 特殊标签
    allowed = set(cats) | {k.rsplit("/", 1)[-1] for k in cats} | set(g.SLOT_SPECIALS)
    norm, bad = [], set()
    for r in recs:
        if not isinstance(r, list):
            continue
        row = []
        for slot in r:
            names = slot if isinstance(slot, list) else [slot]
            for c in names:
                if c not in allowed:
                    bad.add(str(c))
            row.append([c for c in names])
        norm.append(row)
    if bad:
        raise HTTPException(400, "AI 给的类别在素材池里不存在：" + "、".join(sorted(bad)))
    if not norm or any(not (2 <= len(r) <= 12) for r in norm):
        raise HTTPException(400, "AI 给的配方段数不合理（每套需 2~12 段）")

    slots = g.normalize_slots(plan.get("slots"))
    api_save_spec(key, SpecSaveReq(fields={
        "shot_recipes": norm if len(norm) > 1 else None,
        "shot_recipe": norm[0] if len(norm) == 1 else None,
        "slots": slots,
        "slot_groups": None,
    }))
    return {"ok": True, "shot_recipes": norm, "slots": slots,
            "note": str(plan.get("note") or "").strip()}


@app.post("/api/stores/{key}/spec")
def api_save_spec(key: str, req: SpecSaveReq):
    """把用户改过的规则写回该店面的 spec.json（写前自动备份）。

    例：fields = {"shot_recipes": [...], "pool_dirs": [...],
                 "exclusive_adjacent": [...], "beat_sync": false}
    值为 null 的字段会被删除。
    """
    if key not in BY_KEY:
        raise HTTPException(404, "未知店面")
    spec_path = BY_KEY[key]["spec"]
    if not os.path.isfile(spec_path):
        raise HTTPException(500, f"spec 不存在：{spec_path}")
    spec = json.load(open(spec_path, encoding="utf-8"))
    backup = f"{spec_path}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
    shutil.copy2(spec_path, backup)
    for k, v in (req.fields or {}).items():
        if v is None:
            spec.pop(k, None)
        else:
            spec[k] = v
    with open(spec_path, "w", encoding="utf-8") as f:
        json.dump(spec, f, ensure_ascii=False, indent=2)
    keys = ("name_prefix", "pool_dirs", "bgm_dirs", "bgm_dir",
            "shot_recipes", "shot_recipe", "slots",
            "slot_groups", "exclusive_adjacent", "beat_sync", "rule_mode")
    return {"ok": True, "backup": os.path.basename(backup),
            "spec": {k: spec.get(k) for k in keys}}


# ---------------------------------------------------------------------------
# 花字库 / 字体池
# ---------------------------------------------------------------------------
@app.get("/api/huazi")
def api_huazi():
    """全量花字 + 启用状态 + 字体池。rows 里 fav=True 即为「已启用」。"""
    rows = _huazi_rows()
    have = _huazi_preview_index()
    for r in rows:
        r["img"] = r["id"] in have
    return {
        "rows": rows,
        "fonts": _fav_fonts_raw(),
        "fav_file": os.path.relpath(FAV_HUAZI_JSON, HERE),
        "font_file": os.path.relpath(FAV_FONTS_JSON, HERE),
        "with_preview": sorted(have),
        "total": len(rows),
        "fav_count": sum(1 for r in rows if r["fav"]),
    }


class HuaziFavReq(BaseModel):
    ids: list = []                      # 启用池（ID 或名称都行，会归一化成 ID）
    fonts: Optional[list] = None        # 传了才写字体池


@app.post("/api/huazi/fav")
def api_save_huazi_fav(req: HuaziFavReq):
    """把「启用池」写回 favorite_huazi.json（写前自动备份 .bak-时间戳）。

    生成标题时会从这份池里随机挑 1 个花字 → 保存后下次出片即生效。
    """
    rows = _huazi_rows()
    ids = _normalize_fav([str(x) for x in (req.ids or [])], rows)
    unknown = [str(x) for x in (req.ids or [])
               if str(x).strip() not in {r["id"] for r in rows}
               and str(x).strip() not in {r["title"] for r in rows}]
    if not ids:
        raise HTTPException(
            400, "启用池不能为空——至少留 1 个花字，否则标题会没有花字效果")
    backup = _write_json_atomic(FAV_HUAZI_JSON, ids)
    out = {"ok": True, "count": len(ids), "ids": ids,
           "backup": backup, "unknown": unknown}
    if req.fonts is not None:
        fonts, s = [], set()
        for n in req.fonts:
            n = str(n).strip()
            if n and n not in s:
                s.add(n)
                fonts.append(n)
        out["font_backup"] = _write_json_atomic(FAV_FONTS_JSON, fonts)
        out["font_count"] = len(fonts)
    _HUAZI_ROWS_CACHE["key"] = None      # 让下一次读取重新算 fav 标记
    return out


@app.get("/api/huazi/preview/{eid}")
def api_huazi_preview(eid: str):
    """花字封面图（项目内抽好的，或剪映缓存里的 cover_icon.png）。"""
    p = _huazi_preview_path(eid)
    if not p:
        raise HTTPException(404, "该花字本地没有预览图")
    return FileResponse(p, media_type="image/png")


# ---------------------------------------------------------------------------
# 文案库（标题库）
#   库文件每行 `[类型] 第一段|第二段`（类型可省 → 未分类；`#` 开头为注释）
#   spec.titles        → 这个店面用哪个库文件（没配 = 共享默认库 assets/titles.txt）
#   spec.title_filter  → 从库里选哪些（默认全部 / 整类排除 / 单条勾选）
#   使用次数           → 从 produced_history*.jsonl 聚合（只统计真出过的片）
# ---------------------------------------------------------------------------
def _to_tilde(p: str) -> str:
    home = os.path.expanduser("~")
    return "~" + p[len(home):] if p.startswith(home) else p


def _lib_of(key: str = None) -> dict:
    """这个店面（key 为空 = 默认库）用哪个文案库文件。"""
    raw = DEFAULT_TITLES
    if key and key in BY_KEY:
        raw = _load_spec(key).get("titles") or DEFAULT_TITLES
    path = os.path.expanduser(raw)
    return {
        "raw": raw,
        "path": path,
        "rel": os.path.relpath(path, HERE) if path.startswith(HERE) else path,
        "is_default": os.path.abspath(path) == os.path.abspath(DEFAULT_TITLES),
        "exists": os.path.isfile(path),
    }


def _all_history() -> list:
    """汇总所有店面的产出历史（全局使用次数用）。"""
    out = []
    for p in sorted(set(glob.glob(os.path.join(HERE, "produced_history*.jsonl")))):
        out.extend(T.load_history(p))
    return out


def _bgm_usage(key: str = None) -> dict:
    """BGM 文件名 → 用过几次。给了 key 就只统计那个店面。"""
    hist = (T.load_history(_load_spec(key).get("history_path"))
            if (key and key in BY_KEY) else _all_history())
    cnt = {}
    for h in hist:
        b = os.path.basename((h.get("bgm") or "").strip())
        if b:
            cnt[b] = cnt.get(b, 0) + 1
    return cnt


def _lib_payload(key: str = None, lib: str = None) -> dict:
    """文案库全量：条目（含类型 / 全局次数 / 本店次数）+ 类型汇总 + 本店选用。"""
    info = _lib_of(key)
    if lib:
        path = os.path.expanduser(lib)
        info = dict(info, path=path,
                    rel=os.path.relpath(path, HERE) if path.startswith(HERE) else path,
                    is_default=os.path.abspath(path) == os.path.abspath(DEFAULT_TITLES),
                    exists=os.path.isfile(path))
    entries = T.load_library(info["path"])

    global_cnt = T.usage_counts(_all_history())
    store_cnt = (T.usage_counts(T.load_history(_load_spec(key).get("history_path")))
                 if (key and key in BY_KEY) else {})

    rows = [{"type": e["type"], "text": e["text"],
             "count": global_cnt.get(e["text"], 0),
             "count_store": store_cnt.get(e["text"], 0)}
            for e in entries]

    types = []
    for name in T.types_of(entries):
        sub = [r for r in rows if r["type"] == name]
        types.append({"name": name, "count": len(sub),
                      "used": sum(1 for r in sub if r["count"] > 0),
                      "uses": sum(r["count"] for r in sub)})

    spec = _load_spec(key) if (key and key in BY_KEY) else {}
    flt = T.normalize_filter(spec.get("title_filter"))
    effective = T.apply_filter(entries, flt)

    lib_texts = {r["text"] for r in rows}
    return {
        "key": key,
        "path": info["path"], "rel": info["rel"],
        "is_default": info["is_default"], "exists": info["exists"],
        "default_rel": os.path.relpath(DEFAULT_TITLES, HERE),
        "default_path": DEFAULT_TITLES,
        "rows": rows, "types": types,
        "total": len(rows),
        "used_total": sum(1 for r in rows if r["count"] > 0),
        "unused_total": sum(1 for r in rows if r["count"] == 0),
        # 历史里用过、但已经不在库里的文案（换过库/删过条目留下的「漂移」）
        "stale": sorted([t for t in global_cnt if t not in lib_texts],
                        key=lambda t: -global_cnt[t])[:50],
        "stale_total": len([t for t in global_cnt if t not in lib_texts]),
        "filter": flt,
        "filter_is_default": T.is_default_filter(flt),
        "effective": len(effective),
    }


@app.get("/api/copy")
def api_copy(key: Optional[str] = None, lib: Optional[str] = None):
    """文案库全量（类型 / 使用次数 / 本店选用 / 生效条数）。"""
    if key and key not in BY_KEY:
        raise HTTPException(404, "未知店面")
    return _lib_payload(key, lib)


class CopySaveReq(BaseModel):
    entries: list                    # [{"type":..., "text":...}] 全量覆盖
    lib: Optional[str] = None        # 存到哪个库文件（默认 = 该店面的库）
    key: Optional[str] = None


@app.post("/api/copy")
def api_copy_save(req: CopySaveReq):
    """保存文案库内容（新增 / 删除 / 改文案 / 改类型都在这里，全量覆盖写）。"""
    path = os.path.expanduser(req.lib) if req.lib else _lib_of(req.key)["path"]
    entries = []
    for e in (req.entries or []):
        if isinstance(e, dict):
            entries.append({"type": e.get("type") or T.UNCLASSIFIED,
                            "text": str(e.get("text") or "")})
        else:
            entries.append({"type": T.UNCLASSIFIED, "text": str(e)})
    if not [e for e in entries if e["text"].strip()]:
        raise HTTPException(400, "文案库不能为空——至少留 1 条，否则出片没有标题")

    bak, n = T.save_library(path, entries)
    out = _lib_payload(req.key, path)
    out.update({"ok": True, "backup": bak, "count": n})
    return out


@app.get("/api/copy/export")
def api_copy_export(key: Optional[str] = None, lib: Optional[str] = None,
                    with_type: int = 1, only_used: int = 0, types: str = ""):
    """导出为 txt（可带类型前缀；可直接改完再导回来）。"""
    info = _lib_of(key)
    path = os.path.expanduser(lib) if lib else info["path"]
    entries = T.load_library(path)
    if types.strip():
        keep = {x.strip() for x in types.split(",") if x.strip()}
        entries = [e for e in entries if e["type"] in keep]
    if only_used:
        cnt = T.usage_counts(_all_history())
        entries = [e for e in entries if cnt.get(e["text"], 0) > 0]

    if with_type:
        body = T.dump_library(entries, header="文案库 · 每行一条 · [类型] 第一段|第二段")
    else:
        body = "\n".join(e["text"] for e in entries) + ("\n" if entries else "")
    name = os.path.basename(path) or "文案库.txt"
    return PlainTextResponse(
        body, media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename*=UTF-8''%s" % quote(name)})


class CopyImportReq(BaseModel):
    text: str                        # 导入的 txt 内容（也支持直接粘贴）
    mode: str = "merge"              # merge（新类型覆盖旧）/ append（只补新的）/ replace（整库替换）
    lib: Optional[str] = None
    key: Optional[str] = None


@app.post("/api/copy/import")
def api_copy_import(req: CopyImportReq):
    """导入文案：解析 → 与现库合并 → 保存。"""
    if req.mode not in ("merge", "append", "replace"):
        raise HTTPException(400, "mode 只能是 merge / append / replace")
    path = os.path.expanduser(req.lib) if req.lib else _lib_of(req.key)["path"]
    new = T.load_library_text(req.text)
    if not new:
        raise HTTPException(400, "没解析到任何文案（每行一条，格式：[类型] 第一段|第二段）")

    old = T.load_library(path)
    old_texts = {e["text"] for e in old}
    added = sum(1 for e in new if e["text"] not in old_texts)
    merged = T.merge_entries(old, new, req.mode)
    bak, n = T.save_library(path, merged)

    out = _lib_payload(req.key, path)
    out.update({"ok": True, "backup": bak, "count": n,
                "imported": len(new), "added": added, "mode": req.mode})
    return out


class StoreCopyReq(BaseModel):
    path: Optional[str] = None       # 换成这个库文件
    save_as: Optional[str] = None    # 另存为本店专用（只给文件名，落 assets/）
    reset: Optional[bool] = False    # 恢复共享默认库


@app.post("/api/stores/{key}/copy")
def api_store_copy_set(key: str, req: StoreCopyReq):
    """设定这个店面**用哪个**文案库文件（内容编辑走 POST /api/copy）。"""
    if key not in BY_KEY:
        raise HTTPException(404, "未知店面")
    spec_path, spec = BY_KEY[key]["spec"], _load_spec(key)
    cur = _lib_of(key)

    if req.reset:
        target = DEFAULT_TITLES
    elif req.save_as:
        name = os.path.basename(str(req.save_as).strip())
        if not name:
            raise HTTPException(400, "文件名不能为空")
        if not name.lower().endswith(".txt"):
            name += ".txt"
        target = os.path.join(HERE, "assets", name)
        # 以当前库内容为底稿落一个新文件（不存在才写，避免覆盖已有）
        if not os.path.isfile(target):
            T.save_library(target, T.load_library(cur["path"]), backup=False)
    elif req.path:
        target = os.path.expanduser(str(req.path).strip())
        if os.path.isdir(target):
            raise HTTPException(400, "这是文件夹，需要选 .txt 文件：%s" % target)
        if not os.path.isfile(target):
            raise HTTPException(400, "文案库文件不存在：%s" % target)
    else:
        raise HTTPException(400, "要传 path / save_as / reset 之一")

    want = _to_tilde(target)
    spec_backup = ""
    if spec.get("titles") != want:
        spec_backup = _backup_file(spec_path)
        spec["titles"] = want
        with open(spec_path, "w", encoding="utf-8") as f:
            json.dump(spec, f, ensure_ascii=False, indent=2)

    out = _lib_payload(key)
    out.update({"ok": True, "spec_backup": spec_backup})
    return out


class CopyFilterReq(BaseModel):
    exclude_types: Optional[list] = None    # 整类排除
    only: Optional[list] = None             # 只留这些条（单选/多选）
    exclude: Optional[list] = None          # 单条排除
    reset: Optional[bool] = False           # 恢复「使用全部」


@app.get("/api/stores/{key}/copy_filter")
def api_get_copy_filter(key: str):
    if key not in BY_KEY:
        raise HTTPException(404, "未知店面")
    flt = T.normalize_filter(_load_spec(key).get("title_filter"))
    return {"key": key, "filter": flt, "is_default": T.is_default_filter(flt)}


@app.post("/api/stores/{key}/copy_filter")
def api_set_copy_filter(key: str, req: CopyFilterReq):
    """设定本店用文案库里的哪些文案（空 = 用全部）。"""
    if key not in BY_KEY:
        raise HTTPException(404, "未知店面")
    spec_path, spec = BY_KEY[key]["spec"], _load_spec(key)

    if req.reset:
        spec.pop("title_filter", None)
    else:
        flt = T.normalize_filter({"exclude_types": req.exclude_types,
                                  "only": req.only, "exclude": req.exclude})
        if T.is_default_filter(flt):
            spec.pop("title_filter", None)   # 「用全部」不必留字段
        else:
            spec["title_filter"] = flt

    spec_backup = _backup_file(spec_path)
    with open(spec_path, "w", encoding="utf-8") as f:
        json.dump(spec, f, ensure_ascii=False, indent=2)

    out = _lib_payload(key)
    out.update({"ok": True, "spec_backup": spec_backup})
    return out


@app.get("/api/bgm/usage")
def api_bgm_usage(key: Optional[str] = None):
    """BGM 使用次数（从产出历史聚合）。给了 key 就只看那个店面。"""
    cnt = _bgm_usage(key)
    if key and key in BY_KEY:
        dirs = _bgm_dirs_of(_load_spec(key))
    else:
        dirs = sorted({d for k in BY_KEY for d in _bgm_dirs_of(_load_spec(k))})
    rows = []
    seen = set()
    for d in dirs:
        for p in _walk_audio(d):
            name = os.path.basename(p)
            if name in seen:
                continue
            seen.add(name)
            rows.append({"path": p, "name": name, "count": cnt.get(name, 0)})
    rows.sort(key=lambda r: (-r["count"], r["name"]))
    return {"rows": rows, "total": len(rows),
            "used": sum(1 for r in rows if r["count"] > 0),
            "uses": sum(r["count"] for r in rows)}


# ---------------------------------------------------------------------------
# BGM 曲库 v2：默认曲库（来自模板店）+ 本店曲目清单 + 本店选用哪些
# ---------------------------------------------------------------------------
def _bgm_default_of(key: str) -> dict:
    """本店的「默认 BGM 曲库」= 建店时那个模板店的曲库。

    新店创建时会记下 `template`；老店 / 内置店没有记录 → 回退默认模板。
    界面上这就是「留空 = 沿用模板曲库」的那一份。
    """
    row = next((r for r in _load_custom_stores() if r.get("key") == key), None)
    tkey = (row or {}).get("template") or DEFAULT_BGM_TEMPLATE
    t = BY_KEY.get(tkey) or BY_KEY.get(DEFAULT_BGM_TEMPLATE)
    if not t:
        return {"key": None, "label": "（未找到模板店）", "dirs": []}
    return {"key": t["key"], "label": t["label"],
            "dirs": _bgm_dirs_of(_load_spec(t["key"]))}


def _bgm_dir_row(d: str) -> dict:
    return {"path": d,
            "name": os.path.basename(d.rstrip("/\\")) or d,
            "exists": os.path.isdir(d),
            "count": len(_walk_audio(d))}


def _bgm_payload(key: str) -> dict:
    """BGM 全量：本店曲目（含选中状态 / 用量）+ 默认曲库 + 选用规则状态。"""
    spec = _load_spec(key)
    dirs = _bgm_dirs_of(spec)
    flt = g.normalize_bgm_filter(spec.get("bgm_filter"))
    only, ex = set(flt["only"]), set(flt["exclude"])

    store_cnt = _bgm_usage(key)
    glob_cnt = _bgm_usage(None)

    rows, seen = [], set()
    for d in dirs:
        for p in _walk_audio(d):
            ap = os.path.abspath(p)
            if ap in seen:
                continue
            seen.add(ap)
            name = os.path.basename(ap)
            sel = (ap not in ex) and (ap in only if only else True)
            rows.append({
                "path": ap, "name": name,
                "dir": d, "dir_name": os.path.basename(d.rstrip("/\\")) or d,
                "rel": os.path.relpath(ap, HERE) if ap.startswith(HERE) else "",
                "count": store_cnt.get(name, 0),          # 本店用过几次
                "count_all": glob_cnt.get(name, 0),       # 全局用过几次
                "selected": bool(sel),
            })
    rows.sort(key=lambda r: (-r["count"], r["name"]))

    tpl = _bgm_default_of(key)
    ddirs = [_bgm_dir_row(d) for d in tpl["dirs"]]
    return {
        "key": key,
        "dirs": [_bgm_dir_row(d) for d in dirs],
        "rows": rows,
        "total": len(rows),
        "selected_total": sum(1 for r in rows if r["selected"]),
        "used_total": sum(1 for r in rows if r["count"] > 0),
        # 累计用量（曲目清单直接显示，替代原先上方的「📊 BGM 使用次数」面板）
        "uses_total": sum(r["count"] for r in rows),        # 本店累计用过几次
        "uses_all_total": sum(r["count_all"] for r in rows),  # 全局累计用过几次
        "default_from": {"key": tpl["key"], "label": tpl["label"]},
        "default_dirs": ddirs,
        "is_default_dirs": (
            sorted(os.path.abspath(d) for d in dirs)
            == sorted(os.path.abspath(x["path"]) for x in ddirs)),
        "filter": flt,
        "filter_is_default": g.is_default_bgm_filter(spec.get("bgm_filter")),
        "beat_sync": spec.get("beat_sync", True),   # BGM 库独立页的卡点开关
    }


# BGM 规则里允许被「按范围保存」改写的字段（白名单，防止误改其它剪辑规则）
BGM_RULE_FIELDS = ("bgm_dirs", "bgm_dir", "beat_sync", "bgm_filter", "bgm_group")


def _bgm_group_of(key: str) -> str:
    """店面所属的 BGM 分组标签。spec.bgm_group 优先；没配就按 name_prefix 推断
    （鱼杂混剪 / 鱼杂混剪_0911杭州 → 「鱼杂」；牛鞭混剪 → 「牛鞭」）。"""
    spec = _load_spec(key)
    g = (spec.get("bgm_group") or "").strip()
    if g:
        return g
    pref = (spec.get("name_prefix") or "").split("_")[0]
    return pref.replace("混剪", "").strip() or pref or key


def _bgm_write_fields(key: str, fields: dict) -> str:
    """把 BGM 规则字段写回某个 spec（写前自动备份）。值为 None 的字段删除。"""
    spec_path = BY_KEY[key]["spec"]
    spec = json.load(open(spec_path, encoding="utf-8"))
    backup = f"{spec_path}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
    shutil.copy2(spec_path, backup)
    for k, v in fields.items():
        if k not in BGM_RULE_FIELDS:
            continue
        if v is None:
            spec.pop(k, None)
        else:
            spec[k] = v
    with open(spec_path, "w", encoding="utf-8") as f:
        json.dump(spec, f, ensure_ascii=False, indent=2)
    return os.path.basename(backup)


class BgmSaveReq(BaseModel):
    key: str
    scope: str = "store"          # store=只存本项目 / group=按标签批量
    fields: dict = {}
    group: Optional[str] = None  # 非空时给当前项目改标签
    groups: Optional[list] = None  # scope=group 时选中的标签；None=兼容旧调用（当前标签）


@app.post("/api/bgm/save")
def api_bgm_save(req: BgmSaveReq):
    """保存 BGM 使用规则，可选范围：仅本项目 / 勾选标签下的全部项目。"""
    if req.key not in BY_KEY:
        raise HTTPException(404, f"店面不存在：{req.key}")
    group = _bgm_group_of(req.key)          # 按「改标签之前」的组找同组项目
    if req.scope == "group":
        wanted = ({group} if req.groups is None else
                  {g.strip() for g in req.groups if isinstance(g, str) and g.strip()})
        targets = [k for k in BY_KEY if _bgm_group_of(k) in wanted]
    else:
        wanted = set()
        targets = [req.key]

    fields = dict(req.fields or {})
    saved = []
    for k in targets:
        f = dict(fields)
        # 标签改名只写当前项目：批量保存时不把其它标签的项目一起改组
        if k == req.key and req.group is not None:
            g = req.group.strip()
            f["bgm_group"] = g or None     # 留空 = 删掉，回到自动推断
        backup = _bgm_write_fields(k, f)
        saved.append({"key": k, "label": BY_KEY[k]["label"], "backup": backup})
    return {"ok": True, "scope": req.scope, "group": group,
            "groups": sorted(wanted), "targets": targets, "saved": saved}


@app.get("/api/bgm/stream")
def api_bgm_stream(path: str):
    """试听：只放行「任一店面已配置的 BGM 文件夹」内的音频，防任意路径读取。"""
    ap = os.path.abspath(os.path.expanduser(path))
    allowed = []
    for k in BY_KEY:
        for d in _bgm_dirs_of(_load_spec(k)):
            allowed.append(os.path.abspath(os.path.expanduser(d)))
    if not any(ap == d or ap.startswith(d + os.sep) for d in allowed):
        raise HTTPException(403, "该路径不在任何已配置的 BGM 曲库文件夹内")
    if not os.path.isfile(ap) or not ap.lower().endswith(g.AUDIO_EXTS):
        raise HTTPException(404, "音频文件不存在")
    mt = {".mp3": "audio/mpeg", ".wav": "audio/wav", ".m4a": "audio/mp4",
          ".aac": "audio/aac", ".flac": "audio/flac"}.get(
              os.path.splitext(ap)[1].lower(), "application/octet-stream")
    return FileResponse(ap, media_type=mt, filename=os.path.basename(ap))


@app.get("/api/bgm")
def api_bgm(key: str):
    """BGM 区数据：本店曲目清单 + 默认曲库 + 选用规则 + 分组标签。

    保存选用规则直接走 POST /api/stores/{key}/spec（fields={"bgm_filter": …}），
    传 null 即「用全部」。按范围批量保存走 POST /api/bgm/save。
    """
    if key not in BY_KEY:
        raise HTTPException(404, f"店面不存在：{key}")
    d = _bgm_payload(key)
    group = _bgm_group_of(key)
    d["group"] = group
    d["group_explicit"] = bool((_load_spec(key).get("bgm_group") or "").strip())
    d["group_stores"] = [{"key": k, "label": BY_KEY[k]["label"]}
                         for k in BY_KEY if _bgm_group_of(k) == group]
    d["all_stores"] = [{"key": k, "label": BY_KEY[k]["label"],
                        "group": _bgm_group_of(k)} for k in BY_KEY]
    return d


@app.get("/api/pick_file")
def api_pick_file(prompt: str = "选择文案库文件", default_dir: str = ""):
    """macOS 原生「选择文件」对话框（选 .txt 文案库）。

    与 pick_folder 同理：浏览器拿不到本地绝对路径，只能靠系统选择器。
    """
    safe = (prompt or "选择文件").replace("\\", "").replace('"', "") \
                                  .replace("'", "")[:60]
    loc = os.path.expanduser(default_dir) if default_dir else os.path.join(HERE, "assets")
    if not os.path.isdir(loc):
        loc = os.path.expanduser("~")
    try:
        r = subprocess.run(
            ["osascript", "-e",
             f'POSIX path of (choose file with prompt "{safe}" '
             f'default location POSIX file "{loc}")'],
            capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        raise HTTPException(500, "选择超时")
    path = (r.stdout or "").strip()
    return {"path": path, "cancelled": not path}


@app.get("/api/drafts")
def api_drafts():
    return {"drafts": g.list_drafts()}


@app.get("/api/orphans")
def api_orphans():
    return {"orphans": g.list_orphans()}


# ---------------------------------------------------------------------------
# 干跑 / 生成
# ---------------------------------------------------------------------------
def _err_text(e: BaseException) -> str:
    """错误文案：普通异常直接用消息；`SystemExit` 这类 BaseException 要带上类型名。

    ⚠ 为什么要单独处理：`SystemExit` / `KeyboardInterrupt` **不是** `Exception`，
    `except Exception` 抓不到。它们一旦从生成逻辑里逃出来，会把线程池 / 后台线程
    **静默弄死** → 干跑请求永不返回、生成任务永远停在 running（用户只会看到「崩了」，
    看不到任何错）。所以这里的捕获统一用 `except BaseException`。
    """
    return str(e) if isinstance(e, Exception) else f"{type(e).__name__}: {e}"


def _run(key: str, req: RunReq, dry_run: bool) -> dict:
    spec, _, _, _, history = _pool_and_categories(key)
    seed = req.seed if req.seed is not None else (int(time.time() * 1000) % 100000)
    title_offset = req.title_offset if req.title_offset is not None \
        else len(history)
    full = _build_full(key, req.count, seed, title_offset,
                       req.video_speed, req.speed, dry_run,
                       name=req.name, ignore_history=req.ignore_history)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        result = g.generate_remix(full)
    return {
        "seed": seed,
        "title_offset": title_offset,
        "output": buf.getvalue(),
        "result": result,
    }


@app.post("/api/stores/{key}/dry_run")
def api_dry_run(key: str, req: RunReq):
    if key not in BY_KEY:
        raise HTTPException(404, "未知店面")
    try:
        r = _run(key, req, dry_run=True)
    except BaseException as e:      # 含 SystemExit：否则请求永不返回，页面一直转圈
        raise HTTPException(500, _err_text(e))
    return {"ok": True, **r}


@app.post("/api/stores/{key}/generate")
def api_generate(key: str, req: RunReq):
    if key not in BY_KEY:
        raise HTTPException(404, "未知店面")
    if not req.force and _capcut_running():
        raise HTTPException(409, "剪映正在运行，请先 ⌘Q 完全退出再生成（否则草稿索引会写重复）。")
    task_id = str(uuid.uuid4())
    with _TASKS_LOCK:
        TASKS[task_id] = {"id": task_id, "key": key, "status": "running",
                          "output": "", "result": None, "error": None}

    def _worker():
        try:
            r = _run(key, req, dry_run=False)
        except BaseException as e:  # 含 SystemExit：否则线程静默死掉、任务永远 running
            with _TASKS_LOCK:
                TASKS[task_id]["status"] = "error"
                TASKS[task_id]["error"] = _err_text(e)
            return
        with _TASKS_LOCK:
            TASKS[task_id]["status"] = "done"
            TASKS[task_id]["output"] = r["output"]
            TASKS[task_id]["result"] = r["result"]

    threading.Thread(target=_worker, daemon=True).start()
    return {"ok": True, "task_id": task_id}


@app.get("/api/tasks/{task_id}")
def api_task(task_id: str):
    with _TASKS_LOCK:
        t = TASKS.get(task_id)
    if not t:
        raise HTTPException(404, "任务不存在")
    return t


# ---------------------------------------------------------------------------
# 草稿索引维护
# ---------------------------------------------------------------------------
@app.post("/api/register")
def api_register(req: RegisterReq):
    try:
        r = g.register_by_folder(req.folder, req.name)
    except Exception as e:
        raise HTTPException(500, str(e))
    return {"ok": True, **r}


@app.post("/api/fix_index")
def api_fix_index(req: FixReq):
    if _capcut_running():
        raise HTTPException(409, "剪映正在运行，请先 ⌘Q 完全退出（否则修复会被剪映退出时覆盖）。")
    script = os.path.join(HERE, "fix_draft_index.py")
    cmd = [sys.executable, script]
    if req.action == "rebuild":
        if not req.folder:
            raise HTTPException(400, "rebuild 需要 folder")
        cmd += ["--apply", "--rebuild", req.folder]
    else:
        cmd += ["--apply"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        return {"ok": r.returncode == 0, "stdout": r.stdout, "stderr": r.stderr}
    except subprocess.TimeoutExpired:
        raise HTTPException(500, "修复超时")


@app.post("/api/shutdown")
def api_shutdown(soft: bool = False):
    """关闭整个控制台程序（后端进程）。

    - `soft=1`（前端**关页面**时用 sendBeacon 打的）：登记 12 秒倒计时。
      期间只要有人打开/刷新页面打了 `/api/keepalive`，就取消退出 →
      **刷新页面不会再把程序关掉**；真关了页面则 12 秒后自动退出。
    - 不带 soft（用户点「关闭程序」按钮）：0.6 秒后硬退，不给人反悔。
    """
    _SHUTDOWN["deadline"] = time.time() + (12.0 if soft else 0.6)

    def _die():
        while True:
            d = _SHUTDOWN["deadline"]
            if d is None:
                return                      # 被 /api/keepalive 取消了
            left = d - time.time()
            if left <= 0:
                os._exit(0)
            time.sleep(min(0.2, max(0.02, left)))

    threading.Thread(target=_die, daemon=True).start()
    return {"ok": True, "soft": soft,
            "msg": "正在关闭控制台程序…" if not soft else "已登记退出（刷新页面可取消）"}


@app.post("/api/keepalive")
@app.get("/api/keepalive")
def api_keepalive():
    """页面加载时打一下：取消「关页面」登记的待退出倒计时。

    没有它的话，刷新页面 = 先 beforeunload 登记退出、再新页面加载 →
    会把控制台莫名其妙关掉。
    """
    _SHUTDOWN["deadline"] = None
    return {"ok": True, "version": VER.__version__}


# ---------------------------------------------------------------------------
# 静态页
# ---------------------------------------------------------------------------
@app.get("/")
def index():
    return FileResponse(os.path.join(HERE, "console.html"))


if __name__ == "__main__":
    import uvicorn
    print(f"视频混剪控制台 v{VER.__version__} -> http://127.0.0.1:8766  (正式生成前请保持剪映关闭)")
    uvicorn.run(app, host="127.0.0.1", port=8766)
