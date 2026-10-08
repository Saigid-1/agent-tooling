import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { mkdirSync, mkdtempSync, realpathSync, rmSync, symlinkSync, unlinkSync, writeFileSync } from "node:fs";
import { isAbsolute, join } from "node:path";
import { afterAll, beforeAll, expect, it } from "vitest";
import { type DurableInboxEvent, messageToInboxEvent } from "../../src/core/durable-inbox";
import {
	acknowledgeDurableInboxEvent,
	listPendingDurableInboxEvents,
	publishDurableInboxEvent,
} from "../../src/state/durable-inbox-store";
import { loadWorkspaceContext } from "../../src/state/workspace-state";

let root: string;
let repo: string;
let workspaceId: string;
const previousRoot = process.env.KANBAN_STORAGE_ROOT;

beforeAll(async () => {
	const tempRoot = process.env.TMPDIR;
	if (!tempRoot || !isAbsolute(tempRoot) || realpathSync(tempRoot) !== tempRoot)
		throw new Error("Durable inbox tests require an explicit physical TMPDIR");
	root = mkdtempSync(join(tempRoot, "durable-inbox-"));
	repo = join(root, "repo");
	mkdirSync(repo);
	process.env.KANBAN_STORAGE_ROOT = root;
	execFileSync("git", ["init", "-q", repo]);
	writeFileSync(join(repo, "README.md"), "fixture\n");
	execFileSync("git", ["-C", repo, "add", "README.md"]);
	execFileSync("git", [
		"-C",
		repo,
		"-c",
		"user.name=Fixture",
		"-c",
		"user.email=fixture@example.invalid",
		"-c",
		"core.hooksPath=/dev/null",
		"commit",
		"-qm",
		"fixture",
	]);
	workspaceId = (await loadWorkspaceContext(repo)).workspaceId;
});

afterAll(() => {
	if (previousRoot === undefined) delete process.env.KANBAN_STORAGE_ROOT;
	else process.env.KANBAN_STORAGE_ROOT = previousRoot;
	if (root) rmSync(root, { recursive: true, force: true });
});

function event(id: string, recipients = ["session-a", "session-b"]): DurableInboxEvent {
	const text = `Desk note ${id}`;
	return {
		schema_version: "ops.durable-inbox.v1",
		event_id: id,
		tenant_id: "tenant-one",
		workspace_id: workspaceId,
		kind: "desknote",
		destination_desk_id: "desk-one",
		recipient_session_ids: recipients,
		sender: { session_id: "sender-session", desk_id: "sender-desk" },
		created_at: "2026-09-24T12:00:00Z",
		desknote: { reference: `note:${id}`, sha256: createHash("sha256").update(text).digest("hex"), text },
	};
}

it("seals a concurrent duplicate once and refuses a changed payload", async () => {
	const input = event("concurrent");
	const results = await Promise.all([publishDurableInboxEvent(repo, input), publishDurableInboxEvent(repo, input)]);
	expect(results.map((result) => result.replay).sort()).toEqual([false, true]);
	expect(
		(
			await listPendingDurableInboxEvents(repo, {
				tenantId: input.tenant_id,
				workspaceId,
				recipientSessionId: "session-a",
			})
		).events.filter((item) => item.event_id === input.event_id),
	).toHaveLength(1);
	await expect(publishDurableInboxEvent(repo, { ...input, destination_desk_id: "desk-two" })).rejects.toThrow(
		/sealed/,
	);
});

it("replays by exact recipient and persists independent acknowledgements", async () => {
	const input = event("receipts");
	await publishDurableInboxEvent(repo, input);
	const scope = { tenantId: "tenant-one", workspaceId, eventId: input.event_id };
	expect(await acknowledgeDurableInboxEvent(repo, { ...scope, recipientSessionId: "session-a" })).toEqual({
		acknowledged: true,
		replay: false,
	});
	expect(await acknowledgeDurableInboxEvent(repo, { ...scope, recipientSessionId: "session-a" })).toEqual({
		acknowledged: true,
		replay: true,
	});
	expect(
		(
			await listPendingDurableInboxEvents(repo, {
				tenantId: scope.tenantId,
				workspaceId,
				recipientSessionId: "session-a",
			})
		).events,
	).not.toContainEqual(input);
	expect(
		(
			await listPendingDurableInboxEvents(repo, {
				tenantId: scope.tenantId,
				workspaceId,
				recipientSessionId: "session-b",
			})
		).events,
	).toContainEqual(input);
	expect(
		(
			await listPendingDurableInboxEvents(repo, {
				tenantId: "tenant-two",
				workspaceId,
				recipientSessionId: "session-b",
			})
		).events,
	).toEqual([]);
	await expect(acknowledgeDurableInboxEvent(repo, { ...scope, recipientSessionId: "stranger" })).rejects.toThrow(
		/not a recipient/,
	);
});

