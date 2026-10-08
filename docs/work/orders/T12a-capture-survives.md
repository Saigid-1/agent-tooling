# T12a — Capture survives: the watch loop never ends on an error; roles restart; refusals wait

ADR: [AT-0004](../../adr/AT-0004-portable-kanban-suite.md). Header: [ARM-HEADER](ARM-HEADER.md). Dispatch: FEATURE + TEST.

Status: frozen 2026-10-03 (T12 draft r3, Coordinator). Verification ruled it freezable after T11a merges. Base: main with T11a merged. It runs beside T11b; both touch `deploy/image/container.py`, and the second meet reconciles them on main.

Principal direction (2026-10-02, accepted): "net new transcript content is a direct write that can be indexed separately in the background". T12 is the indexer outbox (T12b) and per-desk postings (T12c); T12a ships first, because capture lost its loop twice on 2026-10-03.

**Problem.**
- `workspace_capture_cli.main` wraps the whole loop in `except (CaptureError, OSError, ValueError)` (:90). Any such error, or any other exception, ends the loop.
- `spool_ingest.watch` then exits with the child's status (:715).
- No Compose service declares `restart:`, so every service defaults to `no`.
- On 2026-10-03 this cost two capture outages: a transient git-identity check, and T10's `EpisodeUnavailable`.

**Properties.**
- **A1: the loop survives.**
  - A pass that raises anything reports one JSON line: `status: error`, a `category` (the exception class family), the message capped at 512 chars, and the attempt number. The loop then sleeps with bounded exponential backoff, from the interval up to 10× the interval, and a clean pass resets it.
  - Only a deliberate stop (a signal), reaching `--max-passes`, or operator-file absence (`not_configured`, the existing wait) ends or parks the loop.
  - **Test seam (S7):** `watch --max-passes N`, a CLI argument, so tests drive exactly N passes. Default: unbounded.
  - **Falsifier:** an in-process `main(['--config', …, '--policy', …, 'watch', '--max-passes', '6', '--interval-seconds', '5'])`, with injected failures on passes 2–4 (`CaptureError`, `sqlite3.OperationalError('database is locked')`, `RuntimeError`), fails if any of these happens:
    - it does not complete pass 6;
    - it does not print three error lines, each with its category;
    - it does not back off (sleep calls are recorded through an injectable clock, not patched).
- **A2: services restart, and refusals wait (Q3).**
  - Restart policy: `restart: unless-stopped` for tooling, refresh, capture and board (T3). Tooling is included because an OOM kill or a daemon restart otherwise leaves every `docker exec` memory MCP server without its container. `stop` still stops it, and `prepare` still asks for the stop.
  - **Store-preflight refusals no longer exit 3.** That covers `store_unprepared`, `operator_file_names_outside_store`, `store_not_project_volume` and `manifest_without_store_volume`. Like T7a's `not_configured`, the role:
    - stays up and logs the refusal as JSON;
    - reports health `refused`;
    - re-checks on an interval;
    - never execs the role command until the refusal clears.
  - T7a's "stays up" contract becomes: no restart caused by a missing operator file or a refusal.
  - **Recovery when the store is unprepared (T1, option a).** T9b P4's `writers_running` rule is unchanged: a waiting role is a running container of the project, so `prepare` refuses while it waits. The documented recovery is `docker compose stop` → `prepare` → `up`. The "clears within one re-check interval" falsifier applies only to `operator_file_names_outside_store`, which the operator clears by editing the file, without `prepare` and without a stop. `docs/DOCKER.md`'s sequence says this.
  - **Regression-set exception (named):** the T9b P1b tests read "refuses" as "does not exec the role command, and health says refused" instead of "exit 3". This is limited to that assertion.
  - **Falsifiers (image):**
    - a role started on an unprepared store exits;
    - it restart-loops;
    - it runs its command before `prepare`.
  - **Further falsifiers:**
    - after the operator edits a file named by `operator_file_names_outside_store`, the waiting role does not start its command within one re-check interval;
    - a capture container killed by `docker kill --signal KILL` is not running again within 30 s (`RestartCount` ≥ 1);
    - `docker compose stop` leaves anything restarting.
- **A3: K2 legibility.** When the image is absent, `verify`'s `memory_store.reasons` also lists whether the volume is present. Its status is unchanged.

**Write scope.**
- FEATURE: `workspace_capture_cli.py`, `spool_ingest.py` (watch), `deploy/image/container.py` (preflight wait), the packaged `compose.yaml` (restart), `_impl/runtime_install.py` (K2), `docs/DOCKER.md`.
- TEST: new files, plus the named T9b P1b exception.

## Meet note (Verification C1, C2; 2026-10-03; not an amendment of substance)

