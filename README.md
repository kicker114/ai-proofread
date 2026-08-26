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

```zsh
cd ai-proofread
pip install -e . --break-system-packages   # macOS Homebrew Python 需此参数
```

安装后 `proofread` 命令全局可用（从任意目录调用）。

### Codex 项目入口

在 Codex 中打开本仓库后，根目录 `AGENTS.md` 会自动把 Word/PDF 审校请求路由到
项目 Skill：`$ai-proofread`。入口只在本仓库生效，不需要 OfficeCLI、docx-mcp
或 Adeu MCP。

```text
$ai-proofread 用 pipeline 模式审校 /绝对路径/稿件.docx
$ai-proofread 用 codex-native 模式审校 /绝对路径/书稿.pdf，并联网核验事实项
```

- `pipeline`（默认）：复用 DeepSeek max 流水线；调用模型 API，但不等同于实时联网检索。
- `codex-native`：Codex 读取带位置的审校源文件，按项目规则生成 findings；联网核验时把来源写入批注。
- 两种模式最终都调用同一套本地 02 OOXML / PyMuPDF 写回引擎，且不覆盖原文件。

长文的 `codex-native` 审校按连续位置分批，每批最多 40 个单元且约不超过 12,000
个非空白字符；批次 checkpoint 和最终覆盖审计用于证明每个 `P<n>` / 页码均已审阅。

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

```zsh
# 推荐路径（max_pipeline.py 中 DICT_PATHS 可改）
/Users/kicker114/Downloads/辞典/常用词典/现代汉语词典第7版/现代汉语词典第7版.mdx
/Users/kicker114/Downloads/辞典/常用词典/汉语辞海.mdx
```

首次查询会自动解包建立 `.db` 缓存（几秒到十几秒），之后秒级查询。

---

## CLI 命令速查

```zsh
proofread p  <file.md|docx>              # 单文件快速校对（全文重写）
proofread b  <file.md|docx>              # 全书分块校对（带上下文，异步并发）
proofread m  <file.md|docx>              # ★ 最大化检查（全环节打通）
proofread w  <file.docx>                 # DOCX 修订+批注回写
proofread x  <file.docx|pdf>             # 导出 Codex 位置化审校源 JSON
proofread d  <原稿.md> <校后.md>           # 生成 HTML 词级 diff
proofread s  <file.md>                   # TGSCC 汉字规范专项检查
```

| 参数 | 说明 |
|------|------|
| `--model` | 模型选择：`deepseek-v4-flash`（默认）/ `deepseek-v4-pro` / 旧名 |
| `--concurrent N` | LLM 并发数（book/max 模式，默认 8） |
| `--rpm N` | API 速率限制（默认 15） |
| `--chunk-size N` | max 分块目标字数（默认 200；质量回归通过前不要改默认） |
| `--names` | max 模式启用专名查词（需词典） |
| `--no-view` | 不自动打开浏览器 |
| `--no-diff` | 不生成 diff HTML |

### 子命令简写

`p`=proofread `b`=book `m`=max `w`=writeback `x`=extract `d`=diff `s`=special

### Codex-native 交换格式

```zsh
proofread extract 稿件.docx --out 稿件_review_source.json
proofread extract 书稿.pdf --out 书稿_review_source.json
```

审校源使用 `ai-proofread.source.v1`，包含原文件 SHA-256，以及 Word 的 `P<n>`
或 PDF 的一基页码。Codex 产出的 `ai-proofread.findings.v1` 使用以下核心字段：
`fix_class`、`current`、`suggested`、`reason`、`category`，联网依据可放入
`evidence[{title,url,accessed_at}]`。规范数组名是 `issues`；PDF 每条 issue 必须有
一基整数 `page`。写回会校验源文件哈希，拒绝把旧 findings 应用到另一版本。

---

## 最大化检查模式（`proofread max`）

