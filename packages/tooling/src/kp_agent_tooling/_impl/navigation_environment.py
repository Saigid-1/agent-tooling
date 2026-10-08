"""Bounded identities for navigation inputs, not deployment attestation."""
import hashlib
from pathlib import Path

CONFIG_PATHS = ('.serena/project.yml', '.serena/.gitignore', 'pyproject.toml',
                'requirements.lock', 'uv.lock', 'pyrightconfig.json',
                'tsconfig.json', 'package.json', 'package-lock.json')


def snapshot(repository):
    root = Path(repository).resolve()
    files = {}
    for name in CONFIG_PATHS:
        path = root / name
        if path.is_symlink() or (path.exists() and path.resolve() != root / name):
            raise ValueError('navigation_config_symlink')
        if not path.exists():
            files[name] = {'status': 'absent'}
            continue
        if not path.is_file() or path.stat().st_size > 8_000_000:
            raise ValueError('navigation_config_budget_or_type')
        files[name] = {'status': 'present', 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
    return files


def changes(before, after):
    return [{'path': name, 'before': before[name], 'after': after[name]}
            for name in before if before[name] != after[name]]
