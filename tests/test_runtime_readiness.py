from pathlib import Path

from invoice_parser import runtime


def test_runtime_report_has_stable_quality_fingerprint(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(runtime, "DATA_ROOT", tmp_path / "data")
    monkeypatch.setattr(runtime, "DEFAULT_RAW_DIR", tmp_path / "data" / "data_raw")
    monkeypatch.setattr(runtime, "DEFAULT_PDF_DIR", tmp_path / "data" / "data_pdf")
    monkeypatch.setattr(runtime, "DEFAULT_TEXT_DIR", tmp_path / "data" / "data_txt")
    monkeypatch.setattr(runtime, "DEFAULT_OUTPUT_DIR", tmp_path / "data" / "data_processed")
    monkeypatch.setattr(runtime, "DEFAULT_REPORTS_DIR", tmp_path / "data" / "data_processed" / "reports")
    monkeypatch.setattr(runtime, "DEFAULT_VECTOR_STORE_DIR", tmp_path / "runtime" / "vector_store")
    monkeypatch.setattr(runtime, "_command_version", lambda name: f"{name} 1.0")
    monkeypatch.setattr(runtime, "_tesseract_languages", lambda: ["eng", "por"])
    monkeypatch.setattr(runtime, "_package_versions", lambda: {"faiss-cpu": "1.0"})
    monkeypatch.setattr(runtime, "_import_availability", lambda: {"faiss": True})

    first = runtime.build_runtime_report()
    second = runtime.build_runtime_report()

    assert first["status"] == "ready"
    assert first["quality_fingerprint"] == second["quality_fingerprint"]
    assert all(first["writable"].values())
    assert len(first["quality_fingerprint"]) == 64


def test_readiness_endpoint_fails_closed(monkeypatch) -> None:
    from llm import main

    monkeypatch.setattr(main, "build_runtime_report", lambda **_kwargs: {"status": "not_ready", "checks": {}})

    response = main.readiness_check()

    assert response.status_code == 503
