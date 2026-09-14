from __future__ import annotations

from pathlib import Path

from scripts.second_pass_llm import (
    build_gemini_prompt,
    normalize_structured_output,
    parse_model_text,
    second_pass_extract,
)
import scripts.second_pass_llm as second_pass_module
from llm.agent.models import GeminiPromptContext, NormalizedInvoiceExtraction, RagSnippet
from llm.api.schemas import SecondPassExtractRequest
from rag.adaptive_rag import record_review_corrections

from dotenv import load_dotenv
import os

# Carrega as variáveis do arquivo .env
load_dotenv()

DEFAULT_MODEL = os.getenv('GEMINI_MODEL')




def test_prompt_includes_ocr_schema_and_rag_tip() -> None:
    prompt = build_gemini_prompt(
        "Fatura No FT 123 Total 10,00 EUR",
        provider_name="EPAL",
        invoice_type="water",
        rag_snippets=[{"type": "provider_tip", "provider": "EPAL", "text": "Total appears near Montante."}],
    )

    assert "Fatura No FT 123" in prompt
    assert "invoice_number" in prompt
    assert "Total appears near Montante." in prompt
    assert "Return plain text only" in prompt
    assert "field_name: value" in prompt


def test_missing_pdf_returns_clear_error() -> None:
    result = second_pass_extract("Fatura FT 123", api_key="unused", gemini_caller=lambda *_: "{}")

    assert not result.used
    assert "PDF file is required" in result.errors[0]


def test_missing_api_key_is_handled(tmp_path: Path) -> None:
    pdf_path = tmp_path / "invoice.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n%test\n")
    result = second_pass_extract(
        "Fatura FT 123 Total 10,00 EUR",
        pdf_file=pdf_path,
        api_key="",
        output_dir=tmp_path,
    )

    assert not result.used
    assert "Missing GEMINI_API_KEY" in result.errors[0]


def test_unparseable_text_response_is_handled(tmp_path: Path) -> None:
    pdf_path = tmp_path / "invoice.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n%test\n")
    result = second_pass_extract(
        "Fatura FT 123 Total 10,00 EUR",
        pdf_file=pdf_path,
        api_key="fake",
        output_dir=tmp_path,
        gemini_caller=lambda *_: "not parseable text",
    )

    assert not result.used
    assert "did not contain parseable key-value text" in result.errors[0]


def test_mocked_gemini_text_parses_and_routes_review(tmp_path: Path) -> None:
    pdf_path = tmp_path / "agua_01.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n%test\n")
    payload = "\n".join(
        [
            "invoice_type: water",
            "invoice_number: FT 123",
            "invoice_date: 2026-05-20",
            "currency: eur",
            "provider_name: EPAL",
            "provider_vat_number: 123456789",
            "total_value: 10,00",
            "uncertain_fields: payment_due_date",
        ]
    )

    result = second_pass_extract(
        "Fatura FT 123 Total 10,00 EUR",
        pdf_file=pdf_path,
        provider_name="EPAL",
        invoice_type="water",
        source_file="agua_01.webp",
        api_key="fake",
        output_dir=tmp_path,
        gemini_caller=lambda *_: payload,
    )

    assert result.used
    assert result.parsed["invoice_type"] == "water"
    assert result.parsed["currency"] == "EUR"
    assert result.parsed["total_value"] == "10.00"
    assert result.parsed["review_status"] == "manual_review_required"
    assert "payment_due_date" in result.parsed["missing_fields"]
    assert result.raw_response_path is not None
    assert Path(result.raw_response_path).exists()
    assert result.normalized_output_path is not None
    assert Path(result.normalized_output_path).exists()


def test_second_pass_artifacts_overwrite_stable_files(tmp_path: Path) -> None:
    pdf_path = tmp_path / "invoice.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n%test\n")

    def fake_caller(*_: object) -> str:
        return "\n".join(
            [
                "invoice_type: telecom",
                "invoice_number: FT 456",
                "invoice_date: 2026-05-20",
                "currency: EUR",
                "provider_name: Vodafone",
                "provider_vat_number: 123456789",
                "total_value: 25.00",
            ]
        )

    first = second_pass_extract(pdf_file=pdf_path, api_key="fake", output_dir=tmp_path, gemini_caller=fake_caller)
    second = second_pass_extract(pdf_file=pdf_path, api_key="fake", output_dir=tmp_path, gemini_caller=fake_caller)

    assert first.raw_response_path == second.raw_response_path
    assert first.normalized_output_path == second.normalized_output_path
    assert sorted(path.name for path in tmp_path.glob("invoice_gemini_*.*")) == [
        "invoice_gemini_raw.txt",
        "invoice_gemini_structured.json",
    ]


def test_pdf_file_is_passed_to_gemini_caller(tmp_path: Path) -> None:
    pdf_path = tmp_path / "invoice.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n%test\n")
    seen = {}

    def fake_caller(prompt: str, api_key: str, model: str, timeout: int, pdf_file: Path | None = None) -> str:
        seen["prompt"] = prompt
        seen["pdf_file"] = pdf_file
        return "\n".join(
            [
                "invoice_type: telecom",
                "invoice_number: FT 456",
                "invoice_date: 2026-05-20",
                "currency: EUR",
                "provider_name: Vodafone",
                "provider_vat_number: 123456789",
                "total_value: 25.00",
            ]
        )

    result = second_pass_extract(
        pdf_file=pdf_path,
        provider_name="vodafone",
        invoice_type="telecom",
        api_key="fake",
        output_dir=tmp_path,
        gemini_caller=fake_caller,
    )

    assert result.used
    assert seen["pdf_file"] == pdf_path
    assert "attached invoice PDF" in seen["prompt"]


