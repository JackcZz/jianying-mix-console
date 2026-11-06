#!/bin/bash
# 素材一键预处理：60fps HEVC → 30fps H.264 + 统一 1080×1920
# 目的：解决剪映二次编辑卡顿（60fps 解码翻倍），并减小体积。
# 保持原目录结构 + 原文件名（produced_history 去重不受影响），已存在自动跳过可续跑。
#
# 用法: ./preprocess.sh <源目录> <目标目录> [掐头秒] [掐尾秒] [并发数]
#   例: ./preprocess.sh "~/Desktop/02 鱼杂内容/ai处理好的鱼杂素材/0911杭州鱼杂" \
#                       "~/Desktop/02 鱼杂内容/ai处理好的鱼杂素材/0911杭州鱼杂_处理" 0 0 4
#
# 说明：你现有素材平均 2.7s（最短1.67/最长4.27），已是拍好裁好的短片段，
#       掐头去尾默认给 0 即可；以后若拍长素材，传 1 1 表示前后各掐 1 秒。
set -u
SRC="${1:?缺源目录}"; DST="${2:?缺目标目录}"
HEAD="${3:-0}"; TAIL="${4:-0}"; J="${5:-4}"
SRC="${SRC/#\~/$HOME}"; DST="${DST/#\~/$HOME}"
mkdir -p "$DST"; cd "$SRC" || exit 1

conv() {
  rel="${1#./}"
  out="$DST/${rel%.*}.mp4"
  mkdir -p "$(dirname "$out")" || return
  [ -f "$out" ] && { echo "SKIP $rel"; return; }

  # 掐头去尾：-ss 快速定位到 HEAD 秒，-t 只保留 (总长-头-尾) 秒
  trim=()
  if [ "$HEAD" != "0" ] || [ "$TAIL" != "0" ]; then
    dur=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$rel")
    keep=$(awk -v d="$dur" -v h="$HEAD" -v t="$TAIL" \
           'BEGIN{k=d-h-t; if(k<0.3)k=0.3; print k}')
    trim=(-ss "$HEAD" -t "$keep")
  fi

  ffmpeg -nostdin -loglevel error -y "${trim[@]}" -i "$rel" \
    -vf "scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2" \
    -c:v libx264 -preset medium -crf 20 -r 30 -pix_fmt yuv420p \
    -c:a aac -b:a 128k -movflags +faststart "$out" >/dev/null 2>&1
  [ -f "$out" ] && echo "OK   $rel" || echo "FAIL $rel"
}
export -f conv; export DST HEAD TAIL

find . -type f \( -iname "*.mp4" -o -iname "*.mov" -o -iname "*.m4v" \) -print \
  | xargs -P "$J" -I{} bash -c 'conv "$@"' _ {}
echo "ALL DONE -> $DST"
