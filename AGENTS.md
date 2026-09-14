# AGENTS.md

This file is the root project memory for coding agents working in this repository. Read it before editing.

## Project Mission

This repository is an AI-assisted invoice parser for utility and telecom invoices. It combines:

- Local OCR and PDF preparation.
- Deterministic invoice field extraction.
- Provider-specific RAG memory.
- Gemini PDF second-pass extraction.
- A local browser dashboard for import, review, correction, feedback, and export.

Supported invoice categories:

- `electricity`
- `water`
- `natural gas`
- `telecom`

Unsupported documents must be classified as `unsupported` or `valid_invoice=false`. Do not force unsupported files into a supported invoice type.

## Architecture

Keep the project as a lightweight Python app unless the user explicitly asks for a deeper packaging refactor. The scripts in `scripts/` are stable commands and should remain runnable:

- `scripts/dashboard.py`: local HTTP dashboard, import workflow, manual review, provider memory updates, CSV export, Gemini import controls, and PDF preview.
- `scripts/gradio_app.py`: loopback-only local Plan A/Plan B lab and provider-memory viewer; Plan B Gemini extraction and the independent judge are separate explicit opt-in calls.
- `scripts/ocr_text_extraction.py`: OCR/text extraction pipeline for PDFs and image files.
- `scripts/extract_invoice_fields.py`: deterministic parser that reads OCR text files and writes structured invoice rows.
- `scripts/second_pass_llm.py`: Gemini PDF second-pass orchestration with RAG context, TXT parsing, normalization, validation, and stable artifact output.
- `llm/agent/models.py`: typed LLM prompt, payload, normalized extraction, and runtime result models.
- `llm/agent/prompts.py`: LLM prompt templates.
- `llm/agent/workflow.py`: experimental Plan B coded-agent state machine and isolated A/B artifacts.
- `llm/agent/judge.py`: optional independent A/B evaluator for a user-configured OpenAI-compatible endpoint.
- `llm/api/schemas.py`: typed request/output schemas for API-style integrations.
- `rag/adaptive_rag.py`: local JSON provider memory adapter, OCR corrections, feedback recording, provider-scoped retrieval, and conversion into LLM-facing `RagSnippet` objects.

Shared reusable code belongs in `invoice_parser/`:

- `invoice_parser/paths.py`: shared project path constants and data-directory discovery helpers.
- `invoice_parser/schema.py`: shared structured invoice CSV schema.
- `invoice_parser/text_utils.py`: shared text, null, and money normalization helpers.
- `invoice_parser/providers.py`: open provider recognition and canonical IDs shared by extraction and memory. Aliases are conveniences, not an allowlist; arbitrary named suppliers receive distinct IDs.

Future splitting should move reusable logic into focused modules while preserving the `scripts/` command entry points. Good future targets:

- `invoice_parser/extraction/` for date, money, provider, classification, and row-extraction logic.
- `invoice_parser/ocr/` for OCR scoring, PDF/image preparation, and OCR pipeline helpers.
- `invoice_parser/dashboard/` for dashboard state, actions, and rendering helpers.

Do not introduce a `src/` layout unless the user explicitly requests it.

## Important Files And Folders

- `README.md`: user-facing setup, commands, workflow, and structure.
- `A_B_test.md`: Plan A/Plan B experiment architecture, agent responsibilities, and interaction model.
- `AGENTS.md`: project memory and coding conventions.
- `.gitignore`: local/generated artifact ignore rules.
- `.dockerignore`: excludes secrets, invoice data, runtime memory, caches, tests, and documentation from container build context.
- `.env.example`: placeholder environment configuration only; never store live keys.
- `scripts/`: runnable entry-point scripts.
- `llm/agent/` and `llm/api/`: mirrored typed LLM model/schema structure used by `scripts/second_pass_llm.py`.
- `docs/workflow_graph.md`: Mermaid workflow diagram showing source, private local invoice data, runtime memory, generated outputs, review, tests, logs, and documentation boundaries.
- `deploy/aws/`: ephemeral CloudFormation/SSM deployment, status, destruction, and operating instructions.
- `pyproject.toml`: the only direct dependency and project metadata source; tests use the `dev` dependency group.
- `uv.lock`: reproducible resolution for local and Docker environments.
- `tests/`: pytest tests and smoke tests.
- `docs/`: project notes and supporting documentation that are not runtime entry points.
- `rag/knowledge_base.json`: generated/local persisted provider memory when provider memory exists.
- `rag/last_retrieval_context.json`: generated/local exported retrieval context sample when retrieval is exported.
- `data/data_raw/`: original uploaded/source invoice files. Treat as immutable and never commit them.
- `data/data_pdf/`: canonical/searchable PDFs generated or copied by OCR processing.
- `data/data_txt/`: selected OCR text files and OCR diagnostics.
- `data/data_processed/invoice_structured_fields.csv`: dashboard/manual-review CSV.
- `data/data_processed/reports/`: OCR report JSON/CSV files.
- `data/data_processed/llm_second_pass/`: generated Gemini raw TXT and structured JSON artifacts.
- `data/data_processed/agentic_ab_tests/`: isolated Plan A/Plan B comparisons, agent traces, and human accuracy verdicts.

