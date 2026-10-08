# T9b — The memory store lives where file locks work

ADR: [AT-0004](../../adr/AT-0004-portable-kanban-suite.md). Header: [ARM-HEADER](ARM-HEADER.md). Dispatch: FEATURE + TEST.

## Problem

Measured on 2026-10-02 on the live runtime (Docker Desktop on macOS):

- **No working POSIX locks on bind mounts.** On a host bind mount (`$root/state`, on both an external APFS drive and the internal disk), a POSIX lock (`fcntl.lockf`) held by one process is granted again to a second process. This happens across two containers, and also between two processes in the same container. On a Docker named volume, and on container-local storage, the second process is refused, as it should be.
- **SQLite depends on those locks.** The memory store (`/state/memory/desk-history/*.sqlite3`; `episodes.sqlite3` is in WAL mode) is opened concurrently by the capture worker, the spool ingester, and one memory MCP server per admitted session. On a bind mount, nothing serializes their writes or checkpoints.
- **The failures are already visible.**
  - A memory read in the tooling container died with SIGBUS while the capture container wrote.
  - The 2026-09-29 corruption of `episode-search.sqlite3` happened while two containers wrote it. Its cause is not established; this mechanism is the leading candidate.

## Properties and falsifiers

- **P1: the memory store is on a named volume.** The rendered Compose project mounts one named volume at `/state/memory`, in every service that mounts `/state`. The volume is named for the project. It is owned by `AGENT_UID:AGENT_GID`, mode `0700`. Everything else under `/state` stays on the host bind mount, as today.
  Falsifier: a service in which `/state/memory` resolves to the host bind; a volume not named for the project; wrong owner or mode.
- **P2: locks work for the store.** A POSIX lock on a file under `/state/memory`, held by a process in one role container, is refused to a process in another role container of the same project.
  Falsifier: the second lock is granted.
- **P3: an existing store migrates once, losslessly.** Apply runs against a root whose `state/memory` holds files while the project's named volume is empty. Then:
  - every file is copied into the volume at the same relative path, with owner and mode preserved;
  - each `*.sqlite3` is copied with the SQLite backup API from inside the runtime, and passes `pragma quick_check`;
  - each copied database's row counts match its source;
  - the host directory is renamed `state/memory.migrated-<plan_sha256[:12]>` and never deleted;
  - the receipt records the migration.

  A re-apply when the volume already holds the store copies nothing. Any failure refuses the apply, leaving the host directory and the volume as they were before the step; no partially populated volume is accepted.
  Falsifier:
  - a byte, row or file missing or changed after migration;
  - the host copy deleted;
  - a second copy over a populated volume;
  - an apply that reports success after a failed copy or check.
- **P4: a migration never runs under live writers.** Apply refuses to migrate while any container of the project is running. The refusal names the running services and the documented stop command.
  Falsifier: a migration while a project container runs.
- **P5: the store is reached only from inside the runtime.**
  - `docs/DOCKER.md` states that the host must not open store files, even read-only.
  - Status, backup and integrity checks are documented as one-off `docker compose run` commands.
  - The documented upgrade backup step runs inside a container.
  - It warns that `docker compose down -v` deletes the volume.

  Falsifier: a documented step that opens `state/memory` files from the host.
- **P6: everything else is unchanged.**
  - The other state paths and the operator files are unchanged.
  - A fresh empty-root install works.
  - `plan` stays pure.
  - Every existing test stays unmodified and green.

  Falsifier: any change to them, or a modified existing test.

## Write scope

- **FEATURE:**
  - `packages/tooling/src/kp_agent_tooling/assets/deploy/compose.yaml`, and its copy `deploy/compose.yaml` (keep them identical);
  - `packages/tooling/src/kp_agent_tooling/_impl/runtime_install.py` (`render`, `apply`, the receipt, the migration);
  - `install_cli.py` only if a flag or message is needed;
  - `deploy/image/container.py` only if the role preflight must check the store mount;
  - `docs/DOCKER.md`.
- **TEST:** new files under `tests/install/`, next to `test_p2_apply.py` and the `t7a`/`t7b` image tests.
  - Plan and render properties run without Docker.
  - Volume, lock, migration and refusal properties are `-m image` tests, the kind CI's `images` job runs.
  - Locally, use only Docker resources whose names start with `t9b-`. Never touch an `agent-tooling*` project, container or volume.

