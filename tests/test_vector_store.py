from __future__ import annotations

from llm.agent.models import RagSnippet
from rag.adaptive_rag import build_llm_rag_snippets
from vector_store.base import VectorStoreDependencyError, _load_vector_dependencies
from vector_store.documents import provider_memory_documents


def _doc_text(document: object) -> str:
    if hasattr(document, "get_content"):
        return str(document.get_content(metadata_mode="none"))
    return str(getattr(document, "text", ""))


def test_provider_memory_documents_include_metadata_without_vector_dependencies() -> None:
    kb = {
        "version": 1,
        "providers": {
            "epal": {
                "provider_id": "epal",
                "provider_name": "EPAL",
                "provider_specific_extraction_tips": ["Total appears near Montante."],
                "previously_validated_invoices": [
                    {
                        "signature": "verified-123",
                        "validated_fields": {"invoice_number": "FT 123", "total_value": "10.00"},
                    }
                ],
                "common_ocr_corrections": {"O": "0"},
                "known_invoice_layouts": [
                    {"invoice_type": "water", "fields_seen": ["invoice_number", "total_value"]}
                ],
                "field_correction_patterns": {
                    "total_value": {
                        "total_corrections": 1,
                        "recent_examples": [{"old_value": "null", "corrected_value": "10.00"}],
                    }
                },
                "human_reviewer_feedback": [
                    {"field_name": "total_value", "old_value": "null", "corrected_value": "10.00", "note": "Reviewed"}
                ],
            }
        },
    }

    documents = provider_memory_documents(kb)

    assert len(documents) == 6
    assert any("Montante" in _doc_text(document) for document in documents)
    assert any(
        getattr(document, "metadata", {}).get("memory_section") == "validated_example"
        and getattr(document, "metadata", {}).get("signature") == "verified-123"
        for document in documents
    )
    assert all(getattr(document, "metadata", {})["provider_id"] == "epal" for document in documents)


def test_llm_rag_snippets_prefer_vector_store_hits(monkeypatch) -> None:
    def fake_retrieve_provider_memory_docs(**kwargs):
        assert kwargs["provider_hint"] == "EPAL"
        return [
            {
                "text": "Vector hit: EPAL total_value often appears near Montante.",
                "score": 0.91,
                "metadata": {
                    "provider_id": "epal",
                    "provider_name": "EPAL",
                    "memory_section": "known_layout",
                    "source": "vector_store",
                },
            }
        ]

    monkeypatch.setattr(
        "vector_store.base.retrieve_provider_memory_docs",
        fake_retrieve_provider_memory_docs,
    )

    snippets = build_llm_rag_snippets("Montante", provider_hint="EPAL", invoice_type="water")

    assert snippets
    assert all(isinstance(snippet, RagSnippet) for snippet in snippets)
    assert snippets[0].type == "known_layout"
    assert "Vector hit" in snippets[0].text


def test_vector_store_dependency_error_is_actionable(monkeypatch) -> None:
    real_import = __import__

    def fake_import(name, *args, **kwargs):
        if name == "faiss":
            raise ImportError("missing faiss")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", fake_import)

    try:
        _load_vector_dependencies()
    except VectorStoreDependencyError as exc:
        message = str(exc)
    else:  # pragma: no cover - only possible when the monkeypatch fails.
        raise AssertionError("Expected VectorStoreDependencyError")

    assert "uv sync --frozen" in message


def test_local_embedding_is_stable_and_uses_no_model_download() -> None:
    class FakeBaseEmbedding:
        def __init__(self, **values):
            for key, value in values.items():
                setattr(self, key, value)

    from vector_store.base import _embedding_class

    embedding = _embedding_class(FakeBaseEmbedding)(dimension=64)
    first = embedding._get_text_embedding("EPAL water invoice total")
    second = embedding._get_query_embedding("EPAL water invoice total")

    assert first == second
    assert len(first) == 64
    assert abs(sum(value * value for value in first) - 1.0) < 1e-5
