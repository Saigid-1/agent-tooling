"""Read-only Serena symbol navigation, validated against committed OPS source.

The provider is configured by operators, never by agent request arguments.
Serena owns language resolution; OPS owns source identity and evidence semantics.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
from pathlib import Path
import subprocess
import sys

def _tool_root():
    path = Path(__file__).resolve()
    # The portable wheel relocates this module under kp_agent_tooling._impl.
    return path.parents[3] if path.parents[1].name == '_impl' else path.parents[2]


if __name__ == '__main__':
    sys.path.insert(0, str(_tool_root()))

from kp_agent_tooling._impl.source_citations import source
from kp_agent_tooling._impl.navigation_environment import snapshot, changes

LIMITATIONS = [
    "Language-server symbols are static evidence, not execution or architectural conformance.",
    "Dynamic injection and cross-runtime calls may be unresolved; absence is not established.",
    "Untracked configuration is disclosed; endpoint checks are not an atomic workspace snapshot.",
]


DEFAULT_MAX_MATCHES = 25
MAX_MATCHES = 100


class ProviderToolError(ValueError):
    def __init__(self, payload):
        self.payload = payload
        super().__init__(payload['message'])


def provider_failure(detail):
    """Only transmit recognized diagnostics, never arbitrary provider logs/source."""
    match = re.search(r'Matched (\d{1,9})>max_matches=(\d{1,9}) symbols', detail)
    if match:
        count, limit = map(int, match.groups())
        return dict(code='match_limit_exceeded', matched_count=count, requested_limit=limit,
                    message=f'Serena found {count} symbols, exceeding max_matches={limit}.',
                    next_action=f'Narrow path to a file or directory, or raise max_matches (maximum {MAX_MATCHES}).')
    return dict(code='provider_tool_error',
                message='Serena rejected the tool request; its error has no recognized safe diagnostic format.',
                next_action='Use revision-pinned source search and ask an operator to inspect provider diagnostics.')


def failure_reporting(error):
    """Separate a surfaced boundary error from unavailable provider diagnostics."""
    if isinstance(error, ProviderToolError):
        return dict(error.payload, error_details_status=(
            'reported' if error.payload['code'] == 'match_limit_exceeded' else 'unavailable'),
            underlying_cause=error.payload['code'], exit_code=None)
    from kp_agent_tooling._impl.service.navigation_process import NavigationSubprocessError
    missing = isinstance(error, NavigationSubprocessError) or str(error) == 'navigation subprocess failed'
    return {
        'error_details_status': 'unavailable' if missing or not str(error) else 'reported',
        'underlying_cause': 'not-established',
        'exit_code': getattr(error, 'returncode', None),
        'message': ('The navigation subprocess failed. Its stderr was not captured by this adapter; '
                    'the underlying reason is unavailable. This does not mean the symbol is absent.'
                    if missing else str(error)[:300] or 'No error detail was supplied by the failing operation.'),
        'next_action': ('An operator must inspect the provider execution/configuration with diagnostics enabled. '
                        'Use revision-pinned text search and source reads to continue; do not treat this failure as no match.'),
    }


def _git(repo, *args):
    return subprocess.check_output(['git', *args], cwd=repo, stderr=subprocess.PIPE, timeout=15)


def boundary(repo, revision):
    if _git(repo, 'rev-parse', 'HEAD').decode().strip() != revision:
        raise ValueError('target_checkout_mismatch')
    if _git(repo, 'diff', revision, '--') or _git(repo, 'diff', '--cached', revision, '--'):
        raise ValueError('tracked_worktree_divergence')
    return _git(repo, 'ls-files', '--others', '--exclude-standard', '-z').decode().split('\0')[:-1]


def normalize(repo, revision, path, rows, *, require_body=True, compact=False):
    blob, text = source(repo, revision, path)
    disk = Path(repo) / path
    if disk.resolve() != Path(repo).resolve() / path or disk.read_bytes() != text.encode():
        raise ValueError('source_worktree_divergence')
    if not isinstance(rows, list) or len(rows) > 12:
        raise ValueError('incomplete_or_invalid_provider_result')
    lines = text.splitlines()
    citations = []
    def add_row(row, *, child=False):
        if len(citations) >= 12:
            raise ValueError('incomplete_or_invalid_provider_result')
        if row['relative_path'] != path:
            raise ValueError('unexpected_source_path')
        start, end = row['body_location']['start_line'], row['body_location']['end_line']
        if type(start) is not int or type(end) is not int or not 0 <= start <= end < len(lines):
            raise ValueError('invalid_source_coordinates')
        excerpt = '\n'.join(lines[start:end + 1])
        # Serena removes indentation from the first body line only.
        expected = '\n'.join([lines[start].lstrip(), *lines[start + 1:end + 1]])
        if require_body and row['body'] not in (excerpt, expected):
            raise ValueError('provider_source_mismatch')
        citation = dict(path=path, revision=revision, blob_sha=blob,
            name_path=row['name_path'], start_line=start + 1, end_line=end + 1,
            excerpt_sha256=hashlib.sha256(excerpt.encode()).hexdigest(),
            byte_offset=len(''.join(text.splitlines(keepends=True)[:start]).encode()),
            byte_length=len(''.join(text.splitlines(keepends=True)[start:end + 1]).encode()),
            scope='committed-source', claim_semantics='not-assessed')
        # `compact` trims provider/environment metadata, never a body the caller asked for.
        if require_body:
            citation['excerpt'] = excerpt
        else:
            citation['retrieval'] = dict(operation='navigation.source', arguments=dict(
                path=path, target_revision=revision, start_line=start + 1,
                line_count=min(200, end - start + 1)),
                remaining_lines=max(0, end - start + 1 - 200))
        if child:
            citation['relation'] = 'descendant'
        citations.append(citation)
        children = row.get('children', {})
        if isinstance(children, dict):
            for nested_rows in children.values():
                for nested in nested_rows:
                    add_row(dict(nested, relative_path=path,
                        name_path=row['name_path'] + '/' + nested['name']), child=True)
        elif isinstance(children, list):
            for nested in children:
                add_row(dict(nested, relative_path=path), child=True)
    for row in rows:
        add_row(row)
    return citations


def normalize_references(repo, revision, references, target, limit=12, *, compact=False):
    """One inbound reference hop. Imports and references are not asserted calls."""
    if not isinstance(references, dict):
        raise ValueError('incomplete_reference_response')
    edges = []
    for path, kinds in sorted(references.items()):
        if not isinstance(kinds, dict):
            raise ValueError('invalid_reference_groups')
        for kind, rows in sorted(kinds.items()):
            if not isinstance(rows, list):
                raise ValueError('invalid_reference_rows')
            for row in rows:
                blob, text = source(repo, revision, path)
                disk = Path(repo) / path
                if disk.resolve() != Path(repo).resolve() / path or disk.read_bytes() != text.encode():
                    raise ValueError('reference_worktree_divergence')
                lines = text.splitlines()
                snippet = row['content_around_reference']
                # Serena's JSON field contains escaped newlines between numbered lines.
                chunks = snippet.split('\\n') if '\\n' in snippet else snippet.splitlines()
                selected = []
                for chunk in chunks:
                    match = re.fullmatch(r'(?:\.\.\.|  >)\s*(\d+):(.*)', chunk)
                    if match is None:
                        raise ValueError('invalid_reference_snippet')
                    line = int(match[1])
                    # Some servers include the empty line after a terminal newline
                    # as surrounding context. It is not a reference location.
                    if line == len(lines) and text.endswith('\n') and match[2] == '' and chunk.startswith('...'):
                        continue
                    if not 0 <= line < len(lines) or lines[line] != match[2]:
                        raise ValueError('reference_source_mismatch')
                    if chunk.startswith('  >'):
                        selected.append(line)
                if not selected:
                    raise ValueError('reference_location_missing')
                for line in selected:
                    excerpt = lines[line]
                    citation = dict(
                            path=path, revision=revision, blob_sha=blob,
                            start_line=line + 1, end_line=line + 1,
                            excerpt_sha256=hashlib.sha256(excerpt.encode()).hexdigest())
                    # A reference site is one line; carrying it costs less than the
                    # continuation that would fetch it.
                    citation['excerpt'] = excerpt
                    edges.append(dict(kind='references', source=path + '::' + row['name_path'],
                        target=target, symbol_kind=kind, evidence_kind='language_server',
                        runtime_reachability='not-assessed', citation=citation))
    return dict(edges=edges[:limit], omitted=max(0, len(edges)-limit), depth=1,
        direction='inbound', completeness='not-established',
        gaps=['Dynamic dispatch and cross-runtime calls require independent binding evidence.'])


async def query(command, repo, path, symbol, options=None):
    # Optional dependency stays in the configured provider interpreter.
    from datetime import timedelta
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    import os
    options = options or {}
    params = StdioServerParameters(command=command, args=[
        'start-mcp-server', '--project', str(repo), '--context', 'ide', '--mode', 'planning',
        '--language-backend', 'LSP', '--agent-interface', 'tools',
        '--enable-web-dashboard', 'false', '--open-web-dashboard', 'false',
        '--enable-gui-log-window', 'false'], env=dict(os.environ), cwd=str(repo))
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=120)) as session:
            initialized = await session.initialize()
            if options.get('operation') == 'overview':
                result = await session.call_tool('get_symbols_overview', dict(
                    relative_path=path, depth=options.get('depth', 0),
                    max_answer_chars=options.get('max_answer_chars', 24000)))
            else:
                result = await session.call_tool('find_symbol', dict(
                    name_path_pattern=symbol, relative_path=path,
                    include_body=options.get('include_body', True),
                    include_info=options.get('include_info', False),
                    depth=options.get('depth', 0), max_matches=options.get('max_matches', DEFAULT_MAX_MATCHES),
                    max_answer_chars=options.get('max_answer_chars', 24000)))
            if result.isError:
                detail = ' '.join(c.text for c in result.content if c.type == 'text')
                return dict(provider_error=provider_failure(detail))
            structured = result.structuredContent
            raw = structured.get('result') if isinstance(structured, dict) else None
            if raw is None:
                raw = ''.join(c.text for c in result.content if c.type == 'text')
            try:
                rows = json.loads(raw) if isinstance(raw, str) else raw
            except (TypeError, ValueError):
                rows = raw
            references = {'state': 'not_requested'}
            if options.get('operation') != 'overview' and options.get('include_references', True) and isinstance(rows, list) and len(rows) == 1:
                response = await session.call_tool('find_referencing_symbols', dict(
                    name_path=rows[0]['name_path'], relative_path=path, max_answer_chars=24000))
                if response.isError:
                    references = {'state': 'provider_error'}
                else:
                    data = response.structuredContent
                    value = data.get('result') if isinstance(data, dict) else None
                    if value is None:
                        value = ''.join(c.text for c in response.content if c.type == 'text')
                    try:
                        references = json.loads(value) if isinstance(value, str) else value
                    except ValueError:
                        references = {'state': 'truncated'}
            elif options.get('operation') != 'overview' and options.get('include_references', True):
                references = {'state': 'requires_unique_symbol'}
            return dict(rows=rows, server=initialized.serverInfo.model_dump(mode='json'), references=references)


class SerenaNavigationProvider:
    def __init__(self, command, python=sys.executable):
        self.command, self.python = str(command), str(python)

    def _discovery(self, repository, revision, path, symbol, options):
        """Bounded read-only discovery; every returned location is pinned to source."""
        import yaml
        from kp_agent_tooling._impl.service.navigation_process import NavigationProcessRunner
        if yaml.safe_load((Path(repository) / '.serena/project.yml').read_text()).get('read_only') is not True:
            raise ValueError('read_only_project_required')
        if path:
            source(repository, revision, path)
        before, environment_before = boundary(repository, revision), snapshot(repository)
        runner = NavigationProcessRunner(_tool_root(), timeout=150)
        query_python = sys.executable if Path(__file__).resolve().parents[1].name == '_impl' else self.python
        raw = runner.run([query_python, str(Path(__file__).resolve()), self.command,
            str(repository), path, symbol, json.dumps(options)], cwd=repository,
            max_bytes=65536, pythonpath=Path(__file__).resolve().parents[1].name != '_impl')
        payload = json.loads(raw)
        after, environment_after = boundary(repository, revision), snapshot(repository)
        if before != after or changes(environment_before, environment_after):
            raise ValueError('workspace_changed_during_query')
        if 'provider_error' in payload:
            raise ProviderToolError(payload['provider_error'])
        return payload, before, environment_before

    def overview(self, repository, revision, path, *, depth=0, max_answer_chars=24000):
        try:
            if not isinstance(path, str) or not path or type(depth) is not int or not 0 <= depth <= 3:
                raise ValueError('invalid_overview_request')
            if type(max_answer_chars) is not int or not 1000 <= max_answer_chars <= 65536:
                raise ValueError('invalid_overview_limit')
            payload, untracked, environment = self._discovery(repository, revision, path, '',
                dict(operation='overview', depth=depth, max_answer_chars=max_answer_chars,
                    include_references=False))
            symbols = payload['rows']
            if not isinstance(symbols, dict):
                raise ValueError('incomplete_provider_result')
            blob, _ = source(repository, revision, path)
            return dict(status='ok', provider='serena', evidence_kind='language_server',
                source_revision=revision, report=dict(status='ok' if symbols else 'no_results',
                    path=path, revision=revision, blob_sha=blob, symbols=symbols,
                    provider_identity=payload['server'], untracked_paths=untracked,
                    coverage='file symbol overview; no symbol coordinates or execution claim'),
                environment={'inputs': environment}, diagnostic=dict(
                    state='bounded', next_operation='Inspect a named symbol for source coordinates.'))
        except (ValueError, KeyError, TypeError, OSError, UnicodeError, subprocess.SubprocessError) as error:
            return dict(status='unavailable', provider='serena', source_revision=revision,
                reason=type(error).__name__, detail=str(error)[:300],
                error_reporting=failure_reporting(error), absence_verdict="not-established")

    def find(self, repository, revision, symbol, *, path='', include_body=False,
             max_matches=DEFAULT_MAX_MATCHES, max_answer_chars=24000, response_mode='compact'):
        try:
            if not isinstance(symbol, str) or not symbol or len(symbol) > 256 or '::' in symbol:
                raise ValueError('invalid_symbol')
            if (not isinstance(path, str) or type(include_body) is not bool
                    or type(max_matches) is not int or not 1 <= max_matches <= MAX_MATCHES
                    or type(max_answer_chars) is not int or not 1000 <= max_answer_chars <= 65536
                    or response_mode not in ('full', 'compact')):
                raise ValueError('invalid_find_options')
            payload, untracked, environment = self._discovery(repository, revision, path, symbol,
                dict(include_body=include_body, include_references=False,
                    max_matches=max_matches, max_answer_chars=max_answer_chars))
            rows = payload['rows']
            if not isinstance(rows, list) or len(rows) > max_matches:
                raise ValueError('incomplete_provider_result')
            citations = []
            for row in rows:
                citations.extend(normalize(repository, revision, row['relative_path'], [row],
                    require_body=include_body, compact=response_mode == 'compact'))
            state = 'no_exact_match' if not citations else 'ambiguous' if len(citations) > 1 else 'resolved'
            return dict(status='ok', provider='serena', evidence_kind='language_server',
                source_revision=revision, outcome=state, match_mode='exact_name', query=symbol,
                message=('No exact-name match; a differently named symbol may exist. Use text discovery.' if not citations
                         else 'Exact-name candidates only; this is not an exhaustive semantic search.'),
                absence_verdict='not-established', report=dict(status='ok' if citations else 'no_exact_match',
                    match_mode='exact_name', query=symbol, path_scope=path or 'repository', absence_verdict='not-established',
                    citations=citations, provider_identity=payload['server'],
                    untracked_paths=untracked, coverage='bounded symbol search'),
                environment={'inputs': environment}, diagnostic=dict(symbol_state=state,
                    next_operation=('Use literal text search or inspect a file overview.' if not citations
                                    else 'Inspect a selected path::symbol for references.')))
        except (ValueError, KeyError, TypeError, OSError, UnicodeError, subprocess.SubprocessError) as error:
            return dict(status='unavailable', provider='serena', source_revision=revision,
                reason=type(error).__name__, detail=str(error)[:300],
                error_reporting=failure_reporting(error), absence_verdict="not-established")

    def inspect(self, repository, revision, symbol, *, include_body=True,
                include_references=True, depth=0, max_matches=DEFAULT_MAX_MATCHES,
                max_answer_chars=24000, response_mode='full', include_info=False):
        result = dict(status='unavailable', evidence_kind='language_server',
            provider='serena', source_revision=revision, limitations=list(LIMITATIONS),
            invocation={'worker_attempted': False, 'provider_response_received': False})
        try:
            if (type(include_body) is not bool or type(include_references) is not bool
                    or type(include_info) is not bool or type(depth) is not int or depth < 0 or depth > 3
                    or type(max_matches) is not int or not 1 <= max_matches <= MAX_MATCHES
                    or type(max_answer_chars) is not int or not 1000 <= max_answer_chars <= 65536
                    or response_mode not in ('full', 'compact', 'overview')):
                raise ValueError('invalid_inspect_options')
            if response_mode == 'overview':
                include_body, include_references, depth = False, False, max(depth, 1)
            if include_body and depth:
                raise ValueError('depth_requires_body_omission')
            if not isinstance(symbol, str) or symbol.count('::') != 1:
                raise ValueError('Use repository-relative-path::symbol; do not prepend literal path::')
            path, name = symbol.split('::', 1)
            source(repository, revision, path)  # Validate before any provider invocation.
            if not name or len(name) > 256 or any(ord(c) < 32 for c in name):
                raise ValueError('invalid_symbol')
            before = boundary(repository, revision)
            environment_before = snapshot(repository)
            result['environment'] = {'inputs': environment_before,
                'scope': 'selected navigation configuration and dependency manifests; installed dependencies and deployment not attested'}
            # Startup reads a preconfigured, read-only project. No host registration or onboarding.
            import yaml
            config = yaml.safe_load((Path(repository) / '.serena/project.yml').read_text())
            if config.get('read_only') is not True:
                raise ValueError('read_only_project_required')
            from kp_agent_tooling._impl.service.navigation_process import NavigationProcessRunner
            runner = NavigationProcessRunner(_tool_root(), timeout=150)
            result['invocation']['worker_attempted'] = True
            # The wheel's MCP client dependencies belong to its own Python. A
            # separately pinned Serena interpreter may have a different ABI.
            query_python = sys.executable if Path(__file__).resolve().parents[1].name == '_impl' else self.python
            options = dict(include_body=include_body, include_references=include_references,
                include_info=include_info, depth=depth, max_matches=max_matches,
                max_answer_chars=max_answer_chars)
            raw = runner.run([query_python, str(Path(__file__).resolve()), self.command,
                str(repository), path, name, json.dumps(options)], cwd=repository, max_bytes=65536,
                pythonpath=Path(__file__).resolve().parents[1].name != '_impl')
            payload = json.loads(raw)
            result['invocation']['provider_response_received'] = True
            if 'provider_error' in payload:
                raise ProviderToolError(payload['provider_error'])
            rows = payload['rows']
            if not isinstance(rows, list):
                message = str(rows).lower()
                raise ValueError('truncated_provider_result' if any(token in message for token in
                    ('too many', 'exceeded', 'truncat', 'max_answer_chars', 'max_matches'))
                    else 'invalid_provider_result')
            citations = normalize(repository, revision, path, rows,
                require_body=include_body, compact=response_mode == 'compact')
            reference_state = 'not_requested' if not include_references else 'requires_unique_symbol'
            try:
                if include_references and len(citations) == 1:
                    traversal = normalize_references(repository, revision, payload.get('references'), symbol,
                        compact=response_mode == 'compact')
                    reference_state = 'ok' if traversal['edges'] else 'zero_results'
                    if traversal['omitted']:
                        reference_state = 'truncated'
                else:
                    traversal = dict(edges=[], omitted=0, depth=1, direction='inbound',
                        completeness='not-established', gaps=[])
                    if include_references:
                        traversal['gaps'].append('Traversal requires one uniquely resolved symbol.')
            except (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError):
                traversal = dict(edges=[], omitted=0, depth=1, direction='inbound',
                    completeness='not-established', gaps=['references_unavailable_or_invalid'])
                state = payload.get('references', {})
                reference_state = state.get('state', 'normalization_rejected') if isinstance(state, dict) else 'normalization_rejected'
            after = boundary(repository, revision)
            environment_after = snapshot(repository)
            delta = changes(environment_before, environment_after)
            result['environment']['changes'] = delta
            result['environment']['untracked_added'] = sorted(set(after)-set(before))
            result['environment']['untracked_removed'] = sorted(set(before)-set(after))
            if before != after or delta:
                result['recovery'] = 'Review the recorded input changes; provision provider configuration before retrying. This result contains no accepted source report.'
                raise ValueError('workspace_changed_during_query')
            symbol_state = 'zero_results' if not citations else 'ambiguous' if len(citations) > 1 else 'resolved'
            if symbol_state == 'zero_results':
                next_operation = 'Check the name path or inspect a file overview.'
            elif symbol_state == 'ambiguous':
                next_operation = 'Use an absolute name path or overload index to select one symbol.'
            elif reference_state not in ('ok', 'zero_results', 'not_requested'):
                next_operation = 'Retry references for the uniquely resolved symbol or inspect source directly.'
            else:
                next_operation = 'Read the cited committed source for behavior; static references do not prove execution.'
            coverage_state = ('incomplete' if include_references and reference_state not in ('ok', 'zero_results')
                else 'bounded' if include_references else 'symbol_only')
            return dict(result, status='ok', diagnostic=dict(symbol_state=symbol_state,
                reference_state=reference_state, coverage_state=coverage_state,
                next_operation=next_operation), report=dict(
                status='ok' if citations else 'no_results', citations=citations, traversal=traversal,
                provider_identity=payload['server'], untracked_paths=after,
                coverage='requested symbol' + (' plus bounded one-hop inbound references' if include_references else ''),
                absence_verdict='not-established'))
        except (ValueError, KeyError, TypeError, OSError, UnicodeError, subprocess.SubprocessError) as error:
            state = ('truncated' if str(error) == 'truncated_provider_result'
                else 'normalization_rejected' if result['invocation']['provider_response_received']
                else 'provider_error' if result['invocation']['worker_attempted'] else 'invalid_request')
            return dict(result, reason=type(error).__name__, detail=str(error)[:300],
                error_reporting=failure_reporting(error), absence_verdict='not-established',
                diagnostic=dict(state=state, next_operation='Review the source boundary and provider configuration before retrying.'))


if __name__ == '__main__':
    arguments = sys.argv[1:]
    options = json.loads(arguments.pop()) if len(arguments) == 5 else None
    print(json.dumps(asyncio.run(query(*arguments, options))))
