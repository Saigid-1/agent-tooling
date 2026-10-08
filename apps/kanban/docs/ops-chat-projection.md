# Chat-originated WIP projection pilot

The chat is the command surface. `task observe` records an explicit local assertion
and updates the board without starting a harness, worktree, or automatic review.
This is a local trusted-operator pilot, not an authenticated multi-tenant endpoint.

Use the physical KANBAN_STORAGE_ROOT of the running board and its existing workspace:

```sh
KANBAN_STORAGE_ROOT='/physical/state' ./node_modules/.bin/tsx src/cli.ts task observe \
  --project-path '/physical/repository' --event '/private/observation.json'
```

The strict `ops.work-observation.v1` schema is in `src/core/work-observation.ts`.
It requires workspace_id, stable work_id, sequence, previous_sha256, session_id,
source_revision (full Git hash), recorded_at (timezone timestamp), title, outcome,
stage (backlog/in_progress/review), and evidence (local path/SHA256 pairs).
The first sequence is 1 with null previous hash; later events cite the preceding
receipt's event_sha256. Reuse the same work_id across session handoffs. Work identity
is workspace-qualified. Session IDs are explicit assertions, not verified identities.

Local evidence bytes must match on first ingestion. Review requires a reference,
but reference presence does not validate its claims. Done/acceptance is unsupported.
An old replay reconstructs from the latest sealed event and retries notification;
it cannot roll the board back. Conflicting event bytes or out-of-order events refuse.
Event files are exclusively published before board mutation under the existing
workspace lock; replay repairs a failed projection. No independent scheduler or
new legacy graph/work registry is introduced. The local event spool is an adapter
receipt store; integration with authoritative host capture remains future work.

Evidence limits: this is explicit invocation, not automatic transcript extraction.
No desk is admitted. Existing host-card attribution can link the qualified card to
an independently admitted session, but that was not exercised in this pilot.
Kanban retains its ordinary edit/start/done affordances; projection cards are not
yet read-only, and it displays an anticipated worktree path even if none exists.
Do not use those affordances to dispatch projected work. Continue in the source chat.
No existing board or dispatch service is retired by this pilot.

Live 2026-09-23 trial: current Codex chat projected one work item to In Progress,
then Review with the focused-test log hash. Browser observed both notifications
without reload. Replaying event 1 after event 2 returned replay=true,
latest_sequence=2, projection_saved=false. No task session/worktree was created.
11 focused tests and root TypeScript checking passed. Two-session association was
fixture-tested; a real second-harness continuation is not yet proven.

Next acceptance: a second existing chat reads the receipts and publishes sequence 3
under the same work_id, with its actual session ID and previous event hash. Confirm
one card and both session IDs, then connect the proven exact-session capture adapter.
Add read-only projected-card presentation before unattended use. Limit retained
per-work events or add bounded compaction before high-volume production use.

## K8: automatic lifecycle pilot and observational presentation

Projected cards now disable drag, title edits, dispatch, Done and git actions;
normal clicks expand the summary, and Evidence and source discloses the complete
read-only prompt. The anticipated worktree path and unobserved running spinner
are suppressed. Runtime session startup and worktree creation refuse projected
IDs; normal persisted board writes cannot create, change, move or delete these
cards. Only observation projection may update them. Dependency changes remain
allowed. The shared dependency readiness function excludes projected dependents
from auto-start; server guards remain the backstop for other launch routes.

The opt-in OPS `scripts/capture_codex_board.py` watches one exact native Codex
session from an explicitly selected byte offset. It reads at most 4 MiB per poll
with a 1 MiB line bound, captures only task_started/task_complete/turn_aborted
metadata and source coordinates/hashes, and invokes this existing adapter.
No transcript text, model calls, global hooks, or memory admission are involved.
A native completed turn maps to Review with work outcome unverified. It never
means Done, accepted, merged, deployed or proven. The durable pending receipt
and cursor permit crash replay; a changed source boundary or binding refuses.
This is local operator trust, not authenticated multi-tenant testimony.

The current real session's turn start was captured into port 3485 automatically.
Restarting the observer projected zero duplicate events. The current turn-end
cannot be observed before this reporting turn finishes; its transformation and
restart retry are fixture-tested. Claude lifecycle integration remains unproven.

Live browser checks: projected cards, no fake worktree or edit/start/done controls,
full evidence disclosure, visual dependency arrow and reload persistence passed.
An initial narrow-card badge/title collision was found visually and repaired.
No browser error logs were observed. The pointer gesture for creating links was
not exercised: the existing CLI created the link and the browser rendered it.
No comprehensive accessibility, mobile, network-egress or performance claim.

Direct dependency evaluation found: at least one endpoint must be in Backlog;
two active nodes cannot be linked, links may be removed/reoriented by stage-based
normalization, and a three-node cycle is accepted. Adopt the visual overlay and
notifications; adapt dependency persistence/direction/cycle validation for durable
planning. Do not adopt its dependency-driven auto-start for observed work.

The original port 3484 has a user-started Codex home session and was left running.
This candidate runtime uses a separate dist-observer build and separate physical
kanban-observer-state on the external volume. No user state was copied or reset. Global dispatch,
terminal, git and task-creation controls remain for ordinary cards; only projected
cards are restricted in this slice. Full observation-only workspace mode is a
separate decision.
