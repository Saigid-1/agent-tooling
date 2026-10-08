/** Explicit local requirement assertions, never inferred from board/session state. */
import { createHash, randomUUID } from "node:crypto";
import {
	closeSync,
	existsSync,
	fsyncSync,
	linkSync,
	lstatSync,
	mkdirSync,
	openSync,
	readdirSync,
	readFileSync,
	realpathSync,
	unlinkSync,
	writeFileSync,
} from "node:fs";
import { isAbsolute, join } from "node:path";
import { z } from "zod";
import { lockedFileSystem } from "../fs/locked-file-system";
import { listAdrIntakes } from "./adr-intake-store";
import { getConfiguredStorageRoot } from "./runtime-storage-root";
import { loadWorkspaceState } from "./workspace-state";

const digest = z.string().regex(/^[a-f0-9]{64}$/);
export const adrEvidenceInputSchema = z
	.object({
		eventId: z.string().uuid(),
		actor: z
			.object({
				kind: z.enum(["local_operator_assertion", "agent_assertion"]),
				id: z.string().trim().min(1).max(256),
			})
			.strict(),
		requirementId: z.string().regex(/^acceptance:[1-9][0-9]*$/),
		intakeSha256: digest,
		kind: z.enum(["red", "green", "implementation", "merge", "deployment", "acceptance", "amendment_proposal"]),
		outcome: z.enum(["passed", "failed", "accepted", "rejected", "proposed"]),
		rationale: z.string().trim().min(1).max(4000),
		evidence: z
			.array(z.object({ path: z.string().min(1).max(1024), sha256: digest }).strict())
			.min(1)
			.max(20),
		amendment: z.string().trim().min(1).max(16000).optional(),
	})
	.strict()
	.superRefine((value, ctx) => {
		const expected =
			value.kind === "red"
				? ["failed"]
				: value.kind === "acceptance"
					? ["accepted", "rejected"]
					: value.kind === "amendment_proposal"
						? ["proposed"]
						: ["passed", "failed"];
		if (!expected.includes(value.outcome))
			ctx.addIssue({ code: "custom", message: "Evidence kind and outcome disagree" });
		if ((value.kind === "amendment_proposal") !== (value.amendment !== undefined))
			ctx.addIssue({ code: "custom", message: "Only amendment proposals require amendment text" });
	});
export type AdrEvidenceInput = z.infer<typeof adrEvidenceInputSchema>;
const recordSchema = z
	.object({
		id: z.string().uuid(),
		sequence: z.number().int().positive(),
		taskId: z.string(),
		workspaceId: z.string(),
		recordedAt: z.string().datetime(),
		execution: z.object({ intakeSha256: digest, repository: z.string(), baseRef: z.string() }).strict(),
		input: adrEvidenceInputSchema,
	})
	.strict();
