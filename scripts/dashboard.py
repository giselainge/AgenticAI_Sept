from __future__ import annotations

import argparse
import csv
import html
import json
import re
import sys
import uuid 
from dataclasses import dataclass
from datetime import datetime
from email.parser import BytesParser
from email.policy import default
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock, Thread
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.extract_invoice_fields import extract_batch, extract_row, read_ocr_body
from invoice_parser.paths import (
    DEFAULT_AGENTIC_AB_DIR,
    DEFAULT_LLM_SECOND_PASS_DIR,
    DEFAULT_OUTPUT_DIR,
    DEFAULT_PDF_DIR,
    DEFAULT_RAW_DIR,
    DEFAULT_TEXT_DIR,
    PROJECT_ROOT,
)
from invoice_parser.schema import FIELDNAMES
from rag.adaptive_rag import (
    DEFAULT_KB as RAG_DEFAULT_KB,
    FIELD_NAMES as RAG_FIELD_NAMES,
    canonical_provider,
    ensure_provider,
    load_kb,
    non_null_fields,
    now_iso,
    provider_display_name,
    record_review_corrections,
    row_signature,
    save_kb,
    seed_from_validated_csv,
    summarize_layout,
)


DEFAULT_CSV = DEFAULT_OUTPUT_DIR / "invoice_structured_fields.csv"
DEFAULT_KB = RAG_DEFAULT_KB
NULL_VALUES = {"", "null", "none", "nan"}
AUTO_APPROVAL_MIN_VALIDATED = 5
INVOICE_TYPES = {"electricity", "water", "natural gas", "telecom"}
IMPORTABLE_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".bmp"}
JOB_LOCK = Lock()
BATCH_JOBS: dict[str, dict[str, Any]] = {}

FIELD_GROUPS = {
    "Core Invoice Information": [
        ("invoice_type", "Invoice Type", "Classification of the invoice"),
        ("invoice_number", "Invoice Number", "Unique invoice identifier issued by the provider"),
        ("invoice_date", "Invoice Date", "Official invoice issuance date"),
        ("currency", "Currency", "Currency used in the invoice"),
        ("payment_due_date", "Payment Due Date", "Deadline for payment, when available"),
    ],
    "Provider Information": [
        ("provider_name", "Provider Name", "Legal company name issuing the invoice"),
        ("provider_vat_number", "Provider VAT Number", "Tax/VAT identification number of the provider"),
        ("provider_address", "Provider Address", "Provider billing or headquarters address, when available"),
    ],
    "Buyer Information": [
        ("buyer_name", "Buyer Name", "Customer or company receiving the invoice"),
        ("buyer_vat_number", "Buyer VAT Number", "Tax/VAT identification number of the customer"),
        ("buyer_address", "Buyer Address", "Customer billing address, when available"),
    ],
    "Billing & Consumption": [
        ("service_plan_name", "Service/Plan Name", "The description in the row"),
        ("consumption_start_date", "Consumption Start Date", "Beginning of the billing or consumption period"),
        ("consumption_end_date", "Consumption End Date", "End of the billing or consumption period"),
        ("units_of_consumption", "Units of Consumption", "Quantity consumed"),
        ("unit_type", "Unit Type", "Unit measurement associated with the consumption value"),
    ],
    "Financial Information": [
        ("subtotal_value", "Subtotal Value", "Amount before taxes"),
        ("total_vat", "Total VAT", "Total tax amount applied to the invoice"),
        ("total_value", "Total Value", "Final payable invoice amount"),
    ],
}

CRITICAL_FIELDS = [
    "invoice_type",
    "invoice_number",
    "invoice_date",
    "currency",
    "provider_name",
    "provider_vat_number",
    "total_value",
]
DATE_FIELDS = {"invoice_date", "payment_due_date", "consumption_start_date", "consumption_end_date"}
MONEY_FIELDS = {"subtotal_value", "total_vat", "total_value"}


@dataclass
class ReviewState:
    provider_id: str
    validated_count: int
    validation_errors: list[str]
    auto_approval: bool
    completion: float
    pipeline_status: str


@dataclass
class DashboardConfig:
    csv_path: Path
    kb_path: Path


def e(value: Any) -> str:
    return html.escape(str(value or ""), quote=True)


def is_null(value: Any) -> bool:
    return str(value or "").strip().lower() in NULL_VALUES


def normalize_cell(value: Any) -> str:
    value = "" if value is None else str(value)
    value = value.strip()
    return value if value else "null"


def read_rows(csv_path: Path) -> list[dict[str, str]]:
    if not csv_path.exists():
        return []
    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = []
        for row in reader:
            rows.append({field: normalize_cell(row.get(field)) for field in FIELDNAMES})
        return rows


def write_rows(csv_path: Path, rows: list[dict[str, str]]) -> None:
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows([{field: normalize_cell(row.get(field)) for field in FIELDNAMES} for row in rows])


def load_dashboard_rows(config: "DashboardConfig") -> list[dict[str, str]]:
    rows = read_rows(config.csv_path)
    latest_text_mtime = max(
        (path.stat().st_mtime for path in DEFAULT_TEXT_DIR.glob("*.txt") if path.is_file()),
        default=None,
    )
    csv_mtime = config.csv_path.stat().st_mtime if config.csv_path.exists() else None

    if rows and csv_mtime is not None and (latest_text_mtime is None or csv_mtime >= latest_text_mtime):
        return rows
    if not DEFAULT_TEXT_DIR.exists() or not any(DEFAULT_TEXT_DIR.glob("*.txt")):
        return []
    return extract_batch(DEFAULT_TEXT_DIR, config.csv_path)


def parse_bool(value: Any) -> bool:
    return str(value or "").strip().lower() == "true"


def parse_float(value: Any) -> float | None:
    if is_null(value):
        return None
    try:
        return float(str(value).replace(" ", "").replace(",", "."))
    except ValueError:
        return None


def valid_iso_date(value: Any) -> bool:
    if is_null(value):
        return True
    try:
        datetime.strptime(str(value), "%Y-%m-%d")
    except ValueError:
        return False
    return True


def field_completion(row: dict[str, str]) -> float:
    present = sum(1 for field in RAG_FIELD_NAMES if not is_null(row.get(field)))
    return present / len(RAG_FIELD_NAMES)


def provider_validated_count(kb: dict[str, Any], provider_id: str) -> int:
    if provider_id == "unknown":
        return 0
    provider = kb.get("providers", {}).get(provider_id, {})
    return sum(
        1
        for item in provider.get("previously_validated_invoices", [])
        if str(item.get("valid_invoice", "")).lower() == "true"
    )


def validation_errors(row: dict[str, str]) -> list[str]:
    errors = []
    if not parse_bool(row.get("valid_invoice")):
        errors.append("Invoice is not currently classified as valid.")
    for field in CRITICAL_FIELDS:
        if is_null(row.get(field)):
            errors.append(f"{field} is required for approval.")
    invoice_type = row.get("invoice_type", "")
    if not is_null(invoice_type) and invoice_type not in INVOICE_TYPES:
        errors.append("invoice_type must be electricity, water, natural gas, or telecom.")
    for field in DATE_FIELDS:
        if not valid_iso_date(row.get(field)):
            errors.append(f"{field} must use YYYY-MM-DD.")
    for field in MONEY_FIELDS:
        if not is_null(row.get(field)) and parse_float(row.get(field)) is None:
            errors.append(f"{field} must be numeric.")
    start = row.get("consumption_start_date")
    end = row.get("consumption_end_date")
    if valid_iso_date(start) and valid_iso_date(end) and not is_null(start) and not is_null(end) and start > end:
        errors.append("consumption_start_date cannot be after consumption_end_date.")
    if not is_null(row.get("extraction_warnings")):
        errors.append("Extraction warnings must be resolved before automatic approval.")
    return errors


def is_human_approved(row: dict[str, str], kb: dict[str, Any], provider_id: str) -> bool:
    signature = row_signature(row)
    source_file = normalize_cell(row.get("source_file"))
    ocr_text_file = normalize_cell(row.get("ocr_text_file"))
    provider = kb.get("providers", {}).get(provider_id, {})
    for item in provider.get("previously_validated_invoices", []):
        if item.get("review_decision") != "human_approved":
            continue
        item_source = normalize_cell(item.get("source_file"))
        item_ocr = normalize_cell(item.get("ocr_text_file"))
        if source_file != "null" and item_source == source_file:
            return True
        if ocr_text_file != "null" and item_ocr == ocr_text_file:
            return True
        if item_source == "null" and item_ocr == "null" and item.get("signature") == signature:
            return True
    return False


def has_llm_pass(row: dict[str, str]) -> bool:
    warnings = row.get("extraction_warnings", "")
    return latest_llm_structured_path(row) is not None or "Gemini artifact:" in str(warnings)


def pipeline_status(row: dict[str, str], kb: dict[str, Any], provider_id: str) -> str:
    if is_human_approved(row, kb, provider_id):
        return "approved"
    if has_llm_pass(row):
        return "llm"
    return "ocr"


def review_state(row: dict[str, str], kb: dict[str, Any]) -> ReviewState:
    provider_id = canonical_provider(row.get("provider_name", ""), " ".join(row.values()))
    errors = validation_errors(row)
    count = provider_validated_count(kb, provider_id)
    return ReviewState(
        provider_id=provider_id,
        validated_count=count,
        validation_errors=errors,
        auto_approval=count >= AUTO_APPROVAL_MIN_VALIDATED and not errors,
        completion=field_completion(row),
        pipeline_status=pipeline_status(row, kb, provider_id),
    )


def status_label(state: ReviewState) -> str:
    return {
        "approved": "Approved",
        "llm": "LLM",
        "ocr": "OCR",
    }.get(state.pipeline_status, "OCR")


def upsert_review_memory(row: dict[str, str], decision: str, note: str, kb_path: Path) -> None:
    kb = load_kb(kb_path)
    provider_id = canonical_provider(row.get("provider_name", ""), " ".join(row.values()))
    provider = ensure_provider(kb, provider_id, row.get("provider_name"))
    signature = row_signature(row)
    memory = {
        "signature": signature,
        "source_file": row.get("source_file", ""),
        "ocr_text_file": row.get("ocr_text_file", ""),
        "valid_invoice": row.get("valid_invoice", "null"),
        "validated_fields": non_null_fields(row),
        "extraction_warnings": row.get("extraction_warnings", "null"),
        "review_decision": decision,
        "review_note": note,
        "added_at": now_iso(),
    }
    existing_index = next(
        (
            index
            for index, item in enumerate(provider.get("previously_validated_invoices", []))
            if item.get("signature") == signature
        ),
        None,
    )
    if existing_index is None:
        provider["previously_validated_invoices"].append(memory)
    else:
        provider["previously_validated_invoices"][existing_index].update(memory)

    layout = summarize_layout(row)
    layout_key = (layout.get("layout_id"), layout.get("invoice_type"))
    layouts = provider.setdefault("known_invoice_layouts", [])
    if layout_key not in {(item.get("layout_id"), item.get("invoice_type")) for item in layouts}:
        layouts.append(layout)

    provider.setdefault("validation_history", []).append(
        {
            "source_file": row.get("source_file", ""),
            "event": decision,
            "valid_invoice": row.get("valid_invoice", "null"),
            "missing_required_fields": [field for field in RAG_FIELD_NAMES if is_null(row.get(field))],
            "validation_errors": validation_errors(row),
            "review_note": note,
            "recorded_at": now_iso(),
        }
    )
    save_kb(kb, kb_path)


def record_field_feedback(row: dict[str, str], changes: dict[str, tuple[str, str]], note: str, kb_path: Path) -> None:
    record_review_corrections(row, changes, note, kb_path)


def safe_read_text(path_value: str) -> str:
    if is_null(path_value):
        return ""
    path = Path(path_value)
    if not path.exists():
        return ""
    try:
        _, body = read_ocr_body(path)
    except Exception:
        body = path.read_text(encoding="utf-8", errors="replace")
    return body


def provider_summary(kb: dict[str, Any]) -> list[dict[str, Any]]:
    output = []
    for provider_id, provider in sorted(kb.get("providers", {}).items()):
        output.append(
            {
                "provider_id": provider_id,
                "provider_name": provider.get("provider_name", provider_display_name(provider_id)),
                "validated_invoices": provider_validated_count(kb, provider_id),
                "tips": len(provider.get("provider_specific_extraction_tips", [])),
                "ocr_corrections": len(provider.get("common_ocr_corrections", {})),
                "known_layouts": len(provider.get("known_invoice_layouts", [])),
                "human_feedback": len(provider.get("human_reviewer_feedback", [])),
                "validation_events": len(provider.get("validation_history", [])),
            }
        )
    return output


