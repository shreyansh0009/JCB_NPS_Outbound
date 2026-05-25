"""
generate_prompts.py — Auto-generate all agent prompts from a URL or text.

Give it a website URL or paste a paragraph about your business,
and it rewrites ALL agent prompts (hello, service, sales, closer)
to match that business automatically.

Usage:
  # From a URL
  python generate_prompts.py --url https://www.crmlanding.in/

  # From a text paragraph
  python generate_prompts.py --text "We are XYZ company, a SaaS CRM platform 
      that helps businesses manage leads, automate follow-ups, and close deals faster."

  # Preview only (don't save files)
  python generate_prompts.py --url https://www.crmlanding.in/ --preview

  # Custom agent name
  python generate_prompts.py --url https://www.crmlanding.in/ --agent-name "alex"
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

GROQ_BASE_URL = "https://api.groq.com/openai/v1"
PROMPTS_DIR = Path(__file__).parent / "prompts"


# ── Web scraping ──────────────────────────────────────────────────────────────

def _clean_html(html: str) -> str:
    html = re.sub(r"<(script|style|nav|footer|header|meta|link)[^>]*>.*?</\1>", "", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", html)
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">") \
               .replace("&nbsp;", " ").replace("&#39;", "'").replace("&quot;", '"')
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


async def _fetch_url(url: str) -> str:
    logger.info(f"Fetching {url} ...")
    async with httpx.AsyncClient(
        headers={"User-Agent": "Mozilla/5.0 (compatible; PromptGen/1.0)"},
        timeout=15,
        follow_redirects=True,
    ) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        html = resp.text
    text = _clean_html(html)
    # Limit to first 3000 chars — enough context for prompt generation
    return text[:3000]


# File extensions to skip during crawl
SKIP_EXTENSIONS = {
    ".css", ".js", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico",
    ".webp", ".woff", ".woff2", ".ttf", ".eot", ".otf", ".pdf",
    ".zip", ".mp4", ".mp3", ".xml", ".json", ".txt", ".map"
}


def _is_html_url(url: str) -> bool:
    """Return True only if URL looks like an HTML page (not an asset)."""
    from urllib.parse import urlparse
    path = urlparse(url).path.lower()
    # Skip any URL with a known asset extension
    for ext in SKIP_EXTENSIONS:
        if path.endswith(ext):
            return False
    # Skip asset folders
    for folder in ("/assets/", "/css/", "/js/", "/images/", "/fonts/", "/static/"):
        if folder in path:
            return False
    return True


async def _crawl_site(start_url: str, max_pages: int = 20) -> str:
    """Crawl entire website (same domain), HTML pages only, for prompt generation."""
    from collections import deque
    from urllib.parse import urljoin, urlparse

    domain = urlparse(start_url).netloc
    visited: set[str] = set()
    queue: deque[str] = deque([start_url.rstrip("/")])
    all_text: list[str] = []
    html_pages = 0

    async with httpx.AsyncClient(
        headers={"User-Agent": "Mozilla/5.0 (compatible; PromptGen/1.0)"},
        timeout=15,
        follow_redirects=True,
    ) as client:
        while queue and html_pages < max_pages:
            url = queue.popleft()
            if url in visited:
                continue
            visited.add(url)

            # Skip non-HTML assets immediately
            if not _is_html_url(url):
                logger.info(f"  Skipped (asset): {url}")
                continue

            try:
                resp = await client.get(url)
                if resp.status_code != 200:
                    continue
                # Double-check content-type is HTML
                content_type = resp.headers.get("content-type", "")
                if "text/html" not in content_type:
                    logger.info(f"  Skipped (not HTML: {content_type}): {url}")
                    continue
                html = resp.text
            except Exception as e:
                logger.warning(f"Failed: {url} — {e}")
                continue

            html_pages += 1
            text = _clean_html(html)
            if text:
                # Take up to 800 chars per page for variety across pages
                all_text.append(f"[Page: {url}]\n{text[:800]}")
                logger.info(f"  ({html_pages}/{max_pages}) {url} — {len(text)} chars")

            # Extract same-domain HTML links only
            links = re.findall(r'href=["\']([^"\'#?]+)["\']', html)
            for link in links:
                full = urljoin(url, link)
                parsed = urlparse(full)
                if parsed.netloc == domain and parsed.scheme in ("http", "https"):
                    clean = full.rstrip("/")
                    if clean not in visited and _is_html_url(clean):
                        queue.append(clean)

    combined = "\n\n".join(all_text)
    logger.info(f"Crawled {html_pages} HTML pages, {len(combined)} total chars.")
    # Limit to 4000 chars for LLM context
    return combined[:4000]


# ── Groq LLM call ─────────────────────────────────────────────────────────────

async def _groq_chat(messages: list[dict], api_key: str) -> str:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            f"{GROQ_BASE_URL}/chat/completions",
            headers=headers,
            json={
                "model": "llama-3.3-70b-versatile",  # use 70b for best prompt quality
                "messages": messages,
                "temperature": 0.4,
                "max_tokens": 4000,
            },
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]


# ── Prompt generation ─────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are an expert voice AI prompt engineer. 
Your job is to rewrite agent system prompts for a voice AI system based on a business description.

The voice AI system has 4 agents:
1. hello    — greets caller, routes to the right agent
2. service  — handles service/support related calls (returns, complaints, help, bookings, queries)  
3. sales    — handles sales/pricing/product questions and lead capture
4. closer   — wraps up the call warmly after another agent has helped

STRICT RULES you must follow:
- Keep ALL special tokens exactly as-is: [HANDOFF:service], [HANDOFF:sales], [HANDOFF:closer], [END_CALL], [MCP:...], [CONTEXT]
- Keep the routing logic in hello agent but adapt keywords to the business
- Keep the flow/structure of each prompt — only change the business name, persona, products/services, and examples
- Make examples realistic for THIS business
- Keep responses SHORT — agents speak in 1-2 sentences max
- Never remove the Critical Rules section from any prompt
- Output ONLY valid JSON — no markdown, no explanation, no backticks

Output format (pure JSON only):
{
  "business_name": "...",
  "agent_name": "...",
  "business_summary": "...",
  "hello": "...full prompt markdown...",
  "service": "...full prompt markdown...",
  "sales": "...full prompt markdown...",
  "closer": "...full prompt markdown..."
}"""


