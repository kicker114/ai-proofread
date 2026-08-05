# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

A Chinese book manuscript proofreading CLI toolkit, forked from Fusyong/ai-proofread. Runs entirely from the terminal (not a VSCode extension). Default model: **DeepSeek V4 Flash**.

## Install & run

```bash
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

```bash
proofread p  <file.md|docx>              # single-file full rewrite proofread
proofread b  <file.md|docx>              # book: split → async pipeline
proofread m  <file.md|docx>              # ★ max pipeline (all stages)
proofread w  <file.docx>                 # DOCX track-changes + comment writeback
proofread d  <original.md> <proofed.md>  # HTML word-level diff
proofread s  <file.md>                   # TGSCC Chinese character spec check
```

Key flags: `--model` (default `deepseek-v4-flash`), `--concurrent N` (LLM concurrency, default 3), `--rpm N` (API rate limit), `--names` (max mode proper-noun dictionary lookup), `--writeback` (max mode auto-writeback to DOCX).

## WorkBuddy agent entry (PDF proofreading)

The project ships a **WorkBuddy agent entry** so WorkBuddy can drive the whole
proofreading pipeline on **PDF input** (mixed text + images) and write findings
back as **highlight annotations on the original PDF**:

- Agent definition: `.workbuddy/agents/pdf-proofreader.md` (name `pdf-proofreader`)
- Toolchain: `src/pdf_pipeline.py` — two subcommands:
  - `pdf2md <input.pdf> [--out <output.md>]` — PDF → Markdown via `pymupdf4llm`. Prints a count + list of text-layer-less pages (illustration/chapter-title/scan pages) that are skipped.
  - `annotate <input.pdf> <findings.json> [--out <annotated.pdf>] [--author <name>] [--dry-run] [--csv <path>]` — locates each finding's original text on the **original PDF** and adds a Highlight annotation + popup (original, suggested fix, fix-class tag). Colors: must_fix=amber, polish=light-yellow, verify=light-blue, tgscc=light-red, variant=amber, structure=light-blue, names=light-green. Outputs default to `{stem}_审阅版.pdf` + `{stem}_批注清单.csv`.

### Three-tier location strategy (annotate)

1. **`exact`** — character-level full-text index (`get_text("dict")` line bboxes). Finds the de-whitespaced sentence anywhere in the whole book and maps the span back to per-line rects, so **cross-line and cross-page continuous text is highlighted in one pass** (the core fix — `page.search_for` cannot match across line breaks).
2. **`fragment`** — longest pure-fragment fallback when the full sentence is split by page headers/footers (common in this PDF: page tail `…答曰："特` + next-page header `002 技术垄断…` + body `乌斯…`). Hits the longest in-page fragment.
3. **`fuzzy`** — `rapidfuzz` sliding-window match (len≥4, `score_cutoff=85`, head/tail `partial_ratio` ≥80). Marked "待复核" in the CSV.

Match `method` and `page` are recorded per finding in the CSV. Skips are classified: no original field / length out of range (<2 or >200 chars) / unlocatable.

### PDF proofreading flow (used by the agent)

```bash
python3 src/pdf_pipeline.py pdf2md  <book.pdf> --out <book.md>
proofread max <book.md> --no-view                       # all stages → *_max_results.json
python3 src/pdf_pipeline.py annotate <book.pdf> <book>_max_results.json --dry-run --csv <preview.csv>   # preview hit rate
python3 src/pdf_pipeline.py annotate <book.pdf> <book>_max_results.json --author "AI审校"               # → _审阅版.pdf + _批注清单.csv
```

`annotate` accepts both grouped findings (`{"llm":[...],"tgscc":[...]}`) and
flat lists. Requires PyMuPDF + rapidfuzz + pymupdf4llm (system Python 3.14 on
this machine already has all three).

### Why local PyMuPDF (not Tencent Docs / WorkBuddy native)

Verified 2026-08-05 — **no native PDF annotation API exists** in the WorkBuddy ecosystem:

- **WorkBuddy V5.3.5「人机双写」** (2026-07-30) supports **Word / Excel / PPT / Markdown** in-place editing only; **PDF is not in scope** (the official PDF test case was "replicate PDF as PPT").
- **Tencent Docs MCP plugin** (local `tencent-docs-plugin`): `grep -ri pdf` returns **zero** hits — no PDF annotation tools; only doc/Word `insert_comment` / `accept_all_revisions`, and the connector is currently disconnected.
- **`tencent-local-office-edit` skill** explicitly states: `.pdf` / `.ofd` are view-only, no editing tools.
- Tencent Docs **web UI** does support manual PDF annotation (rect highlight / pen / voice), but that is a **manual UI operation** with **no MCP/API exposure** — not programmable.

Hence local PyMuPDF is the only reliable programmatic path; the produced PDF
annotations are standard and open in any PDF reader / Tencent Docs viewer.

## Running tests

Tests are manual scripts (no pytest):

```bash
python tests/test_sentence_split.py
python tests/test_performance.py <original.md> <proofed.md>
python tests/test_two_stage.py <original.md> <proofed.md>
```

## Architecture

### Pipeline layers

