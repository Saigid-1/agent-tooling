/** The approved JSON Schema is the validation authority; retain original bytes separately. */
import { createHash } from "node:crypto";
import { Ajv2020 } from "ajv/dist/2020.js";
import addFormats from "ajv-formats";
import approvedSchema from "../../../../docs/work/adr-backlog-intake-v1.2.schema.json";
import type { RuntimeBoardData } from "./api-contract";
import { addTaskDependency, getTaskColumnId, removeTaskDependency } from "./task-board-mutations";
import { observationCardId } from "./work-observation";

export interface AdrIntake {
	schema_version: "ops.adr-backlog-intake.v1.2";
	work_id: string;
	initiative_id: string;
	slice_id: string;
	title: string;
	owner: { repository: string; role: string };
	adr: {
		id: string;
		source: { repository: string; path: string; revision: string | null; sha256: string; origin: string };
		declared_status: string;
		derived_status: string | null;
		verification: string;
		reason: string;
	};
	acceptance_scenarios: { given: string; when: string; then: string }[];
	entry_point: string;
	requester_outcome: string;
	dependencies: { work_id: string; relationship: "requires"; basis: "source_explicit" | "proposed"; reason: string }[];
	unresolved: string[];
	dispatch: { state: "not_authorized" };
}
const validator = new Ajv2020({ strict: false, allErrors: true });
addFormats(validator);
const validate = validator.compile<AdrIntake>(approvedSchema);
export function parseAdrIntake(bytes: Buffer): AdrIntake {
	if (bytes.length > 1_000_000) throw new Error("Intake exceeds 1000000 bytes");
	const value: unknown = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes));
	if (!validate(value)) throw new Error(`Invalid ADR intake: ${validator.errorsText(validate.errors)}`);
	if (value.work_id !== value.work_id.trim() || value.owner.repository !== value.owner.repository.trim())
		throw new Error("Work identity must not have surrounding whitespace");
	if (new Set(value.dependencies.map((d) => d.work_id)).size !== value.dependencies.length)
		throw new Error("Duplicate dependency work identity");
	return value;
}
export function intakeDigest(bytes: Buffer): string {
	return createHash("sha256").update(bytes).digest("hex");
}
/** Repository-qualified identity is unambiguous and independent of the input file or title. */
export function intakeWorkId(repository: string, workId: string): string {
	return `adr:${JSON.stringify([repository, workId])}`;
}
export function nativeIntakeCardId(identity: { workspace_id: string; work_id: string }): string {
	return observationCardId(identity).replace(/^obs_/, "adr_");
}
export function intakeSummary(intake: AdrIntake, sha256: string, supersedes: string | null): string {
	const text = [
		`ADR INTAKE — dispatch: not_authorized`,
		`Owner: ${intake.owner.repository} / ${intake.owner.role}`,
		`Initiative: ${intake.initiative_id}; slice: ${intake.slice_id}`,
		`Source: ${intake.adr.source.repository}:${intake.adr.source.path}@${intake.adr.source.revision ?? "external review package"}`,
		`Source SHA256: ${intake.adr.source.sha256}`,
		`Intake SHA256: ${sha256}; supersedes: ${supersedes ?? "none"}`,
		`ADR ${intake.adr.id}: declared ${intake.adr.declared_status}; derived ${intake.adr.derived_status ?? "unknown"}; ${intake.adr.verification}`,
		`Verification reason: ${intake.adr.reason}`,
		`Entry point: ${intake.entry_point}`,
		`Outcome: ${intake.requester_outcome}`,
		...intake.unresolved.map((item) => `Unresolved: ${item}`),
		...intake.dependencies.map((item) => `Requires ${item.work_id} (${item.basis}): ${item.reason}`),
		"Full citations, derivation, acceptance scenarios, reuse assessment and predictions are retained in the linked intake evidence.",
	].join("\n");
	return text.length <= 8000
		? text
		: `${text.slice(0, 7900)}\n[Summary truncated; consult the complete intake evidence.]`;
}
function dependencyId(cardId: string, target: string): string {
	return `adrdep_${createHash("sha256")
		.update(JSON.stringify([cardId, target]))
		.digest("hex")
		.slice(0, 32)}`;
}
/** Native arrows are display links only. Proposed requirements remain in the retained intake. */
export function projectIntakeDependencies(
	board: RuntimeBoardData,
	workspaceId: string,
	intake: AdrIntake,
	resolveDependency: (workId: string) => string,
): RuntimeBoardData {
	const cardId = nativeIntakeCardId({
		workspace_id: workspaceId,
		work_id: intakeWorkId(intake.owner.repository, intake.work_id),
	});
	if (getTaskColumnId(board, cardId) !== "backlog") throw new Error("Intake projection supports backlog cards only");
	let result = board;
	for (const edge of board.dependencies) {
		if (edge.fromTaskId === cardId && edge.id === dependencyId(cardId, edge.toTaskId))
			result = removeTaskDependency(result, edge.id).board;
	}
	for (const dependency of intake.dependencies.filter((item) => item.basis === "source_explicit")) {
		const target = resolveDependency(dependency.work_id);
		if (target === cardId) throw new Error("Intake cannot depend on itself");
		if (getTaskColumnId(result, target) !== "backlog")
			throw new Error(`Dependency must be an existing backlog intake card: ${dependency.work_id}`);
		const reachable = [target];
		const seen = new Set<string>();
		while (reachable.length) {
			const current = reachable.pop();
			if (!current || seen.has(current)) continue;
			if (current === cardId) throw new Error("Intake dependency cycle");
			seen.add(current);
			reachable.push(
				...result.dependencies.filter((edge) => edge.fromTaskId === current).map((edge) => edge.toTaskId),
			);
		}
		const added = addTaskDependency(result, cardId, target);
		if (added.added && added.dependency) {
			result = {
				...added.board,
				dependencies: added.board.dependencies.map((edge) =>
					edge.id === added.dependency?.id
						? {
								...edge,
								id: dependencyId(cardId, target),
								createdAt:
									board.dependencies.find((prior) => prior.id === dependencyId(cardId, target))?.createdAt ??
									edge.createdAt,
							}
						: edge,
				),
			};
		} else if (added.reason !== "duplicate") throw new Error(`Cannot project dependency: ${added.reason}`);
	}
	// Replaying one card must not reorder its unchanged arrows relative to other cards.
	const priorOrder = new Map(board.dependencies.map((edge, index) => [edge.id, index]));
	return {
		...result,
		dependencies: [...result.dependencies].sort(
			(a, b) =>
				(priorOrder.get(a.id) ?? Number.MAX_SAFE_INTEGER) - (priorOrder.get(b.id) ?? Number.MAX_SAFE_INTEGER),
		),
	};
}
