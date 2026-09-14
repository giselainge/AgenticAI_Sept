from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from llm.agent.models import NormalizedInvoiceExtraction


class SecondPassExtractRequest(BaseModel):
    """Request schema for running a Gemini PDF second pass."""

    pdf_file: Path
    text_file: Path | None = None
    provider_name: str | None = None
    invoice_type: str | None = None
    source_file: str | Path | None = None
    model: str | None = None
    output_dir: Path | None = None


class SecondPassExtractOutput(BaseModel):
    """Response schema for a completed or failed second-pass run."""

    used: bool
    model: str
    parsed: NormalizedInvoiceExtraction | dict[str, Any] = Field(default_factory=dict)
    raw_response_path: str | None = None
    normalized_output_path: str | None = None
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
