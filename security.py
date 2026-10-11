"""Request protections shared by the local and hosted Flask application."""
from contextlib import closing
import hashlib
import hmac
from pathlib import Path
import secrets
import sqlite3
import time
from urllib.parse import unquote, urlsplit

from flask import abort, current_app, g, jsonify, request, session
from i18n import translate
from werkzeug.exceptions import HTTPException


DEFAULT_LIMITS = {
    'login': [('ip', 20, 900), ('ip_email', 10, 900)],
    'register': [('ip', 5, 3600), ('global', 100, 86400)],
    'analyze': [('user', 10, 3600), ('ip', 30, 3600), ('global', 100, 86400)],
    'result_image': [('user', 60, 60), ('ip', 120, 60)],
    'consult_send': [('user', 10, 60), ('user', 100, 86400),
                     ('ip', 30, 60), ('global', 300, 86400)],
}


def session_secret(configured, directory):
    if configured and len(configured) >= 32 and configured not in ('change-me', 'dev-secret'):
        return configured
    path = Path(directory) / 'session.key'
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open('x', encoding='ascii') as stream:
            path.chmod(0o600)
            stream.write(secrets.token_hex(32))
    except FileExistsError:
        pass
    value = path.read_text(encoding='ascii').strip()
    if len(value) < 32:
        raise RuntimeError('Invalid private session key file')
    return value


def safe_next(value):
    """Accept only local absolute paths, including after URL decoding."""
    value = value or ''
    decoded = value
    for _ in range(4):
        updated = unquote(decoded)
        if updated == decoded:
            break
        decoded = updated
    if '%' in decoded or any(ord(c) < 32 or ord(c) == 127 for c in decoded):
        return None
    if not decoded.startswith('/') or decoded.startswith('//') or '\\' in decoded:
        return None
    parts = urlsplit(decoded)
    return value if not parts.scheme and not parts.netloc else None


def reserve_global_quota():
    """Reserve shared capacity only after the view has validated its input."""
    return current_app.extensions['reserve_global_quota']()