一条命令串联全部审校环节，产物：
- `{doc}_refined.md` — 精修版全文（保持原格式，精准句改）
- `{doc}_max_results.json` — 全阶段 findings（Word 来源时含源 SHA-256）
- `{doc}_max_report.html` — 综合报告（**V3 深色主题**：原文内嵌红色高亮错误词、
  按章节分组、严重级别徽章、建议与说明）
- `{doc}_alignment.html` — 句子级对齐勘误表

### 管线各阶段

| 阶段 | 内容 | 引擎 | 说明 |
|------|------|------|------|
| **0a** | 汉字规范 | TGSCC 查表 | 繁体/异体/表外字，确定性 |
| **0b** | 异形词/词形 | 离线词典扫描 | 不规范词形→规范词形 |
| **0c** | 结构检查 | structure_checker | 6 类标题体系层级 + 编号连续性 |
| **1** | LLM 审校 | DeepSeek JSON 发现模式 | 分块异步并发，只改有问题的句子 |
| **2** | 专名查词（`--names`） | LLM 识别 + MDict 词典 | 人名/地名/机构名核验 |
| **3** | 句子对齐 | 锚点算法 | 原文 vs 精修版逐句对比 |
| **4** | 综合报告 | 聚合渲染 | 全部阶段汇总 |

### 为什么 Phase 1 用「JSON 发现模式」而非「全文重写」

- 全文重写有风险：可能误改译名/署名/专名，且输出大、token 贵
- JSON 发现模式：模型只输出 `{"findings": [{original_sentence, corrected_sentence}]}`（对象包装兼容多 provider 的 `json_object` 模式），再用 `match_similar_text` 模糊定位回写；支持 `--failover-models` 多 provider 自动切换
- 效果：**精准句改**，保留原文格式与专名，实测 7 处已知错词全部修正、规范词正确保留

---

## 输入文件格式要求

模型只处理文本。支持 `.md`（直接）和 `.docx`（自动转换）；旧 `.doc` 需先转换为
`.docx`。PDF 必须带文字层，纯扫描件需先 OCR。

**Markdown 要求**（决定分块质量）：
1. 标题级别：若干 `#` 后加一个空格
2. 段间空行：一个或连续多个空行表示分段

没有标题时程序随机选择切分位置；段间没有空行可能导致切分过长、效果变差。
分块器现在对无空行连续文本按句子边界（`。！？；…`）硬切，避免退化成超大块。

**.docx（含 altChunk 格式）**：除标准 `w:p` 段落外，原生支持 PDF→Word 导出 /
腾讯文档产出的 **altChunk（内嵌 MHT）** 格式——正文嵌在 `word/*.mht` 里、没有
`w:p`。`extract_source` 提供共享 MHT 解析器（multipart + quoted-printable/base64 +
字符集）与 `docx_uses_altchunk_body` 判定，DOCX→MD、P 编号映射、02 引擎回写
三处消费方 P 编号严格一致，可直接 `proofread m my.docx --writeback` 得到完整修订+
批注的 `_审阅版.docx`。旧版依赖 LibreOffice 中转，现已免中转。

**PDF**：原生支持。`src/pdf_pipeline.py pdf2md` 直接转 Markdown + 审校后 PyMuPDF 高亮批注回写（见下方「PDF 审校工具链」）。

```zsh
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
和 `<w:comment>` 批注。引擎只拆分命中的文本节点，保留未命中 run、超链接、表格及
既有修订；字段、超链接、受保护结构、错误 P 位置或模糊定位不会跨段强改，而是跳过
或降级为待核批注。过大的扩写同样受振幅门禁保护。

批注高亮已收束到字符级：框选只覆盖 `current↔suggested` 的实际差异块（`_min_change_span`），
而非整个长句；批注锚定范围仍覆盖整段原文，二者解耦，避免字符级修订回退。写入前
`_xml_safe` 会剥离 XML 1.0 非法控制字符，防止 LLM 发现里夹带控制字符导致写回崩溃。

### 用法

```zsh
# 独立回写（已有 findings JSON）
proofread w 稿件.docx --findings findings.json

# 旧版无哈希 findings 必须绑定 proofread extract 生成的源清单
proofread w 稿件.docx --findings old_max_results.json \
  --source-manifest 稿件_review_source.json

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