T11b and T12a run in parallel from that base.
- **C1.** Reconciling `deploy/image/container.py` at the second slice's meet is a Coordinator meet edit, made as a separate commit. The packet shows the merged file whole. The checks on the merged tree, with a fresh image: both slices' tests, plus the T9b P1b image tests and T10h's P3.
- **C2.** "A role refused" has one reading. Every assertion that a role's store preflight refused goes through one harness helper, `tests/install/t9b_harness.py` `assert_role_refused(project, service, reason)`. Its body follows the tree it runs on:
  - before T12a merges, the role exits with status 3;
  - after, it stays up with health `refused` and never execs its command.

  T11b's open-time `store_outside_volume`, raised for `docker exec`'d tools and `compose run`, is not a role refusal. It stays a non-zero exit in both worlds.


## Amendment 1 (meet rulings, Coordinator; additive)

- **A2: every store-preflight refusal waits, for the command shapes that run long (R1, worded as the code).**
  - **Which commands wait.** The discriminator is the command's shape: a command that is `wait`, the board command, or one carrying the token `--watch` waits on every store-preflight refusal. That covers the four named refusals, `store_not_mounted`, and any other preflight reason.
  - **How a waiting role behaves.** It stays up, reports health `refused`, re-checks every 10 s, and never execs its command until the refusal clears.
  - **Every other command exits 3**, for example a one-off `… find`, `… prepare`, or `ingest-spool` without `--watch`.
  - **One-offs.** A one-off `docker compose run <role service>` that keeps the service's default command waits too. `docs/DOCKER.md` says so, and names the bypass: `--entrypoint`, or a different command.
  - **`store_not_mounted`** cannot clear in place. Its `recovery` names re-apply plus `up -d --force-recreate`.
  - **Crashes are outside the contract.** A preflight exception that is not a refusal is a crash. It exits non-zero and Docker's restart policy applies; the contract does not cover it. The example is a failing `stat` of `/state/memory`. An unreadable mount table is not one: `mounts()` returns `{}`, which reads as `store_not_mounted`, a refusal that waits. FEATURE corrected the Coordinator's first example.
- **A2: test cases (Verification).** A TEST case for a one-off command that must exit 3 uses a non-watch command, never `wait`.
- **A2: the restart falsifiers (R2).**
  - **Why the order's wording fails.** Docker treats `docker kill --signal KILL` as a deliberate stop under every restart policy. This was measured on Docker 29.5.3 by FEATURE and independently by Verification: exit 137, RestartCount 0, no restart. `docker kill` therefore leaves the container stopped, and `docs/DOCKER.md` documents it as a stop.
  - **The falsifier of record: a real OOM on the role image under the packaged Compose file.** A Compose override sets `mem_limit` on one role, and the role's main process (tini's child) allocates past it.
    - Required: Exit 137, `OOMKilled` true, and the container running again with RestartCount ≥ 1 within 30 s.
    - Verification measured that an OOM kill of a non-main process leaves the container running with no restart. The packet therefore names which process was killed.
  - **The in-suite stand-in.** A SIGKILL from inside the container of docker-init's child (the role's main process), with the PID named, brings it back within 30 s with RestartCount ≥ 1.
    - This depends on the roles' `init: true` (`compose.yaml` `x-role`). Without an init process, a `kill -9 1` from inside is dropped by the kernel.
    - The test runs on the role image under the packaged Compose file, not on plain alpine.
  - **Capture.** The container comes back after its child is killed only because `spool_ingest.watch` exits with the child's status. The packet names the process killed.
- **Recorded readings** (FEATURE AMBIGUITY 3–10, accepted):
  - Health `refused` exits 1, so Docker shows `unhealthy`. `not_configured` stays 0.
  - The board's healthcheck becomes `tooling-container health`, keeping HTTP-answer semantics.
  - Error lines carry `category`: the declared category if there is one, otherwise the class name. They also carry `exception`, `attempt` (consecutive failures), `pass` and `retry_in_seconds`.
  - The first backoff equals the interval.
  - At `--max-passes` the loop returns 0, or 1 if the last pass failed.
  - Operator-file absence is re-checked on every pass.
  - The re-check interval is fixed at 10 s.
  - The clock seam is `main(argv, *, clock=time)`.
