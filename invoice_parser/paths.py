import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def project_data_root() -> Path:
    """Resolve the one data root shared by local and container runtimes."""
    configured_root = os.getenv("INVOICE_DATA_ROOT")
    if configured_root:
        return Path(configured_root).expanduser()
    return PROJECT_ROOT / "data"


DATA_ROOT = project_data_root()
RUNTIME_ROOT = Path(
    os.getenv("INVOICE_RUNTIME_ROOT", str(PROJECT_ROOT / "runtime"))
).expanduser()
DEFAULT_RAW_DIR = DATA_ROOT / "data_raw"
DEFAULT_PDF_DIR = DATA_ROOT / "data_pdf"
DEFAULT_TEXT_DIR = DATA_ROOT / "data_txt"
DEFAULT_OUTPUT_DIR = DATA_ROOT / "data_processed"
DEFAULT_REPORTS_DIR = DEFAULT_OUTPUT_DIR / "reports"
DEFAULT_LLM_SECOND_PASS_DIR = DEFAULT_OUTPUT_DIR / "llm_second_pass"
DEFAULT_AGENTIC_AB_DIR = DEFAULT_OUTPUT_DIR / "agentic_ab_tests"
DEFAULT_KB_PATH = Path(
    os.getenv(
        "RAG_DB_PATH",
        os.getenv("RAG_KB_PATH", str(RUNTIME_ROOT / "knowledge_base.sqlite3")),
    )
).expanduser()
DEFAULT_VECTOR_STORE_DIR = Path(
    os.getenv("VECTOR_STORE_DIR", str(RUNTIME_ROOT / "vector_store"))
).expanduser()


def first_existing_data_dir(data_root: Path, names: list[str], fallback_name: str) -> Path:
    """Prefer this repo's data_* folders while keeping older defaults usable."""
    for name in names:
        candidate = data_root / name
        if candidate.exists():
            return candidate
    return data_root / fallback_name
