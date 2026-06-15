#!/usr/bin/env python3
"""
Daily mailbox maintenance for the Microsoft 365 sender mailbox.

Two jobs:
  1. Bounce processing -- scan the Inbox for non-delivery reports (NDRs),
     attribute each to a subscriber, and remove subscribers that have
     bounced `threshold` times (default 2). Processed NDRs are deleted so
     they are never counted twice and the inbox stays clean.
  2. Sent Items cleanup -- delete every message in Sent Items.

Both rely on the Mail.ReadWrite application permission on the app
registration. Scope the app to the sender mailbox with an Exchange
Application Access Policy / RBAC for Applications for least privilege.

A `dry_run` mode reports what *would* happen without deleting anything or
removing any subscribers -- useful for verifying detection before going live.
"""

import re
from typing import Dict, Any, List, Callable, Optional

from notifications.providers import (
    get_email_provider,
    M365EmailProvider,
    NoopEmailProvider,
)
from notifications.database import SubscriptionDatabase

# Enhanced SMTP status code, e.g. 5.1.1 (bad mailbox) or 4.4.7 (expired).
STATUS_RE = re.compile(r"\b([45]\.\d{1,3}\.\d{1,3})\b")
EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
TAG_RE = re.compile(r"<[^>]+>")

# PidTagMessageClass -- definitively identifies an NDR (REPORT.IPM.Note.NDR)
# when the extended property is requested. Detection still works without it.
PR_MESSAGE_CLASS = "String 0x001A"

# Addresses that are system/relay senders, never a failed recipient.
SYSTEM_ADDRESS_HINTS = ("postmaster", "microsoftexchange", "mailer-daemon")

# Subjects that mark a genuine Exchange non-delivery report.
NDR_SUBJECT_PREFIXES = ("undeliverable",)


def _message_class(msg: Dict[str, Any]) -> str:
    for prop in msg.get("singleValueExtendedProperties", []) or []:
        if prop.get("id") == PR_MESSAGE_CLASS:
            return prop.get("value") or ""
    return ""


def _headers(msg: Dict[str, Any]) -> Dict[str, str]:
    """Return the message's internet headers as a lower-cased name->value map.

    Requires `internetMessageHeaders` to have been requested via $select.
    """
    return {
        (h.get("name") or "").strip().lower(): (h.get("value") or "").strip()
        for h in (msg.get("internetMessageHeaders") or [])
    }


def is_auto_reply(msg: Dict[str, Any]) -> bool:
    """Return True if a message is an automatic reply (e.g. out-of-office).

    Detection is header-based (RFC 3834) so it is locale-independent and
    will not match a genuine human reply:
      * `Auto-Submitted` present with any value other than `no` -- this is
        what Exchange/Outlook set on out-of-office replies
        (`auto-generated`) and what RFC 3834 mandates for automated mail.
      * `X-Autoreply: yes` / presence of `X-Autorespond` -- used by other
        mail systems (e.g. Postfix vacation, cPanel).

    A real person replying to make contact omits these headers (or sends
    `Auto-Submitted: no`), so their mail is preserved.
    """
    h = _headers(msg)

    auto_submitted = h.get("auto-submitted", "").lower()
    if auto_submitted and auto_submitted != "no":
        return True

    if h.get("x-autoreply", "").lower() in ("yes", "true"):
        return True
    if "x-autorespond" in h:
        return True

    return False


def is_dmarc(msg: Dict[str, Any]) -> bool:
    """Return True if a message is a DMARC aggregate (RUA) report.

    Per RFC 7489 these use a subject of the form
    "Report Domain: <domain> Submitter: <org> Report-ID: <id>". Some
    forwarders prepend a "[Preview] " marker, so we match on substrings
    rather than a strict prefix. These reports are pure noise for a
    transactional no-reply mailbox and are safe to delete.
    """
    subject = (msg.get("subject") or "").strip().lower()
    return "report domain" in subject and "submitter" in subject


