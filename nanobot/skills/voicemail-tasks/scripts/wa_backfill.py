#!/usr/bin/env python3
"""Drive the WhatsApp bridge to backfill voice notes into the voicemail store.

Runs in two phases, because a history sync is far too large to fetch audio for
indiscriminately:

  1. Ask the bridge to replay a window of history and collect only metadata.
  2. Pick the voice notes that match the sender and time window, and ask the
     bridge to download just those.

Ingestion is idempotent on the WhatsApp message id, so this is safe to run
repeatedly and safe to run alongside the live watcher.

Usage:
    python wa_backfill.py --sender 447958778593 --days 2
    python wa_backfill.py --name "Ahmed" --days 2
    python wa_backfill.py --list-senders --days 7
    python wa_backfill.py --contacts "ahmed"
    python wa_backfill.py --health
    python wa_backfill.py --test
"""

import argparse
import asyncio
import json
import os
import sys
from collections import defaultdict
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


def digits_of(value: str) -> str:
    return "".join(c for c in str(value).split("@")[0] if c.isdigit())


def matches_sender(msg: dict, want_digits: str = "", want_name: str = "") -> bool:
    """Match a bridge message against a wanted number and/or display name.

    WhatsApp addresses most chats by an opaque LID that has no relation to the
    phone number, and the number is often absent from the message entirely. So
    match on the number when it is there, and fall back to the display name the
    bridge resolved, which is frequently the only usable identifier.
    """
    if not want_digits and not want_name:
        return True

    if want_digits:
        tail = want_digits[-9:]
        for field in ("sender", "pn"):
            value = digits_of(msg.get(field, ""))
            if value and value.endswith(tail):
                return True

    if want_name:
        needle = want_name.lower()
        for field in ("contactName", "pushName"):
            value = str(msg.get(field) or "").lower()
            if needle in value:
                return True

    return False


def _ids_of(value: str) -> list[str]:
    """Split a comma-separated identifier setting into clean entries."""
    return [p.strip() for p in str(value or "").split(",") if p.strip()]


def is_self_chat(msg: dict, self_ids: str) -> bool:
    """True when the message sits in the account's own 'message yourself' chat.

    Forwarding a voice note to yourself is the deliberate way to pull one in
    that would otherwise be skipped, so the self-chat is always in scope.
    Matched on the chat address, not on fromMe — a message you send *to someone
    else* is also fromMe and must not qualify.
    """
    if not self_ids:
        return False

    sender = str(msg.get("sender", ""))
    sender_digits = digits_of(sender)

    for ident in _ids_of(self_ids):
        if ident.endswith("@lid"):
            # LIDs carry a device suffix (123:19@lid); compare the user part.
            if sender.split(":")[0].split("@")[0] == ident.split(":")[0].split("@")[0]:
                return True
            continue
        want = digits_of(ident)
        if want and sender_digits and sender_digits.endswith(want[-9:]):
            return True
    return False


def should_process(msg: dict, want_digits: str = "", want_name: str = "",
                   self_ids: str = "", include_groups: bool = False) -> tuple[bool, str]:
    """Decide whether a voice note belongs in the task list.

    Returns (accept, reason) so callers can report why something was skipped
    instead of it vanishing silently.
    """
    # Anything forwarded into the self-chat is wanted, group rules aside.
    if is_self_chat(msg, self_ids):
        return True, "forwarded-to-self"

    if msg.get("isGroup") and not include_groups:
        return False, "group"

    if matches_sender(msg, want_digits, want_name):
        return True, "direct"

    return False, "other-sender"


def within_window(ts: int, days: float) -> bool:
    if not ts:
        return False
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    return datetime.fromtimestamp(int(ts), tz=timezone.utc) >= cutoff


def fmt_ts(ts: int) -> str:
    if not ts:
        return "?"
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


async def _collect(ws, seconds: float, on_message) -> None:
    """Drain bridge traffic for a while, passing each message to a callback."""
    deadline = asyncio.get_event_loop().time() + seconds
    while True:
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            return
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
        except asyncio.TimeoutError:
            return
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        on_message(data)


