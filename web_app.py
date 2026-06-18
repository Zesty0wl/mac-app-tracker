#!/usr/bin/env python3
"""
Flask web application and REST API for Mac Apps Version Tracker.

Serves the main UI for browsing release history and update heatmaps,
the subscription pages for email sign-up, and the JSON API consumed
by automation scripts.
"""

from flask import Flask, Response, jsonify, render_template, send_from_directory, request, redirect, url_for
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from jinja2 import ChoiceLoader, FileSystemLoader
from werkzeug.middleware.proxy_fix import ProxyFix
from tracker.database import VersionDatabase
from notifications.manager import SubscriptionManager
from tracker.config import load_apps_config, build_identifier_lookup
import admin.database as adb
from admin import admin_bp
from suggestions import database as sdb
import os
import json
import logging
import subprocess
from pathlib import Path


def _load_app_version() -> str:
    """Resolve the app version string shown in the footer.

    Resolution order:
      1. ``APP_VERSION`` env var (useful in CI / when building images)
      2. ``VERSION`` file shipped with the repo (semver, e.g. ``1.2.3``)
      3. Fallback: ``0.0.0``

    If a short git SHA is available (baked into the image as ``GIT_SHA`` or
    read from the local ``.git`` repo during development), it is appended
    as ``+<sha>``.
    """
    version = os.environ.get('APP_VERSION', '').strip()
    if not version:
        version_file = Path(__file__).resolve().parent / 'VERSION'
        try:
            version = version_file.read_text(encoding='utf-8').strip()
        except OSError:
            version = ''
    if not version:
        version = '0.0.0'

    sha = os.environ.get('GIT_SHA', '').strip()
    if not sha:
        try:
            sha = subprocess.check_output(
                ['git', 'rev-parse', '--short', 'HEAD'],
                cwd=str(Path(__file__).resolve().parent),
                stderr=subprocess.DEVNULL,
                timeout=2,
            ).decode().strip()
        except (OSError, subprocess.SubprocessError):
            sha = ''
    if sha:
        return f'{version}+{sha}'
    return version


APP_VERSION = _load_app_version()

app = Flask(__name__, static_folder='static', template_folder='templates')

_DEFAULT_FLASK_SECRET = 'dev-key-change-in-production'
_flask_secret = os.environ.get('FLASK_SECRET_KEY', _DEFAULT_FLASK_SECRET)
_DEV_MODE = os.environ.get('DEV_MODE', 'false').lower() == 'true'
if _flask_secret == _DEFAULT_FLASK_SECRET and not _DEV_MODE:
    raise RuntimeError(
        "FLASK_SECRET_KEY is not set. Generate one with "
        "`openssl rand -hex 32` and set it in .env, or set DEV_MODE=true "
        "to bypass this check for local development."
    )
app.secret_key = _flask_secret

# Rate limit abusive endpoints (login brute-force in particular). The
# in-memory storage is per-worker, but combined with the DB-backed account
# lockout in admin.database this is sufficient for the 2-worker default.
# For multi-host deployments point ``RATELIMIT_STORAGE_URI`` at Redis.
limiter = Limiter(
    key_func=get_remote_address,
    app=app,
    default_limits=[],
    storage_uri=os.environ.get('RATELIMIT_STORAGE_URI', 'memory://'),
    headers_enabled=True,
)

# Allow operators to override any bundled template (e.g. _header.html, _footer.html)
# by mounting a directory at TEMPLATE_OVERRIDE_DIR. Overrides are checked first.
_template_override_dir = os.environ.get('TEMPLATE_OVERRIDE_DIR', '/app/templates_override')
if os.path.isdir(_template_override_dir):
    app.jinja_loader = ChoiceLoader([
        FileSystemLoader(_template_override_dir),
        app.jinja_loader,
    ])
    logging.getLogger(__name__).info(
        "Loaded template overrides from %s", _template_override_dir
    )

# Register admin blueprint
app.register_blueprint(admin_bp)


@app.errorhandler(429)
def ratelimit_handler(e):
    """Render a friendly page for rate-limited requests (primarily the admin
    login form) and keep JSON shape for API callers."""
    if request.path.startswith('/admin/') and not request.is_json:
        return render_template(
            'admin/login.html',
            error='Too many login attempts. Please wait a few minutes and try again.'
        ), 429
    return jsonify({'error': 'Too many requests', 'detail': str(e.description)}), 429