批注正文为「`▶ 建议`（校正后）+ `◎ 依据`（修改理由）」。LLM 发现不带 `reason`
（JSON 发现 prompt 禁止解释），因此 `◎ 依据` 不再回落到整句原文、避免与 `▶ 建议`
重复引述；异形词发现的依据取自词典来源（`basis`，如「现代汉语词典/异形词表」）。

### 命令行参考

```
proofread w  <docx>  [--findings PATH] [--out PATH] [--author NAME] [--engine 02|adeu]
proofread m  <docx>  --writeback [--author NAME]
```

`--findings` 不传时自动搜索同目录 `_max_results.json`。新版 Word max 结果自带源哈希；
旧版无哈希结果必须传 `--source-manifest`。`--out` 始终另存；输出完成后会审计
DOCX ZIP/XML、修订文本、批注 ID 和包关系，失败或全部发现均无法定位时命令返回非零状态。

---

## 性能与成本（长稿实测 2026-08-06）

Phase 1（LLM JSON 发现模式）是唯一耗时/成本瓶颈：16 万字书稿在默认
`--concurrent 3 --rpm 15` 下耗时 **~3 小时**，实测 token 消耗约 235 万
（输入 94% / 输出 6%），成本约 ¥5。

**当前安全默认：**

```zsh
proofread m 书稿.md --no-view --concurrent 3 --rpm 15 --chunk-size 200
```

- max Phase 1 每块结果会写入源哈希、模型、JSON prompt 哈希、chunk-size 和 chunk 哈希绑定的原子 checkpoint；中断后只补失败块，合法 `issues: []` 不会重跑。
- SDK 隐式重试已关闭，请求上限 300 秒；每个逻辑请求最多初次请求加两次显式重试，并记录限速等待、请求时间、空响应和无效 JSON。
- `max` 会输出阶段 wall time、实际调用数和 checkpoint 命中数。不要在未经质量回归前把并发或 RPM 提高到 8/60。
- 三处 token 优化已内置：`max_tokens=4096`（JSON 发现模式）、context 裁剪
  （每块只带章节标题 + target 前后 800 字，token 降 38%）、无空行句子硬切。
- 详情见 `CLAUDE.md` 的 Performance tuning 章节。

---

## PDF 审校工具链

`src/pdf_pipeline.py` 提供完整的 PDF 审校链路，无需手动 pandoc 转换：

```zsh
# 1. 生成页码化源文件清单与 SHA-256
proofread extract book.pdf --out book_review_source.json

# 2. PDF → Markdown（pymupdf4llm，提取文本层）
python3 src/pdf_pipeline.py pdf2md book.pdf --out book.md

# 3. 走 max 管线审校
proofread m book.md --no-view

# 4. dry-run 预览批注命中率
python3 src/pdf_pipeline.py annotate book.pdf book_max_results.json \
  --source-manifest book_review_source.json --dry-run --csv preview.csv

# 5. 正式回写 PDF 高亮批注（默认只应用唯一 exact 命中）
python3 src/pdf_pipeline.py annotate book.pdf book_max_results.json \
  --source-manifest book_review_source.json --author "AI审校"
# → book_审阅版.pdf + book_批注清单.csv
```

`pdf2md` 会逐页比较 pymupdf4llm 与 PyMuPDF 原始文字层的文字保留率，而非只比较长度。
当某页低于 75% 时自动改用该页 `get_text("text", sort=True)`，并打印降级页码与
覆盖率，避免复杂图文排版静默漏掉大段正文。

### annotate 三层定位策略

PyMuPDF 的 `page.search_for()` 无法跨行匹配。本工具采用三层回退：

| 层级 | 策略 | 说明 |
|------|------|------|
| `exact` | 字符级全文本索引 | `rawdict` 字符 bbox 生成逐行 quads；只有唯一命中才自动写入 |
| `fragment` | 最长纯片段回退 | dry-run 默认只报告；人工确认后用 `--allow-fragment` 写入 |
| `fuzzy` | rapidfuzz 滑动窗口 | 默认只预览；并列最高分始终 ambiguous，显式允许也不写入 |

