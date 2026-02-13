"""Send WhatsApp tool — proactive outreach to arbitrary phone numbers."""

import asyncio
import json
import os
import re
from typing import Any

from nanobot.agent.tools.base import Tool


class SendWhatsAppTool(Tool):
    """Send a WhatsApp message to any phone number via the Baileys bridge."""

    name = "send_whatsapp"
    description = (
        "Send a WhatsApp message to a phone number. "
        "Use for proactive outreach (e.g., lead generation, notifications). "
        "The phone number must include country code (e.g., +1234567890)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "phone": {
                "type": "string",
                "description": "Phone number with country code, e.g. '+1234567890' or '1234567890'"
            },
            "message": {
                "type": "string",
                "description": "The message text to send"
            }
        },
        "required": ["phone", "message"]
    }

    def __init__(self, bridge_url: str | None = None):
        self.bridge_url = bridge_url or os.environ.get(
            "WHATSAPP_BRIDGE_URL", "ws://localhost:3001"
        )

    @staticmethod
    def _normalize_phone(phone: str) -> str:
        """Normalize phone to digits-only (strip +, spaces, dashes)."""
        return re.sub(r"[^\d]", "", phone)

    @staticmethod
    def _phone_to_jid(phone: str) -> str:
        """Convert phone number to WhatsApp JID format."""
        digits = re.sub(r"[^\d]", "", phone)
        return f"{digits}@s.whatsapp.net"

    async def execute(self, phone: str, message: str, **kwargs: Any) -> str:
        if not phone or not message:
            return "Error: Both 'phone' and 'message' are required."

        digits = self._normalize_phone(phone)
        if len(digits) < 7 or len(digits) > 15:
            return f"Error: Invalid phone number '{phone}' — must be 7-15 digits with country code."

        jid = self._phone_to_jid(phone)

        try:
            import websockets
        except ImportError:
            return "Error: 'websockets' package required. Install: pip install websockets"

        try:
            async with websockets.connect(self.bridge_url) as ws:
                payload = json.dumps({
                    "type": "send",
                    "to": jid,
                    "text": message
                })
                await ws.send(payload)

                # Wait for confirmation (with timeout)
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=15.0)
                    response = json.loads(raw)

                    if response.get("type") == "sent":
                        return f"OK: Message sent to {phone} ({jid})"
                    elif response.get("type") == "error":
                        return f"Error from bridge: {response.get('error', 'unknown')}"
                    else:
                        # Bridge may send status/other messages first; treat as success
                        return f"OK: Message dispatched to {phone} ({jid})"
                except asyncio.TimeoutError:
                    return f"Warning: Message sent but no confirmation received (timeout). Check bridge logs."

        except ConnectionRefusedError:
            return (
                f"Error: Cannot connect to WhatsApp bridge at {self.bridge_url}. "
                "Make sure the bridge is running (cd bridge && npm start)."
            )
        except Exception as e:
            return f"Error sending WhatsApp message: {e}"
