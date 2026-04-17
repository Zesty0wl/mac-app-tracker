#!/usr/bin/env python3
"""
Pluggable Email Notification Module

Supports Microsoft 365 Graph API and Resend as email backends.
Provider selection is configured via the admin UI or environment variables.

Usage:
    from notifications.providers import get_email_provider
    provider = get_email_provider()
    provider.send_email(['user@example.com'], 'Subject', body_html='<p>Hello</p>')

    # Backward-compatible alias still works:
    from notifications.providers import M365EmailNotifier
"""

import os
import json
import requests
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Optional, Dict, Any, List
from dotenv import load_dotenv

load_dotenv()


# ---------------------------------------------------------------------------
# Base class
# ---------------------------------------------------------------------------

class EmailProvider(ABC):
    """Abstract base class for email providers."""

    @abstractmethod
    def send_email(self, to_emails: List[str], subject: str,
                   body_html: str = None, body_text: str = None,
                   cc_emails: List[str] = None) -> Dict[str, Any]:
        """Send an email.  Returns dict with at least 'success' (bool) and 'message' (str)."""
        ...

    def send_test_email(self, recipients: List[str] = None) -> Dict[str, Any]:
        """Send a generic test email."""
        if not recipients:
            raise ValueError("No recipients specified for test email")

        import os
        brand = os.environ.get('EMAIL_BRAND_NAME', os.environ.get('SITE_NAME', 'App Tracker'))
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S UTC")
        subject = f"Email Test -- {brand}"

        html_body = f"""
        <html>
        <body style="font-family: Arial, sans-serif; margin: 20px;">
            <h2 style="color: #0078d4;">Email Provider Test</h2>
            <p>This is a test email from the <strong>{brand}</strong>.</p>
            <div style="background-color: #e6f3ff; padding: 15px; border-radius: 5px; margin: 15px 0;">
                <h3 style="margin-top: 0; color: #0078d4;">Test Details:</h3>
                <ul>
                    <li><strong>Sent at:</strong> {timestamp}</li>
                    <li><strong>Provider:</strong> {self.__class__.__name__}</li>
                </ul>
            </div>
            <hr style="margin: 20px 0;">
            <p style="color: #666; font-size: 12px;">
                This email was sent automatically by the {brand}.
            </p>
        </body>
        </html>
        """

        text_body = (
            f"Email Provider Test\n"
            f"====================\n\n"
            f"This is a test email from the App Tracker.\n\n"
            f"Sent at: {timestamp}\n"
            f"Provider: {self.__class__.__name__}\n"
        )

        return self.send_email(recipients, subject, html_body, text_body)

    def send_version_change_notification(self, app_name: str, old_version: str,
                                         new_version: str, download_url: str = None,
                                         recipients: List[str] = None) -> Dict[str, Any]:
        """Send a version-change notification email."""
        if not recipients:
            raise ValueError("No recipients specified")

        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S UTC")
        subject = f"{app_name} Version Update: {old_version} -> {new_version}"

        html_body = f"""
        <html>
        <body style="font-family: Arial, sans-serif; margin: 20px;">
            <h2 style="color: #0078d4;">App Version Update Detected</h2>
            <div style="background-color: #f3f2f1; padding: 15px; border-radius: 5px; margin: 15px 0;">
                <h3 style="margin-top: 0; color: #323130;">{app_name}</h3>
                <p style="font-size: 16px; margin: 5px 0;">
                    <strong>Previous Version:</strong> <span style="color: #d13438;">{old_version}</span><br>
                    <strong>New Version:</strong> <span style="color: #107c10;">{new_version}</span>
                </p>
                <p style="margin: 5px 0;"><strong>Detected:</strong> {timestamp}</p>
            </div>
            {f'<p><strong>Download URL:</strong> <a href="{download_url}">{download_url}</a></p>' if download_url else ''}
            <hr style="margin: 20px 0;">
            <p style="color: #666; font-size: 12px;">
                This notification was sent automatically by the App Tracker.
            </p>
        </body>
        </html>
        """

        text_body = (
            f"App Version Update Detected\n"
            f"===========================\n\n"
            f"Application: {app_name}\n"
            f"Previous Version: {old_version}\n"
            f"New Version: {new_version}\n"
            f"Detected: {timestamp}\n"
            f"{f'Download URL: {download_url}' if download_url else ''}\n\n"
            f"This notification was sent automatically by the App Tracker.\n"
        )

        return self.send_email(recipients, subject, html_body, text_body)


# ---------------------------------------------------------------------------
# Microsoft 365 Graph API provider
# ---------------------------------------------------------------------------