# Apply rate limits to the admin login endpoint. 5 attempts per minute plus
# 30 per hour per IP stops online brute force without impacting legitimate
# users. Combined with per-account lockout in admin.database this gives
# defence in depth. ``exempt_when`` skips rate limiting in dev mode.
_login_limits = os.environ.get('ADMIN_LOGIN_RATE_LIMIT', '5 per minute;30 per hour')
limiter.limit(
    _login_limits,
    methods=['POST'],
    exempt_when=lambda: _DEV_MODE,
)(app.view_functions['admin.login_post'])

# Initialise admin tables and seed admin user on startup
adb.init_admin_tables()
_admin_pw = os.environ.get('ADMIN_PASSWORD', '')
if _admin_pw:
    adb.create_admin_user('admin', _admin_pw)

# Initialise community-suggestion tables
sdb.init_tables()

# Configure Flask for reverse proxy with /app-tracker prefix
class ReverseProxied:
    def __init__(self, app, script_name=None):
        self.app = app
        self.script_name = script_name

    def __call__(self, environ, start_response):
        if self.script_name:
            environ['SCRIPT_NAME'] = self.script_name
            path_info = environ['PATH_INFO']
            if path_info.startswith(self.script_name):
                environ['PATH_INFO'] = path_info[len(self.script_name):]
        return self.app(environ, start_response)

# Determine if running in dev mode
DEV_MODE = os.environ.get('DEV_MODE', 'false').lower() == 'true'

# Apply reverse proxy configuration based on environment
script_name = '/app-tracker-dev' if DEV_MODE else '/app-tracker'
app.wsgi_app = ReverseProxied(app.wsgi_app, script_name=script_name)

# Trust X-Forwarded-* headers from the reverse proxy (nginx) so that
# Flask-Limiter and request.remote_addr see the real client IP instead of
# 127.0.0.1. Number of trusted proxies is configurable via env var.
_proxy_hops = int(os.environ.get('TRUSTED_PROXY_COUNT', '1'))
app.wsgi_app = ProxyFix(
    app.wsgi_app,
    x_for=_proxy_hops,
    x_proto=_proxy_hops,
    x_host=_proxy_hops,
    x_prefix=_proxy_hops,
)

DB_PATH = os.environ.get('DB_PATH', 'microsoft_apps_versions.db')

# Initialize subscription manager with correct base URL including reverse proxy path
# Prefer site_url from admin DB (editable in admin panel), fall back to env var
_db_site_url = adb.get_email_setting('site_url')
site_url = _db_site_url or os.environ.get('SITE_URL', 'https://localhost')
base_url = f'{site_url}{script_name}'
subscription_manager = SubscriptionManager(base_url=base_url)

APPS_CONFIG = load_apps_config()
APPS_BY_IDENTIFIER = build_identifier_lookup(APPS_CONFIG)

# Inject configurable template globals (analytics, contact email)
@app.context_processor
def inject_site_config():
    return {
        'plausible_domain': os.environ.get('PLAUSIBLE_DOMAIN', ''),
        'plausible_script_url': os.environ.get('PLAUSIBLE_SCRIPT_URL', ''),
        'contact_email': os.environ.get('CONTACT_EMAIL', ''),
    }


def _parse_json_env(var_name: str, default):
    """Parse a JSON environment variable, returning `default` on error."""
    raw = os.environ.get(var_name, '').strip()
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (ValueError, TypeError) as exc:
        logging.getLogger(__name__).warning(
            "Invalid JSON in %s env var (%s); using default", var_name, exc
        )
        return default


# Default nav links for an unbranded deployment.
# Operators can override by setting NAV_LINKS_JSON, or by providing a
# custom _header.html in TEMPLATE_OVERRIDE_DIR.
_DEFAULT_NAV_LINKS = [
    {'label': 'App Tracker', 'href': '/', 'match': '/'},
    {'label': 'Notifications', 'href': '/subscribe'},
    {'label': 'JSON API', 'href': '/api/latest', 'target': '_blank'},
]

_DEFAULT_FOOTER_DISCLAIMER_HTML = (
    '<p>This is an independent, open-source tool for tracking macOS application '
    'release metadata. It is <strong>not officially supported by any vendor</strong>. '
    'All data is provided as-is without warranty. Validate packages against your '
    'own security policies and refer to official vendor documentation for '
    'production guidance.</p>'
)

_DEFAULT_FOOTER_TAGLINE_HTML = (
    'Release metadata sourced from publicly available Microsoft CDN endpoints '
    'and installer manifests. Links are provided for research and operational '
    'readiness only.'
)

