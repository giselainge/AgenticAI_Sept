from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
from typing import Any

from invoice_parser.paths import DEFAULT_VECTOR_STORE_DIR, PROJECT_ROOT
from invoice_parser.providers import canonical_provider
from vector_store.documents import DOCUMENTS, provider_memory_documents


logger = logging.getLogger(__name__)

EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "BAAI/bge-base-en-v1.5")
INDEX_SIMILARITY_TOP_K = int(os.getenv("INDEX_SIMILARITY_TOP_K", "3"))
SENTENCE_SPLITTER_CHUNK_SIZE = int(os.getenv("SENTENCE_SPLITTER_CHUNK_SIZE", "512"))
SENTENCE_SPLITTER_CHUNK_OVERLAP = int(os.getenv("SENTENCE_SPLITTER_CHUNK_OVERLAP", "64"))
INDEX_PATH = DEFAULT_VECTOR_STORE_DIR
DEFAULT_KB = PROJECT_ROOT / "rag" / "knowledge_base.json"

_INDEX_CACHE: Any | None = None
_INDEX_CACHE_PATH: Path | None = None


class VectorStoreDependencyError(RuntimeError):
    """Raised when optional vector-store dependencies are unavailable."""


def _load_vector_dependencies() -> dict[str, Any]:
    try:
        import faiss
        from llama_index.core import StorageContext, VectorStoreIndex, load_index_from_storage
        from llama_index.core.node_parser import SentenceSplitter
        from llama_index.embeddings.huggingface import HuggingFaceEmbedding
        from llama_index.vector_stores.faiss import FaissVectorStore
    except ImportError as exc:
        raise VectorStoreDependencyError(
            "Vector store dependencies are not installed. Install the FAISS/LlamaIndex "
            "packages with `python -m pip install -r requirements.txt` before building "
            "the local index. The first index build may also need access to the "
            "configured HuggingFace embedding model."
        ) from exc

    return {
        "faiss": faiss,
        "StorageContext": StorageContext,
        "VectorStoreIndex": VectorStoreIndex,
        "load_index_from_storage": load_index_from_storage,
        "SentenceSplitter": SentenceSplitter,
        "HuggingFaceEmbedding": HuggingFaceEmbedding,
        "FaissVectorStore": FaissVectorStore,
    }


