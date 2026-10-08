import { describe, expect, it } from "vitest";
import { type HarnessRegistrySnapshot, resolveHarnessSession } from "../../src/core/harness-session-resolution";

const snapshot: HarnessRegistrySnapshot = {
	workspaceId: "repo-a",
	repoPath: "/example/repo-a",
	sessions: {
		"task-1": {
			taskId: "task-1",
			state: "running",
			agentId: "codex",
			workspacePath: "/example/repo-a",
			pid: 42,
			startedAt: 1000,
			updatedAt: 2000,
			lastOutputAt: null,
			reviewReason: null,
			exitCode: null,
			lastHookAt: null,
			latestHookActivity: null,
		},
	},
	evidence: {
		path: "/state/sessions.json",
		sha256: "abc",
		modifiedAt: "2026-09-23T00:00:00.000Z",
		observedAt: "2026-09-23T01:00:00.000Z",
		present: true,
	},
};
const nativeBinding = {
	providerId: "codex" as const,
	nativeId: "native-1",
	observedAt: "2026-09-24T03:00:00.000Z",
	source: "codex_tui_session_meta" as const,
	sourcePath: "/runtime/session-log.jsonl",
	sourceSha256: "a".repeat(64),
};

describe("read-only harness session resolution", () => {
	it("resolves only an exact workspace, task and provider key, retaining saved-state qualifiers", () => {
		const result = resolveHarnessSession([snapshot], {
			kind: "kanban_task",
			workspaceId: "repo-a",
			taskId: "task-1",
			agentId: "codex",
		});
		expect(result.status).toBe("resolved");
		if (result.status !== "resolved") return;
		expect(result.route).toEqual({
			kind: "kanban_task",
			registryPath: "/state/sessions.json",
			workspaceId: "repo-a",
			taskId: "task-1",
			runtimeEndpoint: null,
		});
		expect(result.observation).toMatchObject({
			state: "running",
			stateSource: "persisted_kanban_summary",
			reachability: "not_checked",
			deliveryAssurance: "none",
		});
	});
	it("does not match a task in a different workspace or provider", () => {
		expect(
			resolveHarnessSession([snapshot], {
				kind: "kanban_task",
				workspaceId: "repo-b",
				taskId: "task-1",
				agentId: "codex",
			}).status,
		).toBe("unresolved");
		expect(
			resolveHarnessSession([snapshot], {
				kind: "kanban_task",
				workspaceId: "repo-a",
				taskId: "task-1",
				agentId: "claude",
			}).status,
		).toBe("unresolved");
	});
	it("keeps a native harness ID unresolved without a verified mapping", () => {
		expect(
			resolveHarnessSession([snapshot], {
				kind: "native_session",
				workspaceId: "repo-a",
				nativeId: "task-1",
				agentId: "codex",
			}).status,
		).toBe("unresolved");
	});
	it("resolves an exact recorded native binding with its provenance", () => {
		const bound = {
			...snapshot,
			sessions: { "task-1": { ...snapshot.sessions["task-1"], nativeSessionBindings: [nativeBinding] } },
		};
		const result = resolveHarnessSession([bound], {
			kind: "native_session",
			workspaceId: "repo-a",
			nativeId: "native-1",
			agentId: "codex",
		});
		expect(result.status).toBe("resolved");
		if (result.status !== "resolved") return;
		expect(result.route.taskId).toBe("task-1");
		expect(result.nativeBinding).toEqual(nativeBinding);
		expect(
			resolveHarnessSession([bound], {
				kind: "native_session",
				workspaceId: "other",
				nativeId: "native-1",
				agentId: "codex",
			}).status,
		).toBe("unresolved");
		expect(
			resolveHarnessSession([bound], {
				kind: "native_session",
				workspaceId: "repo-a",
				nativeId: "native-1",
				agentId: "claude",
			}).status,
		).toBe("unresolved");
	});
	it("refuses contradictory bindings for the same native identity", () => {
		const bound = {
			...snapshot,
			sessions: {
				"task-1": { ...snapshot.sessions["task-1"], nativeSessionBindings: [nativeBinding] },
				"task-2": { ...snapshot.sessions["task-1"], taskId: "task-2", nativeSessionBindings: [nativeBinding] },
			},
		};
		expect(
			resolveHarnessSession([bound], {
				kind: "native_session",
				workspaceId: "repo-a",
				nativeId: "native-1",
				agentId: "codex",
			}).status,
		).toBe("ambiguous");
	});
	it("keeps a historical native binding visible after the task changes provider", () => {
		const changed = {
			...snapshot,
			sessions: {
				"task-1": {
					...snapshot.sessions["task-1"],
					agentId: "claude" as const,
					nativeSessionBindings: [nativeBinding],
				},
			},
		};
		const result = resolveHarnessSession([changed], {
			kind: "native_session",
			workspaceId: "repo-a",
			nativeId: "native-1",
			agentId: "codex",
		});
		expect(result.status).toBe("resolved");
		if (result.status !== "resolved") return;
		expect(result.provider).toBe("codex");
		expect(result.currentProvider).toBe("claude");
		expect(result.capabilities.terminal_input).toBe("not_attributed_to_native_session");
	});
	it("keeps host session IDs in their own namespace", () => {
		const result = resolveHarnessSession([snapshot], {
			kind: "host_session",
			workspaceId: "repo-a",
			hostId: "local_de26",
		});
		expect(result.status).toBe("unresolved");
	});
	it("refuses duplicate exact candidates", () => {
		expect(
			resolveHarnessSession([snapshot, snapshot], {
				kind: "kanban_task",
				workspaceId: "repo-a",
				taskId: "task-1",
				agentId: "codex",
			}).status,
		).toBe("ambiguous");
	});
	it("explains a missing registry file", () => {
		const missing = {
			...snapshot,
			evidence: { ...snapshot.evidence, present: false, sha256: null, modifiedAt: null },
			sessions: {},
		};
		const result = resolveHarnessSession([missing], {
			kind: "kanban_task",
			workspaceId: "repo-a",
			taskId: "task-1",
			agentId: "codex",
		});
		expect(result.status).toBe("unresolved");
		if (result.status !== "unresolved") return;
		expect(result.reason).toContain("registry file is missing");
	});
});
