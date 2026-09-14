from pathlib import Path

import pytest

from invoice_parser.paths import DEFAULT_OUTPUT_DIR
from invoice_parser.schema import FIELDNAMES
from llm.agent.models import RagSnippet
from rag.adaptive_rag import (
    DEFAULT_VALIDATED_CSV,
    FIELD_NAMES,
    build_extraction_context,
    build_llm_rag_snippets,
    canonical_provider,
    load_kb,
    record_review_corrections,
    seed_from_validated_csv,
)


def test_rag_uses_shared_invoice_schema_and_paths() -> None:
    expected_fields = [
        field
        for field in FIELDNAMES
        if field not in {"source_file", "ocr_text_file", "valid_invoice", "extraction_warnings"}
    ]

    assert FIELD_NAMES == expected_fields
    assert DEFAULT_VALIDATED_CSV == DEFAULT_OUTPUT_DIR / "invoice_structured_fields.csv"


def test_provider_aliases_are_canonicalized_case_and_accent_insensitive() -> None:
    assert canonical_provider("Águas do EPAL") == "epal"
    assert canonical_provider("", "VODAFONE TELEKOMUNIKASYON A.S.") == "vodafone_tr"


def test_seed_from_validated_csv_requires_existing_csv(tmp_path: Path) -> None:
    missing_csv = tmp_path / "missing.csv"
    kb_path = tmp_path / "knowledge_base.json"

    with pytest.raises(FileNotFoundError, match="Validated invoice CSV does not exist"):
        seed_from_validated_csv(missing_csv, kb_path)


def test_review_corrections_update_feedback_and_field_patterns(tmp_path: Path) -> None:
    kb_path = tmp_path / "knowledge_base.json"
    row = {
        "source_file": "agua_02.png",
        "ocr_text_file": "data/data_txt/agua_02.txt",
        "provider_name": "EPAL",
        "invoice_type": "water",
        "invoice_number": "null",
        "invoice_date": "null",
        "currency": "EUR",
        "payment_due_date": "null",
        "provider_vat_number": "500024170",
        "total_value": "null",
    }
    changes = {
        "invoice_number": ("null", "FT 123"),
        "total_value": ("null", "10.00"),
    }

    feedback = record_review_corrections(row, changes, "Corrected during manual review.", kb_path)
    kb = load_kb(kb_path)
    provider = kb["providers"]["epal"]

    assert len(feedback) == 2
    assert {item["field_name"] for item in provider["human_reviewer_feedback"]} == {"invoice_number", "total_value"}
    assert provider["field_correction_patterns"]["invoice_number"]["total_corrections"] == 1
    assert provider["field_correction_patterns"]["total_value"]["recent_examples"][0]["corrected_value"] == "10.00"
    assert provider["validation_history"][-1]["event"] == "human_review_corrections_recorded"


def test_extraction_context_includes_field_correction_patterns(tmp_path: Path) -> None:
    kb_path = tmp_path / "knowledge_base.json"
    row = {
        "source_file": "agua_02.png",
        "provider_name": "EPAL",
        "invoice_type": "water",
        "invoice_number": "null",
        "total_value": "null",
    }
    record_review_corrections(row, {"total_value": ("null", "10.00")}, "Total appears near Montante.", kb_path)

    context = build_extraction_context("EPAL Montante", provider_hint="EPAL", invoice_type="water", kb_path=kb_path)

    assert context["providers"]
    assert context["providers"][0]["field_correction_patterns"]["total_value"]["total_corrections"] == 1


def test_llm_rag_snippets_use_llm_models(tmp_path: Path) -> None:
    kb_path = tmp_path / "knowledge_base.json"
    row = {
        "source_file": "agua_02.png",
        "provider_name": "EPAL",
        "invoice_type": "water",
        "invoice_number": "null",
        "total_value": "null",
    }
    record_review_corrections(row, {"total_value": ("null", "10.00")}, "Total appears near Montante.", kb_path)

    snippets = build_llm_rag_snippets("EPAL Montante", provider_hint="EPAL", invoice_type="water", kb_path=kb_path)

    assert snippets
    assert all(isinstance(snippet, RagSnippet) for snippet in snippets)
    assert any("Montante" in snippet.text or "total_value" in snippet.text for snippet in snippets)
