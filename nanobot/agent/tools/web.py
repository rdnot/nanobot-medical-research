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

# Scrapling availability check (async browser tier)
try:
    from scrapling.fetchers import AsyncStealthySession
    SCRAPLING_AVAILABLE = True
except ImportError:
    SCRAPLING_AVAILABLE = False

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


def _is_content_sufficient(content_bytes: bytes, url: str) -> bool:
    """
    Returns False if we got a JS shell → escalate to Scrapling browser.
    Tuned on real Reddit HTML (Feb 2026).
    """
    try:
        raw = content_bytes.decode("utf-8", errors="replace").lower()
    except Exception:
        return True

    # Real rendered pages are significantly larger than shells
    if len(raw) < 8000:
        return False

    # Generic JS-shell signals (framework-agnostic)
    if '<div id="root"></div>' in raw or '<div id="app"></div>' in raw:
        return False
    if any(sig in raw for sig in ["enable javascript", "requires javascript", "javascript is required"]):
        return False

    if "reddit.com" in url.lower():
        # Strong positive markers of real content
        if any(m in raw for m in [
            "shreddit-app",            # root component
            "shreddit-post",           # post body
            "shreddit-comment",        # crucial for threads
            "shreddit-comment-tree",   # comment container
            "faceplate-tracker",       # engagement tracker (only in real render)
            'data-testid="post-content"',
        ]):
            return True

        # Edge-case: old Reddit structure without new components = shell
        if 'id="comment-tree"' in raw and "shreddit-comment" not in raw:
            return False

    if "bbc.com" in url.lower() or "bbc.co.uk" in url.lower():
        # BBC SSR sends real HTML but article body is lazy-loaded via XHR.
        # The initial HTML only has a brief intro block — escalate to browser
        # unless we see the full article prose markers.
        # Live blogs (/news/live/) use different component names than standard articles.
        has_article_body = any(m in raw for m in [
            'data-component="text-block"',        # article body paragraphs
            'data-testid="article-body"',         # newer layout
            '"articleBody"',                      # JSON-LD structured data
            'data-e2e="article-body"',            # sport/live pages
            'data-testid="live-post"',            # live blog post block
            'data-component="livepost"',          # live blog component
            'data-component="liveblog"',          # live blog wrapper
            'data-testid="liveblog"',             # live blog testid
            'data-post-id=',                      # individual live blog post
            'data-testid="lx-stream-post"',       # live experience stream post
            'data-e2e="lx-stream-post"',          # live experience stream post (alt)
            '"liveblogposting"',                  # JSON-LD LiveBlogPosting type
        ])
        if not has_article_body:
            return False

    return True


def _is_cloudflare_protected(status: int | None, content: bytes | None) -> bool:
    """
    Detect if curl_cffi hit a solvable Cloudflare challenge page.
    Only returns True for actual CF interstitial/Turnstile pages — NOT bare 403s.
    A bare 403 (e.g. GameStop Bot Fight Mode) has no challenge to solve,
    so solve_cloudflare=True would waste time and still fail.
    """
    if not content:
        return False
    try:
        snippet = content[:8000].decode("utf-8", errors="replace").lower()
        return any(m in snippet for m in [
            "just a moment",            # CF interstitial spinner
            "cf-browser-verification",  # CF challenge form
            "checking your browser",    # CF spinner text
            "cf_chl_opt",               # CF challenge JS variable
            "challenge-platform",       # CF challenge platform
        ])
    except Exception:
        return False


