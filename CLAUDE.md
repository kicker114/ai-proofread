# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

A Chinese book manuscript proofreading CLI toolkit, forked from Fusyong/ai-proofread. Runs entirely from the terminal (not a VSCode extension). Default model: **DeepSeek V4 Flash**.

## Install & run

```zsh
pip install -e . --break-system-packages   # macOS Homebrew Python needs --break-system-packages
```

After install, `proofread` is globally available. The entry point is `src/cli.py` → `main()`.

## API keys

Place in `src/.env` (gitignored):

```
DEEPSEEK_API_KEY=sk-...
GOOGLE_API_KEY=        # optional
ALIYPUN_API_KEY=       # optional (Aliyun Bailian, for deepseek-v3)
```

## CLI commands (from project root)

```zsh
proofread p  <file.md|docx>              # single-file full rewrite proofread
proofread b  <file.md|docx>              # book: split → async pipeline
proofread m  <file.md|docx>              # ★ max pipeline (all stages)
proofread w  <file.docx>                 # DOCX track-changes + comment writeback
proofread x  <file.docx|pdf>             # location-preserving source export for Codex
proofread d  <original.md> <proofed.md>  # HTML word-level diff
proofread s  <file.md>                   # TGSCC Chinese character spec check
```

Key flags: `--model` (default `deepseek-v4-flash`), `--concurrent N` (LLM concurrency, default 8), `--rpm N` (API rate limit), `--chunk-size N` (max target size, default 200), `--request-timeout N` (max-mode per-chunk wall-clock watchdog in seconds, default 180), `--names` (max mode proper-noun dictionary lookup), `--writeback` (max mode auto-writeback to DOCX).

## Codex project entry (Word + PDF)

Codex automatically reads the repository-root `AGENTS.md` and discovers the
project-local `$ai-proofread` Skill at `.agents/skills/ai-proofread/SKILL.md`.
`agents/openai.yaml` supplies the Codex UI metadata. This entry is deliberately
repository-local; do not install or duplicate it under `~/.codex/skills`.

The Skill has two review modes while sharing one deterministic writeback layer:

- `pipeline` (default): run the existing DeepSeek max pipeline. Calling the
  DeepSeek API is not live web research.
- `codex-native`: run `proofread extract`, let Codex review the positioned text
  under the repository rules, optionally verify factual claims on the web, and
  produce `ai-proofread.findings.v1` for the existing writers.

`proofread extract <source.docx|source.pdf> --out <review_source.json>` emits
`ai-proofread.source.v1`, the source SHA-256, and units addressed by Word `P<n>`
or one-based PDF page. Native findings use an `issues` array with
`fix_class/current/suggested/reason/category`, plus `location` for DOCX or
`page` for PDF. Optional `evidence[{title,url,accessed_at}]` is rendered into the
annotation. Writers reject a findings file whose `source_sha256` does not match.
For long native reviews, process contiguous batches of at most 40 units and
about 12,000 non-whitespace characters, persist batch checkpoints, and audit
that every source unit was covered exactly once before merging findings.

Hard gates enforced by `AGENTS.md` and the Skill:

- Never overwrite the input; report absolute artifact paths.
- DOCX output must pass package/XML/revision/comment audits.
- PDF must run `annotate --dry-run` before formal annotation and pass the
  `review_source.json` through `--source-manifest`. Only unique exact matches
  apply by default; partial/fuzzy matches require explicit opt-in, while tied
  fuzzy candidates remain ambiguous even with opt-in.
- Do not route new work through Adeu, OfficeCLI, docx-mcp, or an MCP server.

## WorkBuddy agent entry (PDF proofreading)

The project ships a **WorkBuddy agent entry** so WorkBuddy can drive the whole
proofreading pipeline on **PDF input** (mixed text + images) and write findings
back as **highlight annotations on the original PDF**:

