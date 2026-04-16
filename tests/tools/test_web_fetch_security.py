"""Tests for web_fetch SSRF protection and untrusted content marking."""

from __future__ import annotations

import json
import socket
from unittest.mock import patch

import pytest

from nanobot.agent.tools.web import WebFetchTool


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
async def test_web_fetch_blocks_private_redirect_before_returning_image():
    """Fork uses curl_cffi which handles redirects at the C level.

    Upstream httpx mocks don't apply — mock _fetch_raw directly.
    Verify image fetching returns multimodal content blocks.
    """
    tool = WebFetchTool()

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
