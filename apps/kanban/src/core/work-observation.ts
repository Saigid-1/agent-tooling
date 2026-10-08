/** Explicit host assertions projected onto Kanban; never launches a session. */
import { createHash } from "node:crypto";
import { z } from "zod";
import type { RuntimeBoardData } from "./api-contract";
import { addTaskToColumn, moveTaskToColumn, updateTask } from "./task-board-mutations";

const bounded = z.string().trim().min(1).max(512);
export const workObservationSchema = z
	.object({
		adr_intake: z
			.object({
				execution: z
					.object({ repository: z.string(), baseRef: z.string().regex(/^[a-f0-9]{40}$/) })
					.strict()
					.optional(),
				sha256: z.string().regex(/^[a-f0-9]{64}$/),
				path: z.string().min(1).max(512),
				supersedes_sha256: z
					.string()
					.regex(/^[a-f0-9]{64}$/)
					.nullable(),
			})
			.strict()
			.optional(),
		schema_version: z.literal("ops.work-observation.v1"),
		workspace_id: bounded,
		work_id: bounded,
		sequence: z.number().int().positive(),
		previous_sha256: z
			.string()
			.regex(/^[a-f0-9]{64}$/)
			.nullable(),
		session_id: bounded,
		source_revision: z.string().regex(/^[a-f0-9]{40}$/),
		recorded_at: z.string().datetime({ offset: true }),
		title: bounded,
		outcome: z.string().trim().min(1).max(8000),
		stage: z.enum(["backlog", "in_progress", "review"]),
		evidence: z.array(z.object({ path: bounded, sha256: z.string().regex(/^[a-f0-9]{64}$/) }).strict()).max(20),
	})
	.strict();
export type WorkObservation = z.infer<typeof workObservationSchema>;
export function observationHash(event: WorkObservation): string {
	return createHash("sha256")
		.update(JSON.stringify(workObservationSchema.parse(event)))
		.digest("hex");
}
export function observationCardId<T extends Pick<WorkObservation, "workspace_id" | "work_id">>(event: T): string {
	return (
		"obs_" +
		createHash("sha256")
			.update(JSON.stringify([event.workspace_id, event.work_id]))
			.digest("hex")
			.slice(0, 32)
	);
}
export function validateObservationChain(events: WorkObservation[]): void {
	let previous: WorkObservation | undefined;
	for (const event of events) {
		workObservationSchema.parse(event);
		if (
			event.sequence !== (previous?.sequence ?? 0) + 1 ||
			event.previous_sha256 !== (previous ? observationHash(previous) : null)
		)
			throw new Error("Observation sequence or previous hash mismatch");
		if (previous && (event.work_id !== previous.work_id || event.workspace_id !== previous.workspace_id))
			throw new Error("Observation work identity mismatch");
		if (event.stage === "review" && event.evidence.length === 0)
			throw new Error("Review requires linked evidence; it does not establish acceptance");
		previous = event;
	}
}
export function projectObservations(board: RuntimeBoardData, events: WorkObservation[]): RuntimeBoardData {
	validateObservationChain(events);
	const latest = events.at(-1);
	if (!latest) throw new Error("At least one observation required");
	const id = observationCardId(latest);
	const prompt = [
		latest.outcome,
		"",
		"EXTERNAL WORK OBSERVATION — continue work in its originating chat.",
		"Operator/agent assertions; no desk admission, authenticated host identity, execution proof or acceptance implied.",
		`Work: ${latest.work_id}`,
		`Revision: ${latest.source_revision}`,
		`Sessions: ${[...new Set(events.map((e) => e.session_id))].join(", ")}`,
		`Observation: ${latest.sequence} / ${observationHash(latest)}`,
		...events.flatMap((e) => e.evidence.map((r) => `Evidence (event ${e.sequence}): ${r.path} sha256:${r.sha256}`)),
	].join("\n");
	const input = { title: latest.title, prompt, baseRef: latest.source_revision, autoReviewEnabled: false };
	const exists = board.columns.some((c) => c.cards.some((t) => t.id === id));
	const time = Date.parse(latest.recorded_at);
	const updated = exists
		? updateTask(board, id, input, time).board
		: addTaskToColumn(board, latest.stage, { ...input, taskId: id }, () => id, time).board;
	return moveTaskToColumn(updated, id, latest.stage, time).board;
}