The `data/` tree is ignored because invoices and generated artifacts may contain private information. Let dashboard import/OCR commands create the needed local folders. A file that exists only in `data/data_raw/` is a raw input, not a processed invoice.

## Main Commands

Use PowerShell examples for this Windows repo.

Install the locked core and development dependencies:

```powershell
uv sync --frozen
```

Run OCR over the default raw data folder:

```powershell
uv run python scripts\ocr_text_extraction.py
```

Run OCR for one file:

```powershell
uv run python scripts\ocr_text_extraction.py --input data\data_raw\agua_01.webp
```

Extract structured invoice fields from OCR text:

```powershell
uv run python scripts\extract_invoice_fields.py
```

Seed or refresh provider memory from the structured CSV:

```powershell
uv run python rag\adaptive_rag.py init
```

Retrieve provider-specific context:

```powershell
uv run python rag\adaptive_rag.py retrieve --text-file data\data_txt\telecom_05.txt --provider vodafone --invoice-type telecom
```

Run Gemini PDF second pass for one invoice:

```powershell
$env:GEMINI_API_KEY="your-real-key"
$env:GEMINI_MODEL="gemini-3.5-flash"
uv run python scripts\second_pass_llm.py --pdf-file data\data_pdf\telecom_05.pdf --text-file data\data_txt\telecom_05.txt --provider vodafone --invoice-type telecom
```

Run the dashboard:

```powershell
uv run python scripts\dashboard.py
uv run python scripts\gradio_app.py
```

Default dashboard URL:

```text
http://127.0.0.1:8501
http://127.0.0.1:7860
```

Run tests:

```powershell
uv run pytest
```

## Development Rules

- Inspect existing files before editing.
- Make the smallest useful change that satisfies the task.
- Preserve documented commands and runtime behavior unless the user explicitly asks for a behavior change.
- Do not rewrite unrelated files.
- Do not introduce new dependencies unless clearly needed.
- Prefer `pathlib.Path` for file paths.
- Keep changes compatible with Windows paths and PowerShell examples.
- Keep the `scripts/` entry points directly runnable from the project root.
- Use package-qualified imports such as `from scripts.ocr_text_extraction import process_file` from dashboard/background jobs so commands work from the documented project root.
- Keep raw files in `data/data_raw/` immutable.
- Store generated OCR, CSV, JSON, and report outputs under `data/data_pdf/`, `data/data_txt/`, `data/data_processed/`, or `rag/` as appropriate.
- Keep shared constants and small reusable helpers in `invoice_parser/` instead of duplicating them across entry-point scripts.
- Keep OCR, deterministic extraction, Gemini second-pass extraction, RAG memory, dashboard review, and validation logic separated by clear module boundaries.
- Keep Plan B experimental and side-effect free with respect to the review CSV and provider memory. Agent traces must not include API keys or full OCR/PDF content. A/B accuracy claims require a human verdict or labeled ground truth. The LLM judge remains advisory, outside Plan B, and cannot route or approve invoices.
- Keep LLM schemas/models in `llm/agent/` and `llm/api/`; keep the executable second-pass workflow in `scripts/second_pass_llm.py`.
- RAG may import `llm.agent.models.RagSnippet` for prompt-ready context, but provider memory ownership stays in `rag/adaptive_rag.py`.
- Do not add a model-download or vector-embedding dependency without explicit user authorization. Do not add a `src/` layout unless the user explicitly asks.
- When splitting large files, preserve direct `scripts/` commands so existing workflows keep working.
- Do not delete historical artifacts, generated reports, or old timestamped Gemini artifacts unless the user explicitly approves deletion.
- Summarize changed files and checks after every edit.