- Agent definition: `.workbuddy/agents/pdf-proofreader.md` (name `pdf-proofreader`)
- Toolchain: `src/pdf_pipeline.py` — two subcommands:
  - `pdf2md <input.pdf> [--out <output.md>]` — PDF → Markdown via `pymupdf4llm`. Each page is compared with PyMuPDF sorted raw text by retained source characters, not length alone; when coverage is below 75%, that page falls back to the complete raw text and reports its coverage. Text-layer-less pages are listed and skipped.
  - `annotate <input.pdf> <findings.json> [--source-manifest <review_source.json>] [--out <annotated.pdf>] [--author <name>] [--dry-run] [--csv <path>] [--allow-fragment] [--allow-fuzzy]` — validates native findings or legacy manifest binding, locates each finding's original text on the **original PDF**, and adds a Highlight annotation + popup (original, suggested fix, fix-class tag, optional evidence). Colors: must_fix=amber, polish=light-yellow, verify=light-blue, tgscc=light-red, variant=amber, structure=light-blue, names=light-green. Outputs default to `{stem}_审阅版.pdf` + `{stem}_批注清单.csv` after atomic write-and-reopen validation.

### Three-tier location strategy (annotate)

1. **`exact`** — normalized full-text index backed by `get_text("rawdict")`
   character bboxes. It emits one quad per visual line, supports cross-line/page
   spans, and applies only when the match is unique. A one-based finding `page`
   may disambiguate repeated text.
2. **`fragment`** — longest pure-fragment fallback. It is preview-only unless
   formal annotation explicitly passes `--allow-fragment`.
3. **`fuzzy`** — `rapidfuzz` sliding-window match with a real score. It is
   preview-only unless formal annotation explicitly passes `--allow-fuzzy`;
   tied top candidates are always ambiguous and never written.

Multiple unresolved exact candidates are `ambiguous`, never silently mapped to
the first occurrence. CSV rows record method, score, candidate pages, quad
count, and skip reason. After writing, annotations are reopened and checked for
Highlight subtype, author, content, and page. Location runs against a validated
PDF snapshot, with the original path hashed again before preview output and
formal delivery.

### PDF proofreading flow (used by the agent)

```zsh
proofread extract <book.pdf> --out <review_source.json>
python3 src/pdf_pipeline.py pdf2md  <book.pdf> --out <book.md>
proofread max <book.md> --no-view                       # all stages → *_max_results.json
python3 src/pdf_pipeline.py annotate <book.pdf> <book>_max_results.json --source-manifest <review_source.json> --dry-run --csv <preview.csv>
python3 src/pdf_pipeline.py annotate <book.pdf> <book>_max_results.json --source-manifest <review_source.json> --author "AI审校"
```

`annotate` accepts both grouped findings (`{"llm":[...],"tgscc":[...]}`) and
flat lists. `ai-proofread.findings.v1` is validated strictly: `issues` is
canonical, every PDF issue needs the five common fields and a one-based integer
`page`, and every evidence item needs title, URL, and access date. Requires
PyMuPDF + rapidfuzz + pymupdf4llm.

### Why local PyMuPDF (not Tencent Docs / WorkBuddy native)

Verified 2026-08-05 — **no native PDF annotation API exists** in the WorkBuddy ecosystem:

- **WorkBuddy V5.3.5「人机双写」** (2026-07-30) supports **Word / Excel / PPT / Markdown** in-place editing only; **PDF is not in scope** (the official PDF test case was "replicate PDF as PPT").
- **Tencent Docs MCP plugin** (local `tencent-docs-plugin`): `grep -ri pdf` returns **zero** hits — no PDF annotation tools; only doc/Word `insert_comment` / `accept_all_revisions`, and the connector is currently disconnected.
- **`tencent-local-office-edit` skill** explicitly states: `.pdf` / `.ofd` are view-only, no editing tools.
- Tencent Docs **web UI** does support manual PDF annotation (rect highlight / pen / voice), but that is a **manual UI operation** with **no MCP/API exposure** — not programmable.

