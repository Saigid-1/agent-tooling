import { execFileSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import {
	closeSync,
	constants,
	existsSync,
	fstatSync,
	linkSync,
	openSync,
	readdirSync,
	readFileSync,
	realpathSync,
	unlinkSync,
	writeFileSync,
} from "node:fs";
import { dirname, join, resolve } from "node:path";
import {
	intakeDigest,
	intakeSummary,
	intakeWorkId,
	nativeIntakeCardId,
	parseAdrIntake,
	projectIntakeDependencies,
} from "../core/adr-intake";
import { addTaskToColumn, getTaskColumnId } from "../core/task-board-mutations";
import {
	observationCardId,
	observationHash,
	validateObservationChain,
	workObservationSchema,
} from "../core/work-observation";
import { getConfiguredStorageRoot } from "./runtime-storage-root";
import { ingestWorkObservation } from "./work-observation-store";
import { loadWorkspaceContext } from "./workspace-state";

export interface ReconcileIntakeOptions {
	supersedes?: string;
	/** Explicit target execution revision, independent of documentary source provenance. */
	baseRef?: string;
}
function boundedDirectory(path: string, maximum: number): string[] {
	const names = readdirSync(path);
	if (names.length > maximum) throw new Error("ADR intake directory exceeds traversal limit");
	return names;
}
function readBoundedFile(path: string): Buffer {
	if (realpathSync(path) !== resolve(path)) throw new Error("Intake path must be physical, without symlinks");
	const fd = openSync(path, constants.O_RDONLY | constants.O_NOFOLLOW);
	try {
		const stat = fstatSync(fd);
		if (!stat.isFile() || stat.size > 1_000_000) throw new Error("Intake must be a bounded regular file");
		const bytes = readFileSync(fd);
		if (bytes.length > 1_000_000) throw new Error("Intake exceeds 1000000 bytes");
		return bytes;
	} finally {
		closeSync(fd);
	}
}
function retainBytes(path: string, bytes: Buffer): void {
	if (existsSync(path)) {
		if (!readBoundedFile(path).equals(bytes)) throw new Error("Retained intake bytes mismatch");
		return;
	}
	const pending = `${path}.pending-${randomUUID()}`;
	try {
		writeFileSync(pending, bytes, { flag: "wx", mode: 0o600 });
		linkSync(pending, path);
	} finally {
		if (existsSync(pending)) unlinkSync(pending);
	}
}
export async function reconcileAdrIntake(cwd: string, inputPath: string, options: ReconcileIntakeOptions = {}) {
	const bytes = readBoundedFile(inputPath);
	const intake = parseAdrIntake(bytes);
	const digest = intakeDigest(bytes);
	const context = await loadWorkspaceContext(cwd, { autoCreateIfMissing: false });
	const revision = options.baseRef;
	if (!revision || !/^[a-f0-9]{40}$/.test(revision))
		throw new Error("A pinned 40-character target execution --base-ref is required");

	try {
		execFileSync("git", ["-C", context.repoPath, "cat-file", "-e", `${revision}^{commit}`], { stdio: "pipe" });
	} catch {
		throw new Error("Target execution base must resolve to a commit in the workspace repository");
	}
	const identity = {
		workspace_id: context.workspaceId,
		work_id: intakeWorkId(intake.owner.repository, intake.work_id),
	};
	const result = await ingestWorkObservation(
		cwd,
		identity,
		(events, directory) => {
			for (const event of events) {
				const ref = event.adr_intake;
				if (!ref || ref.path !== join(directory, `intake-${ref.sha256}.json`))
					throw new Error("Not an ADR intake history");
				if (intakeDigest(readBoundedFile(ref.path)) !== ref.sha256)
					throw new Error("Retained intake digest mismatch");
			}
			const replay = events.find((event) => event.adr_intake?.sha256 === digest);
			if (replay) return replay;
			const previous = events.at(-1);
			if ((options.supersedes ?? null) !== (previous?.adr_intake?.sha256 ?? null))
				throw new Error("Explicit --supersedes must match the latest intake SHA256");
			const path = join(directory, `intake-${digest}.json`);
			retainBytes(path, bytes);
			return {
				...identity,
				schema_version: "ops.work-observation.v1",
				adr_intake: {
					execution: { repository: context.repoPath, baseRef: revision },
					sha256: digest,
					path,
					supersedes_sha256: options.supersedes ?? null,
				},
				sequence: (previous?.sequence ?? 0) + 1,
				previous_sha256: previous ? observationHash(previous) : null,
				session_id: "adr-intake-reconciler.v1",
				source_revision: revision,
				recorded_at: new Date().toISOString(),
				title: intake.title,
				outcome: intakeSummary(intake, digest, options.supersedes ?? null),
				stage: "backlog",
				evidence: [{ path, sha256: digest }],
			};
		},
		(board, events) => {
			const cardId = nativeIntakeCardId(identity);
			const currentColumn = getTaskColumnId(board, cardId);
			const latestEvent = events.at(-1);
			const latest = latestEvent?.adr_intake;
			if (!latest || !latestEvent) throw new Error("Missing intake reference");
			const latestIntake = parseAdrIntake(readBoundedFile(latest.path));
			// Immutable native request: supersession records new documentary evidence, never edits a dispatched request.
			if (currentColumn) {
				const existing = board.columns.flatMap((column) => column.cards).find((card) => card.id === cardId);
				if (existing?.baseRef !== revision)
					throw new Error(`Native card is pinned to ${existing?.baseRef}; reconcile cannot retarget execution`);
				return board;
			}
			const executionBase = latest.execution?.baseRef ?? revision;
			if (executionBase !== revision)
				throw new Error(`Retained execution is pinned to ${executionBase}; reconcile cannot retarget execution`);
			if (latest.execution && latest.execution.repository !== context.repoPath)
				throw new Error("Intake execution repository mismatch");
			const prompt = [
				"Deliver this reviewed ADR slice using the native task workspace. Dispatch requires an explicit operator start.",
				"Do not infer acceptance or Built/Deployed/Proven from task movement or successful execution.",
				`Execution repository: ${context.repoPath}; execution base: ${executionBase}`,
				`Retained intake: ${latest.path}; sha256:${latest.sha256}`,
				JSON.stringify(latestIntake, null, 2),
			].join("\n\n");
			const projected = addTaskToColumn(
				board,
				"backlog",
				{
					adrOrigin: {
						workId: identity.work_id,
						historyCardId: observationCardId(identity),
						intakeSha256: latest.sha256,
						executionRepository: context.repoPath,
						dispatchPolicy: "manual_only",
					},
					taskId: cardId,
					title: latestIntake.title,
					prompt,
					baseRef: executionBase,
					autoReviewEnabled: false,
				},
				() => cardId,
				Date.parse(latestEvent.recorded_at),
			).board;
			// Resolve work IDs from retained, verified intake history, never from parent ownership or card prose.
			const targets = new Map<string, string[]>();
			for (const card of projected.columns.flatMap((column) => column.cards)) {
				const historyCardId =
					card.adrOrigin?.historyCardId ?? (/^obs_[a-f0-9]{32}$/.test(card.id) ? card.id : null);
				if (!historyCardId || !/^obs_[a-f0-9]{32}$/.test(historyCardId)) continue;
				if (
					card.id.startsWith("obs_") &&
					projected.columns.some((column) =>
						column.cards.some((native) => native.adrOrigin?.historyCardId === card.id),
					)
				)
					continue;
				const directory = join(dirname(dirname(latest.path)), historyCardId);
				if (!existsSync(directory)) continue;
				if (realpathSync(directory) !== directory) throw new Error("Observation storage must be physical");
				const history =
					card.id === nativeIntakeCardId(identity)
						? events
						: boundedDirectory(directory, 2048)
								.filter((name) => /^\d+\.json$/.test(name))
								.sort((a, b) => Number.parseInt(a, 10) - Number.parseInt(b, 10))
								.map((name) =>
									workObservationSchema.parse(
										JSON.parse(readBoundedFile(join(directory, name)).toString("utf8")),
									),
								);
				validateObservationChain(history);
				const event = history.at(-1);
				if (!event?.adr_intake) continue;
				if (
					event.workspace_id !== identity.workspace_id ||
					history.some((item) => observationCardId(item) !== historyCardId)
				)
					throw new Error("Dependency history identity mismatch");
				const ref = event.adr_intake;
				if (ref.path !== join(directory, `intake-${ref.sha256}.json`))
					throw new Error("Dependency intake path mismatch");
				const retained = readBoundedFile(ref.path);
				if (intakeDigest(retained) !== ref.sha256) throw new Error("Dependency intake digest mismatch");
				const candidate = parseAdrIntake(retained);
				if (intakeWorkId(candidate.owner.repository, candidate.work_id) !== event.work_id)
					throw new Error("Dependency intake identity mismatch");
				targets.set(candidate.work_id, [...(targets.get(candidate.work_id) ?? []), card.id]);
			}
			return projectIntakeDependencies(projected, identity.workspace_id, latestIntake, (workId) => {
				const matches = targets.get(workId) ?? [];
				if (matches.length !== 1 || !matches[0])
					throw new Error(
						`Dependency work_id must resolve to exactly one retained intake card: ${workId} (${matches.length} matches)`,
					);
				return matches[0];
			});
		},
	);
	const executionCard = result.state.board.columns
		.flatMap((column) => column.cards)
		.find((card) => card.id === nativeIntakeCardId(identity));
	return {
		...result,
		value: {
			...result.value,
			card_id: nativeIntakeCardId(identity),
			historical_card_id: observationCardId(identity),
			execution_base_ref: executionCard?.baseRef,
			execution_repository: executionCard?.adrOrigin?.executionRepository,
		},
	};
}

/** Verified documentary snapshots, joined to stable native card identities. */
export async function listAdrIntakes(cwd: string) {
	const context = await loadWorkspaceContext(cwd, { autoCreateIfMissing: false });
	const root = getConfiguredStorageRoot();
	if (!root) throw new Error("Explicit physical KANBAN_STORAGE_ROOT required");
	const parent = join(root, "work-observations");
	if (!existsSync(parent)) return [];
	const results: {
		workspaceId: string;
		taskId: string;
		intake: ReturnType<typeof parseAdrIntake>;
		intakeSha256: string;
	}[] = [];
	for (const name of boundedDirectory(parent, 2048).filter((name) => /^obs_[a-f0-9]{32}$/.test(name))) {
		const directory = join(parent, name);
		if (realpathSync(directory) !== directory) throw new Error("Observation storage must be physical");
		const events = boundedDirectory(directory, 2048)
			.filter((file) => /^\d+\.json$/.test(file))
			.sort((a, b) => Number.parseInt(a, 10) - Number.parseInt(b, 10))
			.map((file) =>
				workObservationSchema.parse(JSON.parse(readBoundedFile(join(directory, file)).toString("utf8"))),
			);
		validateObservationChain(events);
		const latest = events.at(-1);
		if (!latest?.adr_intake || latest.workspace_id !== context.workspaceId) continue;
		if (events.some((event) => observationCardId(event) !== name)) throw new Error("Stored work identity mismatch");
		for (const event of events) {
			const ref = event.adr_intake;
			if (
				!ref ||
				ref.path !== join(directory, `intake-${ref.sha256}.json`) ||
				intakeDigest(readBoundedFile(ref.path)) !== ref.sha256
			)
				throw new Error("Retained intake digest or path mismatch");
		}
		const intake = parseAdrIntake(readBoundedFile(latest.adr_intake.path));
		if (intakeWorkId(intake.owner.repository, intake.work_id) !== latest.work_id)
			throw new Error("Intake identity mismatch");
		results.push({
			workspaceId: context.workspaceId,
			taskId: nativeIntakeCardId(latest),
			intake,
			intakeSha256: latest.adr_intake.sha256,
		});
	}
	return results;
}
