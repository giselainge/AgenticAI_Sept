"""Audit normalized extraction rows across every supplied preprocessed invoice."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from invoice_parser.paths import DEFAULT_AGENTIC_AB_DIR, DEFAULT_TEXT_DIR
from invoice_parser.postprocess import sanitize_invoice_row
from invoice_parser.schema import FIELDNAMES
from llm.agent.workflow import run_agentic_ab_test
from rag.adaptive_rag import DEFAULT_KB
from scripts.july_extract_invoice_fields import extract_row as extract_july_row


EXCLUDED_TEXT_SUFFIXES = ("_raw", "_cleaned", "_diagnostics", "_ocr_comparison")


def _present(value: Any) -> bool:
    return str(value or "").strip().casefold() not in {"", "null", "none", "nan"}


def _row_errors(row: dict[str, str]) -> list[str]:
    errors: list[str] = []
    if list(row) != FIELDNAMES:
        errors.append("schema_order")
    if any(not isinstance(value, str) for value in row.values()):
        errors.append("non_string_value")
    _, rejected_again = sanitize_invoice_row(row)
    if rejected_again:
        errors.extend(f"residual_{field}" for field in rejected_again)
    return errors


def audit_all_preprocessed(
    *,
    text_dir: Path = DEFAULT_TEXT_DIR,
    kb_path: Path = DEFAULT_KB,
) -> dict[str, Any]:
    files = sorted(
        path
        for path in text_dir.glob("*.txt")
        if not path.stem.endswith(EXCLUDED_TEXT_SUFFIXES)
    )
    rejected_counts: Counter[str] = Counter()
    row_error_counts: Counter[str] = Counter()
    completion_by_type: dict[str, list[float]] = defaultdict(list)
    validation_by_type: dict[str, list[int]] = defaultdict(list)
    agent_change_counts: Counter[str] = Counter()
    per_invoice: list[dict[str, Any]] = []

    with TemporaryDirectory(prefix="billing-postprocess-audit-") as temporary:
        output_dir = Path(temporary)
        for text_file in files:
            raw_july = extract_july_row(text_file)
            normalized_july, rejected = sanitize_invoice_row(raw_july)
            rejected_counts.update(rejected)
            row_errors = _row_errors(normalized_july)
            row_error_counts.update(row_errors)

            result = run_agentic_ab_test(
                text_file,
                kb_path=kb_path,
                output_dir=output_dir,
                api_key="",
                baseline_extractor=extract_july_row,
                plan_a_method="Frozen July OCR plus deterministic extraction",
            )
            for plan in (result.plan_a, result.plan_b):
                errors = _row_errors(plan.row)
                row_error_counts.update(errors)
                row_errors.extend(f"{plan.name}:{error}" for error in errors)

            category = result.plan_a.row.get("invoice_type", "unsupported")
            changed_fields = [
                field for field in FIELDNAMES if result.plan_a.row.get(field) != result.plan_b.row.get(field)
            ]
            agent_change_counts.update(changed_fields)
            completion_by_type[category].append(result.plan_a.completion)
            validation_by_type[category].append(len(result.plan_a.validation_errors))
            per_invoice.append(
                {
                    "text_file": text_file.name,
                    "invoice_type": category,
                    "fields_retrieved_percent": round(result.plan_a.completion * 100, 2),
                    "rejected_fields": rejected,
                    "postprocess_schema_errors": sorted(set(row_errors)),
                    "plan_a_validation_error_count": len(result.plan_a.validation_errors),
                    "plan_b_validation_error_count": len(result.plan_b.validation_errors),
                    "offline_rows_equal": result.plan_a.row == result.plan_b.row,
                    "agent_changed_fields": changed_fields,
                }
            )

    unique_texts = {
        hashlib.sha256(path.read_bytes()).hexdigest()
        for path in files
    }
    categories = {
        name: {
            "invoice_count": len(values),
            "average_fields_retrieved_percent": round(sum(values) / len(values) * 100, 2),
            "average_validation_errors": round(sum(validation_by_type[name]) / len(values), 2),
        }
        for name, values in sorted(completion_by_type.items())
    }
    failures = [item for item in per_invoice if item["postprocess_schema_errors"]]

    stored_model_files = sorted(
        (PROJECT_ROOT / "data" / "data_processed" / "agentic_ab_tests").rglob("*_structured.json")
    )
    stored_model_rejections: Counter[str] = Counter()
    stored_model_failures = 0
    for path in stored_model_files:
        try:
            parsed = json.loads(path.read_text(encoding="utf-8"))
            normalized, rejected = sanitize_invoice_row(parsed)
            stored_model_rejections.update(rejected)
            stored_model_failures += bool(_row_errors(normalized))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            stored_model_failures += 1
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scope": "all canonical preprocessed invoice text files, including uploaded duplicate variants",
        "preprocessed_files_tested": len(files),
        "unique_preprocessed_contents": len(unique_texts),
        "plans_checked_per_file": ["frozen_july_rules", "agentic_rules_no_llm"],
        "model_cases": "not run: no API key was configured in the process environment",
        "schema_or_residual_semantic_failure_count": len(failures),
        "schema_or_residual_semantic_error_counts": dict(sorted(row_error_counts.items())),
        "raw_fields_rejected_by_shared_postprocessor": dict(sorted(rejected_counts.items())),
        "all_offline_plan_rows_normalized": not failures,
        "agent_classification_changed_invoice_count": sum(not item["offline_rows_equal"] for item in per_invoice),
        "agent_changed_field_counts": dict(sorted(agent_change_counts.items())),
        "stored_model_outputs_tested": len(stored_model_files),
        "stored_model_schema_or_residual_semantic_failure_count": stored_model_failures,
        "stored_model_fields_rejected_by_shared_postprocessor": dict(sorted(stored_model_rejections.items())),
        "category_summary": categories,
        "per_invoice": per_invoice,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--text-dir", type=Path, default=DEFAULT_TEXT_DIR)
    parser.add_argument("--kb", type=Path, default=DEFAULT_KB)
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_AGENTIC_AB_DIR / "postprocess_comprehensive_audit.json",
    )
    args = parser.parse_args()
    report = audit_all_preprocessed(text_dir=args.text_dir, kb_path=args.kb)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "per_invoice"}, indent=2))
    print(f"Report: {args.output}")


if __name__ == "__main__":
    main()
