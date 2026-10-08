import { createHash, randomUUID } from "node:crypto";
import {
	existsSync,
	linkSync,
	lstatSync,
	mkdirSync,
	readdirSync,
	readFileSync,
	realpathSync,
	unlinkSync,
	writeFileSync,
} from "node:fs";
import { isAbsolute, join } from "node:path";
import type { RuntimeBoardData } from "../core/api-contract";
import type { WorkObservation } from "../core/work-observation";
import {
	observationCardId,
	observationHash,
	projectObservations,
	validateObservationChain,
	workObservationSchema,
} from "../core/work-observation";
import { getConfiguredStorageRoot } from "./runtime-storage-root";
import { loadWorkspaceContext, mutateWorkspaceState } from "./workspace-state";

export async function recordWorkObservation(cwd: string, eventPath: string) {
	if (!lstatSync(eventPath).isFile() || lstatSync(eventPath).size > 32000)
		throw new Error("Observation exceeds 32000 bytes");
	const event = workObservationSchema.parse(JSON.parse(readFileSync(eventPath, "utf8")));
	if (event.adr_intake) throw new Error("ADR intake observations require task reconcile-intake");
	return ingestWorkObservation(cwd, event, (events) => {
		if (events.some((e) => e.adr_intake)) throw new Error("ADR intake history requires task reconcile-intake");
		return event;
	});
}

/** Prepare and validate under the native workspace lock; seal before board save so replay repairs interruptions. */
export async function ingestWorkObservation(
	cwd: string,
	identity: Pick<WorkObservation, "workspace_id" | "work_id">,
	prepare: (events: WorkObservation[], directory: string) => WorkObservation,
	project: (board: RuntimeBoardData, events: WorkObservation[]) => RuntimeBoardData = projectObservations,
) {
	const root = getConfiguredStorageRoot();
	if (!root) throw new Error("Explicit physical KANBAN_STORAGE_ROOT required");
	const context = await loadWorkspaceContext(cwd, { autoCreateIfMissing: false });
	if (context.workspaceId !== identity.workspace_id) throw new Error("Observation workspace mismatch");
	const cardId = observationCardId(identity);
	const parent = join(root, "work-observations");
	mkdirSync(parent, { recursive: true, mode: 0o700 });
	if (realpathSync(parent) !== parent) throw new Error("Observation storage must be physical");
	const directory = join(parent, cardId);
	mkdirSync(directory, { mode: 0o700, recursive: true });
	if (realpathSync(directory) !== directory) throw new Error("Observation storage must be physical");
	return await mutateWorkspaceState(
		cwd,
		(state) => {
			const names = readdirSync(directory);
			if (names.length > 2048) throw new Error("Observation history exceeds traversal limit");
			const events = names
				.filter((n) => /^\d+\.json$/.test(n))
				.sort((a, b) => Number.parseInt(a, 10) - Number.parseInt(b, 10))
				.map((n) => workObservationSchema.parse(JSON.parse(readFileSync(join(directory, n), "utf8"))));
			validateObservationChain(events);
			if (events.some((e) => observationCardId(e) !== cardId)) throw new Error("Stored work identity mismatch");
			const event = workObservationSchema.parse(prepare(events, directory));
			if (observationCardId(event) !== cardId) throw new Error("Prepared work identity mismatch");
			const existing = events.find((e) => e.sequence === event.sequence);
			if (existing && observationHash(existing) !== observationHash(event))
				throw new Error("Observation coordinate already sealed with different bytes");
			// Project first: invalid dependencies must not consume an observation coordinate.
			const board = project(state.board, existing ? events : [...events, event]);
			if (!existing) {
				validateObservationChain([...events, event]);
				for (const ref of event.evidence) {
					if (!isAbsolute(ref.path) || !lstatSync(ref.path).isFile() || lstatSync(ref.path).size > 8000000)
						throw new Error("Evidence must be a bounded local regular file");
					if (createHash("sha256").update(readFileSync(ref.path)).digest("hex") !== ref.sha256)
						throw new Error("Evidence digest mismatch");
				}
				const sealed = join(directory, `${event.sequence}.json`);
				const temp = join(directory, `.pending-${randomUUID()}`);
				try {
					writeFileSync(temp, `${JSON.stringify(event)}\n`, { flag: "wx", mode: 0o600 });
					linkSync(temp, sealed); // Exclusive publication; never overwrite an event.
				} finally {
					if (existsSync(temp)) unlinkSync(temp);
				}
				events.push(event);
			}
			// Reconstruct from the latest sealed event on every replay, including after interruption.

			return {
				board,
				save: JSON.stringify(board) !== JSON.stringify(state.board),
				value: {
					ok: true,
					card_id: cardId,
					event_sha256: observationHash(event),
					...(event.adr_intake
						? { intake_sha256: event.adr_intake.sha256, latest_intake_sha256: events.at(-1)?.adr_intake?.sha256 }
						: {}),
					latest_sequence: events.at(-1)?.sequence,
					replay: Boolean(existing),
					dispatch: false,
					observation_directory: directory,
					evidence_boundary:
						"local assertions; referenced bytes verified on first ingestion; not authenticated host testimony or acceptance",
				},
			};
		},
		{ observationProjection: true },
	);
}
