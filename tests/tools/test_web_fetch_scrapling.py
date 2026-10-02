"""Tests for the scrapling branch's browser tier and page heuristics."""

from __future__ import annotations

import json
import socket
import sys
import types
from typing import Any
from unittest.mock import AsyncMock, patch
from urllib.request import getproxies_environment

import httpx
import pytest

from nanobot.agent.tools import web as web_module
from nanobot.agent.tools.registry import is_tool_error_result
from nanobot.agent.tools.web import (
    WebFetchTool,
    WebSearchConfig,
    WebSearchTool,
    _has_pubmed_article_content,
    _is_cloudflare_protected,
    _is_content_sufficient,
    _is_recaptcha_challenge,
)

_HTML = "<html><head><title>T</title></head><body><p>Hello world</p></body></html>"
_PROXY_ENV_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")


@pytest.fixture(autouse=True)
def _clear_proxy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (*_PROXY_ENV_VARS, "NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("nanobot.security.network.getproxies", getproxies_environment)


def _fake_resolve_public(hostname, port, family=0, type_=0, proto=0, flags=0):
    return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", 0))]


# ---------------------------------------------------------------------------
# heuristics
# ---------------------------------------------------------------------------


def test_small_non_html_payloads_are_sufficient():
    assert _is_content_sufficient(b'{"esummaryresult": []}', "https://eutils.ncbi.nlm.nih.gov/x", "application/json")
    assert _is_content_sufficient(b"<rss/>", "https://x.example/feed", "application/rss+xml")
    assert _is_content_sufficient(b"%PDF-1.4", "https://x.example/a.pdf", "application/pdf")
    assert _is_content_sufficient(b"plain text", "https://x.example/robots.txt", "text/plain")
    # No content-type and no HTML markers: treat as data, not as a shell.
    assert _is_content_sufficient(b'{"a": 1}', "https://x.example/api", "")


def test_small_real_html_page_is_sufficient():
    page = "<html><body>" + "<p>Real sentence here.</p>" * 30 + "</body></html>"
    assert _is_content_sufficient(page.encode(), "https://x.example/page", "text/html")


def test_small_script_only_shell_is_insufficient():
    shell = '<html><head><script src="app.js"></script></head><body><div id="root"></div></body></html>'
    assert not _is_content_sufficient(shell.encode(), "https://x.example/app", "text/html")
    sparse = "<html><body><script>boot()</script><p>hi</p></body></html>"
    assert not _is_content_sufficient(sparse.encode(), "https://x.example/app", "text/html")


def test_cloudflare_and_javascript_walls_are_insufficient():
    cf = ("<html><title>Just a moment...</title>" + "x" * 9000 + "</html>").encode()
    assert not _is_content_sufficient(cf, "https://x.example/", "text/html")
    assert _is_cloudflare_protected(cf)
    beacon = ("<html><script src='/cdn-cgi/challenge-platform/scripts/jsd/main.js'></script>"
              + "<p>real</p>" * 2000 + "</html>").encode()
    assert not _is_cloudflare_protected(beacon)
    js_wall = ("<html><body>Please enable JavaScript to continue" + "x" * 9000 + "</body></html>").encode()
    assert not _is_content_sufficient(js_wall, "https://x.example/", "text/html")


def test_ncbi_bookshelf_banner_is_not_a_shell():
    page = ("<html><body>This site requires JavaScript<p>StatPearls</p>" + "x" * 9000 + "</body></html>").encode()
    assert _is_content_sufficient(page, "https://www.ncbi.nlm.nih.gov/books/NBK1/", "text/html")


def test_pubmed_markers():
    challenge = b"<html>Checking your browser <script src='recaptcha/enterprise.js'></script></html>"
    assert _is_recaptcha_challenge(challenge)
    assert not _has_pubmed_article_content(challenge)
    article = b'<html><body><div id="main-content"><section class="abstract">x</section></div></body></html>'
    assert _has_pubmed_article_content(article)
    assert not _has_pubmed_article_content(b"<html><title>only</title></html>")


def test_pubmed_article_without_known_markers_is_accepted_by_visible_text():
    # Current PMC markup carries none of the legacy markers; a real article is
    # recognised by its amount of visible prose instead of exact HTML hooks.
    body = "".join(f"<p>Pneumonia paragraph {i} with clinical detail and findings.</p>" for i in range(80))
    page = f"<html><head><title>Pneumonia - PMC</title></head><body><main>{body}</main></body></html>".encode()
    assert _has_pubmed_article_content(page)

    shell = (
        "<html><head><title>Pneumonia - PMC</title><script src='app.js'></script></head>"
        "<body><div id='root'></div></body></html>"
    ).encode()
    assert not _has_pubmed_article_content(shell)


def test_ncbi_cookie_proof_of_work_shell_is_a_challenge():
    shell = (
        "<html><head><title>PMC</title></head><body><p>Cookies must be enabled to use this site.</p>"
        "<script>/* proof of work */</script></body></html>"
    ).encode()
    assert _is_recaptcha_challenge(shell)
    assert not _has_pubmed_article_content(shell)
    # A long real article that merely mentions cookies is not a challenge.
    article = ("<html><body>" + "<p>Cookies must be enabled is a phrase in this methods text.</p>" * 400 + "</body></html>").encode()
    assert not _is_recaptcha_challenge(article)
    assert _has_pubmed_article_content(article)


# ---------------------------------------------------------------------------
# browser tier
# ---------------------------------------------------------------------------


class _FakePage:
    def __init__(self, html: str, status: int = 200, headers: dict[str, str] | None = None, dom: str | None = None):
        self.html_content = html
        self.status = status
        self.dom = dom  # live DOM the page action sees, when different from html_content
        if headers is not None:
            self.headers = headers


class _FakeBrowserPage:
    """Minimal Playwright-page stand-in for page actions."""

    def __init__(self, dom: str):
        self._dom = dom

    async def content(self) -> str:
        return self._dom

    async def wait_for_timeout(self, ms: int) -> None:
        return None


def _install_fake_scrapling(monkeypatch, pages: list[Any], calls: list[dict[str, Any]]) -> None:
    class AsyncStealthySession:
        def __init__(self, **kwargs):
            calls.append({"session": kwargs})

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def fetch(self, **kwargs):
            calls.append({"fetch": kwargs})
            page = pages.pop(0)
            if isinstance(page, Exception):
                raise page
            action = kwargs.get("page_action")
            if action is not None and getattr(page, "dom", None) is not None:
                await action(_FakeBrowserPage(page.dom))
            return page

    fetchers = types.ModuleType("scrapling.fetchers")
    fetchers.AsyncStealthySession = AsyncStealthySession  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "scrapling", types.ModuleType("scrapling"))
    monkeypatch.setitem(sys.modules, "scrapling.fetchers", fetchers)
    monkeypatch.setattr(web_module, "SCRAPLING_AVAILABLE", True)