export type AdrEvidenceRecord = z.infer<typeof recordSchema>;
function directoryFor(workspaceId: string, taskId: string): string {
	const root = getConfiguredStorageRoot();
	if (!root) throw new Error("Explicit physical KANBAN_STORAGE_ROOT required");
	const directory = join(root, "adr-evidence", createHash("sha256").update(workspaceId).digest("hex"), taskId);
	mkdirSync(directory, { recursive: true, mode: 0o700 });
	if (realpathSync(directory) !== directory) throw new Error("ADR evidence storage must be physical");
	return directory;
}
function readRecords(directory: string, workspaceId: string, taskId: string): AdrEvidenceRecord[] {
	const names = readdirSync(directory).filter((name) => /^[a-f0-9-]+\.json$/.test(name));
	if (names.length > 10000) throw new Error("ADR evidence history at capacity");
	const records = names
		.map((name) => {
			const path = join(directory, name);
			if (!lstatSync(path).isFile() || lstatSync(path).size > 64000) throw new Error("Invalid ADR evidence record");
			const record = recordSchema.parse(JSON.parse(readFileSync(path, "utf8")));
			if (record.workspaceId !== workspaceId || record.taskId !== taskId || name !== `${record.id}.json`)
				throw new Error("ADR evidence identity mismatch");
			return record;
		})
		.sort((a, b) => a.sequence - b.sequence);
	if (records.some((record, index) => record.sequence !== index + 1))
		throw new Error("ADR evidence sequence gap or duplicate");
	return records;
}
async function locate(cwd: string, taskId: string) {
	const intake = (await listAdrIntakes(cwd)).find((item) => item.taskId === taskId);
	if (!intake) throw new Error("Verified ADR intake card required");
	return { ...intake, directory: directoryFor(intake.workspaceId, taskId) };
}
export async function readAdrEvidence(cwd: string, taskId: string): Promise<AdrEvidenceRecord[]> {
	const intake = await locate(cwd, taskId);
	return readRecords(intake.directory, intake.workspaceId, taskId);
}
export async function recordAdrEvidence(
	cwd: string,
	taskId: string,
	input: AdrEvidenceInput,
): Promise<AdrEvidenceRecord> {
	const parsed = adrEvidenceInputSchema.parse(input);
	const intake = await locate(cwd, taskId);
	return lockedFileSystem.withLock({ path: intake.directory, type: "directory" }, async () => {
		const records = readRecords(intake.directory, intake.workspaceId, taskId);
		const prior = records.find((record) => record.input.eventId === parsed.eventId);
		if (prior) {
			if (JSON.stringify(prior.input) !== JSON.stringify(parsed))
				throw new Error("Evidence event ID is sealed with different input");
			return prior;
		}
		const current = (await listAdrIntakes(cwd)).find((item) => item.taskId === taskId);
		if (current?.intakeSha256 !== parsed.intakeSha256)
			throw new Error("Evidence must reference the current intake SHA256");
		const requirementIndex = Number(parsed.requirementId.slice("acceptance:".length)) - 1;
		if (!Number.isSafeInteger(requirementIndex) || !current.intake.acceptance_scenarios[requirementIndex])
			throw new Error("Requirement must identify an acceptance scenario in this intake");
		const board = (await loadWorkspaceState(cwd)).board;
		const card = board.columns.flatMap((column) => column.cards).find((card) => card.id === taskId);
		if (!card?.adrOrigin) throw new Error("Native ADR execution card required");
		const execution = {
			intakeSha256: card.adrOrigin.intakeSha256,
			repository: card.adrOrigin.executionRepository,
			baseRef: card.baseRef,
		};
		if (
			execution.intakeSha256 !== parsed.intakeSha256 &&
			parsed.kind !== "amendment_proposal" &&
			!(parsed.kind === "acceptance" && parsed.outcome === "rejected")
		)
			throw new Error(
				"Documentary intake differs from native execution scope; a new explicitly scoped execution is required",
			);
		for (const ref of parsed.evidence) {
			if (
				!isAbsolute(ref.path) ||
				!existsSync(ref.path) ||
				realpathSync(ref.path) !== ref.path ||
				!lstatSync(ref.path).isFile() ||
				lstatSync(ref.path).size > 8000000
			)
				throw new Error("Evidence must be a bounded physical file");
			if (createHash("sha256").update(readFileSync(ref.path)).digest("hex") !== ref.sha256)
				throw new Error("Evidence digest mismatch");
		}
		const latestTest = records
			.filter(
				(record) =>
					["red", "green"].includes(record.input.kind) &&
					record.input.requirementId === parsed.requirementId &&
					record.input.intakeSha256 === parsed.intakeSha256,
			)
			.at(-1);
		if (
			parsed.kind === "acceptance" &&
			parsed.outcome === "accepted" &&
			(latestTest?.input.kind !== "green" ||
				latestTest.input.outcome !== "passed" ||
				JSON.stringify(latestTest.execution) !== JSON.stringify(execution))
		)
			throw new Error("Acceptance requires linked passing GREEN evidence for this requirement and intake");
		const record: AdrEvidenceRecord = {
			id: randomUUID(),
			sequence: records.length + 1,
			taskId,
			workspaceId: intake.workspaceId,
			recordedAt: new Date().toISOString(),
			execution,
			input: parsed,
		};
		const bytes = JSON.stringify(record);
		if (Buffer.byteLength(bytes) > 64000) throw new Error("ADR evidence record exceeds byte limit");
		if (records.length >= 10000) throw new Error("ADR evidence history at capacity");
		const pending = join(intake.directory, `.pending-${record.id}`);
		const fd = openSync(pending, "wx", 0o600);
		try {
			writeFileSync(fd, bytes);
			fsyncSync(fd);
		} finally {
			closeSync(fd);
		}
		try {
			linkSync(pending, join(intake.directory, `${record.id}.json`));
		} finally {
			unlinkSync(pending);
		}
		const dirFd = openSync(intake.directory, "r");
		try {
			fsyncSync(dirFd);
		} finally {
			closeSync(dirFd);
		}
		return record;
	});
}
