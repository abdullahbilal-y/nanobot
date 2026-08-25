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
    # Groq's catalogue shifts; check `GET /openai/v1/models` for what your key
    # actually has before changing this.
    "extract_model": "openai/gpt-oss-120b",
    "target_jid": "",       # where summaries get sent
    "watch_sender": "",     # whose voice notes become tasks
    "watch_name": "",
    # Spoken language of the voice notes. Telling Whisper up front is markedly
    # more accurate than letting it guess, especially on short or noisy clips.
    "transcribe_language": "ur",
    # Voice notes sent to a group are ignored: they are rarely dictation aimed
    # at you, and they would flood the task list.
    "include_groups": "false",
    # Your own address(es), comma separated. Anything in your "message
    # yourself" chat is picked up, which is how you pull in a note that would
    # otherwise be skipped - forward it to yourself.
    "self_jid": "",
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

NANOBOT_CONFIG = Path(os.environ.get("NANOBOT_CONFIG", Path.home() / ".nanobot" / "config.json"))


def _groq_key() -> str:
    """Env first, then nanobot's own config.

    The daemon usually runs without GROQ_API_KEY exported — the key lives in
    ~/.nanobot/config.json, which nanobot writes in camelCase. Accept either
    spelling so this works whichever way the config was produced.
    """
    key = os.environ.get("GROQ_API_KEY", "")
    if key:
        return key

    try:
        cfg = json.loads(NANOBOT_CONFIG.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""

    groq = (cfg.get("providers") or {}).get("groq") or {}
    if not isinstance(groq, dict):
        return ""
    return groq.get("apiKey") or groq.get("api_key") or ""


def transcribe_pending(db_path: Path, limit: int = 20) -> str:
    """Transcribe voice notes that have audio but no text yet."""
    import httpx

    key = _groq_key()
    if not key:
        return "Error: GROQ_API_KEY not set."

    data = _load(db_path)
    model = data["settings"]["transcribe_model"]
    language = (data["settings"].get("transcribe_language") or "").strip()
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
                    files = {"file": (path.name, fh), "model": (None, model)}
                    # Transcribe in the spoken language; do NOT ask Whisper to
                    # translate. The raw transcript is the record, and
                    # romanisation/translation happen downstream where they can
                    # be redone without re-uploading audio.
                    if language:
                        files["language"] = (None, language)
                    resp = client.post(
                        f"{GROQ_BASE}/audio/transcriptions",
                        headers={"Authorization": f"Bearer {key}"},
                        files=files,
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


EXTRACT_PROMPT = """You process a voice note transcript. The speaker talks in Urdu, \
often mixing in English words, and is dictating work for the listener to do.

Return ONLY a JSON object:
{
  "roman_urdu": "<the transcript in Roman Urdu>",
  "tasks": [{"text": "<action item in English>", "priority": "high|normal|low", "due": "<date or empty>"}]
}

roman_urdu rules:
- Transliterate what was said into Latin script. Do NOT translate it to English \
and do NOT summarise it — a reader who knows spoken Urdu should recognise the \
same sentences.
- Words the speaker actually said in English stay in English, spelled normally.
- If the transcript is already in Latin script, return it essentially unchanged.

tasks rules:
- Write each action item in clear, natural English — this is a task list an \
English reader will work from.
- One entry per distinct actionable item. Do not merge two separate requests \
into one, and do not split one request into several.
- Preserve names, numbers, amounts, reference codes and dates EXACTLY as \
spoken, in both fields. Never invent, translate or normalise them.
- If there is no action item (small talk, a greeting), return "tasks": [].

Output raw JSON only, no markdown fence, no commentary."""


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

            # Keep the raw transcript untouched and store the romanisation
            # alongside it, so a bad transliteration is always recoverable.
            roman = (payload.get("roman_urdu") or "").strip()
            if roman:
                msg["transcript_roman"] = roman

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

def _display_text(msg: dict) -> str:
    """What a human should read: Roman Urdu when we have it, else the raw text.

    The raw transcript stays on the record either way; this only chooses what
    to show, so a romanisation failure degrades to the original rather than
    hiding the message.
    """
    return (msg.get("transcript_roman") or msg.get("transcript") or "").strip()


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
        text = _display_text(m)
        if text:
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

    body = _display_text(msg)
    if body:
        lines += ["Transcript:", body, ""]
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


def reset(db_path: Path, wipe_messages: bool = False) -> str:
    """Clear the task list, keeping a timestamped backup first.

    By default the ingested voice notes stay and are marked un-extracted, so a
    later `extract` rebuilds the list with the current prompt — useful when the
    tasks were produced by an older, worse one. Settings are always preserved.
    """
    data = _load(db_path)
    n_tasks = len(data["tasks"])
    n_msgs = len(data["messages"])

    if db_path.exists():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        backup = db_path.with_name(f"{db_path.stem}.{stamp}.bak.json")
        backup.write_text(db_path.read_text(encoding="utf-8"), encoding="utf-8")
    else:
        backup = None

    data["tasks"] = []
    if wipe_messages:
        data["messages"] = {}
    else:
        for m in data["messages"].values():
            m["extracted"] = False

    _save(db_path, data)

    parts = [f"Cleared {n_tasks} task(s)"]
    parts.append(f"wiped {n_msgs} voice note(s)" if wipe_messages
                 else f"kept {n_msgs} voice note(s), marked for re-extraction")
    if backup:
        parts.append(f"backup: {backup.name}")
    return ". ".join(parts) + "."


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

    # Roman Urdu is what a human reads; the raw transcript stays on the record.
    urdu = "مجھے کل انوائس بھیج دیں"
    roman = "mujhe kal invoice bhej dein"
    ingest(tmp, "MSG3", "923175081727", int(datetime.now(timezone.utc).timestamp()),
           transcript=urdu)
    d3 = _load(tmp)
    d3["messages"]["MSG3"]["transcript_roman"] = roman
    _save(tmp, d3)

    check("display prefers roman urdu", _display_text(_load(tmp)["messages"]["MSG3"]) == roman)
    check("raw urdu transcript is preserved",
          _load(tmp)["messages"]["MSG3"]["transcript"] == urdu)
    check("digest shows roman, not urdu script",
          roman in digest_for_message(tmp, "MSG3")
          and urdu not in digest_for_message(tmp, "MSG3"))
    check("summary shows roman urdu", roman in summarize(tmp, days=2))

    # Falling back matters: a romanisation failure must not hide the message.
    d3 = _load(tmp)
    del d3["messages"]["MSG3"]["transcript_roman"]
    _save(tmp, d3)
    check("falls back to raw transcript when roman missing",
          _display_text(_load(tmp)["messages"]["MSG3"]) == urdu)
    check("digest still renders without roman", urdu in digest_for_message(tmp, "MSG3"))

    check("urdu is the default transcribe language",
          _load(tmp)["settings"]["transcribe_language"] == "ur")

    # Reset clears tasks, keeps notes and settings, and allows re-extraction.
    before = _load(tmp)
    n_before = len(before["messages"])
    settings_cmd(tmp, ["watch_name=Reset Check"])
    r = reset(tmp)
    after = _load(tmp)
    check("reset reports what it cleared", "Cleared" in r)
    check("reset empties the task list", after["tasks"] == [])
    check("reset keeps voice notes", len(after["messages"]) == n_before)
    check("reset marks notes for re-extraction",
          all(m.get("extracted") is False for m in after["messages"].values()))
    check("reset preserves settings", after["settings"]["watch_name"] == "Reset Check")
    check("reset wrote a backup",
          any(f.name.endswith(".bak.json") for f in tmp.parent.iterdir()))
    check("reset --all wipes notes too", "wiped" in reset(tmp, wipe_messages=True))
    check("notes gone after wipe", _load(tmp)["messages"] == {})

    s = settings_cmd(tmp, ["watch_name=Ahmed Jasra"])
    check("setting persisted", "Ahmed Jasra" in s)
    check("unknown setting rejected", settings_cmd(tmp, ["bogus=1"]).startswith("Error"))

    # Key resolution: env wins, then camelCase config, then snake_case.
    global NANOBOT_CONFIG
    saved_cfg, saved_env = NANOBOT_CONFIG, os.environ.pop("GROQ_API_KEY", None)
    try:
        cfg_path = tmp.parent / "config.json"
        NANOBOT_CONFIG = cfg_path

        cfg_path.write_text(json.dumps({"providers": {"groq": {"apiKey": "FROM_CAMEL"}}}))
        check("key read from camelCase config", _groq_key() == "FROM_CAMEL")

        cfg_path.write_text(json.dumps({"providers": {"groq": {"api_key": "FROM_SNAKE"}}}))
        check("key read from snake_case config", _groq_key() == "FROM_SNAKE")

        os.environ["GROQ_API_KEY"] = "FROM_ENV"
        check("env overrides config", _groq_key() == "FROM_ENV")
        del os.environ["GROQ_API_KEY"]

        cfg_path.write_text("{ not json")
        check("corrupt config yields empty key", _groq_key() == "")

        cfg_path.unlink()
        check("missing config yields empty key", _groq_key() == "")
    finally:
        NANOBOT_CONFIG = saved_cfg
        if saved_env is not None:
            os.environ["GROQ_API_KEY"] = saved_env

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

    p = sub.add_parser("reset")
    p.add_argument("--all", dest="wipe", action="store_true",
                   help="also remove ingested voice notes, not just tasks")

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
    elif args.cmd == "reset":
        print(reset(args.db, args.wipe))
    elif args.cmd == "stats":
        print(stats(args.db))
    else:
        parser.print_help()
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
