"""Tests for the fork's progress hook (force-final notice, tool-call log) and tools summary."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.agent.hook import AgentHookContext
from nanobot.agent.loop import (
    _FORCE_FINAL_PROMPT,
    AgentLoop,
    TurnContext,
    TurnKind,
    _ForkProgressHook,
)
from nanobot.bus.events import InboundMessage
from nanobot.providers.base import ToolCallRequest
from nanobot.session.history_visibility import HIDDEN_HISTORY_META, is_hidden_history_message


def _context(iteration: int, messages: list | None = None) -> AgentHookContext:
    return AgentHookContext(iteration=iteration, messages=messages if messages is not None else [])


@pytest.mark.asyncio
async def test_force_final_notice_is_injected_once_at_threshold():
    hook = _ForkProgressHook(force_final_threshold=3)
    messages: list[dict] = [{"role": "user", "content": "question"}]

    await hook.before_iteration(_context(0, messages))
    await hook.before_iteration(_context(2, messages))
    assert len(messages) == 1

    await hook.before_iteration(_context(3, messages))
    assert len(messages) == 2
    notice = messages[-1]
    assert notice["role"] == "user"
    assert notice["content"] == _FORCE_FINAL_PROMPT
    assert notice[HIDDEN_HISTORY_META] is True
    assert is_hidden_history_message(notice)

    # Later iterations never duplicate the notice.
    await hook.before_iteration(_context(4, messages))
    await hook.before_iteration(_context(10, messages))
    assert len(messages) == 2


@pytest.mark.asyncio
async def test_force_final_notice_skips_first_iteration_even_with_threshold_one():
    hook = _ForkProgressHook(force_final_threshold=1)
    messages: list[dict] = []
    await hook.before_iteration(_context(0, messages))
    assert messages == []
    await hook.before_iteration(_context(1, messages))
    assert len(messages) == 1


@pytest.mark.asyncio
async def test_before_execute_tools_logs_dict_arguments_only():
    hook = _ForkProgressHook(force_final_threshold=100)
    ctx = _context(1)
    ctx.tool_calls = [
        ToolCallRequest(id="1", name="web_search", arguments={"query": "sepsis"}),
        ToolCallRequest(id="2", name="", arguments={"x": 1}),
        ToolCallRequest(id="3", name="exec", arguments="not-a-dict"),
    ]
    await hook.before_execute_tools(ctx)
    assert hook.all_tool_calls_log == [
        {"name": "web_search", "arguments": {"query": "sepsis"}},
        {"name": "exec", "arguments": {}},
    ]


def test_build_tools_summary_uses_real_tool_argument_names():
    summary = AgentLoop._build_tools_summary([
        {"name": "web_search", "arguments": {"query": "pneumonia guidelines"}},
        {"name": "web_fetch", "arguments": {"url": "https://example.com/a"}},
        {"name": "read_file", "arguments": {"path": "/home/u/.nanobot/workspace/notes/a.md"}},
        {"name": "write_file", "arguments": {"path": "/w/report.md", "content": "# Title\nbody"}},
        {"name": "edit_file", "arguments": {"path": "/w/report.md", "old_text": "foo bar", "new_text": "baz"}},
        {"name": "message", "arguments": {"content": "hello\nworld"}},
        {"name": "exec", "arguments": {"command": "ls -la"}},
    ])
    lines = summary.splitlines()
    assert lines[0] == "**Tools used:**"
    # FORK: query/URL/path values are wrapped in inline code so the WebUI's
    # Markdown renderer never auto-links a bare URL into a text-less link.
    assert "- search(`pneumonia guidelines`)" in lines
    assert "- fetch(`https://example.com/a`)" in lines
    assert "- read_file(`~\\.nanobot\\...\\notes\\a.md`)" in lines
    assert "- write_file(`/w/report.md`: # Title body)" in lines
    assert "- edit_file(`/w/report.md`: foo bar)" in lines
    assert "- message(hello world)" in lines
    assert "- exec(ls -la)" in lines


def test_build_tools_summary_strips_backtick_from_coded_values():
    """FORK: a backtick inside a search query/URL/path must not break out of
    the inline-code span the summary wraps it in."""
    summary = AgentLoop._build_tools_summary([
        {"name": "web_search", "arguments": {"query": "sepsis `rescue` therapy"}},
        {"name": "web_fetch", "arguments": {"url": "https://example.com/a`b"}},
        {"name": "read_file", "arguments": {"path": "/w/weird`file.md"}},
    ])
    lines = summary.splitlines()
    assert "- search(`sepsis 'rescue' therapy`)" in lines
    assert "- fetch(`https://example.com/a'b`)" in lines
    assert "- read_file(`/w/weird'file.md`)" in lines
    assert "`" not in "\n".join(lines).replace("`sepsis 'rescue' therapy`", "").replace(
        "`https://example.com/a'b`", "",
    ).replace("`/w/weird'file.md`", "")


def test_build_tools_summary_empty():
    assert AgentLoop._build_tools_summary([]) == ""


def _turn_context(kind: TurnKind, log: list[dict]) -> TurnContext:
    msg = InboundMessage(channel="telegram", sender_id="u1", chat_id="c1", content="hi")
    delivery = MagicMock()
    delivery.background_response.return_value = None
    ctx = TurnContext(
        msg=msg, session_key="k", turn_id="t", runtime=None, kind=kind, delivery=delivery,
    )
    ctx.final_content = "answer"
    ctx.tool_calls_log = log
    return ctx


@pytest.mark.asyncio
async def test_prepare_outbound_publishes_summary_for_user_turns_only():
    loop = AgentLoop.__new__(AgentLoop)
    publish = AsyncMock()
    loop.bus = SimpleNamespace(publish_outbound=publish)  # type: ignore[attr-defined]
    loop._assemble_outbound = MagicMock(return_value=None)  # type: ignore[method-assign]
    log = [{"name": "web_search", "arguments": {"query": "q"}}]

    await loop._prepare_outbound(_turn_context(TurnKind.SYSTEM, log))
    publish.assert_not_awaited()

    await loop._prepare_outbound(_turn_context(TurnKind.USER, []))
    publish.assert_not_awaited()

    await loop._prepare_outbound(_turn_context(TurnKind.USER, log))
    publish.assert_awaited_once()
    outbound = publish.await_args.args[0]
    assert outbound.channel == "telegram" and outbound.chat_id == "c1"
    assert outbound.content.startswith("**Tools used:**")
    assert outbound.metadata["_tools_summary"] is True
