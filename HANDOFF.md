# HANDOFF — ai-proofread 审校 CLI 当前状态（2026-08-10）

**Scope:** `/Users/kicker114/Developer/ai-proofread`
**Branch / HEAD:** `main` / `f20ee89`（DeepSeek thinking-disabled 已提交）
**Status:** 工作树有一批 failover 改造未提交；skill 改造（publish/political → WorkBuddy）待下次。

---

## 当前工作树状态（未提交）

以下 failover 改造已完成并通过全量回归（151 项 + 固定样本），**尚未提交**：

| 文件 | 改动 |
|------|------|
| `src/proofreader.py` | `_MODEL_PARAMS` 每模型参数块（thinking-disabled / response_format / temperature=None）；`_client_config` 扩展 4 provider（DeepSeek 直连、阿里云 dashscope、Moonshot、智谱）；`deepseek_async` 加 `models` 列表 failover 循环 + `provider_failovers` 统计 |
| `src/max_pipeline.py` | 输出契约 `{"findings":[...]}` 对象包装（`_json_extract` 语义反转）；checkpoint identity 剥离 `model` 键（跨 provider 共享）；`run_max`/`phase1`/`worker` 透传 `models` |
| `src/resource/prompt-proofreader-system-outputJSON.xml` | `<EXAMPLE_JSON_OUTPUT>` 改为 `{"findings":[...]}` 包装 |
| `src/cli.py` | `AVAILABLE_MODELS` 加 `qwen3.8-max`/`kimi-k2.6`/`glm-4.7`；max 子命令加 `--failover-models` |
| `tests/test_network_resume.py` | identity 去 model；新增 findings 对象 / failover 切换 / 单模型用例 |
| `tests/test_skip_visibility.py` | fake_json 改对象契约 |
| `CLAUDE.md` / `README.md` | 契约 + failover 文档 |

**验证**：全量 `unittest` 151 项绿、固定样本 `validate_synthetic.py` 精确命中、`compileall` + `git diff --check` 通过；真实 DeepSeek smoke（新对象契约）2.6s 成功返回 `{"findings":[...]}`。

> 注：`.workbuddy/memory/2026-08-10.md` 为 untracked（用户 WorkBuddy 记录），提交时保留不误动。

---

## 关键洞察（2026-08-10，重要，可复用）

### 1. DeepSeek V4 thinking 默认开启 → JSON 截断/空响应（已修，已提交 `f20ee89`）

- **OpenAI 端点**：`thinking` 默认 ON，把输出预算烧在内部推理上，负载高时 JSON 截断或空响应。A/B 实测：默认成功率 **40%**（2/5，失败时 max_tokens 打满 512），显式 `thinking:{type:disabled}` 后 **100%**（5/5，输出 tokens 降到 ~1/6，耗时减半）。
- **修复**：`_deepseek_request_once` 对 `deepseek-v4-flash/v4-pro` 传 `extra_body={"thinking":{"type":"disabled"}}`。
- **Anthropic 端点**（Claude Code agent 走的 `/anthropic/v1/messages`）：同样 thinking 默认 ON（已探测 `content_types=['thinking','text']`），且 **`settings.json` 只能映射 URL/model、无法传 thinking 参数** → publish/political 的 Claude agent 享受不到此修复。

### 2. 三个审校项目架构不同，ai-proofread 的修复无法直接迁移

| 项目 | 类型 | LLM 调用方式 |
|------|------|-------------|
| **ai-proofread** | Python 包（`proofread` CLI） | 直连 OpenAI 端点（已修 thinking/failover） |
| **proofreading-publish** | Claude Code skill 项目 | agent dispatch（`sonnet/haiku/opus` → 全局 settings → DeepSeek Anthropic 端点，thinking 开） |
| **proofreading-political** | Claude Code skill 项目 | 同上 |

- publish/political **无自己的 LLM API 调用代码**，不 import ai-proofread。它们的 agent 走全局 `~/.claude/settings.json` 的 `ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic` + `ANTHROPIC_DEFAULT_*_MODEL=deepseek-v4-*`。
- **结论**：要修 publish/political 的 thinking/稳定性，不能靠迁移 ai-proofread 代码；需在 agent 编排层或调用架构上解决（见下次待办）。

### 3. WorkBuddy 不识别 `.claude/skills/`，需独立 agent 定义