def is_ndr(msg: Dict[str, Any]) -> bool:
    """Return True if a message looks like a genuine Exchange NDR/bounce."""
    subject = (msg.get("subject") or "").strip().lower()

    # Explicitly exclude DMARC reports, which also arrive from
    # mailer-daemon-style addresses but are not delivery failures.
    if is_dmarc(msg):
        return False

    # Strongest signal: the Outlook message class, when available.
    if _message_class(msg).upper().startswith("REPORT.IPM.NOTE.NDR"):
        return True

    # Exchange NDRs use an "Undeliverable: ..." subject.
    if any(subject.startswith(p) for p in NDR_SUBJECT_PREFIXES):
        return True

    return False


def _plain_body(msg: Dict[str, Any]) -> str:
    body = (msg.get("body") or {}).get("content") or msg.get("bodyPreview") or ""
    if (msg.get("body") or {}).get("contentType", "").lower() == "html":
        body = TAG_RE.sub(" ", body)
    return body


def _candidate_recipients(body: str, sender_email: str) -> List[str]:
    """Extract plausible failed-recipient addresses from an NDR body.

    Excludes the sender mailbox and system/relay addresses, and de-duplicates
    so a recipient mentioned several times in one NDR counts only once.
    """
    seen: List[str] = []
    for addr in EMAIL_RE.findall(body):
        low = addr.lower()
        if low == sender_email.lower():
            continue
        if any(h in low for h in SYSTEM_ADDRESS_HINTS):
            continue
        if low not in seen:
            seen.append(low)
    return seen


def _status_code(body: str) -> Optional[str]:
    m = STATUS_RE.search(body)
    return m.group(1) if m else None


