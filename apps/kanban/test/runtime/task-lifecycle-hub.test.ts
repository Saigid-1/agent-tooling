import { expect, it, vi } from "vitest";
import type { ClineTaskSessionService } from "../../src/cline-sdk/cline-task-session-service";
import type { RuntimeTaskSessionSummary } from "../../src/core/api-contract";
import { createRuntimeStateHub } from "../../src/server/runtime-state-hub";
import { createTaskLifecycleObserver } from "../../src/server/task-lifecycle-observer";
import type { TerminalSessionManager } from "../../src/terminal/session-manager";

const summary = (updatedAt: number): RuntimeTaskSessionSummary => ({
	taskId: "task",
	state: updatedAt % 2 ? "running" : "awaiting_review",
	agentId: "cline",
	workspacePath: "/fixture",
	pid: null,
	startedAt: 1,
	updatedAt,
	lastOutputAt: null,
	reviewReason: null,
	exitCode: null,
	lastHookAt: null,
	latestHookActivity: null,
});

it("processes a transition emitted synchronously from overflow delivery", async () => {
	const persisted: number[] = [];
	const delivered: number[] = [];
	const observer = createTaskLifecycleObserver(
		async (_workspace, _source, value, gap) => {
			persisted.push(value.updatedAt);
			return { replay: false, sequence: persisted.length, capture_gap: gap ?? null };
		},
		(_workspace, value) => {
			delivered.push(value.updatedAt);
			if (value.updatedAt === 299) observer.observe("workspace", "cline", summary(300));
		},
	);
	for (let i = 0; i < 300; i++) observer.observe("workspace", "cline", summary(i));
	await observer.drain();
	expect(delivered.slice(-2)).toEqual([299, 300]);
	expect(persisted.at(-1)).toBe(300);
	expect(persisted).toHaveLength(257);
	observer.close();
});

it("unsubscribes all producers before draining despite a throwing unsubscribe", async () => {
	const order: string[] = [];
	let release: (() => void) | undefined;
	const gate = new Promise<void>((resolve) => {
		release = resolve;
	});
	const lifecycle = {
		observe: vi.fn(),
		close: () => {
			order.push("observer-close");
		},
		drain: async () => {
			order.push("drain");
			await gate;
			order.push("drained");
		},
	};
	const hub = createRuntimeStateHub({
		lifecycleObserver: lifecycle,
		workspaceRegistry: {
			resolveWorkspaceForStream: async () => ({
				workspaceId: "workspace",
				workspacePath: "/fixture",
				removedRequestedWorkspacePath: null,
				didPruneProjects: false,
			}),
			buildProjectsPayload: async () => ({ currentProjectId: "workspace", projects: [] }),
			buildWorkspaceStateSnapshot: async () => {
				throw new Error("No snapshot expected");
			},
		},
	});
	const terminal = {
		listSummaries: () => [summary(2)],
		onSummary: () => () => {
			order.push("terminal-unsubscribe");
			throw new Error("unsubscribe failure");
		},
	} as unknown as TerminalSessionManager;
	const cline = {
		listSummaries: () => [],
		onSummary: () => () => {
			order.push("cline-unsubscribe");
		},
		onMessage: () => () => {
			order.push("message-unsubscribe");
		},
	} as unknown as ClineTaskSessionService;
	hub.trackTerminalManager("workspace", terminal);
	expect(lifecycle.observe).toHaveBeenCalledWith("workspace", "terminal", summary(2));
	hub.trackClineTaskSessionService("workspace", "/fixture", cline);
	const closing = hub.close();
	expect(order).toEqual(["observer-close", "terminal-unsubscribe", "cline-unsubscribe", "drain"]);
	const lateSubscribe = vi.fn();
	hub.trackTerminalManager("late", { onSummary: lateSubscribe } as unknown as TerminalSessionManager);
	expect(lateSubscribe).not.toHaveBeenCalled();
	release?.();
	await closing;
	expect(order.slice(-2)).toEqual(["drained", "message-unsubscribe"]);
});

it("does not strand a transition emitted in the delivery microtask", async () => {
	const persisted: number[] = [];
	const observer = createTaskLifecycleObserver(
		async (_workspace, _source, value) => {
			persisted.push(value.updatedAt);
			return { replay: false, sequence: persisted.length, capture_gap: null };
		},
		(_workspace, value) => {
			if (value.updatedAt === 1) queueMicrotask(() => observer.observe("workspace", "cline", summary(2)));
		},
	);
	observer.observe("workspace", "cline", summary(1));
	await observer.drain();
	expect(persisted).toEqual([1, 2]);
	observer.close();
});
