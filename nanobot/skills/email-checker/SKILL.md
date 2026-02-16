---
name: email-checker
description: Check for new/unread emails and send summaries to WhatsApp. Use when asked to monitor email, check inbox, forward emails to WhatsApp, get email notifications, or set up email alerts.
metadata: {"nanobot":{"emoji":"📧","requires":{"bins":["python"],"env":["EMAIL_IMAP_HOST","EMAIL_ADDRESS","EMAIL_PASSWORD","WHATSAPP_NOTIFY_PHONE"]}}}
---

# Email Checker

Monitor an email inbox for new messages and forward summaries to WhatsApp.

## Workflow

### 1. Check for New Emails

Run the checker script to fetch unread emails:

```bash
python nanobot/skills/email-checker/scripts/check_emails.py
```

The script connects via IMAP, fetches all `UNSEEN` emails, and outputs JSON:

```json
{
  "status": "ok",
  "count": 2,
  "emails": [
    {
      "uid": "1234",
      "from": "alice@example.com",
      "subject": "Meeting tomorrow",
      "date": "Mon, 16 Feb 2026 10:30:00 +0500",
      "snippet": "Hi, just wanted to confirm our meeting..."
    }
  ]
}
```

### 2. Forward to WhatsApp

For each email in the result, send a formatted summary via `send_whatsapp`:

**Message format:**

```
📧 New Email
From: {from}
Subject: {subject}
Date: {date}

{snippet}
```

Use the phone number from env var `WHATSAPP_NOTIFY_PHONE`.

### 3. Mark-Only Mode

By default, fetched emails are marked as read. To preview without marking:

```bash
python nanobot/skills/email-checker/scripts/check_emails.py --no-mark-read
```

### 4. Check Specific Folder

```bash
python nanobot/skills/email-checker/scripts/check_emails.py --folder "Promotions"
```

### 5. Limit Results

```bash
python nanobot/skills/email-checker/scripts/check_emails.py --limit 5
```

### 6. Schedule Recurring Checks

Use `cron` to automate periodic email monitoring:

```
cron(action="add", message="Check for new emails and send summaries to WhatsApp. Use the email-checker skill.", every_seconds=300)
```

## Environment Variables

| Variable | Example | Description |
|----------|---------|-------------|
| `EMAIL_IMAP_HOST` | `imap.gmail.com` | IMAP server hostname |
| `EMAIL_ADDRESS` | `you@gmail.com` | Email login address |
| `EMAIL_PASSWORD` | `abcd efgh ijkl mnop` | App password (NOT main password) |
| `WHATSAPP_NOTIFY_PHONE` | `+923001234567` | WhatsApp number for notifications |

## Rules

1. **No duplicates** — the script only fetches `UNSEEN` emails; once sent they won't repeat
2. **Rate limit** — wait 2-3 seconds between WhatsApp messages to avoid bans
3. **Privacy** — only send the snippet (first ~500 chars), not full email body
4. **Error handling** — if IMAP connection fails, report the error clearly to the user
5. **Batch limit** — default max 20 emails per check to avoid WhatsApp flooding
