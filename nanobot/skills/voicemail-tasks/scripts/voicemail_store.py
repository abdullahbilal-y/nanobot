#!/usr/bin/env python3
"""Voicemail task store — JSON-based storage for voice notes and the tasks in them.

Ingestion is idempotent on the WhatsApp message id, which is what makes replay
safe: the live listener, a reconnect and a backfill can all feed this store
concurrently and never double-record the same voice note.

Usage:
    python voicemail_store.py ingest --id "3EB0..." --sender "923001234567" --ts 1755990000 --media "/path/a.ogg"
    python voicemail_store.py transcribe [--limit 20]
    python voicemail_store.py extract [--limit 20]
    python voicemail_store.py summarize [--days 2] [--sender 923001234567]
    python voicemail_store.py list [--status open|done] [--days 2]
    python voicemail_store.py done --task 3
    python voicemail_store.py settings [--set key=value]
    python voicemail_store.py --test
"""

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

DEFAULT_DB = Path(os.environ.get("NANOBOT_VOICEMAIL_DB", Path.home() / ".nanobot" / "voicemail_tasks.json"))

GROQ_BASE = "https://api.groq.com/openai/v1"

# Model ids move faster than this file does, so they are settings, not constants.
DEFAULT_SETTINGS = {
    "transcribe_model": "whisper-large-v3",
    "extract_model": "llama-3.3-70b-versatile",
    "target_jid": "",       # where summaries get sent
    "watch_sender": "",     # whose voice notes become tasks
    "watch_name": "",
}


# ---------------------------------------------------------------- store

def _empty() -> dict:
    return {"messages": {}, "tasks": [], "settings": dict(DEFAULT_SETTINGS)}


