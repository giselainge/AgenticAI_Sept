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
