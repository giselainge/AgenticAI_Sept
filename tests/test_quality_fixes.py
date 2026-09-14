from __future__ import annotations

import os
import subprocess
import sys
import time
from types import SimpleNamespace
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_scripts_expose_help_when_run_directly() -> None:
    for script_name in ["dashboard.py", "ocr_text_extraction.py", "extract_invoice_fields.py"]:
        completed = subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "scripts" / script_name), "--help"],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )

        assert completed.returncode == 0
        assert "usage:" in completed.stdout.lower()


def test_active_safety_config_files_exist() -> None:
    assert (PROJECT_ROOT / ".gitignore").exists()
    assert (PROJECT_ROOT / ".env.example").exists()
    gitignore = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert ".env" in gitignore
    assert "data/data_raw/" not in gitignore
    assert "data/data_pdf/" not in gitignore
    assert "data/data_txt/" not in gitignore
    assert "data/data_processed/invoice_structured_fields.csv" not in gitignore
    assert "rag/knowledge_base.json" in gitignore
    assert "your-gemini-api-key-here" in (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")


def test_workflow_graph_is_documented_and_linked() -> None:
    workflow_graph = PROJECT_ROOT / "docs" / "workflow_graph.md"
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    graph_text = workflow_graph.read_text(encoding="utf-8")

    assert workflow_graph.exists()
    assert "docs/workflow_graph.md" in readme
    assert "```mermaid" in graph_text
    assert "data/data_raw/" in graph_text


def test_ocr_config_has_no_machine_specific_tesseract_path() -> None:
    ocr_source = (PROJECT_ROOT / "scripts" / "ocr_text_extraction.py").read_text(encoding="utf-8")

    assert "pedro.carreiro" not in ocr_source
    assert "TESSERACT_CMD" in ocr_source
    assert "\nconfigure_local_ocr_environment()\n" not in ocr_source


def test_dashboard_helpers_are_importable_and_keep_pdf_paths_inside_pdf_dir() -> None:
    from scripts.dashboard import DEFAULT_PDF_DIR, safe_filename, source_pdf_name, source_pdf_path

    assert safe_filename(r"..\private\invoice?.pdf") == "invoice.pdf"

    row = {"source_file": r"..\outside\secret.pdf", "ocr_text_file": "null"}
    assert source_pdf_name(row) == "secret.pdf"
    assert source_pdf_path(row) == DEFAULT_PDF_DIR / "secret.pdf"


def test_selected_ocr_batch_imports_packaged_ocr_module(tmp_path: Path) -> None:
    from scripts.dashboard import DashboardConfig, batch_job_snapshot, create_batch_job, run_selected_ocr_batch_job

    job_id = create_batch_job("ocr", [])
    config = DashboardConfig(csv_path=tmp_path / "invoice_structured_fields.csv", kb_path=tmp_path / "knowledge_base.json")

    run_selected_ocr_batch_job(job_id, [], config)

    job = batch_job_snapshot(job_id)
    assert job is not None
    assert job["status"] == "complete"
    assert "0 updated" in job["summary"]


def test_import_processes_existing_raw_file_without_overwriting(monkeypatch, tmp_path: Path) -> None:
    import scripts.dashboard as dashboard
    import scripts.ocr_text_extraction as ocr_module

    raw_dir = tmp_path / "data_raw"
    pdf_dir = tmp_path / "data_pdf"
    text_dir = tmp_path / "data_txt"
    raw_dir.mkdir()
    pdf_dir.mkdir()
    text_dir.mkdir()
    existing_raw = raw_dir / "agua_01.webp"
    existing_raw.write_bytes(b"original raw bytes")
    processed_paths: list[Path] = []

    monkeypatch.setattr(dashboard, "DEFAULT_RAW_DIR", raw_dir)
    monkeypatch.setattr(dashboard, "DEFAULT_PDF_DIR", pdf_dir)
    monkeypatch.setattr(dashboard, "DEFAULT_TEXT_DIR", text_dir)
    monkeypatch.setattr(ocr_module, "process_file", lambda path: processed_paths.append(Path(path)) or SimpleNamespace(errors=[]))
    monkeypatch.setattr(
        dashboard,
        "extract_batch",
        lambda text_path, csv_path: [
            {
                "source_file": str(existing_raw),
                "ocr_text_file": str(text_dir / "agua_01.txt"),
                "valid_invoice": "true",
                "invoice_type": "water",
            }
        ],
    )
    monkeypatch.setattr(dashboard, "seed_from_validated_csv", lambda *_args, **_kwargs: None)
    config = dashboard.DashboardConfig(csv_path=tmp_path / "invoice_structured_fields.csv", kb_path=tmp_path / "knowledge_base.json")

    success, warnings = dashboard.process_imported_files([("agua_01.webp", b"new upload bytes")], config)

    assert processed_paths == [existing_raw]
    assert existing_raw.read_bytes() == b"original raw bytes"
    assert any("already existed in raw data and was processed successfully" in message for message in success)
    assert not any("already in the system" in message for message in warnings)


def test_import_blocks_already_processed_invoice(monkeypatch, tmp_path: Path) -> None:
    import scripts.dashboard as dashboard
    import scripts.ocr_text_extraction as ocr_module

    raw_dir = tmp_path / "data_raw"
    pdf_dir = tmp_path / "data_pdf"
    text_dir = tmp_path / "data_txt"
    raw_dir.mkdir()
    pdf_dir.mkdir()
    text_dir.mkdir()
    (text_dir / "agua_01.txt").write_text("already processed", encoding="utf-8")
    processed_paths: list[Path] = []

    monkeypatch.setattr(dashboard, "DEFAULT_RAW_DIR", raw_dir)
    monkeypatch.setattr(dashboard, "DEFAULT_PDF_DIR", pdf_dir)
    monkeypatch.setattr(dashboard, "DEFAULT_TEXT_DIR", text_dir)
    monkeypatch.setattr(ocr_module, "process_file", lambda path: processed_paths.append(Path(path)) or SimpleNamespace(errors=[]))
    config = dashboard.DashboardConfig(csv_path=tmp_path / "invoice_structured_fields.csv", kb_path=tmp_path / "knowledge_base.json")

    success, warnings = dashboard.process_imported_files([("agua_01.webp", b"upload bytes")], config)

    assert success == []
    assert processed_paths == []
    assert warnings == ["agua_01.webp is already in the system. It was not imported."]


def test_import_raw_match_requires_same_filename_not_just_stem(monkeypatch, tmp_path: Path) -> None:
    import scripts.dashboard as dashboard
    import scripts.ocr_text_extraction as ocr_module

    raw_dir = tmp_path / "data_raw"
    pdf_dir = tmp_path / "data_pdf"
    text_dir = tmp_path / "data_txt"
    raw_dir.mkdir()
    pdf_dir.mkdir()
    text_dir.mkdir()
    existing_png = raw_dir / "agua_01.png"
    existing_png.write_bytes(b"existing png bytes")
    uploaded_webp = raw_dir / "agua_01.webp"
    processed_paths: list[Path] = []

    monkeypatch.setattr(dashboard, "DEFAULT_RAW_DIR", raw_dir)
    monkeypatch.setattr(dashboard, "DEFAULT_PDF_DIR", pdf_dir)
    monkeypatch.setattr(dashboard, "DEFAULT_TEXT_DIR", text_dir)
    monkeypatch.setattr(ocr_module, "process_file", lambda path: processed_paths.append(Path(path)) or SimpleNamespace(errors=[]))
    monkeypatch.setattr(
        dashboard,
        "extract_batch",
        lambda text_path, csv_path: [
            {
                "source_file": str(uploaded_webp),
                "ocr_text_file": str(text_dir / "agua_01.txt"),
                "valid_invoice": "true",
                "invoice_type": "water",
            }
        ],
    )
    monkeypatch.setattr(dashboard, "seed_from_validated_csv", lambda *_args, **_kwargs: None)
    config = dashboard.DashboardConfig(csv_path=tmp_path / "invoice_structured_fields.csv", kb_path=tmp_path / "knowledge_base.json")

    success, warnings = dashboard.process_imported_files([("agua_01.webp", b"uploaded webp bytes")], config)

    assert processed_paths == [uploaded_webp]
    assert existing_png.read_bytes() == b"existing png bytes"
    assert uploaded_webp.read_bytes() == b"uploaded webp bytes"
    assert any("was imported successfully" in message for message in success)
    assert not warnings


def test_import_upload_starts_progress_job(monkeypatch, tmp_path: Path) -> None:
    import scripts.dashboard as dashboard

    started: dict[str, object] = {}

    class FakeThread:
        def __init__(self, target, args, daemon=False):
            started["target"] = target
            started["args"] = args
            started["daemon"] = daemon

        def start(self):
            started["started"] = True

    monkeypatch.setattr(dashboard, "parse_import_payload", lambda *_args: ([("agua_01.webp", b"data")], {}))
    monkeypatch.setattr(dashboard, "Thread", FakeThread)
    monkeypatch.setattr(dashboard, "run_import_job", lambda *_args: None)
    config = dashboard.DashboardConfig(csv_path=tmp_path / "invoice_structured_fields.csv", kb_path=tmp_path / "knowledge_base.json")

    path = dashboard.handle_import_upload("multipart/form-data", b"payload", config)

    assert path.startswith("/?view=import&job=")
    assert started["target"] is dashboard.run_import_job
    assert started["daemon"] is True
    assert started["started"] is True
    job_id = str(started["args"][0])
    job = dashboard.batch_job_snapshot(job_id)
    assert job is not None
    assert job["kind"] == "import"
    assert job["items"][0]["label"] == "agua_01.webp"


def test_import_page_renders_progress_panel() -> None:
    import scripts.dashboard as dashboard

    job_id = dashboard.create_batch_job("import", ["agua_01.webp"])

    html = dashboard.render_import({"job": [job_id]})

    assert f'data-job-id="{job_id}"' in html
    assert "Batch Progress" in html


def test_load_dashboard_rows_refreshes_when_text_files_are_newer_than_csv(tmp_path: Path, monkeypatch) -> None:
    PROJECT_ROOT = Path(__file__).resolve().parents[1]
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))

    import scripts.dashboard as dashboard

    csv_path = tmp_path / "invoice_structured_fields.csv"
    text_dir = tmp_path / "data_txt"
    text_dir.mkdir(parents=True, exist_ok=True)
    csv_path.write_text("source_file,ocr_text_file,valid_invoice\nold,row,true\n", encoding="utf-8")
    new_text = text_dir / "nova_01.txt"
    new_text.write_text("OCR body", encoding="utf-8")
    newer = csv_path.stat().st_mtime + 2
    os.utime(new_text, (newer, newer))

    def fake_extract_batch(text_path, output_csv):
        assert text_path == text_dir
        assert output_csv == csv_path
        return [{"source_file": "nova_01.pdf", "ocr_text_file": str(new_text), "valid_invoice": "true", "invoice_type": "water"}]

    monkeypatch.setattr(dashboard, "DEFAULT_TEXT_DIR", text_dir)
    monkeypatch.setattr(dashboard, "extract_batch", fake_extract_batch)

    rows = dashboard.load_dashboard_rows(dashboard.DashboardConfig(csv_path=csv_path, kb_path=tmp_path / "knowledge_base.json"))

    assert rows[0]["source_file"] == "nova_01.pdf"
    assert rows[0]["ocr_text_file"] == str(new_text)


