"""Model gateway: a capability routing table with budgets, artifacts and provenance.

The configuration (``agent-tooling.model-gateway.v1``) is re-read for every tool
listing and every call, so a route change takes effect with no code change and
no restart. Each configured capability becomes one ``model.<capability>`` tool.

Ordering of a call; every step before the request is free and network-silent:
route -> modality support -> arguments -> budget configured -> input artifact
-> request built and digested -> key file checked -> caps and confirmation
reserved in a persistent ledger -> exactly one request -> artifacts and
provenance written atomically under ``artifact_root`` -> optional desk record.

API keys are read only from each provider's private ``api_key_file`` and only
to build the Authorization header. They never enter results, errors,
provenance, artifacts, the ledger or memory.

The content-free mode (S1a P2), ``ModelGateway.complete_content_free``, is the summarizer
role's one completion path. It keeps the same pre-network order (route, budget configured
and priced, key file, reservation in one IMMEDIATE transaction, exactly one request, finish
with the cost) and returns the decoded response to its caller in memory only: it writes its
ledger row and nothing else, no output artifact, no provenance file and no desk memory record.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
import os
import re
import secrets
import shutil
import stat
import time
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import jsonschema

from kp_agent_tooling._impl import leaf
from kp_agent_tooling._impl.service import model_gateway_http as http
from kp_agent_tooling._impl.service.model_gateway_providers import (
    DEFAULT_OPERATIONS, MAX_INLINE_AUDIO_BYTES, MAX_INPUT_ARTIFACT_BYTES, MAX_RESPONSE_BYTES,
    MEDIA_EXTENSIONS, OPERATIONS, PROVIDER_KINDS, PROVIDER_PATHS, RESERVED_PARAMS,
    build_request, input_schema, parse_response, safe_usage)


SCHEMA_VERSION = 'agent-tooling.model-gateway.v1'
PROVENANCE_SCHEMA = 'agent-tooling.model-gateway.provenance.v1'
DOCTOR_SCHEMA = 'agent-tooling.model-gateway.doctor.v1'
TOOL_PREFIX = 'model.'
STATE_DIR = '.model-gateway'
OUTPUT_DIR = 'outputs'

CAPABILITY = re.compile(r'^[a-z][a-z0-9_]{0,31}(\.[a-z][a-z0-9_]{0,31}){1,2}$')
PROVIDER_NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$')
MAX_CONFIG_BYTES = 256 * 1024
MAX_PARAMS_BYTES = 8192
MAX_ARGUMENT_BYTES = 512 * 1024
MAX_KEY_FILE_BYTES = 4096
HOUR, DAY, ESTIMATE_WINDOW = 3600, 86400, 30 * 86400
LIVE_MODALITIES = {'image': 'image', 'speech': 'speech', 'transcription': 'transcription', 'chat': 'text'}


class GatewayConfigError(ValueError):
    """The gateway configuration is invalid; the message names no secret."""


class SecretRefused(RuntimeError):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class InputUnavailable(ValueError):
    pass


@dataclass(frozen=True)
class Provider:
    name: str
    kind: str
    base_url: str
    endpoint: http.Endpoint
    api_key_file: Path
    timeout: float
    capabilities: frozenset | None


@dataclass(frozen=True)
class Route:
    capability: str
    provider: str
    model: str
    params: dict
    operation: str | None


@dataclass(frozen=True)
class Budget:
    max_calls_per_hour: int
    max_usd_per_day: Decimal
    confirm_over_usd: Decimal | None
    estimated_usd_per_call: Decimal | None


@dataclass(frozen=True)
class GatewayConfig:
    path: Path
    artifact_root: Path
    providers: dict
    routes: dict
    budgets: dict
    memory_config: Path | None

    def operation(self, capability):
        """The wire operation for a route, or None when the modality is unsupported."""
        route = self.routes[capability]
        provider = self.providers[route.provider]
        operation = route.operation or DEFAULT_OPERATIONS.get(capability)
        if operation not in PROVIDER_PATHS[provider.kind]:
            return None
        if provider.capabilities is not None and capability not in provider.capabilities:
            return None
        return operation


# ---------------------------------------------------------------- configuration

def _keys(value, required, optional, where):
    if not isinstance(value, dict):
        raise GatewayConfigError(where + ' must be an object')
    missing = required - set(value)
    extra = set(value) - required - optional
    if missing or extra:
        raise GatewayConfigError(f'{where}: missing {sorted(missing)} unexpected {sorted(extra)}')


def _usd(value, where):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) \
            or not 0 <= value <= 1_000_000:
        raise GatewayConfigError(where + ' must be a finite non-negative number of USD')
    return Decimal(str(value))


def _absolute(value, where):
    if not isinstance(value, str) or not value or len(value) > 4096 or '\x00' in value or not os.path.isabs(value):
        raise GatewayConfigError(where + ' must be an absolute path')
    return Path(value)


def _label(value, where):
    if not isinstance(value, str) or not 1 <= len(value) <= 256 or any(ord(c) < 33 or ord(c) == 127 for c in value):
        raise GatewayConfigError(where + ' must be a bounded identifier without whitespace')
    return value


def load_config(path, *, memory_config=None):
    path = Path(os.path.abspath(path))
    try:
        if not path.is_file():
            raise GatewayConfigError('gateway configuration file not found')
        if path.stat().st_size > MAX_CONFIG_BYTES:
            raise GatewayConfigError('gateway configuration exceeds 256 KiB')
        raw = json.loads(path.read_text())
    except GatewayConfigError:
        raise
    except (OSError, ValueError, UnicodeDecodeError):
        raise GatewayConfigError('gateway configuration is not readable JSON') from None
    _keys(raw, {'schema_version', 'routes', 'providers', 'artifact_root'}, {'budgets', 'memory_config'}, 'configuration')
    if raw['schema_version'] != SCHEMA_VERSION:
        raise GatewayConfigError('schema_version must be ' + SCHEMA_VERSION)

    root = _absolute(raw['artifact_root'], 'artifact_root')
    if root.is_symlink() or not root.is_dir():
        raise GatewayConfigError('artifact_root must be an existing directory (not a symlink); no fallback is created')
    root = root.resolve()

    if not isinstance(raw['providers'], dict) or len(raw['providers']) > 64:
        raise GatewayConfigError('providers must be an object of at most 64 providers')
    providers = {}
    for name, entry in raw['providers'].items():
        if not PROVIDER_NAME.fullmatch(name):
            raise GatewayConfigError('provider names must be short identifiers')
        where = f'providers.{name}'
        _keys(entry, {'kind', 'base_url', 'api_key_file'},
              {'timeout_seconds', 'capabilities', 'allow_plain_http'}, where)
        if entry['kind'] not in PROVIDER_KINDS:
            raise GatewayConfigError(f'{where}.kind must be one of {list(PROVIDER_KINDS)}')
        allow_plain = entry.get('allow_plain_http', False)
        if type(allow_plain) is not bool:
            raise GatewayConfigError(where + '.allow_plain_http must be a boolean')
        try:
            endpoint = http.parse_base_url(entry['base_url'], allow_plain_http=allow_plain)
        except ValueError as error:
            raise GatewayConfigError(f'{where}.base_url: {error}') from None
        timeout = entry.get('timeout_seconds', 120)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) \
                or not 1 <= timeout <= 600:
            raise GatewayConfigError(where + '.timeout_seconds must be within 1..600')
        declared = entry.get('capabilities')
        if declared is not None:
            if not isinstance(declared, list) or len(declared) > 64 or \
                    any(not isinstance(c, str) or not CAPABILITY.fullmatch(c) for c in declared):
                raise GatewayConfigError(where + '.capabilities must list capability names')
            declared = frozenset(declared)
        providers[name] = Provider(name, entry['kind'], entry['base_url'], endpoint,
                                   _absolute(entry['api_key_file'], where + '.api_key_file'),
                                   float(timeout), declared)

    if not isinstance(raw['routes'], dict) or len(raw['routes']) > 64:
        raise GatewayConfigError('routes must be an object of at most 64 capabilities')
    routes = {}
    for capability, entry in raw['routes'].items():
        if not isinstance(capability, str) or not CAPABILITY.fullmatch(capability):
            raise GatewayConfigError('route capability names must look like family.verb')
        where = f'routes.{capability}'
        _keys(entry, {'provider', 'model'}, {'params', 'operation'}, where)
        if entry['provider'] not in providers:
            raise GatewayConfigError(where + '.provider is not a configured provider')
        params = entry.get('params', {})
        if not isinstance(params, dict) or any(not isinstance(k, str) or not 1 <= len(k) <= 64 for k in params):
            raise GatewayConfigError(where + '.params must be an object')
        if len(json.dumps(params)) > MAX_PARAMS_BYTES:
            raise GatewayConfigError(where + '.params exceeds 8 KiB')
        reserved = sorted(RESERVED_PARAMS & set(params))
        if reserved:
            raise GatewayConfigError(f'{where}.params may not set gateway-owned fields {reserved}')
        operation = entry.get('operation')
        if operation is not None and operation not in OPERATIONS:
            raise GatewayConfigError(f'{where}.operation must be one of {list(OPERATIONS)}')
        routes[capability] = Route(capability, entry['provider'], _label(entry['model'], where + '.model'),
                                   json.loads(json.dumps(params)), operation)

    budgets = {}
    raw_budgets = raw.get('budgets', {})
    if not isinstance(raw_budgets, dict) or len(raw_budgets) > 64:
        raise GatewayConfigError('budgets must be an object of at most 64 capabilities')
    for capability, entry in raw_budgets.items():
        if not isinstance(capability, str) or not CAPABILITY.fullmatch(capability):
            raise GatewayConfigError('budget capability names must look like family.verb')
        where = f'budgets.{capability}'
        _keys(entry, {'max_calls_per_hour', 'max_usd_per_day'}, {'confirm_over_usd', 'estimated_usd_per_call'}, where)
        calls = entry['max_calls_per_hour']
        if type(calls) is not int or not 0 <= calls <= 1_000_000:
            raise GatewayConfigError(where + '.max_calls_per_hour must be an integer 0..1000000')
        budgets[capability] = Budget(
            calls, _usd(entry['max_usd_per_day'], where + '.max_usd_per_day'),
            None if entry.get('confirm_over_usd') is None else _usd(entry['confirm_over_usd'], where + '.confirm_over_usd'),
            None if entry.get('estimated_usd_per_call') is None
            else _usd(entry['estimated_usd_per_call'], where + '.estimated_usd_per_call'))

    configured = raw.get('memory_config')
    configured = None if configured is None else _absolute(configured, 'memory_config')
    override = None if memory_config is None else Path(os.path.abspath(memory_config))
    if configured is not None and override is not None and configured != override:
        raise GatewayConfigError('memory_config in the configuration and on the command line differ')
    return GatewayConfig(path, root, providers, routes, budgets, override or configured)


def read_api_key(path):
    # Read a key from an owner-only regular file. Never echoes the contents.
    return leaf.read_secret_file(path, max_bytes=MAX_KEY_FILE_BYTES, refuse=SecretRefused)


def key_file_status(path):
    try:
        read_api_key(path)
        return 'ok'
    except SecretRefused as error:
        return error.reason


# ---------------------------------------------------------------- budget ledger

_LEDGER = """
CREATE TABLE IF NOT EXISTS calls (
  call_id TEXT PRIMARY KEY, capability TEXT NOT NULL, provider TEXT NOT NULL, model TEXT NOT NULL,
  started_at REAL NOT NULL, finished_at REAL, status TEXT NOT NULL,
  estimate_usd TEXT, cost_usd TEXT, request_digest TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS calls_by_capability ON calls(capability, started_at);
"""
# 'not_sent' calls never reached a provider (connection refused) and are not counted.
_COUNTED = "status IN ('attempting','succeeded','failed')"


class BudgetLedger:
    """Persistent per-capability counters under ``<artifact_root>/.model-gateway``.

    Windows are rolling: calls in the last hour, spend in the last 24 hours.
    Spend counts the provider-reported cost, else the reservation estimate.
    Reservation happens in one IMMEDIATE transaction, so concurrent calls
    cannot both pass a cap.
    """

    def __init__(self, root):
        self.dir = Path(root) / STATE_DIR
        self.path = leaf.store_path(self.dir, leaf.GATEWAY_LEDGER_DB)

    def exists(self):
        return self.path.is_file()

    def _connect(self, *, create=True):
        if self.dir.is_symlink() or self.path.is_symlink():
            raise RuntimeError('gateway ledger path is a symlink')
        if not create and not self.path.is_file():
            return None
        leaf.mkdir_private(self.dir, exist_ok=True)
        if not self.path.exists():
            # Created 0600 before SQLite opens it (T11b Q3); a concurrent creator wins the race.
            try:
                leaf.create_new_empty(self.path)
            except FileExistsError:
                pass
        db = leaf.sqlite_connect(self.path, mode='rwc', resolve=True, timeout=30, isolation_level=None)
        db.executescript(_LEDGER)
        return db

    @staticmethod
    def _state(db, capability, now):
        calls = db.execute(f'SELECT count(*) FROM calls WHERE capability=? AND started_at>? AND {_COUNTED}',
                           (capability, now - HOUR)).fetchone()[0]
        spend, unpriced = Decimal(0), 0
        for cost, estimate in db.execute(
                f'SELECT cost_usd, estimate_usd FROM calls WHERE capability=? AND started_at>? AND {_COUNTED}',
                (capability, now - DAY)):
            value = cost if cost is not None else estimate
            if value is None:
                unpriced += 1
            else:
                spend += Decimal(value)
        return calls, spend, unpriced

    @staticmethod
    def _estimate(db, capability, provider, model, budget, now):
        if budget.estimated_usd_per_call is not None:
            return budget.estimated_usd_per_call, 'configured'
        rows = db.execute("SELECT cost_usd FROM calls WHERE capability=? AND provider=? AND model=? "
                          "AND status='succeeded' AND cost_usd IS NOT NULL AND started_at>?",
                          (capability, provider, model, now - ESTIMATE_WINDOW)).fetchall()
        if rows:
            return max(Decimal(row[0]) for row in rows), 'observed_max'
        return None, 'unknown'

    def state(self, capability, provider, model, budget, now):
        with closing(self._connect(create=False) or leaf.sqlite_connect(':memory:')) as db:
            db.executescript(_LEDGER)
            calls, spend, unpriced = self._state(db, capability, now)
            estimate, source = self._estimate(db, capability, provider, model, budget, now)
        return _budget_view(budget, calls, spend, unpriced, estimate, source)

    def reserve(self, *, call_id, capability, provider, model, budget, confirm, digest, now):
        with closing(self._connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                calls, spend, unpriced = self._state(db, capability, now)
                estimate, source = self._estimate(db, capability, provider, model, budget, now)
                view = _budget_view(budget, calls, spend, unpriced, estimate, source)
                if calls + 1 > budget.max_calls_per_hour:
                    db.execute('ROLLBACK')
                    return 'refused', 'max_calls_per_hour', view
                if spend >= budget.max_usd_per_day or (estimate is not None and spend + estimate > budget.max_usd_per_day):
                    db.execute('ROLLBACK')
                    return 'refused', 'max_usd_per_day', view
                if budget.confirm_over_usd is not None and confirm is not True and \
                        (estimate is None or estimate > budget.confirm_over_usd):
                    db.execute('ROLLBACK')
                    return 'confirmation_required', ('estimate_unknown' if estimate is None else 'estimate_over_threshold'), view
                db.execute('INSERT INTO calls VALUES (?,?,?,?,?,?,?,?,?,?)',
                           (call_id, capability, provider, model, now, None, 'attempting',
                            None if estimate is None else str(estimate), None, digest))
                db.execute('COMMIT')
                return 'proceed', None, view
            except BaseException:
                if db.in_transaction:
                    db.execute('ROLLBACK')
                raise

    def finish(self, call_id, status, cost, now):
        with closing(self._connect()) as db:
            db.execute('UPDATE calls SET status=?, cost_usd=?, finished_at=? WHERE call_id=?',
                       (status, None if cost is None else str(cost), now, call_id))


def _num(value):
    return None if value is None else float(value)


def _budget_view(budget, calls, spend, unpriced, estimate, source):
    return {'max_calls_per_hour': budget.max_calls_per_hour, 'calls_last_hour': calls,
            'max_usd_per_day': float(budget.max_usd_per_day), 'usd_last_24h': float(spend),
            'unpriced_calls_last_24h': unpriced, 'confirm_over_usd': _num(budget.confirm_over_usd),
            'estimate_usd': _num(estimate), 'estimate_source': source,
            'windows': 'rolling: calls per 3600 s, USD per 86400 s'}


# ---------------------------------------------------------------- artifacts

def resolve_input(root, reference):
    """Read an input artifact that resolves inside artifact_root (never the ledger)."""
    if not isinstance(reference, str) or '\x00' in reference or '\\' in reference:
        raise InputUnavailable('invalid reference')
    candidate = Path(reference)
    if not candidate.is_absolute():
        if '..' in candidate.parts:
            raise InputUnavailable('reference escapes artifact_root')
        candidate = root / candidate
    try:
        real = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        raise InputUnavailable('reference not found') from None
    if not real.is_relative_to(root) or real == root:
        raise InputUnavailable('reference escapes artifact_root')
    relative = real.relative_to(root)
    if relative.parts[0] == STATE_DIR:
        raise InputUnavailable('reference names gateway state')
    try:
        fd = leaf.open_fd(real, access='r', nofollow=True, nonblock=True, optional_flags=True)
    except OSError:
        raise InputUnavailable('reference unreadable') from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_INPUT_ARTIFACT_BYTES:
            raise InputUnavailable('reference must be a non-empty regular file within the size bound')
        chunks, size = [], 0
        while size <= MAX_INPUT_ARTIFACT_BYTES:
            chunk = os.read(fd, 1 << 20)
            if not chunk:
                break
            size += len(chunk)
            chunks.append(chunk)
    finally:
        os.close(fd)
    data = b''.join(chunks)
    if not data or len(data) > MAX_INPUT_ARTIFACT_BYTES:
        raise InputUnavailable('reference must be a non-empty regular file within the size bound')
    return data, relative.as_posix()


def _write_private(path, data):
    return leaf.write_new_private(path, data, optional_flags=True, fsync=False)


def write_outputs(root, capability, call_id, day, outputs, provenance):
    """Write outputs then provenance into a staging directory, then publish it
    with one rename. A failure leaves nothing that looks like a success."""
    parent = root / OUTPUT_DIR / capability / day
    leaf.mkdir_private(parent, parents=True, exist_ok=True)
    if parent.resolve() != root / OUTPUT_DIR / capability / day:
        raise OSError('artifact directory escapes artifact_root')
    final = parent / call_id
    staging = parent / ('.partial-' + call_id)
    leaf.mkdir_private(staging)
    try:
        for index, (data, media) in enumerate(outputs):
            name = provenance['artifacts'][index]['ref'].rsplit('/', 1)[1]
            _write_private(staging / name, data)
        _write_private(staging / 'provenance.json',
                       json.dumps(provenance, indent=1, sort_keys=True, ensure_ascii=True).encode())
        os.rename(staging, final)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return final


# ---------------------------------------------------------------- desk scoping

def record_in_memory(memory_config, provenance):
    """Record a content-free provenance event against the bound session's desk.

    Uses the existing episode store through the operator's memory config. A
    desk whose registry entry disables capture is skipped. Never raises.
    ``indexed`` means this call's own drain ran to completion, so the outbox, this seal
    included, was applied (T12b): always False inside Compose, where only the indexer
    role drains, and False on a host install when the lease was held or the drain failed.
    """
    if memory_config is None:
        return {'status': 'not_configured'}
    try:
        from kp_agent_tooling._impl.service.desk_memory_runtime import components
        config, registry, ledger, store = components(memory_config)
        admitted = ledger.resolve(config['provider_session_id'], registry)
        allows = getattr(registry, 'allows_capture', None)
        if callable(allows) and not allows(admitted.binding_key):
            return {'status': 'skipped', 'reason': 'desk_capture_disabled', 'binding_key': admitted.binding_key}
        text = leaf.canonical_json(provenance, ascii=True, allow_nan=True)
        receipt = store.capture(config['provider_session_id'], source_ref='model-gateway:' + provenance['call_id'],
                                events=[{'event_id': 'model-gateway-provenance', 'role': 'tool', 'text': text}])
    except Exception:
        return {'status': 'unavailable',
                'guidance': 'The artifact and provenance are written; the desk record needs an admitted session.'}
    # T12b: the seal's outbox row carries the indexing. A host install drains once, never waiting
    # (`indexed`: that drain applied the outbox); inside Compose the indexer role drains.
    from kp_agent_tooling._impl.service.episodic_search import drain_after_seal
    drained = drain_after_seal(store)
    indexed = isinstance(drained, dict) and drained.get('status') in ('drained', 'idle')
    return {'status': 'recorded', 'episode_id': receipt['episode_id'], 'binding_key': receipt['binding_key'],
            'indexed': indexed}


# ---------------------------------------------------------------- gateway

GUIDANCE = {
    'configuration_invalid': 'The operator must repair the gateway configuration.',
    'unknown_capability': 'List tools; only configured capabilities are callable.',
    'unsupported_modality': 'This provider class does not implement the capability; the operator must route it elsewhere.',
    'invalid_arguments': 'Check the tool input schema and its bounds.',
    'budget_unconfigured': 'The operator must configure a budget for this capability before it can be called.',
    'budget_exceeded': 'A cap would be exceeded; no request was sent. Wait for the window or ask the operator.',
    'confirmation_required': 'No request was sent. Retry the same call with confirm: true to accept the estimate.',
    'secret_refused': 'The provider key file must be a regular file owned by this user with mode 0600 or 0400.',
    'input_unavailable': 'Pass an artifact reference that resolves inside the gateway artifact_root.',
    'provider_error': 'The provider call failed; nothing was written as success and no retry was made.',
    'artifact_write_failed': 'The provider answered but the artifact could not be written; the call may be billed.',
    'internal_error': 'The gateway failed unexpectedly; no retry was made.',
    'budget_unpriced': 'The content-free mode needs estimated_usd_per_call in the capability budget, so the '
                       'dollar cap holds when the provider reports no cost.',
    'route_params_refused': 'The route params set a field the content-free caller owns or that would weaken '
                            'its privacy routing; remove it from the route.',
    'privacy_routing_required': 'The request must carry provider routing with zdr true, data_collection deny '
                                'and require_parameters true.',
    'model_pin_mismatch': "The request names a model other than the route's; nothing is substituted.",
    'invalid_request': 'The content-free request has an unexpected shape.',
}

# ---------------------------------------------------------------- content-free mode (S1a P2)

# The request fields the content-free caller owns. Route params may add other provider
# options, never one of these: `provider` carries the privacy routing, `models` and `route`
# are OpenRouter's fallback list, and the rest are the caller's validated request.
CONTENT_FREE_FIELDS = frozenset({'model', 'stream', 'max_tokens', 'provider', 'messages',
                                 'response_format', 'reasoning'})
CONTENT_FREE_REFUSED_PARAMS = frozenset({'provider', 'models', 'route', 'max_tokens',
                                         'response_format', 'reasoning'})
# Privacy routing every content-free request carries, exactly (P2: "Privacy routing stays in code").
PRIVACY_ROUTING = {'zdr': True, 'data_collection': 'deny', 'require_parameters': True}
PROVIDER_ROUTING_FIELDS = frozenset({'allow_fallbacks', 'require_parameters', 'zdr', 'data_collection',
                                     'order'})


def content_free_route_refusal(route):
    """The route params a content-free caller refuses (sorted), or [] when the route is usable."""
    return sorted(CONTENT_FREE_REFUSED_PARAMS & set(route.params))


def _content_free_request(route, request):
    """The refusal category for a content-free request, or None when it may be built."""
    if not isinstance(request, dict) or not request or set(request) - CONTENT_FREE_FIELDS:
        return 'invalid_request'
    routing = request.get('provider')
    if (not isinstance(routing, dict) or set(routing) - PROVIDER_ROUTING_FIELDS
            or any(routing.get(key) != value or type(routing.get(key)) is not type(value)
                   for key, value in PRIVACY_ROUTING.items())):
        return 'privacy_routing_required'
    if request.get('model') != route.model:
        return 'model_pin_mismatch'
    if request.get('stream') is not False or not isinstance(request.get('messages'), list):
        return 'invalid_request'
    return None


def _iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def _failure(category, base=None, status='error', **fields):
    result = {'status': status, 'category': category, 'guidance': GUIDANCE[category]}
    result.update(base or {})
    result.update(fields)
    result.setdefault('requests_sent', 0)
    result.setdefault('retry', 'none')
    return result


class ModelGateway:
    def __init__(self, config_path, *, memory_config=None, transport=None, clock=time.time):
        self.config_path = config_path
        self.memory_override = memory_config
        self.transport = transport or http.request
        self.clock = clock

    def load(self):
        return load_config(self.config_path, memory_config=self.memory_override)

    @staticmethod
    def _tool(config, capability):
        route = config.routes[capability]
        provider = config.providers[route.provider]
        operation = config.operation(capability)
        declared = route.operation or DEFAULT_OPERATIONS.get(capability)
        if operation is None:
            note = (f'Configured, but provider class {provider.kind} does not implement this capability; '
                    'calls return unsupported_modality without any request.')
        else:
            note = (f'Routed by operator configuration to provider {route.provider} ({provider.kind}), '
                    f'model {route.model}. Output is written under the gateway artifact_root; the result '
                    'carries only artifact references and provenance. Budget-capped; a confirmation_required '
                    'result sends nothing, retry with confirm: true to accept.')
        return {'name': TOOL_PREFIX + capability,
                'description': f'Model capability {capability}. {note}',
                'inputSchema': input_schema(declared if operation is not None else None)}

    def tools(self):
        config = self.load()
        return [self._tool(config, capability) for capability in sorted(config.routes)]

    def call(self, name, arguments):
        progress = {'request_started': False}
        try:
            return self._call(name, arguments, progress)
        except Exception:
            # Never claim silence after a request may have reached the provider.
            sent = progress['request_started']
            return _failure('internal_error', {'tool': name if isinstance(name, str) else None},
                            requests_sent=1 if sent else 0, billing='unknown' if sent else 'none')

    def _call(self, name, arguments, progress):
        try:
            config = self.load()
        except GatewayConfigError:
            return _failure('configuration_invalid', {'tool': name})
        capability = name[len(TOOL_PREFIX):] if isinstance(name, str) and name.startswith(TOOL_PREFIX) else None
        if capability not in config.routes:
            return _failure('unknown_capability', {'tool': name})
        route = config.routes[capability]
        provider = config.providers[route.provider]
        base = {'capability': capability, 'tool': name, 'provider': route.provider,
                'provider_class': provider.kind, 'model': route.model}
        operation = config.operation(capability)
        if operation is None:
            return _failure('unsupported_modality', base,
                            operation=route.operation or DEFAULT_OPERATIONS.get(capability))
        arguments = {} if arguments is None else arguments
        try:
            if len(json.dumps(arguments)) > MAX_ARGUMENT_BYTES:
                raise ValueError('arguments exceed bound')
            jsonschema.validate(arguments, input_schema(operation))
        except (jsonschema.ValidationError, ValueError, TypeError):
            return _failure('invalid_arguments', base)
        budget = config.budgets.get(capability)
        if budget is None:
            return _failure('budget_unconfigured', base, status='refused')

        inputs, audio = [], None
        if operation == 'transcription':
            given = [key for key in ('audio_ref', 'audio_base64') if key in arguments]
            if len(given) != 1:
                return _failure('invalid_arguments', base, detail='pass exactly one of audio_ref or audio_base64')
            if given[0] == 'audio_ref':
                try:
                    audio, reference = resolve_input(config.artifact_root, arguments['audio_ref'])
                except InputUnavailable:
                    return _failure('input_unavailable', base)
                inputs.append({'ref': reference})
            else:
                try:
                    audio = base64.b64decode(arguments['audio_base64'], validate=True)
                except (binascii.Error, ValueError):
                    return _failure('invalid_arguments', base, detail='audio_base64 is not base64')
                if not audio or len(audio) > MAX_INLINE_AUDIO_BYTES:
                    return _failure('invalid_arguments', base, detail='inline audio exceeds 256 KiB; use audio_ref')
                inputs.append({'inline': True})
            inputs[0].update(sha256=hashlib.sha256(audio).hexdigest(), bytes=len(audio))

        path, body, content_type, accept = build_request(provider.kind, operation, route.model,
                                                         route.params, arguments, audio)
        digest = 'sha256:' + hashlib.sha256(body).hexdigest()
        base['request_digest'] = digest
        try:
            api_key = read_api_key(provider.api_key_file)
        except SecretRefused as error:
            return _failure('secret_refused', base, status='refused', key_file_status=error.reason)

        call_id = secrets.token_hex(16)
        started = self.clock()
        ledger = BudgetLedger(config.artifact_root)
        decision, reason, view = ledger.reserve(
            call_id=call_id, capability=capability, provider=route.provider, model=route.model,
            budget=budget, confirm=arguments.get('confirm'), digest=digest, now=started)
        if decision == 'refused':
            return _failure('budget_exceeded', base, status='refused', reason=reason, budget=view)
        if decision == 'confirmation_required':
            result = _failure('confirmation_required', base, status='confirmation_required',
                              reason=reason, budget=view)
            result.pop('category')
            return result

        started_mono = time.monotonic()
        progress['request_started'] = True
        try:
            response = self.transport(provider.endpoint, 'POST', path, api_key=api_key, timeout=provider.timeout,
                                      max_response_bytes=MAX_RESPONSE_BYTES[operation], body=body,
                                      content_type=content_type, accept=accept)
            outputs, usage, metadata = parse_response(operation, response, route.params)
        except http.ProviderCallFailed as error:
            ledger.finish(call_id, 'failed' if error.request_sent else 'not_sent', None, self.clock())
            fields = {'failure': error.failure, 'requests_sent': 1 if error.request_sent else 0,
                      'billing': 'unknown' if error.request_sent else 'none'}
            if error.http_status is not None:
                fields['http_status'] = error.http_status
            if error.provider_error:
                fields['provider_error'] = error.provider_error
            return _failure('provider_error', base, call_id=call_id, **fields)
        del api_key

        cost = usage.get('cost')
        cost = None if cost is None else Decimal(str(cost))
        finished = self.clock()
        day = datetime.fromtimestamp(finished, timezone.utc).strftime('%Y-%m-%d')
        folder = f'{OUTPUT_DIR}/{capability}/{day}/{call_id}'
        artifacts = [{'ref': f'{folder}/output-{index}.{MEDIA_EXTENSIONS.get(media, "bin")}',
                      'media_type': media, 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
                     for index, (data, media) in enumerate(outputs)]
        generation_id = metadata.get('generation_id') or response.response_ids.get('x-generation-id')
        provenance = {
            'schema_version': PROVENANCE_SCHEMA, 'call_id': call_id, 'capability': capability, 'tool': name,
            'operation': operation, 'provider': route.provider, 'provider_class': provider.kind,
            'model': route.model, 'request_target': provider.endpoint.target(path),
            'request_digest': digest, 'request_bytes': len(body),
            'started_at': _iso(started), 'timestamp': _iso(finished),
            'elapsed_ms': int((time.monotonic() - started_mono) * 1000),
            'cost_usd': _num(cost), 'cost_reported': cost is not None, 'usage': usage,
            'generation_id': generation_id, 'model_reported': metadata.get('model_reported'),
            'finish_reason': metadata.get('finish_reason'),
            'confirmed': arguments.get('confirm') is True,
            'estimate_usd': view['estimate_usd'], 'estimate_source': view['estimate_source'],
            'inputs': inputs, 'artifacts': artifacts,
            'evidence_boundary': 'provider output stored as received; content correctness not verified'}
        try:
            write_outputs(config.artifact_root, capability, call_id, day, outputs, provenance)
        except OSError:
            ledger.finish(call_id, 'failed', cost, finished)
            return _failure('artifact_write_failed', base, call_id=call_id, requests_sent=1, billing='likely')
        ledger.finish(call_id, 'succeeded', cost, finished)

        # The returned provenance must equal provenance.json; paths go only on copies.
        located = [dict(artifact, path=str(config.artifact_root / artifact['ref'])) for artifact in artifacts]
        memory = record_in_memory(config.memory_config, provenance)
        return {'status': 'ok', **base, 'call_id': call_id, 'artifacts': located,
                'provenance_ref': f'{folder}/provenance.json', 'provenance': provenance,
                'cost_usd': _num(cost), 'cost_reported': cost is not None, 'requests_sent': 1,
                'memory_record': memory}

    # ------------------------------------------------------------ content-free mode

    def complete_content_free(self, capability, request):
        """One chat completion for a content-free caller (the summarizer role, S1a P2).

        The pre-network order: the route (a chat route whose params the caller does not
        refuse), the budget configured and priced (``estimated_usd_per_call``), the request
        (only the caller's fields, the privacy routing present, the route's model), the key
        file, the reservation in one IMMEDIATE transaction, then exactly one request to the
        route's provider; ``finish`` records the cost. The decoded response is returned in
        memory only: nothing but the ledger row is written. Every refusal before the request
        reports ``requests_sent: 0``; a failure after it reports whether it was sent.
        """
        progress = {'request_started': False}
        try:
            return self._complete_content_free(capability, request, progress)
        except Exception:
            sent = progress['request_started']
            return _failure('internal_error', {'capability': capability if isinstance(capability, str) else None},
                            requests_sent=1 if sent else 0, billing='unknown' if sent else 'none')

    def _complete_content_free(self, capability, request, progress):
        try:
            config = self.load()
        except GatewayConfigError:
            return _failure('configuration_invalid', {'capability': capability}, status='refused')
        if capability not in config.routes:
            return _failure('unknown_capability', {'capability': capability}, status='refused')
        route = config.routes[capability]
        provider = config.providers[route.provider]
        base = {'capability': capability, 'provider': route.provider, 'provider_class': provider.kind,
                'model': route.model}
        operation = config.operation(capability)
        if operation != 'chat':
            return _failure('unsupported_modality', base, status='refused', operation=operation)
        refused = content_free_route_refusal(route)
        if refused:
            return _failure('route_params_refused', base, status='refused', params=refused)
        budget = config.budgets.get(capability)
        if budget is None:
            return _failure('budget_unconfigured', base, status='refused')
        if budget.estimated_usd_per_call is None:
            return _failure('budget_unpriced', base, status='refused')
        category = _content_free_request(route, request)
        if category is not None:
            return _failure(category, base, status='refused')

        path = PROVIDER_PATHS[provider.kind][operation]
        body = dict(route.params)
        body.update(request)
        # The wire bytes and their digest come from the leaf, the one home of both (T11a).
        raw, hexdigest = leaf.canonical_bytes_sha256(body, ascii=True, allow_nan=True)
        digest = 'sha256:' + hexdigest
        base['request_digest'] = digest
        try:
            api_key = read_api_key(provider.api_key_file)
        except SecretRefused as error:
            return _failure('secret_refused', base, status='refused', key_file_status=error.reason)

        call_id = secrets.token_hex(16)
        ledger = BudgetLedger(config.artifact_root)
        decision, reason, view = ledger.reserve(
            call_id=call_id, capability=capability, provider=route.provider, model=route.model,
            budget=budget, confirm=False, digest=digest, now=self.clock())
        if decision == 'refused':
            return _failure('budget_exceeded', base, status='refused', reason=reason, budget=view)
        if decision == 'confirmation_required':
            return _failure('confirmation_required', base, status='confirmation_required', reason=reason,
                            budget=view)

        target = provider.endpoint.target(path)
        progress['request_started'] = True
        try:
            response = self.transport(provider.endpoint, 'POST', path, api_key=api_key, timeout=provider.timeout,
                                      max_response_bytes=MAX_RESPONSE_BYTES[operation], body=raw,
                                      content_type='application/json', accept='application/json')
        except http.ProviderCallFailed as error:
            ledger.finish(call_id, 'failed' if error.request_sent else 'not_sent', None, self.clock())
            fields = {'failure': error.failure, 'requests_sent': 1 if error.request_sent else 0,
                      'billing': 'unknown' if error.request_sent else 'none', 'request_target': target}
            if error.http_status is not None:
                fields['http_status'] = error.http_status
            if error.provider_error:
                fields['provider_error'] = error.provider_error
            return _failure('provider_error', base, call_id=call_id, **fields)
        del api_key
        try:
            value = json.loads(response.body)
            if not isinstance(value, dict):
                raise ValueError('response is not an object')
        except (ValueError, UnicodeDecodeError):
            ledger.finish(call_id, 'failed', None, self.clock())
            return _failure('provider_error', base, call_id=call_id, failure='provider_response_invalid',
                            requests_sent=1, billing='unknown', request_target=target)
        cost = safe_usage(value.get('usage')).get('cost')
        cost = None if cost is None else Decimal(str(cost))
        ledger.finish(call_id, 'succeeded', cost, self.clock())
        return {'status': 'ok', **base, 'call_id': call_id, 'request_target': target, 'requests_sent': 1,
                'cost_usd': _num(cost), 'cost_reported': cost is not None, 'response': value}

    # ------------------------------------------------------------ doctor

    def doctor(self, *, live=False):
        try:
            config = self.load()
        except GatewayConfigError as error:
            return {'schema_version': DOCTOR_SCHEMA, 'status': 'error', 'category': 'configuration_invalid',
                    'detail': str(error), 'network_requests': 0}
        now = self.clock()
        ledger = BudgetLedger(config.artifact_root)
        requests = [0]
        listings = {}
        routes = []
        for capability in sorted(config.routes):
            route = config.routes[capability]
            provider = config.providers[route.provider]
            operation = config.operation(capability)
            key_status = key_file_status(provider.api_key_file)
            budget = config.budgets.get(capability)
            entry = {'capability': capability, 'tool': TOOL_PREFIX + capability, 'provider': route.provider,
                     'provider_class': provider.kind, 'model': route.model,
                     'operation': operation or route.operation or DEFAULT_OPERATIONS.get(capability),
                     'request_target': provider.endpoint.target(PROVIDER_PATHS[provider.kind][operation])
                     if operation else None,
                     'modality': 'supported' if operation else 'unsupported', 'key_file': key_status,
                     'budget': None if budget is None else
                     ledger.state(capability, route.provider, route.model, budget, now)}
            if operation is None:
                entry['status'] = 'unsupported_modality'
            elif budget is None:
                entry['status'] = 'budget_unconfigured'
            elif key_status != 'ok':
                entry['status'] = 'secret_refused'
            else:
                entry['status'] = 'ready'
            if live and entry['status'] == 'ready':
                entry['live'] = self._live_listing(provider, operation, route.model, listings, requests)
            routes.append(entry)
        return {'schema_version': DOCTOR_SCHEMA,
                'status': 'ready' if all(r['status'] == 'ready' for r in routes) else 'not_ready',
                'config': str(config.path), 'artifact_root': str(config.artifact_root),
                'ledger': 'present' if ledger.exists() else 'absent (no calls yet)',
                'memory': {'configured': config.memory_config is not None},
                'unused_budgets': sorted(set(config.budgets) - set(config.routes)),
                'routes': routes, 'live': live,
                'paid_calls': 0, 'network_requests': requests[0],
                'network': 'model metadata GET only' if live else 'none'}

    def _live_listing(self, provider, operation, model, cache, requests):
        """Unpaid metadata check: is the routed model listed by the provider?"""
        path = '/models'
        if provider.kind == 'openrouter':
            path += '?output_modalities=' + LIVE_MODALITIES[operation]
        cache_key = (provider.name, path)
        if cache_key not in cache:
            try:
                api_key = read_api_key(provider.api_key_file)
                requests[0] += 1
                response = self.transport(provider.endpoint, 'GET', path, api_key=api_key,
                                          timeout=min(provider.timeout, 30), max_response_bytes=16 * 1024 * 1024)
                payload = json.loads(response.body)
                cache[cache_key] = ('ok', payload)
            except http.ProviderCallFailed as error:
                cache[cache_key] = (error.failure, None)
            except (SecretRefused, ValueError, UnicodeDecodeError):
                cache[cache_key] = ('provider_response_invalid', None)
        status, payload = cache[cache_key]
        if status != 'ok':
            return {'status': status}
        data = payload.get('data') if isinstance(payload, dict) else None
        listed = isinstance(data, list) and any(isinstance(row, dict) and row.get('id') == model for row in data)
        result = {'status': 'ok', 'model_listed': listed}
        if provider.kind == 'openrouter' and listed:
            # Reuse the bounded, credential-safe registry parser for pricing.
            from kp_agent_tooling._impl.service.openrouter_models import OpenRouterModelRegistryClient
            try:
                registry = OpenRouterModelRegistryClient(
                    'unused-placeholder', fetch_json=lambda _key, _timeout: payload, cache_seconds=0).registry()
                info = registry.get(model)
                if info is not None:
                    result['pricing_per_token'] = {'prompt': str(info.prompt_price),
                                                   'completion': str(info.completion_price)}
            except Exception:
                pass
        return result
