"""Shared OCR settings used by local, Docker, and temporary AWS runtimes."""

from __future__ import annotations

import os


OCR_PROFILE_VERSION = "invoice-ocr-v2"
OCR_LANGUAGES = tuple(
    language.strip()
    for language in os.getenv("OCR_LANGUAGES", "por+eng").split("+")
    if language.strip()
)
OCR_DPI = int(os.getenv("OCR_DPI", "300"))
OCR_TIMEOUT_SECONDS = int(os.getenv("OCR_TIMEOUT_SECONDS", "900"))
OCR_IMAGE_MIN_DIMENSION = int(os.getenv("OCR_IMAGE_MIN_DIMENSION", "1800"))
OCR_IMAGE_MAX_PIXELS = int(os.getenv("OCR_IMAGE_MAX_PIXELS", "24000000"))
OCR_IMAGE_MAX_SCALE = float(os.getenv("OCR_IMAGE_MAX_SCALE", "3.0"))


def ocr_profile() -> dict[str, object]:
    return {
        "profile_version": OCR_PROFILE_VERSION,
        "languages": list(OCR_LANGUAGES),
        "dpi": OCR_DPI,
        "timeout_seconds": OCR_TIMEOUT_SECONDS,
        "image_min_dimension": OCR_IMAGE_MIN_DIMENSION,
        "image_max_pixels": OCR_IMAGE_MAX_PIXELS,
        "image_max_scale": OCR_IMAGE_MAX_SCALE,
        "image_pipeline": "direct_tesseract_baseline_plus_bounded_enhancement",
        "scanned_pdf_pipeline": "ocrmypdf_deskew_rotate",
    }
