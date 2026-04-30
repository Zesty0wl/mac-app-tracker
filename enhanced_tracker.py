#!/usr/bin/env python3
"""
Version-check orchestrator with email notifications.

Downloads and analyses tracked apps via download_and_analyze, then sends
email notifications to subscribers when new versions are detected.
Called as a subprocess by scheduler.py and the admin scan trigger.
"""

import os
import sys
from datetime import datetime
from pathlib import Path

# Add the current directory to path to import our modules
sys.path.insert(0, str(Path(__file__).parent))

from download_and_analyze import MicrosoftAppAnalyzer, load_apps_config, validate_urls
from notifications.manager import SubscriptionManager
from tracker.database import VersionDatabase
from admin.database import add_log


def analyze_with_notifications(app_id, app_info, db_path, keep_downloads=False):
    """
    Analyze an app and send notifications if new version detected
    
    Args:
        app_id: Application ID from config
        app_info: Application configuration dict
        db_path: Database file path
        keep_downloads: Whether to keep downloaded files
        
    Returns:
        Dict with analysis results and notification info
    """
    print(f"\n📦 Analyzing {app_info['name']}...")
    
    # Initialize subscription manager
    subscription_manager = SubscriptionManager()
    
    # Get previous version info
    with VersionDatabase(db_path) as db:
        cursor = db.conn.cursor()
        cursor.execute(
            "SELECT version FROM versions WHERE package_identifier = ? ORDER BY first_detected DESC LIMIT 1",
            (app_info['identifier'],)
        )
        previous_version_row = cursor.fetchone()
        previous_version = previous_version_row['version'] if previous_version_row else None
    
    # Analyze the app
    analyzer = MicrosoftAppAnalyzer(
        app_name=app_info['name'],
        download_url=app_info['url'],
        expected_identifier=app_info['identifier'],
        package_type=app_info.get('type', 'single'),
        db_path=db_path,
        keep_downloads=keep_downloads,
        url_type=app_info.get('url_type', 'direct')
    )
    
    results = analyzer.analyze()
    notification_result = None
    
    if results is None:
        # Analysis failed
        return {
            'success': False,
            'app_id': app_id,
            'app_name': app_info['name'],
            'message': 'Analysis failed',
            'notification_sent': False
        }
    
    if isinstance(results, dict) and results.get('unchanged'):
        # No version change
        return {
            'success': True,
            'app_id': app_id,
            'app_name': app_info['name'],
            'message': 'No version change detected',
            'notification_sent': False,
            'unchanged': True
        }
    
    # New version detected - get the new version info
    with VersionDatabase(db_path) as db:
        cursor = db.conn.cursor()
        cursor.execute(
            """SELECT * FROM versions 
               WHERE package_identifier = ? ORDER BY first_detected DESC LIMIT 1""",
            (app_info['identifier'],)
        )
        latest_version_row = cursor.fetchone()
        
        # Get components if available
        components = []
        if latest_version_row:
            version_id = latest_version_row['id']
            cursor.execute(
                """SELECT * FROM components WHERE version_id = ?""",
                (version_id,)
            )
            components = [dict(row) for row in cursor.fetchall()]
        
        if latest_version_row:
            new_version = latest_version_row['version']
            download_url = latest_version_row['download_url']
            detection_time = datetime.fromisoformat(latest_version_row['first_detected'])
            
            # Only send notification if version actually changed
            if previous_version and previous_version != new_version:
                print(f"🔄 Version change detected: {previous_version} → {new_version}")
                add_log('INFO', 'tracker', f"Version change detected for {app_info['name']}: {previous_version} → {new_version}")

                release_notes_url = app_info.get('release_notes_url', '')

                print("📧 Sending notifications to subscribers...")
                
                try:
                    # Convert Row to dict for easier handling
                    version_row_dict = dict(latest_version_row)
                    
                    # Prepare version details for email
                    version_details = {
                        'size_bytes': version_row_dict.get('size_bytes'),
                        'checksum_sha256': version_row_dict.get('checksum_sha256'),
                        'actual_url': version_row_dict.get('actual_url'),
                        'app_path': version_row_dict.get('app_path'),
                        'bundle_id': version_row_dict.get('bundle_id'),
                        'num_files': version_row_dict.get('num_files'),
                        'install_kb': version_row_dict.get('install_kb'),
                        'components': components,
                        'release_notes_url': release_notes_url,
                    }
                    
                    notification_result = subscription_manager.send_version_notification(
                        app_id=app_id,
                        app_name=app_info['name'],
                        old_version=previous_version,
                        new_version=new_version,
                        download_url=download_url,
                        detection_time=detection_time,
                        version_details=version_details
                    )
                    
                    if notification_result['success']:
                        print(f"✅ Notifications sent to {notification_result['sent_count']} subscribers")
                        if notification_result['failed_count'] > 0:
                            print(f"⚠️ {notification_result['failed_count']} notifications failed")
                        add_log('INFO', 'tracker', f"Notifications sent for {app_info['name']} v{new_version}: {notification_result['sent_count']} sent, {notification_result['failed_count']} failed")
                    else:
                        print(f"❌ Notification sending failed: {notification_result['message']}")
                        add_log('ERROR', 'tracker', f"Notification sending failed for {app_info['name']}: {notification_result['message']}")
                        
                except Exception as e:
                    print(f"❌ Error sending notifications: {e}")
                    add_log('ERROR', 'tracker', f"Error sending notifications for {app_info['name']}: {e}")
                    notification_result = {
                        'success': False,
                        'message': f'Error sending notifications: {e}',
                        'sent_count': 0,
                        'failed_count': 0
                    }
            
            return {
                'success': True,
                'app_id': app_id,
                'app_name': app_info['name'],
                'previous_version': previous_version,
                'new_version': new_version,
                'message': f'New version detected: {new_version}',
                'notification_sent': notification_result is not None,
                'notification_result': notification_result
            }
    
    return {
        'success': True,
        'app_id': app_id,
        'app_name': app_info['name'],
        'message': 'Analysis completed',
        'notification_sent': False
    }