_DEFAULT_SOURCE_URL = 'https://github.com/Zesty0wl/mac-app-tracker'


# Branding / white-labelling: everything here is overridable via env vars.
# For full HTML control (e.g. custom logo markup), drop a replacement
# _header.html / _footer.html into TEMPLATE_OVERRIDE_DIR.
#
# Two distinct concepts:
#   site_name  - the product/app itself (h1, page title, emails, copyright).
#                Defaults to "Mac Apps Version Tracker".
#   brand_name - the parent brand shown in the site header (top-left).
#                Useful when the tracker is mounted inside a larger site.
#                Defaults to site_name.
@app.context_processor
def inject_branding():
    site_name = os.environ.get('SITE_NAME', 'Mac Apps Version Tracker')
    brand_name = os.environ.get('BRAND_NAME', '') or site_name
    seo_page_title = os.environ.get('SEO_PAGE_TITLE', '') or site_name
    seo_description = os.environ.get(
        'SEO_DESCRIPTION',
        'Real-time version tracking for macOS applications used in '
        'Microsoft Intune-managed environments.'
    )
    seo_intro_html = os.environ.get(
        'SEO_INTRO_HTML',
        ''
    )
    seo_canonical_url = os.environ.get('SEO_CANONICAL_URL', '')
    return {
        'site_name': site_name,
        'brand_name': brand_name,
        'brand_url': os.environ.get('BRAND_URL', '/'),
        'seo_page_title': seo_page_title,
        'seo_description': seo_description,
        'seo_intro_html': seo_intro_html,
        'seo_canonical_url': seo_canonical_url,
        'nav_links': _parse_json_env('NAV_LINKS_JSON', _DEFAULT_NAV_LINKS),
        'footer_logo_url': os.environ.get('FOOTER_LOGO_URL', ''),
        'footer_logo_alt': os.environ.get('FOOTER_LOGO_ALT', ''),
        'footer_attribution': os.environ.get(
            'FOOTER_ATTRIBUTION', 'Developed by Neil Johnson'
        ),
        'footer_tagline_html': os.environ.get(
            'FOOTER_TAGLINE_HTML', _DEFAULT_FOOTER_TAGLINE_HTML
        ),
        'footer_disclaimer_title': os.environ.get(
            'FOOTER_DISCLAIMER_TITLE', 'About This Tracker'
        ),
        'footer_disclaimer_html': os.environ.get(
            'FOOTER_DISCLAIMER_HTML', _DEFAULT_FOOTER_DISCLAIMER_HTML
        ),
        'footer_links': _parse_json_env('FOOTER_LINKS_JSON', [
            {'label': 'Notifications', 'href': '/subscribe'},
            {'label': 'Suggest Apps', 'href': '/suggest'},
            {'label': 'JSON API', 'href': '/api/latest', 'target': '_blank'},
        ]),
        'source_url': os.environ.get('SOURCE_URL', _DEFAULT_SOURCE_URL),
    }


@app.context_processor
def inject_version():
    return {'app_version': APP_VERSION}


def get_display_name(package_identifier: str) -> str:
    """Return a friendly app name for a package identifier."""
    # Check live DB first (picks up apps added after startup)
    apps = load_apps_config()
    lookup = build_identifier_lookup(apps)
    meta = lookup.get(package_identifier)
    if meta and meta.get('name'):
        return meta['name']
    suffix = package_identifier.split('.')[-1]
    return suffix.replace('_', ' ').title()

@app.route('/')
def index():
    """Serve the main page"""
    dev_mode = DEV_MODE
    # Build a static, server-rendered list of tracked app names so the
    # content is indexable by search engines that don't execute JS.
    try:
        apps_cfg = load_apps_config()
        tracked_app_names = sorted({
            (meta.get('name') or '').strip()
            for meta in apps_cfg.values()
            if meta.get('name')
        })
    except Exception:
        tracked_app_names = []
    return render_template(
        'index.html',
        dev_mode=dev_mode,
        tracked_app_names=tracked_app_names,
    )

