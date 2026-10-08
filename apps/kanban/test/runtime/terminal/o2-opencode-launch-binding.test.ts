// Order O2 (docs/work/orders/O2-opencode-third-harness.md): the board half of OpenCode as a third harness.
// R2/F2/F6: the adapter applies the desk task's launch binding, per launch, under O1's policy, and the
// launched process carries the disable flags, the binding's environment and no provider key. R1/R4: the
// plugin maps a root idle to Stop and compaction to PreCompact without ever holding OpenCode, and the
// terminal's exit runs SessionEnd. The launch binding command is a fake `kp-agent-launch` printing a
// prepare result in the shape tests/launch/test_o2_r2_opencode_prepare.py pins for the real one.
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
import { delimiter, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const ptySessionSpawnMock = vi.hoisted(() => vi.fn());

vi.mock("../../../src/terminal/pty-session.js", () => ({
	PtySession: {
		spawn: ptySessionSpawnMock,
	},
}));

import { prepareAgentLaunch } from "../../../src/terminal/agent-session-adapters";
import { TerminalSessionManager } from "../../../src/terminal/session-manager";

const POLICY = { edit: "allow", bash: "ask", webfetch: "ask", external_directory: "deny" };
const DESK = "desk:7b0c7a35-8f5e-4d55-9a3a-2f3f2a6f6e01";
const OTHER_DESK = "desk:1d6ad0c4-2f8e-4b8c-8c3e-5b8b7c0e9a02";
const SAVED = [
	"HOME",
	"PATH",
	"KANBAN_STORAGE_ROOT",
	"KANBAN_LAUNCH_BINDING_COMMAND",
	"KANBAN_HOOK_TASK_ID",
	"KANBAN_OPENCODE_OPENROUTER_KEY_FILE",
	"KP_AGENT_LAUNCH_HOOK_COMMAND",
	"OPENROUTER_API_KEY",
	"ANTHROPIC_API_KEY",
	"OPENCODE_AUTH_CONTENT",
	"OPENCODE_CONFIG",
	"O2_FAKE_PREPARE",
	"O2_FAKE_REQUESTS",
] as const;

let saved: Record<string, string | undefined> = {};
let root = "";
let home = "";
let work = "";

interface SpawnRequest {
	binary: string;
	args?: string[] | string;
	cwd: string;
	env?: Record<string, string | undefined>;
	onExit?: (event: { exitCode: number | null }) => void;
}

function writeExecutable(path: string, text: string): string {
	writeFileSync(path, text);
	chmodSync(path, 0o755);
	return path;
}

/** A recorder for the launch hook command: appends {stdin, cwd} as one JSON line to its first argument. */
function recorder(): { argv: string[]; out: string; entries: () => Array<Record<string, unknown>> } {
	const script = writeExecutable(
		join(root, "hook-recorder.cjs"),
		`const fs = require("node:fs");
let body = "";
process.stdin.on("data", (chunk) => (body += chunk));
process.stdin.on("end", () => {
	if (process.env.O2_HOOK_SLEEP_MS) { setTimeout(() => {}, Number(process.env.O2_HOOK_SLEEP_MS)); }
	fs.appendFileSync(process.argv[2], JSON.stringify({ stdin: body, cwd: process.cwd() }) + "\\n");
});
`,
	);
	const out = join(root, "hook-record.jsonl");
	return {
		argv: [process.execPath, script, out],
		out,
		entries: () =>
			existsSync(out)
				? readFileSync(out, "utf8")
						.split("\n")
						.filter(Boolean)
						.map((line) => {
							const entry = JSON.parse(line) as { stdin: string; cwd: string };
							return { ...(JSON.parse(entry.stdin) as Record<string, unknown>), processCwd: entry.cwd };
						})
				: [],
	};
}

interface Prepared {
	receipt: string;
	content: Record<string, unknown>;
	hook: string[];
	envAdditions: Record<string, string>;
	argvAdditions: string[];
}

/** The prepare result a real `kp-agent-launch` returns for an `opencode` board launch (one per desk). */
function prepared(desk: string, hook: string[], overrides: Partial<Prepared> = {}): Prepared {
	const launch = join(root, "launches", desk.slice(-12));
	mkdirSync(launch, { recursive: true });
	const receipt = join(launch, "launch.json");
	const content = overrides.content ?? {
		mcp: {
			kp_desk_memory: {
				type: "local",
				command: [
					"/usr/local/bin/python3",
					"-m",
					"kp_agent_tooling.memory_cli",
					"--config",
					join(launch, "memory.json"),
					"serve",
				],
				environment: { PYTHONPATH: "" },
				enabled: true,
			},
		},
	};
	return {
		receipt,
		content,
		hook,
		argvAdditions: overrides.argvAdditions ?? [],
		envAdditions: overrides.envAdditions ?? {
			KP_AGENT_LAUNCH_RECEIPT: receipt,
			OPENCODE_CONFIG_CONTENT: JSON.stringify(content),
			KP_AGENT_LAUNCH_HOOK_COMMAND: JSON.stringify(hook),
		},
	};
}

/** Point KANBAN_LAUNCH_BINDING_COMMAND at a fake `kp-agent-launch` that answers `prepare` with `result`. */
function bindWith(result: Prepared): void {
	const resultPath = join(root, `prepare-${Math.random().toString(16).slice(2)}.json`);
	writeFileSync(
		resultPath,
		JSON.stringify({
			receipt_path: result.receipt,
			native_session_id: null,
			argv_additions: result.argvAdditions,
			env_additions: result.envAdditions,
			files: { receipt: result.receipt, mcp_config: join(result.receipt, "..", "mcp.json"), hook_settings: null },
			deferred_files: { memory_config: join(result.receipt, "..", "memory.json") },
		}),
	);
	process.env.O2_FAKE_PREPARE = resultPath;
}

function launch(overrides: Partial<Parameters<typeof prepareAgentLaunch>[0]> = {}) {
	return prepareAgentLaunch({
		taskId: "o2-card",
		agentId: "opencode",
		binary: "opencode",
		args: [],
		cwd: work,
		prompt: "Fix the failing test",
		workspaceId: "o2-workspace",
		deskId: DESK,
		...overrides,
	});
}

beforeEach(() => {
	saved = Object.fromEntries(SAVED.map((name) => [name, process.env[name]]));
	root = realpathSync(mkdtempSync(join(tmpdir(), "o2-opencode-binding-")));
	home = join(root, "home");
	work = join(root, "work");
	mkdirSync(home, { recursive: true });
	mkdirSync(work, { recursive: true });
	for (const name of SAVED) if (name !== "PATH") delete process.env[name];
	process.env.HOME = home;
	const launcher = writeExecutable(
		join(root, "kp-agent-launch"),
		`#!${process.execPath}
const fs = require("node:fs");
let body = "";
process.stdin.on("data", (chunk) => (body += chunk));
process.stdin.on("end", () => {
	if (process.env.O2_FAKE_REQUESTS) fs.appendFileSync(process.env.O2_FAKE_REQUESTS, body + "\\n");
	process.stdout.write(fs.readFileSync(process.env.O2_FAKE_PREPARE, "utf8"));
});
`,
	);
	writeFileSync(join(root, "registry.json"), "{}\n"); // the board refuses while the named registry config is absent
	process.env.KANBAN_LAUNCH_BINDING_COMMAND = JSON.stringify([launcher, "--config", join(root, "registry.json")]);
	process.env.O2_FAKE_REQUESTS = join(root, "prepare-requests.jsonl");
	ptySessionSpawnMock.mockReset();
});

afterEach(() => {
	for (const name of SAVED) {
		if (saved[name] === undefined) delete process.env[name];
		else process.env[name] = saved[name];
	}
	rmSync(root, { recursive: true, force: true });
});

function boardConfig(env: Record<string, string | undefined>): Record<string, unknown> {
	return JSON.parse(readFileSync(env.OPENCODE_CONFIG as string, "utf8")) as Record<string, unknown>;
}

describe("O2 R2: the launch binding reaches the OpenCode process, per launch", () => {
	it("applies the binding's environment under O1's policy, never in the board-wide file", async () => {
		const hook = recorder();
		const result = prepared(DESK, hook.argv);
		bindWith(result);
		const prepared1 = await launch();
		const requests = readFileSync(process.env.O2_FAKE_REQUESTS as string, "utf8")
			.trim()
			.split("\n");
		expect(JSON.parse(requests[0] as string)).toMatchObject({ harness: "opencode", desk_id: DESK, source: "board" });
		for (const [name, value] of Object.entries(result.envAdditions)) {
			expect(prepared1.env[name], name).toBe(value);
		}
		expect(prepared1.args).not.toContain(result.receipt);
		// O1's policy is untouched: the generated file names it, and nothing in the binding can loosen it.
		const config = boardConfig(prepared1.env);
		expect(config.permission).toEqual(POLICY);
		expect(Object.keys(config).sort()).toEqual(["permission", "plugin"]);
		const text = readFileSync(prepared1.env.OPENCODE_CONFIG as string, "utf8");
		expect(text).not.toContain("kp_desk_memory");
		expect(text).not.toContain(result.receipt);
		for (const name of ["OPENCODE_DISABLE_PROJECT_CONFIG"]) expect(prepared1.env[name]).toBe("1");
		for (const name of ["OPENCODE_CONFIG_DIR", "OPENCODE_PERMISSION", "OPENCODE_TEST_MANAGED_CONFIG_DIR"]) {
			expect(prepared1.env[name], name).toBe("");
		}
		expect(prepared1.env.OPENCODE_TEST_HOME).toBe(home);
	});

	it("A1: a bound launch's global config directory is board-owned, empty and read-only", async () => {
		bindWith(prepared(DESK, recorder().argv));
		const prepared1 = await launch();
		const directory = join(prepared1.env.XDG_CONFIG_HOME as string, "opencode");
		expect(statSync(directory).mode & 0o777).toBe(0o555);
		expect(readdirSync(directory)).toEqual([]);
		expect((prepared1.env.XDG_CONFIG_HOME as string).startsWith(join(home, ".cline", "kanban"))).toBe(true);
		// An unbound launch keeps O1's directory, which O1's own instruments write into.
		const unbound = await launch({ deskId: undefined });
		expect(unbound.env.XDG_CONFIG_HOME).not.toBe(prepared1.env.XDG_CONFIG_HOME);
		// A file there would be a global config source beneath the policy: the next bound launch refuses.
		chmodSync(directory, 0o755);
		writeFileSync(join(directory, "opencode.json"), JSON.stringify({ permission: { "*": "allow" } }));
		await expect(launch()).rejects.toThrow(/must stay empty/);
		rmSync(join(directory, "opencode.json"));
	});

	it("F2: two launches bound to two desks each carry only their own server", async () => {
		const hook = recorder();
		const first = prepared(DESK, hook.argv);
		const second = prepared(OTHER_DESK, hook.argv);
		bindWith(first);
		const a = await launch({ taskId: "o2-card-a" });
		bindWith(second);
		const b = await launch({ taskId: "o2-card-b", deskId: OTHER_DESK });
		const servers = [a, b].map(
			(item) =>
				(JSON.parse(item.env.OPENCODE_CONFIG_CONTENT as string) as { mcp: Record<string, { command: string[] }> })
					.mcp,
		);
		expect(servers.map((server) => Object.keys(server))).toEqual([["kp_desk_memory"], ["kp_desk_memory"]]);
		expect(servers[0]?.kp_desk_memory?.command).toContain(join(first.receipt, "..", "memory.json"));
		expect(servers[1]?.kp_desk_memory?.command).toContain(join(second.receipt, "..", "memory.json"));
		expect(a.env.OPENCODE_CONFIG).toBe(b.env.OPENCODE_CONFIG);
		expect(Object.keys(boardConfig(a.env))).not.toContain("mcp");
	});

	it("refuses a binding whose content or environment could reach past the reviewed shape", async () => {
		const hook = recorder().argv;
		bindWith(prepared(DESK, hook));
		await expect(launch()).resolves.toBeDefined(); // positive control
		const server = (prepared(DESK, hook).content as { mcp: Record<string, unknown> }).mcp.kp_desk_memory;
		const refused: Array<[string, Partial<Prepared>]> = [
			["a permission key", { content: { mcp: { kp_desk_memory: server }, permission: { "*": "allow" } } }],
			["a second server", { content: { mcp: { kp_desk_memory: server, other: server } } }],
			["a plugin", { content: { mcp: { kp_desk_memory: server }, plugin: ["file:///x.js"] } }],
			["argv additions", { argvAdditions: ["--auto"] }],
		];
		for (const [name, overrides] of refused) {
			bindWith(prepared(DESK, hook, overrides));
			await expect(launch(), name).rejects.toThrow();
		}
		for (const variable of [
			"OPENCODE_PERMISSION",
			"OPENCODE_CONFIG",
			"XDG_CONFIG_HOME",
			"HOME",
			"OPENCODE_DISABLE_SHARE",
			"OPENROUTER_API_KEY",
		]) {
			const base = prepared(DESK, hook);
			bindWith({ ...base, envAdditions: { ...base.envAdditions, [variable]: "x" } });
			await expect(launch(), variable).rejects.toThrow(variable);
		}
		const base = prepared(DESK, hook);
		const { KP_AGENT_LAUNCH_HOOK_COMMAND: _dropped, ...withoutHook } = base.envAdditions;
		bindWith({ ...base, envAdditions: withoutHook });
		await expect(launch(), "no hook command").rejects.toThrow();
	});

	it("names the operator's OpenRouter key file by reference only, and only when it is readable", async () => {
		bindWith(prepared(DESK, recorder().argv));
		const keyFile = join(root, "secrets", "openrouter.key");
		mkdirSync(join(root, "secrets"), { recursive: true });
		const secret = "o2-not-a-real-key-6f1c";
		writeFileSync(keyFile, `${secret}\n`, { mode: 0o600 });
		process.env.KANBAN_OPENCODE_OPENROUTER_KEY_FILE = keyFile;
		const withKey = await launch();
		const config = boardConfig(withKey.env) as { provider?: { openrouter?: { options?: { apiKey?: string } } } };
		expect(config.provider?.openrouter?.options?.apiKey).toBe(`{file:${keyFile}}`);
		expect(readFileSync(withKey.env.OPENCODE_CONFIG as string, "utf8")).not.toContain(secret);
		expect(Object.values(withKey.env).some((value) => value?.includes(secret))).toBe(false);
		process.env.KANBAN_OPENCODE_OPENROUTER_KEY_FILE = join(root, "secrets", "absent.key");
		expect(boardConfig((await launch()).env).provider).toBeUndefined();
	});
});

describe("O2 F6: the OpenCode process the board launches", () => {
	it("carries the disable flags, the binding's environment and no provider key", async () => {
		const hook = recorder();
		const result = prepared(DESK, hook.argv);
		bindWith(result);
		const bin = join(root, "stub-bin");
		mkdirSync(bin, { recursive: true });
		const record = join(root, "stub-record.json");
		writeExecutable(
			join(bin, "opencode"),
			`#!${process.execPath}
require("node:fs").writeFileSync(${JSON.stringify(record)}, JSON.stringify(process.env));
`,
		);
		process.env.PATH = `${bin}${delimiter}${process.env.PATH ?? ""}`;
		process.env.OPENROUTER_API_KEY = "sk-or-o2-not-a-key";
		process.env.ANTHROPIC_API_KEY = "sk-ant-o2-not-a-key";
		process.env.OPENCODE_AUTH_CONTENT = '{"openrouter":{"type":"api","key":"o2-not-a-key"}}';
		ptySessionSpawnMock.mockImplementation((request: SpawnRequest) => {
			const env = Object.fromEntries(
				Object.entries(request.env ?? {}).filter((entry): entry is [string, string] => entry[1] !== undefined),
			);
			const args = typeof request.args === "string" ? [request.args] : (request.args ?? []);
			const ran = spawnSync(request.binary, args, { cwd: request.cwd, env, encoding: "utf8", timeout: 30_000 });
			if (ran.status !== 0) throw new Error(`stub opencode did not run: ${ran.stderr}`);
			return {
				pid: 1,
				write: vi.fn(),
				resize: vi.fn(),
				pause: vi.fn(),
				resume: vi.fn(),
				stop: vi.fn(),
				wasInterrupted: () => false,
			};
		});
		const manager = new TerminalSessionManager();
		await manager.startTaskSession({
			taskId: "o2-card",
			agentId: "opencode",
			binary: "opencode",
			args: [],
			autonomousModeEnabled: true,
			cwd: work,
			prompt: "Fix the failing test",
			workspaceId: "o2-workspace",
			deskId: DESK,
		});
		const env = JSON.parse(readFileSync(record, "utf8")) as Record<string, string>;
		for (const name of [
			"OPENCODE_DISABLE_AUTOUPDATE",
			"OPENCODE_DISABLE_SHARE",
			"OPENCODE_DISABLE_MODELS_FETCH",
			"OPENCODE_DISABLE_CLAUDE_CODE",
		]) {
			expect(env[name], name).toBe("1");
		}
		for (const [name, value] of Object.entries(result.envAdditions)) expect(env[name], name).toBe(value);
		for (const name of ["OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "OPENCODE_AUTH_CONTENT"]) {
			expect(env[name] ?? "", name).toBe("");
		}
		expect(JSON.stringify(env)).not.toContain("not-a-key");
	});
});

describe("O2 R1/R4: turn end in the plugin, session end at the terminal's exit", () => {
	type Hooks = {
		event: (input: { event: { type: string; properties: Record<string, unknown> } }) => Promise<void>;
		"experimental.session.compacting": (input: { sessionID: string }, output: unknown) => Promise<void>;
	};

	async function loadPlugin(pluginUrl: string, client: unknown, shellCalls: string[]): Promise<Hooks> {
		const copy = join(root, `kanban-${Math.random().toString(16).slice(2)}.mjs`);
		writeFileSync(copy, readFileSync(fileURLToPath(pluginUrl), "utf8"));
		const globals = globalThis as { __kanbanOpencodePluginV3?: boolean };
		delete globals.__kanbanOpencodePluginV3;
		const module = (await import(pathToFileURL(copy).href)) as {
			KanbanPlugin: (input: { $: unknown; client: unknown }) => Promise<Hooks>;
		};
		const shell = (strings: TemplateStringsArray, ...values: unknown[]) => {
			shellCalls.push(
				strings.reduce(
					(text, part, index) => text + part + (index < values.length ? String(values[index]) : ""),
					"",
				),
			);
			return Promise.resolve();
		};
		const hooks = await module.KanbanPlugin({ $: shell, client });
		delete globals.__kanbanOpencodePluginV3;
		return hooks;
	}

	async function waitFor<T>(read: () => T[], count: number, timeoutMs = 10_000): Promise<T[]> {
		const started = Date.now();
		while (read().length < count && Date.now() - started < timeoutMs) {
			await new Promise((resolve) => setTimeout(resolve, 50));
		}
		return read();
	}

	const client = {
		session: {
			list: async () => ({ data: [{ id: "ses_root" }, { id: "ses_child", parentID: "ses_root" }] }),
		},
	};

	it("maps a root idle to Stop and compaction to PreCompact, never a child's, with board hooks off too", async () => {
		const hook = recorder();
		bindWith(prepared(DESK, hook.argv));
		const prepared1 = await launch({ workspaceId: undefined });
		expect(prepared1.env.KANBAN_HOOK_TASK_ID).toBeUndefined();
		process.env.KP_AGENT_LAUNCH_HOOK_COMMAND = prepared1.env.KP_AGENT_LAUNCH_HOOK_COMMAND;
		const shellCalls: string[] = [];
		const plugin = boardConfig(prepared1.env).plugin as string[];
		const hooks = await loadPlugin(plugin[0] as string, client, shellCalls);
		const status = (sessionID: string, type: string) =>
			hooks.event({ event: { type: "session.status", properties: { sessionID, status: { type } } } });
		await status("ses_child", "busy");
		await status("ses_child", "idle");
		await status("ses_root", "busy");
		await status("ses_root", "idle");
		await hooks["experimental.session.compacting"]({ sessionID: "ses_child" }, { context: [] });
		await hooks["experimental.session.compacting"]({ sessionID: "ses_root" }, { context: [] });
		const entries = await waitFor(hook.entries, 2);
		await new Promise((resolve) => setTimeout(resolve, 300));
		const seen = hook.entries();
		expect(seen.map((entry) => [entry.hook_event_name, entry.session_id]).sort()).toEqual([
			["PreCompact", "ses_root"],
			["Stop", "ses_root"],
		]);
		expect(entries.every((entry) => entry.cwd === process.cwd())).toBe(true);
		expect(shellCalls, "board hooks are off without a task id").toEqual([]);
	});

	it("never waits on the hook command: a hook that runs for 15 s does not hold the event or the compaction", async () => {
		const hook = recorder();
		const slow = [process.execPath, "-e", "setTimeout(() => {}, 15000)"];
		bindWith(prepared(DESK, slow));
		const prepared1 = await launch();
		process.env.KP_AGENT_LAUNCH_HOOK_COMMAND = prepared1.env.KP_AGENT_LAUNCH_HOOK_COMMAND;
		process.env.KANBAN_HOOK_TASK_ID = "o2-card";
		const hooks = await loadPlugin((boardConfig(prepared1.env).plugin as string[])[0] as string, client, []);
		const started = Date.now();
		await hooks.event({
			event: { type: "session.status", properties: { sessionID: "ses_root", status: { type: "busy" } } },
		});
		await hooks.event({
			event: { type: "session.status", properties: { sessionID: "ses_root", status: { type: "idle" } } },
		});
		await hooks["experimental.session.compacting"]({ sessionID: "ses_root" }, { context: [] });
		expect(Date.now() - started).toBeLessThan(2_000);
		expect(hook.entries()).toEqual([]);
	});

	it("runs SessionEnd for the bound session when the board terminal exits", async () => {
		const hook = recorder();
		const result = prepared(DESK, hook.argv);
		bindWith(result);
		let onExit: SpawnRequest["onExit"];
		ptySessionSpawnMock.mockImplementation((request: SpawnRequest) => {
			onExit = request.onExit;
			return {
				pid: 1,
				write: vi.fn(),
				resize: vi.fn(),
				pause: vi.fn(),
				resume: vi.fn(),
				stop: vi.fn(),
				wasInterrupted: () => false,
			};
		});
		const manager = new TerminalSessionManager();
		const start = () =>
			manager.startTaskSession({
				taskId: "o2-card",
				agentId: "opencode",
				binary: "opencode",
				args: [],
				autonomousModeEnabled: true,
				cwd: work,
				prompt: "Fix the failing test",
				workspaceId: "o2-workspace",
				deskId: DESK,
			});
		// Never bound (no hook ran): the exit has no session to end.
		await start();
		onExit?.({ exitCode: 0 });
		await new Promise((resolve) => setTimeout(resolve, 500));
		expect(hook.entries()).toEqual([]);
		// Bound by its first hook: the launch directory records the session.
		writeFileSync(
			join(result.receipt, "..", "session.json"),
			JSON.stringify({
				schema_version: "agent-tooling.launch-session.v1",
				native_session_id: "ses_bound",
				transcript: null,
			}),
		);
		await start();
		onExit?.({ exitCode: 0 });
		const [entry] = await waitFor(hook.entries, 1);
		expect(entry).toMatchObject({ hook_event_name: "SessionEnd", session_id: "ses_bound", cwd: work });
	});
});
