# jianying-mix-console · 剪映混剪控制台

把「多段素材 + BGM + 标题」批量装配成剪映草稿，一键批量导出成片。
面向多店面运营：每个店面一套独立的素材池、剪辑配方、BGM 曲库、**文案库**、去重历史。

## 快速开始

```bash
bash scripts/setup.sh     # 首次：建 venv + 装依赖 + 拉引擎
bash scripts/run.sh       # 启动 → http://127.0.0.1:8766
```

跑之前请 **⌘Q 完全退出剪映**（剪映运行时生成会产生重复的草稿索引条目）。

## 目录结构

```
jianying-mix-console/
├── console.py            # Web 控制台后端（FastAPI，端口 8766）
├── console.html          # 控制台前端（单文件，无构建）
├── generator.py          # 出片核心：卡点 / 拼接 / 标题 / 体检
├── titlelib.py           # 文案库轮子（解析 / 筛选 / 计数 / 导入导出，纯标准库）
├── gen_project.py        # 命令行出片入口（PROJECTS 定义各店面）
├── beats.py              # BGM 卡点检测
├── fix_draft_index.py    # 剪映草稿索引修复（去重 / 重建）
├── gen_huazi_viewer.py   # 生成离线「花字库」浏览页
├── assets/               # 数据资产（进版本库）
│   ├── huazi.json            # 全量花字库 1673 条
│   ├── huazi_categories.json # 花字分类
│   ├── favorite_huazi.json   # 【启用池】出片时随机挑 1 个
│   ├── favorite_fonts.json   # 字体池
│   ├── titles.txt            # 【默认文案库】每行一条，格式：[类型] 第一段|第二段
│   ├── huazi_library/preview # 花字封面图（从剪映缓存抽取）
│   └── composite_ref.json    # 复合片段结构模板
├── projects/<店面>/spec.json # 每个店面的规则与路径（进版本库）
├── scripts/                  # setup / run / fetch_engine
├── tools/                    # 素材预处理脚本
├── docs/                     # 验收报告、文案原稿等
└── engine/                   # 引擎依赖（不进 git，setup.sh 自动拉取，2.7MB）
```

## 店面配置（`projects/<店面>/spec.json`）

| 字段 | 说明 |
|---|---|
| `name_prefix` | 草稿命名前缀，如 `鱼杂混剪_0911跃杨` |
| `pool_dirs` | 素材池目录（可多个，递归扫描） |
| `bgm_dirs` | BGM 曲库目录（可多个；兼容旧的单个 `bgm_dir`） |
| **`titles`** | **该店面的文案库文件路径**（每行一条，`第一段\|第二段`） |
| `huazi_file` / `font_file` | 该店面的花字池 / 字体池 |
| `history_path` | 该店面的去重历史（跨批次不重题） |
| `shot_recipes` | 镜头配方（每段钉死一个素材类别） |
| `exclusive_adjacent` | 排他类别（相邻两条最多一条含该类别） |
| `beat_sync` | 是否按 BGM 卡点 |
| `slots` / `shot_dur` | 段数 / 单段时长 |

> 路径支持 `~`。默认文案库是 `assets/titles.txt`；想让某店面用不同的文案库，
> 改它的 `titles` 指向别的 txt 即可（控制台里也能改）。

## 文案库（v2）

- 格式：纯文本，**每行一条**，形如 `[远道而来] 骑行20公里|吃一口肥肠鱼杂`
  - `[类型]` 是**可选标签**，只用于分类和筛选，**不会出现在成片标题里**（出片前自动剥掉）。
  - `|` 把文案分成两段显示（第一段 / 第二段）；只有一段就不写 `|`。
  - 空行忽略、完全相同的行自动去重。类型缺省为「未分类」。
- 载体：核心逻辑在 `titlelib.py`（纯标准库轮子），`console.py` 与 `generator.py` 共用。
- 去重：按 `history_path` 记录已用过的文案，优先挑**用得最少**的，跨批次不重复。
- **每个店面各自一个文案库文件**，互不干扰；新店面默认先用**共享默认库**
  `assets/titles.txt`，之后可单独改写。

### 在控制台里改（侧栏「📝 文案库」）

页面有两个标签页：

**📚 编辑文案库** —— 对库本身做增删改

