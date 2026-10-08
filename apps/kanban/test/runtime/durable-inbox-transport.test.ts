import { createServer } from "node:http";
import type { AddressInfo } from "node:net";
import { afterEach, describe, expect, it } from "vitest";
import { WebSocket } from "ws";
import type { DurableInboxEvent, DurableInboxPendingResult } from "../../src/core/durable-inbox";
import { createRuntimeStateHub, type RuntimeStateHub } from "../../src/server/runtime-state-hub";

const workspaceId = "workspace-a";
const workspacePath = "/missing-inbox-test-workspace";
const event: DurableInboxEvent = {
	schema_version: "ops.durable-inbox.v1",
	event_id: "event-1",
	tenant_id: "tenant-a",
	workspace_id: workspaceId,
	kind: "desknote",
	destination_desk_id: "desk-a",
	recipient_session_ids: ["session-a"],
	sender: { session_id: "sender-a" },
	created_at: "2026-09-24T00:00:00.000Z",
	desknote: { reference: "note", sha256: "0".repeat(64) },
};

let server: ReturnType<typeof createServer> | undefined;
let hub: RuntimeStateHub | undefined;
const clients: WebSocket[] = [];

afterEach(async () => {
	for (const client of clients.splice(0)) client.close();
	if (hub) await hub.close();
	hub = undefined;
	if (server) await new Promise<void>((resolve) => server?.close(() => resolve()));
	server = undefined;
});

async function openClient(): Promise<{ client: WebSocket; messages: Array<Record<string, unknown>> }> {
	if (!server) throw new Error("Test server missing");
	const port = (server.address() as AddressInfo).port;
	const client = new WebSocket(`ws://127.0.0.1:${port}/api/runtime/ws?workspaceId=${workspaceId}`);
	clients.push(client);
	const messages: Array<Record<string, unknown>> = [];
	client.on("message", (bytes) => messages.push(JSON.parse(bytes.toString()) as Record<string, unknown>));
	await new Promise<void>((resolve, reject) => {
		client.once("open", resolve);
		client.once("error", reject);
	});
	return { client, messages };
}

async function until(messages: Array<Record<string, unknown>>, type: string): Promise<void> {
	const deadline = Date.now() + 3000;
	while (!messages.some((message) => message.type === type)) {
		if (Date.now() > deadline) throw new Error(`Timed out waiting for ${type}: ${JSON.stringify(messages)}`);
		await new Promise((resolve) => setTimeout(resolve, 5));
	}
}
async function untilCount(messages: Array<Record<string, unknown>>, type: string, count: number): Promise<void> {
	const deadline = Date.now() + 3000;
	while (messages.filter((message) => message.type === type).length < count) {
		if (Date.now() > deadline) throw new Error(`Timed out waiting for ${count} ${type} messages`);
		await new Promise((resolve) => setTimeout(resolve, 5));
	}
}

