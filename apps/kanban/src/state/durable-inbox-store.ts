import { createHash, randomUUID } from "node:crypto";
import { constants, existsSync, lstatSync, mkdirSync, realpathSync } from "node:fs";
import { open, rename, rm } from "node:fs/promises";
import { dirname, join } from "node:path";
import { z } from "zod";
import {
	type DurableInboxAckInput,
	type DurableInboxAckResult,
	type DurableInboxEvent,
	type DurableInboxPendingQuery,
	type DurableInboxPendingResult,
	durableInboxAckInputSchema,
	durableInboxEventSchema,
	durableInboxPendingQuerySchema,
} from "../core/durable-inbox";
import { lockedFileSystem } from "../fs/locked-file-system";
import { getConfiguredStorageRoot } from "./runtime-storage-root";
import { loadWorkspaceContext } from "./workspace-state";

const MAX_NAMESPACE_BYTES = 4 * 1024 * 1024;
const MAX_EVENTS = 512;
const MAX_EVENT_BYTES = 8192;

const recordSchema = z
	.object({
		event: durableInboxEventSchema,
		acknowledged_session_ids: z.array(z.string()).max(32),
	})
	.strict()
	.superRefine((record, context) => {
		if (
			new Set(record.acknowledged_session_ids).size !== record.acknowledged_session_ids.length ||
			record.acknowledged_session_ids.some((id) => !record.event.recipient_session_ids.includes(id))
		)
			context.addIssue({ code: "custom", path: ["acknowledged_session_ids"], message: "Invalid recipient receipt" });
	});
const namespaceSchema = z
	.object({
		version: z.literal(1),
		tenant_id: z.string(),
		workspace_id: z.string(),
		records: z.array(recordSchema).max(MAX_EVENTS),
	})
	.strict();
type Namespace = z.infer<typeof namespaceSchema>;

function hashKey(value: string): string {
	return createHash("sha256").update(value).digest("hex");
}

function physicalDirectory(path: string): void {
	if (!lstatSync(path).isDirectory() || realpathSync(path) !== path)
		throw new Error(`Durable inbox storage must be a physical directory: ${path}`);
}

function namespacePath(root: string, tenantId: string, workspaceId: string, create: boolean): string | null {
	let current = root;
	for (const part of ["durable-inbox", "v1", hashKey(tenantId), hashKey(workspaceId)]) {
		current = join(current, part);
		if (!existsSync(current)) {
			if (!create) return null;
			try {
				mkdirSync(current, { mode: 0o700 });
			} catch (error) {
				if (typeof error !== "object" || error === null || !("code" in error) || error.code !== "EEXIST")
					throw error;
			}
		}
		physicalDirectory(current);
	}
	return join(current, "inbox.json");
}

async function readNamespace(path: string, tenantId: string, workspaceId: string): Promise<Namespace> {
	let bytes: Buffer;
	try {
		const file = await open(path, constants.O_RDONLY | constants.O_NOFOLLOW);
		try {
			const info = await file.stat();
			if (!info.isFile() || info.size > MAX_NAMESPACE_BYTES)
				throw new Error("Durable inbox namespace is not a bounded regular file");
			const bounded = Buffer.alloc(MAX_NAMESPACE_BYTES + 1);
			let length = 0;
			while (length < bounded.length) {
				const { bytesRead } = await file.read(bounded, length, bounded.length - length, null);
				if (bytesRead === 0) break;
				length += bytesRead;
			}
			if (length > MAX_NAMESPACE_BYTES) throw new Error("Durable inbox namespace exceeds its byte limit");
			bytes = bounded.subarray(0, length);
		} finally {
			await file.close();
		}
	} catch (error) {
		if (typeof error === "object" && error !== null && "code" in error && error.code === "ENOENT")
			return { version: 1, tenant_id: tenantId, workspace_id: workspaceId, records: [] };
		throw error;
	}
	const parsed = namespaceSchema.parse(JSON.parse(bytes.toString("utf8")) as unknown);
	if (parsed.tenant_id !== tenantId || parsed.workspace_id !== workspaceId)
		throw new Error("Durable inbox namespace scope mismatch");
	const ids = new Set<string>();
	for (const record of parsed.records) {
		if (record.event.tenant_id !== tenantId || record.event.workspace_id !== workspaceId)
			throw new Error("Durable inbox event scope mismatch");
		if (ids.has(record.event.event_id)) throw new Error("Duplicate durable inbox event ID in stored namespace");
		ids.add(record.event.event_id);
	}
	return parsed;
}

