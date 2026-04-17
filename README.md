# Mac Apps Version Tracker

A self-hosted web app and CLI toolkit for tracking macOS application releases. It downloads installer packages (PKG/ZIP), extracts version metadata, and records every release into SQLite. A built-in scheduler checks for updates hourly, and subscribers receive email notifications when new versions appear.

Originally built for Microsoft Mac apps (Company Portal, Defender, Edge, Office), it can track **any** macOS application that distributes a downloadable PKG or ZIP.

**Live demo:** see it running at [appledevicepolicy.tools/app-tracker](https://appledevicepolicy.tools/app-tracker).

## Features

- **Automatic hourly checks** with header-aware change detection (ETag / Last-Modified) to skip redundant downloads
- **Web UI** for browsing release history, update heatmaps, and subscribing to email notifications
- **Admin panel** (JWT auth) for managing tracked apps, configuring email providers, and viewing logs
- **Pluggable email** -- Microsoft 365 Graph API and Resend are supported out of the box; falls back to a no-op provider when unconfigured
- **Email subscriptions** with double opt-in confirmation and per-app filtering
- **Component tracking** and SHA-256 checksums for suite packages (Office, Defender)
- **CLI** for manual scans, history export, and URL validation
- **Docker-first** with `docker compose up -d`

## Quick Start

### Docker Compose (recommended)

```bash
cp .env.template .env
# Edit .env with your email provider credentials (optional) and admin password

docker compose up -d
```

The web interface is available at **http://localhost:5000**.

### Manual Installation

```bash
# System dependencies (Ubuntu/Debian)
sudo apt-get install -y p7zip-full cpio file

# Python
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Run
export DB_PATH="$(pwd)/data/microsoft_apps_versions.db"
python3 scheduler.py &
flask --app web_app run --host 0.0.0.0 --port 5000
```

## Configuration

### Environment Variables

All runtime configuration comes from a single `.env` file in the
project root (the same directory as `docker-compose.yml`). Docker
Compose loads it automatically via the `env_file: .env` stanza in
`docker-compose.yml`, and the Flask app reads from `os.environ` at
startup.

#### Creating the file

Two starter files ship with the repo:

- **`.env.template`** — bare minimum (admin + email provider secrets).
- **`.env.example`** — full reference, including every branding
  variable with inline comments and examples.

Copy whichever you prefer and edit it:

```bash
cp .env.example .env      # or: cp .env.template .env
$EDITOR .env
```

`.env` is already listed in [`.gitignore`](.gitignore) — do not commit
it.

#### Syntax

Standard dotenv format, one variable per line:

```dotenv
# Comments start with a hash.
KEY=value
ANOTHER_KEY=another value

# Quote values only if they contain a '#' or leading/trailing whitespace.
# Most values (including JSON) do NOT need quotes.
NAV_LINKS_JSON=[{"label":"App Tracker","href":"/"}]

# No spaces around '=', no trailing semicolons.
# Newlines inside a value are not supported - keep each var on one line.
```

After editing `.env`, restart the container so Docker picks up the
changes:

```bash
docker compose up -d --force-recreate
```

Environment variables can also be overridden per-deployment via
`docker-compose.override.yml` (also gitignored) or by exporting them
in the shell before `docker compose up` — useful in CI.

#### Required / common variables

| Variable | Required | Description |
|---|---|---|
| `ADMIN_PASSWORD` | Yes | Password for the `/admin` panel. Seeded on first boot as the `admin` user |
| `ADMIN_JWT_SECRET` | Yes | Random secret for admin JWT signing. Generate with e.g. `openssl rand -hex 32` |
| `FLASK_SECRET_KEY` | Yes | Flask session secret. Generate with `openssl rand -hex 32` |
| `SITE_URL` | Yes | Public HTTPS origin, used in email links. Example: `https://tracker.example.com` (no trailing slash) |
| `DEV_MODE` | No | `true` mounts under `/app-tracker-dev` and disables the scheduler. Default `false` |
| `DB_PATH` | No | SQLite path for version DB. Default `/data/microsoft_apps_versions.db` inside the container |
| `SUBSCRIPTION_DB_PATH` | No | SQLite path for subscriptions DB. Default `/data/subscriptions.db` |

#### Email provider (choose one)

Pick **either** Microsoft 365 Graph **or** Resend. You can also leave
all of these unset and configure the provider from the admin UI at
`/admin/email` instead — values saved there take precedence over env
vars.

| Variable | Required | Description |
|---|---|---|
| `M365_CLIENT_ID` | If using M365 | Entra App Registration client ID |
| `M365_CLIENT_SECRET` | If using M365 | Entra App Registration client secret |
| `M365_TENANT_ID` | If using M365 | Azure AD tenant ID |
| `SENDER_EMAIL` | If using M365 | Mailbox to send from |
| `RESEND_API_KEY` | If using Resend | Resend API key (`re_...`) |
| `RESEND_FROM_EMAIL` | If using Resend | Verified sender address |
| `NOTIFICATION_RECIPIENTS` | No | Comma-separated default recipients for admin/test emails |

#### Optional: analytics & UI

| Variable | Required | Description |
|---|---|---|
| `PLAUSIBLE_DOMAIN` | No | Plausible analytics domain (e.g. `tracker.example.com`) |
| `PLAUSIBLE_SCRIPT_URL` | No | Plausible script URL (e.g. `https://plausible.io/js/script.js`) |
| `CONTACT_EMAIL` | No | Contact email shown in the UI |

See [`.env.example`](.env.example) for the full list with inline
comments, including the branding variables documented below.

### Branding / white-labelling

Everything in the header and footer is overridable without touching source.

| Variable | Description |
|---|---|
| `SITE_NAME` | Product name, used in `<h1>`, `<title>`, emails, copyright. Default: "Mac Apps Version Tracker" |
| `BRAND_NAME` | Parent-brand text in the top-left of the header. Defaults to `SITE_NAME`. Useful when embedding the tracker inside a larger site |
| `BRAND_URL` | Where the brand text links to. Default: `/` |
| `NAV_LINKS_JSON` | JSON array of header links: `[{"label":"...","href":"...","target":"_blank","match":"/prefix"}]` |
| `FOOTER_LOGO_URL` | Optional. Path/URL to a footer logo. Hidden when empty |
| `FOOTER_LOGO_ALT` | Alt text for the logo |
| `FOOTER_ATTRIBUTION` | Optional line next to the logo (e.g. "Maintained by ..."). Hidden when empty |
| `FOOTER_TAGLINE_HTML` | HTML shown under the logo row |
| `FOOTER_DISCLAIMER_TITLE` | Disclaimer card title. Default: "About This Tracker" |
| `FOOTER_DISCLAIMER_HTML` | Disclaimer card body (HTML) |
| `FOOTER_LINKS_JSON` | JSON array of footer links: `[{"label":"...","href":"...","target":"_blank"}]` |
| `EMAIL_BRAND_NAME` | Brand name used in email subjects/bodies. Defaults to `SITE_NAME` |

For anything the env vars can't express (custom logo markup, extra nav sections, etc.), mount a directory of Jinja2 overrides:

```yaml
volumes:
  - ./my-branding/templates:/app/templates_override:ro
```

Any template there (typically `_header.html` and/or `_footer.html`) is checked before the bundled one. Set `TEMPLATE_OVERRIDE_DIR` to change the path.

### Tracked Apps

Apps are managed from the admin panel at `/admin/apps`. When a new app is added, an initial scan runs automatically in the background.

On first run, Company Portal is seeded as a default app. Add more from the admin panel.

Each app entry requires:

| Field | Description |
|---|---|
| `app_id` | Unique slug (e.g. `companyportal`) |
| `name` | Display name |
| `url` | Download URL (direct link or fwlink redirect) |
| `identifier` | Expected bundle identifier (e.g. `com.microsoft.CompanyPortalMac`) |
| `type` | `single` or `suite` |
| `url_type` | `direct` or `metadata_json` |

## Admin Panel

Navigate to `/admin` and log in with the credentials set via `ADMIN_PASSWORD`.

- **Dashboard** -- stats, recent activity, JSON-to-DB migration
- **Apps** -- add, edit, enable/disable, and delete tracked apps
- **Email** -- select provider (M365 / Resend), enter credentials, send test emails
- **Logs** -- filterable activity log

## CLI Usage

```bash
# Analyze a specific app
python3 download_and_analyze.py companyportal

# Analyze all configured apps
python3 download_and_analyze.py all

# Show version history
python3 download_and_analyze.py --show-history

# List available apps
python3 download_and_analyze.py --list-apps

# Export to JSON
python3 download_and_analyze.py --export-json output.json

# Keep downloaded files (deleted by default)
python3 download_and_analyze.py all --keep-downloads
```

## Publishing / Deployment

The recommended production layout is:

```
Cloudflare (DNS + TLS)  ->  nginx reverse proxy  ->  Docker container (127.0.0.1:5000)
```

- **Cloudflare** handles public DNS and edge TLS. Point an A/AAAA record
  (or CNAME) at your server. Set SSL/TLS mode to "Full (strict)" and
  issue an origin cert from Cloudflare for the nginx host.
- **nginx** terminates TLS on the server using the Cloudflare origin
  cert and proxies requests on `127.0.0.1:5000` to the container. The
  Flask app is reverse-proxy-aware and mounts itself under the
  `/app-tracker` prefix.
- **Docker** runs the tracker via `docker compose up -d`. Only bind
  the container port to `127.0.0.1` in production so nginx is the
  only public entry point.

A working example config and step-by-step setup lives in
[`nginx/`](nginx/) — copy `nginx/app-tracker.conf` into
`/etc/nginx/sites-available/`, swap in your domain and cert paths,
enable the site, and reload. The README there also covers co-hosting
multiple apps on a single domain via extra path prefixes.

Set `SITE_URL` in `.env` to the public HTTPS origin (e.g.
`https://tracker.example.com`) so confirmation and unsubscribe links
in outgoing emails use the correct address.

## API

See [API.md](API.md) for full endpoint documentation. Key endpoints:

| Endpoint | Description |
|---|---|
| `GET /api/apps` | List tracked apps with latest version |
| `GET /api/app/<id>/versions` | Version history for an app |
| `GET /api/all-versions` | All versions across all apps |
| `GET /api/latest` | Latest version per app (JSON) |
| `GET /api/stats` | Overall statistics |

## Project Structure

```
web_app.py                  # Flask app and API routes
scheduler.py                # Hourly background check runner
enhanced_tracker.py         # Analyzer + email notification orchestration
download_and_analyze.py     # CLI entry point
admin/
  __init__.py               # Admin blueprint (JWT auth, CRUD, email settings)
  database.py               # Admin DB tables (users, apps, logs, email settings)
notifications/
  providers.py              # Pluggable email providers (M365, Resend, Noop)
  manager.py                # Email subscription logic (confirm, notify, unsubscribe)
  database.py               # Subscriber DB (tokens, preferences)
tracker/
  config.py                 # App catalogue loader (reads from admin DB)
  download.py               # URL resolution, header checks, streaming download
  extraction.py             # PKG/ZIP/Payload extraction (7z, xar, cpio)
  analyzer.py               # Orchestrate download -> extract -> parse -> store
  database.py               # SQLite wrapper for version/component data
  validator.py              # Re-check stored URLs and flag removed assets
templates/                  # Jinja2 templates (main UI, subscription, admin)
static/                     # CSS and images
```

## License

MIT
