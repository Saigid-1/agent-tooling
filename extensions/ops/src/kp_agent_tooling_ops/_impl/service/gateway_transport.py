"""Bounded, non-disclosing HTTP/JSON-RPC client for the optional OPS gateway."""

from __future__ import annotations

import http.client
import json
import os
import ipaddress
import time
from urllib.parse import urlsplit
from collections.abc import Mapping


MAX_RESPONSE_BYTES = 2_000_000
# Context composes retrieval and navigation; its backend may spend 45s on
# navigation alone. Keep the ordinary RPC bound, with a finite composition budget.
CONTEXT_TIMEOUT_SECONDS = 180
DEFAULT_TIMEOUT_SECONDS = 45


class GatewayFailure(RuntimeError):
    """A fixed-code boundary result; remote bodies and exception text stay private."""

    def __init__(self, reason: str, category: str, *, retryable: bool = False, diagnostics=None):
        super().__init__(reason)
        self.reason = reason
        self.category = category
        self.retryable = retryable
        self.diagnostics = diagnostics or {}

    def result(self, *, stage: str = 'call') -> dict[str, object]:
        next_step = {
            'auth': 'review the existing gateway credential configuration',
            'configuration': 'review the configured gateway endpoint',
            'request': 'review arguments and target against the operation schema',
            'timeout': 'check gateway responsiveness before retrying',
            'unavailable': 'check gateway health before retrying',
            'protocol': 'review gateway protocol compatibility',
            'execution': 'inspect backend health or use scoped local evidence',
        }[self.category]
        return {
            'status': 'error' if self.category in ('request', 'execution') else 'unavailable',
            'reason': self.reason,
            'category': self.category,
            'retryable': self.retryable,
            'stage': stage,
            'next_step': next_step,
            'evidence_status': 'not_obtained',
            **self.diagnostics,
        }


def _http_failure(status: int) -> GatewayFailure:
    if status in (401, 403):
        return GatewayFailure('gateway_auth_failed', 'auth')
    if status in (400, 422):
        return GatewayFailure('gateway_request_rejected', 'request')
    if status == 404:
        return GatewayFailure('gateway_endpoint_unavailable', 'configuration')
    if status == 408 or status == 504:
        return GatewayFailure('gateway_timeout', 'timeout', retryable=True)
    if status == 429:
        return GatewayFailure('gateway_rate_limited', 'unavailable', retryable=True)
    if status >= 500:
        return GatewayFailure('gateway_service_unavailable', 'unavailable', retryable=True)
    return GatewayFailure('gateway_http_failure', 'unavailable')


def catalog_tools(result: object, allowed_names: set[str]) -> dict[str, dict]:
    """Accept only complete advertised descriptors; reject malformed catalogs."""

    import jsonschema
    rows = result.get('tools') if isinstance(result, Mapping) else None
    if not isinstance(rows, list):
        raise GatewayFailure('gateway_catalog_invalid', 'protocol')
    catalog = {}
    seen = set()
    for row in rows:
        if not isinstance(row, Mapping):
            raise GatewayFailure('gateway_catalog_invalid', 'protocol')
        name = row.get('name')
        if not isinstance(name, str) or not name or name in seen:
            raise GatewayFailure('gateway_catalog_invalid', 'protocol')
        seen.add(name)
        if name not in allowed_names:
            continue
        if not isinstance(row.get('description'), str) or not isinstance(row.get('inputSchema'), Mapping):
            raise GatewayFailure('gateway_catalog_invalid', 'protocol')
        try:
            jsonschema.Draft202012Validator.check_schema(row['inputSchema'])
        except (jsonschema.SchemaError, RecursionError, TypeError, ValueError):
            raise GatewayFailure('gateway_catalog_invalid', 'protocol') from None
        catalog[name] = dict(row)
    return catalog


DEFAULT_ENDPOINT = 'http://127.0.0.1:8400/api/mcp'


def endpoint_parts(endpoint):
    """Operator-selected endpoint; credentials belong in a separate private file."""
    value = urlsplit(endpoint)
    if (value.scheme not in {'http', 'https'} or not value.hostname or
            value.username is not None or value.password is not None or
            value.query or value.fragment or not value.path.startswith('/') or
            any(c.isspace() for c in endpoint)):
        raise ValueError('explicit HTTP(S) endpoint without inline credentials required')
    _ = value.port
    # Docker Desktop's explicit host bridge is a local single-owner transport.
    loopback = value.hostname == 'localhost' or (
        value.hostname == 'host.docker.internal' and os.environ.get('OPS_ALLOW_DOCKER_HOST_HTTP') == '1')
    try:
        loopback = loopback or ipaddress.ip_address(value.hostname).is_loopback
    except ValueError:
        pass
    if value.scheme == 'http' and not loopback:
        raise ValueError('HTTPS required for non-loopback gateway endpoints')
    return value


def rpc(method: str, params: Mapping[str, object], headers: Mapping[str, str],
        *, endpoint: str = DEFAULT_ENDPOINT) -> object:
    """Send one configured request; no implicit retry or remote text in errors."""

    url = endpoint_parts(endpoint)
    connection = http.client.HTTPSConnection if url.scheme == 'https' else http.client.HTTPConnection
    timeout_seconds = (CONTEXT_TIMEOUT_SECONDS if method == 'tools/call' and
                       params.get('name') == 'knowledge.context' else DEFAULT_TIMEOUT_SECONDS)
    started = time.monotonic()
    conn = connection(url.hostname, url.port or (443 if url.scheme == 'https' else 80), timeout=timeout_seconds)
    try:
        request_headers = {**headers, 'Content-Type': 'application/json',
                           'Accept': 'application/json', 'MCP-Protocol-Version': '2025-06-18'}
        body = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params})
        conn.request('POST', url.path, body, request_headers)
        response = conn.getresponse()
        raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise GatewayFailure('gateway_response_oversized', 'protocol')
        if response.status != 200:
            raise _http_failure(response.status)
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeDecodeError, RecursionError):
            raise GatewayFailure('gateway_response_invalid', 'protocol') from None
        if not isinstance(data, dict) or data.get('jsonrpc') != '2.0' or data.get('id') != 1:
            raise GatewayFailure('gateway_response_invalid', 'protocol')
        if 'error' in data:
            error = data['error']
            code = error.get('code') if isinstance(error, dict) else None
            if code in (-32600, -32601, -32602, -32700):
                raise GatewayFailure('gateway_request_rejected', 'request',
                    diagnostics={'rpc_error_code': code, 'request_diagnostic': {
                        -32600: 'invalid_rpc_request', -32601: 'operation_not_available',
                        -32602: 'arguments_rejected_by_gateway', -32700: 'invalid_rpc_encoding'}[code]})
            raise GatewayFailure('gateway_service_unavailable', 'unavailable', retryable=True)
        if 'result' not in data or not isinstance(data['result'], dict):
            raise GatewayFailure('gateway_response_invalid', 'protocol')
        if data['result'].get('isError') is True:
            raise GatewayFailure('gateway_tool_failed', 'execution')
        return data['result']
    except TimeoutError:
        raise GatewayFailure('gateway_timeout', 'timeout', retryable=True, diagnostics={
            'timeout_seconds': timeout_seconds, 'elapsed_seconds': round(time.monotonic() - started, 3),
            'timeout_scope': 'gateway_http_socket_wait'}) from None
    except (OSError, http.client.HTTPException):
        raise GatewayFailure('gateway_unavailable', 'unavailable', retryable=True) from None
    finally:
        try:
            conn.close()
        except OSError:
            pass
