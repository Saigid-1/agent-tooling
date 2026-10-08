import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { SessionHistoryImportDialog } from "@/components/detail-panels/session-history-import-dialog";
import type { RuntimeTaskSessionSummary } from "@/runtime/types";

const runImport = vi.hoisted(() => vi.fn());
const readSetup = vi.hoisted(() => vi.fn());
const client = vi.hoisted(() => ({ sessionImport: { setup: { query: readSetup }, run: { mutate: runImport } } }));

vi.mock("@/runtime/trpc-client", () => ({
	getRuntimeTrpcClient: () => client,
}));
vi.mock("@/components/ui/dialog", () => ({
	Dialog: ({ children, open }: { children: React.ReactNode; open: boolean }) => open ? <div>{children}</div> : null,
	DialogHeader: ({ title }: { title: string }) => <h2>{title}</h2>,
	DialogBody: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
	DialogFooter: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
}));

const summary: RuntimeTaskSessionSummary = {
	taskId: "task-1", state: "awaiting_review", agentId: "codex", workspacePath: "/workspace",
	pid: null, startedAt: null, updatedAt: 1, lastOutputAt: null, reviewReason: null,
	exitCode: null, lastHookAt: null, latestHookActivity: null,
	nativeSessionBindings: [{
		providerId: "codex", nativeId: "native-1", observedAt: "2026-09-24T12:00:00Z",
		source: "codex_tui_session_meta", sourcePath: "/private/rollout-native-1.jsonl",
		sourceSha256: "a".repeat(64),
	}],
};

describe("session history import dialog", () => {
	let container: HTMLDivElement;
	let root: Root;
	let priorAct: boolean | undefined;

	beforeEach(() => {
		priorAct = (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT;
		(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
		container = document.createElement("div");
		document.body.appendChild(container);
		root = createRoot(container);
		sessionStorage.clear();
		readSetup.mockResolvedValue({ configured: true, message: "ready", executablePath: "/host/kp-agent-session-import", configPath: "/host/desk.json" });
		runImport.mockImplementation(async (input: { action: string }) => {
			if (input.action === "desks") return { schema_version: "ops.session-import.result.v1", status: "ok", desks: [{ binding_key: "reviewed", desk_label: "Reviewed Desk", role: "implementation", repo_key: "repo" }] };
			if (input.action === "preview") return { schema_version: "ops.session-import.result.v1", status: "preview", plan_token: "reviewed-plan", counts: { rows: 3 }, next_batch_offset: 20, coverage: { start_offset: 0, next_offset: 0, observed_size: 20, reviewed_complete_end: 20, complete_at_snapshot: false }, attribution: { selected_desk_id: "reviewed" } };
			if (input.action === "apply") return { schema_version: "ops.session-import.result.v1", status: "ok", job_id: "job-1", phase: "queued" };
			if (input.action === "assert-owner") return { schema_version: "ops.session-import.result.v1", status: "ok", job_id: "job-1", claim_id: "claim-1" };
			if (input.action === "follow") return { schema_version: "ops.session-import.result.v1", status: "ok", job_id: "job-1", phase: "start_requested" };
			return { schema_version: "ops.session-import.result.v1", status: "ok", job_id: "job-1", phase: "processing", counts: { imported: 1 }, attribution: { selected_desk_id: "reviewed", status: "proposed_unasserted" } };
		});
	});

	afterEach(async () => {
		await act(async () => root.unmount());
		container.remove();
		vi.clearAllMocks();
		(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = priorAct;
	});

	it("uses an observed native binding, previews a chosen desk, and applies only after consent", async () => {
		await act(async () => root.render(<SessionHistoryImportDialog open onOpenChange={() => undefined} taskId="task-1" workspaceId="workspace-1" summary={summary} />));
		await act(async () => undefined);
		const inputs = Array.from(container.querySelectorAll("input"));
		expect((inputs.find((input) => input.value === "/private/rollout-native-1.jsonl"))?.value).toBe("/private/rollout-native-1.jsonl");
		expect((inputs.find((input) => input.value === "native-1"))?.value).toBe("native-1");
		const desks = Array.from(container.querySelectorAll("select")).find((select) => select.querySelector('option[value="reviewed"]'));
		expect(desks).toBeDefined();
		await act(async () => {
			if (!desks) throw new Error("Desk selector missing");
			desks.value = "reviewed";
			desks.dispatchEvent(new Event("change", { bubbles: true }));
		});
		const previewButton = Array.from(container.querySelectorAll("button")).find((button) => button.textContent === "Preview");
		await act(async () => previewButton?.click());
		expect(container.textContent).toContain("rows: 3");
		expect(container.textContent).toContain("First bounded batch preview");
		expect(runImport).toHaveBeenCalledWith(expect.objectContaining({ action: "preview", nativeSessionId: "native-1", selectedDeskId: "reviewed" }));
		const applyButton = Array.from(container.querySelectorAll("button")).find((button) => button.textContent === "Apply import");
		expect(applyButton?.disabled).toBe(true);
		const consents = Array.from(container.querySelectorAll<HTMLInputElement>('input[type="checkbox"]'));
		await act(async () => { consents.forEach((input) => input.click()); });
		const ownerInputs = Array.from(container.querySelectorAll("input")).filter((input) => input.type === "text" && input.value === "");
		await act(async () => {
			for (const input of ownerInputs) {
				const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
				setter?.call(input, "Operator");
				input.dispatchEvent(new Event("input", { bubbles: true }));
			}
		});
		await act(async () => applyButton?.click());
		expect(runImport).toHaveBeenCalledWith({ action: "apply", planToken: "reviewed-plan", consent: true });
		expect(runImport).toHaveBeenCalledWith({ action: "follow", jobId: "job-1" });
		expect(runImport).not.toHaveBeenCalledWith(expect.objectContaining({ action: "assert-owner" }));
		await act(async () => root.unmount());
		root = createRoot(container);
		await act(async () => root.render(<SessionHistoryImportDialog open onOpenChange={() => undefined} taskId="task-1" workspaceId="workspace-1" summary={summary} />));
		await act(async () => undefined);
		expect(runImport).toHaveBeenCalledWith(expect.objectContaining({ action: "assert-owner", jobId: "job-1", selectedDeskId: "reviewed" }));
		const ownerCalls = runImport.mock.calls.filter(([input]) => input.action === "assert-owner").length;
		await act(async () => root.unmount());
		root = createRoot(container);
		await act(async () => root.render(<SessionHistoryImportDialog open onOpenChange={() => undefined} taskId="task-1" workspaceId="workspace-1" summary={summary} />));
		await act(async () => undefined);
		expect(runImport.mock.calls.filter(([input]) => input.action === "assert-owner")).toHaveLength(ownerCalls);
		const otherSummary = { ...summary, taskId: "task-2", nativeSessionBindings: [{ ...summary.nativeSessionBindings![0]!, nativeId: "native-2", sourcePath: "/private/rollout-native-2.jsonl" }] };
		await act(async () => root.render(<SessionHistoryImportDialog open onOpenChange={() => undefined} taskId="task-2" workspaceId="workspace-2" summary={otherSummary} />));
		expect(container.textContent).not.toContain("job-1");
		expect(Array.from(container.querySelectorAll("input")).some((input) => input.value === "/private/rollout-native-2.jsonl")).toBe(true);
	});
});
