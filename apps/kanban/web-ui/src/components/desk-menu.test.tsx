import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { DeskMenu } from "@/components/desk-menu";
import type { DeskDirectory } from "@/hooks/use-desk-registry";

const mocks = vi.hoisted(() => ({
	registry: { data: null as unknown, error: null, busy: false, refresh: vi.fn(), save: vi.fn(), annotate: vi.fn() },
	saveRole: vi.fn(),
	bind: vi.fn(),
	search: vi.fn(),
}));
vi.mock("@/hooks/use-desk-registry", () => ({ useDeskRegistry: () => mocks.registry }));
vi.mock("@/runtime/trpc-client", () => ({
	getRuntimeTrpcClient: () => ({
		desks: { saveRole: { mutate: mocks.saveRole }, bind: { mutate: mocks.bind }, search: { query: mocks.search } },
	}),
}));

const desk = {
	desk_id: "desk:00000000-0000-4000-8000-000000000003",
	name: "Scribe desk",
	description: "Notes.",
	role: "scribe",
	repos: [],
	capture: true,
	memory_write: true,
	context_doc: null,
	tenant_id: "acme",
	version: 1,
	recorded_at: "2026-09-30T00:00:00+00:00",
	authority: "operator desk registry record",
};
/** Only what the order defines for the directory: no roster version, source, mode or bindings. */
const orderDirectory = {
	desks: [desk],
	contexts: [],
	roles: [{ role_id: "scribe", label: "Scribe", purpose: "Notes." }],
	sessions: [],
	session_limit_reached: false,
	tenant_id: "acme",
	authorization_changed: false,
} as DeskDirectory;
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
function click(label: string) {
	const target = Array.from(container.querySelectorAll("button")).find((b) => b.textContent === label);
	expect(target, `button ${label}`).toBeDefined();
	act(() => target?.click());
}
function type(label: string, value: string) {
	const field = Array.from(container.querySelectorAll("label"))
		.find((l) => l.textContent?.startsWith(label))
		?.querySelector("input") as HTMLInputElement;
	act(() => {
		Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set?.call(field, value);
		field.dispatchEvent(new Event("input", { bubbles: true }));
	});
}

it("works from a directory without the optional registry metadata", async () => {
	mocks.registry.data = orderDirectory;
	act(() => root.render(<DeskMenu />));
	click("Scribe desk");
	expect(container.textContent).toContain("Bound sessions not reported");
	click("Add role");
	type("Role name", "Data steward");
	await act(async () => click("Add role"));
	expect(mocks.saveRole).toHaveBeenCalledWith({
		role_id: "data-steward",
		label: "Data steward",
		purpose: "",
		expected_version: 0,
	});
	click("Scribe desk");
	click("Bind session");
	type("Native session ID", "sess-1");
	click("Next");
	type("Harness", "codex");
	type("Provider", "openai");
	type("Model", "gpt-x");
	click("Next");
	type("Workspace", "/work");
	click("Next");
	await act(async () => click("Bind session"));
	expect(mocks.bind).toHaveBeenCalledWith(
		expect.objectContaining({ desk_id: desk.desk_id, native_session_id: "sess-1", source: "operator" }),
	);
});

it("uses reported metadata and hides portable actions for a legacy catalog", () => {
	mocks.registry.data = {
		...orderDirectory,
		roster_version: 5,
		roster_source: "configured",
		registry_mode: "registry",
		bindings: [],
	} as DeskDirectory;
	act(() => root.render(<DeskMenu />));
	click("Scribe desk");
	expect(container.textContent).toContain("0 bound sessions");
	mocks.registry.data = { ...orderDirectory, registry_mode: "legacy" } as DeskDirectory;
	act(() => root.render(<DeskMenu />));
	expect(Array.from(container.querySelectorAll("button")).map((b) => b.textContent)).not.toContain("Add role");
	expect(container.textContent).not.toContain("Bind session");
	expect(container.textContent).toContain("governed by the operator catalog");
});
