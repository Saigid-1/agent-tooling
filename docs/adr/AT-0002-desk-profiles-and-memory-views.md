# AT-0002 — Desk profiles and transcript memory views

Declared status: Deployed. Derived status: Deployed.

Built and merged. Deployed: a local Docker and browser receipt, with environment and image digest, is kept with the operator's records (not published). The host-emitted automatic hook requirement remains unverified, so this does not claim the whole journey Proven.

## Accepted direction

The Principal requested: “a menu screen where I can configure desks” and “all transcripts should be captured” with “desk/role/session, ADR/Cards associated, repo(s), and account” plus model/provider metadata. The same request proposed desk, repository and global views. Acceptance is of this direction; deployment and proof require receipts.

## Decision

A desk has a stable ID, name, description and role template. It requires no repository. Operator-authored session annotations associate captured source sessions with a desk, zero or more repositories, ADRs, cards, account reference, provider and model. Unknown values remain null or unassigned. Provider/model metadata records provenance, not ownership. One session may cover several repositories; multiple sessions may occupy a desk concurrently.

Reuse the portable SQLite episode/source-session store and existing admission checks. Append hashed, versioned profiles and context annotations; maintain relational repository rows as a verified projection. Search intersects the selected view with the existing tenant-scoped source selection. Global means the admitted workspace tenant, never every account on the host. An annotation does not grant admission, change source ownership, or make previously uncitable sources writable.

The local operator UI uses a fixed server-configured command to the registry CLI. A browser cannot choose the tenant, configuration file or executable. Desk creation asks one concern per screen: name, description, role, review. A separate dialogue records session context. It does not launch sessions.

## Capture

The scoped capture desk uses the existing exact host/native identity mapping, lifecycle hooks and index refresh. Visible large messages are split into bounded, reconstructable events. Raw host transcripts remain the source; control and unsupported events retain explicit omission accounting. Capture is bounded and reports remaining bytes; no receipt means no claim of complete capture. General enrollment of unassigned host sessions remains separate from this scoped enablement.

## Validation boundary

Tests cover repository-independent creation, version conflicts, tenant isolation, metadata tampering, memory view selection, large visible rows, UI progression and bridge request bounds. Real hook emission after host restart is distinct from an operator replay of the same hook payload. No synthetic fixture establishes that the host emitted an event.

## Accepted capture-scope extension — 2026-09-29

The Principal ruled: “yes agreed on option 2 - chats tied to a specific project or workspace where memory is relevant to the work being delivered is the scope that matters.” For the initial set: “All repositories already registered in the desk catalog”.

A catalog-pinned decision record, kept with the operator's records (not published), lists the selected repositories. Sessions in those projects are eligible for local source capture whether or not they have a desk. Unassigned sessions remain unresolved; capture grants no seat, desk writes or outbound summarization. Repository membership must come from verified harness/workspace evidence, not transcript prose or directory-name similarity. Missing or ambiguous membership is a visible coverage gap, not permission to sweep the host. Later desk attribution preserves the original source and attribution history. Global remains within the selected tenant.

This extension is **Accepted**, not Deployed: existing exact-session hooks and manual imports are not a cross-project discovery worker. Reuse the native import converters, resumable cursor/index receipts and session catalog when wiring discovery. In particular, the current resumable job is Codex-only and requires a selected desk; these constraints must be addressed without manufacturing ownership for unassigned sessions. The existing whole-file native import also has an eight-megabyte bound, so it cannot silently stand in for the streaming capture path.

Acceptance requires included assigned and unassigned sessions, excluded unrelated workspaces, verified worktree/repository membership, replay/append recovery without duplicate source records, explicit capture/index coverage, and no changes to desk admission or outbound provider permissions. Approval of this extension does not advance its implementation status.