def test_import_job_marks_unfinished_items_on_fatal_error(monkeypatch, tmp_path: Path) -> None:
    import scripts.dashboard as dashboard

    def fail_import(*_args, **_kwargs):
        raise RuntimeError("boom")

    job_id = dashboard.create_batch_job("import", ["agua_01.webp", "gas_01.webp"])
    monkeypatch.setattr(dashboard, "process_imported_files", fail_import)
    config = dashboard.DashboardConfig(csv_path=tmp_path / "invoice_structured_fields.csv", kb_path=tmp_path / "knowledge_base.json")

    dashboard.run_import_job(job_id, [("agua_01.webp", b"a"), ("gas_01.webp", b"b")], {}, config)

    job = dashboard.batch_job_snapshot(job_id)
    assert job is not None
    assert job["status"] == "error"
    assert all(item["status"] == "error" for item in job["items"])


def test_import_job_keeps_warning_when_gemini_succeeds_after_ocr_warning(monkeypatch, tmp_path: Path) -> None:
    import scripts.dashboard as dashboard
    import scripts.ocr_text_extraction as ocr_module
    import scripts.second_pass_llm as second_pass_module

    raw_dir = tmp_path / "data_raw"
    pdf_dir = tmp_path / "data_pdf"
    text_dir = tmp_path / "data_txt"
    raw_dir.mkdir()
    pdf_dir.mkdir()
    text_dir.mkdir()
    pdf_path = pdf_dir / "agua_01.pdf"
    processed_paths: list[Path] = []

    def fake_process_file(path: Path) -> SimpleNamespace:
        processed_paths.append(Path(path))
        pdf_path.write_bytes(b"%PDF-1.4\n")
        return SimpleNamespace(errors=["low quality"])

    monkeypatch.setattr(dashboard, "DEFAULT_RAW_DIR", raw_dir)
    monkeypatch.setattr(dashboard, "DEFAULT_PDF_DIR", pdf_dir)
    monkeypatch.setattr(dashboard, "DEFAULT_TEXT_DIR", text_dir)
    monkeypatch.setattr(ocr_module, "process_file", fake_process_file)
    monkeypatch.setattr(
        dashboard,
        "extract_batch",
        lambda text_path, csv_path: [
            {
                **{field: "null" for field in dashboard.FIELDNAMES},
                "source_file": str(raw_dir / "agua_01.webp"),
                "ocr_text_file": str(text_dir / "agua_01.txt"),
                "valid_invoice": "true",
                "invoice_type": "water",
                "provider_name": "EPAL",
                "invoice_number": "FT 123",
                "invoice_date": "2026-05-20",
                "currency": "EUR",
                "provider_vat_number": "123456789",
                "total_value": "10.00",
            }
        ],
    )
    monkeypatch.setattr(dashboard, "seed_from_validated_csv", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        second_pass_module,
        "second_pass_extract",
        lambda **_kwargs: SimpleNamespace(
            errors=[],
            parsed={
                "invoice_type": "water",
                "invoice_number": "FT 123",
                "invoice_date": "2026-05-20",
                "currency": "EUR",
                "provider_name": "EPAL",
                "provider_vat_number": "123456789",
                "total_value": "10.00",
                "review_status": "ready_for_review",
                "validation_errors": [],
            },
            normalized_output_path=str(tmp_path / "agua_01_gemini_structured.json"),
        ),
    )
    job_id = dashboard.create_batch_job("import", ["agua_01.webp"])
    config = dashboard.DashboardConfig(csv_path=tmp_path / "invoice_structured_fields.csv", kb_path=tmp_path / "knowledge_base.json")

    dashboard.process_imported_files([("agua_01.webp", b"raw")], config, gemini_api_key="fake", job_id=job_id)

    job = dashboard.batch_job_snapshot(job_id)
    assert job is not None
    assert job["items"][0]["status"] == "warning"
    assert job["items"][0]["message"] == "Gemini applied; earlier warnings remain."


