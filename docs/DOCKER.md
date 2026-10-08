# Docker runtime: install, operate, upgrade

One image, one manifest, one installer ([ADR AT-0003](adr/AT-0003-distributable-package.md)).
[AT-0004](adr/AT-0004-portable-kanban-suite.md) keeps that design and adds an `ops`
image variant for the OPS extension. `deploy/compose.yaml` defines every role.
`kp-agent-install` renders a private runtime root from an empty directory. It writes the host paths, user IDs and the
image digest into that root, never into the manifest. Nothing is registered or
started implicitly. `plan` and `apply` never call Docker. `verify` starts no role; when
Docker is reachable and the image is present, it observes the store with one read-only
one-off container of the runtime image, as `prepare` does. `kp-agent-install prepare`
runs short one-off containers of the runtime image to create
the [store volume](#the-store-volume) and move stores into it, never a role. The
operator starts containers with `docker compose` and installs the rendered MCP snippets
explicitly.

## Roles

| Role | Profile | Command | Boundary |
|---|---|---|---|
| `tooling` | always | `wait` | The exec target for the navigation and memory MCP servers. Publishes no port. |
| `refresh` | `refresh` | `kp-agent-refresh --request /config/refresh.json --watch --interval 300` | The only role that mounts the source credential (`/config/github-token`, read-only). Until `$root/config/refresh.json` is written it logs `not_configured` and waits. |
| `capture` | `capture` | Entrypoint `tooling-container kp-agent-launch ingest-spool --root /spool --watch --`, which ingests host-adapter hook events and supervises the command `kp-agent-workspace-capture ... watch --interval-seconds 30` | `network_mode: none`. Container name `<project>-capture`, the exec target of host launches ([HOST-ADAPTER.md](HOST-ADAPTER.md)). Transcript, repository and spool (`$root/spool` at `/spool`) binds are read-only. `/state` is its only writable bind. No other role mounts the spool. Until `$root/config/capture/session.json` and `$root/config/capture/workspace-policy.json` are written, workspace capture logs `not_configured` and waits, while spool ingestion and host launches keep working. A capture pass that fails never ends the loop ([Restarts and refusals](#restarts-and-refusals)). |
| `board` | `board` | `kanban` | Publishes `127.0.0.1:<board port>` only. The board's passcode gate stays enabled. Its Desks menu and desk task launches use the registry operator config `/config/launch/registry.json`, the same file host launches use; until `$root/config/launch/registry.json` is written the menu reports the desk registry as not configured and the board keeps serving. No repository is mounted, except with `--board-agents` (read-write at the host paths, for the `agents` image; see [board](#optional-roles)). |
| `indexer` | `indexer` | `kp-agent-desk indexer --root /state/memory --watch --interval 2` | The one writer of every store's search index ([The indexer](#the-indexer)). `network_mode: none`. Selected whenever `capture` or `board` is. |
| `summarizer` | `summarizer` | `kp-agent-summarizer --approval /config/summarizer/approval.json --gateway-config /config/summarizer/gateway.json --state /state/memory/summarizer --watch` | Opt-in: selected only when a plan names it ([summarizer](#optional-roles)). Egress by Compose's default network, to the model gateway route's provider only. The only role that mounts the summarizer's provider key (`/config/summarizer.key`, read-only). Until its approval, gateway route, priced budget, key and desk admissions exist it logs `not_configured` and opens no connection. |
| `tempo`, `collector` | `telemetry` | as upstream | Trace storage under `tempo/`. No published ports. |

Every role runs read-only, with all capabilities dropped and `no-new-privileges`,
as the rendered non-root UID:GID. Every bind uses `create_host_path: false`, so a
missing or unmounted runtime root fails at start instead of starting empty.
No role mounts the Docker socket or a home directory. `tooling`, `refresh`, `capture`,
`board`, `indexer` and `summarizer` restart `unless-stopped` ([Restarts and refusals](#restarts-and-refusals)).

Every role that mounts `/state` also mounts the project's named volume
`<project>_memory` at `/state/memory`, and every store a role opens is under it: desk
memory, the desk registry and its roster, launches, sessions and assistant stores
([The store volume](#the-store-volume)). Each role's preflight refuses to start its
command when `/state/memory` is not that volume, when the volume is not prepared, or
when an operator file names a store path outside `/state/memory` or a `config_template`
under `/state`. A refused role does
not exit: it waits, with health `refused`, until the refusal clears
([Restarts and refusals](#restarts-and-refusals)).

## Install

Prerequisites:

- Docker with Compose v2.24 or later (the board's optional `env_file`). The store
  volume is a plain named volume (no `subpath`), so it raises neither the Compose nor
  the Engine floor. `kp-agent-install prepare` and `verify` use the `docker` CLI of
  your environment: run them where `docker` reaches the daemon that will run the roles.
- An image by digest, never a tag: `registry/name@sha256:<64 hex>`, or a local
  `sha256:<64 hex>` image ID. The manifest expects the `product` image, or `agents`
  for the provider CLIs (see [Images](#images)). Build it from a checkout of this
  repository with `deploy/Dockerfile` (step 0 of the [first run](#first-run-from-an-empty-root)).
- `kp-agent-install`, from one of the install paths below.
- Committed Git repositories to navigate. Each must be a repository root with its
  own `.git` directory: linked worktrees, submodules and `alternates` clones keep
  objects outside the bind mount, so they are refused.

### Getting the installer

Release 0.4.0 publishes container images only ([Images](#images)): no wheel, and
no release on a package index. Get `kp-agent-install` from a wheel you build from
a checkout of this repository, or from the checkout itself. The `product` image can
also run `plan` and `apply` (below); `prepare` and `verify` still need the
installer on the host.

The manifest ships as package data (`kp_agent_tooling/assets/deploy/`), so an
installed wheel needs no checkout. In this repository, `deploy/compose.yaml` and
`deploy/telemetry/*.yaml` are symlinks to that one hand-edited copy. Edit the
target. If an editor replaces a symlink with a differing regular file, the
installer refuses with `manifest_diverged` when run from the checkout.

**From a wheel you build.** In a checkout of this repository, build the wheel
from `packages/tooling` alone, then install it into a new virtual environment
(Python 3.11 or later). The install fetches the wheel's pinned dependencies from
the package index.

```sh
venv=/absolute/path/to/kp-agent-install   # a directory that does not exist yet
python3 -m pip wheel --no-deps --wheel-dir dist ./packages/tooling
python3 -m venv "$venv"
"$venv/bin/pip" install ./dist/kp_agent_tooling-<version>-py3-none-any.whl
"$venv/bin/kp-agent-install" plan --help
```

Verified with these commands at 0.4.0, in a fresh clone of this repository with
Python 3.12 and `venv` a new directory: `pip wheel` wrote one wheel, and the
install and `kp-agent-install plan --help` each exited 0.

**From a source checkout.** Run `pip install -e packages/tooling`. The installer
still reads the packaged copy. It falls back to the checkout's `deploy/` only if
the package data is missing.

**From the product image, with no Python on the host.** `plan` and `apply` run
in the `product` image itself. Bind-mount the runtime root, every repository and
every transcript root at its identical absolute path, and pass `--uid`, `--gid`
and `--home` explicitly: the container cannot see the host user, and its own home
is `/state`. Without `--home`, the container plans `/state` as `inputs.home`,
renders the `claude` and `codex` profiles disabled, and its `plan_sha256` differs
from the host's. The transcript-root mounts make `$home` exist in the container.
With `root`, `image`, `product`, `home`, `project` and `port` set as in the
[first run](#first-run-from-an-empty-root):

```sh
mkdir -p "$home/.claude/projects" "$home/.codex/sessions"
mkdir -m 700 "$root"
install_args=(--runtime-root "$root" --image "$image" --repository product="$product"
  --components tooling,refresh,capture,board --uid "$(id -u)" --gid "$(id -g)"
  --board-port "$port" --project-name "$project" --home "$home"
  --transcript-root "$home/.claude/projects" --transcript-root "$home/.codex/sessions")
in_image=(docker run --rm --network none --cap-drop ALL --security-opt no-new-privileges
  --user "$(id -u):$(id -g)" --entrypoint kp-agent-install
  --mount type=bind,source="$root",target="$root"
  --mount type=bind,source="$product",target="$product",readonly
  --mount type=bind,source="$home/.claude/projects",target="$home/.claude/projects",readonly
  --mount type=bind,source="$home/.codex/sessions",target="$home/.codex/sessions",readonly
  "$image")
"${in_image[@]}" plan "${install_args[@]}" > plan.json
"${in_image[@]}" apply "${install_args[@]}" --expected-plan-sha256 "$(jq -r .plan_sha256 plan.json)"
kp-agent-install prepare --runtime-root "$root"
kp-agent-install verify --runtime-root "$root"
```

`prepare` and `verify` still run on the host, with the installer from one of the
paths above: this container has no Docker access, and `prepare` creates the store
volume with Docker ([Prepare the store volume](#prepare-the-store-volume)).

Verified 2026-10-08 on Docker Desktop for macOS, with a `product` image built from
this tree and every path physical: the container's `plan_sha256` equals that of
`kp-agent-install plan` run on the host with the same arguments, so the rendered
root is the one a host-side install renders. `apply` exits 0, `prepare` reports
`prepared` and `verify` reports `verified`. A path with a symlinked component gives
another plan: the host resolves it, while the container sees only the mount target.
The image-marked test `tests/install/test_b65_product_image_installer_image.py`
runs this block as written.

### Plan and apply

Create the runtime root on physical storage. It must exist, be empty, and have no
symlinked path component: name the volume by its physical mount path, never through
a symlink to it.

```sh
root='/absolute/physical/path/agent-tooling-runtime'
mkdir -m 700 "$root"
kp-agent-install plan \
  --runtime-root "$root" \
  --image 'ghcr.io/saigid-1/agent-tooling@sha256:<digest>' \
  --repository product=/absolute/path/to/product \
  --components tooling,refresh,capture,board \
  --uid "$(id -u)" --gid "$(id -g)" \
  --board-port 3487 \
  --project-name agent-tooling-2 > plan.json
```

Other options:

- `--repository KEY=PATH` repeats.
- `--transcript-root PATH` repeats. It adds a native transcript root, such as
  `~/.claude/projects` or `~/.codex/sessions`, to the capture overlay. Each must
  be an existing directory.
- `--home PATH` names the home directory that `~/` in the harness profiles expands
  against (recorded as `inputs.home`). It must be an absolute, existing directory.
  Without it, the invoking user's `$HOME` applies. `verify` never compares it with
  the `$HOME` of whoever runs `verify`.
- `--components` defaults to `tooling`. `--uid` and `--gid` default to the
  invoking user; 0 is refused.
- `--board-agents` is for the `agents` image, whose board runs the Claude and Codex
  CLIs itself (recorded as `inputs.board_agents`). It mounts every `--repository`
  read-write at its host path into the `board` role only, so the board can create
  and remove task worktrees there; every other role keeps its mounts. It requires
  the `board` component: without it, `plan` refuses with
  `board_agents_without_board`. Without the flag the board gets no repository.

`plan` writes nothing. It prints JSON with:

- `inputs`, including each repository's HEAD commit;
- the rendered `files` with their sha256 and the `directories`;
- `operator_files`: the paths only you write, per component (see
  [Operator files](#operator-files-and-readiness));
- `memory_store`: the store volume `<project>_memory`, its mount point
  `/state/memory`, and its owner and mode;
- `plan_sha256`, which covers everything above;
- `preview`, which `plan_sha256` does not cover. It says what apply would create,
  replace or leave unchanged, and lists warnings. Its `memory_store` lists the
  pre-T9b store directories still on the bind (`store_directories_on_bind`, from
  directory listings only) with where `prepare` will move them, and every operator file
  that names a store path outside `/state/memory`
  (`operator_files_naming_old_store_paths`, each with its `required` path).

Identical inputs give the same hash. Changing any input, a repository HEAD or
the manifest gives a different one.

Review the plan, then apply exactly that plan:

```sh
kp-agent-install apply <the same arguments> --expected-plan-sha256 "$(jq -r .plan_sha256 plan.json)"
```

Apply refuses with no writes when:

- the hash differs;
- the root is neither empty nor a previous installation that matches its receipt;
- the root contains a symlink (outside container-owned runtime data);
- a file exists that differs from both the plan and the receipt.

Directories are 0700 and files 0600. Re-applying the same plan changes no bytes
and reports `"status": "unchanged"`. `apply` never calls Docker: it creates no volume
and pulls nothing. Its output repeats `memory_store` from the plan; the next step is
[`prepare`](#prepare-the-store-volume).

Each file is written to a temporary sibling and renamed into place, and
`receipt.json` is written last. If a write fails, for example because the disk is
full, apply rolls back:

- it removes every file and directory it created;
- it restores every file it replaced;
- it reports `"status": "failed"` with `"rollback": "complete"`.

A failed first apply therefore leaves the root empty, and a failed upgrade leaves
the previous installation intact. Re-run the same apply once the cause is fixed.
If the rollback itself cannot write, it reports `"rollback": "incomplete"` and
lists the paths in `left_behind`. Remove or restore those paths, then run `verify`.

### Prepare the store volume

Run `prepare` after `apply` and before `up`:

```sh
kp-agent-install prepare --runtime-root "$root"
```

"Prepared" is one observable state: the volume `<project>_memory` exists, its root is
owned by the planned UID:GID with mode 0700, and no pre-T9b store directory is left on
the bind. `prepare`, `verify` and each role's preflight use that test; the receipt only
records it. In order, `prepare`:

0. reports `"status": "prepared"` and changes nothing when the root is already
   prepared, without any stop;
1. refuses `writers_running` while any container of the project runs, one-off
   `docker compose run` containers included, naming them and the stop command. A role
   waiting on a store refusal is a running container too: stop it first
   ([Restarts and refusals](#restarts-and-refusals));
2. refuses `docker_unreachable`;
3. refuses `image_absent` when the runtime image (the receipt's `image`) is not present
   locally; it inspects, never pulls;
4. creates the volume where absent, with Compose's project labels so `up` adopts it, and
   has a one-off root container of the runtime image (`CHOWN` and `FOWNER` only, no
   network) chown its root to the planned UID:GID and chmod it 0700, nothing else;
5. moves every pre-T9b store directory into the volume
   ([Moving existing stores](#moving-existing-stores-into-the-volume));
6. records the outcome in `receipt.json` under `memory_store`.

Nothing is created before steps 1 to 3 pass. A refusal in step 5 (`source_damaged`,
`host_files_beside_populated_volume`, `copy_or_check_failed`) leaves the bind and the
volume as they were: a volume this run created is removed again. `prepare` cannot see
processes on the host: stop any host process that opens the store yourself.

The runtime root then contains:

| Path | Purpose |
|---|---|
| `$root/.env` | `AGENT_*` settings plus `COMPOSE_FILE` and `COMPOSE_PROFILES`. Do not edit it. |
| `$root/compose.yaml` | Byte-identical copy of `deploy/compose.yaml`. |
| `$root/compose.workspaces.yaml` | Read-only repository binds at `/workspaces/<key>` for `tooling` and `refresh`. With `--board-agents`, also the `board` role's read-write repository binds at their host paths, and its launch profiles (below). |
| `$root/compose.capture.yaml` | Read-only transcript roots and repositories for `capture`, at their host paths. |
| `$root/config/navigation.json` | Schema `ops.agent-tooling.v1` with container paths, pinned to each repository's planned HEAD. Its enabled tools are core tools only. |
| `$root/state/.ops-tooling-volume` | The state marker `ops-tooling-state-v1`, required by the health check. |
| `$root/state/{tmp,snapshots,search,delivery,models}` | Runtime state. `HOME` is `/state`. Offline models belong in `$root/state/models`. |
| `$root/state/memory` | Only the mount point of the store volume `<project>_memory`, which the roles see at `/state/memory`. Docker creates it, empty, at the first `up`. Leave it empty: a role never sees what the host puts there. |
| `$root/state/<name>.migrated-<12 hex>` | After `prepare` moved a pre-T9b store directory (`memory`, `registry`, `assistant`, `desk-memory`) into the volume: that directory as it was, renamed, never deleted by the installer. The suffix is the first 12 characters of the receipt's `plan_sha256`. |
| `$root/host/claude-mcp.json`, `$root/host/codex-mcp.toml` | MCP registration snippets. |
| `$root/host/kp-agent-host.json` | The `kp-agent-host` adapter config for this runtime: docker mode, container `<project>-capture`, registry config `/config/launch/registry.json`, the spool and the planned transcript roots ([HOST-ADAPTER.md](HOST-ADAPTER.md)). |
| `$root/config/launch/harness-profiles.json` | The packaged harness profiles with `~/` capture roots expanded against the planning user's home (`inputs.home`). A profile whose root is not under a planned `--transcript-root` is rendered disabled, and the plan warns. Name it as `harness_profiles_path` in the launch registry's descriptor. |
| `$root/config/launch/board-harness-profiles.json` (board) | The packaged harness profiles with `~/` capture roots expanded against `/state`, the home of the CLIs the board itself runs. With `--board-agents` the `board` role sees this file at `/config/launch/harness-profiles.json`, so the descriptor's one `harness_profiles_path` serves host launches and board launches. |
| `$root/spool/` | Host-adapter hook events, appended on the host and read by `capture` at `/spool`. |
| `$root/config/github-token` (refresh) | Empty mount point. The credential itself goes in `$root/secrets/github-token`. |
| `$root/secrets/github-token` (refresh) | Operator-owned. Apply creates it empty (0600) only where it is absent, so the first `up` can bind it, and never replaces, reads or hashes it. Empty serves public remotes; write your token here for private ones. |
| `$root/telemetry/*.yaml`, `$root/tempo/` (telemetry) | Collector and Tempo configuration and storage. |
| `$root/receipt.json` | The plan hash, the sha256 and mode of every written file, the `operator_files` (no digest), and `memory_store`: the volume, its owner and mode, a record of the last successful `prepare`, and every migration with its counts. Written last; `prepare` updates `memory_store`. |

`kp-agent-install verify --runtime-root "$root"` re-hashes the root against the
receipt. It exits 0 when the root matches, or 1 with a `drift` list naming each
changed, missing, symlinked or re-moded path. Its `readiness.memory_store` reports the
store like `not_configured` (it never changes the status or the exit code). The state
is observed, never read from the receipt: `unprepared` while a pre-T9b store directory
is left on the bind (seen on the host, no Docker needed); otherwise, with Docker
reachable and the receipt's image present locally, `prepared` or `unprepared` as the
volume is; otherwise `not_observed`, naming why (Docker unreachable, or the image
absent). With the image absent, its `reasons` also say whether the volume is present,
from `docker volume inspect` alone; the status stays `not_observed`. A receipt written
before the store volume reports `unprepared`: re-run `plan` and `apply`. Every state
but `prepared` carries its `reasons` and the `next` step, and each lists `operator_files_naming_old_store_paths`. Files the operator adds are not
tracked: `$root/config/refresh.json`, `$root/config/capture/*`,
`$root/config/board/*`, `$root/config/launch/registry.json` and its descriptor,
`$root/secrets/github-token`, and the contents of `$root/spool/`. `verify` also
reports their readiness, below.

### Operator files and readiness

These files are yours. The installer never creates them with content, replaces or
hashes them; `plan` and the receipt list them under `operator_files`.

| Component | File | Until it is written |
|---|---|---|
| `refresh` | `$root/config/refresh.json` | `refresh` logs `not_configured` and waits. |
| `refresh` | `$root/secrets/github-token` | Apply creates it empty (0600) where absent. If you delete it, `refresh` cannot be created (its bind source is missing): re-run apply or write the token. |
| `capture` | `$root/config/capture/session.json`, `$root/config/capture/workspace-policy.json` | Workspace capture logs `not_configured` and waits. Spool ingestion and host launches still run. |
| `capture` (host launches) | `$root/config/launch/registry.json` | `kp-agent-host` docker-mode launches are refused ([HOST-ADAPTER.md](HOST-ADAPTER.md)). |
| `board` (desks) | `$root/config/launch/registry.json`, the same file | The Desks menu reports that the desk registry is not configured, and a desk task does not start (the board shows why). The board keeps serving, and a task without a desk starts as before. Once the file is written, the next Desks menu request and desk task use it: no restart is needed. |
| `board` (optional) | `$root/config/board/session-import.json` | The import dialog reports that it is unconfigured. |
| `summarizer` | `$root/config/summarizer/approval.json`, `$root/config/summarizer/gateway.json` | `summarizer` logs `not_configured`, naming what is missing, opens no connection and waits. |
| `summarizer` | `$root/secrets/summarizer.key` | Apply creates it empty (0600) where absent. Until it holds the provider key, `summarizer` reads `not_configured` (`key_file`). |
| `summarizer` (optional) | `$root/config/summarizer/summarizer.env` | The interval stays at its default, 900 seconds. |

`plan`, the receipt and `verify` name these files relative to the runtime root,
without the `$root/` prefix.

A file counts as written once it exists and is not empty (the token placeholder
counts once it exists). `verify` prints a `readiness` entry for every selected
component (`ready` or `not_configured`, with `missing`), the list `not_configured`,
and, for `capture`, a `host_launch` entry, and for `board`, a `desks` entry; both
name the registry, `$root/config/launch/registry.json`, while it is unwritten. Those two roles keep
running without it, so it never makes the role itself `not_configured`. In `plan`
and the receipt the registry is one `operator_files` row: with both components
selected, the row is capture's, and the board's use is listed under `also`. `verify`
checks presence only; each role checks the contents when it starts. A role that is
`not_configured` does not make `verify` fail: it still reports `"status": "verified"`
and exits 0 when nothing drifted.

## Start, stop, status

`.env` names the Compose files with absolute paths, so any working directory works:

```sh
docker compose --project-directory "$root" up -d tooling   # navigation and memory only
docker compose --project-directory "$root" up -d           # tooling plus the selected profiles
docker compose --project-directory "$root" ps
docker compose --project-directory "$root" logs --tail 30 refresh
docker compose --project-directory "$root" stop            # keeps containers
docker compose --project-directory "$root" down            # removes containers, never state
```

Never add `-v` (`--volumes`) to `down`: `docker compose down -v` deletes the
`<project>_memory` volume, and every store in it.

Right after apply, `up -d` creates every selected role, whatever operator files
are still missing. A role without its operator files stays up, does not restart,
and logs one JSON line with `"status": "not_configured"`. Its `missing` list names
each file relative to the runtime root, and `missing_paths` names it as the
container sees it (under `/config`). It checks again every 10 seconds and starts its work once they are written; no
restart is needed. Its health check then reports `ready`.

Observed once on Docker Desktop for macOS: right after a runtime root path was
deleted and recreated, `up` briefly failed with `bind source path does not exist`.
The same command succeeded moments later. A never-used path did not show this.

`tooling` becomes healthy when the state marker and navigation configuration are
present. Health covers lifecycle and configuration only. It does not cover
provider coverage, index freshness or memory admission. A role waiting for its
operator files is healthy (it is doing what it should); its health output says
`"status":"not_configured"` and names the files. The board is healthy when its HTTP
port answers. Never delete files to stop a service.

### Restarts and refusals

`tooling`, `refresh`, `capture`, `board`, `indexer` and `summarizer` run with `restart: unless-stopped`. A role
whose main process dies (an out-of-memory kill, a crash, a Docker daemon restart) is
started again by Docker; `tooling` is included because every `docker exec`'d memory and
navigation MCP server depends on its container. The main process is the role command,
the child of the init every role runs with (`init: true`). A process killed beside it,
such as a `docker exec`'d server, leaves the container running and restarts nothing;
capture's workspace-capture command is the exception, because its supervisor exits with
it (below). A deliberate stop ends this:
`docker compose --project-directory "$root" stop` (or `down`) leaves nothing restarting,
and Docker treats `docker kill` as a deliberate stop too. One-off `docker compose run`
containers never restart. Mount the storage before starting roles: a role whose bind
source is missing is not created.

A role never restarts for a missing operator file or a store refusal; it waits:

- **Missing operator files** (`not_configured`, above): the role is healthy and starts
  its work once the files are written.
- **A store refusal.** No refusal of the role preflight exits a role: not
  `store_unprepared`, `operator_file_names_outside_store`, `store_not_project_volume`,
  `manifest_without_store_volume` or `store_not_mounted`, nor any other. The role stays
  up, logs the refusal as one JSON line (`"status": "refused"`, its `reason`, `detail`
  and `recovery`) whenever it changes, and runs the preflight again every 10 seconds. Its
  health check reports `"status": "refused"` and exits 1, so `docker compose ps` shows it
  `unhealthy`. It never starts its command until the refusal clears; then it logs
  `"status": "cleared"` and starts.
- **Which commands wait.** The command's shape decides, not how it was started: `wait`,
  `kanban`, or any command with the token `--watch` waits out every store refusal; any
  other command is refused at once with exit 3, such as the
  [status and integrity](#the-store-volume) `docker compose run` commands. So
  `docker compose run <role>` with the service's own command waits too (a one-off
  container never restarts, but it does not return until the refusal clears or you stop
  it). To get the refusal at once instead, give the one-off another command, or bypass
  the preflight with `--entrypoint`.
- **A preflight error that is not a refusal** (an unexpected `OSError`, for example from
  `stat` of `/state/memory`) is a crash: the role exits non-zero and the restart policy
  starts it again. An unreadable mount table is not such an error: it reads as no volume
  at `/state/memory`, the refusal `store_not_mounted`.

Recovery from a refusal:

- `store_unprepared` (the volume was never prepared, was deleted, or a pre-T9b store
  directory is left on the bind). A waiting role is a running container of the project,
  so `prepare` refuses with `writers_running` while it waits. Stop the roles, prepare,
  then start them:

  ```sh
  docker compose --project-directory "$root" stop
  kp-agent-install prepare --runtime-root "$root"
  docker compose --project-directory "$root" up -d
  ```

- `operator_file_names_outside_store`: edit each file the refusal names to its
  `required` path (`verify` lists them). The waiting role starts its command within one
  re-check (10 seconds) of the edit, with no stop and no `prepare`.
- `manifest_without_store_volume`: stop the roles, re-run `plan` and `apply` with this
  installer, `prepare`, then `up -d`.
- `store_not_project_volume`: the container was created with another mount at
  `/state/memory` (a stale manifest); its mounts cannot change. Remove it with
  `docker compose --project-directory "$root" down` (never `-v`) and start the roles
  from the runtime root with `up -d`.
- `store_not_mounted`: the container was created without the volume at `/state/memory`
  (a manifest from before the store volume, or edited); its mounts cannot change, so it
  waits until it is recreated. Re-run `kp-agent-install plan` and `apply` with this
  installer, then recreate the roles: `docker compose --project-directory "$root" up -d`
  with `--force-recreate`.

Workspace capture's `watch` loop does not end on an error either. A pass that raises
anything prints one JSON line, `"status": "error"` with the error's `category` (its
class family), its message capped at 512 characters, and the `attempt` number (the
count of consecutive failed passes), then waits with exponential backoff: the interval
(30 seconds in the manifest), doubled per failed attempt, at most 10 times the interval.
A clean pass resets the backoff. Only a signal (a stop) ends the loop, or
`--max-passes N` after N passes (the manifest sets none, so the role's loop is
unbounded); missing operator files park it in `not_configured`. Its supervisor, the
spool ingestion watcher, keeps its rule: when the capture command ends (a signal, or a
fault the loop cannot survive, such as an out-of-memory kill), the watcher stops and
exits with the command's status, and the restart policy starts the role again.

## The store volume

Every store a role opens is under `/state/memory`, the project's named volume
`<project>_memory` (Compose volume key `memory`), mounted in `tooling`, `refresh`,
`capture`, `board` and `indexer`: any `state_root`, the registry database and its roster, launches,
sessions and the assistant's stores. Everything else under `/state` stays on the runtime
root's bind mount.

Why a volume: on Docker Desktop, a POSIX lock (`fcntl.lockf` or `fcntl.flock`) on a
file in a host bind mount, held by one process, is granted again to a second process,
in another container or in the same one. On a named volume the second process is
refused. SQLite serializes writers and checkpoints with these locks, and the store is
opened concurrently: by capture, by its spool ingestion, and by one memory MCP server per
admitted session.

Rules:

- **The Docker runtime's store files are reached only from inside the runtime.** Do not
  copy, inspect, back up or edit them from the host, not even read-only, and do not put
  files into `$root/state/memory` on the host (the roles never see them). Status,
  integrity, backup and restore are one-off containers, below. (A host install without
  Docker opens its stores directly, by design.)
- **Store paths are under `/state/memory`.** Operator files name them there: the
  registry's `state_root` (`/state/memory/registry`) and the descriptor's `roster_path`
  (`/state/memory/registry/roles.json`), each `$root/config/sessions/*.json` and
  `$root/config/capture/session.json` `state_root`, and the assistant's store
  (`/state/memory/assistant/store`, with its launches in `launches/` under it).
  Operator-written files live under `/config`, never on the volume: the assistant's
  `config_template` among them. A role is refused, and waits with health `refused`, while
  an operator file under `/config` names a `state_root` or `roster_path` outside
  `/state/memory`, or a `config_template` anywhere under `/state`; `verify` lists each one
  with its `required` path, and the role starts within one re-check of the edit
  ([Restarts and refusals](#restarts-and-refusals)).
- **A store path outside the volume is refused at open.** Every tool in the runtime,
  including one started later with `docker exec` or `docker compose run`, refuses to open
  a store whose path does not resolve under `/state/memory`, for reading as well as
  writing: a `state_root` or `roster_path` written after the role started, a launch
  receipt, or a store a receipt names. This open-time refusal exits non-zero with the
  category `store_outside_volume` and names the path; nothing is opened or created. The
  runtime is recognized by `AGENT_MEMORY_VOLUME`, which Compose sets in every role and
  `docker exec` and `docker compose run` inherit. Paths that are not stores are never
  refused: the model gateway's ledger, `/state/refresh`, `/state/knowledge`, the
  delivery, search, snapshot, model, backup and tmp directories, and operator files such
  as `/config/launch/desks.json`. A host install without Docker is not affected.
- **`docker compose down -v` deletes the volume**, and every store in it. So does
  `docker volume rm <project>_memory`. Use `stop`, or `down` without `-v`.
- If the volume was deleted, run `prepare` again before `up`: Compose would otherwise
  create it empty and owned by root, and every role would wait, refused
  (`store_unprepared`), until you stop the roles, `prepare` and `up` again
  ([Restarts and refusals](#restarts-and-refusals)).

**Status.** What the store holds, with modes, owners and sizes. Besides the stores, the
listing shows the indexer's files ([The indexer](#the-indexer)): `indexer-health.json` at
the volume root (and, during a replace, `.indexer-health.json.tmp-<hex>`), and
`index.lock`, `episode-search.sqlite3` and, during a reindex, `episode-search.build.sqlite3`
in each state root:

```sh
# memory store: status (a one-off container; the host never opens store files)
docker compose --project-directory "$root" run --rm -T tooling \
  find /state/memory -printf '%M %u:%g %12s %p\n'
```

**Integrity.** `pragma quick_check` of every database:

```sh
# memory store: integrity (quick_check of every database, in a one-off container)
docker compose --project-directory "$root" run --rm -T tooling python3 -c '
import pathlib, sqlite3
for path in sorted(pathlib.Path("/state/memory").rglob("*.sqlite3")):
    db = sqlite3.connect(path)
    print(db.execute("pragma quick_check").fetchone()[0], path)
    db.close()'
```

Each line starts with `ok` for a sound database.

**Backup.** Stop the writers first (`docker compose --project-directory "$root" stop`).
The command opens each database read-only and copies it with the SQLite backup API,
which includes its WAL, and every other file as is, into
`$root/state/backups/memory-<UTC time>/`, and prints that path as the container sees
it. It covers `/state/memory` and, on a root not yet prepared, the pre-T9b store
directories `/state/registry`, `/state/assistant` and `/state/desk-memory`, so it is
also the upgrade's backup step. Keep the backup private; treat it like `$root/state/`.

```sh
# memory store: backup (writers stopped; a one-off container writes it under $root/state/backups)
docker compose --project-directory "$root" run --rm -T tooling python3 -c '
import os, pathlib, shutil, sqlite3, stat, time
target = pathlib.Path("/state/backups", time.strftime("memory-%Y%m%dT%H%M%SZ", time.gmtime()))
os.makedirs(target.parent, mode=0o700, exist_ok=True)
os.makedirs(target, mode=0o700)
for name in ("memory", "registry", "assistant", "desk-memory"):
    source = pathlib.Path("/state", name)
    if not source.is_dir():
        continue
    for path in sorted(source.rglob("*")):
        copy = target / name / path.relative_to(source)
        os.makedirs(copy.parent, mode=0o700, exist_ok=True)
        if path.is_dir():
            os.makedirs(copy, mode=0o700, exist_ok=True)
        elif path.name.rpartition("-")[0].endswith(".sqlite3"):
            continue  # a -wal, -shm or -journal file: included in its database copy
        elif path.suffix == ".sqlite3":
            db, out = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True), sqlite3.connect(copy)
            db.backup(out)
            out.close()
            db.close()
            os.chmod(copy, stat.S_IMODE(path.stat().st_mode))
        else:
            shutil.copy2(path, copy)
print(target)'
```

**Restore.** With every role stopped and removed (`docker compose --project-directory
"$root" down`, without `-v`), delete the volume (`docker volume rm <project>_memory`),
run `kp-agent-install prepare --runtime-root "$root"` (it creates the volume empty,
owned and 0700), then copy the backup's `memory` directory in and start the roles:

```sh
# memory store: restore a backup into the empty volume (one-off container)
docker compose --project-directory "$root" run --rm -T tooling python3 -c '
import shutil, sys
shutil.copytree(sys.argv[1] + "/memory", "/state/memory", dirs_exist_ok=True)' /state/backups/memory-<UTC time>
```

### The indexer

Search reads each store's index, `episode-search.sqlite3` beside its `episodes.sqlite3`.
One process writes it: the `indexer` role. Every seal (a capture, an import, a host card,
a claim or a link) appends a row to the store's index outbox in the same transaction, and
the hook that sealed returns without opening the index. Every 2 seconds the indexer walks
`/state/memory` for stores (it skips `launches/` directories and symlinks, and walks at
most 10,000 directories per pass, in sorted order: a store beyond that bound is not
drained) and, for each, takes the lease `<state_root>/index.lock`
without waiting (a held lease is a skip, not an error), applies the outbox rows above the
index's watermark, at most 500 per index transaction, and deletes the applied rows. A
new capture is searchable within one or two intervals. Inside the runtime no other
process drains: not a hook, a capture pass, the board or the model gateway.

- **Selection.** `plan` selects `indexer` whenever `capture` or `board` is selected
  (both seal). A runtime root planned before the indexer has no `indexer` service: its
  seals wait in the outbox and search does not see them. `verify` names that gap under
  `gaps` (`indexer_missing`, with the components that seal); the step is `plan`, `apply`,
  then `docker compose --project-directory "$root" up -d`. The upgrade writes nothing into
  the stores: the outbox is created empty on the first write, and everything sealed
  before it was indexed then. `kp-agent-desk ... upgrade-sources` requests one reindex
  only for an older-writer store, one whose index does not cover every sealed episode
  (an absent index covers nothing, so a store with sealed episodes and no index is
  requested one on the first run; an empty store requests none).
  A process that sets `AGENT_MEMORY_VOLUME` outside Compose seals and never drains; only
  the `gaps` line names it.
- **Lag and logs.** Each drain prints one JSON line (`docker compose --project-directory
  "$root" logs --tail 20 indexer`): `status` (`drained`, `idle`, `skipped`, `error`),
  `applied_rows`, `watermark`, `index_lag` (the outbox rows not yet applied),
  `advanced` (whether this drain moved the watermark, on an error too), `coverage` (when
  marks were lost; see below) and `health`. `memory.connection_status` reports the same
  `index_lag` beside `status`.
- **Health.** The role is unhealthy while a store's watermark has not advanced across 5
  consecutive drains (lease skips excepted) with outbox rows waiting: the drain keeps
  failing on the same rows. A store whose outbox cannot be read at all (its line shows
  `index_lag` null) counts as rows waiting. A catch-up, with the watermark moving, stays
  healthy. Read the cause from the `error` lines in its log. The health check reads
  `indexer-health.json`, which the role writes when it starts and whenever its health
  changes; an absent record, or one written by a process that is no longer running, reads
  unhealthy. A failed write of that file is logged (`"scope": "health_write"`) and retried
  on the next pass; it never stops the role.
- **Lost coverage marks.** When a drain fails after it has applied a batch to the index
  (marking the batch's episodes as covered failed: for example `coverage_token_unstable`,
  or the store busy), the store keeps a `coverage_marks_lost` record. Every line for that
  store then shows `coverage: lost` with the failure's `category` and `seq`, and the role
  is unhealthy while the record is there (the health record lists the store under
  `coverage_lost`), whether or not rows are waiting and across restarts of the role. The
  record holds until the whole store is re-marked: a reindex
  (`kp-agent-desk --config <session.json> index-history`) or
  `kp-agent-desk --config <session.json> upgrade-sources`. A new seal does not clear it:
  its drain marks only the new episode. If the record itself could not be written (the
  store busy), the line shows `coverage_record` with `status: unwritten`, and the role
  writes it again on the next pass.
- **Files it keeps.** At the volume root, `indexer-health.json` is the role's health
  state, which its health check reads. It is replaced whenever the health changes,
  through a sibling `.indexer-health.json.tmp-<hex>` that exists only during the
  replace. In each store's state root, beside `episodes.sqlite3`, are
  `episode-search.sqlite3` (the index), `index.lock` (the lease; its content names the
  last holder) and, only during a reindex, `episode-search.build.sqlite3`. None of these is a
  stray file or a store to move. The indexer writes them only once its store preflight
  passes, so after `prepare`; `verify` never reports them as drift, and deleting the
  health file or the lease only resets them (the next pass writes them again).
- **Reindex.** `kp-agent-desk --config <session.json> index-history` (and
  `import-native-history --apply`) only requests a reindex: an outbox row. A host card
  requests none: its capture and claims are drained like any other seal. The indexer builds a new index file beside the old one, from every sealed row,
  and renames it into place; readers keep reading the old file until the rename, and
  nothing is deleted in place. Inside the runtime the request's own output reports 0
  indexed episodes: the indexer applies it within its interval (its log line shows
  `reindex`). A host install drains at once.
- **Search by scope.** The index keeps each episode's desks and tenant beside its text
  (its postings), so a desk or topic search matches its own scope's episodes before
  anything is ranked: text in other desks and tenants is never ranked or read, and the
  ranking is the same as before. A claim or a link changes an episode's desks in the store at
  once. The indexer rewrites the postings when it applies that outbox row; until then a
  search reads the waiting row in the store, so the next search sees the reassignment
  either way.
- **Index schema upgrade.** The index file records its schema version. When the indexer
  finds an index whose version is not its own (an index built before postings), it writes
  that store's `reindex` row itself and builds the new file as any reindex does. This is
  the one reindex the indexer requests on its own: an empty or absent index gets none
  (that is `upgrade-sources`' or an operator's request). The old file answers every search
  until the rename, at its old cost (a search ranks every match in the store). The build
  reads every sealed row, as any reindex does: on a store of about 228,000 episodes a
  reindex took about 162 s from start to rename at the indexer's 0.5 CPU and peaked near
  350 MB (measured 2026-10-05, before postings). Postings add about a quarter to the build
  time (a third at most in three paired builds), about 6% to its peak memory and about
  200 bytes per event to the index file (measured 2026-10-06 on a 100,000-episode store, one
  process, unlimited CPU: median 15.2 s to 19.3 s, peak 72 MB to 76 MB, index 48 MB to
  68 MB). Expect about 205 s, up to about 220 s, from start to rename on a store of that
  size.
  After the `up` that brings the new schema, expect, per store whose index predates it:
  - the indexer writes one `reindex` row;
  - `index_lag` reads 1 for the whole build, then 0 (a seal written during the build adds
    its own row until the next drain); the loop makes no passes during that one drain, and
    its first lines read `reindex`, not `idle`;
  - health reads healthy throughout: no passes means no stalls;
  - `upgrade-sources` still reports `reindex_requested: false`: the request is the
    indexer's own, after `up`;
  - a deploy watcher waiting on the indexer is bounded by the measured build time plus a
    margin, not 60 s.

  An image from before postings cannot read or rebuild the new file: after a rollback its
  searches read the index as unavailable and its drains fail. Stop the indexer, move that
  store's `episode-search.sqlite3` aside in a one-off container, request a reindex
  (`index-history`), then start the indexer.
- **Memory during a reindex.** The indexer runs at `cpus: "0.5"` and `mem_limit: 512m`. A
  build holds a digest for every episode plus SQLite's caches: on a store of about 228,000
  episodes it peaked near 350 MB (measured 2026-10-05), so a store roughly half again as large
  can exceed the limit. The role is then killed mid-build and restarted, the reindex request is
  still waiting, and the build starts over. Its health does NOT show this: the stall count lives
  in the process that is killed, so every restart starts it at 0 and the role can keep reading
  healthy. Docker shows it instead: `docker compose --project-directory "$root" ps indexer`
  shows the indexer restarting, `docker inspect --format '{{.RestartCount}}'` on its container
  counts the restarts, and its log ends mid-build each time. Raise the running indexer's limit,
  then let it build:

  ```sh
  docker update --memory 1g --memory-swap 1g \
    "$(docker compose --project-directory "$root" ps -q indexer)"
  ```

  The raised limit holds across restarts of that container until it is recreated (a later
  `up -d` that changes the role, or `down`); a supported setting for it is not yet part of
  the manifest.
- **A refused reindex.** When the store's coverage marks claim episodes the new build
  does not hold with their sealed digest, the reindex is refused, the old index is kept,
  and the refusal names the episodes. Check that store's integrity first, inside the
  runtime, then follow [Moving existing stores](#moving-existing-stores-into-the-volume)'s
  recovery for a damaged database, or restore a [backup](#the-store-volume):

```sh
# one store's integrity, inside the runtime (the path is the refusal's store)
docker compose --project-directory "$root" exec -T tooling python3 -c '
import sqlite3, sys
db = sqlite3.connect("file:" + sys.argv[1] + "?mode=ro", uri=True)
print(db.execute("pragma quick_check").fetchone()[0], sys.argv[1])
db.close()' /state/memory/registry/episodes.sqlite3
```

  `ok` is a sound database; anything else names the damage. The [integrity
  command](#the-store-volume) checks every database in the volume.

### Moving existing stores into the volume

A runtime root installed before the volume keeps its stores on the bind:
`$root/state/memory/*`, `$root/state/registry` (T7b-era installs), the
`$root/state/assistant` store and `$root/state/desk-memory`, plus any other
`$root/state/<name>` an operator file names as a store path. `prepare` moves each one,
once, into the volume at its new path: `state/memory/*` to `/state/memory/*`, and
`state/<name>` to `/state/memory/<name>`. When a directory on the bind holds files and
the volume is empty or absent:

1. A one-off container, running as the planned UID:GID with no capability and no
   network, mounts the source directories read-only and checks every source first.
   Each `*.sqlite3` is recovered from a private copy: the database and whichever of its
   `-wal`, `-shm` and `-journal` files exist are copied into the container's own `/tmp`
   and opened read-write there, so SQLite applies committed WAL frames and rolls back a
   hot journal, as it would after a crash. The recovered copy must pass
   `pragma quick_check` (else `source_damaged`, naming the database). Every entry must
   be a file or directory owned by the planned UID:GID (else `copy_or_check_failed`).
   The sources on the bind are only read, never opened by SQLite or changed. Nothing is
   created until this passes.
2. The volume is created and owned (step 4 above).
3. A one-off container running as the planned UID:GID, with no capability and no
   network, mounts the source directories read-only and the volume, and stages a copy
   in the volume. Each `*.sqlite3` is recovered from a private copy in the container's
   `/tmp`, as in step 1, and copied with the SQLite backup API from that recovered copy,
   so committed transactions still in its WAL are included and a hot journal's
   uncommitted changes are not; no `-wal`, `-shm` or `-journal` file is ever copied
   into the volume. The copy must pass `quick_check`, and the content digest of each
   ordinary table (its rows in primary-key or rowid order) must equal the recovered
   source's. Every other file is copied byte for byte
   and checked, with its mode. Only then do the staged entries move into place and take
   their source modes.
4. Each source directory is renamed `$root/state/<name>.migrated-<12 hex>` and never
   deleted.
5. The receipt records the migration under `memory_store.migrations`: what moved where,
   the counts of files, directories and bytes, each database's per-table rows and
   digests, and a digest of the copied manifest. `prepare` prints the same under
   `migration`.

Any failure (`copy_or_check_failed`) removes what was staged, removes a volume this run
created, and leaves every source directory with its name and contents. A re-run when
the volume already holds the store copies nothing. Nothing is ever copied over a
populated volume: when a directory on the bind holds files and the volume holds a
store, `prepare` refuses with `host_files_beside_populated_volume`, naming both.

**Operator files are never rewritten.** `apply`, `prepare` and `verify` list each
operator file that names an old store path, with its `required` path, under
`operator_files_naming_old_store_paths`. Edit them before `up`; until then every role
waits, refused (`operator_file_names_outside_store`), and starts within one re-check
(10 seconds) of the edit.

**Sessions launched before the migration must be relaunched.** Their launch receipts
and per-launch memory configurations record the old absolute paths, so their hooks now
refuse (the receipt or its `memory_config` no longer resolves) and write nothing.

**A damaged source database.** `source_damaged` names a database that fails
`quick_check` even after the recovery above, or cannot be opened at all. A database
left by a crash, with committed frames only in its `-wal` or with a hot journal, is
not damaged: `prepare` recovers it. With every role stopped, restore the damaged
database from the backup taken before the upgrade, or move it and its `-wal`, `-shm`
and `-journal` files out of the store directory, then run `prepare` again. Both are
one-off containers of the runtime image that mount the bind, for example:

```sh
image="$(jq -r .image "$root/receipt.json")"
docker run --rm --network none --user "$(id -u):$(id -g)" -v "$root/state:/state" \
  --entrypoint sh "$image" -c 'mkdir -p -m 700 /state/quarantine && mv /state/registry/<database>* /state/quarantine/'
```

**When to remove `<name>.migrated-<12 hex>`.** It is the store as it was before the
migration. Remove it only after the upgraded runtime's `verify` reports
`readiness.memory_store` `prepared` with no operator files listed, and one backup with
the command above has completed. Do not open its files from the host meanwhile.

**Known limits (out of scope here).** These locks stay on binds: the refresh
double-start guard `.refresh.lock` (`refresh_cli.py:777`), the knowledge lifecycle locks
under `/state/knowledge`, and the assistant binding lock on its `/config` file
(`assistant_host_cli.py:136`). A store opened after the role started is checked at open,
not by the role preflight: the open-time refusal (`store_outside_volume`, above) covers
it. The preflight recognizes the project volume by its mount-table
root ending in `/volumes/<project>_memory/_data`, as Docker's `local` driver mounts it: a
volume plugin, or a volume on its own filesystem, is refused (`store_not_project_volume`).
The supported boundary is one trusted user on one host.

## First run from an empty root

This is the whole sequence for the `tooling,refresh,capture,board` role set. It
ends with every role healthy, a portable desk registry with one desk, one bound
session, workspace capture running and the model gateway's `doctor` reporting
`ready`. [DESK-MENU.md](DESK-MENU.md#in-the-docker-runtime),
[MODEL-GATEWAY.md](MODEL-GATEWAY.md#in-the-docker-runtime) and
[HOST-ADAPTER.md](HOST-ADAPTER.md#docker-runtime-installer) explain the files it
writes. Run it in one bash or zsh shell: set the variables, then paste each step
as written.

```sh
umask 077   # every file below is created private; the roles refuse group- or world-readable configuration
root='/absolute/physical/path/agent-tooling-runtime'   # must not exist yet
product='/absolute/path/to/product'   # a committed Git repository root whose origin refresh can reach
home="$HOME"                          # the home that holds .claude/projects and .codex/sessions
project='agent-tooling-2'
port=3487                             # the board's loopback port
tenant='my-team'
checkout='/absolute/path/to/agent-tooling'   # a checkout of this repository, for step 0
```

Run the steps from any working directory; step 1 writes `plan.json` there.

**0. The image.** Pull the published `product` image by digest before `plan`:
`docker pull ghcr.io/saigid-1/agent-tooling@sha256:<digest>`, with the `product` digest
from the release page. The page lists, per target, one manifest list for both
architectures (linux/amd64 and linux/arm64) and each architecture's own digest; either
works. `--image` in step 1 takes that same reference. The installer never pulls, so the
image must be present before `prepare`.

```sh
image='ghcr.io/saigid-1/agent-tooling@sha256:<digest>'   # the product digest from the release page
docker pull "$image"
```

The published images are `runtime`, `product`, `ops` and the optional `opencode`
([Images](#images)). `agents` is never published: build it from a checkout.

Or build `product` from the checkout and use its image ID instead:

```sh
docker build -f "$checkout/deploy/Dockerfile" --target product \
  --build-arg SOURCE_REVISION="$(git -C "$checkout" rev-parse HEAD)" -t agent-tooling:product "$checkout"
image="$(docker image inspect --format '{{.Id}}' agent-tooling:product)"
```

Either way, the image's `tooling.identity` reports its revision (`status: known`,
origin `image-build-declaration`), the same value as its
`org.opencontainers.image.revision` label. A published image's revision is the commit
its release tag points at.

**1. Plan, apply and verify.** Both transcript roots must exist; an empty one is
fine. Capture mounts them read-only.

```sh
mkdir -p "$home/.claude/projects" "$home/.codex/sessions"
mkdir -m 700 "$root"
args=(--runtime-root "$root" --image "$image" --repository product="$product"
      --components tooling,refresh,capture,board --uid "$(id -u)" --gid "$(id -g)"
      --board-port "$port" --project-name "$project" --home "$home"
      --transcript-root "$home/.claude/projects" --transcript-root "$home/.codex/sessions")
kp-agent-install plan "${args[@]}" > plan.json
kp-agent-install apply "${args[@]}" --expected-plan-sha256 "$(jq -r .plan_sha256 plan.json)"
kp-agent-install prepare --runtime-root "$root"
kp-agent-install verify --runtime-root "$root"
```

`prepare` reports `"status": "prepared"`: it created the volume `${project}_memory`,
owned by you, mode 0700, with nothing to migrate. `verify` reports
`"status": "verified"`, `readiness.memory_store` `prepared`, and lists `refresh` and
`capture` under `not_configured`, with the files each one waits for.

**2. Start every role.** Nothing else is needed for the containers to be created.
`--wait` returns once every role is running and healthy.

```sh
docker compose --project-directory "$root" up -d --wait
docker compose --project-directory "$root" ps
docker compose --project-directory "$root" logs refresh capture
```

Every container is up and healthy. `refresh` and `capture` log `not_configured`,
naming their files, and wait; their health output says the same.

**3. The desk registry.** `initialize` creates the registry's stores in an
existing `state_root`; it does not create the directory itself. The `state_root` is
in the store volume, so it is created inside the runtime.

```sh
docker compose --project-directory "$root" run --rm -T tooling sh -c 'mkdir -m 700 /state/memory/registry'
cat > "$root/config/launch/desks.json" <<EOF
{"schema_version": "agent-tooling.desk-registry.v1", "tenant_id": "$tenant",
 "roster_path": "/state/memory/registry/roles.json",
 "harness_profiles_path": "/config/launch/harness-profiles.json"}
EOF
cat > "$root/config/launch/registry.json" <<'EOF'
{"schema_version": "ops.desk-memory.local.v1", "state_root": "/state/memory/registry",
 "catalog_path": "/config/launch/desks.json", "workspace_root": "/config/launch",
 "provider_instance": "agent-tooling", "provider_session_id": "registry-operator"}
EOF
registry=(docker exec -i "${project}-tooling" kp-agent-desk-registry --config /config/launch/registry.json)
"${registry[@]}" initialize
```

From here the board's Desks menu works too, without a restart: open
`http://127.0.0.1:$port/?view=desks` and enter the passcode from
`docker compose --project-directory "$root" logs board`. The board runs the same
`kp-agent-desk-registry --config /config/launch/registry.json` in its own container
and writes the same state under `/state/memory/registry` in the store volume, which the
`tooling` and `capture` roles read.

**4. One desk and one bound session.** The bound session is the registry
configuration's own `provider_session_id`, so search and capture can run as it.
The Desks menu's **Create desk** and **Bind session** do the same as the two
commands below.

```sh
desk="desk:$(python3 -c 'import uuid; print(uuid.uuid4())')"
printf '{"desk_id": "%s", "name": "Product", "description": "Work on the product repository.", "role": "general", "repos": ["product"], "capture": true, "memory_write": true, "expected_version": 0}' "$desk" \
  | "${registry[@]}" save
printf '{"harness": "operator", "provider": "none", "model": "none", "native_session_id": "registry-operator", "desk_id": "%s", "source": "operator", "workspace": "%s", "parent_session_id": null}' "$desk" "$product" \
  | "${registry[@]}" bind
"${registry[@]}" list
```

**5. Workspace capture.** Its session is the bound registry session. Write the
policy last: capture starts as soon as `session.json` and the policy are both
written.

```sh
cp "$root/config/launch/registry.json" "$root/config/capture/session.json"
printf '{"tenant_id": "%s", "repositories": ["product"]}\n' "$tenant" > "$root/config/capture/workspace-approval.json"
approval_sha256="$(shasum -a 256 "$root/config/capture/workspace-approval.json" | cut -d' ' -f1)"
cat > "$root/config/capture/workspace-policy.json" <<EOF
{"schema_version": "ops.workspace-capture.v1",
 "approval_record": "/config/capture/workspace-approval.json",
 "approval_sha256": "$approval_sha256", "tenant_id": "$tenant",
 "approved_repo_keys": ["product"], "repos": {"product": ["$product"]},
 "native_roots": {"claude": "$home/.claude/projects", "codex": "$home/.codex/sessions"},
 "excluded_sessions": {"claude": [], "codex": []},
 "max_candidates": 64, "max_batch_bytes": 2000000, "max_batch_rows": 500}
EOF
```

**6. Refresh.** Refresh follows the repository's `origin` default branch, so
`origin` must be reachable from the container. For a private github.com remote,
write a read-only token to `$root/secrets/github-token` first; empty serves public
remotes. Set `python_scope` to the repository's Python directories.

```sh
mkdir -m 700 "$root/config/serena"
printf 'project_name: product\nlanguage: python\n' > "$root/config/serena/product.yml"
cat > "$root/config/refresh.json" <<'EOF'
{"repositories": {"product": {"repository": "/workspaces/product", "python_scope": ["src"],
   "serena_template": "/config/serena/product.yml",
   "workspace_observation_roots": ["/workspaces/product"],
   "analysis_dependencies": [], "semantic_deadline_seconds": 900}},
 "output_root": "/state/refresh", "publication": "/state/refresh/profile.json",
 "check_status": "/state/refresh/status.json", "toolchain": "/opt/toolchain",
 "node": "/usr/local/bin/node", "semantic": false}
EOF
```

**7. The model gateway.** It runs in the `tooling` role. Its `artifact_root` must
exist before the first call: the gateway never creates it. Put your OpenRouter key
in `OPENROUTER_API_KEY` first; the file holds it, not the configuration.

```sh
mkdir -m 700 "$root/config/models" "$root/state/model-artifacts"
printf '%s\n' "$OPENROUTER_API_KEY" > "$root/config/models/openrouter.key"
cat > "$root/config/models/gateway.json" <<'EOF'
{"schema_version": "agent-tooling.model-gateway.v1",
 "artifact_root": "/state/model-artifacts",
 "providers": {"openrouter": {"kind": "openrouter", "base_url": "https://openrouter.ai/api/v1",
                              "api_key_file": "/config/models/openrouter.key"}},
 "routes": {"image.generate": {"provider": "openrouter", "model": "google/gemini-2.5-flash-image"}},
 "budgets": {"image.generate": {"max_calls_per_hour": 20, "max_usd_per_day": 2.0,
                                "confirm_over_usd": 0.10}}}
EOF
docker exec -i "${project}-tooling" kp-agent-models --config /config/models/gateway.json doctor
```

`doctor` sends no request and reports `ready` for the route.

Board tasks run with OpenCode only when the board runs the optional `opencode` image's
digest (see [Images](#images)): the `opencode` binary exists only there. Those tasks use
this same key file. The board role sees it read-only at `/config/models/openrouter.key`
(`KANBAN_OPENCODE_OPENROUTER_KEY_FILE` in the manifest) and names it to OpenCode as
`{file:/config/models/openrouter.key}`; the key is never written into the board's files,
a launch receipt or a launched process's environment.

**8. Check.** Within about a minute of steps 5 and 6:

```sh
docker compose --project-directory "$root" ps
kp-agent-install verify --runtime-root "$root"
docker compose --project-directory "$root" logs --tail 5 capture refresh
```

Every role is `(healthy)`, `verify` lists nothing under `not_configured`, and
capture logs a pass with `"status": "ok"` every 30 seconds. Host launches of the
Claude and Codex CLIs now work in docker mode: run `kp-agent-host --config
"$root/host/kp-agent-host.json" launch claude --desk "$desk"` in the repository
([HOST-ADAPTER.md](HOST-ADAPTER.md)).

**9. Search.** In that host launch, end one turn that names a word you can search
for, for example `juniper`. The launch is bound to the desk, and its hooks bring the
turn there. `kp-agent-host launch claude` writes a hook settings file for the launch
and passes it to Claude with `--settings`: one hook for each event of the Claude
profile in `/config/launch/harness-profiles.json`, the file step 3's
`harness_profiles_path` names. Each hook only appends Claude's event to the launch's
spool under `$root/spool`, and the capture role applies the spool as it arrives.
Claude's `Stop` hook fires at the end of the turn, and the capture role then ingests
the turn under the desk. When Claude exits, its `SessionEnd` hook does the same for
whatever is left. The indexer makes the turn searchable within about a minute.
Search as the bound session:

```sh
docker exec -i "${project}-tooling" kp-agent-memory --config /config/launch/registry.json \
call --tool memory.search --arguments '{"query": "juniper"}'
```

Each entry under `results` quotes the turn, with a `next_call` that reads its exact
source event: a search that answers. `index_status` reads `results` once the index
covers every episode in scope; `index_unavailable` or `incomplete_index` means the
indexer has not caught up yet, so search again. A captured session must be assigned
to a desk before its turns are searchable at desk scope, the default. The host
launch was bound to `$desk` when it started, so the turns its hooks bring are the
desk's. A harness or wrapper that never fires these hooks, such as a stub `claude`,
leaves nothing at desk scope: workspace capture assigns no desk, and only a
`"scope": "topic"` search reaches what it records
([MEMORY.md](MEMORY.md#workspace-capture-is-unresolved-evidence)).

### Board tasks with in-container agents (`agents` image)

The `agents` image's board runs the Claude or Codex CLI of a task itself, in the
board container, in a task worktree of a planned repository. The same first run
works with these changes:

- **Step 0:** build `--target agents` (tag it, for example, `agent-tooling:agents`)
  and set `image` to its ID.
- **Step 1:** add `--board-agents` to `args`. The board role then mounts each
  repository read-write at its host path (`$product` stays `$product` inside the
  board). The plan warns that board tasks can change the repositories.
- **Before the first task:** log in once (see [Provider login](#provider-login-agents-target)).
  The login lives under `$root/state/`, the `HOME` of every role, so the board's
  CLIs use it.

Then, in the board (passcode from `docker compose --project-directory "$root" logs board`):

1. Add the project at the repository's host path, `$product`.
2. Create a task, choose its **Desk** (the desk of step 4) and start it with
   Claude or Codex.

The board runs `kp-agent-launch --config /config/launch/registry.json prepare` in its
own container with `"source": "board"` ([LAUNCH-BINDING.md](LAUNCH-BINDING.md)).
Claude is bound when it launches, Codex on its first hook. The launch receipt and
memory configuration are under `/state/memory/registry/launches/<launch id>/` in the
store volume (read them with a one-off container, as in [status](#the-store-volume)). When
the task's agent stops, its `Stop` hook captures the turn under the desk (the desk
has capture on), and the session's `kp_desk_memory` MCP server answers
`memory.search` with that turn. `"${registry[@]}" list` shows the binding with
`"source": "board"`.

- A task without a desk starts exactly as before: no launch binding, no desk memory.
- Until `$root/config/launch/registry.json` is written, a desk task does not start
  and the board shows that launch binding is not configured.
- The board's CLIs write their transcripts under `/state` (their home): Claude under
  `/state/.claude/projects`, Codex under `/state/.codex/sessions`. A board launch's
  hooks look for them there because, with `--board-agents`, the board role sees
  `$root/config/launch/board-harness-profiles.json` at
  `/config/launch/harness-profiles.json`, the `harness_profiles_path` of step 3.
  The other roles, and so host launches, keep the host-path profiles. If your
  descriptor names another profiles file, its capture roots must be where the
  board's CLIs write, or board tasks are not captured.
- Task worktrees live in `$root/state/kanban/worktrees/` (`/state/kanban/worktrees/`
  in the board). Git records that container path in the repository's
  `.git/worktrees/`, so on the host `git worktree list` marks these worktrees
  prunable and `git worktree prune` would drop their records while the board still
  uses them. Do not prune them on the host; remove a task's worktree from the board.
- Without `--board-agents` the board has no repository, so it can create no task
  worktree and starts no task.

## Register a harness

The snippets name the rendered container `<project>-tooling` and run
`docker exec -i <container> kp-agent-tooling --config /config/navigation.json serve`.
MCP uses stdio: never add `-t`.

- Claude Code: merge `host/claude-mcp.json` into the project's `.mcp.json`, or run
  `claude mcp add-json <name> '<the server object>'`.
- Codex: copy `host/codex-mcp.toml` into `~/.codex/config.toml`.

If the harness starts with a reduced `PATH`, replace `docker` with its absolute
path in your copy. Stopping the container disconnects clients; reconnect them
after the next start.

Desk memory is a second MCP server in the same container. It needs one session
config per exact host session (see [MEMORY.md](MEMORY.md)):

```json
{"command":"docker","args":["exec","-i","<project>-tooling","kp-agent-memory","--config","/config/sessions/SESSION.json","serve"]}
```

Admission stays explicit (`docker exec … kp-agent-desk`). Starting a container or
registering a server admits nothing.

## Optional roles

**refresh.** Copy [`deploy/examples/refresh.example.json`](../deploy/examples/refresh.example.json)
to `$root/config/refresh.json` (0600) and adapt it. Repository paths are
`/workspaces/<key>`. Serena templates referenced from it live under `$root/config/`.
Write a read-only, narrowly scoped GitHub token to `$root/secrets/github-token` (0600).
The image's credential helper answers only `github.com` over HTTPS. For public
remotes an empty file works. Refresh publishes generations under
`/state/refresh`. The installer's `navigation.json` serves the planned HEAD pins;
re-plan to move them.

Optional refresh settings. An absent field takes its default, so existing
requests stay valid. Values outside the range are refused before any source work.

| Field | Where | Range | Default | Effect |
|---|---|---|---|---|
| `python_index_timeout_seconds` | repository | 60–3600 | 300 | Timeout for that repository's `scip-python` run. |
| `typescript_package_files` | repository | up to 32 repository-relative paths | `<typescript_prefix>/package.json` and `<typescript_prefix>/package-lock.json` when `typescript_prefix` is set, otherwise none | Published as dependency artifacts and recorded as TypeScript index provenance. |
| `min_rebuild_interval_seconds` | top level | 0–86400 | 0 (no debounce) | At most one rebuild starts per interval. A check inside it reports `pending_rebuild` with `next_eligible_at` and does no clone, index or embedding. |
| `retain_generations` | top level | 1–50 | 3 | Applies only when `snapshot_registry` is set. After each successful publish, keep the published generations, generations referenced by the snapshot registry, by a file listed in `retention_references` or by a live process, generations of unknown provenance (no `profile.json`), and at most this many minus one other complete generations, newest first. |
| `semantic_retry_after_seconds` | top level | 300–604800 | 86400 | A failed semantic build is not retried until its source revision or semantic build identity changes, or this long has passed. Readiness reports the reason and `semantic_retry_after`. |
| `tenant_id` | top level | non-blank, at most 80 bytes | the packaged compatibility tenant | Tenant written to every published repository entry. |
| `snapshot_registry` | top level | absolute directory | not configured | The same path as the navigation config's `snapshot_registry` (`/state/snapshots` in the container). Generations its review snapshots reference are never pruned. Unset, nothing is pruned. |
| `retention_references` | top level | up to 32 canonical absolute paths to files under `/config` or `/state` | none | Files whose mentioned generations are never pruned, read as they are at each pruning pass. Use it for a knowledge catalog you maintain and pin separately from refresh. A listed file that is missing, unreadable or over the read budget means that pass removes nothing. |

A repository keeps its published semantic index while its own revision and
semantic build identity are unchanged, even when an analysis dependency moves and
its SCIP index is rebuilt. A build that fails before publication always removes
its own partial generation. Pruning of other generations is fail safe: it runs only
when `snapshot_registry` is set and every reference surface is readable. Unset, the
receipt reports `status: "skipped"` with `reason: "snapshot_registry_not_configured"`
and nothing is removed. A generation without `profile.json` that is not the current
build's own is never removed; the receipt counts it under `retained_unknown`.

A knowledge catalog you maintain and pin separately from refresh (for example the
`catalog_path` of a [portable knowledge](PORTABLE-KNOWLEDGE.md) runtime,
`/config/knowledge.json`) can name generations that are neither published nor in
the snapshot registry. The processes that read them run in the tooling container,
which the refresh container cannot see. List such a catalog in
`retention_references` by its container path, and every generation it mentions is
kept. Do not copy it into the snapshot registry instead: a copy goes stale when you
update the catalog.
Each pruning pass reads the listed files as they are at that moment. A file mentions
a generation by the snapshot registry's rules: an absolute path under the output
root, in a JSON value or anywhere in its text. A listed file that is missing,
unreadable, not a regular file or over the 16,000,000-byte read budget means that pass
removes nothing: the receipt reports `status: "skipped"` with a reason naming the
file, and the publication itself still succeeds. The receipt lists the files under
`surfaces.reference_files` and the generations they kept under
`referenced_by_reference_files`. Without `retention_references` none of this
applies and retention is unchanged.

Retention renames each removed generation to a tombstone and then deletes it. Every
pruning pass writes `retention-receipt.json` under the output root. The receipt
lists each removal and names the surfaces it consulted. Live processes are observed
only through `/proc` in the refresh container's own PID namespace, so the tooling
container's readers are covered by the retained newest generations and by the
files listed in `retention_references`, not by the live-process check.

**capture.** Plan with `--transcript-root` for each native root. Then provide:

- `$root/config/capture/session.json`, an admitted desk-memory config
  ([example](../deploy/examples/capture-session.example.json)). With a portable
  registry it can be a copy of the registry config whose `provider_session_id`
  you bound to a desk ([first run](#first-run-from-an-empty-root), steps 3 to 5);
- `$root/config/capture/workspace-policy.json`
  ([example](../deploy/examples/capture-workspace-policy.example.json));
- the approval record the policy pins
  ([example](../deploy/examples/capture-workspace-approval.example.json)).

Paths in the policy are host paths, because capture mounts sources at their host
paths. Capture has no network. Workspace capture starts once both `session.json`
and `workspace-policy.json` are written, so write the approval record and
`session.json` first and the policy last, after the session is bound. A file that
is present but wrong (a group-readable mode, an unbound session, a digest that
does not match) is an error, not `not_configured`: each workspace-capture pass
reports it as one JSON line (`"status": "error"`, its `category`) and retries with
backoff, up to 10 times the interval, while spool ingestion and host launches go on.
Fix the file; the next pass reads it, with no restart.

What workspace capture records, and what it does not:

- Workspace capture records **unresolved evidence**. It verifies that a transcript belongs to an
  approved repository, not to a desk, so the episodes it captures carry no desk
  owner, even for a session that `bind` attached to a desk. Desk-scope
  `memory.search` excludes them and counts them in `excluded_unresolved_episodes`;
  `scope: "topic"` searches reach them ([MEMORY.md](MEMORY.md#historical-session-sources-and-claim-filters)).
- **Desk-attributed capture** comes from the launch hooks of a bound launch (T3,
  [LAUNCH-BINDING.md](LAUNCH-BINDING.md)) and from the host adapter's spool
  ingestion in this same role (T4, [HOST-ADAPTER.md](HOST-ADAPTER.md)). Those
  capture under the bound desk, so desk-scope search finds them.

**board.** The image's `kanban` command serves the board on loopback. Its passcode
is in `docker compose … logs board`. For history import, provide
`$root/config/board/session-import.json`
([example](../deploy/examples/board-session-import.example.json)) with its
catalog, and copy only the transcripts you deliberately select into
`$root/import-sources/`. The board sees them read-only at `/import-sources`.
Without the config, the import dialog reports that it is unconfigured. The steps are in
[CONTAINER-IMPORT.md](CONTAINER-IMPORT.md).

The board role sets its desk integrations itself. Each runs inside the board
container and names a fixed path, so you only write files:

| Variable | Value in the board role | Reads |
|---|---|---|
| `KANBAN_DESK_REGISTRY_COMMAND` | `["/usr/local/bin/kp-agent-desk-registry", "--config", "/config/launch/registry.json"]` | The registry operator config of [first run](#first-run-from-an-empty-root) step 3, the one host launches use. The Desks menu lists, saves and binds through it, and its writes land in the registry's `state_root` under `/state/memory`, which `tooling` and `capture` read. |
| `KANBAN_LAUNCH_BINDING_COMMAND` | `["/usr/local/bin/kp-agent-launch", "--config", "/config/launch/registry.json"]` | The same file. A desk task launches bound to its desk ([board tasks](#board-tasks-with-in-container-agents-agents-image)). |
| `KANBAN_ASSISTANT_MEMORY_COMMAND` | `["/usr/local/bin/python3", "-m", "kp_agent_tooling.assistant_host_cli", "--binding", "/config/board/assistant-binding.json"]` | The assistant binding, `$root/config/board/assistant-binding.json`, once you set `KANBAN_ASSISTANT_MEMORY_WORKSPACE` (below). |

Until `$root/config/launch/registry.json` is written, the Desks menu reports that
the desk registry is not configured, a desk task does not start (the board says
launch binding is not configured), and the board keeps serving. No restart is
needed once you write it.

The board also reads the optional operator file `$root/config/board/board.env`
(Compose `env_file`, read on the host by `docker compose`; Compose 2.24 or later).
It holds the board variables the manifest leaves to you; a variable the board role
sets itself, above, cannot be changed there. Recreate the board after editing it:
`docker compose --project-directory "$root" up -d board`.

**Sidebar assistant memory.** The sidebar assistant keeps its own isolated memory:
the one assistant desk of its own catalog, which the binding's configuration names,
separate from the desks of step 3. It needs the binding file named
above, the files it names, and the workspace variable
`KANBAN_ASSISTANT_MEMORY_WORKSPACE`: the board project whose sidebar gets the
memory, as the board sees it. With `--board-agents` that is a repository's host
path, such as `$product`. It works for fresh Claude sidebar sessions. In the first
run's shell:

```sh
assistant_workspace="$product"
assistant_key="binding:$(printf '%s' "$tenant|assistant|personal" | shasum -a 256 | cut -d' ' -f1)"
docker compose --project-directory "$root" run --rm -T tooling sh -c 'umask 077 && mkdir -m 700 /state/memory/assistant /state/memory/assistant/store'
cat > "$root/config/board/assistant-memory.json" <<EOF
{"schema_version": "ops.assistant-memory.local.v1", "state_root": "/state/memory/assistant/store",
 "catalog_path": "/config/board/assistant-desk.json", "workspace_root": "$assistant_workspace",
 "provider_instance": "kanban-claude-assistant", "provider_session_id": "assistant-template",
 "assistant_policy_path": "/config/board/assistant-policy.json"}
EOF
cat > "$root/config/board/assistant-desk.json" <<EOF
{"schema_version": "ops.imported-desk-catalog.v1", "approval_ref": "operator:assistant",
 "bindings": [{"binding_key": "$assistant_key", "tenant_id": "$tenant", "role": "assistant",
               "repo_key": "personal", "desk_label": "Workspace Assistant", "source": "operator",
               "memory_write_allowed": true}]}
EOF
cat > "$root/config/board/assistant-policy.json" <<'EOF'
{"schema_version": "ops.assistant-summary-policy.v1", "focus_questions": []}
EOF
cat > "$root/config/board/assistant-binding.json" <<'EOF'
{"schema_version": "agent.assistant-host.v1", "config_template": "/config/board/assistant-memory.json",
 "transcript_root": "/state/.claude/projects"}
EOF
printf 'KANBAN_ASSISTANT_MEMORY_WORKSPACE=%s\n' "$assistant_workspace" > "$root/config/board/board.env"
docker compose --project-directory "$root" up -d board
```

- The binding (`agent.assistant-host.v1`) and its `config_template`,
  `$root/config/board/assistant-memory.json`, are read-only operator files under
  `/config`, written on the host (above). Each sidebar launch writes its private launch
  directory under the template's own `state_root`, at
  `/state/memory/assistant/store/launches/`, a store path in the volume; nothing is
  written under `/config`. A `config_template` under `/state`, `/state/memory`
  included, is refused: the board waits, refused (`operator_file_names_outside_store`),
  `verify` names the binding file with its `required` path, and a sidebar launch is
  refused. Its `transcript_root` is where the board's Claude writes, under its home
  `/state`.
- The configuration's `state_root` must exist, empty and 0700, before the first
  sidebar session; its catalog holds exactly one binding with role `assistant`.
- Each sidebar session is admitted to that one desk. Its `assistant-memory` MCP
  server answers from that desk's store only, and its `Stop` hook captures the turn
  there.
- Without the workspace variable the sidebar starts as before, with no assistant
  memory configured: it gets no `assistant-memory` server. With the variable but
  without the binding's files, the sidebar does not start and reports that its
  assistant binding failed.

**Moving the assistant template out of the volume (a runtime set up before T11b).** The
template used to be written in the volume, at `/state/memory/assistant/memory.json`, with
the launches beside it in `/state/memory/assistant/launches/`. On the new image the board
waits, refused, and `verify` names `$root/config/board/assistant-binding.json`
(`config_template`, required: a path under `/config`), until the template is an operator
file. With the board stopped, in a shell with `umask 077`:

1. Print the template with a one-off container into its operator file. The one-off uses
   its own entrypoint, because the role preflight refuses while the binding still names
   the old template:

   ```sh
   docker compose --project-directory "$root" run --rm -T --entrypoint cat tooling /state/memory/assistant/memory.json > "$root/config/board/assistant-memory.json"
   ```

   If its `state_root` still names `/state/assistant/store` (a runtime from before the
   store volume), change it to `/state/memory/assistant/store` in the new file.
2. Point the binding's `config_template` at it:

   ```sh
   python3 - "$root/config/board/assistant-binding.json" <<'PY'
   import json, sys
   path = sys.argv[1]
   value = json.load(open(path))
   value["config_template"] = "/config/board/assistant-memory.json"
   open(path, "w").write(json.dumps(value) + "\n")
   PY
   ```
3. Start the board (`docker compose --project-directory "$root" up -d board`); `verify`
   no longer lists the binding.

The template and `launches/` left in the volume are not migrated and nothing reads them;
the assistant's store and its desk binding are unchanged, so the sidebar keeps its memory. A
sidebar session launched before the move is relaunched: its hook refuses a launch that is
not under the template's `state_root`.

**telemetry.** Tempo and the collector keep 24 hours of traces in `tempo/`. This
role does not instrument any application.

**summarizer.** Opt-in: plan with `--components ...,summarizer`. No default plan, and no
other component, selects it. Every interval it summarizes the sealed episodes queued for
each desk in its standing approval, through the model gateway, under the gateway's budget.
With the role selected and a desk in the approval, every sealed episode of that desk that
reaches the queue is sent for summarization, without a per-episode digest approval. Without
the role, or for a desk not in the approval, install, binding, capture and reads never
trigger an outbound call ([MEMORY.md](MEMORY.md)). Selecting it in a live runtime is the
Principal's act.

The operator's steps (the role never admits a desk, writes an operator file or creates its
state). In the first run's shell, after step 4 (`$desk` is the desk to summarize):

1. **The role's state directory**, in the store volume. It holds the model metadata receipt
   (`model-profile.json`), the status file (`status.json`) and, as the gateway's
   `artifact_root`, the gateway's ledger (`.model-gateway/ledger.sqlite3`), which counts the
   role's calls and spend:

   ```sh
   docker compose --project-directory "$root" run --rm -T tooling sh -c 'mkdir -m 700 /state/memory/summarizer'
   ```

2. **The key file.** Write the provider key to `$root/secrets/summarizer.key` (0600). Only the
   summarizer mounts it, read-only, at `/config/summarizer.key`. Apply creates it empty where
   absent; empty, group- or world-readable, it reads `not_configured` (`key_file`).

   ```sh
   printf '%s\n' "$OPENROUTER_API_KEY" > "$root/secrets/summarizer.key"
   ```

3. **The gateway route and a priced budget**, `$root/config/summarizer/gateway.json`. The route
   names the provider and the approved model, with `"operation": "chat"`. The budget needs
   `max_calls_per_hour`, `max_usd_per_day` AND `estimated_usd_per_call`: without the estimate
   the role reads `not_configured` (`budget_estimate`), so the dollar cap holds even when the
   provider reports no cost. Route `params` may not set `provider`, `models`, `route`,
   `max_tokens`, `response_format` or `reasoning` (`not_configured`, `route_params`): the role
   sends `zdr: true`, `data_collection: deny` and `require_parameters: true` on every request,
   and never a fallback model list.

   ```sh
   cat > "$root/config/summarizer/gateway.json" <<'EOF'
   {"schema_version": "agent-tooling.model-gateway.v1",
    "artifact_root": "/state/memory/summarizer",
    "providers": {"openrouter": {"kind": "openrouter", "base_url": "https://openrouter.ai/api/v1",
                                 "api_key_file": "/config/summarizer.key"}},
    "routes": {"memory.summarize": {"provider": "openrouter", "model": "z-ai/glm-5.3-flash",
                                    "operation": "chat"}},
    "budgets": {"memory.summarize": {"max_calls_per_hour": 60, "max_usd_per_day": 0.50,
                                     "estimated_usd_per_call": 0.002}}}
   EOF
   ```

4. **The approval**, `$root/config/summarizer/approval.json`
   (`agent-tooling.summarizer-approval.v1`): the gateway capability the role calls, the
   approved model (equal to the route's), the five profile reserves of
   `scripts/memory_model_profile.py`, and the approved desks. Each desk is its `binding_key`
   and the desk-memory configuration of the role's own session for that desk: provider
   instance `summarizer`, a per-desk session id, the desk's state root and catalog.

   ```sh
   cat > "$root/config/summarizer/desk-product.json" <<'EOF'
   {"schema_version": "ops.desk-memory.local.v1", "state_root": "/state/memory/registry",
    "catalog_path": "/config/launch/desks.json", "workspace_root": "/config/launch",
    "provider_instance": "summarizer", "provider_session_id": "summarizer-product"}
   EOF
   binding="$("${registry[@]}" list | jq -r --arg desk "$desk" '.desks[] | select(.desk_id == $desk) | .binding_key')"
   cat > "$root/config/summarizer/approval.json" <<EOF
   {"schema_version": "agent-tooling.summarizer-approval.v1", "capability": "memory.summarize",
    "model_id": "z-ai/glm-5.3-flash",
    "reserves": {"system_tokens": 4096, "tool_tokens": 1024, "reasoning_tokens": 4096,
                 "output_tokens": 8192, "safety_tokens": 2048},
    "desks": [{"binding_key": "$binding", "memory_config": "/config/summarizer/desk-product.json"}]}
   EOF
   ```

   The registry's `list` names each desk's `binding_key`; the `admit` of the next step prints
   the same value.

5. **Admit each desk**, with the route's provider and the approved model. This is the
   existing `admit`, run by you; the role never calls it:

   ```sh
   docker exec -i "${project}-tooling" kp-agent-desk --config /config/summarizer/desk-product.json \
     admit --desk-id "$desk" --provider-id openrouter --model-id z-ai/glm-5.3-flash
   ```

   Admission rows are immutable. To change the approved model, admit each desk again under a
   new per-desk session id (a new desk-memory configuration), then point the approval's
   `model_id` and `memory_config` at them; until then that desk reads `not_configured`
   (`admission`).

6. **The interval** (optional): `AGENT_SUMMARIZER_INTERVAL_SECONDS` (1..86400, default 900) in
   `$root/config/summarizer/summarizer.env`, read by Compose when the role is created:

   ```sh
   printf 'AGENT_SUMMARIZER_INTERVAL_SECONDS=900\n' > "$root/config/summarizer/summarizer.env"
   docker compose --project-directory "$root" up -d summarizer
   ```

What the role does on each tick:

- **Checks first, no connection.** The approval, the gateway route (a chat route at the
  approved model, with no refused params), the budget and its estimate, the key file, its
  state directory and each desk's admission (its own row, at the approved model and the
  route's provider). Anything missing reads `"status": "not_configured"` with `missing`
  naming it (never the key), in its log line and in `/state/memory/summarizer/status.json`,
  and the tick opens no connection.
- **Model metadata, at most once per 24 hours.** Once the checks pass, the role refreshes its
  model metadata receipt from the route provider's model listing: one GET of
  `<base_url>/models` with the route provider's key file, never a completion and never another
  host. The listing must still name the approved model and support its parameters (JSON
  output and low reasoning for the GLM baseline); otherwise the role reads
  `not_configured` (`profile_changed`), uses no other model, and does not read the listing
  again for that model for 24 hours.
- **The work.** For each approved desk, one job at a time, until the queue is idle, the
  gateway refuses, or 8 jobs (the per-tick cap; at most 4 requests per job). Every request goes
  through the gateway's pre-network order (route, budget, key file, reservation, one request,
  cost) to the route's base URL, with `model` equal to the approved model. The gateway keeps
  only its ledger row for the role: no output artifact, no desk memory record; the summary's
  only home is the desk's capsule, read with `memory.status`, `memory.list`,
  `memory.handoff` and `memory.resume` (never desk search).
- **Refusals before the network** (budget exceeded, confirmation required, key file refused)
  send nothing: the attempt is recorded `not_sent`, the job stays `queued` for a later tick,
  and the tick ends (the budget is shared by every desk). A request that may have reached the
  provider and failed, or a response whose reported model is not the requested model, goes
  to `needs_review` with no capsule, and is never resent.
- **Revocation.** Remove a desk from the approval: from the next tick none of its episodes
  are read, nothing is sent for it, and its queued jobs stay `queued`.

`verify` lists the summarizer's operator files under `readiness`; the container's health
check covers its lifecycle only, so read its `not_configured` state from its log line or
its status file.

## Provider login (`agents` target)

Install with the digest of the opt-in `agents` image (planned in AT-0004 T6a). It
is the `product` image plus the `claude` and `codex` CLIs. No image contains
credentials. Log in inside the running tooling container. The login persists only under `/state`, which is
`HOME` in every role, so the CLIs the board runs for its tasks
([board tasks](#board-tasks-with-in-container-agents-agents-image)) use it too:

```sh
docker exec -it <project>-tooling claude        # complete its login flow, then exit
docker exec -it <project>-tooling codex login   # choose its device-code flow; no browser runs in the container
```

These login flows are unverified against the pinned CLI versions: confirm them
headlessly before relying on them. `-t` is correct for these interactive logins
only, never for MCP registration.
Treat `$root/state/` as credential-bearing from then on: back it up privately and do
not copy it into images or other roots.

## Upgrade

A new digest means re-plan and re-apply. Nothing else changes the image. The order is:
stop the writers, plan, apply, prepare, edit operator files, up, verify.

1. Record the current receipt: `jq '{plan_sha256, image}' "$root/receipt.json"`, and
   build or pull the new image ([step 0](#first-run-from-an-empty-root)): `prepare`
   needs it present locally.
2. Stop the writers: `docker compose --project-directory "$root" stop` (never
   `down -v`), and any host process that opens the store, which no step can detect.
3. Back up the stores with the [backup command](#the-store-volume), a one-off container
   (on a root from before the store volume, it runs on the old image and covers the
   pre-T9b store directories). Never copy a live SQLite file, and never open a store
   from the host. Back up the rest of `$root/state/` with the writers still stopped.
4. Run `plan` with the new `--image` and otherwise identical arguments. Its
   `preview.replace` lists the installer-owned files that will change, normally
   `$root/.env` and, when repositories moved, `$root/config/navigation.json`. Its
   `preview.memory_store` lists the store directories `prepare` will move and the
   operator files that name old store paths.
5. Run `apply` with the new `plan_sha256`. Only files that still match the old
   receipt are replaced. Anything changed by hand is refused as drift.
6. Run `kp-agent-install prepare --runtime-root "$root"`. The first time on a root from
   before the store volume, it moves the store directories into the volume
   ([Moving existing stores](#moving-existing-stores-into-the-volume)). Check its
   `status` (`prepared`) and `migration`.
7. Edit every file listed under `operator_files_naming_old_store_paths` to its
   `required` path (for example `"state_root": "/state/registry"` in
   `$root/config/launch/registry.json` becomes `"/state/memory/registry"`, and the
   descriptor's `roster_path` `/state/memory/registry/roles.json`). The assistant's
   template is an operator file: when the binding's `config_template` is listed, move the
   template out of the volume as [Moving the assistant template out of the
   volume](#optional-roles) describes (its steps 1 and 2; the board starts in step 8).

   Sessions launched before the migration must be relaunched: their hooks refuse.
8. Run `docker compose --project-directory "$root" up -d` to recreate the containers
   on the new image, then `verify` (`readiness.memory_store` `prepared`, no operator
   files listed, no `gaps`), run the [integrity command](#the-store-volume), and exercise the
   tools. A root planned before the indexer gains the `indexer` role here: plan, apply
   and `up -d` add it ([The indexer](#the-indexer)).

Re-planning also re-pins navigation to each repository's current HEAD. Adding or
removing a component follows the same flow. Removed components leave their files
untracked in place; nothing is deleted.

## Rollback

Roll back like an upgrade, with the recorded previous digest: re-plan, apply,
prepare, then `up -d`. The image is immutable, so the digest selects the exact previous
build. State is not rolled back. If a newer build migrated state, restore the
backup taken in upgrade step 3 while the writers are stopped ([restore](#the-store-volume)).
Never delete immutable episodes to fit an older build.

The store volume stays in place across an image rollback. Do not roll the installer
itself back to a release without the store volume: its manifest mounts
`$root/state/memory` from the bind again, so the roles would no longer see the stores
in the volume.

## Boundaries

- A receipt detects drift. It is not a signature, and it proves no readiness. The
  installer reports that image presence, container health, MCP connectivity and
  host registration are not verified.
- The rendered files are private to the invoking user. The containers must run as
  that user (`--uid`/`--gid`). `plan` warns when they differ.
- Models are offline (`HF_HUB_OFFLINE=1`). Provision the verified cache under
  `$root/state/models`. A missing model is not evidence of no semantic matches.
- The supported boundary is one trusted OS user on one host. Multi-user or remote
  hosting needs its own acceptance.
- The stores are in a Docker volume because POSIX locks do not hold across processes
  on a Docker Desktop bind mount. Locks on the other bind-mounted paths are not relied
  on ([known limits](#the-store-volume)).

## Images

`deploy/Dockerfile` is the one multi-target Dockerfile (AT-0004 T6a), built from
this tree. The targets, as AT-0003 and the T6a order define them, plus the
optional `opencode` target (order O3):

| Target | Contents |
|---|---|
| `runtime` | A slim, memory-only build. |
| `product` | The image this manifest expects: navigation, memory, capture, refresh and the board. No extension code. |
| `agents` | `product` plus the `claude` and `codex` CLIs, for provider login. No extension code. |
| `opencode` | `product` plus the pinned OpenCode CLI (`opencode`). Optional: used only when you install its digest. No `claude` or `codex`, no extension code. |
| `ops` | `product` plus the OPS extension (`extensions/ops`), for existing deployments during the transition. |

**The optional `opencode` image.** Like the `agents` image, it is never selected for
you: the installer runs whichever image digest you pass as `--image`, and the
default images and the plan's roles and profiles do not change. It is an optional
pull: install it only if board tasks should run OpenCode. To use it, pull its digest
(the `opencode` row of the release page) and pass that reference as `--image`, exactly as
in step 0 of the [first run](#first-run-from-an-empty-root):

```sh
image='ghcr.io/saigid-1/agent-tooling@sha256:<digest>'   # the opencode digest from the release page
docker pull "$image"
docker run --rm --network none --entrypoint opencode "$image" --version   # prints the pinned version
```

Or build the target from a checkout and install its image ID:

```sh
docker build -f "$checkout/deploy/Dockerfile" --target opencode \
  --build-arg SOURCE_REVISION="$(git -C "$checkout" rev-parse HEAD)" -t agent-tooling:opencode "$checkout"
image="$(docker image inspect --format '{{.Id}}' agent-tooling:opencode)"
docker run --rm --network none --entrypoint opencode "$image" --version   # prints the pinned version
```

The pinned version is `ARG OPENCODE_VERSION` with `deploy/image/opencode/package.json`
and its lockfile, and the image states it in the labels `agent-tooling.opencode.version`
and `org.opencontainers.image.version`. The binary comes from the lockfile at build
time, so nothing is fetched when it runs; auto-update is off
(`OPENCODE_DISABLE_AUTOUPDATE=1`). The OpenCode CLI is MIT; the image carries its
LICENSE at `/usr/local/share/doc/opencode/LICENSE` (see NOTICE). It carries neither
`claude` nor `codex`: no target combines OpenCode with the `agents` CLIs.

Build a target with `--build-arg SOURCE_REVISION=<40-hex commit>`. Every target
is labelled `org.opencontainers.image.revision=<that commit>` and declares it in
`/usr/local/share/agent-tooling-build-identity.json`, so its `tooling.identity`
reports `status: known` with that revision and origin `image-build-declaration`.
Built without the argument, the label and the declaration say `unknown`, and so
does `tooling.identity`. Only the last layer of each target depends on the
revision; a new revision rebuilds nothing shared.

The installer's default tool list is core-only, whichever image is installed. The
OPS tools are served only where the extension is installed and the navigation
config enables them.

## Retired deployment files

S3 removed the legacy fixed-name Compose project, its board import overlay and the
in-place release script that was tied to one host's volume. Their behavior moved
into the manifest and installer:

- The `board` role mounts `/config` (read-only), `/state` and `import-sources/`
  (read-only), and names `KANBAN_SESSION_IMPORT_CONFIG=/config/board/session-import.json`.
- A release is re-plan, re-apply and `up -d`. The reviewed plan hash and the
  receipt replace the old script's checks.
