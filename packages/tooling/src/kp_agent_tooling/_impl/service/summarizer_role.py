"""The summarizer role (S1a): scheduled, opt-in consolidation of a desk's queued episodes.

The operator's acts, never the role's:
- one approval file (``agent-tooling.summarizer-approval.v1``, APPROVAL_SCHEMA) naming the
  approved desks, the approved model and its five profile reserves, and the gateway
  capability the role calls;
- for each approved desk, an admission row under the role's own provider instance
  (PROVIDER_INSTANCE, ``summarizer``) and a per-desk session id, made with the existing
  ``kp-agent-desk admit`` (provider = the route's provider, model = the approved model).
  Admission rows are immutable: a new approved model needs a new admission per desk.

Every tick (DEFAULT_INTERVAL_SECONDS, or INTERVAL_VARIABLE) the role checks, before any
connection: the approval, the gateway route (a chat route at the approved model whose params
weaken nothing), the budget and its estimate, the key file, and each desk's admission. Any
missing piece reads ``not_configured``, named in its status file and log line, and the tick
opens NO connection. Only then, at most once per receipt lifetime (24 h), it refreshes the
model metadata receipt from the route provider's model listing (a GET to the route's base
URL with that provider's key file: a metadata read, never a completion). Then, for each
approved desk, it works queued jobs one at a time, through the gateway's content-free mode,
until the queue is idle, the gateway refuses (which ends the tick) or the per-tick cap.

A desk not in the approval is never claimed: none of its episodes are read and nothing is
sent for it. Revocation is the approval file's: a desk removed from it stops on the next tick.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import threading
import time
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from kp_agent_tooling._impl import leaf

APPROVAL_SCHEMA = 'agent-tooling.summarizer-approval.v1'
STATUS_SCHEMA = 'agent-tooling.summarizer-status.v1'
PROFILE_SCHEMA = 'ops.memory-model-profile.v1'
# The role's own provider instance: its admission rows are keyed by it, never another harness's.
PROVIDER_INSTANCE = 'summarizer'
# The documented capability name; the approval names the one the role calls.
DEFAULT_CAPABILITY = 'memory.summarize'
INTERVAL_VARIABLE = 'AGENT_SUMMARIZER_INTERVAL_SECONDS'
DEFAULT_INTERVAL_SECONDS = 900
MAX_INTERVAL_SECONDS = 86400
# Per desk per tick: at most this many jobs (each at most MAX_PROPOSAL_CALLS requests).
DEFAULT_MAX_JOBS_PER_TICK = 8
MAX_PROPOSAL_CALLS = 4
LEASE_SECONDS = 300
WORKER = 'summarizer-role'
# Container paths (deploy/compose.yaml); the role's state is in the store volume.
DEFAULT_APPROVAL = '/config/summarizer/approval.json'
DEFAULT_GATEWAY_CONFIG = '/config/summarizer/gateway.json'
DEFAULT_STATE = '/state/memory/summarizer'
PROFILE_FILE = 'model-profile.json'
STATUS_FILE = 'status.json'
PROFILE_LIFETIME = timedelta(hours=24)
# A receipt is refreshed once it would expire within one job's lease: never mid-job.
REFRESH_MARGIN = timedelta(seconds=LEASE_SECONDS)
LISTING_PATH = '/models'
MAX_LISTING_BYTES = 16 * 1024 * 1024
RESERVES = ('system_tokens', 'tool_tokens', 'reasoning_tokens', 'output_tokens', 'safety_tokens')
MAX_DESKS = 64


class NotConfigured(ValueError):
    def __init__(self, piece, reason):
        super().__init__(f'{piece}: {reason}')
        self.piece, self.reason = piece, reason


# ---------------------------------------------------------------- the interval

def interval_seconds(value):
    """The tick interval: ``value`` (``--interval``), else INTERVAL_VARIABLE, else the default."""
    if value is None:
        raw = os.environ.get(INTERVAL_VARIABLE)
        if raw is None or raw == '':
            return DEFAULT_INTERVAL_SECONDS
        try:
            value = int(raw, 10)
        except ValueError:
            raise ValueError(f'{INTERVAL_VARIABLE} must be an integer number of seconds') from None
    if type(value) is not int or not 1 <= value <= MAX_INTERVAL_SECONDS:
        raise ValueError(f'the interval must be 1..{MAX_INTERVAL_SECONDS} seconds')
    return value


# ---------------------------------------------------------------- the approval

def load_approval(path):
    """The operator's standing approval, validated; NotConfigured('approval', reason) otherwise."""
    try:
        raw = leaf.read_private_json(path)
    except FileNotFoundError:
        raise NotConfigured('approval', 'missing') from None
    except (OSError, ValueError):
        raise NotConfigured('approval', 'unreadable_or_not_private') from None
    from kp_agent_tooling._impl.service.model_gateway import CAPABILITY
    if (not isinstance(raw, dict) or set(raw) != {'schema_version', 'capability', 'model_id', 'reserves', 'desks'}
            or raw['schema_version'] != APPROVAL_SCHEMA):
        raise NotConfigured('approval', 'invalid_schema')
    if not isinstance(raw['capability'], str) or not CAPABILITY.fullmatch(raw['capability']):
        raise NotConfigured('approval', 'invalid_capability')
    model = raw['model_id']
    if not isinstance(model, str) or not 1 <= len(model) <= 256 or model != model.strip():
        raise NotConfigured('approval', 'invalid_model_id')
    reserves = raw['reserves']
    if (not isinstance(reserves, dict) or set(reserves) != set(RESERVES)
            or any(type(v) is not int or not 0 <= v <= 100_000_000 for v in reserves.values())
            or reserves['output_tokens'] < 1):
        raise NotConfigured('approval', 'invalid_reserves')
    desks = raw['desks']
    if not isinstance(desks, list) or not 1 <= len(desks) <= MAX_DESKS:
        raise NotConfigured('approval', 'invalid_desks')
    seen_keys, seen_paths = set(), set()
    for desk in desks:
        if (not isinstance(desk, dict) or set(desk) != {'binding_key', 'memory_config'}
                or not isinstance(desk['binding_key'], str) or not 1 <= len(desk['binding_key']) <= 512
                or not isinstance(desk['memory_config'], str) or not os.path.isabs(desk['memory_config'])
                or desk['binding_key'] in seen_keys or desk['memory_config'] in seen_paths):
            raise NotConfigured('approval', 'invalid_desks')
        seen_keys.add(desk['binding_key'])
        seen_paths.add(desk['memory_config'])
    return raw


