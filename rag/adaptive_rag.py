from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import os
import re
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT_PATH = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT_PATH) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT_PATH))

from invoice_parser.paths import DEFAULT_OUTPUT_DIR, DEFAULT_VECTOR_STORE_DIR, PROJECT_ROOT
from invoice_parser.schema import FIELDNAMES, NULL_VALUE
from invoice_parser.providers import canonical_provider, provider_display_name
from llm.agent.models import LlmRagContext, RagSnippet


DEFAULT_KB = Path(os.getenv("RAG_KB_PATH", str(PROJECT_ROOT / "rag" / "knowledge_base.json")))
DEFAULT_VALIDATED_CSV = DEFAULT_OUTPUT_DIR / "invoice_structured_fields.csv"
DEFAULT_VECTOR_INDEX = DEFAULT_VECTOR_STORE_DIR
FIELD_NAMES = [
    field
    for field in FIELDNAMES
    if field not in {"source_file", "ocr_text_file", "valid_invoice", "extraction_warnings"}
]

logger = logging.getLogger(__name__)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def fold_text(value: str | None) -> str:
    normalized = unicodedata.normalize("NFKD", value or "")
    normalized = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    return normalized.lower()


def empty_kb() -> dict[str, Any]:
    return {"version": 1, "providers": {}}


def ensure_provider(kb: dict[str, Any], provider_id: str, provider_name: str | None = None) -> dict[str, Any]:
    providers = kb.setdefault("providers", {})
    provider = providers.setdefault(
        provider_id or "unknown",
        {
            "provider_id": provider_id or "unknown",
            "provider_name": provider_display_name(provider_id or "unknown"),
            "aliases": [],
            "provider_specific_extraction_tips": [],
            "common_ocr_corrections": {},
            "known_invoice_layouts": [],
            "previously_validated_invoices": [],
            "human_reviewer_feedback": [],
            "field_correction_patterns": {},
            "validation_history": [],
        },
    )
    provider.setdefault("provider_id", provider_id or "unknown")
    provider.setdefault("provider_name", provider_display_name(provider_id or "unknown"))
    provider.setdefault("aliases", [])
    provider.setdefault("provider_specific_extraction_tips", [])
    provider.setdefault("common_ocr_corrections", {})
    provider.setdefault("known_invoice_layouts", [])
    provider.setdefault("previously_validated_invoices", [])
    provider.setdefault("human_reviewer_feedback", [])
    provider.setdefault("field_correction_patterns", {})
    provider.setdefault("validation_history", [])
    if provider_name and canonical_provider(provider_name) == provider_id and provider_id != "unknown":
        provider["provider_name"] = provider_name
        if provider_name not in provider["aliases"]:
            provider["aliases"].append(provider_name)
    return provider


def load_kb(kb_path: str | Path = DEFAULT_KB) -> dict[str, Any]:
    path = Path(kb_path)
    if not path.exists():
        return empty_kb()
    return json.loads(path.read_text(encoding="utf-8"))


def save_kb(kb: dict[str, Any], kb_path: str | Path = DEFAULT_KB) -> None:
    path = Path(kb_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(kb, indent=2, ensure_ascii=False), encoding="utf-8")


def is_non_null(value: Any) -> bool:
    return str(value or "").strip().lower() not in {"", "null", "none", "nan"}


def non_null_fields(row: dict[str, Any]) -> dict[str, Any]:
    return {field: row.get(field) for field in FIELD_NAMES if is_non_null(row.get(field))}


def row_signature(row: dict[str, Any]) -> str:
    basis = "|".join(str(row.get(field, "")) for field in ["source_file", "invoice_number", "invoice_date", "total_value"])
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


def summarize_layout(row: dict[str, Any]) -> dict[str, Any]:
    provider_id = canonical_provider(row.get("provider_name", ""), " ".join(str(value) for value in row.values()))
    invoice_type = row.get("invoice_type", NULL_VALUE)
    return {
        "layout_id": f"{provider_id}:{invoice_type}",
        "provider_id": provider_id,
        "invoice_type": invoice_type,
        "fields_seen": sorted(non_null_fields(row).keys()),
        "source_file": row.get("source_file", ""),
        "recorded_at": now_iso(),
    }


