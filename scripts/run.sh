#!/bin/bash
# 启动 Web 控制台 → http://127.0.0.1:8766
set -e
HERE="$(cd "$(dirname "$0")/.." && pwd)"
cd "$HERE"

if [ ! -x .venv/bin/python ]; then
  echo "❌ 尚未初始化。先运行：bash scripts/setup.sh" >&2
  exit 1
fi
if [ ! -d engine ]; then
  echo "→ engine/ 缺失，自动拉取"
  bash scripts/fetch_engine.sh
fi

echo "控制台 → http://127.0.0.1:8766"
echo "⚠ 正式出片前请 ⌘Q 完全退出剪映（否则草稿索引会重复）"
exec .venv/bin/python console.py