- WorkBuddy 用自己格式（`.workbuddy/agents/`、`~/.workbuddy/skills/`、marketplace），app 不引用 `.claude/skills/`。
- **已有先例**：ai-proofread 的 `.workbuddy/agents/pdf-proofreader.md`（frontmatter: `name/description/model/tools/color` + 正文指令）驱动 `proofread` CLI 做 PDF 审校。
- 因此让 publish/political 可被 WorkBuddy 使用 = 各写一个 `.workbuddy/agents/` 定义，复用其确定性脚本 + 规则库，agent 编排用 WorkBuddy 替代 Claude dispatch。

---

## 下次待办：publish/political → WorkBuddy skill 改造

**目标**：让 proofreading-publish / proofreading-political 可被 WorkBuddy 使用（用户 2026-08-10 提出，确认下次做）。CLI 网关方案已搁置。

### 两个项目的确定性资产（可复用）

- **publish**（最大，~22K LOC）：25 个 Python 脚本——`extract_text`/`chunk_text`/`scan_known_errors`/`merge_results`/`apply_corrections_*`/`generate_report` 等；`run_pipeline.sh` 主入口；错例库 `references/knowledge_base/cases.jsonl`。仅 `dispatch_agents.py` 依赖 Claude `Agent` 工具。
- **political**（~4.4K LOC）：`scan_political.py` 确定性关键词扫描 + `references/*.md` 规则库 + 少量 agent workflow（`online-review.js`）。

### 改造要点

1. 各写 `.workbuddy/agents/publish-proofreader.md` / `political-proofreader.md`（参考 ai-proofread 的 `pdf-proofreader.md` 格式）。
2. 驱动确定性脚本（extract → chunk → scan → merge → writeback → report），agent 编排用 WorkBuddy 替代 Claude dispatch。
3. publish 的 `dispatch_agents.py`（Claude `Agent` 依赖）是主要改造点。
4. 优先跑确定性扫描（不依赖 DeepSeek），LLM 环节走 WorkBuddy 自身模型通道。

### 已知待验证

- WorkBuddy agent 协议细节（字段、工具白名单）以现有 `pdf-proofreader.md` 为准，不确定处查 WorkBuddy 文档。
- publish 错例库路径是相对路径，WorkBuddy agent 需在正确 CWD 下驱动。

---

## 项目核心事实（保持不变，后续工作需遵守）

### CLI 命令（项目根）

```zsh
proofread p  <file.md|docx>              # 单文件全量重写
proofread b  <file.md|docx>              # 全书（split + async）
proofread m  <file.md|docx>              # ★ max 管线（全部阶段，见 CLAUDE.md）
proofread w  <file.docx>                 # DOCX 修订+批注写回（02 引擎）
proofread x  <file.docx|pdf>             # 位置保留源导出（Codex）
proofread d  <original.md> <proofed.md>  # HTML 词级 diff
proofread s  <file.md>                   # TGSCC 汉字规范检查
```

### 关键约束

- **P 编号一致性**：`writeback_engine._para_raw_text()` 与 `max_pipeline._build_para_text_map()` 必须逐字节一致，勿重构为共享函数（唯一例外 altChunk 判定 `extract_source.docx_uses_altchunk_body`）。
- **两个 system prompt 勿互换**：`prompt-proofreader-system.xml`（全文重写，`proofread p/b`）vs `prompt-proofreader-system-outputJSON.xml`（JSON 发现，max Phase 1）。
- **Python ≥ 3.10**（`str | None` 语法）。无 pytest，新回归用 `unittest`。
- **源文件只读**；DOCX/PDF 写回必须先过哈希门禁 / `--dry-run`。

### 数据依赖

- TGSCC：`src/resource/tgscc_data.json`；词形：`reliable-proofreading-data/`；MDict（`--names`）；结构规则 `src/structure_checker/rules.example.json`。

### 测试

```zsh
python3 -m unittest tests.test_network_resume tests.test_skip_visibility  # 本次改动相关
python3 samples/validate_synthetic.py   # 固定样本确定性回归（秒级）
```
全量专项：`tests.test_extract_source / codex_entry / pdf_pipeline / word_writeback / altchunk / splitter_context / skip_visibility / book_path / structure_scanner / network_resume`（当前 151 项）。