## Security And Privacy

- Treat invoice files as potentially private.
- Do not print full invoice contents in normal logs or final responses.
- Do not expose `.env` contents, credentials, tokens, API keys, or private invoice data.
- Keep `.env` ignored.
- Keep `.env.example` as placeholders only.
- Keep `.gitignore` active for local caches, logs, environments, secrets, and runtime RAG memory.
- Keep the ignored `data/` tree local. Do not commit raw invoices, OCR outputs, generated reports, structured rows, or Gemini artifacts.
- Keep `rag/knowledge_base.json` and `rag/last_retrieval_context.json` ignored because they can contain local reviewer feedback and retrieval traces.
- Legacy `env.example` and `gitignore` files may exist from earlier setup; `.env.example` and `.gitignore` are the active convention.
- Dashboard API key fields are request-only; do not persist or log them.
- Validate user-supplied file paths and uploaded filenames.
- Prevent path traversal when serving PDFs or writing uploaded files.
- Avoid unsafe shell command construction.
- Do not overwrite raw source invoices in `data/data_raw/`.

## OCR Pipeline Notes

`ocr_text_extraction.py` prepares first-pass text. It should not replace final structured invoice extraction logic.

Current behavior:

- Accepts PDFs and image files: `.pdf`, `.png`, `.jpg`, `.jpeg`, `.webp`, `.tif`, `.tiff`, `.bmp`.
- Converts images to PDFs when needed.
- Detects whether a PDF already has embedded text.
- Uses PDF text directly when good text is available.
- Uses OCRmyPDF/Tesseract when OCR is required.
- `--force-enhanced` enables OCRmyPDF cleanup/oversampling options for the OCR pass; the active implementation does not run a separate baseline-vs-enhanced comparison pass.
- Scores OCR quality using invoice-specific signals.
- OCR scoring, diagnostics, and comparison must not depend on a provider-brand keyword list.
- Writes selected text files to `data/data_txt/`.
- Writes canonical/searchable PDFs to `data/data_pdf/`.
- Writes JSON/CSV OCR reports to `data/data_processed/reports/`.
- Can call `scripts/second_pass_llm.py` / `run_llm_second_pass(...)` when OCR quality is low.

Local OCR dependencies:

- `pytesseract`
- Tesseract executable
- OCRmyPDF
- PyMuPDF
- Pillow
- qpdf/OCRmyPDF runtime tools when OCR is needed

Be careful with hardcoded Tesseract paths in `ocr_text_extraction.py`. If changing OCR configuration, prefer robust environment/path detection over machine-specific paths.

## Structured Extraction Schema

The shared schema lives in `invoice_parser/schema.py`. `extract_invoice_fields.py` writes `data/data_processed/invoice_structured_fields.csv` with these fields:

- `source_file`
- `ocr_text_file`
- `valid_invoice`
- `invoice_type`
- `invoice_number`
- `invoice_date`
- `currency`
- `payment_due_date`
- `provider_name`
- `provider_vat_number`
- `provider_address`
- `buyer_name`
- `buyer_vat_number`
- `buyer_address`
- `service_plan_name`
- `consumption_start_date`
- `consumption_end_date`
- `units_of_consumption`
- `unit_type`
- `subtotal_value`
- `total_vat`
- `total_value`
- `extraction_warnings`

Use `null` for missing values. Do not hallucinate fields. If a value is missing, unreadable, ambiguous, or unsupported, keep it `null` and add an extraction warning when useful.

Dates should be normalized to `YYYY-MM-DD`. Money values should be normalized to decimal strings where possible.

## Classification And Validation

Current classification is deterministic and uses filename prefixes plus invoice keywords:

- `agua_*` maps to `water`.
- `gas_*` maps to `natural gas`.
- `luz_*` maps to `electricity`.
- `telecom_*` maps to `telecom`.
- Otherwise, content keywords are used.
- Documents without enough invoice signals should become unsupported or invalid, not guessed.

Dashboard validation expectations:

- Critical fields include invoice type, invoice number, invoice date, currency, provider name, provider VAT number, and total value.
- Dates must be parseable as `YYYY-MM-DD` after extraction.
- Money fields must be numeric after normalization.
- `consumption_start_date` must not be after `consumption_end_date`.
- Rows with `extraction_warnings` require manual review before automatic approval.
- Automatic approval requires at least 5 previously validated invoices for the provider and no validation errors.

## Gemini Second Pass

`scripts/second_pass_llm.py` sends the PDF invoice to Gemini and includes first-pass OCR text as optional supporting context. RAG context from `rag/adaptive_rag.py` is included as guidance.

Rules:

- The real extraction input is the PDF file.
- OCR text is optional supporting context, not the primary input.
- Gemini credentials come from `GEMINI_API_KEY`, from the dashboard Import Invoices form, or from the Manual Review Gemini PDF Pass form.
- Default model is `gemini-3.5-flash`.
- Real Gemini PDF calls use the `google-genai` SDK. Tests must keep using injected or monkeypatched callers and must not call the external API.
- Raw Gemini TXT response and normalized structured output must be saved separately.
- New second-pass runs use stable filenames and overwrite prior artifacts for the same invoice:
  - `{stem}_gemini_raw.txt`
  - `{stem}_gemini_structured.json`
- Do not create timestamped duplicate Gemini artifacts for new runs.
- Old timestamped artifacts may exist from earlier runs; do not delete them unless explicitly asked.
- Gemini fields can be applied to `invoice_structured_fields.csv` for Manual Review.

## RAG And Provider Memory

Provider memory lives in `rag/knowledge_base.json` and is managed by `rag/adaptive_rag.py`. Retrieval is deterministic, local, and scoped to the identified provider.

The knowledge base stores:

- Provider aliases.
- Provider-specific extraction tips.
- Common OCR corrections.
- Known invoice layouts.
- Previously validated invoices.
- Human reviewer feedback.
- Field correction patterns learned from manual review.
- Validation history.

Use `build_extraction_context(...)` for deterministic JSON context before improving extraction logic. Use `record_feedback(...)` or the dashboard review workflow to store reviewer corrections.

Use `build_llm_rag_snippets(...)` when Gemini prompt code needs provider memory. It reads only the matching provider's JSON memory and returns typed `RagSnippet` objects from `llm.agent.models`.

When fields are corrected in the dashboard, store both the corrected row and structured RAG feedback. Field-level corrections should update provider `human_reviewer_feedback`, `field_correction_patterns`, and validation history so future extraction and Gemini prompts can learn from them.

Do not treat one provider's feedback as a universal rule unless the user explicitly confirms it.

Known provider IDs include:

- `epal`
- `eamb`
- `vodafone_pt`
- `vodafone_tr`
- `galp`
- `edp`
- `eem`
- `arm`
- `unknown`

New named suppliers use stable `provider_...` IDs. Retrieval must only apply the identified provider's feedback. Unidentified suppliers cannot gain approval eligibility from the shared legacy `unknown` bucket. Existing mixed or misidentified memory needs manual source review, not automatic reassignment.

## Dashboard Workflow

`dashboard.py` runs a local HTTP dashboard with these main views:

- Overview
- Import Invoices
- Manual Review
- RAG Context: selected invoice details plus compact provider context relevant to that invoice.
- Provider Memory: provider-wide memory, examples, tips, corrections, and history.
- Invoice Fields
- Export

Dashboard responsibilities:

- Load or generate structured CSV rows.
- Import uploaded invoices into `data/data_raw/`.
- If an uploaded filename already exists only in `data/data_raw/`, process the existing raw file without overwriting it. Treat matching PDF/text artifacts or CSV rows as already processed duplicates.
- Run Import Invoices processing as a background job and show the shared batch progress panel in the Import tab.
- Run OCR for supported imported files.
- Optionally run Gemini PDF extraction during import using an API key entered in the Import Invoices form.
- Rebuild structured extraction rows.
- Apply Gemini fields into the Manual Review CSV when requested.
- Show dashboard invoice status as `OCR`, `LLM`, or `Approved`: `OCR` for first-pass extraction rows, `LLM` when a Gemini artifact exists or Gemini fields were applied, and `Approved` only after the dashboard approval action records human approval.
- Show Manual Review completion %, validation error count, and provider memory count.
- Seed provider memory from the CSV.
- Allow human approval, rejection, correction, re-extraction, Gemini PDF pass with a request-only API key, and applying latest Gemini fields.
- Store reviewer feedback and provider memory updates.
- Serve source PDFs only from `data/data_pdf/`.
- Keep the RAG Context page scoped to the selected invoice. It may show compact provider context relevant to that invoice, such as provider tips, OCR corrections, matching layouts, and field correction patterns. Do not show broad provider history or full memory tables there; use Provider Memory for that broader view.