@app.route('/api/apps')
def get_apps():
    """Get list of tracked applications"""
    with VersionDatabase(DB_PATH) as db:
        cursor = db.conn.cursor()
        cursor.execute("""
            SELECT 
                package_identifier,
                MAX(first_detected) as last_checked,
                (SELECT download_url FROM versions v 
                 WHERE v.package_identifier = versions.package_identifier 
                 ORDER BY first_detected DESC LIMIT 1) as download_url
            FROM versions
            WHERE package_identifier NOT IN ('__manifest_cache__', '__last_check__')
            GROUP BY package_identifier
            ORDER BY package_identifier
        """)
        version_rows = cursor.fetchall()
        
        # Build lookup of release_notes_url from tracked_apps
        rn_urls = {}
        try:
            cursor.execute("SELECT identifier, release_notes_url FROM tracked_apps WHERE release_notes_url != ''")
            for rn_row in cursor.fetchall():
                rn_urls[rn_row['identifier']] = rn_row['release_notes_url']
        except Exception:
            pass

        apps = []
        for row in version_rows:
            identifier = row['package_identifier']
            friendly_name = get_display_name(identifier)
            
            apps.append({
                'id': identifier,
                'name': friendly_name,
                'last_checked': row['last_checked'],
                'download_url': row['download_url'],
                'release_notes_url': rn_urls.get(identifier, ''),
            })
        
        # Sort alphabetically by name
        apps.sort(key=lambda x: x['name'])
        
        return jsonify(apps)

@app.route('/api/app/<path:app_id>/versions')
def get_app_versions(app_id):
    """Get version history for a specific app"""
    with VersionDatabase(DB_PATH) as db:
        cursor = db.conn.cursor()
        cursor.execute("""
            SELECT 
                id,
                first_detected,
                download_url,
                actual_url,
                version,
                size_bytes,
                checksum_sha256,
                package_identifier,
                app_path,
                bundle_id,
                num_files,
                install_kb,
                last_modified,
                etag
            FROM versions
            WHERE package_identifier = ?
            ORDER BY first_detected DESC
        """, (app_id,))
        
        versions = []
        for row in cursor.fetchall():
            # Check if this is Intune Agent (hide sensitive data)
            is_intune_agent = row['package_identifier'] == 'com.microsoft.intuneMDMAgent'
            
            version_data = {
                'id': row['id'],
                'first_detected': row['first_detected'],
                'download_url': '—' if is_intune_agent else row['download_url'],
                'actual_url': '—' if is_intune_agent else row['actual_url'],
                'version': row['version'],
                'size_bytes': row['size_bytes'],
                'size_mb': round(row['size_bytes'] / (1024 * 1024), 2) if row['size_bytes'] else 0,
                'checksum': '—' if is_intune_agent else row['checksum_sha256'],
                'package_identifier': row['package_identifier'],
                'app_path': row['app_path'],
                'bundle_id': row['bundle_id'],
                'num_files': row['num_files'],
                'install_kb': row['install_kb'],
                'last_modified': row['last_modified'],
                'etag': row['etag'],
            }
            
            # Get components for this version (excluding version "0" components)
            cursor.execute("""
                SELECT 
                    package_identifier,
                    version,
                    app_path,
                    bundle_id,
                    install_location,
                    num_files,
                    install_kb
                FROM components
                WHERE version_id = ?
                AND version != '0'
                ORDER BY package_identifier
            """, (row['id'],))
            
            components = []
            for comp in cursor.fetchall():
                # Create friendly name from package identifier
                pkg_id = comp['package_identifier']
                name = pkg_id.split('.')[-1]
                
                # Extract app name from path if available
                if comp['app_path'] and '.app' in comp['app_path']:
                    app_name = comp['app_path'].split('/')[-1].replace('.app', '')
                else:
                    app_name = name.replace('_', ' ').replace('.', ' ').title()
                
                components.append({
                    'name': app_name,
                    'package_identifier': comp['package_identifier'],
                    'version': comp['version'],
                    'app_path': comp['app_path'],
                    'bundle_id': comp['bundle_id'],
                    'install_location': comp['install_location'],
                    'num_files': comp['num_files'],
                    'install_kb': comp['install_kb']
                })
            
            version_data['components'] = components
            versions.append(version_data)
        
        return jsonify(versions)

@app.route('/api/app/<path:app_id>/heatmap')
def get_app_heatmap(app_id):
    """Get update frequency data for heatmap visualization"""
    with VersionDatabase(DB_PATH) as db:
        cursor = db.conn.cursor()
        
        # Get all version dates for this app, grouped by date (not datetime)
        cursor.execute("""
            SELECT 
                DATE(first_detected) as date,
                COUNT(*) as count
            FROM versions
            WHERE package_identifier = ?
            GROUP BY DATE(first_detected)
            ORDER BY date ASC
        """, (app_id,))
        
        heatmap_data = []
        for row in cursor.fetchall():
            heatmap_data.append({
                'date': row['date'],
                'value': row['count']
            })
        
        return jsonify(heatmap_data)

