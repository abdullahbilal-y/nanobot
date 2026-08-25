---
name: voicemail-tasks
description: Turn WhatsApp voice notes from a specific person into a running task list — downloads the voice notes, transcribes them with Groq Whisper, extracts action items, and sends a digest over WhatsApp. Use when asked to catch up on someone's voicemails, summarize voice notes, or track tasks someone keeps voice-messaging.
metadata: {"nanobot":{"emoji":"🎙️","requires":{"bins":["python","node"],"env":["GROQ_API_KEY"]}}}
---

# Voicemail Tasks

Turns a stream of WhatsApp voice notes into a deduplicated task list.

## Pipeline

```
WhatsApp voice note
      │
   Baileys bridge ── downloads + decrypts the audio itself (primitives, not a
      │              convenience wrapper) → ~/.nanobot/media/voice/<id>.ogg
      ▼
   ingest  ── keyed on the WhatsApp message id → ~/.nanobot/voicemail_tasks.json
      │        (duplicate id = no-op, so replay/backfill is always safe)
      ▼
   transcribe (Groq whisper-large-v3, ur)  ┐ best-effort:
   extract    (Groq gpt-oss-120b, JSON)    ┘ failures are recorded, never fatal
              → Roman Urdu transcript + English tasks
      ▼
   summarize → digest text → send_whatsapp
```

The audio and the raw transcript are always kept. If extraction fails, the voice
note still shows up in the digest as a transcript, so nothing is silently lost.

## Languages

Ahmad's voice notes are spoken in Urdu, so the pipeline splits the two jobs:

- **Whisper transcribes in Urdu** (`transcribe_language=ur`) and is never asked
  to translate. That raw Urdu transcript is the record and is kept verbatim.
- **The extraction step returns two things**: the same transcript transliterated
  into **Roman Urdu** (for reading), and the action items rewritten in
  **English** (for the task list).

Doing the romanisation downstream rather than in Whisper means it can be redone
— with a better prompt or model — without re-uploading any audio.

Display prefers `transcript_roman` and falls back to the raw transcript, so a
romanisation failure degrades to Urdu script rather than hiding the message.
Names, numbers and reference codes are preserved exactly in both fields.

## What gets picked up

| Where the voice note is | Processed? |
|---|---|
| Direct chat with the watched sender | yes |
| Any group, including one they are in | no — `include_groups=false` |
| Your own "message yourself" chat | yes, whoever originally sent it |

Group notes are skipped because they are rarely dictation aimed at you and
would swamp the list. Forwarding a note into your own chat is the deliberate
override: it is always picked up, which is how you pull in something the
filters would otherwise skip.

`self_jid` takes a comma-separated list and should hold both your number and
your LID, e.g. `923175081727,225537473675300@lid`. Matching is on the *chat
address*, not on `fromMe` — a note you send to someone else is also `fromMe`
and correctly does not qualify.

## Prerequisites

1. **Bridge running and linked.** `cd bridge && npm run build && npm start`, then
   scan the QR from WhatsApp → Settings → Linked Devices. Auth persists in
   `~/.nanobot/whatsapp-auth`.
2. **`GROQ_API_KEY` set** in the environment (or in `providers.groq.api_key`).

## Usage

### Backfill someone's recent voice notes

```bash
cd nanobot/skills/voicemail-tasks/scripts

# 1. pull recent history for a chat and ingest its voice notes
python wa_backfill.py --sender 923001234567 --days 2

# 2. transcribe + extract tasks (both best-effort, both resumable)
python voicemail_store.py transcribe
python voicemail_store.py extract

# 3. read the digest
python voicemail_store.py summarize --days 2 --sender 923001234567
```

### Live watch (digest per voice note)

Runs as a daemon. Every new voice note from the watched sender is transcribed,
turned into tasks, and sent as a digest to `target_jid`:

```bash
python wa_watch.py --sender 923001234567 --target 923175081727

# see what it would send, without sending anything
python wa_watch.py --sender 923001234567 --dry-run
```

A replayed or duplicate message never re-sends a digest — the send is gated on
the ingest being new.

### Day-to-day

```bash
python voicemail_store.py list --status open     # current task list
python voicemail_store.py done --task 3          # tick one off
python voicemail_store.py stats                  # counts + failure counts
```

### Send a digest by hand

Feed the `summarize` output to the `send_whatsapp` tool. **Confirm the
destination with the user before sending to a number they have not already
approved** — a sent WhatsApp message cannot be recalled.

### Settings

Model ids and targets live in the store, not in code, so they can change without
a code edit:

```bash
python voicemail_store.py settings --set watch_sender=923001234567 \
                                   --set watch_name="Ahmed Jasra" \
                                   --set target_jid=923175081727
python voicemail_store.py settings --set extract_model=openai/gpt-oss-120b
```

## Health

The bridge socket reporting "connected" is *not* proof the media path works — a
vendor-side change can break downloads while the session stays authenticated:

```bash
python wa_backfill.py --health
```

Many attempts with zero successes means the download path is broken, not the
connection. Restarting will not fix that; the media code needs re-checking
against the current Baileys/WhatsApp behaviour.

## Known limits

- **History is the phone's to give.** `--days 2` filters what WhatsApp actually
  replays. The linked phone must be online, and WhatsApp decides how far back it
  syncs — a window may come back partial or empty. The bridge console shows how
  many messages arrived.
- **CDN media expires.** Older voice notes can sync as messages whose audio no
  longer downloads. Those appear in the digest marked as not transcribed rather
  than being dropped.
- **Baileys is an unofficial client.** It rides WhatsApp's private protocol and
  can break with no warning on a vendor update; pinning the version does not
  help, because the change is server-side. Run the health check after any
  unexplained silence.

## Self-tests

Both scripts run offline, with no bridge and no API key:

```bash
python voicemail_store.py --test
python wa_backfill.py --test
python wa_watch.py --test
```
