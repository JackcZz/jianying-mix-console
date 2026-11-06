#!/bin/bash
# 素材池批量转代理：4K HEVC → 1080x1920 H.264（成片画布本就是 1080x1920，画质无损）
# 保持源目录结构，已存在的输出自动跳过（可断点续跑）。
#
# 用法: make_proxy.sh <源目录> <目标目录> [并发数]
#   例: ./make_proxy.sh "~/Desktop/02 鱼杂内容/ai处理好的鱼杂素材" \
#                       "~/Desktop/02 鱼杂内容/ai处理好的鱼杂素材_1080代理" 4
set -u
SRC="${1:?缺源目录}"
DST="${2:?缺目标目录}"
J="${3:-4}"
SRC="${SRC/#\~/$HOME}"
DST="${DST/#\~/$HOME}"
mkdir -p "$DST"
cd "$SRC" || exit 1

conv() {
  rel="${1#./}"
  out="$DST/${rel%.*}.mp4"
  mkdir -p "$(dirname "$out")" || return
  if [ -f "$out" ]; then echo "SKIP $rel"; return; fi
  ffmpeg -nostdin -loglevel error -y -i "$rel" \
    -vf "scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2" \
    -c:v libx264 -preset medium -crf 18 -pix_fmt yuv420p \
    -c:a aac -b:a 128k -movflags +faststart "$out" >/dev/null 2>&1
  if [ -f "$out" ]; then echo "OK   $rel"; else echo "FAIL $rel"; fi
}
export -f conv
export DST

find . -type f \( -iname "*.mp4" -o -iname "*.mov" -o -iname "*.m4v" \) -print \
  | xargs -P "$J" -I{} bash -c 'conv "$@"' _ {}
echo "ALL DONE -> $DST"
