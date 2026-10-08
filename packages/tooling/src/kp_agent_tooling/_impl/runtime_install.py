"""Render one agent-tooling runtime root from the single manifest (ADR AT-0003).

The manifest is package data (`assets/deploy/`), so a wheel or image installs
without a source checkout; `deploy/` in the repository links to it.

`plan` reads the manifest, the selected repositories and the runtime root and
writes nothing. `apply` accepts only the reviewed plan digest; it refuses a
symlink, a foreign or drifted file, or any other conflict before its first write,
replaces only files its own receipt recorded, and writes `receipt.json` last. If
a write fails, it rolls back what it created and restores what it replaced.
`verify` re-hashes the root against that receipt and reports, per selected role,
which operator files are still absent (`readiness`).

`--board-agents` (plan input `board_agents`, board component only) is for the
`agents` image, whose board runs the Claude and Codex CLIs itself: the board role
then mounts every planned repository read-write at its host path, so Kanban can
add task worktrees there, and sees the board launch profiles at the path the
registry descriptor names. No other role's mounts change.

Operator files (a token, `refresh.json`, `config/capture/*`, the launch registry)
are never created with content, replaced or hashed. Where a role binds one as a
single-file mount source, apply creates an empty 0600 placeholder only where it is
absent, so that the first `docker compose up` can create the container; the
receipt records it as operator-owned, without a digest.

Every store a role opens lives under `/state/memory`, the project's named volume
`<project>_memory`, not on the `/state` bind (T9b): on a Docker Desktop bind mount a
POSIX lock held by one process is granted again to another, and SQLite relies on those
locks. `plan`, `apply` and `verify` never start a container and never create a volume.
`prepare` (run after `apply`, before `up`) is the one step that writes through Docker:
while no container of the project runs, it creates the volume, has a one-off root
container of the runtime image chown and chmod 0700 its root (nothing else), and moves
every pre-T9b store directory on the bind into it once, losslessly, in a one-off
container running as AGENT_UID:AGENT_GID. The host never opens a store file: every
copy, hash and SQLite read happens in those containers. Operator files that name an old
store path are reported (by `apply`, `prepare` and `verify`), never rewritten.

Nothing here pulls an image, starts a role, reads a credential, or registers a host.
The rendered MCP snippets are for the operator to install explicitly. A receipt
detects drift; it is not a signature and proves no runtime readiness.
"""
from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
import stat
import subprocess

import yaml

from kp_agent_tooling._impl import leaf
from kp_agent_tooling._impl.leaf import sha256_hex as sha256

PLAN_SCHEMA = 'agent-tooling.install-plan.v1'
RECEIPT_SCHEMA = 'agent-tooling.install-receipt.v1'
RECEIPT = 'receipt.json'
VOLUME_MARKER = 'ops-tooling-state-v1'
NAVIGATION_SCHEMA = 'ops.agent-tooling.v1'
# `summarizer` (S1a) is opt-in: selected only when a plan names it, never by default or with another role.
COMPONENTS = ('tooling', 'refresh', 'capture', 'board', 'indexer', 'telemetry', 'summarizer')
SERVICES = frozenset({'tooling', 'refresh', 'capture', 'board', 'indexer', 'tempo', 'collector', 'summarizer'})
# The components that seal episodes inside Compose; the `indexer` role (T12b), the one drainer of
# their index outbox, is selected whenever one of them is.
SEALERS = ('capture', 'board')
OVERLAYS = ('compose.workspaces.yaml', 'compose.capture.yaml')
DIRECTORY_MODE = 0o700
FILE_MODE = 0o600
# Reviewed default tool surface of the image's navigation example configuration.
ENABLED_TOOLS = ['tooling.identity', 'navigation.source', 'navigation.paths', 'navigation.search',
                 'navigation.manifest', 'navigation.search_page', 'navigation.imports',
                 'navigation.dependencies', 'delivery.read']

_NAME = r'[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*'
IMAGE = re.compile(r'(?:sha256:[0-9a-f]{64}|(?:[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?(?::[0-9]{1,5})?/)?'
                   + _NAME + r'(?:/' + _NAME + r')*@sha256:[0-9a-f]{64})\Z')
REPO_KEY = re.compile(r'[A-Za-z][A-Za-z0-9_-]{0,63}\Z')
PROJECT = re.compile(r'[a-z0-9][a-z0-9_-]{0,62}\Z')
REVISION = re.compile(r'(?:[0-9a-f]{40}|[0-9a-f]{64})\Z')
PLAN_SHA = re.compile(r'[0-9a-f]{64}\Z')
CONTROL = re.compile(r'[\x00-\x1f\x7f]')
# Capture binds keep host paths; none may shadow or enter these container paths.
RESERVED = tuple(PurePosixPath(p) for p in (
    '/state', '/config', '/import-sources', '/spool', '/proc', '/sys', '/dev', '/run', '/var/run',
    '/usr', '/etc', '/bin', '/sbin', '/lib', '/app', '/opt/serena', '/opt/toolchain'))
# Containers (and, for spool, the host adapter's hooks) own everything below these
# except the paths the plan itself names.
RUNTIME_DATA = ('state', 'tempo', 'import-sources', 'spool')
HOST_ADAPTER_SCHEMA = 'agent-tooling.host-adapter.v1'
# The one registry operator config of the runtime: host launches (in the capture
# container) and the board's Desks menu and desk task launches all name this path.
HOST_LAUNCH_CONFIG = '/config/launch/registry.json'
# The harness profiles the registry descriptor names for host launches (`~/` expanded
# against the planning home), and the board's own copy: the board's CLIs run in the
# container, whose home is /state. With --board-agents the board role sees its copy
# at HARNESS_PROFILES, so one descriptor serves host and board launches.
HARNESS_PROFILES = '/config/launch/harness-profiles.json'
BOARD_PROFILES = 'config/launch/board-harness-profiles.json'
CONTAINER_HOME = '/state'
MAX_RECEIPT_BYTES = 1_000_000
# Operator-owned files by component, as root-relative paths. The installer never
# creates them with content, replaces or hashes them; `verify` reports the unwritten ones.
# - required: the role's own command waits (`not_configured`) until they are written
#   (exist and are not empty);
# - placeholder: bound as single-file mount sources, so apply creates each one
#   empty (0600) where absent, and the role starts; an empty token serves public remotes;
# - host_launch: what `kp-agent-host` docker-mode launches need in the capture role;
# - desks: what the board's Desks menu and desk task launches need; the board serves
#   without it and reports the desk registry as not configured;
# - optional: an integration that reports itself unconfigured without them.
# A file several components use is one row, with the further uses under `also`.
OPERATOR_FILES = {
    'refresh': {'required': ('config/refresh.json',), 'placeholder': ('secrets/github-token',)},
    'capture': {'required': ('config/capture/session.json', 'config/capture/workspace-policy.json'),
                'host_launch': (HOST_LAUNCH_CONFIG.removeprefix('/'),)},
    'board': {'desks': (HOST_LAUNCH_CONFIG.removeprefix('/'),), 'optional': ('config/board/session-import.json',)},
    # S1a: the standing approval and the role's gateway configuration; the provider key file is
    # bound into the summarizer alone (SUMMARIZER_KEY), like refresh's token; its interval is the
    # optional Compose env_file's AGENT_SUMMARIZER_INTERVAL_SECONDS.
    'summarizer': {'required': ('config/summarizer/approval.json', 'config/summarizer/gateway.json'),
                   'placeholder': ('secrets/summarizer.key',),
                   'optional': ('config/summarizer/summarizer.env',)},
}
# The summarizer's provider key: secrets/summarizer.key on the host, bound read-only at this path
# (inside the read-only configuration bind, so apply renders an empty mount point there).
SUMMARIZER_KEY = '/config/summarizer.key'
PLACEHOLDER = 'empty-0600-where-absent'
OPERATOR_USES = ('required', 'placeholder', 'host_launch', 'desks', 'optional')
# Readiness sub-entries: a use whose absence leaves the role itself ready.
SUB_ENTRIES = ('host_launch', 'desks')
# The store volume (T9b): its mount point in every role that mounts /state, and the
# pre-T9b store directories on the bind that `prepare` moves into it. state/memory's
# entries go to the volume's root; every other store directory to /state/memory/<name>.
MEMORY_TARGET = str(leaf.STORE_ROOT)
MEMORY_HOST = 'state/memory'
MEMORY_KEY = 'memory'  # the volume's key in the manifest's top-level `volumes`
MEMORY_MODE = 0o700
LEGACY_STORES = leaf.LEGACY_STORES
# Directories under state/ that the installer renders for other uses; never a store.
NOT_STORES = frozenset({'memory', 'tmp', 'snapshots', 'search', 'delivery', 'models'})
# Operator-file keys that name a store path (the rule is the leaf's required_store_path).
STORE_KEYS = leaf.STORE_KEYS
TEMPLATE_KEY = leaf.TEMPLATE_KEY
MAX_OPERATOR_JSON = 1_000_000
COMPOSE_PROJECT_LABEL = 'com.docker.compose.project'
COMPOSE_VOLUME_LABEL = 'com.docker.compose.volume'


class ApplyFailed(OSError):
    """A failure after preflight; the partial writes were rolled back where possible."""

    def __init__(self, error, rollback):
        super().__init__(f'{type(error).__name__}: {error}')
        self.error, self.rollback = error, rollback

    def result(self):
        complete = not self.rollback['left_behind']
        return {'status': 'failed', 'error': type(self.error).__name__, 'detail': str(self.error)[:300],
                'rollback': 'complete' if complete else 'incomplete', **self.rollback,
                'recovery': ('the runtime root is back in its state before this apply' if complete else
                             'remove or restore the paths listed in left_behind, then run verify')}


class InstallRefused(ValueError):
    """A refusal raised before any write."""

    def __init__(self, reason, detail, **facts):
        super().__init__(detail)
        self.reason, self.detail, self.facts = reason, detail, facts

    def result(self):
        return {'status': 'refused', 'reason': self.reason, 'detail': self.detail, **self.facts,
                'writes': 'none'}


def canonical(value):
    return leaf.canonical_bytes(value, ascii=True, allow_nan=False)


# ---------------------------------------------------------------- input checks

def _path_text(raw, label, *, extra=''):
    if not isinstance(raw, str) or not raw:
        raise InstallRefused('path_invalid', f'{label}: an absolute path is required')
    if '$' in raw or CONTROL.search(raw) or any(char in raw for char in extra):
        shown = ', '.join(['$', 'a newline or other control character', *extra])
        raise InstallRefused('path_unsafe', f'{label} must not contain any of: {shown} '
                             '(Compose interpolation, .env quoting, COMPOSE_FILE separator)',
                             path=repr(raw))
    path = Path(raw)
    if not path.is_absolute():
        raise InstallRefused('path_invalid', f'{label} must be absolute', path=raw)
    return path


def runtime_root(raw):
    """The runtime root as an existing, physical, non-symlinked directory."""
    path = _path_text(raw, 'runtime root', extra=("'", ':'))
    if path.is_symlink():
        raise InstallRefused('runtime_root_symlink', 'runtime root is a symlink', path=raw)
    if not path.exists():
        raise InstallRefused('runtime_root_missing', 'runtime root must already exist', path=raw)
    if path.resolve(strict=True) != path:
        raise InstallRefused('runtime_root_not_physical',
                             'runtime root must be a physical path without symlinked components',
                             path=raw, physical=str(path.resolve(strict=True)))
    if not path.is_dir():
        raise InstallRefused('runtime_root_not_directory', 'runtime root must be a directory', path=raw)
    return path


def image_reference(value):
    if not isinstance(value, str) or not IMAGE.fullmatch(value):
        raise InstallRefused('image_not_digest', 'image must be a digest reference '
                             '(registry/name@sha256:<64 hex> or sha256:<64 hex>), never a tag',
                             image=repr(value))
    return value


