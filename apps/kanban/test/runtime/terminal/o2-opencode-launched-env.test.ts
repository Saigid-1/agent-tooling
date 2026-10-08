// Order O2 (docs/work/orders/O2-opencode-third-harness.md), F6: the OpenCode process the board launches.
// Seams: tests/o2_seams.py (read here through `python3 tests/o2_seams.py --json`), O2-S9, S14, S17.
//
// TerminalSessionManager.startTaskSession prepares each launch through the real adapter. A desk task's
// binding comes from a stub KANBAN_LAUNCH_BINDING_COMMAND that prints a `kp-agent-launch prepare` result for
// harness `opencode` (a hook-strategy result: no native id, the receipt in the environment, the files a
// prepare writes). The PTY spawn is replaced by running a stub `opencode` (first on PATH) with exactly the
// binary, argv, cwd and environment the manager handed the PTY (O1-S3); the stub records them.
//
// GREEN-IF (F6):
// - every OpenCode launch, bound or not, carries OPENCODE_DISABLE_AUTOUPDATE=1 and OPENCODE_DISABLE_SHARE=1;
// - a desk task's launch carries every variable of the binding's env_additions, with its value;
// - the operator's provider key (a sentinel in the key file the board is told of, O2-S14) appears nowhere:
//   not in the launched environment (no provider-key variable either), not in any file under the test root
//   (the receipt directory, the board's generated config and plugin, any per-launch config), and not in
//   anything the board wrote to its console during the launch.
// Mutants: drop OPENCODE_DISABLE_AUTOUPDATE, drop OPENCODE_DISABLE_SHARE, drop the binding (the adapter
// ignores input.launchBinding), write the key's value instead of a file reference.
// RED at base: the adapter never applies the binding for OpenCode and sets no OPENCODE_DISABLE_SHARE.

