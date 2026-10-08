import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { DeskBindDialogue } from "@/components/desk-bind-dialogue";
import { DeskDialogue } from "@/components/desk-dialogue";
import { DeskRoleDialogue } from "@/components/desk-role-dialogue";
import type { DeskProfile } from "@/hooks/use-desk-registry";

const OPS_ROLES = [
	"Coordinator",
	"Test Implementer",
	"Feature Implementer",
	"Verification",
	"UX Coordinator",
	"Deployment Engineering",
	"Security",
	"Analyst/Researcher",
	"Auditor",
];
const roster = [
	{ role_id: "scribe", label: "Scribe", purpose: "Keep notes." },
	{ role_id: "release-helper", label: "Release helper", purpose: "Ship releases." },
];
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
function button(label: string) {
	return Array.from(container.querySelectorAll("button")).find((b) => b.textContent === label);
}
function click(label: string) {
	const target = button(label);
	expect(target, `button ${label}`).toBeDefined();
	act(() => target?.click());
}
function type(element: Element | null, value: string) {
	const input = element as HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement;
	const prototype =
		input.tagName === "INPUT"
			? HTMLInputElement.prototype
			: input.tagName === "SELECT"
				? HTMLSelectElement.prototype
				: HTMLTextAreaElement.prototype;
	act(() => {
		Object.getOwnPropertyDescriptor(prototype, "value")?.set?.call(input, value);
		input.dispatchEvent(new Event(input.tagName === "SELECT" ? "change" : "input", { bubbles: true }));
	});
}
function fieldByLabel(text: string) {
	const label = Array.from(container.querySelectorAll("label")).find((l) => l.textContent?.startsWith(text));
	return label?.querySelector("input,textarea,select") ?? null;
}
function toReview() {
	type(container.querySelector("input"), "Release notes");
	click("Next");
	type(container.querySelector("textarea"), "Keeps release notes.");
	click("Next");
}

it("lists only the configured roster in the role picker", () => {
	act(() => root.render(<DeskDialogue roles={roster} onSave={vi.fn()} onCancel={() => {}} />));
	toReview();
	const options = Array.from(container.querySelectorAll("option")).map((o) => [o.value, o.textContent]);
	expect(options).toEqual([
		["scribe", "Scribe"],
		["release-helper", "Release helper"],
	]);
	for (const role of OPS_ROLES) expect(container.innerHTML).not.toContain(role);
});

it("creates a desk with the new fields, one optional concern per screen", async () => {
	const save = vi.fn().mockResolvedValue(undefined);
	act(() => root.render(<DeskDialogue roles={roster} onSave={save} onCancel={() => {}} />));
	toReview();
	type(container.querySelector("select"), "release-helper");
	click("Next");
	click("Change repositories");
	expect(container.querySelectorAll("textarea,input")).toHaveLength(1);
	type(container.querySelector("textarea"), "alpha\nbeta\n\nalpha");
	click("Done");
	click("Change memory");
	const [capture, write] = Array.from(container.querySelectorAll<HTMLInputElement>('input[type="checkbox"]'));
	expect([capture?.checked, write?.checked]).toEqual([true, true]);
	act(() => write?.click());
	click("Done");
	expect(container.textContent).toContain("Capture on · Memory proposals not allowed");
	click("Change context");
	type(container.querySelector("textarea"), "Keep receipts.");
	click("Done");
	expect(container.textContent).toContain("Repositories: alpha, beta");
	await act(async () => click("Create desk"));
	expect(save).toHaveBeenCalledTimes(1);
	expect(save.mock.calls[0]?.[0]).toMatchObject({
		name: "Release notes",
		description: "Keeps release notes.",
		role: "release-helper",
		repos: ["alpha", "beta"],
		capture: true,
		memory_write: false,
		context_doc: "Keep receipts.",
		expected_version: 0,
	});
	expect(Object.keys(save.mock.calls[0]?.[0]).sort()).toEqual(
		[
			"capture",
			"context_doc",
			"description",
			"desk_id",
			"expected_version",
			"memory_write",
			"name",
			"repos",
			"role",
		].sort(),
	);
});

it("defaults a new desk to capture and memory write on, with no repositories or context document", async () => {
	const save = vi.fn().mockResolvedValue(undefined);
	act(() => root.render(<DeskDialogue roles={roster} onSave={save} onCancel={() => {}} />));
	toReview();
	click("Next");
	expect(container.textContent).toContain("Capture on · Memory proposals allowed");
	await act(async () => click("Create desk"));
	expect(save.mock.calls[0]?.[0]).toMatchObject({ repos: [], capture: true, memory_write: true, context_doc: null });
});

