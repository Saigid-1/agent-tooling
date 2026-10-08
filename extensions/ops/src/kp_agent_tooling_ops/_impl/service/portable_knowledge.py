"""Host-configured, in-process knowledge composition; no legacy HTTP gateway.

Without a ``navigation_profile`` the operator's catalog (``catalog_path``) is the whole
catalog, exactly as before. Under a profile the operator's catalog is an overlay: the
generation fields of each served repository come from the active profile's published
catalog, admitted only through ``navigation_workspace.active``, and the operator keeps
its own fields and the served set (``merge_generation``). A generation that cannot be
served is refused as a knowledge result; nothing is answered from either catalog.
"""
import hashlib
import json
from pathlib import Path
from threading import RLock
import jsonschema

from kp_agent_tooling._impl.service.desk_memory_runtime import private_json
from .knowledge import KnowledgeService, KnowledgeRequestError, _LIMITATIONS, _load_config
from .knowledge_schemas import knowledge_tools
from .document_corpus import DocumentCorpus
from kp_agent_tooling._impl.embeddings.embedders import RealEmbedder

# Generation fields of a served repository row (P1); with the served repositories'
# ``platforms`` and ``scip_indexes`` entries they are the generation's. Every other row
# field (``capabilities`` included), the ``navigation`` section and which
# repositories are served stay the operator's (P2).
GENERATION_FIELDS = ('ref', 'path')

# The refusal reason words (P3), one per case.
PROFILE_REFUSED = 'navigation_profile_refused'              # active() refused; its own words pass through
REPOSITORY_NOT_PUBLISHED = 'served_repository_not_published'  # a served repo is absent from the published catalog
ENTRY_NOT_PUBLISHED = 'published_entry_missing'              # a platforms/scip_indexes entry a served repo needs
MERGE_INVALID = 'merged_catalog_invalid'                     # the merge fails the catalog's own validation

_NEXT_ACTION = {
    PROFILE_REFUSED: 'Inspect tooling.identity and the refresh status; the published profile or its catalog was not admitted.',
    REPOSITORY_NOT_PUBLISHED: 'Run refresh for every repository the operator catalog serves; do not edit the operator catalog to pin a revision.',
    ENTRY_NOT_PUBLISHED: 'Run refresh so the generation publishes the platform and SCIP entries the operator catalog declares for its served repositories.',
    MERGE_INVALID: 'Reconcile the published generation with the operator overlay: the merged catalog failed validation (for example a published index entry without a valid digest).',
}
# active() reads each of the profile, the catalog and the status file within this budget.
_CATALOG_BUDGET = 131072


class GenerationRefused(Exception):
    """The active generation cannot be served (P3): never mix, never fall back."""

    def __init__(self, refusal, reason, detail=None):
        super().__init__(reason)
        self.refusal, self.reason, self.detail = refusal, reason, detail
        self.profile = None  # the admitted profile whose generation was refused, if any

    def result(self, name, args):
        error = {'code': 'knowledge_unavailable', 'refusal': self.refusal, 'reason': self.reason,
                 'message': 'The active navigation generation cannot be served; nothing is answered '
                            'from the published or the operator catalog.',
                 'next_action': _NEXT_ACTION[self.refusal]}
        if self.detail:
            error['detail'] = self.detail[:240]
        report = {'schema_version': 'ops.knowledge.v1', 'operation': name.removeprefix('knowledge.'),
                  'repo_key': args.get('repo_key') if isinstance(args, dict) else None,
                  'source_revision': None, 'status': 'error', 'reason': self.reason,
                  'data': {'error': error}, 'omitted': 0, 'limitations': list(_LIMITATIONS)}
        if self.profile is not None:
            report['refused_navigation_profile'] = _profile_identity(self.profile)
        return report


def _profile_identity(profile):
    return {k: profile.get(k) for k in ('profile', 'published_at', 'profile_sha256')}


def _active_refusal(error):
    """active()'s own refusal passes through as the reason; it is classified, never re-checked."""
    if isinstance(error, ValueError):
        reason = str(error)
    elif isinstance(error, OSError):
        reason = f'{type(error).__name__}: {error.strerror or "navigation profile or catalog unreadable"}'
    else:
        reason = f'{type(error).__name__}: malformed navigation profile or catalog ({error})'
    return GenerationRefused(PROFILE_REFUSED, reason[:240])


