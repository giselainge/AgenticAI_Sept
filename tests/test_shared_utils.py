from invoice_parser.paths import DATA_ROOT, DEFAULT_OUTPUT_DIR, DEFAULT_TEXT_DIR, PROJECT_ROOT
from invoice_parser.schema import FIELDNAMES
from invoice_parser.text_utils import fold_text, normalize_money, normalize_space, null_if_empty


def test_project_paths_are_repo_relative() -> None:
    assert DATA_ROOT == PROJECT_ROOT / "data"
    assert DEFAULT_TEXT_DIR == DATA_ROOT / "data_txt"
    assert DEFAULT_OUTPUT_DIR == DATA_ROOT / "data_processed"


def test_invoice_schema_keeps_required_columns() -> None:
    assert FIELDNAMES[:4] == ["source_file", "ocr_text_file", "valid_invoice", "invoice_type"]
    assert "total_value" in FIELDNAMES
    assert FIELDNAMES[-1] == "extraction_warnings"


def test_text_helpers_normalize_common_values() -> None:
    assert fold_text("ÁGUA  São") == "agua  sao"
    assert normalize_space("  total\n\t value  ") == "total value"
    assert null_if_empty("   ") == "null"
    assert normalize_money("1.234,56 EUR") == "1234.56"
    assert normalize_money("123456") == "1234.56"