def _integer(value, label, low, high, reason):
    try:
        number = int(str(value), 10)
    except (TypeError, ValueError):
        number = None
    if number is None or isinstance(value, bool) or not low <= number <= high:
        raise InstallRefused(reason, f'{label} must be an integer in {low}..{high}', value=repr(value))
    return number


def identity(value, label):
    if str(value).strip() in {'0', '-0', '+0'}:
        raise InstallRefused(label + '_root', f'{label} 0 (root) is never used')
    return _integer(value, label, 1, 2 ** 31 - 2, label + '_invalid')


def board_port(value):
    return _integer(value, 'board port', 1024, 65535, 'board_port_invalid')


def project_name(value):
    if not isinstance(value, str) or not PROJECT.fullmatch(value):
        raise InstallRefused('project_name_invalid', 'project name must match [a-z0-9][a-z0-9_-]{0,62}',
                             value=repr(value))
    return value


def components(value):
    items = value.split(',') if isinstance(value, str) else list(value or ())
    items = [item.strip() for item in items]
    unknown = sorted({item for item in items if item not in COMPONENTS})
    if unknown:
        raise InstallRefused('component_unknown', 'unknown component', components=unknown,
                             known=list(COMPONENTS))
    if len(items) != len(set(items)) or 'tooling' not in items:
        raise InstallRefused('component_invalid', 'components must be unique and include tooling',
                             components=items)
    if any(name in items for name in SEALERS) and 'indexer' not in items:
        items.append('indexer')  # capture and board seal: their drainer is selected with them (T12b)
    return [name for name in COMPONENTS if name in items]


def operator_files(selected):
    """The operator-owned paths of the selected components, as the plan and receipt record them.

    A path more than one selected component uses (the launch registry: capture's host
    launches and the board's desks) is one row for the first component, and lists
    each further use under ``also``.
    """
    rows = {}
    for component in selected:
        for use, names in OPERATOR_FILES.get(component, {}).items():
            for name in names:
                if name in rows:
                    rows[name].setdefault('also', []).append({'component': component, 'use': use})
                    continue
                row = {'owner': 'operator', 'component': component, 'use': use}
                if use == 'placeholder':
                    row['placeholder'] = PLACEHOLDER
                rows[name] = row
    return dict(sorted(rows.items()))


def _operator_uses(rows):
    """(path, component, use) for every use a declared operator-file table records."""
    for name, row in rows.items():
        yield name, row['component'], row['use']
        for extra in row.get('also', ()):
            yield name, extra['component'], extra['use']


def _home_roots(planned=()):
    """Home directory roots never bound: system roots, the invoking user's and any planned ``--home``."""
    roots = {PurePosixPath('/root'), PurePosixPath('/var/root'), PurePosixPath('/private/var/root')}
    try:
        roots.add(PurePosixPath(Path.home().resolve()))
    except (OSError, RuntimeError):
        pass
    roots.update(PurePosixPath(home) for home in planned)
    return roots


def _contains(outer, inner):
    return outer == inner or outer in inner.parents


def _mountable(path, label, planned_homes=()):
    """Refuse a bind source that is, or exposes, a home directory root or the Docker socket."""
    pure = PurePosixPath(path)
    homes = _home_roots(planned_homes)
    if (pure.parent in (PurePosixPath('/Users'), PurePosixPath('/home')) or
            any(_contains(pure, home) for home in homes)):
        raise InstallRefused('mount_forbidden', f'{label} is or contains a home directory root',
                             path=str(path))
    if any(_contains(home / '.docker', pure) for home in homes):
        raise InstallRefused('mount_forbidden', f'{label} is inside a Docker client directory',
                             path=str(path))
    # Capture mounts host paths at the same container path.
    if any(_contains(pure, reserved) or _contains(reserved, pure) for reserved in RESERVED):
        raise InstallRefused('mount_forbidden', f'{label} collides with a reserved container path',
                             path=str(path))


def _git(repo, *args):
    env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    try:
        result = subprocess.run(['git', '--no-optional-locks', '-c', 'core.fsmonitor=false',
                                 '-C', str(repo), *args], capture_output=True, text=True,
                                env=env, timeout=30)
    except (OSError, subprocess.SubprocessError) as error:
        raise InstallRefused('git_unavailable', f'git could not run: {type(error).__name__}') from error
    return result.returncode, result.stdout.strip()


def repository(spec, planned_homes=()):
    """Parse KEY=ABSOLUTE_PATH; the path must be the root of a committed Git repository."""
    if not isinstance(spec, str) or '=' not in spec:
        raise InstallRefused('repository_invalid', 'repository must be KEY=ABSOLUTE_PATH', value=repr(spec))
    key, raw = spec.split('=', 1)
    if not REPO_KEY.fullmatch(key):
        raise InstallRefused('repository_key_invalid', 'repository key must match '
                             '[A-Za-z][A-Za-z0-9_-]{0,63}', key=repr(key))
    given = _path_text(raw, f'repository {key}')
    if not given.is_dir():
        raise InstallRefused('repository_invalid', f'repository {key} is not an existing directory', path=raw)
    path = given.resolve(strict=True)
    git_dir = path / '.git'
    if git_dir.is_symlink() or not git_dir.is_dir():
        raise InstallRefused('repository_invalid', f'repository {key} is not a Git repository root with '
                             'its own .git directory (linked worktrees and submodules keep objects '
                             'outside the bind mount)', path=raw)
    code, top = _git(path, 'rev-parse', '--show-toplevel')
    if code or Path(top).resolve() != path:
        raise InstallRefused('repository_invalid', f'repository {key} is not a Git repository root', path=raw)
    code, common = _git(path, 'rev-parse', '--path-format=absolute', '--git-common-dir')
    if code or Path(common).resolve() != git_dir.resolve():
        raise InstallRefused('repository_invalid', f'repository {key} shares another object store', path=raw)
    if (git_dir / 'objects' / 'info' / 'alternates').exists():
        raise InstallRefused('repository_invalid', f'repository {key} borrows objects through alternates '
                             'outside its bind mount', path=raw)
    code, revision = _git(path, 'rev-parse', '--verify', '--quiet', 'HEAD^{commit}')
    if code or not REVISION.fullmatch(revision):
        raise InstallRefused('repository_invalid', f'repository {key} has no committed HEAD', path=raw)
    _mountable(path, f'repository {key}', planned_homes)
    return key, {'path': str(path), 'revision': revision}


def transcript_root(raw, planned_homes=()):
    given = _path_text(raw, 'transcript root')
    if not given.is_dir():
        raise InstallRefused('transcript_root_invalid', 'transcript root must be an existing directory',
                             path=raw)
    path = given.resolve(strict=True)
    _mountable(path, 'transcript root', planned_homes)
    return str(path)


def home_directory(raw=None):
    """The home directory ``~/`` expands against, as a physical path; a plan input (``inputs.home``).

    ``raw`` is ``kp-agent-install --home``: an absolute, existing directory. Without
    it, the invoking user's home (``Path.home()``, which honours ``$HOME``) applies.
    """
    given = _path_text(str(Path.home()) if raw is None else raw, 'home directory')
    if not given.is_dir():
        raise InstallRefused('home_invalid', 'home directory must be an existing directory', path=str(given))
    return str(given.resolve(strict=True))


def _under(path, roots):
    return any(path == root or root in path.parents for root in roots)


def launch_profiles(home, transcript_roots):
    """The packaged harness profiles for host launches, with ``~/`` capture roots expanded.

    The container's home is /state, so a ``~/`` root cannot be resolved there; the
    installer runs as the launching user and resolves it here. A profile whose root
    is not equal to or under a planned transcript root is rendered disabled.
    Returns (document, [(harness, expanded root)] of the profiles it disabled).
    """
    from kp_agent_tooling._impl.service.harness_profiles import validate
    raw = json.loads((Path(__file__).resolve().parents[1] / 'assets' / 'harness-profiles.json').read_text())
    validate(raw)
    roots = [PurePosixPath(root) for root in transcript_roots]
    profiles, disabled = [], []
    for profile in raw['profiles']:
        profile = json.loads(json.dumps(profile))
        capture = profile['capture']
        if capture['mode'] == 'transcript':
            root = capture['root']
            expanded = Path(home) / root[2:] if root.startswith('~/') else Path(root)
            expanded = PurePosixPath(os.path.realpath(expanded))
            capture['root'] = str(expanded)
            if profile['enabled'] and not _under(expanded, roots):
                profile['enabled'] = False
                disabled.append((profile['harness'], str(expanded)))
        profiles.append(profile)
    document = {'schema_version': raw['schema_version'], 'profiles': profiles}
    validate(document)
    return document, disabled


def board_launch_profiles():
    """The packaged harness profiles as the board's own CLIs use them (`agents` image).

    Board tasks run the CLI inside the board container, whose home is /state, so a
    ``~/`` capture root expands against /state: that is where the in-container CLI
    writes its transcripts, and where a board launch's hooks find them. Every
    packaged profile keeps its ``enabled`` value.
    """
    from kp_agent_tooling._impl.service.harness_profiles import validate
    raw = json.loads((Path(__file__).resolve().parents[1] / 'assets' / 'harness-profiles.json').read_text())
    validate(raw)
    document = json.loads(json.dumps(raw))
    for profile in document['profiles']:
        capture = profile['capture']
        if capture['mode'] == 'transcript' and capture['root'].startswith('~/'):
            capture['root'] = str(PurePosixPath(CONTAINER_HOME, capture['root'][2:]))
    validate(document)
    return document


# -------------------------------------------------------------------- manifest

MANIFEST_FILES = ('compose.yaml', 'telemetry/tempo.yaml', 'telemetry/collector.yaml')


def packaged_directory():
    """The canonical manifest, shipped as package data in every wheel and image."""
    return Path(__file__).resolve().parents[1] / 'assets' / 'deploy'


def deploy_directory():
    """`deploy/` of a source checkout; None unless running from one (editable install)."""
    parents = Path(__file__).resolve().parents
    if len(parents) <= 5:  # installed at a shallow path: certainly not a source checkout
        return None
    root = parents[5]
    return root / 'deploy' if (root / 'packages' / 'tooling' / 'pyproject.toml').is_file() else None


def _read(directory, *, follow):
    data = {}
    for name in MANIFEST_FILES:
        path = directory / name
        if (not follow and path.is_symlink()) or not path.is_file():
            return None
        data[name] = path.read_bytes()
    return data


def manifest(deploy=None):
    """Manifest bytes and their source: the package copy, else a source checkout's deploy/."""
    if deploy is not None:
        source, data = Path(deploy), _read(Path(deploy), follow=True)
    else:
        source, data = packaged_directory(), _read(packaged_directory(), follow=False)
        checkout = deploy_directory()
        linked = _read(checkout, follow=True) if checkout is not None else None
        if data is None and linked is not None:
            source, data = checkout, linked
        elif data is not None and linked is not None and linked != data:
            # deploy/ links to the package copy; a differing regular file there is a second copy.
            raise InstallRefused('manifest_diverged', 'deploy/ differs from the packaged manifest; '
                                 'restore the deploy/ symlinks and edit the packaged copy',
                                 packaged=str(packaged_directory()), checkout=str(checkout))
    if data is None:
        raise InstallRefused('manifest_unavailable', 'the packaged manifest is missing from this '
                             'installation', searched=str(source))
    try:
        model = yaml.safe_load(data['compose.yaml'])
    except yaml.YAMLError as error:
        raise InstallRefused('manifest_invalid', 'the manifest is not valid YAML') from error
    if not isinstance(model, dict) or set(model.get('services') or {}) != SERVICES:
        raise InstallRefused('manifest_contract_changed', 'the manifest roles changed; review the '
                             'installer before rendering', expected=sorted(SERVICES))
    unmounted = _memory_unmounted(model)
    if unmounted:
        # apply moves the store into the volume; a manifest whose roles still see the
        # host directory at /state/memory would split it.
        raise InstallRefused('manifest_contract_changed', f'every role that mounts /state must mount the '
                             f'named volume {MEMORY_KEY!r} at {MEMORY_TARGET}; review the installer '
                             'before rendering', services=unmounted)
    return data, source / 'compose.yaml'


