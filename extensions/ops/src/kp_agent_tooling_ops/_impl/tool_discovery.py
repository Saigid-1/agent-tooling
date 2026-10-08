"""Bounded lexical discovery of operator entry points in immutable Git source.

This module reads source; it never imports, runs, or grants access to a tool.
"""
from __future__ import annotations

import ast
import hashlib
import os
from pathlib import Path
import re
import subprocess
from typing import Protocol


class DiscoveryError(ValueError):
    """A source or query cannot be resolved within the discovery contract."""


class SourceReader(Protocol):
    revision: str

    def entries(self) -> list[tuple[str, str, str]]: ...
    def read(self, blob: str) -> bytes: ...


class GitSource:
    """Read only an explicit local repository, with bounded blobs and Git waits."""

    def __init__(self, repository: Path, ref: str):
        self.repository = Path(repository).resolve()
        if not self.repository.is_dir():
            raise DiscoveryError('repository must be an existing local directory')
        self.revision = self._git('rev-parse', '--verify', '--end-of-options', ref + '^{commit}').decode().strip()
        if not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', self.revision):
            raise DiscoveryError('revision did not resolve to a commit')
        root = self._git('rev-parse', '--show-toplevel').decode().strip()
        self.repository = Path(root)

    def _git(self, *args: str) -> bytes:
        # Do not inherit an ambient GIT_DIR/worktree/index override.
        env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
        env.update(GIT_TERMINAL_PROMPT='0', GIT_OPTIONAL_LOCKS='0', GIT_NO_REPLACE_OBJECTS='1')
        try:
            result = subprocess.run(['git', '-C', str(self.repository), *args],
                                    env=env, capture_output=True, timeout=10, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DiscoveryError('local Git read unavailable or timed out') from exc
        if result.returncode:
            raise DiscoveryError('local Git source or revision unavailable')
        return result.stdout

    def entries(self) -> list[tuple[str, str, str]]:
        data = self._git('ls-tree', '-z', self.revision + ':scripts')
        if len(data) > 2_000_000:
            raise DiscoveryError('script inventory exceeds the source budget')
        rows = []
        for entry in data.split(b'\0'):
            if not entry:
                continue
            header, name = entry.split(b'\t', 1)
            mode, kind, blob = header.decode('ascii').split()
            try:
                path = name.decode('utf-8')
            except UnicodeError as exc:
                raise DiscoveryError('script path is not UTF-8') from exc
            if kind == 'blob' and path.endswith('.py'):
                rows.append((mode, 'scripts/' + path, blob))
        if len(rows) > 2000:
            raise DiscoveryError('script inventory exceeds 2000 files')
        return sorted(rows, key=lambda row: row[1])

    def read_bounded(self, blob: str, max_bytes: int) -> bytes:
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
            raise DiscoveryError('invalid source byte budget')
        if int(self._git('cat-file', '-s', blob)) > max_bytes:
            raise DiscoveryError('source exceeds the byte budget')
        return self._git('cat-file', 'blob', blob)

    def read(self, blob: str) -> bytes:
        return self.read_bounded(blob, 2_000_000)

    def entry(self, path: str) -> tuple[str, str] | None:
        """Resolve one literal path without following symlinks or reading content."""
        data = self._git('ls-tree', '--full-tree', '-z', self.revision, '--', ':(literal)' + path)
        if not data:
            return None
        header, name = data.rstrip(b'\0').split(b'\t', 1)
        if name.decode('utf-8') != path:
            raise DiscoveryError('source path did not resolve exactly')
        mode, _kind, blob = header.decode('ascii').split()
        return mode, blob


def source_metadata(text: str) -> tuple[str, list[str]]:
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError) as exc:
        raise DiscoveryError('script is not parseable Python source') from exc
    doc = ast.get_docstring(tree)
    purpose = ''
    if doc and 'exec "$(dirname' not in doc:
        purpose = ' '.join(doc.strip().split('\n')[0].split())
    if not purpose:
        for line in text.splitlines():
            line = line.strip()
            if line.startswith('#') and not line.startswith('#!'):
                body = line.lstrip('#').strip()
                if body and not body.startswith(('-*-', 'type:')):
                    purpose = body
                    break
    commands = sorted({node.args[0].value for node in ast.walk(tree)
                       if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                       and node.func.attr == 'add_parser' and node.args
                       and isinstance(node.args[0], ast.Constant)
                       and isinstance(node.args[0].value, str)})
    return purpose or '(no stated purpose)', commands


def _words(text: str) -> list[str]:
    return re.findall(r'[a-z0-9]+', text.lower())


def search(source: SourceReader, query: str) -> dict:
    try:
        query_bytes = query.encode('utf-8')
    except UnicodeError as exc:
        raise DiscoveryError('query must be valid UTF-8') from exc
    needles = _words(query)
    if not needles:
        raise DiscoveryError('query must contain letters or digits')
    if len(query_bytes) > 512:
        raise DiscoveryError('query exceeds 512 UTF-8 bytes')
    matches = []
    symlinks = 0
    for mode, path, blob in source.entries():
        if mode == '120000':
            symlinks += 1
            continue
        raw = source.read(blob)
        try:
            text = raw.decode('utf-8')
        except UnicodeError as exc:
            raise DiscoveryError('script source is not UTF-8') from exc
        purpose, commands = source_metadata(text)
        words = _words(' '.join([Path(path).name, purpose, *commands]))
        if not all(any(word.startswith(needle) for word in words) for needle in needles):
            continue
        row = {'tool': Path(path).name, 'purpose': purpose[:240], 'subcommands': commands,
               'source': {'path': path, 'commit': source.revision, 'blob_sha': blob,
                          'sha256': hashlib.sha256(raw).hexdigest(), 'line': 1,
                          'end_line': max(1, len(text.splitlines()))}}
        if len(purpose) > 240:
            row['purpose_truncated'] = True
        matches.append(row)
    exact = query.strip().lower()
    matches.sort(key=lambda r: (exact not in {r['tool'].lower(), Path(r['tool']).stem.lower()}, r['tool']))
    return {'schema_version': 'ops.tool-discovery.v1', 'query': query, 'revision': source.revision,
            'source_scope': 'committed scripts/*.py; working-tree changes excluded',
            'absence_verdict': 'not-established', 'runtime_verification': 'not-performed',
            'available': len(matches), 'shown': len(matches), 'truncated': 0,
            'excluded_symlinks': symlinks, 'results': matches,
            'limitations': ['Lexical search covers only committed top-level Python scripts.',
                            'Literal subcommands are source candidates, not proven reachable commands.',
                            'Grants, dependencies, runtime configuration and successful execution are not verified.']}
