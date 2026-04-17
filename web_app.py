#!/usr/bin/env python3
"""
Flask web application and REST API for Mac Apps Version Tracker.

Serves the main UI for browsing release history and update heatmaps,
the subscription pages for email sign-up, and the JSON API consumed
by automation scripts.
"""

from flask import Flask, jsonify, render_template, send_from_directory, request, redirect, url_for
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from jinja2 import ChoiceLoader, FileSystemLoader
from werkzeug.middleware.proxy_fix import ProxyFix
from tracker.database import VersionDatabase
from notifications.manager import SubscriptionManager
from tracker.config import load_apps_config, build_identifier_lookup
import admin.database as adb
from admin import admin_bp
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
    return {
        'site_name': site_name,
        'brand_name': brand_name,
        'brand_url': os.environ.get('BRAND_URL', '/'),
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
    return render_template('index.html', dev_mode=dev_mode)

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
        
        apps = []
        for row in cursor.fetchall():
            identifier = row['package_identifier']
            friendly_name = get_display_name(identifier)
            
            apps.append({
                'id': identifier,
                'name': friendly_name,
                'last_checked': row['last_checked'],
                'download_url': row['download_url']
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
                'etag': row['etag']
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

@app.route('/manage-subscriptions')
def manage_subscriptions():
    """Show subscription management page"""
    return render_template('manage_subscriptions.html')

@app.route('/unsubscribe')
def unsubscribe():
    """Handle unsubscribe request"""
    token = request.args.get('token')
    
    if not token:
        return render_template('unsubscribe_error.html', message='Invalid unsubscribe link.')
    
    success, message = subscription_manager.unsubscribe(token)
    
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

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
