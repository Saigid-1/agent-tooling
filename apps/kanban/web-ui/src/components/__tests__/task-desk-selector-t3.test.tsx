// T3 (docs/work/orders/T3-launch-binding-harness-profiles.md), web-ui side: a task's optional
// desk is set from task create/edit, and the desk selector lists the registry desks.
//
// Dispatcher amendment A3 (T3 meet, SPEC-GAP resolution): the create dialog and the inline edit
// card render the desk selector when their parent wires the desk props (`deskId` +
// `onDeskIdChange`), as App does; the selector fetches the registry desks itself through
// `useDeskRegistry` and offers a "No desk" choice. The components are rendered here with those
// props supplied; the App wiring is verified by the Coordinator at the meet.
//
// The registry is served through `useDeskRegistry` (and `desks.list` over tRPC, mocked with the
// same directory). Desk names are unusual so that a hard-coded list cannot match them. The
// selector is found generically: a native <select> listing the desks, a listbox/radiogroup
// listing them, or a control labelled "desk" whose opened menu lists them.
//
// Readings (reported under AMBIGUITY): choosing a desk calls `onDeskIdChange` with its `desk_id`;
// choosing "No desk" calls it with no desk (undefined, null or ""); the desk props are passed
// untyped so this file does not fix their TypeScript types.
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { TaskCreateDialog } from "@/components/task-create-dialog";
import { TaskInlineCreateCard } from "@/components/task-inline-create-card";

const DESKS = [
	{
		desk_id: "desk:7d3f1a2b-4c5e-4f60-8a71-b2c3d4e5f601",
		name: "Quillon archive desk",
		description: "Keeps the archive.",
		role: "curator",
		repos: [],
		capture: true,
		memory_write: true,
		context_doc: null,
		tenant_id: "tenant-t3",
		version: 1,
		recorded_at: "2026-09-30T00:00:00+00:00",
		authority: "operator desk registry record",
	},
	{
		desk_id: "desk:8e4a2b3c-5d6f-4071-9b82-c3d4e5f6a702",
		name: "Marrowgate review desk",
		description: "Reviews changes.",
		role: "scribe",
		repos: ["alpha"],
		capture: false,
		memory_write: true,
		context_doc: null,
		tenant_id: "tenant-t3",
		version: 1,
		recorded_at: "2026-09-30T00:00:00+00:00",
		authority: "operator desk registry record",
	},
];
const DESK_IDS = DESKS.map((desk) => desk.desk_id);
const DESK_NAMES = DESKS.map((desk) => desk.name);
const DIRECTORY = {
	desks: DESKS,
	contexts: [],
	roles: [
		{ role_id: "curator", label: "Curator", purpose: "Archive." },
		{ role_id: "scribe", label: "Scribe", purpose: "Record." },
	],
	roster_version: 1,
	roster_source: "configured",
	registry_mode: "registry",
	bindings: [],
	sessions: [],
	session_limit_reached: false,
	tenant_id: "tenant-t3",
	authorization_changed: false,
};

const mocks = vi.hoisted(() => ({ directory: null as unknown }));

vi.mock("@/hooks/use-desk-registry", () => ({
	useDeskRegistry: () => ({
		data: mocks.directory,
		error: null,
		busy: false,
		refresh: vi.fn(),
		save: vi.fn(),
		annotate: vi.fn(),
	}),
}));

vi.mock("@/runtime/trpc-client", () => {
	const route = (path: string[]): unknown =>
		new Proxy(() => undefined, {
			get: (_target, property) => {
				if (property === "then") return undefined;
				if (property === "query" || property === "mutate") {
					return async () => {
						return path.join(".") === "desks.list" ? mocks.directory : {};
					};
				}
				return route([...path, String(property)]);
			},
		});
	return { getRuntimeTrpcClient: () => route([]), createWorkspaceTrpcClient: () => route([]) };
});

// The repository's searchable dropdown opens in a portal that jsdom cannot always open; this
// double lists its options in place (as role=option buttons) so the desks are observable.
vi.mock("@/components/search-select-dropdown", () => ({
	SearchSelectDropdown: ({
		options,
		onSelect,
		buttonText,
		id,
	}: {
		options: ReadonlyArray<{ value: string; label: string }>;
		onSelect: (value: string) => void;
		buttonText?: string;
		id?: string;
	}) => (
		<div>
			<button type="button" id={id}>
				{buttonText ?? ""}
			</button>
			<div role="listbox">
				{options.map((option) => (
					<button
						type="button"
						role="option"
						aria-selected={false}
						key={option.value}
						data-value={option.value}
						onClick={() => onSelect(option.value)}
					>
						{option.label}
					</button>
				))}
			</div>
		</div>
	),
}));