Hence local PyMuPDF is the only reliable programmatic path; the produced PDF
annotations are standard and open in any PDF reader / Tencent Docs viewer.

## Running tests

Tests use the standard-library `unittest` runner (no pytest dependency):

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

# Legacy/manual suites
python tests/test_sentence_split.py
python tests/test_performance.py <original.md> <proofed.md>
python tests/test_two_stage.py <original.md> <proofed.md>
```

`test_performance.py` and `test_two_stage.py` currently import the removed
`src.sentence_aligner_simple` module and therefore fail during discovery. This
is a pre-existing test-suite defect, not a Codex entry dependency.

## Architecture

### Pipeline layers

```
CLI (src/cli.py)
  ├── proofread p  → proofreader.deepseek()          [single full-rewrite call]
  ├── proofread b  → splitter + proofreader.process_paragraphs_async()  [chunked async]
  ├── proofread m  → max_pipeline.run_max()           [all stages, see below]
  ├── proofread w  → writeback_engine (02) or writeback (adeu)
  ├── proofread x  → extract_source (positioned source + SHA-256)
  ├── proofread d  → diff_tools.jsdiff_md_text()
  └── proofread s  → special_checker.check_to_tgscc()
```

### `proofread max` pipeline (`src/max_pipeline.py`)

The flagship pipeline. Runs in order:

| Phase | What | Engine |
|-------|------|--------|
| 0a | Character spec (TGSCC) | Lookup table — traditional/variant/non-standard chars |
| 0b | Word-form check | Offline dictionary scan (xh7_compressed + yixingci) |
| 0c | Structure check | `structure_checker/` — heading hierarchy gaps + numbering continuity |
| 1 | LLM proofread | LLM JSON discovery mode — chunked async, returns `{"findings": [{original_sentence, corrected_sentence}]}` (object wrapper so multi-provider `json_object` mode can pin structure), then `match_similar_text` fuzzy-locates and writes back. NOT full rewrite — precise sentence-level fixes only. Multi-provider failover: `--failover-models qwen3.8-max,kimi-k2.6,...` switches provider after per-provider retry exhaustion; checkpoint identity excludes model so failed chunks share checkpoints across providers |
| 2 | Proper-noun check (--names) | LLM identifies proper nouns → `mdict.query_mdx()` dictionary lookup |
| 3 | Sentence alignment | Anchor algorithm + Jaccard n-gram (`sentence_aligner.py`) |
| 4 | Master report | Aggregates all phases into single self-contained HTML (`html_report_v2.py`) |

Phase 1 uses a **JSON discovery prompt** (`src/resource/prompt-proofreader-system-outputJSON.xml`) vs the rewrite prompt (`src/resource/prompt-proofreader-system.xml`). The key difference: JSON mode asks the model to output only changed sentences as a JSON array, avoiding the risk and token cost of full-text rewriting.

### DOCX writeback — 02 engine (`src/writeback_engine.py`)

The **02 engine** (default) manipulates DOCX directly via `lxml` + `zipfile` — no intermediate text conversion:

1. `build_para_map()` — walks `word/document.xml` body children, assigns P-numbers to non-empty paragraphs (includes table cells). Builds `{pn: w:p_element}` and `{pn: text}`.
2. For `must_fix` findings: `difflib` character-level diff → split only the
   affected direct text runs → `<w:del>/<w:ins>` track changes + styled
   `<w:comment>`. Unaffected runs and properties remain byte-structurally intact.
3. For `polish`/`verify` findings: highlight + comment only, no text change.
4. Preserves existing revisions (accept-view text via `_para_raw_text()` — includes `<w:ins>`, excludes `<w:del>` ancestors). A target crossing a hyperlink, field, tab/drawing, content control, protected range, or existing revision is downgraded to a comment rather than rewritten. Declared P locations never fall back to another paragraph, fuzzy P resolution is comment-only, and an amplitude budget blocks short-anchor sentence expansion.
5. Runs a package audit before success: ZIP/XML parse, relationship targets,
   comment marker/reference IDs, revision author/date/text, and dangling modern
   comment parts. Each invocation uses isolated findings staging and atomically
   replaces the requested output only after the audit passes.

**P-numbering must be byte-identical** between the 02 engine and `max_pipeline._build_para_text_map()`. Both use the same `_para_raw_text()` logic and walk body children identically. Finding location strings like `P3` map directly to these paragraph indices.
Word max captures the source DOCX hash before review, stores it in
`_max_results.json`, and checks it again immediately before Phase 5 writeback.
Standalone legacy Word findings without that hash require a
`proofread extract` manifest via `proofread w --source-manifest`.

The `fix_class` routing:
- `must_fix` → character-level track changes + comment
- `polish` → comment only (TGSCC single-char fixes — avoid false matches)
- `verify` → comment only (pending human verification)

The old **Adeu MCP** path (`src/writeback.py`, `--engine adeu`) requires Claude Code agent context and is retained for compatibility.

### Key modules

| Module | Role |
|--------|------|
| `src/cli.py` | argparse CLI, DOCX→MD auto-conversion, dispatches to all pipelines |
| `src/extract_source.py` | `ai-proofread.source.v1` export for DOCX/PDF Codex-native review |
| `src/proofreader.py` | DeepSeek/Google API calls, `RateLimiter`, async pipeline `process_paragraphs_async()` with checkpoint resume |
| `src/splitter.py` | Markdown split by heading levels + length, Chinese sentence segmentation (`split_chinese_sentences`) |
| `src/max_pipeline.py` | Max pipeline orchestrator, P-number text map, finding→P resolution |
| `src/writeback_engine.py` | 02 engine: direct OOXML track-changes + comments |
| `src/writeback.py` | Adeu MCP writeback (legacy) |
| `src/pdf_pipeline.py` | PDF toolchain: `pdf2md` (pymupdf4llm) + `annotate` (PyMuPDF highlight writeback on original PDF) |
| `src/sentence_aligner.py` | Anchor-based sentence alignment with Jaccard n-gram similarity |
| `src/html_report_v2.py` | HTML report rendering for alignment and max reports |
| `src/html_report_v3.py` | V3 dark-theme report: inline error highlighting, chapter grouping, severity badges (used by `phase4_report`) |
| `src/diff_tools.py` | HTML word-level diff generation via difflib |
| `src/special_checker/` | TGSCC character check, MDict lookup, checker/NGram model, fuzzy text matching |
| `src/structure_checker/` | Heading hierarchy + numbering continuity validation (scanner → builder → rules) |
| `src/resource/` | System prompts (XML), TGSCC data JSON, jsdiff HTML template |

### Data dependencies

- **TGSCC data**: `src/resource/tgscc_data.json` — General Standard Chinese Characters table
- **Word-form dictionaries**: `reliable-proofreading-data/` — xh7_compressed + yixingci data
- **MDict dictionaries** (optional, for `--names`): external `.mdx` files, paths configured in `max_pipeline.py` `DICT_PATHS` dict
- **Structure rules**: `src/structure_checker/rules.example.json` — defines heading patterns and numbering rules. `scanner.py` 识别 markdown 标题（ATX `#` 可无空格/全角空格/BOM 容忍、setext `===`、无 `#` 的"第X章 标题"普通行），跳过代码围栏，不再全文正则扫描。支持 **6 类标题体系**：章/节/目（`第X章/节/N.`）、部/编/卷/篇/册（`第一部/第1卷/卷一`，part 边界重置章连续性、同号同单位 part 合并）、中文序号（`一、`）、括号序号（`（一）`/`(一)`）、数字顿号（`1、`）、多级数字（`1.1` 按点数定级）、特殊部分（`前言/附录A/后记` 等词边界，不参与编号检查）。全角归一化（数字/句点/拉丁/繁體節/Unicode 罗马/小写 iv）偏移映射回原文。`N.`/`num_dot` 防小数（12.9亿 / 12. 5亿 / 2024.1）。建树以 markdown 标题层级优先（`## 1.` 编号节不误报 gap；真跳级仍报）；纯文本无 # 时柔性小节（一、/（一））不报 gap。合并标题取全编号（`第1章 第1节`/`第一部 第一章`/`卷一 第一章`），叙述性提及排除，同 line_id 嵌套、跨行同号 + 同号 part 合并使节号连续可检。罗马严格判定（非法→number_missing、第X章占位、第I章不误伤、体系切换跳过）。测试 `tests/test_structure_scanner.py`（68 例）。

