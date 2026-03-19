"""WhatsApp channel implementation using Node.js bridge."""

import asyncio
import json
import mimetypes
import re
from collections import OrderedDict
from typing import Any

from loguru import logger

from pydantic import Field

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.config.schema import Base


# Safe placeholders using STX/ETX control characters (rarely appear in text)
_PH_BOLD_START = "\x02B\x02"
_PH_BOLD_END = "\x02/B\x02"
_PH_BOLD_ITALIC_START = "\x02BI\x02"
_PH_BOLD_ITALIC_END = "\x02/BI\x02"
_PH_CODE_BLOCK = "\x02CB"
_PH_INLINE_CODE = "\x02IC"


def _markdown_to_whatsapp(text: str) -> str:
    """
    Convert markdown to WhatsApp formatting.

    WhatsApp supports:
    - *bold* (single asterisk)
    - _italic_
    - ~strikethrough~
    - `monospace`

    Does NOT support headers (#, ##) or tables.
    """
    if not text:
        return ""

    # 0. Protect backslash-escaped characters before any formatting
    escaped_chars: list[str] = []
    def save_escaped(m: re.Match) -> str:
        escaped_chars.append(m.group(1))
        return f"\x02ESC{len(escaped_chars) - 1}\x02"
    text = re.sub(r'\\([*_~`#|\\])', save_escaped, text)

    # 1. Protect code blocks first
    code_blocks: list[str] = []
    def save_code_block(m: re.Match) -> str:
        code_blocks.append(m.group(1))
        return f"{_PH_CODE_BLOCK}{len(code_blocks) - 1}\x02"
    text = re.sub(r'```[\w]*\n?([\s\S]*?)```', save_code_block, text)

    # 2. Protect inline code
    inline_codes: list[str] = []
    def save_inline_code(m: re.Match) -> str:
        inline_codes.append(m.group(1))
        return f"{_PH_INLINE_CODE}{len(inline_codes) - 1}\x02"
    text = re.sub(r'`([^`]+)`', save_inline_code, text)

    # 3. Convert headers to bold (WhatsApp doesn't support # headers)
    def convert_header(m: re.Match) -> str:
        return f"*{m.group(1).strip().upper()}*"
    text = re.sub(r'^#{1,6}\s+(.+)$', convert_header, text, flags=re.MULTILINE)

    # 4. Handle ***bold italic*** -> *_bold italic_* (via placeholder to avoid later corruption)
    text = re.sub(r'\*\*\*(.+?)\*\*\*', f'{_PH_BOLD_ITALIC_START}\\1{_PH_BOLD_ITALIC_END}', text)

    # 5. Convert bold: **text** -> *text* (via placeholders)
    text = re.sub(r'\*\*(.+?)\*\*', f'{_PH_BOLD_START}\\1{_PH_BOLD_END}', text)

    # 6. Convert markdown italic: *text* -> _text_
    text = re.sub(r'\*([^*]+)\*', r'_\1_', text)

    # 7. Restore bold placeholders as WhatsApp bold
    text = text.replace(_PH_BOLD_START, '*').replace(_PH_BOLD_END, '*')
    text = text.replace(_PH_BOLD_ITALIC_START, '*_').replace(_PH_BOLD_ITALIC_END, '_*')

    # 8. Handle __text__ -> _text_
    text = re.sub(r'__(.+?)__', r'_\1_', text)

    # 9. Strikethrough ~~text~~ -> ~text~
    text = re.sub(r'~~(.+?)~~', r'~\1~', text)

    # 10. Convert markdown tables to simple text format
    lines = text.split('\n')
    result_lines: list[str] = []
    in_table = False
    for line in lines:
        if re.match(r'^\s*\|.+\|\s*$', line):
            if re.match(r'^\s*\|[\s\-:|]+\|\s*$', line):
                continue  # Skip separator
            cells = [c.strip() for c in line.strip().strip('|').split('|')]
            result_lines.append(' | '.join(cells))
            in_table = True
        else:
            if in_table:
                in_table = False
            result_lines.append(line)
    text = '\n'.join(result_lines)

    # 11. Convert bullet lists: - item or * item -> • item
    text = re.sub(r'^[-*]\s+', '• ', text, flags=re.MULTILINE)

    # 12. Restore inline code
    for i, code in enumerate(inline_codes):
        text = text.replace(f"{_PH_INLINE_CODE}{i}\x02", f"`{code}`")

    # 13. Restore code blocks
    for i, code in enumerate(code_blocks):
        text = text.replace(f"{_PH_CODE_BLOCK}{i}\x02", f"```\n{code}\n```")

    # 14. Restore escaped chars as plain literals
    for i, ch in enumerate(escaped_chars):
        text = text.replace(f"\x02ESC{i}\x02", ch)

    return text


class WhatsAppConfig(Base):
    """WhatsApp channel configuration."""

    enabled: bool = False
    bridge_url: str = "ws://localhost:3001"
    bridge_token: str = ""
    allow_from: list[str] = Field(default_factory=list)