import { spawnSync } from "node:child_process";
import {
	chmodSync,
	existsSync,
	mkdirSync,
	mkdtempSync,
	readdirSync,
	readFileSync,
	realpathSync,
	rmSync,
	statSync,
	writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { delimiter, dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const ptySessionSpawnMock = vi.hoisted(() => vi.fn());

vi.mock("../../../src/terminal/pty-session.js", () => ({
	PtySession: {
		spawn: ptySessionSpawnMock,
	},
}));

import { TerminalSessionManager } from "../../../src/terminal/session-manager";

interface Seams {
	harness: string;
	disable_env: Record<string, string>;
	launch_binding_variable: string;
	board_launch: { agentId: "opencode"; binary: string; args: string[]; autonomousModeEnabled: boolean; workspaceId: string };
	key_file_variable: string;
	key_env_names: string[];
	events: string[];
}

const REPO_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "../../../../..");
const SEAMS = loadSeams();
const DESK = "desk:7c1e9b20-4d3a-4f6e-8a2b-9d0c5e1f2a36";
const BINDING_PROBE = { name: "O2_BINDING_PROBE", value: "o2-binding-probe-value" };
const RECEIPT_ENV = "KP_AGENT_LAUNCH_RECEIPT";
const KEY_SENTINEL = "sk-or-v1-o2f6sentinel0123456789abcdef";

function loadSeams(): Seams {
	const python = process.env.PYTHON ?? "python3";
	const result = spawnSync(python, [join(REPO_ROOT, "tests", "o2_seams.py"), "--json"], { encoding: "utf8" });
	if (result.status !== 0) {
		throw new Error(`O2 seams unreadable (${python} tests/o2_seams.py --json): ${result.stderr ?? result.error}`);
	}
	return JSON.parse(result.stdout) as Seams;
}

// The stub binding command: records argv + stdin, prints the prepared result for the requested harness.
const BINDING_STUB = String.raw`
const fs = require("node:fs");
const [log, results] = process.argv.slice(2, 4);
let body = "";
process.stdin.on("data", (d) => (body += d));
process.stdin.on("end", () => {
	fs.appendFileSync(log, JSON.stringify({ argv: process.argv.slice(4), body }) + "\n");
	let harness = null;
	try { harness = JSON.parse(body).harness; } catch {}
	const path = results + "/" + harness + ".json";
	if (!fs.existsSync(path)) {
		process.stdout.write(JSON.stringify({ status: "error", category: "harness_unavailable", message: "none" }));
		process.exit(3);
	}
	process.stdout.write(fs.readFileSync(path, "utf8"));
});
`;

interface StubRecord {
	argv: string[];
	cwd: string;
	env: Record<string, string>;
}

interface SpawnRequest {
	binary: string;
	args?: string[] | string;
	cwd: string;
	env?: Record<string, string | undefined>;
}

const SAVED_ENV = { ...process.env };
let root = "";
let workspace = "";
let recordPath = "";
let bindingLog = "";
let keyFile = "";
let consoleText: string[] = [];

function writePrivate(path: string, value: unknown): void {
	writeFileSync(path, typeof value === "string" ? value : JSON.stringify(value));
	chmodSync(path, 0o600);
}

/** The stub `kp-agent-launch prepare` result for harness `opencode`: hook strategy, receipt in env. */
function writeOpenCodeBinding(results: string): Record<string, string> {
	const launch = join(root, "state", "launches", "o2-launch");
	mkdirSync(launch, { recursive: true, mode: 0o700 });
	const receipt = join(launch, "launch.json");
	const hooks = join(launch, "hooks.json");
	const mcp = join(launch, "mcp.json");
	const command = (event: string) => `o2-capture-hook --event ${event}`;
	writePrivate(receipt, { stub: true, harness: SEAMS.harness, desk_id: DESK });
	writePrivate(hooks, {
		hooks: Object.fromEntries(
			SEAMS.events.map((event) => [event, [{ hooks: [{ type: "command", command: command(event), timeout: 30 }] }]]),
		),
	});
	writePrivate(mcp, { mcpServers: { kp_desk_memory: { command: "/opt/o2/bin/python3", args: ["-m", "memory"] } } });
	// O2 meet (seam): FEATURE's prepare carries OpenCode's server and hook argv in env_additions
	// (mcp config_content_env, hooks plugin_env), in the shape launch_binding.prepare writes them.
	const envAdditions = {
		[RECEIPT_ENV]: receipt,
		[BINDING_PROBE.name]: BINDING_PROBE.value,
		OPENCODE_CONFIG_CONTENT: JSON.stringify({
			mcp: {
				kp_desk_memory: {
					type: "local",
					command: ["/opt/o2/bin/python3", "-m", "memory"],
					environment: {},
					enabled: true,
				},
			},
		}),
		KP_AGENT_LAUNCH_HOOK_COMMAND: JSON.stringify(["o2-capture-hook"]),
	};
	writeFileSync(
		join(results, `${SEAMS.harness}.json`),
		JSON.stringify({
			receipt_path: receipt,
			native_session_id: null,
			argv_additions: [],
			env_additions: envAdditions,
			files: { receipt, mcp_config: mcp, hook_settings: hooks },
			deferred_files: { memory_config: join(launch, "memory.json") },
		}),
	);
	return envAdditions;
}

function installStubOpencode(): void {
	const bin = join(root, "stub-bin");
	mkdirSync(bin, { recursive: true });
	recordPath = join(root, "stub-record.json");
	const stub = join(bin, "opencode");
	writeFileSync(
		stub,
		`#!${process.execPath}
const fs = require("node:fs");
fs.writeFileSync(${JSON.stringify(recordPath)}, JSON.stringify({ argv: process.argv.slice(2), cwd: process.cwd(), env: process.env }));
`,
	);
	chmodSync(stub, 0o755);
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
	root = realpathSync(mkdtempSync(join(tmpdir(), "o2-f6-")));
	workspace = join(root, "worktree");
	const home = join(root, "home");
	mkdirSync(workspace, { recursive: true });
	mkdirSync(home, { recursive: true });
	for (const key of [
		"KANBAN_STORAGE_ROOT",
		"OPENCODE_CONFIG",
		"XDG_CONFIG_HOME",
		"APPDATA",
		"LOCALAPPDATA",
		"KANBAN_ASSISTANT_MEMORY_WORKSPACE",
		"KANBAN_ASSISTANT_MEMORY_COMMAND",
		...SEAMS.key_env_names,
		...Object.keys(SEAMS.disable_env),
	]) {
		delete process.env[key];
	}
	process.env.HOME = home;
	const results = join(root, "binding-results");
	mkdirSync(results);
	bindingLog = join(root, "binding-calls.jsonl");
	writeFileSync(bindingLog, "");
	const bindingStub = join(root, "binding-stub.cjs");
	writeFileSync(bindingStub, BINDING_STUB);
	process.env[SEAMS.launch_binding_variable] = JSON.stringify([process.execPath, bindingStub, bindingLog, results]);
	keyFile = join(root, "operator-config", "models", "openrouter.key");
	mkdirSync(dirname(keyFile), { recursive: true });
	writePrivate(keyFile, `${KEY_SENTINEL}\n`);
	process.env[SEAMS.key_file_variable] = keyFile;
	installStubOpencode();
	consoleText = [];
	for (const method of ["log", "info", "warn", "error", "debug"] as const) {
		vi.spyOn(console, method).mockImplementation((...args: unknown[]) => {
			consoleText.push(args.map(String).join(" "));
		});
	}
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
		return mockSession(4343);
	});
});