def _memory_unmounted(model):
    """Services that mount /state without the memory volume at /state/memory (and `volumes` if undeclared)."""
    declared = (model.get('volumes') or {}).get(MEMORY_KEY)
    missing = [] if isinstance(declared, dict) and declared.get('name') else ['volumes']
    for name, service in sorted((model.get('services') or {}).items()):
        mounts = [m for m in (service or {}).get('volumes') or () if isinstance(m, dict)]
        if not any(m.get('target') == '/state' for m in mounts):
            continue
        if not any(m.get('type') == 'volume' and m.get('source') == MEMORY_KEY and
                   m.get('target') == MEMORY_TARGET for m in mounts):
            missing.append(name)
    return missing


# ------------------------------------------------------------------- rendering

def _bind(source, target, *, read_only=True):
    return {'type': 'bind', 'source': source, 'target': target, 'read_only': read_only,
            'bind': {'create_host_path': False}}


def _yaml(header, value):
    return (header + yaml.safe_dump(value, sort_keys=False, default_flow_style=False, width=4096)).encode()


def _json(value):
    return (json.dumps(value, indent=2, sort_keys=True) + '\n').encode()


def render(inputs, deploy_files):
    """Every file the plan owns, as bytes, keyed by root-relative path."""
    root, project = inputs['runtime_root'], inputs['project_name']
    selected, repos = inputs['components'], inputs['repositories']
    container = project + '-tooling'
    compose_files = [f'{root}/{name}' for name in ('compose.yaml', *OVERLAYS)]
    env = [('AGENT_PROJECT_NAME', project), ('AGENT_TOOLING_IMAGE', inputs['image']),
           ('AGENT_ROOT', root), ('AGENT_UID', str(inputs['uid'])), ('AGENT_GID', str(inputs['gid'])),
           ('AGENT_BOARD_PORT', str(inputs['board_port'])), ('COMPOSE_FILE', ':'.join(compose_files)),
           ('COMPOSE_PROFILES', ','.join(name for name in selected if name != 'tooling'))]
    rendered = '# Rendered by kp-agent-install from a reviewed plan. Do not edit: apply and\n' \
               '# verify treat any change as drift. Re-plan and re-apply instead.\n'
    files = {'.env': (rendered + ''.join(f"{key}='{value}'\n" for key, value in env)).encode(),
             'compose.yaml': deploy_files['compose.yaml']}
    def workspaces():
        return [_bind(row['path'], f'/workspaces/{key}') for key, row in repos.items()]
    overlay = {'services': {'tooling': {'volumes': workspaces()}, 'refresh': {'volumes': workspaces()}}}
    header = '# Rendered by kp-agent-install: read-only repository binds for tooling and refresh.\n'
    if inputs['board_agents']:
        # --board-agents: the board's own CLIs work in task worktrees of these repositories.
        # Read-write at the host path, so worktree metadata names paths that exist on the
        # host too; the board's launch profiles shadow the host-launch profiles for it alone.
        overlay['services']['board'] = {'volumes': [
            *(_bind(row['path'], row['path'], read_only=False) for row in repos.values()),
            _bind(f'{root}/{BOARD_PROFILES}', HARNESS_PROFILES)]}
        header += ('# --board-agents: read-write repository binds at their host paths for board, and the\n'
                   f'# board launch profiles ({BOARD_PROFILES}) at {HARNESS_PROFILES} in board only.\n')
    files['compose.workspaces.yaml'] = _yaml(header, overlay)
    native = [_bind(path, path) for path in (*inputs['transcript_roots'],
                                             *(row['path'] for row in repos.values()))]
    files['compose.capture.yaml'] = _yaml(
        '# Rendered by kp-agent-install: native transcript roots and repositories for capture,\n'
        '# read-only at their host paths so recorded working directories resolve.\n',
        {'services': {'capture': {'volumes': native}}})
    files['config/navigation.json'] = _json({
        'schema_version': NAVIGATION_SCHEMA,
        'repos': {key: {'path': f'/workspaces/{key}', 'revision': row['revision']}
                  for key, row in repos.items()},
        'snapshot_registry': '/state/snapshots', 'navigation_registry_path': '/state/search',
        'delivery_root': '/state/delivery', 'enabled_tools': ENABLED_TOOLS})
    files['state/.ops-tooling-volume'] = (VOLUME_MARKER + '\n').encode()
    serve = ['exec', '-i', container, 'kp-agent-tooling', '--config', '/config/navigation.json', 'serve']
    files['host/claude-mcp.json'] = _json({'mcpServers': {project: {'command': 'docker', 'args': serve}}})
    files['host/codex-mcp.toml'] = (
        '# Rendered by kp-agent-install. Copy into the Codex configuration explicitly;\n'
        '# nothing is registered implicitly. MCP uses stdio: never add -t.\n'
        f'[mcp_servers.{project}]\ncommand = "docker"\n'
        f'args = [{", ".join(json.dumps(arg) for arg in serve)}]\n').encode()
    # kp-agent-host: launches exec into the capture container (it mounts the
    # workspaces and transcript roots at their host paths); hooks append to spool/.
    files['host/kp-agent-host.json'] = _json({
        'schema_version': HOST_ADAPTER_SCHEMA,
        'runtime': {'mode': 'docker', 'container': project + '-capture', 'config_path': HOST_LAUNCH_CONFIG},
        'spool_root': f'{root}/spool', 'transcript_roots': list(inputs['transcript_roots'])})
    # The profiles host launches use: the registry descriptor's harness_profiles_path
    # names /config/launch/harness-profiles.json (docs/HOST-ADAPTER.md).
    files['config/launch/harness-profiles.json'] = _json(
        launch_profiles(inputs['home'], inputs['transcript_roots'])[0])
    if 'board' in selected:
        # The profiles board task launches use; mounted for the board with --board-agents.
        files[BOARD_PROFILES] = _json(board_launch_profiles())
    if 'refresh' in selected:
        # Mount point for the credential inside the read-only configuration bind.
        files['config/github-token'] = b''
    if 'summarizer' in selected:
        # Mount point for the summarizer's provider key inside the read-only configuration bind.
        files[SUMMARIZER_KEY.removeprefix('/')] = b''
    if 'telemetry' in selected:
        files['telemetry/tempo.yaml'] = deploy_files['telemetry/tempo.yaml']
        files['telemetry/collector.yaml'] = deploy_files['telemetry/collector.yaml']
    directories = ['config', 'config/launch', 'host', 'spool', 'state', 'state/tmp', 'state/snapshots',
                   'state/search', 'state/delivery', 'state/models']
    for name, extra in (('refresh', ['secrets']), ('capture', ['config/capture']),
                        ('board', ['config/board', 'import-sources']), ('telemetry', ['telemetry', 'tempo']),
                        ('summarizer', ['secrets', 'config/summarizer'])):
        if name in selected:
            directories += extra
    return files, sorted(set(directories)), container, compose_files


# ------------------------------------------------------------- root inspection

def _lstat(path):
    try:
        return path.lstat()
    except FileNotFoundError:
        return None


def _kind(root, relative):
    """Classify root/relative without following any symlink on the way."""
    current = root
    parts = PurePosixPath(relative).parts
    for index, part in enumerate(parts):
        current = current / part
        info = _lstat(current)
        if info is None:
            return 'missing'
        if stat.S_ISLNK(info.st_mode):
            return 'symlink'
        if index < len(parts) - 1 and not stat.S_ISDIR(info.st_mode):
            return 'not-a-directory'
    if stat.S_ISREG(info.st_mode):
        return 'file'
    return 'directory' if stat.S_ISDIR(info.st_mode) else 'other'


def _mode(path):
    return path.lstat().st_mode & 0o777


def _relative(value):
    pure = PurePosixPath(value) if isinstance(value, str) else None
    return (pure is not None and value and not pure.is_absolute() and '..' not in pure.parts and
            str(pure) == value and value != '.')


def load_receipt(root):
    path = root / RECEIPT
    kind = _kind(root, RECEIPT)
    if kind != 'file':
        raise InstallRefused('receipt_invalid', f'receipt.json is {kind}', path=str(path))
    raw = path.read_bytes()
    try:
        value = json.loads(raw) if len(raw) <= MAX_RECEIPT_BYTES else None
    except (UnicodeDecodeError, json.JSONDecodeError):
        value = None
    valid = (isinstance(value, dict) and value.get('schema_version') == RECEIPT_SCHEMA and
             isinstance(value.get('plan_sha256'), str) and PLAN_SHA.fullmatch(value['plan_sha256']) and
             isinstance(value.get('files'), dict) and isinstance(value.get('directories'), dict) and
             all(_relative(name) and name != RECEIPT and isinstance(row, dict) and
                 isinstance(row.get('sha256'), str) and row.get('mode') == '0600'
                 for name, row in value['files'].items()) and
             all(_relative(name) and mode == '0700' for name, mode in value['directories'].items()) and
             _operator_rows_valid(value.get('operator_files', {}), value['files']) and
             isinstance(value.get('memory_store', {}), dict) and
             isinstance(value.get('memory_store', {}).get('migrations', []), list))
    if not valid:
        raise InstallRefused('receipt_invalid', 'receipt.json is not a receipt from this installer',
                             path=str(path))
    return value, raw


def _operator_rows_valid(rows, files):
    """Receipts before T7a carry no operator_files; a present table must be well formed."""
    def use(row):
        return row.get('component') in COMPONENTS and row.get('use') in OPERATOR_USES

    return isinstance(rows, dict) and all(
        _relative(name) and name != RECEIPT and name not in files and isinstance(row, dict) and
        row.get('owner') == 'operator' and use(row) and
        isinstance(row.get('also', []), list) and
        all(isinstance(extra, dict) and set(extra) == {'component', 'use'} and use(extra) and
            extra['use'] != 'placeholder' for extra in row.get('also', []))
        for name, row in rows.items())


def symlinks(root, planned):
    """Symlinks anywhere in the root, except inside container-owned runtime data."""
    found = []

    def walk(directory, prefix):
        with os.scandir(directory) as entries:
            for entry in sorted(entries, key=lambda item: item.name):
                name = f'{prefix}{entry.name}'
                owned = name.split('/', 1)[0] in RUNTIME_DATA and '/' in name
                if owned and name not in planned:
                    continue
                if entry.is_symlink():
                    found.append(name)
                elif entry.is_dir(follow_symlinks=False):
                    walk(entry.path, name + '/')

    walk(root, '')
    return found


def drift(root, receipt, desired=None):
    """Recorded paths that no longer match the receipt (or, during apply, the new plan)."""
    problems = []
    for name, row in sorted(receipt['files'].items()):
        kind = _kind(root, name)
        if kind != 'file':
            problems.append({'path': name, 'problem': kind})
            continue
        path = root / name
        data = path.read_bytes()
        if sha256(data) != row['sha256'] and (desired is None or desired.get(name) != data):
            problems.append({'path': name, 'problem': 'content'})
        elif _mode(path) != FILE_MODE:
            problems.append({'path': name, 'problem': 'mode', 'mode': oct(_mode(path))})
    for name in sorted(receipt['directories']):
        kind = _kind(root, name)
        if kind != 'directory':
            problems.append({'path': name, 'problem': kind})
        elif _mode(root / name) != DIRECTORY_MODE:
            problems.append({'path': name, 'problem': 'mode', 'mode': oct(_mode(root / name))})
    return problems




