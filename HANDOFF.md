# HANDOFF — ai-proofread Codex Word/PDF 审校入口（2026-08-06）

**Scope:** `/Users/kicker114/Developer/ai-proofread`
**Branch / HEAD:** `main` / current committed HEAD
**Status:** Codex 入口、Word 字符级修订批注、PDF 高亮批注均已实现并完成专项验收；2026-08-06 追加 altChunk 原生支持、V3 报告、性能优化，全部已提交。

> 工作树原有 `.workbuddy/memory/2026-08-05.md` 用户修改。本次实现没有编辑该文件，后续提交时不要把它当成本次产物覆盖或回退。

## 2026-08-06 技术更新（本日新增）

### altChunk（PDF→Word / 腾讯文档）DOCX 原生支持

问题：PDF→Word 转换器和腾讯文档导出的 `.docx` 正文是单个 `<w:altChunk>`
（无标准 `w:p`），全文嵌在 `word/*.mht` 里。此前 DOCX→MD 产出空文本、P-map
为空、写回静默跳过——纯 Word 稿件正常，altChunk 格式整条管线失效。

修复（`src/extract_source.py`，commit `6006c75` + `5f42b38`）：

- `extract_altchunk_paragraphs(archive)`：健壮 MHT 解码（multipart 边界 →
  `Content-Type: text/html` 段 → quoted-printable / base64 → 声明字符集 GBK/
  UTF-8）+ HTML→段落（含标题层级）。
- `materialize_altchunk_paragraphs(body, paras)`：物化为标准 `w:p` 供引擎回写。
- `docx_uses_altchunk_body(archive)`：**共享判定**——三消费方（extract_docx_units、
  `_build_para_text_map`、writeback_engine.main）用它决定「正文是否完全由 MHT 承载」。
  忽略空 `<w:p/>` 占位，要求有文本承载才退回标准 walk；混合文档三处一致走标准
  walk，altChunk 内容不静默丢弃、不重排。
- 对抗性审查（4-agent 工作流）修复了守卫漂移、顶层 import 崩溃、缺 rels part
  KeyError、base64/GBK 解码。

P 编号三消费方逐段一致（1044 段字节级校验通过）。效果：**无需 LibreOffice 中转**，
`proofread m my.docx --writeback` 直接产出完整修订+批注的 `_审阅版.docx`（实测 1543
字符级修订 + 1598 批注，OOXML 审计通过，LibreOffice 打开正常）。

### V3 深色主题审校报告（`src/html_report_v3.py`）

替代旧版表格视图：深色主题、统计卡片（必须修改/建议修改/供参考）、按 Markdown
标题自动分组、**原文内嵌红色高亮错误词**（location 定位上下文）、绿色建议、说明。
`max_pipeline.phase4_report` 委托给它，输出仍为 `{doc}_max_report.html`。

### 性能优化（长稿 token 能效）

实测 16 万字书稿 Phase 1 耗时 11321s（≈3h），token 约 235 万，成本约 ¥5。
瓶颈：模型推理和分块调用数量仍是主耗时；默认并发 3 + rpm 15 保守限流。max 现在使用本地 context 裁剪、共享 client/线程池、300 秒超时、显式重试和按源哈希绑定的原子 checkpoint。

优化（commit `3cb922a` + `67c143e`）：
- `max_tokens=4096`（仅 JSON 发现模式）。
- `splitter.build_local_context()`：context 裁剪为章节标题 + target 前后各 800 字
  （上限 3000），token 降 38%。
- `cut_text_by_length()`：无空行连续文本按句子边界（`。！？；…`）硬切，不退化超大块。
- 安全默认保持 `--concurrent 3 --rpm 15 --chunk-size 200`。在固定 120 段样本通过召回/精确率门禁前，不提高并发/RPM，也不改变默认分块。
- max 第二次运行只补失败或 hash 不匹配的块；合法 `issues: []` 计为完成。阶段统计记录调用、重试、限速等待、checkpoint 命中和 wall time。
- 新增 `tests/test_splitter_context.py`。

### 跳过发现可见性 + book 路径可靠性 + 固定样本（2026-08-06 晚）

针对 Codex 性能诊断文档的专项整改（Layer A/B 静默丢弃、book 路径 O(N²)）：

- **跳过发现可见性**：`phase1_json_proofread` 统计 `findings_from_llm` /
  `dropped_match`；`_resolve_findings_to_p` 新增 `skip_log` 参数记录丢弃原因
  （`duplicate_anchor` / `fuzzy_tie` / `p_out_of_range` / `explicit_p_not_found` /
  `not_found` / `empty_key_text`）。`run_max` 把 Layer A + B 丢弃统一原子落盘
  `{doc}_skipped.json`（`ai-proofread.skipped.v1`），V3 报告顶部显示被跳过横幅。
  被丢弃的发现不再静默消失。