## Amendment 1 (2026-10-02, after the Verification desk's review; supersedes the text above where they differ)

Verification reproduced the premise on throwaway containers: on host bind mounts, both `fcntl.lockf` and `fcntl.flock` are granted to a second process, in the same container and across containers. A named volume refuses both. The source uses `flock` at 14 sites.

- **P1 (replaces): every runtime store path is on the project volume.**
  - **Which paths:** every store and lock path that the roles open for desk memory, the desk registry and its roster, sessions, launches and assistant memory. That means the live `/state/memory/desk-history`, plus every path `docs/DOCKER.md` documents or the installer renders for those stores: at least `/state/registry` and `/state/assistant`.
  - **How:** one project-named volume, mounted at those paths (for example with Compose volume `subpath`) or with the documented paths moved under it. This is FEATURE's choice. Operator files keep working unchanged.
  - **Ownership:** a fresh volume is owned by `AGENT_UID:AGENT_GID`, mode `0700`, on an empty root with no migration as well.

  Falsifier:
  - a documented or rendered store path that resolves to the host bind;
  - a store built by the documented first-run steps that lands on the bind;
  - wrong owner or mode on an empty root.
- **P1b (new): roles refuse a store that is not on the volume.** A role's preflight refuses to start when any of those store mounts is not the project volume, for example a stale compose file or a `compose run` from an old root. It never creates a fresh empty store on the bind.
  Falsifier: remove the volume mount from a role; the role starts.
- **P2 (replaces): SQLite locking works across roles.** In two different role containers of the project, the following are refused to the second holder:
  - `lockf` and `flock` on a file on the volume;
  - SQLite: role A holds `BEGIN IMMEDIATE` on a database on the volume, and role B's `BEGIN IMMEDIATE` fails with "database is locked".

  Falsifier: any of the three is granted.
- **P3 (clarifies): what a migration copies, and how it is checked.**
  - **Excluded:** `-wal`, `-shm` and `-journal` sidecars are never copied.
  - **Databases:** each `*.sqlite3` is copied with the backup API, and equality is logical: `quick_check` is ok, and each ordinary table's content digest (rows in primary-key or rowid order) equals the source's.
  - **Other files:** copied byte-equal, with mode preserved.
  - **Damaged source:** a source database that fails `quick_check` refuses the apply. The refusal names the database and the documented recovery, and leaves both sides as they were.
  - **Host files beside a populated volume:** when the host `state/memory` (or another store path) holds files and the volume is already populated, apply refuses loudly and names both. This does not apply to the renamed `memory.migrated-<…>` directory, or to an empty mount point Docker recreated.

  Falsifier:
  - a table whose content changed but whose count matches;
  - a sidecar copied;
  - a damaged source accepted;
  - host files beside a populated volume ignored silently.
- **P4 (clarifies): which writers apply can detect.** "Running containers of the project" includes one-off `compose run` containers, matched by the Compose project label. If Docker is unreachable, apply refuses to migrate. `docs/DOCKER.md` states the limit: host processes cannot be detected, so stopping them is the operator's duty.
- **P5 (scoped): docs.** The rule "the host must not open store files" applies to the Docker runtime; a host install without Docker opens them by design. `docs/DOCKER.md` says when the operator may remove `memory.migrated-<…>`: after the upgraded runtime's `verify` passes and one in-runtime backup has completed.
- **Known limits, out of scope (documented as such in `docs/DOCKER.md`).** Locks that stay on binds after T9b:
  - the refresh double-start guard `.refresh.lock` (`refresh_cli.py:787`);
  - knowledge lifecycle locks under `/state/knowledge`;
  - the assistant binding lock on a `/config` file (`assistant_host_cli.py:123`).

  A follow-up order takes these.
- **Write scope (widened):**
  - `deploy/image/container.py` is now in scope, for P1b;
  - `docs/DOCKER.md` must cover the paths and steps above;
  - TEST covers P1b, the widened P1 and P2, P3 and P4 under `tests/install/`.

