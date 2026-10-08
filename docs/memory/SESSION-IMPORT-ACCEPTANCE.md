# Session import and attribution acceptance

Requested outcome: a new user can preview and import a selected native
session, choose its desk attribution, and explicitly select full history or the
current turn and subsequent visible messages. This delivery introduces the
streaming job and Cline dialog for Codex; the existing bounded Claude importer
remains available, but Claude streaming/follow is not claimed. Codex compaction must not restrict
persistent memory to the active context window. Attribution remains a claim;
it never grants session admission or establishes the truth of transcript text.

## Observable scenarios

1. A native Codex file larger than the former 8 MB limit imports in bounded,
   resumable batches, preserving original native session identity, byte offsets,
   source digests and earlier visible messages across compaction boundaries.
2. Preview performs no memory writes. Apply accepts only the reviewed source,
   scope, desk and boundary. A changed prefix/rotated source or mismatched native
   session refuses continuation explicitly; it cannot silently restart elsewhere.
3. Current-turn mode derives a verifiable native turn/user boundary, shows it in
   preview, excludes earlier events, and follows only that exact session.
   Unsupported/ambiguous boundaries are named. Full mode is never a fallback.
4. Persistent job state distinguishes source coverage, index coverage, omitted
   control rows, partial last rows, budget limits, failures and future capture.
   Stop/resume is visible; reaching today's EOF is not final completeness of a
   still-running session. Hidden reasoning is not imported as visible memory.
5. Cline provides a discoverable import dialog and a local slash command. The
   slash command opens the dialog rather than being sent to an agent. Missing
   host configuration and unregistered desks have actionable setup guidance.
6. Ownership, contributions and role assignments are separate, append-only
   claims. The observer, actual native actor and source timestamps remain
   explicit. Parent assertions do not masquerade as authenticated host roles.
7. Delegated Sol sessions are attributed only when native metadata establishes
   their exact child identity and parent link. A changed assignment is dated now;
   a whole-session role claim is not evidence that earlier work had that role.
   Use a verified source range when filtering episodes by assignment. No transcript
   or role claim may admit a session or acquire another desk's write authority.
8. Existing imported data, same-seat concurrent coordinators, CLI/MCP source
   reads, Cline session execution and migration/cutover behavior remain intact.

## Initial evidence

A Coordinator's native session has a >43 MB
rollout with 11 recorded compactions and visible messages preceding them. The
existing importer refuses it at an 8,000,000-byte file bound. Two explicitly
labeled checkpoint/delivery-record episodes are indexed; no complete transcript
backfill has yet been claimed. This work is isolated from deployment and the
other session's memory migration and host-registration cutover.

## Operator trial result

The reviewed native snapshot completed at byte 46,167,069. The job captured and
incrementally indexed 537 source episodes containing 541 visible event segments.
It observed 12 compaction markers and explicitly omitted 11 oversized control
rows; there was no partial trailing row at the reviewed boundary. A later-growing
source is not represented as continuously captured by this full-snapshot job.

An explicit owner assertion linked the native session to Agent Tooling
Coordinator. Desk-scoped literal search verified 540/540 indexed session episodes
(the 537 new source records plus three preserved legacy records), including
source matches before the first compaction. Private receipts remain outside Git.

The delegate trial recorded two implementer role claims and one analyst role
claim with verified native parent/child relationships. These are dated claims
on reused sessions, not newly admitted desks or proof that all earlier work had
the same role. The trial did not import those children's complete transcripts.

## Final validation on integrated main

Rebased onto agent-tooling main before the final gate.

- Python: 381 passed, one pre-existing legacy skip.
- Web UI: 500 passed; final modal test also rerun after diagnostic changes.
- Kanban server/web typechecks and production build passed.
- Final runtime/utilities gate: 618 passed, two failures. Both failures are the
  unchanged agent-registry expectations for five launchable agents versus the
  existing catalog's two. Reproduced both on an untouched archive of main;
  neither the catalog nor those tests were changed by this feature.
- Real Python CLI through the host bridge and integrated public-origin tests:
  eight passed. Set `TEST_IMPORT_PYTHON` to an installed tooling interpreter to
  run the cross-runtime fixture; Node-only environments explicitly skip it.
- Synthetic browser review verified the registered desk selector, snapshot
  scope, first-batch counts, scrolling preview, and disabled apply before consent.
- No shared deployment, global capture hook, MCP registration or cutover was
  performed. The live operator import above is a completed snapshot, not an
  installed standing follower.


## Independent integration review

Two blocking defects were reproduced and repaired before merge: inactive job
responses carry a null heartbeat (the board schema had rejected apply/status),
and reviewed but not-yet-captured source bytes could change after apply without
refusal. The real bridge test now runs preview, apply, capture and status; the
source regression requires refusal with no imported records. Reviewed snapshot
integrity is checked on every continuation in addition to the captured prefix.

Container integration was a separate, explicit gap at review time: the Node-only
board image did not contain the import CLI used by this host bridge. See
[the deployment assessment](../COMPOSABLE-DEPLOYMENT.md). The board image has since
gained the portable memory CLI ([CONTAINER-IMPORT.md](../CONTAINER-IMPORT.md)).
No image acceptance or live cutover is implied by this source integration.

Reviewer rerun: 374 Python tests passed, one legacy skip; eight real-bridge/origin
tests and the dialog test passed; server and web typechecks passed. These are
the review checkout's measured totals, separate from the author's earlier report.
The two agent-registry expectation failures were reproduced; their test and
implementation files are unchanged from main. They still require catalog/test
reconciliation and are not counted as passing.
