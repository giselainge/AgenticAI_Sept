## Agentic A/B experiment

`agentic_ab_test.py` keeps the current OCR/regex extraction as Plan A and runs the experimental five-agent workflow as Plan B. It stores a side-by-side JSON result under `data/data_processed/agentic_ab_tests/` and never applies Plan B fields to the review CSV automatically.

`gradio_app.py` provides a loopback-only local interface for uploading an invoice PDF/image, running built-in OCR and both plans, inspecting the agent trace and provider memory, rebuilding the local FAISS index, and recording the human verdict. It disables external LLM calls.

```powershell
uv run python scripts\gradio_app.py
```

```powershell
$env:GEMINI_API_KEY="your-real-key"
uv run python scripts\agentic_ab_test.py --text-file data\data_txt\agua_01.txt --pdf-file data\data_pdf\agua_01.pdf
```
