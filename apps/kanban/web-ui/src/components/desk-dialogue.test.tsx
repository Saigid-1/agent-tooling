import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { DeskDialogue } from "@/components/desk-dialogue";

let container: HTMLDivElement;
let root: Root;
beforeEach(() => {
	container = document.createElement("div");
	document.body.append(container);
	root = createRoot(container);
});
afterEach(() => {
	act(() => root.unmount());
	container.remove();
});
function click(label: string) {
	act(() => {
		Array.from(container.querySelectorAll("button"))
			.find((b) => b.textContent === label)
			?.click();
	});
}
function change(value: string) {
	const input = container.querySelector("input,textarea") as HTMLInputElement | HTMLTextAreaElement;
	act(() => {
		Object.getOwnPropertyDescriptor(
			input.tagName === "INPUT" ? HTMLInputElement.prototype : HTMLTextAreaElement.prototype,
			"value",
		)?.set?.call(input, value);
		input.dispatchEvent(new Event("input", { bubbles: true }));
	});
}
it("collects one concern per screen, preserves back edits and saves no repo", async () => {
	const save = vi.fn().mockResolvedValue(undefined);
	act(() =>
		root.render(
			<DeskDialogue
				roles={[{ role_id: "Deployment Engineering", label: "Deployment Engineering", purpose: "Deploy" }]}
				onSave={save}
				onCancel={() => {}}
			/>,
		),
	);
	expect(container.querySelector("textarea")).toBeNull();
	change("Release engineer");
	click("Next");
	expect(container.querySelector("input")).toBeNull();
	change("Operate releases.");
	click("Back");
	expect((container.querySelector("input") as HTMLInputElement).value).toBe("Release engineer");
	click("Next");
	click("Next");
	expect(container.querySelector("select")).not.toBeNull();
	click("Next");
	expect(save).not.toHaveBeenCalled();
	await act(async () => click("Create desk"));
	expect(save).toHaveBeenCalledTimes(1);
	expect(save.mock.calls[0]?.[0]).toMatchObject({
		name: "Release engineer",
		description: "Operate releases.",
		role: "Deployment Engineering",
		expected_version: 0,
	});
	expect(save.mock.calls[0]?.[0]).not.toHaveProperty("repo_key");
});
it("keeps a failed save available for correction", async () => {
	const save = vi.fn().mockRejectedValue(new Error("desk changed; reload before saving"));
	act(() =>
		root.render(
			<DeskDialogue
				roles={[{ role_id: "Security", label: "Security", purpose: "Review" }]}
				onSave={save}
				onCancel={() => {}}
			/>,
		),
	);
	change("Security");
	click("Next");
	change("Review changes");
	click("Next");
	click("Next");
	await act(async () => click("Create desk"));
	expect(container.querySelector('[role="alert"]')?.textContent).toContain("desk changed");
	expect(container.textContent).toContain("Create desk");
});
