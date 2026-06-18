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
import base64
import requests
from abc import ABC, abstractmethod
from datetime import datetime
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from typing import Optional, Dict, Any, List
from dotenv import load_dotenv

load_dotenv()


def _resolve_sender_identity(config: Dict[str, str], sender_email: str):
    """Resolve the friendly From-name, Reply-To and unsubscribe mailto shared by
    every provider, from config (admin DB) first then environment.

    A recognisable From-name and a working Reply-To are basic deliverability
    hygiene — mail from a bare address with no display name looks more like spam
    and gives recipients nowhere to reply.
    """
    config = config or {}
    brand = os.environ.get('EMAIL_BRAND_NAME', os.environ.get('SITE_NAME', 'Mac Apps Version Tracker'))
    from_name = (config.get('email_from_name') or os.environ.get('EMAIL_FROM_NAME') or brand).strip()
    reply_to = (config.get('email_reply_to') or os.environ.get('EMAIL_REPLY_TO')
                or os.environ.get('CONTACT_EMAIL') or '').strip() or None
    unsub_mailto = (config.get('email_unsubscribe_mailto')
                    or os.environ.get('EMAIL_UNSUBSCRIBE_MAILTO')
                    or os.environ.get('CONTACT_EMAIL') or '').strip() or None
    return from_name, reply_to, unsub_mailto


def _list_unsubscribe_header(url: str, mailto: str = None):
    """Build the (List-Unsubscribe, List-Unsubscribe-Post) header values for a
    one-click unsubscribe URL, per RFC 2369 / RFC 8058.

    Gmail and Yahoo's bulk-sender rules expect a List-Unsubscribe header on
    subscribed mail, and reward a working one-click variant. The https URL must
    accept POST for the one-click flow to function.
    """
    if not url:
        return None, None
    parts = [f"<{url}>"]
    if mailto:
        parts.append(f"<mailto:{mailto}?subject=unsubscribe>")
    return ", ".join(parts), "List-Unsubscribe=One-Click"


# ---------------------------------------------------------------------------
# Base class
# ---------------------------------------------------------------------------

