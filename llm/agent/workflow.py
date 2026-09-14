"""Experimental Plan B: an explicit, inspectable multi-agent invoice workflow."""

from __future__ import annotations

import json
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Literal

from pydantic import BaseModel, Field

from invoice_parser.paths import DEFAULT_AGENTIC_AB_DIR
from invoice_parser.providers import canonical_provider
from invoice_parser.schema import FIELDNAMES, NULL_VALUE
from llm.agent.judge import run_llm_judge
from llm.agent.models import JudgeCaller, LlmJudgeResult, RagSnippet, SecondPassResult
from rag.adaptive_rag import build_llm_rag_snippets, load_kb, row_signature
from scripts.extract_invoice_fields import extract_invoice_type, extract_row, read_ocr_body


SUPPORTED_TYPES = {"electricity", "water", "natural gas", "telecom"}
CRITICAL_FIELDS = {
    "invoice_type",
    "invoice_number",
    "invoice_date",
    "currency",
    "provider_name",
    "provider_vat_number",
    "total_value",
}
DATE_FIELDS = {"invoice_date", "payment_due_date", "consumption_start_date", "consumption_end_date"}
MONEY_FIELDS = {"subtotal_value", "total_vat", "total_value"}
COMPARABLE_FIELDS = [field for field in FIELDNAMES if field not in {"source_file", "ocr_text_file"}]
APPROVED_DECISIONS = {"human_approved", "automatic_approved"}
AUTO_APPROVAL_MIN_VALIDATED = 5


class AgentEvent(BaseModel):
    sequence: int
    agent: str
    status: Literal["completed", "fallback", "skipped"]
    decision: str
    duration_ms: float
    metrics: dict[str, Any] = Field(default_factory=dict)


class PlanResult(BaseModel):
    name: str
    method: str
    row: dict[str, str]
    validation_errors: list[str] = Field(default_factory=list)
    route: str
    completion: float
    llm_used: bool = False


class AgenticABResult(BaseModel):
    schema_version: int = 2
    generated_at: str
    source_name: str
    plan_a: PlanResult
    plan_b: PlanResult
    comparison: dict[str, Any]
    plan_b_trace: list[AgentEvent]
    artifact_path: str | None = None
    preprocessing: dict[str, Any] | None = None
    audit_log: list[dict[str, Any]] = Field(default_factory=list)
    llm_judge: LlmJudgeResult | None = None
    evaluation: dict[str, Any] | None = None


@dataclass
class AgentContext:
    text_path: Path
    pdf_path: Path | None
    source_name: str
    ocr_text: str
    baseline_row: dict[str, str]
    working_row: dict[str, str]
    kb_path: Path
    output_dir: Path
    api_key: str
    model: str | None
    llm_provider: str = "gemini"
    llm_runner: Callable[..., SecondPassResult] | None = None
    provider_id: str = "unknown"
    rag_snippets: list[RagSnippet] = field(default_factory=list)
    validation_errors: list[str] = field(default_factory=list)
    route: str = "manual_review_required"
    prior_approved_count: int = 0
    llm_used: bool = False


def _is_missing(value: Any) -> bool:
    return str(value or "").strip().lower() in {"", "null", "none", "nan"}


def _date(value: str) -> datetime | None:
    try:
        return datetime.strptime(value, "%Y-%m-%d")
    except (TypeError, ValueError):
        return None


def _money(value: Any) -> Decimal | None:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return amount if amount.is_finite() else None