vi.mock("@/runtime/runtime-config-query", () => ({
	fetchClineProviderCatalog: vi.fn(async () => []),
	fetchClineProviderModels: vi.fn(async () => []),
}));

let container: HTMLDivElement;
let root: Root;
beforeEach(() => {
	mocks.directory = DIRECTORY;
	container = document.createElement("div");
	document.body.append(container);
	root = createRoot(container);
});
afterEach(() => {
	act(() => root.unmount());
	container.remove();
	document.body.innerHTML = "";
});

async function settle() {
	for (let i = 0; i < 5; i += 1) {
		await act(async () => {
			await new Promise((resolve) => setTimeout(resolve, 0));
		});
	}
}

interface Choice {
	value: string | null;
	text: string;
	choose: () => Promise<void>;
}

function setValue(element: HTMLSelectElement, value: string) {
	Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value")?.set?.call(element, value);
	element.dispatchEvent(new Event("change", { bubbles: true }));
}

async function click(element: HTMLElement) {
	await act(async () => {
		element.click();
	});
	await settle();
}

function choice(element: HTMLElement): Choice {
	return {
		value: element.getAttribute("data-value") ?? element.getAttribute("value"),
		text: (element.textContent ?? "").trim(),
		choose: () => click(element),
	};
}

const mentionsDesk = (text: string) =>
	DESK_IDS.some((id) => text.includes(id)) || DESK_NAMES.some((n) => text.includes(n));

/** Text that names a control: its own text and labels, and its immediate container's text. */
function controlText(element: HTMLElement): string {
	const labelledBy = (element.getAttribute("aria-labelledby") ?? "")
		.split(/\s+/)
		.map((id) => (id ? (document.getElementById(id)?.textContent ?? "") : ""));
	const forLabel = element.id ? (document.querySelector(`label[for="${element.id}"]`)?.textContent ?? "") : "";
	return [
		element.textContent ?? "",
		element.getAttribute("aria-label") ?? "",
		element.getAttribute("title") ?? "",
		element.id,
		forLabel,
		...labelledBy,
		element.parentElement?.textContent ?? "",
	].join(" ");
}

/** The desk selector's choices, or null when no control lists the registry desks. */
async function deskChoices(): Promise<Choice[] | null> {
	for (const select of Array.from(document.querySelectorAll("select"))) {
		const options = Array.from(select.options).map((o) => ({
			value: o.value,
			text: (o.textContent ?? "").trim(),
			choose: async () => {
				await act(async () => setValue(select, o.value));
				await settle();
			},
		}));
		if (options.some((o) => mentionsDesk(`${o.value} ${o.text}`))) return options;
	}
	for (const list of Array.from(document.querySelectorAll<HTMLElement>("[role='listbox'], [role='radiogroup']"))) {
		const options = Array.from(list.querySelectorAll<HTMLElement>("[role='option'], [role='radio'], button")).map(
			choice,
		);
		if (options.some((o) => mentionsDesk(`${o.value ?? ""} ${o.text}`))) return options;
	}
	const triggers = Array.from(document.querySelectorAll<HTMLElement>("button, [role='combobox']")).filter((element) =>
		/desk/i.test(controlText(element)),
	);
	for (const trigger of triggers) {
		const before = new Set<Element>(Array.from(document.querySelectorAll("button")));
		await act(async () => {
			trigger.dispatchEvent(new MouseEvent("pointerdown", { bubbles: true }));
			trigger.click();
		});
		await settle();
		const roles = Array.from(
			document.querySelectorAll<HTMLElement>(
				"[role='option'], [role='menuitem'], [role='menuitemradio'], [role='radio'], [cmdk-item]",
			),
		);
		const appeared = Array.from(document.querySelectorAll<HTMLElement>("button")).filter((b) => !before.has(b));
		const options = [...roles, ...appeared].map(choice);
		if (options.some((o) => mentionsDesk(`${o.value ?? ""} ${o.text}`))) return options;
	}
	return null;
}

