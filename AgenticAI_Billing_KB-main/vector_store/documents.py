from __future__ import annotations

from dataclasses import dataclass
from typing import Any


try:
    from llama_index.core import Document as LlamaIndexDocument
except ImportError:  # pragma: no cover - exercised when optional deps are absent.
    LlamaIndexDocument = None


@dataclass
class VectorDocument:
    """Small fallback document used when LlamaIndex is not installed."""

    text: str
    metadata: dict[str, Any]


def make_document(text: str, metadata: dict[str, Any]) -> Any:
    if LlamaIndexDocument is not None:
        return LlamaIndexDocument(text=text, metadata=metadata)
    return VectorDocument(text=text, metadata=metadata)


def _provider_metadata(provider_id: str, provider: dict[str, Any], section: str) -> dict[str, Any]:
    return {
        "source": "rag/knowledge_base.json",
        "provider_id": provider_id,
        "provider_name": provider.get("provider_name") or provider_id.replace("_", " ").title(),
        "department": "Invoice Processing",
        "document_type": "provider_memory",
        "memory_section": section,
    }


def build_static_documents() -> list[Any]:
    return [
        make_document(
            text="""
Invoice OCR Pipeline

Raw invoice PDFs and images belong in data/data_raw. The OCR pipeline materializes
generated searchable PDFs in data/data_pdf, selected OCR text in data/data_txt,
and OCR reports in data/data_processed/reports. Generated outputs must never be
written inside data/data_raw.
""",
            metadata={
                "source": "invoice_ocr_pipeline",
                "title": "Invoice OCR Pipeline",
                "version": "1.0",
                "department": "Invoice Processing",
                "document_type": "architecture",
            },
        ),
        make_document(
            text="""
Structured Invoice Field Extraction

scripts/extract_invoice_fields.py reads OCR text files from data/data_txt and
writes the canonical CSV to data/data_processed/invoice_structured_fields.csv.
The CSV field order is defined by invoice_parser.schema.FIELDNAMES.
""",
            metadata={
                "source": "invoice_field_extraction",
                "title": "Structured Invoice Field Extraction",
                "version": "1.0",
                "department": "Invoice Processing",
                "document_type": "architecture",
            },
        ),
        make_document(
            text="""
Adaptive RAG Provider Memory

rag/adaptive_rag.py maintains local provider memory in rag/knowledge_base.json.
The vector store is generated from that memory and retrieves provider-specific
tips, OCR corrections, layouts, reviewer feedback, and field correction patterns.
""",
            metadata={
                "source": "adaptive_rag_memory",
                "title": "Adaptive RAG Provider Memory",
                "version": "1.0",
                "department": "Invoice Processing",
                "document_type": "architecture",
            },
        ),
        make_document(
            text="""
Invoice Review Dashboard

scripts/dashboard.py serves a local review dashboard for import, review,
correction, approval, provider-memory updates, Gemini PDF second pass, and export.
Dashboard actions must not expose secrets or full private invoice contents.
""",
            metadata={
                "source": "invoice_review_dashboard",
                "title": "Invoice Review Dashboard",
                "version": "1.0",
                "department": "Invoice Processing",
                "document_type": "architecture",
            },
        ),
    ]


def provider_memory_documents(kb: dict[str, Any]) -> list[Any]:
    documents: list[Any] = []
    for provider_id, provider in kb.get("providers", {}).items():
        provider_name = provider.get("provider_name") or provider_id.replace("_", " ").title()

        for tip in provider.get("provider_specific_extraction_tips", []):
            documents.append(
                make_document(
                    text=f"{provider_name} extraction tip: {tip}",
                    metadata=_provider_metadata(provider_id, provider, "provider_tip"),
                )
            )

        for wrong, right in provider.get("common_ocr_corrections", {}).items():
            documents.append(
                make_document(
                    text=f'{provider_name} OCR correction: replace "{wrong}" with "{right}".',
                    metadata=_provider_metadata(provider_id, provider, "ocr_correction"),
                )
            )

        for layout in provider.get("known_invoice_layouts", []):
            fields = ", ".join(layout.get("fields_seen", []))
            invoice_type = layout.get("invoice_type", "invoice")
            documents.append(
                make_document(
                    text=f"{provider_name} {invoice_type} layout commonly includes fields: {fields}.",
                    metadata={
                        **_provider_metadata(provider_id, provider, "known_layout"),
                        "invoice_type": invoice_type,
                    },
                )
            )

        for field_name, pattern in provider.get("field_correction_patterns", {}).items():
            examples = "; ".join(
                f"{item.get('old_value', 'null')} -> {item.get('corrected_value', 'null')}"
                for item in pattern.get("recent_examples", [])[:5]
            )
            documents.append(
                make_document(
                    text=(
                        f"{provider_name} field correction pattern for {field_name}: "
                        f"{pattern.get('total_corrections', 0)} correction(s). "
                        f"Recent examples: {examples or 'none'}."
                    ),
                    metadata={
                        **_provider_metadata(provider_id, provider, "field_correction_pattern"),
                        "field_name": field_name,
                    },
                )
            )

        for item in provider.get("human_reviewer_feedback", []):
            documents.append(
                make_document(
                    text=(
                        f"{provider_name} reviewer feedback for {item.get('field_name', 'unknown_field')}: "
                        f"{item.get('old_value', 'null')} -> {item.get('corrected_value', 'null')}. "
                        f"{item.get('note', '')}".strip()
                    ),
                    metadata={
                        **_provider_metadata(provider_id, provider, "human_feedback"),
                        "field_name": item.get("field_name", "unknown_field"),
                    },
                )
            )
    return documents


DOCUMENTS = build_static_documents()