## Amendment 2 (2026-10-02, after Verification's review of Amendment 1; supersedes earlier text where they differ)

- **P1 (generic; replaces both earlier versions): one volume holds every store.**
  - **The volume:** the Compose volume key is `memory`, so the volume is named `<project>_memory`. It is mounted at `/state/memory` in every role that mounts `/state`.
  - **The rule:** every store path a role opens resolves under `/state/memory`: any `state_root`, the registry database and roster, launches, sessions, and assistant memory stores.
  - **Documented paths move under it:**
    - `/state/registry` → `/state/memory/registry`;
    - `/state/assistant` (the store) → `/state/memory/assistant`;
    - `/state/desk-memory` → `/state/memory/desk-memory`;
    - every `deploy/examples` path, likewise.
  - **Operator-written files** live under `/config`, never on the volume. For example, the assistant template (`memory.json`) moves under `/config`, with docs and examples following.
  - **Ownership:** a fresh volume is owned by `AGENT_UID:AGENT_GID`, mode `0700`, with no migration needed.

  The listed paths are examples; the rule is generic.
  Falsifier: any store path a role opens that resolves outside `/state/memory`; a documented or example store path outside it; wrong owner or mode on an empty root.
- **P1b (generic; replaces): roles refuse what is not on the volume.** A role refuses to open a store whose path does not resolve under `/state/memory`. It also refuses to start when `/state/memory` is not the project volume, which it determines from the mount table (`/proc/self/mountinfo`), not from a marker file.
  Falsifier:
  - remove the volume mount;
  - mount a host directory holding a copied marker or a restored `memory.migrated-<…>` at `/state/memory`;
  - configure a `state_root` outside `/state/memory`.

  In each case the role must refuse and does not.
- **P3 (scope): every store directory migrates.** Every store directory that a pre-T9b root holds migrates into the volume at its new path, under P3's rules:
  - `state/memory/*`;
  - `state/registry` (populated on T7b-era installs);
  - `state/assistant`'s store files;
  - `state/desk-memory`, if present.

  Each host directory is renamed `<name>.migrated-<plan_sha256[:12]>` and never deleted.
  - **Operator files that name an old path** (for example `roster_path` or `state_root` in `config/…` files) are not edited by apply. Apply's report lists each file and the required new path. `docs/DOCKER.md`'s upgrade steps include those edits. Under P1b, roles refuse until the edits are made.

  Falsifier: a populated pre-T9b store directory that is neither migrated nor reported; an operator file silently rewritten.
- **First run happens inside the runtime.** Documented first-run steps that create or populate a store use in-runtime commands, of the form `docker compose run --rm -T <role> sh -c '<the same commands>'`, against `/state/memory/...`. No documented step writes a store path from the host.
- **No subpath mounts.** P1 needs no Compose `subpath`. If FEATURE uses any feature that raises the Compose or Engine floor, `docs/DOCKER.md` states the new floor.
- **Regression-set exception (resolves P6 against P1 and P1b).**
  - **Who:** the TEST arm alone.
  - **What it may modify:** exactly these existing files:
    - `tests/install/t7b_harness.py`;
    - `tests/install/test_t7b_p4_assistant_memory_image.py`;
    - `tests/install/test_t7a_p1_readiness_and_operator_files.py`;
    - `tests/install/test_t7a_p2_documented_steps_image.py`;
    - `tests/host/test_t7a_p7_unconfigured_capture_image.py`;
    - any other existing test that creates or locates a store on the host bind, listed by name in its report.
  - **How far:** only where a store is located (paths under `/state/memory`), and how it is created. Creation goes through a one-off container that mounts the project volume, for example `docker run --rm -v <project>_memory:/state/memory …`, or through the in-runtime pattern above.
  - **Not allowed:** changing any assertion.
  - **FEATURE** modifies no existing test.
  - **At the meet,** Verification diffs each modified existing test against main and reports any changed assertion as a finding.

## Amendment 3 (2026-10-02, after Verification's review of Amendment 2; supersedes earlier text where they differ)

