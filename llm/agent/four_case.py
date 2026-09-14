"""Four-case OCR/LLM/agentic experiment with an independent advisory judge."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from invoice_parser.providers import canonical_provider
from llm.agent.judge import run_four_case_judge
from llm.agent.models import FourCaseJudgeResult, JudgeCaller
from llm.agent.workflow import (
    PlanResult,
    count_prior_approved,
    field_completion,
    route_invoice,
    run_agentic_ab_test,
    validate_invoice,
)
from rag.adaptive_rag import load_kb
from scripts.extract_invoice_fields import read_ocr_body


CASE_IDS = ("ocr_rules", "ocr_llm", "ocr_agentic", "ocr_llm_agentic")


class FourCaseCandidate(BaseModel):
    case_id: Literal["ocr_rules", "ocr_llm", "ocr_agentic", "ocr_llm_agentic"]
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
    schema_version: int = 1
    generated_at: str
    source_name: str
    cases: dict[str, FourCaseCandidate]
    judge: FourCaseJudgeResult | None = None
    human_evaluation: dict[str, Any] | None = None
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


def _direct_llm_plan(row: dict[str, Any], kb_path: Path) -> PlanResult:
    normalized = {key: str(value) for key, value in row.items() if isinstance(value, (str, int, float, bool))}
    errors = validate_invoice(normalized)
    provider_id = canonical_provider(normalized.get("provider_name", ""))
    approved = count_prior_approved(load_kb(kb_path), provider_id, normalized)
    return PlanResult(
        name="OCR + LLM",
        method="Gemini PDF/OCR extraction without provider RAG or agent routing",
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
) -> FourCaseEvaluation:
    text_path = Path(text_file)
    pdf_path = Path(pdf_file) if pdf_file else None
    source_name, ocr_text = read_ocr_body(text_path)
    output = Path(output_dir)

    offline = run_agentic_ab_test(
        text_path,
        pdf_file=pdf_path,
        kb_path=kb_path,
        output_dir=output / "offline",
        api_key="",
    )
    cases: dict[str, FourCaseCandidate] = {
        "ocr_rules": _candidate("ocr_rules", "OCR + deterministic fields", offline.plan_a),
        "ocr_agentic": _candidate("ocr_agentic", "OCR + agentic rules", offline.plan_b),
    }

    if not gemini_api_key or pdf_path is None or not pdf_path.exists():
        reason = "A PDF and explicitly supplied Gemini API key are required."
        cases["ocr_llm"] = FourCaseCandidate(
            case_id="ocr_llm",
            label="OCR + LLM",
            status="unavailable",
            method="Gemini PDF/OCR extraction without agentic RAG",
            errors=[reason],
        )
        cases["ocr_llm_agentic"] = FourCaseCandidate(
            case_id="ocr_llm_agentic",
            label="OCR + LLM + agentic workflow",
            status="unavailable",
            method="Plan B supervisor with provider RAG and Gemini extraction",
            errors=[reason],
        )
    else:
        from scripts.second_pass_llm import second_pass_extract

        direct = second_pass_extract(
            ocr_text=ocr_text,
            pdf_file=pdf_path,
            provider_name=offline.plan_a.row.get("provider_name"),
            invoice_type=offline.plan_a.row.get("invoice_type"),
            source_file=source_name,
            api_key=gemini_api_key,
            model=gemini_model,
            output_dir=output / "direct_llm",
            rag_snippets=[],
            kb_path=kb_path,
        )
        if direct.used and not direct.errors:
            direct_plan = _direct_llm_plan(direct.parsed, Path(kb_path))
            direct_plan.row["ocr_text_file"] = str(text_path)
            cases["ocr_llm"] = _candidate("ocr_llm", "OCR + LLM", direct_plan)
        else:
            cases["ocr_llm"] = FourCaseCandidate(
                case_id="ocr_llm",
                label="OCR + LLM",
                status="failed",
                method="Gemini PDF/OCR extraction without agentic RAG",
                errors=list(direct.errors),
            )

        agentic = run_agentic_ab_test(
            text_path,
            pdf_file=pdf_path,
            kb_path=kb_path,
            output_dir=output / "agentic_llm",
            api_key=gemini_api_key,
            model=gemini_model,
        )
        cases["ocr_llm_agentic"] = _candidate(
            "ocr_llm_agentic",
            "OCR + LLM + agentic workflow",
            agentic.plan_b,
            status="measured" if agentic.plan_b.llm_used else "fallback",
        )

    ordered_cases = {case_id: cases[case_id] for case_id in CASE_IDS}
    result = FourCaseEvaluation(
        generated_at=datetime.now(timezone.utc).isoformat(),
        source_name=source_name,
        cases=ordered_cases,
    )
    artifact = output / f"{text_path.stem.removesuffix('_selected_text')}_four_case.json"
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
    text_path = Path(result.cases["ocr_rules"].row.get("ocr_text_file", ""))
    if not text_path.exists() or not text_path.is_file():
        raise FileNotFoundError("The OCR evidence file recorded by the four-case run is unavailable.")
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
    best_case: Literal[
        "ocr_rules", "ocr_llm", "ocr_agentic", "ocr_llm_agentic", "tie", "inconclusive"
    ],
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