@app.route('/api/stats')
def get_stats():
    """Get overall statistics"""
    with VersionDatabase(DB_PATH) as db:
        cursor = db.conn.cursor()
        
        # Total apps tracked (excluding special entries)
        cursor.execute("""
            SELECT COUNT(DISTINCT package_identifier) as count 
            FROM versions 
            WHERE package_identifier NOT IN ('__manifest_cache__', '__last_check__')
        """)
        apps_count = cursor.fetchone()['count']
        
        # Total versions tracked (excluding special entries)
        cursor.execute("""
            SELECT COUNT(*) as count 
            FROM versions 
            WHERE package_identifier NOT IN ('__manifest_cache__', '__last_check__')
        """)
        versions_count = cursor.fetchone()['count']
        
        # Get last check time
        last_check = db.get_last_check_time()
        
        return jsonify({
            'apps_tracked': apps_count,
            'total_versions': versions_count,
            'last_check': last_check
        })

@app.route('/api/all-versions')
def get_all_versions():
    """Get all versions across all apps, sorted by last updated"""
    with VersionDatabase(DB_PATH) as db:
        cursor = db.conn.cursor()
        cursor.execute("""
            SELECT 
                id,
                first_detected,
                download_url,
                actual_url,
                version,
                size_bytes,
                checksum_sha256,
                package_identifier,
                app_path,
                bundle_id,
                num_files,
                install_kb,
                last_modified,
                etag
            FROM versions
            WHERE package_identifier NOT IN ('__manifest_cache__', '__last_check__')
            ORDER BY first_detected DESC
        """)
        
        versions = []
        for row in cursor.fetchall():
            identifier = row['package_identifier']
            app_name = get_display_name(identifier)
            
            # Check if this is Intune Agent (hide sensitive data)
            is_intune_agent = identifier == 'com.microsoft.intuneMDMAgent'
            
            versions.append({
                'id': row['id'],
                'app_name': app_name,
                'package_identifier': identifier,
                'first_detected': row['first_detected'],
                'download_url': '—' if is_intune_agent else row['download_url'],
                'actual_url': '—' if is_intune_agent else row['actual_url'],
                'version': row['version'],
                'size_bytes': row['size_bytes'],
                'checksum': '—' if is_intune_agent else row['checksum_sha256']
            })
        
        return jsonify(versions)

@app.route('/api/latest')
def get_latest_versions():
    """Get latest version for all tracked apps - script-friendly endpoint"""
    with VersionDatabase(DB_PATH) as db:
        cursor = db.conn.cursor()
        
        # Get all unique apps
        cursor.execute("""
            SELECT DISTINCT package_identifier
            FROM versions
            WHERE package_identifier NOT IN ('__manifest_cache__', '__last_check__')
            ORDER BY package_identifier
        """)
        
        apps = []
        for row in cursor.fetchall():
            pkg_id = row['package_identifier']
            
            # Get the latest version for this app
            cursor.execute("""
                SELECT 
                    package_identifier,
                    version,
                    first_detected,
                    download_url,
                    actual_url,
                    size_bytes,
                    checksum_sha256,
                    app_path,
                    bundle_id,
                    num_files,
                    install_kb,
                    last_modified,
                    etag,
                    id
                FROM versions
                WHERE package_identifier = ?
                ORDER BY first_detected DESC
                LIMIT 1
            """, (pkg_id,))
            
            version_row = cursor.fetchone()
            if not version_row:
                continue
            
            friendly_name = get_display_name(pkg_id)
            
            # Determine if this is a suite package or Intune Agent
            is_suite = pkg_id == 'com.microsoft.suite'
            is_intune_agent = pkg_id == 'com.microsoft.intuneMDMAgent'
            
            app_data = {
                'name': friendly_name,
                'package_identifier': version_row['package_identifier'],
                'type': 'suite' if is_suite else 'application',
                'version': version_row['version'],
                'detected': version_row['first_detected'],
                'download_url': '—' if is_intune_agent else version_row['download_url'],
                'direct_url': '—' if is_intune_agent else version_row['actual_url'],
                'size_bytes': version_row['size_bytes'],
                'size_mb': round(version_row['size_bytes'] / (1024 * 1024), 2) if version_row['size_bytes'] else 0,
                'sha256': '—' if is_intune_agent else version_row['checksum_sha256'],
                'last_modified': version_row['last_modified'],
                'etag': version_row['etag']
            }
            
            # Only include app_path and bundle_id for non-suite packages
            if not is_suite and version_row['app_path']:
                app_data['app_path'] = version_row['app_path']
                app_data['bundle_id'] = version_row['bundle_id']
                app_data['num_files'] = version_row['num_files']
                app_data['install_kb'] = version_row['install_kb']
            
            # Get components for suite packages (excluding version "0")
            cursor.execute("""
                SELECT 
                    package_identifier,
                    version,
                    app_path,
                    bundle_id
                FROM components
                WHERE version_id = ?
                AND version != '0'
                ORDER BY package_identifier
            """, (version_row['id'],))
            
            components = []
            for comp in cursor.fetchall():
                # Extract app name from path
                if comp['app_path'] and '.app' in comp['app_path']:
                    app_name = comp['app_path'].split('/')[-1].replace('.app', '')
                else:
                    app_name = comp['package_identifier'].split('.')[-1]
                
                components.append({
                    'name': app_name,
                    'package_identifier': comp['package_identifier'],
                    'version': comp['version'],
                    'bundle_id': comp['bundle_id'],
                    'app_path': comp['app_path']
                })
            
            if components:
                app_data['components'] = components
                app_data['component_count'] = len(components)
            
            apps.append(app_data)
        
        return jsonify({
            'generated': cursor.execute("SELECT datetime('now')").fetchone()[0],
            'apps': apps
        })