def _load(db_path: Path) -> dict:
    """Load the store, tolerating a missing or corrupt file."""
    if not db_path.exists():
        return _empty()
    try:
        data = json.loads(db_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return _empty()

    # Merge in any settings added since the file was written, without
    # clobbering what the user already set.
    base = _empty()
    base["messages"].update(data.get("messages", {}))
    base["tasks"] = data.get("tasks", [])
    base["settings"].update(data.get("settings", {}))
    return base


def _save(db_path: Path, data: dict) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = db_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(db_path)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _digits(value: str) -> str:
    return "".join(c for c in str(value) if c.isdigit())


# ---------------------------------------------------------------- ingest

def ingest(db_path: Path, msg_id: str, sender: str, ts: int, media: str = "",
           transcript: str = "", from_me: bool = False) -> str:
    """Record a voice note. A repeat id is a no-op, so replay is always safe."""
    data = _load(db_path)

    if msg_id in data["messages"]:
        existing = data["messages"][msg_id]
        # A later pass may supply media/transcript the first pass could not get.
        changed = False
        if media and not existing.get("media_path"):
            existing["media_path"] = media
            changed = True
        if transcript and not existing.get("transcript"):
            existing["transcript"] = transcript
            existing["transcribed_at"] = _now()
            changed = True
        if changed:
            _save(db_path, data)
            return f"UPDATED: {msg_id} (filled in missing fields)"
        return f"DUPLICATE: {msg_id} already ingested"

    data["messages"][msg_id] = {
        "id": msg_id,
        "sender": _digits(sender) or sender,
        "sender_raw": sender,
        "timestamp": int(ts) if ts else 0,
        "media_path": media,
        "transcript": transcript,
        "transcribed_at": _now() if transcript else None,
        "from_me": from_me,
        "extracted": False,
        "ingested_at": _now(),
    }
    _save(db_path, data)
    return f"OK: ingested {msg_id}"


# ---------------------------------------------------------------- enrichment

def _groq_key() -> str:
    return os.environ.get("GROQ_API_KEY", "")


def transcribe_pending(db_path: Path, limit: int = 20) -> str:
    """Transcribe voice notes that have audio but no text yet."""
    import httpx

    key = _groq_key()
    if not key:
        return "Error: GROQ_API_KEY not set."

    data = _load(db_path)
    model = data["settings"]["transcribe_model"]
    pending = [
        m for m in data["messages"].values()
        if m.get("media_path") and not m.get("transcript")
    ][:limit]

    if not pending:
        return "Nothing to transcribe."

    done, failed = 0, 0
    for msg in pending:
        path = Path(msg["media_path"])
        if not path.exists():
            msg["transcript_error"] = "audio file missing"
            failed += 1
            continue
        try:
            with httpx.Client(timeout=120.0) as client:
                with open(path, "rb") as fh:
                    resp = client.post(
                        f"{GROQ_BASE}/audio/transcriptions",
                        headers={"Authorization": f"Bearer {key}"},
                        files={"file": (path.name, fh), "model": (None, model)},
                    )
            resp.raise_for_status()
            msg["transcript"] = resp.json().get("text", "").strip()
            msg["transcribed_at"] = _now()
            msg.pop("transcript_error", None)
            done += 1
        except Exception as e:
            # Best-effort: a failure is recorded and retried later, never fatal.
            msg["transcript_error"] = str(e)[:200]
            failed += 1

    _save(db_path, data)
    return f"Transcribed {done}, failed {failed}."


EXTRACT_PROMPT = """You extract ACTION ITEMS from a voice note transcript.

The speaker is dictating work for the listener to do. Return ONLY a JSON object:
{"tasks": [{"text": "...", "priority": "high|normal|low", "due": "<date or empty>"}]}

Rules:
- One entry per distinct actionable item. Keep the speaker's own wording where possible.
- Preserve names, numbers, amounts and dates EXACTLY as spoken. Never invent or normalise them.
- If the transcript contains no action item (small talk, a greeting), return {"tasks": []}.
- Do not merge two separate requests into one task, and do not split one request into several.
- Output raw JSON only, no markdown fence, no commentary."""


def extract_pending(db_path: Path, limit: int = 20) -> str:
    """Turn transcripts into tasks via Groq. Best-effort per message."""
    import httpx

    key = _groq_key()
    if not key:
        return "Error: GROQ_API_KEY not set."

    data = _load(db_path)
    model = data["settings"]["extract_model"]
    pending = [
        m for m in data["messages"].values()
        if m.get("transcript") and not m.get("extracted")
    ][:limit]

    if not pending:
        return "Nothing to extract."

    next_id = max((t["id"] for t in data["tasks"]), default=0) + 1
    added, failed = 0, 0

    for msg in pending:
        try:
            with httpx.Client(timeout=90.0) as client:
                resp = client.post(
                    f"{GROQ_BASE}/chat/completions",
                    headers={"Authorization": f"Bearer {key}"},
                    json={
                        "model": model,
                        "temperature": 0,
                        "response_format": {"type": "json_object"},
                        "messages": [
                            {"role": "system", "content": EXTRACT_PROMPT},
                            {"role": "user", "content": msg["transcript"]},
                        ],
                    },
                )
            resp.raise_for_status()
            payload = json.loads(resp.json()["choices"][0]["message"]["content"])

            for item in payload.get("tasks", []):
                text = (item.get("text") or "").strip()
                if not text:
                    continue
                data["tasks"].append({
                    "id": next_id,
                    "text": text,
                    "priority": item.get("priority", "normal"),
                    "due": item.get("due", ""),
                    "status": "open",
                    "source_msg_id": msg["id"],
                    "source_sender": msg.get("sender", ""),
                    "source_ts": msg.get("timestamp", 0),
                    "created_at": _now(),
                })
                next_id += 1
                added += 1

            msg["extracted"] = True
            msg.pop("extract_error", None)
        except Exception as e:
            msg["extract_error"] = str(e)[:200]
            failed += 1

    _save(db_path, data)
    return f"Extracted {added} task(s) from {len(pending) - failed} message(s), {failed} failed."


# ---------------------------------------------------------------- reporting

def _within(ts: int, days: float) -> bool:
    if not ts:
        return False
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    return datetime.fromtimestamp(ts, tz=timezone.utc) >= cutoff


def _fmt_ts(ts: int) -> str:
    if not ts:
        return "unknown time"
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone().strftime("%a %d %b %H:%M")


def list_tasks(db_path: Path, status: str = "", days: float = 0, sender: str = "") -> str:
    data = _load(db_path)
    tasks = data["tasks"]

    if status:
        tasks = [t for t in tasks if t.get("status") == status]
    if days:
        tasks = [t for t in tasks if _within(t.get("source_ts", 0), days)]
    if sender:
        want = _digits(sender)
        tasks = [t for t in tasks if _digits(t.get("source_sender", "")).endswith(want[-9:])]

    if not tasks:
        return "No tasks found."

    lines = []
    for t in sorted(tasks, key=lambda x: (x.get("source_ts", 0), x["id"])):
        mark = "x" if t.get("status") == "done" else " "
        due = f" (due {t['due']})" if t.get("due") else ""
        pri = "!" if t.get("priority") == "high" else ""
        lines.append(f"[{mark}] #{t['id']} {pri}{t['text']}{due}  — {_fmt_ts(t.get('source_ts', 0))}")
    return "\n".join(lines)


def summarize(db_path: Path, days: float = 2, sender: str = "") -> str:
    """Build the digest text. Plain formatting — this goes into WhatsApp."""
    data = _load(db_path)

    msgs = [m for m in data["messages"].values() if _within(m.get("timestamp", 0), days)]
    if sender:
        want = _digits(sender)
        msgs = [m for m in msgs if _digits(m.get("sender", "")).endswith(want[-9:])]

    msg_ids = {m["id"] for m in msgs}
    tasks = [t for t in data["tasks"] if t.get("source_msg_id") in msg_ids]

    name = data["settings"].get("watch_name") or sender or "sender"
    header = f"Voice notes from {name} — last {days:g} day(s)"

    if not msgs:
        return f"{header}\n\nNo voice notes found in this window."

    open_tasks = [t for t in tasks if t.get("status") == "open"]
    untranscribed = [m for m in msgs if not m.get("transcript")]

    lines = [header, f"{len(msgs)} voice note(s), {len(open_tasks)} open task(s)", ""]

    lines.append("TASKS")
    if open_tasks:
        for i, t in enumerate(sorted(open_tasks, key=lambda x: x.get("source_ts", 0)), 1):
            due = f" (due {t['due']})" if t.get("due") else ""
            pri = "[!] " if t.get("priority") == "high" else ""
            lines.append(f"{i}. {pri}{t['text']}{due}")
    else:
        lines.append("(none extracted)")

    lines += ["", "VOICE NOTES"]
    for m in sorted(msgs, key=lambda x: x.get("timestamp", 0)):
        stamp = _fmt_ts(m.get("timestamp", 0))
        if m.get("transcript"):
            text = m["transcript"]
            snippet = text if len(text) <= 300 else text[:300] + "..."
            lines.append(f"- {stamp}: {snippet}")
        else:
            why = m.get("transcript_error", "not transcribed")
            lines.append(f"- {stamp}: [audio not transcribed — {why}]")

    if untranscribed:
        lines += ["", f"NOTE: {len(untranscribed)} voice note(s) could not be transcribed; "
                      "the raw audio is kept and can be retried."]

    return "\n".join(lines)


def digest_for_message(db_path: Path, msg_id: str) -> str:
    """Digest for a single voice note — what the live watcher sends per note."""
    data = _load(db_path)
    msg = data["messages"].get(msg_id)
    if not msg:
        return ""

    name = data["settings"].get("watch_name") or msg.get("sender", "unknown")
    stamp = _fmt_ts(msg.get("timestamp", 0))
    tasks = [t for t in data["tasks"] if t.get("source_msg_id") == msg_id]
    open_total = sum(1 for t in data["tasks"] if t.get("status") == "open")

    lines = [f"Voice note from {name} — {stamp}", ""]

    if msg.get("transcript"):
        lines += ["Transcript:", msg["transcript"], ""]
    else:
        why = msg.get("transcript_error", "not transcribed")
        lines += [f"[audio could not be transcribed — {why}]",
                  f"file: {msg.get('media_path', '?')}", ""]

    if tasks:
        lines.append("Tasks added:")
        for t in tasks:
            due = f" (due {t['due']})" if t.get("due") else ""
            pri = "[!] " if t.get("priority") == "high" else ""
            lines.append(f"  #{t['id']} {pri}{t['text']}{due}")
    else:
        lines.append("No action items found in this one.")

    lines += ["", f"Open tasks total: {open_total}"]
    return "\n".join(lines)


def mark_done(db_path: Path, task_id: int) -> str:
    data = _load(db_path)
    for t in data["tasks"]:
        if t["id"] == task_id:
            t["status"] = "done"
            t["completed_at"] = _now()
            _save(db_path, data)
            return f"OK: task #{task_id} marked done."
    return f"Error: no task #{task_id}."


def settings_cmd(db_path: Path, assignments: list[str]) -> str:
    data = _load(db_path)
    for pair in assignments or []:
        if "=" not in pair:
            return f"Error: expected key=value, got '{pair}'"
        key, value = pair.split("=", 1)
        if key not in DEFAULT_SETTINGS:
            return f"Error: unknown setting '{key}'. Known: {', '.join(DEFAULT_SETTINGS)}"
        data["settings"][key] = value
    if assignments:
        _save(db_path, data)
    return json.dumps(data["settings"], indent=2)


def stats(db_path: Path) -> str:
    data = _load(db_path)
    msgs = list(data["messages"].values())
    return json.dumps({
        "voice_notes": len(msgs),
        "transcribed": sum(1 for m in msgs if m.get("transcript")),
        "transcribe_failed": sum(1 for m in msgs if m.get("transcript_error")),
        "tasks_total": len(data["tasks"]),
        "tasks_open": sum(1 for t in data["tasks"] if t.get("status") == "open"),
        "db": str(db_path),
    }, indent=2)


# ---------------------------------------------------------------- self-test

def self_test() -> int:
    """Offline test of store mechanics — no network, no API key needed."""
    import tempfile

    tmp = Path(tempfile.mkdtemp()) / "vm.json"
    failures = []

    def check(label, cond):
        print(f"  {'PASS' if cond else 'FAIL'}  {label}")
        if not cond:
            failures.append(label)

    print("voicemail_store self-test")

    r1 = ingest(tmp, "MSG1", "+92 317 5081727", 1755990000, media="/tmp/a.ogg")
    check("first ingest accepted", r1.startswith("OK"))

    r2 = ingest(tmp, "MSG1", "923175081727", 1755990000, media="/tmp/a.ogg")
    check("duplicate id is a no-op", r2.startswith("DUPLICATE"))

    r3 = ingest(tmp, "MSG1", "923175081727", 1755990000, transcript="call the supplier")
    check("later pass fills missing transcript", r3.startswith("UPDATED"))

    data = _load(tmp)
    check("exactly one message stored", len(data["messages"]) == 1)
    check("phone normalised to digits", data["messages"]["MSG1"]["sender"] == "923175081727")

    # Task reporting without touching the network.
    data["tasks"].append({
        "id": 1, "text": "call the supplier", "priority": "high", "due": "",
        "status": "open", "source_msg_id": "MSG1", "source_sender": "923175081727",
        "source_ts": int(datetime.now(timezone.utc).timestamp()), "created_at": _now(),
    })
    _save(tmp, data)

    check("open task listed", "#1" in list_tasks(tmp, status="open"))
    check("done filter excludes open task", list_tasks(tmp, status="done") == "No tasks found.")
    check("mark done works", mark_done(tmp, 1).startswith("OK"))
    check("missing task id reports error", mark_done(tmp, 99).startswith("Error"))

    recent = ingest(tmp, "MSG2", "923175081727", int(datetime.now(timezone.utc).timestamp()),
                    transcript="send the invoice tomorrow")
    check("second message ingested", recent.startswith("OK"))
    summary = summarize(tmp, days=2)
    check("summary includes recent transcript", "send the invoice tomorrow" in summary)
    check("old message outside window excluded", "call the supplier" not in summary)

    s = settings_cmd(tmp, ["watch_name=Ahmed Jasra"])
    check("setting persisted", "Ahmed Jasra" in s)
    check("unknown setting rejected", settings_cmd(tmp, ["bogus=1"]).startswith("Error"))

    print(f"\n{'ALL PASSED' if not failures else str(len(failures)) + ' FAILED'}")
    return 1 if failures else 0


# ---------------------------------------------------------------- cli

def main() -> int:
    parser = argparse.ArgumentParser(description="Voicemail task store")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--test", action="store_true", help="run offline self-test")
    sub = parser.add_subparsers(dest="cmd")

    p = sub.add_parser("ingest")
    p.add_argument("--id", required=True)
    p.add_argument("--sender", default="")
    p.add_argument("--ts", type=int, default=0)
    p.add_argument("--media", default="")
    p.add_argument("--transcript", default="")
    p.add_argument("--from-me", action="store_true")

    p = sub.add_parser("transcribe")
    p.add_argument("--limit", type=int, default=20)

    p = sub.add_parser("extract")
    p.add_argument("--limit", type=int, default=20)

    p = sub.add_parser("summarize")
    p.add_argument("--days", type=float, default=2)
    p.add_argument("--sender", default="")

    p = sub.add_parser("list")
    p.add_argument("--status", default="")
    p.add_argument("--days", type=float, default=0)
    p.add_argument("--sender", default="")

    p = sub.add_parser("done")
    p.add_argument("--task", type=int, required=True)

    p = sub.add_parser("settings")
    p.add_argument("--set", dest="assignments", action="append", default=[])

    sub.add_parser("stats")

    args = parser.parse_args()

    if args.test:
        return self_test()

    if args.cmd == "ingest":
        print(ingest(args.db, args.id, args.sender, args.ts, args.media,
                     args.transcript, args.from_me))
    elif args.cmd == "transcribe":
        print(transcribe_pending(args.db, args.limit))
    elif args.cmd == "extract":
        print(extract_pending(args.db, args.limit))
    elif args.cmd == "summarize":
        print(summarize(args.db, args.days, args.sender))
    elif args.cmd == "list":
        print(list_tasks(args.db, args.status, args.days, args.sender))
    elif args.cmd == "done":
        print(mark_done(args.db, args.task))
    elif args.cmd == "settings":
        print(settings_cmd(args.db, args.assignments))
    elif args.cmd == "stats":
        print(stats(args.db))
    else:
        parser.print_help()
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
