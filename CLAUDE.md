# CLAUDE.md

Guidance for AI assistants (and human contributors) working on this repo.

## What this repo is

A white-label, self-hostable tracker for macOS app versions (Flask + SQLite,
run via docker-compose). It is published for reuse: anyone should be able to
clone it, set env vars, and run their own tracker. It is **not** a record of
any one deployment.

## Keep instance-specific content out

Never commit content that only makes sense for a particular deployment:

- Real domains, email addresses, tenant/subscription IDs, or credentials —
  including in examples, comments, and User-Agent strings. Use
  `example.com`-style placeholders.
- Incident- or time-specific copy in templates or emails (outage apologies,
  "we're currently being filtered as spam", etc.).
- One-off operational scripts ("resend the notification we missed last
  Tuesday"). `tools/` is for generic, reusable diagnostics only.
- Changelog entries describing instance state (which apps a deployment
  tracks, moderation decisions). The changelog records product changes.

## Where instance-specific things live instead

- **Runtime data** (tracked apps, suggestions, subscribers, email settings):
  the SQLite databases under `data/` (gitignored). Apps are managed through
  the admin panel, not in code.
- **Secrets and branding**: `.env` (gitignored; see `.env.template` /
  `.env.example` for the full white-label surface).
- **Custom template content**: mount a directory at `TEMPLATE_OVERRIDE_DIR`
  (default `/app/templates_override`); files there shadow `templates/` by
  name. Pair with a gitignored `docker-compose.override.yml` for the volume
  mount and extra env files.

## Conventions

- Version lives in `VERSION` (semver); user-visible changes go in
  `CHANGELOG.md` under Added/Changed/Fixed.
- Emails must be restrained in design, HTML-escape user input, and send in
  the background so a mail failure never breaks a user-facing request.
- Scans run as subprocesses (`enhanced_tracker.py`), hourly via
  `scheduler.py`; the web app and scheduler share one container.