# Subscription Routes
@app.route('/subscribe')
def subscribe_page():
    """Subscription page"""
    available_apps = subscription_manager.get_available_apps()
    return render_template('subscribe.html', apps=available_apps)

@app.route('/subscribe', methods=['POST'])
def subscribe_post():
    """Handle subscription form submission"""
    try:
        email = request.form.get('email', '').strip()
        selected_apps = request.form.getlist('apps')
        
        if not email:
            return redirect(url_for('subscribe_page'))
        
        # Convert "all" selection to empty list (meaning all apps)
        if 'all' in selected_apps:
            selected_apps = []
        
        success, message = subscription_manager.subscribe(email, selected_apps)
        
        if success:
            return render_template('subscribe_success.html', email=email)
        else:
            return render_template('subscribe_error.html', message=message)
            
    except Exception as e:
        flash('An error occurred processing your subscription.', 'error')
        return redirect(url_for('subscribe_page'))

@app.route('/confirm-subscription')
def confirm_subscription():
    """Handle subscription confirmation"""
    token = request.args.get('token')
    
    if not token:
        return render_template('confirm_error.html', message='Invalid confirmation link.')
    
    success, message = subscription_manager.confirm_subscription(token)
    
    if success:
        return render_template('confirm_success.html')
    else:
        return render_template('confirm_error.html', message=message)

@app.route('/resend-confirmation', methods=['POST'])
def resend_confirmation():
    """Re-send a confirmation email for a pending sign-up."""
    email = request.form.get('email', '').strip()

    if not email:
        return redirect(url_for('subscribe_page'))

    success, message = subscription_manager.resend_confirmation(email)
    return render_template('resend_result.html', success=success,
                           message=message, email=email)

@app.route('/manage-subscriptions')
def manage_subscriptions():
    """Show subscription management page"""
    return render_template('manage_subscriptions.html')

@app.route('/unsubscribe', methods=['GET', 'POST'])
def unsubscribe():
    """Handle unsubscribe request.

    GET renders the confirmation page for a human clicking the in-email link.
    POST is the RFC 8058 one-click path: mailbox providers (Gmail, Yahoo, etc.)
    POST to the List-Unsubscribe URL with no UI, so we just action it and
    return a bare 200/400 rather than an HTML page.
    """
    token = request.args.get('token') or request.form.get('token')

    if not token:
        if request.method == 'POST':
            return ('Missing token', 400)
        return render_template('unsubscribe_error.html', message='Invalid unsubscribe link.')

    success, message = subscription_manager.unsubscribe(token)

    if request.method == 'POST':
        # One-click: no rendered page is shown to the user.
        return ('Unsubscribed', 200) if success else (message, 400)

    if success:
        return render_template('unsubscribe_success.html')
    else:
        return render_template('unsubscribe_error.html', message=message)

@app.route('/api/subscription-stats')
def get_subscription_stats():
    """Get subscription statistics for admins"""
    try:
        stats = subscription_manager.get_subscription_stats()
        return jsonify(stats)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ---------------------------------------------------------------------------
