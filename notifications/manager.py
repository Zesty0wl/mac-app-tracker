#!/usr/bin/env python3
"""
High-level subscription management.

Coordinates between the subscription database and the pluggable email
provider to handle sign-up, confirmation, notification dispatch, and
unsubscribe flows.
"""

import os
import re
from datetime import datetime
from typing import List, Dict, Any, Optional, Tuple
from urllib.parse import urljoin

from notifications.database import SubscriptionDatabase
from notifications.providers import get_email_provider


class SubscriptionManager:
    """High-level subscription management"""
    
    def __init__(self, base_url: str = None):
        """
        Initialize the subscription manager
        
        Args:
            base_url: Base URL for confirmation/unsubscribe links.
                      If not provided, reads site_url from admin email
                      settings, then falls back to SITE_URL env var.
        """
        self.db = SubscriptionDatabase()
        self.email_notifier = get_email_provider()
        
        if base_url is None:
            try:
                from admin.database import get_email_setting
                site_url = get_email_setting('site_url')
            except Exception:
                site_url = None
            self.base_url = (site_url or os.environ.get('SITE_URL', 'https://localhost')).rstrip('/')
        else:
            self.base_url = base_url.rstrip('/')
        
        # App configuration mapping
        self.app_config = {
            'companyportal': {
                'name': 'Microsoft Company Portal',
                'page_url': f'{self.base_url}/microsoft-company-portal-macos',
                'description': 'Microsoft Intune Company Portal for macOS'
            },
            'defender': {
                'name': 'Microsoft Defender',
                'page_url': f'{self.base_url}/microsoft-defender-macos',
                'description': 'Microsoft Defender for Endpoint on macOS'
            },
            'edge': {
                'name': 'Microsoft Edge',
                'page_url': f'{self.base_url}/microsoft-edge-macos',
                'description': 'Microsoft Edge web browser for macOS'
            },
            'office': {
                'name': 'Microsoft Office Suite',
                'page_url': f'{self.base_url}/microsoft-office-suite-macos',
                'description': 'Microsoft 365 and Office applications'
            },
            'autoupdate': {
                'name': 'Microsoft AutoUpdate',
                'page_url': f'{self.base_url}/microsoft-autoupdate-macos',
                'description': 'Microsoft AutoUpdate for Mac'
            }
        }
    
    def get_available_apps(self) -> Dict[str, Dict[str, str]]:
        """Return tracked apps available for subscription from the database."""
        try:
            from tracker.config import load_apps_config
            apps = load_apps_config()
            return {
                app_id: {
                    'name': info.get('name', app_id),
                    'description': info.get('description', f"{info.get('name', app_id)} for macOS"),
                }
                for app_id, info in apps.items()
            }
        except Exception as e:
            print(f"Error loading apps configuration: {e}")
            return {}
    
    def validate_email(self, email: str) -> Tuple[bool, str]:
        """
        Validate email address
        
        Args:
            email: Email address to validate
            
        Returns:
            Tuple of (is_valid, error_message)
        """
        email = email.strip().lower()
        
        if not email:
            return False, "Email address is required"
        
        # Basic email validation
        if not re.match(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$', email):
            return False, "Invalid email address format"
        
        if len(email) > 254:
            return False, "Email address is too long"
        
        return True, ""
    
    def validate_app_selection(self, app_ids: List[str]) -> Tuple[bool, str, List[str]]:
        """
        Validate app selection
        
        Args:
            app_ids: List of app IDs to validate
            
        Returns:
            Tuple of (is_valid, error_message, filtered_app_ids)
        """
        if not app_ids:
            # Empty list means subscribe to all apps
            return True, "", []
        
        # Use the same app list as the subscription form
        available_apps = set(self.get_available_apps().keys())
        invalid_apps = [app_id for app_id in app_ids if app_id not in available_apps]
        
        if invalid_apps:
            return False, f"Invalid app(s): {', '.join(invalid_apps)}", []
        
        return True, "", app_ids
    
    def subscribe(self, email: str, app_ids: List[str] = None) -> Tuple[bool, str]:
        """
        Subscribe an email to app notifications
        
        Args:
            email: Email address
            app_ids: List of app IDs (empty = all apps)
            
        Returns:
            Tuple of (success, message)
        """
        try:
            # Validate email
            valid, error = self.validate_email(email)
            if not valid:
                return False, error
            
            # Validate app selection
            valid, error, filtered_apps = self.validate_app_selection(app_ids or [])
            if not valid:
                return False, error
            
            # Add to database (this handles both new and existing subscribers)
            subscriber_id, token = self.db.add_subscriber(email, filtered_apps)
            
            # Always send confirmation email (whether new or existing subscriber)
            success = self._send_confirmation_email(email, token, filtered_apps)
            
            if success:
                return True, "Subscription request submitted! Please check your email for confirmation."
            else:
                return False, "Failed to send confirmation email. Please try again later."
                
        except Exception as e:
            print(f"Error in subscribe: {e}")
            return False, "An error occurred while processing your subscription."
    
    def _send_confirmation_email(self, email: str, token: str, app_ids: List[str]) -> bool:
        """
        Send confirmation email
        
        Args:
            email: Email address
            token: Confirmation token
            app_ids: List of subscribed app IDs
            
        Returns:
            True if email sent successfully
        """
        try:
            confirmation_url = f"{self.base_url}/confirm-subscription?token={token}"
            
            # Determine subscription description
            if not app_ids:
                subscription_desc = "all Microsoft Mac applications"
                app_list_html = "<li>All available Microsoft Mac applications (current and future)</li>"
                app_list_text = "- All available Microsoft Mac applications (current and future)"
            else:
                # Get app names from the dynamic config
                available_apps = self.get_available_apps()
                app_names = [available_apps.get(app_id, {}).get('name', app_id) for app_id in app_ids]
                subscription_desc = f"{len(app_names)} selected application(s)"
                app_list_html = "".join([f"<li>{name}</li>" for name in app_names])
                app_list_text = "\n".join([f"- {name}" for name in app_names])
            
            brand = os.environ.get('EMAIL_BRAND_NAME', os.environ.get('SITE_NAME', 'Mac Apps Version Tracker'))
            subject = f"Confirm your {brand} Notifications"

            html_body = f"""
            <html>
            <body style="font-family: Arial, sans-serif; margin: 20px; line-height: 1.6;">
                <div style="max-width: 600px; margin: 0 auto;">
                    <h2 style="color: #0078d4; border-bottom: 2px solid #0078d4; padding-bottom: 10px;">
                        {brand} Notifications
                    </h2>
                    
                    <p>Hello!</p>
                    
                    <p>Thank you for subscribing to receive email notifications about application version updates. 
                    You've requested notifications for <strong>{subscription_desc}</strong>.</p>
                    
                    <div style="background-color: #f3f2f1; padding: 15px; border-radius: 5px; margin: 20px 0;">
                        <h3 style="margin-top: 0; color: #323130;">Your Subscription:</h3>
                        <ul>
                            {app_list_html}
                        </ul>
                    </div>
                    
                    <div style="background-color: #e6f3ff; padding: 20px; border-radius: 5px; margin: 20px 0; text-align: center;">
                        <h3 style="margin-top: 0; color: #0078d4;">Confirm Your Subscription</h3>
                        <p>Click the button below to confirm your subscription:</p>
                        <a href="{confirmation_url}" 
                           style="display: inline-block; background-color: #0078d4; color: white; 
                                  padding: 12px 30px; text-decoration: none; border-radius: 5px; 
                                  font-weight: bold; margin: 10px 0;">
                            Confirm Subscription
                        </a>
                        <p style="font-size: 12px; color: #666; margin-top: 15px;">
                            Link expires in 24 hours
                        </p>
                    </div>
                    <div style="border-top: 1px solid #ddd; padding-top: 20px; margin-top: 30px;">
                        <p style="font-size: 12px; color: #666;">
                            If you didn't request this subscription, you can safely ignore this email.<br>
                            The subscription will not be activated without confirmation.
                        </p>
                        <p style="font-size: 12px; color: #666;">
                            This email was sent by the {brand} at 
                            <a href="{self.base_url}">{self.base_url}</a>
                        </p>
                    </div>
                </div>
            </body>
            </html>
            """
            
            text_body = f"""
            {brand} Notifications - Confirmation Required
            ==================================================================
            
            Hello!
            
            Thank you for subscribing to receive email notifications about Microsoft Mac application 
            version updates. You've requested notifications for {subscription_desc}.
            
            Your Subscription:
            {app_list_text}
            
            CONFIRM YOUR SUBSCRIPTION:
            Please click this link to confirm your subscription:
            {confirmation_url}
            
            (Link expires in 24 hours)
            
            If you didn't request this subscription, you can safely ignore this email.
            The subscription will not be activated without confirmation.
            
            This email was sent by the {brand} at {self.base_url}
            """
            
            result = self.email_notifier.send_email([email], subject, html_body, text_body)
            return result['success']
            
        except Exception as e:
            print(f"Error sending confirmation email: {e}")
            return False
    
    def confirm_subscription(self, token: str) -> Tuple[bool, str]:
        """
        Confirm a subscription using token
        
        Args:
            token: Confirmation token
            
        Returns:
            Tuple of (success, message)
        """
        try:
            success = self.db.confirm_subscription(token)
            
            if success:
                return True, "Your subscription has been confirmed successfully! You will now receive version update notifications."
            else:
                return False, "Invalid or expired confirmation link. Please try subscribing again."
                
        except Exception as e:
            print(f"Error confirming subscription: {e}")
            return False, "An error occurred while confirming your subscription."
    
    def unsubscribe(self, token: str) -> Tuple[bool, str]:
        """
        Unsubscribe using token
        
        Args:
            token: Unsubscribe token
            
        Returns:
            Tuple of (success, message)
        """
        try:
            success = self.db.unsubscribe(token)
            
            if success:
                return True, "You have been successfully unsubscribed from all notifications."
            else:
                return False, "Invalid or expired unsubscribe link."
                
        except Exception as e:
            print(f"Error unsubscribing: {e}")
            return False, "An error occurred while processing your unsubscribe request."
    
    def send_version_notification(self, app_id: str, app_name: str, old_version: str, 
                                new_version: str, download_url: str = None, 
                                detection_time: datetime = None, version_details: Dict[str, Any] = None) -> Dict[str, Any]:
        """
        Send version update notification to subscribers
        
        Args:
            app_id: Application ID
            app_name: Application display name
            old_version: Previous version
            new_version: New version
            download_url: Download URL (optional)
            detection_time: When the change was detected (optional)
            version_details: Dictionary with size_bytes, checksum_sha256, components, etc.
            
        Returns:
            Dictionary with notification results
        """
        try:
            # Get subscribers for this app
            subscribers = self.db.get_subscribers_for_app(app_id)
            
            if not subscribers:
                return {
                    'success': True,
                    'message': 'No subscribers for this app',
                    'sent_count': 0,
                    'failed_count': 0
                }
            
            detection_time = detection_time or datetime.now()
            
            # Get app info from dynamic config, fallback to provided data
            available_apps = self.get_available_apps()
            app_info = available_apps.get(app_id, {})
            
            # If not found in config, create basic app info
            if not app_info:
                app_info = {
                    'id': app_id,
                    'name': app_name,
                    'description': f'{app_name} application'
                }
            
            # Ensure we have required fields
            if 'id' not in app_info:
                app_info['id'] = app_id
            if 'name' not in app_info:
                app_info['name'] = app_name
            
            # Add page URL (link to main tracker page)
            app_info['page_url'] = f'{self.base_url}/app-tracker'
            
            sent_count = 0
            failed_count = 0
            
            for email in subscribers:
                try:
                    # Generate unsubscribe token for this email
                    unsubscribe_token = self.db.generate_unsubscribe_token(email)
                    unsubscribe_url = f"{self.base_url}/app-tracker/unsubscribe?token={unsubscribe_token}" if unsubscribe_token else None
                    
                    # Send notification
                    success = self._send_version_notification_email(
                        email, app_info, old_version, new_version, 
                        download_url, detection_time, unsubscribe_url, version_details
                    )
                    
                    if success:
                        sent_count += 1
                        self.db.update_notification_sent(email)
                    else:
                        failed_count += 1
                        
                except Exception as e:
                    print(f"Error sending notification to {email}: {e}")
                    failed_count += 1
            
            return {
                'success': True,
                'message': f'Notifications sent to {sent_count} subscribers',
                'sent_count': sent_count,
                'failed_count': failed_count,
                'total_subscribers': len(subscribers)
            }
            
        except Exception as e:
            print(f"Error sending version notifications: {e}")
            return {
                'success': False,
                'message': f'Error sending notifications: {e}',
                'sent_count': 0,
                'failed_count': 0
            }
    
    def _send_version_notification_email(self, email: str, app_info: Dict[str, str], 
                                       old_version: str, new_version: str, 
                                       download_url: str, detection_time: datetime,
                                       unsubscribe_url: str, version_details: Dict[str, Any] = None) -> bool:
        """Send version notification email to a single subscriber"""
        
        try:
            app_name = app_info['name']
            app_id = app_info['id']
            # Add app_id parameter to URL to pre-select the app
            app_page_url = f"{self.base_url}/app-tracker?app={app_id}"
            detection_str = detection_time.strftime("%Y-%m-%d at %H:%M UTC")

            # Intune Agent (com.microsoft.intuneMDMAgent) cannot include a download link
            intune_agent_ids = {'intuneagent'}
            can_include_download = bool(download_url) and app_id not in intune_agent_ids
            
            # Helper function to format file size
            def format_size(bytes_val):
                if not bytes_val:
                    return "Unknown"
                for unit in ['bytes', 'KB', 'MB', 'GB']:
                    if bytes_val < 1024.0:
                        return f"{bytes_val:.2f} {unit}"
                    bytes_val /= 1024.0
                return f"{bytes_val:.2f} TB"
            
            subject = f"{app_name} Updated: v{old_version} → v{new_version}"
            
            # Build version details table HTML
            details_html = ""
            if version_details:
                size_str = format_size(version_details.get('size_bytes', 0))
                sha256 = version_details.get('checksum_sha256', 'N/A')
                sha256_short = sha256[:16] + '...' if sha256 != 'N/A' else 'N/A'
                components = version_details.get('components', [])
                component_count = len(components)
                
                details_html = f"""
                    <div style="background-color: #ffffff; padding: 15px; border: 1px solid #e1e1e1; border-radius: 5px; margin: 20px 0;">
                        <h4 style="margin-top: 0; color: #323130;">Version Details</h4>
                        <table style="width: 100%; border-collapse: collapse;">
                            <tr style="border-bottom: 1px solid #e1e1e1;">
                                <td style="padding: 8px; font-weight: bold; width: 30%;">Size:</td>
                                <td style="padding: 8px;">{size_str}</td>
                            </tr>
                            <tr style="border-bottom: 1px solid #e1e1e1;">
                                <td style="padding: 8px; font-weight: bold;">SHA256:</td>
                                <td style="padding: 8px; font-family: monospace; font-size: 12px;">{sha256_short}</td>
                            </tr>
                            <tr>
                                <td style="padding: 8px; font-weight: bold;">Components:</td>
                                <td style="padding: 8px;">{component_count + 1}</td>
                            </tr>
                        </table>
                """
                
                # Add component details if available
                if components:
                    details_html += """
                        <div style="margin-top: 15px;">
                            <h5 style="margin-bottom: 10px; color: #605e5c;">Included Components:</h5>
                            <div style="font-size: 13px;">
                    """
                    # Add main app first from version_details
                    main_bundle_id = version_details.get('bundle_id', 'N/A')
                    main_version = new_version
                    main_app_path = version_details.get('app_path', 'N/A')
                    if main_app_path and main_app_path != 'N/A':
                        main_app_display = main_app_path.split('/')[-1].replace('.app', '')
                    else:
                        main_app_display = app_name
                    
                    details_html += f"""
                        <div style="padding: 8px 0; border-bottom: 1px solid #f5f5f7;">
                            <div style="font-weight: 500; color: #1d1d1f; margin-bottom: 4px;">{main_app_display}</div>
                            <div style="padding-left: 16px; font-size: 11px; color: #6b7280;">
                                <div style="font-family: monospace; margin-bottom: 2px; word-break: break-all;">{main_bundle_id}</div>
                                <div>Version: {main_version}</div>
                            </div>
                        </div>
                    """
                    
                    # Add other components
                    for comp in components[:10]:  # Show up to 10 components
                        comp_bundle_id = comp.get('bundle_id') or 'N/A'
                        comp_version = comp.get('version') or 'N/A'
                        comp_app_path = comp.get('app_path') or ''
                        
                        # Get display name from app_path if available
                        if comp_app_path and comp_app_path.strip():
                            comp_display_name = comp_app_path.split('/')[-1].replace('.app', '')
                        else:
                            # Fallback to package identifier
                            comp_display_name = comp.get('package_identifier', '').split('.')[-1].replace('_', ' ').title()
                        
                        details_html += f"""
                        <div style="padding: 8px 0; border-bottom: 1px solid #f5f5f7;">
                            <div style="color: #1d1d1f; margin-bottom: 4px;">{comp_display_name}</div>
                            <div style="padding-left: 16px; font-size: 11px; color: #6b7280;">
                                <div style="font-family: monospace; margin-bottom: 2px; word-break: break-all;">{comp_bundle_id}</div>
                                <div>Version: {comp_version}</div>
                            </div>
                        </div>
                        """
                    
                    if len(components) > 10:
                        details_html += f"""
                        <div style="padding: 8px 0; font-style: italic; color: #6b7280;">
                            ...and {len(components) - 10} more
                        </div>
                        """
                    
                    details_html += "</div></div>"
                
                details_html += "</div>"
            
            brand = os.environ.get('EMAIL_BRAND_NAME', os.environ.get('SITE_NAME', 'Mac Apps Version Tracker'))
            html_body = f"""
            <html>
            <head>
                <style>
                    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; margin: 0; padding: 0; background-color: #f5f5f7; }}
                    .container {{ max-width: 600px; margin: 40px auto; background-color: #ffffff; }}
                    .header {{ background-color: #ffffff; padding: 20px 30px; border-bottom: 1px solid #d2d2d7; }}
                    .header h1 {{ margin: 0; font-size: 20px; font-weight: 600; color: #1d1d1f; }}
                    .content {{ padding: 30px; }}
                    .app-name {{ font-size: 24px; font-weight: 600; color: #1d1d1f; margin-bottom: 20px; }}
                    .version-box {{ background-color: #f5f5f7; padding: 20px; margin: 20px 0; border-radius: 8px; }}
                    .version-row {{ display: table; width: 100%; margin: 8px 0; }}
                    .version-label {{ display: table-cell; width: 40%; color: #86868b; font-size: 14px; }}
                    .version-value {{ display: table-cell; color: #1d1d1f; font-size: 14px; font-weight: 500; }}
                    .version-new {{ color: #0071e3; }}
                    .details-box {{ border: 1px solid #d2d2d7; padding: 20px; margin: 20px 0; border-radius: 8px; }}
                    .details-box h3 {{ margin: 0 0 15px 0; font-size: 16px; font-weight: 600; color: #1d1d1f; }}
                    .detail-row {{ margin: 10px 0; font-size: 14px; color: #1d1d1f; }}
                    .detail-label {{ color: #86868b; display: inline-block; width: 100px; }}
                    .component-list {{ margin: 15px 0 0 0; padding: 0; list-style: none; }}
                    .component-list li {{ padding: 5px 0; font-size: 14px; color: #1d1d1f; border-bottom: 1px solid #f5f5f7; }}
                    .component-list li:last-child {{ border-bottom: none; }}
                    .link-section {{ margin: 30px 0; padding: 20px; background-color: #f5f5f7; border-radius: 8px; text-align: center; }}
                    .link-section a {{ color: #0071e3; text-decoration: none; font-size: 14px; margin: 0 15px; }}
                    .link-section a:hover {{ text-decoration: underline; }}
                    .footer {{ padding: 20px 30px; border-top: 1px solid #d2d2d7; font-size: 12px; color: #86868b; }}
                    .footer a {{ color: #0071e3; text-decoration: none; }}
                </style>
            </head>
            <body>
                <div class="container">
                    <div class="header">
                        <h1>{brand}</h1>
                    </div>
                    
                    <div class="content">
                        <div class="app-name">{app_name}</div>
                        
                        <div class="version-box">
                            <div class="version-row">
                                <span class="version-label">Previous Version</span>
                                <span class="version-value">{old_version}</span>
                            </div>
                            <div class="version-row">
                                <span class="version-label">New Version</span>
                                <span class="version-value version-new">{new_version}</span>
                            </div>
                            <div class="version-row">
                                <span class="version-label">Detected</span>
                                <span class="version-value">{detection_str}</span>
                            </div>
                        </div>
                        
                        {details_html if details_html else ''}
                        
                        <div class="link-section">
                            <a href="{app_page_url}">View Details</a>
                            {f'<a href="{download_url}">Download Package</a>' if can_include_download else ''}
                        </div>
                    </div>
                    
                    <div class="footer">
                        <p>This notification was sent automatically by the {brand}.<br>
                        You are receiving this because you subscribed to {app_name} updates.</p>
                        {f'<p><a href="{unsubscribe_url}">Unsubscribe from all notifications</a></p>' if unsubscribe_url else ''}
                        <p>Visit <a href="{self.base_url}/app-tracker">{self.base_url}/app-tracker</a> for more information.</p>
                    </div>
                </div>
            </body>
            </html>
            """
            
            # Build text body with details
            details_text = ""
            if version_details:
                size_str = format_size(version_details.get('size_bytes', 0))
                sha256 = version_details.get('checksum_sha256', 'N/A')
                components = version_details.get('components', [])
                
                details_text = f"""
            Version Details:
            ----------------
            Size: {size_str}
            SHA256: {sha256}
            Components: {len(components) + 1}
            """
                
                if components:
                    main_bundle_id = version_details.get('bundle_id') or 'N/A'
                    main_version = new_version
                    main_app_path = version_details.get('app_path') or ''
                    if main_app_path and main_app_path.strip():
                        main_app_display = main_app_path.split('/')[-1].replace('.app', '')
                    else:
                        main_app_display = app_name
                    
                    details_text += f"\nIncluded Components:\n"
                    details_text += f"  {main_app_display:<30} {main_bundle_id:<35} v{main_version}\n"
                    
                    for comp in components[:10]:
                        comp_bundle_id = comp.get('bundle_id') or 'N/A'
                        comp_version = comp.get('version') or 'N/A'
                        comp_app_path = comp.get('app_path') or ''
                        
                        if comp_app_path and comp_app_path.strip():
                            comp_display_name = comp_app_path.split('/')[-1].replace('.app', '')
                        else:
                            comp_display_name = comp.get('package_identifier', '').split('.')[-1].replace('_', ' ').title()
                        
                        details_text += f"  {comp_display_name:<30} {comp_bundle_id:<35} v{comp_version}\n"
                    
                    if len(components) > 10:
                        details_text += f"  ...and {len(components) - 10} more\n"
            
            text_body = f"""
{brand}

{app_name}
{'=' * len(app_name)}

Previous Version: {old_version}
New Version: {new_version}
Detected: {detection_str}
{details_text}
View Details: {app_page_url}
{f'Download Package: {download_url}' if can_include_download else ''}

This notification was sent automatically by the {brand}.
You are receiving this because you subscribed to {app_name} updates.

{f'Unsubscribe from all notifications: {unsubscribe_url}' if unsubscribe_url else ''}

Visit {self.base_url}/app-tracker for more information.
            """
            
            result = self.email_notifier.send_email([email], subject, html_body, text_body)
            return result['success']
            
        except Exception as e:
            print(f"Error sending version notification email: {e}")
            return False
    
    def get_subscription_stats(self) -> Dict[str, Any]:
        """Get subscription statistics"""
        return self.db.get_stats()
    
    def cleanup_expired_tokens(self):
        """Clean up expired tokens"""
        self.db.cleanup_expired_tokens()


def main():
    """Test the subscription manager"""
    print("🧪 Testing Subscription Manager")
    print("=" * 50)
    
    # Initialize manager
    manager = SubscriptionManager()
    
    # Test subscription
    print("Testing subscription...")
    success, message = manager.subscribe("test@example.com", ["companyportal", "defender"])
    print(f"   Result: {success}")
    print(f"   Message: {message}")
    
    # Get stats
    print("\n Subscription statistics:")
    stats = manager.get_subscription_stats()
    for key, value in stats.items():
        print(f"   {key}: {value}")
    
    print("\n Subscription manager test completed!")


if __name__ == "__main__":
    main()