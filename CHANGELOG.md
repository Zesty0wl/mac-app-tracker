# Changelog

Notable changes to the Mac Apps Version Tracker.

## [1.3.0] - 2026-08-04

### Added
- Prebuilt multi-arch Docker images (linux/amd64 + linux/arm64), published
  to GitHub Container Registry on every push to main as
  `ghcr.io/zesty0wl/mac-app-tracker` with `latest`, `<version>` and
  `sha-<short sha>` tags. README and DEPLOYMENT now document image-based
  deployment as the recommended path — no clone or build required.

### Fixed
- The Dockerfile no longer copies `.env` into the image, so local builds
  cannot bake secrets into a shareable image. Runtime configuration comes
  from docker-compose `env_file` (as before) and a new `.dockerignore`
  keeps secrets, databases and downloads out of the build context.

## [1.2.1] - 2026-08-04

### Changed
- Removed deployment-specific content so the repo stays cleanly reusable:
  the subscribe-success page no longer carries an instance-specific delivery
  notice (use `TEMPLATE_OVERRIDE_DIR` for content like that), a one-off
  operational script was dropped from `tools/`, and the GitHub API
  User-Agent and admin-panel example URL are now generic.
- Added `CLAUDE.md` with contributor guidance on keeping instance-specific
  content out of the repo.

## [1.2.0] - 2026-08-04

### Added
- Email notification to the admin when a new app suggestion is submitted.
  Recipient is the new `suggestion_notify_email` setting (Admin → Email
  Settings); leave blank to disable. Sends in the background so a mail
  failure never affects the public submission.
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
- The subscribe-success page gives clearer next steps, including checking
  spam/junk folders and marking the confirmation email "Not spam".
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