def test_invoice_pipeline_statuses_are_ocr_llm_and_approved(monkeypatch, tmp_path: Path) -> None:
    import scripts.dashboard as dashboard

    monkeypatch.setattr(dashboard, "DEFAULT_LLM_SECOND_PASS_DIR", tmp_path / "llm_second_pass")
    row = {field: "null" for field in dashboard.FIELDNAMES}
    row.update(
        {
            "source_file": "agua_01.webp",
            "ocr_text_file": "data/data_txt/agua_01.txt",
            "valid_invoice": "true",
            "invoice_type": "water",
            "provider_name": "EPAL",
            "invoice_number": "FT 123",
            "invoice_date": "2026-05-20",
            "currency": "EUR",
            "provider_vat_number": "123456789",
            "total_value": "10.00",
            "extraction_warnings": "null",
        }
    )

    ocr_state = dashboard.review_state(row, {"providers": {}})
    assert dashboard.status_key(ocr_state) == "ocr"
    assert dashboard.status_label(ocr_state) == "OCR"

    llm_row = dict(row)
    llm_row["extraction_warnings"] = "Gemini artifact: data/data_processed/llm_second_pass/agua_01_gemini_structured.json"
    llm_state = dashboard.review_state(llm_row, {"providers": {}})
    assert dashboard.status_key(llm_state) == "llm"
    assert dashboard.status_label(llm_state) == "LLM"

    provider_id = dashboard.canonical_provider(row.get("provider_name"), " ".join(row.values()))
    approved_kb = {
        "providers": {
            provider_id: {
                "previously_validated_invoices": [
                    {
                        "signature": dashboard.row_signature(row),
                        "review_decision": "human_approved",
                        "valid_invoice": "true",
                    }
                ]
            }
        }
    }
    approved_state = dashboard.review_state(row, approved_kb)
    assert dashboard.status_key(approved_state) == "approved"
    assert dashboard.status_label(approved_state) == "Approved"