### Model routing in `proofreader.py`

```
deepseek-v4-flash / deepseek-v4-pro / deepseek-chat / deepseek-reasoner
  → api.deepseek.com via OpenAI client

deepseek-v3
  → Aliyun Bailian (dashscope.aliyuncs.com)

Google models (gemini-*)
  → google-genai SDK
```

`temperature` is hardcoded at 1.3 (tuned for proofreading — lower values reduce edits too much).

### Performance tuning (measured 2026-08-06)

`proofread max` Phase 1 (LLM JSON discovery) is the wall-clock bottleneck — for a
16万字 book it took 11321s (≈3h) at the old defaults. Pure-Word and altChunk
runs have identical API cost; misreports do not affect duration.

**Safe default until the chunk-size quality gate passes:**

```zsh
proofread m 书稿.md --no-view --concurrent 3 --rpm 15 --chunk-size 200
```

- Do not raise concurrency or RPM merely to reduce wall time. First run the fixed
  120-paragraph quality sample at 600/800/1200 characters and keep 200 until recall
  and precision pass the gate.
- Each max chunk is atomically checkpointed under `.<doc>_max_checkpoint/`; the
  identity includes source hash, model, prompt hash, chunk-size and chunk hash.
- API retries are explicit and observable: SDK retries are disabled, timeout is
  `API_TIMEOUT_SECONDS` (120s, per-inactivity — a trickling server can reset it
  indefinitely), and each logical request has at most two retries with exponential
  backoff (`API_RETRY_DELAYS = (15s, 45s)` ±30% jitter, configurable for tests).
  Empty or invalid JSON responses are failures, not successful empty reviews.