```
CLI (src/cli.py)
  ├── proofread p  → proofreader.deepseek()          [single full-rewrite call]
  ├── proofread b  → splitter + proofreader.process_paragraphs_async()  [chunked async]
  ├── proofread m  → max_pipeline.run_max()           [all stages, see below]
  ├── proofread w  → writeback_engine (02) or writeback (adeu)
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
| 1 | LLM proofread | DeepSeek JSON discovery mode — chunked async, returns `[{original_sentence, corrected_sentence}]`, then `match_similar_text` fuzzy-locates and writes back. NOT full rewrite — precise sentence-level fixes only |
| 2 | Proper-noun check (--names) | LLM identifies proper nouns → `mdict.query_mdx()` dictionary lookup |
| 3 | Sentence alignment | Anchor algorithm + Jaccard n-gram (`sentence_aligner.py`) |
| 4 | Master report | Aggregates all phases into single self-contained HTML (`html_report_v2.py`) |

Phase 1 uses a **JSON discovery prompt** (`src/resource/prompt-proofreader-system-outputJSON.xml`) vs the rewrite prompt (`src/resource/prompt-proofreader-system.xml`). The key difference: JSON mode asks the model to output only changed sentences as a JSON array, avoiding the risk and token cost of full-text rewriting.

### DOCX writeback — 02 engine (`src/writeback_engine.py`)

The **02 engine** (default) manipulates DOCX directly via `lxml` + `zipfile` — no intermediate text conversion:

1. `build_para_map()` — walks `word/document.xml` body children, assigns P-numbers to non-empty paragraphs (includes table cells). Builds `{pn: w:p_element}` and `{pn: text}`.
2. For `must_fix` findings: `difflib` character-level diff → `<w:del>/<w:ins>` track changes + styled `<w:comment>`.
3. For `polish`/`verify` findings: highlight + comment only, no text change.
4. Preserves existing revisions (accept-view text via `_para_raw_text()` — includes `<w:ins>`, excludes `<w:del>` ancestors).

**P-numbering must be byte-identical** between the 02 engine and `max_pipeline._build_para_text_map()`. Both use the same `_para_raw_text()` logic and walk body children identically. Finding location strings like `P3` map directly to these paragraph indices.

The `fix_class` routing:
- `must_fix` → character-level track changes + comment
- `polish` → comment only (TGSCC single-char fixes — avoid false matches)
- `verify` → comment only (pending human verification)

The old **Adeu MCP** path (`src/writeback.py`, `--engine adeu`) requires Claude Code agent context and is retained for compatibility.

### Key modules

| Module | Role |
|--------|------|
| `src/cli.py` | argparse CLI, DOCX→MD auto-conversion, dispatches to all pipelines |
| `src/proofreader.py` | DeepSeek/Google API calls, `RateLimiter`, async pipeline `process_paragraphs_async()` with checkpoint resume |
| `src/splitter.py` | Markdown split by heading levels + length, Chinese sentence segmentation (`split_chinese_sentences`) |
| `src/max_pipeline.py` | Max pipeline orchestrator, P-number text map, finding→P resolution |
| `src/writeback_engine.py` | 02 engine: direct OOXML track-changes + comments |
| `src/writeback.py` | Adeu MCP writeback (legacy) |
| `src/pdf_pipeline.py` | PDF toolchain: `pdf2md` (pymupdf4llm) + `annotate` (PyMuPDF highlight writeback on original PDF) |
| `src/sentence_aligner.py` | Anchor-based sentence alignment with Jaccard n-gram similarity |
| `src/html_report_v2.py` | HTML report rendering for alignment and max reports |
| `src/diff_tools.py` | HTML word-level diff generation via difflib |
| `src/special_checker/` | TGSCC character check, MDict lookup, checker/NGram model, fuzzy text matching |
| `src/structure_checker/` | Heading hierarchy + numbering continuity validation (scanner → builder → rules) |
| `src/resource/` | System prompts (XML), TGSCC data JSON, jsdiff HTML template |

### Data dependencies

- **TGSCC data**: `src/resource/tgscc_data.json` — General Standard Chinese Characters table
- **Word-form dictionaries**: `reliable-proofreading-data/` — xh7_compressed + yixingci data
- **MDict dictionaries** (optional, for `--names`): external `.mdx` files, paths configured in `max_pipeline.py` `DICT_PATHS` dict
- **Structure rules**: `src/structure_checker/rules.example.json` — defines heading patterns and numbering rules

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

### DOCX→MD conversion

Done inline in `cli.py._docx_to_md()` using `python-docx`. Heading styles (`Heading 1`–`9`) become `#`–`#########`. Non-heading paragraphs pass through as-is. The `.md` is written adjacent to the source `.docx`.

## Important constraints

- **P-numbering consistency**: Any change to `_para_raw_text()` or paragraph walking logic in `writeback_engine.py` must be mirrored exactly in `max_pipeline._build_para_text_map()`. They are deliberately duplicated — do NOT refactor into a shared function unless you also update all `P{n}` resolution code.
- **Prompt files**: Two distinct system prompts in `src/resource/` — the JSON discovery prompt is used by max pipeline Phase 1; the rewrite prompt is used by `proofread p` and `proofread b`. Do not swap them.
- **Python ≥ 3.10 required** (uses `str | None` union syntax).
- **No pytest/conftest** — tests are standalone scripts with manual assertions and print-based output. Add new tests as new scripts in `tests/`.
- **Rate limiting**: `proofreader.RateLimiter` uses `asyncio.Lock` with RPM-based intervals. Concurrency and RPM are separate knobs — `--concurrent N` controls simultaneous API calls, `--rpm N` controls the per-minute cap.
