# B57: the real-OOM test attributes the death from a second source

Header: [ARM-HEADER](ARM-HEADER.md). Dispatch: one arm (FEATURE). The change is test code.

Status: frozen 2026-10-07 (the Coordinator's draft r2; Verification's reads r1 → r2).
- r2 takes in Verification's first read, five changes:
  1. `State.OOMKilled` is ruled out up front;
  2. the second source must identify THIS container, and the candidates are ranked;
  3. the skip is of the source, never of the test;
  4. the falsifiers run on the runners, through a self-falsifying test in the module;
  5. P3's line names the evidence.

Base: main after D0a-2.

**The Principal's decision this order carries:**
- **Backlog 57 before rc.1** (2026-10-07, direct in the Coordinator's session): "let's fix 57 now". Verification had named it a release-path exposure.

## Problem (measured, 2026-10-07)
**`test_a2d_ii_a_real_oom_of_the_main_process_restarts_the_role`** (`tests/install/test_t12a_a2_roles_restart_and_refusals_wait_image.py`, T12a A2 (d-ii), Verification's falsifier of record) failed once on the arm64 rehearsal of D0b's PR.
- **What held:**
  - the allocating process was the main process (PID 7, child of docker-init);
  - it died with a `die` event, exitCode 137;
  - the role was running again within `RESTART_WINDOW` with `RestartCount >= 1` and its restart logged.
- **What failed:** the last assertion, `assert oom_killed`. `docker events` held only `die` (exitCode 137): no `oom` action and no `oomKilled` attribute.
- **The same test passed elsewhere:** on arm64 in that PR's first run and in the re-run's second attempt, and on amd64 in every run.

**The test's attribution has ONE source:** the Docker daemon's OOM report. `t12a_harness.events()` reads `docker events` filtered to `die` and `oom`. The `oom` action and the `die` event's `oomKilled` attribute both come from the daemon's OOM monitoring. When that monitoring misses the kill, the test is red although the property held.

**Why it matters for the release:**
- The tag run executes `tests/install -m image` in `rehearsal` and, per architecture, in `publish-digests`; `manifests` needs both jobs.
- A miss on the rc.1 tag stops the publish before any manifest list or tag.
- A pull request that touches `tests/install/**` runs both rehearsals and `images.yml`.

## Properties
- **P1. The death is attributed to OOM from the daemon's report OR from a second source that is independent of it AND identifies this container.**
  - **The daemon's report:** every `docker events` field and `State.OOMKilled`. `State.OOMKilled` is the daemon's own flag, set from the same OOM event that produces the `oom` action. It stays false when the daemon misses the event, and the restart replaces State before a poll can read it. It is never a second source.
  - **The second source identifies THIS container:** its cgroup (by container id) or its task. A bare count is not admissible. A parent cgroup's hierarchical `oom_kill` moves for any descendant's OOM, so on a shared host a foreign kill would attribute this container's death.
  - **Candidates, ranked. The arm measures from the top and takes the first that works on the runners:**
    1. **The kernel's own OOM record.** On cgroup v2 the kill line carries `oom_memcg=…docker-<container id>.scope`, `task=…` and the host pid. It is read with `journalctl -k --since <trigger time>` or `sudo -n dmesg`; the arm measures which of the two the runner user can read. It is persistent, container-specific and independent of the daemon.
    2. **The container cgroup's own `memory.events` `oom_kill`,** read before the restart re-creates the cgroup. This is racy by design and only a fallback.
    3. **A parent cgroup's `oom_kill` delta,** only together with a per-container discriminator, never alone.
  - The arm names the source and its independence argument in the test's docstring and in its report.
  - **Where the second source cannot be read** (for example Docker Desktop's VM kernel, from the Mac host), the SOURCE is skipped, with a printed reason. The daemon's report then remains the only source, so a missed daemon report FAILS the test there exactly as at base. The skip is of the source, never of the test.
- **P2. Everything else the test asserts is unchanged:**
  - the main-process precondition;
  - exit 137 on the `die` event;
  - running again within `RESTART_WINDOW` with `RestartCount >= 1` and "t12a-oom: restarted" in the log.
- **P3. The test names the attribution and its evidence.** It prints one line:
  `t12a (d-ii): OOM attributed by <daemon|kernel log|cgroup> (<the matched line or the counter values>)`.
  That line shows, across rehearsals and tag runs, how often the daemon misses.
- **P4. The falsifiers run on the runners.** A new image-marked test in the same module, `test_a2d_ii_attribution_falsifiers`, runs three shapes, each in its own OOM world, and asserts each shape's stated outcome of the ATTRIBUTION step:
  - **F1, the daemon's report dropped:** no `oom` action and no `oomKilled`, with a real OOM. Attributed through the second source. On a host where the second source is skipped, the case is skipped with that reason. A skip is never a pass.
  - **F2, a death that is not an OOM:** the trigger is never written. The main process is SIGKILLed through `docker exec` (`kill -KILL <pid>`), so it dies with 137 below its limit. With the daemon's report dropped as well, it is NOT attributed: the second source answers "no OOM for this container" in the window.
  - **F3, the second source blinded:** a real OOM, with the daemon's report dropped and the second source's reading forced to "no OOM". NOT attributed.

  The shapes are applied through a test-only parameter inside the module, for example a helper that both tests call with a shape. They never go through the product or the workflows. The arm measures the falsifier test's cost (three OOM cycles) and reports it.
- **P5. Nothing else changes.**
  - The other T12a tests and `t12a_harness`'s existing helpers keep their behaviour.
  - The module docstring's (d-ii) reading is updated: the two sources, the ranking chosen, and the skip rule.

## Falsifiers (the meet's mutants)
- **On the runners, through the PR (the run of record):** the falsifier test's three shapes (P4), on amd64 and arm64.
  - F1 is attributed by the second source.
  - F2 and F3 are not attributed.
  - The real (d-ii) test is green.
- **Static mutants, applied to the module at the meet (the Coordinator and Verification):**
  - **M1:** the shape parameter is ignored (the daemon's report never dropped). The falsifier test's F1 case FAILS, because F1 is then attributed by the daemon, not the second source.
  - **M2:** the second source accepts any OOM line, not only this container's. F2 FAILS on a host where another OOM occurred in the window. This is demonstrable statically, by feeding the matcher a foreign container's line.
  - **M3:** the second source blinded permanently. F1 FAILS on the runners.
  - **M4:** the source's skip turned into a pass. Any host without the source "attributes" a dropped report, which must FAIL.
- **F4, a guard, GREEN at base:** `restart: "no"` for tooling fails the real (d-ii) test on the restart window, as at base.

## Write scope
- **FEATURE:**
  - `tests/install/test_t12a_a2_roles_restart_and_refusals_wait_image.py`: the (d-ii) test, the new falsifier test, `OOM_SCRIPT` if the source needs it, and the module docstring's (d-ii) reading;
  - `tests/install/t12a_harness.py`: new helpers only.
- **Not in scope:**
  - product code;
  - the workflows;
  - every other test.

## Runs (the arm; every Docker step under the host guard)
- **The local build:** one local `product` build of the arm's head.
- **Locally:** the real (d-ii) test three times at head, and the falsifier test once. On this Mac the second source is expected to be skipped; the arm reports what it measured.
- **The T12a module** (`-m image`): once at head.
- **The static mutants M1–M4,** where they can be shown locally, for example the M2 matcher with a foreign line.
- **The PR's CI:** both rehearsals and `images.yml` run on the change (their path filters include `tests/install/**`). The arm64 rehearsal is the run of record for the runner that missed.

## Open (for the freeze)
- None.