- **Volume preparation: who, when, and how it is measured (D1).**
  - **Who and when:** `apply` prepares the volume through Docker, using the runtime image as a one-off root container whose only actions are creating the volume, then `chown AGENT_UID:AGENT_GID` and `chmod 0700` on its root. No Compose service runs as root. `test_p4_rendered_isolation` is unchanged.
  - **Docker unreachable:** `apply` still renders and writes every file, succeeds, and reports `volume_preparation: pending`. `verify` reports the root as not ready, naming the pending step. A role started on an unprepared volume refuses in its preflight with a named reason; it never runs on a root-owned volume.
  - **Existing tests:** `tests/install/test_p2_apply.py` (no Docker) stays green.
  - **When the falsifier measures:** after `apply` with Docker reachable, then `up`.
- **P1b is narrowed to what a role preflight can see (D2).** At role start (`deploy/image/container.py`), the role refuses when:
  - `/state/memory` is not the project volume (mount table);
  - the volume is unprepared;
  - an operator file present at start names a store path outside `/state/memory`.

  The open-time refusal (any `state_root` read by `docker exec`'d tools, written after start, or opened in `desk_memory_runtime.py`, `launch_binding.py`, `spool_ingest.py`, `assistant_host_cli.py` or `queue_cli.py`) moves to T11, whose leaf module is the single place stores are opened. T9b and T10 therefore stay disjoint.
  Falsifier for T9b: remove the mount; mount a host directory holding a copied marker or a restored `memory.migrated-<…>`; leave the volume unprepared; start with an operator file naming a store path outside `/state/memory`. In each case the role must refuse and does not.
- **Pre-migration launches (D3).** Launch receipts and per-launch memory configs record absolute paths. After a migration that moves a store path, a hook for a launch made before the migration refuses (for example, the existing receipt and `memory_config` check fails) and never writes outside the volume. `docs/DOCKER.md`'s upgrade steps state that sessions launched before the migration must be relaunched.
  Falsifier: a hook for a pre-migration launch writes outside `/state/memory`, or succeeds against an old path.
- **The upgrade is incomplete until operator edits are made.** `verify`'s readiness report names every operator file that still names an old store path, with the required new path, until it is edited.

## Amendment 4 (2026-10-02, after Verification's review of Amendment 3; supersedes Amendment 3's D1 text and P3's "apply migrates")

- **`apply` never calls Docker.** It renders and writes files exactly as before. All existing installer tests keep their current behaviour, with no volumes created and no pulls.
- **New explicit verb: `kp-agent-install prepare --runtime-root <root>`.** Run after `apply` and before `up`. In order, it:
  1. refuses if any container of the project is running, one-off containers included (P4);
  2. refuses if Docker is unreachable;
  3. refuses if the runtime image is not present locally; it inspects and never pulls;
  4. creates the `<project>_memory` volume if absent, with a one-off root container from the runtime image whose only actions are `chown AGENT_UID:AGENT_GID` and `chmod 0700` on the volume root;
  5. migrates every pre-T9b store directory under P3's rules;
  6. records the outcome in the receipt.

  It creates no volume before checks 1–3 pass. Its outcomes are:
  - `prepared`;
  - `refused`, with a named reason: `writers_running`, `docker_unreachable`, `image_absent`, `source_damaged`, `host_files_beside_populated_volume`, or `copy_or_check_failed`, with both sides unchanged.
  - Re-running `prepare` on a prepared root is a no-op that reports `prepared`.
- **`verify`** reports the store as `unprepared` (not ready) until `prepare` has succeeded for the current receipt. It also names any operator files that still point at old store paths.
- **Roles** refuse in preflight on an unprepared volume (P1b).
- **When the falsifiers measure:** after `apply`, then `prepare` with Docker reachable and the image present, then `up`. The `prepare` refusals are each falsifiable under their named condition.
- **Upgrade and first-run docs (`docs/DOCKER.md`)** follow this order: stop writers; `plan`; `apply`; `prepare`; operator file edits; `up`; `verify`.
- **TEST:** the `prepare` and volume tests are `-m image`. The default suite stays Docker-free. No `<project>_memory` volume survives a test: tests use `t9b-test-`-prefixed projects and remove their volumes.

## Amendment 5: CONSOLIDATED ORDER AS AMENDED (2026-10-02; authoritative; supersedes every earlier section, including the original properties and Amendments 1–4)

The Problem section above stands. Everything below replaces all earlier properties, scope and notes.

**Definition: "prepared".** The project volume `<project>_memory` exists, its root is owned by `AGENT_UID:AGENT_GID` with mode `0700`, and no pre-T9b store directory is left to migrate on the bind. This observable state is the single test used by `prepare`, `verify` and the role preflight. The receipt entry is a record, not the test.

- **P1: store location.**
  - The volume is mounted at `/state/memory` in every role that mounts `/state`.
  - Every store path a role opens resolves under `/state/memory`: any `state_root`, the registry database and roster, launches, sessions, and assistant stores.
  - Documented and example paths move under it: `/state/registry` → `/state/memory/registry`; the `/state/assistant` store → `/state/memory/assistant`; `/state/desk-memory` → `/state/memory/desk-memory`; `deploy/examples` likewise.
  - Operator-written files (for example the assistant `memory.json` template) live under `/config`.

  Falsifier: a store path a role opens, or a documented or example store path, outside `/state/memory`; a service whose `/state/memory` is the host bind.
- **P1b: the role preflight refuses** (`deploy/image/container.py`, at role start) when:
  - `/state/memory` is not the project volume (`/proc/self/mountinfo`, not a marker file);
  - the store is not "prepared";
  - an operator file present at start names a store path outside `/state/memory`.

  Each refusal has a named reason. Open-time refusal by `docker exec`'d tools is T11's, not this order's.
  Falsifier: remove the mount; mount a host directory holding a copied marker or a restored `memory.migrated-<…>`; leave the volume unprepared; start with an operator file naming an outside path. In each case the role must refuse and does not.
- **P2: locking works.** In two different role containers of the project, the second holder is refused for:
  - `fcntl.lockf` and `fcntl.flock` on a file on the volume;
  - SQLite: role A holds `BEGIN IMMEDIATE` on a database on the volume, and role B's `BEGIN IMMEDIATE` fails with "database is locked".

  Falsifier: any of the three is granted.
- **P3: the migration, performed only by `prepare`.**
  - **What migrates:** every pre-T9b store directory on the bind (`state/memory/*`, `state/registry`, the `state/assistant` store files, `state/desk-memory`) moves into the volume at its new path. Each host directory is renamed `<name>.migrated-<plan_sha256[:12]>` and never deleted.
  - **Excluded:** `-wal`, `-shm` and `-journal` sidecars are never copied.
  - **Databases:** each `*.sqlite3` is copied with the backup API, and equality is logical: `quick_check` ok, and each ordinary table's content digest (pk or rowid order) equals the source's.
  - **Other files:** byte-equal, mode preserved.
  - **Operator files that name an old path** are never rewritten. `apply`'s report (no Docker needed) and `verify` list each one with its required new path.

  Falsifier: a changed table with a matching count; a sidecar copied; a populated directory neither migrated nor reported; an operator file rewritten; a host directory deleted.
- **P4: `kp-agent-install prepare --runtime-root <root>`**, in scope in `install_cli.py` and `runtime_install.py`. In order:
  0. If the root is already "prepared", report `prepared` and change nothing, with no stop required.
  1. Refuse `writers_running` if any project container runs, one-off `compose run` containers included.
  2. Refuse `docker_unreachable`.
  3. Refuse `image_absent` if the runtime image is not present locally; inspect, never pull.
  4. Create the volume if absent, with a one-off root container from the runtime image whose only actions are `chown` and `chmod 0700` on the volume root.
  5. Migrate under P3: refuse `source_damaged` on a source that fails `quick_check` (naming the database and the documented recovery), `host_files_beside_populated_volume`, or `copy_or_check_failed`, with both sides unchanged.
  6. Record the outcome in the receipt.

  Nothing is created before steps 1–3 pass.
  Falsifier: a volume created by a refused run; a refusal under the wrong condition; a pull; a re-run on a prepared root that requires a stop or changes anything.
- **P5: `apply` and `verify`.**
  - **`apply`:** never calls Docker, and renders as before.
  - **`verify`:** reports the store as a readiness state like `not_configured`. Its exit code and top-level `status` (`verified`/`drift`) are unchanged by `unprepared`. It names operator files that still point at old paths.

  Falsifier: a default-suite `apply` that creates a volume or pulls; `verify` exiting non-zero or reporting a different top-level status for an unprepared but otherwise clean root.
- **P6: pre-migration launches.** After a migration that moves a store path, a hook for a launch made before the migration refuses (for example, the existing receipt and `memory_config` check) and never writes outside `/state/memory`.
  Falsifier: such a hook writes outside the volume, or succeeds against an old path.
- **P7: docs (`docs/DOCKER.md`, `deploy/examples`).**
  - The operator sequence: stop writers; `plan`; `apply`; `prepare`; operator file edits; `up`; `verify`.
  - First-run store steps are in-runtime commands, of the form `docker compose run --rm -T <role> sh -c '…'`.
  - The Docker runtime's store files are reached only from inside the runtime (a host install without Docker is exempt); status, backup and integrity checks are documented as one-off containers.
  - `docker compose down -v` deletes the volume.
  - `memory.migrated-<…>` may be removed after `verify` passes and one in-runtime backup completes.
  - Sessions launched before a migration must be relaunched.
  - The new Compose or Engine floor is stated, if one is raised.
  - Known limits, out of scope: the refresh `.refresh.lock` (`refresh_cli.py:787`), the `/state/knowledge` lifecycle locks, the assistant binding lock on `/config` (`assistant_host_cli.py:123`), and the open-time refusal (T11).

  Falsifier: a documented step that writes a store path from the host; any listed item missing.
- **P8: everything else is unchanged.**
  - Other state paths and the operator files are unchanged.
  - A fresh empty root works through the documented sequence.
  - `plan` stays pure.
  - The default suite stays Docker-free, and no `<project>_memory` or test volume survives a test.
  - Every existing test stays unmodified and green, except under the regression-set exception: the TEST arm alone may modify `tests/install/t7b_harness.py`, `tests/install/test_t7b_p4_assistant_memory_image.py`, `tests/install/test_t7a_p1_readiness_and_operator_files.py`, `tests/install/test_t7a_p2_documented_steps_image.py`, `tests/host/test_t7a_p7_unconfigured_capture_image.py`, and any other existing test that creates or locates a store on the host bind (listed by name in its report). It may change only store location and creation, never an assertion.

**Write scope.**
- **FEATURE:**
  - `packages/tooling/src/kp_agent_tooling/assets/deploy/compose.yaml` and `deploy/compose.yaml` (kept identical);
  - `_impl/runtime_install.py` and `install_cli.py` (the `prepare` verb);
  - `deploy/image/container.py`;
  - `docs/DOCKER.md`;
  - `deploy/examples/*`.
- **TEST:** new files under `tests/install/` (image-marked where Docker is needed), plus the exception above.

## Amendment 6 (2026-10-02; restores clauses the consolidation dropped, and records Coordinator meet rulings; additive to Amendment 5)

- **H1: a refused `prepare` leaves nothing behind.** When any step from 4 on refuses, `prepare` removes the volume it created in this run (only if it created it in this run, and the volume is still empty). "Nothing created" holds for the whole refused run.
  - **What the role preflight can see:** owner and mode of the mounted volume, plus the bind store directories that remain visible (`state/registry`, `state/assistant`, `state/desk-memory`).
  - **What it cannot see:** the host `state/memory` it shadows. `prepare` and `verify` observe the full Definition.

  Falsifier: a volume left behind by a refused run.
- **H6: the migration copies inside the runtime.** Each `*.sqlite3` is copied and checked (backup API, `quick_check`, content digests) inside a one-off container from the runtime image. That container mounts the bind source read-only at a path other than `/state/memory`, and the volume at `/state/memory`.
  Falsifier: the migration opens a `*.sqlite3` with the host's Python.
- **H2: Docker resource rules for arms.** Arms create Docker resources only with their assigned prefix (`t9b-`) and never touch an `agent-tooling*` project, container or volume.
- **H3: writers.** The `writers_running` refusal names the running services and the documented stop command. `docs/DOCKER.md` states that host processes cannot be detected, so stopping them is the operator's duty.
- **H4: empty directories.** An empty directory, such as the mount point Docker recreates, is not "left to migrate".
- **H5: when `verify` cannot observe.** If `verify` cannot observe the store (no Docker), it reports a distinct readiness value (for example `not_observed`), never `unprepared`, with exit code and status unchanged.
  Falsifier: `verify` claims `unprepared` without observing.
- **Meet rulings (Coordinator).**
  - The regression-set exception also covers `tests/install/test_p5_empty_root_image.py` and `tests/install/test_t7a_p1_roles_start_image.py`, limited to adding the `prepare` step after `apply`. Under P1b, a role refuses an unprepared store.
  - `docs/DESK-MENU.md`'s registry first-run steps move to `/state/memory/registry` and the in-runtime pattern, as a Coordinator meet edit.
  - `tests/image` is unaffected: the preflight runs only in roles of a rendered root.

## Amendment 7 (2026-10-03; Coordinator meet rulings on Verification's T9b meet findings J1–J3; additive to Amendments 5 and 6)

- **J1: `verify` observes, the receipt records.** `verify`'s `readiness.memory_store` is the Definition as observed, never the receipt's `prepared` entry. P5's "`unprepared` until `prepare` has succeeded for the current receipt" reads as "until the observed Definition holds", which is what a successful `prepare` produces.
  - A pre-T9b store directory left on the bind: `unprepared`, observed on the host, no Docker needed.
  - Otherwise, Docker reachable and the receipt's image present locally: observe the volume; `prepared` or `unprepared` with its reasons. The receipt entry, present or absent, does not change the result.
  - Otherwise: `not_observed`, naming why (Docker unreachable, or the image absent). The receipt entry may be quoted as a record; it never makes the result `prepared` or `unprepared`.
  - A receipt without the store volume (written by an installer before T9b): `unprepared`, naming re-plan and re-apply. `prepare` refuses that receipt (`receipt_outdated`), so no store can be prepared under it.
  - `prepare`'s step 0 is unchanged: on a prepared root it changes nothing, `receipt.json` included.
  - Exit code and top-level status stay unchanged in every case.

  Falsifiers:
  - Two roots with the same project name: root A is applied and prepared, then root B is applied and prepared (step 0, nothing changed). A `verify` on B with Docker reachable reports anything but `prepared`.
  - With no `docker` executable on PATH and no store directory on the bind, `verify` reports `unprepared` or `prepared`.
- **J2: the assistant template stays beside its store until T11.** `/state/memory/assistant/memory.json`, the config template, is written and edited in the runtime because `assistant_host_cli` writes `launches/` beside it. The role preflight and `test_t9b_p7` exempt that template under `/state/memory/assistant`. This replaces P1's example "the assistant template (`memory.json`) moves under `/config`"; T11's leaf module moves it.
- **J3: known limit, volume drivers.** The role preflight recognizes the project volume by its mountinfo root ending in `/volumes/<name>/_data`, as Docker's `local` driver mounts it. A volume plugin, or a volume on its own filesystem, refuses `store_not_project_volume`. `docs/DOCKER.md`'s known limits say so; the supported boundary is one trusted user on one host.
- **M1/M2: the refused copy is a test.** An image-marked test drives `prepare` through its `docker=` parameter. A wrapper alters one non-key column of one row in the migration copy (row count unchanged) after the copy is made. It asserts:
  - the refusal `copy_or_check_failed` naming the table;
  - no `<project>_memory` volume afterwards (the run created it);
  - the host tree, `receipt.json` and the absence of any `.migrated-` directory, all unchanged.

  This kills "a refusal after creation keeps the volume" (H1) and "the per-table digest is skipped".
- **CI portability (Coordinator meet edit).** P4's "no `docker` on PATH" case runs with PATH set to one empty directory, because Linux runners keep `docker` in `/usr/bin`.
- **Arms for this round.**
  - FEATURE: J1 in `_impl/runtime_install.py`, J1 and J3 in `docs/DOCKER.md`.
  - TEST: J1's falsifiers and the M1/M2 test as new image-marked tests under `tests/install/`. TEST also makes `test_t9b_p5_apply_verify.py`'s default-suite verify test honest under J1. With PATH holding no `docker`: a clean root reports `not_observed`, and a root with a store directory on the bind reports `unprepared`, both with exit 0 and `verified`.
