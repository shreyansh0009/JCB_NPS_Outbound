from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class RetrievedChunk:
    text: str
    source: str        # document name / file path
    score: float       # similarity score (higher = more relevant)


class BaseRetriever(ABC):
    @abstractmethod
    async def retrieve(self, query: str, top_k: int = 3) -> list[RetrievedChunk]:
        """
        Retrieve the top_k most relevant chunks for the given query.
        Returns chunks ordered by relevance (most relevant first).
        """
        ...

    def format_context(self, chunks: list[RetrievedChunk]) -> str:
        """Format retrieved chunks into a string to inject into the LLM prompt."""
        if not chunks:
            return ""
        parts = []
        for i, chunk in enumerate(chunks, 1):
            parts.append(f"[Source {i}: {chunk.source}]\n{chunk.text}")
        return "\n\n".join(parts)
