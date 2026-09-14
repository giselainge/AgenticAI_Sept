from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field

from invoice_parser.schema import FIELDNAMES, NULL_VALUE

DEFAULT_MODEL = "gemini-3.5-flash"

InvoiceType = Literal["electricity", "water", "natural gas", "telecom", "unsupported"]
ReviewStatus = Literal["manual_review_required", "ready_for_review"]
GeminiCaller = Callable[..., str]
JudgeCaller = Callable[..., str | dict[str, Any]]


class RagSnippet(BaseModel):
    """Small provider-memory hint included in an LLM extraction prompt."""

    type: str = "context"
    provider: str = ""
    text: str

    model_config = ConfigDict(extra="allow")


class LlmRagContext(BaseModel):
    """Provider-specific RAG context converted for LLM prompt use."""

    provider_id: str
    provider_name: str
    score: int = 0
    snippets: list[RagSnippet] = Field(default_factory=list)


class GeminiPromptContext(BaseModel):
    """Typed prompt input for the PDF second-pass extraction."""

    ocr_text: str = ""
    provider_name: str | None = None
    invoice_type: str | None = None
    rag_snippets: list[RagSnippet] = Field(default_factory=list)


class GeminiExtractionPayload(BaseModel):
    """Flexible key-value extraction payload parsed from model text."""

    source_file: str | None = None
    ocr_text_file: str | None = None
    valid_invoice: bool | str | None = None
    invoice_type: str | None = None
    invoice_number: str | None = None
    invoice_date: str | None = None
    currency: str | None = None
    payment_due_date: str | None = None
    provider_name: str | None = None
    provider_vat_number: str | None = None
    provider_address: str | None = None
    buyer_name: str | None = None
    buyer_vat_number: str | None = None
    buyer_address: str | None = None
    service_plan_name: str | None = None
    consumption_start_date: str | None = None
    consumption_end_date: str | None = None
    units_of_consumption: str | None = None
    unit_type: str | None = None
    subtotal_value: str | None = None
    total_vat: str | None = None
    total_value: str | None = None
    extraction_warnings: str | None = None
    confidence: dict[str, Any] = Field(default_factory=dict)
    uncertain_fields: list[str] = Field(default_factory=list)

    model_config = ConfigDict(extra="allow")

    def invoice_fields(self) -> dict[str, Any]:
        return {field: getattr(self, field, NULL_VALUE) for field in FIELDNAMES}


class NormalizedInvoiceExtraction(BaseModel):
    """Normalized structured output saved as the Gemini second-pass artifact."""

    source_file: str = NULL_VALUE
    ocr_text_file: str = NULL_VALUE
    valid_invoice: str = "true"
    invoice_type: str = NULL_VALUE
    invoice_number: str = NULL_VALUE
    invoice_date: str = NULL_VALUE
    currency: str = NULL_VALUE
    payment_due_date: str = NULL_VALUE
    provider_name: str = NULL_VALUE
    provider_vat_number: str = NULL_VALUE
    provider_address: str = NULL_VALUE
    buyer_name: str = NULL_VALUE
    buyer_vat_number: str = NULL_VALUE
    buyer_address: str = NULL_VALUE
    service_plan_name: str = NULL_VALUE
    consumption_start_date: str = NULL_VALUE
    consumption_end_date: str = NULL_VALUE
    units_of_consumption: str = NULL_VALUE
    unit_type: str = NULL_VALUE
    subtotal_value: str = NULL_VALUE
    total_vat: str = NULL_VALUE
    total_value: str = NULL_VALUE
    extraction_warnings: str = NULL_VALUE
    confidence: dict[str, Any] = Field(default_factory=dict)
    uncertain_fields: list[str] = Field(default_factory=list)
    missing_fields: list[str] = Field(default_factory=list)
    validation_errors: list[str] = Field(default_factory=list)
    review_status: ReviewStatus = "manual_review_required"

    model_config = ConfigDict(extra="allow")

    def as_artifact_dict(self) -> dict[str, Any]:
        return self.model_dump()


class SecondPassResult(BaseModel):
    """Runtime result returned by the LLM second-pass workflow."""

    used: bool = False
    model: str | None = DEFAULT_MODEL
    raw_response: str = ""
    parsed: dict[str, Any] = Field(default_factory=dict)
    text: str = ""
    raw_response_path: str | None = None
    normalized_output_path: str | None = None
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class SecondPassArtifactPaths(BaseModel):
    """Stable artifact filenames for one PDF second-pass run."""

    raw_response_path: Path
    normalized_output_path: Path


class JudgeFieldDecision(BaseModel):
    """One evidence-based comparison made by the independent A/B judge."""

    field: str
    winner: Literal["plan_a", "plan_b", "tie", "unverifiable"]
    reason: str


class LlmJudgeResult(BaseModel):
    """Auditable recommendation from an LLM judge; it never replaces human review."""

    status: Literal["completed", "unavailable", "failed"]
    model: str | None = None
    evidence_scope: Literal["ocr_text"] = "ocr_text"
    preferred_plan: Literal["plan_a", "plan_b", "tie", "inconclusive"] = "inconclusive"
    plan_a_score: float | None = Field(default=None, ge=0, le=10)
    plan_b_score: float | None = Field(default=None, ge=0, le=10)
    confidence: float | None = Field(default=None, ge=0, le=1)
    summary: str = ""
    field_decisions: list[JudgeFieldDecision] = Field(default_factory=list)
    human_verdict_required: bool = True
    judged_at: str | None = None
    errors: list[str] = Field(default_factory=list)