function find(choices: Choice[] | null, desk: (typeof DESKS)[number]): Choice {
	const found = (choices ?? []).find((c) => c.value === desk.desk_id || c.text.includes(desk.name));
	expect(found, `${desk.name} is not a choice`).toBeDefined();
	return found as Choice;
}

function noDesk(choices: Choice[] | null): Choice {
	const found = (choices ?? []).find((c) => c.value === "" || /no desk|none|unassigned|without/i.test(c.text));
	expect(found, "no 'no desk' choice").toBeDefined();
	return found as Choice;
}

/** The two desk props (A3), passed untyped. */
function deskProps(deskId: string | undefined, onDeskIdChange: (value: unknown) => void): Record<string, unknown> {
	return { deskId, onDeskIdChange };
}

function expectRegistryDesks(choices: Choice[] | null) {
	expect(choices, "no desk selector lists the registry desks").not.toBeNull();
	const listed = (choices ?? []).map((c) => `${c.value ?? ""} ${c.text}`);
	for (const desk of DESKS) {
		expect(
			listed.some((entry) => entry.includes(desk.desk_id) || entry.includes(desk.name)),
			`${desk.name} missing`,
		).toBe(true);
	}
	// An optional desk: one choice selects no desk.
	expect(
		(choices ?? []).some((c) => c.value === "" || /no desk|none|unassigned|without/i.test(c.text)),
		"no 'no desk' choice",
	).toBe(true);
}

const noop = () => undefined;

it("the task create dialog lists the registry desks in a desk selector and sets the task's desk", async () => {
	const onDeskIdChange = vi.fn();
	await act(async () => {
		root.render(
			<TaskCreateDialog
				open
				onOpenChange={noop}
				prompt="Fix the lantern"
				onPromptChange={noop}
				images={[]}
				onImagesChange={noop}
				onCreate={() => null}
				onCreateMultiple={() => []}
				startInPlanMode={false}
				onStartInPlanModeChange={noop}
				autoReviewEnabled={false}
				onAutoReviewEnabledChange={noop}
				autoReviewMode="commit"
				onAutoReviewModeChange={noop}
				workspaceId="workspace-t3"
				branchRef="main"
				branchOptions={[{ value: "main", label: "main" }]}
				onBranchRefChange={noop}
				agentId="claude"
				defaultAgentId="claude"
				{...deskProps(undefined, onDeskIdChange)}
			/>,
		);
	});
	await settle();
	const choices = await deskChoices();
	expectRegistryDesks(choices);
	await find(choices, DESKS[1] as (typeof DESKS)[number]).choose();
	expect(onDeskIdChange).toHaveBeenLastCalledWith(DESKS[1]?.desk_id);
});

it("the inline task edit card lists the registry desks in a desk selector and changes the task's desk", async () => {
	const onDeskIdChange = vi.fn();
	await act(async () => {
		root.render(
			<TaskInlineCreateCard
				mode="edit"
				idPrefix="inline-edit-task-t3"
				prompt="Fix the lantern"
				onPromptChange={noop}
				onCreate={noop}
				onCancel={noop}
				startInPlanMode={false}
				onStartInPlanModeChange={noop}
				autoReviewEnabled={false}
				onAutoReviewEnabledChange={noop}
				autoReviewMode="commit"
				onAutoReviewModeChange={noop}
				workspaceId="workspace-t3"
				branchRef="main"
				branchOptions={[{ value: "main", label: "main" }]}
				onBranchRefChange={noop}
				agentId="codex"
				defaultAgentId="claude"
				{...deskProps(DESKS[0]?.desk_id, onDeskIdChange)}
			/>,
		);
	});
	await settle();
	const choices = await deskChoices();
	expectRegistryDesks(choices);
	await find(choices, DESKS[1] as (typeof DESKS)[number]).choose();
	expect(onDeskIdChange).toHaveBeenLastCalledWith(DESKS[1]?.desk_id);
	await noDesk(choices).choose();
	const cleared = onDeskIdChange.mock.calls.at(-1)?.[0];
	expect(cleared === undefined || cleared === null || cleared === "", `"No desk" set ${String(cleared)}`).toBe(true);
	expect(onDeskIdChange.mock.calls.length).toBeGreaterThanOrEqual(2);
});
