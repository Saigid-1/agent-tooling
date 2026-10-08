// D0f F5: after the install action the Claude provider is registered and reaches the
// SDK. The probe is credential-free and makes no model call: a load hook preloaded into
// the board (d0f-harness sdkImportRecorderSource) records each load of the installed SDK's
// entry module (its package's exports "." or main) with the loading process's pid, the
// file URL and the package's version, for the published SDK at the pin and the stand-in
// at any other version alike. Native Cline sessions are disabled in this
// board (runtime-server createClineTaskSessionService({ disableLaunch: true })), so no
// board function makes a model call; the probe asks the board for every Claude-provider
// function it has (status, catalog, model list, selection) and then requires that the
// board process itself has imported the installed entry module from under the state root.

import { fileURLToPath } from "node:url";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import {
	type BoardBundle,
	buildBoardBundle,
	CLAUDE_PROVIDER_ID,
	catalogIds,
	createScratch,
	describeResult,
	installAcknowledged,
	isRunning,
	lockfilePin,
	openWorld,
	type RunningBoard,
	readSdkEvents,
	readStatus,
	type Scratch,
	SDK_PACKAGE,
	type SdkEvent,
	saveProvider,
	trpcQuery,
	type World,
	waitFor,
} from "./d0f-harness";
import { installSucceeded } from "./d0f-seams";

/** Every Claude-provider function the board has, short of a model call. */
async function exerciseClaudeProvider(board: RunningBoard): Promise<void> {
	await readStatus(board);
	await catalogIds(board);
	await trpcQuery(board, "runtime.getClineProviderModels", { providerId: CLAUDE_PROVIDER_ID });
	await saveProvider(board, { providerId: CLAUDE_PROVIDER_ID, modelId: "sonnet" });
}

function importsBy(world: World, pid: number | undefined): SdkEvent[] {
	return readSdkEvents(world.state.markerDir).filter((event) => event.event === "import" && event.pid === pid);
}

describe("D0f F5: the Claude provider reaches the SDK", () => {
	let scratch: Scratch;
	let bundle: BoardBundle;
	const worlds: World[] = [];

	beforeAll(() => {
		scratch = createScratch("d0f-f5-");
		bundle = buildBoardBundle(scratch.path);
	}, 300_000);

	afterAll(async () => {
		for (const world of worlds) await world.close().catch(() => undefined);
		scratch?.cleanup();
	});

	const open = async (name: string) => {
		const world = await openWorld(bundle, scratch.path, name);
		worlds.push(world);
		return world;
	};

	it("F5a: after the action, without a restart, the provider is registered and the board imports the installed SDK entry module", async () => {
		// GREEN-IF: after the acknowledged action, in the same board process: the status reads
		// installed at the pin, the catalog lists claude-code, and the board's own pid has
		// imported the installed SDK's entry module, a file under the state root, at the pin.
		// Control: nothing imported the stand-in before the action.
		const world = await open("f5a");
		const pid = world.board.child.pid;
		await exerciseClaudeProvider(world.board);
		expect(readSdkEvents(world.state.markerDir), "control: no SDK import before the action").toEqual([]);
		const installed = await installAcknowledged(world.board);
		expect(installSucceeded(installed.data), describeResult(installed)).toBe(true);
		await exerciseClaudeProvider(world.board);
		const imports = await waitFor(() => {
			const found = importsBy(world, pid);
			return found.length > 0 ? found : null;
		}, 15_000);
		expect(
			imports,
			`the board (pid ${pid}) imported the SDK; events: ${JSON.stringify(readSdkEvents(world.state.markerDir))}`,
		).not.toBeNull();
		const entry = (imports as SdkEvent[])[0] as SdkEvent;
		expect(
			fileURLToPath(entry.url).startsWith(`${world.state.stateDir}/`),
			`${entry.url} is under the state root`,
		).toBe(true);
		expect(entry.version, "the imported SDK is the installed pin").toBe(lockfilePin(SDK_PACKAGE));
		expect(await catalogIds(world.board), "registered").toContain(CLAUDE_PROVIDER_ID);
		expect((await readStatus(world.board)).status).toMatchObject({
			installed: true,
			version: lockfilePin(SDK_PACKAGE),
		});
		expect(isRunning(world.board) && world.board.child.pid === pid, "the same board process").toBe(true);
	}, 300_000);

	it("F5b: after a board restart the provider is registered again and the new board process imports the installed SDK", async () => {
		// GREEN-IF: after the action and a restart, the new board process's own pid imports the
		// installed SDK's entry module from under the state root, and the status and catalog
		// read installed and registered.
		const world = await open("f5b");
		const installed = await installAcknowledged(world.board, world.versions.older);
		expect(installSucceeded(installed.data), describeResult(installed)).toBe(true);
		const board = await world.restart();
		const pid = board.child.pid;
		await exerciseClaudeProvider(board);
		const imports = await waitFor(() => {
			const found = importsBy(world, pid);
			return found.length > 0 ? found : null;
		}, 15_000);
		expect(
			imports,
			`the restarted board (pid ${pid}) imported the SDK; events: ${JSON.stringify(readSdkEvents(world.state.markerDir))}`,
		).not.toBeNull();
		const entry = (imports as SdkEvent[])[0] as SdkEvent;
		expect(fileURLToPath(entry.url).startsWith(`${world.state.stateDir}/`)).toBe(true);
		expect(entry.version).toBe(world.versions.older);
		expect(await catalogIds(board)).toContain(CLAUDE_PROVIDER_ID);
		expect((await readStatus(board)).status).toMatchObject({ installed: true, version: world.versions.older });
	}, 300_000);
});
