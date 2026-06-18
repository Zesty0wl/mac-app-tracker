#!/usr/bin/env python3
"""
SQLite database layer for email subscriptions.

Manages subscriber records, per-app preferences, double-opt-in
confirmation tokens, and unsubscribe tokens.
"""

import sqlite3
import os
import secrets
import hashlib
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional, Tuple
from dataclasses import dataclass


# How long a confirmation link stays valid. Kept generous (a full week) so
# people who sign up and only check their mail a day or two later — or over a
# weekend — still land on a live link instead of an "expired" error. This is
# the single source of truth; copy shown to users should match it.
CONFIRM_TOKEN_TTL_DAYS = 7


@dataclass
class Subscriber:
    """Data class for subscriber information"""
    id: Optional[int] = None
    email: str = ""
    confirmed: bool = False
    created_at: Optional[datetime] = None
    confirmed_at: Optional[datetime] = None
    last_notification: Optional[datetime] = None
    active: bool = True


@dataclass 
class AppSubscription:
    """Data class for app subscription preferences"""
    id: Optional[int] = None
    subscriber_id: int = 0
    app_id: str = ""
    subscribed: bool = True
    created_at: Optional[datetime] = None


@dataclass
class SubscriptionToken:
    """Data class for subscription tokens"""
    id: Optional[int] = None
    subscriber_id: int = 0
    token: str = ""
    token_type: str = ""  # 'confirm' or 'unsubscribe'
    expires_at: datetime = None
    used: bool = False
    created_at: Optional[datetime] = None


