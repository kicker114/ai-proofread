# HANDOFF — ai-proofread 中文书稿审校工具集 (2026-08-05 17:30)

**Scope:** repo:ai-proofread | branch:main | commit:68dae93 (clean)
**Status:** shipped — 全线可投产，刚完成一份 5.8MB DOCX 书稿的完整审校
**From:** Claude Code session (deepseek-v4-pro)
**To:** 下一个 Agent / 新会话 / 未来的自己

---

## Goal

基于 [Fusyong/ai-proofread](https://github.com/Fusyong/ai-proofread) 改造的中文书稿审校 CLI 工具集。核心交付：终端一键 `proofread max` 打通全部审校环节（确定性检查 → LLM 审校 → 对齐 → 报告 → DOCX 回写），外加 PDF 审校工具链。

## Current Status

全部子命令可用，02 引擎（直接 OOXML）为默认回写引擎。最近一次实战验证：对 131,238 字的 DOCX 书稿跑通完整管线，产出精修版 MD + 综合报告 + 句子级勘误表 + 带修订批注的 DOCX。

## Source of Truth

- 代码：`main` 分支，工作树干净，无未提交改动
- 文档：`README.md`（使用说明）、`CLAUDE.md`（架构与开发指南）、`docs/`（对齐算法分析与对比报告）
- 安装方式：`pip install -e . --break-system-packages`，安装在系统 Python 3.14 (`/usr/local/bin/proofread`)
- API Key：`src/.env` 已配置 `DEEPSEEK_API_KEY`（gitignored）

## Files Changed (本次会话)

- `CLAUDE.md` — 新建，项目架构与开发指南（含 WorkBuddy/PDF 工具链文档）
- `HANDOFF.md` — 新建，本文件

## Architecture Summary

```
src/
├── cli.py                  # argparse CLI 入口，6 个子命令，DOCX→MD 自动转换
├── max_pipeline.py         # ★ max 模式编排器：Phase 0a→0b→0c→1→2→3→4 + writeback (851行)
├── proofreader.py           # DeepSeek/Google API 调用，RateLimiter，async pipeline + 断点续跑
├── splitter.py             # Markdown 按标题/长度切分 + 中文句子切分
├── writeback_engine.py     # 02 引擎：直接 OOXML 字符级修订+批注 (1352行，最复杂模块)
├── writeback.py            # Adeu MCP 旧方案（--engine adeu，需 agent 上下文）
├── sentence_aligner.py     # 锚点 + Jaccard n-gram 句子对齐
├── html_report_v2.py       # HTML 报告渲染（对齐勘误表 + 综合报告）
├── diff_tools.py           # HTML 词级 diff
├── pdf_pipeline.py         # PDF 工具链：pdf2md + annotate（PyMuPDF 高亮批注）
├── special_checker/        # TGSCC 汉字规范 + MDict 词典查询 + 模糊匹配
├── structure_checker/      # 层级 + 编号连续性检查（scanner→builder→rules→report）
└── resource/               # 系统提示词 (rewrite/JSON 两套) + TGSCC 数据 + jsdiff 模板
```

### max pipeline 各阶段

| Phase | 内容 | 引擎 | 耗时特征 |
|-------|------|------|----------|
| 0a | 汉字规范 | TGSCC 查表 | 秒级 |
| 0b | 异形词/词形 | 离线词典扫描 | 毫秒级 |
| 0c | 结构检查 | structure_checker | 毫秒级 |
| 1 | LLM 审校 | DeepSeek JSON 发现模式，分块异步并发 | **占 99%+ 时间** |
| 2 | 专名查词 | LLM 识别 + MDict 词典（需 --names） | 可选 |
| 3 | 句子对齐 | 锚点 + Jaccard n-gram | 秒级 |
| 4 | 综合报告 | HTML 聚合渲染 | 秒级 |
| 5 | DOCX 回写 | 02 引擎 OOXML 修订+批注（需 --writeback） | 十秒级 |

### 关键设计决策

- **JSON 发现模式 vs 全文重写**：Phase 1 使用 JSON 发现模式（模型只输出 `[{original, corrected}]`），而非全文重写。原因：全文重写可能误改译名/署名/专名，且 token 消耗大。效果：精准句改，保留原文格式。
- **02 引擎 vs Adeu MCP**：02 引擎（默认）用 lxml+zipfile 直接操作 `word/document.xml`，零中间文本转换，保留既有修订。Adeu 是旧方案，需 Claude Code agent 上下文，保留兼容但不推荐。
- **P 编号贯穿**：`max_pipeline._build_para_text_map()` 与 `writeback_engine.build_para_map()` 必须逐字节一致。两者有重复的 `_para_raw_text()` 逻辑——这是故意的，不要重构为共享函数。
- **DeepSeek V4 Flash 默认**：`temperature=1.3`（校对推荐值，更低会减少改动）。

## Commands Run + Results

本会话执行的命令：

```
proofread m "/Users/kicker114/Downloads/四节以后全部书稿.docx" --writeback --author "审校助手"
```

结果（历时 ~28 分钟）：

| 阶段 | 发现/统计 | 耗时 |
|------|-----------|------|
| 0a TGSCC | 0 条 | 3.8s |
| 0b 异形词 | 7 条 | 0.06s |
| 0c 结构 | 130 条 | 0.01s |
| 1 LLM 审校 | 621 条修正 (74 chunks) | 1687s |
| 3 句子对齐 | 2193 match / 258 del / 228 ins | 6s |
| 5 DOCX 回写 | 391 track changes / 4 超幅降级批注 | — |

产物均在 `/Users/kicker114/Downloads/`：
- `output_四节以后全部书稿/四节以后全部书稿_审阅版.docx` — 审阅版 DOCX
- `四节以后全部书稿_max_report.html` — 综合报告 (274KB)
- `四节以后全部书稿_alignment.html` — 句子级勘误表 (3.7MB)
- `四节以后全部书稿_refined.md` — 精修版全文 (383KB)
- `四节以后全部书稿_max_results.json` — 全部发现 (517KB)

## Verification Passed

- [x] 6 个子命令（p/b/m/w/d/s）均可通过 `proofread --help` 查看
- [x] max pipeline 全阶段跑通（Phase 0a→0b→0c→1→3→4→5），exit code 0
- [x] DOCX→MD 自动转换正常（5.8MB → 381KB，1157 段）
- [x] LLM JSON 发现模式 74 chunks 全部处理、无一失败
- [x] 02 引擎回写成功生成审阅版 DOCX（391 处修订 + 626 条批注）
- [x] 超幅保护生效：4 处超过安全阈值的改写自动降级为仅批注
- [x] PDF 工具链代码已合并（`pdf_pipeline.py`，commit 68dae93）

## Not Verified Yet

- Google Gemini 模型路径未测试（当前只用 DeepSeek）
- Aliyun Bailian (`deepseek-v3`) 路径未测试
- `--names` 专名查词功能未在本次实战中启用（需 MDict 词典文件）
- PDF annotate 跨页定位在实际排版复杂的 PDF 上的命中率
- 并发场景下的 RPM 限速器是否严格守约

## Key Decisions

| 决策 | 选择 | 理由 |
|------|------|------|
| 默认模型 | deepseek-v4-flash | 速度快、中文审校质量足够、token 便宜 |
| 回写引擎 | 02 引擎（直接 OOXML） | 零中间转换损耗，保留既有修订，不依赖外部 MCP |
| Phase 1 模式 | JSON 发现而非全文重写 | 精准句改，防误改专名/署名，token 消耗更低 |
| PDF 批注 | 本地 PyMuPDF | WorkBuddy/Tencent Docs 均无 PDF 标注 API |
| 振幅保护 | must_fix 有 changed_span_cap / retention_floor | 防 LLM 过度改写，超幅降级为仅批注 |
| temperature | 1.3 | 校对实测最优：太低改动少，太高幻觉多 |

## Assumptions / Risks

- 假设 DeepSeek API (`api.deepseek.com`) 可通过当前网络直连（需在代理白名单中）
- 假设输入 DOCX 的段落样式使用标准 `Heading 1`–`9` 命名
- 假设 `src/.env` 中的 `DEEPSEEK_API_KEY` 余额充足
- 风险：02 引擎 P 编号依赖 `_para_raw_text()` 逻辑不变；如果修改此函数需同步更新 `max_pipeline.py` 中的副本
- 风险：超长书稿（>200,000 字）的 LLM 审校可能消耗大量 token（本次 131,238 字消耗约 74 chunks × ~2,000 token/chunk ≈ 150K token）

## Environment

- Python: 3.14.6 (`/usr/local/bin/python3.14`)
- CLI: `/usr/local/bin/proofread`（通过 `pip install -e .` 安装）
- OS: macOS Darwin 24.6.0
- 关键依赖: openai, google-genai, jieba, numpy, scikit-learn, mdict-utils, loguru, pymupdf4llm, rapidfuzz, python-dotenv, lxml
- API Key: `src/.env` 已配置 DEEPSEEK_API_KEY

## Next Safe Action

若继续开发：

1. 先读 `CLAUDE.md` 了解架构约束，再读 `README.md` 了解使用方式
2. 如果要改回写引擎：打开 `src/writeback_engine.py`，特别注意 P 编号逻辑（`build_para_map` / `_para_raw_text`），任何改动需同步 `src/max_pipeline.py:95-140`
3. 如果要改 LLM 审校：打开 `src/max_pipeline.py` 的 `phase1_json_proofread()` + `src/resource/prompt-proofreader-system-outputJSON.xml`
4. 如果要加新检查环节：在 `src/max_pipeline.py` 的 `run_max()` 中插入新 Phase，遵循 `results[key] = ...` 模式
5. 改完后跑一次完整 max pipeline 验证（用小文件即可，不必每次 5MB+）
6. 提交前确认 `git diff --stat` 无意外改动

若只是使用（审校新书稿）：

```bash
proofread m <新书稿.docx> --writeback --author "审校助手"
# 可选参数: --concurrent 5 --model deepseek-v4-pro --names
```

## Don't Do / Gotchas

- **不要重构 `_para_raw_text()` 为共享函数**：`max_pipeline.py` 和 `writeback_engine.py` 各有一份实现，这是故意的——P 编号回写要求两者逐字节一致，共享后一旦一方被误改，回写会静默错位到错误段落
- **不要降低 temperature 到 1.0 以下**：会让模型过于保守、几乎不改任何文字
- **不要对同一文件同时跑两个 `proofread max`**：输出文件会互相覆盖
- **不要在 `src/.env` 中使用引号包裹 API Key**：`python-dotenv` 会原样读取，引号会被当作 key 的一部分
- **PDF annotate 前先 dry-run**：`python3 src/pdf_pipeline.py annotate book.pdf findings.json --dry-run --csv preview.csv` 确认命中率再正式跑
- **不要在生产书稿上测试未经 dry-run 验证的规则改动**

## Related Docs

- `README.md` — 完整使用说明（CLI 命令、快速开始、常见问题）
- `CLAUDE.md` — 架构与开发指南（供 Agent 使用）
- `docs/comparison-analysis-ai-proofread-max-vs-pub-political.md` — 双管线对比分析
- `docs/新对齐算法设计方案.md` — 对齐算法设计文档
- `.workbuddy/agents/pdf-proofreader.md` — WorkBuddy PDF 审校 Agent 入口