class EmailProvider(ABC):
    """Abstract base class for email providers."""

    @abstractmethod
    def send_email(self, to_emails: List[str], subject: str,
                   body_html: str = None, body_text: str = None,
                   cc_emails: List[str] = None, *,
                   from_name: str = None, reply_to: str = None,
                   list_unsubscribe_url: str = None) -> Dict[str, Any]:
        """Send an email.  Returns dict with at least 'success' (bool) and 'message' (str).

        Optional deliverability args:
            from_name: friendly display name for the From address.
            reply_to: Reply-To address.
            list_unsubscribe_url: https URL for one-click unsubscribe; when set,
                List-Unsubscribe and List-Unsubscribe-Post headers are added.
        """
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

        self.from_name, self.reply_to, self.unsub_mailto = _resolve_sender_identity(config, self.sender_email)

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

    def _build_mime(self, to_emails: List[str], subject: str,
                    body_html: str, body_text: str, cc_emails: List[str],
                    from_name: str, reply_to: str,
                    list_unsubscribe_url: str) -> bytes:
        """Assemble an RFC 5322 MIME message.

        We send MIME rather than Graph's JSON message because Graph's
        internetMessageHeaders only accepts ``X-`` prefixed headers, which makes
        a standards-compliant ``List-Unsubscribe`` / ``List-Unsubscribe-Post``
        impossible. MIME also lets us set a friendly From-name, a Reply-To and a
        proper multipart/alternative (plain-text + HTML) body — all of which
        help inbox placement.
        """
        msg = EmailMessage()
        msg['Subject'] = subject
        msg['From'] = formataddr((from_name or self.sender_email, self.sender_email))
        msg['To'] = ', '.join(e.strip() for e in to_emails)
        if cc_emails:
            msg['Cc'] = ', '.join(e.strip() for e in cc_emails)
        if reply_to:
            msg['Reply-To'] = reply_to
        msg['Date'] = formatdate(localtime=True)
        msg['Message-ID'] = make_msgid(domain=self.sender_email.split('@')[-1])

        lu, lu_post = _list_unsubscribe_header(list_unsubscribe_url, self.unsub_mailto)
        if lu:
            msg['List-Unsubscribe'] = lu
            msg['List-Unsubscribe-Post'] = lu_post

        # multipart/alternative: text first, HTML preferred.
        # Force base64 transfer-encoding: quoted-printable soft-wraps long lines
        # with '=' breaks, and Graph/Outlook can mangle those into visible
        # artefacts (e.g. "Microsoft =ac applications"). base64 has no such
        # line-break ambiguity.
        if body_text and body_html:
            msg.set_content(body_text, cte='base64')
            msg.add_alternative(body_html, subtype='html', cte='base64')
        elif body_html:
            msg.set_content('This message requires an HTML-capable email client.', cte='base64')
            msg.add_alternative(body_html, subtype='html', cte='base64')
        else:
            msg.set_content(body_text or '', cte='base64')

        return msg.as_bytes()

    def send_email(self, to_emails: List[str], subject: str,
                   body_html: str = None, body_text: str = None,
                   cc_emails: List[str] = None, *,
                   from_name: str = None, reply_to: str = None,
                   list_unsubscribe_url: str = None) -> Dict[str, Any]:
        try:
            token = self._get_access_token()

            if not to_emails:
                raise ValueError("At least one recipient email must be provided")
            if not body_html and not body_text:
                raise ValueError("Either body_html or body_text must be provided")

            mime_bytes = self._build_mime(
                to_emails, subject, body_html, body_text, cc_emails,
                from_name or self.from_name,
                reply_to if reply_to is not None else self.reply_to,
                list_unsubscribe_url,
            )

            headers = {
                'Authorization': f'Bearer {token}',
                'Content-Type': 'text/plain',  # base64-encoded MIME payload
            }

            send_url = f"{self.graph_url}/users/{self.sender_email}/sendMail"
            response = requests.post(send_url, headers=headers,
                                     data=base64.b64encode(mime_bytes), timeout=30)

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

    # ------------------------------------------------------------------
    # Mailbox access (read / delete)
    #
    # Used for bounce processing and mailbox housekeeping. Requires the
    # Mail.ReadWrite application permission on the app registration. Scope
    # the app to the sender mailbox only (Exchange Application Access Policy
    # or RBAC for Applications) to keep this least-privilege.
    # ------------------------------------------------------------------

    def _graph_request(self, method: str, path: str, *, params: Dict[str, str] = None,
                       json_body: Dict[str, Any] = None,
                       extra_headers: Dict[str, str] = None) -> requests.Response:
        """Issue an authenticated Graph request.

        `path` is appended to the configured Graph base URL and must start
        with '/'.
        """
        token = self._get_access_token()
        headers = {'Authorization': f'Bearer {token}'}
        if json_body is not None:
            headers['Content-Type'] = 'application/json'
        if extra_headers:
            headers.update(extra_headers)
        url = f"{self.graph_url}{path}"
        return requests.request(
            method, url, headers=headers, params=params,
            data=json.dumps(json_body) if json_body is not None else None,
            timeout=30,
        )

    def read_messages(self, folder: str = 'inbox', select: List[str] = None,
                      page_size: int = 50, max_messages: Optional[int] = None,
                      body_as_text: bool = True) -> List[Dict[str, Any]]:
        """Return messages from a mailbox folder, following pagination.

        Args:
            folder: well-known folder name (e.g. 'inbox', 'sentitems').
            select: message properties to request.
            page_size: messages per page ($top).
            max_messages: stop after this many messages (None = all).
            body_as_text: request plain-text bodies for easier parsing.
        """
        select = select or ['id', 'subject', 'from', 'receivedDateTime', 'bodyPreview', 'body']
        params = {
            '$top': str(page_size),
            '$orderby': 'receivedDateTime desc',
            '$select': ','.join(select),
        }
        extra_headers = {}
        if body_as_text:
            extra_headers['Prefer'] = 'outlook.body-content-type="text"'

        path = f"/users/{self.sender_email}/mailFolders/{folder}/messages"
        messages: List[Dict[str, Any]] = []
        resp = self._graph_request('GET', path, params=params, extra_headers=extra_headers)

        while True:
            if resp.status_code != 200:
                raise Exception(f"Graph read failed (HTTP {resp.status_code}): {resp.text[:300]}")
            data = resp.json()
            messages.extend(data.get('value', []))

            if max_messages is not None and len(messages) >= max_messages:
                return messages[:max_messages]

            next_link = data.get('@odata.nextLink')
            if not next_link:
                return messages

            # nextLink is an absolute URL with the query baked in.
            token = self._get_access_token()
            headers = {'Authorization': f'Bearer {token}'}
            if body_as_text:
                headers['Prefer'] = 'outlook.body-content-type="text"'
            resp = requests.get(next_link, headers=headers, timeout=30)

    def delete_message(self, message_id: str, permanent: bool = False) -> bool:
        """Delete a single message.

        Soft delete (default) moves it to Deleted Items; permanent delete
        places it in the recoverable-items Purges folder. Both require
        Mail.ReadWrite.
        """
        if permanent:
            path = f"/users/{self.sender_email}/messages/{message_id}/permanentDelete"
            resp = self._graph_request('POST', path)
        else:
            path = f"/users/{self.sender_email}/messages/{message_id}"
            resp = self._graph_request('DELETE', path)

        if resp.status_code in (200, 202, 204):
            return True
        raise Exception(f"Graph delete failed (HTTP {resp.status_code}): {resp.text[:300]}")

    def empty_folder(self, folder: str = 'sentitems', permanent: bool = False,
                     max_iterations: int = 1000) -> int:
        """Delete every message in a folder and return the number deleted.

        Fetches a page of message IDs and deletes them, repeating until the
        folder is empty (bounded by max_iterations as a safety stop).
        """
        deleted = 0
        path = f"/users/{self.sender_email}/mailFolders/{folder}/messages"

        for _ in range(max_iterations):
            resp = self._graph_request('GET', path, params={'$top': '100', '$select': 'id'})
            if resp.status_code != 200:
                raise Exception(f"Graph read failed (HTTP {resp.status_code}): {resp.text[:300]}")

            ids = [m['id'] for m in resp.json().get('value', [])]
            if not ids:
                break

            progress = 0
            for mid in ids:
                try:
                    if self.delete_message(mid, permanent=permanent):
                        deleted += 1
                        progress += 1
                except Exception as e:
                    print(f"[empty_folder] Failed to delete message: {e}")

            # Stop if a full page yielded no successful deletions (avoid spin).
            if progress == 0:
                break

        return deleted

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

        self.from_name, self.reply_to, self.unsub_mailto = _resolve_sender_identity(config, self.from_email)

    def _format_from(self, from_name: str) -> str:
        """Return the From value, adding the display name unless the configured
        RESEND_FROM_EMAIL already includes one (e.g. 'Name <a@b.com>')."""
        if from_name and '<' not in self.from_email:
            return formataddr((from_name, self.from_email))
        return self.from_email

    def send_email(self, to_emails: List[str], subject: str,
                   body_html: str = None, body_text: str = None,
                   cc_emails: List[str] = None, *,
                   from_name: str = None, reply_to: str = None,
                   list_unsubscribe_url: str = None) -> Dict[str, Any]:
        try:
            import resend as resend_lib
            resend_lib.api_key = self.api_key

            if not to_emails:
                raise ValueError("At least one recipient email must be provided")
            if not body_html and not body_text:
                raise ValueError("Either body_html or body_text must be provided")

            params: Dict[str, Any] = {
                "from": self._format_from(from_name or self.from_name),
                "to": [e.strip() for e in to_emails],
                "subject": subject,
            }
            if body_html:
                params["html"] = body_html
            if body_text:
                params["text"] = body_text
            if cc_emails:
                params["cc"] = [e.strip() for e in cc_emails]

            rt = reply_to if reply_to is not None else self.reply_to
            if rt:
                params["reply_to"] = rt

            lu, lu_post = _list_unsubscribe_header(list_unsubscribe_url, self.unsub_mailto)
            if lu:
                params["headers"] = {
                    "List-Unsubscribe": lu,
                    "List-Unsubscribe-Post": lu_post,
                }

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
                   cc_emails: List[str] = None, *,
                   from_name: str = None, reply_to: str = None,
                   list_unsubscribe_url: str = None) -> Dict[str, Any]:
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