async def _fetch_raw(url: str, proxy: str | None = None) -> tuple[bytes, dict, int, str]:
    """
    Fetch URL bytes with tiered fallback strategy:
      1. curl_cffi           — Chrome TLS impersonation, fast, no browser
                               (skipped for Reddit — always needs real browser)
      2. AsyncStealthySession — stealth Playwright (Patchright), handles JS-rendered
                                pages: Reddit comments, Cloudflare, heavy SPAs.
                                solve_cloudflare auto-enabled when CF detected.
      3. httpx               — last resort, no stealth
    Returns (content_bytes, headers_dict, status_code, fetcher_name)
    """
    is_reddit = "reddit.com" in url.lower()
    curl_cffi_status: int | None = None
    curl_cffi_content: bytes | None = None

    # --- Tier 1: curl_cffi (Chrome TLS fingerprint, fast, no browser) ---
    # Skipped for Reddit: always returns a JS shell or triggers "prove you are human"
    if not is_reddit:
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
                    headers={"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"},
                    proxy=proxy,
                )
                curl_cffi_status = r.status_code
                curl_cffi_content = r.content
                if r.status_code < 400 and _is_content_sufficient(r.content, url):
                    return r.content, dict(r.headers), r.status_code, "curl_cffi"
                # status >= 400 or JS shell → fall through to browser tier
        except httpx.ProxyError as e:
            logger.error("curl_cffi proxy error: {}", e)
            # Proxy error, skip to next tier
        except Exception as e:
            logger.error("curl_cffi error: {}", e)
            error_str = str(e).lower()
            # Check if it's a timeout error - if so, server is down, skip all other methods
            if any(x in error_str for x in ["timeout", "timed out", "operation timed out"]):
                logger.error("curl_cffi timeout → server appears down, skipping other fetchers")
                raise Exception(f"Server timeout: {url} is not responding") from e

    # --- Tier 2: AsyncStealthySession (scrapling) — stealth Playwright (Patchright) ---
    # Uses Playwright Chromium + Patchright stealth patches (Camoufox removed in v0.4)
    # network_idle is intentionally disabled for both Reddit and CF sites:
    #   - Reddit: never fully idles (realtime polls, ads, notifications) → waits full timeout
    #   - CF sites: background pings after Turnstile solve → hangs
    # Instead we use load event + a short fixed wait for JS content to inject
    if SCRAPLING_AVAILABLE:
        try:
            solve_cf = _is_cloudflare_protected(curl_cffi_status, curl_cffi_content)
            if solve_cf:
                logger.debug("Cloudflare detected → enabling solve_cloudflare")
            logger.debug("AsyncStealthySession fetch: {}", "proxy enabled" if proxy else "direct connection")
            async with AsyncStealthySession(
                headless=True,
                solve_cloudflare=solve_cf,
                proxy=proxy,
            ) as session:
                page = await session.fetch(
                    url,
                    network_idle=False,          # disabled — Reddit/CF never fully idle
                    adaptive=True,
                    timeout=30000 if solve_cf else 45000,  # CF=30s, Reddit/SPA=45s
                )
                if page:
                    status = getattr(page, "status", getattr(page, "status_code", 200))
                    if status < 400:
                        html_bytes = getattr(page, "html_content", getattr(page, "html", "")).encode("utf-8", errors="replace")
                        headers = {"content-type": "text/html; charset=utf-8"}
                        logger.debug("Scrapling browser fetch succeeded")
                        return html_bytes, headers, status, "scrapling"
        except Exception as e:
            logger.error("Scrapling error: {}", e)

    # --- Tier 3: httpx (last resort, no stealth) ---
    try:
        logger.debug("httpx fetch (fallback): {}", "proxy enabled" if proxy else "direct connection")
        async with httpx.AsyncClient(
            follow_redirects=True,
            max_redirects=MAX_REDIRECTS,
            timeout=30.0,
            headers={"User-Agent": USER_AGENT},
            proxy=proxy,
        ) as client:
            r = await client.get(url)
            # Don't raise_for_status — caller needs content even on 4xx/5xx for error diagnosis
            logger.debug("httpx fallback fetch succeeded")
            return r.content, dict(r.headers), r.status_code, "httpx"
    except httpx.ProxyError as e:
        logger.error("httpx proxy error: {}", e)
        raise Exception(f"All fetchers failed for {url}: {e}") from e
    except Exception as e:
        logger.error("httpx error: {}", e)
        raise Exception(f"All fetchers failed for {url}: {e}") from e