def process_mailbox(threshold: int = 2, process_bounces: bool = True,
                    clear_sent: bool = True, permanent: bool = True,
                    dry_run: bool = False,
                    logger: Callable[[str], None] = None) -> Dict[str, Any]:
    """Run the daily mailbox maintenance.

    Args:
        threshold: remove a subscriber after this many bounces.
        process_bounces: scan the Inbox and process NDRs.
        clear_sent: empty the Sent Items folder.
        permanent: permanently delete (vs. move to Deleted Items).
        dry_run: report findings without deleting or removing anything.
        logger: optional callable for status lines (defaults to print).

    Returns a summary dict.
    """
    def log(message: str):
        (logger or print)(message)

    provider = get_email_provider()
    if isinstance(provider, NoopEmailProvider):
        return {'ok': False, 'error': 'No email provider configured'}
    if not isinstance(provider, M365EmailProvider):
        return {
            'ok': False,
            'error': f'Mailbox maintenance requires the M365 provider '
                     f'(current provider is {provider.__class__.__name__})',
        }

    sender = provider.sender_email
    summary: Dict[str, Any] = {
        'ok': True,
        'dry_run': dry_run,
        'mailbox': sender,
        'scanned': 0,
        'ndrs': 0,
        'bounces_recorded': 0,
        'subscribers_removed': [],
        'ndrs_deleted': 0,
        'dmarc_deleted': 0,
        'autoreplies_deleted': 0,
        'sent_deleted': 0,
    }

    sub_db = SubscriptionDatabase()

    # ------------------------------------------------------------------
    # 1. Bounce processing
    # ------------------------------------------------------------------
    if process_bounces:
        try:
            messages = provider.read_messages(
                folder='inbox',
                select=['id', 'subject', 'from', 'receivedDateTime', 'bodyPreview',
                        'body', 'internetMessageHeaders'],
                page_size=50,
                max_messages=None,
                body_as_text=True,
            )
        except Exception as e:
            return {'ok': False, 'error': f'Failed to read inbox: {e}'}

        summary['scanned'] = len(messages)

        for msg in messages:
            if is_ndr(msg):
                summary['ndrs'] += 1
                body = _plain_body(msg)
                status = _status_code(body)
                recipients = _candidate_recipients(body, sender)

                for rcpt in recipients:
                    if dry_run:
                        matched = sub_db.get_subscriber_info(rcpt) is not None
                        if matched:
                            summary['bounces_recorded'] += 1
                            log(f"[dry-run] would record bounce for {rcpt} (status {status or 'n/a'})")
                    else:
                        res = sub_db.record_bounce(rcpt, status_code=status, threshold=threshold)
                        if res['matched']:
                            summary['bounces_recorded'] += 1
                            if res['removed']:
                                summary['subscribers_removed'].append(rcpt)
                                log(f"[bounce] Removed {rcpt} after {res['bounce_count']} bounce(s)")

                # Delete the NDR so it is never counted twice and the inbox
                # stays clean. Bounce evidence is preserved in the subscriber
                # record.
                if dry_run:
                    summary['ndrs_deleted'] += 1
                else:
                    try:
                        provider.delete_message(msg['id'], permanent=permanent)
                        summary['ndrs_deleted'] += 1
                    except Exception as e:
                        log(f"[bounce] Failed to delete NDR: {e}")

            elif is_dmarc(msg):
                # DMARC aggregate reports are pure noise for this no-reply
                # mailbox. Delete them but leave genuine inbound mail (e.g.
                # someone replying to get in touch) untouched.
                if dry_run:
                    summary['dmarc_deleted'] += 1
                else:
                    try:
                        provider.delete_message(msg['id'], permanent=permanent)
                        summary['dmarc_deleted'] += 1
                    except Exception as e:
                        log(f"[dmarc] Failed to delete report: {e}")

            elif is_auto_reply(msg):
                # Out-of-office / automatic replies generated by our own
                # outbound notifications. Safe to delete; a genuine human
                # reply omits the Auto-Submitted header and is preserved.
                if dry_run:
                    summary['autoreplies_deleted'] += 1
                else:
                    try:
                        provider.delete_message(msg['id'], permanent=permanent)
                        summary['autoreplies_deleted'] += 1
                    except Exception as e:
                        log(f"[auto-reply] Failed to delete message: {e}")

    # ------------------------------------------------------------------
    # 2. Sent Items cleanup
    # ------------------------------------------------------------------
    if clear_sent:
        if dry_run:
            try:
                sent = provider.read_messages(
                    folder='sentitems', select=['id'], page_size=100,
                    max_messages=None, body_as_text=False,
                )
                summary['sent_deleted'] = len(sent)
                log(f"[dry-run] would delete {len(sent)} sent item(s)")
            except Exception as e:
                summary['ok'] = False
                summary['error'] = f'Failed to read Sent Items: {e}'
                log(summary['error'])
        else:
            try:
                summary['sent_deleted'] = provider.empty_folder('sentitems', permanent=permanent)
            except Exception as e:
                summary['ok'] = False
                summary['error'] = f'Sent Items cleanup failed: {e}'
                log(summary['error'])

    return summary


def main():
    """CLI entry point -- runs a dry-run by default for safety."""
    import argparse

    parser = argparse.ArgumentParser(description="Process mailbox bounces and clear Sent Items.")
    parser.add_argument("--live", action="store_true",
                        help="Actually delete messages and remove subscribers (default is dry-run).")
    parser.add_argument("--threshold", type=int, default=2,
                        help="Remove a subscriber after this many bounces (default 2).")
    parser.add_argument("--no-clear-sent", action="store_true", help="Skip clearing Sent Items.")
    parser.add_argument("--soft-delete", action="store_true",
                        help="Move to Deleted Items instead of permanently deleting.")
    args = parser.parse_args()

    result = process_mailbox(
        threshold=args.threshold,
        process_bounces=True,
        clear_sent=not args.no_clear_sent,
        permanent=not args.soft_delete,
        dry_run=not args.live,
    )

    print("=" * 60)
    for key, value in result.items():
        print(f"{key}: {value}")
    return 0 if result.get('ok') else 1


if __name__ == "__main__":
    raise SystemExit(main())
