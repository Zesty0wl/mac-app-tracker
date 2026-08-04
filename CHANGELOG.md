# Changelog

Notable changes to the Mac Apps Version Tracker.

## [1.2.0] - 2026-08-04

### Added
- Email notification to the admin when a new app suggestion is submitted.
  Recipient is the new `suggestion_notify_email` setting (Admin → Email
  Settings); leave blank to disable. Sends in the background so a mail
  failure never affects the public submission.
- Global Secure Access Client is now tracked (promoted from community
  suggestion #2).
- Analyzer support for packages whose payload contains only an uninstaller
  `.app` (e.g. Global Secure Access): when no payload bundle carries the
  expected identifier, the PackageInfo package identifier is matched and
  stored instead, keeping identifier-keyed version lookups intact.
- M365 mailbox maintenance: bounce processing and Sent Items cleanup.
- Open Graph / Twitter social cards, favicon link tags and new project logo.

### Changed
- Subscription confirmation links now stay valid for 8 weeks (was 7 days),
  and confirmation/reminder emails include a one-click opt-out that also
  works for not-yet-confirmed recipients.
- The subscribe-success page now walks new subscribers through finding the
  confirmation email in spam/junk and marking it "Not spam".
- Download-cache detection uses `actual_url` + `Content-Length` as the
  change signal for CDNs that serve no usable ETag/Last-Modified headers
  (Microsoft onecdn, download.msappproxy.net).

### Fixed
- The hourly URL validator no longer marks live downloads as
  `[download removed]` when a host rejects HEAD requests:
  `download.msappproxy.net` (Global Secure Access) answers 404 to HEAD while
  serving GET normally. A one-byte ranged GET now gets the final say before
  a URL is condemned.
- Email deliverability improvements: MIME sends via M365 Graph, one-click
  `List-Unsubscribe` headers, and From-name / Reply-To defaults.

## [1.1.0] - 2026-04-30

- App version shown above the footer (`VERSION` file plus git SHA).
- Earlier history not itemized.
