#!/usr/bin/env python3
"""
Diagnostic: read the sender mailbox via Microsoft Graph and list any
non-delivery reports (NDRs / bounce messages).

This script is READ-ONLY. It does not delete or modify anything. It exists
to verify that the app registration's newly granted Mail.ReadWrite (or
Mail.Read) permission works and that we can actually see bounce messages.

Usage
-----
Inside the running container (recommended -- creds + network live here):
    docker compose exec app python3 tools/check_ndr.py

Locally (needs M365 creds in .env or the admin DB):
    DB_PATH=./data/microsoft_apps_versions.db python3 tools/check_ndr.py

Options
-------
    --top N      How many recent inbox messages to scan (default 50)
    --all        Show every message scanned, not just detected NDRs
"""

import sys
import os
import re
import argparse
from pathlib import Path

# Make the project package importable when run from anywhere.
sys.path.insert(0, str(Path(__file__).parent.parent))

import requests

from notifications.providers import (
    get_email_provider,
    M365EmailProvider,
    NoopEmailProvider,
)

GRAPH_API_URL = os.environ.get("GRAPH_API_URL", "https://graph.microsoft.com/v1.0")

# PidTagMessageClass -- definitively identifies an NDR as REPORT.IPM.Note.NDR
PR_MESSAGE_CLASS = "String 0x001A"

# Fallback heuristics when the message class is unavailable.
NDR_FROM_HINTS = ("postmaster", "mailer-daemon", "microsoftexchange")
NDR_SUBJECT_HINTS = (
    "undeliverable",
    "delivery has failed",
    "delivery status notification",
    "mail delivery failed",
    "returned mail",
)

# Enhanced SMTP status code, e.g. 5.1.1 (bad mailbox) or 4.4.7 (expired).
STATUS_RE = re.compile(r"\b([45]\.\d{1,3}\.\d{1,3})\b")
EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
TAG_RE = re.compile(r"<[^>]+>")


def _message_class(msg: dict) -> str:
    """Extract PidTagMessageClass from a message's expanded extended properties."""
    for prop in msg.get("singleValueExtendedProperties", []) or []:
        if prop.get("id") == PR_MESSAGE_CLASS:
            return prop.get("value", "") or ""
    return ""


def is_ndr(msg: dict, message_class: str) -> bool:
    """Decide whether a message looks like a non-delivery report."""
    if message_class and message_class.upper().startswith("REPORT.IPM.NOTE.NDR"):
        return True
    frm = (msg.get("from") or {}).get("emailAddress", {}).get("address", "").lower()
    if any(h in frm for h in NDR_FROM_HINTS):
        return True
    subject = (msg.get("subject") or "").lower()
    if any(h in subject for h in NDR_SUBJECT_HINTS):
        return True
    return False


def _plain_body(msg: dict) -> str:
    body = (msg.get("body") or {}).get("content", "") or msg.get("bodyPreview", "") or ""
    if (msg.get("body") or {}).get("contentType", "").lower() == "html":
        body = TAG_RE.sub(" ", body)
    return body


def extract_details(msg: dict, sender_email: str) -> dict:
    """Pull the failed recipient and SMTP status code out of an NDR body."""
    body = _plain_body(msg)

    status = None
    m = STATUS_RE.search(body)
    if m:
        status = m.group(1)

    # The failed recipient is some address in the body that is neither the
    # sender mailbox nor a postmaster/system address.
    failed_recipient = None
    for addr in EMAIL_RE.findall(body):
        low = addr.lower()
        if low == sender_email.lower():
            continue
        if any(h in low for h in NDR_FROM_HINTS):
            continue
        failed_recipient = addr
        break

    return {"status": status, "failed_recipient": failed_recipient}


