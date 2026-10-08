import { describe, expect, it } from "vitest";
import type { RuntimeBoardData } from "../../src/core/api-contract";
import { preserveProjectedCards, requireDispatchableTask } from "../../src/core/projection-policy";
import { addTaskDependency, trashTaskAndGetReadyLinkedTaskIds } from "../../src/core/task-board-mutations";
import {
	observationCardId,
	observationHash,
	projectObservations,
	validateObservationChain,
	type WorkObservation,
	workObservationSchema,
} from "../../src/core/work-observation";

const board = (): RuntimeBoardData => ({
	columns: ["backlog", "in_progress", "review", "trash"].map((id) => ({
		id: id as "backlog" | "in_progress" | "review" | "trash",
		title: id,
		cards: [],
	})),
	dependencies: [],
});
const event = (): WorkObservation => ({
	schema_version: "ops.work-observation.v1",
	workspace_id: "fixture",
	work_id: "one",
	sequence: 1,
	previous_sha256: null,
	session_id: "fixture-session-a",
	source_revision: "a".repeat(40),
	recorded_at: "2026-09-23T23:00:00Z",
	title: "Fixture work",
	outcome: "Prove projection",
	stage: "in_progress",
	evidence: [],
});
describe("external work projection", () => {
	it("replays without duplicate cards and never enables auto review", () => {
		const e = event();
		const first = projectObservations(board(), [e]);
		expect(projectObservations(first, [e])).toEqual(first);
		expect(first.columns[1].cards).toHaveLength(1);
		expect(first.columns[1].cards[0].autoReviewEnabled).toBe(false);
	});
	it("links two fixture sessions to one card, with review evidence", () => {
		const a = event();
		const b = {
			...a,
			sequence: 2,
			previous_sha256: observationHash(a),
			session_id: "fixture-session-b",
			stage: "review" as const,
			evidence: [{ path: "/fixture/receipt", sha256: "b".repeat(64) }],
		};
		const result = projectObservations(projectObservations(board(), [a]), [a, b]);
		expect(result.columns[1].cards).toHaveLength(0);
		expect(result.columns[2].cards).toHaveLength(1);
		expect(result.columns[2].cards[0].prompt).toContain("fixture-session-a, fixture-session-b");
	});
	it("refuses gaps and stale previous hashes", () => {
		const a = event();
		expect(() => validateObservationChain([a, { ...a, sequence: 3 }])).toThrow(/sequence/);
		expect(() => validateObservationChain([a, { ...a, sequence: 2 }])).toThrow(/hash/);
	});
	it("refuses review with no evidence", () => {
		expect(() => projectObservations(board(), [{ ...event(), stage: "review" }])).toThrow(/evidence/);
	});
	it("does not accept done or additional authorization claims", () => {
		expect(() => workObservationSchema.parse({ ...event(), stage: "done" })).toThrow();
		expect(() => workObservationSchema.parse({ ...event(), desk_admitted: true })).toThrow();
	});
	it("qualifies work identity by workspace, not session", () => {
		const a = event();
		expect(observationCardId({ ...a, session_id: "other" })).toBe(observationCardId(a));
		expect(observationCardId({ ...a, workspace_id: "other" })).not.toBe(observationCardId(a));
	});
	it("preserves unrelated cards", () => {
		const a = event();
		const existing = projectObservations(board(), [{ ...a, work_id: "unrelated" }]);
		expect(projectObservations(existing, [a]).columns[1].cards).toHaveLength(2);
	});
});

it("protects observation cards from manual mutation and dispatch while allowing visual links", () => {
	const a = event();
	const first = projectObservations(board(), [a]);
	expect(() => requireDispatchableTask(observationCardId(a))).toThrow(/source chat/);
	expect(() => preserveProjectedCards(first, board())).toThrow(/captured observations/);
	const backlog = projectObservations(first, [{ ...a, work_id: "dependent", stage: "backlog" }]);
	const link = addTaskDependency(backlog, observationCardId({ ...a, work_id: "dependent" }), observationCardId(a));
	expect(link.added).toBe(true);
	expect(() => preserveProjectedCards(backlog, link.board)).not.toThrow();
	const reviewed = projectObservations(link.board, [
		{ ...a, stage: "review", evidence: [{ path: "/fixture", sha256: "b".repeat(64) }] },
	]);
	expect(trashTaskAndGetReadyLinkedTaskIds(reviewed, observationCardId(a)).readyTaskIds).toEqual([]);
});