async function writeNamespace(path: string, namespace: Namespace): Promise<void> {
	const bytes = Buffer.byteLength(JSON.stringify(namespace, null, 2));
	if (bytes > MAX_NAMESPACE_BYTES) throw new Error("Durable inbox namespace is at capacity");
	// Caller holds the namespace lock. Flush bytes before making the new snapshot visible.
	const tempPath = `${path}.tmp.${randomUUID()}`;
	try {
		const file = await open(tempPath, constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL, 0o600);
		try {
			await file.writeFile(JSON.stringify(namespace, null, 2), "utf8");
			await file.sync();
		} finally {
			await file.close();
		}
		await rename(tempPath, path);
		const directory = await open(dirname(path), constants.O_RDONLY);
		try {
			await directory.sync();
		} finally {
			await directory.close();
		}
	} finally {
		await rm(tempPath, { force: true });
	}
}

async function verifyWorkspace(cwd: string, workspaceId: string): Promise<void> {
	const context = await loadWorkspaceContext(cwd, { autoCreateIfMissing: false });
	if (context.workspaceId !== workspaceId) throw new Error("Durable inbox workspace mismatch");
}

function storageRoot(): string {
	const root = getConfiguredStorageRoot();
	if (!root) throw new Error("Explicit physical KANBAN_STORAGE_ROOT required for durable inbox");
	return root;
}

/** Persist before transport notification. Repeating the same ID and payload is a no-op. */
export async function publishDurableInboxEvent(
	cwd: string,
	input: DurableInboxEvent,
): Promise<{ event: DurableInboxEvent; replay: boolean }> {
	const event = durableInboxEventSchema.parse(input);
	if (Buffer.byteLength(JSON.stringify(event)) > MAX_EVENT_BYTES)
		throw new Error("Durable inbox event exceeds its byte limit");
	await verifyWorkspace(cwd, event.workspace_id);
	const path = namespacePath(storageRoot(), event.tenant_id, event.workspace_id, true);
	if (!path) throw new Error("Durable inbox namespace path unavailable");
	return await lockedFileSystem.withLock({ path, type: "file" }, async () => {
		const namespace = await readNamespace(path, event.tenant_id, event.workspace_id);
		const prior = namespace.records.find((record) => record.event.event_id === event.event_id);
		if (prior) {
			if (JSON.stringify(prior.event) !== JSON.stringify(event))
				throw new Error("Durable inbox event ID is sealed with a different payload");
			return { event: prior.event, replay: true };
		}
		if (namespace.records.length >= MAX_EVENTS) throw new Error("Durable inbox namespace is at capacity");
		namespace.records.push({ event, acknowledged_session_ids: [] });
		await writeNamespace(path, namespace);
		return { event, replay: false };
	});
}

/** Returns bounded pending events for one exact recipient, including after restart. */
export async function listPendingDurableInboxEvents(
	cwd: string,
	input: DurableInboxPendingQuery,
): Promise<DurableInboxPendingResult> {
	const query = durableInboxPendingQuerySchema.parse(input);
	await verifyWorkspace(cwd, query.workspaceId);
	const path = namespacePath(storageRoot(), query.tenantId, query.workspaceId, false);
	if (!path) return { events: [], hasMore: false };
	const namespace = await readNamespace(path, query.tenantId, query.workspaceId);
	const pending = namespace.records
		.filter(
			(record) =>
				record.event.recipient_session_ids.includes(query.recipientSessionId) &&
				!record.acknowledged_session_ids.includes(query.recipientSessionId),
		)
		.map((record) => record.event)
		.sort(
			(left, right) =>
				left.created_at.localeCompare(right.created_at) || left.event_id.localeCompare(right.event_id),
		);
	const limit = query.limit ?? 100;
	return { events: pending.slice(0, limit), hasMore: pending.length > limit };
}

/** Consumer receipt only; does not assert model read, session liveness, or authorization. */
export async function acknowledgeDurableInboxEvent(
	cwd: string,
	input: DurableInboxAckInput,
): Promise<DurableInboxAckResult> {
	const ack = durableInboxAckInputSchema.parse(input);
	await verifyWorkspace(cwd, ack.workspaceId);
	const path = namespacePath(storageRoot(), ack.tenantId, ack.workspaceId, false);
	if (!path) return { acknowledged: false, replay: false };
	return await lockedFileSystem.withLock({ path, type: "file" }, async () => {
		const namespace = await readNamespace(path, ack.tenantId, ack.workspaceId);
		const record = namespace.records.find((item) => item.event.event_id === ack.eventId);
		if (!record) return { acknowledged: false, replay: false };
		if (!record.event.recipient_session_ids.includes(ack.recipientSessionId))
			throw new Error("Session is not a recipient of this durable inbox event");
		if (record.acknowledged_session_ids.includes(ack.recipientSessionId)) return { acknowledged: true, replay: true };
		record.acknowledged_session_ids.push(ack.recipientSessionId);
		await writeNamespace(path, namespace);
		return { acknowledged: true, replay: false };
	});
}
