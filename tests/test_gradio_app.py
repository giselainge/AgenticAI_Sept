import json
from pathlib import Path
from types import SimpleNamespace

from scripts import gradio_app


SYNTHETIC_INVOICE = """Fornecedor: Águas da Vila, Lda.
Fatura: FT 2026/123
Data de emissão: 14/09/2026
Data limite de pagamento: 30/09/2026
NIPC: 599999990
Abastecimento de água 12 m3
Subtotal: 10,00 EUR
Total IVA: 2,30 EUR
Total da Fatura: 12,30 EUR
"""


def configure_local_invoice(monkeypatch, tmp_path: Path) -> Path:
    source = tmp_path / "invoice.pdf"
    source.write_bytes(b"%PDF-1.4\n% synthetic local test\n")
    selected_text = tmp_path / "text" / "invoice.txt"
    selected_text.parent.mkdir(parents=True, exist_ok=True)
    selected_text.write_text(SYNTHETIC_INVOICE, encoding="utf-8")
    monkeypatch.setattr(gradio_app, "DEFAULT_RAW_DIR", tmp_path / "raw")
    monkeypatch.setattr(
        gradio_app,
        "process_file",
        lambda *args, **kwargs: SimpleNamespace(
            errors=[], selected_text_file=str(selected_text), searchable_pdf=str(source)
        ),
    )
    return source


def test_local_upload_copy_is_content_stable_and_does_not_cascade_names(tmp_path: Path) -> None:
    source = tmp_path / "invoice.pdf"
    source.write_bytes(b"same invoice")
    uploads = tmp_path / "uploads"

    first = gradio_app._copy_local(source, uploads)
    repeated_source = tmp_path / f"{first.stem}_abcdef12.pdf"
    repeated_source.write_bytes(source.read_bytes())
    second = gradio_app._copy_local(repeated_source, uploads)

    assert first == second
    assert len(list(uploads.glob("*.pdf"))) == 1


def test_local_gradio_callback_runs_both_plans_without_llm(tmp_path: Path, monkeypatch) -> None:
    output_dir = tmp_path / "ab"
    kb_path = tmp_path / "knowledge_base.json"
    monkeypatch.setattr(gradio_app, "DEFAULT_AGENTIC_AB_DIR", output_dir)
    monkeypatch.setattr(gradio_app, "DEFAULT_KB", kb_path)
    source = configure_local_invoice(monkeypatch, tmp_path)

    status, plan_a, plan_b, comparison, trace, artifact, state = gradio_app.run_local_ab(source)

    assert "external LLM used: false" in status
    assert plan_a["name"] == "Plan A"
    assert plan_b["name"] == "Plan B"
    assert plan_b["llm_used"] is False
    assert comparison["accuracy_requires_human_verdict"] is True
    assert [event["sequence"] for event in trace] == [1, 2, 3, 4, 5]
    assert Path(artifact).exists()
    assert artifact == state


