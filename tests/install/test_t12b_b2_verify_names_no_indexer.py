"""T12b B2: `verify` names a runtime root that selects capture or board and has no `indexer`.

Order: docs/work/orders/T12b-one-indexer-outbox.md, B2, frozen r5: "Until then, `verify` names the gap: a root with
capture or board selected and no `indexer` service is reported by name, never silent." and "`verify`'s gap line
('capture or board selected and no `indexer`') is the one place it is reported, so a search that never updates has a
named cause."

In-process, as tests/install/test_t12a_a3_verify_names_volume_presence.py runs `verify` (the `kp-agent-install`
entry point, `kp_agent_tooling.install_cli.main`, with PATH holding only its fake `docker`, volume present, image
absent). The root is applied by this installer, then made into a root of an older installer: the `indexer` service
is removed from its rendered compose.yaml, `indexer` from COMPOSE_PROFILES in .env and from the receipt's recorded
components, and the receipt's recorded sha256 of those files is updated, so `verify` sees no drift. A root as this
installer renders it is the control.

GREEN-IF, for a tooling+capture and a tooling+board root without an indexer, some string of verify's report names
`indexer` together with its absence (`no`, `not`, `missing`, `absent`, `without`, `lacks`), and the control root's
report has no such string.
Reading (AMBIGUITY): the gap line is any string in the report; its wording and place are FEATURE's.
"""
from __future__ import annotations

import hashlib
import json
import re

import pytest

from s3_harness import World
from test_t12a_a3_verify_names_volume_presence import _fake_docker

INDEXER = "indexer"
NAMED = re.compile(r"\bindexer\b", re.I)
ABSENT = re.compile(r"\b(no|not|missing|absent|without|lacks?|none)\b", re.I)


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, child in value.items():
            yield str(key)
            yield from _strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _strings(child)


def _drop_indexer(world: World) -> None:
    import yaml
    root = world.root
    receipt = json.loads((root / "receipt.json").read_text())
    compose = yaml.safe_load((root / "compose.yaml").read_text())
    (compose.get("services") or {}).pop(INDEXER, None)
    (root / "compose.yaml").write_text(yaml.safe_dump(compose, sort_keys=False, width=4096))
    lines = []
    for line in (root / ".env").read_text().splitlines(keepends=True):
        if line.startswith("COMPOSE_PROFILES="):
            value = line.split("=", 1)[1].strip().strip("'")
            line = "COMPOSE_PROFILES='" + ",".join(p for p in value.split(",") if p and p != INDEXER) + "'\n"
        lines.append(line)
    (root / ".env").write_text("".join(lines))

    def strip(node):
        if isinstance(node, dict):
            for key, child in list(node.items()):
                if key == "components" and isinstance(child, list):
                    node[key] = [c for c in child if c != INDEXER]
                else:
                    strip(child)
        elif isinstance(node, list):
            for child in node:
                strip(child)
    strip(receipt)
    for name in ("compose.yaml", ".env"):
        if name in receipt["files"]:
            receipt["files"][name]["sha256"] = hashlib.sha256((root / name).read_bytes()).hexdigest()
    (root / "receipt.json").write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    for name in ("compose.yaml", ".env", "receipt.json"):
        (root / name).chmod(0o600)


def _verify(world, monkeypatch, capsys):
    from kp_agent_tooling import install_cli
    _fake_docker(world.base, world.project, True, monkeypatch)
    capsys.readouterr()
    code = install_cli.main(["verify", "--runtime-root", str(world.root)])
    return code, json.loads(capsys.readouterr().out)


def _gap_lines(report):
    return [s for s in _strings(report) if NAMED.search(s) and ABSENT.search(s)]


@pytest.mark.parametrize("components", [("tooling", "capture"), ("tooling", "board")], ids=["capture", "board"])
def test_verify_names_a_root_with_capture_or_board_and_no_indexer(tmp_path, installer, monkeypatch, capsys,
                                                                    components):
    world = World.create(tmp_path / "old", components=components)
    world.apply_ok()
    _drop_indexer(world)
    code, report = _verify(world, monkeypatch, capsys)
    assert not report.get("drift"), f"precondition: the older root reads without drift: {report.get('drift')}"
    gap = _gap_lines(report)
    assert gap, (f"verify does not name the missing `{INDEXER}` of a {'+'.join(components)} root (exit {code}):\n"
                 + json.dumps(report, indent=1)[:4000])


def test_verify_names_no_indexer_gap_on_a_root_this_installer_rendered(tmp_path, installer, monkeypatch, capsys):
    """Control: a root as this installer renders it (tooling+capture: the indexer selected) has no gap line."""
    world = World.create(tmp_path / "new", components=("tooling", "capture"))
    world.apply_ok()
    code, report = _verify(world, monkeypatch, capsys)
    assert not _gap_lines(report), f"verify reports an indexer gap on a fresh root: {_gap_lines(report)}"