def query_path(view: str, **params: Any) -> str:
    payload = {"view": view}
    payload.update({key: value for key, value in params.items() if value is not None})
    return "/?" + urlencode(payload)


def table(headers: list[str], rows: list[list[Any]], empty: str = "No rows.") -> str:
    if not rows:
        return f"<div class='empty-state compact'><strong>{e(empty)}</strong></div>"
    head = "".join(f"<th>{e(header)}</th>" for header in headers)
    body = []
    for row in rows:
        body.append("<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>")
    return f"<div class='table-wrap'><table><thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table></div>"


def notice(message: str, kind: str = "info") -> str:
    return f"<div class='notice {kind}'>{e(message)}</div>"


def empty_state(title: str, message: str, action_html: str = "") -> str:
    action = f"<div class='actions'>{action_html}</div>" if action_html else ""
    return f"""<section class="empty-state">
  <h2>{e(title)}</h2>
  <p>{e(message)}</p>
  {action}
</section>"""


def create_batch_job(kind: str, labels: list[str]) -> str:
    job_id = uuid.uuid4().hex
    now = now_iso()
    with JOB_LOCK:
        BATCH_JOBS[job_id] = {
            "id": job_id,
            "kind": kind,
            "status": "running",
            "total": len(labels),
            "completed": 0,
            "current": "",
            "items": [
                {"label": label, "status": "pending", "message": ""}
                for label in labels
            ],
            "created_at": now,
            "updated_at": now,
            "summary": "",
        }
    return job_id


def update_batch_job(job_id: str, **updates: Any) -> None:
    with JOB_LOCK:
        job = BATCH_JOBS.get(job_id)
        if not job:
            return
        job.update(updates)
        job["updated_at"] = now_iso()


def update_batch_item(job_id: str, item_index: int, status: str, message: str = "") -> None:
    with JOB_LOCK:
        job = BATCH_JOBS.get(job_id)
        if not job:
            return
        if 0 <= item_index < len(job.get("items", [])):
            job["items"][item_index].update({"status": status, "message": message})
        job["completed"] = sum(
            1
            for item in job.get("items", [])
            if item.get("status") in {"success", "skipped", "warning", "error"}
        )
        job["updated_at"] = now_iso()


def batch_job_snapshot(job_id: str) -> dict[str, Any] | None:
    with JOB_LOCK:
        job = BATCH_JOBS.get(job_id)
        return json.loads(json.dumps(job)) if job else None


def mark_unfinished_batch_items(job_id: str, status: str, message: str) -> None:
    job = batch_job_snapshot(job_id)
    if not job:
        return
    for index, item in enumerate(job.get("items", [])):
        if item.get("status") not in {"success", "skipped", "warning", "error"}:
            update_batch_item(job_id, index, status, message)


def parse_selected_indices(form: dict[str, list[str]], row_count: int) -> list[int]:
    indices: list[int] = []
    for value in form.get("selected_invoice", []):
        try:
            index = int(value)
        except ValueError:
            continue
        if 0 <= index < row_count and index not in indices:
            indices.append(index)
    return indices


def flash_notice(message: str) -> str:
    kind = "info"
    text = message
    prefixes = {
        "SUCCESS:": "success",
        "WARNING:": "warning",
        "ERROR:": "danger",
    }
    for prefix, prefix_kind in prefixes.items():
        if text.startswith(prefix):
            kind = prefix_kind
            text = text[len(prefix) :].strip()
            break
    return notice(text, kind)