When editing dashboard code, preserve path safety checks for PDF serving and upload filename sanitization.

## Docker And API Runtime

`Dockerfile` is a multi-stage, non-root runtime containing required local OCR tools. `docker-compose.yaml` runs the FastAPI health service on port `8000`, the review dashboard on port `8501`, and the Gradio A/B lab on port `7860` from the same image. The services persist `/app/data` and `/app/runtime`; none should write application code. The API is not an OpenAI-compatible inference server.

Deployment is manual for this private college project. Do not add GitOps, pull-request workflows, CI/CD workflows, Vercel configuration, repository deployment hooks, or automatic application releases. Do not create or publish a PR for project changes. AWS scripts must remain explicitly operator-invoked; the scheduled AWS action is limited to deleting the temporary stack.

The AWS deployment under `deploy/aws/` is deliberately ephemeral. It must use an IP-restricted security group, SSM instead of SSH, no uploaded LLM secrets or invoice data, and both scheduled and manual stack-deletion paths. It deploys the invoice application only, not the historical Qwen GPU endpoint. Do not claim an AWS deployment passed unless it was run with an authenticated profile and its stack deletion was verified.

## Testing And Verification

For code changes, run the smallest practical smoke check:

- Shared helpers or extraction logic: `uv run pytest`.
- Gemini second pass only: `uv run pytest tests\test_second_pass.py`.
- Deterministic extraction changes: `uv run python scripts\extract_invoice_fields.py`, then verify the CSV is generated.
- RAG memory changes: `python rag\adaptive_rag.py init`, then verify the knowledge base refreshes.
- Gradio behavior changes: run `uv run python scripts\gradio_app.py`, verify loopback access, and exercise both A/B output panels.
- Dashboard behavior changes: import the module, then run `uv run python scripts\dashboard.py` if manual review is needed.

Do not claim tests passed unless they were actually run.

If adding tests, focus on:

- Shared schema and path constants.
- Gemini prompt construction.
- Stable second-pass artifact output.
- Missing API key handling.
- PDF requirement handling.
- Unparseable Gemini TXT response handling.
- `extract_invoice_type`
- `normalize_money`
- Date parsing.
- Unsupported document rejection.
- Provider canonicalization.
- RAG feedback persistence.
- Dashboard validation rules.

## Documentation Expectations

Keep documentation practical and aligned with the actual project files.

Root `README.md` should include:

- Project purpose.
- Supported invoice types.
- Setup steps for Python, OCR dependencies, and Gemini configuration.
- Commands for OCR, extraction, RAG memory, Gemini second pass, and dashboard.
- Explanation of `data/data_raw`, `data/data_pdf`, `data/data_txt`, `data/data_processed`, and `data/data_processed/llm_second_pass`.
- Manual review workflow.
- Known limitations.

Update `AGENTS.md` when stable project conventions change. Update `README.md` when user-facing structure, setup, or commands change.

## Known Limitations

- Some text contains visible encoding artifacts from OCR or source files.
- OCR depends on local Tesseract/OCRmyPDF installation and PATH configuration.
- Deterministic extraction is regex/heuristic logic.
- Gemini second pass depends on API key, model access, quota, and network availability.
- Provider memory is JSON-based and uses deterministic provider-scoped retrieval.
- Clean checkouts contain no invoice data; import/OCR/extraction workflows create the ignored local folders.
- Existing historical reports and old timestamped Gemini artifacts may contain stale generated data from earlier runs.

## Conflict Markers

If merge conflict markers such as `<<<<<<<`, `=======`, or `>>>>>>>` appear in project files, resolve them carefully and preserve the intended content.

## Final Response Requirements

After every change, summarize:

- Files created or changed.
- Why each change was needed.
- Tests or checks run.
- Commands to run the project.
- Assumptions made.
- Known limitations.
- What should be reviewed manually before committing.

Do not claim tests passed unless they were actually run.
