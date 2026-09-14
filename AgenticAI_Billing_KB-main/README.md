# A. About this project / Invoice Parser Agent

AI-assisted invoice parser for utility and telecom invoices. 
The project combines local OCR, deterministic field extraction, provider-specific RAG memory, a Gemini PDF second pass and a local dashboard for human review.

This documment is organized in these sections:
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
|-- requirements.txt
|-- .env.example
|-- .gitignore
|-- .dockerignore
|-- Dockerfile
|-- docker-compose.yaml
|-- deploy/
|   |-- publish_to_iseg.ps1
|   |-- aws/
|       |-- deploy.ps1
|       |-- status.ps1
|       |-- destroy.ps1
|       |-- ephemeral-stack.yaml
|-- scripts/
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
|-- vector_store/
|   |-- base.py
|   |-- documents.py
|-- rag/
|   |-- adaptive_rag.py
|   |-- knowledge_base.json              (generated/local, when provider memory exists)
|   |-- last_retrieval_context.json      (generated/local, when retrieval is exported)
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
|       |-- vector_store/
```

Folder description:
- `scripts/`: runnable entry-point scripts for the dashboard, OCR, and deterministic extraction.
- `data/data_raw/`: original uploaded invoices waitinting to be processed. Do not overwrite existing files. This folder is trackable, so review invoice contents before committing.
- `data/data_pdf/`: canonical/searchable PDFs generated or copied by OCR processing.
- `data/data_txt/`: selected OCR text and diagnostics.
- `data/data_processed/`: structured CSV, reports, and generated outputs.
- `data/data_processed/llm_second_pass/`: Gemini raw TXT and normalized JSON outputs.
- `data/data_processed/vector_store/`: generated FAISS/LlamaIndex provider-memory index.
- `rag/`: provider memory adapter and retrieval utilities.
- `llm/`: typed LLM models, prompt templates, and request/response schemas.
- `vector_store/`: local vector-store builder and provider-memory document conversion.
- `invoice_parser/`: shared schema, paths, and normalization helpers used by the pipeline entry points.
- `docs/`: supporting project notes.

Notes:
- The `data/` tree is intentionally trackable in this repository. A fresh checkout contains shared invoice data only when it has been committed; otherwise the pipeline creates generated output folders as needed. 



## Workflow visualization:

- [docs/workflow_graph.md](docs/workflow_graph.md) shows the project pipeline and the boundary between trackable invoice data, local runtime memory, source code, generated outputs, tests, logs, and documentation.


## LLM module structure

It includes:
- `scripts/second_pass_llm.py`: orchestration, Gemini TXT parsing, normalization, validation, artifact writing, and CLI.
- `llm/agent/models.py`: typed prompt, payload, normalized extraction, and runtime result models.
- `llm/agent/prompts.py`: prompt templates.
- `llm/api/schemas.py`: typed request/output schemas for API-style integrations.
- `vector_store/base.py`: FAISS/LlamaIndex vector index factory in the same style as `Agenti-AI-main`.
- `vector_store/documents.py`: converts local provider memory into vector-store documents.
- `rag/adaptive_rag.py`: provider memory adapter; retrieves vector hits and converts them into `llm.agent.models.RagSnippet` objects for Gemini prompts.



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

2. if you don't have, install uv:
```bash
pip install uv
```

3. Clone project code:
```bash
git clone https://github.com/orgs/<your-org>/repositories/AgenticAI_Billing_KB.git  .

```

4. Activar o ambiente para o projecto:
```bash
uv init  
uv venv .venv
.venv\Scripts\activate
```

5. Install Python, needed libraries and dependencies:
```bash
uv pip install -r requirements.txt
```

6. Local OCR requires Tesseract and OCRmyPDF runtime tools available on PATH. 
On Windows, import Tesseract with:
```bash
uv run python -c "import pytesseract; print(pytesseract.get_tesseract_version())"
```

## Gemini Configuration

A GEMINI_API_KEY is needed to execute code and dashboard.
If you don't have one, register on https://aistudio.google.com/ and create your own key (Free).

For command-line use, set:
```bash
$env:GEMINI_API_KEY="your-real-key" 
$env:GEMINI_MODEL="gemini-3.5-flash"
```

Notes:
- The dashboard also has a GEMINI_API_KEY field in **Import Invoices**. It is used only for to import request and is not saved.
- Gemini PDF extraction uses the optional `google-genai` SDK from `requirements.txt`. 
- Tests continue to use mocked callers and do not call the real API.



# C. Execution
Process has 2 steps:
1. Batch processing to extract text from file(s) (ocr);
2. Dashboard operation.


## 1. Step-by-step batch processing to extract text from file(s)

### 1.1 OCR text extratction processes all files in `data/data_raw/`.
New invoices can be added manualy to folder ./data/data_raw/` by yourself.
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