def main():
    """Enhanced main function with notification support"""
    import argparse
    
    # Load apps configuration
    KNOWN_APPS = load_apps_config()
    
    parser = argparse.ArgumentParser(
        description='Download, analyze Microsoft Mac applications and send email notifications',
        epilog=f'Available apps: {", ".join(KNOWN_APPS.keys())}'
    )
    parser.add_argument(
        'app',
        nargs='?',
        help='Application to analyze (use "all" for all apps, default: companyportal)'
    )
    parser.add_argument(
        '--no-notifications',
        action='store_true',
        help='Disable email notifications (analysis only)'
    )
    parser.add_argument(
        '--show-history',
        action='store_true',
        help='Show version history from database'
    )
    parser.add_argument(
        '--export-json',
        type=str,
        metavar='FILE',
        help='Export version history to JSON file'
    )
    parser.add_argument(
        '--custom-url',
        type=str,
        help='Custom download URL (requires --custom-name)'
    )
    parser.add_argument(
        '--custom-name',
        type=str,
        help='Custom app name (requires --custom-url)'
    )
    parser.add_argument(
        '--db',
        type=str,
        default=os.environ.get('DB_PATH', 'microsoft_apps_versions.db'),
        help='Database file path (default: DB_PATH env var or microsoft_apps_versions.db)'
    )
    parser.add_argument(
        '--list-apps',
        action='store_true',
        help='List all available apps'
    )
    parser.add_argument(
        '--keep-downloads',
        action='store_true',
        help='Keep downloaded .pkg files (default is to delete after successful analysis)'
    )
    parser.add_argument(
        '--validate-urls',
        action='store_true',
        help='Check all stored URLs and mark removed ones as [download removed]'
    )
    parser.add_argument(
        '--subscription-stats',
        action='store_true',
        help='Show email subscription statistics'
    )
    parser.add_argument(
        '--cleanup-tokens',
        action='store_true',
        help='Clean up expired subscription tokens'
    )
    
    args = parser.parse_args()
    
    # Show subscription stats
    if args.subscription_stats:
        try:
            subscription_manager = SubscriptionManager()
            stats = subscription_manager.get_subscription_stats()
            
            print("\n📊 Email Subscription Statistics")
            print("=" * 50)
            print(f"Total subscribers: {stats['total_subscribers']}")
            print(f"Confirmed subscribers: {stats['confirmed_subscribers']}")
            print(f"Active subscribers: {stats['active_subscribers']}")
            print(f"Pending confirmations: {stats['pending_confirmations']}")
            
            if stats['app_subscription_counts']:
                print(f"\nApp subscription counts:")
                for app_id, count in stats['app_subscription_counts'].items():
                    app_name = KNOWN_APPS.get(app_id, {}).get('name', app_id)
                    print(f"  {app_name}: {count} subscribers")
            
            print("=" * 50)
        except Exception as e:
            print(f"❌ Error getting subscription stats: {e}")
        return 0
    
    # Clean up expired tokens
    if args.cleanup_tokens:
        try:
            subscription_manager = SubscriptionManager()
            subscription_manager.cleanup_expired_tokens()
            print("✅ Expired tokens cleaned up")
        except Exception as e:
            print(f"❌ Error cleaning up tokens: {e}")
        return 0
    
    # List available apps
    if args.list_apps:
        print("\nAvailable applications:")
        print("=" * 80)
        for app_id, app_info in KNOWN_APPS.items():
            print(f"\n{app_id}:")
            print(f"  Name: {app_info['name']}")
            print(f"  URL: {app_info['url']}")
            print(f"  Identifier: {app_info['identifier']}")
            if app_info.get('description'):
                print(f"  Description: {app_info['description']}")
        print("\n" + "=" * 80)
        return 0
    
    # If only showing history, don't download/analyze
    if args.show_history:
        with VersionDatabase(args.db) as db:
            db.print_version_history()
        return 0
    
    # Validate URLs if requested
    if args.validate_urls:
        validate_urls(args.db)
        return 0
    
    if args.export_json:
        with VersionDatabase(args.db) as db:
            db.export_to_json(args.export_json)
        return 0
    
    # Handle custom URL (no notifications for custom apps)
    if args.custom_url and args.custom_name:
        print(f"Using custom app: {args.custom_name}")
        analyzer = MicrosoftAppAnalyzer(
            app_name=args.custom_name,
            download_url=args.custom_url,
            package_type='single',
            db_path=args.db,
            keep_downloads=args.keep_downloads
        )
        results = analyzer.analyze()
        return 0 if results else 1
    
    # Determine which app to analyze
    app_to_analyze = args.app or 'companyportal'
    
    # Handle 'all' - analyze all known apps
    if app_to_analyze == 'all':
        print("🚀 Analyzing all applications...")
        if not args.no_notifications:
            print("📧 Email notifications enabled for version changes")
        
        all_results = []
        all_success = True
        
        for app_key, app_info in KNOWN_APPS.items():
            if args.no_notifications:
                # Use original analyzer without notifications
                analyzer = MicrosoftAppAnalyzer(
                    app_name=app_info['name'],
                    download_url=app_info['url'],
                    expected_identifier=app_info['identifier'],
                    package_type=app_info.get('type', 'single'),
                    db_path=args.db,
                    keep_downloads=args.keep_downloads,
                    url_type=app_info.get('url_type', 'direct')
                )
                results = analyzer.analyze()
                if results is None:
                    all_success = False
            else:
                # Use enhanced analyzer with notifications
                result = analyze_with_notifications(
                    app_id=app_key,
                    app_info=app_info,
                    db_path=args.db,
                    keep_downloads=args.keep_downloads
                )
                all_results.append(result)
                if not result['success']:
                    all_success = False
        
        # Summary report
        if not args.no_notifications and all_results:
            print("\n" + "=" * 60)
            print("📊 ANALYSIS SUMMARY")
            print("=" * 60)
            
            total_apps = len(all_results)
            successful_analyses = sum(1 for r in all_results if r['success'])
            version_changes = sum(
                1 for r in all_results
                if r.get('new_version') and r.get('previous_version') != r.get('new_version')
            )
            notifications_sent = sum(1 for r in all_results if r.get('notification_sent'))
            total_notification_count = sum(
                r.get('notification_result', {}).get('sent_count', 0) 
                for r in all_results if r.get('notification_result')
            )
            
            print(f"📦 Apps analyzed: {successful_analyses}/{total_apps}")
            print(f"🔄 Version changes detected: {version_changes}")
            print(f"📧 Apps with notifications sent: {notifications_sent}")
            print(f"📬 Total notifications sent: {total_notification_count}")
            
            if version_changes > 0:
                print(f"\n🎯 Version Changes:")
                for result in all_results:
                    if result.get('new_version') and result.get('previous_version') != result.get('new_version'):
                        prev = result.get('previous_version', 'Unknown')
                        new = result['new_version']
                        app_name = result['app_name']
                        print(f"  • {app_name}: {prev} → {new}")
            
            print("=" * 60)
        
        # After checking all apps, validate stored URLs
        print("\n🔍 Validating stored URLs...")
        validate_urls(args.db)
        
        return 0 if all_success else 1
    
    # Validate app exists in config
    if app_to_analyze not in KNOWN_APPS:
        print(f"✗ Unknown app: {app_to_analyze}")
        print(f"Available apps: {', '.join(KNOWN_APPS.keys())}")
        print(f"Use --list-apps to see details")
        return 1
    
    # Normal operation: download and analyze single app
    app_info = KNOWN_APPS[app_to_analyze]
    
    if args.no_notifications:
        # Use original analyzer without notifications
        analyzer = MicrosoftAppAnalyzer(
            app_name=app_info['name'],
            download_url=app_info['url'],
            expected_identifier=app_info['identifier'],
            package_type=app_info.get('type', 'single'),
            db_path=args.db,
            keep_downloads=args.keep_downloads,
            url_type=app_info.get('url_type', 'direct')
        )
        results = analyzer.analyze()
        
        if results is not None:
            if isinstance(results, dict) and results.get('unchanged'):
                return 0
            elif results:
                print("\n" + "=" * 60)
                print("✓ Analysis complete!")
                print("=" * 60)
                return 0
        
        print("\n✗ Analysis failed")
        return 1
    else:
        # Use enhanced analyzer with notifications
        result = analyze_with_notifications(
            app_id=app_to_analyze,
            app_info=app_info,
            db_path=args.db,
            keep_downloads=args.keep_downloads
        )
        
        if result['success']:
            print("\n" + "=" * 60)
            print("✓ Analysis complete!")
            if result.get('notification_sent'):
                notification_result = result.get('notification_result', {})
                sent_count = notification_result.get('sent_count', 0)
                print(f"📧 Notifications sent to {sent_count} subscribers")
            print("=" * 60)
            return 0
        else:
            print(f"\n✗ Analysis failed: {result['message']}")
            return 1


if __name__ == "__main__":
    sys.exit(main())