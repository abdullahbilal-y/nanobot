#!/usr/bin/env python3
"""Live watcher — every voice note from one person becomes a task digest.

Listens on the WhatsApp bridge. For each new voice note from the watched sender:
ingest (idempotent) -> transcribe -> extract tasks -> send a digest to your own
number.

Capture is never sacrificed to enrichment: the voice note is stored the moment it
arrives, and a Groq failure downgrades the digest rather than losing the note.

Usage:
    python wa_watch.py --sender 923001234567 --target 923175081727
    python wa_watch.py --sender 923001234567 --dry-run
    python wa_watch.py --test
"""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from voicemail_store import (  # noqa: E402
    DEFAULT_DB, _load, _save, digest_for_message, extract_pending, ingest,
    transcribe_pending,
)
from wa_backfill import BRIDGE_URL, matches_sender, to_jid  # noqa: E402


def build_digest(db_path: Path, msg_id: str) -> str:
    """Run enrichment for one message and return its digest text.

    Both enrichment steps are best-effort. If either fails, the digest still
    goes out, carrying whatever we do have.
    """
    try:
        transcribe_pending(db_path, limit=5)
    except Exception as e:
        print(f"  transcribe step failed: {e}")
    try:
        extract_pending(db_path, limit=5)
    except Exception as e:
        print(f"  extract step failed: {e}")
    return digest_for_message(db_path, msg_id)


async def watch(sender: str, target: str, db_path: Path, dry_run: bool,
                name: str = "") -> int:
    import websockets

    want = "".join(c for c in sender if c.isdigit())
    target_jid = to_jid(target) if target else ""

    print(f"Bridge:  {BRIDGE_URL}")
    print(f"Watching voice notes from: {sender or '(any number)'} / name {name or '(any)'}")
    print(f"Digest goes to: {target_jid or '(dry run — nothing sent)'}")
    print("Ctrl-C to stop.\n")

    while True:
        try:
            async with websockets.connect(BRIDGE_URL) as ws:
                print("Connected to bridge.")
                async for raw in ws:
                    try:
                        data = json.loads(raw)
                    except json.JSONDecodeError:
                        continue

                    if data.get("type") != "message":
                        continue
                    if not data.get("mediaPath"):
                        continue
                    if not matches_sender(data, want, name):
                        continue

                    msg_id = data.get("id", "")
                    result = ingest(
                        db_path,
                        msg_id=msg_id,
                        sender=data.get("pn") or data.get("sender", ""),
                        ts=data.get("timestamp", 0),
                        media=data.get("mediaPath", ""),
                        from_me=data.get("fromMe", False),
                    )
                    print(f"{result}")

                    # Already handled — a replay must not re-send a digest.
                    if not result.startswith("OK"):
                        continue

                    digest = build_digest(db_path, msg_id)
                    if not digest:
                        continue

                    print("-" * 50)
                    print(digest)
                    print("-" * 50)

                    if dry_run or not target_jid:
                        continue

                    try:
                        await ws.send(json.dumps({
                            "type": "send", "to": target_jid, "text": digest,
                        }))
                        print(f"Digest sent to {target_jid}\n")
                        _mark_notified(db_path, msg_id)
                    except Exception as e:
                        print(f"Failed to send digest: {e}\n")

        except asyncio.CancelledError:
            return 0
        except ConnectionRefusedError:
            print(f"Bridge unreachable at {BRIDGE_URL}; retrying in 5s...")
            await asyncio.sleep(5)
        except Exception as e:
            print(f"Watcher error: {e}; reconnecting in 5s...")
            await asyncio.sleep(5)


def _mark_notified(db_path: Path, msg_id: str) -> None:
    data = _load(db_path)
    if msg_id in data["messages"]:
        data["messages"][msg_id]["notified"] = True
        _save(db_path, data)


def self_test() -> int:
    """Offline test of the watcher's decision logic."""
    import tempfile
    from datetime import datetime, timezone

    failures = []

    def check(label, cond):
        print(f"  {'PASS' if cond else 'FAIL'}  {label}")
        if not cond:
            failures.append(label)

    print("wa_watch self-test")

    tmp = Path(tempfile.mkdtemp()) / "vm.json"
    now = int(datetime.now(timezone.utc).timestamp())

    r1 = ingest(tmp, "V1", "923001234567", now, media="/tmp/v1.ogg")
    check("new voice note ingests", r1.startswith("OK"))
    r2 = ingest(tmp, "V1", "923001234567", now, media="/tmp/v1.ogg")
    check("replay does NOT re-trigger a digest", not r2.startswith("OK"))

    # Digest with no transcript must still be sendable, not empty.
    d = digest_for_message(tmp, "V1")
    check("digest exists without transcript", bool(d))
    check("digest names the missing audio", "could not be transcribed" in d)
    check("digest reports open task total", "Open tasks total:" in d)

    data = _load(tmp)
    data["messages"]["V1"]["transcript"] = "please send the invoice today"
    data["tasks"].append({
        "id": 1, "text": "send the invoice today", "priority": "high", "due": "",
        "status": "open", "source_msg_id": "V1", "source_sender": "923001234567",
        "source_ts": now, "created_at": "x",
    })
    data["settings"]["watch_name"] = "Ahmed Jasra"
    _save(tmp, data)

    d = digest_for_message(tmp, "V1")
    check("digest carries transcript", "please send the invoice today" in d)
    check("digest lists extracted task", "#1" in d and "send the invoice today" in d)
    check("digest flags high priority", "[!]" in d)
    check("digest uses configured name", "Ahmed Jasra" in d)
    check("unknown message yields empty digest", digest_for_message(tmp, "NOPE") == "")

    _mark_notified(tmp, "V1")
    check("notified flag persists", _load(tmp)["messages"]["V1"].get("notified") is True)

    print(f"\n{'ALL PASSED' if not failures else str(len(failures)) + ' FAILED'}")
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Watch WhatsApp voice notes")
    parser.add_argument("--sender", default="", help="whose voice notes to watch")
    parser.add_argument("--name", default="", help="match on contact/display name instead")
    parser.add_argument("--target", default=os.environ.get("VOICEMAIL_TARGET", ""),
                        help="number to send digests to")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--dry-run", action="store_true",
                        help="print digests instead of sending them")
    parser.add_argument("--test", action="store_true")
    args = parser.parse_args()

    if args.test:
        return self_test()

    # Fall back to stored settings so the daemon can run without flags.
    data = _load(args.db)
    sender = args.sender or data["settings"].get("watch_sender", "")
    target = args.target or data["settings"].get("target_jid", "")
    name = args.name or data["settings"].get("watch_name", "")
    if not sender and not name:
        parser.error("need --sender or --name (or set watch_sender/watch_name in settings)")

    try:
        return asyncio.run(watch(sender, target, args.db, args.dry_run, name))
    except KeyboardInterrupt:
        print("\nStopped.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
