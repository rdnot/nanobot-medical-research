"""Web tools: web_search and web_fetch."""

import html
import json
import os
import re
from typing import Any
from urllib.parse import urlparse

import httpx
from loguru import logger

from nanobot.agent.tools.base import Tool

# Shared constants
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_7_2) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
MAX_REDIRECTS = 5
DEFAULT_SEARXNG_URL = ""  # Local SearXNG instance eg. http://localhost:8888


def _strip_tags(text: str) -> str:
    """Remove HTML tags and decode entities."""
    text = re.sub(r'<script[\s\S]*?</script>', '', text, flags=re.I)
    text = re.sub(r'<style[\s\S]*?</style>', '', text, flags=re.I)
    text = re.sub(r'<[^>]+>', '', text)
    return html.unescape(text).strip()


def _normalize(text: str) -> str:
    """Normalize whitespace."""
    text = re.sub(r'[ \t]+', ' ', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def _validate_url(url: str) -> tuple[bool, str]:
    """Validate URL: must be http(s) with valid domain."""
    try:
        p = urlparse(url)
        if p.scheme not in ('http', 'https'):
            return False, f"Only http/https allowed, got '{p.scheme or 'none'}'"
        if not p.netloc:
            return False, "Missing domain"
        return True, ""
    except Exception as e:
        return False, str(e)


def _smart_truncate(text: str, max_chars: int) -> str:
    """Truncate at paragraph boundary instead of mid-sentence."""
    if len(text) <= max_chars:
        return text
    cutoff = text[:max_chars].rfind('\n\n')
    if cutoff > max_chars * 0.8:
        return text[:cutoff] + "\n\n[...truncated...]"
    cutoff = text[:max_chars].rfind('. ')
    if cutoff > max_chars * 0.8:
        return text[:cutoff + 1] + " [...truncated...]"
    return text[:max_chars] + " [...truncated...]"


def _extract_pdf_text(pdf_data: bytes) -> str:
    """Extract text from PDF using PyMuPDF."""
    try:
        import fitz  # PyMuPDF
        doc = fitz.open(stream=pdf_data, filetype="pdf")
        text_lines = []
        for page_num in range(len(doc)):
            page = doc[page_num]
            text = page.get_text()
            text_lines.append(f"--- Page {page_num + 1} ---\n{text}")
        doc.close()
        return "\n".join(text_lines)
    except ImportError:
        return "Error: PyMuPDF (fitz) not installed. Install with: pip install PyMuPDF"
    except Exception as e:
        return f"Error extracting PDF: {e}"


def _extract_meta(raw_html: str) -> dict[str, str]:
    """Extract useful meta tags: author, date, description, og fields."""
    meta: dict[str, str] = {}
    patterns = [
        (r'<meta\s+name=["\']author["\']\s+content=["\']([^"\']+)["\']', 'author'),
        (r'<meta\s+property=["\']article:author["\']\s+content=["\']([^"\']+)["\']', 'author'),
        (r'<meta\s+property=["\']article:published_time["\']\s+content=["\']([^"\']+)["\']', 'published'),
        (r'<meta\s+name=["\']publication_date["\']\s+content=["\']([^"\']+)["\']', 'published'),
        (r'<meta\s+name=["\']description["\']\s+content=["\']([^"\']+)["\']', 'description'),
        (r'<meta\s+property=["\']og:description["\']\s+content=["\']([^"\']+)["\']', 'description'),
        (r'<meta\s+property=["\']og:site_name["\']\s+content=["\']([^"\']+)["\']', 'site_name'),
    ]
    for pattern, key in patterns:
        if key not in meta:
            m = re.search(pattern, raw_html, re.I)
            if m:
                meta[key] = html.unescape(m.group(1).strip())
    return meta


async def _fetch_raw(url: str, proxy: str | None = None) -> tuple[bytes, dict, int, str]:
    """
    Fetch URL bytes. Tries curl_cffi (Chrome impersonation) first,
    falls back to httpx if curl_cffi is not installed or fails.
    Returns (content_bytes, headers_dict, status_code, fetcher_name)
    """
    # --- Primary: curl_cffi (bypasses Cloudflare, TLS fingerprinting) ---
    try:
        from curl_cffi.requests import AsyncSession
        logger.debug("curl_cffi fetch: {}", "proxy enabled" if proxy else "direct connection")
        async with AsyncSession() as session:
            r = await session.get(
                url,
                impersonate="chrome",
                allow_redirects=True,
                max_redirects=MAX_REDIRECTS,
                timeout=30,
                proxy=proxy,
            )
            return r.content, dict(r.headers), r.status_code, "curl_cffi"
    except ImportError:
        print("curl_cffi not installed – run: pip install curl_cffi")
    except Exception:
        pass  # curl_cffi failed, fall through to httpx

    # --- Fallback: httpx ---
    logger.debug("httpx fetch: {}", "proxy enabled" if proxy else "direct connection")
    async with httpx.AsyncClient(
        follow_redirects=True,
        max_redirects=MAX_REDIRECTS,
        timeout=30.0,
        proxy=proxy,
    ) as client:
        r = await client.get(url, headers={"User-Agent": USER_AGENT})
        r.raise_for_status()
        return r.content, dict(r.headers), r.status_code, "httpx"


def _html_to_text(raw_html: str, extract_mode: str = "markdown") -> tuple[str, str]:
    """
    Extract main content from HTML.
    Tries trafilatura first (best for articles), falls back to readability.
    Returns (text, extractor_name)
    """
    # --- Primary: trafilatura ---
    try:
        import trafilatura
        result = trafilatura.extract(
            raw_html,
            include_tables=True,
            include_images=False,
            include_links=(extract_mode == "markdown"),
            output_format="markdown" if extract_mode == "markdown" else "txt",
            with_metadata=False,
        )
        if result and len(result.strip()) > 200:
            return result, "trafilatura"
    except ImportError:
        print("trafilatura not installed – pip install trafilatura")

    # --- Fallback: readability ---
    try:
        from readability import Document
        doc = Document(raw_html)
        summary = doc.summary()
        if extract_mode == "markdown":
            content = _readability_to_markdown(summary)
        else:
            content = _strip_tags(summary)
        title = doc.title() or ""
        text = f"# {title}\n\n{content}" if title else content
        return text, "readability"
    except Exception:
        pass

    # --- Last resort: strip tags ---
    return _normalize(_strip_tags(raw_html)), "strip_tags"


def _readability_to_markdown(raw_html: str) -> str:
    """Convert readability HTML output to markdown."""
    # Try markdownify first
    try:
        from markdownify import markdownify as md
        return _normalize(md(raw_html, heading_style="ATX", strip=['a'] if False else []))
    except ImportError:
        print("markdownify not installed  –  pip install markdownify")

    # Manual fallback (original logic)
    text = re.sub(r'<a\s+[^>]*href=["\']([^"\']+)["\'][^>]*>([\s\S]*?)</a>',
                  lambda m: f'[{_strip_tags(m[2])}]({m[1]})', raw_html, flags=re.I)
    text = re.sub(r'<h([1-6])[^>]*>([\s\S]*?)</h\1>',
                  lambda m: f'\n{"#" * int(m[1])} {_strip_tags(m[2])}\n', text, flags=re.I)
    text = re.sub(r'<li[^>]*>([\s\S]*?)</li>', lambda m: f'\n- {_strip_tags(m[1])}', text, flags=re.I)
    text = re.sub(r'</(p|div|section|article)>', '\n\n', text, flags=re.I)
    text = re.sub(r'<(br|hr)\s*/?>', '\n', text, flags=re.I)
    return _normalize(_strip_tags(text))


class WebSearchTool(Tool):
    """Search the web using local SearXNG (with Brave fallback)."""

    name = "web_search"
    description = "Search the web. Returns titles, URLs, and snippets."
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query"},
            "count": {"type": "integer", "description": "Results (1-10)", "minimum": 1, "maximum": 10}
        },
        "required": ["query"]
    }

    def __init__(self, api_key: str | None = None, max_results: int = 5, proxy: str | None = None):
        self._init_api_key = api_key
        self.searxng_url = os.environ.get("SEARXNG_URL", DEFAULT_SEARXNG_URL)
        self.max_results = max_results
        self.proxy = proxy

    @property
    def api_key(self) -> str:
        """Resolve API key at call time so env/config changes are picked up."""
        return self._init_api_key or os.environ.get("BRAVE_API_KEY", "")

    async def execute(self, query: str, count: int | None = None, **kwargs: Any) -> str:
        # Try local SearXNG first
        if self.searxng_url:
            try:
                result = await self._search_searxng(query, count)
                if not result.startswith("Error"):
                    return result
                else:
                    # SearXNG returned an error, falling back to Brave
                    print(f" DEBUG: SearXNG search failed with error, falling back to Brave API")
            except Exception as e:
                # SearXNG threw an exception, falling back to Brave
                print(f" DEBUG: SearXNG search threw exception, falling back to Brave API")
        else:
            pass

        # Fallback to Brave
        return await self._search_brave(query, count)

    async def _search_searxng(self, query: str, count: int | None = None) -> str:
        """Search using local SearXNG instance."""
        n = min(max(count or self.max_results, 1), 10)

        try:
            logger.debug("SearXNG search: {}", "proxy enabled" if self.proxy else "direct connection")
            async with httpx.AsyncClient(proxy=self.proxy) as client:
                r = await client.get(
                    f"{self.searxng_url.rstrip('/')}/search",
                    params={"q": query, "format": "json"},
                    headers={"Accept": "application/json", "User-Agent": USER_AGENT},
                    timeout=10.0
                )
                r.raise_for_status()

            data = r.json()
            results = data.get("results", [])

            if not results:
                return f"No results for: {query}"

            lines = [f"Results for: {query}\n"]
            for i, item in enumerate(results[:n], 1):
                lines.append(f"{i}. {item.get('title', '')}\n   {item.get('url', '')}")
                if content := item.get("content"):
                    lines.append(f"   {content}")
                if published := item.get("publishedDate"):
                    lines.append(f"   Published: {published}")
            return "\n".join(lines)
        except httpx.ProxyError as e:
            logger.error("SearXNG proxy error: {}", e)
            return f"Error: {e}"
        except Exception as e:
            logger.error("SearXNG error: {}", e)
            return f"Error: {e}"

    async def _search_brave(self, query: str, count: int | None = None) -> str:
        """Search using Brave Search API."""
        
        if not self.api_key:
            return (
                "Error: Brave Search API key not configured. "
                "Set it in ~/.nanobot/config.json under tools.web.search.apiKey "
                "(or export BRAVE_API_KEY), then restart the gateway."
            )

        try:
            n = min(max(count or self.max_results, 1), 10)
            logger.debug("Brave search: {}", "proxy enabled" if self.proxy else "direct connection")
            async with httpx.AsyncClient(proxy=self.proxy) as client:
                r = await client.get(
                    "https://api.search.brave.com/res/v1/web/search",
                    params={"q": query, "count": n},
                    headers={"Accept": "application/json", "X-Subscription-Token": self.api_key},
                    timeout=10.0
                )
                r.raise_for_status()

            results = r.json().get("web", {}).get("results", [])
            if not results:
                return f"No results for: {query}"

            lines = [f"Results for: {query}\n"]
            for i, item in enumerate(results[:n], 1):
                lines.append(f"{i}. {item.get('title', '')}\n   {item.get('url', '')}")
                if desc := item.get("description"):
                    lines.append(f"   {desc}")
                if age := item.get("age"):
                    lines.append(f"   Published: {age}")
            return "\n".join(lines)
        except httpx.ProxyError as e:
            logger.error("Brave search proxy error: {}", e)
            return f"Error: {e}"
        except Exception as e:
            logger.error("Brave search error: {}", e)
            return f"Error: {e}"


