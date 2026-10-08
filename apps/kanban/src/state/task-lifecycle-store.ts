/** Durable, bounded observations of native session state, not execution or acceptance proof. */
import { createHash } from "node:crypto";
import { constants, existsSync, lstatSync, mkdirSync, realpathSync } from "node:fs";
import { open } from "node:fs/promises";
import { join } from "node:path";
import { z } from "zod";
import { type RuntimeTaskSessionSummary, runtimeTaskSessionSummarySchema } from "../core/api-contract";
import { lockedFileSystem } from "../fs/locked-file-system";
import { getConfiguredStorageRoot } from "./runtime-storage-root";

const MAX_BYTES = 4 * 1024 * 1024;
const MAX_RECORDS = 4096;
const digest = (value: unknown): string => createHash("sha256").update(JSON.stringify(value)).digest("hex");
const sourceSchema = z.enum(["cline", "terminal"]);
export type LifecycleSource = z.infer<typeof sourceSchema>;
const stateSchema = runtimeTaskSessionSummarySchema.pick({
	taskId: true,
	state: true,
	agentId: true,
	workspacePath: true,
	pid: true,
	startedAt: true,
	reviewReason: true,
	exitCode: true,
	nativeSessionBindings: true,
	latestTurnCheckpoint: true,
});
const recordSchema = z
	.object({
		sequence: z.number().int().positive(),
		previous_sha256: z.string().nullable(),
		observed_at: z.string().datetime(),
		producer_updated_at: z.number(),
		capture_gap: z.string().max(1024).nullable(),
		source: sourceSchema,
		state: stateSchema,
		sha256: z.string().regex(/^[a-f0-9]{64}$/),
	})
	.strict();
const journalSchema = z
	.object({
		schema_version: z.literal("agent-tooling.task-lifecycle.v1"),
		workspace_id: z.string().min(1).max(512),
		task_id: z.string().min(1).max(512),
		records: z.array(recordSchema).max(MAX_RECORDS),
	})
	.strict();
type Journal = z.infer<typeof journalSchema>;
export function lifecycleState(summary: RuntimeTaskSessionSummary) {
	return stateSchema.parse(summary);
}
export function lifecycleFingerprint(source: LifecycleSource, summary: RuntimeTaskSessionSummary): string {
	return digest({ source, state: lifecycleState(summary) });
}
function journalPath(workspaceId: string, taskId: string, create: boolean): string | null {
	if (!workspaceId || workspaceId.length > 512 || !taskId || taskId.length > 512)
		throw new Error("Invalid lifecycle identity");
	const root = getConfiguredStorageRoot();
	if (!root) throw new Error("Explicit KANBAN_STORAGE_ROOT required for lifecycle capture");
	let current = root;
	for (const part of ["task-lifecycle", digest(workspaceId)]) {
		current = join(current, part);
		if (!existsSync(current)) {
			if (!create) return null;
			try {
				mkdirSync(current, { mode: 0o700 });
			} catch (error) {
				if (!(error instanceof Error) || !("code" in error) || error.code !== "EEXIST") throw error;
			}
		}
		if (!lstatSync(current).isDirectory() || realpathSync(current) !== current)
			throw new Error("Lifecycle storage must be physical");
	}
	return join(current, `${digest(taskId)}.json`);
}
async function readJournal(path: string | null, workspaceId: string, taskId: string): Promise<Journal> {
	const empty: Journal = {
		schema_version: "agent-tooling.task-lifecycle.v1",
		workspace_id: workspaceId,
		task_id: taskId,
		records: [],
	};
	if (!path) return empty;
	let bytes: Buffer;
	try {
		const file = await open(path, constants.O_RDONLY | constants.O_NOFOLLOW);
		try {
			const stat = await file.stat();
			if (!stat.isFile() || stat.size > MAX_BYTES)
				throw new Error("Lifecycle journal exceeds bounded regular-file limit");
			const buffer = Buffer.alloc(stat.size + 1);
			let length = 0;
			while (length < buffer.length) {
				const result = await file.read(buffer, length, buffer.length - length, null);
				if (!result.bytesRead) break;
				length += result.bytesRead;
			}
			if (length > stat.size) throw new Error("Lifecycle journal grew during read; retry");
			bytes = buffer.subarray(0, length);
		} finally {
			await file.close();
		}
	} catch (error) {
		if (error instanceof Error && "code" in error && error.code === "ENOENT") return empty;
		throw error;
	}
	const journal = journalSchema.parse(JSON.parse(bytes.toString("utf8")));
	if (journal.workspace_id !== workspaceId || journal.task_id !== taskId) throw new Error("Lifecycle scope mismatch");
	let previous: string | null = null;
	for (const [index, record] of journal.records.entries()) {
		const { sha256, ...body } = record;
		if (
			record.sequence !== index + 1 ||
			record.previous_sha256 !== previous ||
			digest(body) !== sha256 ||
			record.state.taskId !== taskId
		)
			throw new Error("Lifecycle journal integrity mismatch");
		previous = sha256;
	}
	return journal;
}
export async function recordTaskLifecycle(
	workspaceId: string,
	source: LifecycleSource,
	summary: RuntimeTaskSessionSummary,
	captureGap: string | null = null,
) {
	const state = lifecycleState(summary);
	sourceSchema.parse(source);
	if (!Number.isFinite(summary.updatedAt)) throw new Error("Invalid producer timestamp");
	const path = journalPath(workspaceId, state.taskId, true);
	if (!path) throw new Error("Lifecycle path unavailable");
	return lockedFileSystem.withLock({ path, type: "file" }, async () => {
		const journal = await readJournal(path, workspaceId, state.taskId);
		const previous = journal.records.at(-1);
		const capture_gap = (captureGap ?? previous?.capture_gap ?? null)?.slice(0, 1024) ?? null;
		if (
			previous &&
			capture_gap === previous.capture_gap &&
			digest({ source: previous.source, state: previous.state }) === digest({ source, state })
		)
			return { replay: true, sequence: previous.sequence, capture_gap };
		if (journal.records.length >= MAX_RECORDS)
			throw new Error("Lifecycle journal at capacity; export before continuing capture");
		const body = {
			sequence: journal.records.length + 1,
			previous_sha256: previous?.sha256 ?? null,
			observed_at: new Date().toISOString(),
			producer_updated_at: summary.updatedAt,
			capture_gap,
			source,
			state,
		};
		journal.records.push({ ...body, sha256: digest(body) });
		const encoded = JSON.stringify(journal);
		if (Buffer.byteLength(encoded) > MAX_BYTES) throw new Error("Lifecycle journal byte capacity reached");
		await lockedFileSystem.writeTextFileAtomic(path, encoded, { lock: null });
		return { replay: false, sequence: body.sequence, capture_gap };
	});
}
export async function readTaskLifecycle(workspaceId: string, taskId: string) {
	const journal = await readJournal(journalPath(workspaceId, taskId, false), workspaceId, taskId);
	return {
		...journal,
		evidence_boundary:
			"Observed native summary transitions only; startedAt is an attempt coordinate, not authenticated desk/session admission. No execution, test success, merge or acceptance verdict is inferred. Capture begins when this observer is attached; prior history is not reconstructed.",
	};
}