@pytest.mark.asyncio
async def test_browser_tier_serves_generic_js_shell_site(monkeypatch):
    """Regression: the browser tier must work for non-NCBI URLs (no page action)."""
    calls: list[dict[str, Any]] = []
    rendered = "<html><body>" + "<p>Rendered comment text.</p>" * 50 + "</body></html>"
    _install_fake_scrapling(monkeypatch, [_FakePage(rendered)], calls)

    async def _curl_shell(url, proxy, user_agent):
        shell = '<html><head><script src="a.js"></script></head><body><div id="root"></div></body></html>'
        return shell.encode(), {"content-type": "text/html"}, 200, "curl_cffi"

    async def _unexpected_httpx(*a, **kw):
        raise AssertionError("httpx tier must not run when the browser tier succeeds")

    monkeypatch.setattr(web_module, "_fetch_curl_cffi", _curl_shell)
    monkeypatch.setattr(web_module, "_fetch_httpx", _unexpected_httpx)

    content, headers, status, fetcher = await web_module._fetch_raw("https://spa.example/thread")

    assert fetcher == "scrapling" and status == 200
    assert b"Rendered comment text." in content
    assert headers["content-type"].startswith("text/html")
    fetch_call = next(c["fetch"] for c in calls if "fetch" in c)
    assert callable(fetch_call["page_action"])  # DOM capture runs for every site
    assert fetch_call["network_idle"] is False
    assert calls[0]["session"]["solve_cloudflare"] is False


@pytest.mark.asyncio
async def test_browser_tier_uses_real_headers_and_pubmed_action(monkeypatch):
    calls: list[dict[str, Any]] = []
    article = '<html><body><div id="main-content"><section class="abstract">Abstract.</section></div></body></html>'
    _install_fake_scrapling(monkeypatch, [_FakePage(article, headers={"Content-Type": "text/html; charset=utf-8"})], calls)

    async def _unexpected_curl(url, proxy, user_agent):
        raise AssertionError("curl_cffi tier must be skipped for PubMed")

    monkeypatch.setattr(web_module, "_fetch_curl_cffi", _unexpected_curl)

    content, headers, status, fetcher = await web_module._fetch_raw("https://pmc.ncbi.nlm.nih.gov/articles/PMC1/")

    assert fetcher == "scrapling"
    assert headers == {"content-type": "text/html; charset=utf-8"}
    fetch_call = next(c["fetch"] for c in calls if "fetch" in c)
    assert callable(fetch_call["page_action"])


