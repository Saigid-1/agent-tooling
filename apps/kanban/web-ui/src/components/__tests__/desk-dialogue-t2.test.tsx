// T2 P5 (web-ui side): the role picker lists the configured roster it is given, and
// saving a desk sends the portable desk fields. The dialogue is walked generically,
// screen by screen, so the test does not fix how many screens the flow has.
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { DeskDialogue } from "@/components/desk-dialogue";

const ROSTER = [
	{ role_id: "scribe", label: "Scribe", purpose: "Record decisions as they are made." },
	{ role_id: "curator", label: "Curator", purpose: "Keep the shared archive tidy." },
];
const ROSTER_IDS = ROSTER.map((r) => r.role_id);
const OPS_NINE = [
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
const CONTRACT_FIELDS = [
	"desk_id",
	"name",
	"description",
	"role",
	"repos",
	"capture",
	"memory_write",
	"context_doc",
	"expected_version",
];
const NAME = "Archive scribe";
const DESCRIPTION = "Keeps a record of what was decided.";

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

const buttons = () => Array.from(container.querySelectorAll("button"));
const button = (label: RegExp) => buttons().find((b) => label.test((b.textContent ?? "").trim()));
function setValue(element: HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement, value: string) {
	const prototype =
		element instanceof HTMLSelectElement
			? HTMLSelectElement.prototype
			: element instanceof HTMLTextAreaElement
				? HTMLTextAreaElement.prototype
				: HTMLInputElement.prototype;
	act(() => {
		Object.getOwnPropertyDescriptor(prototype, "value")?.set?.call(element, value);
		element.dispatchEvent(new Event(element instanceof HTMLSelectElement ? "change" : "input", { bubbles: true }));
	});
}
function rolePicker(): { values: string[]; choose: (id: string) => void } | null {
	const select = Array.from(container.querySelectorAll("select")).find((s) =>
		Array.from(s.options).some((o) => ROSTER_IDS.includes(o.value) || OPS_NINE.includes(o.value)),
	);
	if (select)
		return {
			values: Array.from(select.options)
				.map((o) => o.value)
				.filter(Boolean),
			choose: (id) => setValue(select, id),
		};
	const radios = Array.from(container.querySelectorAll<HTMLInputElement>('input[type="radio"]')).filter(
		(r) => ROSTER_IDS.includes(r.value) || OPS_NINE.includes(r.value),
	);
	if (radios.length)
		return {
			values: radios.map((r) => r.value),
			choose: (id) => act(() => radios.find((r) => r.value === id)?.click()),
		};
	return null;
}
function fillRequiredText() {
	for (const input of Array.from(
		container.querySelectorAll<HTMLInputElement>('input:not([type]), input[type="text"]'),
	)) {
		if (!input.value) setValue(input, NAME);
	}
	for (const area of Array.from(container.querySelectorAll("textarea"))) {
		if (!area.value) setValue(area, DESCRIPTION);
	}
}
async function walk(chooseRole?: string) {
	const pickers: string[][] = [];
	for (let screen = 0; screen < 16; screen++) {
		const finish = button(/^(Create desk|Save desk)$/);
		if (finish) {
			await act(async () => finish.click());
			return pickers;
		}
		const picker = rolePicker();
		if (picker) {
			pickers.push(picker.values);
			if (chooseRole) picker.choose(chooseRole);
		}
		if (button(/^Next$/)?.disabled) fillRequiredText();
		const next = button(/^Next$/);
		expect(next, `screen ${screen}: no Next button`).toBeDefined();
		expect(next?.disabled, `screen ${screen}: Next stays disabled`).toBe(false);
		act(() => next?.click());
	}
	throw new Error("the desk dialogue never reached its save screen");
}

it("lists the configured roster and creates a desk with the portable fields", async () => {
	const save = vi.fn().mockResolvedValue(undefined);
	act(() => root.render(<DeskDialogue roles={ROSTER} onSave={save} onCancel={() => {}} />));
	const pickers = await walk("curator");
	expect(pickers.length).toBeGreaterThan(0);
	for (const values of pickers) expect(values).toEqual(ROSTER_IDS);
	expect(save).toHaveBeenCalledTimes(1);
	const sent = save.mock.calls[0]?.[0] as Record<string, unknown>;
	expect(sent.desk_id).toMatch(/^desk:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/);
	expect(sent).toMatchObject({ name: NAME, description: DESCRIPTION, role: "curator", expected_version: 0 });
	expect(Array.isArray(sent.repos)).toBe(true);
	expect((sent.repos as unknown[]).length).toBeLessThanOrEqual(32);
	expect(typeof sent.capture).toBe("boolean");
	expect(typeof sent.memory_write).toBe("boolean");
	// Optional context_doc: absent, null (none) or a string; any other type is refused (dispatcher ruling).
	if ("context_doc" in sent)
		expect(
			sent.context_doc === null || typeof sent.context_doc === "string",
			`context_doc ${typeof sent.context_doc}`,
		).toBe(true);
	expect(Object.keys(sent).filter((k) => !CONTRACT_FIELDS.includes(k))).toEqual([]);
});

it("editing a desk sends back its repositories, capture, write and context settings", async () => {
	const save = vi.fn().mockResolvedValue(undefined);
	const existing = {
		desk_id: "desk:1b4e28ba-2fa1-41d2-883f-0016d3cca427",
		name: "Archive curator",
		description: "Keeps the shared archive tidy.",
		role: "curator",
		repos: ["alpha", "beta"],
		capture: true,
		memory_write: false,
		context_doc: "Archive rules live here.",
		tenant_id: "tenant",
		version: 3,
		recorded_at: "2026-09-30T12:00:00+00:00",
		authority: "operator profile metadata; not session admission",
	};
	act(() =>
		root.render(<DeskDialogue existing={existing as never} roles={ROSTER} onSave={save} onCancel={() => {}} />),
	);
	const pickers = await walk();
	for (const values of pickers) expect(values).toEqual(ROSTER_IDS);
	expect(save).toHaveBeenCalledTimes(1);
	const sent = save.mock.calls[0]?.[0] as Record<string, unknown>;
	expect(sent).toMatchObject({
		desk_id: existing.desk_id,
		name: existing.name,
		description: existing.description,
		role: "curator",
		capture: true,
		memory_write: false,
		context_doc: "Archive rules live here.",
		expected_version: 3,
	});
	expect([...(sent.repos as string[])].sort()).toEqual(["alpha", "beta"]);
	expect(Object.keys(sent).filter((k) => !CONTRACT_FIELDS.includes(k))).toEqual([]);
});