- **DeepSeek/Aliyun 强制直连**: `_get_openai_client` 用显式 `http_client=httpx.Client(
  proxy=None)` 创建，不读环境代理（HTTP_PROXY/HTTPS_PROXY/ALL_PROXY），规避本地
  Clash 等代理注入的失败点。Google genai 客户端保持系统默认（trust_env），需代理
  环境（大陆访问 gemini）不受影响。
- Phase 1 has a wall-clock watchdog per chunk: `worker` wraps the API call in
  `asyncio.wait_for(..., timeout=request_timeout)` (default 180s, override with
  `--request-timeout N`). A chunk that never returns is marked `failed`
  (checkpoint error "墙钟超时 Ns"). 
- **Phase 1 自动续跑**: 失败块记 checkpoint 后 run_max 自动重跑（最多
  `MAX_PHASE1_ROUNDS=3` 轮，轮间退避 `PHASE1_RETRY_DELAY_BASE` 递增）。服务端
  时点性劣化（高峰空响应/慢响应）等负载回落即自动收敛，一次命令最终审完；
  轮次耗尽仍抛 RuntimeError fast-fail。`cmd_max` 捕获后 flush + `os._exit(1)`
  （timed-out executor threads 不可取消，避免 _python_exit join 卡退出）。
- The max report includes phase wall time, logical calls, attempts, retries, rate-limit
  waits and checkpoint hits. A second run should produce zero API calls for completed chunks.

