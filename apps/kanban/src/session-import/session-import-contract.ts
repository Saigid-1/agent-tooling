import { z } from "zod";

const absolutePath = z.string().min(1).max(4096).refine((value) => value.startsWith("/"), "An absolute host path is required");
const jobId = z.string().min(1).max(256);

export const sessionImportActionSchema = z.discriminatedUnion("action", [
	z.object({
		action: z.literal("preview"),
		sourceFile: absolutePath,
		nativeSessionId: z.string().min(1).max(256),
		selectedDeskId: z.string().min(1).max(256),
		mode: z.enum(["full", "current-turn-and-forward"]),
	}).strict(),
	z.object({ action: z.literal("desks") }).strict(),
	z.object({ action: z.literal("list-following") }).strict(),
	z.object({ action: z.literal("apply"), planToken: z.string().min(1).max(4096), consent: z.literal(true) }).strict(),
	z.object({
		action: z.literal("assert-owner"),
		jobId,
		selectedDeskId: z.string().min(1).max(256),
		assertedBy: z.string().min(1).max(256),
	}).strict(),
	z.object({ action: z.enum(["status", "continue", "stop", "resume", "follow"]), jobId }).strict(),
]);
export type SessionImportAction = z.infer<typeof sessionImportActionSchema>;

export const sessionImportResultSchema = z.object({
	schema_version: z.literal("ops.session-import.result.v1"),
	status: z.string(),
	code: z.string().optional(),
	message: z.string().optional(),
	plan_token: z.string().optional(),
	next_batch_offset: z.number().optional(),
	job_id: z.string().optional(),
	claim_id: z.string().optional(),
	phase: z.string().optional(),
	worker_active: z.boolean().optional(),
	follower_heartbeat_at: z.string().nullish(),
	index_pending_episodes: z.number().optional(),
	coverage: z.object({
		start_offset: z.number().optional(),
		next_offset: z.number().optional(),
		observed_size: z.number().nullish(),
		reviewed_complete_end: z.number().optional(),
		reviewed_partial_trailing_bytes: z.number().optional(),
		partial_trailing_bytes: z.number().optional(),
		pre_anchor_excluded_bytes: z.number().optional(),
		compaction_policy: z.string().optional(),
		mode: z.string().optional(),
		complete_at_snapshot: z.boolean().optional(),
	}).passthrough().nullish(),
	counts: z.record(z.string(), z.unknown()).optional(),
	desks: z.array(z.object({
		binding_key: z.string(), desk_label: z.string(), role: z.string(), repo_key: z.string(),
	}).passthrough()).optional(),
	jobs: z.array(z.object({ job_id: z.string(), phase: z.string().optional() }).passthrough()).optional(),
	tenant_id: z.string().optional(),
	current_turn_anchor: z.object({
		user_row_offset: z.number(),
		user_row_sha256: z.string(),
		native_session_id: z.string().optional(),
	}).passthrough().nullish(),
	attribution: z.record(z.string(), z.unknown()).optional(),
	cursor: z.unknown().optional(),
	errors: z.array(z.unknown()).optional(),
}).passthrough();
export type SessionImportResult = z.infer<typeof sessionImportResultSchema>;

export const sessionImportSetupSchema = z.object({
	configured: z.boolean(),
	message: z.string(),
	executablePath: z.string().nullable(),
	configPath: z.string().nullable(),
});
export type SessionImportSetup = z.infer<typeof sessionImportSetupSchema>;