# ---------------------------------------------------------------- the profile receipt

def build_profile_receipt(entry, *, provider_id, observed_at, reserves, now=None):
    """The ``ops.memory-model-profile.v1`` receipt of one listing entry, as the repository's
    profile script builds it; ValueError when the entry or the reserves do not validate."""
    from kp_agent_tooling._impl.service.memory_budget import memory_budget, openrouter_profile
    profile = openrouter_profile(entry, provider_id=provider_id, observed_at=observed_at, now=now)
    budget = memory_budget(profile, **{name: reserves[name] for name in RESERVES}, now=now)
    return {'schema_version': PROFILE_SCHEMA, 'raw_model_entry': entry, 'selected_route': None,
            'profile': {**asdict(profile), 'observed_at': profile.observed_at.isoformat()},
            'budget': {name: getattr(budget, name) for name in (*RESERVES, 'input_tokens')},
            'chunk_counter': 'utf8-byte-estimate',
            'generated_at': (now or datetime.now(timezone.utc)).isoformat()}


def selected_options(model_id, entry):
    """The summarizer options for ``model_id`` (the queue's defaults), once the listing entry
    establishes them (``queue_cli.py``'s metadata checks); None when it does not."""
    from kp_agent_tooling.queue_cli import summary_options
    options = summary_options(model_id)
    parameters = entry.get('supported_parameters') if isinstance(entry, dict) else None
    parameters = parameters if isinstance(parameters, list) else []
    reasoning = entry.get('reasoning') if isinstance(entry.get('reasoning'), dict) else {}
    if options['json_output'] and 'response_format' not in parameters:
        return None
    if options['reasoning_effort'] and ('reasoning' not in parameters or
                                        options['reasoning_effort'] not in (reasoning.get('supported_efforts') or [])):
        return None
    return options


