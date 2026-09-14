# Assessment requirements review

Reviewed on 2026-09-14 against the supplied `FinalAssessment.pdf`, pages 1–6.
The PDF is a requirements reference; the requested implementation scope is open-company bill processing. The remaining work below is recommended, not claimed complete.

## Implemented: company-independent processing

The assessment restricts document categories to electricity, water, natural gas, and telecom (page 1). It does not impose a supplier or tenant allowlist. No explicit tenant authorization check was found in this repository.

- `scripts/ocr_text_extraction.py`: removed the hardcoded provider keyword list from scoring, diagnostics, and comparison tie-breakers. Scores now use document content and OCR noise without rewarding familiar brands. Reallocated the former brand weight to invoice keywords and tax-number signals; thresholds need evaluation on real bills.
- `invoice_parser/providers.py`: shared provider recognition. Existing supplier aliases remain conveniences; arbitrary explicit seller names receive distinct, stable IDs. EEM and ARM aliases are included. Unknown names are not forced to EDP or another familiar company.
- `scripts/extract_invoice_fields.py`: uses shared seller extraction, recognizes English water/electricity terms, matches whole service words, and explicitly marks unsupported categories invalid. Missing seller names require review.
- `rag/adaptive_rag.py` and `vector_store/base.py`: scope JSON and vector guidance to the identified provider. Another water company's feedback is not applicable merely because both bills concern water. The local Torch feature-hash embedding preserves FAISS/LlamaIndex retrieval without Hugging Face or model downloads.
- `scripts/dashboard.py`: retains newly supplied seller names in provider memory and prevents an unidentified supplier from acquiring approval eligibility through the shared legacy `unknown` bucket. Also fixes Windows upload/PDF basename handling under Linux.
- `llm/agent/prompts.py`: explicitly accepts unfamiliar companies within supported categories and treats the invoice PDF as the source of seller identity.
- `tests/test_open_providers.py`: synthetic coverage for new suppliers, category rejection, OCR brand neutrality, and memory separation. `tests/test_quality_fixes.py` now sets a deterministic timestamp in its newer-file test.

