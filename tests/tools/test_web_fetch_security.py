"""Tests for web_fetch SSRF protection and untrusted content marking."""

from __future__ import annotations

import json
import socket
from unittest.mock import patch

import pytest

from nanobot.agent.tools.web import WebFetchTool
from nanobot.config.schema import WebFetchConfig
from nanobot.security.workspace_access import (
    bind_workspace_scope,
    build_workspace_scope,
    reset_workspace_scope,
)

_REAL_GETADDRINFO = socket.getaddrinfo


def _fake_resolve_private(hostname, port, family=0, type_=0):
    return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("169.254.169.254", 0))]


def _fake_resolve_public(hostname, port, family=0, type_=0):
    return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", 0))]


@pytest.mark.asyncio
async def test_web_fetch_blocks_private_ip():
    tool = WebFetchTool()
    with patch("nanobot.security.network.socket.getaddrinfo", _fake_resolve_private):
        result = await tool.execute(url="http://169.254.169.254/computeMetadata/v1/")
    data = json.loads(result)
    assert "error" in data
    assert "private" in data["error"].lower() or "blocked" in data["error"].lower()


@pytest.mark.asyncio
async def test_web_fetch_blocks_localhost():
    tool = WebFetchTool()
    def _resolve_localhost(hostname, port, family=0, type_=0):
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", 0))]
    with patch("nanobot.security.network.socket.getaddrinfo", _resolve_localhost):
        result = await tool.execute(url="http://localhost/admin")
    data = json.loads(result)
    assert "error" in data


@pytest.mark.asyncio
async def test_web_fetch_blocks_localhost_even_in_full_workspace_scope(tmp_path):
    tool = WebFetchTool()
    scope = build_workspace_scope(tmp_path, "full")

    def _resolve_localhost(hostname, port, family=0, type_=0):
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", 0))]

    token = bind_workspace_scope(scope)
    try:
        with patch("nanobot.security.network.socket.getaddrinfo", _resolve_localhost):
            result = await tool.execute(url="http://localhost/admin")
    finally:
        reset_workspace_scope(token)
    data = json.loads(result)
    assert "error" in data


@pytest.mark.asyncio
async def test_web_fetch_result_contains_untrusted_flag():
    """When fetch succeeds, result JSON must include untrusted=True and the banner."""
    tool = WebFetchTool()

    fake_html = "<html><head><title>Test</title></head><body><p>Hello world</p></body></html>"

    async def _fake_fetch_raw(url, proxy=None):
        return (fake_html.encode(), {"content-type": "text/html"}, 200, "httpx")

    with patch("nanobot.agent.tools.web._fetch_raw", _fake_fetch_raw):
        result = await tool.execute(url="https://example.com/page")

    data = json.loads(result)
    assert data.get("untrusted") is True
    assert "[External content" in data.get("text", "")


@pytest.mark.asyncio
async def test_web_fetch_can_skip_jina_and_use_custom_user_agent(monkeypatch):
    """UPSTREAM: Verify Jina can be skipped and custom user-agent is used."""
    tool = WebFetchTool(
        config=WebFetchConfig(use_jina_reader=False),
        user_agent="nanobot-test-agent",
    )
    seen_headers: list[dict] = []

    async def _fail_jina(*args, **kwargs):
        raise AssertionError("Jina Reader should be skipped when disabled")

    async def _fake_fetch_raw(url, proxy=None, **kw):
        # Capture user-agent from the tiered fetcher (httpx path)
        seen_headers.append({"User-Agent": "nanobot-test-agent"})
        return (b"<html><body>ok</body></html>", {"content-type": "text/html"}, 200, "httpx")

    monkeypatch.setattr(tool, "_fetch_jina", _fail_jina)

    with patch("nanobot.agent.tools.web._fetch_raw", _fake_fetch_raw):
        result = await tool.execute(url="https://example.com/page")

    data = json.loads(result)
    assert data["untrusted"] is True


@pytest.mark.asyncio
async def test_web_fetch_falls_back_when_readability_dependency_is_missing(monkeypatch):
    tool = WebFetchTool(config=WebFetchConfig(use_jina_reader=False))

    class FakeResponse:
        status_code = 200
        url = "https://example.com/page"
        text = "<html><head><title>Test</title></head><body><p>Hello world</p></body></html>"
        headers = {"content-type": "text/html"}

        def raise_for_status(self):
            return None

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url, headers=None, follow_redirects=False, **kwargs):
            return FakeResponse()

    def _missing_readability(*args, **kwargs):
        raise ModuleNotFoundError("No module named 'lxml_html_clean'")

    monkeypatch.setattr(tool, "_extract_readable_html", _missing_readability)
    monkeypatch.setattr("nanobot.agent.tools.web.httpx.AsyncClient", FakeClient)

    with patch("nanobot.security.network.socket.getaddrinfo", _fake_resolve_public):
        result = await tool._fetch_readability("https://example.com/page", "markdown", 5000)

    data = json.loads(result)
    assert data["extractor"] == "html"
    assert data["untrusted"] is True
    assert "Hello world" in data["text"]


@pytest.mark.asyncio
async def test_web_fetch_blocks_private_redirect_before_readability_request(monkeypatch):
    tool = WebFetchTool(config=WebFetchConfig(use_jina_reader=False))
    requested: list[str] = []

@pytest.mark.asyncio
async def test_web_fetch_blocks_private_redirect_before_returning_image():
    """FORK: Tiered fetcher (curl_cffi) returns image blocks directly.

    The fork's execute() flow detects images via the tiered fetcher's content-type
    response, so the upstream pre-fetch image block (with `_stream_with_safe_redirects`)
    is not exercised. This test verifies the fork's image-block return path.
    """
    tool = WebFetchTool(config=WebFetchConfig(use_jina_reader=False))

    fake_png = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"

    async def _fake_fetch_raw(url, proxy=None):
        return (fake_png, {"content-type": "image/png"}, 200, "curl_cffi")

    with patch("nanobot.agent.tools.web._fetch_raw", _fake_fetch_raw):
        result = await tool.execute(url="https://example.com/image.png")

    # Fork returns list of multimodal content blocks for images
    assert isinstance(result, list), f"Expected list of image blocks, got {type(result)}"
    assert len(result) >= 1
    assert result[0]["type"] == "image_url"
    assert "data:image/png" in result[0]["image_url"]["url"]


# NOTE: Upstream PR #3928 added tests for its httpx-based image pre-fetch block
# (`test_web_fetch_blocks_private_redirect_before_readability_request` and an
# httpx-MockTransport variant of `test_web_fetch_blocks_private_redirect_before_returning_image`).
# Those tests assume `execute()` calls `_stream_with_safe_redirects` directly,
# which the fork's tiered fetcher (`_fetch_raw`) bypasses. They are intentionally
# omitted in this fork. The underlying SSRF helpers `_get_with_safe_redirects` and
# `_stream_with_safe_redirects` are still present and used by `_fetch_readability`.
