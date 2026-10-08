import { describe, it, expect } from "vitest";
import { projectDeskTemporal } from "../../src/core/desk-temporal";
const row = (id = "a", desk = "coordinator") => ({
	schema_version: "ops.session-claim.v1",
	claim_id: id,
	session_id: "s",
	predicate: "session.owner",
	object: { kind: "desk", id: desk },
	asserted_by: "fixture",
	recorded_at: "2026-09-23T10:00:00Z",
	evidence: ["fixture:note"],
	valid_from: null as string | null,
	valid_until: null as string | null,
	supersedes: null as string | null,
	retracted: false,
});
const input = (claims = [row()]) => ({
	schema_version: "ops.source-session.v1",
	tenant_id: "t",
	runtime: "fixture",
	native_id: "n",
	session_id: "s",
	claims,
	active_claims: [],
	desk_bindings: [],
	attribution_status: "untrusted-cache",
	authority: "historical",
});
describe("historical desk projection", () => {
	it("exposes desk session identities without fabricating contact routes", () => {
		const p = projectDeskTemporal(input(), "t", "s");
		expect(p.directory[0].concurrency).toBe("allowed");
		expect(p.directory[0].sessions[0].native_id).toBe("n");
		expect(p.directory[0].sessions[0].contact_route).toBeNull();
	});

	it("does not turn recording time into a boundary or grant", () => {
		const p = projectDeskTemporal(input(), "t", "s");
		expect(p.rows[0].from).toBeNull();
		expect(p.rows[0].until).toBeNull();
		expect(p.authorization).toBe("not_assessed");
	});
	it("retains an end-only retirement interval", () => {
		const c = row();
		c.valid_until = "2026-09-23T09:00:00Z";
		expect(projectDeskTemporal(input([c]), "t", "s").rows[0].boundary_status).toBe("incomplete");
	});
	it("preserves gap between bounded claims", () => {
		const a = row(),
			b = row("b", "reviewer");
		a.valid_from = "2026-09-23T01:00:00Z";
		a.valid_until = "2026-09-23T02:00:00Z";
		b.valid_from = "2026-09-23T04:00:00Z";
		b.valid_until = "2026-09-23T05:00:00Z";
		const p = projectDeskTemporal(input([a, b]), "t", "s");
		expect(p.rows).toHaveLength(2);
		expect(p.conflicts).toEqual([]);
	});
	it("marks unknown overlapping competing owners", () =>
		expect(projectDeskTemporal(input([row(), row("b", "reviewer")]), "t", "s").status).toBe("potential_conflict"));
	it("retains superseded history and retraction", () => {
		const a = row(),
			b = row("b");
		b.supersedes = "a";
		b.retracted = true;
		const p = projectDeskTemporal(input([a, b]), "t", "s");
		expect(p.rows.map((r) => r.assertion_state)).toEqual(["superseded", "retracted"]);
	});
	it("refuses wrong tenant/session", () => {
		expect(() => projectDeskTemporal(input(), "other", "s")).toThrow();
		expect(() => projectDeskTemporal(input(), "t", "other")).toThrow();
	});
	it("refuses cycles and forked predecessors", () => {
		const a = row(),
			b = row("b");
		a.supersedes = "b";
		b.supersedes = "a";
		expect(() => projectDeskTemporal(input([a, b]), "t", "s")).toThrow();
		a.supersedes = null;
		const c = row("c");
		c.supersedes = "a";
		expect(() => projectDeskTemporal(input([a, b, c]), "t", "s")).toThrow();
	});
	it("refuses reversed intervals and duplicate identities", () => {
		const a = row();
		a.valid_from = "2026-09-23T04:00:00Z";
		a.valid_until = "2026-09-23T03:00:00Z";
		expect(() => projectDeskTemporal(input([a]), "t", "s")).toThrow();
		expect(() => projectDeskTemporal(input([row(), row()]), "t", "s")).toThrow();
	});
	it("does not mutate input or use cached attribution", () => {
		const data = input();
		const before = JSON.stringify(data);
		expect(projectDeskTemporal(data, "t", "s").rows).toHaveLength(1);
		expect(JSON.stringify(data)).toBe(before);
	});
});
