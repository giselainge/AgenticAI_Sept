import argparse
import csv
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from invoice_parser.ocr_config import (
    OCR_DPI,
    OCR_IMAGE_MAX_PIXELS,
    OCR_IMAGE_MAX_SCALE,
    OCR_IMAGE_MIN_DIMENSION,
    OCR_LANGUAGES,
    OCR_TIMEOUT_SECONDS,
)
from invoice_parser.paths import first_existing_data_dir, project_data_root
from invoice_parser.text_utils import fold_text

import pytesseract


# ============================================================
# LAYER 6 - INDEPENDENT CONDITIONAL OCR QUALITY PIPELINE
# ============================================================
#
# This layer is intentionally independent from the earlier project layers.
# It does not import layer4/layer5 and does not read layer5 outputs.
#
# Role:
# - prepare the best possible OCR text for downstream processing
# - do not extract final invoice fields here
# - use only local tools: PyMuPDF, OCRmyPDF, Tesseract via OCRmyPDF
#
# Flow:
# input file
# -> direct PDF text extraction when a PDF already has text
# -> OCR when OCR is required
# -> quality scoring
# -> one canonical PDF + one canonical text file
# ============================================================


SUPPORTED_PDF_EXTENSIONS = {".pdf"}
SUPPORTED_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".bmp"}
SUPPORTED_EXTENSIONS = SUPPORTED_PDF_EXTENSIONS | SUPPORTED_IMAGE_EXTENSIONS

DEFAULT_OCR_THRESHOLD = 0.95
MANUAL_REVIEW_THRESHOLD = 0.50
MIN_TEXT_CHARS = 80

INVOICE_KEYWORDS = [
    "fatura",
    "factura",
    "invoice",
    "recibo",
    "documento",
    "pagamento",
    "total",
    "valor",
    "iva",
    "contribuinte",
    "nif",
    "cliente",
    "consumo",
    "eletricidade",
    "electricidade",
    "agua",
    "gas",
    "telecom",
]

DATE_RE = re.compile(
    r"\b(?:\d{2}[/-]\d{2}[/-]\d{4}|\d{4}-\d{2}-\d{2})\b"
)
MONEY_RE = re.compile(
    r"(?i)(?:\b\d{1,5}(?:[.,]\d{2})\s*(?:eur|euro|euros|tl|\u20ac)\b|"
    r"(?:eur|tl|\u20ac)\s*\d{1,5}(?:[.,]\d{2})\b)"
)
VAT_RE = re.compile(r"\b(?:PT\s*)?\d{9}\b", re.IGNORECASE)
CONSUMPTION_UNIT_RE = re.compile(
    r"(?i)\b(?:\d+(?:[.,]\d+)?\s*)?(?:kwh|kw h|m3|m\^3|m\u00b3|gb|minutos|minutes|min|sms)\b"
)


@dataclass
class OcrPassResult:
    used: bool = False
    method: str | None = None
    searchable_pdf: str | None = None
    raw_text_file: str | None = None
    cleaned_text_file: str | None = None
    diagnostics_file: str | None = None
    raw_text: str = ""
    cleaned_text: str = ""
    quality: dict[str, Any] | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class Layer6Result:
    source_file: str
    file_type: str
    extraction_method: str | None = None
    selected_text: str = ""
    selected_text_file: str | None = None
    raw_text_file: str | None = None
    cleaned_text_file: str | None = None
    diagnostics_file: str | None = None
    ocr_comparison_file: str | None = None
    searchable_pdf: str | None = None
    baseline: OcrPassResult = field(default_factory=OcrPassResult)
    enhanced: OcrPassResult = field(default_factory=OcrPassResult)
    llm_second_pass: OcrPassResult = field(default_factory=OcrPassResult)
    quality_score: float = 0.0
    requires_ocr: bool = True
    requires_manual_review: bool = True
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


DATA_ROOT = project_data_root()
RAW_DIR = first_existing_data_dir(DATA_ROOT, ["data_raw", "data__raw", "raw"], "data_raw")
PDF_DIR = first_existing_data_dir(DATA_ROOT, ["data_pdf", "pdf", "processed"], "data_pdf")
PROCESSED_DIR = first_existing_data_dir(DATA_ROOT, ["data_processed", "processed"], "data_processed")
SEARCHABLE_PDF_DIR = PDF_DIR
EXTRACTED_TEXT_DIR = first_existing_data_dir(DATA_ROOT, ["data_txt", "extracted_text"], "data_txt")
REPORTS_DIR = PROCESSED_DIR / "reports"


def configure_local_ocr_environment() -> None:
    """Make freshly installed local OCR tools visible to this Python process."""
    project_tessdata = PROJECT_ROOT / "runtime" / "tessdata"
    if (project_tessdata / "por.traineddata").exists() and not os.environ.get("TESSDATA_PREFIX"):
        os.environ["TESSDATA_PREFIX"] = str(project_tessdata)

    program_files = Path(os.environ.get("ProgramFiles", "C:/Program Files"))
    candidate_path_dirs = [
        Path.home() / "AppData" / "Local" / "Programs" / "Tesseract-OCR",
        Path.home() / "Tesseract-OCR",
        program_files / "Tesseract-OCR",
    ]
    candidate_path_dirs.extend(path / "bin" for path in program_files.glob("qpdf*"))
    existing_dirs = [str(path) for path in candidate_path_dirs if path.exists()]
    current_paths = [part for part in (os.environ.get("PATH") or "").split(os.pathsep) if part]

    for directory in existing_dirs:
        if directory not in current_paths:
            current_paths.append(directory)

    if existing_dirs:
        os.environ["PATH"] = os.pathsep.join(current_paths)

    user_tessdata = Path.home() / "AppData" / "Roaming" / "Tesseract-OCR" / "tessdata"
    if user_tessdata.exists() and not os.environ.get("TESSDATA_PREFIX"):
        os.environ["TESSDATA_PREFIX"] = str(user_tessdata)

    tesseract_cmd = os.environ.get("TESSERACT_CMD")
    candidate_executables = [
        Path(tesseract_cmd) if tesseract_cmd else None,
        Path.home() / "AppData" / "Local" / "Programs" / "Tesseract-OCR" / "tesseract.exe",
        Path.home() / "Tesseract-OCR" / "tesseract.exe",
        program_files / "Tesseract-OCR" / "tesseract.exe",
    ]
    for candidate in candidate_executables:
        if candidate and candidate.exists():
            pytesseract.pytesseract.tesseract_cmd = str(candidate)
            break