def test_approved_status_survives_mutable_field_changes(monkeypatch, tmp_path: Path) -> None:
    import scripts.dashboard as dashboard

    monkeypatch.setattr(dashboard, "DEFAULT_LLM_SECOND_PASS_DIR", tmp_path / "llm_second_pass")
    row = {field: "null" for field in dashboard.FIELDNAMES}
    row.update(
        {
            "source_file": "agua_01.webp",
            "ocr_text_file": "data/data_txt/agua_01.txt",
            "valid_invoice": "true",
            "invoice_type": "water",
            "provider_name": "EPAL",
            "invoice_number": "FT 123",
            "invoice_date": "2026-05-20",
            "currency": "EUR",
            "provider_vat_number": "123456789",
            "total_value": "10.00",
        }
    )
    approved_memory = dict(row)
    edited_row = dict(row)
    edited_row["invoice_number"] = "FT 999"
    edited_row["total_value"] = "12.50"
    provider_id = dashboard.canonical_provider(row.get("provider_name"), " ".join(row.values()))
    kb = {
        "providers": {
            provider_id: {
                "previously_validated_invoices": [
                    {
                        "signature": dashboard.row_signature(approved_memory),
                        "source_file": approved_memory["source_file"],
                        "ocr_text_file": approved_memory["ocr_text_file"],
                        "review_decision": "human_approved",
                    }
                ]
            }
        }
    }

    state = dashboard.review_state(edited_row, kb)

    assert dashboard.status_key(state) == "approved"