def seed_from_validated_csv(
    csv_path: str | Path = DEFAULT_VALIDATED_CSV,
    kb_path: str | Path = DEFAULT_KB,
) -> dict[str, Any]:
    path = Path(csv_path)
    if not path.exists():
        raise FileNotFoundError(f"Validated invoice CSV does not exist: {path}")

    kb = load_kb(kb_path)
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            provider_id = canonical_provider(row.get("provider_name", ""), " ".join(row.values()))
            provider = ensure_provider(kb, provider_id, row.get("provider_name"))
            signature = row_signature(row)
            entry = {
                "signature": signature,
                "source_file": row.get("source_file", ""),
                "valid_invoice": row.get("valid_invoice", NULL_VALUE),
                "validated_fields": non_null_fields(row),
                "added_at": now_iso(),
            }
            examples = provider.setdefault("previously_validated_invoices", [])
            if not any(item.get("signature") == signature for item in examples):
                examples.append(entry)
            layout = summarize_layout(row)
            layouts = provider.setdefault("known_invoice_layouts", [])
            if not any(item.get("layout_id") == layout["layout_id"] for item in layouts):
                layouts.append(layout)
    save_kb(kb, kb_path)
    return kb


def record_review_corrections(
    row: dict[str, Any],
    changes: dict[str, tuple[str, str]],
    note: str,
    kb_path: str | Path = DEFAULT_KB,
) -> list[dict[str, Any]]:
    kb = load_kb(kb_path)
    provider_id = canonical_provider(row.get("provider_name", ""), " ".join(str(value) for value in row.values()))
    provider = ensure_provider(kb, provider_id, row.get("provider_name"))
    feedback_items: list[dict[str, Any]] = []
    for field_name, (old_value, corrected_value) in changes.items():
        item = {
            "source_file": row.get("source_file", ""),
            "field_name": field_name,
            "old_value": old_value,
            "corrected_value": corrected_value,
            "note": note,
            "recorded_at": now_iso(),
        }
        provider.setdefault("human_reviewer_feedback", []).append(item)
        pattern = provider.setdefault("field_correction_patterns", {}).setdefault(
            field_name,
            {"total_corrections": 0, "recent_examples": []},
        )
        pattern["total_corrections"] = int(pattern.get("total_corrections", 0)) + 1
        pattern.setdefault("recent_examples", []).insert(
            0,
            {
                "old_value": old_value,
                "corrected_value": corrected_value,
                "source_file": row.get("source_file", ""),
                "note": note,
            },
        )
        pattern["recent_examples"] = pattern["recent_examples"][:5]
        feedback_items.append(item)
    provider.setdefault("validation_history", []).append(
        {
            "source_file": row.get("source_file", ""),
            "event": "human_review_corrections_recorded",
            "fields": sorted(changes.keys()),
            "note": note,
            "recorded_at": now_iso(),
        }
    )
    save_kb(kb, kb_path)
    return feedback_items


def _provider_score(provider_id: str, provider: dict[str, Any], query_text: str, provider_hint: str, invoice_type: str) -> int:
    query = fold_text(f"{query_text} {provider_hint}")
    score = 0
    if provider_id != "unknown" and provider_id.replace("_", " ") in query:
        score += 3
    if fold_text(provider.get("provider_name", "")) in query:
        score += 4
    for alias in provider.get("aliases", []):
        if fold_text(alias) in query:
            score += 2
    if invoice_type and any(item.get("invoice_type") == invoice_type for item in provider.get("known_invoice_layouts", [])):
        score += 1
    return score