def validate_invoice(row: dict[str, str]) -> list[str]:
    """Plan B validation agent rules, kept independent from the dashboard UI."""
    errors: list[str] = []
    invoice_type = str(row.get("invoice_type") or "")
    if invoice_type not in SUPPORTED_TYPES:
        errors.append("Unsupported document: invoice_type must be electricity, water, natural gas, or telecom.")
    if str(row.get("valid_invoice") or "").lower() != "true":
        errors.append("Invoice is not classified as valid.")
    for name in sorted(CRITICAL_FIELDS):
        if _is_missing(row.get(name)):
            errors.append(f"{name} is required for approval.")
    parsed_dates: dict[str, datetime] = {}
    for name in DATE_FIELDS:
        value = row.get(name)
        if _is_missing(value):
            continue
        parsed = _date(str(value))
        if parsed is None:
            errors.append(f"{name} must be a real date in YYYY-MM-DD format.")
        else:
            parsed_dates[name] = parsed
    if parsed_dates.get("payment_due_date") and parsed_dates.get("invoice_date"):
        if parsed_dates["payment_due_date"] < parsed_dates["invoice_date"]:
            errors.append("payment_due_date cannot be before invoice_date.")
    if parsed_dates.get("consumption_start_date") and parsed_dates.get("consumption_end_date"):
        if parsed_dates["consumption_start_date"] > parsed_dates["consumption_end_date"]:
            errors.append("consumption_start_date cannot be after consumption_end_date.")
    amounts: dict[str, Decimal] = {}
    for name in MONEY_FIELDS:
        value = row.get(name)
        if _is_missing(value):
            continue
        parsed = _money(value)
        if parsed is None or parsed < 0:
            errors.append(f"{name} must be a non-negative finite number.")
        else:
            amounts[name] = parsed
    if all(name in amounts for name in ("subtotal_value", "total_vat", "total_value")):
        delta = abs(amounts["subtotal_value"] + amounts["total_vat"] - amounts["total_value"])
        if delta > Decimal("0.02"):
            errors.append("subtotal_value plus total_vat is inconsistent with total_value.")
    if not _is_missing(row.get("extraction_warnings")):
        errors.append("Extraction warnings require human review.")
    return errors


def field_completion(row: dict[str, str]) -> float:
    measured = [field for field in COMPARABLE_FIELDS if field not in {"valid_invoice", "extraction_warnings"}]
    return sum(not _is_missing(row.get(field)) for field in measured) / len(measured)


def count_prior_approved(kb: dict[str, Any], provider_id: str, current_row: dict[str, str]) -> int:
    """Count distinct prior approvals; legacy extracted rows never qualify."""
    if provider_id == "unknown":
        return 0
    current_signature = row_signature(current_row)
    seen: set[str] = set()
    for item in kb.get("providers", {}).get(provider_id, {}).get("previously_validated_invoices", []):
        if item.get("review_decision") not in APPROVED_DECISIONS:
            continue
        if str(item.get("valid_invoice") or "").lower() != "true":
            continue
        signature = str(item.get("signature") or "")
        if not signature or signature == current_signature:
            continue
        seen.add(signature)
    return len(seen)


def route_invoice(row: dict[str, str], errors: list[str], approved_count: int) -> str:
    if str(row.get("invoice_type") or "") not in SUPPORTED_TYPES or str(row.get("valid_invoice") or "").lower() != "true":
        return "unsupported_rejected"
    if approved_count >= AUTO_APPROVAL_MIN_VALIDATED and not errors:
        return "automatic_approval_eligible"
    return "manual_review_required"


class PipelineAgent(ABC):
    name: str

    @abstractmethod
    def run(self, context: AgentContext, sequence: int) -> AgentEvent:
        raise NotImplementedError

    def event(
        self,
        sequence: int,
        started: float,
        status: Literal["completed", "fallback", "skipped"],
        decision: str,
        **metrics: Any,
    ) -> AgentEvent:
        return AgentEvent(
            sequence=sequence,
            agent=self.name,
            status=status,
            decision=decision,
            duration_ms=round((time.perf_counter() - started) * 1000, 3),
            metrics=metrics,
        )


class ClassificationAgent(PipelineAgent):
    name = "classification_agent"

    def run(self, context: AgentContext, sequence: int) -> AgentEvent:
        started = time.perf_counter()
        detected = extract_invoice_type(context.text_path.stem, context.ocr_text)
        context.working_row["invoice_type"] = detected
        if detected == "unsupported":
            context.working_row["valid_invoice"] = "false"
        return self.event(
            sequence,
            started,
            "completed",
            "supported_candidate" if detected in SUPPORTED_TYPES else "unsupported_candidate",
            invoice_type=detected,
        )


class ProviderMemoryAgent(PipelineAgent):
    name = "provider_memory_agent"

    def run(self, context: AgentContext, sequence: int) -> AgentEvent:
        started = time.perf_counter()
        provider_name = context.working_row.get("provider_name", "")
        context.provider_id = canonical_provider(provider_name, context.ocr_text)
        if context.provider_id == "unknown":
            return self.event(sequence, started, "skipped", "provider_unidentified", snippets=0)
        try:
            context.rag_snippets = build_llm_rag_snippets(
                context.ocr_text,
                provider_hint=provider_name,
                invoice_type=context.working_row.get("invoice_type", ""),
                kb_path=context.kb_path,
            )
            return self.event(
                sequence,
                started,
                "completed",
                "provider_context_retrieved",
                provider_id=context.provider_id,
                snippets=len(context.rag_snippets),
            )
        except Exception as exc:
            return self.event(sequence, started, "fallback", "provider_context_unavailable", error=type(exc).__name__)


