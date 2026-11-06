#!/bin/bash
# 一键初始化：建虚拟环境 + 装依赖 + 拉引擎
set -e
HERE="$(cd "$(dirname "$0")/.." && pwd)"
cd "$HERE"

PY="${PYTHON:-python3}"
echo "→ Python: $("$PY" -V 2>&1)"

if [ ! -x .venv/bin/python ]; then
  echo "→ 创建虚拟环境 .venv"
  "$PY" -m venv .venv
fi

echo "→ 安装依赖（requirements.txt）"
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt
echo "✅ 依赖就绪"

bash scripts/fetch_engine.sh

echo
echo "全部就绪。启动控制台："
echo "    bash scripts/run.sh"
