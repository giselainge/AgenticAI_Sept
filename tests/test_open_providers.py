"""Synthetic bills only: no customer data, OCR executables, or external API calls."""

from pathlib import Path

import pytest

from invoice_parser.providers import canonical_provider, extract_provider_name
from rag.adaptive_rag import build_extraction_context, build_llm_rag_snippets, load_kb, record_review_corrections
from scripts.dashboard import provider_validated_count
from scripts.extract_invoice_fields import extract_provider, extract_row, extract_invoice_type
from scripts.ocr_text_extraction import score_ocr_quality


@pytest.mark.parametrize(("seller", "service", "expected_type", "provider_id"), [
    ("EEM - Empresa de Electricidade da Madeira, S.A.", "Eletricidade 120 kWh", "electricity", "eem"),
    ("ARM - Águas e Resíduos da Madeira, S.A.", "Água 12 m3", "water", "arm"),
    ("Águas da Vila, Lda.", "Abastecimento de água 12 m3", "water", "provider_aguas_da_vila_lda"),
    ("Ilha Energia, S.A.", "Eletricidade 120 kWh", "electricity", "provider_ilha_energia_s_a"),
    ("New Utility", "Water supply 12 m3", "water", "provider_new_utility"),
])
def test_unfamiliar_suppliers_are_valid_invoices(tmp_path, seller, service, expected_type, provider_id):
    text = f"""Fornecedor: {seller}
Fatura: FT 2026/123
Data de emissão: 14/09/2026
NIPC: 599999990
{service}
Subtotal: 10,00 EUR
Total IVA: 2,30 EUR
Total da Fatura: 12,30 EUR
"""
    source = tmp_path / "uploaded_bill.txt"
    source.write_text(text, encoding="utf-8")
    row = extract_row(source)
    assert row["valid_invoice"] == "true"
    assert row["invoice_type"] == expected_type
    assert row["provider_name"] == seller
    assert canonical_provider(row["provider_name"], text) == provider_id
    assert row["total_value"] == "12.30"


def test_provider_is_not_inferred_from_electricity_or_incidental_brand():
    assert extract_provider("Eletricidade\nFatura: FT 123\n120 kWh") is None
    assert extract_provider("Ilha Energia, S.A.\nEletricidade\nPague via Vodafone") == "Ilha Energia, S.A."
    assert canonical_provider("Ilha Energia", "EDP Vodafone EPAL") == "provider_ilha_energia"
    assert canonical_provider("Águas da Vila, Lda.") == canonical_provider("AGUAS DA VILA LDA")
    assert canonical_provider("", "Fatura: FT 123\nEletricidade") == "unknown"


def test_known_provider_below_long_header_is_still_recognized():
    text = "\n".join([f"Account detail {index}" for index in range(14)] + ["GALP", "Total: 10,00 EUR"])

    assert extract_provider_name(text) == "GALP"


def test_unrecognized_document_remains_invalid(tmp_path):
    source = tmp_path / "receipt.txt"
    source.write_text("Fornecedor: Office Supplies, Lda.\nFatura: FT 123\nData de emissão: 14/09/2026\n"
                      "Material de escritório e mobiliário\nTotal da Fatura: 12,30 EUR", encoding="utf-8")
    row = extract_row(source)
    assert row["valid_invoice"] == "false"
    assert row["invoice_type"] == "unsupported"
    assert "Unsupported document" in row["extraction_warnings"]
    assert extract_invoice_type("bill", "Pagamento de material de escritório") == "unsupported"


def test_portuguese_electricity_layout_with_named_dates_is_extracted(tmp_path):
    source = tmp_path / "luz_invoice.txt"
    source.write_text(
        """EDP Comercial - Comercialização de Energia, S.A.
Fatura nº FT2026 A123/456 De: 14 de setembro de 2026 Valor: 52,30 EUR
Período de faturação: 15 de agosto a 14 de setembro de 2026
Serviços - EDP Full
Consumo real 120 kWh
Total s/IVA 42,52 EUR
Total da Fatura: 52,30 EUR
""",
        encoding="utf-8",
    )

    row = extract_row(source)

    assert row["invoice_type"] == "electricity"
    assert row["provider_name"] == "EDP"
    assert row["invoice_number"].startswith("FT2026")
    assert row["invoice_date"] == "2026-09-14"
    assert row["consumption_start_date"] == "2026-08-15"
    assert row["consumption_end_date"] == "2026-09-14"
    assert row["service_plan_name"] == "EDP Full"
    assert row["units_of_consumption"] == "120.00"
    assert row["total_value"] == "52.30"


def test_ocr_quality_is_independent_of_known_brands():
    body = "\nFatura cliente consumo total valor IVA NIF pagamento\n14/09/2026\n12,30 EUR\n599999990\n120 kWh"
    # Equal-length names isolate brand recognition from character/noise scoring.
    known = score_ocr_quality("Vodafone EPAL Galp" + body)
    unfamiliar = score_ocr_quality("Novafone NOVA Nova" + body)
    assert known["score"] == unfamiliar["score"]
    assert known["warnings"] == unfamiliar["warnings"]
    assert "provider_keyword_count" not in known
    assert "provider_keywords" not in known["signals"]


def test_new_providers_keep_separate_feedback_and_retrieval(tmp_path):
    kb_path = tmp_path / "kb.json"
    for name, value in [("Águas da Vila", "10.00"), ("Águas da Serra", "20.00")]:
        record_review_corrections({"provider_name": name, "invoice_type": "water"},
                                 {"total_value": ("null", value)}, "Read total", kb_path)
    kb = load_kb(kb_path)
    assert len(kb["providers"]) == 2
    assert kb["providers"][canonical_provider("Águas da Vila")]["provider_name"] == "Águas da Vila"
    context = build_extraction_context("Águas da Serra EPAL", "Águas da Vila", "water", kb_path=kb_path)
    assert [p["provider_id"] for p in context["providers"]] == [canonical_provider("Águas da Vila")]
    assert context["providers"][0]["human_reviewer_feedback"][0]["corrected_value"] == "10.00"
    assert build_extraction_context("Fatura água", kb_path=kb_path)["providers"] == []
    assert provider_validated_count({"providers": {"unknown": {
        "previously_validated_invoices": [{"valid_invoice": "true"}] * 5}}}, "unknown") == 0


def test_llm_snippets_use_only_the_identified_providers_json_memory(tmp_path):
    kb_path = tmp_path / "kb.json"
    record_review_corrections({"provider_name": "Águas da Vila"},
                             {"total_value": ("null", "10.00")}, "Own supplier guidance", kb_path)
    record_review_corrections({"provider_name": "EPAL"},
                             {"total_value": ("null", "99.00")}, "Other supplier guidance", kb_path)
    snippets = build_llm_rag_snippets("Fatura água", "Águas da Vila", "water", kb_path=kb_path)
    assert snippets
    assert all("Other supplier" not in item.text for item in snippets)
    assert any("Own supplier guidance" in item.text for item in snippets)
