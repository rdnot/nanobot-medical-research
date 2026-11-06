"""FORK: /c clears the session without memory consolidation."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.agent.loop import AgentLoop
from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.providers.base import GenerationSettings, LLMResponse


def _make_loop(tmp_path: Path) -> AgentLoop:
    provider = MagicMock(aclose=AsyncMock())
    provider.get_default_model.return_value = "test-model"
    provider.estimate_prompt_tokens.return_value = (10, "test")
    provider.generation = GenerationSettings(max_tokens=100)
    loop = AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path, model="test-model")
    loop.provider.chat_stream_with_retry = AsyncMock(
        return_value=LLMResponse(content="ok", tool_calls=[])
    )
    loop.tools.get_definitions = MagicMock(return_value=[])
    return loop


@pytest.mark.asyncio
async def test_clear_command_resets_live_session_without_archiving(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path)
    session = loop.sessions.get_or_create("cli:test")
    for i in range(3):
        session.add_message("user", f"msg{i}")
        session.add_message("assistant", f"resp{i}")
    loop.sessions.save(session)
    archive = AsyncMock()
    loop.consolidator.archive_session = archive  # type: ignore[method-assign]

    # A normal turn first, so the async session owner is running as in a gateway.
    await loop._process_message(
        InboundMessage(channel="cli", sender_id="user", chat_id="test", content="hi")
    )
    assert loop.sessions.state.is_bound

    response = await loop._process_message(
        InboundMessage(channel="cli", sender_id="user", chat_id="test", content="/c")
    )

    assert response is not None
    assert "session cleared" in response.content.lower()
    assert (await loop.sessions.state.get("cli:test")).messages == []
    archive.assert_not_called()
    await loop.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command,needle",
    [("/c", "session cleared"), ("/new", "new session started")],
)
async def test_session_command_reply_survives_own_worker(
    tmp_path: Path, command: str, needle: str
) -> None:
    """FORK: /c and /new dispatched on an idle session run on its worker.

    On chat channels (discord, whatsapp) an idle session has no pending queue,
    so the gateway enqueues the command and the session worker itself runs it.
    The command cancels the session's active tasks as its first action; before
    the fix that included the worker delivering the command's own confirmation,
    so the channel only showed the typing indicator and never the reply
    (CLI/WebUI dispatch outside the worker and were unaffected).
    """
    loop = _make_loop(tmp_path)
    loop.consolidator.archive_session = AsyncMock()  # type: ignore[method-assign]
    replies: list[OutboundMessage] = []
    loop.bus.outbound_recorder = replies.append  # type: ignore[attr-defined]

    # Bind the async session owner with one ordinary turn, as a gateway would.
    await loop._process_message(
        InboundMessage(channel="discord", sender_id="user", chat_id="dm", content="hi")
    )
    assert loop.sessions.state.is_bound

    # Gateway dispatch: no pending queue exists, so the command creates the
    # session worker and runs ON it.
    await loop._dispatch_inbound(
        InboundMessage(channel="discord", sender_id="user", chat_id="dm", content=command)
    )
    key = "discord:dm"
    workers = tuple(loop._active_tasks.get(key, ()))
    assert workers, "session worker was not created"
    results = await asyncio.gather(*workers, return_exceptions=True)
    assert not any(isinstance(r, BaseException) for r in results), results

    assert any(
        needle in (m.content or "").lower() for m in replies
    ), [m.content for m in replies]
    assert (await loop.sessions.state.get(key)).messages == []
    await loop.aclose()
