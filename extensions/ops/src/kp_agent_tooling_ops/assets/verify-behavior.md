---
name: verify-behavior
description: Verify one bounded behavioral claim using exact source and compatible execution evidence across repositories.
---

## Intake reuse gate

Before admitting implementation, use code navigation to pattern-match the requested
entry/observable behavior and implementation method against existing and in-flight
work. Read candidate source, callers and contract tests at declared revisions.
Record reusable machinery, duplicate/overlap risks, searched scope and tool gaps.
Choose reuse, wire, extend, new, or unresolved in the existing work record before
editing code. Introduce machinery only when scoped evidence shows it does not exist
or existing machinery cannot support the outcome through wiring/extension. Missing
results, stale indexes and failed tools are not absence proof. Stop affected work
at unresolved overlap; continue independently admitted obligations. No extra board
service or automatic authorization is implied. The legacy source repository's full
procedure is its `docs/INTAKE-REUSE.md`; this contract also applies when the standalone
guide is used without that repository.

## Standing requirement-to-execution evidence contract

Owner: the OPS extension. Standing requirement for specification authors and all tooling users.
Applies to design, investigation, implementation and verification across repositories.
This governs evidence quality; it grants no execution or acceptance authority.

For every requirement, preserve its ID and exact accepted text. Before implementation,
record the following four fields, then update them with actual results:

1. **Entry point and intended observable behavior.** Identify the production entry
   (repository, full revision, path/symbol), relevant callers and boundary crossings,
   stimulus/preconditions, expected result and falsifying scenario. Write acceptance
   scenarios in Given/When/Then form. An unknown entry remains an explicit discovery
   question; do not substitute a nearby helper or rewrite the requirement to fit it.
2. **Execution receipts.** Link the exact test/scenario and retained output to the
   command or invocation, source revision, candidate diff/artifact hash, environment
   and dependency identities, timestamp, exit status and observed assertions. Record
   fixture ownership, contention checks when applicable, and cleanup outcomes.
   Source inspection, test authorship and a successful tool invocation are not
   execution receipts. Missing receipts mean execution is unverified; environmental
   failures are not behavioral RED. Retain only sanitized, task-relevant evidence.
3. **Proof scope.** State what each source or receipt establishes and what it excludes.
   Classify support as source-only, partial execution, full required scenario,
   contradicted, or unverified, with a reason. A component test does not establish
   end-to-end completion; a local fixture does not establish deployed behavior.
   Every independently falsifiable obligation needs its own evidence. Passing tests
   and schema validation do not themselves establish semantic correctness or acceptance.
4. **Unresolved interface contracts.** Name the missing caller/provider contract,
   signature or event/result schema, its affected requirements, owner/decision needed
   and next verification action. Label any proposed contract as unaccepted. Do not
   guess callable names, swallow failures or treat arbitrary matching output as an
   agreed interface. Continue independent work while the blocked obligation stays open.

Use a compact evidence ledger (Markdown or equivalent structured rows):

| Requirement ID / exact text | Entry and observable behavior | Test/scenario + receipt | Proof scope and exclusions | Unresolved contract / next action |
| --- | --- | --- | --- | --- |

This ledger can accompany a board card or existing verification artifact; it does not
replace registered contracts or change their validator schemas. Structural validators
check only their declared fields, not the truth of this ledger. Design-stage receipts
may be pending, but delivery claims require retained execution evidence.

Find → analyze → execute → verify: stop orientation once the entry, observable oracle
and material boundary are understood. Execute the first bounded vertical slice early
and reserve capacity for the remaining requirements, failure controls, cleanup and
final evidence assessment. Use navigation, source, compiler and runtime tools where
needed; neither using all tools nor minimizing call count proves coverage. If a budget
ends, retain the precise incomplete rows rather than narrowing the accepted requirement.

Fixture cleanup must follow exact run ownership. A shared name prefix alone does not
establish that a resource is abandoned or safe to delete. Crash recovery must establish
ownership and liveness before reclaiming leftovers.

## Find the evidence needed for the question

