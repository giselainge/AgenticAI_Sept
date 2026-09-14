"""Local-only Gradio interface for the Plan A/Plan B invoice experiment."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from invoice_parser.paths import (
    DEFAULT_AGENTIC_AB_DIR,
    DEFAULT_PDF_DIR,
    DEFAULT_RAW_DIR,
    DEFAULT_TEXT_DIR,
    DEFAULT_VECTOR_STORE_DIR,
)
from llm.agent.four_case import (
    FourCaseEvaluation,
    judge_four_case_artifact,
    record_four_case_field_verdicts,
    record_four_case_verdict,
    run_four_case_evaluation,
)
from llm.agent.workflow import (
    AgenticABResult,
    PlanResult,
    compare_plans,
    judge_ab_artifact,
    record_ab_verdict,
    run_agentic_ab_test,
)
from rag.adaptive_rag import DEFAULT_KB, load_kb, record_validated_invoice
from scripts.extract_invoice_fields import read_ocr_body
from scripts.july_extract_invoice_fields import extract_row as extract_july_row
from scripts.ocr_text_extraction import SUPPORTED_EXTENSIONS, process_file


LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
FIELD_GROUPS = {
    "Invoice": ["invoice_type", "invoice_number", "invoice_date", "currency", "payment_due_date"],
    "Provider": ["provider_name", "provider_vat_number", "provider_address"],
    "Buyer": ["buyer_name", "buyer_vat_number", "buyer_address"],
    "Service": [
        "service_plan_name",
        "consumption_start_date",
        "consumption_end_date",
        "units_of_consumption",
        "unit_type",
    ],
    "Financial": ["subtotal_value", "total_vat", "total_value"],
}
REQUIRED_FIELDS = [field for fields in FIELD_GROUPS.values() for field in fields]


def _safe_stem(filename: str) -> str:
    clean = "".join(character if character.isalnum() or character in {"-", "_"} else "_" for character in filename)
    return clean.strip("_")[:80] or "invoice"


def _uploaded_path(uploaded_file: Any) -> Path | None:
    if uploaded_file is None:
        return None
    if isinstance(uploaded_file, (str, Path)):
        return Path(uploaded_file)
    name = getattr(uploaded_file, "name", None)
    return Path(name) if name else None


def _copy_local(source: Path, destination_dir: Path) -> Path:
    destination_dir.mkdir(parents=True, exist_ok=True)
    stem = _safe_stem(source.stem)
    while re.search(r"_[0-9a-f]{8}$", stem, re.IGNORECASE):
        stem = stem[:-9]
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    destination = destination_dir / f"{stem}_{digest[:8]}{source.suffix.lower()}"
    if destination.exists() and hashlib.sha256(destination.read_bytes()).hexdigest() == digest:
        return destination
    if source.resolve() == destination.resolve():
        return source
    shutil.copy2(source, destination)
    return destination


def _base_invoice_stem(stem: str) -> str:
    clean = _safe_stem(stem)
    while re.search(r"_[0-9a-f]{8}$", clean, re.IGNORECASE):
        clean = clean[:-9]
    return clean


def _july_baseline_text(source: Path) -> Path | None:
    """Resolve the frozen preprocessed text supplied with the July baseline."""
    direct = DEFAULT_TEXT_DIR / f"{_base_invoice_stem(source.stem)}.txt"
    if direct.exists():
        return direct

    source_size = source.stat().st_size
    source_digest = hashlib.sha256(source.read_bytes()).digest()
    for directory in (DEFAULT_RAW_DIR, DEFAULT_PDF_DIR):
        if not directory.exists():
            continue
        for candidate in directory.iterdir():
            if not candidate.is_file() or candidate.stat().st_size != source_size:
                continue
            if hashlib.sha256(candidate.read_bytes()).digest() != source_digest:
                continue
            baseline = DEFAULT_TEXT_DIR / f"{_base_invoice_stem(candidate.stem)}.txt"
            if baseline.exists():
                return baseline
    return None


def _prepare_inputs(uploaded_file: Any) -> tuple[Path, Path | None, str, Any]:
    source = _uploaded_path(uploaded_file)
    if source is None:
        raise ValueError("Upload an invoice PDF or image.")
    if source is not None and (not source.exists() or not source.is_file()):
        raise ValueError("The uploaded file is unavailable.")

    suffix = source.suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        raise ValueError("Use a PDF or supported invoice image.")
    local_source = _copy_local(source, DEFAULT_RAW_DIR)
    ocr_result = process_file(local_source, ocr_quality_threshold=0.0)
    if ocr_result.errors or not ocr_result.selected_text_file:
        details = "; ".join(ocr_result.errors or ["No usable OCR text was produced."])
        raise ValueError(details)
    july_text = _july_baseline_text(source)
    setattr(ocr_result, "july_baseline_used", july_text is not None)
    setattr(ocr_result, "july_baseline_text_file", str(july_text) if july_text else None)
    return (
        july_text or Path(ocr_result.selected_text_file),
        Path(ocr_result.searchable_pdf) if ocr_result.searchable_pdf else None,
        source.name,
        ocr_result,
    )


def _present(value: Any) -> bool:
    return str(value or "").strip().lower() not in {"", "null", "none", "nan"}


def _pass_summary(ocr_pass: Any) -> dict[str, Any]:
    quality = getattr(ocr_pass, "quality", None) or {}
    return {
        "used": bool(getattr(ocr_pass, "used", False)),
        "method": getattr(ocr_pass, "method", None),
        "quality_score_percent": round(float(quality.get("score", 0.0)) * 100, 2),
        "warnings": list(getattr(ocr_pass, "warnings", []) or []),
        "errors": list(getattr(ocr_pass, "errors", []) or []),
    }


def _ocr_diagnostics(ocr_result: Any) -> dict[str, Any]:
    return {
        "file_type": getattr(ocr_result, "file_type", None),
        "extraction_method": getattr(ocr_result, "extraction_method", None),
        "quality_score_percent": round(float(getattr(ocr_result, "quality_score", 0.0) or 0.0) * 100, 2),
        "requires_ocr": bool(getattr(ocr_result, "requires_ocr", False)),
        "requires_manual_review": bool(getattr(ocr_result, "requires_manual_review", True)),
        "baseline": _pass_summary(getattr(ocr_result, "baseline", None)),
        "enhanced": _pass_summary(getattr(ocr_result, "enhanced", None)),
        "warnings": list(getattr(ocr_result, "warnings", []) or []),
        "errors": list(getattr(ocr_result, "errors", []) or []),
        "selected_text_file": getattr(ocr_result, "selected_text_file", None),
        "searchable_pdf": getattr(ocr_result, "searchable_pdf", None),
        "july_baseline_used": bool(getattr(ocr_result, "july_baseline_used", False)),
        "july_baseline_text_file": getattr(ocr_result, "july_baseline_text_file", None),
    }


def _judge_winners(result: AgenticABResult) -> dict[str, str]:
    if result.llm_judge is None:
        return {}
    return {item.field: item.winner for item in result.llm_judge.field_decisions}


def field_comparison_rows(result: AgenticABResult) -> list[list[Any]]:
    winners = _judge_winners(result)
    rows: list[list[Any]] = []
    for group, fields in FIELD_GROUPS.items():
        for field_name in fields:
            plan_a_value = result.plan_a.row.get(field_name, "null")
            plan_b_value = result.plan_b.row.get(field_name, "null")
            rows.append(
                [
                    group,
                    field_name,
                    plan_a_value,
                    plan_b_value,
                    "yes" if _present(plan_a_value) else "no",
                    "yes" if _present(plan_b_value) else "no",
                    "yes" if str(plan_a_value) == str(plan_b_value) else "no",
                    winners.get(field_name, "not judged"),
                ]
            )
    return rows


def _run_summary(result: AgenticABResult) -> dict[str, Any]:
    plan_a_retrieved = sum(_present(result.plan_a.row.get(field)) for field in REQUIRED_FIELDS)
    plan_b_retrieved = sum(_present(result.plan_b.row.get(field)) for field in REQUIRED_FIELDS)
    return {
        "required_fields": len(REQUIRED_FIELDS),
        "plan_a_fields_retrieved": plan_a_retrieved,
        "plan_a_fields_retrieved_percent": round(plan_a_retrieved / len(REQUIRED_FIELDS) * 100, 2),
        "plan_b_fields_retrieved": plan_b_retrieved,
        "plan_b_fields_retrieved_percent": round(plan_b_retrieved / len(REQUIRED_FIELDS) * 100, 2),
        "changed_fields": result.comparison.get("changed_field_count", 0),
        "plan_a_validation_errors": len(result.plan_a.validation_errors),
        "plan_b_validation_errors": len(result.plan_b.validation_errors),
        "plan_a_route": result.plan_a.route,
        "plan_b_route": result.plan_b.route,
        "plan_b_llm_used": result.plan_b.llm_used,
        "accuracy_status": "requires a human verdict or labeled ground truth",
    }


def _postprocess_summary(result: AgenticABResult) -> dict[str, Any]:
    memory_event = next((event for event in result.plan_b_trace if event.agent == "provider_memory_agent"), None)
    route_event = next((event for event in result.plan_b_trace if event.agent == "review_routing_agent"), None)
    return {
        "classification": result.plan_b.row.get("invoice_type", "unsupported"),
        "plan_a_validation_errors": result.plan_a.validation_errors,
        "plan_b_validation_errors": result.plan_b.validation_errors,
        "plan_a_route": result.plan_a.route,
        "plan_b_route": result.plan_b.route,
        "provider_memory": memory_event.metrics if memory_event else {},
        "review_policy": route_event.metrics if route_event else {},
        "comparison": result.comparison,
    }


def _audit_rows(result: AgenticABResult, ocr: dict[str, Any]) -> list[dict[str, Any]]:
    rows = [
        {
            "stage": "ocr_preprocessing",
            "status": "completed" if not ocr.get("errors") else "failed",
            "decision": str(ocr.get("extraction_method") or "unknown"),
            "duration_ms": None,
            "details": {
                "quality_score_percent": ocr.get("quality_score_percent"),
                "requires_manual_review": ocr.get("requires_manual_review"),
            },
        },
        {
            "stage": "plan_a_extraction",
            "status": "completed",
            "decision": result.plan_a.route,
            "duration_ms": None,
            "details": {"fields_retrieved_percent": round(result.plan_a.completion * 100, 2)},
        },
    ]
    rows.extend(
        {
            "stage": event.agent,
            "status": event.status,
            "decision": event.decision,
            "duration_ms": event.duration_ms,
            "details": event.metrics,
        }
        for event in result.plan_b_trace
    )
    rows.append(
        {
            "stage": "ab_comparison",
            "status": "completed",
            "decision": "human_verdict_required",
            "duration_ms": None,
            "details": {
                "changed_field_count": result.comparison.get("changed_field_count", 0),
                "validation_error_delta": result.comparison.get("validation_error_delta", 0),
            },
        }
    )
    return rows


def _audit_table(rows: list[dict[str, Any]]) -> list[list[Any]]:
    return [
        [
            row.get("stage"),
            row.get("status"),
            row.get("decision"),
            row.get("duration_ms"),
            json.dumps(row.get("details", {}), ensure_ascii=False),
        ]
        for row in rows
    ]


def _execute_local_ab(
    uploaded_file: Any,
    enable_plan_b_llm: bool,
    plan_b_model: str,
    plan_b_api_key: str,
    llm_provider: str = "gemini",
) -> tuple[AgenticABResult, str, dict[str, Any]]:
    text_path, pdf_path, source_label, ocr_result = _prepare_inputs(uploaded_file)
    selected_provider = "openai" if llm_provider == "openai" else "gemini"
    key_env = "OPENAI_API_KEY" if selected_provider == "openai" else "GEMINI_API_KEY"
    model_env = "OPENAI_MODEL" if selected_provider == "openai" else "GEMINI_MODEL"
    model_default = "gpt-5.6-terra" if selected_provider == "openai" else "gemini-3.5-flash"
    selected_key = (plan_b_api_key or os.getenv(key_env, "")) if enable_plan_b_llm else ""
    selected_model = (plan_b_model or os.getenv(model_env, model_default)).strip()
    result = run_agentic_ab_test(
        text_path,
        pdf_file=pdf_path,
        kb_path=DEFAULT_KB,
        output_dir=DEFAULT_AGENTIC_AB_DIR,
        api_key=selected_key,
        model=selected_model,
        llm_provider=selected_provider,
        baseline_extractor=extract_july_row,
        plan_a_method="Frozen July OCR plus deterministic extraction",
    )
    ocr = _ocr_diagnostics(ocr_result)
    result.preprocessing = ocr
    result.audit_log = _audit_rows(result, ocr)
    if result.artifact_path:
        Path(result.artifact_path).write_text(
            json.dumps(result.model_dump(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    return result, source_label, ocr


def _plan_from_four_case(candidate: Any, name: str) -> PlanResult:
    if candidate.status not in {"measured", "fallback"} or not candidate.row:
        details = "; ".join(candidate.errors or [f"{candidate.label} did not produce a usable result."])
        raise ValueError(details)
    return PlanResult(
        name=name,
        method=candidate.method,
        row=dict(candidate.row),
        validation_errors=list(candidate.validation_errors),
        route=candidate.route,
        completion=round(float(candidate.fields_retrieved_percent or 0.0) / 100, 4),
        llm_used=bool(candidate.llm_used),
    )


def _execute_july_vs_agentic(
    uploaded_file: Any,
    model: str,
    api_key: str,
    llm_provider: str,
) -> tuple[AgenticABResult, str, dict[str, Any]]:
    """Run the complete July OCR+LLM baseline against agentic OCR+LLM."""
    text_path, pdf_path, source_label, ocr_result = _prepare_inputs(uploaded_file)
    selected_provider = "openai" if llm_provider == "openai" else "gemini"
    key_env = "OPENAI_API_KEY" if selected_provider == "openai" else "GEMINI_API_KEY"
    model_env = "OPENAI_MODEL" if selected_provider == "openai" else "GEMINI_MODEL"
    default_model = "gpt-5.6-terra" if selected_provider == "openai" else "gemini-3.5-flash"
    selected_key = (api_key or os.getenv(key_env, "")).strip()
    if not selected_key:
        raise ValueError(f"Paste a {selected_provider.title()} API key to run the July-vs-agentic A/B test.")
    selected_model = (model or os.getenv(model_env, default_model)).strip()

    four = run_four_case_evaluation(
        text_path,
        pdf_file=pdf_path,
        kb_path=DEFAULT_KB,
        output_dir=DEFAULT_AGENTIC_AB_DIR / "four_case",
        llm_provider=selected_provider,
        llm_api_key=selected_key,
        llm_model=selected_model,
    )
    plan_a = _plan_from_four_case(four.cases["ocr_llm"], "Plan A (July)")
    plan_b = _plan_from_four_case(four.cases["ocr_llm_agentic"], "Plan B (Agentic)")
    result = AgenticABResult(
        generated_at=four.generated_at,
        source_name=four.source_name,
        plan_a=plan_a,
        plan_b=plan_b,
        comparison=compare_plans(plan_a, plan_b),
        plan_b_trace=four.agent_trace,
    )
    ocr = _ocr_diagnostics(ocr_result)
    result.preprocessing = ocr
    result.audit_log = _audit_rows(result, ocr)
    artifact = DEFAULT_AGENTIC_AB_DIR / f"{text_path.stem.removesuffix('_selected_text')}_july_vs_agentic.json"
    result.artifact_path = str(artifact)
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(json.dumps(result.model_dump(), indent=2, ensure_ascii=False), encoding="utf-8")
    return result, source_label, ocr


def run_local_ab(
    uploaded_file: Any,
    enable_plan_b_llm: bool = False,
    plan_b_model: str = "",
    plan_b_api_key: str = "",
    llm_provider: str = "gemini",
) -> tuple[Any, ...]:
    """Run both plans; Plan B model use requires an explicit UI opt-in."""
    try:
        result, source_label, _ = _execute_local_ab(
            uploaded_file, enable_plan_b_llm, plan_b_model, plan_b_api_key, llm_provider
        )
    except Exception as exc:
        status = f"Local A/B test failed: {exc}"
        return status, {}, {}, {}, [], "", ""

    plan_a = result.plan_a.model_dump()
    plan_b = result.plan_b.model_dump()
    trace = [event.model_dump() for event in result.plan_b_trace]
    status = (
        f"Completed locally for {source_label}. "
        f"Plan A route: {result.plan_a.route}; Plan B route: {result.plan_b.route}; "
        f"external LLM used: {str(result.plan_b.llm_used).lower()}."
    )
    return status, plan_a, plan_b, result.comparison, trace, result.artifact_path or "", result.artifact_path or ""


def run_local_inspection(
    uploaded_file: Any,
    enable_plan_b_llm: bool = False,
    plan_b_model: str = "",
    plan_b_api_key: str = "",
    llm_provider: str = "gemini",
) -> tuple[Any, ...]:
    """Populate the field, OCR, post-processing and audit inspection views."""
    try:
        result, source_label, ocr = _execute_local_ab(
            uploaded_file, enable_plan_b_llm, plan_b_model, plan_b_api_key, llm_provider
        )
    except Exception as exc:
        return f"Local A/B test failed: {exc}", {}, [], {}, {}, {}, [], {}, [], "", ""
    status = (
        f"Completed locally for {source_label}. Plan A route: {result.plan_a.route}; "
        f"Plan B route: {result.plan_b.route}; Plan B LLM used: {str(result.plan_b.llm_used).lower()}."
    )
    return (
        status,
        _run_summary(result),
        field_comparison_rows(result),
        result.plan_a.model_dump(),
        result.plan_b.model_dump(),
        result.comparison,
        [event.model_dump() for event in result.plan_b_trace],
        {"ocr": ocr, "post_processing": _postprocess_summary(result)},
        _audit_table(result.audit_log),
        result.artifact_path or "",
        result.artifact_path or "",
    )


def retrieve_fields_with_llm(
    uploaded_file: Any,
    plan_b_model: str = "",
    plan_b_api_key: str = "",
    llm_provider: str = "openai",
) -> tuple[Any, ...]:
    """Compare the complete July OCR+LLM pipeline with agentic OCR+LLM."""
    try:
        result, source_label, ocr = _execute_july_vs_agentic(
            uploaded_file, plan_b_model, plan_b_api_key, llm_provider
        )
    except Exception as exc:
        return f"July-vs-agentic A/B test failed: {exc}", {}, [], {}, {}, {}, [], {}, [], "", ""
    status = (
        f"Completed July-vs-agentic A/B test locally for {source_label}. "
        f"Plan A (July) retrieved {result.plan_a.completion * 100:.2f}%; "
        f"Plan B (Agentic) retrieved {result.plan_b.completion * 100:.2f}%."
    )
    return (
        status,
        _run_summary(result),
        field_comparison_rows(result),
        result.plan_a.model_dump(),
        result.plan_b.model_dump(),
        result.comparison,
        [event.model_dump() for event in result.plan_b_trace],
        {"ocr": ocr, "post_processing": _postprocess_summary(result)},
        _audit_table(result.audit_log),
        result.artifact_path or "",
        result.artifact_path or "",
    )


def save_verdict(artifact_path: str, preferred_plan: str, note: str) -> str:
    if not artifact_path:
        return "Run an A/B test before saving a verdict."
    try:
        result = record_ab_verdict(artifact_path, preferred_plan, note)
    except Exception as exc:
        return f"Could not save the local verdict: {exc}"
    return f"Saved verdict '{result.evaluation['preferred_plan']}' in {artifact_path}."


def run_judge(
    artifact_path: str,
    base_url: str,
    model: str,
    api_key: str,
    provider: str = "openai_compatible",
    extraction_api_key: str = "",
    extraction_provider: str = "gemini",
) -> tuple[str, dict[str, Any]]:
    """Run the optional judge only after an explicit local UI action."""
    if not artifact_path:
        return "Run an A/B test before invoking the judge.", {}
    selected_provider = provider if provider in {"gemini", "openai"} else "openai_compatible"
    selected_url = (base_url or os.getenv("JUDGE_BASE_URL", "")).strip()
    selected_model = (
        model
        or os.getenv("JUDGE_MODEL", "")
        or (os.getenv("GEMINI_MODEL", "gemini-3.5-flash") if selected_provider == "gemini" else "")
        or (os.getenv("OPENAI_MODEL", "gpt-5.6-terra") if selected_provider == "openai" else "")
    ).strip()
    if selected_provider in {"gemini", "openai"}:
        reusable_key = extraction_api_key if extraction_provider == selected_provider else ""
        provider_env = "GEMINI_API_KEY" if selected_provider == "gemini" else "OPENAI_API_KEY"
        selected_key = api_key or reusable_key or os.getenv("JUDGE_API_KEY", "") or os.getenv(provider_env, "")
    else:
        selected_key = api_key or os.getenv("JUDGE_API_KEY", "")
    try:
        result = judge_ab_artifact(
            artifact_path,
            base_url=selected_url,
            api_key=selected_key,
            model=selected_model,
            provider=selected_provider,
        )
    except Exception as exc:
        return f"LLM judge could not run: {exc}", {}
    judge = result.llm_judge
    if judge is None:
        return "LLM judge produced no result.", {}
    if judge.status != "completed":
        return f"LLM judge status: {judge.status}. {judge.summary}", judge.model_dump()
    return (
        f"{selected_provider} judge recommends {judge.preferred_plan} with confidence {judge.confidence}. "
        "A human verdict is still required.",
        judge.model_dump(),
    )


def run_judge_inspection(
    artifact_path: str,
    base_url: str,
    model: str,
    api_key: str,
    provider: str = "openai_compatible",
    extraction_api_key: str = "",
    extraction_provider: str = "gemini",
) -> tuple[str, dict[str, Any], list[list[Any]]]:
    status, judge = run_judge(
        artifact_path,
        base_url,
        model,
        api_key,
        provider,
        extraction_api_key,
        extraction_provider,
    )
    rows: list[list[Any]] = []
    path = Path(artifact_path) if artifact_path else None
    if path and path.exists():
        try:
            result = AgenticABResult.model_validate_json(path.read_text(encoding="utf-8"))
            rows = field_comparison_rows(result)
        except Exception:
            rows = []
    return status, judge, rows


def _four_case_table(result: FourCaseEvaluation) -> list[list[Any]]:
    return [
        [
            case.case_id,
            case.label,
            case.status,
            case.fields_retrieved_percent,
            len(case.validation_errors),
            case.route,
            "yes" if case.llm_used else "no",
        ]
        for case in result.cases.values()
    ]


def _source_preview_pages(uploaded_file: Any) -> list[str]:
    source = _uploaded_path(uploaded_file)
    if source is None or not source.exists():
        return []
    if source.suffix.lower() != ".pdf":
        return [str(source)]
    renderer = shutil.which("pdftoppm")
    if renderer is None:
        return []
    digest = hashlib.sha256(source.read_bytes()).hexdigest()[:12]
    preview_dir = DEFAULT_AGENTIC_AB_DIR / "previews" / digest
    preview_dir.mkdir(parents=True, exist_ok=True)
    existing = sorted(preview_dir.glob("page-*.jpg"))
    if existing:
        return [str(path) for path in existing]
    completed = subprocess.run(
        [renderer, "-jpeg", "-r", "120", str(source), str(preview_dir / "page")],
        check=False,
        capture_output=True,
        timeout=120,
    )
    if completed.returncode != 0:
        return []
    return [str(path) for path in sorted(preview_dir.glob("page-*.jpg"))]


def _table_records(value: Any) -> list[Any]:
    if value is None:
        return []
    if hasattr(value, "to_dict"):
        try:
            return list(value.to_dict("records"))
        except TypeError:
            return []
    return list(value) if isinstance(value, (list, tuple)) else []


def _verified_values_from_table(value: Any) -> dict[str, str]:
    verified: dict[str, str] = {}
    for row in _table_records(value):
        if isinstance(row, dict):
            field_name = str(row.get("Field") or "").strip()
            source_value = str(row.get("Source-verified value") or "").strip()
        elif isinstance(row, (list, tuple)) and len(row) >= 3:
            field_name = str(row[1] or "").strip()
            source_value = str(row[2] or "").strip()
        else:
            continue
        if field_name in REQUIRED_FIELDS and source_value:
            verified[field_name] = source_value
    return verified


def _four_case_field_rows(
    result: FourCaseEvaluation,
    verified_values: dict[str, str] | None = None,
) -> list[list[Any]]:
    verified = verified_values or {}
    judge_items = {
        item.field: (item.winner, item.reason)
        for item in (result.judge.field_decisions if result.judge else [])
    }
    rows: list[list[Any]] = []
    for group, fields in FIELD_GROUPS.items():
        for field_name in fields:
            values = []
            for case_id in ("ocr_rules", "ocr_llm", "ocr_agentic", "ocr_llm_agentic"):
                case = result.cases[case_id]
                values.append(case.row.get(field_name, "null") if case.row else f"({case.status})")
            winner, reason = judge_items.get(field_name, ("not judged", ""))
            rows.append([group, field_name, verified.get(field_name, ""), *values, winner, reason])
    return rows


def run_four_cases(
    uploaded_file: Any,
    model: str,
    api_key: str,
    llm_provider: str = "gemini",
) -> tuple[Any, ...]:
    """Run all available cases; missing model configuration remains explicit."""
    try:
        text_path, pdf_path, source_label, _ = _prepare_inputs(uploaded_file)
        result = run_four_case_evaluation(
            text_path,
            pdf_file=pdf_path,
            kb_path=DEFAULT_KB,
            output_dir=DEFAULT_AGENTIC_AB_DIR / "four_case",
            llm_provider=llm_provider,
            llm_api_key=api_key,
            llm_model=model,
        )
    except Exception as exc:
        return f"Four-case assessment failed: {exc}", [], [], [], {}, "", ""
    measured = sum(case.status == "measured" for case in result.cases.values())
    status = (
        f"Four-case artifact created locally for {source_label}. Measured cases: {measured}/4. "
        "Accuracy still requires the judge plus a human verdict."
    )
    return (
        status,
        _four_case_table(result),
        _four_case_field_rows(result),
        _source_preview_pages(uploaded_file),
        result.model_dump(),
        result.artifact_path or "",
        result.artifact_path or "",
    )


def run_four_case_judge_ui(
    artifact_path: str,
    base_url: str,
    model: str,
    api_key: str,
    provider: str = "openai_compatible",
    extraction_api_key: str = "",
    extraction_provider: str = "gemini",
    field_rows: Any = None,
) -> tuple[str, dict[str, Any], list[list[Any]]]:
    if not artifact_path:
        return "Run the four cases before invoking the judge.", {}, []
    try:
        selected_provider = provider if provider in {"gemini", "openai"} else "openai_compatible"
        selected_model = (
            model
            or os.getenv("JUDGE_MODEL", "")
            or (os.getenv("GEMINI_MODEL", "gemini-3.5-flash") if selected_provider == "gemini" else "")
            or (os.getenv("OPENAI_MODEL", "gpt-5.6-terra") if selected_provider == "openai" else "")
        ).strip()
        if selected_provider in {"gemini", "openai"}:
            reusable_key = extraction_api_key if extraction_provider == selected_provider else ""
            provider_env = "GEMINI_API_KEY" if selected_provider == "gemini" else "OPENAI_API_KEY"
            selected_key = api_key or reusable_key or os.getenv("JUDGE_API_KEY", "") or os.getenv(provider_env, "")
        else:
            selected_key = api_key or os.getenv("JUDGE_API_KEY", "")
        result = judge_four_case_artifact(
            artifact_path,
            base_url=(base_url or os.getenv("JUDGE_BASE_URL", "")).strip(),
            api_key=selected_key,
            model=selected_model,
            provider=selected_provider,
        )
    except Exception as exc:
        return f"Four-case judge could not run: {exc}", {}, []
    judge = result.judge
    if judge is None:
        return "Four-case judge produced no result.", {}, []
    verified = _verified_values_from_table(field_rows)
    return (
        f"Judge status: {judge.status}; best case: {judge.best_case}; confidence: {judge.confidence}. "
        "Human verification is still required.",
        judge.model_dump(),
        _four_case_field_rows(result, verified),
    )


def score_four_case_fields_ui(artifact_path: str, field_rows: Any) -> tuple[str, dict[str, Any], list[list[Any]]]:
    if not artifact_path:
        return "Run the four cases before scoring fields.", {}, []
    verified = _verified_values_from_table(field_rows)
    try:
        result = record_four_case_field_verdicts(artifact_path, verified)
    except Exception as exc:
        return f"Source verification could not be saved: {exc}", {}, []
    evaluation = result.human_field_evaluation or {}
    return (
        f"Scored {evaluation.get('verified_field_count', 0)} source-verified fields across all available cases.",
        evaluation,
        _four_case_field_rows(result, verified),
    )


def save_verified_to_kb_ui(
    artifact_path: str,
    field_rows: Any,
    selected_case: str,
    note: str,
) -> tuple[str, dict[str, Any]]:
    if not artifact_path:
        return "Run the four cases before saving provider memory.", {}
    verified = _verified_values_from_table(field_rows)
    if not verified:
        return "Enter at least one source-verified value before saving provider memory.", {}
    try:
        result = record_four_case_field_verdicts(artifact_path, verified)
        candidate = result.cases[selected_case]
        if candidate.status not in {"measured", "fallback"}:
            raise ValueError(f"{selected_case} has no usable extraction to review.")
        reviewed_row = dict(candidate.row)
        reviewed_row.update(
            {field: "null" if value.casefold() in {"null", "none", "<absent>"} else value for field, value in verified.items()}
        )
        reviewed_row["source_file"] = result.source_name
        reviewed_row["valid_invoice"] = "true"
        saved = record_validated_invoice(reviewed_row, note, DEFAULT_KB)

        from vector_store.base import build_index, retrieve_provider_memory_docs

        build_index(kb_path=DEFAULT_KB, index_path=DEFAULT_VECTOR_STORE_DIR, force_rebuild=True)
        text_path = Path(str(result.cases["ocr_rules"].row.get("ocr_text_file") or ""))
        _, query_text = read_ocr_body(text_path) if text_path.exists() else ("", "")
        hits = retrieve_provider_memory_docs(
            query_text=query_text,
            provider_hint=str(reviewed_row.get("provider_name") or ""),
            invoice_type=str(reviewed_row.get("invoice_type") or ""),
            top_k=10,
            kb_path=DEFAULT_KB,
            index_path=DEFAULT_VECTOR_STORE_DIR,
        )
        signature = saved["signature"]
        searchable = any((hit.get("metadata") or {}).get("signature") == signature for hit in hits)
        details = {
            **saved,
            "selected_case": selected_case,
            "vector_index_rebuilt": True,
            "retrieval_hits": len(hits),
            "retrieved_sections": sorted(
                {str((hit.get("metadata") or {}).get("memory_section") or "") for hit in hits}
            ),
            "saved_example_searchable": searchable,
        }
        status = (
            "Reviewed invoice saved to local provider memory; FAISS index rebuilt; "
            f"saved example searchable: {str(searchable).lower()}."
        )
        return status, details
    except Exception as exc:
        return f"Provider-memory verification failed: {exc}", {}


def save_four_case_verdict(artifact_path: str, best_case: str, note: str) -> str:
    if not artifact_path:
        return "Run the four cases before saving a human verdict."
    try:
        result = record_four_case_verdict(artifact_path, best_case, note)
    except Exception as exc:
        return f"Could not save the four-case verdict: {exc}"
    assert result.human_evaluation is not None
    return f"Saved human four-case verdict '{result.human_evaluation['best_case']}' locally."


def knowledge_base_view() -> tuple[list[list[Any]], dict[str, Any]]:
    kb = load_kb(DEFAULT_KB)
    rows: list[list[Any]] = []
    for provider_id, provider in sorted(kb.get("providers", {}).items()):
        rows.append(
            [
                provider_id,
                provider.get("provider_name", provider_id),
                len(provider.get("provider_specific_extraction_tips", [])),
                len(provider.get("human_reviewer_feedback", [])),
                len(provider.get("previously_validated_invoices", [])),
            ]
        )
    return rows, kb


def rebuild_vector_index() -> str:
    try:
        from vector_store.base import build_index

        build_index(kb_path=DEFAULT_KB, index_path=DEFAULT_VECTOR_STORE_DIR, force_rebuild=True)
    except Exception as exc:
        return f"Vector index was not built: {exc}"
    return f"FAISS/LlamaIndex provider index rebuilt locally at {DEFAULT_VECTOR_STORE_DIR}."


def build_demo() -> Any:
    os.environ.setdefault("GRADIO_ANALYTICS_ENABLED", "False")
    os.environ.setdefault("MPLCONFIGDIR", "/tmp/billing-matplotlib")
    import gradio as gr

    with gr.Blocks(title="Agentic Invoice A/B Lab") as demo:
        gr.Markdown(
            "# Agentic Invoice A/B Lab\n"
            "Runs on this computer. The primary A/B test compares the complete July OCR + LLM baseline "
            "with OCR + LLM inside the five-agent supervisor workflow and provider knowledge base. "
            "The supported categories are electricity, water, natural gas, and telecom; "
            "other documents are rejected. Plan B extraction and the independent judge call a model only "
            "when you explicitly enable the corresponding action."
        )
        gr.Markdown(
            "**Temporary OpenAI access:** invite each tester to the AgenticBilling API project, let each "
            "tester create their own restricted project key with an expiry of **7 days or less**, and remove "
            "their project membership by day 7 when access must end. Never share one user's key."
        )
        with gr.Tab("Plan A / Plan B"):
            invoice = gr.UploadButton(
                "Upload invoice PDF or image",
                file_types=[".pdf", ".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".bmp"],
                file_count="single",
                type="filepath",
            )
            with gr.Accordion("Retrieve fields with GPT / Gemini", open=True):
                gr.Markdown(
                    "Plan A runs the original July OCR + direct LLM field-retrieval pipeline. Plan B uses "
                    "the same OCR evidence and model inside the agent workflow with provider memory and "
                    "validation. Both outputs use the same 19-field normalization and contamination checks. "
                    "Keys remain in memory for this request."
                )
                plan_b_provider = gr.Dropdown(
                    choices=[("OpenAI", "openai"), ("Gemini", "gemini")],
                    value=os.getenv("LLM_PROVIDER", "openai"),
                    label="Extraction provider",
                )
                plan_b_model = gr.Textbox(
                    value="",
                    label="A/B extraction model (blank uses provider default)",
                    placeholder="OpenAI: gpt-5.6-terra; Gemini: gemini-3.5-flash",
                )
                plan_b_api_key = gr.Textbox(label="Provider API key", type="password")
                retrieve_fields_button = gr.Button(
                    "Retrieve fields with GPT / Gemini and compare Plan A vs Plan B",
                    variant="primary",
                )
            enable_plan_b_llm = gr.State(False)
            run_button = gr.Button("Run no-LLM rule ablations (diagnostic only)")
            status = gr.Markdown()
            run_summary = gr.JSON(label="Field retrieval and routing summary")
            field_table = gr.Dataframe(
                headers=[
                    "Group",
                    "Field",
                    "Plan A (July) value",
                    "Plan B (Agentic) value",
                    "A retrieved",
                    "B retrieved",
                    "Same value",
                    "Judge decision",
                ],
                datatype=["str"] * 8,
                interactive=False,
                label="Required-field inspection",
            )
            with gr.Accordion("Raw plan outputs", open=False):
                with gr.Row():
                    plan_a = gr.JSON(label="Plan A (July)")
                    plan_b = gr.JSON(label="Plan B (Agentic)")
                comparison = gr.JSON(label="Comparison")
            with gr.Accordion("OCR, post-processing and extraction log", open=False):
                processing_details = gr.JSON(label="OCR and post-processing details")
                trace = gr.JSON(label="Plan B agent trace")
                audit_log = gr.Dataframe(
                    headers=["Stage", "Status", "Decision", "Duration ms", "Details"],
                    datatype=["str", "str", "str", "number", "str"],
                    interactive=False,
                    label="Local extraction audit log",
                )
            artifact = gr.Textbox(label="Local result artifact", interactive=False)
            artifact_state = gr.State("")

            run_button.click(
                run_local_inspection,
                inputs=[invoice, enable_plan_b_llm, plan_b_model, plan_b_api_key, plan_b_provider],
                outputs=[
                    status,
                    run_summary,
                    field_table,
                    plan_a,
                    plan_b,
                    comparison,
                    trace,
                    processing_details,
                    audit_log,
                    artifact,
                    artifact_state,
                ],
            )
            retrieve_fields_button.click(
                retrieve_fields_with_llm,
                inputs=[invoice, plan_b_model, plan_b_api_key, plan_b_provider],
                outputs=[
                    status,
                    run_summary,
                    field_table,
                    plan_a,
                    plan_b,
                    comparison,
                    trace,
                    processing_details,
                    audit_log,
                    artifact,
                    artifact_state,
                ],
            )

            gr.Markdown("### Human accuracy verdict")
            verdict = gr.Dropdown(
                choices=["plan_a", "plan_b", "tie", "inconclusive"],
                value="inconclusive",
                label="Preferred result",
            )
            verdict_note = gr.Textbox(label="Reviewer note")
            verdict_button = gr.Button("Save verdict locally")
            verdict_status = gr.Markdown()
            verdict_button.click(
                save_verdict,
                inputs=[artifact_state, verdict, verdict_note],
                outputs=verdict_status,
            )

            with gr.Accordion("Optional independent LLM judge", open=False):
                gr.Markdown(
                    "The judge compares both outputs only with OCR evidence. The invoice text is sent to the "
                    "configured endpoint only when you click **Run LLM judge**. Its recommendation does not "
                    "replace the human verdict. Select the same official provider to reuse the Plan B key; "
                    "leave the judge-key field blank in that case."
                )
                judge_provider = gr.Dropdown(
                    choices=[
                        ("Gemini (same key allowed)", "gemini"),
                        ("OpenAI (same project key allowed)", "openai"),
                        ("OpenAI-compatible / Qwen", "openai_compatible"),
                    ],
                    value=os.getenv("JUDGE_PROVIDER", "gemini"),
                    label="Judge provider",
                )
                judge_base_url = gr.Textbox(
                    value=os.getenv("JUDGE_BASE_URL", ""),
                    label="Base URL (only for OpenAI-compatible / Qwen)",
                    placeholder="http://127.0.0.1:8000/v1",
                )
                judge_model = gr.Textbox(
                    value=os.getenv("JUDGE_MODEL", ""),
                    label="Judge model (blank uses official-provider default)",
                    placeholder="gpt-5.6-terra, gemini-3.5-flash, or served model name",
                )
                judge_api_key = gr.Textbox(
                    label="Judge API key (blank reuses a matching extraction key)",
                    type="password",
                )
                judge_button = gr.Button("Run LLM judge")
                judge_status = gr.Markdown()
                judge_result = gr.JSON(label="Advisory judge result")
                judge_button.click(
                    run_judge_inspection,
                    inputs=[
                        artifact_state,
                        judge_base_url,
                        judge_model,
                        judge_api_key,
                        judge_provider,
                        plan_b_api_key,
                        plan_b_provider,
                    ],
                    outputs=[judge_status, judge_result, field_table],
                )

        with gr.Tab("Four-case assessment"):
            gr.Markdown(
                "Compare the four requested configurations on one invoice. With the provider key blank, "
                "the two local cases run and the two LLM cases are marked unavailable. Pasting a key and "
                "clicking **Run four cases** makes two extraction calls: one without provider RAG "
                "and one inside the agentic workflow. The password field is request-only and is not saved."
            )
            four_case_invoice = gr.UploadButton(
                "Upload invoice PDF or image",
                file_types=[".pdf", ".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".bmp"],
                file_count="single",
                type="filepath",
            )
            four_case_provider = gr.Dropdown(
                choices=[("OpenAI", "openai"), ("Gemini", "gemini")],
                value=os.getenv("LLM_PROVIDER", "openai"),
                label="Extraction provider",
            )
            four_case_model = gr.Textbox(
                value="",
                label="Model (blank uses provider default)",
                placeholder="OpenAI: gpt-5.6-terra; Gemini: gemini-3.5-flash",
            )
            four_case_key = gr.Textbox(label="Provider API key", type="password")
            four_case_button = gr.Button("Run four cases", variant="primary")
            four_case_status = gr.Markdown()
            four_case_table = gr.Dataframe(
                headers=[
                    "Case",
                    "Configuration",
                    "Status",
                    "Fields retrieved %",
                    "Validation errors",
                    "Route",
                    "LLM used",
                ],
                datatype=["str", "str", "str", "number", "number", "str", "str"],
                interactive=False,
                label="Four-case comparison",
            )
            gr.Markdown(
                "### Field-by-field source check\n"
                "Inspect the original page and compare each of the 19 fields across all four scenarios. "
                "Enter the exact source value in **Source-verified value**; enter `<absent>` when the source "
                "does not contain that field."
            )
            with gr.Row():
                four_source_preview = gr.Gallery(
                    label="Original source pages",
                    columns=1,
                    height=720,
                    preview=True,
                )
                four_field_table = gr.Dataframe(
                    headers=[
                        "Group",
                        "Field",
                        "Source-verified value",
                        "Ablation: July OCR + rules",
                        "Plan A: July OCR + LLM",
                        "Ablation: July OCR + agentic rules",
                        "Plan B: July OCR + LLM + agents",
                        "Judge winner",
                        "Judge reason",
                    ],
                    datatype=["str"] * 9,
                    interactive=True,
                    type="array",
                    label="Extracted fields compared with the source",
                )
            with gr.Accordion("Candidate outputs", open=False):
                four_case_result = gr.JSON(label="Four-case artifact contents")
                four_case_artifact = gr.Textbox(label="Local result artifact", interactive=False)
            four_case_artifact_state = gr.State("")
            four_case_button.click(
                run_four_cases,
                inputs=[four_case_invoice, four_case_model, four_case_key, four_case_provider],
                outputs=[
                    four_case_status,
                    four_case_table,
                    four_field_table,
                    four_source_preview,
                    four_case_result,
                    four_case_artifact,
                    four_case_artifact_state,
                ],
            )

            with gr.Accordion("Optional independent four-case judge", open=False):
                gr.Markdown(
                    "The judge uses the OCR evidence to rank only the available candidates. It is advisory; "
                    "save a separate human verdict after checking the source invoice. Select the same official "
                    "provider to reuse the extraction key already pasted above."
                )
                four_judge_provider = gr.Dropdown(
                    choices=[
                        ("Gemini (same key allowed)", "gemini"),
                        ("OpenAI (same project key allowed)", "openai"),
                        ("OpenAI-compatible / Qwen", "openai_compatible"),
                    ],
                    value=os.getenv("JUDGE_PROVIDER", "gemini"),
                    label="Judge provider",
                )
                four_judge_base_url = gr.Textbox(
                    value=os.getenv("JUDGE_BASE_URL", ""),
                    label="Base URL (only for OpenAI-compatible / Qwen)",
                    placeholder="http://127.0.0.1:8000/v1",
                )
                four_judge_model = gr.Textbox(
                    value=os.getenv("JUDGE_MODEL", ""),
                    label="Judge model (blank uses official-provider default)",
                    placeholder="gpt-5.6-terra, gemini-3.5-flash, or served model name",
                )
                four_judge_key = gr.Textbox(
                    label="Judge API key (blank reuses a matching extraction key)",
                    type="password",
                )
                four_judge_button = gr.Button("Run four-case LLM judge")
                four_judge_status = gr.Markdown()
                four_judge_result = gr.JSON(label="Advisory judge result")
                four_judge_button.click(
                    run_four_case_judge_ui,
                    inputs=[
                        four_case_artifact_state,
                        four_judge_base_url,
                        four_judge_model,
                        four_judge_key,
                        four_judge_provider,
                        four_case_key,
                        four_case_provider,
                        four_field_table,
                    ],
                    outputs=[four_judge_status, four_judge_result, four_field_table],
                )

            gr.Markdown("### Human source verification")
            four_score_button = gr.Button("Score all four cases against source-verified fields")
            four_score_status = gr.Markdown()
            four_score_result = gr.JSON(label="Measured field accuracy from human ground truth")
            four_score_button.click(
                score_four_case_fields_ui,
                inputs=[four_case_artifact_state, four_field_table],
                outputs=[four_score_status, four_score_result, four_field_table],
            )

            gr.Markdown("### Human four-case verdict")
            four_verdict = gr.Dropdown(
                choices=[
                    "ocr_rules",
                    "ocr_llm",
                    "ocr_agentic",
                    "ocr_llm_agentic",
                    "tie",
                    "inconclusive",
                ],
                value="inconclusive",
                label="Best verified configuration",
            )
            four_verdict_note = gr.Textbox(label="Reviewer note")
            four_verdict_button = gr.Button("Save four-case verdict locally")
            four_verdict_status = gr.Markdown()
            four_verdict_button.click(
                save_four_case_verdict,
                inputs=[four_case_artifact_state, four_verdict, four_verdict_note],
                outputs=four_verdict_status,
            )

            gr.Markdown("### Save reviewed result and verify provider-memory retrieval")
            gr.Markdown(
                "Choose the candidate to use as a base. Source-verified values override that candidate. "
                "This explicit action saves the reviewed extraction locally, rebuilds FAISS, and queries "
                "the index to prove the new provider example is searchable."
            )
            four_kb_case = gr.Dropdown(
                choices=list(("ocr_rules", "ocr_llm", "ocr_agentic", "ocr_llm_agentic")),
                value="ocr_llm_agentic",
                label="Reviewed candidate",
            )
            four_kb_note = gr.Textbox(label="Reviewer / provider-memory note")
            four_kb_button = gr.Button("Save reviewed fields to KB and test retrieval", variant="primary")
            four_kb_status = gr.Markdown()
            four_kb_result = gr.JSON(label="Knowledge-base upload and retrieval evidence")
            four_kb_button.click(
                save_verified_to_kb_ui,
                inputs=[four_case_artifact_state, four_field_table, four_kb_case, four_kb_note],
                outputs=[four_kb_status, four_kb_result],
            )

        with gr.Tab("Provider knowledge base"):
            gr.Markdown(
                "Provider memories remain separate. Unknown suppliers may be added through reviewed invoice feedback."
            )
            refresh_button = gr.Button("Refresh knowledge base")
            provider_table = gr.Dataframe(
                headers=["Provider ID", "Provider", "Tips", "Feedback", "Validated invoices"],
                datatype=["str", "str", "number", "number", "number"],
                interactive=False,
            )
            kb_json = gr.JSON(label="Local provider memory")
            index_button = gr.Button("Rebuild local FAISS index")
            index_status = gr.Markdown()
            refresh_button.click(knowledge_base_view, outputs=[provider_table, kb_json])
            index_button.click(rebuild_vector_index, outputs=index_status)
            demo.load(knowledge_base_view, outputs=[provider_table, kb_json])

    return demo


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the local Gradio A/B invoice lab.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7860)
    parser.add_argument("--inbrowser", action="store_true")
    return parser


def main() -> None:
    args = build_arg_parser().parse_args()
    container_bind_allowed = os.getenv("ALLOW_CONTAINER_BIND") == "1" and args.host == "0.0.0.0"
    if args.host not in LOCAL_HOSTS and not container_bind_allowed:
        raise SystemExit("Local-only mode requires --host 127.0.0.1, localhost, or ::1.")
    print(f"Local Gradio server: http://{args.host}:{args.port}")
    build_demo().launch(
        server_name=args.host,
        server_port=args.port,
        share=False,
        inbrowser=args.inbrowser,
    )


if __name__ == "__main__":
    main()
