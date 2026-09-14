# Plan A / Plan B A/B Test

## Plan B agentic architecture

Plan B uses a **hybrid supervisor workflow with sequential agent handoffs**. It is not currently a Mixture of Agents (MoA).

```mermaid
flowchart LR
    Input["PDF + OCR text"] --> A1["Classification Agent"]
    A1 --> A2["Provider Memory Agent"]
    A2 --> A3["Extraction Agent"]
    A3 --> A4["Validation Agent"]
    A4 --> A5["Review Routing Agent"]

    KB["Provider RAG memory"] --> A2
    A2 -->|"retrieved guidance"| A3
    Gemini["Gemini PDF model"] --> A3

    A5 --> Reject["Reject unsupported"]
    A5 --> Human["Manual review"]
    A5 --> Auto["Automatic-approval eligible"]

    Baseline["Plan A: OCR + regex"] --> Compare["A/B comparison"]
    A5 --> Compare
    OCR["OCR evidence"] --> Judge["Independent LLM Judge"]
    Compare --> Judge
    Judge --> Advisory["Advisory score + field verdicts"]
    Compare --> Verdict["Human accuracy verdict"]
    Advisory --> Verdict
```

The supervisor is [`PlanBOrchestrator`](llm/agent/workflow.py). It runs the agents in a fixed sequence and passes a typed shared context between them.

| Agent | Responsibility | Agent type |
| --- | --- | --- |
| [`ClassificationAgent`](llm/agent/workflow.py) | Classifies the document as electricity, water, natural gas, telecom, or unsupported. It stops unsupported documents from qualifying as valid invoices. | Deterministic rules agent |
| [`ProviderMemoryAgent`](llm/agent/workflow.py) | Identifies the supplier, retrieves only that provider's RAG knowledge, and supplies tips, layout knowledge, OCR corrections, and reviewer feedback. | Retrieval/tool agent |
| [`ExtractionAgent`](llm/agent/workflow.py) | Sends the PDF, OCR text, classification hint, and RAG context to Gemini. If Gemini or the PDF is unavailable, it records the failure and keeps deterministic extraction. | LLM tool-using agent |
| [`ValidationAgent`](llm/agent/workflow.py) | Checks required fields, real calendar dates, date ordering, finite monetary values, and `subtotal + VAT ≈ total`. | Deterministic critic agent |
| [`ReviewRoutingAgent`](llm/agent/workflow.py) | Chooses unsupported rejection, manual review, or automatic-approval eligibility. It requires five distinct prior approved invoices for automation. | Policy/router agent |

The interaction pattern is a **shared-state blackboard with sequential handoffs**:

1. Agents receive the same typed `AgentContext`.
2. Each agent reads previous results and adds its own output.
3. Later agents act on earlier decisions.
4. The orchestrator records status, decision, metrics, sequence, and duration.
5. Agents do not exchange natural-language messages with one another.
6. There is no voting, debate, parallel candidate generation, or LLM-based manager.

Only the extraction agent is generative. The other agents provide predictable guardrails around the model. This architecture suits invoices because financial extraction benefits from controlled decisions and auditable validation.

MoA would use several extraction agents to analyze the same invoice independently—for example, a regex agent, a Gemini visual agent, an OCR-text agent, and a provider-layout agent. Plan A and Plan B remain an A/B experiment rather than an MoA ensemble. The optional LLM judge adds an evaluator pattern after the two plans; it does not turn the fixed Plan B workflow into MoA.

## Independent LLM judge

The optional judge is deliberately outside `PlanBOrchestrator`, so Plan B does not grade itself. It receives the OCR evidence, both structured candidates, their deterministic validation errors, and their routes. It returns:

- a recommendation of Plan A, Plan B, tie, or inconclusive;
- an estimated 0–10 score for each plan;
- a 0–1 confidence value;
- field-level decisions for disagreements; and
- a short explanation.

The prompt treats invoice text as untrusted evidence and tells the model to abstain when the OCR does not support a value. The judge excludes provider RAG context to reduce preference toward Plan B. Its result is stored in the isolated A/B JSON artifact without storing the API key or raw prompt.

The judge reads OCR text rather than the original pixels, so it cannot detect an OCR error that changes the evidence itself. Its verdict is advisory and cannot approve or reject an invoice. The human verdict remains required and supplies the ground truth for measuring judge agreement and actual A/B accuracy.

The connector accepts an OpenAI-compatible `/v1/chat/completions` endpoint. Configure `JUDGE_BASE_URL`, `JUDGE_MODEL`, and, only when the server requires one, `JUDGE_API_KEY`. No model is downloaded by this project.

## Local Gradio test interface

Run `uv run python scripts\gradio_app.py` and open `http://127.0.0.1:7860`. The interface accepts an invoice PDF or image, runs the built-in OCR pipeline, shows both plan results and the five-agent trace, exposes the provider knowledge base and local FAISS rebuild action, and records the human accuracy verdict in the A/B artifact. Plan B Gemini extraction is disabled by default and requires an explicit checkbox plus a request-only API key. The judge makes a separate external call only after the user configures an endpoint and clicks **Run LLM judge**. The server rejects non-loopback host arguments.

The assessment defines electricity, water, natural gas, and telecom as supported invoice categories. The classification and review-routing agents reject documents outside those four categories.