def merge_generation(operator, published):
    """The one place generation fields (P1) and operator fields (P2) meet.

    ``operator`` is the validated operator catalog; it names the served repositories and
    keeps every operator field. ``published`` is the catalog ``active()`` admitted. Each
    served repository takes ``ref`` and ``path`` from ``published``;
    ``platforms`` and ``scip_indexes`` are the published entries for served repositories
    only. A published repository, tenant or corpus scope never widens what is served.
    The platform and SCIP entries a served repository needs are the ones the operator
    catalog declares (platform anchors and members, index keys); their values come
    from the generation.
    """
    served = operator['repositories']
    rows = published.get('repositories') if isinstance(published, dict) else None
    if not isinstance(rows, dict):
        raise GenerationRefused(REPOSITORY_NOT_PUBLISHED, REPOSITORY_NOT_PUBLISHED,
                                'the published catalog lists no repositories')
    absent = sorted(set(served) - set(rows))
    if absent:
        raise GenerationRefused(REPOSITORY_NOT_PUBLISHED, REPOSITORY_NOT_PUBLISHED,
                                'served repositories absent from the published catalog: ' + ', '.join(absent))
    repositories = {}
    for key, row in served.items():
        source = rows[key]
        missing = [field for field in GENERATION_FIELDS if not isinstance(source, dict) or field not in source]
        if missing:
            raise GenerationRefused(ENTRY_NOT_PUBLISHED, ENTRY_NOT_PUBLISHED,
                                    f'published repository {key!r} lacks ' + ', '.join(missing))
        repositories[key] = {**row, **{field: source[field] for field in GENERATION_FIELDS}}
    published_platforms = published.get('platforms')
    platforms = {anchor: manifest for anchor, manifest in
                 (published_platforms.items() if isinstance(published_platforms, dict) else ())
                 if anchor in served and isinstance(manifest, dict)
                 and isinstance(manifest.get('sources'), dict) and set(manifest['sources']) <= set(served)}
    published_indexes = published.get('scip_indexes')
    indexes = ({key: entries for key, entries in published_indexes.items() if key in served}
               if isinstance(published_indexes, dict) else {})
    members = {key for manifest in platforms.values() for key in manifest['sources']}
    needed = ([f'platforms.{anchor}' for anchor in sorted(set(operator.get('platforms', {})) - set(platforms))] +
              [f'platform membership of {key}' for key in sorted(
                  {key for manifest in operator.get('platforms', {}).values() for key in manifest['sources']} - members)] +
              [f'scip_indexes.{key}' for key in sorted(set(operator.get('scip_indexes', {})) - set(indexes))])
    if needed:
        raise GenerationRefused(ENTRY_NOT_PUBLISHED, ENTRY_NOT_PUBLISHED,
                                'the published catalog lacks (or widens beyond the served set) ' + ', '.join(needed))
    merged = {**operator, 'repositories': repositories, 'platforms': platforms, 'scip_indexes': indexes}
    try:
        return _load_config(merged)
    except KnowledgeRequestError as error:
        raise GenerationRefused(MERGE_INVALID, MERGE_INVALID, str(error)) from error


