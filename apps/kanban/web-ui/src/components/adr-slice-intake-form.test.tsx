import { act } from "react";
import { createRoot } from "react-dom/client";
import { expect, it, vi } from "vitest";
import { AdrSliceIntakeForm } from "@/components/adr-slice-intake-form";

const mocks = vi.hoisted(() => ({ scope: vi.fn(), reconcile: vi.fn() }));
vi.mock("@/runtime/trpc-client", () => ({
	getRuntimeTrpcClient: (workspaceId: string) => {
		mocks.scope(workspaceId);
		return { planning: { reconcile: { mutate: mocks.reconcile } } };
	},
}));
it("creates a native backlog card in the explicitly selected execution repository", async () => {
	mocks.reconcile.mockResolvedValue({ workspaceId: "execution", taskId: "card-1" });
	const container = document.createElement("div");
	document.body.append(container);
	const root = createRoot(container);
	const open = vi.fn();
	const refresh = vi.fn(async () => {});
	try {
		act(() =>
			root.render(
				<AdrSliceIntakeForm
					projects={[
						{
							id: "execution",
							name: "Service",
							path: "/repositories/service",
							taskCounts: { backlog: 0, in_progress: 0, review: 0, trash: 0 },
						},
					]}
					initialWorkspaceId="execution"
					onReconciled={refresh}
					onOpenTrack={open}
				/>,
			),
		);
		expect(mocks.reconcile).not.toHaveBeenCalled();
		for (const [name, value] of Object.entries({ path: "/reviewed/slice.json", baseRef: "a".repeat(40) })) {
			const input = container.querySelector<HTMLInputElement>(`[name="${name}"]`);
			if (input) input.value = value;
		}
		await act(async () => {
			container.querySelector("form")?.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
		});
		expect(mocks.scope).toHaveBeenCalledWith("execution");
		expect(mocks.reconcile).toHaveBeenCalledWith({ path: "/reviewed/slice.json", baseRef: "a".repeat(40) });
		expect(refresh).toHaveBeenCalled();
		act(() => {
			Array.from(container.querySelectorAll("button"))
				.find((button) => button.textContent === "Open native card")
				?.click();
		});
		expect(open).toHaveBeenCalledWith("execution", "card-1");
	} finally {
		act(() => root.unmount());
		container.remove();
	}
});
