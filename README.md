# Mac Apps Version Tracker

A self-hosted web app and CLI toolkit for tracking macOS application releases. It downloads installer packages (PKG/ZIP), extracts version metadata, and records every release into SQLite. A built-in scheduler checks for updates hourly, and subscribers receive email notifications when new versions appear.

Originally built for Microsoft Mac apps (Company Portal, Defender, Edge, Office), it can track **any** macOS application that distributes a downloadable PKG or ZIP.

**Live demo:** see it running at [appledevicepolicy.tools/app-tracker](https://appledevicepolicy.tools/app-tracker).

## Features

- **Automatic hourly checks** with header-aware change detection (ETag / Last-Modified) to skip redundant downloads
- **Web UI** for browsing release history, update heatmaps, and subscribing to email notifications
- **Admin panel** (JWT auth, rate-limited with per-account lockout) for managing tracked apps, configuring email providers, and viewing logs
- **Pluggable email** -- Microsoft 365 Graph API and Resend are supported out of the box; falls back to a no-op provider when unconfigured
- **Email subscriptions** with double opt-in confirmation and per-app filtering
- **Component tracking** and SHA-256 checksums for suite packages (Office, Defender)
- **CLI** for manual scans, history export, and URL validation
- **Docker-first** with `docker compose up -d`; footer shows the deployed version + short git SHA

## Quick Start

### Docker Compose (recommended)

```bash
git clone https://github.com/Zesty0wl/mac-app-tracker.git
cd mac-app-tracker

cp .env.template .env

# Generate the two required secrets and write them into .env
sed -i "s|^FLASK_SECRET_KEY=.*|FLASK_SECRET_KEY=$(openssl rand -hex 32)|" .env
sed -i "s|^ADMIN_JWT_SECRET=.*|ADMIN_JWT_SECRET=$(openssl rand -hex 32)|" .env

# Set a strong ADMIN_PASSWORD and SITE_URL, then optionally add email
# provider credentials.
$EDITOR .env

docker compose up -d
```

The web interface is available at **http://localhost:5000** and the
admin panel at **http://localhost:5000/admin** (username `admin`,
password from `ADMIN_PASSWORD`).

> The app refuses to boot if `FLASK_SECRET_KEY` or `ADMIN_JWT_SECRET`
> are left at their placeholder values. Generate real values with
> `openssl rand -hex 32` before starting, or set `DEV_MODE=true` for
> local experimentation only.

### Manual Installation

```bash
git clone https://github.com/Zesty0wl/mac-app-tracker.git
cd mac-app-tracker

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
| `ADMIN_LOGIN_RATE_LIMIT` | No | Flask-Limiter string for `/admin/login`. Default `5 per minute;30 per hour` |
| `ADMIN_MAX_FAILED_ATTEMPTS` | No | Failed logins before account lockout. Default `10` |
| `ADMIN_LOCKOUT_MINUTES` | No | Lockout duration after hitting the threshold. Default `15` |
| `TRUSTED_PROXY_COUNT` | No | Reverse proxy hops to trust for `X-Forwarded-For`. Default `1` |

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
| `url_type` | `direct`, `metadata_json`, or `github_release` |

## Admin Panel

Navigate to `/admin` and log in with the credentials set via `ADMIN_PASSWORD`.

- **Dashboard** -- stats, recent activity, JSON-to-DB migration
- **Apps** -- add, edit, enable/disable, and delete tracked apps
- **Email** -- select provider (M365 / Resend), enter credentials, send test emails
- **Logs** -- filterable activity log

### Login security

The admin login has several brute-force mitigations baked in:

- **IP rate limiting** on `POST /admin/login` via Flask-Limiter
  (default `5 per minute;30 per hour`, configurable with
  `ADMIN_LOGIN_RATE_LIMIT`).
- **Per-account lockout**: after `ADMIN_MAX_FAILED_ATTEMPTS` consecutive
  failures (default 10) the account is locked for `ADMIN_LOCKOUT_MINUTES`
  (default 15). Any successful login clears the counter.
- **Unknown usernames are silently ignored** for lockout accounting so
  attackers cannot enumerate which admin usernames exist.
- **Startup refuses to boot** if `FLASK_SECRET_KEY` or `ADMIN_JWT_SECRET`
  are left at their placeholder values (unless `DEV_MODE=true`).
- **Real client IP** is taken from `X-Forwarded-For` via
  `werkzeug.middleware.proxy_fix.ProxyFix`; set `TRUSTED_PROXY_COUNT` to
  match the number of reverse proxies in front of the container (1 for
  plain nginx, 2 if you are also behind Cloudflare).
- **All attempts** — success, failure, lockout hits — are written to the
  admin activity log visible at `/admin/logs`.
- Session cookies are `httponly`, `secure`, `SameSite=Lax`; JWT expires
  after 8 hours.

If you want an additional layer, add nginx-level rate limiting (see the
commented `limit_req_zone` block in [`nginx/app-tracker.conf`](nginx/app-tracker.conf))
or put a WAF such as Cloudflare in front of the site.

## CLI Usage

The CLI reads the same SQLite database and app catalogue as the web
app, so it needs access to `/data/microsoft_apps_versions.db` and the
system extraction tools (`7z`, `cpio`, `xar`). The easiest way to run
it is **inside the running container** so you inherit all of that
plus the same `.env`:

```bash
# One-off commands (container must be up)
docker compose exec app python3 download_and_analyze.py --list-apps
docker compose exec app python3 download_and_analyze.py companyportal
docker compose exec app python3 download_and_analyze.py all
docker compose exec app python3 download_and_analyze.py --show-history
docker compose exec app python3 download_and_analyze.py --export-json /data/export.json
docker compose exec app python3 download_and_analyze.py all --keep-downloads

