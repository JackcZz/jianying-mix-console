"""
跨环境定位外部命令行工具（ffmpeg / ffprobe …）· 纯标准库

问题背景
--------
本控制台常由 macOS 的 .app 启动器 / launchd 拉起，那种环境下 PATH 只有
    /usr/bin:/bin:/usr/sbin:/sbin
而 ffmpeg 通常装在 Homebrew（/opt/homebrew/bin）或本机工具目录
（~/.workbuddy/binaries/ffmpeg）。于是 subprocess 用裸名 "ffmpeg" 调用时直接
    FileNotFoundError: [Errno 2] No such file or directory: 'ffmpeg'

做法
----
1) 把「确实含 ffmpeg/ffprobe 可执行文件」的目录**前置**到 os.environ["PATH"]
   （幂等，进程级生效，所有子进程继承）。
2) 暴露 ffmpeg_exe() / ffprobe_exe() 返回**绝对路径**，可直接当 argv[0] 用，
   即使 PATH 被上层重置也不受影响。

搜索顺序（优先级从高到低）
--------------------------
  环境变量 JIANYING_FFMPEG_DIR
  项目内置 bin/            （随整目录分发的便携布局）
  engine/bin/
  ~/.workbuddy/binaries/ffmpeg
  /opt/homebrew/bin        （Apple Silicon Homebrew）
  /usr/local/bin           （Intel Homebrew / 手工安装）
  /usr/bin  /bin
"""
from __future__ import annotations

import os
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))

_ENV_DIR = (os.environ.get("JIANYING_FFMPEG_DIR") or "").strip()

CANDIDATE_DIRS = [
    d for d in [
        _ENV_DIR,
        os.path.join(HERE, "bin"),
        os.path.join(HERE, "engine", "bin"),
        os.path.expanduser("~/.workbuddy/binaries/ffmpeg"),
        "/opt/homebrew/bin",
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
    ] if d
]

_NAMES = ("ffmpeg", "ffprobe")


def _exists(path: str) -> bool:
    return os.path.isfile(path) and os.access(path, os.X_OK)


def _which(name: str):
    p = shutil.which(name)
    if p and _exists(p):
        return p
    return None


def _probe_dir(d: str) -> bool:
    """该目录里是否真的有 ffmpeg/ffprobe 可执行文件。"""
    return any(_exists(os.path.join(d, n)) for n in _NAMES)


def ensure_path() -> list:
    """把含 ffmpeg/ffprobe 的候选目录前置到 PATH（幂等）。返回本次新增的目录。"""
    cur = os.environ.get("PATH", "")
    parts = [p for p in cur.split(os.pathsep) if p]
    added = []
    for d in CANDIDATE_DIRS:
        if d in parts or d in added:
            continue
        if os.path.isdir(d) and _probe_dir(d):
            added.append(d)
    if added:
        os.environ["PATH"] = os.pathsep.join(added + parts)
    return added


def _locate(name: str):
    p = _which(name)
    if p:
        return p
    for d in CANDIDATE_DIRS:
        cand = os.path.join(d, name)
        if _exists(cand):
            return cand
    return None


# 导入即生效：先把目录塞进 PATH，再定位绝对路径
ensure_path()

FFMPEG = _locate("ffmpeg")
FFPROBE = _locate("ffprobe")


def ffmpeg_exe(default: str = "ffmpeg") -> str:
    """ffmpeg 绝对路径（找不到时回退裸名，交给系统 PATH 再试一次）。"""
    return FFMPEG or default


def ffprobe_exe(default: str = "ffprobe") -> str:
    """ffprobe 绝对路径。"""
    return FFPROBE or default


def describe() -> str:
    return (
        f"ffmpeg={FFMPEG or '(未找到)'}  ffprobe={FFPROBE or '(未找到)'}\n"
        f"PATH={os.environ.get('PATH', '')}"
    )


if __name__ == "__main__":
    print(describe())
