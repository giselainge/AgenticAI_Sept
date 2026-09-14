import csv
from pathlib import Path

from rag.adaptive_rag import ensure_seeded_kb, load_kb, record_validated_invoice, seed_from_extracted_csv
from rag.sqlite_store import database_counts


def test_sqlite_provider_memory_round_trip(tmp_path: Path) -> None:
    db_path = tmp_path / "knowledge_base.sqlite3"
    csv_path = tmp_path / "invoices.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["source_file", "provider_name", "invoice_type", "invoice_number", "total_value"],
        )
        writer.writeheader()
        writer.writerow(
            {
                "source_file": "water.pdf",
                "provider_name": "EPAL",
                "invoice_type": "water",
                "invoice_number": "FT 123",
                "total_value": "12.30",
            }
        )

    seed_from_extracted_csv(csv_path, db_path)
    seeded = load_kb(db_path)
    assert "epal" in seeded["providers"]
    assert len(seeded["providers"]["epal"]["observed_invoices"]) == 1
    assert seeded["providers"]["epal"]["previously_validated_invoices"] == []

    record_validated_invoice(
        {
            "source_file": "water.pdf",
            "provider_name": "EPAL",
            "invoice_type": "water",
            "invoice_number": "FT 123",
            "total_value": "12.30",
        },
        "Checked against source.",
        db_path,
    )
    reloaded = load_kb(db_path)
    assert reloaded["providers"]["epal"]["previously_validated_invoices"][0]["review_decision"] == "human_approved"
    assert database_counts(db_path)["observed_invoices"] == 1
    assert database_counts(db_path)["validated_invoices"] == 1


def test_fresh_sqlite_database_has_safe_provider_catalog_without_invoice_data(tmp_path: Path) -> None:
    db_path = tmp_path / "knowledge_base.sqlite3"

    kb = ensure_seeded_kb(csv_path=tmp_path / "missing.csv", kb_path=db_path)

    assert {"edp", "eem", "epal", "arm", "galp", "vodafone_pt"}.issubset(kb["providers"])
    assert all(
        not provider["observed_invoices"] and not provider["previously_validated_invoices"]
        for provider in kb["providers"].values()
    )
    assert database_counts(db_path)["providers"] == len(kb["providers"])
