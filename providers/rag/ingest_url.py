"""
ingest_url.py: Scrape web pages or PDFs and ingest them into ChromaDB for RAG.

Supports:
  - Single URL
  - Multiple URLs (space-separated)
  - A .txt file containing one URL per line
  - Recursive crawl of an entire website (same domain only)
  - Single or multiple PDF files
  - Mix of URLs and PDFs together

Usage:
  # Single URL
  python -m providers.rag.ingest_url --collection my_kb --url https://example.com/page

  # Crawl entire site
  python -m providers.rag.ingest_url --collection my_kb \
      --url https://example.com --crawl --max-pages 50

  # Single PDF
  python -m providers.rag.ingest_url --collection my_kb --pdf company.pdf

  # Multiple PDFs
  python -m providers.rag.ingest_url --collection my_kb --pdf file1.pdf file2.pdf

  # Mix of URL + PDF
  python -m providers.rag.ingest_url --collection my_kb \
      --url https://example.com --pdf extra.pdf

Env vars respected: EMBEDDING_PROVIDER, CHROMA_PERSIST_DIR
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import logging
import re
from collections import deque
from pathlib import Path
from urllib.parse import urljoin, urlparse

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)


# ── Text cleaning ─────────────────────────────────────────────────────────────

def _clean_html(html: str) -> str:
    """Strip HTML tags and clean up whitespace."""
    # Remove script and style blocks entirely
    html = re.sub(r"<(script|style|nav|footer|header)[^>]*>.*?</\1>", "", html, flags=re.DOTALL | re.IGNORECASE)
    # Remove all remaining tags
    text = re.sub(r"<[^>]+>", " ", html)
    # Decode common HTML entities
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">") \
               .replace("&nbsp;", " ").replace("&#39;", "'").replace("&quot;", '"')
    # Collapse whitespace
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _split_by_paragraph(text: str, max_chars: int = 500) -> list[str]:
    """Split text into chunks at paragraph boundaries, respecting max_chars."""
    paragraphs = re.split(r"\n\n+", text.strip())
    chunks: list[str] = []
    current = ""

    for para in paragraphs:
        para = para.strip()
        if not para or len(para) < 20:  # skip very short fragments
            continue
        if len(current) + len(para) + 2 <= max_chars:
            current = (current + "\n\n" + para).strip()
        else:
            if current:
                chunks.append(current)
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


# ── HTTP fetching ─────────────────────────────────────────────────────────────

async def _fetch(url: str, session) -> str | None:
    """Fetch a URL and return raw HTML, or None on error."""
    try:
        response = await session.get(url, follow_redirects=True, timeout=15)
        if response.status_code != 200:
            logger.warning(f"HTTP {response.status_code} for {url}")
            return None
        content_type = response.headers.get("content-type", "")
        if "text/html" not in content_type and "text/plain" not in content_type:
            logger.warning(f"Skipping non-HTML content ({content_type}): {url}")
            return None
        return response.text
    except Exception as e:
        logger.warning(f"Failed to fetch {url}: {e}")
        return None


def _extract_links(html: str, base_url: str, allowed_domain: str) -> list[str]:
    """Extract all same-domain links from HTML."""
    links = re.findall(r'href=["\']([^"\'#?]+)["\']', html)
    result = []
    for link in links:
        full = urljoin(base_url, link)
        parsed = urlparse(full)
        if parsed.netloc == allowed_domain and parsed.scheme in ("http", "https"):
            result.append(full.rstrip("/"))
    return list(set(result))


# ── Chunk building ────────────────────────────────────────────────────────────

def _url_to_chunks(url: str, html: str) -> list[dict]:
    """Convert fetched HTML into chunk dicts ready for ChromaDB."""
    text = _clean_html(html)
    if not text:
        return []
    raw_chunks = _split_by_paragraph(text)
    chunks = []
    for i, chunk in enumerate(raw_chunks):
        uid = hashlib.md5(f"{url}:{i}:{chunk[:50]}".encode()).hexdigest()
        chunks.append({"id": uid, "text": chunk, "source": url})
    return chunks


# ── Core ingest logic ─────────────────────────────────────────────────────────

async def ingest_urls(
    collection_name: str,
    urls: list[str],
    crawl: bool = False,
    max_pages: int = 50,
) -> None:
    import httpx
    from providers.rag.embeddings import get_embeddings
    from providers.rag.chroma import ChromaRetriever
    import os

    embeddings = get_embeddings()
    retriever = ChromaRetriever(
        collection_name=collection_name,
        embeddings=embeddings,
        persist_dir=os.getenv("CHROMA_PERSIST_DIR", "./chroma_db"),
    )

    all_chunks: list[dict] = []
    visited: set[str] = set()

    async with httpx.AsyncClient(
        headers={"User-Agent": "Mozilla/5.0 (compatible; RAG-Ingestor/1.0)"},
        timeout=15,
    ) as session:

        # Build queue
        queue: deque[str] = deque(url.rstrip("/") for url in urls)

        while queue and len(visited) < max_pages:
            url = queue.popleft()
            if url in visited:
                continue
            visited.add(url)

            logger.info(f"Fetching ({len(visited)}/{max_pages}): {url}")
            html = await _fetch(url, session)
            if not html:
                continue

            chunks = _url_to_chunks(url, html)
            if chunks:
                logger.info(f"  → {len(chunks)} chunks")
                all_chunks.extend(chunks)
            else:
                logger.warning(f"  → No usable text found")

            # If crawling, enqueue same-domain links
            if crawl:
                domain = urlparse(url).netloc
                links = _extract_links(html, url, domain)
                for link in links:
                    if link not in visited:
                        queue.append(link)

    if not all_chunks:
        logger.error("No content ingested — check that the URLs are accessible.")
        return

    logger.info(f"Ingesting {len(all_chunks)} total chunks into '{collection_name}'...")
    await retriever.add_documents(all_chunks)
    logger.info(f"✅ Done! {len(all_chunks)} chunks from {len(visited)} pages ingested into '{collection_name}'.")



async def ingest_pdfs(collection_name: str, pdf_paths: list[str]) -> None:
    """Ingest one or more PDF files into ChromaDB."""
    import os
    from providers.rag.embeddings import get_embeddings
    from providers.rag.chroma import ChromaRetriever

    embeddings = get_embeddings()
    retriever = ChromaRetriever(
        collection_name=collection_name,
        embeddings=embeddings,
        persist_dir=os.getenv("CHROMA_PERSIST_DIR", "./chroma_db"),
    )

    all_chunks: list[dict] = []
    for path in pdf_paths:
        if not Path(path).exists():
            logger.error(f"PDF not found: {path}")
            continue
        chunks = _extract_pdf_chunks(path)
        all_chunks.extend(chunks)

    if not all_chunks:
        logger.error("No content extracted from PDFs.")
        return

    logger.info(f"Ingesting {len(all_chunks)} chunks from {len(pdf_paths)} PDF(s) into '{collection_name}'...")
    await retriever.add_documents(all_chunks)
    logger.info(f"✅ Done! {len(all_chunks)} chunks ingested into '{collection_name}'.")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Ingest web URLs into ChromaDB for RAG",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Single URL
  python -m providers.rag.ingest_url --collection my_kb --url https://example.com

  # Crawl entire site
  python -m providers.rag.ingest_url --collection my_kb --url https://site.com --crawl --max-pages 50

  # Single PDF
  python -m providers.rag.ingest_url --collection my_kb --pdf company.pdf

  # Multiple PDFs
  python -m providers.rag.ingest_url --collection my_kb --pdf file1.pdf file2.pdf

  # URL + PDF together
  python -m providers.rag.ingest_url --collection my_kb --url https://site.com --pdf extra.pdf
        """,
    )
    parser.add_argument("--collection", required=True, help="ChromaDB collection name")
    parser.add_argument("--url", nargs="+", default=[], help="One or more URLs to ingest")
    parser.add_argument("--file", default=None, help=".txt file with one URL per line")
    parser.add_argument("--pdf", nargs="+", default=[], help="One or more PDF files to ingest")
    parser.add_argument("--crawl", action="store_true", help="Recursively crawl all links on the same domain")
    parser.add_argument("--max-pages", type=int, default=50, help="Max pages to crawl (default: 50)")
    args = parser.parse_args()

    urls: list[str] = list(args.url)
    pdfs: list[str] = list(args.pdf)

    # Load URLs from file if provided
    if args.file:
        file_path = Path(args.file)
        if not file_path.exists():
            print(f"❌ File not found: {args.file}")
            return
        with open(file_path, encoding="utf-8") as f:
            file_urls = [line.strip() for line in f if line.strip() and not line.startswith("#")]
        logger.info(f"Loaded {len(file_urls)} URLs from {args.file}")
        urls.extend(file_urls)

    if not urls and not pdfs:
        print("❌ No input provided. Use --url, --file, or --pdf.")
        parser.print_help()
        return

    # Ingest URLs
    if urls:
        asyncio.run(ingest_urls(
            collection_name=args.collection,
            urls=urls,
            crawl=args.crawl,
            max_pages=args.max_pages,
        ))

    # Ingest PDFs
    if pdfs:
        asyncio.run(ingest_pdfs(
            collection_name=args.collection,
            pdf_paths=pdfs,
        ))


if __name__ == "__main__":
    main()