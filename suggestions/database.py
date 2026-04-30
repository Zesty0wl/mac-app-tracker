#!/usr/bin/env python3
"""SQLite layer for community-submitted app suggestions and voting.

Tables live in the same DB as the version tracker (DB_PATH) so we don't
need another mounted volume.

Anti-gaming model:
  * The browser stores a "voted_suggestions" array in localStorage to stop
    casual double-voting and to disable the button after a vote.
  * The server stores a SHA-256 hash of (client_ip + user_agent + a per-DB
    salt) per (suggestion_id, voter_hash). UNIQUE constraint stops the
    same browser/IP combo from voting more than once.
  * Submissions and votes are rate-limited at the route level.

This is deliberately not bullet-proof -- it is a "good enough for a public
recommendation board" defence. Final approval is always manual via the
admin panel.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
from datetime import datetime
from typing import Any, Dict, List, Optional

DB_PATH = os.environ.get('DB_PATH', '/data/microsoft_apps_versions.db')

VALID_STATUSES = {'pending', 'approved', 'rejected', 'tracked'}

# Per-suggestion fields that come from the public submit form. Kept short
# on purpose so the form is easy to fill in.
_SUBMIT_FIELDS = ('name', 'identifier', 'download_url', 'release_notes_url', 'description', 'submitter_email')


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_tables() -> None:
    """Create suggestion tables and seed config rows."""
    conn = _get_conn()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS app_suggestions (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            name            TEXT    NOT NULL,
            identifier      TEXT    DEFAULT '',
            download_url    TEXT    DEFAULT '',
            release_notes_url TEXT  DEFAULT '',
            description     TEXT    DEFAULT '',
            submitter_email TEXT    DEFAULT '',
            status          TEXT    NOT NULL DEFAULT 'pending',
            admin_notes     TEXT    DEFAULT '',
            votes_count     INTEGER NOT NULL DEFAULT 0,
            submitter_hash  TEXT    DEFAULT '',
            created_at      TEXT    NOT NULL,
            updated_at      TEXT    NOT NULL
        )
    """)

    # Migration: add release_notes_url to existing suggestion tables.
    sugg_cols = {row[1] for row in cur.execute("PRAGMA table_info(app_suggestions)").fetchall()}
    if 'release_notes_url' not in sugg_cols:
        cur.execute("ALTER TABLE app_suggestions ADD COLUMN release_notes_url TEXT DEFAULT ''")

    cur.execute("""
        CREATE TABLE IF NOT EXISTS app_suggestion_votes (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            suggestion_id   INTEGER NOT NULL,
            voter_hash      TEXT    NOT NULL,
            created_at      TEXT    NOT NULL,
            UNIQUE(suggestion_id, voter_hash),
            FOREIGN KEY (suggestion_id) REFERENCES app_suggestions(id) ON DELETE CASCADE
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS app_suggestion_config (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)

    # Seed a per-DB salt used when hashing voter fingerprints.
    cur.execute("SELECT value FROM app_suggestion_config WHERE key = 'voter_salt'")
    if not cur.fetchone():
        cur.execute(
            "INSERT INTO app_suggestion_config (key, value) VALUES (?, ?)",
            ('voter_salt', secrets.token_hex(32)),
        )

    # Seed default approval threshold.
    cur.execute("SELECT value FROM app_suggestion_config WHERE key = 'approval_threshold'")
    if not cur.fetchone():
        cur.execute(
            "INSERT INTO app_suggestion_config (key, value) VALUES (?, ?)",
            ('approval_threshold', '10'),
        )

    cur.execute("CREATE INDEX IF NOT EXISTS idx_suggestions_status ON app_suggestions(status)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_suggestion_votes_sid ON app_suggestion_votes(suggestion_id)")

    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _get_config(key: str) -> Optional[str]:
    conn = _get_conn()
    row = conn.execute("SELECT value FROM app_suggestion_config WHERE key = ?", (key,)).fetchone()
    conn.close()
    return row['value'] if row else None


def _set_config(key: str, value: str) -> None:
    conn = _get_conn()
    conn.execute(
        "INSERT INTO app_suggestion_config (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()
    conn.close()


def get_voter_salt() -> str:
    salt = _get_config('voter_salt')
    if not salt:
        # Defensive: if init_tables hasn't run, create one.
        salt = secrets.token_hex(32)
        _set_config('voter_salt', salt)
    return salt


def get_approval_threshold() -> int:
    raw = _get_config('approval_threshold')
    try:
        return max(1, int(raw)) if raw else 10
    except (TypeError, ValueError):
        return 10


def set_approval_threshold(value: int) -> None:
    _set_config('approval_threshold', str(max(1, int(value))))


# ---------------------------------------------------------------------------
# Voter fingerprinting
# ---------------------------------------------------------------------------

def voter_hash(ip: str, user_agent: str) -> str:
    """Return a non-reversible voter fingerprint.

    We hash IP + UA + per-DB salt. The salt makes the hash useless for
    cross-site correlation even if the DB is leaked.
    """
    salt = get_voter_salt()
    payload = f"{salt}|{(ip or '').strip()}|{(user_agent or '').strip()}".encode()
    return hashlib.sha256(payload).hexdigest()


# ---------------------------------------------------------------------------
# Suggestions CRUD
# ---------------------------------------------------------------------------

def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    d = dict(row)
    # Never expose the submitter hash or email publicly; callers strip
    # what they don't need.
    return d


def add_suggestion(data: Dict[str, Any], submitter_hash: str = '') -> Optional[int]:
    """Insert a new suggestion. Returns the new id or None on duplicate.

    A duplicate is a pending/approved suggestion with the same case-folded
    name OR the same non-empty identifier.
    """
    name = (data.get('name') or '').strip()
    if not name:
        return None

    identifier = (data.get('identifier') or '').strip()
    download_url = (data.get('download_url') or '').strip()
    release_notes_url = (data.get('release_notes_url') or '').strip()
    description = (data.get('description') or '').strip()
    submitter_email = (data.get('submitter_email') or '').strip()

    conn = _get_conn()
    cur = conn.cursor()

    # Duplicate check (case-insensitive on name; exact on identifier).
    if identifier:
        existing = cur.execute(
            "SELECT id FROM app_suggestions "
            "WHERE status IN ('pending','approved') "
            "AND (LOWER(name) = LOWER(?) OR (identifier != '' AND identifier = ?))",
            (name, identifier),
        ).fetchone()
    else:
        existing = cur.execute(
            "SELECT id FROM app_suggestions "
            "WHERE status IN ('pending','approved') AND LOWER(name) = LOWER(?)",
            (name,),
        ).fetchone()
    if existing:
        conn.close()
        return None

    now = datetime.utcnow().isoformat()
    cur.execute(
        """INSERT INTO app_suggestions
               (name, identifier, download_url, release_notes_url, description, submitter_email,
                status, votes_count, submitter_hash, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?)""",
        (name, identifier, download_url, release_notes_url, description, submitter_email,
         submitter_hash, now, now),
    )
    new_id = cur.lastrowid
    conn.commit()
    conn.close()
    return new_id


def list_suggestions_public(limit: int = 200) -> List[Dict[str, Any]]:
    """Return suggestions visible to the public (pending + approved).

    Strips submitter email/hash so they never reach the browser.
    """
    conn = _get_conn()
    rows = conn.execute(
        """SELECT id, name, identifier, download_url, release_notes_url, description, status,
                  votes_count, created_at
           FROM app_suggestions
           WHERE status IN ('pending', 'approved')
           ORDER BY votes_count DESC, created_at ASC
           LIMIT ?""",
        (limit,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def list_suggestions_admin(status: Optional[str] = None, limit: int = 500) -> List[Dict[str, Any]]:
    conn = _get_conn()
    if status and status in VALID_STATUSES:
        rows = conn.execute(
            "SELECT * FROM app_suggestions WHERE status = ? "
            "ORDER BY votes_count DESC, created_at ASC LIMIT ?",
            (status, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM app_suggestions "
            "ORDER BY votes_count DESC, created_at ASC LIMIT ?",
            (limit,),
        ).fetchall()
    conn.close()
    return [_row_to_dict(r) for r in rows]


def get_suggestion(sid: int) -> Optional[Dict[str, Any]]:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM app_suggestions WHERE id = ?", (sid,)).fetchone()
    conn.close()
    return _row_to_dict(row) if row else None


def update_suggestion_status(sid: int, status: str, admin_notes: Optional[str] = None) -> bool:
    if status not in VALID_STATUSES:
        return False
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute("SELECT id FROM app_suggestions WHERE id = ?", (sid,))
    if not cur.fetchone():
        conn.close()
        return False
    now = datetime.utcnow().isoformat()
    if admin_notes is None:
        cur.execute(
            "UPDATE app_suggestions SET status = ?, updated_at = ? WHERE id = ?",
            (status, now, sid),
        )
    else:
        cur.execute(
            "UPDATE app_suggestions SET status = ?, admin_notes = ?, updated_at = ? WHERE id = ?",
            (status, admin_notes, now, sid),
        )
    conn.commit()
    conn.close()
    return True


def update_suggestion_fields(sid: int, fields: Dict[str, Any]) -> bool:
    """Update editable suggestion fields (admin only).

    Accepts any subset of name, identifier, download_url, description,
    submitter_email. Empty / missing keys are ignored. Returns False if
    the suggestion doesn't exist or no valid fields were supplied.
    """
    allowed = ('name', 'identifier', 'download_url', 'release_notes_url', 'description', 'submitter_email')
    updates: Dict[str, str] = {}
    for key in allowed:
        if key in fields and fields[key] is not None:
            updates[key] = str(fields[key]).strip()
    if not updates:
        return False
    if 'name' in updates and not updates['name']:
        # Don't allow blanking the name -- it's the only required field.
        return False

    conn = _get_conn()
    cur = conn.cursor()
    if not cur.execute("SELECT id FROM app_suggestions WHERE id = ?", (sid,)).fetchone():
        conn.close()
        return False

    set_clause = ', '.join(f"{k} = ?" for k in updates)
    params = list(updates.values()) + [datetime.utcnow().isoformat(), sid]
    cur.execute(
        f"UPDATE app_suggestions SET {set_clause}, updated_at = ? WHERE id = ?",
        params,
    )
    conn.commit()
    conn.close()
    return True


def delete_suggestion(sid: int) -> bool:
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM app_suggestions WHERE id = ?", (sid,))
    deleted = cur.rowcount > 0
    conn.commit()
    conn.close()
    return deleted


# ---------------------------------------------------------------------------
# Voting
# ---------------------------------------------------------------------------

def add_vote(sid: int, voter_fingerprint: str) -> tuple[bool, int]:
    """Record a vote.

    Returns (created, current_votes_count). ``created`` is False if this
    voter already voted for this suggestion.
    """
    conn = _get_conn()
    cur = conn.cursor()

    # Suggestion must exist and be open for voting. Pending suggestions
    # are visible to the public but cannot collect votes until an admin
    # has approved them.
    row = cur.execute(
        "SELECT votes_count, status FROM app_suggestions WHERE id = ?", (sid,)
    ).fetchone()
    if not row or row['status'] != 'approved':
        conn.close()
        return (False, 0)

    now = datetime.utcnow().isoformat()
    try:
        cur.execute(
            "INSERT INTO app_suggestion_votes (suggestion_id, voter_hash, created_at) "
            "VALUES (?, ?, ?)",
            (sid, voter_fingerprint, now),
        )
    except sqlite3.IntegrityError:
        conn.close()
        return (False, row['votes_count'])

    new_count = row['votes_count'] + 1
    cur.execute(
        "UPDATE app_suggestions SET votes_count = ?, updated_at = ? WHERE id = ?",
        (new_count, now, sid),
    )
    conn.commit()
    conn.close()
    return (True, new_count)


def has_voted(sid: int, voter_fingerprint: str) -> bool:
    conn = _get_conn()
    row = conn.execute(
        "SELECT 1 FROM app_suggestion_votes WHERE suggestion_id = ? AND voter_hash = ?",
        (sid, voter_fingerprint),
    ).fetchone()
    conn.close()
    return bool(row)


def voted_ids_for(voter_fingerprint: str) -> List[int]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT suggestion_id FROM app_suggestion_votes WHERE voter_hash = ?",
        (voter_fingerprint,),
    ).fetchall()
    conn.close()
    return [r['suggestion_id'] for r in rows]


def stats() -> Dict[str, int]:
    conn = _get_conn()
    cur = conn.cursor()
    out: Dict[str, int] = {}
    for status in VALID_STATUSES:
        out[status] = cur.execute(
            "SELECT COUNT(*) FROM app_suggestions WHERE status = ?", (status,)
        ).fetchone()[0]
    out['total_votes'] = cur.execute("SELECT COUNT(*) FROM app_suggestion_votes").fetchone()[0]
    conn.close()
    return out