it("lets the operator switch both capture and memory write off", async () => {
	const save = vi.fn().mockResolvedValue(undefined);
	act(() => root.render(<DeskDialogue roles={roster} onSave={save} onCancel={() => {}} />));
	toReview();
	click("Next");
	click("Change memory");
	for (const box of Array.from(container.querySelectorAll<HTMLInputElement>('input[type="checkbox"]')))
		act(() => box.click());
	click("Done");
	await act(async () => click("Create desk"));
	expect(save.mock.calls[0]?.[0]).toMatchObject({ capture: false, memory_write: false });
});

it("refuses an oversized context document before saving", () => {
	act(() => root.render(<DeskDialogue roles={roster} onSave={vi.fn()} onCancel={() => {}} />));
	toReview();
	click("Next");
	click("Change context");
	type(container.querySelector("textarea"), "é".repeat(8193));
	expect(button("Done")?.disabled).toBe(true);
	expect(container.querySelector('[role="alert"]')?.textContent).toContain("16386 of 16384 bytes");
});

it("edits an existing desk keeping its stored fields and version", async () => {
	const save = vi.fn().mockResolvedValue(undefined);
	const existing: DeskProfile = {
		desk_id: "desk:00000000-0000-4000-8000-000000000001",
		name: "Scribe desk",
		description: "Notes.",
		role: "scribe",
		repos: ["alpha"],
		capture: true,
		memory_write: false,
		context_doc: "Context.",
		binding_key: `binding:${"0".repeat(64)}`,
		tenant_id: "acme",
		version: 3,
		recorded_at: "2026-09-30T00:00:00+00:00",
		authority: "operator desk registry record",
	};
	act(() => root.render(<DeskDialogue existing={existing} roles={roster} onSave={save} onCancel={() => {}} />));
	click("Next");
	click("Next");
	click("Next");
	await act(async () => click("Save desk"));
	expect(save.mock.calls[0]?.[0]).toEqual({
		desk_id: existing.desk_id,
		name: "Scribe desk",
		description: "Notes.",
		role: "scribe",
		repos: ["alpha"],
		capture: true,
		memory_write: false,
		context_doc: "Context.",
		expected_version: 3,
	});
});

it("adds a role to the roster from the role screen and selects it", async () => {
	const addRole = vi.fn().mockResolvedValue(undefined);
	const render = (roles: typeof roster) =>
		act(() =>
			root.render(
				<DeskDialogue roles={roles} rosterVersion={4} onAddRole={addRole} onSave={vi.fn()} onCancel={() => {}} />,
			),
		);
	render(roster);
	toReview();
	click("Add a role");
	type(fieldByLabel("Role name"), "Data steward");
	expect((fieldByLabel("Role ID") as HTMLInputElement).value).toBe("data-steward");
	type(fieldByLabel("Purpose"), "Curates datasets.");
	await act(async () => click("Add role"));
	expect(addRole).toHaveBeenCalledWith({
		role_id: "data-steward",
		label: "Data steward",
		purpose: "Curates datasets.",
		expected_version: 4,
	});
	render([...roster, { role_id: "data-steward", label: "Data steward", purpose: "Curates datasets." }]);
	expect((container.querySelector("select") as HTMLSelectElement).value).toBe("data-steward");
});

it("refuses a role ID already in the roster", () => {
	act(() => root.render(<DeskRoleDialogue roles={roster} rosterVersion={1} onSave={vi.fn()} onCancel={() => {}} />));
	type(fieldByLabel("Role name"), "Scribe");
	expect(button("Add role")?.disabled).toBe(true);
	expect(container.textContent).toContain("already in the roster");
});

it("binds one exact session as an operator act with the full binding target", async () => {
	const bind = vi.fn().mockResolvedValue(undefined);
	const desk = { desk_id: "desk:00000000-0000-4000-8000-000000000002", name: "Scribe desk" } as DeskProfile;
	act(() => root.render(<DeskBindDialogue desk={desk} onBind={bind} onCancel={() => {}} />));
	type(fieldByLabel("Native session ID"), "has space");
	expect(button("Next")?.disabled).toBe(true);
	type(fieldByLabel("Native session ID"), "0199-session_a");
	click("Next");
	type(fieldByLabel("Harness"), "claude-code");
	type(fieldByLabel("Provider"), "anthropic");
	expect(button("Next")?.disabled).toBe(true);
	type(fieldByLabel("Model"), "claude-x");
	click("Next");
	type(fieldByLabel("Workspace"), "/work/notes");
	click("Next");
	expect(bind).not.toHaveBeenCalled();
	await act(async () => click("Bind session"));
	expect(bind).toHaveBeenCalledWith({
		harness: "claude-code",
		provider: "anthropic",
		model: "claude-x",
		native_session_id: "0199-session_a",
		desk_id: desk.desk_id,
		source: "operator",
		workspace: "/work/notes",
		parent_session_id: null,
	});
});