class PortableKnowledgeProvider:
    def __init__(self, config_path, *, embedder=None, navigation=None):
        self.config_path = config_path
        self.config = private_json(config_path)
        allowed = {'schema_version','tenant_id','catalog_path','documents','embedding','reference_generation','lifecycle_path'}
        if (self.config.get('schema_version') != 'agent-tooling.knowledge-runtime.v1'
                or set(self.config) - allowed or not {'tenant_id','catalog_path','documents','embedding','lifecycle_path'} <= self.config.keys()):
            raise ValueError('invalid portable knowledge configuration')
        catalog = private_json(self.config['catalog_path'])
        navigation_section = catalog.get('navigation', {})
        if navigation_section and navigation_section.get('provider') not in {'serena','published_scip'}:
            raise ValueError('portable knowledge requires Serena or published SCIP; legacy AST subprocess is not packaged')
        self._embedder = embedder
        self._reader = None
        self._lock = RLock()
        reference_store = None
        if self.config.get('reference_generation'):
            from ..code_references.portable_generation import load_generation
            reference_store = load_generation(**self.config['reference_generation'])
        from .knowledge_lifecycle import LifecycleLedger
        lifecycle = LifecycleLedger(self.config['lifecycle_path'])
        lifecycle.entries()  # Refuse missing state; never create an empty replacement.
        self._reference_store, self._lifecycle = reference_store, lifecycle
        self.service = KnowledgeService(self.config['catalog_path'], self.reader,
                                        reference_store=reference_store, lifecycle=lifecycle)
        # K1: the tooling configuration whose navigation_profile the knowledge tools follow.
        # Without one, nothing below runs and the operator catalog is the catalog (P4).
        self._navigation = navigation if isinstance(navigation, dict) and navigation.get('navigation_profile') else None
        self._published = None  # (knowledge_config_sha256, admitted catalog)
        self._built = None      # (knowledge_config_sha256, merged catalog), the key of self.service
        if self._navigation is not None:
            try:
                self._follow()
            except (GenerationRefused, KnowledgeRequestError):
                pass  # Reported per call as a knowledge result (P3), never at construction.

    def reader(self):
        with self._lock:
            if self._reader is None:
                if self._embedder is None:
                    self._embedder = RealEmbedder(**self.config['embedding'])
                self._reader = DocumentCorpus(**self.config['documents'], embedder=self._embedder)
            return self._reader

    def _admitted_catalog(self, profile):
        """The bytes of the catalog active() admitted: read once per generation, bound to its digest."""
        path, digest = profile.get('knowledge_config'), profile.get('knowledge_config_sha256')
        if not path:
            raise GenerationRefused(REPOSITORY_NOT_PUBLISHED, REPOSITORY_NOT_PUBLISHED,
                                    'the active navigation profile publishes no knowledge catalog')
        if self._published is not None and self._published[0] == digest:
            return self._published[1]
        try:
            with Path(path).open('rb') as stream:
                raw = stream.read(_CATALOG_BUDGET + 1)
        except OSError as error:
            raise _active_refusal(error) from error
        if len(raw) > _CATALOG_BUDGET or hashlib.sha256(raw).hexdigest() != digest:
            raise GenerationRefused(PROFILE_REFUSED, 'navigation catalog changed after admission')
        catalog = json.loads(raw)
        self._published = (digest, catalog)
        return catalog

    def _follow(self):
        """Admit the active profile and return (service, profile).

        The rebuild trigger (P5): the service is rebuilt when the admitted catalog's
        ``knowledge_config_sha256`` differs from the one it was built from, or when the
        operator overlay changed (today's per-call operator read); otherwise it is reused.
        No corpus or index is opened here: the corpus reader and reference store are the
        provider's own, created once.
        """
        from kp_agent_tooling._impl import navigation_workspace
        try:
            profile = navigation_workspace.active(self._navigation)
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
            raise _active_refusal(error) from error
        operator = _load_config(self.config['catalog_path'])  # today's per-call read and refusals
        with self._lock:
            try:
                published = self._admitted_catalog(profile)
                merged = merge_generation(operator, published)
            except GenerationRefused as refusal:
                if refusal.refusal != PROFILE_REFUSED:
                    refusal.profile = profile
                raise
            key = (profile.get('knowledge_config_sha256'), merged)
            if self._built is None or self._built != key:
                self.service = KnowledgeService(merged, self.reader, reference_store=self._reference_store,
                                                lifecycle=self._lifecycle)
                self._built = key
            return self.service, profile

    def tools(self):
        if self._navigation is not None:
            try:
                return knowledge_tools(self._follow()[0])
            except (GenerationRefused, KnowledgeRequestError):
                pass  # A listing only; the call names the refusal.
        return knowledge_tools(self.service)

    def call(self, name, args):
        if private_json(self.config_path) != self.config:
            raise ValueError('knowledge runtime configuration changed; reload required')
        if self._navigation is not None:
            return self._call_followed(name, args)
        descriptor = next((t for t in self.tools() if t['name'] == name), None)
        if descriptor is None:
            raise ValueError('unsupported portable knowledge operation')
        jsonschema.validate(args, descriptor['inputSchema'])
        return self.service.execute_for_tenant(name.removeprefix('knowledge.'), args, self.config['tenant_id'])

    def _call_followed(self, name, args):
        try:
            service, profile = self._follow()
        except GenerationRefused as refusal:
            return refusal.result(name, args)
        descriptor = next((t for t in knowledge_tools(service) if t['name'] == name), None)
        if descriptor is None:
            raise ValueError('unsupported portable knowledge operation')
        jsonschema.validate(args, descriptor['inputSchema'])
        result = service.execute_for_tenant(name.removeprefix('knowledge.'), args, self.config['tenant_id'])
        return dict(result, navigation_profile=_profile_identity(profile))
