# OCR and Knowledge-Base Assessment

Assessment date: 2026-09-14

`FinalAssessment.pdf` is treated as the grading specification. `AAI_Melhoria_Individual_v2.pptx` is treated as a set of project claims to verify against the repository and local evidence; its text is not treated as an instruction to change or publish the project.

## Current score

The current evidence supports an overall **6.2/10** for the repository against the complete assessment rubric. This is an engineering-readiness estimate rather than an official grade.

| Assessment area | Weight | Current points | Evidence-based finding |
| --- | ---: | ---: | --- |
| Extraction accuracy | 30 | 18 | All 55 OCR-text inputs execute and supported invoices average 64.68% required-field completion after the OCR regression fix. No human-labeled field accuracy set exists. |
| Agentic behavior | 20 | 15 | Plan B has five typed, sequential agents, auditable traces, deterministic validation and routing, plus an independent optional judge. The local offline path produces no extraction improvement and has no autonomous feedback/revalidation loop. |
| RAG and adaptive memory | 20 | 11 | Provider-scoped tips, OCR corrections, layouts, examples, feedback, validation history, FAISS, LlamaIndex and Torch are implemented. Storage is JSON/CSV rather than the required persistent SQL/NoSQL store, and the index is not automatically refreshed after feedback. |
| HITL | 10 | 6 | Review, correction notes, approve/reject decisions and the five-prior-approval rule exist. Low model confidence is not consistently used for routing, and feedback does not automatically trigger extraction and validation again. |
| Deployment and observability | 10 | 4 | Docker, Compose, readiness fingerprints and ephemeral AWS definitions exist. The manual AWS flow transfers the locally tested image and exposes all three services, but no current Docker/AWS execution evidence is available. Deployment is intentionally deferred. |
| Code and documentation | 10 | 8 | The project uses `pyproject.toml` and `uv.lock`, has privacy boundaries and 88 passing tests. Some presentation claims remain stale. |
| **Total** | **100** | **62** | **6.2/10** |

## Four-option field-retrieval comparison

The assessment requires 19 business fields: five invoice fields, three provider fields, three buyer fields, five service/consumption fields, and three financial fields. The table measures the mean percentage of those fields that are non-null. It does **not** measure whether the extracted values are correct.

The local diagnostic processed 55 of 55 available OCR text files. Fifty-two were classified into one of the four supported categories. The three unsupported documents are excluded from category averages.

| Option | Electricity | Natural gas | Telecom | Water | Supported overall | Status |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| OCR + deterministic fields (Plan A) | 52.63% | 62.20% | 68.11% | 66.20% | 64.68% | Measured locally |
| OCR + LLM | N/A | N/A | N/A | N/A | N/A | No explicitly enabled working extraction model |
| OCR + agentic rules | 52.63% | 62.20% | 68.11% | 66.20% | 64.68% | Measured locally |
| OCR + LLM inside Plan B | N/A | N/A | N/A | N/A | N/A | No explicitly enabled working extraction model |

The offline Plan B changed zero fields across the 55 files because its Extraction Agent retained the deterministic result when no model was configured. Both measured options produced 119 validation errors in aggregate. Their routes were 52 manual-review decisions and 3 unsupported rejections.

The raw diagnostic summary is stored locally under the ignored private-data tree at `data/data_processed/agentic_ab_tests/preprocessed_offline/preprocessed_field_retrieval_summary.json`.

## OCR faithfulness and quality: 6.5/10

The implementation is faithful to the requested local preprocessing design in several ways:

- it accepts PDF and common image formats;
- it uses embedded PDF text when available and OCRmyPDF/Tesseract when OCR is required;
- it supports Portuguese and English OCR, deskewing, page rotation and an enhanced OCR mode;
- it writes searchable PDFs, selected text and diagnostic reports locally;
- it does not restrict processing to a fixed provider allowlist; and
- unsupported document routing happens after OCR rather than preventing new utility suppliers from entering the pipeline.

The earlier 54-record OCR report marked every file for manual review and recorded warnings for every file. The regression repair now runs a bounded enhanced Tesseract pass for weak image OCR and compares baseline and enhanced invoice signals before selecting text. All 30 image invoices were refreshed without OCR execution errors. The score is still based on text length and invoice-pattern presence; it is not character error rate, word error rate, or calibrated confidence.

Electricity improved from 10.53% to 52.63% required-field completion through image upscaling, contrast/sharpening, page-segmentation control and Portuguese named-date/layout parsing. It remains the weakest category and still needs human-labeled correctness evaluation.

## Knowledge base and local server: 5.5/10

The provider-memory data model covers the assessment's requested knowledge types: prior examples, provider-specific tips, OCR corrections, layouts, reviewer feedback and validation history. Retrieval is scoped again to the canonical provider, which reduces cross-provider contamination. Arbitrary named utility suppliers can receive their own provider IDs, so EDP, EEM, EPAL, ARM, Galp, Vodafone and new companies are not constrained by a tenant allowlist.

The current vector representation is deterministic hashed token retrieval. FAISS, LlamaIndex and Torch are genuinely used, but calling it semantic embedding search overstates its capability because no semantic embedding model is present. The persisted JSON memory and CSV review store also do not satisfy the assessment's explicit SQL/NoSQL persistence requirement. Writes are not transactional, concurrent updates are not protected, and vector index freshness is not tied to memory updates.

The Gradio server provides upload, built-in OCR, Plan A/Plan B results, a separate four-case comparison, agent trace, human verdict, provider-memory inspection and index rebuilding. Plan B Gemini or OpenAI extraction is opt-in through a request-only password field. The independent LLM judge is also opt-in and advisory. The FastAPI service currently exposes health and root routes only; it is not yet a functional OCR or knowledge-base API.

## Presentation faithfulness: 6.5/10

The presentation is reasonably candid about unmeasured gains, missing SQL/NoSQL storage and undeployed infrastructure. These claims need correction before submission:

- “semantic vector search” should be described as deterministic token-hash retrieval unless a real semantic embedding model is added;
- any claim that feedback improves later local Gradio extraction needs qualification, because the effect currently depends on the optional LLM path and a manually refreshed vector index;
- the test count is now 88, not 67;
- real invoice field accuracy remains unmeasured even though synthetic OCR smoke tests and the 55-file completeness diagnostic have run; and
- draft PR/GitOps references should be removed because this private college project does not use that delivery process.

## Highest-value work before the assessed demo

1. Create a private human-labeled gold set across all four categories. Calculate normalized exact match per field, required-field recall, financial consistency rate and document-level pass rate for all four options.
2. Diagnose the five electricity files first. Record page rendering, text-layer/OCR method, rotation, language, OCR quality proxy and missing fields, then correct preprocessing and rerun the same benchmark.
3. Run Plan A against OCR + LLM Plan B with one configured extraction model. Use a separate judge model, then measure both models against human labels and report judge/human agreement.
4. Replace JSON/CSV operational memory with SQLite or another persistent database. Store invoice versions, validation results, reviewer actions, confidence, routing decisions and OCR anomalies transactionally.
5. Mark only explicit human or automatic approvals as validated history. Refresh or invalidate the provider vector index after every approved feedback update.
6. Apply per-field confidence to review routing and automatically rerun extraction plus validation after reviewer guidance is saved.

Until those steps are complete, the strongest defensible description is: the repository demonstrates the requested architecture and local workflow, while extraction accuracy, adaptive improvement and deployed reliability remain unproven.