def init_security(app, database):
    app.config.update(
        MAX_CONTENT_LENGTH=8 * 1024 * 1024,
        MAX_FORM_MEMORY_SIZE=64 * 1024,
        MAX_FORM_PARTS=12,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE='Lax',
        PERMANENT_SESSION_LIFETIME=86400,
        RATE_LIMITS=DEFAULT_LIMITS,
        MAX_IMAGE_PIXELS=16_000_000,
        MAX_UPLOAD_STORAGE=200 * 1024 * 1024,
        MAX_USER_UPLOAD_STORAGE=20 * 1024 * 1024,
    )
    with closing(sqlite3.connect(database)) as conn, conn:
        conn.execute('CREATE TABLE IF NOT EXISTS security_limits (key TEXT PRIMARY KEY, count INTEGER NOT NULL, expires REAL NOT NULL)')
        conn.execute('CREATE TABLE IF NOT EXISTS security_leases (key TEXT PRIMARY KEY, token TEXT NOT NULL, expires REAL NOT NULL)')

    def csrf_token():
        if '_csrf' not in session:
            session['_csrf'] = secrets.token_urlsafe(32)
        return session['_csrf']

    app.jinja_env.globals['csrf_token'] = csrf_token

    def error_response(message, status):
        if request.endpoint == 'consult_send':
            return app.make_response((jsonify(ok=False, error=message), status))
        return app.make_response((message, status))

    @app.errorhandler(HTTPException)
    def request_error(error):
        if request.endpoint == 'consult_send':
            return error_response(error.description, error.code)
        return error

    def consume_limits(limits, subjects, now):
        if not limits:
            return None
        with closing(sqlite3.connect(database, timeout=5)) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute('DELETE FROM security_limits WHERE expires <= ?', (now,))
            buckets = []
            for kind, maximum, seconds in limits:
                raw = f'{request.endpoint}:{kind}:{seconds}:{subjects[kind]}'
                key = hmac.new(app.secret_key.encode(), raw.encode(), hashlib.sha256).hexdigest()
                row = conn.execute('SELECT count, expires FROM security_limits WHERE key = ?', (key,)).fetchone()
                if row and row[0] >= maximum:
                    response = error_response('Too many requests. Please try again later.', 429)
                    response.headers['Retry-After'] = str(max(1, int(row[1] - now) + 1))
                    return response
                buckets.append((key, row[0] + 1 if row else 1, row[1] if row else now + seconds))
            conn.executemany('INSERT OR REPLACE INTO security_limits VALUES (?, ?, ?)', buckets)

        return None

    def reserve_shared_capacity():
        if getattr(g, 'global_quota_reserved', False):
            return None
        limits = [limit for limit in app.config['RATE_LIMITS'].get(request.endpoint, [])
                  if limit[0] == 'global']
        limited = consume_limits(limits, {'global': 'all'}, time.time())
        if limited is None:
            g.global_quota_reserved = True
        return limited

    app.extensions['reserve_global_quota'] = reserve_shared_capacity

    @app.before_request
    def protect_request():
        g.csp_nonce = secrets.token_urlsafe(24)
        mutating = request.method in ('POST', 'PUT', 'PATCH', 'DELETE')
        if not mutating and request.endpoint != 'result_image':
            return
        if request.endpoint == 'result_image' and not session.get('auth_user'):
            return  # The view's login_required wrapper handles the redirect.
        if request.endpoint in ('analyze', 'consult_send') and not session.get('auth_user'):
            if request.endpoint == 'consult_send':
                return jsonify({'ok': False, 'error': translate('Unauthorized')}), 401
            abort(401)
        if mutating:
            expected = session.get('_csrf', '')
            provided = request.headers.get('X-CSRF-Token') or request.form.get('csrf_token', '')
            if not expected or not hmac.compare_digest(expected.encode(), provided.encode()):
                abort(400, description='Invalid or missing CSRF token. Reload the page.')

        now = time.time()
        # Do not trust client-supplied X-Forwarded-For. Host proxy configuration
        # must be reviewed separately before changing the trusted client address.
        client_ip = request.remote_addr or 'unknown'
        email = request.form.get('email', '').strip().casefold()
        subjects = {'ip': client_ip,
                    'user': session.get('auth_user', ''),
                    'ip_email': f'{len(client_ip)}:{client_ip}:{email}',
                    'global': 'all'}
        limits = app.config['RATE_LIMITS'].get(request.endpoint, [])
        limited = consume_limits([limit for limit in limits if limit[0] != 'global'], subjects, now)
        if limited is not None:
            return limited

        # A process-shared lease bounds concurrent ML/API work and serializes
        # quota checks. Expiry recovers automatically after a killed worker.
        if request.endpoint in ('analyze', 'consult_send', 'result_image'):
            token = secrets.token_hex(16)
            with closing(sqlite3.connect(database, timeout=5)) as conn, conn:
                conn.execute('BEGIN IMMEDIATE')
                conn.execute('DELETE FROM security_leases WHERE expires <= ?', (now,))
                if conn.execute('SELECT 1 FROM security_leases WHERE key = ?', ('heavy',)).fetchone():
                    response = error_response('Service busy. Please try again shortly.', 429)
                    response.headers['Retry-After'] = '5'
                    return response
                conn.execute('INSERT INTO security_leases VALUES (?, ?, ?)', ('heavy', token, now + 300))
                g.work_lease = token

    @app.teardown_request
    def release_work(_error):
        token = getattr(g, 'work_lease', None)
        if token:
            with closing(sqlite3.connect(database, timeout=5)) as conn, conn:
                conn.execute('DELETE FROM security_leases WHERE key = ? AND token = ?', ('heavy', token))

    @app.after_request
    def security_headers(response):
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Permissions-Policy'] = 'camera=(), microphone=(), geolocation=()'
        nonce = getattr(g, 'csp_nonce', '')
        response.headers['Content-Security-Policy'] = (
            "default-src 'self'; base-uri 'self'; object-src 'none'; frame-ancestors 'none'; "
            "form-action 'self'; connect-src 'self'; img-src 'self' data:; "
            f"script-src 'self' 'nonce-{nonce}' https://cdn.jsdelivr.net; "
            "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://fonts.googleapis.com; "
            "font-src 'self' https://cdn.jsdelivr.net https://fonts.gstatic.com"
        )
        if request.is_secure:
            response.headers['Strict-Transport-Security'] = 'max-age=31536000'
        if request.endpoint != 'static':
            response.headers['Cache-Control'] = 'no-store, private'
        if request.cookies.get('user_id'):
            response.delete_cookie('user_id')
        return response
