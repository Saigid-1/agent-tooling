# T9 — Workspace capture continues across container restarts and directory moves

ADR: [AT-0004](../../adr/AT-0004-portable-kanban-suite.md). Header: [ARM-HEADER](ARM-HEADER.md). Dispatch: FEATURE + TEST.

## Problem

After the T7 cutover (2026-10-02), the live workspace capture stopped advancing on almost every transcript it had already started. It was measured on the live capture journal on 2026-10-02.

- **Inode renumbering.** 2,022 of the 2,063 tracked sources are refused with `source identity, project or inode changed`. `_capture` in `_impl/service/workspace_capture.py` compares the stored `(device, inode)` with the current one. Under Docker Desktop file sharing, the container sees a different inode number for the same host file whenever the container is recreated: the device is constant, the inode differs, and the content prefix is unchanged. All 54 measured main-session sources still match their recorded `prefix_sha256`. About 4 GB of visible transcript is uncaptured. Every later container recreation, such as an image upgrade, freezes every tracked source again.
- **Directory moves.** A Claude row whose `cwd` differs from the session's starting `cwd` raises `native workspace identity changed`, and capture of that source stops at that row for good. A session that changes directory, even into a subdirectory or another approved repository, loses the rest of its history. One desk's Coordinator transcript (24.5 MB) was captured only to its first 1.2 MB, so a later desk-owner claim on it attributed nothing from its last week.
- **Invisible refusals.** The identity check runs before the lease, so its refusal is never written to the source's record. The journal still shows a stale reason from 2026-09-29 (`database disk image is malformed`, 1,416 sources), and the real reason appears only in worker logs.

The content-prefix digest, re-verified before every batch, is the integrity check. The inode is only a hint and is not stable across container restarts.

## Properties and falsifiers

- **P1: continuity across file renumbering.** A tracked source continues from its cursor when all of these hold:
  - its tenant, runtime, native session identity and repository key match the record;
  - its first `cursor` bytes still hash to the recorded `prefix_sha256`.

  Its `(device, inode)` may differ. The record adopts the current `(device, inode)`; no episode is re-imported or duplicated. A source whose prefix differs, or which is shorter than its cursor, stays refused. So does a source whose tenant, runtime, native identity or repository key differs. The checks within one batch (`source changed during batch`, `source changed before cursor receipt`) are unchanged.
  Falsifier: a renumbered source with a matching prefix that is refused or re-imported (duplicate source episodes), or a source with a changed prefix that is accepted.
- **P2: a directory move does not end capture.** A Claude row whose `cwd` differs from the session's starting `cwd` is handled by project membership (the same Git common-directory rule used at discovery):
  - when the `cwd` belongs to any approved repository in the policy, the row is captured as part of the same source session;
  - otherwise (unapproved, missing or unresolvable), the row is omitted with the counted omission `cwd_outside_policy`.

  Capture continues past the row in both cases. The source's sealed project membership stays its starting repository. A row whose `sessionId` names another session, or a sidechain row whose `agentId` names another agent, is still refused. Codex `session_meta` rows follow the same rule for `cwd`; a different session `id` is still refused.
  Falsifier:
  - capture halts at a row whose `cwd` is a subdirectory or another approved repository;
  - a row from outside the approved repositories is imported;
  - an omission that is not counted;
  - a foreign `sessionId` or `agentId` accepted;
  - the source's project membership changed.
- **P3: refusals are recorded and visible.** Every refusal of a tracked source, including the identity check before the lease, is written as that source's current error. `preview` reports, for tracked sources, the current refusal reason per source and a count per reason. A batch that completes clears the error. Reasons are fixed strings: no transcript text appears in them.
  Falsifier: a refusal whose reason is missing from the record or from `preview`; a stale reason surviving a successful batch; transcript text in a reason.
- **P4: everything else is unchanged.**
  - A never-seen source captures exactly as before.
  - Sealed-row conflict detection (`sealed native source row changed`) is unchanged.
  - Lease handling and the batch bounds (`max_batch_bytes`, `max_batch_rows`, `max_candidates`) are unchanged.
  - Every existing test stays unmodified and green.

  Falsifier: a changed result for a fresh source, a sealed-row conflict accepted, a bound exceeded, or a modified existing test.

## Write scope

- **FEATURE:**
  - `packages/tooling/src/kp_agent_tooling/_impl/service/workspace_capture.py`;
  - `docs/workspace-capture.md` (continuity, directory moves, the omission and the refusal reasons).
- **TEST:** new files under `tests/`, next to `tests/test_workspace_capture_claude.py` and `tests/test_workspace_capture_adversarial.py`.
  - Drive the public surface (`kp-agent-workspace-capture … preview|once`, or the `WorkspaceCapture` service the existing tests use) with synthetic Claude and Codex transcripts and Git checkouts in temporary directories.
  - Renumber a source by rewriting it to a new inode with the same bytes plus appended rows. Change its prefix to prove the refusal still holds.
  - No network, no Docker.