class SubscriptionDatabase:
    """Database manager for email subscriptions"""
    
    def __init__(self, db_path: str = None):
        """
        Initialize the subscription database
        
        Args:
            db_path: Path to the SQLite database file
        """
        # Use environment variable or default to the mounted volume
        if db_path is None:
            db_path = os.environ.get('SUBSCRIPTION_DB_PATH', '/data/subscriptions.db')
        
        self.db_path = db_path
        
        # Ensure data directory exists
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        
        # Initialize database
        self._init_database()
    
    def _init_database(self):
        """Initialize the database schema"""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("PRAGMA foreign_keys = ON")
            
            # Create subscribers table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS subscribers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    email TEXT UNIQUE NOT NULL,
                    email_hash TEXT UNIQUE NOT NULL,
                    confirmed BOOLEAN DEFAULT FALSE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    confirmed_at TIMESTAMP,
                    last_notification TIMESTAMP,
                    active BOOLEAN DEFAULT TRUE
                )
            """)
            
            # Create app_subscriptions table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS app_subscriptions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    subscriber_id INTEGER NOT NULL,
                    app_id TEXT NOT NULL,
                    subscribed BOOLEAN DEFAULT TRUE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (subscriber_id) REFERENCES subscribers (id) ON DELETE CASCADE,
                    UNIQUE(subscriber_id, app_id)
                )
            """)
            
            # Create subscription_tokens table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS subscription_tokens (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    subscriber_id INTEGER NOT NULL,
                    token TEXT UNIQUE NOT NULL,
                    token_type TEXT NOT NULL,
                    expires_at TIMESTAMP NOT NULL,
                    used BOOLEAN DEFAULT FALSE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (subscriber_id) REFERENCES subscribers (id) ON DELETE CASCADE
                )
            """)
            
            # Create indexes for performance
            conn.execute("CREATE INDEX IF NOT EXISTS idx_subscribers_email ON subscribers(email)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_subscribers_hash ON subscribers(email_hash)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_subscribers_confirmed ON subscribers(confirmed)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_app_subscriptions_subscriber ON app_subscriptions(subscriber_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_app_subscriptions_app ON app_subscriptions(app_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tokens_token ON subscription_tokens(token)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tokens_type ON subscription_tokens(token_type)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tokens_expires ON subscription_tokens(expires_at)")
            
            conn.commit()
    
    def _hash_email(self, email: str) -> str:
        """
        Create a hash of the email for privacy/security
        
        Args:
            email: Email address to hash
            
        Returns:
            SHA256 hash of the email
        """
        return hashlib.sha256(email.lower().strip().encode()).hexdigest()
    
    def _generate_token(self) -> str:
        """Generate a secure random token"""
        return secrets.token_urlsafe(32)
    
    def add_subscriber(self, email: str, app_ids: List[str] = None) -> Tuple[int, str]:
        """
        Add a new subscriber or update existing one
        
        Args:
            email: Email address
            app_ids: List of app IDs to subscribe to (empty list = all apps)
            
        Returns:
            Tuple of (subscriber_id, confirmation_token)
            
        Raises:
            ValueError: If email is invalid
        """
        email = email.lower().strip()
        if not email or '@' not in email:
            raise ValueError("Invalid email address")
        
        email_hash = self._hash_email(email)
        app_ids = app_ids or []
        
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("PRAGMA foreign_keys = ON")
            
            # Check if subscriber already exists
            cursor = conn.execute(
                "SELECT id, confirmed FROM subscribers WHERE email_hash = ?",
                (email_hash,)
            )
            result = cursor.fetchone()
            
            if result:
                subscriber_id, confirmed = result
                
                # Always delete old tokens and create new one (even if confirmed)
                # This allows users to modify their subscriptions securely
                conn.execute(
                    "DELETE FROM subscription_tokens WHERE subscriber_id = ? AND token_type = 'confirm'",
                    (subscriber_id,)
                )
            else:
                # Create new subscriber
                cursor = conn.execute(
                    "INSERT INTO subscribers (email, email_hash) VALUES (?, ?)",
                    (email, email_hash)
                )
                subscriber_id = cursor.lastrowid
            
            # Update app subscriptions
            self._update_app_subscriptions(conn, subscriber_id, app_ids)
            
            # Generate confirmation token
            token = self._generate_token()
            expires_at = datetime.now() + timedelta(days=CONFIRM_TOKEN_TTL_DAYS)
            
            conn.execute(
                """INSERT INTO subscription_tokens 
                   (subscriber_id, token, token_type, expires_at) 
                   VALUES (?, ?, 'confirm', ?)""",
                (subscriber_id, token, expires_at)
            )
            
            conn.commit()
            return subscriber_id, token
    
    def _update_app_subscriptions(self, conn, subscriber_id: int, app_ids: List[str]):
        """Update app subscriptions for a subscriber"""
        
        # Delete existing subscriptions
        conn.execute(
            "DELETE FROM app_subscriptions WHERE subscriber_id = ?",
            (subscriber_id,)
        )
        
        # Add new subscriptions
        for app_id in app_ids:
            conn.execute(
                "INSERT OR REPLACE INTO app_subscriptions (subscriber_id, app_id) VALUES (?, ?)",
                (subscriber_id, app_id)
            )
    
    def regenerate_confirm_token(self, email: str) -> Optional[Tuple[str, List[str]]]:
        """
        Issue a fresh confirmation token for an existing *unconfirmed*
        subscriber, leaving their app preferences untouched.

        Used by the "resend confirmation" flow so someone who lost the first
        email can get a new link without re-entering the whole form.

        Args:
            email: Email address

        Returns:
            (token, app_ids) for an unconfirmed subscriber, or None if the
            address is unknown or already confirmed. Callers should treat the
            None case opaquely (don't reveal which addresses exist).
        """
        email_hash = self._hash_email(email)

        with sqlite3.connect(self.db_path) as conn:
            conn.execute("PRAGMA foreign_keys = ON")

            row = conn.execute(
                "SELECT id, confirmed FROM subscribers WHERE email_hash = ?",
                (email_hash,),
            ).fetchone()

            if not row or row[1]:
                # Unknown address, or already confirmed — nothing to resend.
                return None

            subscriber_id = row[0]

            # Replace any outstanding confirm tokens with a fresh one.
            conn.execute(
                "DELETE FROM subscription_tokens WHERE subscriber_id = ? AND token_type = 'confirm'",
                (subscriber_id,),
            )
            token = self._generate_token()
            expires_at = datetime.now() + timedelta(days=CONFIRM_TOKEN_TTL_DAYS)
            conn.execute(
                """INSERT INTO subscription_tokens
                   (subscriber_id, token, token_type, expires_at)
                   VALUES (?, ?, 'confirm', ?)""",
                (subscriber_id, token, expires_at),
            )

            app_ids = [
                r[0]
                for r in conn.execute(
                    "SELECT app_id FROM app_subscriptions WHERE subscriber_id = ? AND subscribed = TRUE",
                    (subscriber_id,),
                ).fetchall()
            ]

            conn.commit()
            return token, app_ids

    def confirm_subscription(self, token: str) -> bool:
        """
        Confirm a subscription using a token
        
        Args:
            token: Confirmation token
            
        Returns:
            True if confirmation successful, False otherwise
        """
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("PRAGMA foreign_keys = ON")
            
            # Check if token is valid and not expired
            cursor = conn.execute(
                """SELECT subscriber_id, used, created_at, expires_at
                   FROM subscription_tokens 
                   WHERE token = ? AND token_type = 'confirm' 
                   AND expires_at > CURRENT_TIMESTAMP""",
                (token,)
            )
            result = cursor.fetchone()
            
            if not result:
                print(f"[SUBSCRIPTION DEBUG] Token not found or expired: {token[:20]}...")
                return False
                
            subscriber_id, used, created_at, expires_at = result
            print(f"[SUBSCRIPTION DEBUG] Token found - subscriber_id: {subscriber_id}, used: {used}, created: {created_at}, expires: {expires_at}")
            
            # Check if subscriber is already confirmed
            cursor = conn.execute(
                "SELECT confirmed FROM subscribers WHERE id = ?",
                (subscriber_id,)
            )
            sub_result = cursor.fetchone()
            if sub_result and sub_result[0]:
                print(f"[SUBSCRIPTION DEBUG] Subscriber {subscriber_id} already confirmed - allowing reconfirmation")
                # Already confirmed - this is fine, just return success
                return True
            
            # If token was used before, check if it's recent (Outlook link scanning protection)
            if used:
                print(f"[SUBSCRIPTION DEBUG] Token already used, checking if recent (within 10 minutes)")
                # Check if used recently (within 10 minutes for Outlook scanning)
                cursor = conn.execute(
                    """SELECT (julianday('now') - julianday(created_at)) * 1440 as minutes_since_created
                       FROM subscription_tokens 
                       WHERE token = ?""",
                    (token,)
                )
                time_result = cursor.fetchone()
                minutes_old = time_result[0] if time_result else 999
                print(f"[SUBSCRIPTION DEBUG] Token is {minutes_old:.1f} minutes old")
                
                if minutes_old > 10:
                    print(f"[SUBSCRIPTION DEBUG] Token too old ({minutes_old:.1f} min), rejecting")
                    return False
                else:
                    print(f"[SUBSCRIPTION DEBUG] Token recent enough ({minutes_old:.1f} min), allowing (Outlook scanning protection)")
            else:
                print(f"[SUBSCRIPTION DEBUG] Token not yet used, proceeding with confirmation")
            
            # Mark subscriber as confirmed
            conn.execute(
                "UPDATE subscribers SET confirmed = TRUE, confirmed_at = CURRENT_TIMESTAMP WHERE id = ?",
                (subscriber_id,)
            )
            
            # Mark token as used
            conn.execute(
                "UPDATE subscription_tokens SET used = TRUE WHERE token = ?",
                (token,)
            )
            
            conn.commit()
            return True
    
    def generate_unsubscribe_token(self, email: str) -> Optional[str]:
        """
        Generate an unsubscribe token for an email
        
        Args:
            email: Email address
            
        Returns:
            Unsubscribe token or None if email not found
        """
        email_hash = self._hash_email(email)
        
        with sqlite3.connect(self.db_path) as conn:
            # Find subscriber
            cursor = conn.execute(
                "SELECT id FROM subscribers WHERE email_hash = ? AND confirmed = TRUE AND active = TRUE",
                (email_hash,)
            )
            result = cursor.fetchone()
            
            if not result:
                return None
            
            subscriber_id = result[0]
            
            # Generate token
            token = self._generate_token()
            expires_at = datetime.now() + timedelta(days=30)  # Longer expiry for unsubscribe
            
            conn.execute(
                """INSERT INTO subscription_tokens 
                   (subscriber_id, token, token_type, expires_at) 
                   VALUES (?, ?, 'unsubscribe', ?)""",
                (subscriber_id, token, expires_at)
            )
            
            conn.commit()
            return token
    
    def unsubscribe(self, token: str) -> bool:
        """
        Unsubscribe using a token
        
        Args:
            token: Unsubscribe token
            
        Returns:
            True if unsubscribe successful, False otherwise
        """
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("PRAGMA foreign_keys = ON")
            
            # Check if token is valid
            cursor = conn.execute(
                """SELECT subscriber_id FROM subscription_tokens 
                   WHERE token = ? AND token_type = 'unsubscribe' 
                   AND expires_at > CURRENT_TIMESTAMP AND used = FALSE""",
                (token,)
            )
            result = cursor.fetchone()
            
            if not result:
                return False
            
            subscriber_id = result[0]
            
            # Mark subscriber as inactive
            conn.execute(
                "UPDATE subscribers SET active = FALSE WHERE id = ?",
                (subscriber_id,)
            )
            
            # Mark token as used
            conn.execute(
                "UPDATE subscription_tokens SET used = TRUE WHERE token = ?",
                (token,)
            )
            
            conn.commit()
            return True
    
    def get_subscribers_for_app(self, app_id: str) -> List[str]:
        """
        Get all confirmed, active subscribers for a specific app
        
        Args:
            app_id: Application ID
            
        Returns:
            List of email addresses
        """
        with sqlite3.connect(self.db_path) as conn:
            # Get subscribers who are subscribed to this specific app OR subscribed to all apps
            cursor = conn.execute(
                """SELECT DISTINCT s.email FROM subscribers s
                   LEFT JOIN app_subscriptions a ON s.id = a.subscriber_id
                   WHERE s.confirmed = TRUE AND s.active = TRUE
                   AND (a.app_id = ? OR a.app_id IS NULL)
                   AND (a.subscribed = TRUE OR a.subscribed IS NULL)""",
                (app_id,)
            )
            
            return [row[0] for row in cursor.fetchall()]
    
    def get_subscriber_info(self, email: str) -> Optional[Dict[str, Any]]:
        """
        Get subscriber information
        
        Args:
            email: Email address
            
        Returns:
            Dictionary with subscriber info or None
        """
        email_hash = self._hash_email(email)
        
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                """SELECT id, email, confirmed, created_at, confirmed_at, 
                          last_notification, active FROM subscribers 
                   WHERE email_hash = ?""",
                (email_hash,)
            )
            result = cursor.fetchone()
            
            if not result:
                return None
            
            subscriber_id, email, confirmed, created_at, confirmed_at, last_notification, active = result
            
            # Get app subscriptions
            cursor = conn.execute(
                "SELECT app_id FROM app_subscriptions WHERE subscriber_id = ? AND subscribed = TRUE",
                (subscriber_id,)
            )
            app_subscriptions = [row[0] for row in cursor.fetchall()]
            
            return {
                'id': subscriber_id,
                'email': email,
                'confirmed': bool(confirmed),
                'created_at': created_at,
                'confirmed_at': confirmed_at,
                'last_notification': last_notification,
                'active': bool(active),
                'app_subscriptions': app_subscriptions
            }
    
    def update_notification_sent(self, email: str):
        """
        Update the last notification timestamp for a subscriber
        
        Args:
            email: Email address
        """
        email_hash = self._hash_email(email)
        
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE subscribers SET last_notification = CURRENT_TIMESTAMP WHERE email_hash = ?",
                (email_hash,)
            )
            conn.commit()
    
    def cleanup_expired_tokens(self):
        """Remove expired tokens from the database"""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "DELETE FROM subscription_tokens WHERE expires_at < CURRENT_TIMESTAMP"
            )
            conn.commit()
    
    def get_stats(self) -> Dict[str, Any]:
        """
        Get subscription statistics
        
        Returns:
            Dictionary with various statistics
        """
        with sqlite3.connect(self.db_path) as conn:
            stats = {}
            
            # Total subscribers
            cursor = conn.execute("SELECT COUNT(*) FROM subscribers")
            stats['total_subscribers'] = cursor.fetchone()[0]
            
            # Confirmed subscribers
            cursor = conn.execute("SELECT COUNT(*) FROM subscribers WHERE confirmed = TRUE")
            stats['confirmed_subscribers'] = cursor.fetchone()[0]
            
            # Active subscribers
            cursor = conn.execute("SELECT COUNT(*) FROM subscribers WHERE confirmed = TRUE AND active = TRUE")
            stats['active_subscribers'] = cursor.fetchone()[0]
            
            # Pending confirmations
            cursor = conn.execute("SELECT COUNT(*) FROM subscribers WHERE confirmed = FALSE")
            stats['pending_confirmations'] = cursor.fetchone()[0]
            
            # App subscription counts
            cursor = conn.execute(
                """SELECT a.app_id, COUNT(*) as count FROM app_subscriptions a
                   JOIN subscribers s ON a.subscriber_id = s.id
                   WHERE s.confirmed = TRUE AND s.active = TRUE AND a.subscribed = TRUE
                   GROUP BY a.app_id ORDER BY count DESC"""
            )
            stats['app_subscription_counts'] = dict(cursor.fetchall())
            
            return stats

    # ------------------------------------------------------------------
    # Admin methods
    # ------------------------------------------------------------------

    def get_all_subscribers(self) -> List[Dict[str, Any]]:
        """Return every subscriber with their app subscriptions."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """SELECT id, email, confirmed, created_at, confirmed_at,
                          last_notification, active
                   FROM subscribers ORDER BY created_at DESC"""
            ).fetchall()

            result = []
            for r in rows:
                sid = r['id']
                apps = conn.execute(
                    "SELECT app_id FROM app_subscriptions WHERE subscriber_id = ? AND subscribed = TRUE",
                    (sid,)
                ).fetchall()
                result.append({
                    'id': sid,
                    'email': r['email'],
                    'confirmed': bool(r['confirmed']),
                    'created_at': r['created_at'],
                    'confirmed_at': r['confirmed_at'],
                    'last_notification': r['last_notification'],
                    'active': bool(r['active']),
                    'app_ids': [a['app_id'] for a in apps],
                })
            return result

    def admin_update_subscriber(self, subscriber_id: int, updates: Dict[str, Any]) -> bool:
        """Update subscriber fields from the admin panel."""
        allowed = {'confirmed', 'active'}
        fields = {k: v for k, v in updates.items() if k in allowed}
        if not fields:
            return False

        with sqlite3.connect(self.db_path) as conn:
            for col, val in fields.items():
                conn.execute(
                    f"UPDATE subscribers SET {col} = ? WHERE id = ?",
                    (val, subscriber_id),
                )
                if col == 'confirmed' and val:
                    conn.execute(
                        "UPDATE subscribers SET confirmed_at = CURRENT_TIMESTAMP WHERE id = ? AND confirmed_at IS NULL",
                        (subscriber_id,),
                    )
            conn.commit()
        return True

    def admin_delete_subscriber(self, subscriber_id: int) -> bool:
        """Permanently delete a subscriber and all related records."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("PRAGMA foreign_keys = ON")
            cursor = conn.execute("DELETE FROM subscribers WHERE id = ?", (subscriber_id,))
            conn.commit()
            return cursor.rowcount > 0

    def admin_update_app_subscriptions(self, subscriber_id: int, app_ids: List[str]) -> bool:
        """Replace a subscriber's app subscriptions."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM app_subscriptions WHERE subscriber_id = ?", (subscriber_id,))
            for app_id in app_ids:
                conn.execute(
                    "INSERT INTO app_subscriptions (subscriber_id, app_id) VALUES (?, ?)",
                    (subscriber_id, app_id),
                )
            conn.commit()
        return True

    def cleanup_unconfirmed(self, older_than_days: int) -> int:
        """Delete unconfirmed subscribers older than the given number of days.
        Returns the number of deleted rows."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("PRAGMA foreign_keys = ON")
            cursor = conn.execute(
                """DELETE FROM subscribers
                   WHERE confirmed = FALSE
                   AND julianday('now') - julianday(created_at) > ?""",
                (older_than_days,),
            )
            conn.commit()
            return cursor.rowcount

    def get_unconfirmed_needing_reminder(self, subscribed_days_ago: int,
                                         already_reminded_within_hours: int = 24,
                                         max_reminders: int = 2) -> List[Dict[str, Any]]:
        """Return unconfirmed subscribers who signed up more than
        *subscribed_days_ago* days ago and are still due a reminder.

        A subscriber is due a reminder when all of these hold:
          - they signed up more than *subscribed_days_ago* days ago,
          - their last reminder (if any) was more than
            *already_reminded_within_hours* hours ago, and
          - they have received fewer than *max_reminders* reminders.

        The reminder cap stops us from nagging the same unconfirmed address
        every day until cleanup — repeated unsolicited mail to people who
        never engage is exactly what drives spam complaints and hurts
        deliverability for everyone else.

        Reminder state is tracked via the lazily-added `last_reminder_at` and
        `reminder_count` columns.
        """
        self._ensure_last_reminder_column()
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """SELECT id, email, created_at, last_reminder_at, reminder_count
                   FROM subscribers
                   WHERE confirmed = FALSE
                   AND julianday('now') - julianday(created_at) > ?
                   AND COALESCE(reminder_count, 0) < ?
                   AND (last_reminder_at IS NULL
                        OR (julianday('now') - julianday(last_reminder_at)) * 24 > ?)""",
                (subscribed_days_ago, max_reminders, already_reminded_within_hours),
            ).fetchall()
            return [dict(r) for r in rows]

    def mark_reminder_sent(self, subscriber_id: int):
        """Record that a confirmation reminder was just sent."""
        self._ensure_last_reminder_column()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """UPDATE subscribers
                   SET last_reminder_at = CURRENT_TIMESTAMP,
                       reminder_count = COALESCE(reminder_count, 0) + 1
                   WHERE id = ?""",
                (subscriber_id,),
            )
            conn.commit()

    def _ensure_last_reminder_column(self):
        """Add the reminder-tracking columns if they don't exist yet."""
        with sqlite3.connect(self.db_path) as conn:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(subscribers)").fetchall()]
            if 'last_reminder_at' not in cols:
                conn.execute("ALTER TABLE subscribers ADD COLUMN last_reminder_at TIMESTAMP")
            if 'reminder_count' not in cols:
                conn.execute("ALTER TABLE subscribers ADD COLUMN reminder_count INTEGER NOT NULL DEFAULT 0")
            conn.commit()

    # ------------------------------------------------------------------
    # Bounce / non-delivery handling
    # ------------------------------------------------------------------

    def _ensure_bounce_columns(self):
        """Add bounce-tracking columns to the subscribers table if missing."""
        with sqlite3.connect(self.db_path) as conn:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(subscribers)").fetchall()]
            if 'bounce_count' not in cols:
                conn.execute("ALTER TABLE subscribers ADD COLUMN bounce_count INTEGER NOT NULL DEFAULT 0")
            if 'last_bounce_at' not in cols:
                conn.execute("ALTER TABLE subscribers ADD COLUMN last_bounce_at TIMESTAMP")
            if 'last_bounce_status' not in cols:
                conn.execute("ALTER TABLE subscribers ADD COLUMN last_bounce_status TEXT")
            conn.commit()

    def record_bounce(self, email: str, status_code: str = None,
                      threshold: int = 2, remove: bool = True) -> Dict[str, Any]:
        """Record a delivery bounce (NDR) for an email address.

        Increments the matching subscriber's bounce_count. When the count
        reaches `threshold`, the subscriber is deleted (if `remove` is True).
        Addresses that don't match a known subscriber are ignored.

        Args:
            email: the failed recipient address parsed from the NDR.
            status_code: enhanced SMTP status (e.g. '5.1.1'), stored for audit.
            threshold: bounce count at which the subscriber is removed.
            remove: whether to delete the subscriber once the threshold is hit.

        Returns:
            dict with keys: matched (bool), removed (bool),
            bounce_count (int), email (str).
        """
        self._ensure_bounce_columns()
        email_hash = self._hash_email(email)

        with sqlite3.connect(self.db_path) as conn:
            conn.execute("PRAGMA foreign_keys = ON")
            row = conn.execute(
                "SELECT id, bounce_count FROM subscribers WHERE email_hash = ?",
                (email_hash,),
            ).fetchone()

            if not row:
                return {'matched': False, 'removed': False, 'bounce_count': 0, 'email': email}

            subscriber_id, current = row
            new_count = (current or 0) + 1
            conn.execute(
                """UPDATE subscribers
                   SET bounce_count = ?, last_bounce_at = CURRENT_TIMESTAMP, last_bounce_status = ?
                   WHERE id = ?""",
                (new_count, status_code, subscriber_id),
            )

            removed = False
            if remove and new_count >= threshold:
                conn.execute("DELETE FROM subscribers WHERE id = ?", (subscriber_id,))
                removed = True

            conn.commit()
            return {
                'matched': True,
                'removed': removed,
                'bounce_count': new_count,
                'email': email,
            }


