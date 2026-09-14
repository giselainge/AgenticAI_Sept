from __future__ import annotations

import json
from pathlib import Path

from llm import gemini_rest, openai_rest
from llm.agent.judge import run_llm_judge
from scripts.second_pass_llm import second_pass_extract


class _Response:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


def test_openai_responses_request_keeps_key_in_header_and_disables_storage(
    monkeypatch, tmp_path: Path
) -> None:
    pdf = tmp_path / "invoice.pdf"
    pdf.write_bytes(b"%PDF-1.4\n%test\n")
    seen = {}

    def fake_urlopen(req, timeout):
        seen["url"] = req.full_url
        seen["authorization"] = req.get_header("Authorization")
        seen["body"] = json.loads(req.data.decode("utf-8"))
        seen["timeout"] = timeout
        return _Response(
            {"output": [{"content": [{"type": "output_text", "text": "invoice_type: water"}]}]}
        )

    monkeypatch.setattr(openai_rest.request, "urlopen", fake_urlopen)
    text = openai_rest.create_response(
        prompt="extract this invoice",
        api_key="request-only-secret",
        model="test-model",
        pdf_file=pdf,
    )

    assert text == "invoice_type: water"
    assert seen["url"] == "https://api.openai.com/v1/responses"
    assert "request-only-secret" not in seen["url"]
    assert seen["authorization"] == "Bearer request-only-secret"
    assert seen["body"]["store"] is False
    assert seen["body"]["input"][0]["content"][1]["type"] == "input_file"
    assert seen["body"]["input"][0]["content"][1]["file_data"].startswith(
        "data:application/pdf;base64,"
    )
    assert "request-only-secret" not in json.dumps(seen["body"])


def test_gemini_rest_request_keeps_key_out_of_url_and_includes_pdf(monkeypatch, tmp_path: Path) -> None:
    pdf = tmp_path / "invoice.pdf"
    pdf.write_bytes(b"%PDF-1.4\n%test\n")
    seen = {}

    def fake_urlopen(req, timeout):
        seen["url"] = req.full_url
        seen["key"] = req.get_header("X-goog-api-key")
        seen["body"] = json.loads(req.data.decode("utf-8"))
        return _Response({"candidates": [{"content": {"parts": [{"text": "invoice_type: water"}]}}]})

    monkeypatch.setattr(gemini_rest.request, "urlopen", fake_urlopen)
    text = gemini_rest.generate_content(
        prompt="extract this invoice",
        api_key="request-only-secret",
        model="gemini-test",
        pdf_file=pdf,
    )

    assert text == "invoice_type: water"
    assert "request-only-secret" not in seen["url"]
    assert seen["key"] == "request-only-secret"
    parts = seen["body"]["contents"][0]["parts"]
    assert parts[1]["inline_data"]["mime_type"] == "application/pdf"


def test_openai_second_pass_uses_provider_specific_artifacts(tmp_path: Path) -> None:
    pdf = tmp_path / "invoice.pdf"
    pdf.write_bytes(b"%PDF-1.4\n%test\n")
    model_text = "\n".join(
        [
            "invoice_type: water",
            "invoice_number: FT 7",
            "invoice_date: 2026-09-14",
            "currency: EUR",
            "provider_name: EPAL",
            "provider_vat_number: 123456789",
            "total_value: 12.30",
        ]
    )
    result = second_pass_extract(
        pdf_file=pdf,
        api_key="request-only-secret",
        provider="openai",
        model="test-model",
        output_dir=tmp_path,
        openai_caller=lambda *_args, **_kwargs: model_text,
    )

    assert result.used is True
    assert result.provider == "openai"
    assert result.raw_response_path and result.raw_response_path.endswith("_openai_raw.txt")
    artifact = Path(result.normalized_output_path or "").read_text(encoding="utf-8")
    assert "request-only-secret" not in artifact


def test_official_openai_judge_needs_no_base_url() -> None:
    candidate = {"row": {"invoice_type": "water"}, "validation_errors": [], "route": "manual"}
    result = run_llm_judge(
        ocr_text="water invoice",
        plan_a=candidate,
        plan_b=candidate,
        provider="openai",
        api_key="request-only-secret",
        model="test-model",
        caller=lambda **_: {
            "preferred_plan": "tie",
            "plan_a_score": 7,
            "plan_b_score": 7,
            "confidence": 0.5,
            "summary": "Same supported values.",
            "field_decisions": [],
        },
    )

    assert result.status == "completed"
    assert result.provider == "openai"