Work in small behavioral slices: entry → relevant binding → observable result →
cleanup. Preserve guards, exceptions, dispatch ambiguity and adjacent obligations.
Separate baseline source, generated candidate and deployed runtime. Static navigation
finds possible paths; assertions and traces demonstrate only their tested scope.
Unobserved does not mean dead or absent.

Use verification.plan with relevant questions (baseline, lifecycle, performance,
deployment). It supplies configured source pins, exact typed references, bounded
next_calls and two recovery calls of reserve. It is an authored plan for the configured
product repositories, not an arbitrary repository discovery engine. Valid supplied references can avoid repeat
reads; this declaration means you already received and reviewed complete evidence.
A successful identity check alone does not establish receipt, comprehension or truth.
Use knowledge.platform when platform identity is unresolved; do not repeat equivalent
orientation of the product repositories when the same pinned snapshot already supplies both.

Existing packet slices: ats-pool-lifecycle, ats-worker-performance, ats-pool-failures,
ats-auth-acquisition, ats-response-correctness, ats-constrained-pool. Plan metadata
exposes typed adjacent obligations. Follow only questions material to this review;
not every tool or neighboring slice is required. Preserve explicit continuations for
unread obligations. Graph/question distance is not runtime call depth.

For baseline pooling, inspect entry/import plus Core get_tenant_db and _connect.
An imported pooling helper does not prove invocation. For the worker candidate, inspect
transformation, injected acquisition/release bindings, assignment, candidate source,
and compatible execution receipts. Keep product and evidence-repository revisions distinct.
For optimization, read the performance comparison; one successful request cannot prove
a speedup. Equal-pool async/thread comparisons already use pooling in both arms.

## Read complete evidence efficiently

Use the smallest sufficient path. Reuse an adequate preserved Serena invocation rather
than repeating live inspection. Registry list metadata gives exact original_read_bytes
and next_call. Copy its budget; do not guess 4,000 bytes for an 11,000-byte original.
Observation read takes one id. Assess takes ids and expected_scope (sources and
binding_sha256); mode is named mode, not op. Never alter a binding to force a join.

Packet reads default to 12,000 serialized ASCII JSON bytes, not tokens. Multi-item
reads are capped there. Plans give identity-pinned read_groups; select only needed
items and emit reads separately. Oversized indivisible items require the returned
explicit single-item call, up to 100,000 bytes. Do not repeat the same oversized request.
Changed packet identity invalidates a combination. Preserve complete source/trace items,
not selective snippets which discard cleanup or guards.

OPS-owned transports expose decoded structuredContent rather than nested escaped JSON.
Prefer structuredContent; otherwise decode the single JSON text result once. Never dump
unrelated tool catalogs or combine large result envelopes. Individual limits cannot
constrain an arbitrary host's aggregation. Check actual delivered output for truncation.

A continued delivery is not evidence yet. Follow delivery.read text fragments in offset
order; concatenate them, verify ASCII byte count and sha256, then parse the complete JSON.
Keep fragments separate in transport; do not claim completion from a final page alone.
Use native JavaScript orchestration for exact reassembly if available. No shell fallback
is required. If unsupported or out of budget, retain the delivery gap. A successful tool
call does not prove the model received the entire body.

Track delivered bytes, duplicated reads and remaining context. Keep evidence comfortably
below 100k tokens, leaving thinking space. Reserve recovery capacity before additional
orientation. Tool counts are telemetry, not a substitute for coverage. When a declared
budget is exhausted, keep the precise gap rather than inventing a conclusion.

## Use original, typed references

v5 finding evidence entries contain reference, establishes and limitation. Copy returned
references. Packet references identify kind=packet, slice_id, id and packet_identity.
Observation references identify kind=observation and immutable id. Lifecycle references
identify kind=lifecycle, allowlisted evidence_id, revision and blob_sha. Source references
identify kind=source, configured repo_key, relative path, revision and blob_sha.
Do not turn lifecycle.evidence or serena.inspect into invented packet slice IDs.
A live Serena source reference proves the pinned source only; its invocation receipt is
a different fact. Preserved Serena originals use observation references.

