"""Read-only, harness-independent composition of existing OPS navigation services.

The core serves navigation, identity, delivery and local knowledge operations. Other
operations come from providers installed under the ``kp_agent_tooling.tools``
entry-point group (see ``tool_providers``).
"""
import json
import hashlib
import tempfile
import os
import subprocess
# Providers read the clock and pinned sources through this module, so one patch
# point governs every served operation.
from time import perf_counter_ns  # noqa: F401
from pathlib import Path
from kp_agent_tooling._impl import leaf
from kp_agent_tooling._impl.source_citations import source, source_reference
from kp_agent_tooling._impl.server_identity import identity as server_identity
from kp_agent_tooling._impl.service.import_context import import_context
from kp_agent_tooling._impl.service.serena_navigation import SerenaNavigationProvider, boundary
from kp_agent_tooling._impl.service import tool_providers
from kp_agent_tooling._impl.local_knowledge import OPERATIONS as LOCAL_KNOWLEDGE_OPERATIONS


def revision_selection_failure(repo_key, requested, selected, snapshot_id=None):
    return {'status': 'unavailable', 'reason': 'requested_revision_not_selected',
            'repo_key': repo_key, 'requested_revision': requested,
            'selected_revision': selected, 'snapshot_id': snapshot_id,
            'absence_verdict': 'not-established',
            'message': 'This operation requires the selected source snapshot; the requested revision differs.',
            'recovery': 'Inspect tooling.identity for the active source revision. If using an older frozen snapshot, capture a new navigation.snapshot after refresh publishes the intended revision. Do not substitute another revision as evidence.',
            'next_call': {'name': 'tooling.identity', 'arguments': {}}}


