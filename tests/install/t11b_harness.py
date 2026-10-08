"""Contract harness for order T11b's image-marked tests (docs/work/orders/T11b-leaf-behaviour.md, Q1, Q4).

Public surfaces only:
- the `kp-agent-install` console script beside the test interpreter (plan, apply, prepare, verify), through
  the S3/T9b harnesses;
- the rendered Compose project, driven with `docker compose --project-directory "$root"`;
- console scripts run inside a role container with `docker exec -i`, or in a one-off
  `docker compose run --rm -T tooling`, wrapped by tests/t11b_audit.py's `python3 -c` program, which runs
  the console script's own `main` under `sys.addaudithook` (`open`, `os.mkdir`, `sqlite3.connect`);
- listings of container paths made by a separate `docker exec` (never under the hook).
No product module is imported.

Image-marked tests read AGENT_TOOLING_TEST_IMAGE and FAIL (never skip) when it is unset. Every Compose
project, container and volume they create is named `t11b-test-...`; each project lives in a never-reused
directory under TMPDIR and is taken down with its volumes.
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import tempfile
import uuid
from dataclasses import replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from s3_harness import World, docker_env, explain  # noqa: E402
from t7a_harness import Runtime, free_port, required_docker, resolve_image, write_private  # noqa: E402
from t9b_harness import install  # noqa: E402
from t11b_audit import WRAPPER, describe, parse, spec  # noqa: E402

IMAGE_VARIABLE = 'AGENT_TOOLING_TEST_IMAGE'
PREFIX = 't11b-test'
STORE = '/state/memory'
REFUSAL = 'store_outside_volume'
TRACEBACK = 'Traceback (most recent call last)'

LIST = r'''
import hashlib, json, os, stat, sys
root = sys.argv[1]
out = {}
if os.path.lexists(root):
    for current, dirs, files in os.walk(root):
        for name in sorted(dirs + files):
            path = os.path.join(current, name)
            info = os.lstat(path)
            kind = "link" if stat.S_ISLNK(info.st_mode) else "dir" if stat.S_ISDIR(info.st_mode) else "file"
            digest = None
            if kind == "file":
                with open(path, "rb") as handle:
                    digest = hashlib.sha256(handle.read()).hexdigest()
            out[os.path.relpath(path, root)] = [kind, oct(stat.S_IMODE(info.st_mode)), digest]
print(json.dumps({"exists": os.path.lexists(root), "entries": out}))
'''


def required_image() -> str:
    ref = os.environ.get(IMAGE_VARIABLE, '').strip()
    if not ref:
        pytest.fail(f'{IMAGE_VARIABLE} is unset. This T11b test is image-marked and needs a built product image; '
                    'this is a failure, not a skip.', pytrace=False)
    return ref


def image_world(label: str, components=('tooling',)) -> tuple[World, str]:
    docker = required_docker()
    ref = required_image()
    run_id = uuid.uuid4().hex[:8]
    base = Path(tempfile.mkdtemp(prefix=f'{PREFIX}-{label}-{run_id}-')).resolve()
    world = World.create(base, components=tuple(components), uid=os.getuid() or 10001, gid=os.getgid() or 10001,
                         board_port=free_port(), project=f'{PREFIX}-{label}-{run_id}')
    return replace(world, image=resolve_image(docker, ref, docker_env(world.home))), docker


class Project(Runtime):
    """A rendered root driven with `docker compose --project-directory "$root"`, taken down with its volumes."""

    def __init__(self, docker: str, world: World):
        super().__init__(docker, world)
        assert self.project.startswith(PREFIX + '-'), self.project

    @property
    def tooling(self) -> str:
        return f'{self.project}-tooling'

    def up(self, *services: str, check: bool = True) -> subprocess.CompletedProcess:
        proc = self.compose('up', '-d', *services, timeout=600)
        if check:
            assert proc.returncode == 0, '`docker compose up -d` failed\n' + explain(proc)
        return proc

    def down(self) -> list[str]:
        self.started = True
        remaining = super().down()
        label = f'label=com.docker.compose.project={self.project}'
        volumes = set(self.docker_run('volume', 'ls', '-q', '--filter', label).stdout.split())
        volumes |= {name for name in self.docker_run('volume', 'ls', '-q', '--filter',
                                                     f'name={self.project}').stdout.split()
                    if name.startswith(self.project)}
        for name in sorted(volumes):
            self.docker_run('volume', 'rm', '-f', name)
        return remaining

    def sh(self, container: str, script: str, *, stdin: str = '', timeout: int = 300) -> subprocess.CompletedProcess:
        """A setup step in a running role (never under the audit hook)."""
        return self.docker_run('exec', '-i', container, 'sh', '-c', script, input=stdin, timeout=timeout)

    def run_ok(self, container: str, *argv: str, stdin: str = '') -> subprocess.CompletedProcess:
        proc = self.docker_run('exec', '-i', container, *argv, input=stdin, timeout=300)
        assert proc.returncode == 0, f'setup step failed in {container}: {argv}\n' + explain(proc)
        return proc

    def listing(self, container: str, root: str) -> dict:
        proc = self.docker_run('exec', '-i', container, 'python3', '-c', LIST, root, input='', timeout=120)
        assert proc.returncode == 0, f'could not list {root}\n' + explain(proc)
        return json.loads(proc.stdout)

    def audited_exec(self, container: str, entry: str, argv0: str, argv, *, watch=('/state', '/config'),
                     stdin: str = '', code: str | None = None):
        """`docker exec -i <container> python3 -c WRAPPER ...`: (CompletedProcess, audit record or None)."""
        program = spec(entry, argv0=argv0, watch=watch) if code is None else spec(code=code, watch=watch)
        proc = self.docker_run('exec', '-i', container, 'python3', '-c', WRAPPER, program, *map(str, argv),
                               input=stdin, timeout=600)
        return proc, parse(proc.stderr)

    def audited_run(self, service: str, entry: str, argv0: str, argv, *, watch=('/state', '/config'),
                    stdin: str = ''):
        """`docker compose --project-directory "$root" run --rm -T <service> python3 -c WRAPPER ...`."""
        program = spec(entry, argv0=argv0, watch=watch)
        self.started = True
        proc = subprocess.run([self.docker, 'compose', '--project-directory', str(self.root), 'run', '--rm', '-T',
                               service, 'python3', '-c', WRAPPER, program, *map(str, argv)], cwd=self.root,
                              env=self.env, capture_output=True, text=True, timeout=600, input=stdin)
        return proc, parse(proc.stderr)


def prepared_project(label: str, components=('tooling',)) -> tuple[World, Project]:
    """plan, apply and prepare (docs/DOCKER.md's order), then the roles up."""
    world, docker = image_world(label, components)
    project = Project(docker, world)
    install(world)
    project.up()
    return world, project


def host_state(world: World, relative: str) -> Path:
    """The host side of the /state bind (`$root/state/<relative>`): operator test files, never a store."""
    return world.root / 'state' / relative


def write_operator_json(path: Path, value) -> Path:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    return write_private(path, json.dumps(value) + '\n')


def refusal_problems(proc, record, root: str, before: dict, after: dict) -> list[str]:
    """Why `proc` is not Q1's refusal: non-zero, no traceback, `store_outside_volume` and the path named, no
    store path below `root` opened or made (audit), and `root`'s tree unchanged."""
    from t11b_audit import touches, without_record
    shown = proc.stdout + without_record(proc.stderr)
    problems = []
    if record is None:
        problems.append('the audit wrapper left no record (it did not run to its end)')
    elif record['crashed'] or TRACEBACK in shown:
        problems.append('the CLI crashed (a Python traceback); a crash is not a refusal')
    if proc.returncode == 0:
        problems.append('it exited 0')
    if REFUSAL not in shown:
        problems.append(f'its output does not name `{REFUSAL}`')
    if root not in shown:
        problems.append(f'its output does not name the path {root}')
    opened = touches(record, root) if record else []
    if opened:
        problems.append('store paths opened or made below the state_root:\n    ' + '\n    '.join(opened))
    if after != before:
        changed = sorted(k for k in set(before['entries']) | set(after['entries'])
                         if before['entries'].get(k) != after['entries'].get(k))
        problems.append(f'the state_root changed: {changed}')
    return problems


def shown(proc, record) -> str:
    return describe(proc, record)


def quote(argv) -> str:
    return ' '.join(shlex.quote(str(a)) for a in argv)
