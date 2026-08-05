# HANDOFF — ai-proofread Codex Word/PDF 审校入口（2026-08-06）

**Scope:** `/Users/kicker114/Developer/ai-proofread`
**Branch / HEAD:** `main` / current committed HEAD
**Status:** Codex 入口、Word 字符级修订批注、PDF 高亮批注均已实现并完成专项验收；本次实现已提交。

> 工作树原有 `.workbuddy/memory/2026-08-05.md` 用户修改。本次实现没有编辑该文件，后续提交时不要把它当成本次产物覆盖或回退。

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
  tests.test_word_writeback
```

- 39/39 tests passed。
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
- Tests：四个 `tests/test_*` Codex/Word/PDF 回归文件。
- Docs：`README.md`、`CLAUDE.md`、`HANDOFF.md`、`.workbuddy/agents/pdf-proofreader.md`。

## Next Safe Action

1. 先运行上面的 39 项专项回归。
2. 检查 `git diff --check` 和 `git status --short`。
3. 提交时保留用户原有 `.workbuddy/memory/2026-08-05.md` 修改，不回退，也不要误称为本次实现。
4. 对新的真实稿件始终先生成审校源/哈希；PDF 必须先 dry-run。
