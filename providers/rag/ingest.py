"""
Ingest script: load markdown/text documents into ChromaDB.

Usage:
  python -m providers.rag.ingest \
    --collection screener_eligibility \
    --files knowledge_bases/eligibility_faq.md knowledge_bases/program_info.md

  # Or ingest an entire folder:
  python -m providers.rag.ingest \
    --collection screener_eligibility \
    --dir knowledge_bases/screener/

The script:
  1. Reads each file
  2. Splits into chunks (by paragraph or fixed size)
  3. Embeds each chunk
  4. Stores in ChromaDB

Env vars respected: EMBEDDING_PROVIDER, OLLAMA_BASE_URL, CHROMA_PERSIST_DIR
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import logging
import os
import re
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def _split_by_paragraph(text: str, max_chars: int = 500) -> list[str]:
    """Split text into chunks at paragraph boundaries, respecting max_chars."""
    paragraphs = re.split(r"\n\n+", text.strip())
    chunks = []
    current = ""

    for para in paragraphs:
        para = para.strip()
        if not para:
            continue
        if len(current) + len(para) + 2 <= max_chars:
            current = (current + "\n\n" + para).strip()
        else:
            if current:
                chunks.append(current)
            # If a single paragraph exceeds max_chars, split by sentence
            if len(para) > max_chars:
                sentences = re.split(r"(?<=[.!?])\s+", para)
                buf = ""
                for sent in sentences:
                    if len(buf) + len(sent) + 1 <= max_chars:
                        buf = (buf + " " + sent).strip()
                    else:
                        if buf:
                            chunks.append(buf)
                        buf = sent
                if buf:
                    chunks.append(buf)
            else:
                current = para

    if current:
        chunks.append(current)

    return [c for c in chunks if c.strip()]


def _file_chunks(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8")
    raw_chunks = _split_by_paragraph(text)
    result = []
    for i, chunk in enumerate(raw_chunks):
        uid = hashlib.md5(f"{path.name}:{i}:{chunk[:50]}".encode()).hexdigest()
        result.append({"id": uid, "text": chunk, "source": path.name})
    return result


async def ingest(collection_name: str, files: list[Path]) -> None:
    from providers.rag.embeddings import get_embeddings
    from providers.rag.chroma import ChromaRetriever

    embeddings = get_embeddings()
    retriever = ChromaRetriever(
        collection_name=collection_name,
        embeddings=embeddings,
        persist_dir=os.getenv("CHROMA_PERSIST_DIR", "./chroma_db"),
    )

    all_chunks = []
    for path in files:
        if not path.exists():
            logger.warning(f"File not found: {path}")
            continue
        chunks = _file_chunks(path)
        logger.info(f"{path.name}: {len(chunks)} chunks")
        all_chunks.extend(chunks)

    if not all_chunks:
        logger.error("No chunks to ingest.")
        return

    await retriever.add_documents(all_chunks)
    logger.info(f"Ingested {len(all_chunks)} chunks into collection '{collection_name}'")


def main():
    parser = argparse.ArgumentParser(description="Ingest documents into ChromaDB")
    parser.add_argument("--collection", required=True, help="ChromaDB collection name")
    parser.add_argument("--files", nargs="*", default=[], help="Specific files to ingest")
    parser.add_argument("--dir", default=None, help="Directory of files to ingest")
    args = parser.parse_args()

    files: list[Path] = [Path(f) for f in args.files]
    if args.dir:
        d = Path(args.dir)
        files += list(d.glob("*.md")) + list(d.glob("*.txt"))

    if not files:
        print("No files specified. Use --files or --dir.")
        return

    asyncio.run(ingest(args.collection, files))


if __name__ == "__main__":
    main()
