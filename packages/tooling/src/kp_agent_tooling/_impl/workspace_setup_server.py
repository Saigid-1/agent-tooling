"""Authenticated loopback HTTP transport for operator-owned workspace setup."""

from http.server import BaseHTTPRequestHandler, HTTPServer
import hmac
import json
from pathlib import Path
import secrets
import subprocess
from threading import Lock
from urllib.parse import urlsplit

from kp_agent_tooling._impl.workspace_setup import apply, catalog, plan


MAX_BODY = 20 * 1024
ASSETS = {'/': ('index.html', 'text/html; charset=utf-8'),
          '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
          '/style.css': ('style.css', 'text/css; charset=utf-8')}


def _asset_dir():
    source = Path(__file__).with_name('workspace_ui')
    if source.is_dir():
        return source
    return Path(__file__).resolve().parents[1] / 'assets' / 'workspace_ui'


def _reject_constant(_):
    raise ValueError('nonstandard JSON constant')


def _is_conflict(reason):
    return any(marker in reason for marker in (
        'inputs changed', 'working-tree changes', 'target checkout mismatch',
        'compiler digest mismatch', 'existing bundle'))


class WorkspaceSetupServer(HTTPServer):
    """One operator session, bound exclusively to IPv4 loopback."""

    def __init__(self, port=0):
        if not isinstance(port, int) or not 0 <= port <= 65535:
            raise ValueError('port must be between 0 and 65535')
        self._token = secrets.token_urlsafe(32)
        self._apply_lock = Lock()
        super().__init__(('127.0.0.1', port), SetupHandler)

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(10)
        return connection, address

    @property
    def origin(self):
        return f'http://127.0.0.1:{self.server_port}'

    @property
    def launch_url(self):
        return f'{self.origin}/#token={self._token}'

    def authorized(self, value):
        prefix = 'Bearer '
        if not value or not value.startswith(prefix):
            return False
        try:
            candidate = value[len(prefix):].encode('ascii')
        except UnicodeEncodeError:
            return False
        return hmac.compare_digest(candidate, self._token.encode('ascii'))


class SetupHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, format, *args):
        # Request paths and headers are deliberately never logged.
        pass

    def _send(self, status, body, content_type='application/json; charset=utf-8'):
        self.close_connection = True
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Connection', 'close')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status, value):
        self._send(status, json.dumps(value, allow_nan=False, separators=(',', ':')).encode())

    def _error(self, status, reason):
        self._json(status, {'status': 'error', 'reason': reason})

    def _route(self):
        # Queries are not part of this small protocol and cannot select assets.
        try:
            parsed = urlsplit(self.path)
        except ValueError:
            return None
        return parsed.path if not parsed.query and not parsed.fragment else None

    def _same_origin(self):
        host = self.headers.get_all('Host', [])
        if host != [f'127.0.0.1:{self.server.server_port}']:
            return False
        origin = self.headers.get_all('Origin', [])
        if origin and origin != [self.server.origin]:
            return False
        if self.headers.get('Sec-Fetch-Site') == 'cross-site':
            return False
        return True

    def _authorize_api(self):
        authorization = self.headers.get_all('Authorization', [])
        if len(authorization) != 1 or not self.server.authorized(authorization[0]):
            self._error(401, 'authorization required')
            return False
        return True

    def do_GET(self):
        if not self._same_origin():
            self._error(400, 'invalid origin or host')
            return
        route = self._route()
        if route == '/api/catalog':
            if self._authorize_api():
                self._json(200, catalog())
            return
        if route not in ASSETS:
            self._error(404, 'route not found')
            return
        name, content_type = ASSETS[route]
        try:
            body = (_asset_dir() / name).read_bytes()
        except OSError:
            self._error(500, 'setup page unavailable')
            return
        self._send(200, body, content_type)

    def do_POST(self):
        if not self._same_origin():
            self._error(400, 'invalid origin or host')
            return
        route = self._route()
        if route not in ('/api/plan', '/api/apply'):
            self._error(404, 'route not found')
            return
        if not self._authorize_api():
            return
        if self.headers.get('Transfer-Encoding') or len(self.headers.get_all('Content-Length', [])) != 1:
            self._error(400, 'content length required')
            return
        try:
            length = int(self.headers['Content-Length'])
        except ValueError:
            self._error(400, 'invalid content length')
            return
        if length < 1 or length > MAX_BODY:
            self._error(400, 'request body exceeds limit or is empty')
            return
        if self.headers.get('Content-Type', '').split(';', 1)[0].strip().lower() != 'application/json':
            self._error(400, 'application/json required')
            return
        try:
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError('incomplete request body')
            payload = json.loads(raw, parse_constant=_reject_constant)
        except (ValueError, UnicodeDecodeError, TimeoutError, OSError, RecursionError):
            self._error(400, 'invalid JSON')
            return
        required = {'request'} if route == '/api/plan' else {'request', 'expected_plan_sha256'}
        if not isinstance(payload, dict) or payload.keys() != required:
            self._error(400, 'invalid request envelope')
            return
        if route == '/api/apply' and (not isinstance(payload['expected_plan_sha256'], str) or
                                       len(payload['expected_plan_sha256']) != 64 or
                                       any(ch not in '0123456789abcdef' for ch in payload['expected_plan_sha256'])):
            self._error(400, 'invalid plan digest')
            return
        try:
            if route == '/api/plan':
                result = plan(payload['request'])
            else:
                with self.server._apply_lock:
                    result = apply(payload['request'], payload['expected_plan_sha256'])
        except ValueError as error:
            reason = str(error)
            status = 409 if route == '/api/apply' and _is_conflict(reason) else 400
            self._error(status, reason)
            return
        except (TypeError, FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            self._error(400, 'invalid or unavailable setup input')
            return
        except Exception:
            self._error(500, 'setup operation failed')
            return
        self._json(200, result)