def _extract_jsonld_liveblog(raw_html: str, extract_mode: str = "markdown") -> str | None:
    """
    Extract BBC (and any site using schema.org) live blog content from JSON-LD.
    Looks for @type=LiveBlogPosting with liveBlogUpdate array.
    Returns formatted text or None if not found / insufficient content.
    """
    scripts = re.findall(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>([\s\S]*?)</script>', raw_html, re.I)
    for script in scripts:
        try:
            data = json.loads(script)
        except (json.JSONDecodeError, ValueError):
            continue

        # Handle @graph wrapper
        if isinstance(data, dict) and "@graph" in data:
            candidates = data["@graph"]
        elif isinstance(data, list):
            candidates = data
        else:
            candidates = [data]

        for item in candidates:
            if not isinstance(item, dict):
                continue
            item_type = item.get("@type", "")
            if isinstance(item_type, list):
                item_type = " ".join(item_type)
            if "LiveBlogPosting" not in item_type:
                continue

            updates = item.get("liveBlogUpdate", [])
            if not updates or len(updates) < 2:
                continue

            # Build blog title header
            blog_title = item.get("headline", item.get("name", ""))
            lines: list[str] = []
            if blog_title:
                lines.append(f"# {blog_title}\n")

            for post in updates:
                if not isinstance(post, dict):
                    continue
                headline = post.get("headline", "")
                date_pub = post.get("datePublished", "")
                body = post.get("articleBody", post.get("text", ""))

                # articleBody can be plain text or nested HTML — strip tags if needed
                if body and re.search(r'<[a-z]', body, re.I):
                    body = _normalize(_strip_tags(body))

                if not headline and not body:
                    continue

                # Timestamp (ISO → HH:MM if possible)
                time_str = ""
                if date_pub:
                    m = re.search(r'T(\d{2}:\d{2})', date_pub)
                    time_str = f" — {m.group(1)}" if m else f" — {date_pub}"

                if extract_mode == "markdown":
                    if headline:
                        lines.append(f"## {headline}{time_str}")
                    if body:
                        lines.append(body)
                    lines.append("")
                else:
                    if headline:
                        lines.append(f"{headline}{time_str}")
                    if body:
                        lines.append(body)
                    lines.append("")

            text = "\n".join(lines).strip()
            if len(text) > 200:
                return text

    return None


def _extract_bbc_liveblog_html(raw_html: str, extract_mode: str = "markdown") -> str | None:
    """
    Fallback BBC live blog extractor targeting data-testid="content-post" article elements.
    Used when JSON-LD is absent or too sparse (e.g. BBC strips body text from JSON-LD).
    Returns formatted text or None.
    """
    # Find all live post articles
    posts = re.findall(
        r'<article[^>]+data-testid=["\']content-post["\'][^>]*>([\s\S]*?)</article>',
        raw_html, re.I
    )
    if not posts:
        return None

    lines: list[str] = []

    # Page title from <h1> or og:title
    title_m = re.search(r'<h1[^>]*>([\s\S]*?)</h1>', raw_html, re.I)
    if title_m:
        title = _strip_tags(title_m.group(1)).strip()
        if title:
            lines.append(f"# {title}\n")

    for post_html in posts:
        # Headline: <h3> inside header
        h_m = re.search(r'<h[23][^>]*>([\s\S]*?)</h[23]>', post_html, re.I)
        headline = _strip_tags(h_m.group(1)).strip() if h_m else ""

        # Timestamp
        ts_m = re.search(r'data-testid=["\']timestamp["\'][^>]*>([\s\S]*?)</', post_html, re.I)
        time_str = f" — {_strip_tags(ts_m.group(1)).strip()}" if ts_m else ""

        # Body paragraphs — grab all <p> not inside <header>
        # Strip the header block first to avoid picking up lede text twice
        body_html = re.sub(r'<header[\s\S]*?</header>', '', post_html, flags=re.I)
        paragraphs = re.findall(r'<p[^>]*>([\s\S]*?)</p>', body_html, re.I)
        body = "\n\n".join(_strip_tags(p).strip() for p in paragraphs if _strip_tags(p).strip())

        if not headline and not body:
            continue

        if extract_mode == "markdown":
            if headline:
                lines.append(f"## {headline}{time_str}")
            if body:
                lines.append(body)
            lines.append("")
        else:
            if headline:
                lines.append(f"{headline}{time_str}")
            if body:
                lines.append(body)
            lines.append("")

    text = "\n".join(lines).strip()
    return text if len(text) > 200 else None


def _extract_bbc_next_data(raw_html: str, extract_mode: str = "markdown") -> str | None:
    """
    Extract BBC standard article content from __NEXT_DATA__ (Optimo CMS / Next.js).

    Actual JSON path (verified 2026-03-12):
      props -> pageProps -> page -> <article-key> -> contents[]

    Each content block has:
      { "type": "headline"|"paragraph"|"text"|"subheadline", "model": { "blocks": [...] } }

    Inner blocks carry the actual text:
      { "type": "fragment", "model": { "text": "..." } }
    or for paragraphs, a nested "blocks" list of fragments.

    Returns formatted text or None if not found / insufficient content.
    """
    next_data_m = re.search(
        r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>([\s\S]*?)</script>', raw_html, re.I
    )
    if not next_data_m:
        return None

    try:
        data = json.loads(next_data_m.group(1))
    except (json.JSONDecodeError, ValueError):
        return None

    try:
        page_props = data.get("props", {}).get("pageProps", {})
        # The article lives under pageProps.page, keyed by a CMS path string
        # e.g. '"news","articles","c0e55g03v2zo",' — we don't know the key,
        # so grab the first dict value that has a "contents" list.
        page = page_props.get("page", {})
        article_data: dict | None = None
        if isinstance(page, dict):
            for v in page.values():
                if isinstance(v, dict) and "contents" in v:
                    article_data = v
                    break

        if not article_data:
            return None

        contents = article_data.get("contents", [])
        if not contents:
            return None

        def _blocks_to_text(blocks: list) -> str:
            """Recursively collect text from Optimo fragment/inline blocks."""
            parts = []
            for b in blocks:
                if not isinstance(b, dict):
                    continue
                model = b.get("model", {})
                # Leaf fragment: has direct text
                if "text" in model and isinstance(model["text"], str):
                    parts.append(model["text"])
                # Nested blocks
                elif "blocks" in model and isinstance(model["blocks"], list):
                    parts.append(_blocks_to_text(model["blocks"]))
            return "".join(parts)

        lines: list[str] = []

        # Article-level headline from metadata
        metadata = article_data.get("metadata", {})
        title = metadata.get("headline") or metadata.get("title") or ""
        if not title:
            # Try pageProps.metadata
            title = page_props.get("metadata", {}).get("headline", "")
        if title:
            lines.append(f"# {title}\n")

        for block in contents:
            if not isinstance(block, dict):
                continue
            btype = block.get("type", "")
            model = block.get("model", {})
            inner_blocks = model.get("blocks", [])

            if btype == "headline":
                text = _blocks_to_text(inner_blocks).strip()
                if text:
                    lines.append(f"# {text}\n")

            elif btype == "subheadline":
                text = _blocks_to_text(inner_blocks).strip()
                if text:
                    lines.append(f"## {text}\n")

            elif btype in ("paragraph", "text"):
                text = _blocks_to_text(inner_blocks).strip()
                if text:
                    lines.append(text)
                    lines.append("")

            # Skip images, media, crossheads, ads, etc.

        result = "\n".join(lines).strip()
        return result if len(result) > 300 else None

    except Exception:
        return None


def _html_to_text(raw_html: str, extract_mode: str = "markdown", url: str = "") -> tuple[str, str]:
    """
    Extract main content from HTML.
    Tries trafilatura first (best for articles), falls back to readability.
    Returns (text, extractor_name)
    """
    is_markdown = extract_mode == "markdown"

    # --- BBC live blog: JSON-LD first, then custom HTML parser ---
    # trafilatura and readability both fail on BBC's React/SSR live blog structure.
    # JSON-LD (LiveBlogPosting) is the cleanest source; HTML fallback targets
    # data-testid="content-post" article elements directly.
    is_bbc = "bbc.com" in url.lower() or "bbc.co.uk" in url.lower()
    is_live = "/news/live/" in url.lower() or "/sport/live/" in url.lower()
    if is_bbc and is_live:
        result = _extract_jsonld_liveblog(raw_html, extract_mode)
        if result:
            return result, "jsonld_liveblog"
        result = _extract_bbc_liveblog_html(raw_html, extract_mode)
        if result:
            return result, "bbc_liveblog_html"

    # --- BBC standard article: Optimo CMS via __NEXT_DATA__ ---
    # readability/trafilatura cannot reach content stored in Next.js JSON.
    # Verified path: props.pageProps.page.<cms-key>.contents[]
    if is_bbc and not is_live:
        result = _extract_bbc_next_data(raw_html, extract_mode)
        if result:
            return result, "bbc_next_data"

    # --- Primary: trafilatura ---
    try:
        import trafilatura
        common_kwargs = dict(
            include_tables=True,
            include_images=False,
            include_links=is_markdown,
            output_format="markdown" if is_markdown else "txt",
            with_metadata=False,
        )
        result = trafilatura.extract(raw_html, **common_kwargs)

        # BBC (and some other news sites) get rejected by trafilatura's default
        # paywall/quality heuristic. Re-extract with favour_recall=True which
        # disables content-length and quality filters.
        if (not result or len(result.strip()) < 200):
            result = trafilatura.extract(raw_html, favour_recall=True, **common_kwargs)

        if result and len(result.strip()) > 50:
            return result, "trafilatura"
    except ImportError:
        pass

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
        return _normalize(md(raw_html, heading_style="ATX", strip=[]))
    except ImportError:
        pass

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
                    logger.debug("SearXNG search failed with error, falling back to Brave API")
            except Exception as e:
                logger.debug("SearXNG search threw exception: {}, falling back to Brave API", e)
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

    Fetcher priority:  curl_cffi → StealthyFetcher (scrapling) → httpx
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

    def __init__(self, max_chars: int = 200000, proxy: str | None = None):
        self.max_chars = max_chars
        self.proxy = proxy
        self._cache: dict[str, str] = {}  # session-level URL cache
        self._cache_max = 50  # prevent unbounded memory growth

    async def execute(self, url: str, extractMode: str = "markdown", **kwargs: Any) -> str:
        # Validate
        is_valid, error_msg = _validate_url(url)
        if not is_valid:
            return json.dumps({"error": f"URL validation failed: {error_msg}", "url": url}, ensure_ascii=False)

        # Convert Reddit URLs to JSON API endpoints
        from urllib.parse import urlparse, urlunparse
        parsed = urlparse(url)
        if "reddit.com" in parsed.netloc.lower() and not parsed.path.endswith(".json"):
            new_path = parsed.path.rstrip('/') + '/.json'
            url = urlunparse(parsed._replace(path=new_path))

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
                text, extractor = _html_to_text(raw_html, extractMode, url=url)
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

            if len(self._cache) >= self._cache_max:
                # Evict oldest entry (insertion-order dict, Python 3.7+)
                self._cache.pop(next(iter(self._cache)))
            self._cache[cache_key] = result
            return result

        except Exception as e:
            return json.dumps({"error": str(e), "url": url}, ensure_ascii=False)
