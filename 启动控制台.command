#!/bin/bash
# 启动视频混剪控制台 → http://127.0.0.1:8766
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
PORT=8766
PY="$HERE/.venv/bin/python"; [ -x "$PY" ] || PY="/usr/bin/python3"

# 已在运行？直接开浏览器，不重复启动
if /usr/bin/curl -fsS --noproxy '*' -o /dev/null --max-time 2 "http://127.0.0.1:$PORT/api/status" 2>/dev/null; then
  echo "✅ 控制台已在运行，直接打开浏览器"
  /usr/bin/open "http://127.0.0.1:$PORT"
  exit 0
fi

# 后台等服务起来再自动开浏览器
( for _ in $(seq 1 60); do sleep 0.5
    /usr/bin/curl -fsS --noproxy '*' -o /dev/null --max-time 2 "http://127.0.0.1:$PORT/api/status" \
      && { /usr/bin/open "http://127.0.0.1:$PORT"; break; }
  done ) &

echo "控制台 → http://127.0.0.1:$PORT"
echo "⚠ 正式出片前请 ⌘Q 完全退出剪映（否则草稿索引会重复）"
cd "$HERE" && exec "$PY" console.py
