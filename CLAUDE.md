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
  - `pdf2md <input.pdf> [--out <output.md>]` — PDF → Markdown via `pymupdf4llm` (text-layer pages only; image-only pages skipped)
  - `annotate <input.pdf> <findings.json> [--out <annotated.pdf>] [--author <name>]` — locates each finding's original text on the **original PDF** and adds a Highlight annotation + popup (original, suggested fix, fix-class tag). Colors: must_fix=amber, polish=light-yellow, verify=light-blue, tgscc=light-red, variant=amber, structure=light-blue, names=light-green. Output defaults to `{stem}_审阅版.pdf`.

PDF proofreading flow (used by the agent):

```bash
python3 src/pdf_pipeline.py pdf2md  <book.pdf> --out <book.md>
proofread max <book.md> --no-view                       # all stages → *_max_results.json
python3 src/pdf_pipeline.py annotate <book.pdf> <book>_max_results.json --author "AI审校"
```

`annotate` accepts both grouped findings (`{"llm":[...],"tgscc":[...]}`) and
flat lists. Findings shorter than 2 chars or longer than 200 chars are skipped
(to avoid false matches); unlocatable findings (no text layer / cross-page
split) are logged and skipped. Requires PyMuPDF (`pip install pymupdf`; system
Python 3.14 on this machine already has it).

**Note**: the Tencent Docs MCP connector has **no PDF annotation tools** (only
doc/Word `insert_comment` / `accept_all_revisions`), so PDF highlight writeback
is done locally via PyMuPDF — no cloud round-trip needed.

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
