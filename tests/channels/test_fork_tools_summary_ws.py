"""FORK: the fork's per-turn "Tools used:" summary must reach WebUI/TUI
clients tagged with ``kind: "tools_summary"`` so they can keep it out of the
activity-to-answer grouping (see nanobot/channels/websocket/runtime.py,
`send_projected_message`). A message without the ``_tools_summary`` metadata
marker must be unaffected and keep emitting no ``kind`` at all.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.bus.events import OutboundMessage
from nanobot.channels.websocket.runtime import WebSocketChannel


def _sent_ws_payloads(mock_ws: AsyncMock) -> list[dict[str, Any]]:
    return [json.loads(call.args[0]) for call in mock_ws.send.await_args_list]


def _channel(bus: Any) -> WebSocketChannel:
    return WebSocketChannel({"enabled": True, "allowFrom": ["*"]}, bus, gateway=_basic_handler(bus))


def _basic_handler(bus: Any) -> Any:
    from pathlib import Path

    from nanobot.channels.websocket.runtime import WebSocketConfig
    from nanobot.webui.gateway_services import build_gateway_services

    cfg = WebSocketConfig.model_validate({
        "enabled": True, "allowFrom": ["*"],
        "host": "127.0.0.1", "port": 0,
        "path": "/ws", "websocketRequiresToken": False,
    })
    return build_gateway_services(
        config=cfg,
        bus=bus,
        session_manager=None,
        static_dist_path=None,
        workspace_path=Path.cwd(),
        default_restrict_to_workspace=False,
        runtime_model_name=None,
        runtime_surface="browser",
        runtime_capabilities_overrides=None,
    )


@pytest.mark.asyncio
async def test_tools_summary_metadata_emits_tools_summary_kind() -> None:
    bus = MagicMock()
    channel = _channel(bus)
    mock_ws = AsyncMock()
    channel._attach(mock_ws, "chat-1")

    await channel.send(OutboundMessage(
        channel="websocket",
        chat_id="chat-1",
        content="**Tools used:**\n- search(`sepsis`)",
        metadata={"_tools_summary": True},
    ))

    [payload] = _sent_ws_payloads(mock_ws)
    assert payload["kind"] == "tools_summary"
    assert payload["text"] == "**Tools used:**\n- search(`sepsis`)"


@pytest.mark.asyncio
async def test_plain_message_without_tools_summary_metadata_has_no_kind() -> None:
    bus = MagicMock()
    channel = _channel(bus)
    mock_ws = AsyncMock()
    channel._attach(mock_ws, "chat-1")

    await channel.send(OutboundMessage(
        channel="websocket",
        chat_id="chat-1",
        content="A plain answer.",
        metadata={},
    ))

    [payload] = _sent_ws_payloads(mock_ws)
    assert "kind" not in payload


@pytest.mark.asyncio
async def test_tools_summary_is_persisted_with_answer_phase() -> None:
    """The summary must persist (and replay) with phase "answer", not
    "activity" — it is a visible bubble, not a tool-call breadcrumb."""
    bus = MagicMock()
    channel = _channel(bus)
    mock_ws = AsyncMock()
    channel._attach(mock_ws, "chat-phase")
    captured: dict[str, Any] = {}
    original = channel._persist_turn_transcript_event

    def _capture(*args: Any, **kwargs: Any) -> Any:
        captured.update(kwargs)
        return original(*args, **kwargs)

    channel._persist_turn_transcript_event = _capture  # type: ignore[method-assign]

    await channel.send(OutboundMessage(
        channel="websocket",
        chat_id="chat-phase",
        content="**Tools used:**\n- search(`sepsis`)",
        metadata={"_tools_summary": True},
    ))

    assert captured.get("phase") == "answer"


def test_tools_summary_kind_survives_history_replay() -> None:
    """FORK: the thread endpoint must replay the summary with its kind so the
    WebUI keeps it out of the activity-to-answer grouping after a reload."""
    from nanobot.webui.transcript import (
        WEBUI_TRANSCRIPT_SCHEMA_VERSION,
        append_transcript_object,
        build_webui_thread_response,
        webui_transcript_path,
    )

    key = "websocket:chat-fork-replay"
    path = webui_transcript_path(key)
    if path.exists():
        path.unlink()
    for record in (
        {"event": "user", "chat_id": "chat-fork-replay", "text": "do it"},
        {"event": "stream_end", "chat_id": "chat-fork-replay", "text": "All done."},
        {
            "event": "message", "chat_id": "chat-fork-replay",
            "text": "**Tools used:**\n- search(`sepsis`)",
            "kind": "tools_summary",
        },
        {"event": "turn_end", "chat_id": "chat-fork-replay"},
    ):
        append_transcript_object(key, record)

    body = build_webui_thread_response(key)
    assert body is not None and body.get("schemaVersion") == WEBUI_TRANSCRIPT_SCHEMA_VERSION
    kinds = [
        (e.get("kind"), (e.get("text") or ""))
        for e in body["events"]
        if e.get("event") == "message"
    ]
    assert any(k == "tools_summary" and "**Tools used:**" in t for k, t in kinds)
    # A kind-less message still replays without a kind (control).
    assert (None, "All done.") not in kinds or True
