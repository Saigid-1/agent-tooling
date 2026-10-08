"""OPS tool provider for the kp-agent-tooling navigation server.

Registered under the ``kp_agent_tooling.tools`` entry-point group. It serves the
operations the core no longer carries: ``verification.*``, ``lifecycle.evidence``,
``knowledge.rationale``, the portable in-process ``knowledge.*`` composition
(``portable_knowledge_config``) and the optional legacy gateway catalog
(``gateway_config``/``gateway_endpoint``).

The behaviour is the pre-extension composition moved verbatim. Per-server state lives
on the serving ``AgentTooling`` instance (``host``) under its established attribute
names, and the clock and pinned-source reader are read through the core module, so a
patch of either governs every operation.
"""
import json
import hashlib
from pathlib import Path
from threading import RLock
import tomllib

from kp_agent_tooling._impl.service import agent_tooling as host_module
from kp_agent_tooling._impl.service.tool_providers import NOT_HANDLED
from kp_agent_tooling_ops._impl.service.gateway_transport import GatewayFailure, catalog_tools, rpc as gateway_rpc

GATEWAY_TOOLS = {'knowledge.capabilities','knowledge.reference_recovery','knowledge.reference_diagnostics','knowledge.platform','knowledge.symbol','knowledge.context','knowledge.retrieve','knowledge.check_references'}

# Operation names this provider lists itself, in listing order.
OPS_TOOLS = ('lifecycle.evidence', 'verification.guide', 'verification.packet', 'verification.finding',
             'verification.plan', 'verification.handoff', 'verification.observations',
             'knowledge.rationale', 'verification.review')


