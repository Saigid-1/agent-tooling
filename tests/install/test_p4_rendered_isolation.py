"""S3 P4: isolation in the rendered configuration, and the manifest's role table.

In `docker compose config` output for all profiles:
- `capture` has `network_mode: none` and only read-only binds [clarified by the
  dispatcher: every capture bind is read-only except at most one writable bind,
  whose container target is `/state`];
- only `refresh` references `github-token` [clarified by the dispatcher: exactly one
  service, refresh, has any bind whose source or target contains `github-token`;
  the credential's container path is not fixed];
- `board` publishes only on `127.0.0.1`;
- no service mounts `/var/run/docker.sock` or a home directory root;
- every tooling service's image is the supplied digest.
Falsifier: any violation.

The rendered root is installed with every component and two repositories. Its
configuration comes from `docker compose config` when the docker CLI (with the
compose plugin) exists; otherwise from a labelled YAML parse (a UserWarning
names the instrument in the pytest summary).
"""
from __future__ import annotations

import json
import os
import re

import pytest
import yaml

from s3_harness import (ABSENT, ALL_COMPONENTS, DIGEST_REGISTRY, MANIFEST, ROLE_SERVICES, TELEMETRY_SERVICES,
                        TOOLING_ROLES, World, binds, declared_binds, home_roots, json_strings,
                        rendered_config)


@pytest.fixture(scope="module")
def rendered(tmp_path_factory, installer):
    world = World.create(tmp_path_factory.mktemp("p4"), components=ALL_COMPONENTS)
    world.repos = {"fixture": world.repo, "second": world.repo_b}
    world.apply_ok()
    config, instrument = rendered_config(world)
    nav = json.loads((world.root / "config" / "navigation.json").read_text())
    return world, config.get("services") or {}, instrument, nav


def _service(services, name):
    assert name in services, f"service {name} absent from the rendered configuration: {sorted(services)}"
    return services[name]


def test_overlays_rendered(rendered):
    world, _, _, _ = rendered
    for name in ("compose.workspaces.yaml", "compose.capture.yaml"):
        assert (world.root / name).is_file(), f"apply did not render {name}"


def test_config_declares_every_role(rendered):
    _, services, instrument, _ = rendered
    missing = [name for name in ROLE_SERVICES if name not in services]
    assert not missing, f"{instrument}: roles missing: {missing}"


ROLE_TABLE = {
    "tooling": (["wait"], []),
    "refresh": (["kp-agent-refresh", "--request", "/config/refresh.json", "--watch", "--interval", "300"],
                ["refresh"]),
    "capture": (None, ["capture"]),
    "board": (["kanban"], ["board"]),
    # T12b: the one drainer; its command carries `--watch` (T12a R1) and the fixed 2 s interval (B2).
    "indexer": (None, ["indexer"]),
    "tempo": (None, ["telemetry"]),
    "collector": (None, ["telemetry"]),
}


@pytest.mark.parametrize("role", ROLE_SERVICES)
def test_role_command_and_profile(rendered, role):
    _, services, instrument, _ = rendered
    svc = _service(services, role)
    command, profiles = ROLE_TABLE[role]
    assert sorted(svc.get("profiles") or []) == profiles, f"{instrument}: {role} profiles {svc.get('profiles')}"
    if role == "capture":
        cmd = svc.get("command") or []
        assert (len(cmd) == 8 and cmd[:2] == ["kp-agent-workspace-capture", "--config"]
                and cmd[3:] == ["--policy", "/config/capture/workspace-policy.json", "watch",
                                "--interval-seconds", "30"]), f"{instrument}: capture command {cmd}"
    elif role == "indexer":
        from t12b_seams import ORDER_INTERVAL_SECONDS, indexer_interval
        cmd = svc.get("command") or []
        assert "--watch" in cmd and indexer_interval(cmd) == ORDER_INTERVAL_SECONDS, (
            f"{instrument}: indexer command {cmd} (needs --watch and the 2 s interval)")
    elif command is not None:
        assert svc.get("command") == command, f"{instrument}: {role} command {svc.get('command')}"


def test_capture_network_none(rendered):
    _, services, instrument, _ = rendered
    assert _service(services, "capture").get("network_mode") == "none", instrument


