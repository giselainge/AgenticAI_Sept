from invoice_parser.postprocess import sanitize_invoice_row


def test_financial_value_is_rejected_from_buyer_address() -> None:
    row, rejected = sanitize_invoice_row(
        {
            "buyer_address": "Rua do Mercado 12, 1000-001 Lisboa 37,50 EUR",
            "extraction_warnings": "null",
        }
    )

    assert row["buyer_address"] == "null"
    assert rejected == ["buyer_address"]
    assert row["extraction_warnings"] == "Post-processing rejected implausible field(s): buyer_address."


def test_valid_address_keeps_street_number_floor_and_postal_code() -> None:
    value = "Rua do Mercado 12, 3.º Esq., 1000-001 Lisboa"

    row, rejected = sanitize_invoice_row({"buyer_address": value})

    assert row["buyer_address"] == value
    assert rejected == []


def test_names_and_vat_numbers_cannot_contain_financial_values() -> None:
    row, rejected = sanitize_invoice_row(
        {
            "provider_name": "Total a pagar 45,20 EUR",
            "buyer_vat_number": "IVA 45,20 EUR",
        }
    )

    assert row["provider_name"] == "null"
    assert row["buyer_vat_number"] == "null"
    assert rejected == ["buyer_vat_number", "provider_name"]


def test_supported_scalar_values_are_normalized_to_one_schema() -> None:
    row, rejected = sanitize_invoice_row(
        {
            "invoice_type": "gás natural",
            "invoice_date": "14/09/2026",
            "currency": "€",
            "provider_vat_number": "PT 501 234 567",
            "units_of_consumption": "12,5 kWh",
            "unit_type": "KWH",
            "total_value": "12,30 EUR",
            "valid_invoice": "TRUE",
        }
    )

    assert rejected == []
    assert row["invoice_type"] == "natural gas"
    assert row["invoice_date"] == "2026-09-14"
    assert row["currency"] == "EUR"
    assert row["provider_vat_number"] == "PT501234567"
    assert row["units_of_consumption"] == "12.50"
    assert row["unit_type"] == "kWh"
    assert row["total_value"] == "12.30"
    assert row["valid_invoice"] == "true"