def ensure_output_dirs(
    text_output_dir: Path = EXTRACTED_TEXT_DIR,
    pdf_output_dir: Path = PDF_DIR,
    processed_output_dir: Path = PROCESSED_DIR,
    reports_output_dir: Path = REPORTS_DIR,
) -> dict[str, Path]:
    for directory in [text_output_dir, pdf_output_dir, processed_output_dir, reports_output_dir]:
        directory.mkdir(parents=True, exist_ok=True)

    return {
        "text": text_output_dir,
        "pdf": pdf_output_dir,
        "processed": processed_output_dir,
        "searchable_pdf": pdf_output_dir,
        "reports": reports_output_dir,
    }


def import_fitz():
    try:
        import fitz  # type: ignore
    except ImportError as exc:
        raise RuntimeError("PyMuPDF/fitz is not installed.") from exc

    return fitz


def clean_text(text: str) -> str:
    text = (text or "").replace("\r", "\n").replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def write_text_file(path: Path, text: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text or "", encoding="utf-8")
    return str(path)


def extract_pdf_text_pages(pdf_path: Path) -> tuple[str, list[str]]:
    try:
        fitz = import_fitz()
    except RuntimeError as fitz_error:
        if shutil.which("pdftotext") is None:
            raise fitz_error
        completed = subprocess.run(
            ["pdftotext", "-layout", str(pdf_path), "-"],
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or "").strip()
            raise RuntimeError(f"pdftotext could not extract PDF text: {detail}") from fitz_error
        page_texts = completed.stdout.split("\f")
        return "\n\n".join(page_texts), page_texts

    page_texts: list[str] = []
    try:
        with fitz.open(pdf_path) as doc:
            for page in doc:
                page_texts.append(page.get_text("text") or "")
    except Exception as exc:
        raise RuntimeError(f"PyMuPDF could not open/extract PDF text: {exc}") from exc

    return "\n\n".join(page_texts), page_texts


def has_pdf_text_layer(pdf_path: str | Path, min_chars: int = 30) -> bool:
    """Check direct PDF text first so text-native PDFs skip unnecessary OCR."""
    try:
        text, _ = extract_pdf_text_pages(Path(pdf_path))
    except RuntimeError:
        return False

    return len(clean_text(text)) >= min_chars


def image_to_pdf(image_path: Path, output_pdf: Path) -> Path:
    """OCRmyPDF expects PDFs; PyMuPDF can locally wrap most images as PDFs."""
    output_pdf.parent.mkdir(parents=True, exist_ok=True)
    if output_pdf.exists():
        output_pdf.unlink()

    try:
        fitz = import_fitz()
        image_doc = fitz.open(image_path)
        pdf_bytes = image_doc.convert_to_pdf()
        image_doc.close()
        pdf_doc = fitz.open("pdf", pdf_bytes)
        pdf_doc.save(output_pdf)
        pdf_doc.close()
    except Exception as exc:
        try:
            from PIL import Image

            with Image.open(image_path) as image:
                if image.mode in {"RGBA", "LA", "P"}:
                    image = image.convert("RGB")
                image.save(output_pdf, "PDF", resolution=300.0)
        except Exception as fallback_exc:
            raise RuntimeError(
                f"Could not convert image to PDF with PyMuPDF ({exc}) or Pillow ({fallback_exc})."
            ) from fallback_exc

    return output_pdf


def materialize_source_pdf(source_file: Path, pdf_output_dir: Path) -> tuple[Path | None, list[str]]:
    errors: list[str] = []
    suffix = source_file.suffix.lower()

    output_pdf = pdf_output_dir / f"{source_file.stem}.pdf"

    # Cache
    if output_pdf.exists():
        pdf_mtime = output_pdf.stat().st_mtime
        source_mtime = source_file.stat().st_mtime

        if pdf_mtime >= source_mtime:
            return output_pdf, errors


    if suffix in SUPPORTED_PDF_EXTENSIONS:
        try:
            if source_file.resolve() != output_pdf.resolve():
                output_pdf.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_file, output_pdf)
            return output_pdf, errors
        except Exception as exc:
            errors.append(f"Could not copy PDF to {output_pdf}: {exc}")
            return None, errors

    if suffix in SUPPORTED_IMAGE_EXTENSIONS:
        try:
            return image_to_pdf(source_file, output_pdf), errors
        except RuntimeError as exc:
            errors.append(str(exc))
            return None, errors

    errors.append(f"Unsupported file type: {suffix}")
    return None, errors


def score_ocr_quality(text: str, page_texts: list[str] | None = None) -> dict[str, Any]:
    """Deterministic invoice-specific OCR quality score in the range 0..1."""
    cleaned = clean_text(text)
    folded_text = fold_text(cleaned)
    words = re.findall(r"\b[\w\-]+\b", folded_text)

    character_count = len(cleaned)
    word_count = len(words)
    date_count = len(DATE_RE.findall(cleaned))
    money_count = len(MONEY_RE.findall(cleaned))
    vat_number_count = len(VAT_RE.findall(cleaned))
    invoice_keyword_hits = [word for word in INVOICE_KEYWORDS if word in folded_text]
    consumption_unit_count = len(CONSUMPTION_UNIT_RE.findall(cleaned))

    replacement_count = cleaned.count("\ufffd")
    symbol_count = len(re.findall(r"[^A-Za-z0-9À-ÿ\s.,:/()\-€]", cleaned))
    short_tokens = [word for word in words if len(word) == 1]
    token_count = max(1, len(words))

    replacement_ratio = replacement_count / max(1, character_count)
    symbol_ratio = symbol_count / max(1, character_count)
    short_token_ratio = len(short_tokens) / token_count
    noise_score = min(
        1.0,
        replacement_ratio * 8.0 + symbol_ratio * 3.0 + max(0.0, short_token_ratio - 0.22),
    )

    score = 0.0
    score += min(0.20, character_count / 4000 * 0.20)
    score += min(0.20, len(invoice_keyword_hits) / 8 * 0.20)
    score += min(0.15, money_count / 4 * 0.15)
    score += min(0.15, date_count / 3 * 0.15)
    score += min(0.15, vat_number_count / 2 * 0.15)
    score += min(0.10, consumption_unit_count / 2 * 0.10)
    score -= min(0.20, noise_score * 0.20)
    score = round(max(0.0, min(1.0, score)), 3)

    warnings: list[str] = []
    if character_count < MIN_TEXT_CHARS:
        warnings.append("Very low character count.")
    if not date_count:
        warnings.append("No date patterns detected.")
    if not money_count:
        warnings.append("No money values detected.")
    if not invoice_keyword_hits:
        warnings.append("No invoice keywords detected.")
    if noise_score > 0.35:
        warnings.append("High OCR noise detected.")

    return {
        "score": score,
        "character_count": character_count,
        "word_count": word_count,
        "date_count": date_count,
        "money_count": money_count,
        "vat_number_count": vat_number_count,
        "invoice_keyword_count": len(invoice_keyword_hits),
        "consumption_unit_count": consumption_unit_count,
        "noise_score": round(noise_score, 3),
        "warnings": warnings,
        "signals": {
            "invoice_keywords": invoice_keyword_hits,
            "page_count": len(page_texts or []),
            "replacement_character_count": replacement_count,
            "isolated_short_token_ratio": round(short_token_ratio, 3),
            "symbol_ratio": round(symbol_ratio, 3),
        },
    }