def test_capture_binds_read_only_except_state(rendered):
    _, services, instrument, _ = rendered
    writable = [b for b in binds(_service(services, "capture")) if b.get("read_only") is not True]
    assert len(writable) <= 1 and all(b.get("target") == "/state" for b in writable), (
        f"{instrument}: capture may hold only one writable bind, at /state: {writable}")


def test_only_refresh_references_github_token(rendered):
    _, services, instrument, _ = rendered
    offenders = [name for name, svc in services.items()
                 if name != "refresh" and "github-token" in json.dumps(svc)]
    assert not offenders, f"{instrument}: github-token referenced by {offenders}"


def _credential_binds(svc):
    return [b for b in binds(svc)
            if "github-token" in str(b.get("source", "")) or "github-token" in str(b.get("target", ""))]


def test_source_credential_bound_only_into_refresh(rendered):
    _, services, instrument, _ = rendered
    holders = sorted(name for name, svc in services.items() if _credential_binds(svc))
    assert holders == ["refresh"], f"{instrument}: services binding github-token: {holders}"
    writable = [b for b in _credential_binds(services["refresh"]) if b.get("read_only") is not True]
    assert not writable, f"{instrument}: refresh mounts the source credential writable: {writable}"


def test_source_credential_not_reachable_through_other_binds(rendered):
    # Derived from the role table ("the only role that mounts the source credential")
    # and AT-0003 decision 2: a directory bind that contains the credential's host
    # file mounts the credential too.
    _, services, instrument, _ = rendered
    sources = [os.path.normpath(b["source"]) for b in _credential_binds(_service(services, "refresh"))]
    assert sources, f"{instrument}: refresh binds no github-token credential"
    exposed = [(name, b["source"]) for name, svc in services.items() if name != "refresh"
               for b in binds(svc) for source in sources
               if source == os.path.normpath(b["source"])
               or source.startswith(os.path.normpath(b["source"]).rstrip("/") + "/")]
    assert not exposed, f"{instrument}: the credential's host file is reachable from {exposed}"


def test_board_publishes_loopback_only(rendered):
    world, services, instrument, _ = rendered
    board = _service(services, "board")
    assert board.get("network_mode") != "host", f"{instrument}: board uses host networking"
    ports = board.get("ports") or []
    assert ports, f"{instrument}: board publishes nothing"
    assert all(p.get("host_ip") == "127.0.0.1" for p in ports), f"{instrument}: board ports {ports}"
    assert any(str(p.get("target")) == "3486" and str(p.get("published")) == str(world.board_port)
               for p in ports), f"{instrument}: board does not publish 127.0.0.1:{world.board_port}:3486"


def test_no_docker_socket_or_home_root_mounts(rendered):
    world, services, instrument, _ = rendered
    homes = home_roots(world.home)
    offenders = []
    for name, svc in services.items():
        for b in binds(svc):
            source = os.path.normpath(b.get("source", ""))
            if source.endswith("docker.sock") or str(b.get("target", "")).endswith("docker.sock"):
                offenders.append((name, "docker socket", source))
            if any(source == h or h.startswith(source.rstrip("/") + "/") for h in homes):
                offenders.append((name, "home directory root", source))
    assert not offenders, f"{instrument}: {offenders}"


def test_tooling_role_images_are_supplied_digest(rendered):
    _, services, instrument, _ = rendered
    wrong = {name: svc.get("image") for name, svc in services.items()
             if name not in TELEMETRY_SERVICES and svc.get("image") != DIGEST_REGISTRY}
    assert not wrong, f"{instrument}: images differ from {DIGEST_REGISTRY}: {wrong}"


@pytest.mark.parametrize("role", TOOLING_ROLES)
def test_tooling_roles_hardened(rendered, role):
    world, services, instrument, _ = rendered
    svc = _service(services, role)
    problems = []
    if svc.get("read_only") is not True:
        problems.append("read_only is not true")
    if "ALL" not in (svc.get("cap_drop") or []):
        problems.append(f"cap_drop {svc.get('cap_drop')}")
    if not {"no-new-privileges:true", "no-new-privileges=true"} & set(svc.get("security_opt") or []):
        problems.append(f"security_opt {svc.get('security_opt')}")
    if svc.get("user") != f"{world.uid}:{world.gid}":
        problems.append(f"user {svc.get('user')!r} != {world.uid}:{world.gid}")
    assert not problems, f"{instrument}: {role}: {problems}"


