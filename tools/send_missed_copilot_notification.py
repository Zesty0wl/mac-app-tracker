#!/usr/bin/env python3
"""
Send the missed M365 Copilot notification
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
BASE_URL = os.environ.get('SITE_URL', 'https://localhost')

def send_missed_notification():
    """Send the missed M365 Copilot notification"""
    
    app_id = 'copilot'
    package_identifier = 'com.microsoft.m365copilot'
    
    # Get the version info from database
    with VersionDatabase(DB_PATH) as db:
        cursor = db.conn.cursor()
        
        # Get the two most recent versions
        cursor.execute("""
            SELECT * FROM versions 
            WHERE package_identifier = ? 
            ORDER BY first_detected DESC 
            LIMIT 2
        """, (package_identifier,))
        
        versions = cursor.fetchall()
        
        if not versions or len(versions) < 2:
            print(f"❌ Not enough versions found for M365 Copilot")
            return False
        
        latest = dict(versions[0])
        previous = dict(versions[1])
        
        # Get components
        cursor.execute("""
            SELECT * FROM components 
            WHERE version_id = ?
        """, (latest['id'],))
        
        components = [dict(row) for row in cursor.fetchall()]
        
        print(f"📦 App: M365 Copilot")
        print(f"📊 Previous Version: {previous['version']}")
        print(f"📊 New Version: {latest['version']}")
        print(f"🔧 Components: {len(components)}")
        
        # Prepare version details
        version_details = {
            'size_bytes': latest.get('size_bytes'),
            'checksum_sha256': latest.get('checksum_sha256'),
            'actual_url': latest.get('actual_url'),
            'app_path': latest.get('app_path'),
            'bundle_id': latest.get('bundle_id'),
            'num_files': latest.get('num_files'),
            'install_kb': latest.get('install_kb'),
            'components': components
        }
        
    # Initialize subscription manager
    print(f"🔧 Initializing subscription manager...")
    manager = SubscriptionManager(base_url=BASE_URL)
    
    # Send notifications
    print(f"📧 Sending notifications to all subscribers...")
    result = manager.send_version_notification(
        app_id=app_id,
        app_name='M365 Copilot',
        old_version=previous['version'],
        new_version=latest['version'],
        download_url=latest.get('actual_url') or latest.get('download_url'),
        detection_time=datetime.fromisoformat(latest['first_detected']),
        version_details=version_details
    )
    
    if result['success']:
        print(f"✅ Notifications sent to {result['sent_count']} subscribers")
        if result['failed_count'] > 0:
            print(f"⚠️ {result['failed_count']} notifications failed")
        return True
    else:
        print(f"❌ Failed to send notifications: {result['message']}")
        return False


if __name__ == "__main__":
    send_missed_notification()