def should_run_enhanced_ocr(quality: dict[str, Any], threshold: float = DEFAULT_OCR_THRESHOLD) -> bool:
    if not quality:
        return True
    if quality.get("score", 0.0) < threshold:
        return True
    if quality.get("character_count", 0) < MIN_TEXT_CHARS:
        return True
    if quality.get("money_count", 0) == 0:
        return True
    if quality.get("date_count", 0) == 0:
        return True
    if quality.get("invoice_keyword_count", 0) == 0:
        return True
    return False


def tool_missing_warnings() -> list[str]:
    warnings = []
    if shutil.which("ocrmypdf") is None:
        if shutil.which("pdftoppm") is not None:
            warnings.append("OCRmyPDF is unavailable; using the local Poppler + Tesseract PDF fallback.")
        else:
            warnings.append("OCRmyPDF and pdftoppm are not installed or not on PATH.")
    if shutil.which("tesseract") is None:
        warnings.append("Tesseract is not installed or not on PATH.")
    return warnings


def run_ocrmypdf(input_pdf: Path, output_pdf: Path, enhanced: bool = False) -> tuple[bool, list[str]]:
    if shutil.which("ocrmypdf") is None:
        return False, ["OCRmyPDF is not installed or not on PATH."]

    output_pdf.parent.mkdir(parents=True, exist_ok=True)
    command = [
        "ocrmypdf",
        "--language",
        "+".join(OCR_LANGUAGES),
        "--deskew",
        "--rotate-pages",
    ]

    if enhanced:
        if shutil.which("unpaper") is not None:
            command.extend(["--clean", "--clean-final"])
        command.extend(["--oversample", str(OCR_DPI)])

    command.extend(["--force-ocr", str(input_pdf), str(output_pdf)])

    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=OCR_TIMEOUT_SECONDS,
        )
    except FileNotFoundError:
        return False, ["OCRmyPDF is not installed or not on PATH."]
    except subprocess.TimeoutExpired:
        return False, ["OCRmyPDF command timed out."]

    errors = []
    if completed.returncode != 0:
        stderr = (completed.stderr or "").strip()
        errors.append(f"OCRmyPDF failed with exit code {completed.returncode}: {stderr}")
    if not output_pdf.exists():
        errors.append("OCRmyPDF did not create the expected searchable PDF.")

    return not errors, errors


def diagnostics_text(label: str, source_file: Path, searchable_pdf: Path | None, quality: dict[str, Any]) -> str:
    warnings = quality.get("warnings", [])
    signals = quality.get("signals", {})
    lines = [
        f"SOURCE FILE: {source_file}",
        f"OCR RESULT: {label}",
        f"SEARCHABLE PDF: {searchable_pdf or ''}",
        "",
        f"score: {quality.get('score', 0.0)}",
        f"characters: {quality.get('character_count', 0)}",
        f"words: {quality.get('word_count', 0)}",
        f"dates found: {quality.get('date_count', 0)}",
        f"money values found: {quality.get('money_count', 0)}",
        f"VAT/NIF numbers found: {quality.get('vat_number_count', 0)}",
        f"invoice keywords found: {quality.get('invoice_keyword_count', 0)}",
        f"consumption units found: {quality.get('consumption_unit_count', 0)}",
        f"noise score: {quality.get('noise_score', 0.0)}",
        "",
        "signals:",
        json.dumps(signals, indent=2, ensure_ascii=False),
        "",
        "warnings:",
    ]
    lines.extend(f"- {warning}" for warning in warnings)
    return "\n".join(lines).strip() + "\n"


def save_pass_text_files(
    label: str,
    source_file: Path,
    raw_text: str,
    cleaned_text: str,
    quality: dict[str, Any],
    text_output_dir: Path,
    searchable_pdf: Path | None,
) -> tuple[str, str, str]:
    stem = source_file.stem
    raw_path = text_output_dir / f"{stem}_{label}_raw.txt"
    cleaned_path = text_output_dir / f"{stem}_{label}_cleaned.txt"
    diagnostics_path = text_output_dir / f"{stem}_{label}_diagnostics.txt"

    write_text_file(raw_path, raw_text)
    write_text_file(cleaned_path, cleaned_text)
    write_text_file(diagnostics_path, diagnostics_text(label, source_file, searchable_pdf, quality))

    return str(raw_path), str(cleaned_path), str(diagnostics_path)


def build_ocr_pass_result(
    label: str,
    source_file: Path,
    searchable_pdf: Path | None,
    raw_text: str,
    page_texts: list[str],
    text_output_dir: Path,
    errors: list[str] | None = None,
) -> OcrPassResult:
    cleaned = clean_text(raw_text)
    quality = score_ocr_quality(cleaned, page_texts)

    return OcrPassResult(
        used=True,
        method=label,
        searchable_pdf=str(searchable_pdf) if searchable_pdf else None,
        raw_text=raw_text,
        cleaned_text=cleaned,
        quality=quality,
        errors=errors or [],
        warnings=quality.get("warnings", []),
    )