Company aliases were checked against the [Portuguese government EEM entry](https://www.gov.pt/entidades/eem-empresa-de-eletricidade-da-madeira) and [ARM company page](https://arm.pt/empresa/). Recognition does not establish extraction accuracy on those companies' actual bill layouts.

## Prioritized remaining work

An experimental coded-agent Plan B now provides an assessment/demo path without changing production results. It coordinates classification, provider memory, Gemini extraction, stronger validation, and review routing; stores a step trace; compares its fields with deterministic Plan A; and captures a human accuracy verdict. The strict five-distinct-prior-approvals rule and stronger consistency checks currently belong to Plan B, so the P0 production gaps below remain until A/B evidence supports promotion.

| Priority | Requirement and current evidence | Concrete next step and completion evidence |
| --- | --- | --- |
| P0 | Validated history and approval, pages 3–4. `seed_from_validated_csv()` imports extraction rows into `previously_validated_invoices`; `provider_validated_count()` counts `valid_invoice=true` without requiring a human approval decision. Thus five extracted rows can be mistaken for five reviewed invoices. `review_state()` computes eligibility, but the pipeline records approval through a human action. | Separate extracted history from approved examples. Count five distinct, prior, approved invoices from the same provider, excluding the current invoice and rejected or superseded versions. Persist automatic decisions with reasons if automation is enabled. Test four versus five approved examples, repeated uploads, corrections, and rejection. |
| P0 | Consistency and confidence, pages 2–5. Dashboard checks missing critical fields, dates, numeric values, warnings, and period order. Gemini validation checks date shape rather than calendar validity. Confidence dictionaries are stored in LLM artifacts but not used as a calibrated approval gate. | Share one validator across both paths; use decimal arithmetic for subtotal + VAT versus total, finite numeric checks, real calendar dates, and due-date consistency with explicit exceptions. Store per-field provenance and uncertainty; route missing or low-confidence data to review. Test impossible dates, contradictory totals, non-finite numbers, and ambiguous fields. |
| P0 | Extraction accuracy (30% of the grade), page 6. Schema covers the requested fields, but deterministic extraction remains heuristic. Filename prefixes override content; seller/buyer VAT selection and consumption extraction can select unrelated numbers. `normalize_money()` can reinterpret long integers as cents. | Build a redacted, labeled dataset across all four categories, unfamiliar providers, image/PDF inputs, and unsupported documents. Separate provider/buyer blocks; validate numeric roles; normalize quantities separately from currency; make classification primarily content based. Report field accuracy and document rejection errors on held-out invoices. |
| P1 | Persistent SQL/NoSQL knowledge base, page 4. Runtime storage is CSV and a JSON file, with a derived local FAISS index. No database implementation currently exists. | Add SQLite tables for providers, invoices, extraction versions, validation results, review events, confidence, OCR anomalies, and guidance. Use transactional writes and stable invoice IDs; retain CSV export. Demonstrate persistence after restart and safe concurrent reviews. |
| P1 | Adaptive feedback and revalidation, pages 3–4. Structured feedback and provider-scoped vector retrieval exist. The deterministic parser does not consume retrieved guidance, the index requires an explicit refresh, and approvals are matched by source filename even after fields change. | Version memory and extraction results, invalidate stale indexes, apply approved guidance during repeat extraction, and revalidate the exact new version. Reserve approved status for the approved version. Demonstrate a correction changing a later extraction and reducing errors without copying old invoice values. |
| P1 | Evaluation of learning, pages 4–6. Tests verify behavior but do not demonstrate accuracy improvement or reduced human intervention. | Compare OCR/regex alone, LLM without memory, and LLM with validated memory on the same held-out examples. Record field accuracy, manual review rate, incorrect approvals, latency, and model usage. Separate providers/layouts between learning and evaluation to avoid leakage. |
| P1 | Deployment and observability, page 5. Docker now includes OCR tools and Compose runs the health API plus dashboard with persistent data/runtime paths. An ephemeral CloudFormation/SSM deployment and cleanup runbook exist, but have not been executed against the assigned AWS account. Structured application metrics/tracing remain absent. | Run and delete the temporary stack with the assigned AWS role; preserve redacted deployment evidence. Add structured stage logs, correlation IDs, duration/error metrics, and redacted traces. Demonstrate one traced invoice from upload to review. |
| P2 | Code quality, documentation, report, and demo, pages 5–6. The application lives at the Git root and uses one `pyproject.toml` plus `uv.lock`. A loopback-only Gradio lab exposes the A/B trace and knowledge base. GitOps, PR automation, and CI/CD are prohibited for this private college project. No final report/evaluation artifact was found. | Keep verification and deployment operator-invoked, split large scripts into focused modules while retaining entry points, and prepare the eight requested report sections plus a 10–15 minute demo covering rejection, review, learning, and deployment. |

Recommended implementation order: trustworthy approval history and shared validation; database-backed extraction/review versions; measured feedback learning; full deployment and tracing. Start collecting the evaluation dataset immediately so each change can be assessed.

## Compatibility and review before use

- New named providers have separate memory; genuinely missing seller names stay `unknown` and require review. This is provider grouping, not an authentication or access-control model.
- Provider names normalize case, accents, spacing, and punctuation. Substantially different spellings and trading/legal names may still need explicit aliases. A future database should use verified seller VAT/country identity to disambiguate legal entities.
- Historical `unknown` memory may contain multiple companies, and old rows may have incorrectly inferred EDP. It is not automatically redistributed because seller identity cannot be safely reconstructed without reviewing the source. Correct seller fields, approve the relevant invoices again, and rebuild the vector index.
- Existing OCR artifacts retain their original diagnostics. New OCR reports no longer include `provider_keyword_count` or `signals.provider_keywords`; external consumers must not require those fields.
- Generic deterministic extraction recognizes labeled sellers and likely company headers. Unreadable or unusual layouts can still leave fields null; use manual review or the Gemini PDF pass. Do not interpret these changes as universal extraction accuracy.
- Real invoice PDFs, live Gemini calls, model downloads, Docker, and AWS were not exercised. No private invoice data was needed for the regression tests. The Gradio interface disables external LLM calls.

## Verification and commands

The initial suite had 41 passing tests and two failures: Windows basename handling on Linux, and a test assuming two immediately written files always have different timestamps. Both are addressed. After the current changes, `python3 -m pytest -q` completed with **70 passed**. The local pytest asyncio plugin emitted a configuration deprecation warning.

The deterministic extraction CLI and provider-memory `init` CLI also passed using one synthetic EEM bill and temporary output paths. A local FAISS/LlamaIndex index build and provider-scoped EPAL retrieval passed with the Torch feature-hash embedding. The Gradio app responded on `127.0.0.1:7860`, and its synthetic A/B callback produced all five agent events without an external LLM call. Existing private invoice files and runtime memory were not regenerated.

From the project folder containing `AGENTS.md`, run in PowerShell:

```powershell
uv run pytest
uv run python scripts\gradio_app.py
uv run python scripts\dashboard.py
```

Open `http://127.0.0.1:7860` for the local A/B lab or `http://127.0.0.1:8501` for the full review dashboard. Check one real EEM invoice, one real water bill, and one telecom bill before relying on their layout extraction. Review changed code and any existing private/generated artifacts before committing.
