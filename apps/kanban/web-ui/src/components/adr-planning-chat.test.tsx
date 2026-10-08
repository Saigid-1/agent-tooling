import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AdrPlanningChat } from "@/components/adr-planning-chat";
import { createIdleTaskSession } from "@/hooks/app-utils";
import type { RuntimeConfigResponse } from "@/runtime/types";

const mocks = vi.hoisted(() => ({ history: vi.fn(), send: vi.fn() }));
vi.mock("@/runtime/trpc-client", () => ({
	getRuntimeTrpcClient: () => ({
		runtime: { getTaskChatMessages: { query: mocks.history }, sendTaskChatMessage: { mutate: mocks.send } },
	}),
}));
vi.mock("@/components/detail-panels/cline-agent-chat-panel", () => ({
	ClineAgentChatPanel: ({ taskId }: { taskId: string }) => <div data-chat={taskId}>Native conversation</div>,
}));
let container: HTMLDivElement;
let root: Root;
beforeEach(() => {
	vi.clearAllMocks();
	container = document.createElement("div");
	document.body.append(container);
	root = createRoot(container);
});
afterEach(() => {
	act(() => root.unmount());
	container.remove();
});
function render(prompt = "Reviewed architecture context") {
	act(() =>
		root.render(
			<AdrPlanningChat
				workspaceId="repo-a"
				identity="initiative/ADR-1"
				prompt={prompt}
				config={{} as RuntimeConfigResponse}
				sessions={{}}
				messages={{}}
			/>,
		),
	);
}
async function start() {
	await act(async () => {
		container.querySelector("button")?.click();
	});
}
describe("explicit native ADR planning conversation", () => {
	it("never starts on navigation and sends context in plan mode only after Start", async () => {
		mocks.history.mockResolvedValue({ ok: false, messages: [], error: "Task chat session is not available." });
		mocks.send.mockResolvedValue({ ok: true, summary: createIdleTaskSession("planning"), message: null });
		render();
		expect(mocks.send).not.toHaveBeenCalled();
		expect(mocks.history).not.toHaveBeenCalled();
		await start();
		expect(mocks.send).toHaveBeenCalledWith(
			expect.objectContaining({
				mode: "plan",
				text: "Reviewed architecture context",
				taskId: expect.stringContaining("__home_agent__:repo-a:cline:adr:"),
			}),
		);
		expect(container.textContent).toContain("Native conversation");
	});
	it("reopens retained history without sending context again or restarting", async () => {
		mocks.history.mockResolvedValue({ ok: true, messages: [{ id: "previous" }] });
		render();
		await start();
		expect(mocks.send).not.toHaveBeenCalled();
		expect(container.textContent).toContain("Native conversation");
	});
	it("surfaces history load errors and does not risk replacing history", async () => {
		mocks.history.mockRejectedValue(new Error("Disconnected"));
		render();
		await start();
		expect(mocks.send).not.toHaveBeenCalled();
		expect(container.querySelector('[role="alert"]')?.textContent).toBe("Disconnected");
	});
});

it("preserves history and sends refreshed current context only on explicit request", async () => {
	mocks.history.mockResolvedValue({ ok: true, messages: [{ id: "previous" }] });
	mocks.send.mockResolvedValue({ ok: true, summary: createIdleTaskSession("planning"), message: null });
	render();
	await start();
	expect(container.textContent).toContain("Existing messages may describe earlier revisions");
	render("Revised architecture context");
	expect(mocks.send).not.toHaveBeenCalled();
	await act(async () => {
		Array.from(container.querySelectorAll("button"))
			.find((button) => button.textContent === "Send refreshed planning context")
			?.click();
	});
	expect(mocks.send).toHaveBeenCalledTimes(1);
	expect(mocks.send).toHaveBeenCalledWith(
		expect.objectContaining({ text: "Revised architecture context", mode: "plan" }),
	);
	expect(mocks.history).toHaveBeenCalledTimes(1);
	expect(container.textContent).toContain("The current planning context has been sent");
	render("Another revision");
	expect(container.textContent).toContain("Existing messages may describe earlier revisions");
	expect(mocks.send).toHaveBeenCalledTimes(1);
});

it("does not claim context was refreshed when the native send fails", async () => {
	mocks.history.mockResolvedValue({ ok: true, messages: [{ id: "previous" }] });
	mocks.send.mockResolvedValue({ ok: false, error: "Provider unavailable" });
	render();
	await start();
	await act(async () => {
		Array.from(container.querySelectorAll("button"))
			.find((button) => button.textContent === "Send refreshed planning context")
			?.click();
	});
	expect(container.querySelector('[role="alert"]')?.textContent).toBe("Provider unavailable");
	expect(container.textContent).toContain("Existing messages may describe earlier revisions");
});