def run_tesseract_image_pass(
    source_file: Path,
    searchable_pdf: Path,
    text_output_dir: Path,
    *,
    enhanced: bool,
) -> OcrPassResult:
    """Run one local image OCR pass with bounded enhancement for small scans."""
    from PIL import Image, ImageFilter, ImageOps

    available_languages = set(pytesseract.get_languages(config=""))
    languages = "+".join(language for language in OCR_LANGUAGES if language in available_languages)
    label = "tesseract_image_enhanced" if enhanced else "tesseract_image_baseline"
    with Image.open(source_file) as opened:
        image = ImageOps.exif_transpose(opened).convert("RGB")
        if enhanced:
            minimum_dimension = max(1, min(image.width, image.height))
            scale = min(OCR_IMAGE_MAX_SCALE, OCR_IMAGE_MIN_DIMENSION / minimum_dimension)
            max_scale_for_pixels = (OCR_IMAGE_MAX_PIXELS / max(1, image.width * image.height)) ** 0.5
            scale = max(1.0, min(scale, max_scale_for_pixels))
            if scale > 1.05:
                image = image.resize(
                    (round(image.width * scale), round(image.height * scale)),
                    Image.Resampling.LANCZOS,
                )
            image = ImageOps.autocontrast(ImageOps.grayscale(image), cutoff=1)
            image = image.filter(ImageFilter.UnsharpMask(radius=2, percent=180, threshold=3))
        raw_text = pytesseract.image_to_string(
            image,
            lang=languages or None,
            config=f"--oem 1 --psm 3 --dpi {OCR_DPI} -c preserve_interword_spaces=1",
        )

    result = build_ocr_pass_result(
        label,
        source_file,
        searchable_pdf,
        raw_text,
        [raw_text],
        text_output_dir,
    )
    raw_file, cleaned_file, diagnostics_file = save_pass_text_files(
        label,
        source_file,
        result.raw_text,
        result.cleaned_text,
        result.quality or {},
        text_output_dir,
        searchable_pdf,
    )
    result.raw_text_file = raw_file
    result.cleaned_text_file = cleaned_file
    result.diagnostics_file = diagnostics_file
    return result


