import { execFileSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import { mkdirSync, mkdtempSync, readFileSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { afterAll, beforeAll, expect, it, vi } from "vitest";
import { intakeDigest, intakeWorkId, parseAdrIntake } from "../../src/core/adr-intake";
import { runtimeBoardCardSchema } from "../../src/core/api-contract";
import { requireDispatchableTask } from "../../src/core/projection-policy";
import { moveTaskToColumn, trashTaskAndGetReadyLinkedTaskIds } from "../../src/core/task-board-mutations";
import {
	type AdrEvidenceInput,
	readAdrEvidence,
	recordAdrEvidence as recordOriginal,
} from "../../src/state/adr-evidence-store";
import { listAdrIntakes, reconcileAdrIntake as reconcileOriginal } from "../../src/state/adr-intake-store";
import { ingestWorkObservation, recordWorkObservation } from "../../src/state/work-observation-store";
import { loadWorkspaceContext, mutateWorkspaceState } from "../../src/state/workspace-state";

const source = {
	repository: "example/repo",
	path: "docs/ADR-1.md",
	revision: "a".repeat(40),
	sha256: "b".repeat(64),
	origin: "committed",
};
function fixture(workId = "slice-1") {
	return {
		schema_version: "ops.adr-backlog-intake.v1.2",
		work_id: workId,
		initiative_id: "initiative",
		slice_id: workId,
		title: "Reviewed slice",
		owner: { repository: "example/repo", role: "maintainer" },
		adr: {
			id: "ADR-1",
			source,
			declared_status: "Accepted",
			derived_status: null,
			verification: "unchecked",
			reason: "No checker receipt",
			citations: [
				{
					locator: "PR#1",
					kind: "pull_request",
					target: { type: "pull_request", repository: "example/repo", identifier: "#1", resolved_source: null },
					verification: "unchecked",
					reason_code: "offline",
					reason: "Not fetched",
				},
			],
			derivation: null,
			declared_status_text: "Accepted",
			status_scope: null,
		},
		entry_point: "CLI",
		requester_outcome: "A durable card",
		// biome-ignore lint/suspicious/noThenProperty: Approved schema outcome field.
		acceptance_scenarios: [{ given: "intake", when: "replayed", then: "one card" }],
		reuse_assessment: { status: "pending", references: [source], reuse_targets: [], remaining_gap: "Review pending" },
		dependencies: [] as { work_id: string; relationship: string; basis: string; reason: string }[],
		unresolved: ["No runtime proof"],
		dispatch: { state: "not_authorized" },
		predictions: [],
		prediction_gap: "Not yet registered",
	};
}
let root: string, repo: string, executionBase: string;
function recordAdrEvidence(
	cwd: string,
	taskId: string,
	input: Omit<AdrEvidenceInput, "eventId" | "actor"> & Partial<Pick<AdrEvidenceInput, "eventId" | "actor">>,
) {
	return recordOriginal(cwd, taskId, {
		eventId: randomUUID(),
		actor: { kind: "agent_assertion", id: "fixture-agent" },
		...input,
	});
}
function reconcileAdrIntake(cwd: string, path: string, options: { supersedes?: string; baseRef?: string } = {}) {
	return reconcileOriginal(cwd, path, { baseRef: executionBase, ...options });
}
const previousRoot = process.env.KANBAN_STORAGE_ROOT;
beforeAll(async () => {
	root = mkdtempSync(join(tmpdir(), "adr-intake-"));
	repo = join(root, "repo");
	mkdirSync(repo);
	process.env.KANBAN_STORAGE_ROOT = root;
	execFileSync("git", ["init", repo]);
	writeFileSync(join(repo, "README.md"), "fixture");
	execFileSync("git", ["-C", repo, "add", "."]);
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
	executionBase = execFileSync("git", ["-C", repo, "rev-parse", "HEAD"], { encoding: "utf8" }).trim();
	await loadWorkspaceContext(repo);
});
afterAll(() => {
	if (previousRoot === undefined) delete process.env.KANBAN_STORAGE_ROOT;
	else process.env.KANBAN_STORAGE_ROOT = previousRoot;
});
function input(value: unknown, name = "input"): string {
	const path = join(root, `${name}.json`);
	writeFileSync(path, JSON.stringify(value, null, 2));
	return path;
}
it("validates exact schema constraints including nested citations and conditionals", () => {
	const valid = fixture();
	expect(parseAdrIntake(Buffer.from(JSON.stringify(valid)))).toEqual(valid);
	for (const invalid of [
		{ ...valid, extra: true },
		{ ...valid, dispatch: { state: "authorized" } },
		{ ...valid, prediction_gap: null },
		{ ...valid, adr: { ...valid.adr, derived_status: "Proven" } },
		{ ...valid, adr: { ...valid.adr, citations: [{ ...valid.adr.citations[0], extra: true }] } },
	])
		expect(() => parseAdrIntake(Buffer.from(JSON.stringify(invalid)))).toThrow(/Invalid ADR intake/);
	expect(() => parseAdrIntake(Buffer.from([0xff]))).toThrow();
});
it("retains original bytes and citations, concurrent replay creates one manually dispatchable card", async () => {
	const path = input(fixture());
	const results = await Promise.all([reconcileAdrIntake(repo, path), reconcileAdrIntake(repo, path)]);
	expect(results.filter((r) => r.value.replay)).toHaveLength(1);
	const result = results[1];
	expect(result.state.board.columns.flatMap((c) => c.cards)).toHaveLength(1);
	const card = result.state.board.columns[0].cards[0];
	expect(card.prompt).toContain("No runtime proof");
	expect(card.prompt).toContain("acceptance_scenarios");
	expect(card.prompt).toContain("reuse_assessment");
	expect(card.baseRef).toBe(executionBase);
	expect(card.baseRef).not.toBe(source.revision);
	expect(card.adrOrigin?.dispatchPolicy).toBe("manual_only");
	for (const invalid of [{ intakeSha256: "bad" }, { historyCardId: "../escape" }, { extra: true }])
		expect(runtimeBoardCardSchema.safeParse({ ...card, adrOrigin: { ...card.adrOrigin, ...invalid } }).success).toBe(
			false,
		);
	expect(() => requireDispatchableTask(card.id)).not.toThrow();
	expect(() => requireDispatchableTask(result.value.historical_card_id)).toThrow();
	const retained = join(result.value.observation_directory, `intake-${intakeDigest(readFileSync(path))}.json`);
	expect(readFileSync(retained)).toEqual(readFileSync(path));
	expect(JSON.parse(readFileSync(retained, "utf8")).adr.citations[0].target.identifier).toBe("#1");
	expect((await reconcileAdrIntake(repo, path)).saved).toBe(false);
});
it("requires explicit supersession and never rolls back on old replay", async () => {
	const old = input(fixture(), "old");
	const oldDigest = intakeDigest(readFileSync(old));
	const next = input({ ...fixture(), title: "Updated reviewed source" }, "next");
	await expect(reconcileAdrIntake(repo, next)).rejects.toThrow(/supersedes/);
	await expect(reconcileAdrIntake(repo, next, { supersedes: "c".repeat(64) })).rejects.toThrow(/supersedes/);
	const updated = await reconcileAdrIntake(repo, next, { supersedes: oldDigest });
	expect(updated.value.latest_sequence).toBe(2);
	const replay = await reconcileAdrIntake(repo, old);
	expect(replay.state.board.columns[0].cards[0].title).toBe("Reviewed slice");
	expect(replay.value.latest_sequence).toBe(2);
	const observation = JSON.parse(readFileSync(join(updated.value.observation_directory, "2.json"), "utf8"));
	expect(observation.adr_intake.supersedes_sha256).toBe(oldDigest);
	await expect(recordWorkObservation(repo, join(updated.value.observation_directory, "2.json"))).rejects.toThrow(
		/reconcile-intake/,
	);
});
it("missing dependencies leave no sealed event; arrows preserve direction, replay, and reject cycles", async () => {
	const dependent = fixture("dependent");
	dependent.dependencies.push({
		work_id: "prerequisite",
		relationship: "requires",
		basis: "source_explicit",
		reason: "Needs it",
	});
	const path = input(dependent, "dependent");
	await expect(reconcileAdrIntake(repo, path)).rejects.toThrow(/exactly one retained/);
	const prerequisite = await reconcileAdrIntake(repo, input(fixture("prerequisite"), "prerequisite"));
	const result = await reconcileAdrIntake(repo, path);
	expect(result.value.latest_sequence).toBe(1);
	expect(result.state.board.dependencies).toEqual([
		expect.objectContaining({ fromTaskId: result.value.card_id, toTaskId: prerequisite.value.card_id }),
	]);
	const clock = vi.spyOn(Date, "now").mockReturnValue(Date.now() + 60_000);
	try {
		expect((await reconcileAdrIntake(repo, path)).saved).toBe(false);
	} finally {
		clock.mockRestore();
	}
});
it("proposed dependencies remain evidence only; superseded requirements retain the original execution arrows", async () => {
	const value = fixture("dependent");
	value.dependencies.push({ work_id: "unknown", relationship: "requires", basis: "proposed", reason: "Needs review" });
	const result = await reconcileAdrIntake(repo, input(value, "proposal"), {
		supersedes: intakeDigest(readFileSync(join(root, "dependent.json"))),
	});
	expect(result.state.board.dependencies).toHaveLength(1);
});
it("replay repairs interrupted board projection and preserves progressed cards", async () => {
	const path = input(fixture("repair"), "repair");
	const first = await reconcileAdrIntake(repo, path);
	await mutateWorkspaceState(
		repo,
		(state) => ({
			board: {
				...state.board,
				columns: state.board.columns.map((c) => ({
					...c,
					cards: c.cards.filter((card) => card.id !== first.value.card_id),
				})),
			},
			value: null,
		}),
		{ observationProjection: true },
	);
	const repaired = await reconcileAdrIntake(repo, path);
	expect(repaired.value.replay).toBe(true);
	expect(repaired.saved).toBe(true);
	await mutateWorkspaceState(
		repo,
		(state) => ({ board: moveTaskToColumn(state.board, first.value.card_id, "review").board, value: null }),
		{ observationProjection: true },
	);
	const replay = await reconcileAdrIntake(repo, path);
	expect(replay.saved).toBe(false);
	expect(
		replay.state.board.columns.find((c) => c.id === "review")?.cards.some((c) => c.id === first.value.card_id),
	).toBe(true);
});

it("resolves cross-repository work IDs and refuses ambiguous targets", async () => {
	const core = fixture("core:foundation");
	core.owner = { repository: "example/core", role: "core" };
	const target = await reconcileAdrIntake(repo, input(core, "core"));
	const ats = fixture("ats:feature");
	ats.owner = { repository: "example/ats", role: "ats" };
	ats.dependencies.push({
		work_id: "core:foundation",
		relationship: "requires",
		basis: "source_explicit",
		reason: "Cross repository prerequisite",
	});
	const result = await reconcileAdrIntake(repo, input(ats, "ats"));
	expect(result.state.board.dependencies).toContainEqual(
		expect.objectContaining({ fromTaskId: result.value.card_id, toTaskId: target.value.card_id }),
	);
	const duplicate = fixture("core:foundation");
	duplicate.owner = { repository: "example/other", role: "other" };
	await reconcileAdrIntake(repo, input(duplicate, "duplicate-identity"));
	const ambiguous = fixture("ambiguous");
	ambiguous.dependencies = ats.dependencies;
	await expect(reconcileAdrIntake(repo, input(ambiguous, "ambiguous"))).rejects.toThrow(/2 matches/);
});

it("public CLI reconciles and replays the same fixture as the adapter", async () => {
	const path = input(fixture("public-cli"), "public-cli");
	const adapter = await reconcileAdrIntake(repo, path);
	const output = execFileSync(
		process.execPath,
		[
			"--import",
			"tsx",
			resolve("src/cli.ts"),
			"task",
			"reconcile-intake",
			"--project-path",
			repo,
			"--intake",
			path,
			"--base-ref",
			executionBase,
		],
		{
			env: { ...process.env, KANBAN_RUNTIME_HOST: "127.0.0.1", KANBAN_RUNTIME_PORT: "1", KANBAN_RUNTIME_HTTPS: "0" },
			encoding: "utf8",
			timeout: 10_000,
		},
	);
	const result = JSON.parse(output);
	expect(result).toMatchObject({
		ok: true,
		card_id: adapter.value.card_id,
		replay: true,
		dispatch: false,
		projection_saved: false,
		intake_sha256: intakeDigest(readFileSync(path)),
	});
});
it("concurrent source supersessions admit only one matching prior digest", async () => {
	const original = input(fixture("concurrent-update"), "concurrent-original");
	await reconcileAdrIntake(repo, original);
	const supersedes = intakeDigest(readFileSync(original));
	const outcomes = await Promise.allSettled([
		reconcileAdrIntake(repo, input({ ...fixture("concurrent-update"), title: "Candidate A" }, "candidate-a"), {
			supersedes,
		}),
		reconcileAdrIntake(repo, input({ ...fixture("concurrent-update"), title: "Candidate B" }, "candidate-b"), {
			supersedes,
		}),
	]);
	expect(outcomes.filter((result) => result.status === "fulfilled")).toHaveLength(1);
	expect(outcomes.filter((result) => result.status === "rejected")).toHaveLength(1);
});

it("retains historical obs cards while explicit migration binds native execution to target repo", async () => {
	const value = fixture("legacy-migration");
	const path = input(value, "legacy-migration");
	const context = await loadWorkspaceContext(repo);
	const sha = intakeDigest(readFileSync(path));
	const identity = { workspace_id: context.workspaceId, work_id: intakeWorkId(value.owner.repository, value.work_id) };
	const legacy = await ingestWorkObservation(repo, identity, (_events, directory) => {
		const retained = join(directory, `intake-${sha}.json`);
		writeFileSync(retained, readFileSync(path));
		return {
			...identity,
			schema_version: "ops.work-observation.v1",
			adr_intake: { sha256: sha, path: retained, supersedes_sha256: null },
			sequence: 1,
			previous_sha256: null,
			session_id: "legacy",
			source_revision: source.revision,
			recorded_at: new Date().toISOString(),
			title: value.title,
			outcome: "Legacy documentary-only intake",
			stage: "backlog",
			evidence: [{ path: retained, sha256: sha }],
		};
	});
	const original = legacy.state.board.columns.flatMap((c) => c.cards).find((c) => c.id === legacy.value.card_id);
	const migrated = await reconcileAdrIntake(repo, path);
	const cards = migrated.state.board.columns.flatMap((c) => c.cards);
	expect(cards.find((c) => c.id === legacy.value.card_id)).toEqual(original);
	expect(cards.find((c) => c.id === migrated.value.card_id)?.baseRef).toBe(executionBase);
	expect(migrated.value.replay).toBe(true);
	expect(() => requireDispatchableTask(legacy.value.card_id)).toThrow();
	expect(() => requireDispatchableTask(migrated.value.card_id)).not.toThrow();
});
it("requires an explicit target execution commit rather than borrowing documentary provenance", async () => {
	const path = input(fixture("execution-target"), "execution-target");
	await expect(reconcileOriginal(repo, path)).rejects.toThrow(/target execution/);
	await expect(reconcileOriginal(repo, path, { baseRef: source.revision })).rejects.toThrow(/resolve to a commit/);
	const result = await reconcileAdrIntake(repo, path);
	const card = result.state.board.columns.flatMap((c) => c.cards).find((c) => c.id === result.value.card_id);
	expect(card?.baseRef).toBe(executionBase);
	expect(card?.prompt).toContain(source.revision);
	expect(card?.adrOrigin?.executionRepository).toBe(repo);
	expect(result.value.execution_base_ref).toBe(executionBase);
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
		"--allow-empty",
		"-m",
		"new target",
	]);
	const anotherBase = execFileSync("git", ["-C", repo, "rev-parse", "HEAD"], { encoding: "utf8" }).trim();
	await expect(reconcileOriginal(repo, path, { baseRef: anotherBase })).rejects.toThrow(/cannot retarget/);
});
it("native ADR dependencies never authorize automatic cascade on review to trash", async () => {
	const current = await reconcileAdrIntake(repo, join(root, "dependent.json"));
	const prerequisite = (await listAdrIntakes(repo)).find((i) => i.intake.work_id === "prerequisite");
	if (!prerequisite) throw new Error("Missing prerequisite fixture");
	const reviewed = moveTaskToColumn(current.state.board, prerequisite.taskId, "review").board;
	expect(trashTaskAndGetReadyLinkedTaskIds(reviewed, prerequisite.taskId).readyTaskIds).toEqual([]);
});
it("explicit requirement acceptance needs matching GREEN evidence; rejected decisions and amendment proposals remain history", async () => {
	const path = input(fixture("evidence"), "evidence");
	const created = await reconcileAdrIntake(repo, path);
	const evidence = [{ path, sha256: intakeDigest(readFileSync(path)) }];
	const base = {
		requirementId: "acceptance:1",
		intakeSha256: intakeDigest(readFileSync(path)),
		rationale: "Fixture observed result",
		evidence,
	};
	await expect(
		recordAdrEvidence(repo, created.value.card_id, { ...base, kind: "acceptance", outcome: "accepted" }),
	).rejects.toThrow(/GREEN/);
	await recordAdrEvidence(repo, created.value.card_id, { ...base, kind: "red", outcome: "failed" });
	await recordAdrEvidence(repo, created.value.card_id, { ...base, kind: "acceptance", outcome: "rejected" });
	await recordAdrEvidence(repo, created.value.card_id, { ...base, kind: "green", outcome: "passed" });
	await expect(
		recordAdrEvidence(repo, created.value.card_id, {
			...base,
			requirementId: "acceptance:2",
			kind: "acceptance",
			outcome: "accepted",
		}),
	).rejects.toThrow(/acceptance scenario/);
	await recordAdrEvidence(repo, created.value.card_id, { ...base, kind: "acceptance", outcome: "accepted" });
	await recordAdrEvidence(repo, created.value.card_id, {
		...base,
		kind: "amendment_proposal",
		outcome: "proposed",
		amendment: "Proposed: record scenario 1 acceptance; runtime deployment unresolved.",
	});
	const records = await readAdrEvidence(repo, created.value.card_id);
	expect(records).toHaveLength(5);
	expect(records.some((r) => r.input.outcome === "rejected")).toBe(true);
	expect(readFileSync(path)).toEqual(
		readFileSync(join(created.value.observation_directory, `intake-${base.intakeSha256}.json`)),
	);
	const output = execFileSync(
		process.execPath,
		[
			"--import",
			"tsx",
			resolve("src/cli.ts"),
			"task",
			"adr-evidence",
			"--project-path",
			repo,
			"--task-id",
			created.value.card_id,
		],
		{ encoding: "utf8", timeout: 10000 },
	);
	expect(JSON.parse(output).records).toHaveLength(5);
	await recordAdrEvidence(repo, created.value.card_id, { ...base, kind: "red", outcome: "failed" });
	await expect(
		recordAdrEvidence(repo, created.value.card_id, { ...base, kind: "acceptance", outcome: "accepted" }),
	).rejects.toThrow(/GREEN/);
	const updated = input({ ...fixture("evidence"), title: "Changed acceptance scope" }, "evidence-next");
	await reconcileAdrIntake(repo, updated, { supersedes: base.intakeSha256 });
	await expect(
		recordAdrEvidence(repo, created.value.card_id, { ...base, kind: "acceptance", outcome: "accepted" }),
	).rejects.toThrow(/current intake/);
});
it("evidence CLI retries are idempotent, retain assertion attribution, and reject changed payload reuse", async () => {
	const path = input(fixture("evidence-retry"), "evidence-retry");
	const created = await reconcileAdrIntake(repo, path);
	const event = {
		eventId: randomUUID(),
		actor: { kind: "local_operator_assertion" as const, id: "fixture-operator" },
		requirementId: "acceptance:1",
		intakeSha256: intakeDigest(readFileSync(path)),
		kind: "red" as const,
		outcome: "failed" as const,
		rationale: "Explicit assertion, no authenticated identity",
		evidence: [{ path, sha256: intakeDigest(readFileSync(path)) }],
	};
	const eventPath = input(event, "evidence-request");
	const args = [
		"--import",
		"tsx",
		resolve("src/cli.ts"),
		"task",
		"adr-evidence",
		"--project-path",
		repo,
		"--task-id",
		created.value.card_id,
		"--event",
		eventPath,
	];
	const first = JSON.parse(execFileSync(process.execPath, args, { encoding: "utf8", timeout: 10000 }));
	const second = JSON.parse(execFileSync(process.execPath, args, { encoding: "utf8", timeout: 10000 }));
	expect(second.record).toEqual(first.record);
	expect(first.record.input.actor.kind).toBe("local_operator_assertion");
	expect(first.record.execution).toEqual({
		intakeSha256: event.intakeSha256,
		repository: repo,
		baseRef: executionBase,
	});
	expect(await readAdrEvidence(repo, created.value.card_id)).toHaveLength(1);
	await expect(
		recordAdrEvidence(repo, created.value.card_id, { ...event, rationale: "Changed payload" }),
	).rejects.toThrow(/sealed/);
	const updated = input({ ...fixture("evidence-retry"), title: "New documentary scope" }, "evidence-retry-next");
	await reconcileAdrIntake(repo, updated, { supersedes: event.intakeSha256 });
	expect(await recordAdrEvidence(repo, created.value.card_id, event)).toEqual(first.record);
	const newScope = { ...event, eventId: randomUUID(), intakeSha256: intakeDigest(readFileSync(updated)) };
	await expect(
		recordAdrEvidence(repo, created.value.card_id, { ...newScope, kind: "green", outcome: "passed" }),
	).rejects.toThrow(/execution scope/);
	await expect(
		recordAdrEvidence(repo, created.value.card_id, { ...newScope, kind: "acceptance", outcome: "accepted" }),
	).rejects.toThrow(/execution scope/);
	const proposal = await recordAdrEvidence(repo, created.value.card_id, {
		...newScope,
		kind: "amendment_proposal",
		outcome: "proposed",
		amendment: "Review new scope separately.",
	});
	expect(proposal.execution.intakeSha256).toBe(event.intakeSha256);
});
it("replay verifies retained evidence and rejects symlink or oversized intake", async () => {
	const path = input(fixture("tamper"), "tamper");
	const result = await reconcileAdrIntake(repo, path);
	const retained = join(result.value.observation_directory, `intake-${intakeDigest(readFileSync(path))}.json`);
	writeFileSync(retained, "{}");
	await expect(reconcileAdrIntake(repo, path)).rejects.toThrow(/digest mismatch/);
	writeFileSync(retained, readFileSync(path));
	const link = join(root, "symlink.json");
	symlinkSync(path, link);
	await expect(reconcileAdrIntake(repo, link)).rejects.toThrow(/physical/);
	const oversized = join(root, "oversized");
	writeFileSync(oversized, Buffer.alloc(1_000_001));
	await expect(reconcileAdrIntake(repo, oversized)).rejects.toThrow(/bounded/);
});

it("intake traversal limits reject oversized directories before parsing event files", async () => {
	const path = input(fixture("bounded-directory"), "bounded-directory");
	const created = await reconcileAdrIntake(repo, path);
	writeFileSync(join(created.value.observation_directory, "1.json"), "invalid JSON must not be parsed");
	for (let index = 0; index < 2049; index++)
		writeFileSync(join(created.value.observation_directory, `extra-${index}`), "");
	await expect(reconcileAdrIntake(repo, path)).rejects.toThrow(/traversal limit/);
});
