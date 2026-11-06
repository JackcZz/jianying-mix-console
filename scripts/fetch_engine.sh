#!/bin/bash
# 拉取引擎依赖 —— engine/ 不进版本库（2.7MB，来自 jianying-batch-drafts skill）
set -e
HERE="$(cd "$(dirname "$0")/.." && pwd)"
DEST="$HERE/engine"

CANDIDATES=(
  "$HOME/.workbuddy/skills/jianying-batch-drafts/references/engine"
  "$HOME/agent-skills/skills/jianying-batch-drafts/references/engine"
)

for c in "${CANDIDATES[@]}"; do
  if [ -f "$c/config.py" ]; then
    echo "→ 从 $c 复制引擎"
    rm -rf "$DEST"
    cp -a "$c" "$DEST"
    echo "✅ engine 就绪（$(du -sh "$DEST" | cut -f1)）"
    exit 0
  fi
done

echo "❌ 未找到引擎来源。请任选其一：" >&2
echo "   1) 安装 skill：把 jianying-batch-drafts 装到 ~/.workbuddy/skills/" >&2
echo "   2) 手动把 engine/ 目录放到：$DEST" >&2
exit 1
