#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成「花字库查看器」HTML —— 把 huazi.json / favorite_huazi.json / huazi_categories.json
合成一个可搜索、可筛选的离线网页，并内嵌本地剪映缓存抽出的预览图。

用法：
    python3 gen_huazi_viewer.py
输出：
    huazi_library/花字库.html   （双击即可用浏览器打开，无需服务器）
    huazi_library/preview/*.png （从剪映缓存抽出的花字封面图）

预览图来源：~/Movies/JianyingPro/User Data/Cache/artistEffect/<effect_id>/*/cover_icon.png
"""
import os
import glob
import json
import shutil
import html

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "assets", "huazi_library")
PREVIEW_DIR = os.path.join(OUT_DIR, "preview")

HUAZI_JSON = os.path.join(HERE, "assets", "huazi.json")
FAV_JSON = os.path.join(HERE, "assets", "favorite_huazi.json")
FONT_JSON = os.path.join(HERE, "assets", "favorite_fonts.json")
CAT_JSON = os.path.join(HERE, "assets", "huazi_categories.json")
JY_CACHE = os.path.expanduser("~/Movies/JianyingPro/User Data/Cache/artistEffect")

# 各项目 spec 里引用的收藏池（用于展示"哪个项目用哪份池"）
SPEC_DIRS = [
    ("鱼杂（yuza）", os.path.join(HERE, "projects", "yuza", "spec.json")),
    ("0911 杭州鱼杂", os.path.join(HERE, "projects", "yuza0911", "spec.json")),
    ("0911 跃杨鱼杂", os.path.join(HERE, "projects", "yuza0911yy", "spec.json")),
    ("红太阳牛鞭", os.path.expanduser("~/Desktop/03 品牌项目/红太阳牛鞭项目/config/niubian_spec.json")),
]


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def extract_previews():
    """从剪映缓存抽取 cover_icon.png → huazi_library/preview/<id>.png

    注意：**只处理有 cover_icon.png 的目录**。剪映的 artistEffect 目录名不一定
    都是花字（有些是 amazing 贴纸/面罩特效），这类目录里没有 cover_icon，跳过。
    返回「有预览图」的 id 集合 —— 含项目内已存在的（可能是手工放进来的）。
    """
    os.makedirs(PREVIEW_DIR, exist_ok=True)
    got = {}
    # 项目内已有的（之前抽的 / 手工补的），一并算作有图
    for f in os.listdir(PREVIEW_DIR):
        if f.lower().endswith(".png"):
            got[f[:-4]] = True
    if not os.path.isdir(JY_CACHE):
        return got
    for d in sorted(os.listdir(JY_CACHE)):
        p = os.path.join(JY_CACHE, d)
        if not os.path.isdir(p):
            continue
        covers = sorted(glob.glob(os.path.join(p, "*", "cover_icon.png")),
                        key=lambda x: -os.path.getsize(x))
        if covers:
            shutil.copy2(covers[0], os.path.join(PREVIEW_DIR, d + ".png"))
            got[d] = True
    return got


def build():
    all_h = load_json(HUAZI_JSON, [])
    fav = load_json(FAV_JSON, [])
    fonts = load_json(FONT_JSON, [])
    cats = load_json(CAT_JSON, {})
    fav = [str(x) for x in fav]
    got = extract_previews()

    # id → 分类名
    id2cat = {}
    for name, ids in (cats.get("categories") or {}).items():
        for i in ids:
            id2cat.setdefault(str(i), name)
    for i in (cats.get("other") or []):
        id2cat.setdefault(str(i), "其他")

    # 去重（按 id 保留首条）
    seen, rows = set(), []
    for e in all_h:
        i = str(e.get("id"))
        if not i or i in seen:
            continue
        seen.add(i)
        rows.append({
            "id": i,
            "title": e.get("title") or "(无标题)",
            "vip": bool(e.get("is_vip")),
            "cat": id2cat.get(i, "未分类"),
            "img": i in got,
        })

    # 各项目引用的池
    pools = []
    for label, sp in SPEC_DIRS:
        d = load_json(sp, {})
        if not isinstance(d, dict):
            continue
        hf = d.get("huazi_file")
        pools.append({
            "project": label,
            "huazi_file": os.path.expanduser(hf) if hf else None,
            "exists": bool(hf and os.path.isfile(os.path.expanduser(hf))),
        })

    payload = {
        "rows": rows,
        "fav": fav,
        "fonts": fonts,
        "pools": pools,
    }
    data_js = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")

    doc = HTML_TMPL.replace("/*__DATA__*/", data_js)
    out = os.path.join(OUT_DIR, "花字库.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(doc)

    print("全库条目      :", len(rows))
    print("当前启用池    :", len(fav))
    print("本地有预览图  :", len(got))
    print("收藏字体      :", len(fonts))
    print("输出          :", out)
    return out


HTML_TMPL = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>花字库查看器</title>
<style>
:root{
  --bg:#0f1115; --panel:#171a21; --panel2:#1d2129; --line:#262b36;
  --txt:#e6e9ef; --muted:#8a9099; --dim:#6b7280;
  --brand:#ff6b35; --gold:#f5c451; --green:#34d399; --red:#f87171; --blue:#60a5fa;
  --radius:12px;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--txt);
  font:14px/1.6 -apple-system,"PingFang SC","Helvetica Neue",Arial,sans-serif;
  -webkit-font-smoothing:antialiased}
a{color:var(--brand);text-decoration:none}
.wrap{max-width:1240px;margin:0 auto;padding:26px 22px 80px}

/* ── 顶部 ── */
header{display:flex;align-items:flex-end;justify-content:space-between;gap:16px;flex-wrap:wrap}
h1{margin:0;font-size:23px;letter-spacing:.3px}
h1 .sub{font-size:12.5px;color:var(--muted);font-weight:400;margin-left:10px;letter-spacing:0}
.stats{display:flex;gap:10px;flex-wrap:wrap}
.stat{background:var(--panel);border:1px solid var(--line);border-radius:10px;
  padding:8px 13px;min-width:86px}
.stat b{display:block;font-size:18px;line-height:1.25;color:var(--gold)}
.stat span{font-size:11.5px;color:var(--muted)}

/* ── 工具条 ── */
.toolbar{position:sticky;top:0;z-index:9;margin:20px 0 8px;padding:12px;
  background:rgba(15,17,21,.92);backdrop-filter:blur(10px);
  border:1px solid var(--line);border-radius:var(--radius);
  display:flex;gap:10px;align-items:center;flex-wrap:wrap}
input[type=search],select{background:var(--panel2);color:var(--txt);border:1px solid var(--line);
  border-radius:9px;padding:8px 11px;font-size:13px;outline:none;font-family:inherit}
input[type=search]{min-width:250px;flex:1 1 250px}
input[type=search]:focus,select:focus{border-color:var(--brand)}
.chk{display:flex;align-items:center;gap:6px;font-size:13px;color:var(--muted);
  background:var(--panel2);border:1px solid var(--line);border-radius:9px;padding:8px 11px;cursor:pointer;
  user-select:none;white-space:nowrap}
.chk.on{color:var(--txt);border-color:var(--brand)}
.chk input{accent-color:var(--brand);margin:0}
.btn{background:var(--panel2);color:var(--txt);border:1px solid var(--line);border-radius:9px;
  padding:8px 13px;font-size:13px;cursor:pointer;font-family:inherit;white-space:nowrap}
.btn:hover{border-color:var(--brand);color:var(--brand)}
.count{font-size:12.5px;color:var(--muted);margin-left:auto;white-space:nowrap}

/* ── 分区 ── */
section{margin-top:26px}
.sec-h{display:flex;align-items:baseline;gap:10px;margin-bottom:12px;flex-wrap:wrap}
.sec-h h2{margin:0;font-size:16px}
.sec-h .tip{font-size:12.5px;color:var(--muted)}

/* ── 卡片 ── */
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(146px,1fr));gap:12px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);
  overflow:hidden;cursor:pointer;transition:transform .12s,border-color .12s;position:relative}
.card:hover{transform:translateY(-2px);border-color:var(--brand)}
.card.sel{border-color:var(--gold);box-shadow:0 0 0 1px var(--gold) inset}
.thumb{aspect-ratio:1/1;background:#0b0d11;display:flex;align-items:center;justify-content:center;
  overflow:hidden;position:relative}
.thumb img{width:100%;height:100%;object-fit:contain;display:block}
.thumb .ph{font-size:26px;font-weight:700;opacity:.82;
  display:flex;align-items:center;justify-content:center;width:100%;height:100%}
.meta{padding:8px 9px 9px}
.meta .t{font-size:12.5px;line-height:1.35;height:2.7em;overflow:hidden;
  display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical}
.meta .i{font-size:10.5px;color:var(--dim);font-family:ui-monospace,Menlo,monospace;margin-top:4px}
.tags{display:flex;gap:4px;margin-top:5px;flex-wrap:wrap}
.tag{font-size:10px;padding:1px 6px;border-radius:20px;border:1px solid var(--line);color:var(--muted)}
.tag.vip{color:var(--gold);border-color:rgba(245,196,81,.4)}
.tag.free{color:var(--green);border-color:rgba(52,211,153,.4)}
.tag.cat{color:var(--blue);border-color:rgba(96,165,250,.35)}
.tag.img{color:var(--brand);border-color:rgba(255,107,53,.4)}

/* 启用池卡片更大 */
.pool .grid{grid-template-columns:repeat(auto-fill,minmax(168px,1fr))}
.pool .card{border-color:rgba(245,196,81,.28)}

/* ── 字体 ── */
.fonts{display:flex;flex-wrap:wrap;gap:8px}
.fontpill{background:var(--panel);border:1px solid var(--line);border-radius:9px;
  padding:7px 13px;font-size:13.5px}

.more{display:block;margin:18px auto 0;padding:10px 22px;border-radius:10px;
  background:var(--panel);border:1px solid var(--line);color:var(--txt);cursor:pointer;font-family:inherit}
.more:hover{border-color:var(--brand);color:var(--brand)}

.notice{margin-top:14px;background:var(--panel);border:1px solid var(--line);
  border-left:3px solid var(--brand);border-radius:10px;padding:12px 15px;font-size:12.8px;color:var(--muted)}
.notice b{color:var(--txt);font-weight:600}
code{background:#0b0d11;border:1px solid var(--line);border-radius:5px;padding:1px 6px;
  font-family:ui-monospace,Menlo,monospace;font-size:12px;color:var(--gold)}
.empty{padding:34px;text-align:center;color:var(--dim)}

/* ── 选中提示 ── */
.selbar{position:fixed;left:50%;transform:translateX(-50%);bottom:18px;z-index:30;
  background:rgba(29,33,41,.97);border:1px solid var(--gold);border-radius:11px;
  padding:9px 16px;font-size:13px;display:none;align-items:center;gap:12px;
  box-shadow:0 10px 30px rgba(0,0,0,.5);max-width:92vw}
.selbar.show{display:flex}
.selbar .nm{color:var(--gold);font-weight:600}
.selbar .id{font-family:ui-monospace,Menlo,monospace;color:var(--txt);user-select:all;font-size:12.5px}
.selbar .x{cursor:pointer;color:var(--muted)}
.selbar .x:hover{color:var(--red)}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>花字库查看器<span class="sub">剪映花字 · 离线可查</span></h1>
    <div class="stats" id="stats"></div>
  </header>

  <div class="toolbar">
    <input type="search" id="q" placeholder="搜索花字名称或 ID…">
    <select id="cat"><option value="">全部分类</option></select>
    <select id="vip">
      <option value="">全部</option>
      <option value="vip">仅 VIP</option>
      <option value="free">仅免费</option>
    </select>
    <label class="chk" id="imgChk"><input type="checkbox" id="onlyImg">只看有预览图</label>
    <label class="chk" id="favChk"><input type="checkbox" id="onlyFav">只看启用的池</label>
    <button class="btn" id="expBtn">导出 CSV</button>
    <span class="count" id="count"></span>
  </div>

  <section class="pool" id="secPool">
    <div class="sec-h">
      <h2>✅ 当前启用的花字池</h2>
      <span class="tip">每次生成标题时，从这份池里<b>随机挑 1 个</b>花字</span>
    </div>
    <div class="grid" id="gridPool"></div>
    <div class="notice" id="poolNotice"></div>
  </section>

  <section>
    <div class="sec-h"><h2>🔤 标题字体池</h2>
      <span class="tip">与花字叠加使用（在剪映里随机取一款）</span></div>
    <div class="fonts" id="gridFonts"></div>
  </section>

  <section id="secImg">
    <div class="sec-h"><h2>🖼 本地有预览图的花字</h2>
      <span class="tip">从剪映缓存抽出的封面图，只覆盖你打开过的花字</span></div>
    <div class="grid" id="gridImg"></div>
  </section>

  <section>
    <div class="sec-h"><h2>📚 全量花字库</h2>
      <span class="tip" id="allTip"></span></div>
    <div class="grid" id="gridAll"></div>
    <button class="more" id="moreBtn">显示更多</button>
  </section>
</div>

<div class="selbar" id="selbar">
  <span class="nm" id="selName"></span>
  <span class="id" id="selId"></span>
  <span class="x" id="selClose">✕</span>
</div>

<script>
const DATA = /*__DATA__*/;
const rows = DATA.rows;
const favSet = new Set(DATA.fav.map(String));
const byId = {}; rows.forEach(r => byId[r.id] = r);
const PAGE = 120;
let state = { q:'', cat:'', vip:'', onlyImg:false, onlyFav:false, limit:PAGE };

/* 名称 → 稳定色（无预览图时的占位底色） */
function hue(s){ let h=0; for(let i=0;i<s.length;i++) h=(h*31+s.charCodeAt(i))>>>0; return h%360; }
function phStyle(t){
  const h = hue(t);
  return `background:linear-gradient(145deg,hsl(${h} 62% 22%),hsl(${(h+38)%360} 68% 32%));`+
         `color:hsl(${h} 90% 82%)`;
}

function card(r, big){
  const img = r.img ? `<img loading="lazy" src="preview/${r.id}.png" alt="">` :
                      `<div class="ph" style="${phStyle(r.title)}">${(r.title||'?').slice(0,1)}</div>`;
  const tags = [
    r.vip ? '<span class="tag vip">VIP</span>' : '<span class="tag free">免费</span>',
    r.cat && r.cat!=='未分类' ? `<span class="tag cat">${r.cat}</span>` : '',
    r.img ? '<span class="tag img">有图</span>' : '',
    favSet.has(r.id) ? '<span class="tag vip">启用中</span>' : ''
  ].filter(Boolean).join('');
  return `<div class="card${favSet.has(r.id)?' sel':''}" data-id="${r.id}">
    <div class="thumb">${img}</div>
    <div class="meta">
      <div class="t">${r.title}</div>
      <div class="i">${r.id}</div>
      <div class="tags">${tags}</div>
    </div>
  </div>`;
}

function pass(r){
  if(state.vip==='vip' && !r.vip) return false;
  if(state.vip==='free' && r.vip) return false;
  if(state.onlyImg && !r.img) return false;
  if(state.onlyFav && !favSet.has(r.id)) return false;
  if(state.cat && r.cat!==state.cat) return false;
  if(state.q){
    const q = state.q.toLowerCase();
    if(!(r.title.toLowerCase().includes(q) || r.id.includes(q))) return false;
  }
  return true;
}

let filtered = [];
function render(){
  const pool = DATA.fav.map(id => byId[id]).filter(Boolean);
  document.getElementById('gridPool').innerHTML = pool.map(r => card(r,true)).join('') ||
    '<div class="empty">收藏池为空</div>';

  document.getElementById('gridFonts').innerHTML =
    DATA.fonts.map(f => `<span class="fontpill">${f}</span>`).join('') ||
    '<span class="empty">未配置字体池</span>';

  const withImg = rows.filter(r => r.img);
  document.getElementById('gridImg').innerHTML = withImg.map(r => card(r)).join('') ||
    '<div class="empty">暂无本地预览图</div>';

  filtered = rows.filter(pass);
  document.getElementById('gridAll').innerHTML =
    filtered.slice(0, state.limit).map(r => card(r)).join('') ||
    '<div class="empty">没有匹配的花字</div>';
  document.getElementById('moreBtn').style.display =
    filtered.length > state.limit ? 'block' : 'none';
  document.getElementById('moreBtn').textContent =
    `显示更多（已显示 ${Math.min(state.limit, filtered.length)} / ${filtered.length}）`;

  document.getElementById('count').textContent =
    `匹配 ${filtered.length} 条 / 全库 ${rows.length} 条`;
  document.getElementById('allTip').textContent =
    `共 ${rows.length} 条；带图 ${withImg.length} 条；VIP ${rows.filter(r=>r.vip).length} 条 / 免费 ${rows.filter(r=>!r.vip).length} 条`;
  document.getElementById('stats').innerHTML = [
    ['全库', rows.length],
    ['启用池', DATA.fav.length],
    ['有预览图', withImg.length],
    ['字体池', DATA.fonts.length],
  ].map(([k,v]) => `<div class="stat"><b>${v}</b><span>${k}</span></div>`).join('');

  const poolPaths = DATA.pools.map(p =>
    `<div>· <b>${p.project}</b> → <code>${p.huazi_file || '未设置'}</code> ${p.exists?'✅':'⚠️ 文件不存在'}</div>`
  ).join('');
  document.getElementById('poolNotice').innerHTML =
    `<div><b>这份池存在哪：</b><code>assets/favorite_huazi.json</code>`+
    `（数组，可填 <b>花字名称</b> 或 <b>ID</b>；填名称会自动匹配 ID）</div>`+
    `<div style="margin-top:6px"><b>各项目引用：</b></div>${poolPaths}`+
    `<div style="margin-top:8px">想改池子：编辑 <code>favorite_huazi.json</code>，或在控制台里改。改完重新出片即生效。</div>`;
}

/* ── 交互 ── */
const $ = id => document.getElementById(id);
$('q').addEventListener('input', e => { state.q = e.target.value.trim(); state.limit = PAGE; render(); });
$('cat').addEventListener('change', e => { state.cat = e.target.value; state.limit = PAGE; render(); });
$('vip').addEventListener('change', e => { state.vip = e.target.value; state.limit = PAGE; render(); });
$('onlyImg').addEventListener('change', e => { state.onlyImg = e.target.checked; state.limit = PAGE; $('imgChk').classList.toggle('on', e.target.checked); render(); });
$('onlyFav').addEventListener('change', e => { state.onlyFav = e.target.checked; state.limit = PAGE; $('favChk').classList.toggle('on', e.target.checked); render(); });
$('moreBtn').addEventListener('click', () => { state.limit += PAGE; render(); });

$('expBtn').addEventListener('click', () => {
  const head = ['名称','ID','是否VIP','分类','有预览图','在启用池'];
  const lines = [head.join(',')].concat(filtered.map(r =>
    [`"${r.title.replace(/"/g,'""')}"`, r.id, r.vip?'是':'否', r.cat, r.img?'是':'否', favSet.has(r.id)?'是':'否'].join(',')
  ));
  const blob = new Blob(['\ufeff' + lines.join('\n')], {type:'text/csv;charset=utf-8'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = '花字库导出.csv';
  a.click();
  URL.revokeObjectURL(a.href);
});

document.addEventListener('click', e => {
  const c = e.target.closest('.card');
  if(!c) return;
  const r = byId[c.dataset.id];
  if(!r) return;
  $('selName').textContent = r.title;
  $('selId').textContent = r.id;
  $('selbar').classList.add('show');
});
$('selClose').addEventListener('click', () => $('selbar').classList.remove('show'));

/* 分类下拉 */
const catNames = [...new Set(rows.map(r => r.cat))].filter(c => c !== '未分类').sort();
$('cat').innerHTML = '<option value="">全部分类</option>' +
  catNames.map(c => `<option value="${c}">${c}（${rows.filter(r=>r.cat===c).length}）</option>`).join('');

render();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    build()
