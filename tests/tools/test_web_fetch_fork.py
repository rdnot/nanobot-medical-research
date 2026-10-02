"""Tests for the fork's tiered web_fetch helpers (curl_cffi tier, PDF, images, limits)."""

from __future__ import annotations

import io
import json
import socket
import sys
import types
from typing import Any
from unittest.mock import patch
from urllib.request import getproxies_environment

import httpx
import pytest

from nanobot.agent.tools import web as web_module
from nanobot.agent.tools.registry import is_tool_error_result
from nanobot.agent.tools.web import (
    RedirectBlockedError,
    WebFetchTool,
    _extract_meta,
    _extract_pdf_text,
    _image_mime_for,
    _smart_truncate,
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
# Pure helpers
# ---------------------------------------------------------------------------


def test_smart_truncate_prefers_paragraph_then_sentence_boundary():
    paragraph_text = "A" * 100 + "\n\n" + "B" * 20
    assert _smart_truncate(paragraph_text, 110) == "A" * 100 + "\n\n[...truncated...]"

    sentence_text = "Word word. " * 30
    out = _smart_truncate(sentence_text, 300)
    assert out.endswith(". [...truncated...]")
    assert len(out) <= 300 + len(" [...truncated...]")

    assert _smart_truncate("x" * 500, 100) == "x" * 100 + " [...truncated...]"
    assert _smart_truncate("short", 100) == "short"


def test_extract_meta_reads_author_date_description():
    html = (
        '<html><head><meta name="author" content="Jane &amp; Co">'
        '<meta property="article:published_time" content="2026-01-02">'
        '<meta property="og:description" content="A summary"></head></html>'
    )
    meta = _extract_meta(html)
    assert meta == {"author": "Jane & Co", "published": "2026-01-02", "description": "A summary"}


@pytest.mark.parametrize(
    ("ctype", "url", "expected"),
    [
        ("image/png", "https://x.example/a", "image/png"),
        ("image/jpeg; charset=binary", "https://x.example/a", "image/jpeg"),
        ("text/html; charset=utf-8", "https://x.example/chart.png", None),
        ("", "https://x.example/chart.png?x=1", "image/png"),
        ("application/octet-stream", "https://x.example/photo.jpg", "image/jpeg"),
        ("", "https://x.example/page", None),
    ],
)
def test_image_mime_for_prefers_content_type(ctype, url, expected):
    assert _image_mime_for(ctype, url) == expected


def test_extract_pdf_text_uses_pypdf_when_pymupdf_missing(monkeypatch):
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    buf = io.BytesIO()
    writer.write(buf)

    monkeypatch.setitem(sys.modules, "pymupdf", None)
    monkeypatch.setitem(sys.modules, "fitz", None)
    text = _extract_pdf_text(buf.getvalue())
    assert text.startswith("--- Page 1 ---")


def test_extract_pdf_text_uses_pymupdf_when_available():
    pymupdf = pytest.importorskip("pymupdf")
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Hello from PyMuPDF")
    data = doc.tobytes()
    doc.close()

    text = _extract_pdf_text(data)
    assert text.startswith("--- Page 1 ---")
    assert "Hello from PyMuPDF" in text


def test_extract_pdf_text_raises_when_no_extractor(monkeypatch):
    monkeypatch.setitem(sys.modules, "pymupdf", None)
    monkeypatch.setitem(sys.modules, "fitz", None)
    monkeypatch.setitem(sys.modules, "pypdf", None)
    with pytest.raises(RuntimeError, match="No PDF extractor"):
        _extract_pdf_text(b"%PDF-1.4")


def test_extract_pdf_text_raises_on_corrupt_input(monkeypatch):
    monkeypatch.setitem(sys.modules, "pymupdf", None)
    monkeypatch.setitem(sys.modules, "fitz", None)
    with pytest.raises(RuntimeError, match="PDF extraction failed"):
        _extract_pdf_text(b"not a pdf at all")


# ---------------------------------------------------------------------------
# curl_cffi tier (simulated module)
# ---------------------------------------------------------------------------


class _FakeCurlResponse:
    def __init__(self, status: int, headers: dict[str, str], content: bytes = b""):
        self.status_code = status
        self.headers = headers
        self.content = content


def _install_fake_curl_cffi(monkeypatch, routes: dict[str, _FakeCurlResponse]) -> list[dict[str, Any]]:
    """Install a fake ``curl_cffi.requests.AsyncSession`` and record each request."""
    calls: list[dict[str, Any]] = []

    class AsyncSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, **kwargs):
            calls.append({"url": url, **kwargs})
            if url not in routes:
                raise AssertionError(f"unexpected request to {url}")
            return routes[url]

    pkg = types.ModuleType("curl_cffi")
    requests_mod = types.ModuleType("curl_cffi.requests")
    requests_mod.AsyncSession = AsyncSession  # type: ignore[attr-defined]
    pkg.requests = requests_mod  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "curl_cffi", pkg)
    monkeypatch.setitem(sys.modules, "curl_cffi.requests", requests_mod)
    return calls


