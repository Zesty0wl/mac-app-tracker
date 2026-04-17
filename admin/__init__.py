#!/usr/bin/env python3
"""
Flask blueprint for the admin panel.

Provides JWT-authenticated routes for managing tracked apps, configuring
email providers, viewing activity logs, and triggering manual scans.
"""

import os
import json
import functools
import subprocess
import threading
from datetime import datetime, timedelta

import jwt
from flask import (
    Blueprint, request, jsonify, render_template,
    redirect, url_for, make_response,
)

from admin import database as adb

admin_bp = Blueprint('admin', __name__, url_prefix='/admin',
                     template_folder='../templates')

_DEFAULT_JWT_SECRET = 'change-me-in-production'
JWT_SECRET = os.environ.get('ADMIN_JWT_SECRET', _DEFAULT_JWT_SECRET)
JWT_EXPIRY_HOURS = 8

# Refuse to run with the placeholder secret outside of explicit dev mode.
if JWT_SECRET == _DEFAULT_JWT_SECRET:
    if os.environ.get('DEV_MODE', 'false').lower() != 'true':
        raise RuntimeError(
            "ADMIN_JWT_SECRET is not set. Generate one with "
            "`openssl rand -hex 32` and set it in .env, or set DEV_MODE=true "
            "to bypass this check for local development."
        )
    import logging as _logging
    _logging.getLogger(__name__).warning(
        "ADMIN_JWT_SECRET is using the insecure default; DEV_MODE=true is set."
    )


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def _create_token(username: str) -> str:
    payload = {
        'sub': username,
        'iat': datetime.utcnow(),
        'exp': datetime.utcnow() + timedelta(hours=JWT_EXPIRY_HOURS),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm='HS256')


def _verify_token(token: str) -> str | None:
    """Return the username if the token is valid, else None."""
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=['HS256'])
        return payload.get('sub')
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
        return None


