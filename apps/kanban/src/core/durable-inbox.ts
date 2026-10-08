import { createHash } from "node:crypto";
import { z } from "zod";

const identifier = z
	.string()
	.min(1)
	.max(128)
	.regex(/^[A-Za-z0-9][A-Za-z0-9._:-]*$/);
const sha256 = z.string().regex(/^[a-f0-9]{64}$/);

/** A bounded notification about a desknote. It contains no transcript or authority claim. */
export const durableInboxEventSchema = z
	.object({
		schema_version: z.literal("ops.durable-inbox.v1"),
		event_id: identifier,
		tenant_id: identifier,
		workspace_id: identifier,
		kind: z.enum(["desknote", "handoff"]),
		destination_desk_id: identifier,
		recipient_session_ids: z.array(identifier).min(1).max(32),
		sender: z.object({ session_id: identifier, desk_id: identifier.optional() }).strict(),
		created_at: z.string().datetime({ offset: true }),
		desknote: z
			.object({
				reference: z.string().min(1).max(512),
				sha256,
				text: z.string().min(1).max(4000).optional(),
			})
			.strict(),
	})
	.strict()
	.superRefine((event, context) => {
		if (new Set(event.recipient_session_ids).size !== event.recipient_session_ids.length)
			context.addIssue({ code: "custom", path: ["recipient_session_ids"], message: "Recipients must be unique" });
		if (
			event.desknote.text !== undefined &&
			createHash("sha256").update(event.desknote.text, "utf8").digest("hex") !== event.desknote.sha256
		)
			context.addIssue({ code: "custom", path: ["desknote", "sha256"], message: "Text digest mismatch" });
	});

export type DurableInboxEvent = z.infer<typeof durableInboxEventSchema>;

export const durableInboxPendingQuerySchema = z
	.object({
		tenantId: identifier,
		workspaceId: identifier,
		recipientSessionId: identifier,
		limit: z.number().int().min(1).max(100).optional(),
	})
	.strict();
export type DurableInboxPendingQuery = z.infer<typeof durableInboxPendingQuerySchema>;
export interface DurableInboxPendingResult {
	events: DurableInboxEvent[];
	hasMore: boolean;
}

export const durableInboxAckInputSchema = z
	.object({
		tenantId: identifier,
		workspaceId: identifier,
		eventId: identifier,
		recipientSessionId: identifier,
	})
	.strict();
export type DurableInboxAckInput = z.infer<typeof durableInboxAckInputSchema>;

export interface DurableInboxAckResult {
	acknowledged: boolean;
	replay: boolean;
}

/** Local message publication: source references and text hashes are generated, not asserted. */
export const durableInboxMessageSchema = z
	.object({
		message_id: identifier.max(120),
		tenant_id: identifier,
		workspace_id: identifier,
		destination_desk_id: identifier,
		recipient_session_ids: z.array(identifier).min(1).max(32),
		sender: z.object({ session_id: identifier, desk_id: identifier.optional() }).strict(),
		created_at: z.string().datetime({ offset: true }),
		text: z.string().min(1).max(4000),
	})
	.strict();
export type DurableInboxMessage = z.infer<typeof durableInboxMessageSchema>;

export function messageToInboxEvent(input: DurableInboxMessage): DurableInboxEvent {
	const message = durableInboxMessageSchema.parse(input);
	const digest = createHash("sha256").update(message.text, "utf8").digest("hex");
	return durableInboxEventSchema.parse({
		schema_version: "ops.durable-inbox.v1",
		event_id: `message:${message.message_id}`,
		tenant_id: message.tenant_id,
		workspace_id: message.workspace_id,
		kind: "desknote",
		destination_desk_id: message.destination_desk_id,
		recipient_session_ids: [...message.recipient_session_ids].sort(),
		sender: message.sender,
		created_at: message.created_at,
		desknote: { reference: `sha256:${digest}`, sha256: digest, text: message.text },
	});
}
