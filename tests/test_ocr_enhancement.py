from pathlib import Path

from PIL import Image

from scripts import ocr_text_extraction as ocr


def test_small_image_ocr_selects_measured_enhanced_pass_and_refreshes_cache(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "invoice.png"
    Image.new("RGB", (120, 180), "white").save(source)
    text_dir = tmp_path / "text"
    pdf_dir = tmp_path / "pdf"
    processed_dir = tmp_path / "processed"
    reports_dir = tmp_path / "reports"
    text_dir.mkdir()
    (text_dir / "invoice.txt").write_text("stale cached OCR", encoding="utf-8")

    monkeypatch.setattr(ocr, "configure_local_ocr_environment", lambda: None)
    # Image invoices must use the same direct-Tesseract pipeline even when
    # OCRmyPDF is installed in the Docker/AWS runtime.
    monkeypatch.setattr(ocr.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(ocr.pytesseract, "get_languages", lambda config="": ["eng"])

    def fake_image_to_string(image, **_kwargs):
        if image.width > 120:
            return (
                "Fornecedor: Example Energy, S.A.\nFatura: FT 2026/123\n"
                "Data de emissão: 14/09/2026\nNIF: 599999990\n"
                "Consumo de eletricidade 120 kWh\nTotal da Fatura: 12,30 EUR"
            )
        return "Fatura ilegível"

    monkeypatch.setattr(ocr.pytesseract, "image_to_string", fake_image_to_string)

    result = ocr.process_file(
        source,
        refresh=True,
        text_output_dir=text_dir,
        pdf_output_dir=pdf_dir,
        processed_output_dir=processed_dir,
        reports_output_dir=reports_dir,
    )

    assert result.errors == []
    assert result.baseline.used is True
    assert result.enhanced.used is True
    assert result.extraction_method == "tesseract_image_enhanced"
    assert result.quality_score > (result.baseline.quality or {})["score"]
    assert result.ocr_comparison_file and Path(result.ocr_comparison_file).exists()
    assert "stale cached OCR" not in Path(result.selected_text_file or "").read_text(encoding="utf-8")


def test_scanned_pdf_falls_back_to_poppler_and_tesseract_without_ocrmypdf(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "invoice.pdf"
    source.write_bytes(b"%PDF-1.4\n% synthetic scanned invoice\n")
    text_dir = tmp_path / "text"
    pdf_dir = tmp_path / "pdf"
    processed_dir = tmp_path / "processed"
    reports_dir = tmp_path / "reports"

    monkeypatch.setattr(ocr, "configure_local_ocr_environment", lambda: None)
    monkeypatch.setattr(ocr, "materialize_source_pdf", lambda *_: (source, []))
    monkeypatch.setattr(ocr, "has_pdf_text_layer", lambda *_: False)
    monkeypatch.setattr(
        ocr.shutil,
        "which",
        lambda name: None if name == "ocrmypdf" else f"/usr/bin/{name}",
    )
    monkeypatch.setattr(
        ocr,
        "run_ocrmypdf",
        lambda *_args, **_kwargs: (False, ["OCRmyPDF is not installed or not on PATH."]),
    )

    def fake_pdf_pass(*_args, enhanced: bool, **_kwargs):
        text = (
            "Fatura FT 7 Data 14/09/2026 Total 12,30 EUR NIF 599999990"
            if enhanced
            else "Fatura ilegível"
        )
        quality = ocr.score_ocr_quality(text)
        return ocr.OcrPassResult(
            used=True,
            method="tesseract_pdf_enhanced" if enhanced else "tesseract_pdf_baseline",
            searchable_pdf=str(source),
            raw_text=text,
            cleaned_text=text,
            quality=quality,
        )

    monkeypatch.setattr(ocr, "run_tesseract_pdf_pass", fake_pdf_pass)
    result = ocr.process_file(
        source,
        refresh=True,
        text_output_dir=text_dir,
        pdf_output_dir=pdf_dir,
        processed_output_dir=processed_dir,
        reports_output_dir=reports_dir,
    )

    assert result.errors == []
    assert result.baseline.used is True
    assert result.enhanced.used is True
    assert result.extraction_method == "tesseract_pdf_enhanced"
    assert result.selected_text_file and Path(result.selected_text_file).exists()
    assert any("Poppler + Tesseract" in warning for warning in result.warnings)