async def backfill(sender: str, name: str, days: float, collect_secs: float,
                   count: int, db_path: Path) -> int:
    import websockets

    want = "".join(c for c in sender if c.isdigit())
    jid = to_jid(sender) if sender else ""

    print(f"Bridge:  {BRIDGE_URL}")
    print(f"Chat:    {jid or '(any)'}")
    print(f"Name:    {name or '(any)'}")
    print(f"Window:  last {days:g} day(s)")
    print(f"Collect: {collect_secs:g}s\n")

    candidates: dict[str, dict] = {}
    stats = defaultdict(int)

    def on_message(data: dict) -> None:
        kind = data.get("type")
        if kind == "backfill_requested":
            print(f"History requested (req {data.get('requestId')}) — listening...")
            return
        if kind == "error":
            print(f"Bridge error: {data.get('error')}")
            return
        if kind != "message":
            return
        if data.get("mediaType") != "voice" and not data.get("mediaPath"):
            return

        stats["seen"] += 1
        if not matches_sender(data, want, name):
            stats["wrong_sender"] += 1
            return
        if not within_window(data.get("timestamp", 0), days):
            stats["outside_window"] += 1
            return
        msg_id = data.get("id", "")
        if msg_id:
            candidates[msg_id] = data

    try:
        async with websockets.connect(BRIDGE_URL, max_size=None) as ws:
            if jid:
                await ws.send(json.dumps({"type": "backfill", "jid": jid, "count": count}))
            await _collect(ws, collect_secs, on_message)

            print(f"\nVoice notes seen:  {stats['seen']}")
            print(f"  wrong sender:    {stats['wrong_sender']}")
            print(f"  outside window:  {stats['outside_window']}")
            print(f"  MATCHED:         {len(candidates)}")

            if not candidates:
                print("\nNothing matched. Try --list-senders to see who is actually in")
                print("the replayed history, or widen --days.")
                return 0

            # Phase 2: fetch audio only for what matched.
            need = [i for i, m in candidates.items() if not m.get("mediaPath")]
            paths: dict[str, str] = {
                i: m["mediaPath"] for i, m in candidates.items() if m.get("mediaPath")
            }

            if need:
                print(f"\nRequesting {len(need)} voice note download(s)...")
                await ws.send(json.dumps({"type": "download", "ids": need}))

                got = {}

                def on_dl(data: dict) -> None:
                    if data.get("type") == "downloaded":
                        got.update(data.get("results") or {})

                # Downloads are serialised bridge-side; allow generous time.
                await _collect(ws, max(60.0, len(need) * 6.0), on_dl)
                for k, v in got.items():
                    if v:
                        paths[k] = v

            ingested = failed = 0
            for msg_id, msg in candidates.items():
                path = paths.get(msg_id, "")
                if not path:
                    failed += 1
                result = ingest(
                    db_path,
                    msg_id=msg_id,
                    sender=msg.get("pn") or msg.get("sender", ""),
                    ts=msg.get("timestamp", 0),
                    media=path,
                    from_me=msg.get("fromMe", False),
                )
                if result.startswith("OK"):
                    ingested += 1

            print(f"\nIngested: {ingested}")
            print(f"Audio unavailable (expired/failed): {failed}")
            return 0

    except ConnectionRefusedError:
        print(f"Error: cannot reach the bridge at {BRIDGE_URL}.")
        print("Start it with:  cd bridge && npm start")
        return 1
    except Exception as e:
        print(f"Error: {e}")
        return 1


