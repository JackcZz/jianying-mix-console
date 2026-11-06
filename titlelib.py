"""文案库（标题库）读写与筛选 —— 纯标准库，无第三方依赖。

库文件格式（txt，一行一条）：

    # 以 # 开头 → 注释，整行忽略
    [类型] 第一段|第二段        # 方括号类型可选
    第一段|第二段              # 没写类型 → 未分类

设计要点
--------
1. 「类型」只是给人看 + 给筛选用的标签；**出片时用的是「文案」本身**，
   `[类型] ` 前缀绝不能混进成片标题（`parse_line()` 负责剥掉）。
2. 文案的**身份 = 文本内容本身**（不是行号、不是 id）——因为库是 txt、
   用户会随手加删行，用行号当身份一改就错位。用内容当身份后，
   店铺的「单条勾选」在库重排/插行后依然有效，只有真的改了这句字才会失效。
3. 完全相同的文案自动去重（保序，留第一条的类型）。
4. 向后兼容：旧的无前缀 txt 直接可用，全部归为「未分类」。
"""

import json
import os
import re
import shutil
import time

UNCLASSIFIED = "未分类"

# [类型] 文案   —— 类型限 1~20 字、不含方括号，避免误吞文案里的方括号
_LINE_RE = re.compile(r"^\[([^\[\]]{1,20})\]\s*(.+)$")


# ---------------------------------------------------------------------------
# 解析 / 格式化
# ---------------------------------------------------------------------------
def parse_line(raw):
    """一行原文 → (type, text)。注释行 / 空行 → None。"""
    s = (raw or "").strip()
    if not s or s.startswith("#"):
        return None
    m = _LINE_RE.match(s)
    if m:
        t = m.group(1).strip()
        body = m.group(2).strip()
        if body:
            return (t or UNCLASSIFIED), body
    return UNCLASSIFIED, s


def format_line(type_, text):
    """(type, text) → 一行原文。"""
    t = (type_ or "").strip() or UNCLASSIFIED
    return "[%s] %s" % (t, (text or "").strip())


def split_title(text):
    """成片标题 → (第一行, 第二行)。`|` 最多切一刀，第二行可空。"""
    parts = [p.strip() for p in (text or "").split("|", 1)]
    first = parts[0] if parts else ""
    second = parts[1] if len(parts) > 1 else ""
    return first, second


# ---------------------------------------------------------------------------
# 读写
# ---------------------------------------------------------------------------
def load_library(path):
    """读库文件 → [{"type":..., "text":...}]，保序 + 按 text 去重。"""
    if not path or not os.path.isfile(os.path.expanduser(path)):
        return []
    return _dedup(_iter_entries(os.path.expanduser(path)))


def load_library_text(text):
    """从一段文本解析（导入 / 粘贴用）→ [{"type":..., "text":...}]。

    宽容处理：允许行尾多余空白、允许 `[类型]文案` 中间不空格、
    也允许整行只有文案（→ 未分类）、`#` 开头的注释行自动跳过。
    """
    out = []
    for ln in (text or "").splitlines():
        got = parse_line(ln)
        if got:
            out.append({"type": got[0], "text": got[1]})
    return _dedup(out)


def _iter_entries(path):
    with open(path, encoding="utf-8") as f:
        for raw in f:
            got = parse_line(raw)
            if got:
                yield {"type": got[0], "text": got[1]}


def _dedup(entries):
    out, seen = [], set()
    for e in entries:
        t = " ".join(str(e.get("text", "")).split())
        if not t or t in seen:
            continue
        seen.add(t)
        out.append({"type": (e.get("type") or UNCLASSIFIED).strip() or UNCLASSIFIED,
                    "text": t})
    return out


def dump_library(entries, header=None):
    """entries → 库文件全文。"""
    lines = []
    if header:
        lines.append("# " + str(header).replace("\n", " "))
    for e in entries:
        lines.append(format_line(e.get("type"), e.get("text")))
    return "\n".join(lines) + ("\n" if lines else "")


