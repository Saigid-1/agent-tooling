import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { buildPlanningChatContext, groupPlanningAdrs } from "@/components/adr-planning-model";
import { AdrPlanningView } from "@/components/adr-planning-view";
import { createInitialBoardData } from "@/data/board-data";
import type { PlanningWorkspace } from "@/hooks/use-adr-planning";

const mocked = vi.hoisted(() => ({ workspaces: [] as PlanningWorkspace[] }));
vi.mock("@/hooks/use-adr-planning", () => ({
	useAdrPlanning: () => ({ workspaces: mocked.workspaces, errors: [], loading: false, refresh: vi.fn() }),
}));
vi.mock("@/components/adr-evidence-panel", () => ({ AdrEvidencePanel: () => <div>Requirement evidence</div> }));
vi.mock("@/components/adr-planning-chat", () => ({
	AdrPlanningChat: () => <button type="button">Start architecture chat</button>,
}));
function workspace(id: string): PlanningWorkspace {
	return {
		project: {
			id,
			name: id,
			path: `/repositories/${id}`,
			taskCounts: { backlog: 0, in_progress: 0, review: 0, trash: 0 },
		},
		documents: [],
		intakes: [],
		state: {
			repoPath: `/repositories/${id}`,
			statePath: `/state/${id}`,
			git: { currentBranch: "main", defaultBranch: "main", branches: ["main"] },
			board: createInitialBoardData(),
			sessions: {},
			revision: 1,
		},
	};
}
const adrDocument = {
	workspaceId: "docs",
	initiativeId: "shared-product",
	adrId: "ADR-1",
	path: "adr.md",
	title: "Decide a boundary",
	declaredStatus: "Proposed",
	sourceRevision: "a".repeat(40),
	sha256: "b".repeat(64),
	content: "# Decide a boundary",
	registeredAt: "2026-09-26T00:00:00Z",
};
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
			.find((button) => button.textContent?.includes(label))
			?.click();
	});
}
describe("ADR to repository-backed execution journey", () => {
	it("shows ADRs without slices, then follows an explicit cross-repository native card link", () => {
		const docs = workspace("docs");
		docs.documents = [adrDocument];
		const execution = workspace("execution");
		execution.state.board.columns[0]!.cards = [
			{
				id: "adr_card",
				title: "Implement interface",
				prompt: "Scope",
				startInPlanMode: false,
				baseRef: "main",
				createdAt: 1,
				updatedAt: 1,
			},
		];
		execution.intakes = [
			{
				workspaceId: "execution",
				taskId: "adr_card",
				intakeSha256: "c".repeat(64),
				intake: {
					schema_version: "ops.adr-backlog-intake.v1.2",
					work_id: "work-1",
					initiative_id: "shared-product",
					slice_id: "slice-1",
					title: "Implement interface",
					owner: { repository: "service", role: "implementation" },
					adr: {
						id: "ADR-1",
						source: {
							repository: "docs",
							path: "adr.md",
							revision: "a".repeat(40),
							sha256: "b".repeat(64),
							origin: "git",
						},
						declared_status: "Proposed",
						derived_status: null,
						verification: "unverified",
						reason: "Awaiting evidence",
					},
					// biome-ignore lint/suspicious/noThenProperty: The approved intake schema uses given/when/then scenarios.
					acceptance_scenarios: [{ given: "caller", when: "invoked", then: "works" }],
					entry_point: "caller",
					requester_outcome: "Interface works",
					dependencies: [],
					unresolved: ["Who owns the boundary?"],
					dispatch: { state: "not_authorized" },
				},
			},
		];
		mocked.workspaces = [docs];
		expect(groupPlanningAdrs(mocked.workspaces)[0]?.slices).toHaveLength(0);
		mocked.workspaces = [docs, execution];
		const open = vi.fn();
		act(() =>
			root.render(
				<AdrPlanningView
					projects={[docs.project, execution.project]}
					workspaceId="docs"
					board={docs.state.board}
					sessions={{}}
					config={null}
					messages={{}}
					onOpenTrack={open}
				/>,
			),
		);
		expect(container.textContent).toContain("2 repositories");
		click("ADR-1");
		expect(container.textContent).toContain("Who owns the boundary?");
		expect(container.textContent).toContain("Intake-derived: unknown");
		expect(container.textContent).toContain("snapshot may include uncommitted changes");
		click("Open native card");
		expect(open).toHaveBeenCalledWith("execution", "adr_card");
	});
});

it("bounds chat context and excludes historical source revisions", () => {
	const docs = workspace("docs");
	docs.documents = [
		{ ...adrDocument, content: "older secret phrase", registeredAt: "2026-09-25T00:00:00Z" },
		{ ...adrDocument, sha256: "c".repeat(64), content: "current context ".repeat(2000) },
	];
	const group = groupPlanningAdrs([docs])[0]!;
	const context = buildPlanningChatContext(group);
	expect(context.prompt.length).toBeLessThanOrEqual(24000);
	expect(context.prompt).not.toContain("older secret phrase");
	expect(context.notice).toContain("1 historical snapshots excluded");
	expect(context.notice).toContain("1 excerpts");
});
