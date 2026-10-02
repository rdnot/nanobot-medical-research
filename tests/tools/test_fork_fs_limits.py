"""Tests for the fork's write_file/edit_file size limits and read_file limits."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from nanobot.agent.tools.context import ToolContext
from nanobot.agent.tools.filesystem import (
    _DEFAULT_MAX_TOKENS,
    _READ_FILE_MAX_PDF_PAGES,
    EditFileTool,
    ReadFileTool,
    WriteFileTool,
    _max_content_chars_for_tokens,
)
from nanobot.config.schema import ToolsConfig


def test_tools_never_read_the_config_file(tmp_path):
    with patch("nanobot.config.loader.load_config", side_effect=AssertionError("must not load config")):
        write = WriteFileTool(workspace=tmp_path)
        edit = EditFileTool(workspace=tmp_path)
    assert write._max_content_chars == _max_content_chars_for_tokens(_DEFAULT_MAX_TOKENS)
    assert edit._max_new_text_chars == _max_content_chars_for_tokens(_DEFAULT_MAX_TOKENS) // 2


def test_create_from_context_uses_loop_output_budget(tmp_path):
    ctx = ToolContext(config=ToolsConfig(), workspace=str(tmp_path), max_output_tokens=2000)
    write = WriteFileTool.create(ctx)
    edit = EditFileTool.create(ctx)
    assert isinstance(write, WriteFileTool) and isinstance(edit, EditFileTool)
    assert write._max_content_chars == _max_content_chars_for_tokens(2000)
    assert edit._max_new_text_chars == _max_content_chars_for_tokens(2000) // 2

    default_ctx = ToolContext(config=ToolsConfig(), workspace=str(tmp_path))
    write_default = WriteFileTool.create(default_ctx)
    assert isinstance(write_default, WriteFileTool)
    assert write_default._max_content_chars == _max_content_chars_for_tokens(_DEFAULT_MAX_TOKENS)


def test_max_content_chars_floor():
    assert _max_content_chars_for_tokens(100) == 4_000
    assert _max_content_chars_for_tokens(8192) == 8192 * 3 - 1_500


@pytest.mark.asyncio
async def test_write_file_rejects_oversized_content(tmp_path):
    tool = WriteFileTool(workspace=tmp_path, max_tokens=2000)
    limit = tool._max_content_chars
    target = tmp_path / "out.md"

    result = await tool.execute(path=str(target), content="x" * (limit + 1))
    assert "content too large" in result
    assert f"limit {limit:,}" in result
    assert not target.exists()

    result = await tool.execute(path=str(target), content="y" * limit)
    assert "too large" not in result
    assert target.read_text(encoding="utf-8") == "y" * limit


@pytest.mark.asyncio
async def test_edit_file_limit_matches_description(tmp_path):
    tool = EditFileTool(workspace=tmp_path, max_tokens=2000)
    limit = tool._max_new_text_chars
    assert f"new_text must not exceed {limit:,} characters" in tool.description

    target = tmp_path / "a.txt"
    target.write_text("hello world", encoding="utf-8")
    result = await tool.execute(path=str(target), old_text="world", new_text="z" * (limit + 1))
    assert "new_text too large" in result
    assert f"limit {limit:,}" in result
    assert target.read_text(encoding="utf-8") == "hello world"

    result = await tool.execute(path=str(target), old_text="world", new_text="z" * limit)
    assert "too large" not in result
    assert target.read_text(encoding="utf-8") == "hello " + "z" * limit


def test_read_file_schema_matches_pdf_page_limit():
    assert ReadFileTool._MAX_PDF_PAGES == _READ_FILE_MAX_PDF_PAGES
    schema = ReadFileTool().parameters
    pages_desc = schema["properties"]["pages"]["description"]
    assert f"max {_READ_FILE_MAX_PDF_PAGES} pages" in pages_desc
    assert "minimum 200" not in schema["properties"]["limit"]["description"]
