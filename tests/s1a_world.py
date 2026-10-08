"""A synthetic runtime for the S1a summarizer role (docs/work/orders/S1a-scheduled-summarizer.md).

One private state root holds the stores of two desks of the example workspace catalog
(`config/desk-context/catalog.example.json`): `implementation-desk` and `verification-desk`.
Each desk has a capture session (provider instance `fixture`) that seals episodes and
enqueues them, as a bound launch does, and a summarizer session (provider instance
`summarizer`) that the operator admits with the approved model. The model gateway's route
`memory.summarize` names a provider whose host is reserved (`*.test`): every request goes
to an injected transport, `StubProvider`, which records it. No test makes a real or paid
request, and the key file holds a synthetic test value.
"""
from __future__ import annotations

import json
import shutil
import socket
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from kp_agent_tooling._impl.service.desk_memory_runtime import admit, components, initialize
from kp_agent_tooling._impl.service.episodic_queue import ConsolidationQueue
from kp_agent_tooling._impl.service.model_gateway_http import HttpResponse

MODEL = 'z-ai/glm-5.3-flash'
OTHER_MODEL = 'z-ai/glm-5.3-pro'
CAPABILITY = 'memory.summarize'
PROVIDER = 'openrouter'
BASE_URL = 'https://openrouter.s1a.test/api/v1'
COMPLETIONS = BASE_URL + '/chat/completions'
LISTING = BASE_URL + '/models'
KEY_VALUE = 's1a-synthetic-test-key-0000'
RESERVES = {'system_tokens': 4096, 'tool_tokens': 1024, 'reasoning_tokens': 4096,
            'output_tokens': 8192, 'safety_tokens': 2048}
IMPL, VERIF = 'implementation-desk', 'verification-desk'
DESKS = (IMPL, VERIF)
EVENT_TEXT = 'Choose pool B; retain pool A for recovery.'
QUOTE = 'Choose pool B'
REPO = Path(__file__).resolve().parents[1]


def model_entry(model=MODEL, *, parameters=('response_format', 'reasoning', 'tools', 'max_tokens'),
                efforts=('low', 'medium', 'high')):
    return {'id': model, 'name': model, 'context_length': 200000,
            'top_provider': {'context_length': 200000, 'max_completion_tokens': 16384},
            'pricing': {'prompt': '0.0000001', 'completion': '0.0000004'},
            'supported_parameters': list(parameters),
            'reasoning': {'mandatory': False, 'supported_efforts': list(efforts)}}


@dataclass
class Call:
    method: str
    target: str
    body: dict | None
    key_matches: bool


class StubProvider:
    """The gateway's injected transport: records every request; never opens a socket."""

    def __init__(self):
        self.calls: list[Call] = []
        self.reported_model = MODEL
        self.listing = {'data': [model_entry(), model_entry(OTHER_MODEL)]}
        self.cost = 0.0004

    def __call__(self, endpoint, method, path, *, api_key, timeout, max_response_bytes, body=None,
                 content_type=None, accept='application/json'):
        target = endpoint.target(path)
        self.calls.append(Call(method, target, json.loads(body) if body else None, api_key == KEY_VALUE))
        if method == 'GET':
            return HttpResponse(200, 'application/json', json.dumps(self.listing).encode(), {})
        request = json.loads(body)
        wire = json.loads(request['messages'][1]['content'])
        proposal = {'items': [{'kind': 'decision', 'text': QUOTE,
                               'source_ids': [wire['sources'][0]['source_id']]}],
                    'unresolved_questions': []}
        response = {'id': 'gen-s1a', 'choices': [{'finish_reason': 'stop', 'message': {
                        'role': 'assistant', 'content': json.dumps(proposal)}}],
                    'usage': {'prompt_tokens': 120, 'completion_tokens': 30, 'cost': self.cost}}
        if self.reported_model is not None:
            response['model'] = self.reported_model
        return HttpResponse(200, 'application/json', json.dumps(response).encode(), {})

    def posts(self):
        return [call for call in self.calls if call.method == 'POST']

    def gets(self):
        return [call for call in self.calls if call.method == 'GET']


def write_private(path: Path, value) -> Path:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    data = value if isinstance(value, (bytes, str)) else json.dumps(value)
    path.write_bytes(data.encode() if isinstance(data, str) else data)
    path.chmod(0o600)
    return path