class M365EmailProvider(EmailProvider):
    """Send email via Microsoft 365 Graph API (client-credentials flow)."""

    def __init__(self, config: Dict[str, str] = None):
        config = config or {}
        self.client_id = config.get('m365_client_id') or os.getenv('M365_CLIENT_ID')
        self.client_secret = config.get('m365_client_secret') or os.getenv('M365_CLIENT_SECRET')
        self.tenant_id = config.get('m365_tenant_id') or os.getenv('M365_TENANT_ID', 'common')
        self.sender_email = config.get('sender_email') or os.getenv('SENDER_EMAIL')

        recipients_raw = config.get('notification_recipients') or os.getenv('NOTIFICATION_RECIPIENTS', '')
        self.default_recipients = [r.strip() for r in recipients_raw.split(',') if r.strip()]

        if not all([self.client_id, self.client_secret, self.sender_email]):
            raise ValueError(
                "Missing required M365 configuration. "
                "Set M365_CLIENT_ID, M365_CLIENT_SECRET, and SENDER_EMAIL."
            )

        self.token_url = f"https://login.microsoftonline.com/{self.tenant_id}/oauth2/v2.0/token"
        self.graph_url = os.getenv('GRAPH_API_URL', 'https://graph.microsoft.com/v1.0')
        self.access_token = None
        self.token_expires_at = None

    def _get_access_token(self) -> str:
        if self.access_token and self.token_expires_at:
            if datetime.now().timestamp() < self.token_expires_at:
                return self.access_token

        token_data = {
            'grant_type': 'client_credentials',
            'client_id': self.client_id,
            'client_secret': self.client_secret,
            'scope': 'https://graph.microsoft.com/.default'
        }

        response = requests.post(self.token_url, data=token_data, timeout=30)
        response.raise_for_status()

        token_response = response.json()
        self.access_token = token_response['access_token']
        expires_in = token_response.get('expires_in', 3600)
        self.token_expires_at = datetime.now().timestamp() + expires_in - 300

        return self.access_token

    def send_email(self, to_emails: List[str], subject: str,
                   body_html: str = None, body_text: str = None,
                   cc_emails: List[str] = None) -> Dict[str, Any]:
        try:
            token = self._get_access_token()

            if not to_emails:
                raise ValueError("At least one recipient email must be provided")
            if not body_html and not body_text:
                raise ValueError("Either body_html or body_text must be provided")

            to_recipients = [{"emailAddress": {"address": e.strip()}} for e in to_emails]
            cc_recipients = [{"emailAddress": {"address": e.strip()}} for e in (cc_emails or [])]

            message = {
                "message": {
                    "subject": subject,
                    "body": {
                        "contentType": "HTML" if body_html else "Text",
                        "content": body_html if body_html else body_text
                    },
                    "toRecipients": to_recipients
                }
            }

            if cc_recipients:
                message["message"]["ccRecipients"] = cc_recipients

            headers = {
                'Authorization': f'Bearer {token}',
                'Content-Type': 'application/json'
            }

            send_url = f"{self.graph_url}/users/{self.sender_email}/sendMail"
            response = requests.post(send_url, headers=headers,
                                     data=json.dumps(message), timeout=30)

            if response.status_code == 202:
                return {
                    "success": True,
                    "message": "Email sent successfully",
                    "recipients": to_emails,
                    "cc_recipients": cc_emails or []
                }
            else:
                error_msg = f"HTTP {response.status_code}"
                if response.text:
                    try:
                        error_data = response.json()
                        error_msg += f": {error_data.get('error', {}).get('message', response.text)}"
                    except Exception:
                        error_msg += f": {response.text}"
                raise Exception(error_msg)

        except Exception as e:
            raise Exception(f"Failed to send email: {e}")

    def send_test_email(self, recipients: List[str] = None) -> Dict[str, Any]:
        recipients = recipients or self.default_recipients
        return super().send_test_email(recipients)

    def send_version_change_notification(self, app_name: str, old_version: str,
                                         new_version: str, download_url: str = None,
                                         recipients: List[str] = None) -> Dict[str, Any]:
        recipients = recipients or self.default_recipients
        return super().send_version_change_notification(
            app_name, old_version, new_version, download_url, recipients)


# Backward-compatibility alias
M365EmailNotifier = M365EmailProvider


# ---------------------------------------------------------------------------
# Resend provider
# ---------------------------------------------------------------------------