def test_overview_status_filter_uses_pipeline_statuses() -> None:
    from scripts.dashboard import render_overview_filters

    html = render_overview_filters([], {"status": ["llm"]})

    assert "OCR</option>" in html
    assert "LLM</option>" in html
    assert "Approved</option>" in html
    assert "Needs review" not in html
    assert "Auto-approved" not in html


def test_rag_context_shows_selected_invoice_and_compact_provider_context(monkeypatch, tmp_path: Path) -> None:
    import scripts.dashboard as dashboard

    monkeypatch.setattr(dashboard, "DEFAULT_LLM_SECOND_PASS_DIR", tmp_path / "llm_second_pass")
    ocr_file = tmp_path / "agua_01.txt"
    ocr_file.write_text("Selected invoice OCR text only.", encoding="utf-8")
    row = {field: "null" for field in dashboard.FIELDNAMES}
    row.update(
        {
            "source_file": "agua_01.webp",
            "ocr_text_file": str(ocr_file),
            "valid_invoice": "true",
            "invoice_type": "water",
            "provider_name": "EPAL",
            "invoice_number": "FT 123",
            "invoice_date": "2026-05-20",
            "currency": "EUR",
            "provider_vat_number": "123456789",
            "total_value": "10.00",
        }
    )
    kb = {
        "providers": {
            "epal": {
                "provider_name": "EPAL",
                "provider_specific_extraction_tips": ["Check total near Montante."],
                "common_ocr_corrections": {"O": "0"},
                "known_invoice_layouts": [
                    {"invoice_type": "water", "fields_seen": ["invoice_number", "total_value"]},
                    {"invoice_type": "telecom", "fields_seen": ["service_plan_name"]},
                ],
                "field_correction_patterns": {
                    "total_value": {
                        "total_corrections": 2,
                        "recent_examples": [{"old_value": "null", "corrected_value": "10.00"}],
                    },
                    "service_plan_name": {
                        "total_corrections": 1,
                        "recent_examples": [{"old_value": "null", "corrected_value": "Fiber"}],
                    },
                },
                "human_reviewer_feedback": [{"field_name": "total_value", "note": "Broad history should not render."}],
                "validation_history": [{"event": "human_approved", "note": "History should not render."}],
            }
        }
    }

    html = dashboard.render_rag([row], kb, 0)

    assert "Selected Invoice Details" in html
    assert "agua_01.webp" in html
    assert "FT 123" in html
    assert "Selected invoice OCR text only." in html
    assert "Provider Context" in html
    assert "EPAL" in html
    assert "Check total near Montante." in html
    assert "O -> 0" in html
    assert "invoice_number, total_value" in html
    assert "total_value: 2 correction(s)" in html
    assert "Fiber" not in html
    assert "Broad history should not render." not in html
    assert "History should not render." not in html
    assert "Past approved examples" not in html
    assert "Helpful Memory For This Invoice" not in html
    assert "Recent Similar Invoices" not in html


def test_informational_ocr_warning_does_not_force_manual_review() -> None:
    from scripts.ocr_text_extraction import Layer6Result, update_review_flags

    result = Layer6Result(
        source_file="invoice.pdf",
        file_type="pdf",
        selected_text="Invoice text with enough characters " * 10,
        warnings=["Skipped OCR because the final text file already exists."],
    )
    update_review_flags(
        result,
        {
            "score": 0.9,
            "character_count": 300,
            "invoice_keyword_count": 1,
            "money_count": 1,
            "date_count": 1,
        },
    )

    assert result.requires_manual_review is False