def test_tooling_role_binds_are_long_syntax_without_host_path_creation(rendered):
    # Property: no tooling-role bind asks Compose to create its host path.
    # The rendered files are the authority. Compose's normalized output omits
    # create_host_path when it equals that release's default, and releases differ:
    # v5.1.4 prints false and omits true, older releases omit false and print true.
    # So absence there is accepted only for a bind the files declare false.
    world, services, instrument, _ = rendered
    declared = declared_binds(world.root)
    problems = [f"{name} {target}: {d['syntax']} syntax in {d['file']}, "
                f"create_host_path={d['create_host_path']!r} (must be written false, long syntax)"
                for (name, target), d in sorted(declared.items())
                if name not in TELEMETRY_SERVICES
                and (d["syntax"] != "long" or d["create_host_path"] is not False)]
    for name, svc in sorted(services.items()):
        if name in TELEMETRY_SERVICES:
            continue
        for b in binds(svc):
            target = b.get("target")
            shown = (b.get("bind") or {}).get("create_host_path", ABSENT)
            written = declared.get((name, target), {})
            if shown is True:
                problems.append(f"{name} {target}: {instrument} reports create_host_path: true")
            elif shown is ABSENT and not (written.get("syntax") == "long"
                                          and written.get("create_host_path") is False):
                problems.append(f"{name} {target}: {instrument} omits create_host_path and no "
                                f"rendered file declares it false")
    assert declared, "no bind declarations found in the rendered compose files"
    assert not problems, "binds that may create host paths:\n" + "\n".join(problems)


def _container_path_bound(svc, host, container):
    for b in binds(svc):
        if os.path.normpath(b.get("source", "")) != os.path.normpath(str(host)):
            continue
        target = b.get("target", "")
        if b.get("read_only") is True and (container == target or container.startswith(target.rstrip("/") + "/")):
            return True
    return False


@pytest.mark.parametrize("role", ["tooling", "refresh"])
def test_workspace_binds_read_only(rendered, role):
    world, services, instrument, nav = rendered
    svc = _service(services, role)
    for key, host in world.repos.items():
        container = ((nav.get("repos") or {}).get(key) or {}).get("path", "")
        assert _container_path_bound(svc, host, container), (
            f"{instrument}: {role} lacks a read-only bind of {host} serving navigation path {container!r}")
    writable = [b for b in binds(svc) if b.get("read_only") is not True
                and os.path.normpath(b.get("source", "")) in {os.path.normpath(str(h)) for h in world.repos.values()}]
    assert not writable, f"{instrument}: {role} binds a repository writable: {writable}"


def test_capture_binds_each_repository_read_only(rendered):
    world, services, instrument, _ = rendered
    capture = _service(services, "capture")
    for key, host in world.repos.items():
        assert any(os.path.normpath(b.get("source", "")) == os.path.normpath(str(host))
                   and b.get("read_only") is True for b in binds(capture)), (
            f"{instrument}: capture lacks a read-only bind of repository {key}")


IMAGE_VARIABLE = re.compile(r"\$\{AGENT_TOOLING_IMAGE(?::?\?[^}]*)?\}")


def test_manifest_tooling_roles_use_image_variable():
    raw = yaml.safe_load(MANIFEST.read_text()) or {}
    services = raw.get("services") or {}
    missing = [name for name in TOOLING_ROLES if name not in services]
    assert not missing, f"deploy/compose.yaml lacks roles {missing}"
    wrong = {name: svc.get("image") for name, svc in services.items()
             if name not in TELEMETRY_SERVICES and not IMAGE_VARIABLE.fullmatch(str(svc.get("image")))}
    assert not wrong, f"tooling roles must use image: ${{AGENT_TOOLING_IMAGE}}: {wrong}"


def test_manifest_has_no_host_specific_values():
    raw = yaml.safe_load(MANIFEST.read_text()) or {}
    found = [value for value in json_strings(raw)
             if any(marker in value for marker in ("/Users/", "/Volumes/", "/home/"))]
    assert not found, f"deploy/compose.yaml carries host-specific paths: {found}"
    services = raw.get("services") or {}
    users = {name: svc.get("user") for name, svc in services.items() if name not in TELEMETRY_SERVICES}
    wrong = {name: user for name, user in users.items()
             if not re.fullmatch(r"\$\{AGENT_UID[^}]*\}:\$\{AGENT_GID[^}]*\}", str(user))}
    assert not wrong, f"tooling roles must run as ${{AGENT_UID}}:${{AGENT_GID}}: {wrong}"
