"""FORK: the fork's per-turn "Tools used:" summary must reach WebUI/TUI
clients tagged with ``kind: "tools_summary"`` so they can keep it out of the
activity-to-answer grouping (see nanobot/webui/outbound_delivery.py,
`_send_message`). A message without the ``_tools_summary`` metadata
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
    outbound = channel._outbound
    original = outbound._persist_turn_transcript_event

    def _capture(*args: Any, **kwargs: Any) -> Any:
        captured.update(kwargs)
        return original(*args, **kwargs)

    outbound._persist_turn_transcript_event = _capture  # type: ignore[method-assign]

    await channel.send(OutboundMessage(
        channel="websocket",
        chat_id="chat-phase",
        content="**Tools used:**\n- search(`sepsis`)",
        metadata={"_tools_summary": True},
    ))

    assert captured.get("phase") == "answer"


async def test_tools_summary_kind_survives_history_replay(tmp_path) -> None:
    """FORK: the thread endpoint must replay the summary with its kind so the
    WebUI keeps it out of the activity-to-answer grouping after a reload
    (upstream removed the legacy ``~/.nanobot/webui/*.jsonl`` import, so only the
    live recorder append path remains)."""
    from nanobot.session.display_history import HistoryKind
    from nanobot.session.manager import SessionManager
    from nanobot.webui.history import fact_from_record
    from nanobot.webui.transcript import (
        WEBUI_TRANSCRIPT_SCHEMA_VERSION,
        build_webui_thread_response,
    )

    def records(chat_id: str) -> list[dict[str, Any]]:
        return [
            {"event": "user", "chat_id": chat_id, "text": "do it", "created_at_ms": 1000},
            {"event": "stream_end", "chat_id": chat_id, "text": "All done.", "created_at_ms": 1001},
            {
                "event": "message", "chat_id": chat_id, "created_at_ms": 1002,
                "text": "**Tools used:**\n- search(`sepsis`)", "kind": "tools_summary",
            },
            {"event": "turn_end", "chat_id": chat_id, "created_at_ms": 1003},
        ]

    assert fact_from_record(records("x")[2]).kind is HistoryKind.TOOLS_SUMMARY

    sessions = SessionManager(tmp_path / "workspace", sessions_root=tmp_path / "sessions")
    live = await sessions.state.get("websocket:live")
    await sessions.state.finish_turn(live)
    await sessions.state.append_history(
        live.key, [fact_from_record(r) for r in records("live")], expected_generation=live.generation,
    )

    for key in ("websocket:live",):
        body = await sessions.state.project_history(
            key, lambda history, key=key: build_webui_thread_response(key, history=history),
        )
        assert body is not None and body.get("schemaVersion") == WEBUI_TRANSCRIPT_SCHEMA_VERSION
        kinds = [(e.get("kind"), e.get("text") or "") for e in body["events"] if e.get("event") == "message"]
        assert any(k == "tools_summary" and "**Tools used:**" in t for k, t in kinds), (key, kinds)
        # The recap must not replace the turn's answer in session previews.
        answers = [e for e in body["events"] if e.get("event") == "stream_end"]
        assert answers and answers[0].get("text") == "All done."
    await sessions.state.aclose()
