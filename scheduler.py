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
            try:
                from notifications.providers import get_email_provider, NoopEmailProvider
                from notifications.manager import send_confirmation_reminders
                provider = get_email_provider()
                if isinstance(provider, NoopEmailProvider):
                    return

                site_url = adb.get_email_setting('site_url') or ''
                result = send_confirmation_reminders(
                    sub_db, provider, site_url, days=days,
                    logger=lambda m: print(f"[subscription-maintenance] {m}"),
                )
                sent = result['sent']
                if sent:
                    print(f"[subscription-maintenance] Sent {sent} confirmation reminder(s)")
                    adb.add_log('INFO', 'subscriptions', f"Auto-reminders: sent {sent} confirmation reminder(s)")
            except Exception as exc:
                print(f"[subscription-maintenance] Reminder batch failed: {exc}")

    except Exception as e:
        print(f"[subscription-maintenance] Error: {e}")

def run_mailbox_maintenance():
    """Once per day: process bounces and clear the M365 Sent Items folder.

    Gated by admin settings so it only fires for the M365 provider, at the
    configured UTC hour, at most once per calendar day.
    """
    try:
        import admin.database as adb

        if (adb.get_email_setting('mailbox_maint_enabled') or '1') != '1':
            return

        provider_type = (adb.get_email_setting('provider') or '').lower()
        if not provider_type and os.environ.get('M365_CLIENT_ID'):
            provider_type = 'm365'
        if provider_type and provider_type != 'm365':
            return

        now = datetime.utcnow()
        target_hour = int(adb.get_email_setting('mailbox_maint_hour') or 4)
        if now.hour < target_hour:
            return

        today = now.strftime('%Y-%m-%d')
        if (adb.get_email_setting('mailbox_maint_last_run') or '') == today:
            return

        threshold = int(adb.get_email_setting('bounce_threshold') or 2)
        clear_sent = (adb.get_email_setting('mailbox_clear_sent') or '1') == '1'
        permanent = (adb.get_email_setting('mailbox_permanent_delete') or '1') == '1'

        from notifications.bounce_processor import process_mailbox
        result = process_mailbox(
            threshold=threshold,
            process_bounces=True,
            clear_sent=clear_sent,
            permanent=permanent,
            dry_run=False,
            logger=lambda m: print(f"[mailbox-maintenance] {m}"),
        )

        # Record the run date even on partial failure to avoid hammering the
        # mailbox repeatedly within the same day.
        adb.set_email_setting('mailbox_maint_last_run', today)

        if result.get('ok'):
            removed = result.get('subscribers_removed', [])
            msg = (f"Processed {result.get('ndrs', 0)} NDR(s), recorded "
                   f"{result.get('bounces_recorded', 0)} bounce(s), removed "
                   f"{len(removed)} subscriber(s), deleted "
                   f"{result.get('dmarc_deleted', 0)} DMARC report(s) and "
                   f"{result.get('autoreplies_deleted', 0)} auto-reply(ies), cleared "
                   f"{result.get('sent_deleted', 0)} sent item(s)")
            print(f"[mailbox-maintenance] {msg}")
            adb.add_log('INFO', 'email', f"Mailbox maintenance: {msg}")
        else:
            err = result.get('error', 'unknown error')
            print(f"[mailbox-maintenance] Failed: {err}")
            adb.add_log('ERROR', 'email', f"Mailbox maintenance failed: {err}")

    except Exception as e:
        print(f"[mailbox-maintenance] Error: {e}")
        try:
            import admin.database as adb
            adb.add_log('ERROR', 'email', f"Mailbox maintenance error: {e}")
        except Exception:
            pass

def main():
    """Main scheduler loop"""
    print("Starting Mac Apps version checker scheduler")
    print(f"Will check every {CHECK_INTERVAL} seconds ({CHECK_INTERVAL/3600} hours)")
    
    # Run immediately on startup
    run_check()
    run_subscription_maintenance()
    run_mailbox_maintenance()
    
    # Then run on schedule
    while True:
        time.sleep(CHECK_INTERVAL)
        run_check()
        run_subscription_maintenance()
        run_mailbox_maintenance()

if __name__ == '__main__':
    main()
