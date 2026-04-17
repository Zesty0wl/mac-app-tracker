#!/usr/bin/env python3
"""
Send a test notification email for a specific app
"""

import sys
import os
from pathlib import Path
from datetime import datetime

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from notifications.manager import SubscriptionManager
from tracker.database import VersionDatabase

DB_PATH = os.environ.get('DB_PATH', './data/microsoft_apps_versions.db')
SUB_DB_PATH = os.environ.get('SUBSCRIPTIONS_DB', './data/subscriptions.db')
BASE_URL = os.environ.get('SITE_URL', 'https://localhost')

def send_test_notification(email: str, app_id: str):
    """Send a test notification for an app to a specific email"""
    
    # Get the latest version info from database
    with VersionDatabase(DB_PATH) as db:
        cursor = db.conn.cursor()
        
        # Get the two most recent versions
        cursor.execute("""
            SELECT * FROM versions 
            WHERE package_identifier = ? 
            ORDER BY first_detected DESC 
            LIMIT 2
        """, (app_id,))
        
        versions = cursor.fetchall()
        
        if not versions:
            print(f"❌ No versions found for app: {app_id}")
            return False
        
        latest = dict(versions[0])
        previous = dict(versions[1]) if len(versions) > 1 else latest
        
        # Get components
        cursor.execute("""
            SELECT * FROM components 
            WHERE version_id = ?
        """, (latest['id'],))
        
        components = [dict(row) for row in cursor.fetchall()]
        
        print(f"📦 App: {app_id}")
        print(f"📊 Previous Version: {previous['version']}")
        print(f"📊 New Version: {latest['version']}")
        print(f"📧 Sending test notification to: {email}")
        print(f"🔧 Components: {len(components)}")
        
        # Prepare version details
        version_details = {
            'size_bytes': latest['size_bytes'],
            'checksum_sha256': latest['checksum_sha256'],
            'actual_url': latest['actual_url'],
            'app_path': latest.get('app_path'),
            'bundle_id': latest.get('bundle_id'),
            'num_files': latest.get('num_files'),
            'install_kb': latest.get('install_kb'),
            'components': components
        }
        
        # Get app name from identifier
        app_name_map = {
            'com.microsoft.teams2': 'Microsoft Teams',
            'com.microsoft.edgemac': 'Microsoft Edge',
            'com.microsoft.Word': 'Microsoft Word',
            'com.microsoft.Excel': 'Microsoft Excel',
            'com.microsoft.Outlook': 'Microsoft Outlook'
        }
        app_name = app_name_map.get(app_id, app_id.split('.')[-1].title())
        
    # Initialize subscription manager
    print(f"🔧 Initializing subscription manager...")
    manager = SubscriptionManager(
        base_url=BASE_URL
    )
    
    # Send notification directly to this email
    from notifications.database import SubscriptionDatabase
    sub_db = SubscriptionDatabase()
    
    # Generate unsubscribe token
    unsubscribe_token = sub_db.generate_unsubscribe_token(email)
    unsubscribe_url = f"{BASE_URL}/app-tracker/unsubscribe?token={unsubscribe_token}" if unsubscribe_token else None
    
    # Get app info
    app_info = {
        'id': app_id,
        'name': app_name,
        'page_url': f'{BASE_URL}/app-tracker'
    }
    
    # Send the email
    success = manager._send_version_notification_email(
        email=email,
        app_info=app_info,
        old_version=previous['version'],
        new_version=latest['version'],
        download_url=latest.get('actual_url') or latest.get('download_url'),
        detection_time=datetime.fromisoformat(latest['first_detected']),
        unsubscribe_url=unsubscribe_url,
        version_details=version_details
    )
    
    if success:
        print(f"✅ Test notification sent successfully!")
        return True
    else:
        print(f"❌ Failed to send test notification")
        return False


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python3 send_test_notification.py <email> <app_id>")
        print("\nExample app IDs:")
        print("  com.microsoft.teams2")
        print("  com.microsoft.edgemac")
        print("  com.microsoft.Word")
        print("  com.microsoft.Excel")
        print("  com.microsoft.Outlook")
        sys.exit(1)
    
    email = sys.argv[1]
    app_id = sys.argv[2]
    
    send_test_notification(email, app_id)