def main() -> int:
    parser = argparse.ArgumentParser(description="List NDR/bounce messages in the sender mailbox.")
    parser.add_argument("--top", type=int, default=50, help="How many recent messages to scan.")
    parser.add_argument("--all", action="store_true", help="Show every scanned message, not just NDRs.")
    args = parser.parse_args()

    print("Microsoft Graph NDR / bounce reader (read-only)")
    print("=" * 60)

    provider = get_email_provider()
    if isinstance(provider, NoopEmailProvider):
        print("ERROR: No email provider configured. Set M365 creds in .env or the admin DB.")
        return 1
    if not isinstance(provider, M365EmailProvider):
        print(f"ERROR: Configured provider is {provider.__class__.__name__}, not M365.")
        print("       NDR reading via Graph only applies to the M365 provider.")
        return 1

    sender = provider.sender_email
    print(f"Tenant:  {provider.tenant_id}")
    print(f"Mailbox: {sender}")
    print("-" * 60)

    # 1. Authenticate (re-uses the provider's client-credentials flow).
    try:
        token = provider._get_access_token()
    except Exception as e:
        print(f"ERROR: Failed to acquire access token: {e}")
        return 1
    print("Auth:    OK (access token acquired)")

    # 2. Read the inbox, requesting the message-class extended property so we
    #    can identify NDRs definitively.
    url = f"{GRAPH_API_URL}/users/{sender}/mailFolders/inbox/messages"
    params = {
        "$top": str(args.top),
        "$orderby": "receivedDateTime desc",
        "$select": "subject,from,receivedDateTime,bodyPreview,body",
        "$expand": f"singleValueExtendedProperties($filter=id eq '{PR_MESSAGE_CLASS}')",
    }
    headers = {
        "Authorization": f"Bearer {token}",
        # Ask for a plain-text body so our regexes work cleanly.
        "Prefer": 'outlook.body-content-type="text"',
    }

    try:
        resp = requests.get(url, headers=headers, params=params, timeout=30)
    except Exception as e:
        print(f"ERROR: Graph request failed: {e}")
        return 1

    if resp.status_code == 403:
        print("Read:    DENIED (HTTP 403)")
        print("         The token is valid but access to this mailbox is blocked.")
        print("         Check admin consent for Mail.ReadWrite and any Application")
        print("         Access Policy / RBAC scoping for this mailbox.")
        print(f"         Detail: {resp.text[:400]}")
        return 1
    if resp.status_code == 401:
        print("Read:    UNAUTHORIZED (HTTP 401) -- token rejected.")
        print(f"         Detail: {resp.text[:400]}")
        return 1
    if resp.status_code != 200:
        print(f"Read:    FAILED (HTTP {resp.status_code})")
        print(f"         Detail: {resp.text[:400]}")
        return 1

    messages = resp.json().get("value", [])
    print(f"Read:    OK ({len(messages)} message(s) scanned in Inbox)")
    print("=" * 60)

    ndrs = []
    for msg in messages:
        mclass = _message_class(msg)
        flagged = is_ndr(msg, mclass)
        if flagged:
            ndrs.append((msg, mclass))
        if args.all and not flagged:
            subj = msg.get("subject", "(no subject)")
            frm = (msg.get("from") or {}).get("emailAddress", {}).get("address", "?")
            print(f"[ ] {msg.get('receivedDateTime', '')}  {frm}  -- {subj}")

    if not ndrs:
        print("No NDR / bounce messages detected in the scanned window.")
        print("(Access works -- there simply are no bounces to show right now.)")
        return 0

    print(f"Detected {len(ndrs)} likely NDR / bounce message(s):\n")
    for msg, mclass in ndrs:
        subj = msg.get("subject", "(no subject)")
        frm = (msg.get("from") or {}).get("emailAddress", {}).get("address", "?")
        received = msg.get("receivedDateTime", "")
        details = extract_details(msg, sender)
        print(f"- Received:        {received}")
        print(f"  From:            {frm}")
        print(f"  Subject:         {subj}")
        if mclass:
            print(f"  Message class:   {mclass}")
        print(f"  Failed recipient:{details['failed_recipient'] or ' (could not parse)'}")
        print(f"  Status code:     {details['status'] or ' (could not parse)'}")
        print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
