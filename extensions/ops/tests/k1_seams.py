"""K1 seams: every interface name the K1 tests assume, in one module.

The K1 tests (`test_k1_published_knowledge_catalog.py`) were written blind to the
FEATURE arm. Each name below is the TEST arm's reading of the order
(`docs/work/orders/K1-published-knowledge-catalog.md`) or of the code at the base
the order was cut from. The meet reconciles THIS module against the FEATURE arm's
implementation; the assertions in the test module stay as written.

Groups:
- the server surface (how a test reaches the OPS knowledge tools);
- the tooling configuration keys;
- how a navigation profile is published in the world;
- the stub embedder and the corpus-open counter;
- the result fields (answered revision, profile identity, answer rows, and the
  fields of the base reports the assertions read);
- the refusal shape and the reason words (P3), one per case.
"""
import hashlib
import json
import os
from pathlib import Path
import tempfile


# -- the server surface -------------------------------------------------------------

def build_server(tooling_config_path):
    """The served composition: the core server with the installed OPS provider.

    The OPS extension is discovered through the `kp_agent_tooling.tools` entry
    point, so `knowledge.*` reaches whatever `OpsToolProvider.knowledge_provider`
    supplies. No transport is involved; a raised exception here is what a
    transport would surface as a tool error.
    """
    from kp_agent_tooling._impl.service.agent_tooling import AgentTooling
    return AgentTooling(tooling_config_path)


def call(server, name, arguments):
    return server.call(name, dict(arguments))


def knowledge_service(server):
    """The knowledge service object the OPS provider currently answers from.

    P5: on the same generation the provider reuses it; only a generation change
    rebuilds it. Read as the `service` attribute of the knowledge provider the
    server holds (the attribute `PortableKnowledgeProvider` has at base).
    """
    return server.knowledge_provider.service


def advertised_repositories(server, tool='knowledge.capabilities'):
    """The repo keys a knowledge tool's listed input schema accepts (the served set)."""
    descriptor = next(t for t in server.tools(include_gateway=False) if t['name'] == tool)
    return set(descriptor['inputSchema']['properties']['repo_key']['enum'])


# -- tooling configuration keys --------------------------------------------------------

TOOLING_SCHEMA = 'ops.agent-tooling.v1'
PROFILE_CONFIG_KEY = 'navigation_profile'           # the core's key for the active profile
PORTABLE_CONFIG_KEY = 'portable_knowledge_config'   # the OPS key for knowledge-runtime.json
RUNTIME_SCHEMA = 'agent-tooling.knowledge-runtime.v1'


# -- publication ------------------------------------------------------------------------

