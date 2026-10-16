"""FORK: /c clears the session without memory consolidation."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.agent.loop import AgentLoop
from nanobot.bus.events import InboundMessage
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