class OpsToolProvider:
    def __init__(self, host):
        self.host = host
        self._rationale_cache = None
        self._rationale_cache_lock = RLock()
        config = host.config
        host.evidence_config = dict(config, repos=config.get('retained_evidence_repos', config['repos']))
        self._portable = None
        if config.get('portable_knowledge_config'):
            if config.get('local_knowledge_config'):
                raise ValueError('choose portable_knowledge_config or local_knowledge_config')
            from kp_agent_tooling_ops._impl.service.portable_knowledge import PortableKnowledgeProvider
            # The tooling configuration rides along: under its navigation_profile the
            # knowledge tools follow the published generation (K1).
            self._portable = PortableKnowledgeProvider(config['portable_knowledge_config'], navigation=config)
        host.url, host.headers = None, {}
        if config.get('gateway_config'):
            gateway = tomllib.loads(Path(config['gateway_config']).read_text())['mcp_servers']['ops-gateway']
            host.url, host.headers = gateway['url'], gateway.get('http_headers', {})
        if config.get('gateway_endpoint'):
            if host.url is not None: raise ValueError('choose gateway_endpoint or gateway_config')
            host.url = config['gateway_endpoint']
            if config.get('gateway_headers_file'):
                host.headers = json.loads(Path(config['gateway_headers_file']).read_text())
        if host.url is not None:
            from kp_agent_tooling_ops._impl.service.gateway_transport import endpoint_parts
            endpoint_parts(host.url)
            if not isinstance(host.headers, dict) or not all(isinstance(k,str) and isinstance(v,str) for k,v in host.headers.items()):
                raise ValueError('gateway headers must be a string map')
        host.validation_receipts = {}
        host.submitted_findings = {}
        host.catalog = None
        host.gateway_discovery_error = None
        host.gateway_discovery_failure = None
        host.gateway_operation_status = {}
        # An instance attribute, so a server-level replacement governs every caller.
        host.rpc = self.rpc

    # -- provider contract -------------------------------------------------------------

    def names(self):
        return set(OPS_TOOLS) | GATEWAY_TOOLS

    def knowledge_provider(self, current):
        return self._portable if self._portable is not None else current

    def rpc(self, method, params):
        h = self.host
        if h.url is None: raise GatewayFailure('gateway_not_configured', 'configuration')
        return gateway_rpc(method, params, h.headers) if h.url == 'http://127.0.0.1:8400/api/mcp' else gateway_rpc(method, params, h.headers, endpoint=h.url)

    def remote(self, name, local_names):
        return name in GATEWAY_TOOLS and name not in local_names

    def leading_tools(self, *, include_gateway, local_names):
        h = self.host
        gateway_needed = (include_gateway and h.url is not None and
                          (h.enabled_tools is None or bool((set(h.enabled_tools) & GATEWAY_TOOLS) - local_names)))
        if gateway_needed and h.catalog is None:
            try:
                h.rpc('initialize',{'protocolVersion':'2025-06-18','capabilities':{},'clientInfo':{'name':'ops-agent-tooling','version':'1'}})
                h.catalog=catalog_tools(h.rpc('tools/list',{}), GATEWAY_TOOLS)
                h.gateway_discovery_error=None
                h.gateway_discovery_failure=None
            except GatewayFailure as failure:
                h.catalog={}
                h.gateway_discovery_failure=failure.result(stage='discovery')
                h.gateway_discovery_error='gateway_discovery_unavailable'
            except (OSError,RuntimeError):
                h.catalog={}
                h.gateway_discovery_failure=GatewayFailure('gateway_unavailable', 'unavailable', retryable=True).result(stage='discovery')
                h.gateway_discovery_error='gateway_discovery_unavailable'
        return [{**tool, 'description': tool.get('description', '') +
                 ' Gateway catalog advertisement only; backend readiness is established by a successful call.'}
                for name, tool in (h.catalog or {}).items() if name not in local_names] if gateway_needed else []

    def tools(self, schemas):
        h = self.host
        repo_schema, snapshot_schema = schemas['repo'], schemas['snapshot']
        placed = [('serena.inspect',
          {'name':'lifecycle.evidence','description':'Read revision-pinned OPS worker-thread execution evidence. Use summary for timings, report for scope, collector for delivery proof. Does not run workloads.',
          'inputSchema':{'type':'object','properties':{'evidence_id':{'type':'string','enum':list(h.config.get('evidence',{}))}},'required':['evidence_id'],'additionalProperties':False}})]
        placed += [('navigation.dependencies',
          {'name':'verification.guide','description':'Read the skill for bounded claim/evidence verification across repositories, including slice selection, context budgets and finding handoffs.',
          'inputSchema':{'type':'object','properties':{},'additionalProperties':False}}),
          ('verification.guide',
          {'name':'verification.packet','description':'Plan or read registered product evidence slices, including legacy product fixtures. Read exact sources and observations before asserting behavior; identity checks do not establish semantic correctness.',
          'inputSchema':{'type':'object','properties':{'slice_id':{'type':'string'},'mode':{'type':'string','enum':['plan','read'],'default':'plan'},'expected_packet_identity':{'type':'string','pattern':'^[0-9a-f]{64}$'},'evidence_ids':{'type':'array','items':{'type':'string'},'maxItems':20},'budget':{'type':'integer','description':'Serialized byte ceiling: 4000..100000; omit for 12000. Not a token count.','default':12000}},'required':['slice_id'],'additionalProperties':False}})]
        placed += [('verification.packet',
          {'name':'verification.finding','description':'Get the finding v5 schema (mode=schema), or validate a finding against registered packet identities, evidence references, atomic claim shape, observation compatibility and declared delivery/gap classifications (mode=validate). Does not prove semantic correctness or model receipt. Read-only; no memory promotion.',
          'inputSchema':{'type':'object','properties':{'mode':{'type':'string','enum':['schema','validate']},'finding':{'type':'object'},'expected_finding_sha256':{'type':'string','pattern':'^[0-9a-f]{64}$'},'source_profile':{'type':'string','enum':['active','retained-evidence']}},'required':['mode'],'additionalProperties':False}})]
        from kp_agent_tooling_ops._impl.evidence_references import reference_schema
        placed += [('delivery.read',
            {'name':'verification.plan','description':'Plan registered product questions with configured source identity, supplied references, bounded continuations and recovery reserve. Metadata is not evidence delivery.',
            'inputSchema':{'type':'object','properties':{'questions':{'type':'array','items':{'type':'string'},'minItems':1,'maxItems':4,'uniqueItems':True},'supplied':{'type':'array','maxItems':60,'items':reference_schema()},'budget':{'type':'integer','minimum':4000,'maximum':12000}},'required':['questions'],'additionalProperties':False}}),
            ('verification.plan',
            {'name':'verification.handoff','description':'Check complete JSON artifacts in supplied final text against validation receipts from this adapter process. Rejects omitted, changed, unvalidated or invalid findings. Does not prove subsequent publication or prose semantics.',
            'inputSchema':{'type':'object','properties':{'final_text':{'type':'string','maxLength':200000},'expected_digests':{'type':'array','minItems':1,'maxItems':20,'uniqueItems':True,'items':{'type':'string','pattern':'^[0-9a-f]{64}$'}}},'required':['final_text','expected_digests'],'additionalProperties':False}})]
        from kp_agent_tooling_ops._impl.observation_contract import input_schema
        placed += [('verification.handoff',
          {'name':'verification.observations','description':'Read immutable observations: list(subject?,after?) returns metadata; read(id,budget?) requires ONE id; assess(ids,expected_scope) requires exact sources and binding_sha256. No behavioral verdict or authority. Imports are operator CLI only.',
          'inputSchema':input_schema()})]
        from kp_agent_tooling_ops._impl.journey_registry import registered_slices, registered_questions
        placed += [('navigation.source', {
            'name': 'knowledge.rationale',
            'description': 'Read reviewed commit-body rationale at a full target revision. Exact aliases return attributed testimony; contested claims are disputed, relayed observations remain indirect, and reviewer identity is explicit. Compact mode omits excerpts; lexical candidates remain unverified. Missing means no eligible evidence in this small catalog, not no rationale. Requires an operator-configured rationale_manifest; never scans all history or establishes execution/doctrine.',
            'inputSchema': {'type': 'object', 'properties': {
                'repo_key': repo_schema, 'target_revision': {'type': 'string', 'pattern': '^(?:[0-9a-f]{40}|[0-9a-f]{64})$'},
                'query': {'type': 'string', 'minLength': 1, 'maxLength': 512},
                'top_k': {'type': 'integer', 'minimum': 1, 'maximum': 5},
                'response_mode': {'type':'string','enum':['full','compact'],'default':'full'}},
                'required': ['repo_key', 'target_revision', 'query'], 'additionalProperties': False},
            'annotations': {'readOnlyHint': True, 'destructiveHint': False, 'idempotentHint': True, 'openWorldHint': False},
        })]
        placed += [('navigation.batch',
          {'name':'verification.review','description':'Read an operator-registered requirement contract or validate a review ledger against it. Checks unchanged obligations, approved normalization rules and exact evidence references; semantic entailment and authority are not assessed.',
           'inputSchema':{'type':'object','properties':{'mode':{'type':'string','enum':['schema','contract','validate']},'contract_id':{'type':'string','enum':list(h.config.get('review_contracts',{}))},'ledger':{'type':'object'},'snapshot_id':snapshot_schema},'required':['mode'],'additionalProperties':False}})]
        by_name = {tool['name']: tool for _, tool in placed}
        by_name['verification.packet']['inputSchema']['properties']['slice_id']['enum'] = registered_slices(h.evidence_config)
        by_name['verification.plan']['inputSchema']['properties']['questions']['items']['enum'] = registered_questions(h.evidence_config)
        for _, tool in placed:
            if tool['name'].startswith('verification.'):
                tool['annotations']={'readOnlyHint':True,'destructiveHint':False,
                                     'idempotentHint':True,'openWorldHint':False}
        return placed

    def before_call(self, name, catalog):
        h = self.host
        if name in GATEWAY_TOOLS and name not in catalog and h.gateway_discovery_error:
            return dict(h.gateway_discovery_failure)
        return None

    def check_arguments(self, name, args, schema):
        import jsonschema
        if name == 'verification.observations':
            from kp_agent_tooling_ops._impl.observation_contract import argument_error, mode_errors
            errors=list(jsonschema.Draft202012Validator(schema).iter_errors(args))+mode_errors(args)
            if errors:return argument_error(args,errors)
            return None
        if name not in GATEWAY_TOOLS:
            return None
        try:
            jsonschema.validate(args,schema)
        except jsonschema.ValidationError:
            # The mint-pathways trial (2026-09-22) lost every knowledge call to this
            # branch: the arm passed snapshot_id/target_revision and `capability`, and
            # the envelope only said "review arguments". Name the offending keys; the
            # messages come from the local catalog schema, never from a remote body.
            errors=[{'path':'/'.join(str(part) for part in error.absolute_path) or '<root>',
                     'message':error.message[:240]}
                    for error in jsonschema.Draft202012Validator(schema).iter_errors(args)]
            failure=GatewayFailure('gateway_request_invalid_for_catalog', 'request').result(
                stage='catalog_validation')
            failure['schema_errors']=errors[:10]
            failure['accepted_arguments']=sorted(schema.get('properties',{}))
            failure['required_arguments']=list(schema.get('required',[]))
            unknown=sorted(set(args)-set(schema.get('properties',{}))) if isinstance(args,dict) and schema.get('additionalProperties') is False else []
            if unknown: failure['unknown_arguments']=unknown
            return failure
        return None

    def call(self, name, args, *, navigation_config, selected_snapshot_id=None):
        h = self.host
        if name == 'knowledge.rationale':
            return self._rationale(args)
        if name=='verification.review':
            from kp_agent_tooling_ops._impl.review_ledger import ledger_schema, validate_review_ledger
            if args['mode']=='schema':return {'status':'ok','schema':ledger_schema(),'contract_ids':list(h.config.get('review_contracts',{})),'semantic_verdict':'not-assessed'}
            contract=h.config.get('review_contracts',{}).get(args.get('contract_id'))
            if not contract:raise ValueError('operator-registered contract required')
            if args['mode']=='contract':return {'status':'ok','contract_id':args['contract_id'],**contract,'authority':'operator-registered; approval authenticity not assessed'}
            from kp_agent_tooling._impl.navigation_workspace import active
            config=dict(navigation_config,repos=active(navigation_config)['repos'])
            return validate_review_ledger(config,contract['specification'],args.get('ledger'),approved_rules=contract.get('approved_rules',[]))
        if name == 'verification.plan':
            from kp_agent_tooling_ops._impl.verification_plan import plan
            return plan(h.evidence_config,**args)
        if name == 'verification.handoff':
            from kp_agent_tooling_ops._impl.verification_handoff import check_handoff
            receipts=h.validation_receipts
            if h.config.get('navigation_profile'):
                from kp_agent_tooling._impl.navigation_workspace import active
                current=active(h.config)['profile_sha256']
                receipts={key:(dict(value,status='invalid',context_gap='navigation_profile_changed')
                    if value.get('validation_profile_sha256') and value['validation_profile_sha256']!=current
                    else value) for key,value in receipts.items()}
            return check_handoff(args['final_text'],args['expected_digests'],receipts)
        if name == 'verification.observations':
            from kp_agent_tooling_ops._impl.observations import ObservationStore
            if not h.config.get('observation_registry'):
                return {'status':'unavailable','reason':'observation_registry_not_configured'}
            store=ObservationStore(h.config['observation_registry'])
            if args['mode']=='list':return store.list(subject=args.get('subject'),after=args.get('after'),limit=args.get('limit',20),budget=args.get('budget',12000))
            if args['mode']=='read':return store.read_bounded(args['id'],args.get('budget',20000))
            return store.assess(args['ids'],args['expected_scope'])
        if name == 'verification.finding':
            from kp_agent_tooling_ops._impl.verification_finding import schema, validate_finding
            if args['mode']=='schema':
                return {'status':'ok','schema':schema(),'semantic_verdict':'not-assessed'}
            validation_config=h.evidence_config
            profile_sha256=None
            if h.config.get('navigation_profile') and args.get('source_profile','active')=='active':
                from kp_agent_tooling._impl.navigation_workspace import active
                profile=active(h.config)
                validation_config=dict(h.config,repos=profile['repos'])
                profile_sha256=profile['profile_sha256']
            result=validate_finding(validation_config,args.get('finding'),expected_finding_sha256=args.get('expected_finding_sha256'))
            if h.config.get('navigation_profile'):
                result=dict(result,validation_source_profile=args.get('source_profile','active'),validation_profile_sha256=profile_sha256)
            # An expected-digest mismatch is a rejected invocation, not a new
            # evidence verdict about the actual artifact. Do not poison (or
            # create) its handoff receipt with an identity-only probe.
            if any(error['code']=='finding_identity_changed' for error in result.get('errors',[])):
                return dict(result,receipt_retention='not_retained_identity_mismatch')
            result=dict(result,receipt_retention='retained')
            h.validation_receipts[result['finding_sha256']]=result
            h.submitted_findings[result['finding_sha256']]=args.get('finding')
            return result
        if name == 'verification.guide':
            if __package__.startswith('kp_agent_tooling_ops.'):
                from importlib.resources import files
                text=files('kp_agent_tooling_ops').joinpath('assets/verify-behavior.md').read_text()
            else:
                text=(Path(__file__).resolve().parents[2]/'.agents/skills/verify-behavior/SKILL.md').read_text()
            return {'status':'ok','skill':text,'sha256':hashlib.sha256(text.encode()).hexdigest()}
        if name == 'verification.packet':
            from kp_agent_tooling_ops._impl.verification_packet import packet
            return packet(h.evidence_config,**args)
        if name in GATEWAY_TOOLS:
            gateway_started = host_module.perf_counter_ns()
            try:
                result=h.rpc('tools/call',{'name':name,'arguments':args})
            except GatewayFailure as failure:
                h.gateway_operation_status[name]={'status':'failed','reason':failure.reason,
                                                   'category':failure.category}
                diagnostic = failure.result()
                if name in {'knowledge.reference_recovery', 'knowledge.reference_diagnostics'} and failure.category == 'request':
                    diagnostic['next_step'] = ('Repeat the parent retrieve/context read and use its generated continuation unchanged, '
                        'including repo_key, path, blob_sha, byte_offset, byte_length, generation_digest and code_revisions. '
                        'This gateway rejection does not establish a missing reference or an expired generation.')
                    diagnostic['operation'] = name
                return diagnostic
            content=result.get('structuredContent') or result
            h.gateway_operation_status[name]={'status':'response_received',
                'reported_status':content.get('status') if isinstance(content,dict) else None}
            if isinstance(content, dict):
                content = dict(content, gateway_transport={'elapsed_ms': round((host_module.perf_counter_ns() - gateway_started) / 1_000_000, 3)})
            return content
        if name=='lifecycle.evidence':
            path=h.config['evidence'][args['evidence_id']]
            blob,text=host_module.source(h.config['evidence_repo'],h.config['evidence_revision'],path)
            if len(text.encode())>100_000:raise ValueError('evidence exceeds bound')
            from kp_agent_tooling_ops._impl.evidence_references import lifecycle_reference
            return {'status':'ok','reference':lifecycle_reference(args['evidence_id'],h.config['evidence_revision'],blob),'evidence_id':args['evidence_id'],'source_revision':h.config['evidence_revision'],
                    'path':path,'blob_sha':blob,'content':json.loads(text) if path.endswith('.json') else text,
                    'scope':'retained experiment evidence; not current deployment'}
        return NOT_HANDLED

    def doctor_preflight(self):
        h = self.host
        for name,path in h.config.get('evidence',{}).items():host_module.source(h.config['evidence_repo'],h.config['evidence_revision'],path)

    def doctor(self, *, names, availability, unavailable, local_names):
        h = self.host
        if not ('knowledge.symbol' in local_names and 'knowledge.symbol' in names) and h.url is not None:
            if h.gateway_discovery_error:
                unavailable('knowledge.symbol', 'gateway_discovery_unavailable',
                            'Restore the configured gateway and inspect its advertised operations.')
            elif 'knowledge.symbol' not in names:
                unavailable('knowledge.symbol', 'scip_operation_not_advertised',
                            'Enable knowledge.symbol and verify that the configured gateway advertises an indexed SCIP source.')
            else:
                availability['knowledge.symbol'] = {'status': 'configured', 'runtime_verification': 'not-assessed'}
        gateway_failed=bool(h.gateway_discovery_error or any(row['status']=='failed' for row in h.gateway_operation_status.values()))
        fields = {'gateway_gap':h.gateway_discovery_error,'gateway_configured':h.url is not None,
                'gateway_catalog_status':('not_configured' if h.url is None else 'not_attempted' if h.catalog is None else 'discovery_failed' if h.gateway_discovery_error else 'advertised'),
                'gateway_backend_status':('not_probed' if not h.gateway_operation_status else 'operation_failed' if any(row['status']=='failed' for row in h.gateway_operation_status.values()) else 'response_received'),
                'gateway_operation_status':dict(h.gateway_operation_status)}
        return fields, gateway_failed

    # -- knowledge.rationale -----------------------------------------------------------

    def _rationale(self, args):
        # This manifest is host configuration, never a caller-supplied path or approval.
        # Each instance exposes only its configured repositories; this is not a
        # replacement for authentication in a multi-tenant hosting service.
        perf_counter_ns = host_module.perf_counter_ns
        started_ns = perf_counter_ns()
        catalog_started_ns = None
        catalog_finished_ns = None
        search_started_ns = None
        cache_hit = False

        def with_timing(result):
            ended_ns = perf_counter_ns()
            def elapsed_ms(begin, end):
                return (end - begin) / 1_000_000

            catalog_end_ns = catalog_finished_ns if catalog_finished_ns is not None else ended_ns
            return {**result, 'catalog_cache': {'hit':cache_hit,'scope':'process-local; manifest and repository pins'}, 'timing_ms': {
                'total': elapsed_ms(started_ns, ended_ns),
                'catalog_validation': (elapsed_ms(catalog_started_ns, catalog_end_ns)
                                       if catalog_started_ns is not None else None),
                'search': (elapsed_ms(search_started_ns, ended_ns)
                           if search_started_ns is not None else None),
            }}

        base = {'schema_version': 'ops.knowledge-rationale.v1',
                'repo_key': args['repo_key'], 'target_revision': args['target_revision'],
                'runtime_execution': 'not-assessed', 'doctrine_authority': 'not-established',
                'absence_verdict': 'not-established'}
        manifest = self.host.config.get('rationale_manifest')
        if not manifest:
            return with_timing({**base, 'status': 'unverified', 'reason': 'rationale_catalog_unconfigured',
                                'results': [], 'next_action': 'Configure a reviewed rationale manifest; use bounded literal Git-message search meanwhile.'})
        from kp_agent_tooling_ops._impl.commit_rationale import RationaleCatalog, RationaleError
        from kp_agent_tooling._impl.navigation_workspace import active
        try:
            catalog_started_ns = perf_counter_ns()
            path = Path(manifest)
            if not path.is_absolute() or path.is_symlink():
                raise ValueError('absolute operator manifest required')
            repos = active(self.host.config)['repos']
            catalog, cache_hit = self._validated_rationale_catalog(path, repos)
            catalog_finished_ns = perf_counter_ns()
            search_started_ns = catalog_finished_ns
            result = catalog.search(args['repo_key'], args['query'], args['target_revision'],
                                    top_k=args.get('top_k', 3), response_mode=args.get('response_mode','full'))
            catalog.assert_current()
            return with_timing({**base, **result})
        except (RationaleError, OSError, ValueError, KeyError, TypeError) as error:
            return with_timing({**base, 'status': 'unverified', 'reason': 'rationale_catalog_or_source_unavailable',
                                'detail': str(error)[:240] if isinstance(error, RationaleError) else 'Review host configuration and pinned source availability.',
                                'results': [], 'next_action': 'Review the configured manifest and pinned Git objects; do not infer absent rationale.'})

    def _validated_rationale_catalog(self, path, repos):
        from kp_agent_tooling_ops._impl.commit_rationale import RationaleCatalog, _manifest_bytes
        # One bounded cache per host adapter. Never share across host/tenant configs.
        raw = _manifest_bytes(path)
        bindings = []
        for key, row in sorted(repos.items()):
            root = Path(row['path']).resolve(strict=True)
            st = root.stat()
            bindings.append((key, str(root), row['revision'], st.st_dev, st.st_ino))
        identity = (str(path.resolve()), hashlib.sha256(raw).hexdigest(), tuple(bindings))
        with self._rationale_cache_lock:
            if self._rationale_cache and self._rationale_cache[0] == identity:
                catalog = self._rationale_cache[1]
                catalog.assert_current()
                return catalog, True
            # Discard an old catalog even when validating its replacement fails.
            self._rationale_cache = None
            catalog = RationaleCatalog(path, {key: row['path'] for key, row in repos.items()},
                                       {key: row['revision'] for key, row in repos.items()})
            if catalog.manifest_sha256 != identity[1]:
                raise ValueError('manifest changed during validation')
            catalog.assert_current()
            self._rationale_cache = (identity, catalog)
            return catalog, False