class WebFetchTool(Tool):
    """
    Fetch and extract content from a URL.

    Fetcher priority:  curl_cffi (Chrome impersonation) → httpx
    Extractor priority: trafilatura → readability → strip_tags
    """

    name = "web_fetch"
    description = "Fetch URL and extract readable content (HTML → markdown/text)."
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "URL to fetch"},
            "extractMode": {"type": "string", "enum": ["markdown", "text"], "default": "markdown"}
        },
        "required": ["url"]
    }

    def __init__(self, max_chars: int = 500000, proxy: str | None = None):
        self.max_chars = max_chars
        self.proxy = proxy
        self._cache: dict[str, str] = {}  # session-level URL cache

    async def execute(self, url: str, extractMode: str = "markdown", **kwargs: Any) -> str:
        # Validate
        is_valid, error_msg = _validate_url(url)
        if not is_valid:
            return json.dumps({"error": f"URL validation failed: {error_msg}", "url": url}, ensure_ascii=False)

        # Cache hit
        cache_key = f"{url}::{extractMode}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        try:
            content_bytes, headers, status_code, fetcher = await _fetch_raw(url, self.proxy)
            ctype = headers.get("content-type", "").lower()

            # --- PDF ---
            if "application/pdf" in ctype or url.lower().endswith(".pdf"):
                text = _extract_pdf_text(content_bytes)
                text = _smart_truncate(text, self.max_chars)
                result = json.dumps({
                    "url": url, "status": status_code, "fetcher": fetcher,
                    "extractor": "pymupdf", "truncated": "[...truncated...]" in text,
                    "word_count": len(text.split()), "length": len(text), "text": text
                }, ensure_ascii=False)

            # --- JSON ---
            elif "application/json" in ctype:
                text = json.dumps(json.loads(content_bytes), indent=2, ensure_ascii=False)
                text = _smart_truncate(text, self.max_chars)
                result = json.dumps({
                    "url": url, "status": status_code, "fetcher": fetcher,
                    "extractor": "json", "truncated": "[...truncated...]" in text,
                    "word_count": len(text.split()), "length": len(text), "text": text
                }, ensure_ascii=False)

            # --- HTML ---
            elif "text/html" in ctype or content_bytes[:256].lower().startswith((b"<!doctype", b"<html")):
                raw_html = content_bytes.decode("utf-8", errors="replace")
                meta = _extract_meta(raw_html)
                text, extractor = _html_to_text(raw_html, extractMode)
                text = _smart_truncate(text, self.max_chars)
                result = json.dumps({
                    "url": url, "status": status_code, "fetcher": fetcher,
                    "extractor": extractor, "truncated": "[...truncated...]" in text,
                    "word_count": len(text.split()), "length": len(text),
                    "meta": meta, "text": text
                }, ensure_ascii=False)

            # --- XML (PubMed, RSS, SearXNG, etc.) ---
            elif "xml" in ctype:
                text = content_bytes.decode("utf-8", errors="replace")
                text = re.sub(r'<\?xml[^>]+\?>', '', text)
                text = _normalize(_strip_tags(text))
                text = _smart_truncate(text, self.max_chars)
                result = json.dumps({
                    "url": url, "status": status_code, "fetcher": fetcher,
                    "extractor": "xml", "truncated": "[...truncated...]" in text,
                    "word_count": len(text.split()), "length": len(text), "text": text
                }, ensure_ascii=False)

            # --- Raw fallback ---
            else:
                text = content_bytes.decode("utf-8", errors="replace")
                text = _smart_truncate(text, self.max_chars)
                result = json.dumps({
                    "url": url, "status": status_code, "fetcher": fetcher,
                    "extractor": "raw", "truncated": "[...truncated...]" in text,
                    "word_count": len(text.split()), "length": len(text), "text": text
                }, ensure_ascii=False)

            self._cache[cache_key] = result
            return result

        except Exception as e:
            return json.dumps({"error": str(e), "url": url}, ensure_ascii=False)