| 操作 | 说明 |
|---|---|
| 改类型 | 每行左侧下拉选类型（也可输入新类型），选中即生效 |
| 改文案 | 每行文本框直接改，失焦保存 |
| 🗑 删除 | 删除单条 |
| ＋ 新增一条 | 表尾新增空行 |
| 导出 / 导入 | 导出为 txt（可选带注释 `# 时间 / 来源`）；导入支持 覆盖 / 追加 / 合并 三种模式 |
| 使用次数 | 每行显示 `全局次数 / 本店次数`，由出片历史自动统计（只读，可「重算」） |
| 📁 选择已有文件 | 系统对话框挑一个 `.txt` 作为本店文案库（只改指向，不动内容） |
| 另存为本店专用 | 把当前编辑内容存成 `assets/<名字>.txt`，本店切过去，与别人隔离 |
| ↺ 恢复默认库 | 切回共享默认库 `assets/titles.txt` |

**🎯 本店选用哪些** —— 决定本店出片时用库里哪些

| 模式 | 说明 |
|---|---|
| 用全部（默认） | 库里所有文案都参与（默认行为） |
| 按类型排除 | 勾掉不要的类型（如本店不要「喝酒聚餐」类） |
| 单条勾选 | 在列表里逐条勾选，只出勾中的；文案按**内容**记身份，库里排序/插行都不影响选择 |
| 保存 / 重置 | 底部浮动条保存到 `spec.title_filter`；重置=回到「用全部」 |

> ⚠ **共享默认库保护**：若本店仍指向 `assets/titles.txt`，直接保存会改到**所有**用它的店面。
> 此时保存按钮会先变红要求二次确认；想只改本店，请用「另存为本店专用」。

### BGM 使用次数

已合并进「曲目清单」：每首右侧直接显示「**本店 N 次 · 全局 M 次**」，清单标题另给
「本店累计用 N 次」。数字来自出片历史（不含独立计数器，数字即真实使用量），
用来判断哪些 BGM 用得太勤、需要补新曲。

### 接口（控制台后端）

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/copy?key=&lib=` | 文案库全量：条目（类型/文案/全局次数/本店次数）、类型汇总、总数/已用/未用/漂移数、当前筛选、有效条数 |
| POST | `/api/copy` | 整库覆盖保存（原子写 + `.bak-时间戳` 备份） |
| GET | `/api/copy/export` | 导出 txt：`with_type` 带不带类型前缀、`only_used` 只导用过的、`types` 按类型筛 |
| POST | `/api/copy/import` | 导入：`mode` = replace / append / merge |
| GET/POST | `/api/stores/{key}/copy_filter` | 读取 / 保存本店选用规则（`exclude_types` / `only` / `exclude`） |
| GET | `/api/stores/{key}/copy` | 返回本店文案库指向信息（`path / rel / is_default / count` 等） |
| POST | `/api/stores/{key}/copy` | 换文件 / 另存为新文件（只改指向；内容改动走 `/api/copy`） |
| GET | `/api/bgm/usage?key=` | BGM 历史使用次数（按次数降序）。控制台界面已改为在「曲目清单」里直接显示累计次数，此接口保留给脚本调用 |
| GET | `/api/pick_file?prompt=` | macOS 原生「选择文件」对话框（浏览器拿不到本地绝对路径） |
| POST | `/api/locate_dropped` | 把**拖进页面的文件夹 / 文件名字**在本机定位成绝对路径（浏览器拖拽拿不到绝对路径，故按名字回搜） |
| GET | `/api/stores/{key}/backups` | 最近 10 份 `spec.json.bak-*` + 与当前 spec 有哪些字段不同 |
| POST | `/api/stores/{key}/restore` | 回退到指定备份（先备份当前版本，可再退回来） |
| GET/POST | `/api/ai/config` | 读/写 DeepSeek 配置（Key 存本机 `projects/_ai.conf`，已 gitignore） |
| POST | `/api/stores/{key}/ai_rules` | 让 AI 按一句自然语言要求改写配方并直接写入 |

## 出片流程

1. 控制台选店面 → 确认素材池 / BGM / 文案库
2. 「干跑」看体检指标（全绿再正式出）
3. 「正式生成」（后台任务，页面可轮询进度）
4. 打开剪映 → 草稿列表 → 批量导出

体检口径：片内相邻同类 / 片内同文件复用 / 相邻边界同类 / 片间同文件重叠 —— 这四项应为 0。

## 依赖

- Python 3.13（`.venv`），依赖见 `requirements.txt`
- 系统命令：`ffmpeg` / `ffprobe`（`brew install ffmpeg`）
- 引擎：`engine/`，由 `scripts/fetch_engine.sh` 从 `jianying-batch-drafts` skill 复制
- 剪映：草稿目录 `~/Movies/JianyingPro/User Data/Projects/com.lveditor.draft`

## 备注

- 原 `app.py`（端口 8765 的早期单店面控制台）已被 `console.py` 取代，未纳入本工程。
- macOS 桌面版启动器（双击 `.app`）在 `~/Desktop/06 工具与脚本/视频混剪控制台/`。
