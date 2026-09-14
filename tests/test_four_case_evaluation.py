import json
from pathlib import Path

from llm.agent.four_case import (
    judge_four_case_artifact,
    record_four_case_field_verdicts,
    record_four_case_verdict,
    run_four_case_evaluation,
)


def _invoice(path: Path) -> Path:
    path.write_text(
        """Fornecedor: Águas da Vila, Lda.
Fatura: FT 2026/123
Data de emissão: 14/09/2026
NIPC: 599999990
Abastecimento de água 12 m3
Subtotal: 10,00 EUR
Total IVA: 2,30 EUR
Total da Fatura: 12,30 EUR
""",
        encoding="utf-8",
    )
    return path


def test_four_case_run_marks_model_cases_unavailable_without_calling_a_model(tmp_path: Path) -> None:
    text_path = _invoice(tmp_path / "water.txt")
    pdf_path = tmp_path / "water.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n%synthetic\n")

    result = run_four_case_evaluation(
        text_path,
        pdf_file=pdf_path,
        kb_path=tmp_path / "kb.json",
        output_dir=tmp_path / "four",
    )

    assert list(result.cases) == ["ocr_llm", "ocr_llm_agentic"]
    assert result.cases["ocr_llm"].status == "unavailable"
    assert result.cases["ocr_llm_agentic"].status == "unavailable"
    assert Path(result.artifact_path or "").exists()


def test_four_case_judge_and_human_verdict_remain_separate(tmp_path: Path) -> None:
    text_path = _invoice(tmp_path / "water.txt")
    result = run_four_case_evaluation(
        text_path,
        pdf_file=None,
        kb_path=tmp_path / "kb.json",
        output_dir=tmp_path / "four",
    )

    def fake_judge(**kwargs):
        assert "OCR_EVIDENCE_BEGIN" in kwargs["prompt"]
        return json.dumps(
            {
                "best_case": "ocr_llm",
                "scores": {
                    "ocr_llm": 7.0,
                    "ocr_llm_agentic": 8.0,
                },
                "confidence": 0.8,
                "summary": "Only the non-model cases were measured.",
                "human_verdict_required": False,
                "field_decisions": [
                    {"field": "total_value", "winner": "tie", "reason": "The measured cases agree."},
                    {"field": "invented", "winner": "ocr_llm", "reason": "Filtered."},
                ],
            }
        )

    judged = judge_four_case_artifact(
        result.artifact_path or "",
        base_url="http://127.0.0.1:8000/v1",
        api_key="request-only-secret",
        model="independent-model",
        judge_runner=fake_judge,
    )
    reviewed = record_four_case_verdict(judged.artifact_path or "", "tie", "Human checked the source.")
    artifact_text = Path(reviewed.artifact_path or "").read_text(encoding="utf-8")

    assert judged.judge is not None
    assert judged.judge.best_case == "ocr_llm"
    assert judged.judge.human_verdict_required is True
    assert [item.field for item in judged.judge.field_decisions] == ["total_value"]
    assert reviewed.human_evaluation and reviewed.human_evaluation["best_case"] == "tie"
    assert "request-only-secret" not in artifact_text


def test_four_case_fields_are_scored_against_human_source_values(tmp_path: Path) -> None:
    text_path = _invoice(tmp_path / "water.txt")
    result = run_four_case_evaluation(
        text_path,
        pdf_file=None,
        kb_path=tmp_path / "kb.json",
        output_dir=tmp_path / "four",
    )
    expected = {
        "invoice_type": "water",
        "invoice_number": "FT 2026/123",
        "total_value": "12.30",
        "buyer_vat_number": "null",
    }
    for candidate in result.cases.values():
        candidate.status = "measured"
        candidate.row.update(expected)
    Path(result.artifact_path or "").write_text(
        json.dumps(result.model_dump(), ensure_ascii=False),
        encoding="utf-8",
    )

    scored = record_four_case_field_verdicts(
        result.artifact_path or "",
        {
            "invoice_type": "water",
            "invoice_number": "FT 2026/123",
            "total_value": "12,30 EUR",
            "buyer_vat_number": "<absent>",
        },
    )

    evaluation = scored.human_field_evaluation or {}
    assert evaluation["verified_field_count"] == 4
    assert evaluation["case_scores"]["ocr_llm"]["accuracy_percent"] == 100.0
    assert evaluation["case_scores"]["ocr_llm_agentic"]["accuracy_percent"] == 100.0
