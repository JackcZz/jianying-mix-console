"""
剪映批量拼接 · 草稿生成器

复用内置的 capcut-mate 草稿引擎（pyJianYingDraft + default2 明文模板），
把本地视频文件直接拷贝进草稿 assets/videos，按真实时长顺序拼接成一条主视频轨，
最后注册进剪映的 root_meta_info.json，使其在剪映草稿列表里可见。

不依赖联网、不需要 http 服务、不需要 UI 自动化。
"""
import os
import sys
import copy
import json
import shutil
import time
import re
import uuid
import tempfile
import random

ENGINE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "engine")
if ENGINE not in sys.path:
    sys.path.insert(0, ENGINE)

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:                   # 保证 titlelib 一定能被 import
    sys.path.insert(0, _HERE)
import titlelib  # noqa: E402
import binpaths  # noqa: E402   # 定位 ffmpeg/ffprobe，兼容 .app 启动的窄 PATH

import config  # noqa: E402
import src.pyJianYingDraft as draft  # noqa: E402
from src.service.create_draft import create_draft  # noqa: E402
from src.service.add_videos import _add_videos_internal  # noqa: E402
from src.utils.draft_cache import DRAFT_CACHE  # noqa: E402
from src.utils import helper  # noqa: E402


class GenError(RuntimeError):
    """「生成前的配置检查没通过」——素材池 / 镜头配方 / 每条段数 配错了。

    ⚠ 一律用这个，**绝不要用 SystemExit**：
    `SystemExit` 继承自 `BaseException`，`except Exception` 抓不到它。
    本模块既被命令行（gen_project.py）调用，也被 Web 控制台的后台线程 / 线程池调用，
    在那里 `SystemExit` 会**静默弄死线程**：干跑请求永不返回（页面一直转圈），
    正式生成的任务状态永远停在 running（前端永远轮询）——用户看到的就是「程序崩了」，
    而且什么错都看不到。详见 pitfalls P13。
    """


try:
    import pymediainfo

    def _duration_us_pymediainfo(path):
        mi = pymediainfo.MediaInfo.parse(path).to_data()
        d = next(
            (float(t["duration"]) for t in mi.get("tracks", [])
             if t.get("track_type") == "Video" and t.get("duration")),
            0.0,
        )
        return int(d * 1000)  # pymediainfo: 毫秒 -> 微秒

    _clip_duration_us = _duration_us_pymediainfo
