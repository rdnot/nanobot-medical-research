"""Tests for the fork's Markdown -> WhatsApp formatting converter."""

from __future__ import annotations

import pytest

from nanobot.channels.whatsapp.runtime import _markdown_to_whatsapp


def test_empty_text():
    assert _markdown_to_whatsapp("") == ""


def test_headers_become_bold_without_changing_case():
    assert _markdown_to_whatsapp("# Dose: 500 mg q8h") == "*Dose: 500 mg q8h*"
    assert _markdown_to_whatsapp("### Ceftriaxone") == "*Ceftriaxone*"


def test_bold_italic_strike_and_inline_code():
    assert _markdown_to_whatsapp("**bold** and *italic* and ~~gone~~ and `code`") == (
        "*bold* and _italic_ and ~gone~ and `code`"
    )
    assert _markdown_to_whatsapp("***both***") == "*_both_*"
    assert _markdown_to_whatsapp("__under__") == "_under_"


def test_bullets_keep_indentation():
    text = "- first\n  - nested\n* star item\n   * deep"
    assert _markdown_to_whatsapp(text) == "• first\n  • nested\n• star item\n   • deep"


def test_italic_does_not_pair_across_lines():
    text = "5 * 3 = 15\nand 2 * 4 = 8"
    assert _markdown_to_whatsapp(text) == text


def test_code_blocks_are_preserved_verbatim():
    text = "before\n```python\n# not a header\n**not bold**\n```\nafter"
    out = _markdown_to_whatsapp(text)
    assert "# not a header\n**not bold**" in out
    assert out.count("```") == 2
    assert out.startswith("before\n```\n") and out.endswith("```\nafter")


def test_tables_flatten_to_pipe_rows():
    text = "| Drug | Dose |\n|------|------|\n| Amox | 1 g |"
    assert _markdown_to_whatsapp(text) == "Drug | Dose\nAmox | 1 g"


def test_escaped_characters_are_literal():
    assert _markdown_to_whatsapp(r"a \* b \_ c") == "a * b _ c"


@pytest.mark.parametrize("text", ["plain text", "no markdown here 100%"])
def test_plain_text_unchanged(text):
    assert _markdown_to_whatsapp(text) == text
