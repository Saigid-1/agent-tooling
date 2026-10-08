"""Shared helpers for the T5b retention-reference contract tests (tests/refresh/ only).

Order: docs/work/orders/T5b-retention-references.md (frozen at its merge commit). The tests
drive the public ``kp_agent_tooling.refresh_cli.refresh(request)`` and the
``kp-agent-refresh`` console script through the unchanged T5 rig (t5_rig.py): its
seams, its temporary generation trees with ``profile.json`` files, and its way of
reading retention receipts (every JSON value the cycle returns or writes).

The container paths. ``retention_references`` names files under ``/config`` or
``/state``, the refresh container's bind mounts of the runtime root's ``config``
and ``state`` directories (deploy/compose.yaml). This host has neither directory
and these tests run no Docker. ``ContainerMounts`` models the two bind mounts for
the test process only: an absolute path at or under ``/config`` or ``/state`` given
to the Python file API (``open``/``io.open``, ``os.open``, ``os.stat``,
``os.lstat``, ``os.access``, ``os.listdir``, ``os.scandir``, ``os.readlink``, and
through them ``pathlib`` and ``os.path``) is served from a temporary directory, as
the bind mount serves the runtime root's directory. Every other path passes
through unchanged. No product module is patched. A subprocess (the console
script) does not see the mounts, so console-script tests only use requests that
must be refused before any file is read.
"""
from __future__ import annotations

import builtins
import io
import json
import os
from pathlib import Path
import posixpath

import pytest

CONTAINER_ROOTS = ('/config', '/state')
# The snapshot registry's per-file read budget at the order's base
# (refresh_retention.REFERENCE_FILE_BYTES); the order reuses "the read budget".
READ_BUDGET_BYTES = 16_000_000
# P3's bound on the number of listed files.
MAX_REFERENCES = 32


class ContainerMounts:
    """``/config`` and ``/state`` bound to temporary directories for this process."""

    def __init__(self, base, monkeypatch):
        self.sources = {}
        for target in CONTAINER_ROOTS:
            source = Path(base) / target.strip('/')
            source.mkdir(parents=True)
            self.sources[target] = source
        self._install(monkeypatch)
        self._self_check()

    # -- mapping -------------------------------------------------------------------
    def _map(self, text):
        if not text.startswith('/'):
            return None
        normal = posixpath.normpath(text)
        if normal.startswith('//'):
            normal = '/' + normal.lstrip('/')
        for target, source in self.sources.items():
            if normal == target or normal.startswith(target + '/'):
                return str(source) + normal[len(target):]
        return None

    def redirect(self, path):
        if path is None or isinstance(path, int):
            return path
        try:
            raw = os.fspath(path)
        except TypeError:
            return path
        if isinstance(raw, bytes):
            mapped = self._map(os.fsdecode(raw))
            return path if mapped is None else os.fsencode(mapped)
        mapped = self._map(raw)
        return path if mapped is None else mapped

    def host(self, container_path):
        mapped = self._map(str(container_path))
        if mapped is None:
            raise ValueError(f'not a container path under /config or /state: {container_path}')
        return Path(mapped)

    # -- operator actions on the host side of the mount ---------------------------
    def write(self, container_path, content):
        """Write (or replace in place) the file the container sees at ``container_path``."""
        host = self.host(container_path)
        host.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            host.write_bytes(content)
        else:
            host.write_text(content)
        return str(container_path)

    def remove(self, container_path):
        self.host(container_path).unlink()

    def chmod(self, container_path, mode):
        self.host(container_path).chmod(mode)

    # -- installation -----------------------------------------------------------------
    def _install(self, monkeypatch):
        redirect = self.redirect
        real_open = builtins.open

        def open_(file, *args, **kwargs):
            return real_open(redirect(file), *args, **kwargs)

        monkeypatch.setattr(builtins, 'open', open_)
        monkeypatch.setattr(io, 'open', open_)

        def wrap(real):
            def wrapper(*args, **kwargs):
                if args:
                    args = (redirect(args[0]),) + args[1:]
                elif 'path' in kwargs:
                    kwargs['path'] = redirect(kwargs['path'])
                return real(*args, **kwargs)
            wrapper.__wrapped__ = real
            return wrapper

        for name in ('open', 'stat', 'lstat', 'access', 'listdir', 'scandir', 'readlink'):
            monkeypatch.setattr(os, name, wrap(getattr(os, name)))

    def _self_check(self):
        """The model must hold for the reads a reader could make, or every test is void."""
        probe = '/config/.t5b-mount-probe/catalog.json'
        self.write(probe, '{"probe": true}')
        with open(probe, 'rb') as stream:
            assert stream.read() == b'{"probe": true}'
        assert Path(probe).read_text() == '{"probe": true}'
        assert os.path.isfile(probe) and Path(probe).is_file() and os.path.isdir('/config')
        assert os.path.getsize(probe) == len('{"probe": true}')
        assert os.access(probe, os.R_OK)
        assert os.path.realpath(probe) == probe and str(Path(probe).resolve()) == probe
        fd = os.open(probe, os.O_RDONLY)
        try:
            assert os.read(fd, 8) == b'{"probe"'
        finally:
            os.close(fd)
        self.remove(probe)
        self.host('/config/.t5b-mount-probe').rmdir()
        assert not os.path.exists(probe) and not Path(probe).exists()
        assert not os.path.exists('/config/../etc/t5b-absent-probe')


@pytest.fixture
def mounts(tmp_path, monkeypatch):
    return ContainerMounts(tmp_path / 'runtime-root', monkeypatch)


# -- generations and their mentions ---------------------------------------------------
def mention(rig, generation, form):
    """File content that names ``generation`` by absolute path, as an operator's file would.

    ``json``: a maintained knowledge catalog, the generation's own published
    ``knowledge.json`` (the live ``config/knowledge.json`` pins generations this way).
    ``text``: a non-JSON text file naming a path inside the generation.
    """
    if form == 'json':
        return (Path(generation) / 'knowledge.json').read_text()
    if form == 'text':
        return f'# pinned by the operator\nKNOWLEDGE_SOURCE={Path(generation) / "solo"}\n'
    raise ValueError(form)


def publish_next(rig, key='solo'):
    """One more successful publish of a fresh generation; returns (result, generation)."""
    rig.bump(key)
    result = rig.refresh()
    assert result['status'] == 'published', result
    return result, rig.published_generation()


def listed_by_reference_files(result):
    """The values this cycle's returned receipt reports under ``referenced_by_reference_files``."""
    values = []

    def walk(value):
        if isinstance(value, dict):
            if 'referenced_by_reference_files' in value:
                values.append(value['referenced_by_reference_files'])
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(result)
    return values


def reported(values, generation):
    """``generation`` is named in a ``referenced_by_reference_files`` value."""
    return any(Path(generation).name in json.dumps(value) for value in values)
