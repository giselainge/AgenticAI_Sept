# Project Workflow Graph

This diagram shows the intended separation between source code, trackable invoice data, local runtime memory, generated outputs, review state, tests, logs, and documentation. It uses Mermaid syntax and does not require a graph dependency.

```mermaid
flowchart TD
    subgraph Source["Source code"]
        Wrappers["scripts/: dashboard.py, ocr_text_extraction.py, extract_invoice_fields.py"]
        Scripts["scripts/: dashboard, OCR, deterministic extraction"]
        Shared["invoice_parser/: paths, schema, text utilities"]
        LLM["scripts/second_pass_llm.py"]
        LLMTypes["llm/agent + llm/api: typed models, schemas, prompts"]
        RAG["rag/adaptive_rag.py"]
        VectorStore["vector_store/: FAISS/LlamaIndex index builder"]
        PlanB["llm/agent/workflow.py: coded-agent Plan B"]
    end

    subgraph InvoiceData["Trackable invoice data"]
        Raw["data/data_raw/ (raw inputs)"]
    end

    subgraph Generated["Generated outputs"]
        PDF["data/data_pdf/ (generated PDFs)"]
        TXT["data/data_txt/ (OCR text)"]
        CSV["data/data_processed/invoice_structured_fields.csv"]
        Reports["data/data_processed/reports/"]
        Gemini["data/data_processed/llm_second_pass/"]
        VectorIndex["data/data_processed/vector_store/"]
        ABArtifact["data/data_processed/agentic_ab_tests/"]
    end

    subgraph LocalMemory["Ignored local runtime memory"]
        Memory["rag/knowledge_base.json (ignored local memory)"]
    end

    subgraph Review["Human review and approval"]
        Dashboard["Local dashboard"]
        ManualReview["Manual Review"]
        Corrections["Reviewer corrections"]
        Export["CSV/provider memory export"]
    end

    subgraph Support["Project support"]
        Tests["tests/"]
        Logs["*.log and logs/"]
        Docs["README.md, AGENTS.md, docs/"]
        Config[".env.example, .gitignore, requirements.txt"]
    end

    Wrappers --> Scripts
    Scripts --> Shared
    Scripts --> LLM
    LLM --> LLMTypes
    Scripts --> RAG
    RAG --> LLMTypes
    RAG --> VectorStore
    Raw --> Scripts
    Scripts --> PDF
    Scripts --> TXT
    Scripts --> Reports
    TXT --> CSV
    PDF --> LLM
    TXT --> LLM
    RAG --> LLM
    VectorStore --> RAG
    Memory --> VectorStore
    VectorStore --> VectorIndex
    TXT --> PlanB
    PDF --> PlanB
    RAG --> PlanB
    PlanB --> ABArtifact
    CSV -. "Plan A snapshot" .-> ABArtifact
    ABArtifact --> Dashboard
    ManualReview -->|"A/B accuracy verdict"| ABArtifact
    LLM --> Gemini
    CSV --> Dashboard
    PDF --> Dashboard
    Gemini --> Dashboard
    Memory --> Dashboard
    Dashboard --> ManualReview
    ManualReview --> Corrections
    Corrections --> Memory
    ManualReview --> CSV
    CSV --> Export
    Memory --> Export
    Tests --> Source
    Docs --> Source
    Config --> Source
    Scripts --> Logs
```

Safety boundaries:

- Treat `data/data_raw/` as immutable input. The `data/` tree is trackable, so review invoice contents before committing.
- Write generated OCR, PDF, CSV, report, Gemini, and vector artifacts only under `data/`.
- Keep provider memory in `rag/knowledge_base.json`; it may contain reviewer feedback and should remain ignored unless sanitized.
- Treat `data/data_processed/vector_store/` as a generated local retrieval index built from provider memory.
- Keep secrets and unapproved private invoice data out of logs, docs, generated reports, and committed files.
