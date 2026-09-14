"""Source-verifiable July-vs-agentic A/B experiment and advisory judge."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from invoice_parser.postprocess import sanitize_invoice_row
from invoice_parser.providers import canonical_provider
from invoice_parser.schema import FIELDNAMES
from invoice_parser.text_utils import fold_text, normalize_money, normalize_space
from llm.agent.judge import run_four_case_judge
from llm.agent.models import FourCaseJudgeResult, JudgeCaller
from llm.agent.workflow import (
    AgentEvent,
    PlanResult,
    count_prior_approved,
    field_completion,
    merge_invoice_rows,
    route_invoice,
    run_agentic_ab_test,
    validate_invoice,
)
from rag.adaptive_rag import load_kb
from scripts.extract_invoice_fields import read_ocr_body
from scripts.july_extract_invoice_fields import extract_row as extract_july_row


CASE_IDS = ("ocr_llm", "ocr_llm_agentic")
EVALUATED_FIELDS = tuple(
    field
    for field in FIELDNAMES
    if field not in {"source_file", "ocr_text_file", "valid_invoice", "extraction_warnings"}
)
MONEY_FIELDS = {"subtotal_value", "total_vat", "total_value"}
DATE_FIELDS = {"invoice_date", "payment_due_date", "consumption_start_date", "consumption_end_date"}


class FourCaseCandidate(BaseModel):
    case_id: Literal["ocr_llm", "ocr_llm_agentic"]
    label: str
    status: Literal["measured", "unavailable", "failed", "fallback"]
    method: str
    row: dict[str, str] = Field(default_factory=dict)
    validation_errors: list[str] = Field(default_factory=list)
    route: str = "not_measured"
    fields_retrieved_percent: float | None = None
    llm_used: bool = False
    errors: list[str] = Field(default_factory=list)


class FourCaseEvaluation(BaseModel):
    schema_version: int = 3
    generated_at: str
    source_name: str
    cases: dict[str, FourCaseCandidate]
    agent_trace: list[AgentEvent] = Field(default_factory=list)
    judge: FourCaseJudgeResult | None = None
    human_evaluation: dict[str, Any] | None = None
    human_field_evaluation: dict[str, Any] | None = None
    artifact_path: str | None = None


def _candidate(case_id: str, label: str, plan: PlanResult, *, status: str = "measured") -> FourCaseCandidate:
    return FourCaseCandidate(
        case_id=case_id,
        label=label,
        status=status,
        method=plan.method,
        row=plan.row,
        validation_errors=plan.validation_errors,
        route=plan.route,
        fields_retrieved_percent=round(plan.completion * 100, 2),
        llm_used=plan.llm_used,
    )


def _direct_llm_plan(
    baseline_row: dict[str, Any],
    model_row: dict[str, Any],
    kb_path: Path,
    provider: str,
) -> PlanResult:
    normalized = merge_invoice_rows(baseline_row, model_row)
    errors = validate_invoice(normalized)
    provider_id = canonical_provider(normalized.get("provider_name", ""))
    approved = count_prior_approved(load_kb(kb_path), provider_id, normalized)
    return PlanResult(
        name="Plan A (July)",
        method=f"{provider.title()} PDF/OCR extraction without provider RAG or agent routing",
        row=normalized,
        validation_errors=errors,
        route=route_invoice(normalized, errors, approved),
        completion=round(field_completion(normalized), 4),
        llm_used=True,
    )


def _write(result: FourCaseEvaluation, path: Path) -> FourCaseEvaluation:
    result.artifact_path = str(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result.model_dump(), indent=2, ensure_ascii=False), encoding="utf-8")
    return result


def run_four_case_evaluation(
    text_file: str | Path,
    *,
    pdf_file: str | Path | None,
    kb_path: str | Path,
    output_dir: str | Path,
    gemini_api_key: str = "",
    gemini_model: str = "gemini-3.5-flash",
    llm_provider: str = "gemini",
    llm_api_key: str | None = None,
    llm_model: str | None = None,
) -> FourCaseEvaluation:
    text_path = Path(text_file)
    pdf_path = Path(pdf_file) if pdf_file else None
    source_name, ocr_text = read_ocr_body(text_path)
    output = Path(output_dir)

    baseline_row, _ = sanitize_invoice_row(extract_july_row(text_path))
    cases: dict[str, FourCaseCandidate] = {}

    selected_provider = "openai" if llm_provider == "openai" else "gemini"
    selected_key = gemini_api_key if llm_api_key is None else llm_api_key
    selected_model = llm_model or (
        "gpt-5.6-terra" if selected_provider == "openai" else gemini_model
    )
    agent_trace: list[AgentEvent] = []
    if not selected_key or pdf_path is None or not pdf_path.exists():
        reason = f"A PDF and explicitly supplied {selected_provider.title()} API key are required."
        cases["ocr_llm"] = FourCaseCandidate(
            case_id="ocr_llm",
            label="Plan A: July OCR + LLM",
            status="unavailable",
            method=f"{selected_provider.title()} PDF/OCR extraction without agentic RAG",
            row={"ocr_text_file": str(text_path)},
            errors=[reason],
        )
        cases["ocr_llm_agentic"] = FourCaseCandidate(
            case_id="ocr_llm_agentic",
            label="Plan B: July OCR + LLM + agentic workflow",
            status="unavailable",
            method=f"Plan B supervisor with provider RAG and {selected_provider.title()} extraction",
            row={"ocr_text_file": str(text_path)},
            errors=[reason],
        )
    else:
        from scripts.second_pass_llm import second_pass_extract

        direct = second_pass_extract(
            ocr_text=ocr_text,
            pdf_file=pdf_path,
            provider_name=baseline_row.get("provider_name"),
            invoice_type=baseline_row.get("invoice_type"),
            source_file=source_name,
            api_key=selected_key,
            model=selected_model,
            provider=selected_provider,
            output_dir=output / "direct_llm",
            rag_snippets=[],
            kb_path=kb_path,
        )
        if direct.used and not direct.errors:
            direct_plan = _direct_llm_plan(
                baseline_row,
                direct.parsed,
                Path(kb_path),
                selected_provider,
            )
            direct_plan.row["ocr_text_file"] = str(text_path)
            cases["ocr_llm"] = _candidate("ocr_llm", "Plan A: July OCR + LLM", direct_plan)
        else:
            cases["ocr_llm"] = FourCaseCandidate(
                case_id="ocr_llm",
                label="Plan A: July OCR + LLM",
                status="failed",
                method=f"{selected_provider.title()} PDF/OCR extraction without agentic RAG",
                errors=list(direct.errors),
            )

        agentic = run_agentic_ab_test(
            text_path,
            pdf_file=pdf_path,
            kb_path=kb_path,
            output_dir=output / "agentic_llm",
            api_key=selected_key,
            model=selected_model,
            llm_provider=selected_provider,
            baseline_extractor=extract_july_row,
            plan_a_method="Frozen July OCR plus deterministic extraction",
        )
        agent_trace = agentic.plan_b_trace
        cases["ocr_llm_agentic"] = _candidate(
            "ocr_llm_agentic",
            "Plan B: July OCR + LLM + agentic workflow",
            agentic.plan_b,
            status="measured" if agentic.plan_b.llm_used else "fallback",
        )

    ordered_cases = {case_id: cases[case_id] for case_id in CASE_IDS}
    result = FourCaseEvaluation(
        generated_at=datetime.now(timezone.utc).isoformat(),
        source_name=source_name,
        cases=ordered_cases,
        agent_trace=agent_trace,
    )
    artifact = output / f"{text_path.stem.removesuffix('_selected_text')}_ab_source_evaluation.json"
    return _write(result, artifact)


def judge_four_case_artifact(
    artifact_path: str | Path,
    *,
    base_url: str = "",
    api_key: str = "",
    model: str = "",
    provider: str = "openai_compatible",
    judge_runner: JudgeCaller | None = None,
) -> FourCaseEvaluation:
    path = Path(artifact_path)
    result = FourCaseEvaluation.model_validate_json(path.read_text(encoding="utf-8"))
    text_path = next(
        (
            Path(candidate.row.get("ocr_text_file", ""))
            for case_id in CASE_IDS
            if (candidate := result.cases.get(case_id)) is not None
            and candidate.row.get("ocr_text_file")
        ),
        Path(),
    )
    if not text_path.exists() or not text_path.is_file():
        raise FileNotFoundError("The OCR evidence file recorded by the A/B run is unavailable.")
    _, ocr_text = read_ocr_body(text_path)
    result.judge = run_four_case_judge(
        ocr_text=ocr_text,
        candidates={case_id: case.model_dump() for case_id, case in result.cases.items()},
        base_url=base_url,
        api_key=api_key,
        model=model,
        provider=provider,
        caller=judge_runner,
    )
    return _write(result, path)


def record_four_case_verdict(
    artifact_path: str | Path,
    best_case: Literal["ocr_llm", "ocr_llm_agentic", "tie", "inconclusive"],
    note: str = "",
) -> FourCaseEvaluation:
    path = Path(artifact_path)
    result = FourCaseEvaluation.model_validate_json(path.read_text(encoding="utf-8"))
    result.human_evaluation = {
        "best_case": best_case,
        "reviewer_note": note.strip(),
        "reviewed_at": datetime.now(timezone.utc).isoformat(),
    }
    return _write(result, path)


def _evaluation_value(field: str, value: Any) -> str:
    text = normalize_space(str(value or ""))
    if text.casefold() in {"", "null", "none", "nan", "<absent>"}:
        return "null"
    if field in MONEY_FIELDS:
        normalized = normalize_money(text)
        return normalized if normalized is not None else fold_text(text)
    if field in DATE_FIELDS:
        for pattern in (r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})$", r"^(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})$"):
            match = re.match(pattern, text)
            if not match:
                continue
            parts = [int(part) for part in match.groups()]
            year, month, day = parts if len(match.group(1)) == 4 else (parts[2], parts[1], parts[0])
            try:
                return datetime(year, month, day).date().isoformat()
            except ValueError:
                break
    if field == "currency":
        return text.upper()
    return fold_text(text)


def record_four_case_field_verdicts(
    artifact_path: str | Path,
    verified_values: dict[str, Any],
) -> FourCaseEvaluation:
    """Score all available candidates against values manually checked in the source."""
    path = Path(artifact_path)
    result = FourCaseEvaluation.model_validate_json(path.read_text(encoding="utf-8"))
    verified = {
        field: str(verified_values[field]).strip()
        for field in EVALUATED_FIELDS
        if field in verified_values and str(verified_values[field]).strip()
    }
    if not verified:
        raise ValueError("Enter at least one source-verified field value before scoring.")

    scores: dict[str, Any] = {}
    for case_id, candidate in result.cases.items():
        if candidate.status not in {"measured", "fallback"}:
            scores[case_id] = {"status": candidate.status, "accuracy_percent": None, "correct": 0}
            continue
        matches = {
            field: _evaluation_value(field, candidate.row.get(field)) == _evaluation_value(field, expected)
            for field, expected in verified.items()
        }
        correct = sum(matches.values())
        scores[case_id] = {
            "status": candidate.status,
            "accuracy_percent": round(correct / len(verified) * 100, 2),
            "correct": correct,
            "matches": matches,
        }

    result.human_field_evaluation = {
        "verified_fields": verified,
        "verified_field_count": len(verified),
        "case_scores": scores,
        "reviewed_at": datetime.now(timezone.utc).isoformat(),
    }
    return _write(result, path)
