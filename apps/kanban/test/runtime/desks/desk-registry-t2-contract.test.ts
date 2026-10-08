// T2 P5 (runtime side): the desk-registry contract defines the portable desk fields,
// and the bridge rejects fields the contract does not define before any operator
// command starts. Bridge actions are the registry CLI action names, because the
// bridge appends its action to the operator argv.
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { deskInputSchema, deskRegistrySchema } from "../../../src/core/desk-registry-contract";
import { invokeDeskRegistry } from "../../../src/server/desk-registry-bridge";

const desk = {
	desk_id: "desk:1b4e28ba-2fa1-41d2-883f-0016d3cca427",
	name: "Archive curator",
	description: "Keeps the shared archive tidy.",
	role: "Curator",
	repos: ["alpha", "beta"],
	capture: true,
	memory_write: false,
	context_doc: "Archive rules live here.",
	expected_version: 0,
};
const binding = {
	harness: "claude",
	provider: "anthropic",
	model: "fixture-model",
	native_session_id: "bound-session-1",
	desk_id: desk.desk_id,
	source: "operator",
	workspace: "/workspace/fixture",
	parent_session_id: null,
	recorded_at: "2026-09-30T12:00:00+00:00",
};
const role = { role_id: "Archivist", label: "Archivist", purpose: "Retire stale records.", expected_version: 0 };

const CHILD = String.raw`
const fs = require("node:fs");
const [log, action] = process.argv.slice(2);
let body = "";
process.stdin.on("data", (d) => (body += d));
process.stdin.on("end", () => {
	fs.appendFileSync(log, JSON.stringify({ action, body }) + "\n");
	const input = body ? JSON.parse(body) : {};
	const { expected_version, ...rest } = input;
	const recorded = { tenant_id: "tenant", version: 1, recorded_at: "2026-09-30T12:00:00+00:00" };
	const out =
		action === "save"
			? { ...rest, ...recorded, authority: "operator profile metadata; not session admission" }
			: action === "bind"
				? { schema_version: "agent-tooling.session-binding.v1", ...input }
				: action === "save-role"
					? { roles: [rest] }
					: {};
	process.stdout.write(JSON.stringify(out));
});
`;

let dir: string;
let log: string;
beforeEach(() => {
	dir = mkdtempSync(join(tmpdir(), "t2-desk-bridge-"));
	log = join(dir, "calls.jsonl");
	writeFileSync(log, "");
	const child = join(dir, "registry-child.cjs");
	writeFileSync(child, CHILD);
	vi.stubEnv("KANBAN_DESK_REGISTRY_COMMAND", JSON.stringify([process.execPath, child, log]));
});
afterEach(() => {
	vi.unstubAllEnvs();
	rmSync(dir, { recursive: true, force: true });
});
const calls = () =>
	readFileSync(log, "utf8")
		.split("\n")
		.filter(Boolean)
		.map((line) => JSON.parse(line) as { action: string; body: string });

it("defines the portable desk fields and refuses fields it does not define", () => {
	expect(deskInputSchema.parse(desk)).toEqual(desk);
	const { context_doc: _omitted, ...withoutContext } = desk;
	expect(deskInputSchema.safeParse(withoutContext).success).toBe(true);
	expect(deskInputSchema.safeParse({ ...desk, repos: [] }).success).toBe(true);
	expect(deskInputSchema.safeParse({ ...desk, repos: Array.from({ length: 32 }, (_, i) => `r${i}`) }).success).toBe(
		true,
	);
	expect(deskInputSchema.safeParse({ ...desk, repos: Array.from({ length: 33 }, (_, i) => `r${i}`) }).success).toBe(
		false,
	);
	expect(deskInputSchema.safeParse({ ...desk, context_doc: "x".repeat(16385) }).success).toBe(false);
	expect(deskInputSchema.safeParse({ ...desk, capture: "yes" }).success).toBe(false);
	// Requiredness of repos/capture/memory_write is not pinned: the legacy five-field save shape keeps its
	// behaviour (dispatcher clarification); the UI always sends them.
	for (const field of ["role", "expected_version"]) {
		const { [field]: _dropped, ...partial } = desk as Record<string, unknown>;
		expect(deskInputSchema.safeParse(partial).success, `missing ${field}`).toBe(false);
	}
	for (const extra of ["doctrine", "approval_ref", "reviewer", "repo_key", "binding_key"]) {
		expect(deskInputSchema.safeParse({ ...desk, [extra]: "x" }).success, `extra ${extra}`).toBe(false);
	}
	// The directory keeps the new fields of a listed desk and carries the configured roster.
	const { expected_version: _version, ...profile } = desk;
	const directory = deskRegistrySchema.parse({
		desks: [
			{
				...profile,
				tenant_id: "tenant",
				version: 1,
				recorded_at: "2026-09-30T12:00:00+00:00",
				authority: "operator profile metadata; not session admission",
			},
		],
		contexts: [],
		roles: [{ role_id: "Curator", label: "Curator", purpose: "Keep the archive." }],
		sessions: [],
		session_limit_reached: false,
		tenant_id: "tenant",
		authorization_changed: false,
	});
	expect(directory.desks[0]).toMatchObject({
		repos: ["alpha", "beta"],
		capture: true,
		memory_write: false,
		context_doc: "Archive rules live here.",
	});
	expect(directory.roles.map((r) => r.role_id)).toEqual(["Curator"]);
});

it("bridge passes defined requests through and rejects undefined fields before any child starts", async () => {
	for (const [action, body] of [
		["save", desk],
		["bind", binding],
		["save-role", role],
	] as const) {
		await invokeDeskRegistry(action as never, body).catch(() => undefined);
	}
	expect(calls().map((c) => [c.action, JSON.parse(c.body)])).toEqual([
		["save", desk],
		["bind", binding],
		["save-role", role],
	]);
	for (const [action, body] of [
		["save", { ...desk, doctrine: "must not be required" }],
		["save", { ...desk, approval_ref: "ruling-1" }],
		["save", { ...desk, reviewer: "someone" }],
		["bind", { ...binding, admitted: true }],
		["bind", { ...binding, approval_ref: "ruling-1" }],
		["save-role", { ...role, doctrine: "x" }],
	] as const) {
		await expect(invokeDeskRegistry(action as never, body), `${action} ${JSON.stringify(body)}`).rejects.toThrow();
	}
	expect(calls()).toHaveLength(3);
});