class WhatsAppChannel(BaseChannel):
    """
    WhatsApp channel that connects to a Node.js bridge.

    The bridge uses @whiskeysockets/baileys to handle the WhatsApp Web protocol.
    Communication between Python and Node.js is via WebSocket.
    """

    name = "whatsapp"
    display_name = "WhatsApp"

    @classmethod
    def default_config(cls) -> dict[str, Any]:
        return WhatsAppConfig().model_dump(by_alias=True)

    def __init__(self, config: Any, bus: MessageBus):
        if isinstance(config, dict):
            config = WhatsAppConfig.model_validate(config)
        super().__init__(config, bus)
        self._ws = None
        self._connected = False
        self._processed_message_ids: OrderedDict[str, None] = OrderedDict()

    async def start(self) -> None:
        """Start the WhatsApp channel by connecting to the bridge."""
        import websockets

        bridge_url = self.config.bridge_url

        logger.info("Connecting to WhatsApp bridge at {}...", bridge_url)

        self._running = True

        while self._running:
            try:
                async with websockets.connect(bridge_url) as ws:
                    self._ws = ws
                    # Send auth token if configured
                    if self.config.bridge_token:
                        await ws.send(json.dumps({"type": "auth", "token": self.config.bridge_token}))
                    self._connected = True
                    logger.info("Connected to WhatsApp bridge")

                    # Listen for messages
                    async for message in ws:
                        try:
                            await self._handle_bridge_message(message)
                        except Exception as e:
                            logger.error("Error handling bridge message: {}", e)

            except asyncio.CancelledError:
                break
            except Exception as e:
                self._connected = False
                self._ws = None
                logger.warning("WhatsApp bridge connection error: {}", e)

                if self._running:
                    logger.info("Reconnecting in 5 seconds...")
                    await asyncio.sleep(5)

    async def stop(self) -> None:
        """Stop the WhatsApp channel."""
        self._running = False
        self._connected = False

        if self._ws:
            await self._ws.close()
            self._ws = None

    async def send(self, msg: OutboundMessage) -> None:
        """Send a message through WhatsApp."""
        if not self._ws or not self._connected:
            logger.warning("WhatsApp bridge not connected")
            return

        try:
            payload = {
                "type": "send",
                "to": msg.chat_id,
                "text": _markdown_to_whatsapp(msg.content) if msg.content else ""
            }
            await self._ws.send(json.dumps(payload, ensure_ascii=False))
        except Exception as e:
            logger.error("Error sending WhatsApp message: {}", e)

    async def _handle_bridge_message(self, raw: str) -> None:
        """Handle a message from the bridge."""
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Invalid JSON from bridge: {}", raw[:100])
            return

        msg_type = data.get("type")

        if msg_type == "message":
            # Incoming message from WhatsApp
            # Deprecated by whatsapp: old phone number style typically: <phone>@s.whatspp.net
            pn = data.get("pn", "")
            # New LID sytle typically:
            sender = data.get("sender", "")
            content = data.get("content", "")
            message_id = data.get("id", "")

            if message_id:
                if message_id in self._processed_message_ids:
                    return
                self._processed_message_ids[message_id] = None
                while len(self._processed_message_ids) > 1000:
                    self._processed_message_ids.popitem(last=False)

            # Extract just the phone number or lid as chat_id
            user_id = pn if pn else sender
            sender_id = user_id.split("@")[0] if "@" in user_id else user_id
            logger.info("Sender {}", sender)

            # Handle voice transcription if it's a voice message
            if content == "[Voice Message]":
                logger.info("Voice message received from {}, but direct download from bridge is not yet supported.", sender_id)
                content = "[Voice Message: Transcription not available for WhatsApp yet]"

            # Extract media paths (images/documents/videos downloaded by the bridge)
            media_paths = data.get("media") or []

            # Build content tags matching Telegram's pattern: [image: /path] or [file: /path]
            if media_paths:
                for p in media_paths:
                    mime, _ = mimetypes.guess_type(p)
                    media_type = "image" if mime and mime.startswith("image/") else "file"
                    media_tag = f"[{media_type}: {p}]"
                    content = f"{content}\n{media_tag}" if content else media_tag

            await self._handle_message(
                sender_id=sender_id,
                chat_id=sender,  # Use full LID for replies
                content=content,
                media=media_paths,
                metadata={
                    "message_id": message_id,
                    "timestamp": data.get("timestamp"),
                    "is_group": data.get("isGroup", False)
                }
            )

        elif msg_type == "status":
            # Connection status update
            status = data.get("status")
            logger.info("WhatsApp status: {}", status)

            if status == "connected":
                self._connected = True
            elif status == "disconnected":
                self._connected = False

        elif msg_type == "qr":
            # QR code for authentication
            logger.info("Scan QR code in the bridge terminal to connect WhatsApp")

        elif msg_type == "error":
            logger.error("WhatsApp bridge error: {}", data.get('error'))