@pytest.mark.asyncio
async def test_browser_tier_retries_pubmed_shell_then_falls_to_httpx(monkeypatch):
    calls: list[dict[str, Any]] = []
    shell = _FakePage("<html><title>only a title</title></html>")
    _install_fake_scrapling(monkeypatch, [shell, shell], calls)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/html"}, content=b"<html>fallback</html>", request=request)

    monkeypatch.setattr(web_module, "_pinned_dns_transport", lambda: httpx.MockTransport(handler))
    with patch("nanobot.security.network.socket.getaddrinfo", _fake_resolve_public):
        content, _headers, _status, fetcher = await web_module._fetch_raw("https://pubmed.ncbi.nlm.nih.gov/123/")

    assert fetcher == "httpx" and content == b"<html>fallback</html>"
    assert len([c for c in calls if "fetch" in c]) == 2


@pytest.mark.asyncio
async def test_browser_tier_enables_cloudflare_solver_and_rejects_unsolved_page(monkeypatch):
    calls: list[dict[str, Any]] = []
    cf_html = "<html><title>Just a moment...</title>" + "x" * 9000 + "</html>"
    _install_fake_scrapling(monkeypatch, [_FakePage(cf_html)], calls)

    async def _curl_cf(url, proxy, user_agent):
        return cf_html.encode(), {"content-type": "text/html"}, 403, "curl_cffi"

    async def _httpx(url, proxy, user_agent):
        return b"<html>plain</html>", {"content-type": "text/html"}, 200, "httpx"

    monkeypatch.setattr(web_module, "_fetch_curl_cffi", _curl_cf)
    monkeypatch.setattr(web_module, "_fetch_httpx", _httpx)

    _content, _headers, _status, fetcher = await web_module._fetch_raw("https://cf.example/")

    assert calls[0]["session"]["solve_cloudflare"] is True
    assert fetcher == "httpx"


@pytest.mark.asyncio
async def test_browser_tier_skipped_when_scrapling_missing(monkeypatch):
    monkeypatch.setattr(web_module, "SCRAPLING_AVAILABLE", False)

    async def _curl_none(url, proxy, user_agent):
        return None

    async def _httpx(url, proxy, user_agent):
        return b"<html>plain</html>", {"content-type": "text/html"}, 200, "httpx"

    monkeypatch.setattr(web_module, "_fetch_curl_cffi", _curl_none)
    monkeypatch.setattr(web_module, "_fetch_httpx", _httpx)
    _c, _h, _s, fetcher = await web_module._fetch_raw("https://www.reddit.com/r/x/comments/1/")
    assert fetcher == "httpx"


# ---------------------------------------------------------------------------
# execute() integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_unwraps_reddit_json_rendered_by_browser(monkeypatch):
    payload = [{"kind": "Listing", "data": {"children": [{"data": {
        "subreddit": "medicine", "author": "a", "score": 3, "title": "Sepsis bundle", "selftext": "Body text", "is_self": True,
    }}]}}, {"kind": "Listing", "data": {"children": []}}]
    wrapped = "<html><body><pre>" + json.dumps(payload).replace("<", "&lt;") + "</pre></body></html>"

    async def _fake(url, proxy=None, user_agent=None):
        return wrapped.encode(), {"content-type": "text/html; charset=utf-8"}, 200, "scrapling"

    monkeypatch.setattr(web_module, "_fetch_raw", _fake)
    result = await WebFetchTool().execute(url="https://www.reddit.com/r/medicine/comments/1/x/.json")
    data = json.loads(result)
    assert not is_tool_error_result(result)
    assert data["extractor"] == "reddit"
    assert "Title: Sepsis bundle" in data["text"]


@pytest.mark.asyncio
async def test_execute_pmc_shell_uses_jina_fallback(monkeypatch):
    async def _fake(url, proxy=None, user_agent=None):
        return b"<html><title>only</title></html>", {"content-type": "text/html"}, 200, "httpx"

    monkeypatch.setattr(web_module, "_fetch_raw", _fake)
    tool = WebFetchTool()
    jina = AsyncMock(return_value=json.dumps({"extractor": "jina", "text": "article"}))
    monkeypatch.setattr(tool, "_fetch_jina", jina)
    result = await tool.execute(url="https://pmc.ncbi.nlm.nih.gov/articles/PMC1/")
    jina.assert_awaited_once()
    assert json.loads(result)["extractor"] == "jina"


