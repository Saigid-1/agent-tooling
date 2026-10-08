// D0f F3: the install action is clickable and user-initiated. The board fetches only
// through the stand-in npm registry (S5), which records every request and serves the SDK
// at the lockfile's pin (the published tarball: the action checks the lockfile's
// integrity), and a stand-in at an older version a user may choose and at a newer
// `latest`; so "installed the pin" and "installed latest" differ.

import { join } from "node:path";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import {
	type BoardBundle,
	buildBoardBundle,
	CLAUDE_PROVIDER_ID,
	catalogIds,
	createScratch,
	describeResult,
	findInstalledSdk,
	installAcknowledged,
	isRunning,
	KANBAN_ROOT,
	lockfilePin,
	openWorld,
	PROVIDER_PACKAGE,
	readStatus,
	type Scratch,
	SDK_PACKAGE,
	saveProvider,
	sha256,
	sleep,
	trpcMutation,
	trpcQuery,
	type World,
} from "./d0f-harness";
import { D0F_SEAMS, installInput, installSucceeded, pinnedNoticeText } from "./d0f-seams";

describe("D0f F3: the install action", () => {
	let scratch: Scratch;
	let bundle: BoardBundle;
	const worlds: World[] = [];

	beforeAll(() => {
		scratch = createScratch("d0f-f3-");
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

	it("F3a: nothing is fetched or installed without the acknowledged action: not at start, restart, selection or an unacknowledged call", async () => {
		// GREEN-IF: across start, idling, the status, the catalog, the Claude provider's model
		// list, selecting it, a restart, and two install calls without an acknowledgement (no
		// input; acknowledged=false), the registry receives no request and no SDK appears under
		// the state root; both calls are refused. Control: the acknowledged action then does
		// reach the registry and install, so the zero above was measured on a channel that
		// carries the install.
		const world = await open("f3a");
		const refusals: Record<string, string> = {};
		const noFetch = (when: string) => {
			expect(world.registry.requests, `registry requests ${when}`).toEqual([]);
			expect(findInstalledSdk(world.state.stateDir), `SDK under the state root ${when}`).toEqual([]);
		};
		await sleep(3_000);
		noFetch("after start");
		const { status } = await readStatus(world.board);
		await catalogIds(world.board);
		await trpcQuery(world.board, "runtime.getClineProviderModels", { providerId: CLAUDE_PROVIDER_ID });
		await saveProvider(world.board, { providerId: CLAUDE_PROVIDER_ID, modelId: "sonnet" });
		noFetch("after status, catalog, model list and selection");
		await world.restart();
		await sleep(3_000);
		await readStatus(world.board);
		noFetch("after a restart");

		const notAcknowledgements: Record<string, Record<string, unknown>> = {
			"no input": {},
			"acknowledged=false": installInput({ acknowledged: false, noticeSha256: status.noticeSha256 }),
		};
		for (const [label, input] of Object.entries(notAcknowledgements)) {
			const result = await trpcMutation(world.board, D0F_SEAMS.installMutation, input);
			if (result.status === 200 && installSucceeded(result.data)) refusals[label] = describeResult(result);
			noFetch(`after the call with ${label}`);
		}
		expect(refusals, "calls that installed without an acknowledgement").toEqual({});
		expect((await readStatus(world.board)).status.installed).toBe(false);

		const acknowledged = await installAcknowledged(world.board);
		expect(
			installSucceeded(acknowledged.data),
			`control: the acknowledged action: ${describeResult(acknowledged)}`,
		).toBe(true);
		expect(world.registry.requests.length, "control: the acknowledged action reached the registry").toBeGreaterThan(
			0,
		);
		expect(findInstalledSdk(world.state.stateDir).length, "control: the SDK is under the state root").toBeGreaterThan(
			0,
		);
	}, 300_000);

	it("F3b: the notice is pinned in the repo, says what the order requires, and is exactly what the board shows", async () => {
		// GREEN-IF: the pinned file exists and says that we neither warrant nor license the use
		// of @anthropic-ai/claude-agent-sdk and that the user obtains it under Anthropic's
		// terms; the board's notice is that file's text, byte for byte, and names its sha256.
		const path = join(KANBAN_ROOT, D0F_SEAMS.noticeModule);
		let text = "";
		expect(() => {
			text = pinnedNoticeText();
		}, `the pinned notice ${path}`).not.toThrow();
		const required: Record<string, RegExp> = {
			"names the package": /@anthropic-ai\/claude-agent-sdk/,
			"disclaims warranty": /\bwarrant/i,
			"disclaims licensing": /\blicen[cs]/i,
			"is a negation": /\b(not|neither|nor|no)\b/i,
			"names Anthropic's terms": /Anthropic[\s\S]{0,80}\bterms\b|\bterms\b[\s\S]{0,80}Anthropic/i,
		};
		expect(
			Object.entries(required)
				.filter(([, pattern]) => !pattern.test(text))
				.map(([label]) => label),
			"what the notice lacks",
		).toEqual([]);
		const world = await open("f3b");
		const { status } = await readStatus(world.board);
		expect(status.notice === text, "the shown notice is the pinned file's text, byte for byte").toBe(true);
		expect(status.noticeSha256).toBe(sha256(text));
	}, 180_000);

	it("F3c: the acknowledged action installs the lockfile's pin by default, and the user's chosen version on request", async () => {
		// GREEN-IF: with no version chosen, the action installs the SDK at the lockfile's pin
		// (not the registry's newer latest) under the state root, the status reads installed
		// at the pin, and a provider package it fetches is at its lockfile pin; with a version
		// chosen, it installs that version.
		const world = await open("f3c");
		const pin = lockfilePin(SDK_PACKAGE);
		const first = await installAcknowledged(world.board);
		expect(installSucceeded(first.data), describeResult(first)).toBe(true);
		const sdkDownloads = world.registry.downloads().filter((item) => item.startsWith(`${SDK_PACKAGE}@`));
		expect(sdkDownloads, "SDK tarballs fetched").toEqual([`${SDK_PACKAGE}@${pin}`]);
		const providerDownloads = world.registry.downloads().filter((item) => item.startsWith(`${PROVIDER_PACKAGE}@`));
		for (const item of providerDownloads) {
			expect(item, "a fetched provider package is at its lockfile pin").toBe(
				`${PROVIDER_PACKAGE}@${lockfilePin(PROVIDER_PACKAGE)}`,
			);
		}
		expect(
			findInstalledSdk(world.state.stateDir).map((found) => found.version),
			"the SDK under the state root",
		).toEqual([pin]);
		expect((await readStatus(world.board)).status).toMatchObject({ installed: true, version: pin });

		const chosen = await installAcknowledged(world.board, world.versions.older);
		expect(installSucceeded(chosen.data), describeResult(chosen)).toBe(true);
		expect(world.registry.downloads()).toContain(`${SDK_PACKAGE}@${world.versions.older}`);
		expect(findInstalledSdk(world.state.stateDir).map((found) => found.version)).toEqual([world.versions.older]);
		expect((await readStatus(world.board)).status).toMatchObject({ installed: true, version: world.versions.older });
		expect(world.registry.downloads(), "never the registry's latest").not.toContain(
			`${SDK_PACKAGE}@${world.versions.latest}`,
		);
	}, 300_000);

	it("F3d: no install or upgrade happens at start: an installed older version survives a restart unchanged", async () => {
		// GREEN-IF: after the user installs a version other than the pin, a restart sends no
		// request to the registry, the installed version is unchanged, and the status reads
		// installed at that version.
		const world = await open("f3d");
		const chosen = await installAcknowledged(world.board, world.versions.older);
		expect(installSucceeded(chosen.data), describeResult(chosen)).toBe(true);
		const before = world.registry.requests.length;
		await world.restart();
		await sleep(3_000);
		const { status } = await readStatus(world.board);
		await catalogIds(world.board);
		expect(world.registry.requests.slice(before), "registry requests at and after the restart").toEqual([]);
		expect(findInstalledSdk(world.state.stateDir).map((found) => found.version)).toEqual([world.versions.older]);
		expect({ installed: status.installed, version: status.version }).toEqual({
			installed: true,
			version: world.versions.older,
		});
		expect(isRunning(world.board)).toBe(true);
	}, 300_000);

	it("F3e: an acknowledgement installs only when it names the notice the board shows", async () => {
		// The conservative reading of "the notice shown AND acknowledged": the acknowledgement
		// carries the sha256 of the notice it acknowledges. GREEN-IF: acknowledged=true naming
		// no notice, or naming any other text, is refused with no request to the registry and
		// no SDK under the state root. Control: naming the shown notice installs.
		const world = await open("f3e");
		const { status } = await readStatus(world.board);
		const unbound: Record<string, Record<string, unknown>> = {
			"no notice named": installInput({ acknowledged: true }),
			"another notice named": installInput({ acknowledged: true, noticeSha256: sha256(`${status.notice}\nedited`) }),
		};
		const installed: Record<string, string> = {};
		for (const [label, input] of Object.entries(unbound)) {
			const result = await trpcMutation(world.board, D0F_SEAMS.installMutation, input);
			if (result.status === 200 && installSucceeded(result.data)) installed[label] = describeResult(result);
			expect(world.registry.requests, `registry requests after the call with ${label}`).toEqual([]);
			expect(findInstalledSdk(world.state.stateDir), `SDK after the call with ${label}`).toEqual([]);
		}
		expect(installed, "acknowledgements that did not name the shown notice but installed").toEqual({});
		const named = await installAcknowledged(world.board);
		expect(installSucceeded(named.data), `control: naming the shown notice: ${describeResult(named)}`).toBe(true);
		expect(findInstalledSdk(world.state.stateDir).length, "control: installed").toBeGreaterThan(0);
	}, 300_000);
});