def operator_store_paths(root):
    """Operator JSON files under config/ that name a store path outside /state/memory.

    Read on the host (they are operator configuration, not store files); never rewritten.
    Each row: the root-relative file, the key, the path it names and the required path.
    """
    found = []
    if _kind(root, 'config') != 'directory':
        return found
    seen = 0
    for current, dirnames, filenames in os.walk(root / 'config'):
        dirnames[:] = sorted(name for name in dirnames if not (Path(current) / name).is_symlink())
        for name in sorted(filenames):
            path = Path(current) / name
            if not name.endswith('.json') or path.is_symlink():
                continue
            seen += 1
            if seen > 2000:
                return found
            try:
                if path.stat().st_size > MAX_OPERATOR_JSON:
                    continue
                value = json.loads(path.read_bytes())
            except (OSError, ValueError):
                continue
            if not isinstance(value, dict):
                continue
            for key in (*STORE_KEYS, TEMPLATE_KEY):
                required = leaf.required_store_path(key, value.get(key), reject_control=True)
                if required:
                    found.append({'file': path.relative_to(root).as_posix(), 'key': key,
                                  'path': value[key], 'required': required})
    return found


def store_directories(root, old_paths=()):
    """Pre-T9b store directories left on the bind, read with lstat and listings only.

    `state/memory` (its entries go to the volume's root), `state/registry`,
    `state/assistant`, `state/desk-memory`, and any other `state/<name>` an operator file
    names as a store path. An absent or empty directory has nothing to migrate (an empty
    `state/memory` is the mount point Docker created). No store file is opened.
    """
    names = list(LEGACY_STORES)
    for row in old_paths:
        path = leaf.state_path(row.get('path'), reject_control=True)
        if path is not None and _contains(PurePosixPath('/state'), path) and path != PurePosixPath('/state'):
            first = path.relative_to('/state').parts[0]
            if first not in NOT_STORES and '.migrated-' not in first and first not in names:
                names.append(first)
    rows = []
    for name in (MEMORY_KEY, *names):
        relative = f'state/{name}'
        kind = _kind(root, relative)
        if kind == 'missing':
            continue
        row = {'path': relative, 'target': MEMORY_TARGET if name == MEMORY_KEY else f'{MEMORY_TARGET}/{name}',
               'files': 0, 'directories': 0, 'bytes': 0}
        if kind != 'directory':
            rows.append({**row, 'invalid': f'{relative} is a {kind}, not a directory'})
            continue
        unsupported = []

        def walk(directory, prefix):
            with os.scandir(directory) as entries:
                for entry in sorted(entries, key=lambda item: item.name):
                    info = entry.stat(follow_symlinks=False)
                    if stat.S_ISDIR(info.st_mode):
                        row['directories'] += 1
                        walk(entry.path, prefix + entry.name + '/')
                    elif stat.S_ISREG(info.st_mode):
                        row['files'] += 1
                        row['bytes'] += info.st_size
                    else:
                        unsupported.append(prefix + entry.name)

        try:
            walk(root / relative, relative + '/')
        except OSError as error:
            rows.append({**row, 'invalid': f'{relative} cannot be listed: {type(error).__name__}: {error}'})
            continue
        if unsupported:
            row['invalid'] = (f'{relative} holds entries that are neither files nor directories: '
                              + ', '.join(unsupported[:10]))
        if row['files'] or row['directories'] or unsupported:
            rows.append(row)
    return rows


def preflight(root, files, directories, operator=None):
    """Decide every write, or refuse before the first one.

    An operator placeholder is created only where absent ('placeholder-create');
    an existing one is the operator's and is never read, replaced or hashed.
    """
    receipt = None
    if any(root.iterdir()):
        if _lstat(root / RECEIPT) is None:
            raise InstallRefused('runtime_root_not_empty', 'runtime root must be empty or a previous '
                                 'installation with its receipt.json', path=str(root))
        receipt, _ = load_receipt(root)
        problems = drift(root, receipt, files)
        if problems:
            raise InstallRefused('runtime_root_drift', 'runtime root does not match its receipt; '
                                 'run verify and resolve the drift first', drift=problems)
    planned = set(files) | set(directories) | {RECEIPT}
    links = symlinks(root, planned)
    if links:
        raise InstallRefused('symlink_in_root', 'symlinks are refused in the runtime root', symlinks=links)
    actions = {}
    for name in directories:
        kind = _kind(root, name)
        if kind not in ('missing', 'directory'):
            raise InstallRefused('differing_file', f'{name} must be a directory', path=name, found=kind)
        actions[name] = 'create' if kind == 'missing' else 'unchanged'
    recorded = receipt['files'] if receipt else {}
    for name, data in sorted(files.items()):
        kind = _kind(root, name)
        if kind == 'missing':
            actions[name] = 'create'
            continue
        if kind != 'file':
            raise InstallRefused('differing_file', f'{name} is {kind}', path=name)
        current = (root / name).read_bytes()
        if current == data and _mode(root / name) == FILE_MODE:
            actions[name] = 'unchanged'
        elif name in recorded and sha256(current) == recorded[name]['sha256']:
            actions[name] = 'replace'
        else:
            raise InstallRefused('differing_file', f'{name} exists and differs from both the plan and '
                                 'the receipt; it is never overwritten', path=name)
    for name, row in sorted((operator or {}).items()):
        if row['use'] != 'placeholder':
            continue
        kind = _kind(root, name)
        if kind not in ('missing', 'file'):
            raise InstallRefused('differing_file', f'{name} is operator-owned and must be a regular file '
                                 f'or absent; found {kind}', path=name)
        actions[name] = 'placeholder-create' if kind == 'missing' else 'operator-owned'
    return receipt, actions


# ------------------------------------------------------------------ operations

def board_agents_flag(value, selected):
    """`--board-agents`: a plan input that only the board component can take."""
    if type(value) is not bool:
        raise InstallRefused('board_agents_invalid', 'board_agents must be true or false', value=repr(value))
    if value and 'board' not in selected:
        raise InstallRefused('board_agents_without_board', '--board-agents mounts the repositories read-write '
                             'into the board role; select the board component (--components ...,board)',
                             components=selected)
    return value


def _build(runtime_root_path, image, repositories, component_list, uid, gid, port, project,
           transcript_roots=(), deploy=None, home=None, board_agents=False):
    # Syntactic checks first, then the filesystem; all before any decision to write.
    image = image_reference(image)
    selected = components(component_list)
    board_agents = board_agents_flag(board_agents, selected)
    uid, gid, port = identity(uid, 'uid'), identity(gid, 'gid'), board_port(port)
    project = project_name(project)
    root = runtime_root(runtime_root_path)
    home = home_directory(home)
    if not repositories:
        raise InstallRefused('repository_invalid', 'at least one --repository KEY=ABSOLUTE_PATH is required')
    repos = {}
    for spec in repositories:
        key, row = repository(spec, (home,))
        if key in repos:
            raise InstallRefused('repository_invalid', f'repository key {key} is repeated')
        repos[key] = row
    transcripts = sorted(transcript_root(raw, (home,)) for raw in transcript_roots or ())
    sources = [row['path'] for row in repos.values()] + transcripts
    if len(set(sources)) != len(sources):
        raise InstallRefused('overlap', 'each repository and transcript root must be a distinct path')
    for source in sources:
        if _contains(PurePosixPath(source), PurePosixPath(root)) or \
                _contains(PurePosixPath(root), PurePosixPath(source)):
            raise InstallRefused('overlap', 'the runtime root and a mounted source must not contain '
                                 'each other', path=source)
    inputs = {'runtime_root': str(root), 'image': image, 'components': selected,
              'repositories': dict(sorted(repos.items())), 'transcript_roots': transcripts,
              'uid': uid, 'gid': gid, 'board_port': port, 'project_name': project, 'home': home,
              'board_agents': board_agents}
    deploy_files, manifest_source = manifest(deploy)
    files, directories, container, compose_files = render(inputs, deploy_files)
    operator = operator_files(selected)
    desired = {'schema_version': PLAN_SCHEMA, 'inputs': inputs,
               'compose': {'project_name': project, 'tooling_container': container,
                           'files': compose_files,
                           'profiles': [name for name in selected if name != 'tooling']},
               'directories': {name: '0700' for name in directories},
               'files': {name: {'sha256': sha256(data), 'bytes': len(data), 'mode': '0600'}
                         for name, data in sorted(files.items())},
               'operator_files': operator,
               'memory_store': memory_store_spec(project, uid, gid),
               'receipt': RECEIPT}
    plan_sha256 = leaf.canonical_sha256(desired, ascii=True, allow_nan=False)
    receipt, actions = preflight(root, files, directories, operator)
    warnings = []
    if leaf.shared_bits(root.stat().st_mode):
        warnings.append('The runtime root itself is accessible to other users; its rendered '
                        'children are private, but consider chmod 700 on the root.')
    if (uid, gid) != (os.getuid(), os.getgid()):
        warnings.append('The rendered files are private (0600/0700) to the invoking user '
                        f'{os.getuid()}:{os.getgid()}; the containers run as {uid}:{gid}.')
    for harness, expanded in launch_profiles(home, transcripts)[1]:
        warnings.append(f'Harness profile {harness!r} is rendered disabled in config/launch/harness-profiles.json: '
                        f'its transcript root {expanded} is not under a planned --transcript-root, so host '
                        f'launches cannot be captured. To enable it, plan with --transcript-root {expanded}.')
    if board_agents:
        warnings.append('--board-agents: the board role mounts every planned repository read-write at its host '
                        'path, so board tasks and the agent CLIs they run can change them. Install the agents '
                        'image for in-container agents.')
    preview = {'covered_by_plan_sha256': False,
               'runtime_root_state': 'installed' if receipt else 'empty',
               'previous_plan_sha256': receipt['plan_sha256'] if receipt else None,
               'create': sorted(name for name, action in actions.items() if action == 'create'),
               'replace': sorted(name for name, action in actions.items() if action == 'replace'),
               'unchanged': sorted(name for name, action in actions.items() if action == 'unchanged'),
               'operator_placeholders': {
                   'create': sorted(n for n, action in actions.items() if action == 'placeholder-create'),
                   'left_in_place': sorted(n for n, action in actions.items() if action == 'operator-owned')},
               'manifest_source': str(manifest_source),
               'memory_store': memory_store_report(root, project, uid, gid, plan_sha256),
               'warnings': warnings,
               'not_verified': ['image presence, platform and revision label', 'container health',
                                'MCP connectivity and navigation results', 'host registration']}
    document = {**desired, 'plan_sha256': plan_sha256, 'preview': preview}
    return root, document, files, directories, actions, receipt


def plan(runtime_root, image, repositories, components, uid, gid, board_port, project_name,
         transcript_roots=(), deploy=None, home=None, board_agents=False):
    """The reviewed plan; writes nothing anywhere. ``home`` defaults to the invoking user's."""
    return _build(runtime_root, image, repositories, components, uid, gid, board_port, project_name,
                  transcript_roots, deploy, home, board_agents)[1]


def _write(path, data):
    return leaf.replace_file(path, data, temp_prefix='.kp-agent-install-', fchmod=True, cleanup='on-error')


def _rollback(root, created_directories, created_files, originals):
    """Undo one failed apply: restore replaced files, remove created files, then directories."""
    left_behind = []
    for name, data in originals.items():
        try:
            _write(root / name, data)
        except OSError:
            left_behind.append(name)
    for name in reversed(created_files):
        try:
            if os.path.lexists(root / name):
                os.unlink(root / name)
        except OSError:
            left_behind.append(name)
    for name in reversed(created_directories):
        try:
            os.rmdir(root / name)  # only ever empty: its files were removed above
        except OSError:
            left_behind.append(name)
    return {'restored': sorted(set(originals) - set(left_behind)),
            'removed': sorted((set(created_files) | set(created_directories)) - set(left_behind)),
            'left_behind': sorted(left_behind)}