def admin_required(fn):
    """Decorator that protects a route with JWT cookie auth."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        token = request.cookies.get('admin_token')
        user = _verify_token(token) if token else None
        if not user:
            if request.is_json or request.path.startswith('/admin/api/'):
                return jsonify({'error': 'Unauthorized'}), 401
            return redirect(url_for('admin.login_page'))
        request.admin_user = user
        return fn(*args, **kwargs)
    return wrapper


# ---------------------------------------------------------------------------
# Login / Logout
# ---------------------------------------------------------------------------

@admin_bp.route('/login', methods=['GET'])
def login_page():
    token = request.cookies.get('admin_token')
    if token and _verify_token(token):
        return redirect(url_for('admin.dashboard'))
    return render_template('admin/login.html')


@admin_bp.route('/login', methods=['POST'])
def login_post():
    if request.is_json:
        data = request.get_json()
        username = data.get('username', '')
        password = data.get('password', '')
    else:
        username = request.form.get('username', '')
        password = request.form.get('password', '')

    # Check account lockout BEFORE verifying the password. Returns (False, 0)
    # for unknown usernames so attackers can't enumerate accounts by timing.
    locked, seconds_remaining = adb.get_lockout_status(username)
    if locked:
        minutes = max(1, (seconds_remaining + 59) // 60)
        adb.add_log('WARN', 'auth',
                    f'Login attempt against locked account: {username} '
                    f'({seconds_remaining}s remaining)')
        msg = (f'Account temporarily locked due to repeated failed logins. '
               f'Try again in ~{minutes} minute(s).')
        if request.is_json:
            return jsonify({'error': msg}), 429
        return render_template('admin/login.html', error=msg), 429

    if adb.verify_admin(username, password):
        adb.reset_failed_login(username)
        adb.update_last_login(username)
        adb.add_log('INFO', 'auth', f'Admin login: {username}')
        token = _create_token(username)
        if request.is_json:
            resp = jsonify({'ok': True})
        else:
            resp = make_response(redirect(url_for('admin.dashboard')))
        resp.set_cookie('admin_token', token, httponly=True, secure=True,
                        samesite='Lax', max_age=JWT_EXPIRY_HOURS * 3600)
        return resp

    attempts, lockout_seconds = adb.record_failed_login(username)
    if lockout_seconds:
        adb.add_log('WARN', 'auth',
                    f'Account locked after {attempts} failed logins: {username}')
    else:
        adb.add_log('WARN', 'auth',
                    f'Failed login attempt for: {username} (attempt {attempts})')
    if request.is_json:
        return jsonify({'error': 'Invalid credentials'}), 401
    return render_template('admin/login.html', error='Invalid username or password')


@admin_bp.route('/logout')
def logout():
    resp = make_response(redirect(url_for('admin.login_page')))
    resp.delete_cookie('admin_token')
    return resp


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

@admin_bp.route('/')
@admin_required
def dashboard():
    return render_template('admin/dashboard.html')


# ---------------------------------------------------------------------------
# Apps management — page
# ---------------------------------------------------------------------------

@admin_bp.route('/apps')
@admin_required
def apps_page():
    return render_template('admin/apps.html')


# ---------------------------------------------------------------------------
# Logs page
# ---------------------------------------------------------------------------

@admin_bp.route('/logs')
@admin_required
def logs_page():
    return render_template('admin/logs.html')


# ---------------------------------------------------------------------------
# REST API — Apps
# ---------------------------------------------------------------------------

@admin_bp.route('/api/apps', methods=['GET'])
@admin_required
def api_list_apps():
    apps = adb.list_tracked_apps(include_disabled=True)
    return jsonify(apps)


@admin_bp.route('/api/apps', methods=['POST'])
@admin_required
def api_add_app():
    data = request.get_json(force=True)
    required = ['app_id', 'name', 'url', 'identifier']
    missing = [f for f in required if not data.get(f)]
    if missing:
        return jsonify({'error': f'Missing fields: {", ".join(missing)}'}), 400

    if adb.add_tracked_app(data):
        _trigger_scan(data['app_id'])
        return jsonify({'ok': True}), 201
    return jsonify({'error': 'App ID already exists'}), 409


def _trigger_scan(app_id: str):
    """Run enhanced_tracker for a single app in a background thread."""
    db_path = os.environ.get('DB_PATH', '/data/microsoft_apps_versions.db')

    def _run():
        try:
            result = subprocess.run(
                ['python3', 'enhanced_tracker.py', app_id, '--db', db_path],
                capture_output=True, text=True, timeout=600,
            )
            if result.returncode == 0:
                adb.add_log('INFO', 'scan', f'Initial scan completed for {app_id}')
            else:
                # Grab last meaningful lines from stdout/stderr for the log
                output = (result.stdout or '') + (result.stderr or '')
                lines = [l.strip() for l in output.strip().splitlines() if l.strip()]
                tail = ' | '.join(lines[-3:]) if lines else 'no output'
                adb.add_log('WARN', 'scan',
                            f'Initial scan for {app_id} exited {result.returncode}: {tail}')
        except subprocess.TimeoutExpired:
            adb.add_log('ERROR', 'scan', f'Initial scan for {app_id} timed out after 600s')
        except Exception as exc:
            adb.add_log('ERROR', 'scan', f'Initial scan failed for {app_id}: {exc}')

    threading.Thread(target=_run, daemon=True).start()


@admin_bp.route('/api/apps/<app_id>', methods=['GET'])
@admin_required
def api_get_app(app_id):
    app = adb.get_tracked_app(app_id)
    if not app:
        return jsonify({'error': 'Not found'}), 404
    return jsonify(app)


@admin_bp.route('/api/apps/<app_id>', methods=['PUT'])
@admin_required
def api_update_app(app_id):
    data = request.get_json(force=True)
    required = ['name', 'url', 'identifier']
    missing = [f for f in required if not data.get(f)]
    if missing:
        return jsonify({'error': f'Missing fields: {", ".join(missing)}'}), 400

    if adb.update_tracked_app(app_id, data):
        return jsonify({'ok': True})
    return jsonify({'error': 'Not found'}), 404


@admin_bp.route('/api/apps/<app_id>', methods=['DELETE'])
@admin_required
def api_delete_app(app_id):
    if adb.delete_tracked_app(app_id):
        return jsonify({'ok': True})
    return jsonify({'error': 'Not found'}), 404


# ---------------------------------------------------------------------------
# REST API -- Logs
# ---------------------------------------------------------------------------

@admin_bp.route('/api/logs', methods=['GET'])
@admin_required
def api_get_logs():
    limit = request.args.get('limit', 200, type=int)
    level = request.args.get('level')
    source = request.args.get('source')
    logs = adb.get_logs(limit=min(limit, 1000), level=level, source=source)
    return jsonify(logs)


@admin_bp.route('/api/logs', methods=['DELETE'])
@admin_required
def api_clear_logs():
    adb.clear_logs()
    return jsonify({'ok': True})


# ---------------------------------------------------------------------------
# Email settings — page
# ---------------------------------------------------------------------------

@admin_bp.route('/email')
@admin_required
def email_page():
    return render_template('admin/email.html')


# ---------------------------------------------------------------------------
# REST API — Email settings
# ---------------------------------------------------------------------------

@admin_bp.route('/api/email/settings', methods=['GET'])
@admin_required
def api_get_email_settings():
    settings = adb.get_email_settings_masked()
    return jsonify(settings)


@admin_bp.route('/api/email/settings', methods=['PUT'])
@admin_required
def api_save_email_settings():
    data = request.get_json(force=True)

    allowed_keys = {
        'provider', 'm365_client_id', 'm365_client_secret', 'm365_tenant_id',
        'sender_email', 'resend_api_key', 'resend_from_email',
        'notification_recipients', 'site_url',
    }
    filtered = {k: v for k, v in data.items() if k in allowed_keys}
    adb.save_email_settings(filtered)
    adb.add_log('INFO', 'admin', f"Email settings updated by {request.admin_user}")
    return jsonify({'ok': True})


@admin_bp.route('/api/email/test', methods=['POST'])
@admin_required
def api_send_test_email():
    data = request.get_json(force=True)
    recipient = data.get('recipient', '').strip()
    if not recipient:
        return jsonify({'error': 'Recipient email is required'}), 400

    try:
        from notifications.providers import get_email_provider, NoopEmailProvider
        provider = get_email_provider()
        if isinstance(provider, NoopEmailProvider):
            return jsonify({'error': 'No email provider is configured'}), 400

        result = provider.send_test_email([recipient])
        adb.add_log('INFO', 'email', f"Test email sent to {recipient} via {provider.__class__.__name__}")
        return jsonify(result)
    except Exception as e:
        adb.add_log('ERROR', 'email', f"Test email failed: {e}")
        return jsonify({'error': str(e)}), 500


# ---------------------------------------------------------------------------
# Subscriptions management -- page
# ---------------------------------------------------------------------------

@admin_bp.route('/subscriptions')
@admin_required
def subscriptions_page():
    return render_template('admin/subscriptions.html')


# ---------------------------------------------------------------------------
# REST API -- Subscriptions
# ---------------------------------------------------------------------------

def _get_sub_db():
    from notifications.database import SubscriptionDatabase
    return SubscriptionDatabase()


@admin_bp.route('/api/subscriptions', methods=['GET'])
@admin_required
def api_list_subscriptions():
    db = _get_sub_db()
    subscribers = db.get_all_subscribers()
    stats = db.get_stats()
    return jsonify({'subscribers': subscribers, 'stats': stats})


@admin_bp.route('/api/subscriptions/<int:sub_id>', methods=['PUT'])
@admin_required
def api_update_subscription(sub_id):
    data = request.get_json(force=True)
    db = _get_sub_db()

    # Update status fields
    status_fields = {}
    if 'confirmed' in data:
        status_fields['confirmed'] = bool(data['confirmed'])
    if 'active' in data:
        status_fields['active'] = bool(data['active'])
    if status_fields:
        db.admin_update_subscriber(sub_id, status_fields)

    # Update app subscriptions if provided
    if 'app_ids' in data:
        db.admin_update_app_subscriptions(sub_id, data['app_ids'])

    adb.add_log('INFO', 'admin', f"Subscriber {sub_id} updated by {request.admin_user}")
    return jsonify({'ok': True})


@admin_bp.route('/api/subscriptions/<int:sub_id>', methods=['DELETE'])
@admin_required
def api_delete_subscription(sub_id):
    db = _get_sub_db()
    if db.admin_delete_subscriber(sub_id):
        adb.add_log('INFO', 'admin', f"Subscriber {sub_id} deleted by {request.admin_user}")
        return jsonify({'ok': True})
    return jsonify({'error': 'Not found'}), 404


@admin_bp.route('/api/subscriptions/cleanup', methods=['POST'])
@admin_required
def api_cleanup_unconfirmed():
    data = request.get_json(force=True)
    days = int(data.get('days', 7))
    if days < 1:
        return jsonify({'error': 'days must be >= 1'}), 400
    db = _get_sub_db()
    deleted = db.cleanup_unconfirmed(days)
    adb.add_log('INFO', 'admin',
                f"Cleaned up {deleted} unconfirmed subscriber(s) older than {days} day(s)")
    return jsonify({'ok': True, 'deleted': deleted})


@admin_bp.route('/api/subscriptions/send-reminders', methods=['POST'])
@admin_required
def api_send_reminders():
    data = request.get_json(force=True)
    days = int(data.get('days', 2))
    if days < 1:
        return jsonify({'error': 'days must be >= 1'}), 400

    db = _get_sub_db()
    pending = db.get_unconfirmed_needing_reminder(days)
    sent = 0

    if pending:
        try:
            from notifications.providers import get_email_provider, NoopEmailProvider
            provider = get_email_provider()
            if isinstance(provider, NoopEmailProvider):
                return jsonify({'error': 'No email provider configured'}), 400

            from admin.database import get_email_setting
            site_url = get_email_setting('site_url') or ''
            script_name = '/app-tracker'

            for sub in pending:
                # Generate a fresh confirmation token
                sid = sub['id']
                email = sub['email']
                token = db._generate_token()
                from datetime import datetime, timedelta
                expires_at = datetime.now() + timedelta(hours=48)

                with __import__('sqlite3').connect(db.db_path) as conn:
                    conn.execute("PRAGMA foreign_keys = ON")
                    conn.execute(
                        "DELETE FROM subscription_tokens WHERE subscriber_id = ? AND token_type = 'confirm'",
                        (sid,),
                    )
                    conn.execute(
                        """INSERT INTO subscription_tokens
                           (subscriber_id, token, token_type, expires_at)
                           VALUES (?, ?, 'confirm', ?)""",
                        (sid, token, expires_at),
                    )
                    conn.commit()

                confirmation_url = f"{site_url}{script_name}/confirm-subscription?token={token}"
                html = (
                    f"<p>Hi,</p>"
                    f"<p>You recently signed up for Mac app version notifications but haven't confirmed yet.</p>"
                    f"<p><a href=\"{confirmation_url}\">Click here to confirm your subscription</a></p>"
                    f"<p>This link expires in 48 hours. If you did not request this, you can ignore this email.</p>"
                )
                try:
                    provider.send_email(
                        to_emails=[email],
                        subject="Reminder: Confirm your Mac App Tracker subscription",
                        body_html=html,
                    )
                    db.mark_reminder_sent(sid)
                    sent += 1
                except Exception as exc:
                    adb.add_log('WARN', 'email', f"Reminder to {email} failed: {exc}")
        except Exception as exc:
            adb.add_log('ERROR', 'email', f"Reminder batch failed: {exc}")
            return jsonify({'error': str(exc)}), 500

    adb.add_log('INFO', 'admin', f"Sent {sent} confirmation reminder(s)")
    return jsonify({'ok': True, 'sent': sent, 'pending': len(pending)})


@admin_bp.route('/api/subscriptions/settings', methods=['GET'])
@admin_required
def api_get_subscription_settings():
    """Return auto-cleanup and auto-reminder settings."""
    return jsonify({
        'auto_cleanup_enabled': adb.get_email_setting('sub_auto_cleanup_enabled') == '1',
        'auto_cleanup_days': int(adb.get_email_setting('sub_auto_cleanup_days') or 7),
        'auto_reminder_enabled': adb.get_email_setting('sub_auto_reminder_enabled') == '1',
        'auto_reminder_days': int(adb.get_email_setting('sub_auto_reminder_days') or 2),
    })


@admin_bp.route('/api/subscriptions/settings', methods=['PUT'])
@admin_required
def api_save_subscription_settings():
    data = request.get_json(force=True)
    mapping = {
        'auto_cleanup_enabled': '1' if data.get('auto_cleanup_enabled') else '0',
        'auto_cleanup_days': str(int(data.get('auto_cleanup_days', 7))),
        'auto_reminder_enabled': '1' if data.get('auto_reminder_enabled') else '0',
        'auto_reminder_days': str(int(data.get('auto_reminder_days', 2))),
    }
    adb.save_email_settings({f'sub_{k}' if not k.startswith('sub_') else k: v
                             for k, v in mapping.items()})
    adb.add_log('INFO', 'admin', f"Subscription settings updated by {request.admin_user}")
    return jsonify({'ok': True})
