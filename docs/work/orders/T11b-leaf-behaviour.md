# T11b — The leaf enforces: store paths at open, private writes, one SQLite profile, the template leaves the volume

ADR: [AT-0004](../../adr/AT-0004-portable-kanban-suite.md). Header: [ARM-HEADER](ARM-HEADER.md). Dispatch: FEATURE + TEST.

Status: frozen 2026-10-03 (r3, Coordinator). r3 adds Verification's N1–N4 and the carry from T11a's meet. Split from r1's T11 by observability (Verification Q3). Base: main with T11a merged; T11b's base-vs-head instruments use it as "base". Every property here is a ruled behaviour change, made through the leaf's arguments that T11a introduced.

## Properties and falsifiers

- **Q1: In the Docker runtime, store paths outside the volume are refused, for reads and writes.** This carries T9b Amendment 3 D2, and Verification's Q2, L4, N1 and N2.
  - **How the runtime is recognised.** The leaf treats a process as being in the Docker runtime when `AGENT_MEMORY_VOLUME` is set. Every role receives it from Compose `x-environment`, and `docker exec` and `docker compose run` inherit it. The T9b preflight already keys `manifest_without_store_volume` on the same variable.
  - **What a store path is (N2).** It is defined by how the path is derived, not by file type. A store path is derived from `state_root`, `roster_path` or a `/state` `config_template`, or from their descendants:
    - `launches/`, its receipts and per-launch memory configs;
    - the registry roster;
    - the spool-ingest and session-import ledgers;
    - the stores `components()` derives.

    These are the census's 14 sites plus `components()`.
  - **Where the refusal is enforced.** At the derivation points: the leaf's `store_file(state_root, name)`, the roster and `launches/` derivations, and `components()`.
    - Each one returns a marked store path: a `StorePath` type or a `store=True` argument. FEATURE chooses and names it.
    - The generic helpers (`sqlite_open`, `ensure_private_dir`, the write helpers, `read_private_json`) refuse with `store_outside_volume`, naming the path, only when a marked path does not resolve under `/state/memory`.
  - **Unmarked paths are not stores, and are unaffected.**
    - model_gateway's `ledger.sqlite3` (`model_gateway.py:326`);
    - knowledge_publish's `references.sqlite3` (`:107`);
    - the code_references manifest (`manifest.py:41`);
    - the knowledge lifecycle database under `/state/knowledge`;
    - runtime_install's NOT_STORES directories: `/state/refresh`, `/state/delivery`, `/state/search`, `/state/snapshots`, `/state/tmp`, `/state/models`, `/state/backups`.
  - **Reads are refused as well as writes (N1).** A torn read from the bind is how T9b's corruption happened.
  - **Direction of the variable.** A host process that sets the variable only refuses more. A host install without it is exempt by design, as T9b P5 scoped.

  Falsifiers (image-marked, `t11-` prefix). Each case runs inside the container under a `sys.addaudithook` on `open`, `os.mkdir` and `sqlite3.connect`, wrapped around the CLI's main through `python3 -c`. In every refused case, no store file is opened for read or for write, and none is created.
  - (a) `docker exec` of `kp-agent-desk` with a configuration whose `state_root` is outside `/state/memory`;
  - (b) the same through `docker compose run --rm tooling`;
  - (c) `docker exec` of `kp-agent-launch … prepare` with an outside `state_root`: no directory is created.

  Positive controls (N2):
  - (d) inside the runtime, model_gateway's ledger and a `/state/refresh` publication still open and write;
  - (e) a `docker exec` that reads `/config/launch/desks.json` still works;
  - (f) a host install with no `AGENT_MEMORY_VOLUME` keeps its own `state_root` working.
- **Q2: One SQLite profile (L2).**
  - **Resolved URIs.** Every connection uses a resolved URI, ruled here. The 13 sites that do not resolve today change how a symlinked `state_root` behaves, and Q1's check needs the resolved path anyway.
  - **Timeout.** The default stays at sqlite3's 5.0. The sites with 10 and 30 keep theirs, and `isolation_level=None` and `check_same_thread=False` stay where they are.

  Falsifier:
  - a connection opened through a symlinked parent that reaches a file other than the resolved target;
  - a timeout that differs from its T11a value (measured with the T10 instrument).