def _load_kb(kb_path: str | Path) -> dict[str, Any]:
    path = Path(kb_path)
    if not path.exists():
        return {"version": 1, "providers": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def _embedding_dimension(embed_model: Any) -> int:
    model = getattr(embed_model, "_model", None)
    if hasattr(model, "get_sentence_embedding_dimension"):
        return int(model.get_sentence_embedding_dimension())
    if isinstance(model, (list, tuple)) and len(model) > 1:
        dimension = getattr(model[1], "word_embedding_dimension", None)
        if dimension:
            return int(dimension)
    return int(os.getenv("EMBEDDING_DIMENSION", "768"))


def _index_exists(index_path: Path) -> bool:
    return (index_path / "default__vector_store.json").exists()


def build_index(
    kb_path: str | Path = DEFAULT_KB,
    index_path: str | Path = INDEX_PATH,
    *,
    force_rebuild: bool = False,
    documents: list[Any] | None = None,
) -> Any:
    """Factory: loads an existing FAISS/LlamaIndex index or builds a new one."""
    global _INDEX_CACHE, _INDEX_CACHE_PATH

    resolved_index_path = Path(index_path)
    if _INDEX_CACHE is not None and _INDEX_CACHE_PATH == resolved_index_path and not force_rebuild:
        return _INDEX_CACHE

    deps = _load_vector_dependencies()
    embed_model = deps["HuggingFaceEmbedding"](model_name=EMBEDDING_MODEL)
    index_settings = {
        "embed_model": embed_model,
        "transformations": [
            deps["SentenceSplitter"](
                chunk_size=SENTENCE_SPLITTER_CHUNK_SIZE,
                chunk_overlap=SENTENCE_SPLITTER_CHUNK_OVERLAP,
            )
        ],
    }

    if _index_exists(resolved_index_path) and not force_rebuild:
        logger.info("Loading vector store from %s...", resolved_index_path)
        vector_store = deps["FaissVectorStore"].from_persist_dir(resolved_index_path)
        storage_context = deps["StorageContext"].from_defaults(
            vector_store=vector_store,
            persist_dir=resolved_index_path,
        )
        index_settings["storage_context"] = storage_context
        index = deps["load_index_from_storage"](**index_settings)
    else:
        logger.info("No vector store built yet. Building from provider memory...")
        kb_documents = documents if documents is not None else provider_memory_documents(_load_kb(kb_path))
        source_documents = kb_documents or DOCUMENTS
        faiss_index = deps["faiss"].IndexFlatL2(_embedding_dimension(embed_model))
        vector_store = deps["FaissVectorStore"](faiss_index=faiss_index)
        storage_context = deps["StorageContext"].from_defaults(vector_store=vector_store)
        index_settings["storage_context"] = storage_context
        index = deps["VectorStoreIndex"].from_documents(source_documents, **index_settings)
        persist_index(index, resolved_index_path)

    _INDEX_CACHE = index
    _INDEX_CACHE_PATH = resolved_index_path
    logger.info("Loaded vector index successfully.")
    return index


def persist_index(index: Any, index_path: str | Path = INDEX_PATH) -> None:
    Path(index_path).mkdir(parents=True, exist_ok=True)
    index.storage_context.persist(persist_dir=Path(index_path))


def get_retriever(similarity_top_k: int = INDEX_SIMILARITY_TOP_K) -> Any:
    """Return a retriever from the already loaded module-level index cache."""
    if _INDEX_CACHE is None:
        raise RuntimeError("Index not loaded. Call build_index() before get_retriever().")
    return _INDEX_CACHE.as_retriever(similarity_top_k=similarity_top_k)


def _node_text(node: Any) -> str:
    if hasattr(node, "get_content"):
        return str(node.get_content(metadata_mode="none"))
    return str(getattr(node, "text", ""))


def retrieve_provider_memory_docs(
    query_text: str,
    provider_hint: str | None = "",
    invoice_type: str | None = "",
    top_k: int = INDEX_SIMILARITY_TOP_K,
    kb_path: str | Path = DEFAULT_KB,
    index_path: str | Path = INDEX_PATH,
) -> list[dict[str, Any]]:
    provider_id = canonical_provider(provider_hint, query_text)
    if provider_id == "unknown":
        return []
    # Retrieval is read-only; an absent index falls back to JSON memory.
    if not _index_exists(Path(index_path)) and not (
        _INDEX_CACHE is not None and _INDEX_CACHE_PATH == Path(index_path)
    ):
        return []
    query = " ".join(part for part in [provider_hint or "", invoice_type or "", query_text] if part).strip()
    index = build_index(kb_path=kb_path, index_path=index_path)
    retriever = index.as_retriever(similarity_top_k=max(1, top_k * 4))
    hits = []
    for item in retriever.retrieve(query):
        node = getattr(item, "node", item)
        if (getattr(node, "metadata", {}) or {}).get("provider_id") != provider_id:
            continue
        hits.append(
            {
                "text": _node_text(node),
                "score": getattr(item, "score", None),
                "metadata": dict(getattr(node, "metadata", {}) or {}),
            }
        )
    return hits[:top_k]


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build or query the local provider-memory vector store.")
    parser.add_argument("--kb", default=str(DEFAULT_KB), help="Provider memory JSON path.")
    parser.add_argument("--index-path", default=str(INDEX_PATH), help="Vector index output path.")
    parser.add_argument("--force", action="store_true", help="Rebuild the index even if persisted files exist.")
    return parser


def main() -> None:
    args = build_arg_parser().parse_args()
    try:
        build_index(kb_path=args.kb, index_path=args.index_path, force_rebuild=args.force)
    except VectorStoreDependencyError as exc:
        raise SystemExit(str(exc)) from exc
    print(f"Vector store ready: {Path(args.index_path)}")


if __name__ == "__main__":
    main()