def write_private_json(path, value):
    """Owner-private (0600) JSON written by atomic replace; returns the sha256 of its bytes."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = (json.dumps(value, indent=2, sort_keys=True) + '\n').encode()
    descriptor, temporary = tempfile.mkstemp(prefix='.k1-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(raw)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return hashlib.sha256(raw).hexdigest()


def publish_profile(publication, profile):
    """Publish a generation: atomically replace the active profile file.

    Refresh writes `generations/<gen>/knowledge.json` and `profile.json`, then
    replaces the one publication path that the tooling configuration's
    `navigation_profile` names. Returns the published profile's sha256, the value
    `active()` reports as `profile_sha256`.
    """
    return write_private_json(publication, profile)


# -- the stub embedder ------------------------------------------------------------------

def install_stub_embedder(monkeypatch, factory):
    """Replace the production embedder so no model is loaded and nothing is fetched.

    `PortableKnowledgeProvider.reader` builds `RealEmbedder(**runtime['embedding'])`
    lazily, through the name the OPS module bound at import. Only that name is
    replaced: the class itself stays, because `isinstance(..., RealEmbedder)` checks
    elsewhere (the document embedder adapter) must keep their meaning.
    """
    import kp_agent_tooling_ops._impl.service.portable_knowledge as portable
    monkeypatch.setattr(portable, 'RealEmbedder', factory)


# -- result fields ------------------------------------------------------------------------

PROFILE_FIELD = 'navigation_profile'   # F1: the field the local path adds in agent_tooling.py
PROFILE_SHA_FIELD = 'profile_sha256'

ANSWER_STATUSES = frozenset({'ok', 'partial', 'no_results', 'review_required', 'corpus_empty'})
REFUSAL_STATUSES = frozenset({'unavailable', 'error'})


def profile_sha(result):
    profile = result.get(PROFILE_FIELD) if isinstance(result, dict) else None
    return profile.get(PROFILE_SHA_FIELD) if isinstance(profile, dict) else None


def answered_revisions(result):
    """Revisions a `knowledge.symbol` result actually answered from.

    The indexed scope and every resolved definition name the revision whose SCIP
    index and source answered. `source_revision` alone is only the request echo.
    """
    data = result.get('data') if isinstance(result, dict) else None
    if not isinstance(data, dict):
        return set()
    revisions = {row.get('revision') for row in data.get('indexed_scope') or []}
    for row in data.get('results') or []:
        for definition in (row.get('resolution') or {}).get('definitions') or []:
            revisions.add(definition.get('revision'))
    return revisions - {None}


def status(result):
    return result.get('status') if isinstance(result, dict) else None


def _data(result):
    data = result.get('data') if isinstance(result, dict) else None
    return data if isinstance(data, dict) else {}


# Fields of reports the base code builds (scip_entry.symbol_report, the knowledge
# service's capability inventory and retrieval report, knowledge_context). FEATURE
# does not own that code; the reads live here so a FEATURE that wraps results is
# reconciled in this module.

def symbol_gaps(result):
    return list(_data(result).get('gaps') or [])


def symbol_results(result):
    return list(_data(result).get('results') or [])


def error_of(result):
    return _data(result).get('error')


def capability_map(result):
    """`knowledge.capabilities`: {capability id: manifest path}."""
    return {row['capability_id']: row['manifest_path'] for row in _data(result).get('capabilities') or []}


def context_navigation(result):
    """`knowledge.context`: (navigation status, the entry symbol it resolved or None)."""
    navigation = _data(result).get('navigation') or {}
    return navigation.get('status'), (navigation.get('report') or {}).get('entry_symbol')


def declared_artifacts(result):
    """`knowledge.retrieve`: how many maintained, owned artifacts the catalog row declares."""
    return _data(result).get('declared_artifacts')


def retrieval_scopes(result):
    """`knowledge.retrieve`: (the report's corpus scopes, each row's scope, each row's text)."""
    data = _data(result)
    rows = data.get('results') or []
    return data.get('repo_keys'), [row.get('repo_key') for row in rows], [row.get('text') for row in rows]


def count_corpus_opens(monkeypatch):
    """A list that grows by one each time the operator's document corpus is opened."""
    from kp_agent_tooling_ops._impl.service.document_corpus import DocumentCorpus
    opened = []
    original = DocumentCorpus.__init__

    def counting(self, *args, **kwargs):
        opened.append(1)
        return original(self, *args, **kwargs)
    monkeypatch.setattr(DocumentCorpus, '__init__', counting)
    return opened


def answer_rows(result):
    """Every answer-bearing collection a knowledge result can carry, flattened."""
    data = result.get('data') if isinstance(result, dict) else None
    if not isinstance(data, dict):
        return []
    rows = []
    for key in ('results', 'capabilities', 'indexed_scope', 'guidance', 'references', 'sources'):
        value = data.get(key)
        if isinstance(value, list):
            rows.extend(value)
        elif isinstance(value, dict) and isinstance(value.get('references'), list):
            rows.extend(value['references'])
    return rows


def refusal_reason(result):
    """The reason word of a P3 refusal, or None when `result` is not a refusal.

    The order: the existing knowledge result status vocabulary, a status plus a
    reason word (the `knowledge_unavailable` family). Read the reason at the top
    level, then inside `data.error`.
    """
    if not isinstance(result, dict) or result.get('status') not in REFUSAL_STATUSES:
        return None
    if isinstance(result.get('reason'), str):
        return result['reason']
    error = (result.get('data') or {}).get('error') if isinstance(result.get('data'), dict) else None
    if isinstance(error, dict):
        for key in ('reason', 'detail'):
            if isinstance(error.get(key), str):
                return error[key]
    return None


def answered(invoke):
    """(True, result) when the call answered; (False, why) when it was refused or raised."""
    try:
        result = invoke()
    except Exception as error:  # a refusal raised by any layer counts as not answered
        return False, f'{type(error).__name__}: {error}'
    if (isinstance(result, dict) and result.get('status') in ANSWER_STATUSES
            and (answer_rows(result) or result.get('status') == 'ok')):
        return True, result
    return False, result


def tenant_refused(invoke):
    """A query outside the served tenant set is refused.

    At base the tenant boundary raises `PermissionError('platform tenant boundary')`;
    a P3-shaped refusal result with no answer rows is accepted as well.
    """
    try:
        result = invoke()
    except PermissionError:
        return True
    return (isinstance(result, dict) and result.get('status') not in ANSWER_STATUSES
            and not answer_rows(result))


# -- refusal reason words (P3) ----------------------------------------------------------------
# active()'s own refusals pass through as the reason (P1's "no second reader"); the
# other words are FEATURE's to name, one per case. The guesses below are the TEST
# arm's placeholders for the meet to reconcile.

REASON = {
    # navigation_workspace.active() refusals, passed through verbatim
    'identity': 'navigation catalog identity mismatch',
    'membership': 'catalog repository membership mismatch',
    'source_differs': 'catalog source differs from active profile',
    # FEATURE-named words (reconciled at the K1 meet to FEATURE's refusal words)
    'served_unpublished': 'served_repository_not_published',
    'generation_entry_missing': 'published_entry_missing',
}


def reason_matches(reason, case):
    return reason == REASON[case]
