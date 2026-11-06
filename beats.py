"""
BGM 节拍 / 重音检测（自研，无 librosa 依赖）

思路：
  1. ffmpeg 解码为单声道 PCM 浮点
  2. 分帧加窗 → FFT → 幅度谱
  3. 谱通量（spectral flux，半波整流差分）= onset 强度包络
  4. 自适应阈值（滑动中位数）选峰 → 重音时间点
  5. 自相关估 BPM，可给出等间隔节拍网格（用于均匀卡点）

对外接口：
  extract_onsets(path)     -> List[float] 秒，重音时间点（按时间排序）
  estimate_bpm(path)       -> float，估计 BPM
  beat_grid(bpm, offset, duration) -> List[float] 秒
  pick_cut_points(path, n, shot_dur) -> List[float] 秒，
      在重音附近选 n+1 个剪辑点，段长接近 shot_dur
"""
from __future__ import annotations

import os
import sys
import subprocess
from typing import List, Optional

import numpy as np

# 定位 ffmpeg：.app / launchd 启动时 PATH 很窄，裸名 "ffmpeg" 会 Errno 2。
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
try:
    import binpaths
    _FFMPEG = binpaths.ffmpeg_exe()
except Exception:                      # binpaths 缺失时退回裸名（旧行为）
    _FFMPEG = "ffmpeg"

SR = 22000          # 分析采样率
FRAME = 1024        # 帧长
HOP = 256           # 帧移（≈11.6ms 分辨率）
MIN_GAP = 0.12      # 两个重音最小间隔（秒）


def _decode_mono(path: str) -> np.ndarray:
    """ffmpeg 解码任意音频为 f32le 单声道 PCM。"""
    cmd = [
        _FFMPEG, "-v", "error", "-i", path,
        "-ac", "1", "-ar", str(SR), "-f", "f32le", "-",
    ]
    raw = subprocess.run(cmd, capture_output=True, check=True).stdout
    return np.frombuffer(raw, dtype=np.float32).copy()


def _onset_envelope(y: np.ndarray) -> np.ndarray:
    """谱通量 onset 强度包络（每帧一个值）。"""
    n = (len(y) - FRAME) // HOP + 1
    if n < 8:
        return np.array([])
    win = np.hanning(FRAME).astype(np.float32)
    frames = np.lib.stride_tricks.as_strided(
        y, shape=(n, FRAME),
        strides=(y.strides[0] * HOP, y.strides[0]),
    ).copy()
    mag = np.abs(np.fft.rfft(frames * win, axis=1))
    flux = np.maximum(0.0, np.diff(mag, axis=0)).sum(axis=1)
    # 轻微平滑，抗抖动
    if len(flux) > 5:
        k = np.array([1, 2, 3, 2, 1], dtype=np.float32)
        k /= k.sum()
        flux = np.convolve(flux, k, mode="same")
    return flux


def _moving_median(x: np.ndarray, win: int) -> np.ndarray:
    pad = win // 2
    xp = np.pad(x, (pad, pad), mode="edge")
    idx = np.arange(win)[None, :] + np.arange(len(x))[:, None]
    return np.median(xp[idx], axis=1)


def extract_onsets(path: str, limit: Optional[float] = None) -> List[float]:
    """返回重音时间点列表（秒）。limit 秒后忽略。"""
    y = _decode_mono(path)
    env = _onset_envelope(y)
    if len(env) == 0:
        return []
    times = (np.arange(len(env)) * HOP + FRAME / 2) / SR
    if limit:
        keep = times <= limit + 0.5
        env, times = env[keep], times[keep]
    if len(env) < 8:
        return []

    thr = _moving_median(env, win=int(0.7 * SR / HOP)) + 0.6 * env.std()
    peaks = []
    last = -1e9
    for i in range(1, len(env) - 1):
        if env[i] > thr[i] and env[i] >= env[i - 1] and env[i] >= env[i + 1]:
            t = times[i]
            if t - last >= MIN_GAP:
                peaks.append(float(t))
                last = t
            elif peaks and env[i] > env[int(round((peaks[-1] * SR - FRAME / 2) / HOP))]:
                # 间隔不够但更强，替换上一个
                peaks[-1] = float(t)
                last = t
    return peaks


def estimate_bpm(path: str, limit: float = 30.0) -> float:
    """自相关估 BPM（60–180 范围）。"""
    y = _decode_mono(path)
    if limit:
        y = y[: int(limit * SR)]
    env = _onset_envelope(y)
    if len(env) < 16:
        return 0.0
    env = env - env.mean()
    ac = np.correlate(env, env, mode="full")[len(env) - 1:]
    # BPM → 帧周期：60/bpm 秒/拍
    fps = SR / HOP
    best, best_v = 0.0, -1.0
    for bpm in range(60, 181):
        lag = 60.0 / bpm * fps
        lo, hi = int(lag * 0.98), int(lag * 1.02) + 1
        if hi >= len(ac):
            break
        v = ac[lo:hi].max()
        if v > best_v:
            best_v, best = v, bpm
    return float(best)


def beat_grid(bpm: float, offset: float = 0.0, duration: float = 30.0) -> List[float]:
    """按 BPM 生成等间隔节拍网格（秒）。"""
    if bpm <= 0:
        return []
    step = 60.0 / bpm
    return [round(offset + i * step, 4) for i in range(int((duration - offset) / step) + 1)]


def pick_cut_points(
    path: str,
    n_shots: int,
    shot_dur: float = 2.0,
    offset: float = 0.0,
    max_scan: float = 60.0,
) -> List[float]:
    """
    选 n_shots+1 个剪辑点（含首尾），使每段时长接近 shot_dur，
    且内部剪辑点尽量落在重音上。

    返回长度 n_shots+1 的时间列表（秒），供视频轨切分。
    """
    onsets = [t for t in extract_onsets(path, limit=max_scan) if t >= offset]
    points = [offset]
    for i in range(1, n_shots):
        target = offset + i * shot_dur
        if onsets:
            nearest = min(onsets, key=lambda t: abs(t - target))
            # 重音距目标不超过 0.35s 才吸附，否则保持等间隔
            if abs(nearest - target) <= 0.35:
                target = nearest
        points.append(round(target, 4))
    points.append(round(offset + n_shots * shot_dur, 4))
    return sorted(points)