def test_local_inspection_exposes_fields_postprocess_and_safe_audit_log(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(gradio_app, "DEFAULT_AGENTIC_AB_DIR", tmp_path / "ab")
    monkeypatch.setattr(gradio_app, "DEFAULT_KB", tmp_path / "knowledge_base.json")
    source = configure_local_invoice(monkeypatch, tmp_path)

    outputs = gradio_app.run_local_inspection(source)
    status, summary, fields, _, _, _, trace, processing, audit, artifact, state = outputs
    saved = json.loads(Path(artifact).read_text(encoding="utf-8"))

    assert "Completed locally" in status
    assert summary["required_fields"] == 19
    assert len(fields) == 19
    assert len(trace) == 5
    assert "ocr" in processing and "post_processing" in processing
    assert any(row[0] == "provider_memory_agent" for row in audit)
    assert saved["preprocessing"] is not None
    assert len(saved["audit_log"]) >= 8
    assert artifact == state


def test_retrieve_fields_button_runs_complete_july_vs_agentic_pair(monkeypatch) -> None:
    captured = {}

    def fake_run(uploaded_file, model, api_key, provider):
        captured.update(
            uploaded_file=uploaded_file,
            model=model,
            api_key=api_key,
            provider=provider,
        )
        plan_a = SimpleNamespace(completion=0.6842, model_dump=lambda: {"name": "Plan A (July)"})
        plan_b = SimpleNamespace(completion=0.7895, model_dump=lambda: {"name": "Plan B (Agentic)"})
        result = SimpleNamespace(
            plan_a=plan_a,
            plan_b=plan_b,
            comparison={},
            plan_b_trace=[],
            audit_log=[],
            artifact_path="local-result.json",
        )
        return result, "invoice.pdf", {}

    monkeypatch.setattr(gradio_app, "_execute_july_vs_agentic", fake_run)
    monkeypatch.setattr(gradio_app, "_run_summary", lambda _: {})
    monkeypatch.setattr(gradio_app, "field_comparison_rows", lambda _: [])
    monkeypatch.setattr(gradio_app, "_postprocess_summary", lambda _: {})
    result = gradio_app.retrieve_fields_with_llm("invoice.pdf", "model", "request-key", "openai")

    assert "Completed July-vs-agentic" in result[0]
    assert result[3]["name"] == "Plan A (July)"
    assert result[4]["name"] == "Plan B (Agentic)"
    assert captured == {
        "uploaded_file": "invoice.pdf",
        "model": "model",
        "api_key": "request-key",
        "provider": "openai",
    }


def test_local_gradio_verdict_is_saved(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(gradio_app, "DEFAULT_AGENTIC_AB_DIR", tmp_path / "ab")
    monkeypatch.setattr(gradio_app, "DEFAULT_KB", tmp_path / "knowledge_base.json")
    source = configure_local_invoice(monkeypatch, tmp_path)
    *_, artifact, _ = gradio_app.run_local_ab(source)

    message = gradio_app.save_verdict(artifact, "plan_b", "Better provider extraction.")

    assert "Saved verdict 'plan_b'" in message
    assert "Better provider extraction." in Path(artifact).read_text(encoding="utf-8")


def test_gradio_judge_callback_keeps_human_verdict_required(tmp_path: Path, monkeypatch) -> None:
    artifact = tmp_path / "comparison.json"
    artifact.write_text("{}", encoding="utf-8")
    seen = {}

    def fake_judge(path, **kwargs):
        seen.update(kwargs)
        return SimpleNamespace(
            llm_judge=SimpleNamespace(
                status="completed",
                preferred_plan="plan_b",
                confidence=0.75,
                model_dump=lambda: {
                    "status": "completed",
                    "preferred_plan": "plan_b",
                    "confidence": 0.75,
                    "human_verdict_required": True,
                },
            )
        )

    monkeypatch.setattr(gradio_app, "judge_ab_artifact", fake_judge)
    status, judge = gradio_app.run_judge(
        str(artifact),
        "http://127.0.0.1:8000/v1",
        "independent-model",
        "request-only-secret",
    )

    assert "recommends plan_b" in status
    assert "human verdict is still required" in status
    assert judge["human_verdict_required"] is True
    assert seen["api_key"] == "request-only-secret"


def test_gradio_gemini_judge_can_reuse_extraction_key(tmp_path: Path, monkeypatch) -> None:
    artifact = tmp_path / "comparison.json"
    artifact.write_text("{}", encoding="utf-8")
    seen = {}

    def fake_judge(path, **kwargs):
        seen.update(kwargs)
        return SimpleNamespace(
            llm_judge=SimpleNamespace(
                status="unavailable",
                summary="mocked",
                model_dump=lambda: {"status": "unavailable"},
            )
        )

    monkeypatch.setattr(gradio_app, "judge_ab_artifact", fake_judge)
    gradio_app.run_judge(
        str(artifact),
        "",
        "gemini-judge-model",
        "",
        "gemini",
        "shared-request-only-key",
    )

    assert seen["provider"] == "gemini"
    assert seen["base_url"] == ""
    assert seen["api_key"] == "shared-request-only-key"
    assert seen["model"] == "gemini-judge-model"


def test_gradio_host_allowlist_is_loopback_only() -> None:
    assert gradio_app.LOCAL_HOSTS == {"127.0.0.1", "localhost", "::1"}


def test_gradio_rejects_ocr_text_upload(tmp_path: Path) -> None:
    text_file = tmp_path / "invoice.txt"
    text_file.write_text(SYNTHETIC_INVOICE, encoding="utf-8")

    try:
        gradio_app._prepare_inputs(text_file)
    except ValueError as exc:
        assert "PDF or supported invoice image" in str(exc)
    else:
        raise AssertionError("OCR text uploads must not bypass the built-in OCR stage")
