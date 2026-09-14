# A. About this project / Invoice Parser Agent

AI-assisted invoice parser for utility and telecom invoices.
The project combines local OCR, deterministic field extraction, provider-specific RAG memory, an agentic A/B workflow, an optional independent LLM judge, a local Gradio lab, a Gemini PDF second pass, and a review dashboard.

This document is organized in these sections:
A. About project
B. Setup
C. Execution
D. Security and Limitations
E. Extra information

## Supported Invoices

- Electricity
- Water
- Natural gas
- Telecom

Note: Unsupported invoices are rejected and marked invalid.

Invoices can come from any company within these categories, including unfamiliar electricity and water suppliers. OCR quality does not depend on a company-name list. Provider aliases (including EEM and ARM) help recognition but do not restrict acceptance. New seller names get separate provider memory; missing seller names require manual review.

See [the assessment requirements review](docs/requirements_review.md) for implemented changes, remaining gaps, and a prioritized improvement plan. Existing rows previously labeled `unknown` or incorrectly labeled EDP need source-based review; their history is not automatically migrated.


## Project Structure

```text
|-- AGENTS.md
|-- README.md
|-- pyproject.toml
|-- uv.lock
|-- .env.example
|-- .gitignore
|-- .dockerignore
|-- Dockerfile
|-- docker-compose.yaml
|-- deploy/
|   |-- aws/
|       |-- deploy.ps1
|       |-- status.ps1
|       |-- destroy.ps1
|       |-- ephemeral-stack.yaml
|-- scripts/
|   |-- gradio_app.py
|   |-- dashboard.py
|   |-- ocr_text_extraction.py
|   |-- extract_invoice_fields.py
|   |-- second_pass_llm.py
|-- invoice_parser/
|   |-- paths.py
|   |-- schema.py
|   |-- text_utils.py
|-- docs/
|   |-- workflow_graph.md
|-- llm/
|   |-- __init__.py
|   |-- agent/
|   |   |-- models.py
|   |   |-- prompts.py
|   |-- api/
|   |   |-- schemas.py
|-- rag/
|   |-- adaptive_rag.py
|   |-- knowledge_base.json              (generated/local, when provider memory exists)
|   |-- last_retrieval_context.json      (generated/local, when retrieval is exported)
|-- vector_store/
|   |-- base.py                          (local Torch embedding + FAISS/LlamaIndex)
|   |-- documents.py
|-- tests/
|   |-- test_shared_utils.py
|   |-- test_second_pass.py
|-- data/
|   |-- data_raw/
|   |-- data_pdf/
|   |-- data_txt/
|   |-- data_processed/
|       |-- invoice_structured_fields.csv
|       |-- reports/
|       |-- llm_second_pass/
```

Folder description:
- `scripts/`: runnable entry-point scripts for Gradio, the review dashboard, OCR, extraction, and A/B testing.
- `data/data_raw/`: original uploaded invoices waiting to be processed. Do not overwrite existing files or commit invoice data.
- `data/data_pdf/`: canonical/searchable PDFs generated or copied by OCR processing.
- `data/data_txt/`: selected OCR text and diagnostics.
- `data/data_processed/`: structured CSV, reports, and generated outputs.
- `data/data_processed/llm_second_pass/`: Gemini raw TXT and normalized JSON outputs.
- `rag/`: provider memory adapter and provider-scoped retrieval utilities.
- `vector_store/`: FAISS/LlamaIndex indexing with a deterministic Torch embedding; it does not use or download Hugging Face models.
- `llm/`: typed LLM models, prompt templates, and request/response schemas.
- `invoice_parser/`: shared schema, paths, and normalization helpers used by the pipeline entry points.
- `docs/`: supporting project notes.

Notes:
- The `data/` tree is ignored because invoices and generated extraction artifacts can contain private customer information. The pipeline creates its folders locally as needed.



## Workflow visualization:

- [docs/workflow_graph.md](docs/workflow_graph.md) shows the project pipeline and the boundary between private local invoice data, runtime memory, source code, generated outputs, tests, logs, and documentation.


## LLM module structure