async def generate_prompts(business_content: str, agent_name: str, api_key: str) -> dict:
    logger.info("Generating prompts with Groq (llama-3.3-70b)...")

    user_message = f"""Here is the business content scraped from their website:

---
{business_content}
---

Agent name to use: {agent_name}

Rewrite all 4 agent prompts (hello, service, sales, closer) for this business.
Replace ALL car dealership references with this business's actual products/services/use cases.
Make the routing keywords in hello agent match what callers would actually say to this business.
Output ONLY the JSON object — nothing else."""

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_message},
    ]

    raw = await _groq_chat(messages, api_key)

    # Strip any accidental markdown fences
    raw = re.sub(r"```json|```", "", raw).strip()

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # Try to extract JSON object if surrounded by extra text
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if match:
            return json.loads(match.group())
        logger.error("Failed to parse JSON from LLM response:")
        logger.error(raw[:500])
        raise ValueError("LLM did not return valid JSON. Try again.")


# ── Save prompts ──────────────────────────────────────────────────────────────

def save_prompts(data: dict, preview: bool = False) -> None:
    agents = ["hello", "service", "sales", "closer"]

    print("\n" + "═" * 60)
    print(f"  Business : {data.get('business_name', 'Unknown')}")
    print(f"  Agent    : {data.get('agent_name', 'Unknown')}")
    print(f"  Summary  : {data.get('business_summary', '')[:100]}...")
    print("═" * 60 + "\n")

    for agent in agents:
        prompt = data.get(agent, "")
        if not prompt:
            logger.warning(f"No prompt generated for: {agent}")
            continue

        if preview:
            print(f"\n{'─'*60}")
            print(f"  PROMPT: {agent}.md")
            print(f"{'─'*60}")
            print(prompt[:400] + ("..." if len(prompt) > 400 else ""))
        else:
            path = PROMPTS_DIR / f"{agent}.md"
            # Backup original
            backup = PROMPTS_DIR / f"{agent}.md.bak"
            if path.exists() and not backup.exists():
                backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
                logger.info(f"  Backed up original → {backup.name}")

            path.write_text(prompt, encoding="utf-8")
            logger.info(f"  ✅ Saved → {path}")

    if preview:
        print("\n⚠️  Preview mode — no files were changed. Remove --preview to save.")
    else:
        print("\n✅ All prompts updated! Restart your server: python main.py")
        print("💡 Originals backed up as *.md.bak — restore with --restore if needed.\n")


def restore_prompts() -> None:
    """Restore original prompts from .bak files."""
    agents = ["hello", "service", "sales", "closer"]
    restored = 0
    for agent in agents:
        backup = PROMPTS_DIR / f"{agent}.md.bak"
        original = PROMPTS_DIR / f"{agent}.md"
        if backup.exists():
            original.write_text(backup.read_text(encoding="utf-8"), encoding="utf-8")
            backup.unlink()
            logger.info(f"  ✅ Restored {agent}.md")
            restored += 1
    if restored == 0:
        print("No backup files found (.md.bak). Nothing to restore.")
    else:
        print(f"\n✅ Restored {restored} prompt(s). Restart your server: python main.py")


# ── Main ──────────────────────────────────────────────────────────────────────


# ── PDF extraction ────────────────────────────────────────────────────────────