Declare delivery.complete as typed references; gaps contain reference, reason and
recovered. Do not declare metadata, fragments, split_required or truncated bodies complete.
Unresolved questions include classification, basis, references and next_action:
- registered_but_unread: registered referenced evidence remains unread.
- reviewed_with_limitations: cite complete reviewed evidence and its remaining boundary.
- observation_required: reviewed evidence explicitly establishes an excluded scenario.
- unverified: available evidence cannot establish a stronger classification.
An unread neighboring slice or empty search does not prove new observations are needed.

## Separate claims, identities and compatibility

One artifact declares one subject, target (baseline/candidate/deployment), predicate
and scalar expected_value, with its own supported/contradicted/unverified outcome.
Do not hide independent propositions in a predicate string. A source-only baseline
finding uses scope.execution_evidence=null. Findings relying on candidate/runtime evidence
use execution_revision, candidate_sha256 and profile inside scope.execution_evidence.
These identify compared evidence, not deployment of the claim subject. Never borrow
execution identity merely to satisfy validation. Keep sources as full revision pins.

When citing observations, declare observation_assessment with exact ids, expected_scope,
declared_status and use. Null means no observations are used. The validator recomputes
compatibility from immutable originals. joint_support requires matching sources/subjects
and compatible records. separate_context preserves different boundaries without claiming
a shared execution path. Singleton compatibility proves no cross-record join. Conflicts
and incompatible bindings remain explicit. Compatibility never proves causal linkage,
behavioral correctness, completeness, authorization or production identity.

## Verify the exact handoff

Read verification.finding(mode=schema), then validate every exact v5 artifact. Structural
validation checks identity/reference consistency; semantic_verdict remains not-assessed.
Fix errors from actual evidence, not relabeling or invented metadata. Revalidate edits;
expected_finding_sha256 detects an unintended change. Never bypass a tool refusal.

Prepare final prose plus complete JSON artifacts. Include every artifact's validation
status and digest. Call verification.handoff with that exact final_text and expected
validation digests before publishing it unchanged. The adapter must retain those receipts;
a restarted process requires revalidation. This checks the supplied text, not subsequent
host publication or arbitrary prose semantics. The legacy OpenRouter harness applies the
same final check automatically. Invalid artifacts and missing JSON are not a successful
handoff. Do not say “not ready” when the verdict is merely readiness unverified.

Report demonstrated behavior, exclusions, adjacent unread questions, next acceptance
scenarios, underlying calls, recovery, actual timing/token measurements (or unavailable).
Controlled failures are not real database cleanup failures. Retained workload performance
is not production sizing, cancellation, multiworker saturation or deployment evidence.
Revalidate when sources, bindings, candidate, scenario, instrumentation or runtime profile
change. Evidence and findings do not grant memory promotion or requester acceptance.

## Current development scope

If tooling.identity exposes an active_navigation_profile, use its exact revisions
for current-source claims. navigation.workspace shows live worktree heads and
potential file overlaps; navigation.source reads a selected full commit. Overlap is
not proof of conflict. Retained packets/observations may use older pins. For an
intentional historical finding, set source_profile to retained-evidence; otherwise
validation uses the active source profile. Revalidate after validation_context_changed.


### Frozen reviews and requirement fidelity

For a multi-step development review, capture `navigation.snapshot` once and carry
its `snapshot_id` and exact source revisions through semantic, source and compiler
reads. Compare live defaults/workspaces separately; do not silently repin the review.
Use `navigation.paths` for unknown filenames and `navigation.search` for a bounded
text question before semantic lookup. These read committed artifacts, not dirty files.
Use `serena.inspect` options to request only needed body/references/import context.
An empty reference result must be interpreted with its diagnostic and coverage state.
Compact citation continuations still require source reads before behavioral claims.

For a spec with named obligations, read `verification.review` with `mode: "schema"`.
When an operator-registered contract exists, read it, preserve every ID and exact
requirement text, and link each row to inspected evidence, an oracle and a decision.
Validate the ledger before handoff. Missing/renamed requirements or unapproved
normalization rules are structural failures; validation does not establish entailment
or human acceptance. If no contract is registered, report that gap and retain the
requirement table; do not invent an approval or substitute an unrelated journey.