# Drop into a shell in the container for interactive use
docker compose exec app bash
```

Files written to `/data/` inside the container are persisted on the
host via the `./data` volume mount, so `--export-json /data/export.json`
appears at `./data/export.json` on the host.

If you have followed the [Manual Installation](#manual-installation)
steps (system deps + venv + `DB_PATH`), you can also run the CLI
directly on the host without the `docker compose exec` prefix.

### Available commands

```bash
# Analyze a specific app by its app_id
python3 download_and_analyze.py <app_id>

# Analyze all configured apps
python3 download_and_analyze.py all

# Show version history
python3 download_and_analyze.py --show-history

# List available apps
python3 download_and_analyze.py --list-apps

# Export all versions to JSON
python3 download_and_analyze.py --export-json output.json

# Keep downloaded installers instead of deleting them after analysis
python3 download_and_analyze.py all --keep-downloads
```

## Publishing / Deployment

The recommended production layout is:

```
Cloudflare (DNS + TLS)  ->  nginx reverse proxy  ->  Docker container (127.0.0.1:5000)
```

### 1. Prepare the server

On a fresh Linux host with Docker + Docker Compose v2 and nginx
installed, clone the repo wherever you keep services on that host
(e.g. under `/srv`, `/opt`, or your home directory):

```bash
git clone https://github.com/Zesty0wl/mac-app-tracker.git
cd mac-app-tracker
cp .env.template .env
```

Fill in `.env` and **generate real values** for the required secrets
— the container will refuse to start otherwise:

```bash
sed -i "s|^FLASK_SECRET_KEY=.*|FLASK_SECRET_KEY=$(openssl rand -hex 32)|" .env
sed -i "s|^ADMIN_JWT_SECRET=.*|ADMIN_JWT_SECRET=$(openssl rand -hex 32)|" .env
$EDITOR .env          # set ADMIN_PASSWORD, SITE_URL, email provider
```

At minimum you need `FLASK_SECRET_KEY`, `ADMIN_JWT_SECRET`,
`ADMIN_PASSWORD` and `SITE_URL`. See the [Configuration](#configuration)
section for the full variable list.

### 2. Bind the container to localhost only

Edit `docker-compose.yml` so the container is **not** reachable
directly from the internet — nginx is the only public entry point:

```yaml
services:
  app:
    ports:
      - "127.0.0.1:5000:5000"   # localhost-only bind
