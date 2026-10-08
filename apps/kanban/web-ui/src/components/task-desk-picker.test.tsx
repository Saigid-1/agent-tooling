import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { TaskDeskPicker } from "@/components/task-desk-picker";
import type { DeskDirectory } from "@/hooks/use-desk-registry";
import { addTaskToColumnWithResult, normalizeBoardData, updateTask } from "@/state/board-state";
import type { BoardData } from "@/types";

const mocks = vi.hoisted(() => ({
	registry: { data: null as unknown, error: null as string | null, busy: false, refresh: vi.fn() },
}));
vi.mock("@/hooks/use-desk-registry", () => ({ useDeskRegistry: () => mocks.registry }));

const deskId = "desk:00000000-0000-4000-8000-000000000007";
const directory = {
	desks: [
		{
			desk_id: deskId,
			name: "Builder",
			description: "Builds.",
			role: "general",
			repos: [],
			capture: true,
			memory_write: true,
			context_doc: null,
			tenant_id: "acme",
			version: 1,
			recorded_at: "2026-09-30T00:00:00+00:00",
			authority: "operator desk registry record",
		},
	],
	contexts: [],
	roles: [{ role_id: "general", label: "General", purpose: "" }],
	sessions: [],
	session_limit_reached: false,
	tenant_id: "acme",
	authorization_changed: false,
} as DeskDirectory;

let container: HTMLDivElement;
let root: Root;
beforeEach(() => {
	mocks.registry.data = null;
	mocks.registry.error = null;
	container = document.createElement("div");
	document.body.append(container);
	root = createRoot(container);
});
afterEach(() => {
	act(() => root.unmount());
	container.remove();
});

function select(): HTMLSelectElement {
	return container.querySelector("select") as HTMLSelectElement;
}

it("lists registry desks and reports the chosen desk or none", () => {
	mocks.registry.data = directory;
	const onDeskIdChange = vi.fn();
	act(() => root.render(<TaskDeskPicker id="desk" deskId={undefined} onDeskIdChange={onDeskIdChange} />));
	expect(Array.from(select().options).map((option) => option.textContent)).toEqual(["No desk", "Builder (general)"]);
	act(() => {
		select().value = deskId;
		select().dispatchEvent(new Event("change", { bubbles: true }));
	});
	expect(onDeskIdChange).toHaveBeenLastCalledWith(deskId);
	act(() => root.render(<TaskDeskPicker id="desk" deskId={deskId} onDeskIdChange={onDeskIdChange} />));
	act(() => {
		select().value = "";
		select().dispatchEvent(new Event("change", { bubbles: true }));
	});
	expect(onDeskIdChange).toHaveBeenLastCalledWith(undefined);
});

it("keeps an assigned desk clearable when the registry is unavailable", () => {
	mocks.registry.error = "Desk registry is not configured on this host.";
	act(() => root.render(<TaskDeskPicker id="desk" deskId={deskId} onDeskIdChange={vi.fn()} />));
	expect(container.textContent).toContain("Desks unavailable");
	expect(select().disabled).toBe(false);
	expect(select().value).toBe(deskId);
	act(() => root.render(<TaskDeskPicker id="desk" deskId={undefined} onDeskIdChange={vi.fn()} />));
	expect(select().disabled).toBe(true);
});

it("stores the desk on the card, keeps it across other edits, and clears it on request", () => {
	const board: BoardData = normalizeBoardData({ columns: [], dependencies: [] }) ?? { columns: [], dependencies: [] };
	const created = addTaskToColumnWithResult(board, "backlog", { prompt: "Do it", baseRef: "main", deskId });
	expect(created.task.deskId).toBe(deskId);
	const titled = updateTask(created.board, created.task.id, { prompt: "Do it now", baseRef: "main" });
	const kept = titled.board.columns.flatMap((column) => column.cards).find((card) => card.id === created.task.id);
	expect(kept?.deskId).toBe(deskId);
	const cleared = updateTask(titled.board, created.task.id, { prompt: "Do it now", baseRef: "main", deskId: null });
	const card = cleared.board.columns.flatMap((column) => column.cards).find((c) => c.id === created.task.id);
	expect(card && "deskId" in card).toBe(false);
	const plain = addTaskToColumnWithResult(board, "backlog", { prompt: "Plain", baseRef: "main" });
	expect("deskId" in plain.task).toBe(false);
	const reloaded = normalizeBoardData({ columns: created.board.columns, dependencies: [] });
	expect(reloaded?.columns.flatMap((column) => column.cards)[0]?.deskId).toBe(deskId);
});
