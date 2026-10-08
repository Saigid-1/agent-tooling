import { act } from "react";
import { createRoot } from "react-dom/client";
import { beforeEach, expect, it, vi } from "vitest";
import { AdrEvidencePanel } from "@/components/adr-evidence-panel";

const mocks = vi.hoisted(() => ({ record: vi.fn(), evidence: vi.fn(), lifecycle: vi.fn(), poll: () => {} }));
vi.mock("@/utils/react-use", () => ({
	useInterval: (callback: () => void) => {
		mocks.poll = callback;
	},
}));
vi.mock("@/runtime/trpc-client", () => ({
	getRuntimeTrpcClient: () => ({
		planning: {
			evidence: { query: mocks.evidence },
			lifecycle: { query: mocks.lifecycle },
			recordEvidence: { mutate: mocks.record },
		},
	}),
}));
beforeEach(() => {
	vi.clearAllMocks();
	mocks.record.mockReset();
	mocks.evidence.mockReset();
	mocks.lifecycle.mockReset();
	mocks.evidence.mockResolvedValue([]);
	mocks.lifecycle.mockResolvedValue({ records: [], evidence_boundary: "Observations only" });
});
it("retains the same request identity when retrying an uncertain evidence write", async () => {
	mocks.record.mockRejectedValueOnce(new Error("Connection lost")).mockResolvedValueOnce({});
	const container = document.createElement("div");
	document.body.append(container);
	const root = createRoot(container);
	try {
		await act(async () =>
			root.render(<AdrEvidencePanel workspaceId="workspace" taskId="card" intakeSha256={"a".repeat(64)} />),
		);
		for (const [name, value] of Object.entries({
			requirementId: "acceptance:1",
			path: "/evidence/test.txt",
			sha256: "b".repeat(64),
			rationale: "Test passed",
		})) {
			const input = container.querySelector<HTMLInputElement>(`[name="${name}"]`);
			if (input) input.value = value;
		}
		await act(async () => {
			container.querySelector("form")?.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
		});
		expect(container.textContent).toContain("Connection lost");
		await act(async () => {
			mocks.poll();
		});
		expect(container.textContent).toContain("Connection lost");
		await act(async () => {
			container.querySelector("form")?.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
		});
		expect(mocks.record).toHaveBeenCalledTimes(2);
		expect(mocks.record.mock.calls[0]?.[0]).toEqual(mocks.record.mock.calls[1]?.[0]);
		expect(mocks.record.mock.calls[0]?.[0]).toMatchObject({
			evidence: { actor: { kind: "local_operator_assertion" }, requirementId: "acceptance:1", kind: "green" },
		});
	} finally {
		act(() => root.unmount());
		container.remove();
	}
});

it("ignores an older refresh that completes after a newer lifecycle snapshot", async () => {
	let finishOld: ((value: { records: never[]; evidence_boundary: string }) => void) | undefined;
	mocks.lifecycle
		.mockImplementationOnce(
			() =>
				new Promise((resolve) => {
					finishOld = resolve;
				}),
		)
		.mockResolvedValueOnce({ records: [], evidence_boundary: "Latest captured observations" });
	const container = document.createElement("div");
	document.body.append(container);
	const root = createRoot(container);
	try {
		await act(async () =>
			root.render(<AdrEvidencePanel workspaceId="workspace" taskId="card" intakeSha256={"a".repeat(64)} />),
		);
		await act(async () => {
			mocks.poll();
		});
		expect(container.textContent).toContain("Latest captured observations");
		await act(async () => {
			finishOld?.({ records: [], evidence_boundary: "Older snapshot" });
		});
		expect(container.textContent).toContain("Latest captured observations");
		expect(container.textContent).not.toContain("Older snapshot");
	} finally {
		act(() => root.unmount());
		container.remove();
	}
});

it("shows agent attribution and captured execution binding without relabeling it as an operator", async () => {
	mocks.evidence.mockResolvedValue([
		{
			id: "record",
			recordedAt: "2026-09-26T00:00:00Z",
			execution: { repository: "/execution/repo", baseRef: "commit-reference", intakeSha256: "a".repeat(64) },
			input: {
				requirementId: "acceptance:1",
				kind: "acceptance",
				outcome: "accepted",
				rationale: "Scenario met",
				intakeSha256: "a".repeat(64),
				actor: { kind: "agent_assertion", id: "agent-session-7" },
				evidence: [],
			},
		},
	]);
	const container = document.createElement("div");
	document.body.append(container);
	const root = createRoot(container);
	try {
		await act(async () =>
			root.render(<AdrEvidencePanel workspaceId="workspace" taskId="card" intakeSha256={"a".repeat(64)} />),
		);
		expect(container.textContent).toContain("Attribution: agent_assertion · agent-session-7");
		expect(container.textContent).toContain("Repository: /execution/repo");
		expect(container.textContent).toContain("Base revision: commit-reference");
		expect(container.textContent).not.toContain("explicit local operator assertions");
	} finally {
		act(() => root.unmount());
		container.remove();
	}
});