def main():
    """Test the subscription database"""
    print("🧪 Testing Subscription Database")
    print("=" * 50)
    
    # Initialize database
    db = SubscriptionDatabase()
    
    # Test adding subscriber
    print("📝 Adding test subscriber...")
    subscriber_id, token = db.add_subscriber("test@example.com", ["companyportal", "defender"])
    print(f"   Subscriber ID: {subscriber_id}")
    print(f"   Confirmation token: {token}")
    
    # Test confirmation
    print("\n✅ Confirming subscription...")
    success = db.confirm_subscription(token)
    print(f"   Confirmation result: {success}")
    
    # Test getting subscribers for app
    print("\n📧 Getting subscribers for Company Portal...")
    subscribers = db.get_subscribers_for_app("companyportal")
    print(f"   Subscribers: {subscribers}")
    
    # Test unsubscribe token generation
    print("\n🚫 Generating unsubscribe token...")
    unsubscribe_token = db.generate_unsubscribe_token("test@example.com")
    print(f"   Unsubscribe token: {unsubscribe_token}")
    
    # Get stats
    print("\n📊 Database statistics:")
    stats = db.get_stats()
    for key, value in stats.items():
        print(f"   {key}: {value}")
    
    print("\n✅ Database test completed!")


if __name__ == "__main__":
    main()