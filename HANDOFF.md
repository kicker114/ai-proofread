# HANDOFF — ai-proofread 审校 CLI 当前状态（2026-08-27）

**Scope:** `/Users/kicker114/Developer/ai-proofread`
**Branch / HEAD:** `main` / `1d4697c`（【依据】reason 回落修复）
**Status:** 工作树干净（仅 `.workbuddy/memory/2026-08-10.md` 为 WorkBuddy 记录，untracked、勿动）。

---

## 最近提交（2026-08-26）

failover 改造（`--failover-models` 多 provider 切换 + JSON 对象包装 + 看门狗预算 +
端到端回写用例 + executor shutdown 重建）已全部落库：`6719927` / `fd8ea26` / `d714ec6`。

随后两轮 writeback 质量修复也已提交：

| Commit | 内容 |
|--------|------|
| `8201f07` | XML 1.0 非法控制字符剥离（`_xml_safe`，防 lxml `ValueError` 崩溃）+ 批注高亮收束到字符级差异块（`_min_change_span`，commentRange 与高亮解耦） |
| `1d4697c` | `_findings_to_issues` 的 `reason` 不再回落到整句 `original`（LLM 发现无 reason → 批注省去 `◎ 依据`；异形词依据改用 `basis` 词典来源） |

**验证**：全量 `unittest` 157 项绿 + 固定样本 `validate_synthetic.py` 精确命中。

> 注：`.workbuddy/memory/2026-08-10.md` 为 WorkBuddy 记录（untracked），提交时保留不误动。

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

## 关键洞察（2026-08-26，writeback 质量）

### 4. LLM 发现不带 reason，不能把 original 当【依据】（`1d4697c`）

- JSON 发现 prompt 明确禁止解释，`1_llm` finding 只有 `original_sentence` + `corrected_sentence`，无 `reason`。
- `_findings_to_issues` 旧回退链 `reason or original or message` 把整句原文当 `reason`，批注 `◎ 依据` 与 `▶ 建议`（校正后整句）几乎逐字相同 → 冗余重复引述。
- **修复**：回退链改用 `reason or basis or message`；LLM 发现 reason 为空 → `build_comment_xml` 因 `evidence` 空不再渲染 `◎ 依据` 行；`0b_variant` 的真实依据在 `basis`（词典来源），从原词改为词典名。

### 5. XML 1.0 非法控制字符会击穿 lxml `.text=`（`8201f07`）

- LLM 输出/离线数据可能夹带 `\x00-\x1f` 等控制字符，直接 `element.text = s` 抛 `ValueError`，整个写回中断。
- **修复**：`load_findings` 用 `_xml_safe` 对 `cur/sug/reason/description/category` 统一剥离非法控制字符后再赋值。凡做 OOXML/XML 写回都应前置此类清洗。

### 6. 批注高亮与 commentRange 必须解耦（`8201f07`）

- 需求：高亮框选要字符级（只框 `current↔suggested` 差异块），但 commentRange 若也锚到差异块，会插入 `commentRangeStart/End` 截断 `current`，导致 `apply_track_change` 的 `_locate_safe_span` 重定位失败（前一轮 3 个测试回归）。
- **修复**：`_min_change_span` 只决定高亮区间；commentRange 仍锚定整段 `current`（`current_runs[0]`/`[-1]`），两套 run 克隆分离。凡「修订 + 批注」同段写回都要注意这个锚定分离。

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
