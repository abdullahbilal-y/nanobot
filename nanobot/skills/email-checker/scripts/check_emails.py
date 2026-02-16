#!/usr/bin/env python3
"""Check for new/unread emails via IMAP and output as JSON.

Usage:
    python check_emails.py [--no-mark-read] [--folder INBOX] [--limit 20]

Environment variables required:
    EMAIL_IMAP_HOST   - IMAP server (e.g. imap.gmail.com)
    EMAIL_ADDRESS     - Email address / login
    EMAIL_PASSWORD    - App password
"""

import argparse
import email
import email.header
import email.utils
import imaplib
import json
import os
import sys
from datetime import datetime


def decode_header_value(raw: str) -> str:
    """Decode a MIME-encoded email header into a plain string."""
    if not raw:
        return ""
    parts = email.header.decode_header(raw)
    decoded = []
    for data, charset in parts:
        if isinstance(data, bytes):
            decoded.append(data.decode(charset or "utf-8", errors="replace"))
        else:
            decoded.append(data)
    return " ".join(decoded)


def extract_text_body(msg: email.message.Message, max_chars: int = 500) -> str:
    """Extract plain-text body from an email, truncated to max_chars."""
    body = ""

    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            cd = str(part.get("Content-Disposition", ""))
            if ct == "text/plain" and "attachment" not in cd:
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    body = payload.decode(charset, errors="replace")
                    break
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            body = payload.decode(charset, errors="replace")

    # Clean up and truncate
    body = body.strip()
    if len(body) > max_chars:
        body = body[:max_chars] + "..."
    return body


def check_emails(
    host: str,
    address: str,
    password: str,
    folder: str = "INBOX",
    mark_read: bool = True,
    limit: int = 20,
) -> dict:
    """Connect to IMAP, fetch unseen emails, return as dict."""
    try:
        # Connect
        if host.endswith(":993") or "gmail" in host or "outlook" in host:
            imap = imaplib.IMAP4_SSL(host)
        else:
            imap = imaplib.IMAP4_SSL(host)

        imap.login(address, password)
    except imaplib.IMAP4.error as e:
        return {"status": "error", "error": f"Login failed: {e}"}
    except Exception as e:
        return {"status": "error", "error": f"Connection failed: {e}"}

    try:
        status, _ = imap.select(folder)
        if status != "OK":
            return {"status": "error", "error": f"Cannot open folder '{folder}'"}

        # Search for unseen emails
        status, data = imap.search(None, "UNSEEN")
        if status != "OK":
            return {"status": "error", "error": "Search failed"}

        msg_ids = data[0].split()
        if not msg_ids:
            return {"status": "ok", "count": 0, "emails": []}

        # Apply limit (take latest N)
        msg_ids = msg_ids[-limit:]

        emails = []
        for msg_id in msg_ids:
            # Use PEEK to avoid auto-marking as read
            fetch_cmd = "(BODY.PEEK[])" if not mark_read else "(RFC822)"
            status, msg_data = imap.fetch(msg_id, fetch_cmd)
            if status != "OK" or not msg_data or not msg_data[0]:
                continue

            raw = msg_data[0][1]
            if isinstance(raw, bytes):
                msg = email.message_from_bytes(raw)
            else:
                msg = email.message_from_string(raw)

            # Get UID for tracking
            uid_status, uid_data = imap.fetch(msg_id, "(UID)")
            uid = ""
            if uid_status == "OK" and uid_data and uid_data[0]:
                uid_str = uid_data[0].decode() if isinstance(uid_data[0], bytes) else str(uid_data[0])
                # Extract UID number from response like 'b"1 (UID 1234)"'
                import re
                uid_match = re.search(r"UID\s+(\d+)", uid_str)
                if uid_match:
                    uid = uid_match.group(1)

            sender = decode_header_value(msg.get("From", ""))
            subject = decode_header_value(msg.get("Subject", "(no subject)"))
            date_str = msg.get("Date", "")
            snippet = extract_text_body(msg)

            emails.append({
                "uid": uid,
                "from": sender,
                "subject": subject,
                "date": date_str,
                "snippet": snippet,
            })

            # If mark_read, explicitly set \Seen flag
            if mark_read:
                imap.store(msg_id, "+FLAGS", "\\Seen")

        return {"status": "ok", "count": len(emails), "emails": emails}

    except Exception as e:
        return {"status": "error", "error": f"Fetch error: {e}"}
    finally:
        try:
            imap.close()
            imap.logout()
        except Exception:
            pass


def main():
    parser = argparse.ArgumentParser(description="Check for new/unread emails via IMAP")
    parser.add_argument(
        "--no-mark-read",
        action="store_true",
        help="Don't mark fetched emails as read",
    )
    parser.add_argument(
        "--folder",
        default="INBOX",
        help="IMAP folder to check (default: INBOX)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Maximum number of emails to fetch (default: 20)",
    )
    args = parser.parse_args()

    # Read credentials from environment
    host = os.environ.get("EMAIL_IMAP_HOST", "")
    address = os.environ.get("EMAIL_ADDRESS", "")
    password = os.environ.get("EMAIL_PASSWORD", "")

    missing = []
    if not host:
        missing.append("EMAIL_IMAP_HOST")
    if not address:
        missing.append("EMAIL_ADDRESS")
    if not password:
        missing.append("EMAIL_PASSWORD")

    if missing:
        result = {
            "status": "error",
            "error": f"Missing environment variables: {', '.join(missing)}",
        }
        print(json.dumps(result, indent=2))
        sys.exit(1)

    result = check_emails(
        host=host,
        address=address,
        password=password,
        folder=args.folder,
        mark_read=not args.no_mark_read,
        limit=args.limit,
    )

    print(json.dumps(result, indent=2, ensure_ascii=False))

    if result["status"] == "error":
        sys.exit(1)


if __name__ == "__main__":
    main()