def _extract_pdf(path: str) -> str:
    """Extract text from a PDF file using pypdf."""
    try:
        from pypdf import PdfReader
    except ImportError:
        try:
            from PyPDF2 import PdfReader
        except ImportError:
            print("PDF support requires pypdf. Install it with: pip install pypdf")
            sys.exit(1)

    reader = PdfReader(path)
    pages_text = []
    for i, page in enumerate(reader.pages):
        text = page.extract_text() or ""
        text = text.strip()
        if text:
            pages_text.append(f"[PDF Page {i+1}]\n{text[:600]}")

    combined = "\n\n".join(pages_text)
    logger.info(f"Extracted {len(pages_text)} pages, {len(combined)} chars from {path}")
    return combined[:4000]


async def main_async(args) -> None:
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        print("GROQ_API_KEY not found in .env file.")
        sys.exit(1)

    if args.url or args.file:
        urls = list(args.url) if args.url else []

        if args.file:
            file_path = Path(args.file)
            if not file_path.exists():
                print(f"File not found: {args.file}")
                sys.exit(1)
            with open(file_path, encoding="utf-8") as f:
                file_urls = [line.strip() for line in f if line.strip() and not line.startswith("#")]
            logger.info(f"Loaded {len(file_urls)} URLs from {args.file}")
            urls.extend(file_urls)

        if not urls:
            print("No URLs found.")
            sys.exit(1)

        # Crawl mode — follow all links on same domain
        if args.crawl:
            logger.info(f"Crawl mode ON — max {args.max_pages} pages from {urls[0]}")
            business_content = await _crawl_site(urls[0], max_pages=args.max_pages)
        else:
            all_content = []
            for url in urls:
                fetched = await _fetch_url(url)
                if fetched:
                    all_content.append(f"[Page: {url}]\n{fetched}")
                    logger.info(f"Fetched {len(fetched)} chars from {url}")
                else:
                    logger.warning(f"Skipped (no content): {url}")

            if not all_content:
                print("Could not fetch content from any URL.")
                sys.exit(1)

            business_content = "\n\n".join(all_content)[:4000]
            logger.info(f"Total content: {len(business_content)} chars from {len(all_content)} page(s).")

    elif args.pdf:
        all_pdf_text = []
        for pdf_path in args.pdf:
            if not Path(pdf_path).exists():
                print(f"PDF not found: {pdf_path}")
                sys.exit(1)
            logger.info(f"Reading PDF: {pdf_path}")
            pdf_text = _extract_pdf(pdf_path)
            if pdf_text:
                all_pdf_text.append(f"[File: {pdf_path}]\n{pdf_text}")
        if not all_pdf_text:
            print("Could not extract text from any PDF.")
            sys.exit(1)
        business_content = "\n\n".join(all_pdf_text)[:4000]
        logger.info(f"Total PDF content: {len(business_content)} chars.")

    elif args.text:
        business_content = args.text
        logger.info(f"Using provided text ({len(business_content)} chars).")
    else:
        print("Provide --url, --file, --pdf, or --text.")
        sys.exit(1)

    data = await generate_prompts(business_content, args.agent_name, api_key)
    save_prompts(data, preview=args.preview)


def main():
    parser = argparse.ArgumentParser(
        description="Auto-generate agent prompts from a URL or text",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Single URL
  python generate_prompts.py --url https://www.crmlanding.in/

  # Multiple URLs
  python generate_prompts.py --url https://www.crmlanding.in/ https://www.crmlanding.in/about

  # From a .txt file (one URL per line)
  python generate_prompts.py --file urls.txt

  # From text
  python generate_prompts.py --text "We are a SaaS CRM platform..."

  # Preview without saving
  python generate_prompts.py --url https://www.crmlanding.in/ --preview

  # Custom agent name
  python generate_prompts.py --url https://www.crmlanding.in/ --agent-name "Priya"

  # Restore original prompts
  python generate_prompts.py --restore
        """,
    )
    parser.add_argument("--url", nargs="+", default=None, help="One or more URLs to scrape")
    parser.add_argument("--file", default=None, help=".txt file with one URL per line")
    parser.add_argument("--text", default=None, help="Paste business description directly")
    parser.add_argument("--pdf", nargs="+", default=None, help="One or more PDF files to read")
    parser.add_argument("--agent-name", default="Alex", help="Name for the voice agent (default: Alex)")
    parser.add_argument("--preview", action="store_true", help="Preview prompts without saving")
    parser.add_argument("--crawl", action="store_true", help="Crawl entire website (same domain)")
    parser.add_argument("--max-pages", type=int, default=20, help="Max pages to crawl (default: 20)")
    parser.add_argument("--restore", action="store_true", help="Restore original prompts from backup")
    args = parser.parse_args()

    if args.restore:
        restore_prompts()
        return

    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