# An empty 0600 file, only where nothing exists (O_EXCL): never a replacement.
def _placeholder(path):
    return leaf.create_new_empty(path, optional_flags=True, fchmod=True)


def receipt_bytes(document, memory=None):
    """The receipt; ``memory`` is the store record carried forward from the previous receipt or
    written by `prepare` (default: the plan's volume, never prepared)."""
    return _json({'schema_version': RECEIPT_SCHEMA, 'plan_sha256': document['plan_sha256'],
                  'image': document['inputs']['image'],
                  'project_name': document['inputs']['project_name'],
                  'components': document['inputs']['components'],
                  'tooling_container': document['compose']['tooling_container'],
                  'directories': document['directories'],
                  'files': {name: {'sha256': row['sha256'], 'mode': row['mode']}
                            for name, row in document['files'].items()},
                  'operator_files': document['operator_files'],
                  'memory_store': memory if memory is not None else memory_record(document['memory_store']),
                  'status': 'rendered-not-started',
                  'not_verified': document['preview']['not_verified']})


# ---------------------------------------------------------------- memory store

def memory_volume(project):
    """The project's store volume: Compose names volume key `memory` `<project>_memory`."""
    return f'{project}_memory'


def memory_store_spec(project, uid, gid):
    return {'volume': memory_volume(project), 'target': MEMORY_TARGET, 'owner': f'{uid}:{gid}', 'mode': '0700'}


def memory_record(spec, previous=None):
    """The receipt's `memory_store`: the plan's volume, its migrations, and the last successful
    `prepare` while the volume, owner and mode are unchanged. A record, never the test of
    "prepared" (that is observed: `prepare`, `verify`, the role preflight)."""
    previous = previous if isinstance(previous, dict) else {}
    history = previous.get('migrations') if isinstance(previous.get('migrations'), list) else []
    record = {**spec, 'migrations': history}
    last = previous.get('prepared')
    if isinstance(last, dict) and all(previous.get(key) == spec[key] for key in ('volume', 'owner', 'mode')):
        record['prepared'] = last
    return record


def memory_store_report(root, project, uid, gid, plan_sha256):
    """What `plan` and `apply` say about the store, from the filesystem alone (no Docker)."""
    old = operator_store_paths(root)
    directories = store_directories(root, old)
    report = {**memory_store_spec(project, uid, gid),
              'store_directories_on_bind': directories,
              'operator_files_naming_old_store_paths': old,
              'next': f'kp-agent-install prepare --runtime-root {json.dumps(str(root))} (after apply, before '
                      'up; Docker, the image present locally and no running container of the project)'}
    if directories:
        report['prepare_will_migrate'] = [{'from': row['path'], 'to': row['target'],
                                           'renamed_to': f"{row['path']}.migrated-{plan_sha256[:12]}"}
                                          for row in directories]
    if old:
        report['operator_edits'] = ('edit each listed file to its `required` path before `up`; the '
                                    'installer never rewrites an operator file, and roles refuse to start '
                                    'while one names a store path outside /state/memory')
    return report


class PrepareRefused(InstallRefused):
    """`prepare` refused with one of its named reasons; anything it had written was undone."""

    def __init__(self, reason, detail, *, undone=False, **facts):
        super().__init__(reason, detail, **facts)
        self.undone = undone

    def result(self):
        value = super().result()
        value['writes'] = 'undone: both sides as they were before this prepare' if self.undone else 'none'
        return value


class _StepFailed(RuntimeError):
    def __init__(self, reason, detail, **facts):
        super().__init__(detail)
        self.reason, self.detail, self.facts = reason, detail, facts


# Scripts run with `python3 -I -c` in one-off containers of the runtime image. They use only
# the standard library, so they do not depend on the image's installed package version.
_INSPECT = r'''
import json, os, stat
info = os.lstat("/volume")
try:
    names, listed = sorted(os.listdir("/volume")), True
except PermissionError:
    names, listed = [], False
print(json.dumps({"uid": info.st_uid, "gid": info.st_gid, "mode": stat.S_IMODE(info.st_mode),
                  "listed": listed, "entries": len(names), "names": names[:20]}))
'''

# The only root step: chown and chmod the volume root; nothing below it is touched.
_OWN = r'''
import json, os, stat, sys
wanted = json.loads(sys.argv[1])
os.chown("/volume", wanted["uid"], wanted["gid"])
os.chmod("/volume", wanted["mode"])
info = os.lstat("/volume")
print(json.dumps({"uid": info.st_uid, "gid": info.st_gid, "mode": stat.S_IMODE(info.st_mode)}))
'''

# Remove the named top-level entries of the volume; run as the store's owner, which owns
# every entry a migration created.
_CLEAR = r'''
import json, os, shutil, stat, sys
names = json.loads(sys.argv[1])["names"]
def writable(path):
    if stat.S_ISDIR(os.lstat(path).st_mode):
        os.chmod(path, 0o700)
        for name in os.listdir(path):
            writable(os.path.join(path, name))
left = []
for name in names:
    path = os.path.join("/volume", name)
    try:
        if os.path.lexists(path):
            writable(path)
            shutil.rmtree(path) if stat.S_ISDIR(os.lstat(path).st_mode) else os.unlink(path)
    except OSError:
        left.append(name)
print(json.dumps({"left": left}))
'''

# Shared by the source check and the migration: inventory a store directory; recover a
# database from a private copy (the database with its -wal, -shm and -journal, copied into
# the container's own /tmp and opened read-write there, so SQLite applies committed WAL
# frames and rolls back a hot journal; the read-only source is never opened by SQLite);
# and digest each ordinary table's content in primary-key (WITHOUT ROWID) or rowid order.
_STORE_LIB = r'''
import hashlib, json, os, sqlite3, stat, sys

SIDECARS = ("-wal", "-shm", "-journal")
UID, GID = os.getuid(), os.getgid()


class Refused(Exception):
    def __init__(self, reason, detail, database=None):
        super().__init__(detail)
        self.reason, self.detail, self.database = reason, detail, database


def emit(value, code):
    sys.stdout.write(json.dumps(value, sort_keys=True) + "\n")
    sys.stdout.flush()
    sys.exit(code)


def inventory(base):
    rows = []
    def walk(directory):
        with os.scandir(directory) as entries:
            for entry in sorted(entries, key=lambda item: item.name):
                info = entry.stat(follow_symlinks=False)
                kind = ("directory" if stat.S_ISDIR(info.st_mode) else "file" if stat.S_ISREG(info.st_mode)
                        else "other")
                rows.append({"path": os.path.relpath(entry.path, base), "kind": kind,
                             "mode": stat.S_IMODE(info.st_mode), "uid": info.st_uid, "gid": info.st_gid,
                             "size": info.st_size, "times": (info.st_atime_ns, info.st_mtime_ns)})
                if kind == "directory":
                    walk(entry.path)
    walk(base)
    other = [row["path"] for row in rows if row["kind"] == "other"]
    if other:
        raise Refused("copy_or_check_failed", "entries that are neither files nor directories: " + json.dumps(other[:20]))
    foreign = [row["path"] for row in rows if (row["uid"], row["gid"]) != (UID, GID)]
    if foreign:
        raise Refused("copy_or_check_failed", "entries not owned by %d:%d, whose owner the copy cannot preserve: %s"
                      % (UID, GID, json.dumps(foreign[:20])))
    return rows


def is_database(row):
    return row["kind"] == "file" and row["path"].endswith(".sqlite3")


def quick_check(db):
    return [[value.decode() if isinstance(value, bytes) else value for value in row]
            for row in db.execute("pragma quick_check").fetchall()]


def sidecar(path, databases):
    return any(path == name + suffix for name in databases for suffix in SIDECARS)


SCRATCH = os.path.join(os.environ.get("TMPDIR") or "/tmp", "kp-recover")
_copies = [0]


def _copy_file(source, target):
    with open(source, "rb") as reader, open(target, "xb") as writer:
        for chunk in iter(lambda: reader.read(1 << 20), b""):
            writer.write(chunk)


def discard(private):
    for suffix in ("",) + SIDECARS:
        if os.path.lexists(private + suffix):
            os.unlink(private + suffix)


def recover(path, label):
    """(connection, private path): a read-write connection to a private, recovered copy.

    The database and whichever of its -wal, -shm and -journal exist are copied byte for
    byte into the container's own /tmp; opening that copy read-write lets SQLite recover
    it as it would on the host after a crash. The source on the read-only bind is only
    read as bytes. A copy that cannot be opened, or fails quick_check after recovery, is
    `source_damaged`.
    """
    os.makedirs(SCRATCH, mode=0o700, exist_ok=True)
    _copies[0] += 1
    private = os.path.join(SCRATCH, "%d.sqlite3" % _copies[0])
    try:
        _copy_file(path, private)
        for suffix in SIDECARS:
            if os.path.lexists(path + suffix):
                _copy_file(path + suffix, private + suffix)
    except BaseException:
        discard(private)
        raise
    try:
        db = sqlite3.connect(private)
        db.text_factory = bytes
        quick = quick_check(db)
    except sqlite3.Error as error:
        discard(private)
        raise Refused("source_damaged", "cannot be opened, even after recovery on a private copy: " + str(error),
                      label)
    if quick != [["ok"]]:
        db.close()
        discard(private)
        raise Refused("source_damaged", "fails quick_check after recovery: " + repr(quick[:5])[:400], label)
    return db, private


def quoted(name):
    return '"' + name.replace('"', '""') + '"'


def digests(db):
    found = {}
    # Ordinary b-tree tables, including the shadow tables that hold an FTS index's data;
    # a virtual table's own rows live in those.
    tables = db.execute("select name, wr from pragma_table_list where schema = 'main' and type in ('table', "
                        "'shadow') order by name").fetchall()
    for name, without_rowid in tables:
        name = name.decode() if isinstance(name, bytes) else name
        columns = db.execute("pragma table_info(" + quoted(name) + ")").fetchall()
        if without_rowid:
            key = [row[1].decode() if isinstance(row[1], bytes) else row[1]
                   for row in sorted(columns, key=lambda row: row[5]) if row[5]]
            order = ", ".join(quoted(column) for column in key)
        else:
            order = "rowid"
        digest, count = hashlib.sha256(), 0
        for row in db.execute("select * from " + quoted(name) + " order by " + order):
            digest.update(repr(row).encode() + b"\n")
            count += 1
        found[name] = {"rows": count, "sha256": digest.hexdigest()}
    return found
'''

# The read-only check before anything is created: every source directory is inventoried
# and every database passes quick_check, read-only. /source/<n> are the store directories.
_CHECK = _STORE_LIB + r'''
sources = json.loads(sys.argv[1])["sources"]
try:
    for index, label in enumerate(sources):
        base = "/source/%d" % index
        rows = inventory(base)
        databases = {row["path"] for row in rows if is_database(row)}
        for path in sorted(databases):
            db, private = recover(os.path.join(base, path), label + "/" + path)
            db.close()
            discard(private)
    emit({"status": "ok"}, 0)
except SystemExit:
    raise
except Refused as error:
    emit({"status": "refused", "reason": error.reason, "detail": error.detail, "database": error.database}, 4)
except BaseException as error:
    emit({"status": "refused", "reason": "copy_or_check_failed", "detail": type(error).__name__ + ": " + str(error)[:500]}, 4)
'''