# ---------------------------------------------------------------------------
# SearXNG fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "method"),
    [("searxng", "_search_duckduckgo"), ("brave", "_search_brave"), ("tavily", "_search_tavily"), ("", "_search_duckduckgo")],
)
async def test_searxng_failure_falls_back_to_configured_provider(monkeypatch, provider, method):
    tool = WebSearchTool(config=WebSearchConfig(provider=provider, base_url="http://searx.local"))

    class FailingClient:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, *a, **kw):
            raise RuntimeError("searxng down")

    monkeypatch.setattr(web_module.httpx, "AsyncClient", FailingClient)
    fallback = AsyncMock(return_value="fallback results")
    monkeypatch.setattr(tool, method, fallback)

    assert await tool._search_searxng("sepsis", 3) == "fallback results"
    fallback.assert_awaited_once_with("sepsis", 3)


# ---------------------------------------------------------------------------
# Chromium provisioning
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_browser_is_provisioned_once_per_process_before_first_fetch(monkeypatch):
    monkeypatch.setattr(web_module, "_browser_provisioned", False)
    monkeypatch.setattr(web_module, "_browser_provision_lock", None)
    installs: list[list[str]] = []

    def _fake_install(argv: list[str]) -> tuple[int, str]:
        installs.append(argv)
        return 0, "Chromium already installed"

    monkeypatch.setattr(web_module, "_run_browser_install", _fake_install)
    calls: list[dict[str, Any]] = []
    rendered = "<html><body>" + "<p>Rendered.</p>" * 50 + "</body></html>"
    _install_fake_scrapling(monkeypatch, [_FakePage(rendered), _FakePage(rendered)], calls)

    async def _curl_none(url, proxy, user_agent):
        return None

    monkeypatch.setattr(web_module, "_fetch_curl_cffi", _curl_none)
    await web_module._fetch_raw("https://spa.example/a")
    await web_module._fetch_raw("https://spa.example/b")

    assert len(installs) == 1
    assert installs[0][1:] == ["-m", "patchright", "install", "chromium"]
    assert web_module._browser_provisioned is True


@pytest.mark.asyncio
async def test_browser_install_failure_is_logged_not_raised(monkeypatch):
    monkeypatch.setattr(web_module, "_browser_provisioned", False)
    monkeypatch.setattr(web_module, "_browser_provision_lock", None)

    def _failing_install(argv: list[str]) -> tuple[int, str]:
        raise FileNotFoundError("patchright")

    monkeypatch.setattr(web_module, "_run_browser_install", _failing_install)
    calls: list[dict[str, Any]] = []
    rendered = "<html><body>" + "<p>Rendered.</p>" * 50 + "</body></html>"
    _install_fake_scrapling(monkeypatch, [_FakePage(rendered)], calls)

    async def _curl_none(url, proxy, user_agent):
        return None

    monkeypatch.setattr(web_module, "_fetch_curl_cffi", _curl_none)
    _c, _h, _s, fetcher = await web_module._fetch_raw("https://spa.example/a")
    assert fetcher == "scrapling"
    assert web_module._browser_provisioned is True


@pytest.mark.asyncio
async def test_browser_tier_uses_rendered_dom_when_response_body_is_an_interstitial(monkeypatch):
    """NCBI serves an interstitial as the navigation response; the article only exists in the DOM."""
    calls: list[dict[str, Any]] = []
    interstitial = "<html><head><title>PMC</title></head><body>Checking your browser <script src='recaptcha.js'></script></body></html>"
    article_dom = (
        '<html><body><main id="main-content"><article><section class="abstract"><h2>Abstract</h2>'
        + "<p>Pneumonia is very common and continues to exact a high burden on health.</p>" * 40
        + "</section></article></main></body></html>"
    )
    _install_fake_scrapling(monkeypatch, [_FakePage(interstitial, dom=article_dom)], calls)

    async def _unexpected_httpx(*a, **kw):
        raise AssertionError("httpx tier must not run when the rendered DOM holds the article")

    monkeypatch.setattr(web_module, "_fetch_httpx", _unexpected_httpx)

    content, _headers, status, fetcher = await web_module._fetch_raw("https://pmc.ncbi.nlm.nih.gov/articles/PMC7241411/")

    assert (fetcher, status) == ("scrapling", 200)
    assert b"Pneumonia is very common" in content
    assert len([c for c in calls if "fetch" in c]) == 1
