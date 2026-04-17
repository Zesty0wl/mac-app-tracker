#!/usr/bin/env python3
"""
Admin database tables and queries.

Manages the admin_users, tracked_apps, admin_log, and email_settings
tables inside the shared SQLite database.
"""

import sqlite3
import hashlib
import secrets
import os
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Any


DB_PATH = os.environ.get('DB_PATH', '/data/microsoft_apps_versions.db')


def _get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _hash_password(password: str, salt: str) -> str:
    return hashlib.sha256((salt + password).encode()).hexdigest()


# ---------------------------------------------------------------------------
# Schema initialisation
# ---------------------------------------------------------------------------

def init_admin_tables():
    """Create admin_users and tracked_apps tables if they don't exist."""
    conn = _get_conn()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS admin_users (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            username    TEXT    NOT NULL UNIQUE,
            password    TEXT    NOT NULL,
            salt        TEXT    NOT NULL,
            created_at  TEXT    NOT NULL,
            last_login  TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS tracked_apps (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            app_id      TEXT    NOT NULL UNIQUE,
            name        TEXT    NOT NULL,
            url         TEXT    NOT NULL,
            identifier  TEXT    NOT NULL,
            description TEXT    DEFAULT '',
            type        TEXT    DEFAULT 'single',
            url_type    TEXT    DEFAULT 'direct',
            enabled     INTEGER DEFAULT 1,
            created_at  TEXT    NOT NULL,
            updated_at  TEXT    NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS admin_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp   TEXT    NOT NULL,
            level       TEXT    NOT NULL DEFAULT 'INFO',
            source      TEXT    NOT NULL DEFAULT 'system',
            message     TEXT    NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS email_settings (
            key        TEXT PRIMARY KEY,
            value      TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)

    conn.commit()
    conn.close()

    # Seed email settings from env vars if the table is empty
    _seed_email_settings_from_env()

    # Seed a default app so first run has something to scan
    _seed_default_apps()


def _seed_email_settings_from_env():
    """Populate email_settings from environment variables on first run."""
    conn = _get_conn()
    count = conn.execute("SELECT COUNT(*) FROM email_settings").fetchone()[0]
    if count > 0:
        conn.close()
        return

    env_map = {
        'm365_client_id':         'M365_CLIENT_ID',
        'm365_client_secret':     'M365_CLIENT_SECRET',
        'm365_tenant_id':         'M365_TENANT_ID',
        'sender_email':           'SENDER_EMAIL',
        'notification_recipients': 'NOTIFICATION_RECIPIENTS',
        'resend_api_key':         'RESEND_API_KEY',
        'resend_from_email':      'RESEND_FROM_EMAIL',
        'site_url':               'SITE_URL',
    }

    now = datetime.utcnow().isoformat()
    seeded = False
    for key, env_var in env_map.items():
        val = os.environ.get(env_var, '').strip()
        if val:
            conn.execute(
                "INSERT OR IGNORE INTO email_settings (key, value, updated_at) VALUES (?, ?, ?)",
                (key, val, now),
            )
            seeded = True

    # Infer provider from whichever credentials are present
    if os.environ.get('M365_CLIENT_ID'):
        conn.execute(
            "INSERT OR IGNORE INTO email_settings (key, value, updated_at) VALUES (?, ?, ?)",
            ('provider', 'm365', now),
        )
    elif os.environ.get('RESEND_API_KEY'):
        conn.execute(
            "INSERT OR IGNORE INTO email_settings (key, value, updated_at) VALUES (?, ?, ?)",
            ('provider', 'resend', now),
        )

    conn.commit()
    conn.close()
    if seeded:
        add_log('INFO', 'system', 'Seeded email settings from environment variables')


def _seed_default_apps():
    """Insert a default tracked app when the table is empty (first run)."""
    conn = _get_conn()
    count = conn.execute("SELECT COUNT(*) FROM tracked_apps").fetchone()[0]
    conn.close()
    if count > 0:
        return

    default_app = {
        'app_id': 'companyportal',
        'name': 'Company Portal',
        'url': 'https://go.microsoft.com/fwlink/?linkid=853070',
        'identifier': 'com.microsoft.CompanyPortalMac',
        'description': 'Microsoft Intune Company Portal for macOS',
        'type': 'single',
        'url_type': 'direct',
    }
    if add_tracked_app(default_app):
        add_log('INFO', 'system',
                'Seeded default app (Company Portal). Add more from the admin panel.')


# ---------------------------------------------------------------------------
# Admin user management
# ---------------------------------------------------------------------------

def create_admin_user(username: str, password: str) -> bool:
    """Create an admin user.  Returns False if username already exists."""
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute("SELECT id FROM admin_users WHERE username = ?", (username,))
    if cur.fetchone():
        conn.close()
        return False

    salt = secrets.token_hex(16)
    hashed = _hash_password(password, salt)
    cur.execute(
        "INSERT INTO admin_users (username, password, salt, created_at) VALUES (?, ?, ?, ?)",
        (username, hashed, salt, datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()
    return True


def verify_admin(username: str, password: str) -> bool:
    """Verify admin credentials."""
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute("SELECT password, salt FROM admin_users WHERE username = ?", (username,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return False
    return _hash_password(password, row['salt']) == row['password']


def update_last_login(username: str):
    conn = _get_conn()
    conn.execute(
        "UPDATE admin_users SET last_login = ? WHERE username = ?",
        (datetime.utcnow().isoformat(), username),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Tracked apps CRUD
# ---------------------------------------------------------------------------

def list_tracked_apps(include_disabled: bool = False) -> List[Dict[str, Any]]:
    conn = _get_conn()
    if include_disabled:
        rows = conn.execute("SELECT * FROM tracked_apps ORDER BY name").fetchall()
    else:
        rows = conn.execute("SELECT * FROM tracked_apps WHERE enabled = 1 ORDER BY name").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_tracked_app(app_id: str) -> Optional[Dict[str, Any]]:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM tracked_apps WHERE app_id = ?", (app_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def add_tracked_app(data: Dict[str, Any]) -> bool:
    """Add a new tracked app.  Returns False if app_id already exists."""
    conn = _get_conn()
    cur = conn.cursor()
    now = datetime.utcnow().isoformat()
    try:
        cur.execute("""
            INSERT INTO tracked_apps (app_id, name, url, identifier, description, type, url_type, enabled, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            data['app_id'], data['name'], data['url'], data['identifier'],
            data.get('description', ''), data.get('type', 'single'),
            data.get('url_type', 'direct'), 1, now, now,
        ))
        conn.commit()
        conn.close()
        add_log('INFO', 'admin', f"Added tracked app: {data['name']} ({data['app_id']})")
        return True
    except sqlite3.IntegrityError:
        conn.close()
        return False


def update_tracked_app(app_id: str, data: Dict[str, Any]) -> bool:
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute("SELECT id FROM tracked_apps WHERE app_id = ?", (app_id,))
    if not cur.fetchone():
        conn.close()
        return False

    now = datetime.utcnow().isoformat()
    cur.execute("""
        UPDATE tracked_apps
        SET name = ?, url = ?, identifier = ?, description = ?,
            type = ?, url_type = ?, enabled = ?, updated_at = ?
        WHERE app_id = ?
    """, (
        data['name'], data['url'], data['identifier'],
        data.get('description', ''), data.get('type', 'single'),
        data.get('url_type', 'direct'), data.get('enabled', 1),
        now, app_id,
    ))
    conn.commit()
    conn.close()
    add_log('INFO', 'admin', f"Updated tracked app: {data['name']} ({app_id})")
    return True


def delete_tracked_app(app_id: str) -> bool:
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute("SELECT name FROM tracked_apps WHERE app_id = ?", (app_id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return False
    name = row['name']
    cur.execute("DELETE FROM tracked_apps WHERE app_id = ?", (app_id,))
    conn.commit()
    conn.close()
    add_log('INFO', 'admin', f"Deleted tracked app: {name} ({app_id})")
    return True


# ---------------------------------------------------------------------------
# Config bridge — returns dict in the same shape tracker/config.py expects
# ---------------------------------------------------------------------------

def load_apps_from_db() -> Dict[str, Dict[str, Any]]:
    """Return tracked apps in the same format as tracker.config.load_apps_config()."""
    apps = list_tracked_apps(include_disabled=False)
    result: Dict[str, Dict[str, Any]] = {}
    for a in apps:
        result[a['app_id']] = {
            'name': a['name'],
            'url': a['url'],
            'identifier': a['identifier'],
            'description': a['description'],
            'type': a['type'],
            'url_type': a['url_type'],
        }
    return result


# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------

def add_log(level: str, source: str, message: str):
    conn = _get_conn()
    conn.execute(
        "INSERT INTO admin_log (timestamp, level, source, message) VALUES (?, ?, ?, ?)",
        (datetime.utcnow().isoformat(), level, source, message),
    )
    conn.commit()
    conn.close()


def get_logs(limit: int = 200, level: Optional[str] = None,
             source: Optional[str] = None) -> List[Dict[str, Any]]:
    conn = _get_conn()
    query = "SELECT * FROM admin_log WHERE 1=1"
    params: list = []
    if level:
        query += " AND level = ?"
        params.append(level)
    if source:
        query += " AND source = ?"
        params.append(source)
    query += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def clear_logs():
    conn = _get_conn()
    conn.execute("DELETE FROM admin_log")
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Email settings
# ---------------------------------------------------------------------------

# Keys that hold secrets -- values are masked when returned via the API
_SECRET_KEYS = {'m365_client_secret', 'resend_api_key'}


def get_email_setting(key: str) -> Optional[str]:
    """Return a single email setting value, or None if not set."""
    conn = _get_conn()
    row = conn.execute("SELECT value FROM email_settings WHERE key = ?", (key,)).fetchone()
    conn.close()
    return row['value'] if row else None


def get_all_email_settings() -> Dict[str, str]:
    """Return all email settings as a plain dict."""
    conn = _get_conn()
    rows = conn.execute("SELECT key, value FROM email_settings").fetchall()
    conn.close()
    return {row['key']: row['value'] for row in rows}


def get_email_settings_masked() -> Dict[str, str]:
    """Return all email settings with secret values masked for the admin UI."""
    settings = get_all_email_settings()
    masked = {}
    for k, v in settings.items():
        if k in _SECRET_KEYS and v:
            masked[k] = v[:4] + '*' * (len(v) - 4) if len(v) > 4 else '****'
        else:
            masked[k] = v
    return masked


def set_email_setting(key: str, value: str):
    """Upsert a single email setting."""
    conn = _get_conn()
    now = datetime.utcnow().isoformat()
    conn.execute("""
        INSERT INTO email_settings (key, value, updated_at) VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
    """, (key, value, now))
    conn.commit()
    conn.close()


def save_email_settings(settings: Dict[str, str]):
    """Upsert multiple email settings in one transaction.

    Secret keys whose value is all-asterisk (masked placeholder) are skipped
    so that a round-trip through the masked API does not clobber real secrets.
    """
    conn = _get_conn()
    now = datetime.utcnow().isoformat()
    for key, value in settings.items():
        # Skip masked secret placeholders
        if key in _SECRET_KEYS and value and set(value) <= {'*'}:
            continue
        conn.execute("""
            INSERT INTO email_settings (key, value, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """, (key, value, now))
    conn.commit()
    conn.close()