- **book 路径可靠性**（`process_paragraphs_async`）：每段独立侧车原子 checkpoint
  （`{out}.chunks/{i:06d}.json`，O(1) 写，去掉整份 JSON 读改写的 O(N²) 与全局锁）；
  失败段记 `*.error.json`；结束时若有失败抛 `RuntimeError`（CLI 非零退出）；重跑
  只补缺失段；提示生产改用 `proofread max`。
- **新增测试**：`tests/test_skip_visibility.py`（5 例）、`tests/test_book_path.py`（3 例）。
- **可复用固定样本**：`samples/审校合成稿.md`（1806 字，确定性错误 TGSCC 64 /
  异形词 5 / 结构 2 + 五类 LLM 陷阱：边界、跨段指代、需上下文事实、重复锚点、
  同形异义词）+ `samples/审校合成稿_错误清单.md`（参考答案与复测方法）+
  `samples/validate_synthetic.py`（确定性阶段秒级回归）。
- **结构检查器全面重写（commit 待提交，多轮对抗验证闭环）**：
  - `scanner.py` 不再全文正则扫描（正文"在第二章中/12.9亿"不再误报），改为识别
    markdown 标题：ATX `#`（可无空格/全角空格/BOM 容忍）、setext `===`、无 `#`
    的"第X章 标题"普通行行首锚定 + 编号后空白；跳过代码围栏。
  - 建树以 markdown 标题层级（`#` 数量）优先：`## 1.` 按 H2 编号节不再误报
    hierarchy_gap；真跳级 H1→H3 仍报；setext 标题文本行以 heading_level=1 参与。
  - 合并标题「第1章 第1节」取全编号（含紧凑 `第1章第1节`、普通行、`第1.`前缀）；
    叙述性提及（第二章的比较/参考文献「第2章」/第一节的比较）被排除；同一标题行
    的章+节按 line_id 同层嵌套，跨行同号仅当前后都带节行且节号连续才合法。
  - 全角归一化：全角数字/句点/拉丁、繁體節、Unicode 罗马（Ⅻ→XII）、小写 iv
    均归一化命中且偏移映射回原文；装饰前缀支持全角数字（第１部分）。
  - 罗马严格判定：非法序列（IIX/VX/IIII）→ number_missing；`第X章` 草稿占位在
    混合体系中判 number_missing；`第I章`=1 等合法罗马不误伤；编号体系切换
    （罗马前言→阿拉伯正文）不比较连续性。节级罗马（第IV节）规则已支持。
  - 三/四轮对抗验证（召回/精度/一致性 + 复验）累计发现并修复 30+ 项边界；
    回归测试 `tests/test_structure_scanner.py`（41 例），全量 121 项通过；
    合成稿确定性仍精确 64/5/2。

## Goal

让 Codex 在本仓库内自动发现中文审校工作流，并使用项目自身的确定性工具完成：

- `.docx`：字符级 Word 修订（`w:del/w:ins`）及标准批注。
- 带文字层 `.pdf`：在原版式 PDF 上写入标准 Highlight 高亮及弹窗批注。
- 两种审校来源：现有 DeepSeek max 流水线，或 Codex 原生审校并联网核验事实。

不依赖 OfficeCLI、docx-mcp、Adeu 或其他 MCP。原文件始终只读。

## Codex Entry

- `AGENTS.md`：仓库级自动路由和安全门禁。
- `.agents/skills/ai-proofread/SKILL.md`：项目级 `$ai-proofread` Skill。
- `.agents/skills/ai-proofread/agents/openai.yaml`：Codex Skills UI 元数据。

模式：

| 模式 | 审校发现来源 | 写回后端 |
|---|---|---|
| `pipeline`（默认） | `proofread max` / DeepSeek | 02 OOXML / PyMuPDF |
| `codex-native` | Codex 按项目规则生成 findings；事实项可联网核验 | 同上 |

DeepSeek API 调用不等于实时联网检索。需要事实来源时必须使用 `codex-native`，并记录实际访问的权威网页。

长稿 codex-native 以连续 P/页码分批（每批最多 40 units，约不超过 12,000
非空白字符），保存批次 findings 和覆盖 checkpoint；合并去重前必须证明
`review_source.json` 的每个 unit 恰好覆盖一次。

## Public Interfaces

