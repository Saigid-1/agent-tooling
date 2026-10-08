# S3 — Single manifest and empty-root installer

ADR: [AT-0003](../../adr/AT-0003-distributable-package.md). Header: [ARM-HEADER](ARM-HEADER.md). Dispatch: FEATURE + TEST.

## Problem (observed at the order's base)

- The live runtime's Compose file was rewritten by hand outside Git. Its image is pinned by raw ID, and it hard-codes user IDs and host paths.
- The repository has three Compose files. None of them defines the `memory` or `capture` roles that the live runtime needs.
- `scripts/deploy_tooling_release.py` targets container names that no longer exist, and hard-codes an operator-specific external-volume path.
- The complete configuration examples exist only in the private runtime.
- `kp-agent-setup` (`_impl/workspace_setup.py`) plans a host bundle (`tooling.json`, `mcp.json`) but not a container runtime root.
- The appliance renderer preserved on branch `preserve/appliance-release-bundle` (`scripts/prepare_appliance_bundle.py`, `tests/test_appliance_bundle.py`, `docs/APPLIANCE-DEPLOYMENT.md`) renders a digest-locked Compose file from a four-image release. It is reusable input for this slice, not a constraint on it.

## Interface surface

### `deploy/compose.yaml`

The single manifest. It declares:

| Role | Command | Profile | Constraints |
|---|---|---|---|
| `tooling` | `[wait]` | default | The exec target for navigation MCP and memory MCP. |
| `refresh` | `[kp-agent-refresh, --request, /config/refresh.json, --watch, --interval, "300"]` | `refresh` | The only role that mounts the source credential (`/config/github-token`, read-only). |
| `capture` | `[kp-agent-workspace-capture, --config, …, --policy, /config/capture/workspace-policy.json, watch, --interval-seconds, "30"]` | `capture` | `network_mode: none`. Only read-only source mounts, supplied by an overlay. |
| `board` | `[kanban]` | `board` | Publishes `127.0.0.1:${AGENT_BOARD_PORT}:3486` only. |
| `tempo`, `collector` | as today | `telemetry` | |

Rules that apply to every role:
- All tooling roles use `image: ${AGENT_TOOLING_IMAGE}`.
- `read_only: true`, `cap_drop: [ALL]`, `security_opt: [no-new-privileges:true]`.
- `user: "${AGENT_UID}:${AGENT_GID}"`.
- Long-syntax bind mounts with `create_host_path: false`.
- Nothing mounts the Docker socket or a home directory.

### Rendered overlays

- `compose.workspaces.yaml`: explicit read-only repository binds for `tooling` and `refresh`.
- `compose.capture.yaml`: native transcript roots and repository binds for `capture`.

### Installer CLI

Entry point `kp-agent-install`, added to `[project.scripts]`. Actions:

- **`plan`.** Takes:
  - `--runtime-root <abs>`: must already exist as a physical, empty directory, or match a previous receipt from this installer;
  - `--image <registry/name@sha256:…|sha256:…>`: digest only, never a tag;
  - `--repository <key>=<abs path>`, repeatable: each must be a committed Git repository;
  - `--components tooling[,refresh][,capture][,board][,telemetry]`;
  - `--uid`, `--gid` (never 0);
  - `--board-port` (1024–65535);
  - `--project-name`.

  It prints the plan as JSON, including `plan_sha256`, and writes nothing.
- **`apply`.** Takes the same arguments plus `--expected-plan-sha256`. It writes the runtime root:
  - `.env`, `compose.yaml` (a copy of the manifest), and the overlays;
  - `config/navigation.json`, schema `ops.agent-tooling.v1`, with container paths;
  - `state/.ops-tooling-volume`, containing `ops-tooling-state-v1`;
  - the required state directories;
  - `host/claude-mcp.json` and `host/codex-mcp.toml`: registration snippets that invoke `docker exec -i <rendered tooling container> kp-agent-tooling --config /config/navigation.json serve`, never `-t`;
  - `receipt.json`, containing the sha256 of every written file.

  Directories are created mode 0700 and files 0600. It never overwrites a differing file, and it refuses symlinks.