it("rejects invalid content and cross-workspace events", async () => {
	await expect(
		publishDurableInboxEvent(repo, {
			...event("bad-digest"),
			desknote: { reference: "note:bad", sha256: "a".repeat(64), text: "wrong" },
		}),
	).rejects.toThrow(/Text digest mismatch/);
	await expect(
		publishDurableInboxEvent(repo, { ...event("wrong-workspace"), workspace_id: "elsewhere" }),
	).rejects.toThrow(/workspace mismatch/);
	await expect(
		publishDurableInboxEvent(repo, event("duplicate-recipients", ["session-a", "session-a"])),
	).rejects.toThrow(/Recipients must be unique/);
});

it("keeps both receipts when two recipients acknowledge concurrently", async () => {
	const input = event("parallel-receipts", ["parallel-a", "parallel-b"]);
	await publishDurableInboxEvent(repo, input);
	const scope = { tenantId: input.tenant_id, workspaceId, eventId: input.event_id };
	const receipts = await Promise.all([
		acknowledgeDurableInboxEvent(repo, { ...scope, recipientSessionId: "parallel-a" }),
		acknowledgeDurableInboxEvent(repo, { ...scope, recipientSessionId: "parallel-b" }),
	]);
	expect(receipts.every((receipt) => receipt.acknowledged)).toBe(true);
	for (const recipientSessionId of input.recipient_session_ids) {
		expect(
			(
				await listPendingDurableInboxEvents(repo, {
					tenantId: input.tenant_id,
					workspaceId,
					recipientSessionId,
				})
			).events,
		).not.toContainEqual(input);
	}
});

it("signals when a bounded replay has more events", async () => {
	for (let index = 0; index < 3; index += 1)
		await publishDurableInboxEvent(repo, event(`page-${index}`, ["page-session"]));
	const result = await listPendingDurableInboxEvents(repo, {
		tenantId: "tenant-one",
		workspaceId,
		recipientSessionId: "page-session",
		limit: 2,
	});
	expect(result.events.map((item) => item.event_id)).toEqual(["page-0", "page-1"]);
	expect(result.hasMore).toBe(true);
	await acknowledgeDurableInboxEvent(repo, {
		tenantId: "tenant-one",
		workspaceId,
		recipientSessionId: "page-session",
		eventId: "page-0",
	});
	expect(
		(
			await listPendingDurableInboxEvents(repo, {
				tenantId: "tenant-one",
				workspaceId,
				recipientSessionId: "page-session",
				limit: 2,
			})
		).hasMore,
	).toBe(false);
});

function namespaceFile(tenantId: string): string {
	const hash = (value: string) => createHash("sha256").update(value).digest("hex");
	return join(root, "durable-inbox", "v1", hash(tenantId), hash(workspaceId), "inbox.json");
}

it("refuses a symlinked namespace file", async () => {
	const input = { ...event("linked-file"), tenant_id: "tenant-symlink" };
	await publishDurableInboxEvent(repo, input);
	const path = namespaceFile(input.tenant_id);
	unlinkSync(path);
	symlinkSync(join(repo, "README.md"), path);
	await expect(
		listPendingDurableInboxEvents(repo, {
			tenantId: input.tenant_id,
			workspaceId,
			recipientSessionId: "session-a",
		}),
	).rejects.toThrow();
});

it("refuses an oversized namespace before parsing", async () => {
	const input = { ...event("large-file"), tenant_id: "tenant-oversized" };
	await publishDurableInboxEvent(repo, input);
	writeFileSync(namespaceFile(input.tenant_id), Buffer.alloc(4 * 1024 * 1024 + 1));
	await expect(
		listPendingDurableInboxEvents(repo, {
			tenantId: input.tenant_id,
			workspaceId,
			recipientSessionId: "session-a",
		}),
	).rejects.toThrow(/bounded regular file/);
});

it("publishes standalone messages without a graph, normalizes recipient order and seals content", async () => {
	const input = {
		message_id: "independent-note",
		tenant_id: "tenant-one",
		workspace_id: workspaceId,
		destination_desk_id: "desk-one",
		recipient_session_ids: ["session-b", "session-a"],
		sender: { session_id: "sender-session" },
		created_at: "2026-09-24T12:00:00Z",
		text: "Exact Unicode note: café 📨\n",
	};
	const event = messageToInboxEvent(input);
	expect(event.desknote.sha256).toBe(createHash("sha256").update(input.text, "utf8").digest("hex"));
	expect(event.desknote.reference).toBe(`sha256:${event.desknote.sha256}`);
	expect((await publishDurableInboxEvent(repo, event)).replay).toBe(false);
	expect(
		(
			await publishDurableInboxEvent(
				repo,
				messageToInboxEvent({ ...input, recipient_session_ids: ["session-a", "session-b"] }),
			)
		).replay,
	).toBe(true);
	await expect(publishDurableInboxEvent(repo, messageToInboxEvent({ ...input, text: "Changed" }))).rejects.toThrow(
		"sealed",
	);
	expect(() => messageToInboxEvent({ ...input, recipient_session_ids: ["session-a", "session-a"] })).toThrow("unique");
});
