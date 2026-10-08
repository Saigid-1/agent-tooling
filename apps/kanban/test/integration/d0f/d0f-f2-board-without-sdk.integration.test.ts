// D0f F2: the board works without the SDK. The board is the shipped bundle beside
// node-pty alone, as the image ships it, so the SDK is absent unless the bundle carries
// it; each test first proves that absence (at main the bundle carries the SDK, so there
// is no SDK-absent board to test, and each test fails on that precondition).

import { afterAll, beforeAll, describe, expect, it } from "vitest";

import {
	addWorkspace,
	type BoardBundle,
	buildBoardBundle,
	CLAUDE_PROVIDER_ID,
	catalogIds,
	createScratch,
	describeResult,
	isRunning,
	lockfilePin,
	openWorld,
	readStatus,
	type Scratch,
	SDK_PACKAGE,
	saveProvider,
	sdkAbsenceProblems,
	sha256,
	trpcQuery,
	type World,
} from "./d0f-harness";

// Non-Claude providers selected in F2c: Anthropic's API (not the SDK), two that sit in the
// same lazily loaded @clinebot/llms module as the Claude provider (openai-codex, opencode,
// dify), and the default.
const SELECTED_PROVIDERS = ["cline", "anthropic", "openai-native", "openai-codex", "opencode", "dify", "litellm"];

describe("D0f F2: the board without the SDK", () => {
	let scratch: Scratch;
	let bundle: BoardBundle;
	let absence: string[];
	const worlds: World[] = [];

	beforeAll(() => {
		scratch = createScratch("d0f-f2-");
		bundle = buildBoardBundle(scratch.path);
		absence = sdkAbsenceProblems(bundle);
	}, 300_000);

	afterAll(async () => {
		for (const world of worlds) await world.close().catch(() => undefined);
		scratch?.cleanup();
	});

	const open = async (name: string) => {
		expect(absence, "precondition: the SDK is absent from the board (bundle and resolution)").toEqual([]);
		const world = await openWorld(bundle, scratch.path, name);
		worlds.push(world);
		return world;
	};

	it("F2a: the board starts and serves, and its board functions answer, with the SDK absent", async () => {
		// GREEN-IF: with the SDK absent, the board starts, GET / answers 200, and the runtime
		// config, a project add and list, and that workspace's state all answer 200.
		const world = await open("f2a");
		const root = await fetch(`${world.board.url}/`);
		expect(root.status, "GET /").toBe(200);
		const config = await trpcQuery(world.board, "runtime.getConfig");
		expect(config.status, describeResult(config)).toBe(200);
		const workspaceId = await addWorkspace(world.board, world.root);
		const projects = await trpcQuery(world.board, "projects.list");
		expect(projects.status, describeResult(projects)).toBe(200);
		expect(JSON.stringify(projects.data)).toContain(workspaceId);
		const state = await trpcQuery(world.board, "workspace.getState", undefined, workspaceId);
		expect(state.status, describeResult(state)).toBe(200);
		expect(isRunning(world.board), "the board is still running").toBe(true);
	}, 180_000);

	it("F2b: the Claude provider reads not installed, with the install action offered", async () => {
		// GREEN-IF: the catalog lists claude-code; the status reads installed=false with no
		// version; the offer carries the default version (the lockfile's pin) and the notice,
		// which is the pinned file's text, with its sha256.
		const world = await open("f2b");
		expect(await catalogIds(world.board), "the provider catalog").toContain(CLAUDE_PROVIDER_ID);
		const { status } = await readStatus(world.board);
		expect({ installed: status.installed, version: status.version }).toEqual({ installed: false, version: null });
		expect(status.defaultVersion, "the offered default is the lockfile's pin").toBe(lockfilePin(SDK_PACKAGE));
		expect(status.notice.trim().length, "the offer carries the notice").toBeGreaterThan(0);
		expect(status.noticeSha256, "the offer names its notice").toBe(sha256(status.notice));
		expect(isRunning(world.board)).toBe(true);
	}, 180_000);

	it("F2c: every non-Claude provider stays listed, and selecting one works", async () => {
		// GREEN-IF: the board's catalog lists every provider @clinebot/llms defines except
		// possibly claude-code, and saving each selected non-Claude provider answers 200 with
		// that provider as the selection.
		const world = await open("f2c");
		const ClineCore = (await import("@clinebot/core")) as unknown as {
			Llms: { getAllProviders: () => Promise<{ id: string }[]> };
		};
		const defined = (await ClineCore.Llms.getAllProviders()).map((provider) => provider.id);
		expect(defined, "control: the provider list is read").toContain("anthropic");
		const listed = await catalogIds(world.board);
		expect(
			defined.filter((id) => id !== CLAUDE_PROVIDER_ID && !listed.includes(id)),
			"non-Claude providers missing from the board",
		).toEqual([]);
		for (const providerId of SELECTED_PROVIDERS) {
			const saved = await saveProvider(world.board, { providerId, modelId: "d0f-model" });
			expect(saved.status, `${providerId}: ${describeResult(saved)}`).toBe(200);
			expect((saved.data as { providerId?: string } | undefined)?.providerId, providerId).toBe(providerId);
		}
		expect(isRunning(world.board)).toBe(true);
	}, 180_000);

	it("F2d: selecting the not-installed Claude provider neither crashes nor blocks the board", async () => {
		// GREEN-IF: after claude-code is selected while not installed, the board is still
		// running and serving, the status still reads not installed, and another provider can
		// be selected.
		const world = await open("f2d");
		const selected = await saveProvider(world.board, { providerId: CLAUDE_PROVIDER_ID, modelId: "sonnet" });
		expect(selected.status, `selection answered (200 or a refusal): ${describeResult(selected)}`).toBeLessThan(500);
		expect(isRunning(world.board), "the board survived the selection").toBe(true);
		expect((await fetch(`${world.board.url}/`)).status).toBe(200);
		expect((await readStatus(world.board)).status.installed).toBe(false);
		const other = await saveProvider(world.board, { providerId: "anthropic", modelId: "d0f-model" });
		expect(other.status, describeResult(other)).toBe(200);
		expect(await catalogIds(world.board)).toContain("anthropic");
	}, 180_000);
});
