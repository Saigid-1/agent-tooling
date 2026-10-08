"""Context entry orientation from already-published SCIP, never an on-demand scan."""
import hashlib
import json
import subprocess
from pathlib import Path

from kp_agent_tooling._impl.source_citations import source, cite
from kp_agent_tooling_ops._impl.tool_discovery import GitSource
from kp_agent_tooling._impl.service.scip_entry import symbol_report


class PublishedScipNavigationProvider:
    def __init__(self, navigation, repositories):
        self.profile_path = Path(navigation['profile_path'])
        self.published_root = Path(navigation['published_root'])
        self.local_root = Path(navigation['local_root'])
        self.repositories = {key: Path(row['path']).resolve() for key, row in repositories.items()}

    def _local(self, value):
        path = Path(value)
        relative = path.relative_to(self.published_root)
        if '..' in relative.parts:
            raise ValueError('published path escapes configured root')
        result = self.local_root / relative
        if not result.resolve().is_relative_to(self.local_root.resolve()):
            raise ValueError('published path escapes configured root')
        return str(result)

    @staticmethod
    def _read(path):
        with Path(path).open('rb') as stream:
            raw = stream.read(131073)
        if len(raw) > 131072:
            raise ValueError('published metadata exceeds budget')
        return raw

    def inspect(self, repository, revision, symbol):
        result = {'status': 'unavailable', 'provider': 'published_scip',
                  'evidence_kind': 'static_compiler_definition', 'source_revision': revision,
                  'limitations': ['Only the configured entry definition is resolved here; no call graph is included.',
                                  'Compiler/source agreement is not runtime execution or architectural conformance.']}
        stage = 'profile'
        try:
            keys = [key for key, path in self.repositories.items() if path == Path(repository).resolve()]
            if len(keys) != 1:
                return dict(result, reason='repository_not_uniquely_configured')
            key = keys[0]
            raw = self._read(self.profile_path)
            profile = json.loads(raw)
            if profile.get('schema_version') != 'ops.navigation-profile.v1':
                raise ValueError('invalid profile')
            result['profile_sha256'] = hashlib.sha256(raw).hexdigest()
            member = profile['repos'][key]
            if member['revision'] != revision:
                return dict(result, reason='target_not_in_published_profile', published_revision=member['revision'])
            stage = 'catalog'
            catalog_raw = self._read(self._local(profile['knowledge_config']))
            if hashlib.sha256(catalog_raw).hexdigest() != profile['knowledge_config_sha256']:
                raise ValueError('catalog identity mismatch')
            catalog = json.loads(catalog_raw)
            declared = catalog['repositories'][key]
            if declared['path'] != member['path'] or declared['ref'] != revision:
                raise ValueError('catalog source mismatch')
            root = self._local(member['path'])
            target = GitSource(Path(root), revision)
            if target.revision != revision:
                raise ValueError('target identity mismatch')
            stage = 'entry'
            if '::' in symbol:
                path, qualified = symbol.split('::', 1)
            else:
                parts = symbol.split('.')
                matches = []
                for cut in range(1, len(parts)):
                    for path in ('/'.join(parts[:cut]) + '.py', '/'.join(parts[:cut]) + '/__init__.py'):
                        if target.entry(path):
                            matches.append((cut, path, '.'.join(parts[cut:])))
                if matches:
                    deepest = max(row[0] for row in matches)
                    matches = [row for row in matches if row[0] == deepest]
                if len(matches) != 1:
                    return dict(result, reason='entry_module_not_unique')
                _, path, qualified = matches[0]
            # Reuse the exact-source citation resolver, including its unique nested-symbol check.
            source(root, revision, path, max_bytes=1_000_000)
            citations = []
            for prefix in ('def ', 'class '):
                try:
                    citations.append(cite(root, revision, path, prefix + qualified.split('.')[-1],
                                          context=0, symbol=qualified))
                except ValueError:
                    pass
            if len(citations) != 1:
                return dict(result, reason='entry_definition_not_unique')
            citation = citations[0]
            stage = 'scip'
            specs = [{**spec, 'path': self._local(spec['path'])}
                     for spec in catalog['scip_indexes'].get(key, [])]
            # Do not broaden context's repo authorization to the platform's other members.
            scoped = {'repositories': {key: {'path': root}},
                      'platforms': {key: {'sources': {key: {'revision': revision}}}},
                      'scip_indexes': {key: specs}}
            report = symbol_report(scoped, key, revision, path, citation['start_line'], repo_key=key)
            _, text = source(root, revision, path, max_bytes=1_000_000)
            rows = [row for row in report['data']['results']
                    if row['occurrence']['definition'] and
                    row['occurrence']['blob_sha'] == citation['blob_sha'] and
                    text.encode()[row['occurrence']['byte_offset']:
                        row['occurrence']['byte_offset'] + row['occurrence']['byte_length']].decode() == qualified.split('.')[-1]]
            if report['data']['gaps'] or len(rows) != 1:
                return dict(result, reason='entry_scip_unverified', index_gaps=report['data']['gaps'])
            return dict(result, status='ok', report={'entry_symbol': symbol, 'citation': citation,
                'compiler_symbol': rows[0]['occurrence']['symbol'], 'coverage': 'entry_definition_only',
                'source_call': {'tool': 'navigation.source', 'arguments': {'repo_key': key,
                    'target_revision': revision, 'path': path, 'start_line': citation['start_line'], 'line_count': 80}},
                'next_call': {'tool': 'knowledge.symbol', 'arguments': {'repo_key': key,
                    'target_revision': revision, 'path': path, 'line': citation['start_line']}}})
        except (OSError, ValueError, KeyError, TypeError, UnicodeError, subprocess.SubprocessError):
            return dict(result, reason='published_navigation_unavailable', failed_stage=stage)