- **Q3: Private writes close their windows (L3).**
  - **Create with mode.** Every private file is created with its mode, so there is no create-then-chmod window. No write follows a symlinked final component.
  - **Durability of publications only.** fsync is ruled for publication writes alone: publish-once (link) and replace (rename). Each fsyncs the file, then its parent directory, so a published artifact (a receipt, a config, a manifest) survives power loss once the call returns.
  - **No fsync for create-new hot-path writes**, such as `launch_binding.write_private` in a hook flow. FEATURE measures the cost per hook flow on Docker Desktop before and after, and states it.

  Falsifier:
  - a private file observable with another mode between creation and close (audit hook on `os.chmod` after `open`);
  - a write through a symlinked final component that succeeds;
  - a publication without a file fsync and a parent-directory fsync.

  Instrument for the fsync falsifier (N4): `os.fsync` raises no audit event, and strace is not available on macOS. So TEST wraps `os.fsync` in-process and records each fd's `os.fstat` identity (`st_dev`, `st_ino`). After the call returns, it compares those against the published file and its parent directory.
- **Q4: The assistant template leaves the volume (T9b Amendment 7 J2, L7).**
  - **New location.** The assistant `memory.json` template is operator-written under `$root/config/`, at the path the docs name.
  - **Where `launches/` lives (N3 i).**
    - It is `state_root/launches`, using the template's own `state_root` (`/state/memory/assistant/store`). So it resolves to `/state/memory/assistant/store/launches`.
    - It is never `template.parent` (`assistant_host_cli.py:41`), and never `state_root.parent`.
    - The existing `/state/memory/assistant/launches/` is not migrated. A launch it holds is relaunched, which is T9b D3's answer.
  - **Removed together (N3 ii).** The T9b exemption lives in three places:
    - `container.py`, `TEMPLATE_KEY` under `/state/memory/assistant`;
    - `runtime_install.required_store_path`, which returns None for a `config_template` under `/state/memory` (`runtime_install.py:742-745`). With it gone, `verify` names the operator file;
    - `test_t9b_p7`'s exemption.
  - **Operator step for an existing runtime.**
    1. A one-off container prints the template: `docker compose --project-directory "$root" run --rm -T tooling cat /state/memory/assistant/memory.json`.
    2. The operator writes it under `$root/config/`.
    3. The operator edits `config_template` in the binding.
    4. Roles refuse until then: the exemption is gone, and `verify` names the operator file.

  Falsifiers:
  - a role that writes under `/config`;
  - a template under `/state/memory` that a role accepts, or that `verify` does not name;
  - after the documented step, the old template left in the volume is read (audit hook);
  - an existing assistant binding stops resolving after the step.

## Write scope

- FEATURE:
  - the leaf's arguments and their call sites;
  - `assistant_host_cli.py`;
  - `deploy/image/container.py`;
  - `docs/DOCKER.md`, `docs/MEMORY.md`;
  - `deploy/examples/*`.
- TEST:
  - new image-marked tests for Q1 and Q4;
  - in-process tests for Q2 and Q3;
  - the exemption's removal in `test_t9b_p7`. That is a regression-set exception, limited to removing the exemption.

## Carried from T11a (meet, 2026-10-03; Amendment 1)

- **The wider P3 primitive set.** It covers chmod, 0700 mkdirs, renames, fsync and `st_mode & 0o077`. In T11a it was a `--report` reading only. T11b's Q3 adopts it as a property, with T11a's guard as the instrument.
  - **At T11a head, 13 sites remain outside the leaf:** Path.mkdir(mode) 8, os.chmod 2, Path.rename 1, os.fsync 1, os.mkdir(mode) 1. T11a's report lists each one.
  - **Sites F1 reverted to base code**, exercised only in subprocesses or images:
    - `container.py:267`;
    - `runtime_install.py:1863/1865`;
    - `launch_binding.py:272`;
    - `model_gateway.py:336/340/476/481`;
    - `host_adapter.py:213/574/777`;
    - the `host_adapter.py:316` spool append.
  - **Q3's instrument for these sites:** image-marked recordings, or an in-process recorder that the subprocess entry points also load.
