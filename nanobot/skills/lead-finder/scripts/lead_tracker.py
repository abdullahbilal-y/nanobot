#!/usr/bin/env python3
"""Lead tracker — JSON-based storage for contacted leads.

Usage:
    python lead_tracker.py add --phone "+1234567890" --name "Joe's Pizza" --source "reddit" --message "intro"
    python lead_tracker.py check --phone "+1234567890"
    python lead_tracker.py list [--status contacted|responded|opted-out|converted]
    python lead_tracker.py update --phone "+1234567890" --status "responded"
    python lead_tracker.py stats
    python lead_tracker.py --test
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Default storage location
DEFAULT_DB = Path.home() / ".nanobot" / "leads.json"


def _load(db_path: Path) -> list[dict]:
    """Load leads from JSON file."""
    if not db_path.exists():
        return []
    try:
        return json.loads(db_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []


def _save(db_path: Path, leads: list[dict]) -> None:
    """Save leads to JSON file."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.write_text(json.dumps(leads, indent=2, ensure_ascii=False), encoding="utf-8")


def _normalize_phone(phone: str) -> str:
    """Strip non-digit chars except leading +."""
    if phone.startswith("+"):
        return "+" + "".join(c for c in phone[1:] if c.isdigit())
    return "".join(c for c in phone if c.isdigit())


def add_lead(db_path: Path, phone: str, name: str = "", source: str = "", message: str = "") -> str:
    """Add a new lead. Returns status message."""
    leads = _load(db_path)
    norm = _normalize_phone(phone)

    # Check duplicate
    for lead in leads:
        if _normalize_phone(lead["phone"]) == norm:
            return f"DUPLICATE: {phone} already in tracker (added {lead['date_added']})"

    leads.append({
        "phone": phone,
        "phone_normalized": norm,
        "name": name,
        "source": source,
        "message_type": message,
        "status": "contacted",
        "date_added": datetime.now(timezone.utc).isoformat(),
        "date_updated": datetime.now(timezone.utc).isoformat(),
        "notes": "",
    })
    _save(db_path, leads)
    return f"OK: Added {phone} ({name}) from {source}"


def check_lead(db_path: Path, phone: str) -> str:
    """Check if a phone number is already in the tracker."""
    leads = _load(db_path)
    norm = _normalize_phone(phone)

    for lead in leads:
        if _normalize_phone(lead["phone"]) == norm:
            return (
                f"FOUND: {lead['phone']} | {lead['name']} | "
                f"status={lead['status']} | source={lead['source']} | "
                f"added={lead['date_added']}"
            )
    return f"NOT_FOUND: {phone} is not in the tracker — safe to contact"


def update_lead(db_path: Path, phone: str, status: str = "", notes: str = "") -> str:
    """Update a lead's status or notes."""
    leads = _load(db_path)
    norm = _normalize_phone(phone)

    for lead in leads:
        if _normalize_phone(lead["phone"]) == norm:
            if status:
                lead["status"] = status
            if notes:
                lead["notes"] = notes
            lead["date_updated"] = datetime.now(timezone.utc).isoformat()
            _save(db_path, leads)
            return f"OK: Updated {phone} → status={lead['status']}"
    return f"NOT_FOUND: {phone}"


def list_leads(db_path: Path, status: str = "") -> str:
    """List leads, optionally filtered by status."""
    leads = _load(db_path)
    if status:
        leads = [l for l in leads if l.get("status") == status]

    if not leads:
        return "No leads found."

    lines = [f"Total: {len(leads)} leads\n"]
    for i, l in enumerate(leads, 1):
        lines.append(
            f"{i}. {l['phone']} | {l.get('name', '?')} | "
            f"status={l.get('status', '?')} | source={l.get('source', '?')} | "
            f"{l.get('date_added', '?')}"
        )
    return "\n".join(lines)


def stats(db_path: Path) -> str:
    """Show lead statistics."""
    leads = _load(db_path)
    if not leads:
        return "No leads yet."

    by_status: dict[str, int] = {}
    by_source: dict[str, int] = {}
    for l in leads:
        s = l.get("status", "unknown")
        by_status[s] = by_status.get(s, 0) + 1
        src = l.get("source", "unknown")
        by_source[src] = by_source.get(src, 0) + 1

    lines = [f"Total leads: {len(leads)}\n", "By status:"]
    for k, v in sorted(by_status.items()):
        lines.append(f"  {k}: {v}")
    lines.append("\nBy source:")
    for k, v in sorted(by_source.items()):
        lines.append(f"  {k}: {v}")
    return "\n".join(lines)


def run_tests() -> None:
    """Quick self-test using a temp file."""
    import tempfile

    tmp = Path(tempfile.mktemp(suffix=".json"))
    try:
        # Add
        r = add_lead(tmp, "+1234567890", "Test Biz", "reddit", "intro")
        assert r.startswith("OK"), f"add failed: {r}"

        # Duplicate
        r = add_lead(tmp, "+1234567890", "Test Biz", "reddit", "intro")
        assert r.startswith("DUPLICATE"), f"dup check failed: {r}"

        # Check found
        r = check_lead(tmp, "+1234567890")
        assert r.startswith("FOUND"), f"check found failed: {r}"

        # Check not found
        r = check_lead(tmp, "+9999999999")
        assert r.startswith("NOT_FOUND"), f"check not-found failed: {r}"

        # Update
        r = update_lead(tmp, "+1234567890", status="responded")
        assert r.startswith("OK"), f"update failed: {r}"

        # List
        r = list_leads(tmp)
        assert "1." in r, f"list failed: {r}"

        # Stats
        r = stats(tmp)
        assert "Total leads: 1" in r, f"stats failed: {r}"

        print("✅ All tests passed!")
    finally:
        if tmp.exists():
            tmp.unlink()


def main() -> None:
    parser = argparse.ArgumentParser(description="Lead tracker for nanobot lead-finder skill")
    parser.add_argument("--test", action="store_true", help="Run self-tests")
    parser.add_argument("--db", type=str, default=str(DEFAULT_DB), help="Path to leads JSON file")

    sub = parser.add_subparsers(dest="command")

    add_p = sub.add_parser("add", help="Add a lead")
    add_p.add_argument("--phone", required=True)
    add_p.add_argument("--name", default="")
    add_p.add_argument("--source", default="")
    add_p.add_argument("--message", default="intro")

    check_p = sub.add_parser("check", help="Check if lead exists")
    check_p.add_argument("--phone", required=True)

    update_p = sub.add_parser("update", help="Update lead status")
    update_p.add_argument("--phone", required=True)
    update_p.add_argument("--status", default="")
    update_p.add_argument("--notes", default="")

    sub.add_parser("list", help="List all leads")
    list_p = sub.add_parser("list", help="List leads")
    list_p.add_argument("--status", default="")

    sub.add_parser("stats", help="Show statistics")

    args = parser.parse_args()
    db = Path(args.db)

    if args.test:
        run_tests()
        return

    if args.command == "add":
        print(add_lead(db, args.phone, args.name, args.source, args.message))
    elif args.command == "check":
        print(check_lead(db, args.phone))
    elif args.command == "update":
        print(update_lead(db, args.phone, args.status, args.notes))
    elif args.command == "list":
        print(list_leads(db, getattr(args, "status", "")))
    elif args.command == "stats":
        print(stats(db))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