def build_extraction_context(
    query_text: str,
    provider_hint: str | None = "",
    invoice_type: str | None = "",
    top_k: int = 3,
    kb_path: str | Path = DEFAULT_KB,
) -> dict[str, Any]:
    kb = load_kb(kb_path)
    hinted_id = canonical_provider(provider_hint, query_text)
    if hinted_id != "unknown":
        ensure_provider(kb, hinted_id, provider_hint)

    ranked = []
    for provider_id, provider in kb.get("providers", {}).items():
        if hinted_id == "unknown" or provider_id != hinted_id:
            continue
        score = _provider_score(provider_id, provider, query_text, provider_hint or "", invoice_type or "")
        if provider_id == hinted_id:
            score += 10
        ranked.append((score, provider_id, provider))
    ranked.sort(key=lambda item: item[0], reverse=True)

    providers = []
    for score, provider_id, provider in ranked[:top_k]:
        if score <= 0 and provider_id != hinted_id:
            continue
        providers.append(
            {
                "provider_id": provider_id,
                "provider_name": provider.get("provider_name", provider_display_name(provider_id)),
                "score": score,
                "provider_specific_extraction_tips": provider.get("provider_specific_extraction_tips", []),
                "common_ocr_corrections": provider.get("common_ocr_corrections", {}),
                "known_invoice_layouts": provider.get("known_invoice_layouts", []),
                "validated_examples": provider.get("previously_validated_invoices", []),
                "human_reviewer_feedback": provider.get("human_reviewer_feedback", []),
                "field_correction_patterns": provider.get("field_correction_patterns", {}),
                "validation_history": provider.get("validation_history", []),
            }
        )
    return {"providers": providers, "query": {"provider_hint": provider_hint, "invoice_type": invoice_type}}


def _llm_context_from_extraction_context(context: dict[str, Any]) -> list[LlmRagContext]:
    llm_context: list[LlmRagContext] = []
    for provider in context.get("providers", []):
        provider_name = str(provider.get("provider_name") or provider.get("provider_id") or "")
        snippets: list[RagSnippet] = []
        for tip in provider.get("provider_specific_extraction_tips", [])[:5]:
            snippets.append(RagSnippet(type="provider_tip", provider=provider_name, text=str(tip)))

        for wrong, right in list(provider.get("common_ocr_corrections", {}).items())[:5]:
            snippets.append(
                RagSnippet(
                    type="ocr_correction",
                    provider=provider_name,
                    text=f'OCR correction: replace "{wrong}" with "{right}".',
                )
            )

        for layout in provider.get("known_invoice_layouts", [])[:3]:
            fields = ", ".join(layout.get("fields_seen", [])[:8])
            if fields:
                snippets.append(
                    RagSnippet(
                        type="known_layout",
                        provider=provider_name,
                        text=f"Known {layout.get('invoice_type', 'invoice')} layout usually includes: {fields}.",
                    )
                )

        for field_name, pattern in list(provider.get("field_correction_patterns", {}).items())[:5]:
            examples = pattern.get("recent_examples", [])[:2]
            example_text = "; ".join(
                f"{item.get('old_value', 'null')} -> {item.get('corrected_value', 'null')}" for item in examples
            )
            snippets.append(
                RagSnippet(
                    type="field_correction_pattern",
                    provider=provider_name,
                    text=(
                        f"Field {field_name} has {pattern.get('total_corrections', 0)} saved correction(s). "
                        f"Recent examples: {example_text or 'none'}."
                    ),
                )
            )

        for item in provider.get("human_reviewer_feedback", [])[:5]:
            field_name = item.get("field_name", "unknown_field")
            note = item.get("note", "")
            snippets.append(
                RagSnippet(
                    type="human_feedback",
                    provider=provider_name,
                    text=(
                        f"Reviewer corrected {field_name}: {item.get('old_value', 'null')} -> "
                        f"{item.get('corrected_value', 'null')}. {note}".strip()
                    ),
                )
            )

        llm_context.append(
            LlmRagContext(
                provider_id=str(provider.get("provider_id") or "unknown"),
                provider_name=provider_name,
                score=int(provider.get("score") or 0),
                snippets=snippets,
            )
        )
    return llm_context


def _score_as_int(score: Any) -> int:
    if score is None:
        return 0
    try:
        return int(round(float(score) * 100))
    except (TypeError, ValueError):
        return 0


def _llm_context_from_vector_hits(hits: list[dict[str, Any]]) -> list[LlmRagContext]:
    contexts: dict[str, LlmRagContext] = {}
    for hit in hits:
        metadata = dict(hit.get("metadata") or {})
        provider_id = str(metadata.get("provider_id") or "unknown")
        provider_name = str(metadata.get("provider_name") or provider_display_name(provider_id))
        score = _score_as_int(hit.get("score"))
        context = contexts.setdefault(
            provider_id,
            LlmRagContext(
                provider_id=provider_id,
                provider_name=provider_name,
                score=score,
                snippets=[],
            ),
        )
        context.score = max(context.score, score)
        context.snippets.append(
            RagSnippet(
                type=str(metadata.get("memory_section") or metadata.get("document_type") or "vector_context"),
                provider=provider_name,
                text=str(hit.get("text") or "").strip(),
                score=hit.get("score"),
                source=metadata.get("source", "vector_store"),
            )
        )
    return [context for context in contexts.values() if context.snippets]


