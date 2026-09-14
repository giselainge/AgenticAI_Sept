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
