# T7a — Role readiness from an empty root

ADR: [AT-0004](../../adr/AT-0004-portable-kanban-suite.md). Header: [ARM-HEADER](ARM-HEADER.md). Dispatch: FEATURE + TEST, after T4 merges (T4 edits the installer and the compose `capture` role).

## Problem

The Coordinator's T7 empty-root rehearsal (2026-10-01) built `product` from main. It then ran `kp-agent-install plan`, `apply` and `verify` into an empty root with `--components tooling,refresh,capture,board`, and followed DOCKER.md, DESK-MENU.md and MODEL-GATEWAY.md.

What worked:
- the plan wrote nothing;
- apply and verify passed;
- once the operator files were in place, every role was healthy;
- idle CPU was about 0% for all four roles;
- the board answered on loopback;
- refresh published in 21 s;
- `navigation.search` returned results;
- capture imported a transcript;
- the model gateway `doctor` reported ready with 0 network requests.

The rehearsal also found these defects:

1. **First start fails.** `plan`, `apply` and `verify` report nothing missing, yet the first `docker compose up -d` fails outright:
   - `refresh` has no `secrets/github-token`: "bind source path does not exist";
   - `capture` exits 1 with "existing absolute operator file required" because `config/capture/*` is absent.
2. **`initialize` doesn't create the state directory.** DESK-MENU.md says `kp-agent-desk-registry … initialize` "creates the memory state if absent". It actually refuses ("existing private absolute state directory required (0700)") until the operator creates `state_root` by hand.
3. **Misleading error.** A malformed `save` request (`desk_id: null`) is reported as "Registry unavailable; inspect the operator configuration" (category `AttributeError`), not as a field error.
4. **Wrong identity.** `tooling.identity` in an image labelled `org.opencontainers.image.revision=<sha>` reports `status: unknown`, `server_build_revision: null` (`unversioned_source`).
   - `server_identity.py` reads `build_identity.json`, which the image never writes.
   - The Dockerfile deliberately declares `SOURCE_REVISION` only in the final stages.
5. **Undocumented `artifact_root` step.** The model gateway refuses a missing `artifact_root` (by design: no fallback), and the container instructions don't say to create it.
6. **The installer takes no `--home`.** T4 renders `config/launch/harness-profiles.json` with `~/` expanded against the planning user's home, recorded as the plan input `inputs.home`. That input can only come from `$HOME`. The `kp-agent-install` CLI has no `--home`, unlike `--uid` and `--gid`.
7. **A failed capture role takes host launches with it.** Since T4 the `capture` container also runs spool ingestion and is the docker-mode host adapter's runtime: `launch` and the memory MCP run through `docker exec -i <project>-capture`. If workspace capture exits for missing operator files, its supervisor exits with it, so host launches fail too. P1 covers this.
8. **Undocumented attribution boundary.** Workspace capture of a session that `bind` attached to a desk stores its episodes as unresolved evidence:
   - desk-scope `memory.search` excludes them (`excluded_unresolved_episodes: 2`);
   - topic scope finds them.

   Desk-attributed capture comes through the launch hook (T3) and the host adapter's spool ingestion (T4). The documentation does not say this.

## Properties and falsifiers

- **P1: no role fails at creation.** From an empty root, `plan`, `apply`, then `docker compose up -d` with any component set creates every selected container.
  - A role whose operator configuration is absent stays up and reports `not_configured`, naming the missing operator files by root-relative path, in its log and in `kp-agent-install verify`. It does not exit, and it does not loop on restart.
  - An operator-written file (a token, `refresh.json`, `config/capture/*`) is never created with content, replaced or hashed by the installer. A placeholder may be created only where absent, empty, mode 0600, and must be recorded as operator-owned.

  Falsifier: an `up` that fails on a missing bind source; a role that exits or restart-loops for missing operator files; a `verify` that reports `verified` with no readiness entry for an unconfigured role; or an operator file overwritten or re-hashed.
