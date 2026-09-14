"""Runtime readiness and quality-profile diagnostics."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import platform
import shutil
import subprocess
import uuid
from importlib import metadata
from pathlib import Path
from typing import Any

from invoice_parser.ocr_config import OCR_LANGUAGES, ocr_profile
from invoice_parser.paths import (
    DATA_ROOT,
    RUNTIME_ROOT,
    DEFAULT_KB_PATH,
    DEFAULT_OUTPUT_DIR,
    DEFAULT_PDF_DIR,
    DEFAULT_RAW_DIR,
    DEFAULT_REPORTS_DIR,
    DEFAULT_TEXT_DIR,
    DEFAULT_VECTOR_STORE_DIR,
)


PACKAGE_VERSIONS = (
    "faiss-cpu",
    "google-genai",
    "gradio",
    "llama-index-core",
    "llama-index-vector-stores-faiss",
    "ocrmypdf",
    "pillow",
    "pydantic",
    "pymupdf",
    "pytesseract",
    "torch",
)
IMPORTS = ("faiss", "fitz", "gradio", "llama_index.core", "PIL", "pytesseract", "torch")


def _command_version(command: str) -> str | None:
    executable = shutil.which(command)
    if not executable:
        return None
    try:
        completed = subprocess.run(
            [executable, "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    output = (completed.stdout or completed.stderr or "").strip().splitlines()
    return output[0].strip() if output else None


def _tesseract_languages() -> list[str]:
    executable = shutil.which("tesseract")
    if not executable:
        return []
    try:
        completed = subprocess.run(
            [executable, "--list-langs"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    lines = (completed.stdout or "").splitlines()
    return sorted(line.strip() for line in lines if line.strip() and "available languages" not in line.lower())


def _package_versions() -> dict[str, str | None]:
    versions: dict[str, str | None] = {}
    for package in PACKAGE_VERSIONS:
        try:
            versions[package] = metadata.version(package)
        except metadata.PackageNotFoundError:
            versions[package] = None
    return versions


def _import_availability() -> dict[str, bool]:
    availability: dict[str, bool] = {}
    for module in IMPORTS:
        try:
            availability[module] = importlib.util.find_spec(module) is not None
        except (ImportError, ModuleNotFoundError):
            availability[module] = False
    return availability


def _writable_directory(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / f".runtime-write-probe-{uuid.uuid4().hex}"
        probe.write_text("ready", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def build_runtime_report(*, check_writes: bool = True) -> dict[str, Any]:
    directories = {
        "data_root": DATA_ROOT,
        "runtime_root": RUNTIME_ROOT,
        "raw": DEFAULT_RAW_DIR,
        "pdf": DEFAULT_PDF_DIR,
        "text": DEFAULT_TEXT_DIR,
        "processed": DEFAULT_OUTPUT_DIR,
        "reports": DEFAULT_REPORTS_DIR,
        "vector_store": DEFAULT_VECTOR_STORE_DIR,
        "knowledge_base_parent": DEFAULT_KB_PATH.parent,
    }
    writable = {
        name: _writable_directory(path) if check_writes else path.exists()
        for name, path in directories.items()
    }
    tools = {
        "tesseract": _command_version("tesseract"),
        "ocrmypdf": _command_version("ocrmypdf"),
        "qpdf": _command_version("qpdf"),
        "ghostscript": _command_version("gs"),
    }
    available_languages = _tesseract_languages()
    packages = _package_versions()
    imports = _import_availability()
    profile = ocr_profile()
    fingerprint_source = {
        "python": platform.python_version(),
        "ocr_profile": profile,
        "tools": tools,
        "packages": packages,
    }
    fingerprint = hashlib.sha256(
        json.dumps(fingerprint_source, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    checks = {
        "directories_writable": all(writable.values()),
        "required_imports": all(imports.values()),
        "tesseract_available": tools["tesseract"] is not None,
        "ocrmypdf_available": tools["ocrmypdf"] is not None,
        "configured_languages_available": set(OCR_LANGUAGES).issubset(available_languages),
    }
    return {
        "status": "ready" if all(checks.values()) else "not_ready",
        "quality_fingerprint": fingerprint,
        "python": platform.python_version(),
        "platform": platform.machine(),
        "ocr_profile": profile,
        "configured_languages": list(OCR_LANGUAGES),
        "available_languages": available_languages,
        "tools": tools,
        "packages": packages,
        "imports": imports,
        "directories": {name: str(path) for name, path in directories.items()},
        "writable": writable,
        "checks": checks,
    }