# App suggestions (community-recommended apps to track)
# ---------------------------------------------------------------------------

def _client_voter_hash() -> str:
    """Return a stable per-(IP, UA) voter fingerprint for the current request."""
    ip = (request.headers.get('X-Forwarded-For', '').split(',')[0].strip()
          or request.remote_addr or '')
    ua = request.headers.get('User-Agent', '')
    return sdb.voter_hash(ip, ua)


@app.route('/suggest')
def suggest_page():
    """Public page combining the suggestion form and the voting list."""
    return render_template(
        'suggest.html',
        approval_threshold=sdb.get_approval_threshold(),
    )


@app.route('/api/suggestions', methods=['GET'])
def api_list_suggestions():
    items = sdb.list_suggestions_public()
    voted = set(sdb.voted_ids_for(_client_voter_hash()))
    out = []
    for item in items:
        out.append({
            'id': item['id'],
            'name': item['name'],
            'identifier': item['identifier'],
            'download_url': item['download_url'],
            'release_notes_url': item.get('release_notes_url', ''),
            'description': item['description'],
            'status': item['status'],
            'votes': item['votes_count'],
            'created_at': item['created_at'],
            'has_voted': item['id'] in voted,
        })
    return jsonify({
        'suggestions': out,
        'approval_threshold': sdb.get_approval_threshold(),
    })


@app.route('/api/suggestions', methods=['POST'])
@limiter.limit('5 per hour;30 per day', exempt_when=lambda: _DEV_MODE)
def api_submit_suggestion():
    """Accept a public submission. Rate-limited per IP."""
    data = request.get_json(silent=True) or request.form.to_dict() or {}

    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'error': 'Name is required'}), 400
    if len(name) > 200:
        return jsonify({'error': 'Name is too long (max 200 chars)'}), 400

    identifier = (data.get('identifier') or '').strip()[:300]
    download_url = (data.get('download_url') or '').strip()[:1000]
    release_notes_url = (data.get('release_notes_url') or '').strip()[:1000]
    description = (data.get('description') or '').strip()[:2000]
    submitter_email = (data.get('submitter_email') or '').strip()[:320]

    if download_url and not (
        download_url.startswith('http://') or download_url.startswith('https://')
    ):
        return jsonify({'error': 'Download URL must start with http:// or https://'}), 400
    if release_notes_url and not (
        release_notes_url.startswith('http://') or release_notes_url.startswith('https://')
    ):
        return jsonify({'error': 'Release notes URL must start with http:// or https://'}), 400

    # Honeypot field - bots typically fill every input. If present, accept
    # silently so the bot thinks it succeeded.
    if (data.get('website') or '').strip():
        return jsonify({'ok': True}), 201

    # Reject suggestions for apps we already track. Bundle identifier is
    # the most reliable key; fall back to a case-insensitive name match
    # when no identifier was supplied.
    if identifier and adb.get_tracked_app_by_identifier(identifier):
        return jsonify({
            'error': f'"{identifier}" is already in the tracker.'
        }), 409
    if not identifier:
        for app in adb.list_tracked_apps(include_disabled=True):
            if (app.get('name') or '').strip().lower() == name.lower():
                return jsonify({
                    'error': f'"{name}" is already in the tracker.'
                }), 409

    sid = sdb.add_suggestion(
        {
            'name': name,
            'identifier': identifier,
            'download_url': download_url,
            'release_notes_url': release_notes_url,
            'description': description,
            'submitter_email': submitter_email,
        },
        submitter_hash=_client_voter_hash(),
    )
    if sid is None:
        return jsonify({'error': 'A suggestion with that name or identifier already exists'}), 409

    # New suggestions start as 'pending' and cannot be voted on until an
    # admin approves them, so we don't auto-cast a vote here.
    adb.add_log('INFO', 'suggestions', f'New app suggestion: {name} (#{sid})')
    return jsonify({'ok': True, 'id': sid}), 201


@app.route('/api/suggestions/<int:sid>/vote', methods=['POST'])
@limiter.limit('30 per hour;120 per day', exempt_when=lambda: _DEV_MODE)
def api_vote_suggestion(sid: int):
    """Cast a vote for a suggestion. One vote per (IP, UA) fingerprint.

    Only suggestions an admin has approved can collect votes.
    """
    fingerprint = _client_voter_hash()
    suggestion = sdb.get_suggestion(sid)
    if not suggestion:
        return jsonify({'error': 'Suggestion not found'}), 404
    if suggestion['status'] != 'approved':
        return jsonify({'error': 'This suggestion is awaiting moderation and is not open for voting yet.'}), 403
    created, count = sdb.add_vote(sid, fingerprint)
    return jsonify({'ok': True, 'votes': count, 'already_voted': not created})