# ---------------------------------------------------------------- the role

class SummarizerRole:
    """One tick at a time; ``transport`` (tests) replaces the gateway's HTTP transport."""

    def __init__(self, *, approval_path, gateway_config, state_dir, transport=None,
                 max_jobs_per_tick=DEFAULT_MAX_JOBS_PER_TICK, clock=time.time):
        if type(max_jobs_per_tick) is not int or not 1 <= max_jobs_per_tick <= 256:
            raise ValueError('the per-tick cap must be 1..256 jobs')
        self.approval_path = Path(approval_path)
        self.gateway_config = Path(gateway_config)
        self.state = leaf.mark_store(str(state_dir))
        self.transport = transport
        self.max_jobs = max_jobs_per_tick
        self.clock = clock

    # -- the tick ---------------------------------------------------------------------
    def tick(self):
        status = {'schema_version': STATUS_SCHEMA, 'tick_at': self._iso(), 'status': 'ok', 'missing': [],
                  'requests_sent': 0, 'listing_reads': 0, 'desks': []}
        try:
            self._tick(status)
        except Exception as error:  # an unexpected fault is reported, never a crash loop
            status.update(status='error', category=leaf.error_category(error))
        if status['missing']:
            status['status'] = 'not_configured'
        self._write_status(status)
        return status

    def _tick(self, status):
        checked = self._checks(status)
        if checked is None:
            return
        approval, gateway, config, route, provider = checked
        desks = [self._desk(desk, approval, route) for desk in approval['desks']]
        status['desks'] = [desk['status'] for desk in desks]
        working = [desk for desk in desks if desk['status']['status'] != 'not_configured']
        if not working:
            status['missing'].append('admission')
            return
        budget, options = self._profile(status, approval, route, provider, gateway)
        if budget is None:
            return
        from kp_agent_tooling._impl.service.episodic_summarizer import GatewayCompletion, OpenRouterEpisodeSummarizer
        completion = GatewayCompletion(gateway, approval['capability'])
        try:
            for desk in working:
                proposer = OpenRouterEpisodeSummarizer(
                    gateway=completion, budget=budget, json_output=options['json_output'],
                    reasoning_effort=options['reasoning_effort'], allow_fallbacks=True, require_zdr=True,
                    trusted_policy=desk['policy'])
                refused = self._work(desk, proposer, budget)
                if refused:
                    # The budget is the capability's, shared by every desk: the tick ends here.
                    status['status'] = 'refused'
                    status['refusal'] = refused
                    break
        finally:
            status['requests_sent'] = completion.requests_sent

    # -- not_configured checks (no connection) ----------------------------------------
    def _checks(self, status):
        missing = status['missing']
        detail = status.setdefault('detail', {})
        try:
            approval = load_approval(self.approval_path)
        except NotConfigured as error:
            missing.append(error.piece)
            detail[error.piece] = error.reason
            approval = None
        from kp_agent_tooling._impl.service import model_gateway as gw
        gateway = gw.ModelGateway(self.gateway_config, transport=self.transport, clock=self.clock)
        try:
            config = gateway.load()
        except gw.GatewayConfigError:
            missing.append('route')
            detail['route'] = 'gateway_configuration_missing_or_invalid'
            return None
        if approval is None:
            return None
        capability = approval['capability']
        route = config.routes.get(capability)
        if route is None or config.operation(capability) != 'chat':
            missing.append('route')
            detail['route'] = 'no_chat_route_for_capability' if route is None else 'route_not_chat'
            return None
        provider = config.providers[route.provider]
        if gw.content_free_route_refusal(route):
            missing.append('route_params')
            detail['route_params'] = gw.content_free_route_refusal(route)
        if route.model != approval['model_id']:
            missing.append('model_pin')
            detail['model_pin'] = 'route_model_differs_from_approved_model'
        budget = config.budgets.get(capability)
        if budget is None:
            missing.append('budget')
        elif budget.estimated_usd_per_call is None:
            missing.append('budget_estimate')
        key = gw.key_file_status(provider.api_key_file)
        if key != 'ok':
            missing.append('key_file')
            detail['key_file'] = key
        if not self._state_ready():
            missing.append('state')
        if missing:
            return None
        return approval, gateway, config, route, provider

    def _state_ready(self):
        try:
            info = os.lstat(self.state)
        except OSError:
            return False
        import stat
        return stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid() and not leaf.shared_bits(info.st_mode)

    def _desk(self, desk, approval, route):
        """Admission check for one approved desk: its own row under this role's instance."""
        from kp_agent_tooling._impl.service.desk_binding import call_scope
        from kp_agent_tooling._impl.service.desk_memory_runtime import components
        report = {'binding_key': desk['binding_key'], 'status': 'configured'}
        entry = {'status': report, 'policy': None}
        try:
            with call_scope():
                config, registry, ledger, store = components(desk['memory_config'])
                if config['provider_instance'] != PROVIDER_INSTANCE:
                    raise NotConfigured('admission', 'not_the_role_instance')
                admitted = ledger.resolve(config['provider_session_id'], registry)
        except NotConfigured as error:
            report.update(status='not_configured', missing=[error.piece], reason=error.reason)
            return entry
        except Exception as error:
            report.update(status='not_configured', missing=['admission'],
                          reason='admission_unavailable', category=leaf.error_category(error))
            return entry
        reason = None
        if admitted.binding_key != desk['binding_key']:
            reason = 'admission_desk_differs'
        elif admitted.model_id != approval['model_id']:
            reason = 'admission_model_differs'
        elif admitted.provider_id != route.provider:
            reason = 'admission_provider_differs'
        if reason:
            report.update(status='not_configured', missing=['admission'], reason=reason)
            return entry
        if config['schema_version'] == 'ops.assistant-memory.local.v1':
            from kp_agent_tooling._impl.service.assistant_memory_policy import load_policy
            entry['policy'] = load_policy(config['assistant_policy_path'])
        entry.update(config=config, store=store, session=config['provider_session_id'])
        return entry

    # -- the model metadata receipt -----------------------------------------------------
    def _profile(self, status, approval, route, provider, gateway):
        """(budget, options) from a current receipt, refreshing it at most once per lifetime."""
        from kp_agent_tooling._impl.service.memory_budget import load_memory_budget_receipt
        path = self.state / PROFILE_FILE
        now = datetime.now(timezone.utc)
        receipt = self._read_json(path)
        if receipt is not None:
            try:
                budget = load_memory_budget_receipt(receipt, now=now + REFRESH_MARGIN)
            except (ValueError, KeyError, TypeError):
                budget = None
            if budget is not None and self._receipt_matches(receipt, budget, approval, route):
                options = selected_options(approval['model_id'], receipt['raw_model_entry'])
                if options is not None:
                    return budget, options
        last = self._last_listing(approval, route)
        if last is not None:
            status['missing'].append('profile_changed')
            status['detail']['profile_changed'] = last['outcome']
            status['profile'] = last
            return None, None
        outcome, entry = self._read_listing(status, provider, approval['model_id'], gateway)
        record = {'model_id': approval['model_id'], 'provider': route.provider, 'observed_at': now.isoformat(),
                  'outcome': outcome}
        if entry is None:
            if outcome == 'listing_unavailable':
                status['status'] = 'error'
                status['category'] = outcome
                status['profile'] = record
                return None, None
            status['missing'].append('profile_changed')
            status['detail']['profile_changed'] = outcome
            status['profile'] = record
            return None, None
        try:
            fresh = build_profile_receipt(entry, provider_id=route.provider, observed_at=now,
                                          reserves=approval['reserves'], now=now)
            budget = load_memory_budget_receipt(fresh, now=now)
        except (ValueError, KeyError, TypeError):
            fresh = budget = None
        options = None if budget is None else selected_options(approval['model_id'], entry)
        if options is None:
            record['outcome'] = 'entry_does_not_support_the_approved_profile'
            status['missing'].append('profile_changed')
            status['detail']['profile_changed'] = record['outcome']
            status['profile'] = record
            return None, None
        leaf.replace_file(path, (json.dumps(fresh, sort_keys=True, indent=2) + '\n').encode(),
                          cleanup='on-error', temp_prefix='.model-profile-', fchmod=True)
        record['outcome'] = 'refreshed'
        status['profile'] = record
        return budget, options

    @staticmethod
    def _receipt_matches(receipt, budget, approval, route):
        return (budget.profile.model_id == approval['model_id'] and budget.profile.provider_id == route.provider
                and all(receipt['budget'].get(name) == approval['reserves'][name] for name in RESERVES))

    def _last_listing(self, approval, route):
        """A listing read in the last 24 h for this model that did not yield a receipt (from the
        previous status file): the role waits for the operator rather than read it again."""
        previous = self._read_json(self.state / STATUS_FILE)
        record = previous.get('profile') if isinstance(previous, dict) else None
        if (not isinstance(record, dict) or record.get('model_id') != approval['model_id']
                or record.get('provider') != route.provider or record.get('outcome') in (None, 'refreshed', 'listing_unavailable')):
            return None
        try:
            observed = datetime.fromisoformat(record['observed_at'])
        except (KeyError, TypeError, ValueError):
            return None
        if not timedelta(0) <= datetime.now(timezone.utc) - observed < PROFILE_LIFETIME:
            return None
        return record

    def _read_listing(self, status, provider, model_id, gateway):
        """The one metadata GET: the route provider's base URL, its own key file, nothing else."""
        from kp_agent_tooling._impl.service import model_gateway as gw
        from kp_agent_tooling._impl.service.model_gateway_http import ProviderCallFailed
        try:
            api_key = gw.read_api_key(provider.api_key_file)
        except gw.SecretRefused:
            return 'listing_unavailable', None
        status['listing_reads'] += 1
        try:
            response = gateway.transport(provider.endpoint, 'GET', LISTING_PATH, api_key=api_key,
                                         timeout=min(provider.timeout, 30), max_response_bytes=MAX_LISTING_BYTES)
            payload = json.loads(response.body)
        except (ProviderCallFailed, ValueError, UnicodeDecodeError):
            return 'listing_unavailable', None
        finally:
            del api_key
        data = payload.get('data') if isinstance(payload, dict) else None
        if not isinstance(data, list):
            return 'listing_invalid', None
        entries = [row for row in data if isinstance(row, dict) and row.get('id') == model_id]
        if len(entries) != 1:
            return 'approved_model_not_listed_once', None
        return 'listed', entries[0]

    # -- the work ---------------------------------------------------------------------
    def _work(self, desk, proposer, budget):
        """Jobs of one desk, one at a time; the refusal category when the gateway refused."""
        from kp_agent_tooling._impl.service.episodic_queue import ConsolidationQueue, RequestNotSent
        report = desk['status']
        report['jobs'] = {}
        queue_path = leaf.store_file(desk['config']['state_root'], leaf.QUEUE_DB)
        if not queue_path.is_file():
            report.update(status='idle', stopped='idle', queue='absent')
            return None
        queue = ConsolidationQueue(queue_path, store=desk['store'])
        report['status'] = 'idle'
        # The desk is approved, admitted under this role's instance at the approved model
        # (checked in _desk before any claim): its sources are approved without a digest list.
        for _ in range(self.max_jobs):
            try:
                result = queue.run_once(desk['session'], WORKER, propose=proposer, budget=budget,
                                        approve_sources=lambda digests: True,
                                        max_proposal_calls=MAX_PROPOSAL_CALLS, lease_seconds=LEASE_SECONDS)
            except RequestNotSent as refusal:
                report.update(status='refused', stopped='gateway_refused', refusal=refusal.category)
                _count(report, 'released')
                return refusal.category
            except Exception as error:
                # The job (if one was claimed) is in review; one failure ends this desk's tick.
                report.update(status='worked', stopped='job_failed', category=leaf.error_category(error))
                _count(report, 'failed')
                return None
            if result is None:
                report['stopped'] = 'idle'
                return None
            report['status'] = 'worked'
            _count(report, result['state'])
        report['stopped'] = 'per_tick_cap'
        return None

    # -- state files --------------------------------------------------------------------
    def _read_json(self, path):
        try:
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 262144:
                return None
            return json.loads(path.read_bytes())
        except (OSError, ValueError):
            return None

    def _write_status(self, status):
        if not self._state_ready():
            return
        try:
            leaf.replace_file(self.state / STATUS_FILE, (json.dumps(status, sort_keys=True) + '\n').encode(),
                              cleanup='on-error', temp_prefix='.status-', fchmod=True)
        except OSError:
            status['status_file'] = 'unwritten'

    def _iso(self):
        return datetime.fromtimestamp(self.clock(), timezone.utc).isoformat()


