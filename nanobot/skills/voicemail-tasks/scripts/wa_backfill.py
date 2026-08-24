#!/usr/bin/env python3
"""Drive the WhatsApp bridge to backfill voice notes into the voicemail store.

Asks the bridge to replay a chat's recent history, collects the voice notes the
bridge downloads and decrypts, and ingests them. Ingestion is idempotent on the
WhatsApp message id, so this is safe to run repeatedly and safe to run while the
live listener is also running.

Usage:
    python wa_backfill.py --sender 923001234567 --days 2
    python wa_backfill.py --sender 923001234567 --days 2 --collect 90
    python wa_backfill.py --health
    python wa_backfill.py --test
"""

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from voicemail_store import DEFAULT_DB, ingest  # noqa: E402

BRIDGE_URL = os.environ.get("WHATSAPP_BRIDGE_URL", "ws://localhost:3001")


def to_jid(sender: str) -> str:
    """Phone number -> WhatsApp JID. Passes through anything already a JID."""
    if "@" in sender:
        return sender
    digits = "".join(c for c in sender if c.isdigit())
    return f"{digits}@s.whatsapp.net"


def matches_sender(msg: dict, want_digits: str) -> bool:
    """Match a bridge message against a wanted number.

    WhatsApp now addresses some chats by an opaque LID rather than the phone
    number, and the bridge reports both, so check either. Compare on the last 9
    digits so country-code and local-prefix spellings of the same number match.
    """
    if not want_digits:
        return True
    tail = want_digits[-9:]
    for field in ("sender", "pn"):
        value = "".join(c for c in str(msg.get(field, "")).split("@")[0] if c.isdigit())
        if value and value.endswith(tail):
            return True
    return False


def within_window(ts: int, days: float) -> bool:
    if not ts:
        return False
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    return datetime.fromtimestamp(int(ts), tz=timezone.utc) >= cutoff


async def backfill(sender: str, days: float, collect_secs: float,
                   count: int, db_path: Path) -> int:
    import websockets

    jid = to_jid(sender)
    want = "".join(c for c in sender if c.isdigit())

    print(f"Bridge:  {BRIDGE_URL}")
    print(f"Chat:    {jid}")
    print(f"Window:  last {days:g} day(s)")
    print(f"Collect: {collect_secs:g}s\n")

    seen, ingested, skipped_old, skipped_other = 0, 0, 0, 0

    try:
        async with websockets.connect(BRIDGE_URL) as ws:
            await ws.send(json.dumps({"type": "backfill", "jid": jid, "count": count}))

            deadline = asyncio.get_event_loop().time() + collect_secs
            while True:
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    break
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                except asyncio.TimeoutError:
                    break

                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                kind = data.get("type")
                if kind == "backfill_requested":
                    print(f"History requested (req {data.get('requestId')}) — listening...")
                    continue
                if kind == "error":
                    print(f"Bridge error: {data.get('error')}")
                    continue
                if kind != "message":
                    continue

                # Voice notes only.
                if data.get("mediaType") != "voice" and not data.get("mediaPath"):
                    continue

                seen += 1
                if not matches_sender(data, want):
                    skipped_other += 1
                    continue
                if not within_window(data.get("timestamp", 0), days):
                    skipped_old += 1
                    continue

                result = ingest(
                    db_path,
                    msg_id=data.get("id", ""),
                    sender=data.get("pn") or data.get("sender", ""),
                    ts=data.get("timestamp", 0),
                    media=data.get("mediaPath", ""),
                    from_me=data.get("fromMe", False),
                )
                print(f"  {result}")
                if result.startswith("OK"):
                    ingested += 1

    except ConnectionRefusedError:
        print(f"Error: cannot reach the bridge at {BRIDGE_URL}.")
        print("Start it with:  cd bridge && npm start")
        return 1
    except Exception as e:
        print(f"Error: {e}")
        return 1

    print(f"\nVoice notes seen: {seen}")
    print(f"  ingested:       {ingested}")
    print(f"  wrong sender:   {skipped_other}")
    print(f"  outside window: {skipped_old}")
    if seen == 0:
        print("\nNo voice notes arrived. Either the phone did not replay history,")
        print("or the chat has none in range. Check the bridge console output.")
    return 0


async def health() -> int:
    """Ask the bridge whether media downloads are actually succeeding."""
    import websockets

    try:
        async with websockets.connect(BRIDGE_URL) as ws:
            await ws.send(json.dumps({"type": "health"}))
            deadline = asyncio.get_event_loop().time() + 15
            while asyncio.get_event_loop().time() < deadline:
                raw = await asyncio.wait_for(ws.recv(), timeout=5)
                data = json.loads(raw)
                if data.get("type") == "health":
                    attempts = data.get("attempts", 0)
                    successes = data.get("successes", 0)
                    print(json.dumps(data, indent=2))
                    if attempts >= 5 and successes == 0:
                        print("\nUNHEALTHY: media downloads are attempted but never succeed.")
                        print("The socket says connected, but the download path is broken.")
                        return 1
                    return 0
            print("No health reply within 15s.")
            return 1
    except ConnectionRefusedError:
        print(f"Error: cannot reach the bridge at {BRIDGE_URL}.")
        return 1


def self_test() -> int:
    """Offline test of the matching/window logic — no bridge needed."""
    failures = []

    def check(label, cond):
        print(f"  {'PASS' if cond else 'FAIL'}  {label}")
        if not cond:
            failures.append(label)

    print("wa_backfill self-test")

    check("plain number -> jid", to_jid("923175081727") == "923175081727@s.whatsapp.net")
    check("formatted number -> jid", to_jid("+92 317-5081727") == "923175081727@s.whatsapp.net")
    check("existing jid passes through", to_jid("123@lid") == "123@lid")

    check("matches on sender jid",
          matches_sender({"sender": "923175081727@s.whatsapp.net", "pn": ""}, "923175081727"))
    check("matches on pn when sender is a lid",
          matches_sender({"sender": "88112233@lid", "pn": "923175081727@s.whatsapp.net"},
                         "923175081727"))
    check("rejects a different number",
          not matches_sender({"sender": "923009999999@s.whatsapp.net", "pn": ""}, "923175081727"))
    check("empty want matches anything", matches_sender({"sender": "x@lid"}, ""))

    now = int(datetime.now(timezone.utc).timestamp())
    old = int((datetime.now(timezone.utc) - timedelta(days=5)).timestamp())
    check("recent ts inside 2-day window", within_window(now, 2))
    check("5-day-old ts outside 2-day window", not within_window(old, 2))
    check("zero ts is not in window", not within_window(0, 2))

    print(f"\n{'ALL PASSED' if not failures else str(len(failures)) + ' FAILED'}")
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill WhatsApp voice notes")
    parser.add_argument("--sender", default="", help="phone number or JID to backfill")
    parser.add_argument("--days", type=float, default=2)
    parser.add_argument("--collect", type=float, default=60,
                        help="seconds to listen for replayed history")
    parser.add_argument("--count", type=int, default=200,
                        help="how many historical messages to request")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--health", action="store_true")
    parser.add_argument("--test", action="store_true")
    args = parser.parse_args()

    if args.test:
        return self_test()
    if args.health:
        return asyncio.run(health())
    if not args.sender:
        parser.error("--sender is required (or use --health / --test)")
    return asyncio.run(backfill(args.sender, args.days, args.collect, args.count, args.db))


if __name__ == "__main__":
    sys.exit(main())
