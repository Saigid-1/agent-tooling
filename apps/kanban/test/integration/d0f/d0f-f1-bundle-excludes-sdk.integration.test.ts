// D0f F1, at the bundle: the board bundle that every image ships carries no
// @anthropic-ai/claude-agent-sdk code or sources. The per-image check
// (tests/image/agent_sdk_absence.py) is the falsifier the order names; this is the
// same property where it is cheapest to see, in the Kanban CI job, with the scanner's
// own markers.

import { existsSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { afterAll, beforeAll, describe, expect, it } from "vitest";

import {
	type BoardBundle,
	buildBoardBundle,
	createScratch,
	KANBAN_ROOT,
	lockfilePin,
	markersInBundle,
	readSourceMap,
	type Scratch,
	SDK_PACKAGE,
	SDK_SOURCE_FRAGMENT,
	scannerMarkers,
} from "./d0f-harness";

describe("D0f F1: the board bundle without claude-agent-sdk", () => {
	let scratch: Scratch;
	let bundle: BoardBundle;

	beforeAll(() => {
		scratch = createScratch("d0f-f1-");
		bundle = buildBoardBundle(scratch.path);
	}, 300_000);

	afterAll(() => {
		scratch?.cleanup();
	});

	it("F1: dist/cli.js, dist/index.js and their source maps carry no SDK code or source, and @clinebot/* stays bundled", () => {
		// GREEN-IF: no emitted file contains a scanner marker, no source map lists a file of
		// the SDK package, and @clinebot/llms and @clinebot/core are still among the bundle's
		// sources. Controls: each marker is in the SDK's own entry module at the lockfile's
		// pin, and the map that is checked is the board's (it lists src/cli.ts).
		const markers = scannerMarkers();
		const sdkEntry = join(KANBAN_ROOT, "node_modules", SDK_PACKAGE, "sdk.mjs");
		expect(existsSync(sdkEntry), `control: ${sdkEntry} (npm ci installs the lockfile's pin) is absent`).toBe(true);
		const sdkVersion = JSON.parse(readFileSync(join(dirname(sdkEntry), "package.json"), "utf8")).version;
		expect(sdkVersion, "control: the SDK in node_modules is the lockfile's pin").toBe(lockfilePin(SDK_PACKAGE));
		const sdkSource = readFileSync(sdkEntry, "utf8");
		expect(
			markers.filter((marker) => !sdkSource.includes(marker)),
			"control: every scanner marker is SDK code",
		).toEqual([]);

		const distDir = dirname(bundle.cliPath);
		const cliMap = readSourceMap(join(distDir, "cli.js.map"));
		expect(
			cliMap.sources.some((source) => source.endsWith("src/cli.ts")),
			"control: cli.js.map is the board's map",
		).toBe(true);

		const mapsListingSdk: Record<string, string[]> = {};
		for (const name of ["cli.js.map", "index.js.map"]) {
			const { sdkSources } = readSourceMap(join(distDir, name));
			if (sdkSources.length > 0) mapsListingSdk[name] = sdkSources;
		}
		const jsWithMarkers = Object.fromEntries(
			Object.entries(markersInBundle(distDir, markers)).filter(([name]) => name.endsWith(".js")),
		);
		expect({ mapsListingSdk, jsWithMarkers }, `no bundle file may carry ${SDK_SOURCE_FRAGMENT}`).toEqual({
			mapsListingSdk: {},
			jsWithMarkers: {},
		});

		const clinebot = ["@clinebot/llms/", "@clinebot/core/"].filter(
			(fragment) => !cliMap.sources.some((source) => source.includes(`node_modules/${fragment}`)),
		);
		expect(clinebot, "@clinebot/* stays bundled (the Principal's reading)").toEqual([]);
	});
});
