import { z } from "zod";

/** Mirrors the Python registry bounds (desk_registry.py, desk_profiles.py, session_bindings.py). */
export const DESK_REPOS_MAX = 32;
export const DESK_CONTEXT_DOC_BYTES = 16384;
export const ROSTER_ROLES_MAX = 200;
export const sessionBindingSources = ["board", "host", "operator", "import"] as const;
const deskId = z.string().regex(/^desk:[0-9a-f-]{36}$/);
const nativeSessionId = z.string().regex(/^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/);
const boundedFree = (max: number) =>
	z
		.string()
		.min(1)
		.max(max)
		.refine(
			(value) => value.trim() === value && value.length > 0,
			"Use 1 or more characters without surrounding space.",
		);
const contextDoc = z
	.string()
	.refine(
		(value) => new TextEncoder().encode(value).length <= DESK_CONTEXT_DOC_BYTES,
		"The context document exceeds 16 KiB.",
	);

export const deskInputSchema = z
	.object({
		desk_id: deskId,
		name: z.string().trim().min(1).max(120),
		description: z.string().trim().min(1).max(4000),
		role: z.string().min(1).max(128),
		repos: z.array(z.string().trim().min(1).max(512)).max(DESK_REPOS_MAX),
		capture: z.boolean(),
		memory_write: z.boolean(),
		context_doc: contextDoc.nullable().optional(),
		expected_version: z.number().int().nonnegative(),
	})
	.strict();
export const deskBindingSchema = z
	.object({
		binding_key: z.string(),
		tenant_id: z.string(),
		role: z.string(),
		repo_key: z.string(),
		source: z.string(),
	})
	.strict();
export const deskProfileSchema = deskInputSchema.omit({ expected_version: true, context_doc: true }).extend({
	context_doc: z.string().nullable(),
	binding_key: z.string().nullable().optional(),
	binding: deskBindingSchema.optional(),
	tenant_id: z.string(),
	version: z.number().int(),
	recorded_at: z.string(),
	authority: z.string(),
});
export const sessionContextInputSchema = z
	.object({
		source_session_id: z.string().min(1).max(128),
		desk_id: z.string().min(1).max(128),
		repos: z.array(z.string().trim().min(1).max(512)).max(32),
		adrs: z.array(z.string().trim().min(1).max(512)).max(32),
		cards: z.array(z.string().trim().min(1).max(512)).max(32),
		account_ref: z.string().max(512).nullable(),
		provider: z.string().max(512).nullable(),
		model: z.string().max(512).nullable(),
		expected_version: z.number().int().nonnegative(),
		source_ref: z.string().min(1).max(1024),
	})
	.strict();
export const sessionContextSchema = sessionContextInputSchema.omit({ expected_version: true }).extend({
	tenant_id: z.string(),
	role: z.string(),
	version: z.number().int(),
	recorded_at: z.string(),
	authority: z.string(),
});
export const roleSchema = z
	.object({
		role_id: z
			.string()
			.min(1)
			.max(128)
			.refine((value) => value.trim() === value, "Use a role ID without surrounding space."),
		label: z.string().trim().min(1).max(128),
		purpose: z.string().max(1000),
	})
	.strict();
export const roleInputSchema = roleSchema.extend({ expected_version: z.number().int().nonnegative() }).strict();
export const roleRosterSchema = z.object({
	schema_version: z.literal("agent-tooling.role-roster.v1"),
	version: z.number().int().nonnegative(),
	roles: z.array(roleSchema).min(1).max(ROSTER_ROLES_MAX),
	source: z.enum(["default", "configured", "legacy"]),
});
export const sessionBindingInputSchema = z
	.object({
		harness: boundedFree(256),
		provider: boundedFree(256),
		model: boundedFree(256),
		native_session_id: nativeSessionId,
		desk_id: deskId,
		source: z.enum(sessionBindingSources),
		workspace: boundedFree(1024),
		parent_session_id: nativeSessionId.nullable(),
		// Optional parts of the agent-tooling.session-binding.v1 record; the registry sets recorded_at when absent.
		schema_version: z.literal("agent-tooling.session-binding.v1").optional(),
		recorded_at: z.iso.datetime({ offset: true }).max(64).optional(),
	})
	.strict();
export const sessionBindingSchema = sessionBindingInputSchema.extend({
	schema_version: z.literal("agent-tooling.session-binding.v1"),
	recorded_at: z.string(),
	binding_key: z.string().optional(),
});
export const sessionBindingResultSchema = z.object({
	status: z.literal("bound"),
	binding: sessionBindingSchema,
	binding_key: z.string(),
	provider_instance: z.string(),
	idempotent: z.boolean(),
	dispatch: z.literal(false),
});
export const deskRegistrySchema = z.object({
	desks: z.array(deskProfileSchema),
	contexts: z.array(sessionContextSchema),
	roles: z.array(z.object({ role_id: z.string(), label: z.string(), purpose: z.string() })),
	// Implementation metadata beyond the order's directory: the registry may send it; its absence is valid.
	roster_version: z.number().int().nonnegative().optional(),
	roster_source: z.enum(["default", "configured", "legacy"]).optional(),
	registry_mode: z.enum(["registry", "legacy"]).optional(),
	bindings: z.array(sessionBindingSchema).optional(),
	sessions: z.array(z.object({ source_session_id: z.string(), runtime: z.string(), native_id: z.string() })),
	session_limit_reached: z.boolean(),
	tenant_id: z.string(),
	authorization_changed: z.literal(false),
});
export const deskSearchInputSchema = z
	.object({
		query: z.string().min(1).max(256),
		limit: z.number().int().min(1).max(20).default(5),
		view: z
			.object({ kind: z.enum(["agent", "repo", "global"]), value: z.string().min(1).max(512).optional() })
			.strict(),
	})
	.strict();
export const deskSearchResultSchema = z.object({
	index_status: z.string(),
	covered_episodes: z.number(),
	total_episodes: z.number(),
	absence_verdict: z.string(),
	results: z.array(
		z.object({ episode_id: z.string(), event_id: z.string(), quote: z.string(), start: z.number(), end: z.number() }),
	),
});

/** The only request each registry action accepts; anything the contract does not define is refused. */
export const deskRegistryRequestSchemas = {
	list: z.undefined(),
	roles: z.undefined(),
	save: deskInputSchema,
	"save-role": roleInputSchema,
	annotate: sessionContextInputSchema,
	search: deskSearchInputSchema,
	bind: sessionBindingInputSchema,
} as const;
export type DeskRegistryAction = keyof typeof deskRegistryRequestSchemas;