It includes:
- `scripts/second_pass_llm.py`: orchestration, Gemini TXT parsing, normalization, validation, artifact writing, and CLI.
- `llm/agent/models.py`: typed prompt, payload, normalized extraction, and runtime result models.
- `llm/agent/prompts.py`: prompt templates.
- `llm/api/schemas.py`: typed request/output schemas for API-style integrations.
- `rag/adaptive_rag.py`: retrieves provider-specific tips and approved reviewer feedback from the local JSON memory and converts them into `llm.agent.models.RagSnippet` objects.



# B. Setup

It is recommended that you use VS Code (or other similar application) to configure and run code.
Configuration details are based on a local Windows environment. Must be adapted to Mac/Linux environments.

1. Open VS Code.
In terminal, create local folder project and run project in an venv context.
(Replace folder\user with your own preference):
```bash
mkdir C:\Users\<Your-User>\Documents\AgenticAI_Billing_KN
cd C:\Users\<Your-User>\Documents\AgenticAI_Billing_KN
```

2. If you do not have `uv`, install it with WinGet:
```powershell
winget install --id=astral-sh.uv -e
```

3. Clone the project:
```bash
git clone <repository-url> .

```

4. Create the project environment and install the locked runtime and test dependencies:
```bash
uv sync --frozen
```

5. Local OCR requires Tesseract and OCRmyPDF runtime tools available on PATH.
On Windows, import Tesseract with:
```bash
uv run python -c "import pytesseract; print(pytesseract.get_tesseract_version())"
```

## Gemini Configuration

A Gemini API key is needed only for Gemini extraction. OCR, deterministic extraction, the dashboard, and mocked tests run without one.
If you don't have one, register on https://aistudio.google.com/ and create your own key (Free).

For command-line use, set:
```bash
$env:GEMINI_API_KEY="your-real-key"
$env:GEMINI_MODEL="gemini-3.5-flash"
```

Notes:
- The dashboard also has a GEMINI_API_KEY field in **Import Invoices**. It is used only for to import request and is not saved.
- Gemini PDF extraction uses the locked `google-genai` dependency declared in `pyproject.toml`.
- Tests continue to use mocked callers and do not call the real API.



# C. Execution

The simplest local test is the Gradio A/B lab. The batch commands and full review dashboard remain available for deeper testing.

## Local Gradio A/B lab

Start the loopback-only server:

```powershell
uv run python scripts\gradio_app.py
```

