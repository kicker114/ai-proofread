---
name: ai-proofread
description: Proofread local Chinese DOCX and PDF files with the ai-proofread repository, producing character-level Word tracked changes and comments or standard PDF highlight annotations. Use for Chinese manuscript preflight, Word review, PDF review, Codex-native proofreading, DeepSeek pipeline proofreading, fact-checking with cited web evidence, or requests to mark issues directly in local documents.
---

# AI Proofread

Work from the repository root. Keep the source file unchanged and write every artifact to a new path. Use the repository CLI and engines directly; do not add or invoke OfficeCLI, docx-mcp, Adeu, or another MCP.

## Select a mode

- Use `pipeline` by default. It runs the established DeepSeek max pipeline and project rules.
- Use `codex-native` when the user explicitly requests Codex's own review, live fact verification, or no DeepSeek API call.
- Ask only when the requested mode cannot be inferred. Do not silently turn a network fact-check request into the offline pipeline.

## Prepare

1. Confirm the input is an existing `.docx` or `.pdf` file. Convert legacy `.doc` first. A PDF with no text layer requires OCR before this workflow.
2. Run `python3 -m pip install -e .` only when the `proofread` command or declared dependencies are unavailable.
3. Create an `ai-proofread.source.v1` location-stable source and preserve its digest:

```zsh
proofread extract "/absolute/input.docx" --out "/absolute/review_source.json"
proofread extract "/absolute/input.pdf" --out "/absolute/review_source.json"
```

## Run pipeline mode

For Word, run the complete pipeline and the 02 OOXML writer:

```zsh
proofread max "/absolute/input.docx" --writeback --author "Codex审校" --no-view
```

For PDF, extract Markdown, run max, inspect a dry-run, then annotate only after the preview is acceptable:

```zsh
python3 src/pdf_pipeline.py pdf2md "/absolute/input.pdf" --out "/absolute/input.review.md"
proofread max "/absolute/input.review.md" --no-view
python3 src/pdf_pipeline.py annotate "/absolute/input.pdf" "/absolute/input.review_max_results.json" --source-manifest "/absolute/review_source.json" --out "/absolute/input_审阅版.pdf" --csv "/absolute/input_批注清单.csv" --dry-run
python3 src/pdf_pipeline.py annotate "/absolute/input.pdf" "/absolute/input.review_max_results.json" --source-manifest "/absolute/review_source.json" --out "/absolute/input_审阅版.pdf" --csv "/absolute/input_批注清单.csv"
```

Legacy `max_results.json` may not contain a source digest, so `--source-manifest` is the required writeback gate for pipeline PDF runs. Do not enable `--allow-fragment` or `--allow-fuzzy` merely to increase the hit count. Use either flag only after inspecting its proposed locations.

## Run codex-native mode

1. Read `review_source.json`, `CLAUDE.md`, and the repository rules relevant to the manuscript.
2. Review every source unit in deterministic source order. For a long document, split units into contiguous batches of at most 40 units and approximately at most 12,000 non-whitespace characters; an individual oversized unit forms its own batch. Write one batch findings file and one checkpoint per batch; each checkpoint records the covered `P<n>` or page range and exact unit IDs. Resume from checkpoints rather than silently skipping processed or unprocessed units.
3. Merge batch findings into one list, deduplicate stable duplicates, and run a final coverage audit proving that every unit in `review_source.json` was reviewed exactly once. Do not write back until the coverage audit passes.
4. Use `location: P<n>` for Word. For PDF, `page` is required and is a one-based integer. Never infer a location from a visually similar repeated sentence.
5. When a claim needs current fact verification, browse authoritative sources. Record only sources actually opened. If verification is unavailable or inconclusive, use `fix_class: verify` and do not invent evidence.
6. Write an `ai-proofread.findings.v1` envelope. `issues` is the canonical array; `findings` is accepted only as an input compatibility alias:

```json
{
  "schema": "ai-proofread.findings.v1",
  "source_sha256": "<copy from review_source.json>",
  "issues": [
    {
      "fix_class": "must_fix",
      "location": "P3",
      "current": "原文片段",
      "suggested": "修订片段",
      "reason": "修改理由",
      "category": "事实错误",
      "evidence": [
        {
          "title": "权威来源标题",
          "url": "https://example.org/source",
          "accessed_at": "YYYY-MM-DD"
        }
      ]
    }
  ]
}
```

Use `must_fix` for a character-level Word replacement, `polish` for a non-destructive editorial comment, and `verify` for a point requiring human confirmation. Every issue requires `fix_class`, `current`, `suggested`, `reason`, and `category`. Keep `suggested` present; it may be empty for `verify`. For PDF, omit `location` and provide the required one-based integer `page`. Evidence is optional, but every evidence item requires non-empty `title`, `url`, and `accessed_at` values.

For Word, write the findings through the 02 engine:

```zsh
proofread writeback "/absolute/input.docx" --findings "/absolute/findings.json" --out "/absolute/input_审阅版.docx" --author "Codex审校"
```

New max and native findings carry their own source hash. When deliberately
reusing a legacy hashless Word findings file, also pass
`--source-manifest "/absolute/review_source.json"`; otherwise writeback must stop.

For PDF, always preview before writing:

```zsh
python3 src/pdf_pipeline.py annotate "/absolute/input.pdf" "/absolute/findings.json" --source-manifest "/absolute/review_source.json" --out "/absolute/input_审阅版.pdf" --csv "/absolute/input_批注清单.csv" --dry-run
python3 src/pdf_pipeline.py annotate "/absolute/input.pdf" "/absolute/findings.json" --source-manifest "/absolute/review_source.json" --out "/absolute/input_审阅版.pdf" --csv "/absolute/input_批注清单.csv"
```

## Validate and deliver

- Treat a source SHA-256 mismatch as a hard stop.
- For Word, require the writer's package audit to pass. Confirm the output exists, reopens with `python-docx`, contains the expected comments and tracked changes, and renders with LibreOffice when available.
- For PDF, inspect the CSV after dry-run. Unique exact matches are the default write set; ambiguous, skipped, fragment, and fuzzy results require review. Tied fuzzy candidates are always `ambiguous` and are never written, even with `--allow-fuzzy`. Reopen the final PDF and confirm the highlight annotations, page numbers, author, and comment text.
- Compare the source digest again at the end. Report absolute artifact paths and counts for findings, applied annotations, and every skip reason. State clearly when live browsing, OCR, LibreOffice rendering, or visual page review was not performed.