async def list_senders(days: float, collect_secs: float) -> int:
    """Show whose voice notes are actually present, so you can pick a filter."""
    import websockets

    groups: dict[str, dict] = defaultdict(
        lambda: {"n": 0, "latest": 0, "names": set(), "pn": set()}
    )

    def on_message(data: dict) -> None:
        if data.get("type") != "message":
            return
        if data.get("mediaType") != "voice" and not data.get("mediaPath"):
            return
        ts = int(data.get("timestamp") or 0)
        if days and not within_window(ts, days):
            return
        rec = groups[data.get("sender", "")]
        rec["n"] += 1
        rec["latest"] = max(rec["latest"], ts)
        for f in ("contactName", "pushName"):
            if data.get(f):
                rec["names"].add(str(data[f]))
        if data.get("pn"):
            rec["pn"].add(str(data["pn"]))

    try:
        async with websockets.connect(BRIDGE_URL, max_size=None) as ws:
            await _collect(ws, collect_secs, on_message)
    except ConnectionRefusedError:
        print(f"Error: cannot reach the bridge at {BRIDGE_URL}.")
        return 1

    if not groups:
        print("No voice notes observed. The bridge may still be syncing.")
        return 0

    print(f"{'sender':<40} {'n':>4}  {'latest':<17} names / number")
    for s, r in sorted(groups.items(), key=lambda kv: kv[1]["latest"], reverse=True):
        label = ", ".join(sorted(r["names"])) or ", ".join(sorted(r["pn"])) or "-"
        print(f"{s:<40} {r['n']:>4}  {fmt_ts(r['latest']):<17} {label}")
    return 0


async def contacts(query: str) -> int:
    import websockets

    try:
        async with websockets.connect(BRIDGE_URL, max_size=None) as ws:
            await ws.send(json.dumps({"type": "contacts", "query": query}))
            found = []

            def on_msg(data: dict) -> None:
                if data.get("type") == "contacts":
                    found.extend(data.get("contacts") or [])

            await _collect(ws, 20, on_msg)
    except ConnectionRefusedError:
        print(f"Error: cannot reach the bridge at {BRIDGE_URL}.")
        return 1

    if not found:
        print(f"No contacts matched {query!r}.")
        return 0
    for c in found[:40]:
        name = c.get("name") or c.get("notify") or "-"
        print(f"id={c.get('id',''):<36} phone={c.get('phone') or '-':<22} name={name}")
    return 0


async def resolve(phone: str) -> int:
    """Map a phone number to the LID its chat is actually addressed by."""
    import websockets

    try:
        async with websockets.connect(BRIDGE_URL, max_size=None) as ws:
            await ws.send(json.dumps({"type": "resolve", "phone": phone}))
            got = {}

            def on_msg(data: dict) -> None:
                if data.get("type") == "resolved":
                    got.update(data)

            await _collect(ws, 30, on_msg)
    except ConnectionRefusedError:
        print(f"Error: cannot reach the bridge at {BRIDGE_URL}.")
        return 1

    if not got:
        print("No reply from bridge.")
        return 1
    print(json.dumps(got, indent=2))
    if got.get("lid"):
        print("")
        print(f"Use this as --sender: {got['lid']}")
    elif got.get("exists"):
        print("")
        print("On WhatsApp, but no LID mapping is known yet.")
        print("The mapping appears once a message is exchanged with them.")
    else:
        print("")
        print("This number does not appear to be on WhatsApp.")
    return 0