class ResendEmailProvider(EmailProvider):
    """Send email via the Resend API."""

    def __init__(self, config: Dict[str, str] = None):
        config = config or {}
        self.api_key = config.get('resend_api_key') or os.getenv('RESEND_API_KEY')
        self.from_email = config.get('resend_from_email') or os.getenv('RESEND_FROM_EMAIL')

        recipients_raw = config.get('notification_recipients') or os.getenv('NOTIFICATION_RECIPIENTS', '')
        self.default_recipients = [r.strip() for r in recipients_raw.split(',') if r.strip()]

        if not self.api_key:
            raise ValueError("Missing RESEND_API_KEY")
        if not self.from_email:
            raise ValueError("Missing RESEND_FROM_EMAIL")

    def send_email(self, to_emails: List[str], subject: str,
                   body_html: str = None, body_text: str = None,
                   cc_emails: List[str] = None) -> Dict[str, Any]:
        try:
            import resend as resend_lib
            resend_lib.api_key = self.api_key

            if not to_emails:
                raise ValueError("At least one recipient email must be provided")
            if not body_html and not body_text:
                raise ValueError("Either body_html or body_text must be provided")

            params: Dict[str, Any] = {
                "from": self.from_email,
                "to": [e.strip() for e in to_emails],
                "subject": subject,
            }
            if body_html:
                params["html"] = body_html
            if body_text:
                params["text"] = body_text
            if cc_emails:
                params["cc"] = [e.strip() for e in cc_emails]

            resend_lib.Emails.send(params)

            return {
                "success": True,
                "message": "Email sent successfully via Resend",
                "recipients": to_emails,
                "cc_recipients": cc_emails or []
            }

        except Exception as e:
            raise Exception(f"Failed to send email via Resend: {e}")

    def send_test_email(self, recipients: List[str] = None) -> Dict[str, Any]:
        recipients = recipients or self.default_recipients
        return super().send_test_email(recipients)

    def send_version_change_notification(self, app_name: str, old_version: str,
                                         new_version: str, download_url: str = None,
                                         recipients: List[str] = None) -> Dict[str, Any]:
        recipients = recipients or self.default_recipients
        return super().send_version_change_notification(
            app_name, old_version, new_version, download_url, recipients)


# ---------------------------------------------------------------------------
# Noop fallback
# ---------------------------------------------------------------------------

class NoopEmailProvider(EmailProvider):
    """Fallback provider when no email service is configured."""

    def send_email(self, to_emails: List[str], subject: str,
                   body_html: str = None, body_text: str = None,
                   cc_emails: List[str] = None) -> Dict[str, Any]:
        print(f"[NoopEmailProvider] Would send '{subject}' to {to_emails}")
        return {
            "success": False,
            "message": "No email provider configured"
        }


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def get_email_provider(provider_type: str = None,
                       config: Dict[str, str] = None) -> EmailProvider:
    """
    Return an EmailProvider instance.

    Resolution order:
      1. Explicit provider_type / config arguments
      2. Admin-DB email_settings table
      3. Environment variables (M365_CLIENT_ID or RESEND_API_KEY)
      4. NoopEmailProvider (logs but does not send)
    """
    if not provider_type:
        # Try admin DB first
        try:
            import admin.database as adb
            provider_type = adb.get_email_setting('provider')
            if provider_type and not config:
                config = adb.get_all_email_settings()
        except Exception:
            pass

    if not provider_type:
        # Infer from env vars
        if os.getenv('M365_CLIENT_ID'):
            provider_type = 'm365'
        elif os.getenv('RESEND_API_KEY'):
            provider_type = 'resend'

    try:
        if provider_type == 'm365':
            return M365EmailProvider(config)
        elif provider_type == 'resend':
            return ResendEmailProvider(config)
    except Exception as e:
        print(f"[get_email_provider] Failed to initialise '{provider_type}': {e}")

    return NoopEmailProvider()


# ---------------------------------------------------------------------------
# CLI entry-point
# ---------------------------------------------------------------------------

def main():
    """Test email functionality with the configured provider."""
    print("Email Notification Test")
    print("=" * 50)

    try:
        provider = get_email_provider()
        if isinstance(provider, NoopEmailProvider):
            print("No email provider configured. Set M365 or Resend credentials.")
            return 1

        print(f"Provider: {provider.__class__.__name__}")
        recipients = os.getenv('NOTIFICATION_RECIPIENTS', '').split(',')
        recipients = [r.strip() for r in recipients if r.strip()]
        if not recipients:
            print("No NOTIFICATION_RECIPIENTS set.")
            return 1

        result = provider.send_test_email(recipients)
        print(f"Result: {result['message']}")
        return 0

    except Exception as e:
        print(f"Error: {e}")
        return 1


if __name__ == "__main__":
    exit(main())