class AgentTooling:
    def __init__(self, config, knowledge_provider=None):
        self.config_path = Path(config)
        config_bytes = self.config_path.read_bytes()
        self.config_sha256 = hashlib.sha256(config_bytes).hexdigest()
        self.config = json.loads(config_bytes)
        if self.config.get('schema_version') != 'ops.agent-tooling.v1': raise ValueError('unsupported configuration')
        mappings=self.config.get('import_mappings',{})
        if not isinstance(mappings,dict):raise ValueError('import mappings must be an object')
        for root,row in mappings.items():
            if (not isinstance(root,str) or not root or '.' in root or not isinstance(row,dict) or
                    set(row)!={'repo_key','distribution'} or row.get('repo_key') not in self.config['repos'] or
                    not isinstance(row.get('distribution'),str) or not row['distribution']):
                raise ValueError('invalid import mapping')
        self.server_identity = server_identity(self.config, self.config_sha256)
        # Installed providers validate the configuration they own when constructed.
        self.providers = tool_providers.load(self)
        self._provider_knowledge = False
        for provider in self.providers:
            if hasattr(provider, 'knowledge_provider'):
                supplied = provider.knowledge_provider(knowledge_provider)
                if supplied is not knowledge_provider:
                    knowledge_provider, self._provider_knowledge = supplied, True
        if knowledge_provider is None and self.config.get('local_knowledge_config'):
            from kp_agent_tooling._impl.local_knowledge import LocalKnowledgeProvider
            knowledge_provider = LocalKnowledgeProvider(self.config['local_knowledge_config'])
        self.knowledge_provider = knowledge_provider
        self.enabled_tools = self.config.get('enabled_tools')
        if self.enabled_tools is not None and (not isinstance(self.enabled_tools, list) or
                not all(isinstance(name, str) for name in self.enabled_tools)):
            raise ValueError('enabled_tools must be a list of operation names')
        from kp_agent_tooling._impl.tool_delivery import DeliveryStore
        namespace=leaf.sorted_sha256(self.config)
        # A 48 KB inline ceiling keeps a whole-tree search page or an eight-read batch in
        # one response; only genuinely large results (SCIP traces, big batches) page.
        configured_delivery = self.config.get('delivery_root')
        if configured_delivery is not None and (not isinstance(configured_delivery, str) or
                not Path(configured_delivery).is_absolute() or
                Path(configured_delivery).resolve() != Path(configured_delivery)):
            raise ValueError('delivery_root must be an absolute real directory')
        delivery_root = ((Path(configured_delivery) if configured_delivery is not None
                          else Path(tempfile.gettempdir())/'ops-tool-delivery') / namespace)
        self.delivery=DeliveryStore(delivery_root,
            max_inline_bytes=48_000, max_page_bytes=16_384, max_response_bytes=64_000)
        configured_registry = self.config.get('snapshot_registry')
        if configured_registry is None:
            self.snapshot_registry=self.delivery.root/'snapshots'
            leaf.mkdir_private(self.snapshot_registry, exist_ok=True)
        else:
            self.snapshot_registry=Path(configured_registry)
            if (not self.snapshot_registry.is_absolute() or
                    not self.snapshot_registry.is_dir() or
                    self.snapshot_registry.is_symlink()):
                raise ValueError('snapshot_registry must be an existing absolute server-owned directory')

    def tools(self, include_gateway=True):
        available, unknown = self._catalog(include_gateway)
        # With a provider installed the known catalog is complete, so an unknown name is a
        # configuration error. With the core alone it is a reported gap (unavailable_tools).
        if unknown and self.providers:
            raise ValueError('unknown configured operations: ' + ', '.join(name for name, _ in unknown))
        return available

    def unavailable_tools(self):
        """Configured operation names nothing installed serves, each with a reason.

        Only the core alone reports any: it serves the available names and lists these
        as a gap instead of refusing the configuration.
        """
        if self.providers or self.enabled_tools is None:
            return []
        _, unknown = self._catalog(include_gateway=False)
        actions = {'provider_not_installed': 'Install a tool provider that serves this operation, or remove it from enabled_tools.',
                   'unknown_operation': 'Correct or remove this operation name in enabled_tools; no installed component serves it.'}
        return [{'name': name, 'reason': reason, 'next_action': actions[reason]} for name, reason in unknown]

    def _catalog(self, include_gateway):
        local = self.knowledge_provider.tools() if self.knowledge_provider is not None else []
        local_names = {tool['name'] for tool in local}
        # Operations advertised by a provider's remote catalog precede local ones.
        leading = []
        for provider in self.providers:
            if hasattr(provider, 'leading_tools'):
                leading += provider.leading_tools(include_gateway=include_gateway, local_names=local_names)
        custom=[{'name':'tooling.identity','description':'Read the serving Git source build revision and raw startup configuration digest. Configured navigation/evidence revisions are separate; executable artifact integrity is not attested. Unversioned or modified source reports unknown.',
          'inputSchema':{'type':'object','properties':{},'additionalProperties':False}},
          {'name':'serena.inspect','description':'Inspect one symbol and bounded inbound references with live Serena. When references are requested for one resolved symbol, also reports separate bounded textual candidates; these may be comments, strings, declarations, or unrelated names and are never references or proven callers. Paths and exact revisions are operator-configured. Returns import context and exact knowledge.symbol continuations; follow these instead of guessing dependency paths. Static references do not prove runtime calls.',
          'inputSchema':{'type':'object','properties':{'repo_key':{'type':'string','enum':list(self.config['repos'])},'path':{'type':'string'},'symbol':{'type':'string'}},'required':['repo_key','path','symbol'],'additionalProperties':False}}]
        custom += [{'name':'navigation.imports','description':'Read bounded committed Python/TypeScript imports and re-exports with exact SCIP declaration lookup continuations. Useful before guessing dependency paths.',
          'inputSchema':{'type':'object','properties':{'repo_key':{'type':'string','enum':list(self.config['repos'])},'path':{'type':'string'},'dependency_revisions':{'type':'object','propertyNames':{'enum':list(self.config['repos'])},'additionalProperties':{'type':'string','pattern':'^(?:[0-9a-f]{40}|[0-9a-f]{64})$'},'maxProperties':16}},'required':['repo_key','path'],'additionalProperties':False}},
          {'name':'navigation.dependencies','description':'Identify ancestor source dependency manifests and selected installed Serena provider packages. Declared, installed and deployed identities remain separate.',
          'inputSchema':{'type':'object','properties':{'repo_key':{'type':'string','enum':list(self.config['repos'])},'path':{'type':'string'}},'required':['repo_key','path'],'additionalProperties':False}}]
        custom += [{'name':'delivery.read','description':'Read one bounded fragment of an immutable large tool response. Reassemble all text pages and verify SHA-256 before treating the original JSON as complete evidence.',
            'inputSchema':{'type':'object','properties':{'token':{'type':'string','pattern':'^[0-9a-f]{64}$'},'offset':{'type':'integer','minimum':0},'limit':{'type':'integer','minimum':1,'maximum':16384}},'required':['token'],'additionalProperties':False}}]
        repo_schema={'type':'string','enum':list(self.config['repos'])}
        custom += [
            {'name':'navigation.workspace','description':'Observe current worktrees and potential file overlaps against the last published default-branch snapshot. Reports exact commits and refresh time. Overlap is not a proven conflict; uncommitted work is not accepted evidence.',
             'inputSchema':{'type':'object','properties':{'repo_key':repo_schema,'path':{'type':'string','description':'Optional relative file or directory focus to keep comparisons small'},'limit':{'type':'integer','minimum':1,'maximum':64}},'required':['repo_key'],'additionalProperties':False}},
            {'name':'navigation.source','description':'Read bounded committed source at an explicit full revision, including available in-flight branch commits. Reports the selected snapshot revision/identity separately from a timestamped active-profile observation. Use when an index is stale or to compare a workspace head.',
             'inputSchema':{'type':'object','properties':{'repo_key':repo_schema,'target_revision':{'type':'string','pattern':'^(?:[0-9a-f]{40}|[0-9a-f]{64})$'},'path':{'type':'string'},'start_line':{'type':'integer','minimum':1},'line_count':{'type':'integer','minimum':1,'maximum':200}},'required':['repo_key','target_revision','path'],'additionalProperties':False}}]
        snapshot_schema={'type':'string','pattern':'^navigation-snapshot:sha256:[0-9a-f]{64}$'}
        revision_schema={'type':'string','pattern':'^(?:[0-9a-f]{40}|[0-9a-f]{64})$'}
        custom += [
          {'name':'navigation.snapshot','description':'Capture or read a frozen review source and analysis selection. Source defaults may advance independently; use snapshot_id for consistent subsequent navigation. Does not freeze runtime behavior.',
           'inputSchema':{'type':'object','properties':{'mode':{'type':'string','enum':['capture','read']},'snapshot_id':snapshot_schema},'required':['mode'],'additionalProperties':False}},
          {'name':'navigation.paths','description':'Find bounded regular-file paths in an exact committed tree, never the dirty checkout. Use before guessing a symbol file. A glob searches repository-relative paths.',
           'inputSchema':{'type':'object','properties':{'repo_key':repo_schema,'target_revision':revision_schema,'pattern':{'type':'string','maxLength':256},'limit':{'type':'integer','minimum':1,'maximum':500},'snapshot_id':snapshot_schema},'required':['repo_key','target_revision'],'additionalProperties':False}},
          {'name':'navigation.search','description':'Search exact committed UTF-8 source using literal text or a line glob. Reports bounded citations, exclusions and truncation; matches are not semantic references.',
           'inputSchema':{'type':'object','properties':{'repo_key':repo_schema,'target_revision':revision_schema,'query':{'type':'string','minLength':1,'maxLength':256},'mode':{'type':'string','enum':['literal','glob']},'path_pattern':{'type':'string','minLength':1,'maxLength':256,'default':'*','description':'Optional repository-relative fnmatch glob; omitted means all regular committed files, subject to search budgets. *.py includes root and nested Python files; **/*.py requires a directory component.'},'limit':{'type':'integer','minimum':1,'maximum':500},'snapshot_id':snapshot_schema},'required':['repo_key','target_revision','query'],'additionalProperties':False}},
          {'name':'navigation.semantic','description':'Rank indexed committed source spans by similarity to a concept query (where is the code that does X). A separately labelled ranking layer: it returns what it found with exact citations and navigation.source continuations, never an absence verdict. Use navigation.search for exhaustive literal coverage. Requires a semantic index published for the selected revision.',
           'inputSchema':{'type':'object','properties':{'repo_key':repo_schema,'target_revision':revision_schema,'query':{'type':'string','minLength':1,'maxLength':512},'limit':{'type':'integer','minimum':1,'maximum':20,'default':8},'path_pattern':{'type':'string','minLength':1,'maxLength':256,'default':'*'},'kinds':{'type':'array','items':{'type':'string','enum':['function','class','method','window']},'maxItems':4},'snapshot_id':snapshot_schema},'required':['repo_key','query'],'additionalProperties':False}},
          {'name':'navigation.batch','description':'Run up to eight independent bounded navigation reads against one frozen snapshot. Per-item failures are explicit. Does not execute tests or invoke Serena; use it for source/discovery/compiler/dependency reads.',
           'inputSchema':{'type':'object','properties':{'snapshot_id':snapshot_schema,'requests':{'type':'array','minItems':1,'maxItems':8,'items':{'type':'object','properties':{'name':{'type':'string','enum':['navigation.paths','navigation.search','navigation.source','navigation.imports','navigation.dependencies','knowledge.platform','knowledge.symbol']},'arguments':{'type':'object'}},'required':['name','arguments'],'additionalProperties':False}}},'required':['snapshot_id','requests'],'additionalProperties':False}}]
        custom += [
          {'name':'serena.overview','description':'List symbols in a committed file without needing a known symbol name. Provider metadata is not source-body delivery; inspect a chosen symbol for exact coordinates.',
           'inputSchema':{'type':'object','properties':{'repo_key':repo_schema,'path':{'type':'string'},'depth':{'type':'integer','minimum':0,'maximum':2},'max_answer_chars':{'type':'integer','minimum':1000,'maximum':24000},'response_mode':{'type':'string','enum':['full','compact']}},'required':['repo_key','path'],'additionalProperties':False}},
          {'name':'serena.find','description':'Find bounded exact-name symbol candidates (not fuzzy or semantic search) across the selected repository, optionally within a file. Every returned location is checked against the selected commit. Ambiguity and no results are explicit.',
           'inputSchema':{'type':'object','properties':{'repo_key':repo_schema,'symbol':{'type':'string','minLength':1,'maxLength':256},'path':{'type':'string'},'include_body':{'type':'boolean'},'max_matches':{'type':'integer','minimum':1,'maximum':100,'default':25},'max_answer_chars':{'type':'integer','minimum':1000,'maximum':24000},'response_mode':{'type':'string','enum':['full','compact']}},'required':['repo_key','symbol'],'additionalProperties':False}}]
        custom += [
          {'name':'navigation.manifest','description':'Inventory every tracked artifact at an exact commit. Paged entries have stable identities and explicit classification/eligibility; inventory completeness is not semantic or runtime coverage.',
           'inputSchema':{'type':'object','properties':{'repo_key':repo_schema,'target_revision':revision_schema,'snapshot_id':snapshot_schema,'offset':{'type':'integer','minimum':0},'limit':{'type':'integer','minimum':1,'maximum':200},'expected_manifest_id':{'type':'string','maxLength':128}},'required':['repo_key','target_revision'],'additionalProperties':False}},
          {'name':'navigation.search_page','description':'Resume bounded text search over a revision-pinned manifest. Follow continuation_token with unchanged arguments until traversal completes; inspect coverage and exclusions before claiming no matches. Requires an operator-configured persistent navigation registry.',
           'inputSchema':{'type':'object','properties':{'repo_key':repo_schema,'target_revision':revision_schema,'snapshot_id':snapshot_schema,'query':{'type':'string','minLength':1,'maxLength':256},'mode':{'type':'string','enum':['literal','glob']},'path_pattern':{'type':'string','minLength':1,'maxLength':256,'default':'*'},'continuation_token':{'type':'string','maxLength':256},'limit':{'type':'integer','minimum':1,'maximum':100}},'required':['repo_key','target_revision','query'],'additionalProperties':False}}]
        schemas={'repo':repo_schema,'snapshot':snapshot_schema,'revision':revision_schema}
        for provider in self.providers:
            for after, tool in provider.tools(schemas):
                names=[t['name'] for t in custom]
                custom.insert(names.index(after)+1 if after in names else len(custom), tool)
        for tool in custom+local:
            if tool['name'] in {'serena.inspect','serena.overview','serena.find','navigation.imports','navigation.dependencies','navigation.source','knowledge.platform','knowledge.symbol'}:
                tool['inputSchema']['properties']['snapshot_id']=snapshot_schema
            if tool['name'] in {'serena.inspect','serena.overview','serena.find','navigation.imports','navigation.dependencies','knowledge.platform'}:
                tool['inputSchema']['properties']['target_revision']=revision_schema
            if tool['name']=='serena.inspect':
                tool['inputSchema']['properties'].update({
                  'include_body':{'type':'boolean'},'include_references':{'type':'boolean'},'include_info':{'type':'boolean'},'include_imports':{'type':'boolean'},
                  'depth':{'type':'integer','minimum':0,'maximum':2},'max_matches':{'type':'integer','minimum':1,'maximum':100,'default':25},
                  'max_answer_chars':{'type':'integer','minimum':1000,'maximum':24000},'response_mode':{'type':'string','enum':['full','compact','overview']}})
        for tool in custom:
            if tool['name'] == 'tooling.identity':
                tool['annotations']={'readOnlyHint':True,'destructiveHint':False,
                                     'idempotentHint':True,'openWorldHint':False}
        available = leading + local + custom
        unknown = []
        if self.enabled_tools is not None:
            known = {t['name'] for t in custom} | local_names | set(LOCAL_KNOWLEDGE_OPERATIONS)
            for provider in self.providers:
                known |= set(provider.names())
            core_namespaces = {t['name'].split('.', 1)[0] for t in custom}
            unknown = [(name, 'unknown_operation' if name.split('.', 1)[0] in core_namespaces
                        else 'provider_not_installed')
                       for name in sorted(set(self.enabled_tools) - known)]
            available = [t for t in available if t['name'] in self.enabled_tools]
        return available, unknown

    def call(self,name,args):
        import jsonschema
        # Local operations do not need remote discovery or send their inputs there.
        local_names = {t['name'] for t in self.knowledge_provider.tools()} if self.knowledge_provider is not None else set()
        remote = any(provider.remote(name, local_names) for provider in self.providers
                     if hasattr(provider, 'remote'))
        catalog={t['name']:t for t in self.tools(include_gateway=remote)}
        for provider in self.providers:
            if hasattr(provider, 'before_call'):
                early = provider.before_call(name, catalog)
                if early is not None: return early
        if name not in catalog:raise ValueError('tool outside read-only allowlist')
        for provider in self.providers:
            if hasattr(provider, 'check_arguments'):
                early = provider.check_arguments(name, args, catalog[name]['inputSchema'])
                if early is not None: return early
        jsonschema.validate(args,catalog[name]['inputSchema'])
        if name == 'tooling.identity':
            result=dict(self.server_identity)
            if self.config.get('navigation_profile'):
                from kp_agent_tooling._impl.navigation_workspace import active
                profile=active(self.config)
                result['active_navigation_profile']={k:profile.get(k) for k in ('profile','published_at','profile_sha256','refresh_status')}
                result['configured_pins_role']='retained-evidence; active navigation revisions reported separately'
                result['active_navigation_source_revisions']={k:r['revision'] for k,r in profile['repos'].items()}
            gap = self.unavailable_tools()
            if gap:
                result['unavailable_tools'] = gap
            for provider in self.providers:
                if hasattr(provider, 'identity'):
                    provider.identity(result)
            return result
        if name=='navigation.snapshot':
            from kp_agent_tooling._impl.navigation_snapshot import capture, select
            if args['mode']=='capture':
                if args.get('snapshot_id'):raise ValueError('capture does not accept snapshot_id')
                selected=capture(self.config,self.snapshot_registry)
            else:
                _,selected=select(self.config,self.snapshot_registry,args.get('snapshot_id'))
            return {'status':'ok',**{k:selected[k] for k in ('snapshot_id','source_profile_sha256','analysis_config_sha256')},
                    'source_revisions':{k:v['revision'] for k,v in selected['repos'].items()},'runtime_execution':'not-assessed'}
        if name=='navigation.batch':
            from kp_agent_tooling._impl.navigation_snapshot import select
            select(self.config,self.snapshot_registry,args['snapshot_id'])
            results=[]
            for row in args['requests']:
                arguments=dict(row['arguments'])
                if arguments.get('snapshot_id',args['snapshot_id'])!=args['snapshot_id']:raise ValueError('batch snapshot mismatch')
                arguments['snapshot_id']=args['snapshot_id']
                try:result=self.call(row['name'],arguments)
                except (ValueError,KeyError,OSError,jsonschema.ValidationError) as error:
                    result={'status':'unavailable','reason':type(error).__name__,'detail':str(error)[:200]}
                # Rows stay inline: the caller's transport applies one delivery decision to
                # the whole envelope, instead of a token per row inside a token.
                results.append({'name':row['name'],'result':result})
            return {'status':'partial' if any(x['result'].get('status') in {'error','invalid','unavailable'} for x in results) else 'ok','snapshot_id':args['snapshot_id'],'results':results}
        navigation_config=self.config
        selection=None
        if args.get('snapshot_id'):
            from kp_agent_tooling._impl.navigation_snapshot import select, target_revision
            navigation_config,selection=select(self.config,self.snapshot_registry,args['snapshot_id'])
            if args.get('repo_key'):
                expected = selection['repos'][args['repo_key']]['revision']
                if args.get('target_revision') and args['target_revision'] != expected:
                    return revision_selection_failure(args['repo_key'], args['target_revision'], expected, args['snapshot_id'])
                target_revision(selection,args['repo_key'],args.get('target_revision'))
        args=dict(args)
        selected_snapshot_id=args.pop('snapshot_id',None)
        semantic_profile=None
        if name in {'serena.inspect','serena.overview','serena.find','navigation.imports','navigation.dependencies','knowledge.platform'} and args.get('target_revision'):
            from kp_agent_tooling._impl.navigation_workspace import active
            semantic_profile=active(navigation_config)
            expected=semantic_profile['repos'][args['repo_key']]['revision']
            if args['target_revision'] != expected:
                return revision_selection_failure(args['repo_key'], args['target_revision'], expected, selected_snapshot_id)
            args.pop('target_revision')
        if name in {'navigation.manifest','navigation.search_page'}:
            from kp_agent_tooling._impl.service.repository_coverage import call
            return call(navigation_config, name, args)
        if name=='navigation.semantic':
            return self._semantic(navigation_config, selection, args)
        if name in {'navigation.paths','navigation.search'}:
            from kp_agent_tooling._impl.navigation_discovery import paths,search,DiscoveryBudgetExceeded,DiscoveryInputError
            try:
                return (paths if name=='navigation.paths' else search)(navigation_config,**args)
            except DiscoveryBudgetExceeded as error:
                failure=error.result(args['repo_key'], args['target_revision'],
                                    args.get('path_pattern', args.get('pattern', '*')),
                                    query=args.get('query'),mode=args.get('mode','literal'),
                                    limit=args.get('limit',100),snapshot_id=selected_snapshot_id)
                # A whole-repo literal search over core exceeds the unpaged line budget
                # (2026-09-22 trial: 200 001 lines). When the persistent registry exists the
                # paged search can answer the same question, so serve its first page here
                # instead of an error the caller has to re-issue by hand.
                next_call=failure.get('next_call')
                if (name=='navigation.search' and next_call
                        and isinstance(navigation_config.get('navigation_registry_path'),str)):
                    from kp_agent_tooling._impl.service.repository_coverage import call as coverage_call
                    page_args={k:v for k,v in next_call['arguments'].items() if k!='snapshot_id'}
                    try:
                        page=coverage_call(navigation_config,'navigation.search_page',page_args)
                    except (OSError,RuntimeError,ValueError,subprocess.SubprocessError):
                        return failure
                    if isinstance(page,dict) and page.get('status') in {'ok','partial','incomplete'} and 'results' in page:
                        page['degraded_from']={'name':'navigation.search','reason':'search_budget_exceeded',
                                               'budget':error.budget,
                                               'note':'unpaged search exceeded its budget; this is page one of navigation.search_page over the same scope'}
                        if page.get('continuation_token'):
                            page['next_call']={'name':'navigation.search_page',
                                               'arguments':dict(next_call['arguments'],continuation_token=page['continuation_token'])}
                        return page
                return failure
            except DiscoveryInputError as error:
                return error.result()
        if name in {'navigation.workspace','navigation.source'}:
            from kp_agent_tooling._impl.navigation_workspace import workspace, read_source, observe_active_revision
            if name=='navigation.source' and selected_snapshot_id:
                args['selected_snapshot_id']=selected_snapshot_id
                args['active_navigation_revision']=observe_active_revision(self.config,args['repo_key'])
            return (workspace if name=='navigation.workspace' else read_source)(navigation_config,**args)
        if name in local_names:
            if navigation_config.get('navigation_profile') and not self._provider_knowledge:
                from kp_agent_tooling._impl.navigation_workspace import active
                from kp_agent_tooling._impl.local_knowledge import LocalKnowledgeProvider
                profile=active(navigation_config)
                result=LocalKnowledgeProvider(profile['knowledge_config']).call(name,args)
                result['navigation_profile']={k:profile.get(k) for k in ('profile','published_at','profile_sha256')}
                return result
            return self.knowledge_provider.call(name,args)
        if name == 'delivery.read':
            return self.delivery.read(**args)
        for provider in self.providers:
            result = provider.call(name, args, navigation_config=navigation_config,
                                   selected_snapshot_id=selected_snapshot_id)
            if result is not tool_providers.NOT_HANDLED:
                return result
        if name in {'navigation.imports','navigation.dependencies'}:
            from kp_agent_tooling._impl.navigation_workspace import active
            selected_profile=semantic_profile or active(navigation_config)
            repo=selected_profile['repos'][args['repo_key']]
            if name == 'navigation.dependencies':
                from kp_agent_tooling._impl.dependency_identity import declared, provider
                declarations=declared(repo['path'],repo['revision'],args['path'])
                provider_python=self.config.get('serena',{}).get('python')
                observed=provider(provider_python) if provider_python else {'status':'unavailable','reason':'provider_not_configured'}
                return {'status':'ok' if observed['status']=='observed' else 'partial',
                    'declared':declarations,'provider':observed,'deployed':'not-assessed'}
            if args['path'].endswith('.py'):
                overrides=args.get('dependency_revisions',{})
                authorized_targets={row['repo_key'] for row in
                    navigation_config.get('import_mappings',{}).values()}
                if set(overrides)-authorized_targets:
                    raise ValueError('dependency revision target is not authorized by import mapping')
                return import_context(repo['path'],repo['revision'],args['repo_key'],args['path'],None,[],
                    repositories=selected_profile['repos'],
                    import_mappings=navigation_config.get('import_mappings',{}),
                    dependency_revisions=overrides)
            from kp_agent_tooling._impl.typescript_context import context
            return context(repo['path'],repo['revision'],args['repo_key'],args['path'],navigation_config.get('typescript'))
        path=args.get('path','');symbol=args.get('symbol','')
        if (not path and name!='serena.find') or (path and (Path(path).is_absolute() or '\\' in path or any(p in ('','.','..') for p in path.split('/')) or '::' in path)) or '::' in symbol:
            raise ValueError('canonical relative path and separate symbol required')
        from kp_agent_tooling._impl.navigation_workspace import active
        nav_profile=semantic_profile or active(navigation_config)
        repo=nav_profile['repos'][args['repo_key']];cfg=navigation_config['serena'];home=Path(cfg['runtime_home'])
        # This facade is a dedicated process; calls are serialized by the CLI/MCP host.
        os.environ.update(PATH=os.pathsep.join(dict.fromkeys([str(Path(cfg['command']).parent), *os.environ.get('PATH', os.defpath).split(os.pathsep)])),SERENA_HOME=str(home/'serena-home'),UV_CACHE_DIR=str(home/'uv-cache'),
          UV_PYTHON_INSTALL_DIR=str(home/'uv-python'),UV_TOOL_DIR=str(home/'uv-tools'),npm_config_cache=str(home/'npm-cache'),UV_OFFLINE='true')
        options={k:args[k] for k in ('include_body','include_references','include_info','depth','max_matches','max_answer_chars','response_mode') if k in args}
        import time
        started=time.monotonic()
        provider=SerenaNavigationProvider(cfg['command'],cfg['python'])
        if name=='serena.overview':
            result=provider.overview(Path(repo['path']),repo['revision'],path,**{k:args[k] for k in ('depth','max_answer_chars') if k in args})
        elif name=='serena.find':
            args.setdefault('response_mode','compact')
            result=provider.find(Path(repo['path']),repo['revision'],symbol,path=path,**{k:args[k] for k in ('include_body','max_matches','max_answer_chars','response_mode') if k in args})
            if result.get('outcome') == 'no_exact_match':
                result['next_call']={'name':'navigation.search_page','arguments':{'repo_key':args['repo_key'], 'target_revision':repo['revision'], 'query':symbol.rsplit('/',1)[-1], 'mode':'literal','path_pattern':path or '*','limit':12}}
                result['recovery']='Follow next_call for literal candidates; shorten or change the term if the implementation uses a different name. No match is not evidence of absence.'
        else:
            result=provider.inspect(Path(repo['path']),repo['revision'],path+'::'+symbol,**options)
        result['timing']={'elapsed_seconds':round(time.monotonic()-started,6),'scope':'Serena provider operation including boundary checks; excludes MCP transport and model time'}
        if result['status'] == 'ok':
            if path:
                blob,_=source(repo['path'],repo['revision'],path)
                result['source_reference']=source_reference(args['repo_key'],path,repo['revision'],blob)
            result['source_reference_scope']='Pinned source content only; provider invocation remains separately evidenced'
            if name=='serena.inspect' and args.get('include_imports',args.get('response_mode')!='compact'):result['report']['import_context'] = import_context(repo['path'], repo['revision'],
                args['repo_key'], path, symbol, result['report']['citations'],
                repositories=nav_profile['repos'],import_mappings=navigation_config.get('import_mappings',{}))
            if name=='serena.inspect' and args.get('include_imports',args.get('response_mode')!='compact') and not path.endswith('.py'):
                from kp_agent_tooling._impl.typescript_context import context
                result['report']['import_context'] = context(repo['path'],repo['revision'],args['repo_key'],path,navigation_config.get('typescript'))
            result['report']['source_snapshot'] = {
                key: {'revision': value['revision']} for key, value in nav_profile['repos'].items()}
            result['report']['environment_scope'] = 'configured source snapshots; installed and deployed dependencies not verified'
            references_requested = args.get(
                'include_references', args.get('response_mode') != 'overview')
            if (name == 'serena.inspect' and references_requested
                    and result.get('diagnostic', {}).get('symbol_state') == 'resolved'):
                from kp_agent_tooling._impl.service.repository_coverage import call as coverage_call
                candidate_name = symbol.rsplit('/', 1)[-1].strip()
                textual_limit = 12
                search_arguments = {
                    'repo_key': args['repo_key'], 'target_revision': repo['revision'],
                    'query': candidate_name + '(', 'mode': 'literal',
                    'path_pattern': '*', 'limit': textual_limit,
                }
                try:
                    search_result = coverage_call(
                        navigation_config, 'navigation.search_page', search_arguments)
                except (OSError, RuntimeError, subprocess.SubprocessError) as error:
                    search_result = {
                        'status': 'error', 'reason': 'textual_search_failed',
                        'message': type(error).__name__, 'results': [],
                        'search_complete': False, 'traversal_complete': False,
                        'absence_verdict': 'not-established',
                    }
                candidates = [{**row, 'evidence_kind': 'textual'}
                              for row in search_result.get('results', [])]
                next_call = None
                if search_result.get('continuation_token'):
                    next_arguments = dict(search_arguments,
                        continuation_token=search_result['continuation_token'])
                    if selection:
                        next_arguments['snapshot_id'] = selection['snapshot_id']
                    next_call = {'name': 'navigation.search_page',
                                 'arguments': next_arguments}
                result['report']['textual_candidates'] = {
                    'status': search_result.get('status', 'unavailable'),
                    'reason': search_result.get('reason'),
                    'message': search_result.get('message'),
                    'evidence_kind': 'textual',
                    'source_revision': repo['revision'],
                    'normalized_name': candidate_name,
                    'query': candidate_name + '(',
                    'patterns': ['.' + candidate_name + '(', candidate_name + '('],
                    'query_strategy': 'single_literal_superset',
                    'whitespace_scope': 'zero characters between normalized name and opening parenthesis',
                    'result_limit': textual_limit,
                    'results': candidates,
                    'traversal_complete': search_result.get('traversal_complete', False),
                    'search_complete': search_result.get('search_complete', False),
                    'coverage': search_result.get('coverage'),
                    'scan_limits': search_result.get('scan_limits'),
                    'page_budget': search_result.get('page_budget'),
                    'page_usage': search_result.get('page_usage'),
                    'exclusions': search_result.get('exclusions', []),
                    'failures': search_result.get('failures', []),
                    'pages_remaining_estimate': search_result.get('pages_remaining_estimate'),
                    'continuation_token': search_result.get('continuation_token'),
                    'next_call': next_call,
                    'absence_verdict': search_result.get('absence_verdict', 'not-established'),
                    'scope': search_result.get('scope', 'repository-wide committed source'),
                    'limitations': ['syntactic matches may be comments, strings, declarations, or unrelated names; they are not references or proven callers'],
                }
        def bind_continuations(value):
            if isinstance(value,dict):
                if value.get('operation')=='navigation.source':
                    value.setdefault('arguments',{})['repo_key']=args['repo_key']
                    if selection:value['arguments']['snapshot_id']=selection['snapshot_id']
                for child in value.values():bind_continuations(child)
            elif isinstance(value,list):
                for child in value:bind_continuations(child)
        bind_continuations(result)
        if args.get('response_mode')=='compact' and result.get('status')=='ok':
            details={'environment':result.pop('environment',{}),'provider_identity':result.get('report',{}).pop('provider_identity',{})}
            result['analysis_details']=self.delivery.deliver(details,force=True)
            result['analysis_details_scope']='Provider/environment metadata; source citations remain inline. Retrieve when environment or provider identity is material.'
        if selection:result['review_snapshot']={k:v for k,v in selection.items() if k!='repos'}
        return result

    def semantic_index_dir(self, navigation_config, repo_key):
        """Where a published semantic index for this repo is expected.

        Precedence: the active profile's `semantic_indexes[repo_key]` (published with the
        snapshot), then `semantic_index_root` in the tooling config, then the navigation
        registry's `semantic/` directory (where an operator build lands by default).
        """
        from kp_agent_tooling._impl.navigation_workspace import active
        profile = active(navigation_config)
        published = (profile.get('semantic_indexes') or {}).get(repo_key)
        if isinstance(published, dict) and published.get('dir'):
            return Path(published['dir'])
        if navigation_config.get('semantic_index_root'):
            return Path(navigation_config['semantic_index_root'])
        registry = navigation_config.get('navigation_registry_path')
        if isinstance(registry, str) and Path(registry).is_absolute():
            return Path(registry) / 'semantic'
        return None

    def _semantic_embedder(self):
        if getattr(self, '_semantic_embedder_instance', None) is None:
            from kp_agent_tooling._impl.embeddings.embedders import RealEmbedder, REAL_MODEL_WEIGHTS_DIGEST
            os.environ.setdefault('HF_HUB_OFFLINE', '1'); os.environ.setdefault('TRANSFORMERS_OFFLINE', '1')
            self._semantic_embedder_instance = RealEmbedder(model_digest=REAL_MODEL_WEIGHTS_DIGEST)
        return self._semantic_embedder_instance

    def _semantic(self, navigation_config, selection, args):
        from kp_agent_tooling._impl.navigation_workspace import active
        from kp_agent_tooling._impl.semantic_index import query_semantic_index, SemanticIndexError
        profile = active(navigation_config)
        repo = profile['repos'][args['repo_key']]
        revision = args.get('target_revision') or repo['revision']
        base = {'schema_version': 'ops.navigation-semantic.v1', 'repo_key': args['repo_key'],
                'source_revision': revision, 'evidence_kind': 'semantic_ranking',
                'absence_verdict': 'not-established', 'runtime_execution': 'not-assessed'}
        if revision != repo['revision']:
            return {**base, 'status': 'unavailable', 'reason': 'revision_not_indexed',
                    'message': 'semantic indexes exist only for published snapshot revisions',
                    'next_action': 'Omit target_revision or select the snapshot whose revision you need.'}
        readiness = profile.get('repository_readiness', {}).get(args['repo_key'], {})
        state = readiness.get('semantic')
        if state in {'building', 'unavailable', 'not_requested'}:
            return {**base, 'status': 'unavailable',
                    'reason': 'semantic_index_building' if state == 'building' else 'semantic_index_' + state,
                    'build_state': state, 'build_reason': readiness.get('semantic_reason'),
                    'published_at': profile.get('published_at'),
                    'message': ('The published refresh reports this index is building; this is not a live worker liveness check.'
                                if state == 'building' else 'No usable semantic index was published for this revision.'),
                    'next_action': 'Use navigation.search or navigation.source meanwhile; inspect tooling.identity for refresh status.'}
        index_dir = self.semantic_index_dir(navigation_config, args['repo_key'])
        if index_dir is None:
            return {**base, 'status': 'unavailable', 'reason': 'semantic_index_root_unconfigured',
                    'next_action': 'Configure navigation_registry_path or semantic_index_root, and build the index at publish (scripts/build_semantic_index.py).'}
        try:
            embedder = self._semantic_embedder()
            result = query_semantic_index(index_dir, args['repo_key'], revision, args['query'],
                                          embedder=embedder, limit=args.get('limit', 8),
                                          path_pattern=args.get('path_pattern', '*'), kinds=args.get('kinds'))
        except SemanticIndexError as error:
            return {**base, 'status': 'unavailable', 'reason': error.reason, 'message': str(error),
                    'next_action': 'Build or republish the semantic index for this revision (scripts/build_semantic_index.py); use navigation.search meanwhile.'}
        except (OSError, RuntimeError, ValueError) as error:
            return {**base, 'status': 'unavailable', 'reason': 'semantic_query_failed', 'message': type(error).__name__}
        if selection:
            result['review_snapshot'] = {k: v for k, v in selection.items() if k != 'repos'}
        return result

    def doctor(self):
        build_revision = self.server_identity['server_build_revision']
        from kp_agent_tooling._impl.navigation_workspace import active
        repos = active(self.config)['repos']
        local=[]
        for key,repo in repos.items():
            local.append({'repo_key':key,'revision':repo['revision'],'untracked':boundary(repo['path'],repo['revision'])})
        for provider in self.providers:
            if hasattr(provider, 'doctor_preflight'):
                provider.doctor_preflight()
        names=[t['name'] for t in self.tools()]
        availability = {name: {'status': 'configured', 'runtime_verification': 'not-assessed'} for name in names}
        def unavailable(name, reason, next_action):
            availability[name] = {'status': 'unavailable', 'reason': reason,
                                  'next_action': next_action, 'runtime_verification': 'not-assessed'}
        serena = self.config.get('serena')
        if serena is None:
            for name in ('serena.inspect', 'serena.find', 'serena.overview'):
                unavailable(name, 'provider_not_configured',
                            'Configure an existing Serena runtime and a reviewed read-only project through workspace setup.')
        elif any(not isinstance(serena.get(key), str) or not Path(serena[key]).is_file()
                 for key in ('command', 'python')):
            for name in ('serena.inspect', 'serena.find', 'serena.overview'):
                unavailable(name, 'provider_executable_unavailable',
                            'Restore the configured Serena command and Python executable, then review the setup plan.')
        semantic_missing = []
        for key, repo in repos.items():
            index_dir = self.semantic_index_dir(self.config, key)
            if index_dir is None:
                semantic_missing.append(key)
            elif not (index_dir / f"{key}-{repo['revision']}.semantic.json").is_file():
                semantic_missing.append(key)
        if semantic_missing:
            reason = ('semantic_index_root_unconfigured' if all(
                self.semantic_index_dir(self.config, key) is None for key in repos)
                else 'semantic_index_unavailable')
            unavailable('navigation.semantic', reason,
                        'Build and publish a semantic index for the pinned repository revision; use navigation.search meanwhile.')
            availability['navigation.semantic']['repo_keys'] = semantic_missing
        registry = self.config.get('navigation_registry_path')
        if 'navigation.search_page' in names:
            registry_path = Path(registry) if isinstance(registry, str) else None
            if (registry_path is None or not registry_path.is_absolute() or
                    registry_path.resolve() != registry_path or not registry_path.is_dir()):
                unavailable('navigation.search_page', 'navigation_registry_unavailable',
                            'Configure an existing absolute persistent navigation registry directory.')
            else:
                mode = registry_path.stat()
                if (mode.st_uid != os.getuid() or mode.st_mode & 0o300 != 0o300 or
                        not os.access(registry_path, os.W_OK | os.X_OK)):
                    unavailable('navigation.search_page', 'navigation_registry_not_writable',
                                'Restore owner write and traversal access to the configured navigation registry.')
        local_names = ({tool['name'] for tool in self.knowledge_provider.tools()}
                       if self.knowledge_provider is not None else set())
        if 'knowledge.symbol' in local_names and 'knowledge.symbol' in names:
            # The local catalog advertises a configured SCIP reader. Index and
            # invocation readiness still require an exact knowledge.symbol call.
            pass
        else:
            # A provider with a configured remote catalog replaces this outcome below.
            unavailable('knowledge.symbol', 'gateway_not_configured',
                        'Configure an existing SCIP-capable gateway and indexed repository before using knowledge.symbol.')
        gap = self.unavailable_tools()
        for row in gap:
            unavailable(row['name'], row['reason'], row['next_action'])
        typescript = self.config.get('typescript')
        if typescript is None:
            unavailable('navigation.imports.typescript', 'typescript_runtime_not_configured',
                        'Configure an existing Node runtime and pinned compiler through workspace setup.')
        elif any(not isinstance(typescript.get(key), str) or not Path(typescript[key]).is_file()
                 for key in ('node', 'module')):
            unavailable('navigation.imports.typescript', 'typescript_runtime_unavailable',
                        'Restore the configured Node executable and compiler file, then review the setup plan.')
        contributed, degraded = {}, False
        for provider in self.providers:
            if hasattr(provider, 'doctor'):
                fields, failed = provider.doctor(names=names, availability=availability,
                                                 unavailable=unavailable, local_names=local_names)
                contributed.update(fields)
                degraded = degraded or failed
        requested = set(names) | set(self.enabled_tools or ())
        configured_unavailable = any(row['status'] == 'unavailable' and name in requested
                                     for name, row in availability.items())
        return {'status':'partial' if degraded or configured_unavailable else 'ready',**contributed,
                **({'unavailable_tools':gap} if gap else {}),
                'tools':names,'tool_availability':availability,'sources':local,'evidence_revision':self.config.get('evidence_revision'),
                'server_build_revision':build_revision,'config_sha256':self.config_sha256,
                'limits':['read-only; no dispatch authority','doctor does not prove language-server invocation','no automatic reindex or rollout']}
