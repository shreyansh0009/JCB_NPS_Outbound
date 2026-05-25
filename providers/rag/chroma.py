"""
ChromaRetriever: RAG retrieval backed by ChromaDB (embedded, no server needed).

ChromaDB stores vectors + text chunks locally on disk.
Each agent gets its own collection (named by agent + knowledge base name).

Install: pip install chromadb

Env vars:
  CHROMA_PERSIST_DIR  - where ChromaDB stores data, default: ./chroma_db
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
from functools import partial
from pathlib import Path

from providers.rag.base import BaseRetriever, RetrievedChunk
from providers.rag.embeddings import BaseEmbeddings

logger = logging.getLogger(__name__)


class ChromaRetriever(BaseRetriever):
    def __init__(
        self,
        collection_name: str,
        embeddings: BaseEmbeddings,
        persist_dir: str = "./chroma_db",
    ):
        self._embeddings = embeddings
        self._collection_name = collection_name
        self._persist_dir = Path(persist_dir)
        self._collection = None
        self._backend = "chromadb"
        self._simple_docs: list[dict] = []
        self._simple_path = self._persist_dir / "simple_collections" / f"{collection_name}.json"

        try:
            import chromadb
            client = chromadb.PersistentClient(path=persist_dir)
            self._collection = client.get_or_create_collection(
                name=collection_name,
                metadata={"hnsw:space": "cosine"},
            )
            count = self._collection.count()
            logger.info(f"ChromaDB collection '{collection_name}' loaded ({count} chunks)")
        except Exception as e:
            self._backend = "simple"
            self._load_simple()
            logger.warning(
                f"chromadb unavailable ({e}) — using SimpleJSONRetriever backend "
                f"for '{collection_name}' ({len(self._simple_docs)} chunks)"
            )

    def _load_simple(self) -> None:
        self._simple_path.parent.mkdir(parents=True, exist_ok=True)
        if self._simple_path.exists():
            try:
                self._simple_docs = json.loads(self._simple_path.read_text(encoding="utf-8"))
            except Exception:
                logger.warning(f"Could not parse {self._simple_path}; resetting simple collection")
                self._simple_docs = []
        else:
            self._simple_docs = []

    def _save_simple(self) -> None:
        self._simple_path.parent.mkdir(parents=True, exist_ok=True)
        self._simple_path.write_text(
            json.dumps(self._simple_docs, ensure_ascii=False),
            encoding="utf-8",
        )

    def _count(self) -> int:
        if self._backend == "chromadb" and self._collection is not None:
            return self._collection.count()
        return len(self._simple_docs)

    @staticmethod
    def _cosine(a: list[float], b: list[float]) -> float:
        if not a or not b:
            return 0.0
        n = min(len(a), len(b))
        dot = sum(a[i] * b[i] for i in range(n))
        na = math.sqrt(sum(v * v for v in a[:n]))
        nb = math.sqrt(sum(v * v for v in b[:n]))
        if na == 0 or nb == 0:
            return 0.0
        return dot / (na * nb)

    async def retrieve(self, query: str, top_k: int = 3) -> list[RetrievedChunk]:
        from core.tracing import aspan, set_attr
        async with aspan("rag.retrieve", attrs={
            "rag.collection": self._collection_name,
            "rag.top_k": top_k,
            "rag.backend": self._backend,
        }):
            if self._count() == 0:
                logger.warning(f"Collection '{self._collection_name}' is empty — no RAG context")
                set_attr("rag.empty", True)
                return []

            query_vector = await self._embeddings.embed_one(query)

            chunks = []
            if self._backend == "chromadb" and self._collection is not None:
                loop = asyncio.get_event_loop()
                results = await loop.run_in_executor(
                    None,
                    partial(
                        self._collection.query,
                        query_embeddings=[query_vector],
                        n_results=min(top_k, self._collection.count()),
                        include=["documents", "metadatas", "distances"],
                    ),
                )
                for doc, meta, dist in zip(
                    results["documents"][0],
                    results["metadatas"][0],
                    results["distances"][0],
                ):
                    chunks.append(RetrievedChunk(
                        text=doc,
                        source=meta.get("source", "unknown"),
                        score=1.0 - dist,   # cosine distance → similarity
                    ))
                set_attr("rag.results_count", len(chunks))
                return chunks

            # Simple JSON fallback retrieval (cosine on stored embeddings)
            scored: list[tuple[float, dict]] = []
            for rec in self._simple_docs:
                emb = rec.get("embedding") or []
                sim = self._cosine(query_vector, emb)
                scored.append((sim, rec))

            scored.sort(key=lambda x: x[0], reverse=True)
            for sim, rec in scored[:top_k]:
                chunks.append(
                    RetrievedChunk(
                        text=rec.get("text", ""),
                        source=rec.get("source", "unknown"),
                        score=float(sim),
                    )
                )

            set_attr("rag.results_count", len(chunks))
            return chunks

    async def add_documents(self, chunks: list[dict]) -> None:
        """
        Add text chunks to the collection.
        chunks: [{"text": "...", "source": "filename.md", "id": "unique-id"}, ...]
        """
        texts = [c["text"] for c in chunks]
        vectors = await self._embeddings.embed(texts)

        if self._backend == "chromadb" and self._collection is not None:
            self._collection.add(
                ids=[c["id"] for c in chunks],
                embeddings=vectors,
                documents=texts,
                metadatas=[{"source": c.get("source", "")} for c in chunks],
            )
            logger.info(f"Added {len(chunks)} chunks to '{self._collection_name}'")
            return

        # Simple JSON fallback upsert
        by_id = {d.get("id"): d for d in self._simple_docs}
        for c, v in zip(chunks, vectors):
            by_id[c["id"]] = {
                "id": c["id"],
                "text": c["text"],
                "source": c.get("source", ""),
                "embedding": v,
            }
        self._simple_docs = list(by_id.values())
        self._save_simple()
        logger.info(f"Added {len(chunks)} chunks to '{self._collection_name}'")

    @classmethod
    def for_agent(
        cls,
        agent_name: str,
        kb_name: str,
        embeddings: BaseEmbeddings,
        persist_dir: str | None = None,
    ) -> "ChromaRetriever":
        """Convenience factory: creates a collection scoped to an agent + KB."""
        collection = f"{agent_name}_{kb_name}".replace("-", "_").replace(" ", "_")
        return cls(
            collection_name=collection,
            embeddings=embeddings,
            persist_dir=persist_dir or os.getenv("CHROMA_PERSIST_DIR", "./chroma_db"),
        )