def _xml_escape(value: str) -> str:
    """Escape characters that aren't safe inside XML text/attribute values."""
    return (
        value.replace('&', '&amp;')
             .replace('<', '&lt;')
             .replace('>', '&gt;')
             .replace('"', '&quot;')
             .replace("'", '&apos;')
    )


@app.route('/sitemap.xml')
def sitemap_xml():
    """Dynamic sitemap.xml advertising public, indexable URLs.

    Includes the homepage, the subscribe page, and a deep link per
    tracked application (the SPA reads ``?app=<identifier>`` and renders
    that app's history). ``lastmod`` for app deep links is the most
    recent ``first_detected`` timestamp for that package; for the
    homepage it is the most recent timestamp across all packages.
    """
    # ``base_url`` (module-level) is ``{site_url}{script_name}`` and
    # therefore already includes the ``/app-tracker`` prefix that nginx
    # strips before proxying. Use it directly so the URLs we publish are
    # the public URLs visitors and crawlers actually hit.
    root = base_url.rstrip('/')

    latest_overall = None
    app_entries = []
    try:
        with VersionDatabase(DB_PATH) as db:
            cursor = db.conn.cursor()
            cursor.execute("""
                SELECT
                    package_identifier,
                    MAX(first_detected) AS lastmod
                FROM versions
                WHERE package_identifier NOT IN ('__manifest_cache__', '__last_check__')
                GROUP BY package_identifier
            """)
            for row in cursor.fetchall():
                identifier = row['package_identifier']
                lastmod = row['lastmod']
                if lastmod and (latest_overall is None or lastmod > latest_overall):
                    latest_overall = lastmod
                app_entries.append((identifier, lastmod))
    except Exception:
        logging.getLogger(__name__).exception("Failed to build sitemap app entries")

    # Sort deterministically so the sitemap is stable between requests.
    app_entries.sort(key=lambda r: r[0])

    def _format_lastmod(value):
        """Normalise SQLite timestamps to a strict W3C datetime in UTC.

        Google's sitemap parser rejects fractional seconds and requires
        an explicit timezone. SQLite stores ``first_detected`` as
        ``YYYY-MM-DD HH:MM:SS[.ffffff]`` (no tz), so we drop any
        fractional component and append ``Z``.
        """
        if not value:
            return None
        text = str(value).strip()
        if not text:
            return None
        # Split off fractional seconds if present.
        text = text.split('.', 1)[0]
        # Normalise the date/time separator.
        text = text.replace(' ', 'T')
        # Strip any pre-existing trailing Z so we don't double it up.
        if text.endswith('Z'):
            text = text[:-1]
        return f'{text}Z'

    urls = []
    home_lastmod = _format_lastmod(latest_overall)
    urls.append({
        'loc': f'{root}/',
        'lastmod': home_lastmod,
        'changefreq': 'hourly',
        'priority': '1.0',
    })
    # Subscribe page content changes whenever an app is added/removed
    # from the tracker, so the latest overall ``first_detected`` is a
    # reasonable proxy for its lastmod.
    urls.append({
        'loc': f'{root}/subscribe',
        'lastmod': home_lastmod,
        'changefreq': 'monthly',
        'priority': '0.5',
    })
    urls.append({
        'loc': f'{root}/suggest',
        'lastmod': home_lastmod,
        'changefreq': 'weekly',
        'priority': '0.5',
    })
    for identifier, lastmod in app_entries:
        urls.append({
            'loc': f'{root}/?app={_xml_escape(identifier)}',
            'lastmod': _format_lastmod(lastmod),
            'changefreq': 'daily',
            'priority': '0.8',
        })

    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for entry in urls:
        lines.append('  <url>')
        lines.append(f'    <loc>{_xml_escape(entry["loc"])}</loc>')
        if entry['lastmod']:
            lines.append(f'    <lastmod>{entry["lastmod"]}</lastmod>')
        lines.append(f'    <changefreq>{entry["changefreq"]}</changefreq>')
        lines.append(f'    <priority>{entry["priority"]}</priority>')
        lines.append('  </url>')
    lines.append('</urlset>')

    body = '\n'.join(lines) + '\n'
    return Response(body, mimetype='application/xml')


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