Concurrency **never affects output quality** — each chunk is an independent,
identically-prompted call. Only API quota / 429s are the practical ceiling.

**`max_tokens=4096`** is passed explicitly in `max_pipeline._proofread_one_json`
(JSON discovery mode only) to cap per-call output. The default `deepseek()`
callers (`proofread p/b` full rewrite, `lookup_mdict`) pass `None` = unlimited.

**Context trimming** (in `splitter.split_markdown_by_title_and_length_with_context`):
each chunk previously carried the ENTIRE paragraph as `context` — measured 18.3×
input-token redundancy. Now `build_local_context()` keeps only the chapter title +
`context_pad` (default 800) characters on each side, capped at `max_context`
(3000). Also `cut_text_by_length()` hard-splits text with no blank lines at
sentence boundaries (`_SENT_END = 。！？；!?;…`), so DOCX→MD continuous text no
longer degenerates into one giant chunk. Net effect on a 16万字 book: input tokens
−38%, and sentence-complete chunks. Test: `tests/test_splitter_context.py`.

### DOCX→MD conversion

Done inline in `cli.py._docx_to_md()` through `extract_source`'s lxml OOXML
walker. Heading styles (`Heading 1`–`9`) become `#`–`#########`. Body and
table-cell paragraphs are emitted in document order so table text is not
silently omitted. The `.md` is written adjacent to the source `.docx`.

### altChunk (MHT-embedded) DOCX support

PDF→Word converters and Tencent Docs export `.docx` whose body is a single
`<w:altChunk>` (no standard `w:p`) — all text lives in an embedded MHT file.
Without special handling, DOCX→MD yields empty text, the P-map is empty, and
writeback silently skips. Support lives in `src/extract_source.py`:

- `extract_altchunk_paragraphs(archive)` — robust MHT decoder (multipart
  boundary split → `Content-Type: text/html` part → quoted-printable / base64
  decode → declared charset) + HTML→paragraphs with heading levels.
- `materialize_altchunk_paragraphs(body, paras)` — converts to real `w:p` for
  the writeback engine.
- `docx_uses_altchunk_body(archive)` — **shared predicate** the three consumers
  (extract_docx_units, `_build_para_text_map`, writeback_engine.main) use to
  decide "body fully MHT-carried". It ignores empty `<w:p/>` placeholders and
  requires **text-bearing** paragraphs to fall back to the standard walk, so
  mixed docs (native `w:p` + altChunk) are handled identically by all three and
  the altChunk content is never silently dropped or relocated.

P-numbering stays consistent because all three consumers feed off the same
ordered paragraph list. See `tests/test_altchunk.py`.

## Important constraints

- **P-numbering consistency**: Any change to `_para_raw_text()` or paragraph walking logic in `writeback_engine.py` must be mirrored exactly in `max_pipeline._build_para_text_map()`. They are deliberately duplicated — do NOT refactor into a shared function unless you also update all `P{n}` resolution code. The one intentional exception is the altChunk predicate `extract_source.docx_uses_altchunk_body()` — all three consumers call this shared helper, and the guard drift between them (e.g. `p` vs `p,tbl`, empty `<w:p/>` handling) was a real bug fixed in commit `5f42b38`.
- **Prompt files**: Two distinct system prompts in `src/resource/` — the JSON discovery prompt is used by max pipeline Phase 1; the rewrite prompt is used by `proofread p` and `proofread b`. Do not swap them.
- **Python ≥ 3.10 required** (uses `str | None` union syntax).
- **No pytest/conftest** — new regression tests use `unittest` and must remain directly runnable from `tests/`.
- **Rate limiting**: `proofreader.RateLimiter` uses `asyncio.Lock` with RPM-based intervals. Concurrency and RPM are separate knobs — `--concurrent N` controls simultaneous API calls, `--rpm N` controls the per-minute cap.
