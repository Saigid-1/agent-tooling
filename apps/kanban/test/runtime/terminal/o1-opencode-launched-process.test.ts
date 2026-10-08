// Order O1 (docs/work/orders/O1-opencode-board-launch.md, with amendment-1): the launched OpenCode
// process, as the board starts it. TerminalSessionManager.startTaskSession prepares the launch through
// the real adapter; the PTY spawn is replaced by running a stub `opencode` (first on PATH) with exactly
// the binary, argv, cwd and environment the manager handed the PTY (seam O1-S3 in ./o1-seams.ts). The
// stub records what it was started with and the content of the config its OPENCODE_CONFIG names.

import { spawnSync } from "node:child_process";
import { chmodSync, existsSync, mkdirSync, readFileSync, realpathSync, rmSync, writeFileSync } from "node:fs";
import { delimiter, join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const ptySessionSpawnMock = vi.hoisted(() => vi.fn());

vi.mock("../../../src/terminal/pty-session.js", () => ({
	PtySession: {
		spawn: ptySessionSpawnMock,
	},
}));

import { TerminalSessionManager } from "../../../src/terminal/session-manager";
import { O1_BOARD_LAUNCH, O1_POLICY, type O1TempHome, o1TempHome, writeCallerConfig } from "./o1-seams";

interface StubRecord {
	argv: string[];
	cwd: string;
	opencodeConfig: string | null;
	opencodeConfigText: string | null;
}

interface SpawnRequest {
	binary: string;
	args?: string[] | string;
	cwd: string;
	env?: Record<string, string | undefined>;
}

let temp: O1TempHome | null = null;
let savedPath: string | undefined;
let recordPath = "";

/** A stub `opencode` that records its argv, cwd, OPENCODE_CONFIG and that file's content, then exits 0. */
function installStubOpencode(root: string): void {
	const bin = join(root, "stub-bin");
	mkdirSync(bin, { recursive: true });
	recordPath = join(root, "stub-record.json");
	const stub = join(bin, "opencode");
	writeFileSync(
		stub,
		`#!${process.execPath}
const fs = require("node:fs");
const config = process.env.OPENCODE_CONFIG ?? null;
let text = null;
try { if (config) text = fs.readFileSync(config, "utf8"); } catch {}
fs.writeFileSync(${JSON.stringify(recordPath)}, JSON.stringify({
	argv: process.argv.slice(2), cwd: process.cwd(), opencodeConfig: config, opencodeConfigText: text,
}));
`,
	);
	chmodSync(stub, 0o755);
	savedPath = process.env.PATH;
	process.env.PATH = `${bin}${delimiter}${process.env.PATH ?? ""}`;
}

function mockSession(pid: number) {
	return {
		pid,
		write: vi.fn(),
		resize: vi.fn(),
		pause: vi.fn(),
		resume: vi.fn(),
		stop: vi.fn(),
		wasInterrupted: vi.fn(() => false),
	};
}

beforeEach(() => {
	temp = o1TempHome();
	installStubOpencode(temp.root);
	ptySessionSpawnMock.mockReset();
	ptySessionSpawnMock.mockImplementation((request: SpawnRequest) => {
		const env = Object.fromEntries(
			Object.entries(request.env ?? {}).filter((entry): entry is [string, string] => entry[1] !== undefined),
		);
		const args = typeof request.args === "string" ? [request.args] : (request.args ?? []);
		const result = spawnSync(request.binary, args, { cwd: request.cwd, env, encoding: "utf8", timeout: 30_000 });
		if (result.error || result.status !== 0) {
			throw new Error(`stub opencode did not run: ${result.error?.message ?? result.stderr}`);
		}
		return mockSession(4242);
	});
});

afterEach(() => {
	if (savedPath === undefined) delete process.env.PATH;
	else process.env.PATH = savedPath;
	temp?.restore();
	temp = null;
});

async function launchFromBoard(request: { env?: Record<string, string | undefined>; prompt?: string } = {}) {
	if (!temp) throw new Error("no temp home");
	rmSync(recordPath, { force: true });
	const manager = new TerminalSessionManager();
	await manager.startTaskSession({
		taskId: "o1-card",
		cwd: temp.cwd,
		...O1_BOARD_LAUNCH,
		...request,
	});
	expect(ptySessionSpawnMock, "the board did not start a process").toHaveBeenCalledTimes(1);
	expect(existsSync(recordPath), "the stub opencode was not the launched binary").toBe(true);
	return JSON.parse(readFileSync(recordPath, "utf8")) as StubRecord;
}

function sameFile(a: string, b: string): boolean {
	try {
		return realpathSync(a) === realpathSync(b);
	} catch {
		return a === b;
	}
}

describe("O1: the OpenCode process the board launches", () => {
	// Falsifier: the launched process's OPENCODE_CONFIG being the caller's file rather than the generated one.
	it("P2: the launched OPENCODE_CONFIG is the generated policy file whatever the caller set", async () => {
		if (!temp) throw new Error("no temp home");
		const caller = writeCallerConfig(temp.root);
		const cases: Array<{ name: string; boardEnv?: string; requestEnv?: Record<string, string> }> = [
			{ name: "no caller OPENCODE_CONFIG" },
			{ name: "the board process's OPENCODE_CONFIG", boardEnv: caller.path },
			{ name: "the start request's OPENCODE_CONFIG", requestEnv: { OPENCODE_CONFIG: caller.path } },
			{ name: "both", boardEnv: caller.path, requestEnv: { OPENCODE_CONFIG: caller.path } },
		];
		for (const testCase of cases) {
			ptySessionSpawnMock.mockClear();
			if (testCase.boardEnv) process.env.OPENCODE_CONFIG = testCase.boardEnv;
			else delete process.env.OPENCODE_CONFIG;
			try {
				const record = await launchFromBoard(testCase.requestEnv ? { env: testCase.requestEnv } : {});
				expect(record.opencodeConfig, `${testCase.name}: the launched process has no OPENCODE_CONFIG`).toBeTruthy();
				expect(
					sameFile(record.opencodeConfig as string, caller.path),
					`${testCase.name}: the launched OPENCODE_CONFIG is the caller's file`,
				).toBe(false);
				const launched = JSON.parse(record.opencodeConfigText ?? "null") as { permission?: unknown } | null;
				expect(launched?.permission, `${testCase.name}: the launched config is not the policy`).toEqual(O1_POLICY);
				expect(readFileSync(caller.path, "utf8"), `${testCase.name}: the caller's file was rewritten`).toBe(
					caller.text,
				);
			} finally {
				delete process.env.OPENCODE_CONFIG;
			}
		}
	});

	// Amendment-1 falsifier: a launch argv that uses `run` (as the process receives it).
	it("amendment-1 (c): the launched process gets the interactive form with --prompt, never `run`", async () => {
		const record = await launchFromBoard();
		expect(record.argv, `argv ${JSON.stringify(record.argv)} uses run`).not.toContain("run");
		const promptIndex = record.argv.indexOf("--prompt");
		expect(promptIndex, `argv ${JSON.stringify(record.argv)} has no --prompt`).toBeGreaterThanOrEqual(0);
		expect(record.argv[promptIndex + 1]).toContain(O1_BOARD_LAUNCH.prompt);
		expect(record.cwd && temp && sameFile(record.cwd, temp.cwd)).toBe(true);
	});
});
