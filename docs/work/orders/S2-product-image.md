# S2 — One product image

ADR: [AT-0003](../../adr/AT-0003-distributable-package.md). Header: [ARM-HEADER](ARM-HEADER.md). Dispatch: FEATURE + TEST.

## Problem (observed at the order's base)

- **Two images.** `deploy/tooling/Dockerfile` has targets `runtime`, `full` and `acceptance`. `deploy/kanban/Dockerfile` is a second image with its own Python virtualenv at `/opt/agent-tooling`.
- **The board depends on the host.** Its tooling integrations are configured by environment variables that the live host sets to `docker exec` into another container, or to a host virtualenv:
  - `KANBAN_SESSION_IMPORT_EXECUTABLE`: an absolute path whose basename must be `kp-agent-session-import`;
  - `KANBAN_SESSION_IMPORT_CONFIG`;
  - `KANBAN_DESK_REGISTRY_COMMAND`: a JSON argv array;
  - `KANBAN_ASSISTANT_MEMORY_COMMAND`: a JSON argv array;
  - `KANBAN_ASSISTANT_MEMORY_WORKSPACE`.
- **Measured size.** The `full` image is 2.33 GB. Kanban `dist` plus `node-pty` add about 86 MB.

## Interface surface

- **Build file.** `deploy/Dockerfile`, built from the repository root, with build arg `SOURCE_REVISION`. It has these targets:
  - `runtime`: the slim Python package. No Node, no torch, no Serena.
  - `product`: everything `full` has today, plus the Kanban server.
  - `agents`: `product` plus the `claude` and `codex` CLIs, each at a pinned version.
  - `acceptance`: a test derivative of `product`.
- **Label.** Every target carries the label `org.opencontainers.image.revision` equal to `SOURCE_REVISION`.
- **Board command in `product`.** The command `kanban` runs the Kanban server (`node /app/dist/cli.js`) with the arguments passed to it. Without arguments it serves `--host 0.0.0.0 --port 3486 --no-open`, with `KANBAN_STORAGE_ROOT=/state/kanban`. It does not disable the passcode by default.
- **In-image integration defaults for the board in `product`.** When the operator has not overridden them, these environment defaults point at executables inside the same image. None of them uses `docker`.
  - `KANBAN_SESSION_IMPORT_EXECUTABLE` is an absolute in-image path whose basename is `kp-agent-session-import`.
  - `KANBAN_DESK_REGISTRY_COMMAND` defaults to an argv beginning with the absolute in-image `kp-agent-desk-registry` path. Its `--config <path>` must be supplied by the operator; if the config is absent, the board reports the desk registry as unconfigured.
  - `KANBAN_ASSISTANT_MEMORY_COMMAND` defaults to an argv invoking the in-image `kp_agent_tooling.assistant_host_cli`.
- **Container entry (`tooling-container`).**
  - Existing behaviors unchanged: `health`, `wait`, and exec of any other command.
  - `health` also succeeds for a board container, whose state root has no navigation configuration. The board health check is: the HTTP port answers.
- **Removals.** `deploy/kanban/Dockerfile` and `deploy/tooling/Dockerfile` are removed, or reduced to a comment pointing at `deploy/Dockerfile`. Any reference to them in the repository is updated.

## Properties and falsifiers

- **P1: one image carries every role.** In `product`, every `[project.scripts]` entry point in `packages/tooling/pyproject.toml` resolves on `PATH`; `kanban --help` exits 0; `node-pty` loads.
  Falsifier: any declared entry point is missing, or the board cannot start, in `product`.
- **P2: the board is self-contained.** A `product` container runs the board with a read-only root filesystem, all capabilities dropped, a non-root user and a writable `/state` bind. It answers HTTP 200 on `/` at port 3486. Its three integration commands resolve to existing in-image executables, and the image contains no `docker` binary.
  Falsifier: any integration default names a missing path or a `docker` invocation, or the board writes outside `/state`.
- **P3: credential-free base, opt-in agents.**
  - `product` and `runtime` contain no `claude` and no `codex` executable.
  - `agents` contains both, and each answers `--version` with its pinned version.
  - No target contains credential material. Checked paths: `/root`, `/home/*`, `/state`, `/etc/*token*`, `~/.claude*`, `~/.codex`, and environment variables with names matching `*TOKEN*|*KEY*|*SECRET*`.

  Falsifier: an agent CLI present in `product`, or any credential file or variable present in any target.
- **P4: revision identity.** The revision label equals the build arg in every target.
  Falsifier: a label that differs, is `unknown` when the arg is supplied, or is missing.
- **P5: size budget.** `product` is no more than 150 MB larger than the current `full` build of the same source (2.33 GB measured). `runtime` contains no `torch` package.
  Falsifier: either bound is exceeded.

## Write scope

- **FEATURE.** It may write:
  - `deploy/Dockerfile` (new);
  - `deploy/tooling/image/*` and the new `deploy/image/*`;
  - removals of the two old Dockerfiles, and edits to references to them in `docs/`, `scripts/` and `deploy/*.yaml`;
  - minimal edits under `apps/kanban/` only if the board cannot otherwise honor the in-image defaults. Any such edit must be listed and justified.

  It must not edit `packages/tooling/src`, `tests/` (except its own image smoke test, if any), or the S3 installer files (`deploy/compose.yaml` roles and the installer). It must not edit `docs/DOCKER.md`, which S3 replaces; list any stale Dockerfile reference there under AMBIGUITY instead.
- **TEST.** New tests under `tests/image/` only. Mark them `@pytest.mark.image`. They read the image references from the environment variables `AGENT_TOOLING_TEST_IMAGE` (the product image) and optionally `AGENT_TOOLING_TEST_IMAGE_AGENTS` and `AGENT_TOOLING_TEST_IMAGE_RUNTIME`.
  - When the `image` marker is selected but a required variable is unset, the tests FAIL. They do not skip.
  - Register the marker in `pytest.ini`.
  - The TEST arm builds its own null stub and green-if stub images, from small Dockerfiles in a temporary directory, to demonstrate the pair.

## Acceptance at the meet

The Coordinator builds all three targets from the merged FEATURE branch and runs the TEST arm's `-m image` suite against them. The Coordinator also runs the default suite, which must not require Docker: register the `image` marker and exclude it by default.
