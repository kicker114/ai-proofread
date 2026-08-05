# ai-proofread Codex 入口

处理本仓库中的中文审校、Word 修订批注或 PDF 高亮批注任务时，使用项目级
`$ai-proofread` Skill（`.agents/skills/ai-proofread/SKILL.md`）。

- 默认使用现有 DeepSeek `pipeline`；用户明确要求 Codex 直接审校、联网核验或不调用 DeepSeek 时，使用 `codex-native`。
- 原始 `.docx` / `.pdf` 始终只读，结果写到新文件。开始和交付时核对源文件 SHA-256。
- Word 仅支持 `.docx`；`.doc` 先转换。PDF 必须有文字层；扫描件先 OCR。
- Word 写回只使用项目的 02 OOXML 引擎；PDF 写回只使用 `src/pdf_pipeline.py` 与 PyMuPDF。不引入 OfficeCLI、docx-mcp、Adeu 或其他 MCP。
- 写回前必须 dry-run 或完成结构审计。模糊、重复且无法由位置消歧的文本不得自动写入。
- 联网核验只引用实际访问的权威来源，并在 finding 的 `evidence` 中记录标题、URL 和访问日期；无法核实时标为 `verify`，不得编造来源。
- 交付时报告输出绝对路径、发现数、成功写入数、跳过数及原因，并说明未完成的视觉或联网验证。