class ExtractionAgent(PipelineAgent):
    name = "extraction_agent"

    def run(self, context: AgentContext, sequence: int) -> AgentEvent:
        started = time.perf_counter()
        if context.pdf_path is None or not context.pdf_path.exists() or not context.api_key:
            reasons = []
            if context.pdf_path is None or not context.pdf_path.exists():
                reasons.append("pdf_unavailable")
            if not context.api_key:
                reasons.append("api_key_unavailable")
            return self.event(sequence, started, "fallback", "deterministic_extraction_retained", reasons=reasons)

        runner = context.llm_runner
        if runner is None:
            from scripts.second_pass_llm import second_pass_extract

            runner = second_pass_extract
        result = runner(
            ocr_text=context.ocr_text,
            pdf_file=context.pdf_path,
            provider_name=None if _is_missing(context.working_row.get("provider_name")) else context.working_row.get("provider_name"),
            invoice_type=None if _is_missing(context.working_row.get("invoice_type")) else context.working_row.get("invoice_type"),
            source_file=context.source_name,
            api_key=context.api_key,
            model=context.model,
            provider=context.llm_provider,
            output_dir=context.output_dir / "llm",
            rag_snippets=context.rag_snippets,
            kb_path=context.kb_path,
        )
        if result.errors or not result.used:
            return self.event(
                sequence,
                started,
                "fallback",
                "llm_failed_deterministic_extraction_retained",
                errors=list(result.errors),
            )
        merged = dict(context.working_row)
        for name in FIELDNAMES:
            if name in result.parsed:
                merged[name] = str(result.parsed[name])
        merged["ocr_text_file"] = str(context.text_path)
        context.working_row = merged
        context.provider_id = canonical_provider(merged.get("provider_name", ""), context.ocr_text)
        context.llm_used = True
        return self.event(
            sequence,
            started,
            "completed",
            "llm_extraction_selected",
            model=result.model,
            fields_present=sum(not _is_missing(merged.get(name)) for name in COMPARABLE_FIELDS),
        )


class ValidationAgent(PipelineAgent):
    name = "validation_agent"

    def run(self, context: AgentContext, sequence: int) -> AgentEvent:
        started = time.perf_counter()
        context.validation_errors = validate_invoice(context.working_row)
        return self.event(
            sequence,
            started,
            "completed",
            "validation_passed" if not context.validation_errors else "validation_failed",
            error_count=len(context.validation_errors),
        )


class ReviewRoutingAgent(PipelineAgent):
    name = "review_routing_agent"

    def run(self, context: AgentContext, sequence: int) -> AgentEvent:
        started = time.perf_counter()
        context.provider_id = canonical_provider(context.working_row.get("provider_name", ""), context.ocr_text)
        context.prior_approved_count = count_prior_approved(
            load_kb(context.kb_path), context.provider_id, context.working_row
        )
        context.route = route_invoice(context.working_row, context.validation_errors, context.prior_approved_count)
        return self.event(
            sequence,
            started,
            "completed",
            context.route,
            prior_approved_invoices=context.prior_approved_count,
            threshold=AUTO_APPROVAL_MIN_VALIDATED,
        )


class PlanBOrchestrator:
    """Runs a fixed auditable state machine; agents communicate through typed state."""

    def __init__(self, agents: list[PipelineAgent] | None = None) -> None:
        self.agents = agents or [
            ClassificationAgent(),
            ProviderMemoryAgent(),
            ExtractionAgent(),
            ValidationAgent(),
            ReviewRoutingAgent(),
        ]

    def run(self, context: AgentContext) -> list[AgentEvent]:
        return [agent.run(context, sequence) for sequence, agent in enumerate(self.agents, start=1)]


def _plan(name: str, method: str, row: dict[str, str], kb: dict[str, Any], *, llm_used: bool = False) -> PlanResult:
    errors = validate_invoice(row)
    provider_id = canonical_provider(row.get("provider_name", ""))
    approved = count_prior_approved(kb, provider_id, row)
    return PlanResult(
        name=name,
        method=method,
        row=row,
        validation_errors=errors,
        route=route_invoice(row, errors, approved),
        completion=round(field_completion(row), 4),
        llm_used=llm_used,
    )


