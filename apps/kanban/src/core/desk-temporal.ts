/** Historical attribution only. No seat admission, timestamp inference or writes. */
import { z } from "zod";
const text = z.string().trim().min(1).max(4096);
const time = z.string().datetime({ offset: true });
const claim = z
	.object({
		schema_version: z.literal("ops.session-claim.v1"),
		claim_id: text,
		session_id: text,
		predicate: text,
		object: z.object({ kind: text, id: text }).strict(),
		asserted_by: text,
		recorded_at: time,
		evidence: z.array(text).min(1).max(32),
		valid_from: time.nullable(),
		valid_until: time.nullable(),
		supersedes: text.nullable(),
		retracted: z.boolean(),
	})
	.strict();
const source = z
	.object({
		schema_version: z.literal("ops.source-session.v1"),
		tenant_id: text,
		runtime: text,
		native_id: text,
		session_id: text,
		claims: z.array(claim).max(1000),
		// These cached values are deliberately not trusted for derivation.
		active_claims: z.array(claim),
		desk_bindings: z.array(text),
		attribution_status: text,
		authority: text,
	})
	.strict();
export function projectDeskTemporal(input: unknown, tenant: string, session: string) {
	const data = source.parse(input);
	if (data.tenant_id !== tenant || data.session_id !== session) throw new Error("Desk history scope mismatch");
	const byId = new Map(data.claims.map((c) => [c.claim_id, c]));
	if (byId.size !== data.claims.length) throw new Error("Duplicate claim identity");
	const replaced = new Set<string>();
	for (const c of data.claims) {
		if (c.session_id !== session) throw new Error("Claim session mismatch");
		if (c.valid_from && c.valid_until && Date.parse(c.valid_from) >= Date.parse(c.valid_until))
			throw new Error("Empty or reversed claim interval");
		if (c.retracted && !c.supersedes) throw new Error("Retraction requires predecessor");
		if (c.supersedes) {
			const prior = byId.get(c.supersedes);
			if (!prior || prior.predicate !== c.predicate || Date.parse(prior.recorded_at) > Date.parse(c.recorded_at))
				throw new Error("Invalid claim predecessor");
			if (replaced.has(c.supersedes)) throw new Error("Forked claim history");
			if (c.retracted && (prior.object.id !== c.object.id || prior.object.kind !== c.object.kind))
				throw new Error("Retraction target mismatch");
			replaced.add(c.supersedes);
		}
		const seen = new Set<string>();
		let cursor: typeof c | undefined = c;
		while (cursor) {
			if (seen.has(cursor.claim_id)) throw new Error("Cyclic claim history");
			seen.add(cursor.claim_id);
			cursor = cursor.supersedes ? byId.get(cursor.supersedes) : undefined;
		}
	}
	const rows = data.claims
		.filter((c) => c.predicate === "session.owner" && c.object.kind === "desk")
		.map((c) => ({
			claim_id: c.claim_id,
			desk_id: c.object.id,
			asserted_by: c.asserted_by,
			recorded_at: c.recorded_at,
			from: c.valid_from,
			until: c.valid_until,
			evidence: c.evidence,
			assertion_state: replaced.has(c.claim_id) ? "superseded" : c.retracted ? "retracted" : "current_assertion",
			boundary_status: c.valid_from && c.valid_until ? "bounded" : "incomplete",
		}));
	const active = rows.filter((r) => r.assertion_state === "current_assertion");
	const conflicts: string[][] = [];
	for (let i = 0; i < active.length; i++)
		for (let j = i + 1; j < active.length; j++) {
			const a = active[i],
				b = active[j];
			if (a.desk_id === b.desk_id) continue;
			if (
				(a.until && b.from && Date.parse(a.until) <= Date.parse(b.from)) ||
				(b.until && a.from && Date.parse(b.until) <= Date.parse(a.from))
			)
				continue;
			conflicts.push([a.claim_id, b.claim_id]);
		}
	return {
		schema_version: "ops.desk-temporal-projection.v1.1",
		tenant_id: tenant,
		session_id: session,
		directory: [...new Set(active.map((r) => r.desk_id))].map((desk_id) => ({
			desk_id,
			sessions: [
				{
					session_id: session,
					native_id: data.native_id,
					runtime: data.runtime,
					reachability: "not_checked",
					contact_route: null,
				},
			],
			concurrency: "allowed",
		})),
		conflict_scope: "contradictory ownership claims for this same session only",
		rows,
		conflicts,
		status: conflicts.length ? "potential_conflict" : rows.length ? "historical_attribution" : "unresolved",
		authorization: "not_assessed",
		evidence_verification: "references_not_resolved",
		boundary:
			"Registered claim export; no proof of occupancy, authenticated identity, current admission or work between observations. Unknown bounds stay unknown.",
	};
}
