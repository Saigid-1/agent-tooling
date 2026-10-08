"""T9b P1 (rendered part, no Docker): one volume holds every store.

Order: docs/work/orders/T9b-memory-store-volume.md, P1 as Amendment 2 states it: "the
Compose volume key is `memory`, so the volume is named `<project>_memory`. It is mounted
at `/state/memory` in every role that mounts `/state`." With P6 ("the other state paths
[...] are unchanged"): `/state` stays the bind of `<root>/state`, and nothing else is
mounted below it.
Falsifier: a service in which /state/memory resolves to the host bind; a volume not
named for the project. (Owner and mode need a container: test_t9b_p1_p2_store_volume_image.py;
documented and example store paths: test_t9b_p5_store_docs.py.)

Instrument: the rendered root's Compose files (`compose.yaml` and every rendered
overlay), read as YAML and interpolated with the rendered `.env`, as Compose does.
A service's mount at a target is the last one any file declares there. A volume's
resolved name is its `name`, else `<project>_<key>` (Compose's default); an external
volume without a `name` is named by its key. Every service of `compose.yaml` is read,
whatever its profile: the manifest copy is the same for every component set.

Reading (repeated in the arm report under AMBIGUITY): every service that `compose.yaml`
declares with a `/state` mount is a role that mounts `/state`, whatever its profile.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from s3_harness import compose_files, interpolate, read_dotenv
from t9b_harness import STATE_ROLES, STATE_TARGET, STORE_TARGET, VOLUME_KEY, store_volume_name


def _interpolated(value, env):
    if isinstance(value, dict):
        return {key: _interpolated(child, env) for key, child in value.items()}
    if isinstance(value, list):
        return [_interpolated(child, env) for child in value]
    return interpolate(value, env) if isinstance(value, str) else value


def _mount(entry) -> dict:
    if isinstance(entry, dict):
        return dict(entry)
    parts = str(entry).split(":")
    if len(parts) == 1:
        return {"type": "volume", "source": None, "target": parts[0]}
    kind = "bind" if parts[0].startswith(("/", ".", "~")) else "volume"
    return {"type": kind, "source": parts[0], "target": parts[1]}


def rendered(root: Path) -> tuple[str, dict[str, dict[str, dict]], dict]:
    """(project, {service: {target: mount}}, {volume key: definition}) of a rendered root."""
    env = read_dotenv(root / ".env")
    project, services, volumes = None, {}, {}
    for path in compose_files(root):
        raw = _interpolated(yaml.safe_load(path.read_text()) or {}, env)
        project = raw.get("name") or project
        for key, body in (raw.get("volumes") or {}).items():
            volumes[key] = {**(volumes.get(key) or {}), **(body or {})}
        for name, body in (raw.get("services") or {}).items():
            table = services.setdefault(name, {})
            for entry in (body or {}).get("volumes") or []:
                mount = _mount(entry)
                table[mount.get("target")] = mount
    return project or env.get("AGENT_PROJECT_NAME"), services, volumes


def volume_name(project: str, volumes: dict, key) -> str | None:
    if key is None:
        return None  # anonymous: not a named volume
    body = volumes.get(key) or {}
    if body.get("name"):
        return body["name"]
    return key if body.get("external") else f"{project}_{key}"


def store_mounts(root: Path) -> tuple[str, dict, dict]:
    project, services, volumes = rendered(root)
    return project, services, {name: table.get(STORE_TARGET) for name, table in services.items()
                               if STATE_TARGET in table}


@pytest.mark.parametrize("service", STATE_ROLES)
def test_store_is_a_named_volume_in_every_state_role(world, service):
    """GREEN-IF the service mounts the volume with Compose key `memory` at /state/memory, resolved to
    `<project>_memory`, and keeps /state as the bind of `<root>/state` with no other mount below it."""
    world.apply_ok()
    project, services, store = store_mounts(world.root)
    assert service in store, (f"{service} no longer mounts {STATE_TARGET}; the services that do: {sorted(store)}")
    table = services[service]
    mount = store.get(service)
    assert mount is not None, (f"{service} mounts {STATE_TARGET} but nothing at {STORE_TARGET}: there "
                               "/state/memory resolves to the host bind")
    assert mount.get("type") == "volume", f"{service}: {STORE_TARGET} is {mount}, not a named volume"
    assert mount.get("source") == VOLUME_KEY, f"{service}: the store volume's Compose key is not {VOLUME_KEY!r}: {mount}"
    name = volume_name(project, rendered(world.root)[2], mount.get("source"))
    assert name == store_volume_name(project), (
        f"{service}: the store volume resolves to {name!r}, not {store_volume_name(project)!r}")
    state = table[STATE_TARGET]
    assert state.get("type") == "bind" and state.get("source") == f"{world.root}/state", (
        f"{service}: {STATE_TARGET} is no longer the bind of the runtime root's state: {state}")
    below = sorted(t for t in table if isinstance(t, str) and t.startswith(STATE_TARGET + "/")
                   and t != STORE_TARGET)
    assert not below, f"{service}: other state paths left the bind: {below}"


def test_one_store_volume_per_project(world):
    """GREEN-IF every /state role of a project mounts the same store volume, and another project
    rendered from the same inputs gets another volume, named for it."""
    world.apply_ok()
    other_root = world.base / "root-other"
    other_root.mkdir()
    other_project = world.project + "-other"
    world.apply_ok(world.args(root=other_root, project=other_project))
    names = {}
    for root in (world.root, other_root):
        project, _, store = store_mounts(root)
        volumes = rendered(root)[2]
        resolved = {service: volume_name(project, volumes, (mount or {}).get("source")) if mount else None
                    for service, mount in store.items()}
        assert None not in resolved.values() and len(set(resolved.values())) == 1, (
            f"project {project}: the /state roles do not share one store volume: {resolved}")
        names[project] = resolved[STATE_ROLES[0]]
    assert names[world.project] != names[other_project], (
        f"two projects resolve to the same store volume: {names}")
    assert all(name == store_volume_name(project) for project, name in names.items()), (
        f"not named <project>_{VOLUME_KEY}: {names}")
    assert not re.search(r"\$\{", "".join(names.values())), f"uninterpolated volume name: {names}"