def _comparison(plan_a: PlanResult, plan_b: PlanResult) -> dict[str, Any]:
    changes = [
        {"field": name, "plan_a": plan_a.row.get(name, NULL_VALUE), "plan_b": plan_b.row.get(name, NULL_VALUE)}
        for name in COMPARABLE_FIELDS
        if str(plan_a.row.get(name, NULL_VALUE)) != str(plan_b.row.get(name, NULL_VALUE))
    ]
    return {
        "changed_field_count": len(changes),
        "changed_fields": changes,
        "completion_delta": round(plan_b.completion - plan_a.completion, 4),
        "validation_error_delta": len(plan_b.validation_errors) - len(plan_a.validation_errors),
        "route_changed": plan_a.route != plan_b.route,
        "accuracy_requires_human_verdict": True,
    }


def run_agentic_ab_test(
    text_file: str | Path,
    *,
    pdf_file: str | Path | None = None,
    kb_path: str | Path,
    output_dir: str | Path = DEFAULT_AGENTIC_AB_DIR,
    api_key: str = "",
    model: str | None = None,
    llm_provider: str = "gemini",
    llm_runner: Callable[..., SecondPassResult] | None = None,
) -> AgenticABResult:
    text_path = Path(text_file)
    if not text_path.exists():
        raise FileNotFoundError(f"OCR text file does not exist: {text_path}")
    source_name, ocr_text = read_ocr_body(text_path)
    baseline_row = extract_row(text_path)
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    context = AgentContext(
        text_path=text_path,
        pdf_path=Path(pdf_file) if pdf_file else None,
        source_name=source_name,
        ocr_text=ocr_text,
        baseline_row=dict(baseline_row),
        working_row=dict(baseline_row),
        kb_path=Path(kb_path),
        output_dir=output_path,
        api_key=api_key,
        model=model,
        llm_provider=llm_provider,
        llm_runner=llm_runner,
    )
    trace = PlanBOrchestrator().run(context)
    kb = load_kb(kb_path)
    plan_a = _plan("Plan A", "OCR plus deterministic extraction", baseline_row, kb)
    plan_b = PlanResult(
        name="Plan B",
        method=f"coded agent orchestration with provider RAG and optional {llm_provider.title()} PDF extraction",
        row=context.working_row,
        validation_errors=context.validation_errors,
        route=context.route,
        completion=round(field_completion(context.working_row), 4),
        llm_used=context.llm_used,
    )
    result = AgenticABResult(
        generated_at=datetime.now(timezone.utc).isoformat(),
        source_name=source_name,
        plan_a=plan_a,
        plan_b=plan_b,
        comparison=_comparison(plan_a, plan_b),
        plan_b_trace=trace,
    )
    stem = text_path.stem.removesuffix("_selected_text")
    artifact = output_path / f"{stem}_agentic_ab.json"
    result.artifact_path = str(artifact)
    artifact.write_text(json.dumps(result.model_dump(), indent=2, ensure_ascii=False), encoding="utf-8")
    return result


def record_ab_verdict(
    artifact_path: str | Path,
    preferred_plan: Literal["plan_a", "plan_b", "tie", "inconclusive"],
    note: str = "",
) -> AgenticABResult:
    path = Path(artifact_path)
    result = AgenticABResult.model_validate_json(path.read_text(encoding="utf-8"))
    result.evaluation = {
        "preferred_plan": preferred_plan,
        "reviewer_note": note.strip(),
        "reviewed_at": datetime.now(timezone.utc).isoformat(),
    }
    path.write_text(json.dumps(result.model_dump(), indent=2, ensure_ascii=False), encoding="utf-8")
    return result


def judge_ab_artifact(
    artifact_path: str | Path,
    *,
    base_url: str = "",
    api_key: str = "",
    model: str = "",
    provider: str = "openai_compatible",
    judge_runner: JudgeCaller | None = None,
) -> AgenticABResult:
    """Attach an advisory LLM-judge result without changing the human verdict."""
    path = Path(artifact_path)
    result = AgenticABResult.model_validate_json(path.read_text(encoding="utf-8"))
    text_file = Path(str(result.plan_a.row.get("ocr_text_file") or ""))
    if not text_file.exists() or not text_file.is_file():
        raise FileNotFoundError("The OCR evidence file recorded by the A/B run is unavailable.")
    _, ocr_text = read_ocr_body(text_file)
    result.llm_judge = run_llm_judge(
        ocr_text=ocr_text,
        plan_a=result.plan_a,
        plan_b=result.plan_b,
        base_url=base_url,
        api_key=api_key,
        model=model,
        provider=provider,
        caller=judge_runner,
    )
    path.write_text(json.dumps(result.model_dump(), indent=2, ensure_ascii=False), encoding="utf-8")
    return result