- **P5's two readers.** `leaf.required_store_path(reject_control=…)` has two readers: runtime_install ignores a value containing a control character, and container.py refuses it. T11b's Q1 may unify the two readings as a ruled change, and must name what each consumer then does with `/state/memory/x\x00`, which container.py accepts today. T11a's U3 golden is the instrument.
- **The fsync instrument.** T11a's F1 fsync comparison recorded the file path behind each fd. It is not Q3's instrument. Q3 must see the parent directory's fsync: fd identity (`st_dev`, `st_ino`), compared against the published file's and the parent directory's.
- **The base for base-vs-head comparisons** is T11a's merged head on main. T11a's goldens (P1, P3, P4, P7 and U3) are T11b's regression set. T11b changes only the leaf arguments its properties rule; every other golden must stay byte-identical.

## Meet note (Verification C1, C2; 2026-10-03; not an amendment of substance)

T11b and T12a run in parallel from that base.
- **C1.** Reconciling `deploy/image/container.py` at the second slice's meet is a Coordinator meet edit, made as a separate commit. The packet shows the merged file whole. The checks on the merged tree, with a fresh image: both slices' tests, plus the T9b P1b image tests and T10h's P3.
- **C2.** "A role refused" has one reading. Every assertion that a role's store preflight refused goes through one harness helper, `tests/install/t9b_harness.py` `assert_role_refused(project, service, reason)`. Its body follows the tree it runs on:
  - before T12a merges, the role exits with status 3;
  - after, it stays up with health `refused` and never execs its command.

  T11b's open-time `store_outside_volume`, raised for `docker exec`'d tools and `compose run`, is not a role refusal. It stays a non-zero exit in both worlds.

## Amendment 1 (meet notes, Coordinator with Verification; additive)

- **Q4 step 1 runs outside the preflight.** With the exemption gone, `docker compose run --rm -T tooling cat …` starts a role that its preflight refuses. The documented step is `docker compose --project-directory "$root" run --rm -T --entrypoint cat tooling /state/memory/assistant/memory.json`. The `--entrypoint` form bypasses the role preflight, as DOCKER.md step 7 already does.
- **The template path.** FEATURE's documented `/config` path decides. If TEST's chosen path differs, TEST's exception moves to FEATURE's path.
- **A named regression-set exception for Q4.** `tests/install/test_t7b_p4_assistant_memory_image.py` placed the assistant template under `/state/memory/assistant`. Only its template location and its binding's `config_template` move to the documented `/config` path.
- **Correction to Q1's unmarked list.** The order called `/state/refresh` and `/state/backups` "runtime_install's NOT_STORES directories", but the code's `NOT_STORES` set includes neither. That set is not the mechanism: a store is defined by its derivation, as a marked path, so an unmarked path such as `/state/refresh` is unaffected, whatever `NOT_STORES` contains.
- **Docker naming for arms.** The rule's object is the LIVE Compose project `agent-tooling`: its containers (`agent-tooling-tooling`, `agent-tooling-<role>-1`), its volume `agent-tooling_memory`, and its runtime root. It never covers the test fixtures' `agent-tooling-image-test-*` names. Arms run the repo's image suites, which create and remove their own fixtures.
- **C1/C2:** see the meet note above.
- **Blindness record.** Each meet packet carries every arm's verbatim answer on whether it read any file it did not write in the shared session scratchpad, and a listing of the arm's private scratch directory (its TMPDIR).
- **The template path, as documented:** `/config/board/assistant-memory.json`, which is `$root/config/board/assistant-memory.json` on the host. FEATURE's docs and TEST's tests agree on it.
- **The regression set at the meet: 83 entries ruled.** `tests/test_t11b_regression_set.py` `RULINGS`:
  - Q3: 26 entries. 24 P3 core and 2 ops: create-with-mode, `O_NOFOLLOW`, and the publication's fsync of the file and its parent.
  - Q2: 24 entries. 19 P4 core and 5 ops: every connection is a resolved `file:` URI. Only `database` and `uri` change.
  - Q4: 33 entries. U3 `config_template` under `/state`.
  - P1 and P7 are byte-identical.