- **P2: the documented steps work verbatim.** From an empty root, an operator following DOCKER.md, DESK-MENU.md, MODEL-GATEWAY.md and HOST-ADAPTER.md exactly reaches:
  - a healthy role set;
  - an initialized portable registry with one desk;
  - one bound session;
  - capture running;
  - a gateway `doctor` reporting `ready`.

  No step may be undocumented, such as an unlisted `mkdir`. Either the behaviour changes (for example, `initialize` creates a missing leaf `state_root` at 0700 under an existing private parent, and nothing else) or the document does.
  Falsifier: any step that needs an action the documents do not state.
- **P3: field errors name the field.** Every malformed registry request (`save`, `save-role`, `bind`, `annotate`) is refused as a ValueError-category error naming the offending field. Examples: a missing or null `desk_id`, a non-canonical desk UUID, a missing `expected_version`, or a wrong type.
  Falsifier: a malformed request reported as "Registry unavailable" or with a non-ValueError category.
- **P4: the image knows its revision.** In an image built with `--build-arg SOURCE_REVISION=<40-hex>`, `tooling.identity` reports:
  - `status: known`;
  - `server_build_revision` equal to that revision and to the image label;
  - an origin that names an image or package build declaration.

  In an image built without it, `status` is `unknown`. A new revision still rebuilds only the final stages.
  Falsifier: a null or differing revision in a labelled image, a known revision in an unlabelled one, or a revision change that invalidates the shared stages.
- **P5: the capture boundary is documented.** DOCKER.md's capture section and MEMORY.md state:
  - workspace capture records unresolved evidence, which topic-scope search reaches;
  - desk-attributed capture comes from launch hooks (T3) and host spool ingestion (T4).

  Falsifier: a statement the code contradicts.
- **P6: the home directory is an explicit plan input.** `kp-agent-install plan|apply --home <absolute existing directory>` sets `inputs.home`. Without the flag, `$HOME` applies as today. `verify` does not treat a different invoking `$HOME` as drift.
  Falsifier: `--home` ignored, a relative or missing directory accepted, or a `$HOME` change reported as drift.
- **P7: host launches survive an unconfigured capture.** With `capture` selected but `config/capture/*` absent, the capture container stays up. Spool ingestion keeps running and `kp-agent-host launch` works in docker mode.
  Falsifier: the capture container exits, or a docker-mode launch fails, only because workspace capture is unconfigured.

## Write scope

- **FEATURE:**
  - `_impl/runtime_install.py`, `install_cli.py`, `assets/deploy/compose.yaml`;
  - `deploy/image/container.py` (role readiness);
  - `deploy/Dockerfile` (final stages only, for P4);
  - `_impl/server_identity.py`;
  - `desk_registry_cli.py` and `_impl/service/desk_profiles.py` (error mapping only);
  - `_impl/service/desk_registry.py` or `desk_memory_runtime.py` (only the P2 `initialize` change, if chosen);
  - `refresh_cli.py` and `workspace_capture_cli.py` (only the `not_configured` start path);
  - `docs/DOCKER.md`, `docs/DESK-MENU.md`, `docs/MODEL-GATEWAY.md`, `docs/HOST-ADAPTER.md`, `docs/MEMORY.md`.

  Every existing test stays unmodified and green.
- **TEST:**
  - new tests under `tests/install/` (P1, P2), using the image marker for container behaviour and reading `AGENT_TOOLING_TEST_IMAGE`;
  - `tests/registry/` or the existing T2 test directory, new files only (P3);
  - `tests/image/` (P4, image-marked);
  - `tests/docs/` (P5);
  - `tests/install/` (P6);
  - `tests/host/`, new files only, image-marked for container behaviour (P7).

## Acceptance at the meet

The Coordinator repeats the empty-root rehearsal verbatim from the updated documents, with a fresh image, and records each step's command and result in `docs/CUTOVER.md` under "T7 rehearsal".