@pytest.mark.asyncio
async def test_curl_cffi_tier_follows_only_validated_redirects(monkeypatch):
    calls = _install_fake_curl_cffi(monkeypatch, {
        "https://a.example/start": _FakeCurlResponse(302, {"Location": "/final"}),
        "https://a.example/final": _FakeCurlResponse(200, {"Content-Type": "text/html"}, _HTML.encode()),
    })

    with patch("nanobot.security.network.socket.getaddrinfo", _fake_resolve_public):
        result = await web_module._fetch_curl_cffi(
            "https://a.example/start", None, web_module._DEFAULT_USER_AGENT,
        )

    assert result is not None
    content, headers, status, fetcher = result
    assert (status, fetcher) == (200, "curl_cffi")
    assert headers["content-type"] == "text/html"
    assert content == _HTML.encode()
    assert [c["url"] for c in calls] == ["https://a.example/start", "https://a.example/final"]
    assert all(c["allow_redirects"] is False for c in calls)
    # Default UA is left to Chrome impersonation.
    assert all(c["headers"] is None for c in calls)


@pytest.mark.asyncio
async def test_curl_cffi_tier_blocks_private_redirect_target(monkeypatch):
    calls = _install_fake_curl_cffi(monkeypatch, {
        "https://attacker.example/start": _FakeCurlResponse(
            302, {"location": "http://127.0.0.1:8765/metadata"},
        ),
    })

    def resolve(hostname, port, family=0, type_=0, proto=0, flags=0):
        if hostname == "attacker.example":
            return _fake_resolve_public(hostname, port)
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", 0))]

    with patch("nanobot.security.network.socket.getaddrinfo", resolve):
        with pytest.raises(RedirectBlockedError, match="Redirect blocked"):
            await web_module._fetch_curl_cffi(
                "https://attacker.example/start", None, web_module._DEFAULT_USER_AGENT,
            )
    assert [c["url"] for c in calls] == ["https://attacker.example/start"]


@pytest.mark.asyncio
async def test_execute_reports_blocked_redirect_from_curl_cffi_without_httpx_retry(monkeypatch):
    _install_fake_curl_cffi(monkeypatch, {
        "https://attacker.example/start": _FakeCurlResponse(
            302, {"location": "http://169.254.169.254/latest/meta-data/"},
        ),
    })

    async def _unexpected_httpx(*args, **kwargs):
        raise AssertionError("httpx tier must not retry a blocked redirect")

    monkeypatch.setattr(web_module, "_fetch_httpx", _unexpected_httpx)

    def resolve(hostname, port, family=0, type_=0, proto=0, flags=0):
        if hostname == "attacker.example":
            return _fake_resolve_public(hostname, port)
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("169.254.169.254", 0))]

    with patch("nanobot.security.network.socket.getaddrinfo", resolve):
        result = await WebFetchTool().execute(url="https://attacker.example/start")

    assert is_tool_error_result(result)
    assert "redirect blocked" in json.loads(result)["error"].lower()


@pytest.mark.asyncio
async def test_curl_cffi_tier_sends_custom_user_agent(monkeypatch):
    calls = _install_fake_curl_cffi(monkeypatch, {
        "https://a.example/": _FakeCurlResponse(200, {"content-type": "text/html"}, _HTML.encode()),
    })
    with patch("nanobot.security.network.socket.getaddrinfo", _fake_resolve_public):
        await web_module._fetch_curl_cffi("https://a.example/", None, "custom-agent/1.0")
    assert calls[0]["headers"] == {"User-Agent": "custom-agent/1.0"}


@pytest.mark.asyncio
async def test_fetch_raw_falls_back_to_httpx_when_curl_cffi_unavailable(monkeypatch):
    monkeypatch.setitem(sys.modules, "curl_cffi", None)
    monkeypatch.setitem(sys.modules, "curl_cffi.requests", None)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, headers={"content-type": "text/html"}, content=_HTML.encode(), request=request)

    monkeypatch.setattr(web_module, "_pinned_dns_transport", lambda: httpx.MockTransport(handler))

    with patch("nanobot.security.network.socket.getaddrinfo", _fake_resolve_public):
        content, headers, status, fetcher = await web_module._fetch_raw(
            "https://a.example/", None, "ua-test",
        )

    assert (status, fetcher) == (200, "httpx")
    assert content == _HTML.encode()
    assert seen[0].headers["User-Agent"] == "ua-test"