- **Q2's scope, ruled at the meet.** "Every connection uses a resolved URI" is literal: plain-path opens are included. SQLite names its `-journal` after the path it was opened with, so a plain path through a symlinked component can put the journal beside the link rather than the target. Creation opens use `mode=rwc`. Stores the leaf pre-creates at 0600 (`sqlite_create`) open with `mode=rw`.
- **The guard's `directory_rename` class gains `workspace_setup.py:178`, `apply`, `Path.rename`.** That call renames a temporary staging directory into the bundle path. Q3's wider-set assertion reaches it.
- **Two readers of `reject_control` are kept**, as T11a left them. FEATURE did not unify them; U3 changed only in the Q4 template cases.
- **T11a's byte-identity tests at T11b's head.**
  - **The failure.** The P3, P4 and U3 goldens are the ones T11b's properties change. T11a's five tests over them fail at head by design, and nothing else fails:
    - the P3 tests, core and ops;
    - the P4 tests, core and ops;
    - the P5/U3 test.

    The meet's full run at the first meet head shows the default suite with 5 failed and the ext suite with 2 failed, the ops pair through `test_t11a_ops_goldens.py`. CI on the T11b pull request shows the ext pair red.
  - **The ruling.** Each of those tests is renamed `…_match_base_except_t11b_rulings` and asserts through `test_t11b_regression_set.assert_matches_base_except_ruled`. The text is byte-identical to base, or every differing entry is in `RULINGS` and every other entry is identical.
  - **What does not change.** P1 and P7 keep T11a's byte-for-byte assertion. The goldens are not regenerated: they stay T11a's base reading.
- **A ruling excuses only its own kind of change.**
  - **The gap.** At the first meet head, a ruled entry passed whatever else changed in it. The label that showed an extra change, such as `NOT A Q2 FIELD: timeout`, `outcome changed` or `tree changed`, was printed and never asserted.
  - **The check.** `ruling_mismatch` now requires:
    - a Q2 entry reads exactly `Q2 resolved URI`;
    - a Q3 entry carries only Q3 kinds;
    - a Q4 entry reads `Q4 template`.

    Both the regression-set test and the T11a P3/P4/U3 reading assert it. At head, all 83 ruled entries meet it: Q2 24, Q3 26, Q4 33.
  - **Coordinator mutants.** Each injects an extra unruled change into one ruled entry's base reading. At the first meet head's check all three pass; at the meet head each is red in both its T11a test and the regression-set test:
    - K1: the Q2 entry `budget ledger: reserve` with a timeout change;
    - K2: the Q3 entry `desk_catalog_setup._write_private_json` with an outcome change;
    - K3: the ops Q3 entry `knowledge_publish_cli._write` with a tree change.

    Five more mutants each drop one ruling: one P3 core, one P3 ops, one P4 core, one P4 ops and one U3. Each turns exactly its own T11a test red.
  - **What it cannot see.** U3's label is read from the entry's key, so for Q4 entries the Q4 property tests read the change, not this check.
- **The whole-set check in CI's two environments.**
  - **The failure.** CI's core-only jobs do not install `extensions/ops`, so the ops goldens are not compared there. The stale-ruling check still counted the 7 ops rulings, and `test_every_regression_set_difference_is_listed_and_ruled` failed on 3.11 and 3.12 at the first meet head with "7 rulings name an entry that does not differ". Locally ops is always installed, which is why the meet's runs did not show it.
  - **The fix.** A ruling is stale only if its golden was compared. The ext job now also collects the whole-set test, through `extensions/ops/tests/test_t11a_ops_goldens.py`, where every golden is compared.
  - **Evidence.** A core-only venv shaped like CI (`pip install -e './packages/tooling[test]'`, no ops) was used:
    - the earlier check fails, as CI did;
    - the fix passes;
    - a fake stale ruling on a core golden still fails there;
    - a fake stale ruling on an ops golden fails in the ext collection.
- **Verification's U1: a private write ends exactly 0600 (MV8 survived the meet's set).**
  - **The survivor.** Verification's mutant MV8 (`_settle_mode`'s body replaced by `return`) left all 21 Q1/Q2/Q3/regression tests green. Base guaranteed the final mode, and the leaf's docstrings claim it, but the P3 golden pins umask 0o022 and never overwrites a wider file.
  - **The test.** `tests/test_t11b_u1_private_modes_end_exact.py`, a Coordinator meet commit with tests only:
    - `overwrite_private` over an existing 0644 file ends 0600 with the new content;
    - `touch_new_private` and `write_new_json` under umask 0o277 end 0600.
  - **Controls.** An existing 0600 file, and umask 0o022.
  - **Under MV8**, exactly the three cases are red: 0644, 0400 and 0400. All six are green at head.
  - **The Q3 swap instrument's limit (Verification MV7)** is now named in its docstring. Its "created" set holds a replace's `mkstemp` temporary, not the destination, so a chmod by path after `os.replace` is caught by the regression set and T11a's P3 reading, not by the swap.