async def health() -> int:
    """Ask the bridge whether media downloads are actually succeeding."""
    import websockets

    try:
        async with websockets.connect(BRIDGE_URL, max_size=None) as ws:
            await ws.send(json.dumps({"type": "health"}))
            got = {}

            def on_msg(data: dict) -> None:
                if data.get("type") == "health":
                    got.update(data)

            await _collect(ws, 20, on_msg)
    except ConnectionRefusedError:
        print(f"Error: cannot reach the bridge at {BRIDGE_URL}.")
        return 1

    if not got:
        print("No health reply.")
        return 1
    print(json.dumps(got, indent=2))
    if got.get("attempts", 0) >= 5 and got.get("successes", 0) == 0:
        print("\nUNHEALTHY: downloads attempted but never succeeding.")
        print("The socket says connected, but the download path is broken.")
        return 1
    return 0


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
    check("empty criteria matches anything", matches_sender({"sender": "x@lid"}))

    # Name matching is what rescues LID-only chats.
    check("matches on contactName",
          matches_sender({"sender": "999@lid", "contactName": "Ahmed Jasra"},
                         "", "ahmed"))
    check("matches on pushName",
          matches_sender({"sender": "999@lid", "pushName": "Ahmed J"}, "", "ahmed"))
    check("name match is case-insensitive",
          matches_sender({"sender": "999@lid", "contactName": "AHMED JASRA"}, "", "Ahmed"))
    check("rejects a different name",
          not matches_sender({"sender": "999@lid", "contactName": "Bilal"}, "", "ahmed"))
    check("number OR name is enough",
          matches_sender({"sender": "923175081727@s.whatsapp.net", "contactName": "Someone"},
                         "923175081727", "ahmed"))

    # Routing: groups out, direct messages in, self-forwards always in.
    AHMAD = "73439947845638@lid"
    SELF = "923175081727,225537473675300@lid"
    direct = {"sender": AHMAD, "isGroup": False}
    in_group = {"sender": "120363100352873276@g.us", "isGroup": True,
                "pushName": "Ahmad Jasra"}
    selfchat = {"sender": "923175081727@s.whatsapp.net", "isGroup": False, "fromMe": True}
    selflid = {"sender": "225537473675300:19@lid", "isGroup": False, "fromMe": True}
    other = {"sender": "923009999999@s.whatsapp.net", "isGroup": False}

    check("direct note from Ahmad accepted",
          should_process(direct, "73439947845638", "", SELF) == (True, "direct"))
    check("group note rejected even from Ahmad",
          should_process(in_group, "73439947845638", "Ahmad", SELF) == (False, "group"))
    check("self-chat forward accepted",
          should_process(selfchat, "73439947845638", "", SELF)[0] is True)
    check("self-chat by lid accepted (device suffix tolerated)",
          should_process(selflid, "73439947845638", "", SELF)[0] is True)
    check("self-forward reason is reported",
          should_process(selfchat, "", "", SELF)[1] == "forwarded-to-self")
    check("unrelated sender rejected",
          should_process(other, "73439947845638", "", SELF) == (False, "other-sender"))
    check("group accepted when explicitly enabled",
          should_process(in_group, "", "Ahmad", SELF, include_groups=True)[0] is True)
    check("no self_jid configured: self-chat not special",
          should_process(selfchat, "73439947845638", "", "")[0] is False)
    # A message you send TO someone else is also fromMe, and must not qualify.
    check("outgoing note to a third party is not a self-forward",
          is_self_chat({"sender": "447958778593@s.whatsapp.net", "fromMe": True}, SELF) is False)

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
    parser.add_argument("--name", default="", help="match on contact/display name instead")
    parser.add_argument("--days", type=float, default=2)
    parser.add_argument("--collect", type=float, default=60,
                        help="seconds to listen for replayed history")
    parser.add_argument("--count", type=int, default=200,
                        help="how many historical messages to request")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--list-senders", action="store_true",
                        help="show whose voice notes are present, then exit")
    parser.add_argument("--contacts", default=None, metavar="QUERY",
                        help="search the bridge address book, then exit")
    parser.add_argument("--resolve", default=None, metavar="PHONE",
                        help="map a phone number to its LID, then exit")
    parser.add_argument("--health", action="store_true")
    parser.add_argument("--test", action="store_true")
    args = parser.parse_args()

    if args.test:
        return self_test()
    if args.resolve is not None:
        return asyncio.run(resolve(args.resolve))
    if args.health:
        return asyncio.run(health())
    if args.contacts is not None:
        return asyncio.run(contacts(args.contacts))
    if args.list_senders:
        return asyncio.run(list_senders(args.days, args.collect))
    if not args.sender and not args.name:
        parser.error("need --sender or --name (or --list-senders / --contacts / --health)")
    return asyncio.run(
        backfill(args.sender, args.name, args.days, args.collect, args.count, args.db)
    )


if __name__ == "__main__":
    sys.exit(main())