def build_llm_rag_context(
    query_text: str,
    provider_hint: str | None = "",
    invoice_type: str | None = "",
    top_k: int = 3,
    kb_path: str | Path = DEFAULT_KB,
    index_path: str | Path = DEFAULT_VECTOR_INDEX,
    use_vector_store: bool = True,
) -> list[LlmRagContext]:
    if use_vector_store:
        try:
            from vector_store.base import VectorStoreDependencyError, retrieve_provider_memory_docs

            hits = retrieve_provider_memory_docs(
                query_text=query_text,
                provider_hint=provider_hint,
                invoice_type=invoice_type,
                top_k=top_k,
                kb_path=kb_path,
                index_path=index_path,
            )
            # Semantic similarity alone must never apply another supplier's corrections.
            provider_id = canonical_provider(provider_hint, query_text)
            hits = [hit for hit in hits if provider_id != "unknown"
                    and (hit.get("metadata") or {}).get("provider_id") == provider_id]
            vector_context = _llm_context_from_vector_hits(hits)
            if vector_context:
                return vector_context
        except VectorStoreDependencyError as exc:
            logger.info("Vector store unavailable; falling back to JSON provider memory: %s", exc)

    context = build_extraction_context(
        query_text=query_text,
        provider_hint=provider_hint,
        invoice_type=invoice_type,
        top_k=top_k,
        kb_path=kb_path,
    )
    return _llm_context_from_extraction_context(context)


def build_llm_rag_snippets(
    query_text: str,
    provider_hint: str | None = "",
    invoice_type: str | None = "",
    top_k: int = 3,
    kb_path: str | Path = DEFAULT_KB,
    index_path: str | Path = DEFAULT_VECTOR_INDEX,
) -> list[RagSnippet]:
    snippets: list[RagSnippet] = []
    for context in build_llm_rag_context(query_text, provider_hint, invoice_type, top_k, kb_path, index_path):
        snippets.extend(context.snippets)
    return snippets


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage local provider RAG memory.")
    subparsers = parser.add_subparsers(dest="command")
    init_parser = subparsers.add_parser("init", help="Seed provider memory from the structured CSV.")
    init_parser.add_argument("--csv", default=str(DEFAULT_VALIDATED_CSV))
    init_parser.add_argument("--kb", default=str(DEFAULT_KB))
    retrieve_parser = subparsers.add_parser("retrieve", help="Retrieve provider-specific extraction context.")
    retrieve_parser.add_argument("--text-file", required=True)
    retrieve_parser.add_argument("--provider", default="")
    retrieve_parser.add_argument("--invoice-type", default="")
    retrieve_parser.add_argument("--kb", default=str(DEFAULT_KB))
    index_parser = subparsers.add_parser("index", help="Build the local vector index from provider memory.")
    index_parser.add_argument("--kb", default=str(DEFAULT_KB))
    index_parser.add_argument("--index-path", default=str(DEFAULT_VECTOR_INDEX))
    index_parser.add_argument("--force", action="store_true")
    return parser


def main() -> None:
    args = build_arg_parser().parse_args()
    if args.command == "init":
        kb = seed_from_validated_csv(args.csv, args.kb)
        print(f"Provider memory refreshed: {len(kb.get('providers', {}))} providers")
        return
    if args.command == "retrieve":
        text = Path(args.text_file).read_text(encoding="utf-8", errors="replace")
        context = build_extraction_context(text, args.provider, args.invoice_type, kb_path=args.kb)
        print(json.dumps(context, indent=2, ensure_ascii=False))
        return
    if args.command == "index":
        from vector_store.base import VectorStoreDependencyError, build_index

        try:
            build_index(kb_path=args.kb, index_path=args.index_path, force_rebuild=args.force)
        except VectorStoreDependencyError as exc:
            raise SystemExit(str(exc)) from exc
        print(f"Vector store ready: {Path(args.index_path)}")
        return
    build_arg_parser().print_help()


if __name__ == "__main__":
    main()
