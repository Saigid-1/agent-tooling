"""Bounded, single-attempt HTTP(S) transport for model-gateway provider calls.

One call sends at most one request: no retry, no redirect following, no proxy.
Errors use a fixed vocabulary. Provider error bodies are reduced to the
summarizer's content-free diagnostic vocabulary (``safe_provider_error``); no
provider message, header or credential is ever retained. Plain HTTP is allowed
only for loopback hosts unless the operator opts a provider in explicitly.
"""
from __future__ import annotations

import ipaddress
import json
import re
import socket
import threading
import time
from dataclasses import dataclass, field
from http.client import HTTPConnection, HTTPSConnection
from urllib.parse import urlsplit

# Reuse, not refactor: the summarizer's sanitizers are the shared vocabulary.
from kp_agent_tooling._impl.service.episodic_summarizer import RESPONSE_ID_HEADERS, safe_provider_error


MAX_ERROR_BODY_BYTES = 16384
RESPONSE_ID_PATTERN = re.compile(r'[A-Za-z0-9_.:-]{1,128}')
ID_HEADERS = ('x-generation-id',) + tuple(RESPONSE_ID_HEADERS)

# Fixed failure vocabulary; nothing else is ever placed in an exception message.
FAILURES = frozenset({
    'provider_http_error', 'provider_timeout', 'provider_transport_error',
    'provider_response_oversized', 'provider_response_invalid',
    'provider_response_unsupported', 'provider_response_contains_secret',
})


class ProviderCallFailed(RuntimeError):
    """A provider call failed. ``request_sent`` is False only when no request
    bytes can have reached the provider (for example a refused connection)."""

    def __init__(self, failure, *, request_sent, http_status=None, provider_error=None,
                 response_ids=None):
        if failure not in FAILURES:
            failure = 'provider_transport_error'
        super().__init__(failure)
        self.failure = failure
        self.request_sent = bool(request_sent)
        self.http_status = http_status if type(http_status) is int and 100 <= http_status <= 599 else None
        self.provider_error = provider_error or {}
        self.response_ids = response_ids or {}


@dataclass(frozen=True)
class Endpoint:
    scheme: str
    host: str
    port: int | None
    base_path: str

    def target(self, path):
        """A displayable request target; never carries credentials or a query."""
        netloc = self.host if ':' not in self.host else '[' + self.host + ']'
        if self.port is not None:
            netloc += ':' + str(self.port)
        return f'{self.scheme}://{netloc}{self.base_path}{path}'


@dataclass(frozen=True)
class HttpResponse:
    status: int
    content_type: str | None
    body: bytes
    response_ids: dict = field(default_factory=dict)


def is_loopback(host):
    if host == 'localhost':
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def parse_base_url(value, *, allow_plain_http=False):
    if not isinstance(value, str) or not value or len(value) > 512 or any(ord(c) < 33 for c in value):
        raise ValueError('base_url must be a bounded URL without whitespace')
    parts = urlsplit(value)
    if parts.scheme not in ('https', 'http'):
        raise ValueError('base_url scheme must be https (or http for loopback)')
    if parts.username is not None or parts.password is not None or '@' in parts.netloc:
        raise ValueError('base_url must not carry credentials')
    if parts.query or parts.fragment:
        raise ValueError('base_url must not carry a query or fragment')
    host = parts.hostname
    if not host:
        raise ValueError('base_url host required')
    try:
        port = parts.port
    except ValueError:
        raise ValueError('base_url port invalid') from None
    if parts.scheme == 'http' and not (is_loopback(host) or allow_plain_http is True):
        raise ValueError('plain http is allowed only for loopback hosts unless allow_plain_http is set')
    return Endpoint(parts.scheme, host, port, parts.path.rstrip('/'))


def _sanitize_ids(response):
    ids = {}
    for name in ID_HEADERS:
        value = response.getheader(name)
        if isinstance(value, str) and RESPONSE_ID_PATTERN.fullmatch(value):
            ids[name] = value
    return ids


def request(endpoint, method, path, *, api_key, timeout, max_response_bytes,
            body=None, content_type=None, accept='application/json'):
    """Send exactly one request and return a bounded successful response.

    Raises ProviderCallFailed for every failure, with ``request_sent`` telling
    whether the provider may have received (and billed) the request.
    """
    if not isinstance(endpoint, Endpoint):
        raise TypeError('endpoint required')
    connection_class = HTTPSConnection if endpoint.scheme == 'https' else HTTPConnection
    connection = connection_class(endpoint.host, endpoint.port, timeout=timeout)
    expired = threading.Event()
    sent = [False]

    def expire():
        expired.set()
        active = connection.sock
        if active is not None:
            try:
                active.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    timer = threading.Timer(timeout, expire)
    timer.daemon = True
    timer.start()
    started = time.monotonic()
    try:
        connection.connect()
        if expired.is_set():
            raise TimeoutError()
        headers = {'Authorization': 'Bearer ' + api_key, 'Accept': accept}
        if body is not None:
            headers['Content-Type'] = content_type
        sent[0] = True
        connection.request(method, endpoint.base_path + path, body=body, headers=headers)
        response = connection.getresponse()
        if expired.is_set():
            raise TimeoutError()
        ids = _sanitize_ids(response)
        ctype = response.getheader('Content-Type')
        ctype = ctype.split(';')[0].strip().lower() if isinstance(ctype, str) else None
        if response.status != 200:
            provider_error = {}
            try:
                if connection.sock is not None:
                    connection.sock.settimeout(min(2, timeout))
                chunks, size = [], 0
                while size <= MAX_ERROR_BODY_BYTES:
                    chunk = response.read1(min(4096, MAX_ERROR_BODY_BYTES + 1 - size))
                    if not chunk:
                        break
                    size += len(chunk)
                    chunks.append(chunk)
                    if time.monotonic() - started >= timeout:
                        break
                if size <= MAX_ERROR_BODY_BYTES:
                    envelope = json.loads(b''.join(chunks))
                    if isinstance(envelope, dict):
                        provider_error = safe_provider_error(envelope.get('error'))
            except Exception:
                provider_error = {}
            raise ProviderCallFailed('provider_http_error', request_sent=True, http_status=response.status,
                                     provider_error=provider_error, response_ids=ids)
        declared = response.getheader('Content-Length')
        if isinstance(declared, str) and declared.isdigit() and int(declared) > max_response_bytes:
            raise ProviderCallFailed('provider_response_oversized', request_sent=True, response_ids=ids)
        chunks, size = [], 0
        while size <= max_response_bytes:
            chunk = response.read1(min(1 << 20, max_response_bytes + 1 - size))
            if expired.is_set():
                raise TimeoutError()
            if not chunk:
                break
            size += len(chunk)
            chunks.append(chunk)
        if size > max_response_bytes:
            raise ProviderCallFailed('provider_response_oversized', request_sent=True, response_ids=ids)
        data = b''.join(chunks)
        if api_key.encode() in data:
            # A provider echoing the credential must never reach an artifact or a result.
            raise ProviderCallFailed('provider_response_contains_secret', request_sent=True, response_ids=ids)
        return HttpResponse(response.status, ctype, data, ids)
    except ProviderCallFailed:
        raise
    except Exception as error:
        if expired.is_set() or isinstance(error, (TimeoutError, socket.timeout)):
            raise ProviderCallFailed('provider_timeout', request_sent=sent[0]) from None
        raise ProviderCallFailed('provider_transport_error', request_sent=sent[0]) from None
    finally:
        timer.cancel()
        connection.close()