def render_pdf_pages(input_pdf: Path, work_dir: Path, *, label: str) -> list[Path]:
    """Render a scanned PDF locally when OCRmyPDF is unavailable."""
    renderer = shutil.which("pdftoppm")
    if renderer is None:
        raise RuntimeError("Neither OCRmyPDF nor the pdftoppm PDF renderer is available.")
    work_dir.mkdir(parents=True, exist_ok=True)
    prefix = work_dir / f"{input_pdf.stem}_{label}_page"
    for stale in work_dir.glob(f"{prefix.name}-*.png"):
        stale.unlink()
    try:
        completed = subprocess.run(
            [renderer, "-png", "-r", str(OCR_DPI), str(input_pdf), str(prefix)],
            check=False,
            capture_output=True,
            text=True,
            timeout=OCR_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("PDF page rendering timed out.") from None
    if completed.returncode != 0:
        raise RuntimeError(f"pdftoppm failed with exit code {completed.returncode}.")

    def page_number(path: Path) -> int:
        match = re.search(r"-(\d+)\.png$", path.name)
        return int(match.group(1)) if match else 0

    pages = sorted(work_dir.glob(f"{prefix.name}-*.png"), key=page_number)
    if not pages:
        raise RuntimeError("pdftoppm did not render any PDF pages.")
    return pages


def run_tesseract_pdf_pass(
    source_file: Path,
    input_pdf: Path,
    text_output_dir: Path,
    work_dir: Path,
    *,
    enhanced: bool,
) -> OcrPassResult:
    """OCR all rendered PDF pages with the same bounded image treatment used locally."""
    from PIL import Image, ImageFilter, ImageOps

    label = "tesseract_pdf_enhanced" if enhanced else "tesseract_pdf_baseline"
    pages = render_pdf_pages(input_pdf, work_dir, label=label)
    available_languages = set(pytesseract.get_languages(config=""))
    languages = "+".join(language for language in OCR_LANGUAGES if language in available_languages)
    page_texts: list[str] = []
    for page_path in pages:
        with Image.open(page_path) as opened:
            image = ImageOps.exif_transpose(opened).convert("RGB")
            if enhanced:
                minimum_dimension = max(1, min(image.width, image.height))
                scale = min(OCR_IMAGE_MAX_SCALE, OCR_IMAGE_MIN_DIMENSION / minimum_dimension)
                max_scale_for_pixels = (OCR_IMAGE_MAX_PIXELS / max(1, image.width * image.height)) ** 0.5
                scale = max(1.0, min(scale, max_scale_for_pixels))
                if scale > 1.05:
                    image = image.resize(
                        (round(image.width * scale), round(image.height * scale)),
                        Image.Resampling.LANCZOS,
                    )
                image = ImageOps.autocontrast(ImageOps.grayscale(image), cutoff=1)
                image = image.filter(ImageFilter.UnsharpMask(radius=2, percent=180, threshold=3))
            page_texts.append(
                pytesseract.image_to_string(
                    image,
                    lang=languages or None,
                    config=f"--oem 1 --psm 3 --dpi {OCR_DPI} -c preserve_interword_spaces=1",
                )
            )
    raw_text = "\n\n".join(page_texts)
    result = build_ocr_pass_result(
        label,
        source_file,
        input_pdf,
        raw_text,
        page_texts,
        text_output_dir,
    )
    raw_file, cleaned_file, diagnostics_file = save_pass_text_files(
        label,
        source_file,
        result.raw_text,
        result.cleaned_text,
        result.quality or {},
        text_output_dir,
        input_pdf,
    )
    result.raw_text_file = raw_file
    result.cleaned_text_file = cleaned_file
    result.diagnostics_file = diagnostics_file
    return result


def compare_ocr_results(baseline: dict[str, Any], enhanced: dict[str, Any]) -> dict[str, Any]:
    baseline_quality = baseline.get("quality") or {}
    enhanced_quality = enhanced.get("quality") or {}
    baseline_score = float(baseline_quality.get("score", 0.0))
    enhanced_score = float(enhanced_quality.get("score", 0.0))

    selected = "baseline_ocr"
    reason_parts: list[str] = []

    if enhanced_score > baseline_score + 0.03:
        selected = "enhanced_ocr"
        reason_parts.append("Enhanced OCR had a higher quality score.")
    elif baseline_score > enhanced_score + 0.03:
        reason_parts.append("Baseline OCR had a higher quality score.")
    else:
        tie_breakers = [
            "money_count",
            "date_count",
            "vat_number_count",
            "invoice_keyword_count",
            "consumption_unit_count",
        ]
        baseline_points = 0
        enhanced_points = 0
        for key in tie_breakers:
            if enhanced_quality.get(key, 0) > baseline_quality.get(key, 0):
                enhanced_points += 1
            elif baseline_quality.get(key, 0) > enhanced_quality.get(key, 0):
                baseline_points += 1

        if enhanced_quality.get("noise_score", 1.0) < baseline_quality.get("noise_score", 1.0):
            enhanced_points += 1
        elif baseline_quality.get("noise_score", 1.0) < enhanced_quality.get("noise_score", 1.0):
            baseline_points += 1

        if enhanced_points > baseline_points:
            selected = "enhanced_ocr"
            reason_parts.append("Scores were close, but enhanced OCR had better invoice signals.")
        else:
            reason_parts.append("Scores were close, and baseline OCR had equal or better invoice signals.")

    if not reason_parts:
        reason_parts.append("Baseline OCR was selected by default.")

    return {
        "baseline_score": baseline_score,
        "enhanced_score": enhanced_score,
        "selected_result": selected,
        "reason": " ".join(reason_parts),
        "baseline_character_count": baseline_quality.get("character_count", 0),
        "enhanced_character_count": enhanced_quality.get("character_count", 0),
        "baseline_date_count": baseline_quality.get("date_count", 0),
        "enhanced_date_count": enhanced_quality.get("date_count", 0),
        "baseline_money_count": baseline_quality.get("money_count", 0),
        "enhanced_money_count": enhanced_quality.get("money_count", 0),
        "baseline_vat_number_count": baseline_quality.get("vat_number_count", 0),
        "enhanced_vat_number_count": enhanced_quality.get("vat_number_count", 0),
        "baseline_warnings": baseline_quality.get("warnings", []),
        "enhanced_warnings": enhanced_quality.get("warnings", []),
    }


def comparison_text(source_file: Path, baseline: OcrPassResult, enhanced: OcrPassResult, comparison: dict[str, Any]) -> str:
    def block(title: str, item: OcrPassResult) -> list[str]:
        quality = item.quality or {}
        lines = [
            f"{title}:",
            f"- searchable_pdf: {item.searchable_pdf or ''}",
            f"- score: {quality.get('score', 0.0)}",
            f"- characters: {quality.get('character_count', 0)}",
            f"- words: {quality.get('word_count', 0)}",
            f"- dates found: {quality.get('date_count', 0)}",
            f"- money values found: {quality.get('money_count', 0)}",
            f"- VAT/NIF numbers found: {quality.get('vat_number_count', 0)}",
            f"- consumption units found: {quality.get('consumption_unit_count', 0)}",
            "- warnings:",
        ]
        lines.extend(f"  - {warning}" for warning in quality.get("warnings", []))
        return lines

    lines = [f"SOURCE FILE: {source_file}", ""]
    lines.extend(block("BASELINE OCR", baseline))
    lines.append("")
    lines.extend(block("ENHANCED OCR", enhanced))
    lines.extend(
        [
            "",
            "SELECTED RESULT:",
            comparison.get("selected_result", ""),
            "",
            "REASON:",
            comparison.get("reason", ""),
        ]
    )
    return "\n".join(lines).strip() + "\n"


def selected_text_output(source_file: Path, selected_result: str, quality: dict[str, Any], method: str, text: str) -> str:
    return (
        "==============================\n"
        f"SOURCE FILE: {source_file}\n"
        f"SELECTED OCR RESULT: {selected_result}\n"
        f"QUALITY SCORE: {quality.get('score', 0.0)}\n"
        f"EXTRACTION METHOD: {method}\n"
        "==============================\n\n"
        f"{text.strip()}\n"
    )


def cached_text_result(
    source: Path,
    selected_path: Path,
    pdf_path: Path,
    ocr_quality_threshold: float = DEFAULT_OCR_THRESHOLD,
    debug: bool = False,
) -> Layer6Result:
    cached_text = selected_path.read_text(encoding="utf-8", errors="replace")
    quality = score_ocr_quality(cached_text)
    result = Layer6Result(
        source_file=str(source),
        file_type=source.suffix.lower().lstrip("."),
        extraction_method="cached_text_file",
        selected_text=cached_text,
        selected_text_file=str(selected_path),
        searchable_pdf=str(pdf_path),
        quality_score=float(quality.get("score", 0.0)),
        requires_ocr=False,
    )
    result.warnings.append("Skipped OCR because the final text file already exists.")
    final_text, final_quality, final_method = maybe_run_llm_second_pass(
        result,
        source,
        pdf_path if pdf_path.exists() else None,
        selected_path,
        cached_text,
        quality,
        "cached_text_file",
        ocr_quality_threshold,
        selected_path.parent,
        debug,
    )
    result.extraction_method = final_method
    result.selected_text = final_text
    result.quality_score = float(final_quality.get("score", 0.0))
    update_review_flags(result, final_quality)
    return result


def update_review_flags(result: Layer6Result, quality: dict[str, Any]) -> None:
    warnings = list(result.warnings)

    if result.errors:
        warnings.append("One or more extraction errors occurred.")
    if not result.selected_text.strip():
        warnings.append("No usable text was extracted.")
    if quality.get("score", 0.0) < MANUAL_REVIEW_THRESHOLD:
        warnings.append("Selected OCR quality score is below manual-review threshold.")
    if quality.get("character_count", 0) < MIN_TEXT_CHARS:
        warnings.append("Selected text is too short.")
    if quality.get("invoice_keyword_count", 0) == 0:
        warnings.append("No invoice keywords found in selected text.")
    if quality.get("money_count", 0) == 0:
        warnings.append("No money values found in selected text.")
    if quality.get("date_count", 0) == 0:
        warnings.append("No dates found in selected text.")

    result.warnings = sorted(set(warnings))
    result.requires_manual_review = bool(result.warnings)


def should_run_llm_second_pass(
    quality: dict[str, Any] | None,
    threshold: float = DEFAULT_OCR_THRESHOLD,
) -> bool:
    if not quality:
        return True
    try:
        score = float(quality.get("score", 0.0))
    except (TypeError, ValueError):
        score = 0.0
    return score < threshold


def maybe_run_llm_second_pass(
    result: Layer6Result,
    source_file: Path,
    source_pdf: Path | None,
    selected_path: Path,
    selected_text: str,
    selected_quality: dict[str, Any],
    selected_method: str,
    threshold: float,
    text_output_dir: Path,
    debug: bool = False,
) -> tuple[str, dict[str, Any], str]:
    if not should_run_llm_second_pass(selected_quality, threshold):
        return selected_text, selected_quality, selected_method

    original_score = float(selected_quality.get("score", 0.0))
    result.warnings.append(
        f"OCR quality score {original_score:.3f} is below {threshold:.2f}; LLM second pass requested."
    )

    try:
        from scripts.second_pass_llm import run_llm_second_pass
    except Exception as exc:
        result.warnings.append(f"LLM second pass skipped because the llm module could not load: {exc}")
        return selected_text, selected_quality, selected_method

    llm_result = run_llm_second_pass(
        source_file=source_file,
        pdf_file=source_pdf,
        ocr_text=selected_text,
        ocr_quality=selected_quality,
    )

    if not llm_result.used:
        result.llm_second_pass = OcrPassResult(
            used=False,
            method="llm_second_pass",
            errors=llm_result.errors,
            warnings=llm_result.warnings,
        )
        result.warnings.extend(llm_result.warnings)
        result.warnings.extend(llm_result.errors)
        return selected_text, selected_quality, selected_method

    if getattr(llm_result, "parsed", None):
        result.llm_second_pass = OcrPassResult(
            used=True,
            method=f"llm_second_pass:{llm_result.model or 'gemini'}",
            searchable_pdf=str(source_pdf) if source_pdf else None,
            raw_text_file=getattr(llm_result, "raw_response_path", None),
            cleaned_text_file=getattr(llm_result, "normalized_output_path", None),
            diagnostics_file=getattr(llm_result, "normalized_output_path", None),
            raw_text=getattr(llm_result, "raw_response", ""),
            cleaned_text=llm_result.text,
            quality=selected_quality,
            errors=llm_result.errors,
            warnings=llm_result.warnings,
        )
        result.warnings.extend(llm_result.warnings)
        result.warnings.append("LLM structured extraction was saved separately; first-pass OCR text was preserved.")
        return selected_text, selected_quality, selected_method

    llm_cleaned = clean_text(llm_result.text)
    llm_quality = score_ocr_quality(llm_cleaned)
    raw_file, cleaned_file, diagnostics_file = save_pass_text_files(
        "llm_second_pass",
        source_file,
        llm_result.text,
        llm_cleaned,
        llm_quality,
        text_output_dir,
        source_pdf,
    )
    llm_method = f"llm_second_pass:{llm_result.model or 'gemini'}"
    result.llm_second_pass = OcrPassResult(
        used=True,
        method=llm_method,
        searchable_pdf=str(source_pdf) if source_pdf else None,
        raw_text_file=raw_file,
        cleaned_text_file=cleaned_file,
        diagnostics_file=diagnostics_file,
        raw_text=llm_result.text,
        cleaned_text=llm_cleaned,
        quality=llm_quality,
        errors=llm_result.errors,
        warnings=llm_result.warnings,
    )
    result.warnings.extend(llm_result.warnings)

    llm_score = float(llm_quality.get("score", 0.0))
    if llm_score >= original_score:
        write_text_file(
            selected_path,
            selected_text_output(source_file, "llm_second_pass", llm_quality, llm_method, llm_cleaned),
        )
        result.warnings.append(
            f"LLM second pass selected over {selected_method} ({llm_score:.3f} >= {original_score:.3f})."
        )
        if debug:
            print(f"{source_file.name}: llm_second_pass selected, score={llm_score}")
        return llm_cleaned, llm_quality, llm_method

    result.warnings.append(
        f"LLM second pass ran, but {selected_method} was kept ({original_score:.3f} > {llm_score:.3f})."
    )
    if debug:
        print(f"{source_file.name}: llm_second_pass kept original, score={llm_score}")
    return selected_text, selected_quality, selected_method


def process_file(
    source_file: str | Path,
    baseline_only: bool = False,
    force_enhanced: bool = False,
    refresh: bool = False,
    ocr_quality_threshold: float = DEFAULT_OCR_THRESHOLD,
    text_output_dir: str | Path = EXTRACTED_TEXT_DIR,
    pdf_output_dir: str | Path = PDF_DIR,
    processed_output_dir: str | Path = PROCESSED_DIR,
    reports_output_dir: str | Path = REPORTS_DIR,
    debug: bool = False,
) -> Layer6Result:
    configure_local_ocr_environment()
    source = Path(source_file)
    output_dirs = ensure_output_dirs(
        Path(text_output_dir),
        Path(pdf_output_dir),
        Path(processed_output_dir),
        Path(reports_output_dir),
    )
    result = Layer6Result(source_file=str(source), file_type=source.suffix.lower().lstrip("."))

    if not source.exists():
        result.errors.append("Input file does not exist.")
        update_review_flags(result, {})
        return result

    if source.suffix.lower() not in SUPPORTED_EXTENSIONS:
        result.errors.append(f"Unsupported file type: {source.suffix.lower()}")
        update_review_flags(result, {})
        return result

    selected_path = output_dirs["text"] / f"{source.stem}.txt"
    if selected_path.exists() and not refresh:
        if debug:
            print(f"{source.name}: skipped existing text file {selected_path.name}")
        return cached_text_result(
            source,
            selected_path,
            output_dirs["pdf"] / f"{source.stem}.pdf",
            ocr_quality_threshold,
            debug,
        )

    input_pdf, prepare_errors = materialize_source_pdf(source, output_dirs["pdf"])
    result.errors.extend(prepare_errors)
    if input_pdf is None:
        update_review_flags(result, {})
        return result

    if has_pdf_text_layer(input_pdf):
        try:
            raw_text, page_texts = extract_pdf_text_pages(input_pdf)
            cleaned = clean_text(raw_text)
            quality = score_ocr_quality(cleaned, page_texts)
            pdf_result = build_ocr_pass_result(
                "pdf_text_layer",
                source,
                input_pdf,
                raw_text,
                page_texts,
                output_dirs["text"],
            )
            selected_path = output_dirs["text"] / f"{input_pdf.stem}.txt"
            write_text_file(
                selected_path,
                selected_text_output(source, "pdf_text_layer", quality, "pdf_text_layer", cleaned),
            )
            final_text, final_quality, final_method = maybe_run_llm_second_pass(
                result,
                source,
                input_pdf,
                selected_path,
                cleaned,
                quality,
                "pdf_text_layer",
                ocr_quality_threshold,
                output_dirs["text"],
                debug,
            )

            result.extraction_method = final_method
            result.selected_text = final_text
            result.selected_text_file = str(selected_path)
            result.raw_text_file = pdf_result.raw_text_file
            result.cleaned_text_file = pdf_result.cleaned_text_file
            result.diagnostics_file = pdf_result.diagnostics_file
            result.searchable_pdf = str(input_pdf)
            result.quality_score = float(final_quality.get("score", 0.0))
            result.requires_ocr = False
            result.baseline = OcrPassResult(used=False)
            result.enhanced = OcrPassResult(used=False)
            update_review_flags(result, final_quality)
            return result
        except RuntimeError as exc:
            result.errors.append(str(exc))

    result.requires_ocr = True
    result.warnings.extend(tool_missing_warnings())

    # Image invoices always use this path. This keeps preprocessing identical
    # in bare local runs and in the Docker image, where OCRmyPDF is installed.
    if source.suffix.lower() in SUPPORTED_IMAGE_EXTENSIONS:
        try:
            baseline_pass = run_tesseract_image_pass(
                source,
                input_pdf,
                output_dirs["text"],
                enhanced=False,
            )
            result.baseline = baseline_pass
            selected_pass = baseline_pass
            selected_label = baseline_pass.method or "tesseract_image_baseline"

            baseline_quality = baseline_pass.quality or {}
            if not baseline_only and (force_enhanced or should_run_enhanced_ocr(baseline_quality, ocr_quality_threshold)):
                enhanced_pass = run_tesseract_image_pass(
                    source,
                    input_pdf,
                    output_dirs["text"],
                    enhanced=True,
                )
                result.enhanced = enhanced_pass
                comparison = compare_ocr_results(asdict(baseline_pass), asdict(enhanced_pass))
                comparison_path = output_dirs["text"] / f"{source.stem}_ocr_comparison.txt"
                write_text_file(
                    comparison_path,
                    comparison_text(source, baseline_pass, enhanced_pass, comparison),
                )
                result.ocr_comparison_file = str(comparison_path)
                result.warnings.append(comparison["reason"])
                if comparison["selected_result"] == "enhanced_ocr":
                    selected_pass = enhanced_pass
                    selected_label = enhanced_pass.method or "tesseract_image_enhanced"

            quality = selected_pass.quality or {}
            write_text_file(
                selected_path,
                selected_text_output(source, selected_label, quality, selected_label, selected_pass.cleaned_text),
            )
            result.extraction_method = selected_label
            result.selected_text = selected_pass.cleaned_text
            result.selected_text_file = str(selected_path)
            result.searchable_pdf = str(input_pdf)
            result.raw_text_file = selected_pass.raw_text_file
            result.cleaned_text_file = selected_pass.cleaned_text_file
            result.diagnostics_file = selected_pass.diagnostics_file
            result.quality_score = float(quality.get("score", 0.0))
            update_review_flags(result, quality)
            return result
        except Exception as exc:
            result.errors.append(f"Direct Tesseract image OCR failed: {exc}")
            update_review_flags(result, {})
            return result

    work_dir = output_dirs["processed"] / "_ocr_work"
    work_dir.mkdir(parents=True, exist_ok=True)
    ocr_pdf = work_dir / f"{source.stem}.pdf"
    if ocr_pdf.exists():
        ocr_pdf.unlink()

    selected_label = "ocr"
    use_enhanced = force_enhanced and not baseline_only
    ocr_ok, ocr_errors = run_ocrmypdf(input_pdf, ocr_pdf, enhanced=use_enhanced)
    if not ocr_ok:
        try:
            baseline_pass = run_tesseract_pdf_pass(
                source,
                input_pdf,
                output_dirs["text"],
                work_dir / "rendered_pages",
                enhanced=False,
            )
            result.baseline = baseline_pass
            selected_pass = baseline_pass
            selected_label = baseline_pass.method or "tesseract_pdf_baseline"
            baseline_quality = baseline_pass.quality or {}
            if not baseline_only and (
                force_enhanced or should_run_enhanced_ocr(baseline_quality, ocr_quality_threshold)
            ):
                enhanced_pass = run_tesseract_pdf_pass(
                    source,
                    input_pdf,
                    output_dirs["text"],
                    work_dir / "rendered_pages",
                    enhanced=True,
                )
                result.enhanced = enhanced_pass
                comparison = compare_ocr_results(asdict(baseline_pass), asdict(enhanced_pass))
                comparison_path = output_dirs["text"] / f"{source.stem}_ocr_comparison.txt"
                write_text_file(
                    comparison_path,
                    comparison_text(source, baseline_pass, enhanced_pass, comparison),
                )
                result.ocr_comparison_file = str(comparison_path)
                result.warnings.append(comparison["reason"])
                if comparison["selected_result"] == "enhanced_ocr":
                    selected_pass = enhanced_pass
                    selected_label = enhanced_pass.method or "tesseract_pdf_enhanced"
            result.warnings.extend(f"OCRmyPDF fallback reason: {message}" for message in ocr_errors)
            quality = selected_pass.quality or {}
            write_text_file(
                selected_path,
                selected_text_output(source, selected_label, quality, selected_label, selected_pass.cleaned_text),
            )
            result.extraction_method = selected_label
            result.selected_text = selected_pass.cleaned_text
            result.selected_text_file = str(selected_path)
            result.raw_text_file = selected_pass.raw_text_file
            result.cleaned_text_file = selected_pass.cleaned_text_file
            result.diagnostics_file = selected_pass.diagnostics_file
            result.searchable_pdf = str(input_pdf)
            result.quality_score = float(quality.get("score", 0.0))
            update_review_flags(result, quality)
            return result
        except Exception as exc:
            result.baseline.errors.extend(ocr_errors)
            result.errors.extend(ocr_errors)
            result.errors.append(f"PDF Tesseract fallback failed: {exc}")
            result.errors.append("OCR failed completely; no OCR pass produced usable text.")
            update_review_flags(result, {})
            return result

    ocr_pdf.replace(input_pdf)

    try:
        raw_text, page_texts = extract_pdf_text_pages(input_pdf)
        selected_pass = build_ocr_pass_result(
            selected_label,
            source,
            input_pdf,
            raw_text,
            page_texts,
            output_dirs["text"],
        )
        result.baseline = selected_pass
    except RuntimeError as exc:
        result.errors.append(str(exc))
        update_review_flags(result, {})
        return result

    quality = selected_pass.quality or score_ocr_quality(selected_pass.cleaned_text)
    selected_path = output_dirs["text"] / f"{input_pdf.stem}.txt"
    write_text_file(
        selected_path,
        selected_text_output(source, selected_label, quality, selected_pass.method or selected_label, selected_pass.cleaned_text),
    )
    final_text, final_quality, final_method = maybe_run_llm_second_pass(
        result,
        source,
        input_pdf,
        selected_path,
        selected_pass.cleaned_text,
        quality,
        selected_pass.method or selected_label,
        ocr_quality_threshold,
        output_dirs["text"],
        debug,
    )

    result.extraction_method = final_method
    result.selected_text = final_text
    result.selected_text_file = str(selected_path)
    result.raw_text_file = selected_pass.raw_text_file
    result.cleaned_text_file = selected_pass.cleaned_text_file
    result.diagnostics_file = selected_pass.diagnostics_file
    result.searchable_pdf = selected_pass.searchable_pdf
    result.quality_score = float(final_quality.get("score", 0.0))
    update_review_flags(result, final_quality)

    if debug:
        print(f"{source.name}: selected={final_method}, score={result.quality_score}")

    return result


def result_to_public_dict(result: Layer6Result) -> dict[str, Any]:
    data = asdict(result)
    data["baseline"].pop("raw_text", None)
    data["baseline"].pop("cleaned_text", None)
    data["enhanced"].pop("raw_text", None)
    data["enhanced"].pop("cleaned_text", None)
    data["llm_second_pass"].pop("raw_text", None)
    data["llm_second_pass"].pop("cleaned_text", None)
    return data


def process_batch(
    input_dir: str | Path = RAW_DIR,
    baseline_only: bool = False,
    force_enhanced: bool = False,
    refresh: bool = False,
    ocr_quality_threshold: float = DEFAULT_OCR_THRESHOLD,
    text_output_dir: str | Path = EXTRACTED_TEXT_DIR,
    pdf_output_dir: str | Path = PDF_DIR,
    processed_output_dir: str | Path = PROCESSED_DIR,
    reports_output_dir: str | Path = REPORTS_DIR,
    debug: bool = False,
) -> list[Layer6Result]:
    input_path = Path(input_dir)
    if not input_path.exists():
        return [
            Layer6Result(
                source_file=str(input_path),
                file_type="",
                errors=["Input directory does not exist."],
                warnings=["Batch could not start because input directory is missing."],
                requires_manual_review=True,
            )
        ]

    files = sorted(path for path in input_path.iterdir() if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS)
    results = []

    for file_path in files:
        results.append(
            process_file(
                file_path,
                baseline_only=baseline_only,
                force_enhanced=force_enhanced,
                refresh=refresh,
                ocr_quality_threshold=ocr_quality_threshold,
                text_output_dir=text_output_dir,
                pdf_output_dir=pdf_output_dir,
                processed_output_dir=processed_output_dir,
                reports_output_dir=reports_output_dir,
                debug=debug,
            )
        )

    return results


def write_reports(results: list[Layer6Result], reports_output_dir: str | Path = REPORTS_DIR) -> tuple[Path, Path]:
    reports_dir = Path(reports_output_dir)
    reports_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = reports_dir / f"layer6_ocr_results_{timestamp}.json"
    csv_path = reports_dir / f"layer6_ocr_results_{timestamp}.csv"

    public_results = [result_to_public_dict(result) for result in results]
    json_path.write_text(json.dumps(public_results, indent=2, ensure_ascii=False), encoding="utf-8")

    rows = []
    for result in results:
        rows.append(
            {
                "source_file": result.source_file,
                "file_type": result.file_type,
                "extraction_method": result.extraction_method or "",
                "quality_score": result.quality_score,
                "requires_ocr": result.requires_ocr,
                "requires_manual_review": result.requires_manual_review,
                "selected_text_file": result.selected_text_file or "",
                "searchable_pdf": result.searchable_pdf or "",
                "warnings": " | ".join(result.warnings),
                "errors": " | ".join(result.errors),
            }
        )

    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()) if rows else ["source_file"])
        writer.writeheader()
        writer.writerows(rows)

    return json_path, csv_path


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Layer 6 independent conditional OCR pipeline.")
    parser.add_argument("--input", default=str(RAW_DIR), help="Input file or directory. Defaults to data/data_raw.")
    parser.add_argument("--baseline-only", action="store_true", help="Disable enhanced OCR even if baseline quality is weak.")
    parser.add_argument("--force-enhanced", action="store_true", help="Run enhanced OCR whenever OCR is required.")
    parser.add_argument("--refresh", action="store_true", help="Regenerate OCR text instead of using the selected-text cache.")
    parser.add_argument(
        "--ocr-quality-threshold",
        type=float,
        default=DEFAULT_OCR_THRESHOLD,
        help="Run the LLM second pass when selected OCR quality is below this score. Default: 0.95.",
    )
    parser.add_argument("--text-output-dir", default=str(EXTRACTED_TEXT_DIR))
    parser.add_argument("--pdf-output-dir", default=str(PDF_DIR))
    parser.add_argument("--processed-output-dir", default=str(PROCESSED_DIR))
    parser.add_argument("--reports-output-dir", default=str(REPORTS_DIR))
    parser.add_argument("--debug", action="store_true")
    return parser