def render_layout(title: str, view: str, body: str, config: DashboardConfig, message: str = "") -> bytes:
    nav = [
        ("overview", "Overview"),
        ("import", "Import Invoices"),
        ("review", "Manual Review"),
        ("rag", "RAG Context"),
        ("memory", "Provider Memory"),
        ("schema", "Invoice Fields"),
        ("export", "Export"),
    ]
    links = "".join(
        f"<a class='nav-link {'active' if key == view else ''}' href='{query_path(key)}'>{label}</a>"
        for key, label in nav
    )
    current_label = next((label for key, label in nav if key == view), "Overview")
    html_doc = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{e(title)}</title>
  <style>
    :root {{
      --bg: #f4f7fb;
      --bg-soft: #eef4f8;
      --panel: #ffffff;
      --text: #17202a;
      --muted: #5f6f82;
      --line: #d8e1ea;
      --primary: #176b87;
      --primary-strong: #0f4c5c;
      --danger: #9f2331;
      --success: #1f7a4d;
      --warn: #946200;
      --shadow: 0 10px 30px rgba(21, 52, 72, 0.08);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background:
        linear-gradient(180deg, #edf6fa 0, var(--bg) 280px),
        var(--bg);
      color: var(--text);
      font-family: Inter, Segoe UI, Arial, sans-serif;
      letter-spacing: 0;
    }}
    header {{
      background: rgba(255, 255, 255, 0.94);
      border-bottom: 1px solid var(--line);
      padding: 18px 24px 16px;
      position: sticky;
      top: 0;
      z-index: 5;
      backdrop-filter: blur(8px);
    }}
    h1 {{ font-size: 26px; line-height: 1.15; margin: 0 0 5px; }}
    h2 {{ font-size: 19px; line-height: 1.25; margin: 26px 0 12px; }}
    h3 {{ font-size: 15px; line-height: 1.3; margin: 18px 0 8px; }}
    p {{ line-height: 1.55; }}
    .muted {{ color: var(--muted); }}
    .shell {{ max-width: 1480px; margin: 0 auto; }}
    .topline {{ display: flex; align-items: flex-start; justify-content: space-between; gap: 18px; }}
    .section-pill {{
      border: 1px solid #b7d3df;
      border-radius: 999px;
      color: var(--primary-strong);
      background: #edf8fb;
      padding: 6px 10px;
      font-size: 12px;
      font-weight: 700;
      white-space: nowrap;
      margin-top: 3px;
    }}
    .nav {{ display: flex; gap: 8px; flex-wrap: wrap; margin-top: 14px; }}
    .nav-link {{
      color: var(--text);
      text-decoration: none;
      border: 1px solid var(--line);
      border-radius: 7px;
      padding: 8px 12px;
      background: #fbfcfd;
      font-size: 14px;
      transition: background 0.16s ease, border-color 0.16s ease, color 0.16s ease;
    }}
    .nav-link:hover {{ background: #edf8fb; border-color: #9fc7d5; color: var(--primary-strong); }}
    .nav-link.active {{ background: var(--primary); color: white; border-color: var(--primary); box-shadow: 0 5px 16px rgba(23, 107, 135, 0.22); }}
    main {{ padding: 26px 24px 50px; }}
    .grid {{ display: grid; gap: 16px; }}
    .metrics {{ grid-template-columns: repeat(5, minmax(140px, 1fr)); }}
    .two {{ grid-template-columns: minmax(0, 1fr) minmax(320px, 0.4fr); align-items: start; }}
    .review-layout {{
      display: grid;
      grid-template-columns: minmax(480px, 0.95fr) minmax(420px, 1.05fr);
      gap: 16px;
      align-items: start;
    }}
    .review-fields {{ min-width: 0; }}
    .pdf-pane {{
      position: sticky;
      top: 106px;
      min-width: 0;
      padding: 0;
      overflow: hidden;
    }}
    .pdf-toolbar {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 12px 14px;
      border-bottom: 1px solid var(--line);
      background: #f7fafc;
    }}
    .pdf-title {{ font-weight: 700; overflow-wrap: anywhere; }}
    .pdf-frame {{
      width: 100%;
      height: calc(100vh - 190px);
      min-height: 620px;
      border: 0;
      display: block;
      background: #eef2f6;
    }}
    .panel {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 18px;
      box-shadow: var(--shadow);
    }}
    .metric {{ position: relative; overflow: hidden; }}
    .metric::before {{ content: ""; position: absolute; inset: 0 auto 0 0; width: 4px; background: var(--primary); opacity: 0.8; }}
    .metric .label {{ color: var(--muted); font-size: 11px; text-transform: uppercase; font-weight: 800; letter-spacing: 0.04em; }}
    .metric .value {{ font-size: 29px; font-weight: 800; margin-top: 6px; }}
    .notice {{ border-radius: 8px; padding: 12px 14px; margin: 0 0 16px; border: 1px solid var(--line); line-height: 1.45; }}
    .notice.info {{ background: #edf7fb; border-color: #b8dce9; color: #164d61; }}
    .notice.success {{ background: #ebf7ef; border-color: #b7dfc7; color: #195b3a; }}
    .notice.warning {{ background: #fff7df; border-color: #f0d488; color: #6e4d00; }}
    .notice.danger {{ background: #fff0f1; border-color: #edb7be; color: #7f1d2a; }}
    .table-wrap {{ overflow-x: auto; border: 1px solid var(--line); border-radius: 8px; background: white; box-shadow: var(--shadow); }}
    table {{ border-collapse: collapse; width: 100%; font-size: 13px; }}
    th, td {{ border-bottom: 1px solid var(--line); padding: 10px 11px; text-align: left; vertical-align: top; }}
    th {{ background: #f2f6fa; color: #314152; font-size: 11px; text-transform: uppercase; letter-spacing: 0.04em; }}
    tbody tr:hover {{ background: #f8fbfd; }}
    tr:last-child td {{ border-bottom: 0; }}
    .status {{ display: inline-block; border-radius: 999px; padding: 4px 8px; font-size: 12px; font-weight: 650; }}
    .approved {{ color: var(--success); background: #e8f7ef; }}
    .llm {{ color: var(--primary-strong); background: #e8f4f8; }}
    .ocr {{ color: var(--warn); background: #fff3cd; }}
    a {{ color: var(--primary); }}
    form {{ margin: 0; }}
    fieldset {{ border: 1px solid var(--line); border-radius: 8px; margin: 0 0 14px; padding: 14px; background: #fbfdfe; }}
    legend {{ padding: 0 6px; color: #314152; font-weight: 700; }}
    label {{ display: block; font-size: 12px; color: var(--muted); margin-bottom: 5px; }}
    input[type=text], input[type=password], textarea, select {{
      width: 100%;
      border: 1px solid #cdd6df;
      border-radius: 6px;
      padding: 9px 10px;
      min-height: 38px;
      font: inherit;
      background: white;
    }}
    input[type=text]:focus, input[type=password]:focus, textarea:focus, select:focus {{
      border-color: var(--primary);
      box-shadow: 0 0 0 3px rgba(23, 107, 135, 0.15);
      outline: none;
    }}
    textarea {{ min-height: 82px; resize: vertical; }}
    .field-grid {{ display: grid; grid-template-columns: repeat(2, minmax(220px, 1fr)); gap: 12px; }}
    .actions {{ display: flex; gap: 10px; flex-wrap: wrap; margin-top: 12px; }}
    .filters {{ margin: 16px 0; }}
    .filter-grid {{ display: grid; grid-template-columns: minmax(220px, 1.5fr) repeat(3, minmax(150px, 1fr)); gap: 12px; align-items: end; }}
    .help-text {{ font-size: 14px; line-height: 1.5; max-width: 920px; }}
    .page-intro {{ margin-bottom: 18px; }}
    .summary-list {{ margin: 8px 0 0; padding-left: 18px; }}
    .summary-list li {{ margin: 4px 0; }}
    .evidence-grid {{ display: grid; grid-template-columns: repeat(2, minmax(280px, 1fr)); gap: 14px; }}
    .tip-list {{ list-style: none; margin: 8px 0 0; padding: 0; display: grid; gap: 8px; }}
    .tip-row {{ display: flex; gap: 10px; align-items: flex-start; justify-content: space-between; border: 1px solid var(--line); border-radius: 6px; padding: 9px 10px; background: #fbfcfd; }}
    .tip-row span {{ flex: 1; }}
    .tip-row form {{ flex: 0 0 auto; }}
    .tip-row button {{ padding: 5px 8px; font-size: 12px; }}
    .import-layout {{ display: grid; grid-template-columns: minmax(360px, 0.9fr) minmax(280px, 0.45fr); gap: 16px; align-items: start; }}
    .upload-card input[type=file] {{
      width: 100%;
      border: 1px dashed #9fb2c3;
      border-radius: 8px;
      padding: 18px;
      background: #f8fafc;
    }}
    .result-stack {{ display: grid; gap: 8px; margin-bottom: 16px; }}
    .result-stack .notice {{ margin: 0; }}
    .empty-state {{
      border: 1px dashed #a9c2d0;
      border-radius: 8px;
      background: #f8fcfd;
      padding: 24px;
      color: var(--muted);
      box-shadow: var(--shadow);
      margin: 12px 0 18px;
    }}
    .empty-state h2 {{ color: var(--text); margin-top: 0; }}
    .empty-state.compact {{ padding: 14px; box-shadow: none; margin: 0; }}
    .batch-toolbar {{ margin-top: 16px; padding-top: 16px; border-top: 1px solid var(--line); }}
    .batch-toolbar h3 {{ margin-top: 0; }}
    .selection-cell {{ width: 42px; text-align: center; }}
    .progress-track {{
      height: 12px;
      background: #dbe7ef;
      border-radius: 999px;
      overflow: hidden;
      margin: 12px 0;
    }}
    .progress-fill {{
      height: 100%;
      width: 0%;
      background: var(--primary);
      border-radius: 999px;
      transition: width 0.25s ease;
    }}
    .job-list {{ list-style: none; padding: 0; margin: 12px 0 0; display: grid; gap: 7px; }}
    .job-list li {{
      display: flex;
      justify-content: space-between;
      gap: 12px;
      border: 1px solid var(--line);
      border-radius: 7px;
      padding: 8px 10px;
      background: #fbfdfe;
    }}
    .job-list .pending {{ color: var(--muted); }}
    .job-list .running {{ color: var(--primary-strong); font-weight: 700; }}
    .job-list .success {{ color: var(--success); font-weight: 700; }}
    .job-list .skipped {{ color: var(--muted); font-weight: 700; }}
    .job-list .warning {{ color: var(--warn); font-weight: 700; }}
    .job-list .error {{ color: var(--danger); font-weight: 700; }}
    button {{
      border: 1px solid var(--primary);
      background: var(--primary);
      color: white;
      border-radius: 6px;
      padding: 9px 13px;
      font: inherit;
      font-weight: 700;
      cursor: pointer;
      transition: transform 0.12s ease, box-shadow 0.12s ease, background 0.12s ease;
    }}
    button:hover {{ background: var(--primary-strong); box-shadow: 0 7px 18px rgba(23, 107, 135, 0.22); transform: translateY(-1px); }}
    button.secondary {{ background: white; color: var(--primary); }}
    button.secondary:hover {{ background: #edf8fb; }}
    button.danger {{ background: var(--danger); border-color: var(--danger); }}
    button.danger:hover {{ background: #7f1d2a; }}
    button:disabled {{ opacity: 0.45; cursor: not-allowed; }}
    pre {{
      white-space: pre-wrap;
      word-break: break-word;
      background: #111827;
      color: #f9fafb;
      border-radius: 8px;
      padding: 14px;
      max-height: 460px;
      overflow: auto;
      font-size: 12px;
    }}
    details {{ border: 1px solid var(--line); border-radius: 8px; padding: 10px 12px; background: white; margin-bottom: 10px; }}
    summary {{ cursor: pointer; font-weight: 700; }}
    .footer {{
      color: var(--muted);
      border-top: 1px solid var(--line);
      margin-top: 32px;
      padding-top: 16px;
      font-size: 13px;
    }}
    @media (max-width: 900px) {{
      .metrics, .two, .field-grid, .review-layout, .filter-grid, .evidence-grid, .import-layout {{ grid-template-columns: 1fr; }}
      .pdf-pane {{ position: static; }}
      .pdf-frame {{ height: 72vh; min-height: 460px; }}
      header, main {{ padding-left: 14px; padding-right: 14px; }}
      .topline {{ display: block; }}
      .section-pill {{ display: inline-block; margin-top: 10px; }}
    }}
  </style>
</head>
<body>
  <header>
    <div class="shell">
      <div class="topline">
        <div>
          <h1>Invoice Extraction Dashboard</h1>
          <div class="muted">Import invoices, review extracted fields, and teach provider memory from corrections.</div>
        </div>
        <div class="section-pill">{e(current_label)}</div>
      </div>
      <nav class="nav">{links}</nav>
    </div>
  </header>
  <main class="shell">
    {flash_notice(message) if message else ""}
    {body}
    <div class="footer">Workflow: import files -> OCR and extraction -> manual review -> provider memory -> export CSV or knowledge base.</div>
  </main>
  <script>
    (function () {{
      const selectAll = document.querySelector("[data-select-all]");
      if (selectAll) {{
        selectAll.addEventListener("change", function () {{
          document.querySelectorAll("input[name='selected_invoice']").forEach(function (box) {{
            box.checked = selectAll.checked;
          }});
        }});
      }}
      const panel = document.querySelector("[data-job-id]");
      if (!panel) return;
      const jobId = panel.getAttribute("data-job-id");
      const fill = panel.querySelector("[data-progress-fill]");
      const summary = panel.querySelector("[data-progress-summary]");
      const current = panel.querySelector("[data-progress-current]");
      const list = panel.querySelector("[data-progress-items]");
      function render(job) {{
        const total = Math.max(1, Number(job.total || 0));
        const completed = Number(job.completed || 0);
        const pct = Math.round((completed / total) * 100);
        fill.style.width = pct + "%";
        summary.textContent = job.status + " - " + completed + "/" + job.total + " complete";
        current.textContent = job.current || job.summary || "";
        list.innerHTML = "";
        (job.items || []).forEach(function (item) {{
          const li = document.createElement("li");
          const left = document.createElement("span");
          const right = document.createElement("span");
          left.textContent = item.label || "";
          right.textContent = item.message || item.status || "";
          right.className = item.status || "pending";
          li.appendChild(left);
          li.appendChild(right);
          list.appendChild(li);
        }});
        if (job.status === "running") {{
          window.setTimeout(fetchJob, 1000);
        }}
      }}
      function fetchJob() {{
        fetch("/job-status?id=" + encodeURIComponent(jobId), {{ cache: "no-store" }})
          .then(function (response) {{ return response.json(); }})
          .then(render)
          .catch(function () {{ window.setTimeout(fetchJob, 2000); }});
      }}
      fetchJob();
    }})();
  </script>
</body>
</html>"""
    return html_doc.encode("utf-8")


def render_metrics(rows: list[dict[str, str]], kb: dict[str, Any]) -> str:
    states = [review_state(row, kb) for row in rows]
    valid_count = sum(1 for row in rows if parse_bool(row.get("valid_invoice")))
    auto_ready = sum(1 for state in states if state.auto_approval)
    review_count = sum(1 for state in states if state.validation_errors)
    avg_completion = round(sum(state.completion for state in states) / len(states) * 100) if states else 0
    metrics = [
        ("Invoices", len(rows)),
        ("Valid invoices", valid_count),
        ("Auto-approval ready", auto_ready),
        ("Needs review", review_count),
        ("Avg. field completion", f"{avg_completion}%"),
    ]
    return "<div class='grid metrics'>" + "".join(
        f"<section class='panel metric'><div class='label'>{e(label)}</div><div class='value'>{e(value)}</div></section>"
        for label, value in metrics
    ) + "</div>"


def render_batch_progress_panel(job_id: str) -> str:
    job = batch_job_snapshot(job_id)
    if not job:
        return notice("Batch job was not found. It may have completed before the dashboard restarted.", "warning")
    return f"""<section class="panel" data-job-id="{e(job_id)}">
  <h2>Batch Progress</h2>
  <p class="muted" data-progress-summary>{e(job.get("status", "running"))} - {e(job.get("completed", 0))}/{e(job.get("total", 0))} complete</p>
  <div class="progress-track"><div class="progress-fill" data-progress-fill></div></div>
  <p data-progress-current>{e(job.get("current", ""))}</p>
  <ul class="job-list" data-progress-items></ul>
</section>"""


def row_status_cell(state: ReviewState) -> str:
    label = status_label(state)
    class_name = status_key(state)
    return f"<span class='status {class_name}'>{e(label)}</span>"


def source_pdf_name(row: dict[str, str]) -> str:
    source_file = row.get("source_file", "")
    if not is_null(source_file):
        source_path = Path(source_file.replace("\\", "/"))
        if source_path.suffix.lower() == ".pdf":
            return source_path.name
        return f"{source_path.stem}.pdf"

    ocr_text_file = row.get("ocr_text_file", "")
    if not is_null(ocr_text_file):
        stem = Path(ocr_text_file.replace("\\", "/")).stem.removesuffix("_selected_text")
        return f"{stem}.pdf"

    return "null"


def source_pdf_path(row: dict[str, str]) -> Path:
    return DEFAULT_PDF_DIR / source_pdf_name(row)


def latest_llm_structured_path(row: dict[str, str]) -> Path | None:
    stem = Path(source_pdf_name(row)).stem
    if not stem or stem == "null" or not DEFAULT_LLM_SECOND_PASS_DIR.exists():
        return None
    stable_path = DEFAULT_LLM_SECOND_PASS_DIR / f"{stem}_gemini_structured.json"
    if stable_path.exists():
        return stable_path
    matches = sorted(DEFAULT_LLM_SECOND_PASS_DIR.glob(f"{stem}_gemini_structured*.json"))
    return matches[-1] if matches else None


def load_llm_structured(row: dict[str, str]) -> tuple[dict[str, Any] | None, Path | None, str | None]:
    path = latest_llm_structured_path(row)
    if path is None:
        return None, None, "No Gemini structured extraction was found for this invoice."
    try:
        return json.loads(path.read_text(encoding="utf-8")), path, None
    except json.JSONDecodeError as exc:
        return None, path, f"Gemini structured extraction is not valid JSON: {exc}"


def llm_summary_panel(row: dict[str, str]) -> str:
    data, path, error = load_llm_structured(row)
    if error:
        return notice(error, "info")
    if not data or path is None:
        return ""
    review_status = data.get("review_status", "unknown")
    missing = data.get("missing_fields", [])
    errors = data.get("validation_errors", [])
    extracted_count = sum(1 for field in RAG_FIELD_NAMES if not is_null(data.get(field)))
    details = [
        f"<li>Artifact: {e(path.name)}</li>",
        f"<li>Review status: {e(review_status)}</li>",
        f"<li>Extracted fields: {extracted_count}/{len(RAG_FIELD_NAMES)}</li>",
    ]
    if missing:
        details.append(f"<li>Missing fields: {e(', '.join(str(item) for item in missing[:6]))}</li>")
    if errors:
        details.append(f"<li>Validation errors: {e(' | '.join(str(item) for item in errors[:3]))}</li>")
    return "<section class='panel'><h2>Gemini Second Pass</h2><ul class='summary-list'>" + "".join(details) + "</ul></section>"


def agentic_ab_artifact_path(row: dict[str, str]) -> Path:
    text_file = str(row.get("ocr_text_file") or "").replace("\\", "/")
    stem = Path(text_file).stem.removesuffix("_selected_text") if not is_null(text_file) else Path(source_pdf_name(row)).stem
    return DEFAULT_AGENTIC_AB_DIR / f"{stem}_agentic_ab.json"


def load_agentic_ab_result(row: dict[str, str]) -> tuple[dict[str, Any] | None, Path, str | None]:
    path = agentic_ab_artifact_path(row)
    if not path.exists():
        return None, path, None
    try:
        return json.loads(path.read_text(encoding="utf-8")), path, None
    except json.JSONDecodeError as exc:
        return None, path, f"Agentic A/B artifact is not valid JSON: {exc}"


def agentic_ab_panel(row: dict[str, str], selected_index: int) -> str:
    data, path, error = load_agentic_ab_result(row)
    body = [
        "<section class='panel'><h2>Experimental Agentic A/B Test</h2>",
        "<p class='muted'>Plan A keeps OCR plus deterministic extraction. Plan B coordinates classification, provider memory, extraction, validation, and review-routing agents. The test does not change the invoice CSV or provider memory.</p>",
    ]
    if error:
        body.append(notice(error, "warning"))
    elif data:
        plan_a = data.get("plan_a", {})
        plan_b = data.get("plan_b", {})
        comparison = data.get("comparison", {})
        body.append(
            table(
                ["Plan", "Method", "Completion", "Validation errors", "Route"],
                [
                    ["A", e(plan_a.get("method", "")), f"{round(float(plan_a.get('completion', 0)) * 100)}%", len(plan_a.get("validation_errors", [])), e(plan_a.get("route", ""))],
                    ["B", e(plan_b.get("method", "")), f"{round(float(plan_b.get('completion', 0)) * 100)}%", len(plan_b.get("validation_errors", [])), e(plan_b.get("route", ""))],
                ],
            )
        )
        changes = comparison.get("changed_fields", [])
        body.append(f"<p class='muted'>Artifact: {e(path.name)}. Changed fields: {len(changes)}. Plan B LLM used: {e(str(plan_b.get('llm_used', False)).lower())}.</p>")
        if changes:
            body.append(
                table(
                    ["Field", "Plan A", "Plan B"],
                    [[e(item.get("field", "")), e(item.get("plan_a", "null")), e(item.get("plan_b", "null"))] for item in changes],
                )
            )
        evaluation = data.get("evaluation")
        if evaluation:
            body.append(notice(f"Reviewer verdict: {evaluation.get('preferred_plan', 'inconclusive')}. {evaluation.get('reviewer_note', '')}", "success"))
        body.append(
            f"""<form method="post" action="/action">
  <input type="hidden" name="action" value="record_agentic_ab_verdict">
  <input type="hidden" name="invoice" value="{selected_index}">
  <label for="ab_preferred_plan">Human accuracy verdict</label>
  <select id="ab_preferred_plan" name="preferred_plan">
    <option value="plan_b">Plan B is more accurate</option>
    <option value="plan_a">Plan A is more accurate</option>
    <option value="tie">Tie</option>
    <option value="inconclusive">Inconclusive</option>
  </select>
  <label for="ab_verdict_note">Evidence or correction note</label>
  <textarea id="ab_verdict_note" name="verdict_note"></textarea>
  <div class="actions"><button class="secondary">Save A/B verdict</button></div>
</form>"""
        )
    else:
        body.append("<p class='muted'>No A/B run has been saved for this invoice.</p>")
    body.append(
        f"""<form method="post" action="/action">
  <input type="hidden" name="action" value="run_agentic_ab">
  <input type="hidden" name="invoice" value="{selected_index}">
  <div class="field-grid">
    <div><label for="ab_gemini_api_key">Gemini API key</label><input id="ab_gemini_api_key" name="gemini_api_key" type="password" autocomplete="off" required></div>
    <div><label for="ab_gemini_model">Gemini model</label><input id="ab_gemini_model" name="gemini_model" type="text" value="gemini-3.5-flash"></div>
  </div>
  <p class="muted">The key is used only for Plan B in this request and is not saved.</p>
  <div class="actions"><button>Run A/B test</button></div>
</form></section>"""
    )
    return "".join(body)


def review_metrics_panel(state: ReviewState) -> str:
    return (
        "<div class='grid metrics'>"
        f"<section class='panel metric'><div class='label'>Completion</div><div class='value'>{round(state.completion * 100)}%</div></section>"
        f"<section class='panel metric'><div class='label'>Validation errors</div><div class='value'>{len(state.validation_errors)}</div></section>"
        f"<section class='panel metric'><div class='label'>Provider memory</div><div class='value'>{state.validated_count}</div></section>"
        "</div>"
    )


def apply_llm_structured_to_row(row: dict[str, str], data: dict[str, Any], artifact_path: Path | None = None) -> dict[str, str]:
    updated = dict(row)
    for field in RAG_FIELD_NAMES:
        if field in data:
            updated[field] = normalize_cell(data.get(field))

    warnings = []
    for key, label in [
        ("review_status", "Gemini review status"),
        ("review_reasons", "Gemini review reasons"),
        ("validation_errors", "Gemini validation errors"),
        ("missing_fields", "Gemini missing fields"),
        ("uncertain_fields", "Gemini uncertain fields"),
    ]:
        value = data.get(key)
        if not value:
            continue
        if isinstance(value, list):
            value_text = ", ".join(str(item) for item in value)
        else:
            value_text = str(value)
        warnings.append(f"{label}: {value_text}")
    if artifact_path:
        warnings.append(f"Gemini artifact: {artifact_path}")
    existing = "" if is_null(updated.get("extraction_warnings")) else updated.get("extraction_warnings", "")
    combined = " | ".join(part for part in [existing, *warnings] if part)
    updated["extraction_warnings"] = normalize_cell(combined)
    return {field: normalize_cell(updated.get(field)) for field in FIELDNAMES}


def query_value(query: dict[str, list[str]], key: str) -> str:
    return query.get(key, [""])[0].strip()


def status_key(state: ReviewState) -> str:
    return state.pipeline_status


def option_tags(values: list[str], selected_value: str, default_label: str) -> str:
    options = [f"<option value=''>{e(default_label)}</option>"]
    for value in values:
        selected = " selected" if value == selected_value else ""
        options.append(f"<option value='{e(value)}'{selected}>{e(value)}</option>")
    return "".join(options)


def render_overview_filters(rows: list[dict[str, str]], query: dict[str, list[str]]) -> str:
    q = query_value(query, "q")
    status = query_value(query, "status")
    provider = query_value(query, "provider")
    invoice_type = query_value(query, "invoice_type")
    providers = sorted({row.get("provider_name", "null") for row in rows if not is_null(row.get("provider_name"))})
    invoice_types = sorted({row.get("invoice_type", "null") for row in rows if not is_null(row.get("invoice_type"))})
    status_options = "".join(
        [
            "<option value=''>All statuses</option>",
            f"<option value='ocr'{' selected' if status == 'ocr' else ''}>OCR</option>",
            f"<option value='llm'{' selected' if status == 'llm' else ''}>LLM</option>",
            f"<option value='approved'{' selected' if status == 'approved' else ''}>Approved</option>",
        ]
    )
    return f"""<form method="get" action="/" class="panel filters">
  <input type="hidden" name="view" value="overview">
  <div class="filter-grid">
    <div><label for="q">Search</label><input id="q" name="q" type="text" value="{e(q)}" placeholder="PDF, provider, invoice, date, total"></div>
    <div><label for="status">Status</label><select id="status" name="status">{status_options}</select></div>
    <div><label for="provider">Provider</label><select id="provider" name="provider">{option_tags(providers, provider, "All providers")}</select></div>
    <div><label for="invoice_type">Type</label><select id="invoice_type" name="invoice_type">{option_tags(invoice_types, invoice_type, "All types")}</select></div>
  </div>
  <div class="actions"><button>Apply filters</button><a class="nav-link" href="{query_path('overview')}">Clear filters</a></div>
</form>"""


def overview_row_matches(row: dict[str, str], state: ReviewState, query: dict[str, list[str]]) -> bool:
    q = query_value(query, "q").lower()
    status = query_value(query, "status")
    provider = query_value(query, "provider")
    invoice_type = query_value(query, "invoice_type")
    if status and status_key(state) != status:
        return False
    if provider and row.get("provider_name", "") != provider:
        return False
    if invoice_type and row.get("invoice_type", "") != invoice_type:
        return False
    if q:
        haystack = " ".join(
            [
                source_pdf_name(row),
                row.get("provider_name", ""),
                row.get("invoice_type", ""),
                row.get("invoice_number", ""),
                row.get("invoice_date", ""),
                row.get("total_value", ""),
                status_label(state),
            ]
        ).lower()
        return q in haystack
    return True


def render_overview(rows: list[dict[str, str]], kb: dict[str, Any], query: dict[str, list[str]]) -> str:
    body = [
        """<section class="panel page-intro help-text">
  <h2>Review Pipeline</h2>
  <p>Track imported invoices, find records that need review, and refresh extraction or provider memory when the source data changes.</p>
</section>""",
        render_metrics(rows, kb),
        "<h2>Processing Queue</h2>",
        render_overview_filters(rows, query),
    ]
    job_id = query_value(query, "job")
    if job_id:
        body.append(render_batch_progress_panel(job_id))
    entries = [
        (index, row, review_state(row, kb))
        for index, row in enumerate(rows)
    ]
    visible_entries = [
        entry
        for entry in entries
        if overview_row_matches(entry[1], entry[2], query)
    ]
    body.append(f"<p class='muted'>Showing {len(visible_entries)} of {len(rows)} invoices. Use filters to isolate records that need attention.</p>")
    body.append("<form method='post' action='/action'>")
    body.append("<p class='muted'><label><input type='checkbox' data-select-all> Select all visible invoices</label></p>")
    table_rows = []
    for index, row, state in visible_entries:
        table_rows.append(
            [
                f"<input type='checkbox' name='selected_invoice' value='{index}' aria-label='Select {e(source_pdf_name(row))}'>",
                f"<a href='{query_path('review', invoice=index)}'>{e(source_pdf_name(row))}</a>",
                row_status_cell(state),
                e(row.get("provider_name", "null")),
                e(row.get("invoice_type", "null")),
                e(row.get("invoice_number", "null")),
                e(row.get("invoice_date", "null")),
                e(row.get("total_value", "null")),
                e(state.validated_count),
                e(f"{round(state.completion * 100)}%"),
                e(len(state.validation_errors)),
            ]
        )
    body.append(
        table(
            [
                "Select",
                "Source PDF",
                "Status",
                "Provider",
                "Type",
                "Invoice",
                "Date",
                "Total",
                "Provider memory",
                "Completion",
                "Errors",
            ],
            table_rows,
            "No invoices match the current filters.",
        )
    )
    body.append(render_pipeline_forms())
    body.append("</form>")
    return "".join(body)


def render_pipeline_forms() -> str:
    return """<h2>Pipeline Controls</h2>
<div class="panel">
  <p class="muted">Use these actions when OCR text files or the structured CSV have changed.</p>
  <div class="actions">
    <button name="action" value="batch_extract">Run selected OCR extraction</button>
    <button class="secondary" name="action" value="seed_memory">Seed / refresh RAG memory</button>
  </div>
  <h3>Batch Gemini PDF extraction</h3>
  <p class="muted">Run Gemini over every invoice row that has a matching PDF, then apply successful structured fields to Manual Review. The API key is used only for this request and is not saved.</p>
    <div class="field-grid">
      <div><label for="batch_gemini_api_key">Gemini API key</label><input id="batch_gemini_api_key" name="gemini_api_key" type="password" autocomplete="off"></div>
      <div><label for="batch_gemini_model">Gemini model</label><input id="batch_gemini_model" name="gemini_model" type="text" value="gemini-3.5-flash"></div>
    </div>
    <div class="actions"><button name="action" value="batch_llm_second_pass">Run selected Gemini extraction</button></div>
</div>"""


def render_import(query: dict[str, list[str]]) -> str:
    success_messages = [line for line in query_value(query, "import_success").splitlines() if line.strip()]
    warning_messages = [line for line in query_value(query, "import_warning").splitlines() if line.strip()]
    supported = ", ".join(sorted(IMPORTABLE_EXTENSIONS))
    body = ["""<section class="panel page-intro help-text">
  <h2>Import Invoices</h2>
  <p>Add PDF or image invoices to the review workflow. Imported files are stored as raw sources, then OCR and structured extraction run automatically.</p>
</section>"""]
    if success_messages or warning_messages:
        body.append("<div class='result-stack'>")
        body.extend(notice(message, "success") for message in success_messages)
        body.extend(notice(message, "danger") for message in warning_messages)
        body.append("</div>")
    job_id = query_value(query, "job")
    if job_id:
        body.append(render_batch_progress_panel(job_id))
    body.append(
        f"""<div class="import-layout">
  <form class="panel upload-card" method="post" action="/import" enctype="multipart/form-data">
    <h3>Select invoices</h3>
    <p class="muted">Choose one or more files. Existing invoice names are skipped to avoid accidental duplicates.</p>
    <label for="invoice_files">Files</label>
    <input id="invoice_files" name="invoice_files" type="file" multiple>
    <p class="muted">Supported file formats: {e(supported)}</p>
    <h3>Gemini PDF extraction</h3>
    <p class="muted">Optional. When provided, Gemini fields are applied to Manual Review after OCR.</p>
    <label for="gemini_api_key">Gemini API key</label>
    <input id="gemini_api_key" name="gemini_api_key" type="password" autocomplete="off">
    <label for="gemini_model">Gemini model</label>
    <input id="gemini_model" name="gemini_model" type="text" value="gemini-3.5-flash">
    <p class="muted">The key is used only for this import request and is not saved.</p>
    <div class="actions"><button>Import invoices</button></div>
  </form>
</div>"""
    )
    return "".join(body)


def invoice_selector(rows: list[dict[str, str]], selected_index: int, view: str) -> str:
    options = []
    for index, row in enumerate(rows):
        label = f"{Path(row.get('source_file', '')).name} | {row.get('provider_name', 'null')} | {row.get('invoice_number', 'null')}"
        selected = " selected" if index == selected_index else ""
        options.append(f"<option value='{index}'{selected}>{e(label)}</option>")
    return f"""<form method="get" action="/" class="panel">
  <input type="hidden" name="view" value="{e(view)}">
  <label for="invoice">Invoice</label>
  <select id="invoice" name="invoice" onchange="this.form.submit()">{''.join(options)}</select>
</form>"""


def render_review(rows: list[dict[str, str]], kb: dict[str, Any], selected_index: int) -> str:
    if not rows:
        return empty_state(
            "No invoices to review yet",
            "Import invoices or run batch extraction first. Reviewed corrections will update provider memory automatically.",
            f"<a class='nav-link' href='{query_path('import')}'>Import invoices</a><a class='nav-link' href='{query_path('overview')}'>Go to Overview</a>",
        )
    selected_index = max(0, min(selected_index, len(rows) - 1))
    row = rows[selected_index]
    state = review_state(row, kb)
    body = [invoice_selector(rows, selected_index, "review")]
    left = []
    kind = "success" if state.pipeline_status == "approved" else "warning" if state.validation_errors else "info"
    left.append(notice(f"{status_label(state)}. Provider has {state.validated_count} validated invoice memories.", kind))
    left.append(review_metrics_panel(state))
    if state.validation_errors:
        left.append("<section class='panel'><h2>Validation Errors</h2><ul>")
        left.extend(f"<li>{e(error)}</li>" for error in state.validation_errors)
        left.append("</ul></section>")
    left.append(llm_summary_panel(row))
    left.append(agentic_ab_panel(row, selected_index))

    fields_html = []
    for group_name, fields in FIELD_GROUPS.items():
        fields_html.append(f"<fieldset><legend>{e(group_name)}</legend><div class='field-grid'>")
        for field_name, label, description in fields:
            fields_html.append(
                f"<div><label title='{e(description)}' for='{e(field_name)}'>{e(label)}</label>"
                f"<input id='{e(field_name)}' name='{e(field_name)}' type='text' value='{e(row.get(field_name, 'null'))}'></div>"
            )
        fields_html.append("</div></fieldset>")
    checked = " checked" if parse_bool(row.get("valid_invoice")) else ""
    left.append(
        f"""<form method="post" action="/action" class="panel">
  <input type="hidden" name="action" value="save_review">
  <input type="hidden" name="invoice" value="{selected_index}">
  {''.join(fields_html)}
  <fieldset>
    <legend>Review Decision</legend>
    <p class="muted">Save corrections to teach provider memory. Approval clears warnings and records this invoice as a validated example.</p>
    <label><input type="checkbox" name="valid_invoice" value="true"{checked}> Valid invoice</label>
    <label for="extraction_warnings">Extraction warnings</label>
    <textarea id="extraction_warnings" name="extraction_warnings">{e('' if is_null(row.get('extraction_warnings')) else row.get('extraction_warnings', ''))}</textarea>
    <label for="note">Reviewer note</label>
    <textarea id="note" name="note"></textarea>
  </fieldset>
  <div class="actions">
    <button>Save corrections to memory</button>
  </div>
</form>"""
    )
    left.append(
        f"""<div class="panel actions">
  <form method="post" action="/action"><input type="hidden" name="action" value="approve"><input type="hidden" name="invoice" value="{selected_index}"><button>Approve invoice</button></form>
  <form method="post" action="/action"><input type="hidden" name="action" value="reject"><input type="hidden" name="invoice" value="{selected_index}"><button class="danger">Reject invoice</button></form>
  <form method="post" action="/action"><input type="hidden" name="action" value="re_extract"><input type="hidden" name="invoice" value="{selected_index}"><button class="secondary">Re-extract selected OCR</button></form>
  <form method="post" action="/action"><input type="hidden" name="action" value="apply_latest_llm"><input type="hidden" name="invoice" value="{selected_index}"><button class="secondary">Apply latest Gemini fields</button></form>
</div>"""
    )
    left.append(
        f"""<section class="panel">
  <h2>Run Gemini PDF Pass</h2>
  <form method="post" action="/action">
    <input type="hidden" name="action" value="run_llm_second_pass">
    <input type="hidden" name="invoice" value="{selected_index}">
    <div class="field-grid">
      <div><label for="review_gemini_api_key">Gemini API key</label><input id="review_gemini_api_key" name="gemini_api_key" type="password" autocomplete="off" required></div>
      <div><label for="review_gemini_model">Gemini model</label><input id="review_gemini_model" name="gemini_model" type="text" value="gemini-3.5-flash"></div>
    </div>
    <p class="muted">The key is used only for this request and is not saved.</p>
    <div class="actions"><button class="secondary">Run Gemini PDF pass</button></div>
  </form>
</section>"""
    )
    pdf_name = source_pdf_name(row)
    pdf_path = source_pdf_path(row)
    if pdf_path.exists():
        pdf_view = f"""<section class="panel pdf-pane">
  <div class="pdf-toolbar">
    <div class="pdf-title">{e(pdf_name)}</div>
    <a class="nav-link" href="/pdf?invoice={selected_index}" target="_blank">Open PDF</a>
  </div>
  <iframe class="pdf-frame" title="Invoice PDF preview" src="/pdf?invoice={selected_index}"></iframe>
</section>"""
    else:
        pdf_view = f"""<section class="panel pdf-pane">
  <div class="pdf-toolbar"><div class="pdf-title">{e(pdf_name)}</div></div>
  {notice("PDF file was not found in data/data_pdf.", "warning")}
</section>"""
    body.append(f"<div class='review-layout'><div class='review-fields'>{''.join(left)}</div>{pdf_view}</div>")
    return "".join(body)


def render_rag_provider_context(row: dict[str, str], kb: dict[str, Any], state: ReviewState) -> str:
    provider = kb.get("providers", {}).get(state.provider_id)
    provider_name = provider.get("provider_name", provider_display_name(state.provider_id)) if provider else provider_display_name(state.provider_id)
    invoice_type = row.get("invoice_type", "")
    present_fields = {field for field in RAG_FIELD_NAMES if not is_null(row.get(field))}

    if not provider:
        return (
            "<section class='panel'><h2>Provider Context</h2>"
            f"<p class='muted'>No provider memory is saved yet for {e(provider_name)}. Approve invoices or save corrections to build provider-specific context.</p>"
            "</section>"
        )

    tips = provider.get("provider_specific_extraction_tips", [])[:5]
    corrections = list(provider.get("common_ocr_corrections", {}).items())[:5]
    layouts = [
        layout
        for layout in provider.get("known_invoice_layouts", [])
        if not invoice_type or layout.get("invoice_type") == invoice_type
    ][:3]
    patterns = [
        (field_name, pattern)
        for field_name, pattern in provider.get("field_correction_patterns", {}).items()
        if field_name in present_fields
    ][:5]

    section = [
        "<section class='panel'><h2>Provider Context</h2>",
        "<div class='grid metrics'>",
        f"<section class='panel metric'><div class='label'>Provider</div><div class='value'>{e(provider_name)}</div></section>",
        f"<section class='panel metric'><div class='label'>Provider ID</div><div class='value'>{e(state.provider_id)}</div></section>",
        f"<section class='panel metric'><div class='label'>Approved examples</div><div class='value'>{state.validated_count}</div></section>",
        "</div>",
        "<div class='evidence-grid'>",
        "<div><h3>Review Tips</h3><ul class='summary-list'>",
    ]
    if tips:
        section.extend(f"<li>{e(tip)}</li>" for tip in tips)
    else:
        section.append("<li>No provider tips saved yet.</li>")
    section.append("</ul></div>")

    section.append("<div><h3>OCR Corrections</h3><ul class='summary-list'>")
    if corrections:
        section.extend(f"<li>{e(wrong)} -> {e(right)}</li>" for wrong, right in corrections)
    else:
        section.append("<li>No OCR corrections saved yet.</li>")
    section.append("</ul></div>")

    section.append("<div><h3>Known Layouts For This Type</h3><ul class='summary-list'>")
    if layouts:
        for layout in layouts:
            fields = ", ".join(str(field) for field in layout.get("fields_seen", [])[:8])
            section.append(f"<li>{e(layout.get('invoice_type', 'invoice'))}: {e(fields or 'no fields recorded')}</li>")
    else:
        section.append("<li>No matching known layouts saved for this invoice type.</li>")
    section.append("</ul></div>")

    section.append("<div><h3>Relevant Field Corrections</h3><ul class='summary-list'>")
    if patterns:
        for field_name, pattern in patterns:
            examples = pattern.get("recent_examples", [])[:2]
            example_text = "; ".join(
                f"{item.get('old_value', 'null')} -> {item.get('corrected_value', 'null')}"
                for item in examples
            )
            section.append(
                f"<li>{e(field_name)}: {e(pattern.get('total_corrections', 0))} correction(s). {e(example_text or 'No recent examples.')}</li>"
            )
    else:
        section.append("<li>No field correction patterns match the fields currently present on this invoice.</li>")
    section.append("</ul></div></div></section>")
    return "".join(section)


def render_rag(rows: list[dict[str, str]], kb: dict[str, Any], selected_index: int) -> str:
    if not rows:
        return empty_state(
            "No invoice memory to inspect yet",
            "RAG context becomes useful after invoices have been extracted, reviewed, and saved to provider memory.",
            f"<a class='nav-link' href='{query_path('import')}'>Import invoices</a>",
        )
    selected_index = max(0, min(selected_index, len(rows) - 1))
    row = rows[selected_index]
    state = review_state(row, kb)
    text = safe_read_text(row.get("ocr_text_file", ""))
    detail_rows = [
        [e(field), e(row.get(field, "null"))]
        for field in FIELDNAMES
    ]
    body = [
        invoice_selector(rows, selected_index, "rag"),
        """<section class="panel help-text">
  <h2>Selected Invoice Details</h2>
  <p>This page is scoped to the currently selected invoice. Provider-wide memory, historical corrections, and examples live in Provider Memory.</p>
</section>""",
        f"<div class='grid metrics'><section class='panel metric'><div class='label'>Invoice status</div><div class='value'>{e(status_label(state))}</div></section>"
        f"<section class='panel metric'><div class='label'>Validation errors</div><div class='value'>{len(state.validation_errors)}</div></section>"
        f"<section class='panel metric'><div class='label'>Fields completed</div><div class='value'>{round(state.completion * 100)}%</div></section></div>",
        "<section class='panel'><h2>Extracted Fields</h2>",
        table(["Field", "Value"], detail_rows, "No extracted fields are available."),
        "</section>",
    ]
    body.append(render_rag_provider_context(row, kb, state))
    body.append(llm_summary_panel(row))
    if state.validation_errors:
        body.append("<section class='panel'><h2>Validation Errors</h2><ul>")
        body.extend(f"<li>{e(error)}</li>" for error in state.validation_errors)
        body.append("</ul></section>")
    else:
        body.append(notice("No validation errors are currently detected for this invoice.", "success"))
    if not text:
        body.append(notice("No OCR text file is available for this invoice.", "info"))
        return "".join(body)
    body.append(
        f"""<details>
  <summary>Show extracted text from this invoice</summary>
  <p class="muted">This is the text read from the PDF. It is useful when a field looks wrong in Manual Review.</p>
  <pre>{e(text[:12000])}</pre>
</details>"""
    )
    return "".join(body)


def json_table(items: list[dict[str, Any]]) -> str:
    if not items:
        return "<p class='muted'>No entries.</p>"
    headers = sorted({key for item in items for key in item.keys()})
    rows = []
    for item in items:
        rows.append([e(json.dumps(item.get(header, ""), ensure_ascii=False)) for header in headers])
    return table(headers, rows)


def render_memory(kb: dict[str, Any], selected_provider: str = "") -> str:
    summary = provider_summary(kb)
    if not selected_provider and summary:
        selected_provider = summary[0]["provider_id"]
    body = [
        """<section class="panel help-text">
  <h2>What The System Remembers</h2>
  <p>This page shows the notes, past examples, and text corrections the dashboard has learned for each provider. These memories help future invoices from the same provider get reviewed faster and more consistently.</p>
</section>""",
        "<h2>Choose A Provider</h2>",
    ]
    if not summary:
        body.append(
            empty_state(
                "Provider memory is empty",
                "Run extraction, approve invoices, or save manual corrections to build reusable provider knowledge.",
                f"<a class='nav-link' href='{query_path('overview')}'>Go to Overview</a>",
            )
        )
        return "".join(body)
    body.append(
        table(
            ["Provider", "Display name", "Approved examples", "Review tips", "Text corrections", "Known layouts", "Human corrections", "History"],
            [
                [
                    f"<a href='{query_path('memory', provider=row['provider_id'])}'>{e(row['provider_id'])}</a>",
                    e(row["provider_name"]),
                    e(row["validated_invoices"]),
                    e(row["tips"]),
                    e(row["ocr_corrections"]),
                    e(row["known_layouts"]),
                    e(row["human_feedback"]),
                    e(row["validation_events"]),
                ]
                for row in summary
            ],
        )
    )
    provider = kb.get("providers", {}).get(selected_provider)
    if not provider:
        return "".join(body)

    tips = provider.get("provider_specific_extraction_tips", [])
    corrections = provider.get("common_ocr_corrections", {})
    layouts = provider.get("known_invoice_layouts", [])
    feedback = provider.get("human_reviewer_feedback", [])
    history = provider.get("validation_history", [])
    examples = provider.get("previously_validated_invoices", [])

    body.append(f"<h2>{e(provider.get('provider_name', selected_provider))}</h2>")
    body.append(
        f"<div class='grid metrics'><section class='panel metric'><div class='label'>Approved examples</div><div class='value'>{e(len(examples))}</div></section>"
        f"<section class='panel metric'><div class='label'>Review tips</div><div class='value'>{e(len(tips))}</div></section>"
        f"<section class='panel metric'><div class='label'>Text corrections</div><div class='value'>{e(len(corrections))}</div></section>"
        f"<section class='panel metric'><div class='label'>Known layouts</div><div class='value'>{e(len(layouts))}</div></section></div>"
    )

    body.append("<div class='grid two'><section class='panel'><h3>Review Tips</h3>")
    body.append("<p class='muted'>Short notes that help reviewers know what to look for on this provider's invoices.</p>")
    if tips:
        body.append("<ul class='tip-list'>")
        for index, tip in enumerate(tips):
            body.append(
                f"""<li class="tip-row"><span>{e(tip)}</span>
<form method="post" action="/action">
  <input type="hidden" name="action" value="remove_tip">
  <input type="hidden" name="provider" value="{e(selected_provider)}">
  <input type="hidden" name="tip_index" value="{index}">
  <button class="danger">Remove</button>
</form></li>"""
            )
        body.append("</ul>")
    else:
        body.append("<p class='muted'>No tips saved yet.</p>")

    body.append(
        f"""<form method="post" action="/action">
  <input type="hidden" name="action" value="add_tip">
  <input type="hidden" name="provider" value="{e(selected_provider)}">
  <label for="tip">Add a review tip</label>
  <input id="tip" name="tip" type="text">
  <div class="actions"><button>Add tip</button></div>
</form></section><section class='panel'><h3>Saved Text Corrections</h3>
<p class="muted">Words or symbols that OCR often reads incorrectly, with the replacement the system should use.</p>"""
    )
    if corrections:
        body.append(
            table(
                ["OCR reads", "Use instead"],
                [[e(wrong), e(right)] for wrong, right in corrections.items()],
            )
        )
    else:
        body.append("<p class='muted'>No text corrections saved yet.</p>")
    body.append(
        f"""<form method="post" action="/action">
  <input type="hidden" name="action" value="add_correction">
  <input type="hidden" name="provider" value="{e(selected_provider)}">
  <div class="field-grid"><div><label for="wrong">OCR text</label><input id="wrong" name="wrong" type="text"></div>
  <div><label for="right">Replacement</label><input id="right" name="right" type="text"></div></div>
  <div class="actions"><button>Add correction</button></div>
</form></section></div>"""
    )
    body.append("<h2>Detailed Memory</h2>")
    body.append("<details open><summary>Known invoice layouts</summary>" + json_table(layouts) + "</details>")
    body.append("<details><summary>Human corrections</summary>" + json_table(feedback) + "</details>")
    body.append("<details><summary>Validation history</summary>" + json_table(history) + "</details>")
    return "".join(body)


def render_schema() -> str:
    body = [
        """<section class="panel page-intro help-text">
  <h2>Required Extraction Schema</h2>
  <p>These fields define the structured CSV and Manual Review form. Approval-critical fields must be present before an invoice can be automatically approved.</p>
</section>"""
    ]
    for group_name, fields in FIELD_GROUPS.items():
        body.append(f"<details open><summary>{e(group_name)}</summary>")
        body.append(
            table(
                ["Field", "Key", "Description", "Approval"],
                [
                    [
                        e(label),
                        f"<code>{e(field_name)}</code>",
                        e(description),
                        e("Approval-critical" if field_name in CRITICAL_FIELDS else "Captured when available"),
                    ]
                    for field_name, label, description in fields
                ],
            )
        )
        body.append("</details>")
    return "".join(body)


def render_export(config: DashboardConfig) -> str:
    kb_exists = config.kb_path.exists()
    kb_download = '<a class="nav-link" href="/download?file=kb">Download RAG knowledge base</a>' if kb_exists else ""
    kb_note = "The RAG knowledge base is available for download." if kb_exists else "The RAG knowledge base has not been created yet. Refresh provider memory from Overview first."
    return f"""<section class="panel page-intro help-text">
  <h2>Machine-readable Outputs</h2>
  <p>Download the structured invoice CSV for downstream review or export the provider memory for backup and inspection.</p>
</section>
<div class="panel">
  <p class="muted">CSV path: {e(str(config.csv_path))}</p>
  <p class="muted">{e(kb_note)}</p>
  <div class="actions">
    <a class="nav-link" href="/download?file=csv">Download structured CSV</a>
    {kb_download}
  </div>
</div>"""


def render_page(view: str, query: dict[str, list[str]], config: DashboardConfig, message: str = "") -> bytes:
    rows = load_dashboard_rows(config)
    kb = load_kb(config.kb_path)
    if not rows and view not in {"schema", "import"}:
        body = empty_state(
            "No structured invoice rows yet",
            "Import invoices or run batch extraction to create the review queue. The dashboard will use OCR text files when available.",
            f"<a class='nav-link' href='{query_path('import')}'>Import invoices</a>",
        )
        body += render_pipeline_forms()
        return render_layout("Invoice Extraction Dashboard", view, body, config, message)

    selected_index = int(query.get("invoice", ["0"])[0] or 0)
    if view == "import":
        body = render_import(query)
    elif view == "review":
        body = render_review(rows, kb, selected_index)
    elif view == "rag":
        body = render_rag(rows, kb, selected_index)
    elif view == "memory":
        body = render_memory(kb, query.get("provider", [""])[0])
    elif view == "schema":
        body = render_schema()
    elif view == "export":
        body = render_export(config)
    else:
        body = render_overview(rows, kb, query)
    return render_layout("Invoice Extraction Dashboard", view, body, config, message)


def first_value(form: dict[str, list[str]], key: str, default: str = "") -> str:
    return form.get(key, [default])[0]


def safe_filename(filename: str) -> str:
    name = Path((filename or "invoice").replace("\\", "/")).name
    stem = re.sub(r"[^A-Za-z0-9._ -]+", "_", Path(name).stem).strip(" ._") or "invoice"
    suffix = re.sub(r"[^A-Za-z0-9.]+", "", Path(name).suffix.lower())
    return f"{stem}{suffix}"


def existing_processed_invoice_stems(rows: list[dict[str, str]]) -> set[str]:
    stems: set[str] = set()
    for directory, pattern in [
        (DEFAULT_PDF_DIR, "*.pdf"),
        (DEFAULT_TEXT_DIR, "*.txt"),
    ]:
        if directory.exists():
            stems.update(path.stem.lower() for path in directory.glob(pattern) if path.is_file())
    for row in rows:
        for field in ["source_file", "ocr_text_file"]:
            value = row.get(field, "")
            if not is_null(value):
                stems.add(Path(value).stem.lower())
        pdf_name = source_pdf_name(row)
        if not is_null(pdf_name):
            stems.add(Path(pdf_name).stem.lower())
    return stems


def existing_raw_invoice_paths() -> dict[str, Path]:
    if not DEFAULT_RAW_DIR.exists():
        return {}
    return {path.name.lower(): path for path in DEFAULT_RAW_DIR.glob("*") if path.is_file()}


def parse_import_payload(content_type: str, payload: bytes) -> tuple[list[tuple[str, bytes]], dict[str, str]]:
    if "multipart/form-data" not in content_type:
        return [], {}
    message = BytesParser(policy=default).parsebytes(
        f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("utf-8") + payload
    )
    files: list[tuple[str, bytes]] = []
    fields: dict[str, str] = {}
    for part in message.iter_parts():
        field_name = part.get_param("name", header="content-disposition")
        if field_name == "invoice_files":
            filename = part.get_filename()
            data = part.get_payload(decode=True) or b""
            if filename and data:
                files.append((filename, data))
            continue
        if field_name:
            fields[field_name] = part.get_content().strip()
    return files, fields


def process_imported_files(
    files: list[tuple[str, bytes]],
    config: DashboardConfig,
    gemini_api_key: str = "",
    gemini_model: str = "",
    job_id: str | None = None,
) -> tuple[list[str], list[str]]:
    if not files:
        return [], ["No files selected."]

    try:
        from scripts.ocr_text_extraction import process_file
    except ModuleNotFoundError as exc:
        missing_module = exc.name or str(exc)
        if job_id:
            for item_index in range(len(files)):
                update_batch_item(job_id, item_index, "error", "OCR dependency missing.")
        return [], [
            f"OCR import dependency is missing: {missing_module}. "
            "Install project requirements with the same Python used to run the dashboard."
        ]
    except Exception as exc:
        if job_id:
            for item_index in range(len(files)):
                update_batch_item(job_id, item_index, "error", "OCR pipeline could not load.")
        return [], [f"OCR pipeline could not load: {exc}"]

    existing_stems = existing_processed_invoice_stems(load_dashboard_rows(config))
    raw_paths_by_name = existing_raw_invoice_paths()
    saved_paths: list[tuple[int, Path]] = []
    warning_messages: list[str] = []
    warning_stems: set[str] = set()
    raw_existing_stems: set[str] = set()
    uploaded_stems: set[str] = set()
    for item_index, (filename, data) in enumerate(files):
        safe_name = safe_filename(filename)
        if job_id:
            update_batch_job(job_id, current=f"Importing {safe_name}")
        upload_stem = Path(safe_name).stem.lower()
        if upload_stem in existing_stems or upload_stem in uploaded_stems:
            warning_messages.append(f"{safe_name} is already in the system. It was not imported.")
            warning_stems.add(upload_stem)
            if job_id:
                update_batch_item(job_id, item_index, "skipped", "Already in the system.")
            continue
        uploaded_stems.add(upload_stem)
        destination = raw_paths_by_name.get(safe_name.lower())
        if destination is None:
            destination = DEFAULT_RAW_DIR / safe_name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
        else:
            raw_existing_stems.add(upload_stem)
        saved_paths.append((item_index, destination))
        if destination.suffix.lower() not in IMPORTABLE_EXTENSIONS:
            warning_messages.append(f"{destination.name} was imported, but this file format cannot be processed.")
            warning_stems.add(destination.stem.lower())
            if job_id:
                update_batch_item(job_id, item_index, "warning", "Unsupported file format.")
            continue
        result = process_file(destination)
        if result.errors:
            warning_messages.append(f"{destination.name} was imported, but OCR needs review.")
            warning_stems.add(destination.stem.lower())
            if job_id:
                update_batch_item(job_id, item_index, "warning", "OCR needs review.")

    if not saved_paths:
        return [], warning_messages

    rows = extract_batch(DEFAULT_TEXT_DIR, config.csv_path)

    imported_stems = {path.stem for _, path in saved_paths}
    rows_by_stem = {
        Path(row.get("ocr_text_file", "")).stem: (index, row)
        for index, row in enumerate(rows)
        if Path(row.get("ocr_text_file", "")).stem in imported_stems
    }
    success_messages: list[str] = []
    gemini_enabled = bool(gemini_api_key.strip())
    for item_index, path in saved_paths:
        row_entry = rows_by_stem.get(path.stem)
        row = row_entry[1] if row_entry else None
        display_name = f"{path.stem}.pdf" if path.suffix.lower() != ".pdf" else path.name
        if row and row.get("invoice_type") == "unsupported":
            warning_messages.append(f"{display_name} was imported classified unsupported.")
            warning_stems.add(path.stem.lower())
            if job_id:
                update_batch_item(job_id, item_index, "warning", "Classified unsupported.")
        elif row and path.stem.lower() not in warning_stems:
            if path.stem.lower() in raw_existing_stems:
                success_messages.append(f"{path.name} already existed in raw data and was processed successfully.")
            else:
                success_messages.append(f"{display_name} was imported successfully.")
            if job_id:
                update_batch_item(job_id, item_index, "success", "OCR and fields extracted.")
        elif path.stem.lower() not in warning_stems:
            warning_messages.append(f"{display_name} was imported, but fields were not extracted.")
            warning_stems.add(path.stem.lower())
            if job_id:
                update_batch_item(job_id, item_index, "warning", "Fields were not extracted.")

        if gemini_enabled and row_entry:
            index, current_row = row_entry
            if job_id:
                update_batch_job(job_id, current=f"Gemini extraction: {display_name}")
            pdf_path = source_pdf_path(current_row)
            if not pdf_path.exists():
                warning_messages.append(f"{display_name} skipped Gemini extraction because the PDF was not found: {source_pdf_name(current_row)}.")
                if job_id and path.stem.lower() not in warning_stems:
                    update_batch_item(job_id, item_index, "warning", "Gemini PDF was not found.")
                continue
            try:
                from scripts.second_pass_llm import second_pass_extract
            except Exception as exc:
                warning_messages.append(f"{display_name} skipped Gemini extraction because the LLM module could not load: {exc}")
                if job_id and path.stem.lower() not in warning_stems:
                    update_batch_item(job_id, item_index, "warning", "Gemini module could not load.")
                continue
            result = second_pass_extract(
                ocr_text=safe_read_text(current_row.get("ocr_text_file", "")),
                pdf_file=pdf_path,
                provider_name=None if is_null(current_row.get("provider_name")) else current_row.get("provider_name"),
                invoice_type=None if is_null(current_row.get("invoice_type")) else current_row.get("invoice_type"),
                source_file=pdf_path,
                api_key=gemini_api_key.strip(),
                model=gemini_model.strip() or None,
                kb_path=config.kb_path,
            )
            if result.errors:
                warning_messages.append(
                    f"{display_name} Gemini extraction failed with model {gemini_model.strip() or 'gemini-3.5-flash'}: {' | '.join(result.errors)}"
                )
                if job_id and path.stem.lower() not in warning_stems:
                    update_batch_item(job_id, item_index, "warning", "Gemini extraction failed.")
                continue
            artifact_path = Path(result.normalized_output_path) if result.normalized_output_path else None
            rows[index] = apply_llm_structured_to_row(current_row, result.parsed, artifact_path)
            success_messages.append(
                f"{display_name} Gemini PDF extraction was applied to Manual Review. "
                f"Artifact: {artifact_path.name if artifact_path else 'not saved'}; "
                f"review status: {result.parsed.get('review_status', 'unknown')}; "
                f"errors: {len(result.parsed.get('validation_errors', []))}."
            )
            if job_id:
                if path.stem.lower() in warning_stems:
                    update_batch_item(job_id, item_index, "warning", "Gemini applied; earlier warnings remain.")
                else:
                    update_batch_item(job_id, item_index, "success", "OCR, fields, and Gemini applied.")

    write_rows(config.csv_path, rows)
    seed_from_validated_csv(config.csv_path, config.kb_path)

    return success_messages, warning_messages


def run_import_job(
    job_id: str,
    files: list[tuple[str, bytes]],
    fields: dict[str, str],
    config: DashboardConfig,
) -> None:
    try:
        success_messages, warning_messages = process_imported_files(
            files,
            config,
            gemini_api_key=fields.get("gemini_api_key", ""),
            gemini_model=fields.get("gemini_model", ""),
            job_id=job_id,
        )
    except Exception as exc:
        mark_unfinished_batch_items(job_id, "error", "Import stopped before this file completed.")
        update_batch_job(job_id, status="error", current="", summary=f"Import failed: {exc}")
        return

    status = "complete"
    summary = f"Import complete: {len(success_messages)} success message(s), {len(warning_messages)} warning(s)."
    update_batch_job(job_id, status=status, current="", summary=summary)


def run_batch_gemini_extraction(
    rows: list[dict[str, str]],
    config: DashboardConfig,
    gemini_api_key: str,
    gemini_model: str = "",
) -> tuple[list[dict[str, str]], list[str], list[str]]:
    if not rows:
        return rows, [], ["No invoice rows are available."]
    if not gemini_api_key.strip():
        return rows, [], ["Enter a Gemini API key before running batch Gemini extraction."]

    try:
        from scripts.second_pass_llm import second_pass_extract
    except Exception as exc:
        return rows, [], [f"Gemini extraction could not load the second-pass module: {exc}"]

    updated_rows = [dict(row) for row in rows]
    success_messages: list[str] = []
    warning_messages: list[str] = []
    model = gemini_model.strip() or "gemini-3.5-flash"

    for index, row in enumerate(rows):
        display_name = source_pdf_name(row)
        pdf_path = source_pdf_path(row)
        if not pdf_path.exists():
            warning_messages.append(f"{display_name} skipped because the PDF was not found.")
            continue

        result = second_pass_extract(
            ocr_text=safe_read_text(row.get("ocr_text_file", "")),
            pdf_file=pdf_path,
            provider_name=None if is_null(row.get("provider_name")) else row.get("provider_name"),
            invoice_type=None if is_null(row.get("invoice_type")) else row.get("invoice_type"),
            source_file=pdf_path,
            api_key=gemini_api_key.strip(),
            model=model,
            kb_path=config.kb_path,
        )
        if result.errors:
            warning_messages.append(f"{display_name} Gemini extraction failed: {' | '.join(result.errors)}")
            continue

        artifact_path = Path(result.normalized_output_path) if result.normalized_output_path else None
        updated_rows[index] = apply_llm_structured_to_row(row, result.parsed, artifact_path)
        success_messages.append(
            f"{display_name} applied Gemini fields; review status: {result.parsed.get('review_status', 'unknown')}; "
            f"errors: {len(result.parsed.get('validation_errors', []))}."
        )

    if success_messages:
        write_rows(config.csv_path, updated_rows)
        seed_from_validated_csv(config.csv_path, config.kb_path)

    return updated_rows, success_messages, warning_messages


def selected_labels(rows: list[dict[str, str]], selected_indices: list[int]) -> list[str]:
    return [source_pdf_name(rows[index]) for index in selected_indices if 0 <= index < len(rows)]


def start_selected_ocr_batch(rows: list[dict[str, str]], selected_indices: list[int], config: DashboardConfig) -> str:
    job_id = create_batch_job("ocr", selected_labels(rows, selected_indices))
    Thread(target=run_selected_ocr_batch_job, args=(job_id, selected_indices, config), daemon=True).start()
    return job_id


def start_selected_gemini_batch(
    rows: list[dict[str, str]],
    selected_indices: list[int],
    config: DashboardConfig,
    gemini_api_key: str,
    gemini_model: str,
) -> str:
    job_id = create_batch_job("gemini", selected_labels(rows, selected_indices))
    Thread(
        target=run_selected_gemini_batch_job,
        args=(job_id, selected_indices, config, gemini_api_key, gemini_model),
        daemon=True,
    ).start()
    return job_id


def resolve_source_path(path_value: str) -> Path | None:
    if is_null(path_value):
        return None
    source_path = Path(path_value)
    if source_path.exists():
        return source_path
    fallback = DEFAULT_RAW_DIR / source_path.name
    return fallback if fallback.exists() else None


def run_selected_ocr_batch_job(job_id: str, selected_indices: list[int], config: DashboardConfig) -> None:
    success_count = 0
    warning_count = 0
    try:
        from scripts.ocr_text_extraction import process_file
    except Exception as exc:
        update_batch_job(job_id, status="error", summary=f"OCR pipeline could not load: {exc}", current="")
        return

    rows = load_dashboard_rows(config)
    for item_index, row_index in enumerate(selected_indices):
        if row_index >= len(rows):
            update_batch_item(job_id, item_index, "error", "Row no longer exists.")
            warning_count += 1
            continue
        row = rows[row_index]
        label = source_pdf_name(row)
        update_batch_job(job_id, current=f"OCR extraction: {label}")

        ocr_path = Path(row.get("ocr_text_file", ""))
        if not is_null(row.get("ocr_text_file")) and ocr_path.exists():
            update_batch_item(job_id, item_index, "skipped", "OCR text already exists.")
            continue

        source_path = resolve_source_path(row.get("source_file", ""))
        if source_path is None:
            update_batch_item(job_id, item_index, "error", "Source file was not found.")
            warning_count += 1
            continue

        result = process_file(source_path)
        if result.errors:
            update_batch_item(job_id, item_index, "warning", "OCR finished with errors; review needed.")
            warning_count += 1
            continue
        if not result.selected_text_file or not Path(result.selected_text_file).exists():
            update_batch_item(job_id, item_index, "error", "OCR did not create a selected text file.")
            warning_count += 1
            continue

        rows[row_index] = extract_row(Path(result.selected_text_file))
        write_rows(config.csv_path, rows)
        update_batch_item(job_id, item_index, "success", "OCR text and fields updated.")
        success_count += 1

    if success_count:
        seed_from_validated_csv(config.csv_path, config.kb_path)
    update_batch_job(
        job_id,
        status="complete",
        current="",
        summary=f"OCR batch complete: {success_count} updated, {warning_count} warning(s).",
    )


def run_selected_gemini_batch_job(
    job_id: str,
    selected_indices: list[int],
    config: DashboardConfig,
    gemini_api_key: str,
    gemini_model: str,
) -> None:
    success_count = 0
    warning_count = 0
    if not gemini_api_key.strip():
        update_batch_job(job_id, status="error", summary="Gemini API key is required.", current="")
        return
    try:
        from scripts.second_pass_llm import second_pass_extract
    except Exception as exc:
        update_batch_job(job_id, status="error", summary=f"Gemini module could not load: {exc}", current="")
        return

    model = gemini_model.strip() or "gemini-3.5-flash"
    rows = load_dashboard_rows(config)
    for item_index, row_index in enumerate(selected_indices):
        if row_index >= len(rows):
            update_batch_item(job_id, item_index, "error", "Row no longer exists.")
            warning_count += 1
            continue
        row = rows[row_index]
        label = source_pdf_name(row)
        update_batch_job(job_id, current=f"Gemini extraction: {label}")

        if latest_llm_structured_path(row) is not None:
            update_batch_item(job_id, item_index, "skipped", "Gemini artifact already exists.")
            continue

        pdf_path = source_pdf_path(row)
        if not pdf_path.exists():
            update_batch_item(job_id, item_index, "error", "PDF was not found.")
            warning_count += 1
            continue

        result = second_pass_extract(
            ocr_text=safe_read_text(row.get("ocr_text_file", "")),
            pdf_file=pdf_path,
            provider_name=None if is_null(row.get("provider_name")) else row.get("provider_name"),
            invoice_type=None if is_null(row.get("invoice_type")) else row.get("invoice_type"),
            source_file=pdf_path,
            api_key=gemini_api_key.strip(),
            model=model,
            kb_path=config.kb_path,
        )
        if result.errors:
            update_batch_item(job_id, item_index, "error", " | ".join(result.errors[:2]))
            warning_count += 1
            continue

        artifact_path = Path(result.normalized_output_path) if result.normalized_output_path else None
        rows[row_index] = apply_llm_structured_to_row(row, result.parsed, artifact_path)
        write_rows(config.csv_path, rows)
        update_batch_item(job_id, item_index, "success", "Gemini fields applied.")
        success_count += 1

    if success_count:
        seed_from_validated_csv(config.csv_path, config.kb_path)
    update_batch_job(
        job_id,
        status="complete",
        current="",
        summary=f"Gemini batch complete: {success_count} updated, {warning_count} warning(s).",
    )


def handle_import_upload(content_type: str, payload: bytes, config: DashboardConfig) -> str:
    files, fields = parse_import_payload(content_type, payload)
    if not files:
        return query_path("import", import_warning="No files selected.")
    labels = [safe_filename(filename) for filename, _data in files]
    job_id = create_batch_job("import", labels)
    Thread(target=run_import_job, args=(job_id, files, fields, config), daemon=True).start()
    return query_path("import", job=job_id)


def handle_action(form: dict[str, list[str]], config: DashboardConfig) -> tuple[str, str]:
    action = first_value(form, "action")
    rows = load_dashboard_rows(config)
    invoice_index = int(first_value(form, "invoice", "0") or 0)

    if action == "batch_extract":
        selected_indices = parse_selected_indices(form, len(rows))
        if not selected_indices:
            return query_path("overview"), "ERROR: Select at least one invoice before running selected OCR extraction."
        job_id = start_selected_ocr_batch(rows, selected_indices, config)
        return query_path("overview", job=job_id), f"SUCCESS: Started OCR extraction for {len(selected_indices)} selected invoice(s)."

    if action == "seed_memory":
        seed_from_validated_csv(config.csv_path, config.kb_path)
        return query_path("memory"), "Knowledge base refreshed from structured CSV."

    if action == "batch_llm_second_pass":
        selected_indices = parse_selected_indices(form, len(rows))
        if not selected_indices:
            return query_path("overview"), "ERROR: Select at least one invoice before running selected Gemini extraction."
        if not first_value(form, "gemini_api_key").strip():
            return query_path("overview"), "ERROR: Enter a Gemini API key before running selected Gemini extraction."
        job_id = start_selected_gemini_batch(
            rows,
            selected_indices,
            config,
            gemini_api_key=first_value(form, "gemini_api_key"),
            gemini_model=first_value(form, "gemini_model", "gemini-3.5-flash"),
        )
        return query_path("overview", job=job_id), f"SUCCESS: Started Gemini extraction for {len(selected_indices)} selected invoice(s)."

    if action in {"save_review", "approve", "reject", "re_extract", "run_llm_second_pass", "apply_latest_llm", "run_agentic_ab", "record_agentic_ab_verdict"}:
        if not rows:
            return query_path("overview"), "No invoice rows are available."
        invoice_index = max(0, min(invoice_index, len(rows) - 1))
        row = rows[invoice_index]

    if action == "save_review":
        updated_row = dict(row)
        for field in RAG_FIELD_NAMES:
            updated_row[field] = normalize_cell(first_value(form, field, row.get(field, "null")))
        updated_row["valid_invoice"] = "true" if first_value(form, "valid_invoice") == "true" else "false"
        updated_row["extraction_warnings"] = normalize_cell(first_value(form, "extraction_warnings"))
        note = first_value(form, "note")
        changes = {
            field: (row.get(field, "null"), updated_row.get(field, "null"))
            for field in RAG_FIELD_NAMES + ["valid_invoice", "extraction_warnings"]
            if normalize_cell(row.get(field)) != normalize_cell(updated_row.get(field))
        }
        rows[invoice_index] = updated_row
        write_rows(config.csv_path, rows)
        record_field_feedback(updated_row, changes, note, config.kb_path)
        upsert_review_memory(updated_row, "human_review_saved", note, config.kb_path)
        return query_path("review", invoice=invoice_index), f"Saved {len(changes)} correction(s) and updated provider memory."

    if action == "approve":
        rows[invoice_index]["valid_invoice"] = "true"
        rows[invoice_index]["extraction_warnings"] = "null"
        write_rows(config.csv_path, rows)
        upsert_review_memory(rows[invoice_index], "human_approved", "Approved from dashboard.", config.kb_path)
        return query_path("review", invoice=invoice_index), "Invoice approved and stored in provider memory."

    if action == "reject":
        rows[invoice_index]["valid_invoice"] = "false"
        write_rows(config.csv_path, rows)
        upsert_review_memory(rows[invoice_index], "human_rejected", "Rejected from dashboard.", config.kb_path)
        return query_path("review", invoice=invoice_index), "Invoice rejected and validation history updated."

    if action == "re_extract":
        ocr_path = Path(row.get("ocr_text_file", ""))
        if not ocr_path.exists():
            return query_path("review", invoice=invoice_index), "OCR text file does not exist."
        rows[invoice_index] = extract_row(ocr_path)
        write_rows(config.csv_path, rows)
        upsert_review_memory(rows[invoice_index], "re_extracted", "Re-extracted from dashboard.", config.kb_path)
        return query_path("review", invoice=invoice_index), "Selected invoice was re-extracted."

    if action == "run_llm_second_pass":
        pdf_path = source_pdf_path(row)
        if not pdf_path.exists():
            return query_path("review", invoice=invoice_index), f"ERROR: Gemini PDF pass could not start. PDF file was not found: {source_pdf_name(row)}."
        gemini_api_key = first_value(form, "gemini_api_key").strip()
        gemini_model = first_value(form, "gemini_model", "gemini-3.5-flash").strip() or "gemini-3.5-flash"
        if not gemini_api_key:
            return query_path("review", invoice=invoice_index), "ERROR: Gemini PDF pass could not start. Enter a Gemini API key in the Manual Review Gemini form."
        ocr_text = safe_read_text(row.get("ocr_text_file", ""))
        try:
            from scripts.second_pass_llm import second_pass_extract
        except Exception as exc:
            return query_path("review", invoice=invoice_index), f"ERROR: Gemini PDF pass could not load the second-pass module: {exc}"
        result = second_pass_extract(
            ocr_text=ocr_text,
            pdf_file=pdf_path,
            provider_name=None if is_null(row.get("provider_name")) else row.get("provider_name"),
            invoice_type=None if is_null(row.get("invoice_type")) else row.get("invoice_type"),
            source_file=pdf_path,
            api_key=gemini_api_key,
            model=gemini_model,
            kb_path=config.kb_path,
        )
        if result.errors:
            context = [
                f"PDF: {pdf_path.name}",
                f"model: {gemini_model}",
                f"OCR context characters: {len(ocr_text)}",
                "errors: " + " | ".join(result.errors),
            ]
            if result.warnings:
                context.append("warnings: " + " | ".join(result.warnings))
            return query_path("review", invoice=invoice_index), "ERROR: Gemini PDF pass failed. " + " ; ".join(context)
        rows[invoice_index] = apply_llm_structured_to_row(row, result.parsed, Path(result.normalized_output_path) if result.normalized_output_path else None)
        write_rows(config.csv_path, rows)
        upsert_review_memory(rows[invoice_index], "gemini_second_pass_applied", "Gemini PDF extraction applied from dashboard.", config.kb_path)
        validation_count = len(result.parsed.get("validation_errors", []))
        missing_count = len(result.parsed.get("missing_fields", []))
        artifact = Path(result.normalized_output_path).name if result.normalized_output_path else "not saved"
        return (
            query_path("review", invoice=invoice_index),
            "SUCCESS: Gemini PDF pass completed. "
            f"PDF: {pdf_path.name}; model: {gemini_model}; artifact: {artifact}; "
            f"review status: {result.parsed.get('review_status', 'unknown')}; "
            f"validation errors: {validation_count}; missing fields: {missing_count}.",
        )

    if action == "run_agentic_ab":
        pdf_path = source_pdf_path(row)
        ocr_path = Path(str(row.get("ocr_text_file") or ""))
        if not pdf_path.exists():
            return query_path("review", invoice=invoice_index), f"ERROR: A/B test could not start. PDF file was not found: {source_pdf_name(row)}."
        if not ocr_path.exists():
            return query_path("review", invoice=invoice_index), "ERROR: A/B test could not start. OCR text file was not found."
        gemini_api_key = first_value(form, "gemini_api_key").strip()
        gemini_model = first_value(form, "gemini_model", "gemini-3.5-flash").strip() or "gemini-3.5-flash"
        if not gemini_api_key:
            return query_path("review", invoice=invoice_index), "ERROR: Enter a Gemini API key to run Plan B."
        try:
            from llm.agent.workflow import run_agentic_ab_test

            result = run_agentic_ab_test(
                ocr_path,
                pdf_file=pdf_path,
                kb_path=config.kb_path,
                output_dir=DEFAULT_AGENTIC_AB_DIR,
                api_key=gemini_api_key,
                model=gemini_model,
            )
        except Exception as exc:
            return query_path("review", invoice=invoice_index), f"ERROR: Agentic A/B test failed: {exc}"
        return (
            query_path("review", invoice=invoice_index),
            f"SUCCESS: A/B test saved to {Path(result.artifact_path or '').name}; "
            f"Plan A errors: {len(result.plan_a.validation_errors)}; "
            f"Plan B errors: {len(result.plan_b.validation_errors)}; human verdict required.",
        )

    if action == "record_agentic_ab_verdict":
        preferred = first_value(form, "preferred_plan", "inconclusive")
        if preferred not in {"plan_a", "plan_b", "tie", "inconclusive"}:
            return query_path("review", invoice=invoice_index), "ERROR: Invalid A/B verdict."
        artifact = agentic_ab_artifact_path(row)
        if not artifact.exists():
            return query_path("review", invoice=invoice_index), "ERROR: Run the A/B test before saving a verdict."
        try:
            from llm.agent.workflow import record_ab_verdict

            record_ab_verdict(artifact, preferred, first_value(form, "verdict_note"))
        except Exception as exc:
            return query_path("review", invoice=invoice_index), f"ERROR: A/B verdict could not be saved: {exc}"
        return query_path("review", invoice=invoice_index), "SUCCESS: Human A/B accuracy verdict saved in the experiment artifact."

    if action == "apply_latest_llm":
        data, artifact_path, error = load_llm_structured(row)
        if error or not data:
            return query_path("review", invoice=invoice_index), "WARNING: " + (error or "No Gemini structured extraction was found.")
        rows[invoice_index] = apply_llm_structured_to_row(row, data, artifact_path)
        write_rows(config.csv_path, rows)
        upsert_review_memory(rows[invoice_index], "gemini_latest_applied", "Latest Gemini extraction applied from dashboard.", config.kb_path)
        return query_path("review", invoice=invoice_index), f"SUCCESS: Latest Gemini fields were applied to the manual review form from {artifact_path.name if artifact_path else 'saved artifact'}."

    if action == "add_tip":
        provider_id = first_value(form, "provider", "unknown")
        tip = first_value(form, "tip").strip()
        if tip:
            kb = load_kb(config.kb_path)
            provider = ensure_provider(kb, provider_id)
            provider.setdefault("provider_specific_extraction_tips", []).append(tip)
            save_kb(kb, config.kb_path)
        return query_path("memory", provider=provider_id), "Provider tip saved."

    if action == "remove_tip":
        provider_id = first_value(form, "provider", "unknown")
        try:
            tip_index = int(first_value(form, "tip_index", "-1") or -1)
        except ValueError:
            return query_path("memory", provider=provider_id), "Tip could not be removed."
        kb = load_kb(config.kb_path)
        provider = kb.get("providers", {}).get(provider_id)
        tips = provider.get("provider_specific_extraction_tips", []) if provider else []
        if 0 <= tip_index < len(tips):
            removed_tip = tips.pop(tip_index)
            save_kb(kb, config.kb_path)
            return query_path("memory", provider=provider_id), f"Removed tip: {removed_tip}"
        return query_path("memory", provider=provider_id), "Tip could not be removed."

    if action == "add_correction":
        provider_id = first_value(form, "provider", "unknown")
        wrong = first_value(form, "wrong").strip()
        right = first_value(form, "right")
        if wrong:
            kb = load_kb(config.kb_path)
            provider = ensure_provider(kb, provider_id)
            provider.setdefault("common_ocr_corrections", {})[wrong] = right
            save_kb(kb, config.kb_path)
        return query_path("memory", provider=provider_id), "OCR correction saved."

    return query_path("overview"), "No action was applied."


class DashboardHandler(BaseHTTPRequestHandler):
    config: DashboardConfig

    def send_html(self, payload: bytes, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def send_json(self, payload: dict[str, Any], status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def redirect(self, path: str, message: str = "") -> None:
        if message:
            separator = "&" if "?" in path else "?"
            path = f"{path}{separator}{urlencode({'message': message})}"
        self.send_response(303)
        self.send_header("Location", path)
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if parsed.path == "/job-status":
            job_id = query.get("id", [""])[0]
            job = batch_job_snapshot(job_id)
            if not job:
                self.send_json({"status": "missing", "total": 0, "completed": 0, "items": []}, 404)
                return
            self.send_json(job)
            return
        if parsed.path == "/download":
            self.send_download(query)
            return
        if parsed.path == "/pdf":
            self.send_pdf(query)
            return
        view = query.get("view", ["overview"])[0]
        message = query.get("message", [""])[0]
        payload = render_page(view, query, self.config, message)
        self.send_html(payload)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        payload = self.rfile.read(length)
        parsed = urlparse(self.path)
        if parsed.path == "/import":
            path = handle_import_upload(self.headers.get("Content-Type", ""), payload, self.config)
            self.redirect(path)
            return
        raw = payload.decode("utf-8", errors="replace")
        form = parse_qs(raw)
        path, message = handle_action(form, self.config)
        self.redirect(path, message)

    def send_download(self, query: dict[str, list[str]]) -> None:
        file_kind = query.get("file", ["csv"])[0]
        path = self.config.kb_path if file_kind == "kb" else self.config.csv_path
        if not path.exists():
            self.send_error(404, "File not found")
            return
        payload = path.read_bytes()
        content_type = "application/json" if path.suffix == ".json" else "text/csv"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Disposition", f"attachment; filename={path.name}")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def send_pdf(self, query: dict[str, list[str]]) -> None:
        rows = load_dashboard_rows(self.config)
        try:
            invoice_index = int(query.get("invoice", ["0"])[0] or 0)
        except ValueError:
            self.send_error(400, "Invalid invoice index")
            return
        if not rows or invoice_index < 0 or invoice_index >= len(rows):
            self.send_error(404, "Invoice not found")
            return

        path = source_pdf_path(rows[invoice_index])
        try:
            resolved_path = path.resolve()
            resolved_pdf_dir = DEFAULT_PDF_DIR.resolve()
        except OSError:
            self.send_error(404, "PDF file not found")
            return
        if resolved_pdf_dir not in resolved_path.parents or not resolved_path.exists():
            self.send_error(404, "PDF file not found")
            return

        payload = resolved_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "application/pdf")
        self.send_header("Content-Disposition", f"inline; filename={resolved_path.name}")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: Any) -> None:
        return


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the invoice extraction dashboard.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8501)
    parser.add_argument("--csv", default=str(DEFAULT_CSV))
    parser.add_argument("--kb", default=str(DEFAULT_KB))
    return parser


def main() -> None:
    args = build_arg_parser().parse_args()
    DashboardHandler.config = DashboardConfig(csv_path=Path(args.csv), kb_path=Path(args.kb))
    server = ThreadingHTTPServer((args.host, args.port), DashboardHandler)
    print(f"Dashboard running at http://{args.host}:{args.port}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()


#Teste