# The migration, as AGENT_UID:AGENT_GID with no capability and no network. /source/<n> are
# the store directories (read-only), /volume the empty volume. Everything is staged in the
# volume and verified there: the bytes of every file, and for each *.sqlite3 a copy made with
# the backup API from its recovered private copy, which must pass quick_check and match that
# copy's per-table content digests. No -wal, -shm or -journal file is ever staged.
# Then the staged entries move into place and take the source modes. A failure removes
# everything this run created.
_MIGRATE = _STORE_LIB + r'''
import shutil
VOLUME = "/volume"
params = json.loads(sys.argv[1])
STAGE = os.path.join(VOLUME, params["stage"])
created, moved = [], []


def writable(path):
    if stat.S_ISDIR(os.lstat(path).st_mode):
        os.chmod(path, 0o700)
        for name in os.listdir(path):
            writable(os.path.join(path, name))


def cleanup():
    left = []
    for path in [os.path.join(VOLUME, name) for name in moved] + created:
        try:
            if os.path.lexists(path):
                writable(path)
                shutil.rmtree(path) if stat.S_ISDIR(os.lstat(path).st_mode) else os.unlink(path)
        except OSError:
            left.append(os.path.relpath(path, VOLUME))
    return left


def copy_bytes(source, target):
    digest = hashlib.sha256()
    with open(source, "rb") as reader, open(target, "xb") as writer:
        for chunk in iter(lambda: reader.read(1 << 20), b""):
            digest.update(chunk)
            writer.write(chunk)
        writer.flush()
        os.fsync(writer.fileno())
    return digest.hexdigest()


def file_digest(path):
    digest = hashlib.sha256()
    with open(path, "rb") as reader:
        for chunk in iter(lambda: reader.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def copy_database(source, target, label):
    origin, private = recover(source, label)
    try:
        expected = digests(origin)
        copy = sqlite3.connect(target)
        origin.backup(copy)
        copy.close()
    finally:
        origin.close()
        discard(private)
    check = sqlite3.connect(target)
    check.text_factory = bytes
    quick = quick_check(check)
    found = digests(check)
    check.close()
    for suffix in SIDECARS:
        if os.path.lexists(target + suffix):
            if suffix == "-shm" or os.path.getsize(target + suffix) == 0:
                os.unlink(target + suffix)
            else:
                raise Refused("copy_or_check_failed", "the copy of " + label + " left a non-empty " + suffix)
    if quick != [["ok"]]:
        raise Refused("copy_or_check_failed", "the copy of " + label + " fails quick_check: " + repr(quick[:5])[:400])
    if found != expected:
        differ = sorted(name for name in set(found) | set(expected) if found.get(name) != expected.get(name))
        raise Refused("copy_or_check_failed", "the copy of " + label + " differs from its source in tables "
                      + json.dumps(differ[:20]))
    return expected


step, path = "precondition", None
try:
    if os.listdir(VOLUME):
        emit({"status": "refused", "reason": "host_files_beside_populated_volume", "step": step,
              "detail": "the volume is not empty"}, 4)
    os.mkdir(STAGE, 0o700)
    created.append(STAGE)
    report, manifest, expected, total = {}, [], {}, 0
    for index, store in enumerate(params["stores"]):
        step, base = "inventory", "/source/%d" % index
        rows = inventory(base)
        databases = {row["path"] for row in rows if is_database(row)}
        prefix = store["target"]
        if prefix:
            os.mkdir(os.path.join(STAGE, prefix), 0o700)
            expected[prefix] = {"kind": "directory", "mode": store["mode"], "uid": UID, "gid": GID}
        step = "copy"
        for row in rows:
            path = row["path"]
            final = os.path.join(prefix, path) if prefix else path
            target = os.path.join(STAGE, final)
            label = store["from"] + "/" + path
            if row["kind"] == "directory":
                os.mkdir(target, 0o700)
                manifest.append("d %o %s" % (row["mode"], final))
            elif sidecar(path, databases):
                continue  # never copied: the backup API reads the database with it
            elif path in databases:
                tables = copy_database(os.path.join(base, path), target, label)
                report[final] = {"from": label, "quick_check": "ok", "tables": tables}
                manifest.append("db %o %s %s" % (row["mode"], final, json.dumps(tables, sort_keys=True)))
                total += row["size"]
            else:
                digest = copy_bytes(os.path.join(base, path), target)
                os.utime(target, ns=row["times"])
                if file_digest(target) != digest:
                    raise Refused("copy_or_check_failed", "the bytes of " + label + " differ after copying")
                manifest.append("f %o %s %s" % (row["mode"], final, digest))
                total += row["size"]
            expected[final] = row
        path = None
    step = "check"
    staged = set()
    for current, directories, files in os.walk(STAGE):
        for name in directories + files:
            staged.add(os.path.relpath(os.path.join(current, name), STAGE))
    if staged != set(expected):
        raise Refused("copy_or_check_failed", "staged entries differ from the sources: missing "
                      + json.dumps(sorted(set(expected) - staged)[:20]) + ", extra "
                      + json.dumps(sorted(staged - set(expected))[:20]))
    step = "move"
    for name in sorted(os.listdir(STAGE)):
        os.rename(os.path.join(STAGE, name), os.path.join(VOLUME, name))
        moved.append(name)
    os.rmdir(STAGE)
    created.remove(STAGE)
    step = "modes"
    for path in sorted(expected, key=lambda name: (-name.count("/"), name)):
        row, target = expected[path], os.path.join(VOLUME, path)
        os.chmod(target, row["mode"])
        info = os.lstat(target)
        if (stat.S_IMODE(info.st_mode), info.st_uid, info.st_gid) != (row["mode"], UID, GID):
            raise Refused("copy_or_check_failed", "owner or mode of " + path + " differs from its source")
    path = None
    emit({"status": "migrated", "entries": len(expected), "bytes": total, "databases": report,
          "files": sum(1 for row in expected.values() if row["kind"] == "file"),
          "directories": sum(1 for row in expected.values() if row["kind"] == "directory"),
          "sidecars_not_copied": "every -wal, -shm and -journal file",
          "manifest_sha256": hashlib.sha256("\n".join(manifest).encode()).hexdigest()}, 0)
except SystemExit:
    raise
except BaseException as error:
    left = cleanup()
    reason = error.reason if isinstance(error, Refused) else "copy_or_check_failed"
    detail = error.detail if isinstance(error, Refused) else type(error).__name__ + ": " + str(error)[:500]
    emit({"status": "refused", "reason": reason, "step": step, "path": path, "detail": detail,
          "database": getattr(error, "database", None), "cleanup": "incomplete" if left else "complete",
          "left": left}, 3)
'''

SOURCE_RECOVERY = ('docs/DOCKER.md, "A damaged source database": with every role stopped, restore that '
                   'database from the backup taken before the upgrade, or move it and its -wal, -shm and '
                   '-journal files out of the store directory with a one-off container, then re-run prepare')


class _Docker:
    """The docker CLI of the invoking user's environment (DOCKER_HOST, DOCKER_CONTEXT, ...)."""

    def __init__(self):
        self.executable = shutil.which('docker')

    def __call__(self, *args, timeout=120):
        if self.executable is None:
            raise FileNotFoundError('the docker CLI is not on PATH')
        return subprocess.run([self.executable, *args], capture_output=True, text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL)


def _last_json(text):
    for line in reversed((text or '').strip().splitlines()):
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return None


class StoreVolume:
    """Docker access to one project's store volume: observe it, and (for `prepare`) write it."""

    def __init__(self, root, project, image, uid, gid, docker=None):
        self.root, self.project, self.image, self.uid, self.gid = root, project, image, uid, gid
        self.volume = memory_volume(project)
        self.docker = docker or _Docker()

    def reachable(self):
        """None, or why Docker cannot be used."""
        try:
            server = self.docker('version', '--format', '{{.Server.Version}}', timeout=60)
        except (OSError, subprocess.SubprocessError) as error:
            return f'{type(error).__name__}: {error}'[:300]
        return None if server.returncode == 0 else (server.stderr.strip() or server.stdout.strip())[:300]

    def image_present(self):
        try:
            return self.docker('image', 'inspect', '--format', '{{.Id}}', self.image, timeout=60).returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    def running(self):
        """Running containers of the project (one-off `compose run` containers carry its label too),
        and any running container that mounts the volume: {name: service or None}."""
        found = {}
        for selector in (f'label={COMPOSE_PROJECT_LABEL}={self.project}', f'volume={self.volume}'):
            proc = self.docker('ps', '--filter', selector, '--format',
                               '{{.Names}}\t{{.Label "com.docker.compose.service"}}', timeout=60)
            if proc.returncode:
                raise OSError(f'docker ps failed: {proc.stderr.strip()[:300]}')
            for line in proc.stdout.splitlines():
                name, _, service = line.partition('\t')
                if name:
                    found[name] = service or None
        return found

    def container(self, step, script, *, user, caps=(), mounts=(), argument=None, timeout=120, scratch=False):
        """A one-off container of the runtime image. With ``scratch`` its own /tmp is the
        container's writable layer (on the Docker disk, removed with the container), where
        databases are recovered from private copies; otherwise the root is read-only."""
        name = f'{self.project}-memory-{step}-{secrets.token_hex(4)}'
        private = [] if scratch else ['--read-only', '--tmpfs', '/tmp:rw,nosuid,nodev,mode=1777']
        argv = ['run', '--rm', '--name', name, '--pull', 'never', '--network', 'none', *private,
                '-e', 'TMPDIR=/tmp', '-e', 'SQLITE_TMPDIR=/tmp', '--user', user, '--cap-drop', 'ALL', *(arg for cap in caps for arg in ('--cap-add', cap)),
                '--security-opt', 'no-new-privileges', '--label', f'agent-tooling.install.project={self.project}',
                '--label', f'agent-tooling.install.step=memory-{step}',
                *(arg for mount in mounts for arg in ('-v', mount)),
                '--entrypoint', 'python3', self.image, '-I', '-c', script, json.dumps(argument or {})]
        try:
            proc = self.docker(*argv, timeout=timeout)
        except BaseException:
            # A killed CLI leaves the container running: remove it before anything is undone.
            try:
                self.docker('rm', '-f', name, timeout=60)
            except (OSError, subprocess.SubprocessError):
                pass
            raise
        return proc, _last_json(proc.stdout)

    def labels(self):
        """The volume's labels, or None when it does not exist."""
        proc = self.docker('volume', 'inspect', '--format', '{{json .Labels}}', self.volume, timeout=60)
        if proc.returncode:
            if 'no such volume' in (proc.stderr + proc.stdout).lower():
                return None
            raise OSError(f'docker volume inspect failed: {proc.stderr.strip()[:300]}')
        return json.loads(proc.stdout or 'null') or {}

    def ours(self, labels):
        return (labels.get(COMPOSE_PROJECT_LABEL), labels.get(COMPOSE_VOLUME_LABEL)) == (self.project, MEMORY_KEY)

    def inspect(self):
        """The volume root's owner, mode and entries, read in a container as AGENT_UID (no capability)."""
        proc, state = self.container('inspect', _INSPECT, user=f'{self.uid}:{self.gid}',
                                     mounts=(f'{self.volume}:/volume:ro',))
        if proc.returncode or state is None:
            raise OSError(f'the volume {self.volume} could not be inspected: '
                          f'{(proc.stderr.strip() or proc.stdout.strip())[:300]}')
        return state

    def owned(self, state):
        return state is not None and (state['uid'], state['gid'], state['mode']) == (self.uid, self.gid, MEMORY_MODE)

    def observe(self, directories):
        """The single "prepared" test: the volume exists, its root is AGENT_UID:AGENT_GID 0700, and
        no pre-T9b store directory is left on the bind. Returns (prepared, reasons, volume state)."""
        reasons = [f"{row['path']} is still on the bind (prepare moves it to {row['target']})" for row in directories]
        labels = self.labels()
        if labels is None:
            return False, [f'the volume {self.volume} does not exist', *reasons], None
        if not self.ours(labels):
            return False, [f'the volume {self.volume} is not the `memory` volume of project {self.project}',
                           *reasons], None
        state = self.inspect()
        if not self.owned(state):
            reasons.insert(0, f"the volume root is {state['uid']}:{state['gid']} mode {oct(state['mode'])}, not "
                              f'{self.uid}:{self.gid} mode 0o700')
        return not reasons, reasons, state