def test_default_second_pass_uses_builtin_gemini_sdk_caller(monkeypatch, tmp_path: Path) -> None:
    pdf_path = tmp_path / "invoice.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n%test\n")
    seen = {}

    def fake_sdk_caller(prompt: str, api_key: str, model: str, timeout: int, *, pdf_file: Path | None = None) -> str:
        seen["api_key"] = api_key
        seen["model"] = model
        seen["pdf_file"] = pdf_file
        assert "attached invoice PDF" in prompt
        return "\n".join(
            [
                "invoice_type: telecom",
                "invoice_number: FT 456",
                "invoice_date: 2026-05-20",
                "currency: EUR",
                "provider_name: Vodafone",
                "provider_vat_number: 123456789",
                "total_value: 25.00",
            ]
        )

    monkeypatch.setattr(second_pass_module, "_call_gemini_with_sdk", fake_sdk_caller)

    result = second_pass_extract(
        pdf_file=pdf_path,
        api_key="fake-dashboard-key",
        #model="gemini-3.5-flash",
        model=DEFAULT_MODEL,
        output_dir=tmp_path,
    )

    assert result.used
    assert seen == {
        "api_key": "fake-dashboard-key",
        #"model": "gemini-3.5-flash",
        "model": DEFAULT_MODEL,
        "pdf_file": pdf_path,
    }


def test_missing_gemini_sdk_returns_actionable_error(monkeypatch, tmp_path: Path) -> None:
    pdf_path = tmp_path / "invoice.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n%test\n")

    def fake_sdk_caller(*_: object, **__: object) -> str:
        raise RuntimeError("Google Gemini SDK is not installed. Install `google-genai`.")

    monkeypatch.setattr(second_pass_module, "_call_gemini_with_sdk", fake_sdk_caller)

    result = second_pass_extract(pdf_file=pdf_path, api_key="fake", output_dir=tmp_path)

    assert not result.used
    assert "google-genai" in result.errors[0]


def test_parse_model_text_extracts_key_value_lines() -> None:
    parsed = parse_model_text("invoice_number: FT 123\nuncertain_fields: total_vat, buyer_name")

    assert parsed["invoice_number"] == "FT 123"
    assert parsed["uncertain_fields"] == ["total_vat", "buyer_name"]


def test_validation_errors_are_included(tmp_path: Path) -> None:
    pdf_path = tmp_path / "invoice.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n%test\n")
    result = second_pass_extract(
        "Fatura sem campos suficientes",
        pdf_file=pdf_path,
        api_key="fake",
        output_dir=tmp_path,
        gemini_caller=lambda *_: "invoice_type: not-valid",
    )

    assert result.used
    assert any("invoice_type must be" in error for error in result.parsed["validation_errors"])
    assert result.parsed["review_status"] == "manual_review_required"


def test_llm_type_files_validate_prompt_request_and_output(tmp_path: Path) -> None:
    request = SecondPassExtractRequest(pdf_file=tmp_path / "invoice.pdf", provider_name="EPAL", invoice_type="water")
    prompt_context = GeminiPromptContext(
        ocr_text="Fatura FT 123",
        provider_name=request.provider_name,
        invoice_type=request.invoice_type,
        rag_snippets=[RagSnippet(type="provider_tip", provider="EPAL", text="Total appears near Montante.")],
    )
    normalized = NormalizedInvoiceExtraction.model_validate(
        normalize_structured_output(
            {
                "invoice_type": "water",
                "invoice_number": "FT 123",
                "invoice_date": "2026-05-20",
                "currency": "eur",
                "provider_name": "EPAL",
                "provider_vat_number": "123456789",
                "total_value": "10,00",
            },
            source_file="agua_01.pdf",
        )
    )

    assert request.pdf_file.name == "invoice.pdf"
    assert prompt_context.rag_snippets[0].provider == "EPAL"
    assert normalized.currency == "EUR"
    assert normalized.total_value == "10.00"


def test_second_pass_auto_injects_llm_rag_snippets(tmp_path: Path) -> None:
    kb_path = tmp_path / "knowledge_base.json"
    pdf_path = tmp_path / "invoice.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n%test\n")
    record_review_corrections(
        {
            "source_file": "agua_02.png",
            "provider_name": "EPAL",
            "invoice_type": "water",
            "invoice_number": "null",
            "total_value": "null",
        },
        {"total_value": ("null", "10.00")},
        "Total appears near Montante.",
        kb_path,
    )
    seen = {}

    def fake_caller(prompt: str, *_: object, **__: object) -> str:
        seen["prompt"] = prompt
        return "\n".join(
            [
                "invoice_type: water",
                "invoice_number: FT 123",
                "invoice_date: 2026-05-20",
                "currency: EUR",
                "provider_name: EPAL",
                "provider_vat_number: 123456789",
                "total_value: 10.00",
            ]
        )

    result = second_pass_extract(
        "EPAL Montante",
        pdf_file=pdf_path,
        provider_name="EPAL",
        invoice_type="water",
        api_key="fake",
        output_dir=tmp_path,
        kb_path=kb_path,
        gemini_caller=fake_caller,
    )

    assert result.used
    assert "Total appears near Montante." in seen["prompt"]