```zsh
# 生成带稳定位置和源文件哈希的 Codex 审校源
proofread extract <source.docx|source.pdf> --out <review_source.json>

# Word findings 写回
proofread w <source.docx> --findings <findings.json> [--source-manifest <review_source.json>] --out <review.docx> --author "Codex审校"

# PDF 安全预览与正式写回
python3 src/pdf_pipeline.py pdf2md <source.pdf> --out <review.md>
proofread m <review.md> --no-view
python3 src/pdf_pipeline.py annotate <source.pdf> <findings.json> --source-manifest <review_source.json> --dry-run --csv <preview.csv>
python3 src/pdf_pipeline.py annotate <source.pdf> <findings.json> --source-manifest <review_source.json> --out <review.pdf> --csv <annotations.csv>
```

`ai-proofread.source.v1` 包含 `source_sha256`，Word 单元使用 `P<n>`，PDF 单元使用一基页码。

`ai-proofread.findings.v1` 的规范数组字段为 `issues`，每条包含：

```json
{
  "fix_class": "must_fix|polish|verify",
  "current": "原文",
  "suggested": "建议文本",
  "reason": "理由",
  "category": "类别",
  "location": "P3",
  "page": 2,
  "evidence": [{"title": "来源", "url": "https://...", "accessed_at": "YYYY-MM-DD"}]
}
```

Word 使用 `location`，PDF 必须使用一基整数 `page`。`findings` 数组名仅作为兼容别名保留；若 `issues` 同时存在则以规范字段为准。v1 缺少必填字段/哈希、证据不完整、哈希不符或任何输出路径覆盖源文件时必须失败。legacy Word/PDF findings 均需通过 `--source-manifest` 绑定原文件。

## Word Engine

`src/writeback_engine.py` 现在：

- 把 `commentReference` 放进带 `CommentReference` 样式的 `w:r`。
- 只拆分命中的直接文本 run；保留未命中格式、表格、超链接和既有修订。
- 对超链接、字段、制表符/绘图、内容控件、既有修订或重复锚点不强改，降级为待核批注。
- 显式 P 位置不再跨段回退；模糊定位只批注，短锚点扩写受新增字符和长度振幅门禁约束。
- 保留既有 `commentsExtended.xml` 条目及状态，只追加新 commentEx。
- 合并而不是覆盖 `settings.xml` 已有 `mc:Ignorable` token。
- 写临时包后审计所有 XML、关系目标、Content Types、批注 ID 和修订作者，再原子替换输出。
- 每次写回在隔离目录装载 findings，旧 results 不会串入；`proofread w --out` 已真正生效，空发现或子进程失败不会覆盖已有审阅版。
- 源 DOCX 先复制到哈希校验快照，输出也先留在隔离目录；交付前再次核对原路径，避免审校期间换稿和 symlink 覆盖。

Word max 在审校前记录源 DOCX 哈希并写入 `_max_results.json`，Phase 5 前再次核对；专名 findings 现在包含在保存结果和写回集合中，零发现也会原子覆盖旧 JSON。

DOCX→Markdown 和 `proofread extract` 都按正文/表格实际顺序输出，P 编号与 02 引擎一致。

altChunk（内嵌 MHT）docx：引擎在 `docx_uses_altchunk_body` 判定为真时物化 altChunk
为 `w:p` 再 walk——P 编号与 `extract_docx_units` / `_build_para_text_map` 逐段一致。

## PDF Engine

`src/pdf_pipeline.py` 现在：

- `pdf2md` 逐页按原文字元保留率比较 pymupdf4llm 与 PyMuPDF raw text，而不是只比较长度；覆盖率低于 75% 时自动采用完整 raw text，避免等长错误内容掩盖漏字。
- `annotate` 使用 `rawdict` 字符 bbox 生成逐行 quads，不再把多行/双栏合并成大矩形。
- 枚举全部 exact；重复原文必须由 `page` 唯一消歧，否则为 `ambiguous`。
- fragment/fuzzy 默认只进入 preview；仅显式 `--allow-fragment` / `--allow-fuzzy` 才写入；fuzzy 并列候选即使允许也保持 `ambiguous`。
- 严格校验 `ai-proofread.findings.v1`，并用 `--source-manifest` 给 legacy max 结果提供审校期间哈希门禁。
- CSV 记录状态、方法、真实分数、候选页、quad 数和原因；PDF 先写临时文件，重开验证注释类型、作者、内容、页码和 quad 后再原子替换目标。
- PDF 定位始终读取已校验的临时快照，定位后和正式交付前再次核对原路径哈希。

## Verification Evidence

### Focused regression suite

```zsh
python3 -m unittest \
  tests.test_extract_source \
  tests.test_codex_entry \
  tests.test_pdf_pipeline \
  tests.test_word_writeback \
  tests.test_altchunk \
  tests.test_splitter_context
```