class Preparation:
    """One `prepare` run's writes, so a refusal after a write can undo them."""

    def __init__(self, store, root, plan_sha256):
        self.store, self.root, self.plan_sha256 = store, root, plan_sha256
        self.created, self.owner_from, self.moved_names, self.renamed = False, None, [], []

    def own(self, uid, gid, mode):
        proc, state = self.store.container('own', _OWN, user='0:0', caps=('CHOWN', 'FOWNER'),
                                           mounts=(f'{self.store.volume}:/volume',),
                                           argument={'uid': uid, 'gid': gid, 'mode': mode})
        if proc.returncode or state != {'uid': uid, 'gid': gid, 'mode': mode}:
            raise _StepFailed('copy_or_check_failed', f'setting the owner and mode of {self.store.volume} failed: '
                              f'{(proc.stderr.strip() or proc.stdout.strip())[:300]}')

    def create(self):
        proc = self.store.docker('volume', 'create', '--label', f'{COMPOSE_PROJECT_LABEL}={self.store.project}',
                                 '--label', f'{COMPOSE_VOLUME_LABEL}={MEMORY_KEY}', self.store.volume, timeout=60)
        if proc.returncode:
            raise _StepFailed('copy_or_check_failed', f'docker volume create {self.store.volume} failed: '
                              f'{proc.stderr.strip()[:300]}')
        self.created = True

    def migrate(self, directories):
        stores = [{'from': row['path'], 'target': '' if row['path'] == MEMORY_HOST else row['target'].split('/')[-1],
                   'mode': stat.S_IMODE((self.root / row['path']).lstat().st_mode)} for row in directories]
        # The top-level entries this copy will create in the volume, so undo can remove them.
        names = set()
        for store, row in zip(stores, directories):
            names.update(os.listdir(self.root / row['path']) if not store['target'] else [store['target']])
        self.moved_names = sorted(names)
        mounts = [f'{self.root / row["path"]}:/source/{index}:ro' for index, row in enumerate(directories)]
        proc, report = self.store.container(
            'migrate', _MIGRATE, user=f'{self.store.uid}:{self.store.gid}', timeout=None, scratch=True,
            mounts=(*mounts, f'{self.store.volume}:/volume'),
            argument={'stage': f'.kp-migration-{self.plan_sha256[:12]}', 'stores': stores})
        if proc.returncode or not report or report.get('status') != 'migrated':
            report = report or {'stderr': proc.stderr.strip()[-600:], 'exit': proc.returncode}
            reason = report.get('reason') if report.get('reason') in (
                'source_damaged', 'host_files_beside_populated_volume') else 'copy_or_check_failed'
            facts = {'database': report['database'], 'recovery': SOURCE_RECOVERY} if reason == 'source_damaged' else {}
            raise _StepFailed(reason, 'the migration did not complete: ' + json.dumps(report, sort_keys=True)[:900],
                              **facts)
        for row in directories:
            target = self.root / f"{row['path']}.migrated-{self.plan_sha256[:12]}"
            os.rename(self.root / row['path'], target)
            self.renamed.append((row['path'], target))
        return report

    def undo(self):
        """Return the bind and the volume to their state before this run; list what is left."""
        left = []
        for original, target in reversed(self.renamed):
            try:
                os.rename(target, self.root / original)
            except OSError:
                left.append(str(target.relative_to(self.root)))
        if self.created:
            proc = self.store.docker('volume', 'rm', '-f', self.store.volume, timeout=120)
            if proc.returncode:
                left.append(f'volume:{self.store.volume}')
            return left
        if self.moved_names:
            proc, state = self.store.container('clear', _CLEAR, user=f'{self.store.uid}:{self.store.gid}',
                                               mounts=(f'{self.store.volume}:/volume',),
                                               argument={'names': [*self.moved_names,
                                                                   f'.kp-migration-{self.plan_sha256[:12]}']})
            if proc.returncode or not state or state.get('left'):
                left.append(f'volume:{self.store.volume} (entries this prepare copied)')
        if self.owner_from:
            try:
                self.own(*self.owner_from)
            except _StepFailed:
                left.append(f'volume:{self.store.volume} (root owner and mode)')
        return left


def prepare(runtime_root_path, docker=None):
    """Make the root's store "prepared": create and own the volume, then migrate (P3, P4).

    0. Already prepared: report `prepared`, change nothing, no stop needed.
    1. `writers_running`; 2. `docker_unreachable`; 3. `image_absent` (inspect, never pull).
    4. Create the volume if absent; a one-off root container chowns and chmods its root.
    5. Migrate every pre-T9b store directory: `source_damaged`, `host_files_beside_populated_volume`
       and `copy_or_check_failed` refuse with both sides as they were.
    6. Record the outcome in the receipt.
    """
    root = runtime_root(runtime_root_path)
    receipt, _ = load_receipt(root)
    problems = drift(root, receipt)
    if problems:
        raise InstallRefused('runtime_root_drift', 'runtime root does not match its receipt; run verify and '
                             'resolve the drift first', drift=problems)
    spec = receipt.get('memory_store') or {}
    try:
        uid, gid = (int(part) for part in str(spec.get('owner', '')).split(':'))
    except ValueError:
        uid = gid = None
    if spec.get('volume') != memory_volume(receipt['project_name']) or uid is None:
        raise InstallRefused('receipt_outdated', 'receipt.json was written by an installer without the store '
                             'volume; re-run plan and apply with this installer first')
    old = operator_store_paths(root)
    directories = store_directories(root, old)
    store = StoreVolume(root, receipt['project_name'], receipt['image'], uid, gid, docker)
    base = {'runtime_root': str(root), 'volume': store.volume, 'target': MEMORY_TARGET,
            'owner': spec['owner'], 'mode': '0700', 'operator_files_naming_old_store_paths': old}

    # 0. Already prepared: observed, never taken from the receipt.
    unreachable = store.reachable()
    if unreachable is None and store.image_present():
        try:
            prepared, _, _ = store.observe(directories)
        except (OSError, subprocess.SubprocessError, ValueError):
            prepared = False
        if prepared:
            return {**base, 'status': 'prepared', 'changed': False, 'actions': []}
    # 1-3, only when work is needed. Docker is needed to see the writers at all.
    if unreachable is not None:
        raise PrepareRefused('docker_unreachable', f'Docker is unreachable: {unreachable}', **base)
    try:
        running = store.running()
    except (OSError, subprocess.SubprocessError) as error:
        raise PrepareRefused('docker_unreachable', f'{type(error).__name__}: {error}'[:300], **base) from error
    if running:
        raise PrepareRefused('writers_running', 'containers of the project are running; prepare never writes the '
                             'store while one does (host processes cannot be detected: stop them yourself)',
                             running_services=sorted({s for s in running.values() if s}),
                             running_containers=sorted(running),
                             stop=f'docker compose --project-directory {json.dumps(str(root))} stop', **base)
    if not store.image_present():
        raise PrepareRefused('image_absent', f'the runtime image {store.image} is not present locally; prepare '
                             'never pulls it (build or pull it, then re-run)', **base)
    # Read-only checks before anything is created.
    try:
        labels = store.labels()
        if labels is not None and not store.ours(labels):
            raise PrepareRefused('copy_or_check_failed', f'volume {store.volume} exists but is not the `memory` '
                                 f'volume of Compose project {store.project}; it is never adopted',
                                 labels=labels, **base)
        state = store.inspect() if labels is not None else None
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        raise PrepareRefused('copy_or_check_failed', f'the volume could not be inspected: {error}'[:400],
                             **base) from error
    invalid = [row for row in directories if row.get('invalid')]
    if invalid:
        raise PrepareRefused('copy_or_check_failed', 'a store directory on the bind cannot be migrated as it is',
                             store_directories_on_bind=invalid, **base)
    if directories and state is not None and (state['entries'] or not state['listed']):
        raise PrepareRefused('host_files_beside_populated_volume', 'store directories on the bind hold files and '
                             f'the volume {store.volume} already holds a store; nothing is copied over a '
                             'populated volume. Move the host files out of the listed directories, or, if the '
                             'volume holds an incomplete copy, remove the volume while no container runs',
                             store_directories_on_bind=directories, volume_entries=state['entries'],
                             volume_names=state['names'], **base)
    collisions = [row['path'] for row in directories if row['path'] != MEMORY_HOST and
                  _kind(root, f"{MEMORY_HOST}/{row['target'].split('/')[-1]}") != 'missing']
    taken = [f"{row['path']}.migrated-{receipt['plan_sha256'][:12]}" for row in directories
             if _kind(root, f"{row['path']}.migrated-{receipt['plan_sha256'][:12]}") != 'missing']
    if collisions or taken:
        raise PrepareRefused('copy_or_check_failed', 'two store directories would land on the same volume path, '
                             'or a kept .migrated- directory already has the name this run needs',
                             colliding=collisions, migrated_names_taken=taken, **base)
    if directories:
        proc, checked = store.container('check', _CHECK, user=f'{uid}:{gid}', scratch=True,
                                        mounts=[f'{root / row["path"]}:/source/{index}:ro'
                                                for index, row in enumerate(directories)],
                                        argument={'sources': [row['path'] for row in directories]}, timeout=None)
        if proc.returncode or not checked or checked.get('status') != 'ok':
            checked = checked or {'detail': proc.stderr.strip()[-400:]}
            reason = checked.get('reason') if checked.get('reason') == 'source_damaged' else 'copy_or_check_failed'
            facts = {'database': checked.get('database'), 'recovery': SOURCE_RECOVERY} if reason == 'source_damaged' else {}
            raise PrepareRefused(reason, checked.get('detail') or 'the source check failed', **facts, **base)
    # 4-6: writes, undone on any failure.
    run = Preparation(store, root, receipt['plan_sha256'])
    try:
        if labels is None:
            run.create()
        if not store.owned(state):
            if state is not None:
                run.owner_from = (state['uid'], state['gid'], state['mode'])
            run.own(uid, gid, MEMORY_MODE)
        migration = run.migrate(directories) if directories else None
        record = memory_record({key: spec[key] for key in ('volume', 'target', 'owner', 'mode')}, spec)
        if migration is not None:
            record['migrations'] = [*record['migrations'], {
                'plan_sha256': receipt['plan_sha256'],
                'moved': [{'from': row['path'], 'to': row['target'],
                           'renamed_to': f"{row['path']}.migrated-{receipt['plan_sha256'][:12]}"} for row in directories],
                **{key: migration[key] for key in ('entries', 'files', 'directories', 'bytes', 'manifest_sha256',
                                                   'databases')}}]
        record['prepared'] = {'plan_sha256': receipt['plan_sha256'], 'volume': store.volume, 'owner': spec['owner'],
                              'mode': '0700'}
        updated = json.loads(_json(receipt))
        updated['memory_store'] = record
        _write(root / RECEIPT, _json(updated))
    except BaseException as error:
        try:
            left = run.undo()
        except BaseException as undo_error:  # never mask the first failure
            left = [f'undo failed: {type(undo_error).__name__}']
        if isinstance(error, _StepFailed) and not left:
            raise PrepareRefused(error.reason, error.detail, undone=True, **error.facts, **base) from error
        if not left and isinstance(error, Exception):
            raise PrepareRefused('copy_or_check_failed', f'{type(error).__name__}: {error}'[:400], undone=True,
                                 **base) from error
        raise ApplyFailed(error, {'restored': [], 'removed': [], 'left_behind': left}) from error
    actions = [name for name, done in (('volume_created', run.created),
                                       ('owner_set', run.created or run.owner_from or not store.owned(state)),
                                       ('migrated', migration is not None)) if done]
    result = {**base, 'status': 'prepared', 'changed': True, 'actions': actions}
    if migration is not None:
        result['migration'] = record['migrations'][-1]
    if old:
        result['next'] = ('edit each file in operator_files_naming_old_store_paths to its `required` path; roles '
                          'refuse to start until then')
    return result