@pytest.mark.asyncio
async def test_fetch_raw_falls_back_to_httpx_when_curl_cffi_errors(monkeypatch):
    class AsyncSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, **kwargs):
            raise ConnectionError("tls handshake failed")

    requests_mod = types.ModuleType("curl_cffi.requests")
    requests_mod.AsyncSession = AsyncSession  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "curl_cffi", types.ModuleType("curl_cffi"))
    monkeypatch.setitem(sys.modules, "curl_cffi.requests", requests_mod)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/plain"}, content=b"plain", request=request)

    monkeypatch.setattr(web_module, "_pinned_dns_transport", lambda: httpx.MockTransport(handler))
    with patch("nanobot.security.network.socket.getaddrinfo", _fake_resolve_public):
        content, _headers, status, fetcher = await web_module._fetch_raw("https://a.example/")
    assert (content, status, fetcher) == (b"plain", 200, "httpx")


# ---------------------------------------------------------------------------
# execute(): status handling, images, PDFs
# ---------------------------------------------------------------------------


def _patch_fetch_raw(monkeypatch, content: bytes, ctype: str, status: int = 200, fetcher: str = "curl_cffi"):
    async def _fake(url, proxy=None, user_agent=None):
        return content, {"content-type": ctype}, status, fetcher

    monkeypatch.setattr(web_module, "_fetch_raw", _fake)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [403, 404, 500, 503])
async def test_execute_returns_tool_error_for_http_error_status(monkeypatch, status):
    _patch_fetch_raw(monkeypatch, b"<html><body>Not Found</body></html>", "text/html", status=status)
    result = await WebFetchTool().execute(url="https://example.com/missing")
    assert is_tool_error_result(result)
    data = json.loads(result)
    assert data["status"] == status
    assert str(status) in data["error"]
    assert data["fetcher"] == "curl_cffi"


@pytest.mark.asyncio
async def test_execute_returns_tool_error_when_pdf_extraction_unavailable(monkeypatch):
    _patch_fetch_raw(monkeypatch, b"%PDF-1.4 fake", "application/pdf")
    monkeypatch.setitem(sys.modules, "pymupdf", None)
    monkeypatch.setitem(sys.modules, "fitz", None)
    monkeypatch.setitem(sys.modules, "pypdf", None)
    result = await WebFetchTool().execute(url="https://example.com/guideline.pdf")
    assert is_tool_error_result(result)
    assert "No PDF extractor" in json.loads(result)["error"]


@pytest.mark.asyncio
async def test_execute_extracts_pdf_text(monkeypatch):
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    buf = io.BytesIO()
    writer.write(buf)
    monkeypatch.setitem(sys.modules, "pymupdf", None)
    monkeypatch.setitem(sys.modules, "fitz", None)
    _patch_fetch_raw(monkeypatch, buf.getvalue(), "application/pdf")

    result = await WebFetchTool().execute(url="https://example.com/guideline.pdf")

    assert not is_tool_error_result(result)
    data = json.loads(result)
    assert data["extractor"] == "pymupdf"
    assert "--- Page 1 ---" in data["text"]
    assert data["untrusted"] is True


@pytest.mark.asyncio
async def test_execute_returns_image_blocks_by_content_type(monkeypatch):
    png = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
    _patch_fetch_raw(monkeypatch, png, "image/png")
    result = await WebFetchTool().execute(url="https://example.com/chart")
    assert isinstance(result, list)
    assert result[0]["type"] == "image_url"
    assert result[0]["image_url"]["url"].startswith("data:image/png;base64,")


@pytest.mark.asyncio
async def test_execute_does_not_treat_html_at_image_url_as_image(monkeypatch):
    _patch_fetch_raw(monkeypatch, _HTML.encode(), "text/html; charset=utf-8")
    result = await WebFetchTool().execute(url="https://example.com/chart.png")
    assert isinstance(result, str)
    data = json.loads(result)
    assert not is_tool_error_result(result)
    assert "Hello world" in data["text"]


@pytest.mark.asyncio
async def test_execute_passes_configured_user_agent_to_fetcher(monkeypatch):
    seen: dict[str, Any] = {}

    async def _fake(url, proxy=None, user_agent=None):
        seen["proxy"], seen["user_agent"] = proxy, user_agent
        return _HTML.encode(), {"content-type": "text/html"}, 200, "httpx"

    monkeypatch.setattr(web_module, "_fetch_raw", _fake)
    tool = WebFetchTool(proxy="http://proxy.example:3128", user_agent="ua/2.0")
    await tool.execute(url="https://example.com/page")
    assert seen == {"proxy": "http://proxy.example:3128", "user_agent": "ua/2.0"}


@pytest.mark.asyncio
async def test_execute_truncates_at_boundary_and_flags_it(monkeypatch):
    body = "<html><body>" + "".join(f"<p>Paragraph {i} text.</p>" for i in range(400)) + "</body></html>"
    _patch_fetch_raw(monkeypatch, body.encode(), "text/html")
    result = await WebFetchTool(max_chars=1500).execute(url="https://example.com/long")
    data = json.loads(result)
    assert data["truncated"] is True
    assert data["text"].endswith("[...truncated...]")
    assert len(data["text"]) <= 1500 + len(" [...truncated...]")
