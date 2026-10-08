# Arm header — applies to every order in this directory

An order is frozen at the merge commit that introduced it. That commit is the order's identity. The dispatcher (the Coordinator) states, in each arm's launch message:

- the order path;
- that merge SHA;
- the arm (FEATURE, TEST, or SINGLE);
- the workspace path;
- the interpreter.

## Rules for every arm

1. **Identity and STOP.** Work only in the workspace you were given. It is a Git worktree of `Saigid-1/agent-tooling` checked out at the order's merge SHA, on your own branch. If `git rev-parse HEAD` differs from the stated SHA at start, STOP and report. Never check out, reset or modify any other worktree or checkout, including the main `agent-tooling` checkout, which running containers mount.
2. **Commit shape.** Commit locally on your branch. Never push, never open a PR, never merge. The Coordinator pushes after review.
3. **Blindness.** FEATURE and TEST arms of the same order do not read each other's branches or workspaces. The TEST arm tests the order's contract, never an implementation.
   - **Private scratch.** Each arm works in its own scratch directory, the TMPDIR the dispatch names (one per slice and role, never a shared parent with another arm's). It does not open, list or grep any other scratch directory, including the dispatching session's own. Its report answers whether it read anything it did not write, and the meet packet carries that answer and a listing of the arm's scratch directory.
   - **Lapse, disclosed 2026-10-04.** Until then, every arm dispatched from one Coordinator session received that session's shared scratch directory, so FEATURE and TEST could read each other's working files there. It was found when the T11b TEST arm disclosed it.
     - Affected: T9b J1, T10h, T11a, and the T11b and T12a arms until the fix above.
     - The method claim for those meets: arms blind to each other's code; TEST could read FEATURE's instrument scripts in the shared directory. No arm reports having done so. That is a self-report record, not a measurement.
     - A second class: unchecked writes. Same-named files in the shared directory may have replaced each other unread.
     - The record is kept off-tree by the Coordinator, with the six reachable arms' transcripts and their checksums. AT-0006, in the release document, carries the method-page entry.
     - The Principal's ruling, 2026-10-04, relayed by Verification: the method page discloses the lapse, and the finding stands. A retroactive comparison of the lapse-period meets with the truly blind ones is post-T12 work (RETRO).
4. **RED first.** Before the fix, show the failing scenario, or show your tests failing against a stub of the contract. Record the exact command, exit code and summary line. Both are required: a summary line is not an exit code.
5. **TEST arm pair (§10 rider 3).**
   - Each delivered test fails against a null stub and passes against a minimal stub you write to satisfy the stated GREEN-IF. Delete both stubs before committing.
   - For each test, name a stub that turns it green while leaving another test in the set red. If you can't, report the test as redundant.
6. **Escalate, never resolve.** If the order is ambiguous or contradicts the code, don't pick an interpretation silently. Choose the most conservative reading and list it under AMBIGUITY in your report.
7. **Do not modify** anything outside the order's declared write scope. In particular, never modify files that other orders own, live runtime directories, host settings, `~/.claude*`, `~/.codex`, or anything under the live runtime root (the running `agent-tooling-runtime` deployment the dispatcher names). Start no long-lived services outside your own test fixtures. Stop any container you start.
8. **Environment.**
   - Python: the interpreter the dispatcher names. It is Python 3.12 with the package installed editable from your own workspace; confirm with `python -c "import kp_agent_tooling;print(kp_agent_tooling.__file__)"`.
   - Temporary files: under the `TMPDIR` the dispatcher names, on the external volume.
   - Node 22+ for Kanban.
   - Docker is available. Name every image, container, volume, network and Compose project you create with the prefix your dispatch names (for example `t12a-`). The repo's image suites create and remove their own fixtures (`agent-tooling-image-test-*`, `t7a-*`, `t7b-*`, `t9b-*`, …), which is allowed. Remove intermediate containers.
9. **Honesty.** Report what you ran and what you did not. "Tests pass" requires the command, the exit code and the summary line. Unverified claims are labeled unverified.

## Report format (your final message)

- `WORKSPACE`, `BRANCH`, `HEAD` (full SHA), `BASE` (the order's merge SHA)
- `FILES` changed, one line each, with the reason
- `RED`: command, exit code, summary, before the change
- `GREEN`: command, exit code, summary, after the change
- `PAIR` (TEST arm): the null stub result, the green-if stub result, the separability table
- `AMBIGUITY`: each ambiguity and the reading you chose
- `NOT DONE / NOT VERIFIED`