重复原文会枚举所有候选；只有 findings 提供的 `page` 能唯一消歧时才写入，否则 CSV
记录 `ambiguous`。CSV 同时记录匹配方法、真实分数、候选页、quad 数和跳过原因。
批注颜色按 fix_class 区分：`must_fix`=amber，`polish`=浅黄，`verify`=浅蓝，`tgscc`=浅红。

pipeline 的 legacy `max_results.json` 可能没有源 PDF 哈希，因此 PDF dry-run 和正式
写回都必须传入 `proofread extract` 生成的 `--source-manifest`。输出 PDF 经重开审计
通过后才原子替换既有审阅版。

> 仅支持带文字层 PDF。纯扫描 PDF 需先 OCR；混合 PDF 的无文字层页面会明确列出并跳过。

### WorkBuddy 入口

项目附带 WorkBuddy Agent 定义（`.workbuddy/agents/pdf-proofreader.md`），WorkBuddy 可直接驱动上述全流程。

---

## 核心模块索引

| 模块 | 路径 | 用途 |
|------|------|------|
| CLI 入口 | `src/cli.py` | `proofread` 命令定义 |
| Codex 提取 | `src/extract_source.py` | DOCX/PDF → 带位置与 SHA-256 的审校源 |
| 最大化管线 | `src/max_pipeline.py` | max 模式编排 |
| **DOCX 回写引擎** | `src/writeback_engine.py` | 02 引擎：直接 OOXML 字符级修订+批注 |
| **回写适配** | `src/writeback.py` | Adeu MCP 旧方案（`--engine adeu`） |
| **PDF 工具链** | `src/pdf_pipeline.py` | PDF→MD 转换 + PyMuPDF 高亮批注回写 |
| 校对引擎 | `src/proofreader.py` | DeepSeek/Google API 调用，异步并发+断点续跑 |
| 文本切分 | `src/splitter.py` | 按标题/长度切分，带上下文，中文句子切分 |
| 句子对齐 | `src/sentence_aligner.py` | 锚点算法 + Jaccard n-gram |
| HTML 对齐报告 | `src/html_report_v2.py` | 句子级勘误表渲染 |
| TGSCC 汉字 | `src/special_checker/tgscc.py` | 通用规范汉字表检查 |
| 模糊匹配 | `src/special_checker/match_similar_text.py` | LLM 修正回写定位 |
| 结构检查 | `src/structure_checker/` | 6 类标题体系（章/节/目、部编卷、中文序号、括号序号、数字顿号、多级数字、特殊部分）+ 编号连续性 |
| 词典查询 | `src/special_checker/mdict.py` | MDict 查询，`query_mdx(mdx, word)` |
| 词级 diff | `src/diff_tools.py` | HTML 词级差异（已修转义） |

---

## 回归测试

```zsh
python3 -m unittest \
  tests.test_extract_source \
  tests.test_codex_entry \
  tests.test_pdf_pipeline \
  tests.test_word_writeback \
  tests.test_altchunk \
  tests.test_splitter_context \
  tests.test_network_resume \
  tests.test_skip_visibility \
  tests.test_book_path \
  tests.test_structure_scanner

# 确定性阶段回归（无需 API，秒级）：samples/审校合成稿.md 固定样本
python3 samples/validate_synthetic.py
```

测试覆盖 Codex Skill、DOCX 表格提取、findings 哈希门禁、Word OOXML 修订批注、
PDF quads/歧义/降级策略、**altChunk（MHT 嵌入）DOCX 全管线**、**分块 context
裁剪**、**网络重试/checkpoint 续跑**、**跳过发现可见性**、**book 路径可靠性**、
**结构检查器 6 类标题体系**以及输入文件防覆盖。旧的 `test_performance.py` 和
`test_two_stage.py` 仍引用已移除的 `src.sentence_aligner_simple`，不属于当前回归门。

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
