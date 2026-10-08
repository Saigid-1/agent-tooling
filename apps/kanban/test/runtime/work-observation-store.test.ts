import { execFileSync } from "node:child_process";
import { mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterAll, beforeAll, expect, it } from "vitest";
import { observationHash, type WorkObservation } from "../../src/core/work-observation";
import { recordWorkObservation } from "../../src/state/work-observation-store";
import { loadWorkspaceContext } from "../../src/state/workspace-state";

let root: string, repo: string, event: WorkObservation;
const previousRoot = process.env.KANBAN_STORAGE_ROOT;
beforeAll(async () => {
	root = mkdtempSync(join(tmpdir(), "observation-store-"));
	repo = join(root, "repo");
	mkdirSync(repo);
	process.env.KANBAN_STORAGE_ROOT = root;
	execFileSync("git", ["init", repo]);
	writeFileSync(join(repo, "README.md"), "fixture\n");
	execFileSync("git", ["-C", repo, "add", "README.md"]);
	execFileSync("git", [
		"-C",
		repo,
		"-c",
		"user.name=Fixture",
		"-c",
		"user.email=fixture@example.invalid",
		"-c",
		"core.hooksPath=/dev/null",
		"commit",
		"-m",
		"fixture",
	]);
	const context = await loadWorkspaceContext(repo);
	event = {
		schema_version: "ops.work-observation.v1",
		workspace_id: context.workspaceId,
		work_id: "store-trial",
		sequence: 1,
		previous_sha256: null,
		session_id: "fixture-a",
		source_revision: execFileSync("git", ["-C", repo, "rev-parse", "HEAD"], { encoding: "utf8" }).trim(),
		recorded_at: "2026-09-23T23:00:00Z",
		title: "Fixture",
		outcome: "Test durable projection",
		stage: "in_progress",
		evidence: [],
	};
});
afterAll(() => {
	if (previousRoot === undefined) delete process.env.KANBAN_STORAGE_ROOT;
	else process.env.KANBAN_STORAGE_ROOT = previousRoot;
});
function input(name: string, e: WorkObservation) {
	const p = join(root, name);
	writeFileSync(p, JSON.stringify(e));
	return p;
}
it("concurrent replay produces one event and card", async () => {
	const path = input("one.json", event);
	const results = await Promise.all([recordWorkObservation(repo, path), recordWorkObservation(repo, path)]);
	expect(results.filter((r) => r.value.replay)).toHaveLength(1);
	expect(results[1].state.board.columns.flatMap((c) => c.cards)).toHaveLength(1);
});
it("changed sealed bytes refuse", async () => {
	await expect(recordWorkObservation(repo, input("changed.json", { ...event, title: "changed" }))).rejects.toThrow(
		/sealed/,
	);
});
it("wrong workspace refuses", async () => {
	await expect(recordWorkObservation(repo, input("wrong.json", { ...event, workspace_id: "other" }))).rejects.toThrow(
		/workspace mismatch/,
	);
});
it("invalid evidence refuses without consuming sequence", async () => {
	const e = {
		...event,
		sequence: 2,
		previous_sha256: observationHash(event),
		stage: "review" as const,
		evidence: [{ path: join(repo, "README.md"), sha256: "b".repeat(64) }],
	};
	await expect(recordWorkObservation(repo, input("bad-evidence.json", e))).rejects.toThrow(/digest/);
	const next = { ...event, sequence: 2, previous_sha256: observationHash(event), session_id: "fixture-b" };
	expect((await recordWorkObservation(repo, input("two.json", next))).value.latest_sequence).toBe(2);
	const replay = await recordWorkObservation(repo, input("one-again.json", event));
	expect(replay.value.latest_sequence).toBe(2);
	expect(replay.state.board.columns.flatMap((c) => c.cards)[0].prompt).toContain("fixture-a, fixture-b");
});