class World:
    def __init__(self, root: Path):
        self.root = root
        self.state = root / 'state'
        self.state.mkdir(mode=0o700)
        self.config = root / 'config'
        self.config.mkdir(mode=0o700)
        source = REPO / 'config' / 'desk-context'
        shutil.copyfile(source / 'doctrine.md', root / 'doctrine.md')
        self.catalog = write_private(self.config / 'catalog.json', (source / 'catalog.example.json').read_text())
        self.role_state = self.state / 'summarizer'
        self.role_state.mkdir(mode=0o700)
        self.key_file = write_private(self.config / 'summarizer.key', KEY_VALUE + '\n')
        self.capture = {desk: self.memory_config(self.capture_session(desk), 'fixture') for desk in DESKS}
        initialize(self.capture[IMPL])
        self.binding = {}
        for desk in DESKS:
            self.binding[desk] = admit(self.capture[desk], desk_id=desk, provider_id='fixture',
                                       model_id='fixture')['binding_key']
        self.summarizer = {}
        for desk in DESKS:
            self.summarizer[desk] = self.admit_summarizer(desk, 'summarizer-' + desk.split('-')[0])
        self.queue_path = self.state / 'queue.sqlite3'
        ConsolidationQueue(self.queue_path, store=self.store()).initialize()
        self.provider = StubProvider()
        self.approval_path = self.config / 'summarizer' / 'approval.json'
        self.gateway_path = self.config / 'summarizer' / 'gateway.json'
        self.write_gateway()
        self.write_approval()

    # ------------------------------------------------------------------ configuration
    def memory_config(self, session, instance):
        return write_private(self.config / 'sessions' / (session + '.json'), {
            'schema_version': 'ops.desk-memory.local.v1', 'state_root': str(self.state),
            'catalog_path': str(self.catalog), 'workspace_root': str(self.root),
            'provider_instance': instance, 'provider_session_id': session})

    def admit_summarizer(self, desk, session, *, model=MODEL, provider=PROVIDER, admit_it=True):
        path = self.memory_config(session, 'summarizer')
        if admit_it:
            admit(path, desk_id=desk, provider_id=provider, model_id=model)
        return path

    def gateway_document(self, *, route=None, budget=None, key_file=None, params=None):
        route = dict({'provider': PROVIDER, 'model': MODEL, 'operation': 'chat'}, **(route or {}))
        if params is not None:
            route['params'] = params
        document = {'schema_version': 'agent-tooling.model-gateway.v1',
                    'artifact_root': str(self.role_state),
                    'providers': {PROVIDER: {'kind': 'openrouter', 'base_url': BASE_URL,
                                             'api_key_file': str(key_file or self.key_file)}},
                    'routes': {CAPABILITY: route},
                    'budgets': {CAPABILITY: dict({'max_calls_per_hour': 100, 'max_usd_per_day': 1.0,
                                                  'estimated_usd_per_call': 0.002}, **(budget or {}))}}
        return document

    def write_gateway(self, document=None, **changes):
        return write_private(self.gateway_path, document if document is not None else self.gateway_document(**changes))

    def approval_document(self, desks=(IMPL,), *, model=MODEL, capability=CAPABILITY, reserves=None):
        return {'schema_version': 'agent-tooling.summarizer-approval.v1', 'capability': capability,
                'model_id': model, 'reserves': dict(reserves or RESERVES),
                'desks': [{'binding_key': self.binding[desk], 'memory_config': str(self.summarizer[desk])}
                          for desk in desks]}

    def write_approval(self, document=None, **changes):
        return write_private(self.approval_path, document if document is not None else self.approval_document(**changes))

    # ------------------------------------------------------------------ stores
    def store(self, desk=IMPL):
        return components(self.capture[desk])[3]

    def queue(self, desk=IMPL):
        return ConsolidationQueue(self.queue_path, store=self.store(desk))

    def capture_session(self, desk):
        return 'capture-' + desk.split('-')[0]

    def seal(self, desk=IMPL, text=EVENT_TEXT, ref='visible:1'):
        receipt = self.store(desk).capture(self.capture_session(desk), source_ref=f'{desk}:{ref}',
                                           events=[{'event_id': 'choice', 'role': 'user', 'text': text}])
        return receipt['episode_id']

    def enqueue(self, desk=IMPL, ref='visible:1', text=EVENT_TEXT):
        episode = self.seal(desk, text, ref)
        job = self.queue(desk).enqueue(self.capture_session(desk), episode_ids=[episode], reason='session_end')
        return job['job_id'], episode

    def job(self, desk, job_id):
        return self.queue(desk).get(self.capture_session(desk), job_id)

    def capsules(self):
        import sqlite3
        db = sqlite3.connect(f'file:{self.state / "episodes.sqlite3"}?mode=ro', uri=True)
        try:
            return db.execute('SELECT count(*) FROM capsules').fetchone()[0]
        finally:
            db.close()

    # ------------------------------------------------------------------ role
    def role(self, *, transport='stub', **options):
        from kp_agent_tooling._impl.service.summarizer_role import SummarizerRole
        return SummarizerRole(approval_path=self.approval_path, gateway_config=self.gateway_path,
                              state_dir=self.role_state,
                              transport=self.provider if transport == 'stub' else transport, **options)

    def profile_receipt(self):
        path = self.role_state / 'model-profile.json'
        return json.loads(path.read_text()) if path.exists() else None

    def age_profile(self, hours):
        """Rewrite the role's profile receipt as if its metadata were read ``hours`` ago."""
        from kp_agent_tooling._impl.service.summarizer_role import build_profile_receipt
        receipt = self.profile_receipt()
        observed = datetime.now(timezone.utc) - timedelta(hours=hours)
        aged = build_profile_receipt(receipt['raw_model_entry'], provider_id=receipt['profile']['provider_id'],
                                     observed_at=observed, reserves=RESERVES,
                                     now=observed + timedelta(seconds=1))
        write_private(self.role_state / 'model-profile.json', aged)


class NetworkGuard:
    """Records, and refuses, every socket connection attempt in the test process."""

    def __init__(self, monkeypatch):
        self.attempts = []
        guard = self

        def connect(sock, address, *args, **kwargs):
            guard.attempts.append(('connect', address))
            raise OSError('network disabled by the S1a test guard')

        def create_connection(address, *args, **kwargs):
            guard.attempts.append(('create_connection', address))
            raise OSError('network disabled by the S1a test guard')

        monkeypatch.setattr(socket.socket, 'connect', connect)
        monkeypatch.setattr(socket.socket, 'connect_ex', connect)
        monkeypatch.setattr(socket, 'create_connection', create_connection)
