#!/usr/bin/env python3
"""List the files under apps/kanban that this repository changed relative to upstream Cline Kanban.

Apache-2.0 section 4(b) change record (see apps/kanban/NOTICE). The upstream side is
docs/upstream-tree-abd4912c.json: every file of https://github.com/cline/kanban at the fork
point, with its Git blob sha. This side is the tree's own files, hashed as Git hashes a blob.
A file is modified when both sides have it with different blobs, added when only this side
has it, and removed when only upstream has it. No network access.

    python apps/kanban/scripts/kanban_delta.py            # print the section
    python apps/kanban/scripts/kanban_delta.py --write    # rewrite it in docs/ops-local-delta-manifest.md
    python apps/kanban/scripts/kanban_delta.py --check    # exit 1 when the manifest's section is stale

In a Git checkout the files are those Git tracks plus untracked files it does not ignore;
outside one (for example a `git archive` export), every file except build and dependency
directories.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

KANBAN = Path(__file__).resolve().parents[1]
UPSTREAM = KANBAN / 'docs' / 'upstream-tree-abd4912c.json'
MANIFEST = KANBAN / 'docs' / 'ops-local-delta-manifest.md'
HEADING = '## Files changed relative to the fork point'
_SKIP_DIRS = frozenset({'.git', 'node_modules', 'dist', 'dist-observer', '.worktrees', '__pycache__'})


def git_blob(path: Path) -> str:
    """The Git blob sha of a file (a symlink hashes its target, as Git stores it)."""
    data = os.readlink(path).encode() if path.is_symlink() else path.read_bytes()
    return hashlib.sha1(b'blob %d\0' % len(data) + data).hexdigest()


def local_files(root: Path = KANBAN) -> list[str]:
    try:
        top = subprocess.run(['git', '-C', str(root), 'rev-parse', '--show-toplevel'], capture_output=True, text=True)
        if top.returncode == 0:
            listed = subprocess.run(['git', '-C', str(root), 'ls-files', '-z', '--cached', '--others',
                                     '--exclude-standard', '--', '.'], capture_output=True, check=True)
            names = {name.decode() for name in listed.stdout.split(b'\0') if name}
            return sorted(name for name in names if (root / name).is_file() or (root / name).is_symlink())
    except FileNotFoundError:  # no git executable
        pass
    found = []
    for directory, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        found.extend(Path(directory, name).relative_to(root).as_posix() for name in files)
    return sorted(found)


def upstream(path: Path = UPSTREAM) -> tuple[str, dict[str, str]]:
    data = json.loads(path.read_text())
    return data['commit'], data['files']


def delta(root: Path = KANBAN) -> dict[str, list[str]]:
    _commit, theirs = upstream()
    ours = {name: git_blob(root / name) for name in local_files(root)}
    return {'modified': sorted(p for p in ours if p in theirs and ours[p] != theirs[p]),
            'added': sorted(p for p in ours if p not in theirs),
            'removed': sorted(p for p in theirs if p not in ours)}


def render(changes: dict[str, list[str]]) -> str:
    commit, _ = upstream()
    lines = [HEADING, '',
             'Apache-2.0 section 4(b) change record. The list compares each file\'s Git blob with',
             f'the upstream tree at `{commit}`, recorded in',
             '`upstream-tree-abd4912c.json` beside this manifest (read from the public GitHub API).',
             'Paths are relative to `apps/kanban`. Every other file is byte-identical to upstream at',
             'the fork point. Regenerate with `python apps/kanban/scripts/kanban_delta.py --write`.', '']
    for title, key in (('Modified upstream files', 'modified'), ('Added by this repository', 'added'),
                       ('Removed upstream files', 'removed')):
        lines += [f'**{title} ({len(changes[key])}).**', ''] + [f'- `{p}`' for p in changes[key]] + ['']
    return '\n'.join(lines).rstrip() + '\n'


def manifest_section(text: str) -> str:
    start = text.index(HEADING)
    return text[start:]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--write', action='store_true')
    mode.add_argument('--check', action='store_true')
    args = parser.parse_args()
    section = render(delta())
    if args.write:
        text = MANIFEST.read_text()
        MANIFEST.write_text(text[:text.index(HEADING)] + section)
        return 0
    if args.check:
        if manifest_section(MANIFEST.read_text()) != section:
            print('docs/ops-local-delta-manifest.md is stale; run kanban_delta.py --write', file=sys.stderr)
            return 1
        return 0
    sys.stdout.write(section)
    return 0


if __name__ == '__main__':
    sys.exit(main())
