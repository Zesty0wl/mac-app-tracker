#!/usr/bin/env python3
"""
Background scheduler that triggers a full version check at a fixed interval.

Runs enhanced_tracker.py as a subprocess every hour and logs results.
Launched by docker-entrypoint.sh alongside gunicorn.
"""

import time
import subprocess
import sys
import os
from datetime import datetime
sys.path.insert(0, '/app')
from tracker.database import VersionDatabase

CHECK_INTERVAL = 3600  # 60 minutes in seconds

def run_check():
    """Run the version check for all apps"""
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{timestamp}] Starting version check...")
    
    # Get database path from environment variable
    db_path = os.environ.get('DB_PATH', 'microsoft_apps_versions.db')
    
    try:
        result = subprocess.run(
            ['python3', 'enhanced_tracker.py', 'all', '--db', db_path],
            capture_output=True,
            text=True,
            timeout=3600  # 1 hour timeout
        )
        
        print(result.stdout)
        if result.stderr:
            print("STDERR:", result.stderr, file=sys.stderr)
        
        # Update last check time regardless of whether new versions were found
        with VersionDatabase(db_path) as db:
            db.update_last_check_time()
        
        if result.returncode == 0:
            print(f"[{timestamp}] Check completed successfully")
        else:
            print(f"[{timestamp}] Check completed with errors (exit code: {result.returncode})")
        
        return result.returncode
    except subprocess.TimeoutExpired:
        print(f"[{timestamp}] Check timed out after 1 hour")
        return 1
    except Exception as e:
        print(f"[{timestamp}] Error running check: {e}")
        return 1


def run_subscription_maintenance():
    """Run auto-cleanup and auto-reminder tasks if enabled in admin settings."""
    try:
        import admin.database as adb
        from notifications.database import SubscriptionDatabase

        cleanup_enabled = adb.get_email_setting('sub_auto_cleanup_enabled') == '1'
        reminder_enabled = adb.get_email_setting('sub_auto_reminder_enabled') == '1'

        if not cleanup_enabled and not reminder_enabled:
            return

        sub_db = SubscriptionDatabase()

        # Auto-cleanup unconfirmed subscribers
        if cleanup_enabled:
            days = int(adb.get_email_setting('sub_auto_cleanup_days') or 7)
            deleted = sub_db.cleanup_unconfirmed(days)
            if deleted:
                print(f"[subscription-maintenance] Cleaned up {deleted} unconfirmed subscriber(s) older than {days} day(s)")
                adb.add_log('INFO', 'subscriptions', f"Auto-cleanup removed {deleted} unconfirmed subscriber(s) older than {days}d")

        # Auto-reminders for unconfirmed subscribers
        if reminder_enabled:
            days = int(adb.get_email_setting('sub_auto_reminder_days') or 2)
            pending = sub_db.get_unconfirmed_needing_reminder(days)
            if not pending:
                return

            try:
                from notifications.providers import get_email_provider, NoopEmailProvider
                provider = get_email_provider()
                if isinstance(provider, NoopEmailProvider):
                    return

                site_url = adb.get_email_setting('site_url') or ''
                script_name = '/app-tracker'
                sent = 0

                for sub in pending:
                    sid = sub['id']
                    email = sub['email']
                    token = sub_db._generate_token()
                    from datetime import timedelta
                    expires_at = datetime.now() + timedelta(hours=48)

                    import sqlite3
                    with sqlite3.connect(sub_db.db_path) as conn:
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
                        sub_db.mark_reminder_sent(sid)
                        sent += 1
                    except Exception as exc:
                        print(f"[subscription-maintenance] Reminder to {email} failed: {exc}")

                if sent:
                    print(f"[subscription-maintenance] Sent {sent} confirmation reminder(s)")
                    adb.add_log('INFO', 'subscriptions', f"Auto-reminders: sent {sent} confirmation reminder(s)")
            except Exception as exc:
                print(f"[subscription-maintenance] Reminder batch failed: {exc}")

    except Exception as e:
        print(f"[subscription-maintenance] Error: {e}")

def main():
    """Main scheduler loop"""
    print("Starting Mac Apps version checker scheduler")
    print(f"Will check every {CHECK_INTERVAL} seconds ({CHECK_INTERVAL/3600} hours)")
    
    # Run immediately on startup
    run_check()
    run_subscription_maintenance()
    
    # Then run on schedule
    while True:
        time.sleep(CHECK_INTERVAL)
        run_check()
        run_subscription_maintenance()

if __name__ == '__main__':
    main()