### 1.4 Build or refresh the local provider-memory vector index:
```bash
uv run python rag\adaptive_rag.py index
```
Note:
- The first vector-index build may need to download the configured HuggingFace embedding model. Do not run it during tests or quality checks unless you explicitly want generated index files.


### 1.5 Run Gemini PDF 2nd pass for one invoice:
* FDC pq só duas ?
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
- **Experimental Agentic A/B Test** (inside Manual Review): compare the current deterministic Plan A with coded-agent Plan B, inspect changed fields and routing decisions, then record a human accuracy verdict. The experiment does not alter the reviewed CSV or provider memory.
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
python scripts\agentic_ab_test.py --text-file data\data_txt\agua_01.txt --pdf-file data\data_pdf\agua_01.pdf
```

Without a PDF/API key, the CLI still records the orchestration trace but the extraction agent explicitly retains the deterministic result, so it is not a meaningful model comparison.




## 3. Tests

For test purpose run the current test suite:
```bash
uv run python -m pytest tests
```

The tests mock Gemini API calls and do not call the real API.

# D. Security and Limitations 

## Security Notes

- Keep `.env` ignored.
- Keep `.env.example` as placeholders only.
- Keep `.gitignore` active so local caches, logs, environments, secrets, and runtime RAG memory are not committed accidentally.
- The `data/` folders are no longer ignored. Commit invoice data and generated outputs only after reviewing them for private/customer information.
- Keep generated vector indexes under `data/data_processed/vector_store/` and commit them only when they are intentionally part of the shared dataset.
- Do not commit real API keys, credentials, or unapproved private invoice data.
- Do not overwrite raw files in `data/data_raw/`.
- Historical generated reports may contain old run data; review them before committing.

## Known Limitations

- OCR quality depends on local Tesseract/OCRmyPDF installation.
- Some source/OCR text may contain encoding artifacts.
- Deterministic extraction is regex/heuristic-based.
- Gemini second pass requires API access, quota, and network availability.
- Provider memory is stored in JSON and indexed locally into FAISS/LlamaIndex when vector dependencies are installed.
- Fresh clones contain `data/` only when invoice data has been committed; otherwise local invoice import or pipeline commands create it.
- Old timestamped Gemini artifacts may exist from earlier development runs.



# E. Extra information

## Docker And API

The multi-stage Docker image includes Portuguese/English Tesseract, OCRmyPDF runtime tools, a non-root user, a read-only application filesystem, and health checks. Compose runs two services from the same image:

- FastAPI health service: `http://127.0.0.1:8000/health`
- Invoice dashboard: `http://127.0.0.1:8501/`

Create local writable directories and start both services:

```powershell
New-Item -ItemType Directory -Force data, runtime
docker compose up --build -d
docker compose ps
```

`data/` holds invoice inputs and generated artifacts. `runtime/` holds ignored provider memory, vector indexes, and model caches. Compose binds both ports to localhost unless `BIND_ADDRESS` is explicitly changed. The API on port 8000 is still a health/root service; it is not an OpenAI-compatible `/v1` model server.

For the temporary self-deleting EC2 deployment, AWS SSO setup, old endpoint check, early deletion, and safe nested-repository publication workflow, see [deploy/aws/README.md](deploy/aws/README.md).


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

The vector retrieval index is generated from that provider memory and stored in:
```text
data/data_processed/vector_store/
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
uv python rag\adaptive_rag.py retrieve --text-file data\data_txt\telecom_05.txt --provider vodafone --invoice-type telecom
```

The Gemini second pass asks the vector store for matching provider-memory snippets when OCR text, provider, and invoice type are available. If vector-store dependencies are not installed or the index has not been built, the project falls back to the JSON provider memory so local tests and review can still run.
# AgenticAI_Billing_KB