Open [http://127.0.0.1:7860](http://127.0.0.1:7860). Upload an invoice PDF or image; the app runs the OCR stage automatically. The interface shows Plan A and Plan B side by side, the five Plan B agent events, comparison metrics, the local provider knowledge base, a FAISS index rebuild action, and a human verdict control.

Plan B Gemini extraction is disabled by default. Without it, Plan B still runs classification, provider-memory retrieval, deterministic fallback extraction, validation, and review routing. Enable **Optional Plan B Gemini extraction** and provide a request-only key to run the stronger OCR + LLM + agent comparison. The uploaded invoice, OCR evidence, and retrieved provider context are sent to Gemini only for that explicitly enabled run; the key is not written to the A/B artifact.

The **Optional independent LLM judge** panel can compare both results with the OCR evidence through a user-configured OpenAI-compatible endpoint. It sends invoice text only after **Run LLM judge** is clicked and never replaces the human verdict. Configure the fields in the panel or set:

```powershell
$env:JUDGE_BASE_URL="http://127.0.0.1:8000/v1"
$env:JUDGE_MODEL="your-served-model-name"
$env:JUDGE_API_KEY="only-if-your-server-requires-one"
```

The project does not download a judge model. For a credible evaluation, use a judge model that differs from the model used by Plan B and report agreement with human-labeled invoices.

The server rejects non-loopback host arguments and launches with Gradio sharing disabled.

The batch workflow has two steps:

1. Batch processing to extract text from files with OCR.
2. Full dashboard review.


## 1. Step-by-step batch processing to extract text from file(s)

### 1.1 OCR text extraction processes all files in `data/data_raw/`.
New invoices can be added manually to `data/data_raw/`.
Dashboard can also be used to import invoices before running OCR.
Note:
- The dashboard import flow processes an existing raw file with the same name instead of overwriting it.

```bash
uv run python scripts\ocr_text_extraction.py
```

Note: OCR process for one file only. Example:
```bash
uv run python scripts\ocr_text_extraction.py --input data\data_raw\agua_01.webp
```

Note: The OCR command skips work when the final selected text file already exists.
`--force-enhanced` runs OCRmyPDF with enhanced cleanup/oversampling options when OCR is required; it does not run a separate baseline-vs-enhanced comparison pass.


### 1.2 Extract structured fields from OCR output text file:
```bash
uv run python scripts\extract_invoice_fields.py
```

### 1.3 Refresh provider memory from the structured CSV:
```bash
uv run python rag\adaptive_rag.py init
```

### 1.4 Run Gemini PDF 2nd pass for one invoice:
```bash
uv run python scripts\second_pass_llm.py --pdf-file data\data_pdf\telecom_05.pdf --text-file data\data_txt\telecom_05.txt --provider vodafone --invoice-type telecom
```

Gemini outputs are written with fixed filenames, for example:
```text
data/data_processed/llm_second_pass/telecom_05_gemini_raw.txt
data/data_processed/llm_second_pass/telecom_05_gemini_structured.json
```

New second-pass runs overwrite these stable files for the same invoice instead of creating timestamped duplicates.
The raw Gemini response is plain TXT; the structured artifact remains JSON for dashboard review and export.


## 2. Dashboard operation

Start the dashboard:
```bash
uv run python scripts\dashboard.py
```

Open:
[http://127.0.0.1:8501](http://127.0.0.1:8501)

Main views:
- **Overview**: processing queue, status, completion, and errors.
- **Import Invoices**: upload invoices, optionally provide Gemini API key/model, run OCR and Gemini PDF extraction.
- **Manual Review**: inspect fields, completion %, validation errors, PDF preview, Gemini second-pass summary, and save corrections.
- **Experimental Agentic A/B Test** (inside Manual Review): compare the current deterministic Plan A with coded-agent Plan B, inspect changed fields and routing decisions, optionally request an advisory LLM judge result in Gradio, then record a human accuracy verdict. The experiment does not alter the reviewed CSV or provider memory.
- **RAG Context**: selected invoice details, compact provider context, validation state, Gemini summary, and OCR text.
- **Provider Memory**: provider-wide tips, OCR corrections, feedback, examples, and validation history.
- **Invoice Fields**: required schema reference.
- **Export**: download CSV and provider memory.

Import duplicate handling:
- A file is considered already processed when a matching PDF, OCR text file, or CSV row exists.
- A matching file that exists only in `data/data_raw/` is processed from that existing raw input and is not overwritten by the upload.

Dashboard invoice statuses:
- `OCR`: the invoice has first-pass OCR/deterministic extraction fields.
- `LLM`: a Gemini PDF pass artifact exists or Gemini fields were applied to the row.
- `Approved`: a human reviewer approved the invoice from the dashboard.

Import progress:
- Import Invoices runs uploads, OCR, field extraction, and optional Gemini extraction as a background job.
- The Import tab shows the same progress bar and per-file status list used by batch extraction.

Manual Review actions:
- Save corrections to memory.
- Approve invoice.
- Reject invoice.
- Re-extract selected OCR.
- Run Gemini PDF pass with an API key entered on the Manual Review page.
- Apply latest Gemini fields.
- Run an isolated agentic A/B test and record whether Plan A, Plan B, or neither is more accurate.

## Experimental coded-agent Plan B

See [A_B_test.md](A_B_test.md) for the architecture diagram, agent responsibilities, interaction pattern, and comparison with MoA.

Plan B is an explicit state machine of five coded agents:

1. Classification agent identifies the supported invoice category.
2. Provider-memory agent retrieves guidance only for the identified supplier.
3. Extraction agent runs Gemini with the PDF, OCR context, and retrieved guidance; it records a deterministic fallback when Gemini cannot run.
4. Validation agent checks required fields, real dates, finite non-negative values, date order, and subtotal/VAT/total consistency.
5. Review-routing agent rejects unsupported documents, requires manual review, or marks an invoice eligible for automatic approval after five distinct prior human/automatic approvals.

Each run writes an inspectable trace and side-by-side result under `data/data_processed/agentic_ab_tests/`. API keys are request-only and are not written to the artifact. Completion and validation-error deltas are operational measures; only the saved human verdict supplies an accuracy comparison.

Run Plan B from the Manual Review page, or from PowerShell:

```powershell
$env:GEMINI_API_KEY="your-real-key"
uv run python scripts\agentic_ab_test.py --text-file data\data_txt\agua_01.txt --pdf-file data\data_pdf\agua_01.pdf
```

Without a PDF/API key, the CLI still records the orchestration trace but the extraction agent explicitly retains the deterministic result, so it is not a meaningful model comparison.




## 3. Tests

For test purpose run the current test suite:
```bash
uv run pytest
```

The tests mock Gemini API calls and do not call the real API.

# D. Security and Limitations

## Security Notes

- Keep `.env` ignored.
- Keep `.env.example` as placeholders only.
- Keep `.gitignore` active so local caches, logs, environments, secrets, and runtime RAG memory are not committed accidentally.
- The `data/` tree is ignored. Keep invoice files, OCR outputs, reports, structured rows, and LLM artifacts local.
- Do not commit real API keys, credentials, or unapproved private invoice data.
- Do not overwrite raw files in `data/data_raw/`.
- Historical generated reports may contain private or stale data; do not commit them.

## Known Limitations

- OCR quality depends on local Tesseract/OCRmyPDF installation.
- Some source/OCR text may contain encoding artifacts.
- Deterministic extraction is regex/heuristic-based.
- Gemini second pass requires API access, quota, and network availability.
- Provider memory uses local JSON plus an optional FAISS/LlamaIndex index. Its deterministic Torch feature-hash embedding requires no model repository or model download. Torch uses CUDA automatically when available; FAISS remains the CPU package.
- Fresh clones do not contain invoice data; local invoice import or pipeline commands create the ignored `data/` tree.
- Old timestamped Gemini artifacts may exist from earlier development runs.



# E. Extra information

## Docker And API

The multi-stage Docker image includes Portuguese/English Tesseract, OCRmyPDF runtime tools, a non-root user, a read-only application filesystem, and health checks. Compose runs three services from the same image:

- FastAPI health service: `http://127.0.0.1:8000/health`
- Invoice dashboard: `http://127.0.0.1:8501/`
- Gradio A/B lab: `http://127.0.0.1:7860/`

Create local writable directories and start the services:

```powershell
New-Item -ItemType Directory -Force data, runtime
docker compose up --build -d
docker compose ps
```

`data/` holds invoice inputs and generated artifacts. `runtime/` holds ignored provider memory and the generated vector index. Compose binds all ports to localhost unless `BIND_ADDRESS` is explicitly changed. The API on port 8000 is a health/root service; it is not an OpenAI-compatible `/v1` model server.

For the temporary self-deleting EC2 deployment, AWS SSO setup, old endpoint check, and early deletion workflow, see [deploy/aws/README.md](deploy/aws/README.md).

This private college project has no GitOps, pull-request automation, CI/CD, or Vercel deployment configuration. Local and AWS operations are started manually by an authorized operator.


## Extraction Schema

The structured CSV uses these fields:

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

Missing or uncertain values are `null` by default.


## RAG Memory

Provider memory is stored in:
```text
rag/knowledge_base.json
```

The generated vector index is stored in `data/data_processed/vector_store/` for direct local runs and `/app/runtime/vector_store/` under Compose. Build or refresh it with:

```powershell
uv run python rag\adaptive_rag.py index --force
```

It can store:
- Provider aliases.
- Provider-specific extraction tips.
- OCR corrections.
- Known layouts.
- Previously validated invoices.
- Human reviewer feedback.
- Validation history.

To retrieve context manually run (with example):
```bash
uv run python rag\adaptive_rag.py retrieve --text-file data\data_txt\telecom_05.txt --provider vodafone --invoice-type telecom
```

Provider retrieval prefers matching FAISS results and falls back to provider-scoped JSON when an index is absent. The embedding is computed locally with Torch and never applies one supplier's feedback to another supplier.