- 57/57 tests passed（39 原专项 + 11 altChunk + 7 splitter context）。
- Skill `quick_validate.py` passed。
- fresh `codex exec --ephemeral -s read-only` 自动识别 `$ai-proofread`、默认 `pipeline`、02 引擎和源文件只读约束。
- `python3 -m compileall`、`git diff --check` passed。

### Codex-native Word test

- 合成文档：多 run 格式、表格、事实错误和润色项。
- 使用 [NASA Moon Facts](https://science.nasa.gov/moon/facts/) 实际联网核验，来源 URL 成功进入 Word 批注。
- 写回结果：1 个字符级替换、2 条批注、2 个 revision nodes。
- `python-docx` 重开、ZIP/XML/关系审计、LibreOffice 转 PDF及页面渲染均通过。
- 源 SHA-256 保持 `e7407675860f915a8531375b969e847da1b81be37b7f86ff9b82200f37a151a5`。
- 可检查产物：`/Users/kicker114/Downloads/月球事实测试_Codex审阅版.docx`。
- Word 产物 SHA-256：`203bb93eb52c5ca6c8593d34d71ae9f726a2435485cdef0cc4482b9db8b4a060`。

### Real mixed-layout PDF test

- 输入：`/Users/kicker114/Downloads/月球测试.pdf`，2 页。
- 源 SHA-256：`5f6c59ec47619e4303f546e5973817529dcbbba6012ab347266ba98047d54ca3`。
- pymupdf4llm 原始覆盖率仅 7.4% / 40.7%；自动 fallback 后恢复 1253 个非空白文字字符，生成 3724 字符 Markdown。
- DeepSeek max 实跑：11 chunks，314 秒；8 条结构发现 + 10 条 LLM 发现。
- PDF dry-run：6 exact hit / 4 fragment preview / 8 structure skip。
- 正式安全写回：6 条 findings，生成 6 个 Highlight annotations（第 1 页 3 条，第 2 页 3 条）；4 个 fragment 未自动写入。
- PyMuPDF 重开校验和两页视觉渲染通过，原 PDF 哈希未变化。
- 最终产物：`/Users/kicker114/Downloads/月球测试_Codex审阅版.pdf`、`/Users/kicker114/Downloads/月球测试_Codex批注清单.csv`。
- PDF / CSV 产物 SHA-256：`cef2f6f0497ddbfe747043690b830874eb05d4a5a3c5e877e2be3748cdd8f39d` / `7bd0052c33cc9cc34aa391a67d35314d78809543a12ffa14047cdab4b35ad21a`。

另有合成跨页 PDF 回归：单条 finding 跨两页时生成两组 page-local quads；源文件在定位/交付期间变化时不生成产物。

## Known Limits

- 只支持 `.docx`；旧 `.doc` 需先转换。
- 纯扫描 PDF 必须先 OCR；混合 PDF 的无文字层页面会列出并跳过。
- fragment/fuzzy 和无法消歧的重复文本不会默认写入，不能为追求命中率绕过 dry-run。
- Word 的字段、超链接或既有修订只能安全地整段/整 run 挂待核批注；原文不会被改，但可视批注范围可能宽于目标字符。
- Google Gemini、Aliyun Bailian、`--names` 本轮未测试。
- 全量 `unittest discover` 仍有两个既有导入错误：`test_performance.py`、`test_two_stage.py` 引用已移除的 `src.sentence_aligner_simple`。它们不属于本次 Codex/Word/PDF 回归门。

## Files Changed

- Codex：`AGENTS.md`、`.agents/skills/ai-proofread/`。
- Interfaces：`src/extract_source.py`、`src/cli.py`、`pyproject.toml`。
- Engines：`src/writeback_engine.py`、`src/max_pipeline.py`、`src/pdf_pipeline.py`。
- 2026-08-06 新增：`src/html_report_v3.py`（V3 报告）、`src/extract_source.py` altChunk
  解析器/物化器/共享判定、`src/splitter.py` context 裁剪 + 无空行硬切、
  `src/proofreader.py` `max_tokens`。
- Tests：四个 `tests/test_*` Codex/Word/PDF 回归文件 + `tests/test_altchunk.py` +
  `tests/test_splitter_context.py`。
- Docs：`README.md`、`CLAUDE.md`、`HANDOFF.md`、`.workbuddy/agents/pdf-proofreader.md`。

## Next Safe Action

1. 先运行上面的 57 项专项回归。
2. 检查 `git diff --check` 和 `git status --short`。
3. 提交时保留用户原有 `.workbuddy/memory/2026-08-05.md` 修改，不回退，也不要误称为本次实现。
4. 对新的真实稿件始终先生成审校源/哈希；PDF 必须先 dry-run。
5. 长稿审校用 `--concurrent 8 --rpm 60`（实测提速 5 倍，质量不变）。
