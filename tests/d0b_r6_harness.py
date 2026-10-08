"""R6 of order D0b: the image suites take the PUSHED digests, beside a locally built `agents` image.

Order: docs/work/orders/D0b-release-workflow.md, R6 (F1, F2). Wired by tests/conftest.py for every
image-marked test under tests/ (tests/image, tests/install, tests/host).

Pushed-digest mode starts when any of the four published targets' variables names a registry digest
(`<registry>/<name>@sha256:<64 hex>`), as the release workflow's test jobs do. Nothing changes
otherwise: local runs with tags (images.yml, a developer's machine) behave as before.

In pushed-digest mode:
- every image-marked test FAILS at setup unless the product, runtime, ops and opencode variables all
  name registry digests (a tag or a local build where R6 says the pushed digest), and unless
  AGENT_TOOLING_TEST_IMAGE_AGENTS and AGENT_TOOLING_TEST_IMAGE_UNREVISIONED, when set, name local
  builds (`agents` is never pushed, R2);
- the locally built images are visible only to the tests R6 names (`d0b_seams.LOCAL_BUILD_TESTS`).
  For any other test the two variables are withheld for its setup, call and teardown, so a test
  outside the named list that reaches for a local build FAILS ("unset"), never runs on it.

The pure functions below take an environment mapping and a test id, so tests/test_d0b_r6_harness.py
checks them without Docker.
"""
from __future__ import annotations

import re
from pathlib import Path

import d0b_seams as seams

REPO_ROOT = Path(__file__).resolve().parents[1]
DIGEST_REFERENCE = re.compile(r"[^\s@/]+(?:/[^\s@/]+)+@sha256:[0-9a-f]{64}")
PUSHED_VARIABLES = tuple(seams.IMAGE_VARIABLES[t] for t in seams.PUBLISHED_TARGETS)
LOCAL_VARIABLES = tuple(seams.LOCAL_BUILD_TESTS)


def kind(reference: str | None) -> str:
    text = (reference or "").strip()
    if not text:
        return "unset"
    return "pushed digest" if DIGEST_REFERENCE.fullmatch(text) else "local build or tag"


def pushed_mode(environ) -> bool:
    return any(kind(environ.get(name)) == "pushed digest" for name in PUSHED_VARIABLES)


def problems(environ) -> list[str]:
    """Why the environment breaks R6's split; empty outside pushed-digest mode."""
    if not pushed_mode(environ):
        return []
    found = []
    for name in PUSHED_VARIABLES:
        value = (environ.get(name) or "").strip()
        if kind(value) == "unset":
            found.append(f"{name} is unset; R6 runs every published target from its pushed digest")
        elif kind(value) != "pushed digest":
            found.append(f"{name}={value!r} is not a registry digest: a local build or a tag where R6 says the "
                         "pushed digest")
    for name in LOCAL_VARIABLES:
        value = (environ.get(name) or "").strip()
        if kind(value) == "pushed digest":
            found.append(f"{name}={value!r} is a registry digest; R6 takes a local build for it (R2: never pushed)")
    return found


def failure(environ) -> str | None:
    found = problems(environ)
    if not found:
        return None
    return ("D0b R6 (pushed digests beside a local agents build) does not hold for this environment:\n- "
            + "\n- ".join(found))


def allowed(node: str, variable: str) -> bool:
    """Whether R6 names this test as one that takes the local build of `variable`."""
    for entry in seams.LOCAL_BUILD_TESTS.get(variable, ()):
        if entry.endswith("::") and node.startswith(entry):
            return True
        if node == entry:
            return True
    return False


def withheld(environ, node: str) -> tuple[str, ...]:
    """The local-build variables to hide from this test; none outside pushed-digest mode."""
    if not pushed_mode(environ):
        return ()
    return tuple(name for name in LOCAL_VARIABLES if (environ.get(name) or "").strip() and not allowed(node, name))


def node_id(path, name: str) -> str:
    """`tests/...::name` for a test item, relative to the repository root whatever the rootdir."""
    resolved = Path(str(path)).resolve()
    try:
        relative = resolved.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        relative = resolved.as_posix()
    return f"{relative}::{name}"


def header(environ) -> list[str]:
    if not pushed_mode(environ):
        return []
    rows = ["D0b R6: pushed-digest mode"]
    for name in (*PUSHED_VARIABLES, *LOCAL_VARIABLES):
        rows.append(f"  {name}: {kind(environ.get(name))}")
    return rows
