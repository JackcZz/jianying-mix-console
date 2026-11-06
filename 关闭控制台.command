#!/bin/bash
# 关闭视频混剪控制台（端口 8766）
set -u
PORT=8766

# 杀掉确认是本服务的进程：pgrep 精确匹配命令行为主，lsof 按端口兜底（防 PID 复用误杀）
for pid in $(pgrep -f "console\.py" 2>/dev/null); do
  /bin/kill "$pid" 2>/dev/null
done
PIDS="$(/usr/sbin/lsof -ti tcp:"$PORT" 2>/dev/null | /usr/bin/sort -u)"
for pid in $PIDS; do
  cmd="$(/bin/ps -p "$pid" -o command= 2>/dev/null || true)"
  case "$cmd" in *console.py*) /bin/kill "$pid" 2>/dev/null;; esac
done
sleep 0.5

# 等端口真正释放（uvicorn 优雅退出要 1~2 秒，最多等 6 秒），避免误报
ok=0
for _ in $(seq 1 12); do
  if /usr/bin/curl -fsS --noproxy '*' -o /dev/null --max-time 1 "http://127.0.0.1:$PORT/api/status" 2>/dev/null; then
    sleep 0.5
  else
    ok=1; break
  fi
done
if [ "$ok" = 1 ]; then
  echo "✅ 已关闭（端口 $PORT 已释放）"
else
  echo "⚠ 端口 $PORT 仍占用，请再跑一次，或看是否还有残留进程"
fi
