from __future__ import annotations

import argparse
import hashlib
import logging
import os
import re
from pathlib import Path
from typing import Any

from invoice_parser.paths import DEFAULT_KB_PATH, DEFAULT_VECTOR_STORE_DIR
from invoice_parser.providers import canonical_provider
from vector_store.documents import DOCUMENTS, provider_memory_documents


logger = logging.getLogger(__name__)

EMBEDDING_DIMENSION = int(os.getenv("EMBEDDING_DIMENSION", "768"))
INDEX_SIMILARITY_TOP_K = int(os.getenv("INDEX_SIMILARITY_TOP_K", "3"))
SENTENCE_SPLITTER_CHUNK_SIZE = int(os.getenv("SENTENCE_SPLITTER_CHUNK_SIZE", "512"))
SENTENCE_SPLITTER_CHUNK_OVERLAP = int(os.getenv("SENTENCE_SPLITTER_CHUNK_OVERLAP", "64"))
INDEX_PATH = DEFAULT_VECTOR_STORE_DIR
DEFAULT_KB = DEFAULT_KB_PATH

_INDEX_CACHE: Any | None = None
_INDEX_CACHE_PATH: Path | None = None


class VectorStoreDependencyError(RuntimeError):
    """Raised when optional vector-store dependencies are unavailable."""


def _load_vector_dependencies() -> dict[str, Any]:
    try:
        import faiss
        import torch
        from llama_index.core import StorageContext, VectorStoreIndex, load_index_from_storage
        from llama_index.core.embeddings import BaseEmbedding
        from llama_index.core.node_parser import SentenceSplitter
        from llama_index.vector_stores.faiss import FaissVectorStore
    except ImportError as exc:
        raise VectorStoreDependencyError(
            "Vector store dependencies are not installed. Run `uv sync --frozen` "
            "before building the local index."
        ) from exc

    return {
        "faiss": faiss,
        "torch": torch,
        "BaseEmbedding": BaseEmbedding,
        "StorageContext": StorageContext,
        "VectorStoreIndex": VectorStoreIndex,
        "load_index_from_storage": load_index_from_storage,
        "SentenceSplitter": SentenceSplitter,
        "FaissVectorStore": FaissVectorStore,
    }


def _embedding_class(base_embedding: Any, torch_module: Any) -> type:
    """Create a LlamaIndex embedding backed by deterministic local Torch operations."""

    class LocalHashEmbedding(base_embedding):
        dimension: int = EMBEDDING_DIMENSION
        device: str = "cpu"

        @classmethod
        def class_name(cls) -> str:
            return "local_hash_embedding"

        def _embed_on(self, text: str, device: str) -> list[float]:
            vector = torch_module.zeros(self.dimension, dtype=torch_module.float32, device=device)
            tokens = re.findall(r"[\w]+", (text or "").casefold(), flags=re.UNICODE)
            for token in tokens:
                digest = hashlib.blake2b(token.encode("utf-8"), digest_size=16).digest()
                index = int.from_bytes(digest[:8], "little") % self.dimension
                sign = 1.0 if digest[8] & 1 else -1.0
                vector[index] += sign
            norm = torch_module.linalg.vector_norm(vector)
            if norm.item() > 0:
                vector = vector / norm
            return vector.detach().cpu().tolist()

        def _embed(self, text: str) -> list[float]:
            try:
                return self._embed_on(text, self.device)
            except RuntimeError:
                if self.device == "cpu":
                    raise
                logger.warning("Torch CUDA embedding failed; retrying this index on CPU.")
                object.__setattr__(self, "device", "cpu")
                return self._embed_on(text, "cpu")

        def _get_query_embedding(self, query: str) -> list[float]:
            return self._embed(query)

        async def _aget_query_embedding(self, query: str) -> list[float]:
            return self._embed(query)

        def _get_text_embedding(self, text: str) -> list[float]:
            return self._embed(text)

    return LocalHashEmbedding


def _load_kb(kb_path: str | Path) -> dict[str, Any]:
    from rag.adaptive_rag import load_kb

    return load_kb(kb_path)


def _embedding_device(torch_module: Any) -> str:
    requested = os.getenv("TORCH_EMBEDDING_DEVICE", "cpu").strip().casefold()
    if requested == "auto":
        return "cuda" if torch_module.cuda.is_available() else "cpu"
    if requested == "cuda" and torch_module.cuda.is_available():
        return "cuda"
    if requested not in {"", "cpu", "cuda"}:
        logger.warning("Unknown TORCH_EMBEDDING_DEVICE=%s; using CPU.", requested)
    elif requested == "cuda":
        logger.warning("CUDA embeddings requested but CUDA is unavailable; using CPU.")
    return "cpu"


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
    embedding_type = _embedding_class(deps["BaseEmbedding"], deps["torch"])
    embed_model = embedding_type(
        dimension=EMBEDDING_DIMENSION,
        device=_embedding_device(deps["torch"]),
    )
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
        faiss_index = deps["faiss"].IndexFlatL2(EMBEDDING_DIMENSION)
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
    # Retrieval is read-only; an absent index falls back to provider memory.
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