def save_library(path, entries, backup=True):
    """原子写库文件（tmp + os.replace），先备份 .bak-时间戳。返回 (备份路径, 条数)。"""
    path = os.path.expanduser(path)
    entries = _dedup(entries)
    bak = ""
    if backup and os.path.isfile(path):
        bak = backup_file(path)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(dump_library(entries))
    os.replace(tmp, path)
    return bak, len(entries)


def backup_file(path, tag=None):
    """把文件复制成 <name>.bak-<时间戳>[.tag]。返回备份路径（原文件不存在则空串）。"""
    if not path or not os.path.isfile(path):
        return ""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    suffix = (".bak-%s.%s" % (stamp, tag)) if tag else (".bak-%s" % stamp)
    dst = path + suffix
    n = 1
    while os.path.exists(dst):                  # 同一秒内多次备份不互相覆盖
        n += 1
        dst = path + suffix + "-%d" % n
    shutil.copy2(path, dst)
    return dst


# ---------------------------------------------------------------------------
# 类型 / 用量 / 筛选
# ---------------------------------------------------------------------------
def types_of(entries):
    """→ [类型名]，按「首次出现顺序」排（保持用户自己的排列习惯）。"""
    out = []
    for e in entries:
        t = e.get("type") or UNCLASSIFIED
        if t not in out:
            out.append(t)
    return out


def usage_counts(history):
    """成片历史 → {文案文本: 用过几次}。

    只统计**真的出过片**的（history 是产出记录），所以数字必然可信。
    """
    cnt = {}
    for h in (history or []):
        t = (h.get("title") or "").strip()
        if t:
            cnt[t] = cnt.get(t, 0) + 1
    return cnt


def load_history(path):
    """读 produced_history.jsonl → [dict]。文件不存在 → []。"""
    if not path:
        return []
    path = os.path.expanduser(path)
    if not os.path.isfile(path):
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            try:
                out.append(json.loads(ln))
            except Exception:
                continue
    return out


def normalize_filter(flt):
    """把各种写法归一成 {"exclude_types": [...], "only": [...], "exclude": [...]}。"""
    flt = flt or {}
    def _lst(key):
        v = flt.get(key) or []
        if isinstance(v, str):
            v = [v]
        return [str(x).strip() for x in v if str(x).strip()]
    return {
        "exclude_types": _lst("exclude_types"),
        "only": _lst("only"),
        "exclude": _lst("exclude"),
    }


def is_default_filter(flt):
    """没做任何限制 = 用全部（「使用默认文案」）。"""
    f = normalize_filter(flt)
    return not (f["exclude_types"] or f["only"] or f["exclude"])


def apply_filter(entries, flt):
    """按店铺选用规则过滤 → [文案文本]，保序 + 去重。

    规则叠加顺序：
      1. only 非空 → 先只留白名单里的（单选/多选都走这里）
      2. 去掉类型在 exclude_types 里的（整类排除）
      3. 去掉在 exclude 里的（单条排除）
    """
    f = normalize_filter(flt)
    only = set(f["only"])
    ex_types = set(f["exclude_types"])
    ex = set(f["exclude"])

    out, seen = [], set()
    for e in entries:
        text = e.get("text")
        if not text or text in seen:
            continue
        if only and text not in only:
            continue
        if (e.get("type") or UNCLASSIFIED) in ex_types:
            continue
        if text in ex:
            continue
        seen.add(text)
        out.append(text)
    return out


def merge_entries(old, new, mode="append"):
    """导入时合并。

    replace : 完全用新的
    append  : 旧的在前，追加新的（同名文案保留旧类型）
    merge   : 同名文案以**新**的类型为准（用户从别处拿来的表更新）
    """
    if mode == "replace":
        return _dedup(new)

    old_map = {}
    for e in old:
        old_map.setdefault(e["text"], e)

    if mode == "merge":
        out, seen = [], set()
        for e in new:                       # 先放新的（顺序 = 导入文件顺序）
            if e["text"] in seen:
                continue
            seen.add(e["text"])
            out.append(e)
        for e in old:                       # 旧文件里多出来的补在后面
            if e["text"] not in seen:
                seen.add(e["text"])
                out.append(e)
        return _dedup(out)

    # append
    return _dedup(list(old) + list(new))