def main() -> list[Layer6Result]:
    configure_local_ocr_environment()
    args = build_arg_parser().parse_args()
    input_path = Path(args.input)

    if input_path.is_file():
        results = [
            process_file(
                input_path,
                baseline_only=args.baseline_only,
                force_enhanced=args.force_enhanced,
                refresh=args.refresh,
                ocr_quality_threshold=args.ocr_quality_threshold,
                text_output_dir=args.text_output_dir,
                pdf_output_dir=args.pdf_output_dir,
                processed_output_dir=args.processed_output_dir,
                reports_output_dir=args.reports_output_dir,
                debug=args.debug,
            )
        ]
    else:
        results = process_batch(
            input_path,
            baseline_only=args.baseline_only,
            force_enhanced=args.force_enhanced,
            refresh=args.refresh,
            ocr_quality_threshold=args.ocr_quality_threshold,
            text_output_dir=args.text_output_dir,
            pdf_output_dir=args.pdf_output_dir,
            processed_output_dir=args.processed_output_dir,
            reports_output_dir=args.reports_output_dir,
            debug=args.debug,
        )

    json_path, csv_path = write_reports(results, args.reports_output_dir)
    print("=" * 60)
    print("LAYER 6 INDEPENDENT OCR PIPELINE COMPLETE")
    print("=" * 60)
    print(f"Processed files: {len(results)}")
    print(f"JSON report: {json_path}")
    print(f"CSV report: {csv_path}")
    print(f"Manual review: {sum(result.requires_manual_review for result in results)}/{len(results)}")

    return results


if __name__ == "__main__":
    main()