def apply(expected_plan_sha256, runtime_root, image, repositories, components, uid, gid, board_port,
          project_name, transcript_roots=(), deploy=None, home=None, board_agents=False):
    """Write the reviewed plan; any refusal happens before the first write."""
    if not isinstance(expected_plan_sha256, str) or not PLAN_SHA.fullmatch(expected_plan_sha256):
        raise InstallRefused('plan_mismatch', '--expected-plan-sha256 must be the 64-hex plan_sha256')
    root, document, files, directories, actions, _ = _build(
        runtime_root, image, repositories, components, uid, gid, board_port, project_name,
        transcript_roots, deploy, home, board_agents)
    if document['plan_sha256'] != expected_plan_sha256:
        raise InstallRefused('plan_mismatch', 'inputs, repositories or manifest changed since the reviewed '
                             'plan; review a new plan', expected=expected_plan_sha256,
                             actual=document['plan_sha256'])
    previous_receipt = (root / RECEIPT).read_bytes() if _kind(root, RECEIPT) == 'file' else None
    previous_memory = json.loads(previous_receipt).get('memory_store') if previous_receipt is not None else None
    # The store record (migrations, the last successful prepare) is carried forward; apply
    # itself never calls Docker.
    receipt = receipt_bytes(document, memory_record(document['memory_store'], previous_memory))
    receipt_changed = previous_receipt != receipt
    written = {'create': [], 'replace': [], 'placeholder': []}
    # Each file is written to a temporary sibling and renamed into place. On any
    # failure, everything this apply created is removed and every replaced file is
    # restored, so a failed first apply leaves the root empty again.
    created_directories, created_files, originals = [], [], {}
    try:
        for name in directories:
            if actions[name] == 'create':
                leaf.mkdir_private(root / name)
                created_directories.append(name)
                leaf.chmod_private(root / name, directory=True)
                written['create'].append(name)
        for name, data in sorted(files.items()):
            if actions[name] == 'replace':
                originals[name] = (root / name).read_bytes()
            if actions[name] in ('create', 'replace'):
                _write(root / name, data)
                if actions[name] == 'create':
                    created_files.append(name)
                written[actions[name]].append(name)
        for name in sorted(n for n, action in actions.items() if action == 'placeholder-create'):
            _placeholder(root / name)
            created_files.append(name)
            written['placeholder'].append(name)
        if receipt_changed:
            if previous_receipt is None:
                created_files.append(RECEIPT)
            else:
                originals[RECEIPT] = previous_receipt
            _write(root / RECEIPT, receipt)
    except BaseException as error:
        raise ApplyFailed(error, _rollback(root, created_directories, created_files, originals)) from error
    changed = bool(written['create'] or written['replace'] or written['placeholder'] or receipt_changed)
    quoted = json.dumps(str(root))
    return {'status': 'applied' if changed else 'unchanged', 'changed': changed,
            'plan_sha256': document['plan_sha256'], 'runtime_root': str(root),
            'created': written['create'], 'replaced': written['replace'],
            'operator_placeholders_created': written['placeholder'],
            'unchanged': sorted(name for name, action in actions.items() if action == 'unchanged'),
            'receipt_sha256': sha256(receipt),
            'memory_store': document['preview']['memory_store'],
            'tooling_container': document['compose']['tooling_container'],
            'host_registration': 'rendered-not-installed', 'containers': 'not-started',
            'next': f'kp-agent-install prepare --runtime-root {quoted}; then edit any operator file listed '
                    f'under memory_store; then docker compose --project-directory {quoted} up -d; then '
                    f'kp-agent-install verify --runtime-root {quoted} lists the operator '
                    'files each role still waits for (readiness)'}


def readiness(root, receipt):
    """Per selected component: which operator files are unwritten. Presence only, never content.

    A role whose required files are absent is `not_configured`: it stays up and waits
    for them. An absent placeholder cannot be bound, so that role cannot be created
    until apply recreates the placeholder or the operator writes the file. A sub-entry
    (`host_launch` for capture, `desks` for board) reports a feature of a role that
    keeps running without its file.
    """
    selected = [name for name in COMPONENTS if name in (receipt.get('components') or ())]
    declared = receipt.get('operator_files')
    if declared is None:  # a receipt written before operator files were recorded
        declared = operator_files(selected)
    report = {}
    for component in selected:
        rows = {name: use for name, owner, use in _operator_uses(declared) if owner == component}

        def absent(wanted):
            # A placeholder is written once it exists; any other operator file once it is non-empty.
            return sorted(name for name, use in rows.items() if use == wanted and (
                _kind(root, name) != 'file' or (use != 'placeholder' and (root / name).stat().st_size == 0)))

        missing = absent('required') + absent('placeholder')
        entry = {'status': 'not_configured' if missing else 'ready', 'missing': sorted(missing),
                 'operator_files': sorted(name for name, use in rows.items() if use != 'optional')}
        empty = sorted(name for name, use in rows.items() if use == 'placeholder'
                       and _kind(root, name) == 'file' and (root / name).stat().st_size == 0)
        if empty:
            entry['empty_placeholders'] = empty
        if absent('placeholder'):
            entry['cannot_create'] = absent('placeholder')
        for feature in SUB_ENTRIES:
            if feature in rows.values():
                unwritten = absent(feature)
                entry[feature] = {'status': 'not_configured' if unwritten else 'ready', 'missing': unwritten}
        if absent('optional'):
            entry['optional_missing'] = absent('optional')
        report[component] = entry
    return report


def volume_presence(store):
    """Whether the store volume is present, from `docker volume inspect` alone (the image is
    absent, so the root's owner and mode are not observed): a reason, never a status."""
    try:
        labels = store.labels()
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        return f'whether the volume {store.volume} is present could not be observed: {error}'[:300]
    if labels is None:
        return f'the volume {store.volume} is not present'
    if not isinstance(labels, dict) or not store.ours(labels):
        return f'the volume {store.volume} is present but is not the `memory` volume of project {store.project}'
    return f'the volume {store.volume} is present (its owner and mode are not observed without the image)'


def memory_readiness(root, receipt, docker=None):
    """The store as a readiness state: `prepared`, `unprepared` or `not_observed` (never drift).

    The Definition as observed; the receipt's `prepared` entry is a record and never decides:
    - a pre-T9b store directory left on the bind: `unprepared`, seen on the host, no Docker;
    - else, Docker reachable and the receipt's image present: the volume is observed,
      `prepared` or `unprepared` with its reasons;
    - else `not_observed`, naming why (the receipt's entry is quoted, as a record only); with
      Docker reachable and only the image absent, its reasons also say whether the volume is
      present (`docker volume inspect`, no container), which leaves the status `not_observed`.
    A receipt without the store volume is `unprepared`: `prepare` refuses it (`receipt_outdated`).
    """
    old = operator_store_paths(root)
    directories = store_directories(root, old)
    spec = receipt.get('memory_store') if isinstance(receipt.get('memory_store'), dict) else {}
    entry = {'status': 'unprepared', 'volume': memory_volume(receipt['project_name']), 'target': MEMORY_TARGET,
             'operator_files_naming_old_store_paths': old,
             'next': f'kp-agent-install prepare --runtime-root {json.dumps(str(root))}'}
    if old:
        entry['operator_edits'] = 'edit each listed file to its `required` path; roles refuse to start until then'
    reasons = [f"{row['path']} is still on the bind (prepare moves it to {row['target']})" for row in directories]
    try:
        uid, gid = (int(part) for part in str(spec.get('owner', '')).split(':'))
    except ValueError:
        uid = gid = None
    if spec.get('volume') != entry['volume'] or uid is None:
        entry['reasons'] = ['receipt.json predates the store volume: re-run plan and apply with this installer, '
                            'then prepare', *reasons]
        return entry
    if reasons:
        entry['reasons'] = reasons
        return entry
    store = StoreVolume(root, receipt['project_name'], receipt['image'], uid, gid, docker)
    unreachable = store.reachable()
    if unreachable is not None or not store.image_present():
        last = spec.get('prepared')
        entry.update(status='not_observed', reasons=[
            'the volume cannot be observed: '
            + (f'Docker is unreachable ({unreachable})' if unreachable else
               f'the image {store.image} is not present locally'),
            *([] if unreachable else [volume_presence(store)]),
            *([f"receipt.json records a successful prepare under plan {str(last.get('plan_sha256'))[:12]} "
               '(a record, not an observation)'] if isinstance(last, dict) else [])])
        return entry
    try:
        prepared, reasons, _ = store.observe(directories)
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        entry.update(status='not_observed', reasons=[f'the volume could not be observed: {error}'[:300]])
        return entry
    if prepared:
        entry.pop('next')
        entry['status'] = 'prepared'
    entry['reasons'] = reasons
    return entry


def indexer_gap(root, receipt):
    """T12b: a root with capture or board selected and no `indexer` service, in its receipt's components or
    its rendered manifest, is named here (never silent): those roles seal, and nothing indexes their seals
    until `plan`, `apply` and `docker compose up -d` add the indexer. It is also the one place a process
    that sets AGENT_MEMORY_VOLUME without a Compose indexer (it seals, never drains) is reported."""
    selected = receipt.get('components') or ()
    sealers = [name for name in SEALERS if name in selected]
    if not sealers:
        return []
    services = set()
    if _kind(root, 'compose.yaml') == 'file':
        try:
            model = yaml.safe_load((root / 'compose.yaml').read_bytes())
            services = set((model or {}).get('services') or {}) if isinstance(model, dict) else set()
        except (OSError, yaml.YAMLError):
            services = set()
    if 'indexer' in selected and 'indexer' in services:
        return []
    quoted = json.dumps(str(root))
    return [{'gap': 'indexer_missing', 'selected': sealers,
             'detail': f'{" and ".join(sealers)} selected and no indexer: '
                       + ('the receipt does not select it' if 'indexer' not in selected else
                          'the rendered manifest has no indexer service')
                       + '; their seals wait in the store outbox and search does not see them',
             'next': f'kp-agent-install plan, then apply, for --runtime-root {quoted} with this installer; then '
                     f'docker compose --project-directory {quoted} up -d'}]


def verify(runtime_root_path):
    """Re-hash every recorded path; report drift and, per selected role, absent operator files."""
    root = runtime_root(runtime_root_path)
    receipt, raw = load_receipt(root)
    planned = set(receipt['files']) | set(receipt['directories']) | {RECEIPT}
    problems = drift(root, receipt) + [{'path': name, 'problem': 'symlink'}
                                       for name in symlinks(root, planned)
                                       if name not in receipt['files'] and name not in receipt['directories']]
    roles = readiness(root, receipt)
    not_configured = [name for name, entry in roles.items() if entry['status'] != 'ready']
    # The store is a readiness state like not_configured: it never changes status or exit code.
    roles['memory_store'] = memory_readiness(root, receipt)
    return {'status': 'drift' if problems else 'verified', 'runtime_root': str(root),
            'plan_sha256': receipt['plan_sha256'], 'receipt_sha256': sha256(raw),
            'files_checked': len(receipt['files']), 'directories_checked': len(receipt['directories']),
            'drift': problems, 'readiness': roles,
            'not_configured': not_configured, 'gaps': indexer_gap(root, receipt),
            'readiness_scope': 'presence of operator files only; each role checks their content when it '
                               'starts, and a not_configured role waits for them without exiting'}
