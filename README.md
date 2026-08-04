# ai-proofread（本地改造版）

一个中文书稿审校工具集，基于 [Fusyong/ai-proofread](https://github.com/Fusyong/ai-proofread) 改造。

**改造内容**：
- 新增命令行工具 `proofread`，无需 VSCode，终端直接调用
- 新增 **最大化检查模式** `proofread max`，打通项目全部审校环节
- 默认模型切换为 **DeepSeek V4 Flash**（`deepseek-v4-flash`）
- 支持 `.docx` 直接输入（自动转 Markdown）
- 修复多处底层 bug（TGSCC 噪声、模糊匹配、HTML 注入、数字解析、结构层级验证）

---

## 快速开始

### 1. 安装

```bash
cd ai-proofread
pip install -e . --break-system-packages   # macOS Homebrew Python 需此参数
```

安装后 `proofread` 命令全局可用（从任意目录调用）。

### 2. 配置 API Key

在 `src/` 下新建 `.env`：

```txt
# src/.env
DEEPSEEK_API_KEY=sk-你的key
GOOGLE_API_KEY=           # 可选，Google 模型才需要
ALIYPUN_API_KEY=          # 可选，阿里云百炼才需要
```

> ⚠️ 该文件已被 `.gitignore` 忽略，不会提交到仓库。

### 3. 词典（可选，启用专名查词）

将 MDict 格式词典放到任意路径，max 模式的 `--names` 会调用：

```bash
# 推荐路径（max_pipeline.py 中 DICT_PATHS 可改）
/Users/kicker114/Downloads/辞典/常用词典/现代汉语词典第7版/现代汉语词典第7版.mdx
/Users/kicker114/Downloads/辞典/常用词典/汉语辞海.mdx
```

首次查询会自动解包建立 `.db` 缓存（几秒到十几秒），之后秒级查询。

---

## CLI 命令速查

```bash
proofread p  <file.md|docx>              # 单文件快速校对（全文重写）
proofread b  <file.md|docx>              # 全书分块校对（带上下文，异步并发）
proofread m  <file.md|docx>              # ★ 最大化检查（全环节打通）
proofread w  <file.docx>                 # DOCX 修订+批注回写
proofread d  <原稿.md> <校后.md>           # 生成 HTML 词级 diff
proofread s  <file.md>                   # TGSCC 汉字规范专项检查
```

| 参数 | 说明 |
|------|------|
| `--model` | 模型选择：`deepseek-v4-flash`（默认）/ `deepseek-v4-pro` / 旧名 |
| `--concurrent N` | LLM 并发数（book/max 模式，默认 3） |
| `--rpm N` | API 速率限制（默认 15） |
| `--names` | max 模式启用专名查词（需词典） |
| `--no-view` | 不自动打开浏览器 |
| `--no-diff` | 不生成 diff HTML |

### 子命令简写

`p`=proofread `b`=book `m`=max `d`=diff `s`=special

---

## 最大化检查模式（`proofread max`）

一条命令串联全部审校环节，产物：
- `{doc}_refined.md` — 精修版全文（保持原格式，精准句改）
- `{doc}_max_report.html` — 综合报告（聚合全部阶段）
- `{doc}_alignment.html` — 句子级对齐勘误表
- `{doc}_diff.html` — 词级 diff

### 管线各阶段

| 阶段 | 内容 | 引擎 | 说明 |
|------|------|------|------|
| **0a** | 汉字规范 | TGSCC 查表 | 繁体/异体/表外字，确定性 |
| **0b** | 异形词/词形 | 离线词典扫描 | 不规范词形→规范词形 |
| **0c** | 结构检查 | structure_checker | 章节层级 + 编号连续性 |
| **1** | LLM 审校 | DeepSeek JSON 发现模式 | 分块异步并发，只改有问题的句子 |
| **2** | 专名查词（`--names`） | LLM 识别 + MDict 词典 | 人名/地名/机构名核验 |
| **3** | 句子对齐 | 锚点算法 | 原文 vs 精修版逐句对比 |
| **4** | 综合报告 | 聚合渲染 | 全部阶段汇总 |

### 为什么 Phase 1 用「JSON 发现模式」而非「全文重写」

- 全文重写有风险：可能误改译名/署名/专名，且输出大、token 贵
- JSON 发现模式：模型只输出 `[{original_sentence, corrected_sentence}]`，再用 `match_similar_text` 模糊定位回写
- 效果：**精准句改**，保留原文格式与专名，实测 7 处已知错词全部修正、规范词正确保留

---

## 输入文件格式要求

模型只处理文本。支持 `.md`（直接）和 `.docx`（自动转换）。

**Markdown 要求**（决定分块质量）：
1. 标题级别：若干 `#` 后加一个空格
2. 段间空行：一个或连续多个空行表示分段

没有标题时程序随机选择切分位置；段间没有空行可能导致切分过长、效果变差。

**PDF**：建议先转成 HTML/docx 再整理成 Markdown（见下文 pandoc）。

```bash
# docx → markdown（保留脚注、表格）
pandoc -f docx -t markdown-smart+pipe_tables+footnotes \
  --wrap=none --toc --extract-media="./attachments/%name%" \
  %name%.docx -o %name%.md
```

---

## DOCX 修订+批注回写

将审校发现回写到 Word 文档，生成带**字符级修订标记**和**格式化批注**的 `_审阅版.docx`。

**引擎**：**02 引擎（直接 OOXML，默认）**——纯 lxml+zipfile 操作 `word/document.xml`，
按 P 编号定位段落，用 `difflib` 做字符级最小 diff，直接插入 `<w:del>/<w:ins>` 修订
和 `<w:comment>` 批注。零中间文本转换损耗，保留既有修订。

### 用法

```bash
# 独立回写（已有 findings JSON）
proofread w 稿件.docx --findings findings.json

# 一键回写（审校 → 回写一条龙）
proofread m 稿件.docx --writeback --author "审校助手"

# 旧方案（Adeu MCP，需 agent 上下文）
proofread w 稿件.docx --engine adeu
```

### P 编号贯穿（正本清源）

回写可靠性取决于 finding 能否定位到 DOCX 的 P 段落：

```
DOCX → _build_para_text_map() → {P0: 文本, P1: 文本, ...}   # 02 引擎规则，含表格
  ↓
LLM 发现 → _resolve_findings_to_p() → 每条定位到 P 段落
  ↓         （精确子串 → fuzzy → LCS 最长公共子串）
issues[] → 02 引擎 → 按 P 编号在 DOCX 里精确定位段落
  ↓
字符级最小 diff → <w:del>/<w:ins> + <w:comment>
```

### fix_class 路由

| 发现类型 | fix_class | DOCX 行为 | 示例 |
|----------|-----------|-----------|------|
| TGSCC 繁体/异体（单字） | `polish` | **仅批注**，不改文（防误匹配） | "砦"→"寨" |
| 异形词/不规范词形（多字） | `must_fix` | **字符级修订** + 聚焦批注 | "挺而走险"→"铤而走险" |
| LLM 审校发现（有实质修改） | `must_fix` | **字符级修订** + 批注 | "习近平"→"习近平同志" |
| LLM 审校发现（仅提示） | `verify` | 批注不改文 | 待核实项 |
| 结构诊断 | （跳过） | 在 max report 呈现，不写回 DOCX | 章节编号断裂 |

### 命令行参考

```
proofread w  <docx>  [--findings PATH] [--out PATH] [--author NAME] [--engine 02|adeu]
proofread m  <docx>  --writeback [--author NAME]
```

`--findings` 不传时自动搜索同目录 `_max_results.json`。

---

## 核心模块索引

| 模块 | 路径 | 用途 |
|------|------|------|
| CLI 入口 | `src/cli.py` | `proofread` 命令定义 |
| 最大化管线 | `src/max_pipeline.py` | max 模式编排 |
| **DOCX 回写引擎** | `src/writeback_engine.py` | 02 引擎：直接 OOXML 字符级修订+批注 |
| **回写适配** | `src/writeback.py` | Adeu MCP 旧方案（`--engine adeu`） |
| 校对引擎 | `src/proofreader.py` | DeepSeek/Google API 调用，异步并发+断点续跑 |
| 文本切分 | `src/splitter.py` | 按标题/长度切分，带上下文，中文句子切分 |
| 句子对齐 | `src/sentence_aligner.py` | 锚点算法 + Jaccard n-gram |
| HTML 对齐报告 | `src/html_report_v2.py` | 句子级勘误表渲染 |
| TGSCC 汉字 | `src/special_checker/tgscc.py` | 通用规范汉字表检查 |
| 模糊匹配 | `src/special_checker/match_similar_text.py` | LLM 修正回写定位 |
| 结构检查 | `src/structure_checker/` | 层级 + 编号连续性（已补 hierarchy_gap） |
| 词典查询 | `src/special_checker/mdict.py` | MDict 查询，`query_mdx(mdx, word)` |
| 词级 diff | `src/diff_tools.py` | HTML 词级差异（已修转义） |

---

## 配置词典（MDict）

`query_mdx(mdx_path, word)` 可直接查询任意 MDict 词典。max 模式 `--names` 依赖：

```python
from src.special_checker.mdict import query_mdx
result = query_mdx("/path/to/词典.mdx", "仿佛")   # 首次解包建 .db 缓存
```

---

## 模型信息

- 默认：**`deepseek-v4-flash`**（DeepSeek V4 Flash）
- 可选：`deepseek-v4-pro`（更强推理）
- 旧名 `deepseek-chat` / `deepseek-reasoner` 已弃用但保留兼容（映射到 v4-flash）
- `temperature` 默认 1.3（校对推荐值；0 太低会减少改动，创意场景可到 1.5）

Token 估算：1 中文字符 ≈ 0.6 token，1 英文字符 ≈ 0.3 token。

---

## 常见问题

**Q: `pip install -e .` 报 PEP 668 错误？**
macOS Homebrew Python 需加 `--break-system-packages`。

**Q: 报 `ModuleNotFoundError: No module named 'src'`？**
需在项目根目录运行，或先 `pip install -e .`。

**Q: max 模式 Phase 1 返回空？**
检查 `src/.env` 的 `DEEPSEEK_API_KEY` 是否配置正确，网络能否访问 `api.deepseek.com`（需在代理 NO_PROXY 白名单或关闭代理）。

**Q: 词典查询很慢？**
首次会解包建 `.db`，之后走缓存。词典文件权限不足（`chmod`）也会导致问题。

**Q: 报告里的对齐勘误表打不开？**
`alignment.html` 是自包含单文件，双击用浏览器打开即可；jsdiff 高亮依赖 CDN（离线时降级为纯文本）。

---

## 许可

本 fork 保留原作者双许可：
- 提示词（the prompts）：**CC BY-SA 4.0**
- 其余部分（the others）：**MIT License**

原作者：[Fusyong/ai-proofread](https://github.com/Fusyong/ai-proofread)，原始 VSCode 插件：[ai-proofread-vscode-extension](https://github.com/Fusyong/ai-proofread-vscode-extension)