- **`verify`.** Re-hashes the files against `receipt.json` and reports drift.

`docs/DOCKER.md` is replaced by an operator guide for this flow. It covers install, start/stop, the registration snippets, provider login for the `agents` target, upgrade (a new digest means re-plan and re-apply), and rollback. The stale `deploy/tooling/compose.yaml`, `deploy/compose.import.yaml` and `scripts/deploy_tooling_release.py` are either removed, or reduced to pointers with their behavior folded into the installer. Justify whichever you choose.

## Properties and falsifiers

- **P1: plan is pure.** `plan` writes nothing anywhere. Identical inputs give identical `plan_sha256`, and any input change changes it.
  Falsifier: any file created or modified by `plan`, or a hash that is unstable or insensitive to an input.
- **P2: apply is exact and idempotent.**
  - `apply` with a mismatched `--expected-plan-sha256` writes nothing.
  - A second `apply` with identical inputs changes no bytes and reports no change.
  - A pre-existing differing file, or a symlink in the root, is refused with no writes.

  Falsifier: any partial write on refusal, or any byte change on re-apply.
- **P3: refusals.** Each of these is refused before any write: a missing or symlinked runtime root, an image reference that is a tag, UID or GID 0, a board port below 1024, a path containing `$` or a newline, a repository path that is not a Git repository, and an unknown component.
  Falsifier: any one accepted.
- **P4: isolation in the rendered configuration.** In `docker compose config` output for all profiles:
  - `capture` has `network_mode: none` and only read-only binds;
  - only `refresh` references `github-token`;
  - `board` publishes only on `127.0.0.1`;
  - no service mounts `/var/run/docker.sock` or a home directory root;
  - every tooling service's image is the supplied digest.

  Falsifier: any violation.
- **P5: an empty root works end to end.** With a real image, meaning the S2 `product` target or today's `full` image if S2 has not merged, and one small committed repository:
  - `plan`, then `apply`, then `docker compose up -d tooling` reaches healthy;
  - over `docker exec -i … kp-agent-tooling --config /config/navigation.json serve`, MCP `initialize` succeeds;
  - `tools/list` includes `navigation.search`;
  - a `navigation.search` call on the repository at its HEAD commit returns at least one result for a string known to be in it.

  Falsifier: any step fails from an empty root.

## Write scope

- **FEATURE.** It may write:
  - `deploy/compose.yaml`;
  - new `deploy/examples/**`;
  - a new installer module under `packages/tooling/src/kp_agent_tooling/` (for example `install_cli.py` and `_impl/runtime_install.py`) and its `pyproject.toml` entry point;
  - `docs/DOCKER.md`;
  - removal or rewrite of `deploy/tooling/compose.yaml`, `deploy/compose.import.yaml` and `scripts/deploy_tooling_release.py`, with their references updated.

  It may bring in the preserved appliance files from `preserve/appliance-release-bundle`, adapted or folded in. It must not edit Dockerfiles (S2 owns them), `apps/`, or existing tests.
- **TEST.** New tests under `tests/install/` only.
  - P1 to P4 must run without Docker, except for `docker compose config`: if the Docker CLI is present, use it; otherwise parse YAML and label the check as such.
  - P5 is marked `@pytest.mark.image` and reads `AGENT_TOOLING_TEST_IMAGE`. It fails when that variable is unset and the marker is selected.

## Dispatcher amendments (2026-09-30)

- **P4, capture.** Capture has `network_mode: none` and exactly one writable bind, whose target is `/state`. Every other capture bind is read-only. The original "only read-only binds" was a gap in the order: capture must record what it captures.
- **P4, source credential.** Exactly one service, `refresh`, has any bind whose source or target contains `github-token`. The container path is not fixed.
- **Packaging.** The manifest ships as package data, so that wheel-only and image installs work without a source checkout.

## Acceptance at the meet

The Coordinator runs P1 to P4 in the default suite. The Coordinator runs P5 against a built image, from an empty directory on the external volume with its own project name, beside the live runtime and without touching it.