except Exception:  # pragma: no cover - pymediainfo 缺失时回退 ffprobe
    def _duration_us_ffprobe(path):
        import subprocess
        out = subprocess.run(
            [binpaths.ffprobe_exe(), "-v", "error", "-show_entries",
             "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True,
        )
        return int(float(out.stdout.strip() or 0) * 1_000_000)

    _clip_duration_us = _duration_us_ffprobe


def clip_duration_us(path):
    """返回视频时长（微秒）。"""
    return _clip_duration_us(path)


# 剪映草稿目录（macOS App Store 版）
DEFAULT_DRAFT_DIR = os.path.expanduser(
    "~/Movies/JianyingPro/User Data/Projects/com.lveditor.draft"
)

JIANYING_DRAFT_DIR = DEFAULT_DRAFT_DIR
config.DRAFT_DIR = JIANYING_DRAFT_DIR
# 仅本地使用，DRAFT_URL 不参与草稿加载
config.DRAFT_URL = "http://localhost/draft"

VIDEO_EXTS = (".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm")
AUDIO_EXTS = (".mp3", ".wav", ".m4a", ".aac", ".flac")


def set_draft_dir(d: str) -> None:
    """切换剪映草稿目录（不同版本/平台路径不同）。"""
    global JIANYING_DRAFT_DIR
    JIANYING_DRAFT_DIR = d
    config.DRAFT_DIR = d


def list_clips(folder: str):
    """列出某个目录下的视频文件名（按名排序）。"""
    if not os.path.isdir(folder):
        return []
    return sorted(
        f for f in os.listdir(folder) if f.lower().endswith(VIDEO_EXTS)
    )


def scan_source(root: str):
    """扫描素材根目录：返回 [{name, path, clips:[filename,...]}] 子文件夹列表。"""
    if not os.path.isdir(root):
        return []
    result = []
    for name in sorted(os.listdir(root)):
        p = os.path.join(root, name)
        if os.path.isdir(p):
            clips = list_clips(p)
            if clips:
                result.append({"name": name, "path": p, "clips": clips})
    return result


def _unique_dest_name(src_path: str, used: set = None):
    """目标文件名：保留原始（中文）名，重名时追加 _2/_3 后缀。"""
    ext = os.path.splitext(src_path)[1].lower() or ".mp4"
    base = os.path.splitext(os.path.basename(src_path))[0] or "clip"
    name = f"{base}{ext}"
    if used is not None:
        i = 2
        while name in used:
            name = f"{base}_{i}{ext}"
            i += 1
        used.add(name)
    return name


def _prepare_video_files(draft_dir: str, clips: list, used: set = None):
    """把本地视频拷进草稿 assets/videos（保留原文件名），返回带路径与时长(微秒)的列表。"""
    video_dir = os.path.join(draft_dir, "assets", "videos")
    os.makedirs(video_dir, exist_ok=True)
    prepared = []
    for src in clips:
        dest = os.path.join(video_dir, _unique_dest_name(src, used))
        if not os.path.exists(dest):
            shutil.copy(src, dest)
        prepared.append({
            "src": src,
            "dest": dest,
            "dur_us": clip_duration_us(dest),
        })
    return prepared


def generate(spec: dict):
    """
    生成一条剪映草稿。

    spec:
      name    : 草稿显示名
      width   : 画布宽（默认 1080）
      height  : 画布高（默认 1920，竖屏）
      clips   : 本地视频绝对路径列表（按期望顺序拼接）
    返回: {draft_id, name, duration_us, folder, count}
    """
    name = spec.get("name") or f"拼接_{time.strftime('%m%d_%H%M%S')}"
    width = int(spec.get("width", 1080))
    height = int(spec.get("height", 1920))
    clips = [os.path.abspath(c) for c in spec.get("clips", []) if os.path.isfile(c)]
    if not clips:
        raise RuntimeError("没有可用视频素材")

    # 1) 基于 default2 模板创建空草稿
    url = create_draft(width, height)
    draft_id = helper.get_url_param(url, "draft_id")
    draft_dir = os.path.join(config.DRAFT_DIR, draft_id)

    # 2) 拷贝素材并计算时间轴（本地文件，无需联网）
    prepared = _prepare_video_files(draft_dir, clips, set())
    t = 0
    videos_payload = []
    for p in prepared:
        dur_us = p["dur_us"]
        videos_payload.append({
            "video_url": "",
            "start": t,
            "end": t + dur_us,
            "duration": dur_us,
            "original_start": t,
            "original_end": t + dur_us,
            "local_video_path": p["dest"],
            "volume": 1.0,
        })
        t += dur_us

    # 3) 写入视频片段（直接走已验证的内部链路，跳过联网下载）
    _add_videos_internal(draft_url=url, video_infos="[]", prepared_videos=videos_payload)

    # 4) 注册进剪映索引
    register_draft(draft_id, name, t)
    return {
        "draft_id": draft_id,
        "name": name,
        "duration_us": t,
        "folder": draft_dir,
        "count": len(prepared),
    }


def _add_bgm(script, bgm_path: str, total_us: int, offset_us: int, volume: float,
             speed: float = 1.0, change_pitch: bool = False):
    """加音频轨并放入 BGM（从 offset_us 开始取 total_us 长）。

    speed > 1：拉长取用的源长度（源时长 = total_us × speed），BGM 在原槽位里放快，
    配合 change_pitch=True 随之变调 —— 用来打散平台音频指纹。
    源素材不够长或 speed ≤ 1 时自动退回原速，不会抛错。
    """
    track_name = f"audio_track_{helper.gen_unique_id()}"
    script.add_track(track_type=draft.TrackType.audio, track_name=track_name,
                     relative_index=10)
    mat_dur = draft.AudioMaterial(bgm_path).duration
    room = (mat_dur - offset_us) / max(1, total_us)   # 最多能放快多少倍
    if speed <= 1.0 or room <= 1.0:
        speed, change_pitch = 1.0, False
    else:
        speed = min(speed, room)
    seg = draft.AudioSegment(
        material=bgm_path,
        target_timerange=draft.trange(start=0, duration=total_us),
        source_timerange=draft.trange(start=offset_us,
                                      duration=int(total_us * speed)),
        volume=volume,
        speed=speed,
        change_pitch=change_pitch,
    )
    script.add_segment(seg, track_name)


_HUAZI_PATH = os.path.join(os.path.dirname(__file__), "assets", "huazi.json")
_FAVORITE_HUAZI_PATH = os.path.join(os.path.dirname(__file__), "assets", "favorite_huazi.json")
_FAVORITE_FONT_PATH = os.path.join(os.path.dirname(__file__), "assets", "favorite_fonts.json")
_HUAZI_CACHE = {}   # path -> 全局 huazi.json 内容（按路径缓存）
_FONT_CACHE = {}     # path -> 收藏字体列表（按路径缓存，多项目隔离）


def _pick_random_font(font_file=None):
    """随机选字体（FontType 枚举）。

    优先从收藏字体文件读用户收藏列表（FontType 枚举名）；
    font_file 为 None 时退回默认 `favorite_fonts.json`（鱼杂项目）。
    没有文件/为空时返回 None（使用系统默认字体）。
    缓存按路径隔离，多项目互不污染。
    """
    if font_file is None:
        font_file = _FAVORITE_FONT_PATH
    global _FONT_CACHE
    if font_file not in _FONT_CACHE:
        if os.path.isfile(font_file):
            try:
                _FONT_CACHE[font_file] = json.load(open(font_file, "r", encoding="utf-8"))
            except Exception:
                _FONT_CACHE[font_file] = []
        else:
            _FONT_CACHE[font_file] = []
    import random
    if _FONT_CACHE[font_file]:
        name = random.choice(_FONT_CACHE[font_file])
        try:
            return draft.FontType[name]
        except KeyError:
            pass
    return None


def _ensure_huazi_cache():
    """载入全局 huazi.json（所有花字定义），按路径缓存。"""
    global _HUAZI_CACHE
    if _HUAZI_CACHE.get(_HUAZI_PATH) is None:
        with open(_HUAZI_PATH, "r", encoding="utf-8") as f:
            _HUAZI_CACHE[_HUAZI_PATH] = json.load(f)
    return _HUAZI_CACHE[_HUAZI_PATH]


def _pick_random_huazi(huazi_file=None) -> str:
    """随机选花字 effect_id。

    优先从收藏花字文件读用户收藏列表（名称或 id），
    huazi_file 为 None 时退回默认 `favorite_huazi.json`（鱼杂项目）。
    没有该文件/为空时退回全部 huazi.json 随机。
    """
    if huazi_file is None:
        huazi_file = _FAVORITE_HUAZI_PATH
    all_huazi = _ensure_huazi_cache()
    import random
    # 1) 收藏列表
    fav_ids = _load_favorite_ids(huazi_file)
    if fav_ids:
        return random.choice(fav_ids)
    # 2) 退回全部随机
    return random.choice(all_huazi)["id"]


def _load_favorite_ids(huazi_file=None) -> list:
    """读收藏花字文件，把名称匹配成 huazi.json 里的 id。"""
    if huazi_file is None:
        huazi_file = _FAVORITE_HUAZI_PATH
    if not os.path.isfile(huazi_file):
        return []
    try:
        raw = json.load(open(huazi_file, "r", encoding="utf-8"))
    except Exception:
        raw = []
    if isinstance(raw, dict):
        raw = raw.get("ids", raw.get("names", []))
    if not isinstance(raw, list):
        return []
    all_huazi = _ensure_huazi_cache()
    by_id = {e["id"]: e for e in all_huazi}
    by_name = {e["title"]: e["id"] for e in all_huazi}
    ids = []
    for item in raw:
        item = str(item).strip()
        if not item:
            continue
        if item in by_id:
            ids.append(item)
        elif item in by_name:
            ids.append(by_name[item])
        else:
            # 模糊匹配（标题包含关键词）
            hit = [e["id"] for e in all_huazi if item in e["title"]]
            ids.extend(hit)
    return ids


def _clean_title(text: str, max_chars: int = 12) -> str:
    """标题清洗：去符号 + 按字数自动换行。

    - "|" / "｜" 是强制换行分隔符，本身不会出现在画面里
    - 去掉所有非(中文/字母/数字/空格)的符号（！，。…等）
    - 每段按 max_chars 个汉字切行，保持两行（默认每行≤12字）
    """
    if not text:
        return text
    out_lines = []
    for seg in re.split(r"[|｜]", text):
        # 1) 去符号：仅保留中英文、数字、空白
        cleaned = re.sub(r"[^\u4e00-\u9fff\u3400-\u4dbfa-zA-Z0-9\s]", "", seg)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        if not cleaned:
            continue
        # 2) 按字数换行（中文按字符计）
        #    超长时均分，避免 9+1 这种单字孤行
        if len(cleaned) <= max_chars:
            out_lines.append(cleaned)
            continue
        n_row = -(-len(cleaned) // max_chars)      # 需要几行
        per = -(-len(cleaned) // n_row)            # 每行均分字数
        out_lines.extend(cleaned[i:i + per]
                         for i in range(0, len(cleaned), per))
    return "\n".join(out_lines)


def _add_title(script, text: str, start_us: int, duration_us: int,
               width: int, height: int, font_file=None, huazi_file=None):
    """加文本轨与标题（花字效果，字号15，位置 X=0/Y=1200，缩放102%，标题自动清洗换行，随机收藏字体）。

    font_file / huazi_file 为 None 时退回项目默认收藏池（鱼杂）。
    """
    text = _clean_title(text)
    track_name = f"text_track_{helper.gen_unique_id()}"
    script.add_track_ordered(track_type=draft.TrackType.text, track_name=track_name)

    # 位置：剪映像素(原点在顶部) → ClipSettings 单位(全画布归一化)
    # 实测映射：screen_px = transform * canvas_size，故 transform = px / size
    # X=0 → transform_x = 0（水平居中）；Y=1200 → transform_y = 1200/height
    tx = 0.0 / width if width else 0.0
    ty = 1200.0 / height if height else 0.0

    # 随机字体（从收藏字体池，可按项目指定）
    font = _pick_random_font(font_file)

    seg = draft.TextSegment(
        text=text,
        timerange=draft.Timerange(start=start_us, duration=duration_us),
        font=font,
        style=draft.TextStyle(
            size=15.0, bold=False, color=(1.0, 1.0, 1.0), align=1,
        ),
        clip_settings=draft.ClipSettings(
            transform_x=tx, transform_y=ty,
            scale_x=1.02, scale_y=1.02,
            rotation=0.0,
        ),
    )
    # 随机花字（可按项目指定收藏池）
    try:
        seg.add_effect(_pick_random_huazi(huazi_file))
    except Exception:
        pass  # 花字加载失败时降级为纯文本
    script.add_segment(seg, track_name)


def generate_beat_sync(spec: dict):
    """
    生成卡点视频草稿：BGM 重音驱动剪辑点 + 音频轨 + 可选标题。

    spec:
      name     : 草稿显示名
      width/height : 画布（默认 1080x1920）
      videos   : 本地视频路径列表（每个一段画面）
      bgm      : BGM 音频路径（mp3/m4a/wav…）
      shot_dur : 每段目标时长秒（默认 2.0）
      offset   : BGM 起始偏移秒（跳过前奏，默认 0）
      title    : 标题文本（可选）
      title_dur: 标题持续秒（默认取前 2.2s 与第一段较短者）
      bgm_volume: BGM 音量 0~2（默认 1.0）
    返回: 草稿信息 + cut_points（实际剪辑点，秒）
    """
    import beats as beat_mod

    name = spec.get("name") or f"卡点_{time.strftime('%m%d_%H%M%S')}"
    width = int(spec.get("width", 1080))
    height = int(spec.get("height", 1920))
    videos = [os.path.abspath(v) for v in spec.get("videos", []) if os.path.isfile(v)]
    bgm = spec.get("bgm")
    if not videos:
        raise RuntimeError("没有可用视频素材")
    if not bgm or not os.path.isfile(bgm):
        raise RuntimeError("BGM 文件不存在")
    shot_dur = float(spec.get("shot_dur", 2.0))
    offset = float(spec.get("offset", 0.0))
    bgm_volume = float(spec.get("bgm_volume", 1.0))
    title = (spec.get("title") or "").strip()

    # 1) 建草稿
    url = create_draft(width, height)
    draft_id = helper.get_url_param(url, "draft_id")
    draft_dir = os.path.join(config.DRAFT_DIR, draft_id)
    script = DRAFT_CACHE[draft_id]

    # 2) 拷贝视频素材
    used = set()
    prepared = _prepare_video_files(draft_dir, videos, used)

    # 3) 拷贝 BGM 进草稿
    audio_dir = os.path.join(draft_dir, "assets", "audios")
    os.makedirs(audio_dir, exist_ok=True)
    bgm_dest = os.path.join(audio_dir, _unique_dest_name(bgm, used))
    shutil.copy(bgm, bgm_dest)

    # 4) 按重音取剪辑点（BGM 不够长时自动压缩每段时长）
    bgm_dur_us = draft.AudioMaterial(bgm_dest).duration
    available = max(0.0, (bgm_dur_us - int(offset * 1e6)) / 1e6)
    eff_shot = min(shot_dur, available / len(videos))
    if eff_shot < 0.4:
        raise RuntimeError(
            f"BGM 可用时长 {available:.1f}s 不足以铺 {len(videos)} 段，"
            f"请换长一点的 BGM 或减少素材数"
        )
    cut_points = beat_mod.pick_cut_points(
        bgm_dest, n_shots=len(videos), shot_dur=eff_shot, offset=offset,
    )
    # 视频不够长时截断到素材时长
    shots = []
    for i in range(len(cut_points) - 1):
        dur = cut_points[i + 1] - cut_points[i]
        if prepared[i]["dur_us"] < dur * 1e6:
            dur = prepared[i]["dur_us"] / 1e6
        shots.append(dur)
    total = sum(shots)
    total_us = int(total * 1e6)
    offset_us = int(offset * 1e6)

    # 5) 视频轨
    videos_payload = []
    t = 0
    for i, p in enumerate(prepared):
        dur_us = int(shots[i] * 1e6)
        videos_payload.append({
            "video_url": "",
            "start": t,
            "end": t + dur_us,
            "duration": dur_us,
            "original_start": t,
            "original_end": t + dur_us,
            "local_video_path": p["dest"],
            "volume": 1.0,
        })
        t += dur_us
    _add_videos_internal(draft_url=url, video_infos="[]", prepared_videos=videos_payload)

    # 6) BGM 轨
    _add_bgm(script, bgm_dest, total_us, offset_us, bgm_volume)

    # 7) 标题（默认贯穿全片 = BGM 长度）
    if title:
        title_dur_us = int(
            float(spec.get("title_dur", 0) or total) * 1e6
        )
        title_dur_us = min(title_dur_us, total_us)
        _add_title(script, title, 0, title_dur_us, width, height)

    # 8) 保存 + 注册
    script.save()
    register_draft(draft_id, name, total_us)
    return {
        "draft_id": draft_id,
        "name": name,
        "duration_us": total_us,
        "folder": draft_dir,
        "count": len(prepared),
        "cut_points": cut_points,
        "shots": [round(s, 3) for s in shots],
    }


# ---------------------------------------------------------------------------
# 批量：一个草稿 = 多条成片，每条成片一个「复合片段」，供剪映批量导出
# ---------------------------------------------------------------------------
# 结构来源：剪映 11.3.0 真实复合片段草稿（用剪映自带 libvideoeditor.dylib
# 解密得到），字段模板见 composite_ref.json
_COMPOSITE_REF = None


def _composite_ref():
    """读「复合片段」结构模板 composite_ref.json。

    兼容三种布局，按顺序找（防止整体迁移目录时漏搬这个文件）：
      1. 环境变量 JIANYING_COMPOSITE_REF 指定的路径
      2. 与本文件同目录（旧 jianying-stitcher 布局）
      3. 本文件同目录下的 assets/（现行 jianying-mix-console 布局）
    """
    global _COMPOSITE_REF
    if _COMPOSITE_REF is None:
        env = os.environ.get("JIANYING_COMPOSITE_REF")
        cands = []
        if env:
            cands.append(os.path.expanduser(env))
        cands += [
            os.path.join(_HERE, "composite_ref.json"),
            os.path.join(_HERE, "assets", "composite_ref.json"),
        ]
        for p in cands:
            if os.path.isfile(p):
                with open(p, encoding="utf-8") as f:
                    _COMPOSITE_REF = json.load(f)
                break
        else:
            raise FileNotFoundError(
                "找不到 composite_ref.json（复合片段结构模板）。已查找：\n  "
                + "\n  ".join(cands)
                + "\n把它放到 assets/ 下，或用环境变量 JIANYING_COMPOSITE_REF 指定。"
            )
    return _COMPOSITE_REF


def _beat_cut(bgm_dest: str, n_shots: int, shot_dur: float, offset: float):
    """按重音取剪辑点，BGM 不够长时自动压缩每段。返回 (cut_points, shots)。"""
    import beats as beat_mod

    bgm_dur_us = draft.AudioMaterial(bgm_dest).duration
    available = max(0.0, (bgm_dur_us - int(offset * 1e6)) / 1e6)
    eff_shot = min(shot_dur, available / n_shots)
    if eff_shot < 0.4:
        raise RuntimeError(
            f"BGM 可用时长 {available:.1f}s 不足以铺 {n_shots} 段，"
            f"请换长一点的 BGM 或减少素材数"
        )
    cut_points = beat_mod.pick_cut_points(
        bgm_dest, n_shots=n_shots, shot_dur=eff_shot, offset=offset,
    )
    shots = [cut_points[i + 1] - cut_points[i]
             for i in range(len(cut_points) - 1)]
    return cut_points, shots


def _apply_video_speed(inner: dict, video_speed: float):
    """只给内部视频段加速（画面变快），BGM 段保持原速。

    原理：每个视频段占据的时间轴槽位(target_timerange)不变，
    仅把 source_timerange 拉长到 target×video_speed（拉入更多源素材），
    于是画面以 video_speed 倍速播放，而 BGM 槽位不变、节奏不被拖快。
    """
    videos = {m["id"]: m for m in inner["materials"].get("videos", [])}
    speeds = {m["id"]: m for m in inner["materials"].get("speeds", [])}
    for tr in inner["tracks"]:
        for seg in tr.get("segments", []):
            mid = seg.get("material_id")
            if mid not in videos:
                continue
            target = seg["target_timerange"]["duration"]
            max_source = videos[mid].get("duration", target)
            desired = int(target * video_speed)
            if desired > max_source:
                # 素材太短，强行变速会超出源时长 → 跳过该段，保持原速
                continue
            seg["target_timerange"]["duration"] = target
            seg["source_timerange"]["duration"] = desired
            seg["speed"] = round(desired / target, 4)
            sp_id = (seg.get("extra_material_refs") or [None])[0]
            if sp_id in speeds:
                speeds[sp_id]["speed"] = seg["speed"]


def _randomize_clips(inner: dict, rng: random.Random, intensity: float = 1.0) -> int:
    """给内层草稿的每段视频加随机微变换，打散平台画面查重指纹。

    手法（都很轻，肉眼几乎看不出）：
      - 随机水平镜像 —— 对感知哈希是最彻底的破坏
      - 轻微放大 2%~6% + 轻微位移：裁掉边缘、改变帧内容边界，同时避免旋转露黑边
      - 随机微旋转 ±0.6°
      - 随机放慢到 96%~100% + 掐头 0~0.25s：改变帧序列与起始帧。
        目标时长（= 成片长度）不变，代价是每段丢掉结尾 ≤4%

    刻意不做调色/滤镜：感知哈希基本只看灰度结构，色偏影响很小；而往
    extra_material_refs 里插滤镜材料有破坏草稿结构的风险，不划算。
    返回实际处理过的视频段数。
    """
    videos = {m["id"]: m for m in inner["materials"].get("videos", [])}
    speeds = {m["id"]: m for m in inner["materials"].get("speeds", [])}
    touched = 0
    for tr in inner.get("tracks", []):
        if tr.get("type") != "video":
            continue
        for seg in tr.get("segments", []):
            mat = videos.get(seg.get("material_id"))
            if mat is None:
                continue
            clip = seg.setdefault("clip", {})
            clip.setdefault("flip", {})["horizontal"] = rng.random() < 0.5
            scale = 1.0 + rng.uniform(0.02, 0.06) * intensity
            clip["scale"] = {"x": round(scale, 4), "y": round(scale, 4)}
            clip["transform"] = {
                "x": round(rng.uniform(-0.03, 0.03) * intensity, 4),
                "y": round(rng.uniform(-0.03, 0.03) * intensity, 4),
            }
            clip["rotation"] = round(rng.uniform(-0.6, 0.6) * intensity, 3)
            touched += 1

            src = seg.get("source_timerange") or {}
            tgt = seg.get("target_timerange") or {}
            tgt_us, mat_us = tgt.get("duration"), mat.get("duration")
            src_start = src.get("start", 0) or 0
            if not (tgt_us and mat_us):
                continue
            room = mat_us - src_start                  # 起点到素材末尾的余量
            # 不卡点模式下素材已被 100% 用满（source 窗口 == 整条文件），
            # 没有「加速」余量，只放慢到 96%~100%：目标时长不变、成片长度不变，
            # 换来时序指纹差异；代价是丢掉结尾 ≤4%，这部分再切给「掐头」。
            sp = 1.0 - rng.uniform(0.0, 0.04) * intensity
            need = min(int(tgt_us * sp), room)
            if need < 1000:
                need = min(tgt_us, room)
            head_room = max(0, room - need)
            head_cap = int(min(0.25e6 * intensity, head_room))
            head = int(rng.uniform(0, head_cap)) if head_cap > 1000 else 0
            src["start"] = src_start + head
            src["duration"] = need
            seg["source_timerange"] = src
            seg["speed"] = round(need / tgt_us, 4)
            sp_id = (seg.get("extra_material_refs") or [None])[0]
            if sp_id in speeds:
                speeds[sp_id]["speed"] = seg["speed"]
    return touched


def _randomize_starts(inner: dict, rng: random.Random, max_start: float) -> int:
    """随机化每段视频的「起始点」，专为同一素材复用做去重。

    引擎默认从素材第 0 秒开始截；本函数把起始点随机后移到
    [0, min(max_start, 素材长-实际取长)] 之间。同一素材在多条成片里复用、
    卡点段长又固定时，每条取到不同的画面片段，平台查重指纹就散了。

    关键：素材越比段长长，随机余量越大（余量 = 素材长 - 段长×倍速）。
    素材比段长还短时余量为 0，跳过（无随机空间）。
    返回实际改了起始点的段数。
    """
    videos = {m["id"]: m for m in inner["materials"].get("videos", [])}
    touched = 0
    for tr in inner.get("tracks", []):
        if tr.get("type") != "video":
            continue
        for seg in tr.get("segments", []):
            mat = videos.get(seg.get("material_id"))
            if mat is None:
                continue
            src = seg.get("source_timerange") or {}
            need = src.get("duration") or \
                (seg.get("target_timerange") or {}).get("duration")
            mat_us = mat.get("duration")
            if not (need and mat_us):
                continue
            # 扣掉 _randomize_clips 已经掐掉的头，否则两者叠加会超出素材末尾
            head_room = mat_us - need - (src.get("start", 0) or 0)
            if head_room <= 1000:
                continue                              # 素材用满/太短，无空间
            cap = int(min(max_start * 1e6, head_room))
            if cap <= 1000:
                continue
            head = int(rng.uniform(0, cap))
            src["start"] = (src.get("start", 0) or 0) + head
            seg["source_timerange"] = src
            touched += 1
    return touched


def _build_beat_item(item: dict, draft_dir: str, width: int, height: int,
                     video_speed: float = 1.0, font_file=None, huazi_file=None,
                     rng: random.Random = None, clip_jitter: float = 0.0,
                     bgm_jitter: float = 0.0, used_names: set = None,
                     start_jitter: float = 0.0):
    """造一条卡点成片：返回 (内层草稿 dict, 实际时长秒, 剪辑点, 分段)。

    clip_jitter > 0 时对每段画面做随机微变换（镜像/裁切/微旋转/掐头/变速）。
    bgm_jitter > 0 时给 BGM 加随机加速 + 变调。
    """
    clips = [os.path.abspath(v) for v in item.get("videos", []) if os.path.isfile(v)]
    bgm = item.get("bgm")
    if not clips:
        raise RuntimeError("某条成片没有可用视频素材")
    if not bgm or not os.path.isfile(bgm):
        raise RuntimeError(f"BGM 不存在：{bgm}")
    title = (item.get("title") or "").strip()

    # 素材拷进外层草稿目录（内层材料引用绝对路径，保留中文名）
    prepared = _prepare_video_files(draft_dir, clips, used_names)
    audio_dir = os.path.join(draft_dir, "assets", "audios")
    os.makedirs(audio_dir, exist_ok=True)
    bgm_dest = os.path.join(audio_dir, _unique_dest_name(bgm, used_names))
    shutil.copy(bgm, bgm_dest)

    if item.get("beat_sync", True):
        cut_points, shots = _beat_cut(bgm_dest, len(clips),
                                      float(item.get("shot_dur", 2.0)),
                                      float(item.get("offset", 0.0)))
        # 素材比目标段短时按素材原长收窄
        for i, p in enumerate(prepared):
            if p["dur_us"] < shots[i] * 1e6:
                shots[i] = p["dur_us"] / 1e6
    else:
        # 不卡点：素材按原速原长完整播出，成片长度 = 素材总长，
        # BGM 随后被裁到成片长度（选 BGM 时已保证不短于成片长度，
        # 见 generate_remix 里的 min_durs → pick_bgm_dedup）
        shots = [p["dur_us"] / 1e6 for p in prepared]
        cut_points, _t = [0.0], 0.0
        for _s in shots:
            _t += _s
            cut_points.append(round(_t, 3))
    total = sum(shots)

    # 内层草稿：内存中基于模板构建
    tpl_info = os.path.join(config.TEMPLATE_DIR, "default2", "draft_info.json")
    script = draft.ScriptFile.load_template(tpl_info)
    # ⚠ 关键：load_template 会把 save_path 指向模板文件本身，
    # 而 _add_videos_internal 末尾会自动 save() → 不改写就会污染模板！
    script.save_path = os.path.join(tempfile.mkdtemp(), "draft_content.json")
    script.dual_file_compatibility = True
    script.width, script.height = width, height
    script.content["canvas_config"]["width"] = width
    script.content["canvas_config"]["height"] = height
    script.add_track(track_type=draft.TrackType.video, track_name="main_track",
                     relative_index=0)

    fake_id = f"inner{helper.gen_unique_id()}"
    DRAFT_CACHE[fake_id] = script
    fake_url = f"http://localhost/draft?draft_id={fake_id}"

    videos_payload = []
    t = 0
    for i, p in enumerate(prepared):
        dur_us = int(shots[i] * 1e6)
        videos_payload.append({
            "video_url": "", "start": t, "end": t + dur_us,
            "duration": dur_us, "original_start": t, "original_end": t + dur_us,
            "local_video_path": p["dest"], "volume": 1.0,
        })
        t += dur_us
    _add_videos_internal(draft_url=fake_url, video_infos="[]",
                         prepared_videos=videos_payload)
    _add_bgm(script, bgm_dest, t, int(float(item.get("offset", 0)) * 1e6),
             float(item.get("bgm_volume", 1.0)),
             speed=(1.0 + rng.uniform(0.01, 0.06) * bgm_jitter)
             if (bgm_jitter > 0 and rng is not None) else 1.0,
             change_pitch=bool(bgm_jitter > 0))
    if title:
        title_dur_us = int(float(item.get("title_dur", 0) or total) * 1e6)
        _add_title(script, title, 0, min(title_dur_us, t), width, height,
                   font_file=font_file, huazi_file=huazi_file)

    td = os.path.dirname(script.save_path)
    script.save()
    inner = json.load(open(os.path.join(td, "draft_info.json"),
                           encoding="utf-8"))
    shutil.rmtree(td, ignore_errors=True)
    if video_speed != 1.0:
        _apply_video_speed(inner, video_speed)
    if clip_jitter > 0 and rng is not None:
        _randomize_clips(inner, rng, clip_jitter)
    if start_jitter > 0 and rng is not None:
        _randomize_starts(inner, rng, start_jitter)
    inner["id"] = str(uuid.uuid4()).upper()
    inner["path"] = ""
    return inner, total, cut_points, shots


def generate_batch(spec: dict):
    """
    一个草稿 · 多条成片。每条成片（BGM 卡点 + 标题）打包成一个复合片段，
    依次排在主轨上，供剪映「批量导出」逐条出片。

    spec:
      name  : 草稿显示名
      width/height : 画布
      items : [ { videos:[路径...], bgm:路径, title:文本,
                  shot_dur, offset, bgm_volume, title_dur } ]  # 每项一条成片
    """
    name = spec.get("name") or f"批量卡点_{time.strftime('%m%d_%H%M%S')}"
    width = int(spec.get("width", 1080))
    height = int(spec.get("height", 1920))
    speed = float(spec.get("speed", 1.0))
    video_speed = float(spec.get("video_speed", 1.0))
    font_file = spec.get("font_file")
    huazi_file = spec.get("huazi_file")
    rng = spec.get("rng") or random.Random()
    clip_jitter = float(spec.get("clip_jitter", 0.0))   # 画面随机微变换强度 0~1
    bgm_jitter = float(spec.get("bgm_jitter", 0.0))     # BGM 加速/变调强度 0~1
    start_jitter = float(spec.get("start_jitter", 0.0)) # 随机起始点（秒，0=关）
    items = spec.get("items") or []
    if not items:
        raise RuntimeError("没有成片条目")

    used_names = set()  # 本草稿内已占用的素材文件名（跨复合片段去重，保留中文名）

    # 1) 外层草稿骨架（模板 + 画布）
    url = create_draft(width, height)
    draft_id = helper.get_url_param(url, "draft_id")
    draft_dir = os.path.join(config.DRAFT_DIR, draft_id)
    outer_path = os.path.join(draft_dir, "draft_info.json")
    outer = json.load(open(outer_path, encoding="utf-8"))
    # ⚠ 关键：剪映列表显示的名字取自 draft_info.json 的 name 字段，
    # 必须显式写入中文名，否则显示模板默认乱码
    outer["name"] = name
    outer["config"]["combination_max_index"] = len(items) + 1
    for tr in outer.get("tracks", []):
        if tr.get("name") == "main_track":
            tr["flag"] = 2

    ref = _composite_ref()
    mats = outer["materials"]
    vids = mats.setdefault("videos", [])
    combos = mats.setdefault("drafts", [])
    speeds = mats.setdefault("speeds", [])
    placeholders = mats.setdefault("placeholder_infos", [])
    canvases = mats.setdefault("canvases", [])
    scms = mats.setdefault("sound_channel_mappings", [])
    colors = mats.setdefault("material_colors", [])
    vocals = mats.setdefault("vocal_separations", [])
    main_track = next(tr for tr in outer["tracks"] if tr.get("name") == "main_track")

    def nid():
        return str(uuid.uuid4()).upper()

    total_us = 0
    details = []
    for i, item in enumerate(items):
        inner, dur, cut_points, shots = _build_beat_item(item, draft_dir,
                                                         width, height,
                                                         video_speed=video_speed,
                                                         font_file=font_file,
                                                         huazi_file=huazi_file,
                                                         rng=item.get("rng") or rng,
                                                         clip_jitter=clip_jitter,
                                                         bgm_jitter=bgm_jitter,
                                                         used_names=used_names,
                                                         start_jitter=start_jitter)
        dur_us = int(dur * 1e6)
        # 整条成片变速：画面与内部 BGM 同步变速，卡点不脱节
        sped_us = int(round(dur_us / speed))
        cname = f"复合片段{i + 1}"
        inner["name"] = cname
        inner["duration"] = dur_us

        # 虚拟视频素材（代表整个复合片段）
        vm = copy.deepcopy(ref["outer_video_material"])
        vm["id"] = nid()
        vm["duration"] = sped_us
        vm["width"], vm["height"] = width, height
        vm["material_name"] = cname

        # combination 材料：内嵌完整子草稿
        comb = copy.deepcopy(ref["combination_meta"])
        comb["id"] = nid()
        comb["combination_id"] = nid()
        comb["draft"] = inner

        # speed 材料（标准结构：curve_speed/mode/speed/type）
        speed_mat = copy.deepcopy(ref["speed"])
        speed_mat["id"] = nid()
        speed_mat["speed"] = speed
        speed_mat["mode"] = 0
        speed_mat["curve_speed"] = None
        ph = copy.deepcopy(ref["placeholder_info"]); ph["id"] = nid()
        cv = copy.deepcopy(ref["canvas"]); cv["id"] = nid()
        scm = copy.deepcopy(ref["sound_channel_mapping"]); scm["id"] = nid()
        col = copy.deepcopy(ref["material_color"]); col["id"] = nid()
        voc = copy.deepcopy(ref["vocal_separation"]); voc["id"] = nid()

        seg = copy.deepcopy(ref["outer_segment"])
        seg["id"] = nid()
        seg["material_id"] = vm["id"]
        seg["extra_material_refs"] = [comb["id"], speed_mat["id"], ph["id"],
                                      cv["id"], scm["id"], col["id"], voc["id"]]
        # 目标时长按倍速缩短，源时长保持原长
        seg["target_timerange"] = {"start": total_us, "duration": sped_us}
        seg["source_timerange"] = {"duration": dur_us}
        seg["speed"] = speed
        seg["render_index"] = i + 1
        seg["track_render_index"] = i + 1

        vids.append(vm); combos.append(comb); speeds.append(speed_mat)
        placeholders.append(ph); canvases.append(cv); scms.append(scm)
        colors.append(col); vocals.append(voc)
        main_track.setdefault("segments", []).append(seg)

        details.append({
            "name": cname,
            "duration": round(dur, 3),
            "shots": [round(s, 3) for s in shots],
            "cut_points": cut_points,
            "bgm": os.path.basename(item.get("bgm", "")),
            "title": item.get("title", ""),
        })
        total_us += sped_us

    outer["duration"] = total_us
    json.dump(outer, open(outer_path, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    json.dump(outer, open(os.path.join(draft_dir, "draft_content.json"), "w",
                          encoding="utf-8"), ensure_ascii=False, indent=1)

    register_draft(draft_id, name, total_us)
    return {
        "draft_id": draft_id,
        "name": name,
        "duration_us": total_us,
        "folder": draft_dir,
        "count": len(items),
        "items": details,
    }


def register_draft(draft_id: str, name: str, duration_us: int):
    """把生成的草稿追加进 root_meta_info.json（剪映靠它显示草稿列表）。"""
    idx_path = os.path.join(JIANYING_DRAFT_DIR, "root_meta_info.json")
    if not os.path.isfile(idx_path):
        raise RuntimeError(f"找不到剪映索引文件：{idx_path}")
    idx = json.load(open(idx_path, encoding="utf-8"))
    draft_dir = os.path.join(JIANYING_DRAFT_DIR, draft_id)
    # 尽量沿用草稿自身的 new_version，贴近真实草稿结构
    new_version = ""
    info_path = os.path.join(draft_dir, "draft_info.json")
    if os.path.isfile(info_path):
        try:
            new_version = json.load(open(info_path, encoding="utf-8")).get(
                "new_version", ""
            )
        except Exception:
            new_version = ""
    now = time.time_ns() // 1000  # 微秒
    entry = {
        "cloud_draft_cover": False,
        "cloud_draft_sync": False,
        "draft_cloud_last_action_download": False,
        "draft_cloud_purchase_info": "",
        "draft_cloud_template_id": "",
        "draft_cloud_tutorial_info": "",
        "draft_cloud_videocut_purchase_info": "",
        "draft_cover": os.path.join(draft_dir, "draft_cover.jpg"),
        "draft_fold_path": draft_dir,
        "draft_id": str(uuid.uuid4()).upper(),
        "draft_is_ai_shorts": False,
        "draft_is_cloud_temp_draft": False,
        "draft_is_infinite_canvas_draft": False,
        "draft_is_invisible": False,
        "draft_is_pippit_draft": False,
        "draft_is_web_article_video": False,
        "draft_json_file": os.path.join(draft_dir, "draft_info.json"),
        "draft_name": name,
        "draft_new_version": new_version,
        "draft_root_path": JIANYING_DRAFT_DIR,
        "draft_timeline_materials_size": 0,
        "draft_type": "",
        "draft_web_article_video_enter_from": "",
        "pippit_avatar_url": "",
        "pippit_extra_info": "",
        "pippit_id": "",
        "pippit_user_name": "",
        "streaming_edit_draft_ready": True,
        "tm_draft_cloud_completed": "",
        "tm_draft_cloud_entry_id": -1,
        "tm_draft_cloud_modified": 0,
        "tm_draft_cloud_parent_entry_id": -1,
        "tm_draft_cloud_space_id": -1,
        "tm_draft_cloud_user_id": -1,
        "tm_draft_create": now,
        "tm_draft_modified": now,
        "tm_draft_removed": 0,
        "tm_duration": duration_us,
    }
    idx.setdefault("all_draft_store", []).append(entry)
    json.dump(idx, open(idx_path, "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)


def list_drafts():
    """读取剪映草稿列表（仅返回本地、未删除的）。"""
    idx_path = os.path.join(JIANYING_DRAFT_DIR, "root_meta_info.json")
    if not os.path.isfile(idx_path):
        return []
    idx = json.load(open(idx_path, encoding="utf-8"))
    out = []
    for e in idx.get("all_draft_store", []):
        if e.get("tm_draft_removed", 0):
            continue
        out.append({
            "name": e.get("draft_name"),
            "draft_id": e.get("draft_id"),
            "duration_us": e.get("tm_duration", 0),
            "modified": e.get("tm_draft_modified", 0),
            "folder": e.get("draft_fold_path"),
        })
    return out


def list_orphans():
    """文件夹存在但索引里没条目的草稿（被剪映后台剔除/漏注册的）。"""
    idx_path = os.path.join(JIANYING_DRAFT_DIR, "root_meta_info.json")
    known = set()
    if os.path.isfile(idx_path):
        idx = json.load(open(idx_path, encoding="utf-8"))
        known = {
            e.get("draft_fold_path")
            for e in idx.get("all_draft_store", [])
            if e.get("draft_fold_path")
        }
    orphans = []
    if not os.path.isdir(JIANYING_DRAFT_DIR):
        return orphans
    for name in sorted(os.listdir(JIANYING_DRAFT_DIR)):
        folder = os.path.join(JIANYING_DRAFT_DIR, name)
        if not os.path.isdir(folder) or folder in known:
            continue
        if not os.path.isfile(os.path.join(folder, "draft_info.json")):
            continue
        if name.startswith(".") or name.startswith("root_meta"):
            continue
        dur_us = 0
        try:
            info = json.load(open(os.path.join(folder, "draft_info.json"),
                                  encoding="utf-8"))
            dur_us = int(info.get("duration", 0) or 0)
        except Exception:
            pass
        orphans.append({
            "folder": folder,
            "folder_name": name,
            "duration_us": dur_us,
            "mtime": int(os.path.getmtime(folder) * 1000),
        })
    return orphans


def register_by_folder(folder: str, name: str = ""):
    """把一个草稿文件夹补注册进索引（恢复被剔除的草稿）。"""
    folder = os.path.abspath(folder)
    if not os.path.isfile(os.path.join(folder, "draft_info.json")):
        raise RuntimeError(f"不是有效草稿文件夹：{folder}")
    dur_us = 0
    try:
        info = json.load(open(os.path.join(folder, "draft_info.json"),
                              encoding="utf-8"))
        dur_us = int(info.get("duration", 0) or 0)
    except Exception:
        pass
    fname = os.path.basename(folder)
    register_draft(fname, name or fname, dur_us)
    return {"folder": folder, "name": name or fname, "duration_us": dur_us}


# ---------------------------------------------------------------------------
# 批量混剪 · 三层去重 + 跨批次 history 避重
#
# 规则来源：infoflow-batch-remix（信息流视频批量混剪）。避免"批量生产视频"时
# 不同成片之间画面/BGM/文案重复：
#   1) 坑位去重：任意两条成片逐坑位比较，至少 min_diff 个位置片段不同
#      （换片段/换顺序都算不同）
#   2) BGM 去重：不同成片不同 BGM；不足时循环复用 + offset 每次 +3s 换卡点
#   3) 文案去重：titles.txt 轮换
#   4) 跨批次 history：每次产出写 produced_history.jsonl，下次（追加生成）自动
#      与所有历史组合按文件名比较，至少差 min_diff 个坑位，绝不复用历史组合。
# ---------------------------------------------------------------------------

DEFAULT_HISTORY_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "produced_history.jsonl"
)


def load_history(path: str) -> list:
    """读历史记录 JSONL，每行 {combo:[文件名,...], bgm, title, name}。"""
    if not os.path.isfile(path):
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            try:
                rec = json.loads(ln)
                if isinstance(rec.get("combo"), list) and rec["combo"]:
                    out.append(rec)
            except Exception:
                continue
    return out


def save_history(path: str, records: list):
    with open(path, "a", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _combo_diff(a: list, b: list, order_insensitive: bool = False) -> int:
    """两个组合按坑位逐位比较，不同的坑位数（按文件名跨文件夹比较）。

    order_insensitive=True 时先各自按文件名排序再比。用于「坑位顺序会被随机打乱」
    的模式（牛鞭不卡点），否则 (A,B) 与 (B,A) 会被误判成两个不同组合，去重就失效了。
    """
    if order_insensitive:
        a = sorted(os.path.basename(x) for x in a)
        b = sorted(os.path.basename(x) for x in b)
        return sum(1 for x, y in zip(a, b) if x != y)
    return sum(1 for x, y in zip(a, b)
               if os.path.basename(x) != os.path.basename(y))


def _history_diff(combo: list, hist_combo: list,
                  order_insensitive: bool = False) -> int:
    return _combo_diff(combo, hist_combo, order_insensitive)


# ---------------------------------------------------------------------------
# 素材「类别」= 一个意思的划分（用户定义剪辑规则的基础）
# ---------------------------------------------------------------------------

_TAIL_NUM = re.compile(r"[-_ ]?\d+$")


def category_of(path: str, pool_roots=None) -> str:
    """素材的「意思类别」。

    优先取素材所在文件夹名（0911杭州鱼杂/夹鱼杂/夹鱼杂-1.mp4 → 夹鱼杂）；
    若文件直接躺在某个素材根目录里（没有子文件夹），退回用文件名去掉结尾
    数字的前缀（0917/四合一模板.mp4 → 四合一模板）。
    """
    p = os.path.abspath(os.path.expanduser(path))
    folder = os.path.dirname(p)
    if pool_roots:
        roots = {os.path.abspath(os.path.expanduser(r)) for r in pool_roots}
        if folder in roots:
            base = os.path.splitext(os.path.basename(p))[0]
            return _TAIL_NUM.sub("", base) or base
    return os.path.basename(folder)


def _project_key(path: str, pool_roots=None) -> str:
    """素材属于哪个「项目文件夹」（pool_roots 里最长前缀命中的那个），返回其文件夹名。

    只有一个项目时恒等于该文件夹名；多个项目时用来给重名的子文件夹加前缀。
    """
    p = os.path.abspath(os.path.expanduser(path))
    best = None
    for r in (pool_roots or []):
        ra = os.path.abspath(os.path.expanduser(r)).rstrip(os.sep)
        if (p == ra or p.startswith(ra + os.sep)) and (best is None or len(ra) > len(best)):
            best = ra
    return os.path.basename(best) if best else ""


# 配方里的特殊标签（不是真实文件夹名）
#   「开头段 / 收尾段」不用标签表示 —— 就是配方的第一段 / 最后一段，
#   哪一段都能放单个类别或多个类别（list 槽 = 从这几个夹里挑）。
SLOT_ALL = "*"          # 全部随机：整个素材池里随便挑
SLOT_SPECIALS = (SLOT_ALL,)


def group_by_category(pool: list, pool_roots=None, qualify: bool = True) -> dict:
    """把素材池按「类别 → [文件...]」归类（类内按文件名排序，保证可复现）。

    qualify=True 时**跨项目重名的子文件夹会自动加项目名前缀**：
    两个项目里都有「夹菜」→ 「跃杨鱼杂/夹菜」「杭州鱼杂/夹菜」，互不混淆；
    **不重名的仍用裸名**（「夹菜」），所以现有配方不用改。
    """
    raw, projs = {}, {}
    for p in pool:
        n = category_of(p, pool_roots)
        raw.setdefault(n, []).append(p)
        projs.setdefault(n, set()).add(_project_key(p, pool_roots))
    out = {}
    for n, files in raw.items():
        names = {x for x in projs[n] if x}
        if qualify and len(names) > 1:
            for f in files:
                out.setdefault(f"{_project_key(f, pool_roots)}/{n}", []).append(f)
        else:
            out[n] = files
    for k in out:
        out[k] = sorted(out[k])
    return out


def resolve_shot_recipe(recipe, pool, pool_roots=None):
    """把「镜头配方」解析成 slot_groups（第 i 个坑位只从第 i 组里挑）。

    recipe: list，每项 = 一个类别名，或类别名列表（该坑位可从这几个类里挑）。
            类别名 = 素材文件夹名（见 category_of）。
    例：["勺 鱼杂提上来", "夹鱼杂", ["勺到碗里", "勺碗里"],
         "鱼杂夹+辣油", "鱼杂上桌展示", ["浇汤", "浇汤-2"]]

    返回 (slot_groups, recipe_cats)。recipe_cats[i] 是第 i 段用到的类别元组，
    用于打印与「片内是否重复类别」校验。
    """
    by_cat = group_by_category(pool, pool_roots)

    def _resolve(n):
        """名字 → 文件列表。依次尝试：精确键 → 全部随机 → 裸名（重名取并集）。"""
        if n in by_cat:
            return by_cat[n]
        if n == SLOT_ALL:                       # 全部随机
            return sorted(pool)
        hits = [k for k in by_cat if k.endswith("/" + n)]   # 裸名兜底（兼容旧配方）
        if hits:
            return sorted({f for k in hits for f in by_cat[k]})
        return []

    slot_groups, recipe_cats = [], []
    for i, slot in enumerate(recipe, 1):
        names = list(slot) if isinstance(slot, (list, tuple)) else [slot]
        files, hit = [], []
        for n in names:
            grp = _resolve(n)
            if not grp:
                raise GenError(
                    f"[配方错误] 第 {i} 段类别「{n}」在素材池里找不到。\n"
                    f"          可用类别：{sorted(by_cat)}"
                )
            files += grp
            hit.append(n)
        recipe_cats.append(tuple(hit))
        slot_groups.append(files)

    # 「全部随机」= 全库 **减去本配方其它段已经点名的类别**。
    # 否则配方写成 [夹菜, *, 浇汤] 时，* 可能抽到另一条「夹菜」→ 片内出现两段同意思。
    # 只按类名排（不看文件名）：同名文件另有 used_in_combo 兜底。
    used_cats = set()
    for cats in recipe_cats:
        for n in cats:
            if n != SLOT_ALL:
                used_cats.add(n.rsplit("/", 1)[-1])
    if used_cats:
        for i, cats in enumerate(recipe_cats):
            if SLOT_ALL in cats:
                keep = [f for f in slot_groups[i]
                        if category_of(f, pool_roots).rsplit("/", 1)[-1] not in used_cats]
                if keep:                 # 排除后为空 → 退回全库，别把这一段弄成空组
                    slot_groups[i] = keep

    # 校验：同一个类别出现在 >=2 个坑位 → 片内会出现两段同意思画面。
    # 这是「允许片内重复」的正规用法（例如多放两段夹鱼籽鱼泡），只提示不报错；
    # 取文件时按「用得最少优先」轮转，两段会拿到不同文件，不会一模一样。
    # 比较时只看裸名：「跃杨鱼杂/夹菜」和「杭州鱼杂/夹菜」也算同一类（画面同意思）
    flat = [c.rsplit("/", 1)[-1] for cats in recipe_cats for c in cats]
    dup = sorted({c for c in flat if flat.count(c) > 1})
    if dup:
        print(f"[配方提示] 类别 {dup} 会在同一条片里出现 ≥2 段"
              f"（=「允许片内重复」，两段会取不同文件）。")
    return slot_groups, recipe_cats


def normalize_slots(raw):
    """「每条段数」归一化：空 / 0 / "auto" / "自动" → None（= 按配方长度算）。

    返回 >=1 的整数，或 None。
    """
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, str):
        s = raw.strip().lower()
        if s in ("", "auto", "自动"):
            return None
        try:
            raw = float(s)
        except ValueError:
            return None
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return None
    return n if n >= 1 else None


def fit_recipe_to_slots(recipe, slots: int, avail_cats: list):
    """把配方裁剪/补位到正好 slots 个坑位（「配方当骨架，段数说了算」）。

    - slots < 配方长度 → 截掉末尾多余的坑位。
    - slots > 配方长度 → 保留原配方（拍摄顺序/收尾动作不变），在**末尾**继续补坑位：
      每次从「本配方里用得最少的类别」里挑，且**不与前一段同类**
      （同类相邻是用户最忌讳的观感事故；宁可复用也不让相邻同意思）。

    补位是**确定性的**（同样的配方 + 同样的类别池 → 每次都一样），
    这样同一条配方的所有成片结构一致，不会每条一个样。

    recipe:     list，每项 = 类别名，或类别名列表（该坑位可从这几类里挑）。
    avail_cats: 本店素材池里实际存在的类别名（str 列表）。
    返回新的 recipe list（不改原对象）。
    """
    recipe = [list(s) if isinstance(s, (list, tuple)) else [s] for s in recipe]
    if slots <= len(recipe):
        return recipe[:slots]

    # 统计配方里各已用次数（一个坑位含多个类别时都算一次）
    used = {}
    for slot in recipe:
        for c in slot:
            used[c] = used.get(c, 0) + 1

    def cats_of(slot):
        return set(slot)

    out = list(recipe)
    while len(out) < slots:
        prev = cats_of(out[-1])                       # 前一段的类别（不能重样）
        # 候选 = 池子里所有类别，去掉与前一段同类的；再按「用得最少优先」挑
        # （没用过的计数为 0，天然优先）。只有当池子里只剩这一类时才被迫相邻复用。
        cands = [c for c in avail_cats if c not in prev]
        if not cands:
            cands = list(avail_cats)                   # 整池只有 1 个类别，只能相邻复用了
        if not cands:
            raise GenError("[段数错误] 素材池里没有任何类别，无法自动补坑位。")
        least = min(used.get(c, 0) for c in cands)
        pick = sorted(c for c in cands if used.get(c, 0) == least)[0]
        out.append([pick])
        used[pick] = used.get(pick, 0) + 1
    return out


def plan_combos(pool: list, slots: int, count: int, min_diff: int,
                rng: random.Random, history: list = None,
                slot_groups: list = None, shuffle_slots: bool = False,
                pool_roots: list = None) -> list:
    """每条从 pool 随机取 slots 个素材；与本批已有组合及历史已产出组合
    （文件名比较）都至少差 min_diff 个坑位。

    slot_groups:    可选。给定时第 i 个坑位只从第 i 组里挑（组内随机），
                    用于「必须每段各来自某个文件夹」这类固定来源规则。
    shuffle_slots:  可选。生成的组合会随机打乱坑位顺序（打散成片结构、降低平台
                    画面指纹相似度）。此时去重改用**顺序无关**比较，避免把
                    (A,B) 和 (B,A) 当成两个组合而虚增产量。
    """
    hist = [h["combo"] for h in (history or [])]
    if hist:
        print(f"[去重] 已载入历史 {len(hist)} 条，将避开已产出组合")
    if slot_groups:
        empty = [i + 1 for i, grp in enumerate(slot_groups) if not grp]
        if empty:
            raise GenError(
                f"slot_groups 第 {empty} 组是空的，检查对应的素材路径。"
            )
    elif len(pool) < slots:
        raise GenError(
            f"素材池只有 {len(pool)} 条，不足以铺 {slots} 个坑位。"
            f"请把「每条段数」改小，或补充素材。"
        )
    order_insensitive = bool(shuffle_slots)
    combos, seen = [], []
    # 配方模式：记录各文件已用次数，优先挑「用得最少」的 →
    # 小类别（如「浇汤」只有 2 条）会均匀轮转，相邻成片不会撞同一个文件。
    # 用一份全局计数（不按坑位分开），同一文件出现在多个坑位时也能合并统计。
    file_usage = {}
    cat_of_file = {f: category_of(f, pool_roots) for f in pool}
    max_attempts = count * 300 + 3000
    for _ in range(max_attempts):
        if len(combos) >= count:
            break
        if slot_groups:
            combo = []
            for grp in slot_groups:
                least = min(file_usage.get(f, 0) for f in grp)
                combo.append(rng.choice([f for f in grp
                                         if file_usage.get(f, 0) == least]))
        else:
            # 无配方（简单模式）：逐段挑，优先「本条还没用过的类别」→ 片内不重复；
            # 类别不够时才退到「不与上一段同类」→ 至少不相邻重复；
            # 全程不让同一个文件出现两次。
            # 直接 rng.sample 会抽出 夹鱼籽鱼泡-8 紧跟 夹鱼籽鱼泡-2 这种相邻同意思。
            combo, used_files, used_cats, prev_cat = [], set(), set(), None
            for _k in range(slots):
                cands = [f for f in pool if f not in used_files
                         and cat_of_file[f] not in used_cats]
                if not cands:
                    cands = [f for f in pool if f not in used_files
                             and cat_of_file[f] != prev_cat]
                if not cands:                       # 池子太小，只能放宽
                    cands = [f for f in pool if f not in used_files] or list(pool)
                least = min(file_usage.get(f, 0) for f in cands)
                pick = rng.choice([f for f in cands
                                   if file_usage.get(f, 0) == least])
                combo.append(pick)
                used_files.add(pick)
                used_cats.add(cat_of_file[pick])
                prev_cat = cat_of_file[pick]
        # 接缝约束：这一条的开头不要和上一条的结尾同类
        seam_ok = (not combos) or cat_of_file[combo[0]] != cat_of_file[combos[-1][-1]]
        if seam_ok \
                and all(_combo_diff(combo, prev, order_insensitive) >= min_diff
                        for prev in seen) \
                and all(_history_diff(combo, h, order_insensitive) >= min_diff
                        for h in hist):
            for f in combo:
                file_usage[f] = file_usage.get(f, 0) + 1
            seen.append(list(combo))
            if shuffle_slots:
                rng.shuffle(combo)
            combos.append(combo)
    if len(combos) < count:
        print(f"[警告] 去重约束下只能生成 {len(combos)} 条（目标 {count}）。"
              f"可调小 min_diff 或增加素材变体。")
    return combos


# 「店面选用规则」这类字段，spec.json 里怎么配就怎么原样进 generate_remix。
# ⚠ 坑：gen_project.py 与 console.py 都是用**字段白名单**组装入参的，
#   新增这类字段时如果只加在 generator 里，出片时会静默地「用全部」——
#   必须让两处的 _build_full 都 update 一遍本表。新增选用类字段请加到这里。
SPEC_PASSTHROUGH_KEYS = ("title_filter", "bgm_filter")


def normalize_bgm_filter(flt):
    """规整店面的 BGM 选用规则 → {"only":[...], "exclude":[...]}。

    与 title_filter 同构，但 BGM 的身份是**文件绝对路径**（不是文件名，
    避免不同文件夹里同名歌互相顶掉）。字段缺失/为 None 一律当「用全部」。
    """
    flt = flt or {}
    if not isinstance(flt, dict):
        flt = {}
    out = {}
    for k in ("only", "exclude"):
        v = flt.get(k) or []
        if isinstance(v, str):
            v = [v]
        out[k] = [os.path.abspath(os.path.expanduser(str(x)))
                  for x in v if str(x).strip()]
    return out


def is_default_bgm_filter(flt) -> bool:
    """是否等于「用全部」（没有 only 也没有 exclude）。"""
    f = normalize_bgm_filter(flt)
    return not f["only"] and not f["exclude"]


def apply_bgm_filter(pool: list, flt) -> list:
    """按店面选用规则过滤 BGM 池 → [绝对路径]，保序 + 去重。

    规则叠加顺序：1) only 非空 → 先只留白名单；2) 去掉 exclude 里的。
    only 里的条目若已不是现存文件，额外按**文件名**兜底匹配一次，
    这样用户把文件夹整体挪走/改名后，勾选不会全部失效。
    """
    f = normalize_bgm_filter(flt)
    only = set(f["only"])
    ex = set(f["exclude"])
    pool = [os.path.abspath(p) for p in pool]

    if only:
        # 路径精确匹配不到 → 用文件名再兜一次
        by_name = {}
        for p in pool:
            by_name.setdefault(os.path.basename(p), []).append(p)
        keep = set(p for p in pool if p in only)
        for o in f["only"]:
            if o in keep or os.path.isfile(o):
                continue
            for p in by_name.get(os.path.basename(o), []):
                keep.add(p)
        pool = [p for p in pool if p in keep]

    out, seen = [], set()
    for p in pool:
        if p in ex or p in seen:
            continue
        seen.add(p)
        out.append(p)
    return out


def pick_bgm_dedup(bgm_pool: list, count: int, rng: random.Random,
                   min_durs: list = None):
    """不同成片不同 BGM；不足时循环复用，offset 每次 +3s 换卡点。

    min_durs: 可选，长度需 = count，为每条成片要求的「最小 BGM 时长（秒）」。
              给定时改为「优先挑没用过且够长的 BGM，offset 一律 0」，
              供「素材完整播出、BGM 必须铺满成片」的不卡点模式使用。
              实在挑不到就用最长的兜底，不报错。
    """
    if not bgm_pool:
        raise RuntimeError("BGM 目录没有音频文件")
    files = list(bgm_pool)
    rng.shuffle(files)
    if not min_durs:
        return [(files[i % len(files)], (i // len(files)) * 3.0)
                for i in range(count)]
    durs = {f: draft.AudioMaterial(f).duration / 1e6 for f in files}
    used, out = {}, []
    for need in min_durs:
        ok = [f for f in files if durs[f] >= need]
        if not ok:                  # 全部都不够长 → 用最长的那首兜底
            ok = [max(files, key=lambda f: durs[f])]
        fresh = [f for f in ok if used.get(f, 0) == 0]
        if fresh:
            pick = rng.choice(fresh)
        else:
            # 够长的都用过了：挑「本批用得最少」的轮转。
            # （旧实现这里用 next() 取第一首，导致 280 条里有一首被用 218 次）
            least = min(used.get(f, 0) for f in ok)
            pick = rng.choice([f for f in ok if used.get(f, 0) == least])
        used[pick] = used.get(pick, 0) + 1
        out.append((pick, 0.0))
    return out


def pick_titles_rotating(titles: list, count: int, offset: int = 0) -> list:
    if not titles:
        return ["" for _ in range(count)]
    return [titles[(i + offset) % len(titles)] for i in range(count)]


def pick_titles_least_used(titles: list, count: int, history: list = None,
                           offset: int = 0) -> list:
    """按「历史用量最少」挑文案：优先用还没怎么用过的，且本批内绝不重复。

    标题库轮完一圈后，按下标轮转会让开头几条被反复用（旧实现就是这样，
    172 条里前 10 条用了 3 次）。改成按历史次数排序后，用量会自动摊平。
    offset: 从排序结果里跳过前 offset 条（手动换一批用）。
    """
    if not titles:
        return ["" for _ in range(count)]
    cnt = {}
    for h in (history or []):
        t = h.get("title")
        if t:
            cnt[t] = cnt.get(t, 0) + 1
    order = sorted(range(len(titles)), key=lambda i: (cnt.get(titles[i], 0), i))
    seq = order[offset:] + order[:offset]
    if count <= len(seq):                       # 常规：本批内不重复
        return [titles[i] for i in seq[:count]]
    return [titles[seq[i % len(seq)]] for i in range(count)]


def plan_combos_recipe(recipe_groups: list, count: int, min_diff: int,
                       rng: random.Random, history: list = None) -> list:
    """按「镜头配方」选片：第 k 条成片固定用第 k % R 个配方（R = 配方数）。

    每个配方的每个坑位独立做「用得最少优先」轮转 → 小类别（浇汤只有 2 条）
    会均匀铺开，相邻成片不会撞同一个文件。

    为什么支持多个配方：8 个类别里每条取 6 类，两条配方各取 6 类时交集最少
    4 类。用 2 个配方轮换 + 后续重排，就能把「相邻两条结构完全相同」变成
    「相邻两条只共用 4 类」，这是这类素材能做到的最优。
    """
    R = len(recipe_groups)
    # 全批共享一份「文件已用次数」：同一类别在两套配方里都出现时，用量合并统计，
    # 否则两套配方各挑一轮会撞到同一个文件（如 夹鱼杂-25 与 夹鱼杂-25_2）。
    file_usage = {}
    hist = history or []
    combos, seen = [], []
    max_attempts = count * 300 + 3000
    k = 0
    for _ in range(max_attempts):
        if len(combos) >= count:
            break
        v = k % R
        grps = recipe_groups[v]
        combo = []
        # 片内已选文件：同一类别在一配方里出现 ≥2 段时（如「夹鱼籽鱼泡」出现两次）
        # 必须换文件 —— 否则两个坑位都按「全批用量最少」挑，会 rng.choice 到同一个
        # 文件，出现 夹鱼籽鱼泡-32 与 夹鱼籽鱼泡-32 同片复用（用户只允许同类不同文件）。
        used_in_combo = set()
        for grp in grps:
            cands = [f for f in grp if f not in used_in_combo]
            if not cands:               # 类内文件数 < 该类别在本条出现的段数 → 只能复用
                cands = list(grp)
            least = min(file_usage.get(f, 0) for f in cands)
            pick = rng.choice([f for f in cands
                               if file_usage.get(f, 0) == least])
            combo.append(pick)
            used_in_combo.add(pick)
        if all(_combo_diff(combo, p) >= min_diff for p in seen) \
                and all(_combo_diff(combo, h["combo"]) >= min_diff for h in hist):
            for f in combo:
                file_usage[f] = file_usage.get(f, 0) + 1
            seen.append(list(combo))
            combos.append(combo)
            k += 1
    if len(combos) < count:
        print(f"[警告] 配方+去重约束下只生成 {len(combos)} 条（目标 {count}）。")
    return combos


def _report_sameness(combos: list, pool_roots=None, exclusive=None):
    """打印本批成片的「同意思」体检报告，方便用户验收规则是否生效。

    exclusive: 排他类别（相邻两条不能都有），有则多打一项指标。

    指标（目标都是 0）：
      · 片内相邻同类   —— 同一条视频里前后两个镜头同意思（用户最在意的那个）
      · 片内同文件复用 —— 同一条视频里同一个文件出现 ≥2 次
                          （类别允许重复，但两段必须取不同文件）
      · 片内重复类别的成片 —— 一条视频里同一意思出现 ≥2 次（用户允许时可放宽）
      · 相邻边界同类   —— 上一条的最后一段 与 下一条的第一段 同意思
      · 片间同文件重叠 —— 相邻两条用了同一个文件
      · 排他类别相邻   —— 相邻两条都含了「排他类别」（如 端锅-上桌）
      · 相邻同位置同类 —— 相邻两条在同一镜位上类别相同的平均个数
                          （固定配方模式下不可能为 0，多套配方轮换可压低）
    """
    n = len(combos)
    if not n:
        return
    seqs = [[category_of(x, pool_roots) for x in c] for c in combos]
    fset = [set(c) for c in combos]
    adj_in = sum(1 for s in seqs for k in range(len(s) - 1) if s[k] == s[k + 1])
    dup_in = sum(1 for s in seqs if len(set(s)) < len(s))
    edge = sum(1 for i in range(n) if seqs[i][-1] == seqs[(i + 1) % n][0])
    fov = sum(len(fset[i] & fset[(i + 1) % n]) for i in range(n))
    dup_file_in = sum(1 for c in combos if len(set(c)) < len(c))
    pos = sum(sum(1 for a, b in zip(seqs[i], seqs[(i + 1) % n]) if a == b)
              for i in range(n)) / n
    avg_cat = sum(len(set(s)) for s in seqs) / n
    excl_txt = ""
    ex = 0
    if exclusive:
        e = set(exclusive)
        ex = sum(1 for i in range(n)
                 if (e & fset[i]) and (e & fset[(i + 1) % n]))
        excl_txt = f"排他类别({'/'.join(exclusive)})相邻 {ex} 处(目标0) | "
    print(f"[体检] 片内相邻同类 {adj_in} 处(目标0) | "
          f"片内同文件复用 {dup_file_in}/{n} 条(目标0) | "
          f"片内重复类别的成片 {dup_in}/{n} 条 | "
          f"相邻边界同类 {edge} 处(目标0) | "
          f"片间同文件重叠 {fov} 处(目标0) | "
          f"{excl_txt}"
          f"相邻同位置同类 {pos:.2f} 个 | "
          f"片内平均不同类别 {avg_cat:.2f}/条")
    return {
        "n": n, "adj_in": adj_in, "dup_file_in": dup_file_in, "dup_in": dup_in,
        "edge": edge, "fov": fov, "excl": ex,
        "exclusive": list(exclusive or []), "pos": round(pos, 2),
        "avg_cat": round(avg_cat, 2),
    }


def separate_items(combos: list, bgms: list, title_list: list,
                   pool_roots=None, exclusive=None):
    """重排成片顺序，让相邻两条尽量「不同意思」（类别优先，其次具体文件）。

    exclusive: 可选。**排他类别**名列表（如 ["端锅-上桌"]）—— 相邻两条不能
              都含这个类别。用于「某类镜头每条都出现太重复，要求隔条出现」。

    代价函数（越小越好，相邻两条逐对计算）：
        类别集合重叠数 × 10  +  同镜位同类数 × 6  +  同文件重叠数 × 100
        + 相邻两条都含排他类别 → 1000（硬性规避）
    - 类别集合重叠：两条成片就算素材文件全不同，只要「意思」撞了也看着重复。
    - 同镜位同类：第 k 段都是同一类别（例如两条都是「勺到碗里」打头）——
      多套配方若**类别集合相同、只是顺序不同**，集合重叠项就区分不出来了，
      必须靠这一项把「连续几条同配方」拆开，否则相邻两条镜头结构会一样。
    - 同文件重叠：权重最高，保证相邻两条不出现同一个素材文件。
    - 排他类别：权重最高（1000），保证该镜头不会连着两条都出现。

    做法：贪心多起点建初始排列 → 2-opt 局部搜索（两两交换位置，能降分就换）
    → 取相邻代价最小的排列（含首尾回环，因为列表是循环播放的）。
    """
    n = len(combos)
    if n <= 2:
        return combos, bgms, title_list

    excl = set(exclusive or [])
    file_sets = [set(c) for c in combos]
    seqs = [[category_of(x, pool_roots) for x in c] for c in combos]
    cat_sets = [set(s) for s in seqs]

    def pair_cost(i, j):
        same_pos = sum(1 for a, b in zip(seqs[i], seqs[j]) if a == b)
        cost = (len(cat_sets[i] & cat_sets[j]) * 10
                + same_pos * 6
                + len(file_sets[i] & file_sets[j]) * 100)
        if excl and (excl & cat_sets[i]) and (excl & cat_sets[j]):
            cost += 1000            # 相邻两条都含排他类别 → 重罚
        return cost

    def total(order):
        return sum(pair_cost(order[k], order[(k + 1) % n]) for k in range(n))

    # 1) 贪心多起点：从不同起点各自贪心，取最好的
    best = None
    for start in range(min(n, 6)):
        remaining = [i for i in range(n) if i != start]
        order = [start]
        while remaining:
            last = order[-1]
            remaining.sort(key=lambda j: pair_cost(last, j))
            order.append(remaining.pop(0))
        score = total(order)
        if best is None or score < best[0]:
            best = (score, order)
    order = best[1]

    # 2) 2-opt 局部搜索：交换任意两条能降分就换（n 大时限制轮数，避免拖慢）
    max_pass = 8 if n > 60 else 20
    for _ in range(max_pass):
        improved = False
        for i in range(n - 1):
            for j in range(i + 1, n):
                old = total(order)
                order[i], order[j] = order[j], order[i]
                if total(order) < old:
                    improved = True
                else:
                    order[i], order[j] = order[j], order[i]   # 还原
        if not improved:
            break

    return ([combos[i] for i in order], [bgms[i] for i in order],
            [title_list[i] for i in order])


def generate_remix(spec: dict):
    """批量混剪 + 三层去重 + 跨批次 history 避重。

    spec:
      name          : 草稿显示名
      width/height  : 画布（默认 1080x1920）
      pool          : 素材绝对路径列表（直接给，优先）
      folder        : 或给素材根目录（自动 walk 扫描视频）
      bgm_dir       : BGM 文件夹（单个，兼容旧配置）
      bgm_dirs      : BGM 文件夹列表（可多个，递归扫描子目录，合并成一个曲库；
                      优先生效；优先级高于 bgm_dir）
      titles        : 标题文件路径（每行一条）
      count         : 成片条数（默认 10）
      slots         : 每条片段数（默认 6）
      shot_dur      : 每段时长秒（默认 2.0）
      min_diff      : 坑位去重强度：两条成片最少不同的坑位数（默认 2）
      slot_groups   : 可选。list[目录]；给定时第 i 个坑位只从第 i 个目录里挑
                      （例如「人多 1 条 + 出锅 1 条」），slots 被覆盖为其长度
      pool_roots    : 可选。素材根目录列表。文件直接躺在根目录时，类别退回按
                      「文件名去掉结尾数字」的前缀划分（子文件夹则用文件夹名）
      shot_recipe   : 可选。【用户自定义剪辑规则】镜头配方。list，第 i 项 = 第 i
                      段的类别名（或类别名列表=该段可从多类里挑）。类别名 = 素材
                      文件夹名。给定时一条视频内每段来自不同类别 → 片内绝不会
                      「同意思相邻」。例：
                        ["勺 鱼杂提上来","夹鱼杂",["勺到碗里","勺碗里"],
                         "鱼杂夹+辣油","鱼杂上桌展示",["浇汤","浇汤-2"]]
      shot_recipes  : 可选。多个配方，按成片顺序轮换（第 k 条用第 k % R 个）。
                      8 类里各取 6 类时相邻两条类别交集最少为 4，轮换 + 重排可把
                      「相邻同位置同类」从 6 压到 1。优先级高于 shot_recipe
      beat_sync     : 是否按 BGM 重音卡点（默认 True）。False = 不卡点，
                      素材按原速原长完整播出、不裁不卡点，成片长度 = 素材总长，
                      并自动为每条配一首不短于成片的 BGM（播放时裁到成片长度）
      shuffle_slots : 坑位顺序随机打乱（默认 False）。开启后去重改用顺序无关比较，
                      避免 (A,B)/(B,A) 被当成两个组合而虚增产量
      clip_jitter   : 0~1，画面随机微变换的强度（默认 0 = 关闭）。含随机镜像、
                      2%~6% 放大+位移、±0.6° 微旋转、掐头 0~0.25s、放慢到 96%~100%。
                      ⚠ 前四项改变成片长度（放慢那项不改变），后两项会让每段丢掉
                      结尾 ≤4% 的画面 —— 用于打散平台画面查重指纹
      bgm_jitter    : 0~1，BGM 随机加速 + 变调强度（默认 0 = 关闭）
      start_jitter  : 随机起始点（秒，默认 0 = 关闭）。>0 时每段视频的起始点
                      在 [0, min(start_jitter, 素材长-段长×倍速)] 内随机后移。
                      同一素材在多条成片复用时，每条取到不同的画面片段，打散
                      查重指纹。⚠ 会跳过素材开头 start_jitter 秒的画面；
                      素材比段长还短时该段不随机（无余量）。
      exclusive_adjacent : 可选。**排他类别**名列表（如 ["端锅-上桌"]）。
                      相邻两条成片不能都含这个类别 —— 用于「某类镜头每条都出现
                      太重复，要求隔条出现」。重排时按硬约束规避（权重 1000），
                      体检里单列一项「排他类别相邻」验收。
      history_path  : produced_history.jsonl（默认脚本同目录）
      ignore_history: 忽略历史（允许重复产出）
      seed          : 固定种子可复现同一批组合
      speed         : 整条成片播放速度（默认 1.0；>1 更快，BGM 与画面同步变速）
      video_speed   : 仅画面变速、BGM 不变（默认 1.0；>1 画面更快，时间轴槽位不变故 BGM 节奏不被拖快）
      dry_run       : 只打印方案不生成

    返回: generate_batch 的结果；dry_run 时返回 {"dry_run": True, "combos":[...]}
    """
    name = spec.get("name") or f"批量混剪_{time.strftime('%m%d_%H%M%S')}"
    width = int(spec.get("width", 1080))
    height = int(spec.get("height", 1920))

    # 1) 素材池
    pool = spec.get("pool")
    if not pool:
        folder = spec.get("folder")
        if folder:
            folder = os.path.expanduser(folder)
        if folder and os.path.isdir(folder):
            pool = [os.path.abspath(os.path.join(r, f))
                    for r, _, fs in os.walk(folder)
                    for f in sorted(fs)
                    if f.lower().endswith(VIDEO_EXTS)]
    pool = [os.path.abspath(os.path.expanduser(p)) for p in pool
            if os.path.isfile(os.path.expanduser(p))]
    if not pool:
        raise RuntimeError("没有可用视频素材")

    # 1b) 可选：指定每个坑位的来源目录（第 i 个坑位只从第 i 组挑）
    slot_groups = spec.get("slot_groups")
    if slot_groups:
        slot_groups = [
            [os.path.abspath(os.path.join(r, f))
             for r, _, fs in os.walk(os.path.expanduser(grp))
             for f in sorted(fs) if f.lower().endswith(VIDEO_EXTS)]
            for grp in slot_groups
        ]
        slots = len(slot_groups)

    # 2) BGM 池（支持多文件夹：bgm_dirs 列表；兼容旧的单文件夹 bgm_dir）
    bgm_dirs = spec.get("bgm_dirs")
    if not bgm_dirs:
        single = spec.get("bgm_dir")
        bgm_dirs = [single] if single else []
    if isinstance(bgm_dirs, str):
        bgm_dirs = [bgm_dirs]
    bgm_dirs = [os.path.expanduser(d) for d in bgm_dirs if d]
    if not bgm_dirs:
        raise RuntimeError("未配置 BGM 文件夹（spec 里给 bgm_dirs 或 bgm_dir）")
    missing = [d for d in bgm_dirs if not os.path.isdir(d)]
    if missing:
        raise RuntimeError("BGM 目录不存在：" + "；".join(missing))
    bgm_pool = []
    for d in bgm_dirs:
        for r, _, fs in os.walk(d):
            for f in sorted(fs):
                if f.lower().endswith(AUDIO_EXTS):
                    bgm_pool.append(os.path.abspath(os.path.join(r, f)))
    # 同一首歌出现在多个文件夹时只留一份
    seen, uniq = set(), []
    for p in bgm_pool:
        if p not in seen:
            seen.add(p)
            uniq.append(p)
    bgm_pool = uniq

    # 2b) 本店选用哪些 BGM（spec.bgm_filter：only / exclude；缺省 = 用全部）
    _all_bgm = len(bgm_pool)
    bgm_pool = apply_bgm_filter(bgm_pool, spec.get("bgm_filter"))
    if not bgm_pool and _all_bgm:
        print(f"[警告] bgm_filter 把 BGM 全过滤掉了（曲库有 {_all_bgm} 首，"
              f"勾中 0 首），已回退为「用全部」。"
              f"请到控制台 BGM 区检查「本店选用哪些 BGM」。")
        bgm_pool = uniq
    elif len(bgm_pool) < _all_bgm:
        print(f"[BGM 选用] 本店勾选 {len(bgm_pool)}/{_all_bgm} 首参与出片。")

    # 3) 标题（文案库）
    #    库文件每行 `[类型] 文案`（类型可省）。`[类型] ` 只是标签，
    #    出片用的是「文案」本身 —— titlelib 负责把前缀剥干净，绝不混进成片标题。
    #    再按该店面的选用规则 title_filter 过滤（默认全部 / 类型排除 / 单条挑选）。
    titles_file = spec.get("titles")
    if titles_file:
        titles_file = os.path.expanduser(titles_file)
    _entries = titlelib.load_library(titles_file) if titles_file else []
    titles = titlelib.apply_filter(_entries, spec.get("title_filter"))
    if _entries and not titles:
        # 筛选把文案全滤没了 → 回退用全部，避免出片没有标题
        print("[警告] title_filter 把文案全过滤掉了，已回退为「用全部」：%s"
              % (spec.get("title_filter"),))
        titles = [e["text"] for e in _entries]

    count = int(spec.get("count", 10))
    slots = normalize_slots(spec.get("slots")) or 6
    shot_dur = float(spec.get("shot_dur", 2.0))
    min_diff = int(spec.get("min_diff", 2))
    rng = random.Random(spec.get("seed"))
    title_offset = int(spec.get("title_offset", 0))
    speed = float(spec.get("speed", 1.0))
    video_speed = float(spec.get("video_speed", 1.0))
    font_file = spec.get("font_file")
    huazi_file = spec.get("huazi_file")
    # 平台查重加固：画面随机微变换 / BGM 加速变调 / 随机起始点（0 = 关闭）
    clip_jitter = float(spec.get("clip_jitter", 0.0))
    bgm_jitter = float(spec.get("bgm_jitter", 0.0))
    start_jitter = float(spec.get("start_jitter", 0.0))
    # 排他类别：相邻两条不能都含（如「端锅-上桌」要求隔条出现，不要每条都有）
    exclusive = spec.get("exclusive_adjacent") or []

    # 3b) 镜头配方（用户自定义剪辑规则）：第 i 段固定类别，类内随机换文件。
    #     这样一条视频内部 6 段必然来自 6 个不同「意思」，绝不会同意思相邻。
    #     给多个配方（shot_recipes）时按条轮换，让相邻两条的类别集合不同。
    pool_roots = spec.get("pool_roots")
    recipes = spec.get("shot_recipes") or (
        [spec["shot_recipe"]] if spec.get("shot_recipe") else None)

    # 每条段数：留空 / 0 / "auto" = 按配方长度；填数字 = 强制该段数（配方当骨架，
    # 少了截末尾、多了在末尾补坑位，补位不与前一段同类）。
    want_slots = normalize_slots(spec.get("slots"))

    recipe_groups = []
    if recipes:
        avail_cats = sorted(group_by_category(pool, pool_roots)) if want_slots else []
        for ri, r in enumerate(recipes, 1):
            r2 = fit_recipe_to_slots(r, want_slots, avail_cats) if want_slots else r
            grps, cats = resolve_shot_recipe(r2, pool, pool_roots)
            recipe_groups.append(grps)
            tag = ""
            if want_slots and len(r) != want_slots:
                verb = "截为" if len(r) > want_slots else "补为"
                tag = (f"（配方原 {len(r)} 段 → 按「每条段数={want_slots}」"
                       f"{verb} {want_slots} 段）")
            print(f"[配方{ri}] " + " → ".join("/".join(c) for c in cats) + tag)
        slots = len(recipe_groups[0])
        if len({len(g) for g in recipe_groups}) > 1:
            print("[段数] 多个配方段数不一致（"
                  + " / ".join(str(len(g)) for g in recipe_groups)
                  + " 段），各按各的长度出片。")

    # 4) 跨批次 history
    history_path = spec.get("history_path") or DEFAULT_HISTORY_PATH
    history = [] if spec.get("ignore_history") else load_history(history_path)

    # 配方固定了每个坑位的类别，坑位顺序不能再打乱（打乱就破坏配方）
    shuffle_slots = bool(spec.get("shuffle_slots", False)) and not recipes
    if recipes:
        combos = plan_combos_recipe(recipe_groups, count, min_diff, rng, history)
    else:
        combos = plan_combos(pool, slots, count, min_diff, rng, history,
                             slot_groups=slot_groups, shuffle_slots=shuffle_slots,
                             pool_roots=pool_roots)
    beat_sync = bool(spec.get("beat_sync", True))
    # 不卡点模式：素材完整播出，成片长度先算出来，再按长度配够长的 BGM
    min_durs = None
    if not beat_sync:
        min_durs = [sum(clip_duration_us(c) for c in combo) / 1e6
                    for combo in combos]
    bgms = pick_bgm_dedup(bgm_pool, len(combos), rng, min_durs)
    title_list = pick_titles_least_used(titles, len(combos), history,
                                        title_offset)
    # 创作规则：相邻两条视频的「意思」不要撞（类别优先，其次具体文件）
    combos, bgms, title_list = separate_items(combos, bgms, title_list,
                                              pool_roots=pool_roots,
                                              exclusive=exclusive)
    metrics = _report_sameness(combos, pool_roots, exclusive)

    if spec.get("dry_run"):
        print(f"\n[dry-run] 方案（{len(combos)} 条 × {slots} 段）：")
        for i, (combo, (bgm, off), title) in enumerate(
                zip(combos, bgms, title_list), 1):
            names = " + ".join(
                os.path.splitext(os.path.basename(c))[0] for c in combo)
            print(f"  #{i:03d} {names} | BGM={os.path.basename(bgm)} "
                  f"off={off}s | 文案: {title or '(无)'}")
        return {
            "dry_run": True,
            "name": name,
            "slots": slots,
            "count": len(combos),
            "combos": [[os.path.basename(c) for c in combo] for combo in combos],
            "bgms": [os.path.basename(b) for b, _ in bgms],
            "offsets": [round(o, 1) for _, o in bgms],
            "titles": list(title_list),
            "metrics": metrics,
        }

    items = [
        {"videos": combo, "bgm": bgm, "title": title,
         "shot_dur": shot_dur, "offset": off, "bgm_volume": 0.6,
         "beat_sync": beat_sync}
        for combo, (bgm, off), title in zip(combos, bgms, title_list)
    ]
    result = generate_batch(
        {"name": name, "width": width, "height": height,
         "items": items, "speed": speed, "video_speed": video_speed,
         "font_file": font_file, "huazi_file": huazi_file,
         "rng": rng, "clip_jitter": clip_jitter, "bgm_jitter": bgm_jitter,
         "start_jitter": start_jitter}
    )

    if not spec.get("ignore_history"):
        recs = [{
            "name": name, "draft_id": result["draft_id"],
            "combo": [os.path.basename(c) for c in combo],
            "bgm": os.path.basename(bgm), "title": title,
        } for combo, (bgm, _), title in zip(combos, bgms, title_list)]
        save_history(history_path, recs)
        print(f"[去重] 已把 {len(recs)} 条写入产出记录：{history_path}"
              f"（下次追加生成自动避开）")

    return result