afterEach(() => {
	vi.restoreAllMocks();
	for (const key of Object.keys(process.env)) {
		if (!(key in SAVED_ENV)) delete process.env[key];
	}
	Object.assign(process.env, SAVED_ENV);
	rmSync(root, { recursive: true, force: true });
});

async function launchFromBoard(deskId: string | null): Promise<StubRecord> {
	rmSync(recordPath, { force: true });
	const manager = new TerminalSessionManager();
	await manager.startTaskSession({
		taskId: "o2-card",
		cwd: workspace,
		prompt: "Fix the failing test",
		...SEAMS.board_launch,
		...(deskId ? { deskId } : {}),
	});
	expect(ptySessionSpawnMock, "the board did not start a process").toHaveBeenCalledTimes(1);
	expect(existsSync(recordPath), "the stub opencode was not the launched binary").toBe(true);
	return JSON.parse(readFileSync(recordPath, "utf8")) as StubRecord;
}

function bindingCalls(): Array<Record<string, unknown>> {
	return readFileSync(bindingLog, "utf8")
		.split("\n")
		.filter(Boolean)
		.map((line) => JSON.parse((JSON.parse(line) as { body: string }).body) as Record<string, unknown>);
}

function filesUnder(directory: string): string[] {
	const found: string[] = [];
	for (const entry of readdirSync(directory, { withFileTypes: true })) {
		const path = join(directory, entry.name);
		if (entry.isDirectory()) found.push(...filesUnder(path));
		else if (entry.isFile() && statSync(path).size <= 8 * 1024 * 1024) found.push(path);
	}
	return found;
}

function expectDisableFlags(record: StubRecord, label: string): void {
	for (const [name, value] of Object.entries(SEAMS.disable_env)) {
		expect(record.env[name], `${label}: the launched process lacks ${name}=${value}`).toBe(value);
	}
}

describe("O2 F6: the OpenCode process the board launches", () => {
	it("an OpenCode launch without a desk carries the disable flags", async () => {
		const record = await launchFromBoard(null);
		expectDisableFlags(record, "unbound launch");
		expect(bindingCalls(), "a task without a desk asked for a launch binding").toEqual([]);
	});

	it("a desk task's launch carries the disable flags and the binding's env, and no provider key", async () => {
		const envAdditions = writeOpenCodeBinding(join(root, "binding-results"));
		const record = await launchFromBoard(DESK);
		const calls = bindingCalls();
		expect(calls.map((call) => [call.harness, call.desk_id, call.source])).toEqual([[SEAMS.harness, DESK, "board"]]);
		expectDisableFlags(record, "bound launch");
		for (const [name, value] of Object.entries(envAdditions)) {
			expect(record.env[name], `the launched process lacks the binding's ${name}`).toBe(value);
		}
		for (const name of SEAMS.key_env_names) {
			expect(record.env[name], `the launched process carries ${name}`).toBeUndefined();
		}
		const inEnv = Object.entries(record.env).filter(
			([name, value]) => name.includes(KEY_SENTINEL) || value.includes(KEY_SENTINEL),
		);
		expect(inEnv.map(([name]) => name), "the provider key is in the launched environment").toEqual([]);
		const inFiles = filesUnder(root).filter(
			(path) => path !== keyFile && readFileSync(path).includes(Buffer.from(KEY_SENTINEL)),
		);
		expect(inFiles, "the provider key was written into a file").toEqual([]);
		expect(
			consoleText.filter((line) => line.includes(KEY_SENTINEL)),
			"the provider key reached the board's console",
		).toEqual([]);
	});
});