def _count(report, state):
    report['jobs'][state] = report['jobs'].get(state, 0) + 1


# ---------------------------------------------------------------- the console entry

def main(argv=None):
    parser = argparse.ArgumentParser(prog='kp-agent-summarizer', description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--approval', default=DEFAULT_APPROVAL, help='the operator approval file')
    parser.add_argument('--gateway-config', default=DEFAULT_GATEWAY_CONFIG, help='the model gateway configuration')
    parser.add_argument('--state', default=DEFAULT_STATE, help="the role's private state directory")
    parser.add_argument('--watch', action='store_true', help='tick every interval until a signal')
    parser.add_argument('--interval', type=int, help=f'seconds between ticks (default: {INTERVAL_VARIABLE}, '
                                                     f'else {DEFAULT_INTERVAL_SECONDS})')
    parser.add_argument('--max-ticks', type=int, help='--watch: stop after this many ticks')
    parser.add_argument('--max-jobs-per-tick', type=int, default=DEFAULT_MAX_JOBS_PER_TICK,
                        help='jobs per desk per tick')
    args = parser.parse_args(argv)
    try:
        interval = interval_seconds(args.interval)
    except ValueError as error:
        print(json.dumps({'status': 'error', 'category': 'interval_invalid', 'detail': str(error)}), flush=True)
        return 2
    if args.max_ticks is not None and args.max_ticks < 1:
        parser.error('--max-ticks must be at least 1')
    role = SummarizerRole(approval_path=args.approval, gateway_config=args.gateway_config, state_dir=args.state,
                          max_jobs_per_tick=args.max_jobs_per_tick)
    stop = threading.Event()
    previous = {}
    if args.watch:
        # A stop ends the wait between ticks; a tick in progress finishes its current job first.
        for name in ('SIGTERM', 'SIGINT'):
            number = getattr(signal, name)
            previous[number] = signal.signal(number, lambda *_: stop.set())
    try:
        ticks = 0
        while True:
            status = role.tick()
            print(json.dumps(status, sort_keys=True), flush=True)
            ticks += 1
            if not args.watch or (args.max_ticks is not None and ticks >= args.max_ticks) or stop.wait(interval):
                return 0
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


if __name__ == '__main__':
    raise SystemExit(main())