describe("durable inbox runtime stream", () => {
	it("replays to one exact recipient, reports the page boundary, and isolates live fanout", async () => {
		const queries: unknown[] = [];
		hub = createRuntimeStateHub({
			workspaceRegistry: {
				resolveWorkspaceForStream: async () => ({
					workspaceId,
					workspacePath,
					removedRequestedWorkspacePath: null,
					didPruneProjects: false,
				}),
				buildProjectsPayload: async () => ({ currentProjectId: workspaceId, projects: [] }),
				buildWorkspaceStateSnapshot: async () => ({ board: { columns: [] } }) as never,
			},
			listPendingInboxEvents: async (_path, query): Promise<DurableInboxPendingResult> => {
				queries.push(query);
				return query.recipientSessionId === "session-a"
					? { events: [event], hasMore: true }
					: { events: [], hasMore: false };
			},
		});
		server = createServer();
		server.on("upgrade", (request, socket, head) =>
			hub?.handleUpgrade(request, socket, head, { requestedWorkspaceId: workspaceId }),
		);
		await new Promise<void>((resolve) => server?.listen(0, "127.0.0.1", resolve));
		const a = await openClient();
		const b = await openClient();
		await until(a.messages, "snapshot");
		await until(b.messages, "snapshot");
		a.client.send(
			JSON.stringify({
				type: "inbox_subscribe",
				tenantId: "tenant-a",
				workspaceId,
				recipientSessionId: "session-a",
			}),
		);
		b.client.send(
			JSON.stringify({
				type: "inbox_subscribe",
				tenantId: "tenant-a",
				workspaceId,
				recipientSessionId: "session-b",
			}),
		);
		await until(a.messages, "inbox_subscribed");
		await until(b.messages, "inbox_subscribed");
		expect(queries).toContainEqual({ tenantId: "tenant-a", workspaceId, recipientSessionId: "session-a" });
		expect(a.messages.find((message) => message.type === "inbox_subscribed")).toMatchObject({
			replayed_count: 1,
			has_more: true,
		});
		expect(b.messages.find((message) => message.type === "inbox_subscribed")).toMatchObject({
			replayed_count: 0,
			has_more: false,
		});
		expect(b.messages.filter((message) => message.type === "inbox_event")).toHaveLength(0);
		hub.broadcastInboxEvent(event);
		await untilCount(a.messages, "inbox_event", 2);
		expect(a.messages.filter((message) => message.type === "inbox_event")).toHaveLength(2);
		expect(b.messages.filter((message) => message.type === "inbox_event")).toHaveLength(0);
		a.client.send(
			JSON.stringify({
				type: "inbox_subscribe",
				tenantId: "tenant-a",
				workspaceId,
				recipientSessionId: "session-b",
			}),
		);
		await untilCount(a.messages, "inbox_subscribed", 2);
		hub.broadcastInboxEvent(event);
		await new Promise((resolve) => setTimeout(resolve, 20));
		expect(a.messages.filter((message) => message.type === "inbox_event")).toHaveLength(2);
	});
	it("does not deliver a delayed replay after the subscription switches", async () => {
		let releaseOld: ((result: DurableInboxPendingResult) => void) | undefined;
		const oldPending = new Promise<DurableInboxPendingResult>((resolve) => {
			releaseOld = resolve;
		});
		hub = createRuntimeStateHub({
			workspaceRegistry: {
				resolveWorkspaceForStream: async () => ({
					workspaceId,
					workspacePath,
					removedRequestedWorkspacePath: null,
					didPruneProjects: false,
				}),
				buildProjectsPayload: async () => ({ currentProjectId: workspaceId, projects: [] }),
				buildWorkspaceStateSnapshot: async () => ({ board: { columns: [] } }) as never,
			},
			listPendingInboxEvents: async (_path, query) =>
				query.recipientSessionId === "session-a" ? await oldPending : { events: [], hasMore: false },
		});
		server = createServer();
		server.on("upgrade", (request, socket, head) =>
			hub?.handleUpgrade(request, socket, head, { requestedWorkspaceId: workspaceId }),
		);
		await new Promise<void>((resolve) => server?.listen(0, "127.0.0.1", resolve));
		const { client, messages } = await openClient();
		await until(messages, "snapshot");
		client.send(
			JSON.stringify({
				type: "inbox_subscribe",
				tenantId: "tenant-a",
				workspaceId,
				recipientSessionId: "session-a",
			}),
		);
		client.send(
			JSON.stringify({
				type: "inbox_subscribe",
				tenantId: "tenant-a",
				workspaceId,
				recipientSessionId: "session-b",
			}),
		);
		await until(messages, "inbox_subscribed");
		expect(messages.find((message) => message.type === "inbox_subscribed")).toMatchObject({
			recipientSessionId: "session-b",
		});
		releaseOld?.({ events: [event], hasMore: false });
		await new Promise((resolve) => setTimeout(resolve, 20));
		expect(messages.filter((message) => message.type === "inbox_event")).toHaveLength(0);
		const eventForB = { ...event, recipient_session_ids: ["session-b"] };
		client.send("{");
		await until(messages, "error");
		hub.broadcastInboxEvent(eventForB);
		await new Promise((resolve) => setTimeout(resolve, 20));
		expect(messages.filter((message) => message.type === "inbox_event")).toHaveLength(0);
		client.send(
			JSON.stringify({
				type: "inbox_subscribe",
				tenantId: "tenant-a",
				workspaceId,
				recipientSessionId: "session-b",
			}),
		);
		await untilCount(messages, "inbox_subscribed", 2);
		client.send(
			JSON.stringify({
				type: "inbox_subscribe",
				tenantId: "tenant-a",
				workspaceId: "other",
				recipientSessionId: "session-a",
			}),
		);
		await untilCount(messages, "error", 2);
		hub.broadcastInboxEvent(eventForB);
		await new Promise((resolve) => setTimeout(resolve, 20));
		expect(messages.filter((message) => message.type === "inbox_event")).toHaveLength(0);
	});
});