- **A one-off healthcheck exception:** `compose run` one-off containers get restart policy `no`, as Compose v5.1.4 does by default. Measured.
- **Board health under the wait.** `role_argv(role_command())` reads through docker-init's argv to the token after `tooling-container`. That is correct given `init: true`, and an image test with the real PID 1 checks it.
- **Packet hygiene.** Diff stats are taken against the merge base, because the arms branched at the base, before two later merges. Arm containers and volumes are gone before the meet's resource census.
- **`--force-recreate` is documented in its own code span.** The T7a P2 and T7b P1 docs scanners execute any documented `docker compose … --force-recreate` command they find. `store_not_mounted`'s recovery therefore names `docker compose --project-directory "$root" up -d` and `--force-recreate` as separate spans, so the scanners do not run a destructive recreate.
- **Images.** The meet rebuilds all five targets at the merged head; FEATURE's `agents` and `ops` predate R1's `container.py`.
- **TEST's readings, accepted at the meet** (TEST AMBIGUITY 2–9):
  - `category` is a non-empty string, and its spelling is not pinned;
  - `attempt` is an integer under a key containing `attempt`;
  - the message is capped at 512 characters, not dropped;
  - "health `refused`" means `docker exec … tooling-container health` prints JSON with `status: refused`, and its exit code is not pinned there;
  - "never execs the role command" is a process probe, and health is the observation for tooling's `wait`;
  - one re-check interval is 10 s plus 5 s of slack.
- **The regression set beyond the named P1b exception.** Several tests assert through `t7a_harness.Runtime.assert_stays_up`: RestartCount 0 and the same StartedAt in a quiet window. They are T7a P1, T7a P2, T7a P7, T7b P1 and T9b P1/P2. They keep their assertions: in a quiet window they already mean "no restart caused by a missing operator file or a refusal". T9b P3 D3's docstring now says what it observes: under A2 the refused capture role stays up, so the exec'd hook runs in it and must itself fail. Its assertion is unchanged.
- **Docker object names for arms.** ARM-HEADER now names every image, container, volume, network and Compose project by the prefix the dispatch names, replacing `agent-tooling-arm:<slug>` (TEST AMBIGUITY 11).
- **A wrong capture operator file is a pass error (meet, docs).** `docs/DOCKER.md` said a present-but-wrong capture file makes the capture role exit and needs `up -d capture`. Under A1, each pass reads the policy and config, so the error is reported, retried with backoff, and cleared by the next pass after the fix, with no restart. Measured in-process at the meet: a 0644 policy gave one `ValueError` error line (attempt 1), and after `chmod 0600` passes 2 and 3 were `ok`.
- **Coordinator mutation-RED, in-process (A1 and A3).**
  - **Killed:**
    - MA1, no growth: the backoff tests;
    - MA2, uncapped: the 10× test;
    - MA4, only `CaptureError` survives: 4 tests;
    - MA5, message uncapped: the error-line test;
    - MA6, one pass past `--max-passes`: 4 tests;
    - MA7, no `attempt`: the error-line test;
    - MA8, volume presence not reported: both A3 tests;
    - MA9, presence inverted: both A3 tests.
  - **Survived, then killed by TEST's instruments (a TEST commit, merged at the meet):**
    - MA3, a clean pass resets the wait but not the failure count, so the next failure backs off from the escalated value. That is the order's "a clean pass resets it". Instrument U1: failures on passes 2–4 and 6, with the wait and the attempt number after pass 6 equal to pass 2's.
    - MA10, the loop catches `BaseException`, so a `KeyboardInterrupt` or `SystemExit` in a pass no longer ends it. That is the order's "only a deliberate stop (a signal) … ends … the loop". Instrument U2: such a pass ends the loop, with no later pass and no error line. In the container, SIGTERM's default action kills the process without a Python exception, so MA10 is observable only for SIGINT or SystemExit.
  - **After the merge.** FEATURE passes all 8 A1 tests. MA3 fails U1 (`test_a1_a_failure_after_a_clean_pass_backs_off_from_the_start_again`), and MA10 fails both U2 cases (`test_a1_a_deliberate_stop_ends_the_loop[KeyboardInterrupt|SystemExit]`).
  - **U1 narrows a reading:** the attempt number counts consecutive failures since the last clean pass.
- **Coordinator mutation-RED, image (A2).** These ran against the meet images at the meet head, with `container.py` mutants as overlay images and the Compose mutant in the packaged file the host installer renders. Each is red for its own reason:
  - MI1, no restart policy: both A2d tests. Capture is not running 30 s after a SIGKILL of its main process (PID 7, `kp-agent-launch ingest-spool`), and tooling is exited after its OOM (137, `OOMKilled`).
  - MI2, no command waits: A2a[capture] and A2c. Capture exits 3 and restart-loops.
  - MI3, a waiting role never re-checks: A2c. Capture's command does not run within 15 s of the operator's edit.
  - MI4, health says `ready` while refused: A2a[tooling].
  - FEATURE's real images were first run here: A2 and the edited P1b, 33 passed, exit 0.