```

### 3. Start the container

```bash
docker compose up -d
docker compose logs -f         # watch it boot; Ctrl+C when happy
```

The footer shows the running version (e.g. `v1.0.0+abc1234`) — the
commit SHA comes from `git rev-parse --short HEAD` at build time, so
rebuilds automatically refresh it.

### 4. Configure Cloudflare

- Add an A/AAAA record (proxied, orange cloud) for your domain pointing
  at the server's public IP.
- Under **SSL/TLS** set mode to **Full (strict)**.
- Under **SSL/TLS -> Origin Server** create an origin certificate for
  your domain and save the cert + private key on the server (e.g. at
  `/etc/ssl/certs/cloudflare/yourdomain.pem` and
  `/etc/ssl/private/cloudflare/yourdomain.key`).

### 5. Configure nginx

Copy the example from the repo and adjust:

```bash
sudo cp nginx/app-tracker.conf /etc/nginx/sites-available/app-tracker
sudo $EDITOR /etc/nginx/sites-available/app-tracker   # set server_name + cert paths
sudo ln -s /etc/nginx/sites-available/app-tracker /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

The sample config at [`nginx/app-tracker.conf`](nginx/app-tracker.conf)
terminates TLS, forwards `X-Forwarded-*` headers (required by the
Flask app's `ProxyFix`), and includes a commented-out `limit_req_zone`
block for edge-level rate limiting on `/admin/login` as defence in
depth. See [`nginx/README.md`](nginx/README.md) for multi-app hosting
under a single domain.

### 6. Verify

```bash
curl -sI https://yourdomain.com/app-tracker/       # expect HTTP/2 200
```

- Visit `https://yourdomain.com/app-tracker/` — main UI
- Visit `https://yourdomain.com/app-tracker/admin` — log in with
  `admin` / your `ADMIN_PASSWORD`
- Confirm the version string above the footer matches the commit you
  deployed.

### 7. Updating

From the directory you cloned the repo into:

```bash
git pull
GIT_SHA=$(git rev-parse --short HEAD) docker compose build
docker compose up -d --force-recreate
```

`docker compose up -d --force-recreate` re-reads `.env` and swaps
containers in-place. The footer version will flip to the new SHA
once the new container is healthy.

### Security checklist for production

- [x] `FLASK_SECRET_KEY` and `ADMIN_JWT_SECRET` are random 32-byte
      hex strings (the app will refuse to boot otherwise).
- [x] `ADMIN_PASSWORD` is long and unique.
- [x] Container port is bound to `127.0.0.1` so only nginx can reach it.
- [x] nginx terminates TLS with a valid certificate (Cloudflare
      origin or Let's Encrypt).
- [x] `TRUSTED_PROXY_COUNT` is set correctly (`1` for nginx only,
      `2` if you are also behind Cloudflare). This ensures
      `X-Forwarded-For` and the admin login rate limiter see real
      client IPs.
- [x] Review `ADMIN_LOGIN_RATE_LIMIT`, `ADMIN_MAX_FAILED_ATTEMPTS`,
      `ADMIN_LOCKOUT_MINUTES` if defaults are too loose/strict.
- [x] Optionally enable the nginx `limit_req_zone` block in
      `nginx/app-tracker.conf` for a second layer of rate limiting.
- [x] Consider putting the admin panel behind a Cloudflare Access
      rule or IP allowlist at the nginx layer.

See also the [Login security](#login-security) subsection for the
built-in brute-force mitigations.

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

## Versioning

The canonical version lives in the [`VERSION`](VERSION) file at the
repo root (plain semver, one line, e.g. `1.0.0`). At runtime the app
resolves the version in this order:

1. `APP_VERSION` env var (useful for CI builds from a tag)
2. Contents of `VERSION`
3. Fallback: `0.0.0`

If a short git SHA is available it is appended as `+<sha>`. Sources,
in order:

1. `GIT_SHA` env var (baked into the Docker image by the bundled
   Dockerfile via `ARG GIT_SHA`)
2. `git rev-parse --short HEAD` run inside the container (only works
   if the `.git` directory is present, e.g. in development)

The resulting string — e.g. `v1.0.0+521172f` — is rendered as a small
muted line above the footer on every page and links to
`<SOURCE_URL>/releases`.

To cut a release:

```bash
echo "1.2.0" > VERSION
git commit -am "Release v1.2.0"
git tag v1.2.0 && git push --tags
GIT_SHA=$(git rev-parse --short HEAD) docker compose build
docker compose up -d --force-recreate
```

## License

MIT
