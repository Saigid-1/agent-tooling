// T3 (docs/work/orders/T3-launch-binding-harness-profiles.md), Kanban side, with the
// Coordinator's dispatcher amendments of the T3 meet (SPEC-GAP resolutions):
//   A1: the carrier of capture hooks to merge with Kanban's own hooks is the prepare output's
//       `files.hook_settings`, a private JSON file shaped like Claude settings
//       ({"hooks": {"<Event>": [{"hooks": [{"type": "command", "command", "timeout"}]}]}}), written
//       for every harness whose profile injects hooks. With Kanban's hooks enabled the adapter
//       merges those groups into Kanban's `-c hooks.*` (and `hooks.state`); `argv_additions`
//       `-c hooks.*` overrides are used as-is only when Kanban's hooks are disabled.
//   A2: on the Kanban board card (and start request) the optional field is camelCase `deskId`;
//       snake_case `desk_id` is the field of the `kp-agent-launch prepare` stdin request.
//
// The adapter (prepareAgentLaunch) is driven with a stub KANBAN_LAUNCH_BINDING_COMMAND that
// records its argv and stdin and prints a `kp-agent-launch prepare` result. Real Claude or
// Codex CLIs are never started.
//
// P2: a Claude desk task's spawn carries the minted session id, the MCP config and ONE settings
//     file holding both Kanban's hooks and the capture hooks; the user's other MCP servers are
//     preserved (no --strict-mcp-config).
// P3: a Codex desk task's capture hooks (from `files.hook_settings`) merge with Kanban's own
//     Codex hook overrides; the MCP override and the receipt environment are carried. Run twice:
//     with the capture hooks only in `files.hook_settings`, and also repeated as `-c hooks.*`
//     overrides in `argv_additions` (which, with Kanban's hooks enabled, must not replace them).
// P5: tasks without a desk spawn exactly the base argv and environment (recorded below from the
//     adapter at the order's merge commit) and never call the binding command.
// Task model: the board card keeps an optional `deskId` (A2).
//
// Readings (reported under AMBIGUITY):
// - the adapter input names the task's desk; it carries both `deskId` and `desk_id` (same
//   value), since the order does not name the adapter-input field;
// - the binding command may be called with extra arguments (for example `prepare`); only its
//   stdin request is asserted;
// - Codex applies `-c` overrides in order and a later override replaces the value at its key
//   path, so the effective value per key is the last one;
// - Claude's --mcp-config is variadic, so the argument after the config must not be the prompt.
import { chmodSync, mkdirSync, mkdtempSync, readFileSync, realpathSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { runtimeBoardCardSchema } from "../../../src/core/api-contract";
import { prepareAgentLaunch } from "../../../src/terminal/agent-session-adapters";

const REAL_NODE = process.execPath;
const DESK = "desk:4b9f6c2e-8f3d-4a51-9e0b-2d7c1a5e6f30";
const NATIVE = "6f0a3c2e-1b4d-4e8f-9a7c-5d2e1f0b3a48";
const RECEIPT_ENV = "T3_TEST_LAUNCH_RECEIPT";
const PREPARE_FIELDS = [
	"desk_id",
	"harness",
	"model",
	"parent_session_id",
	"provider",
	"source",
	"task_id",
	"workspace",
];
const KANBAN_NODE = "'/usr/local/bin/node' '/Users/example/repo/dist/cli.js'";
const PROMPT = "Fix the lantern";

// Base spawn of an unassigned task at the order's merge commit (HOME and scratch root replaced by <ROOT>).
const BASE_CLAUDE = {
	binary: null,
	args: ["--settings", "<ROOT>/home/.cline/kanban/hooks/claude/settings.json", PROMPT],
	env: { FORCE_HYPERLINK: "1", KANBAN_HOOK_TASK_ID: "task-t3", KANBAN_HOOK_WORKSPACE_ID: "workspace-t3" },
};
const claudeHook = (event: string) => [
	{ type: "command", command: `${KANBAN_NODE} 'hooks' 'ingest' '--event' '${event}' '--source' 'claude'` },
];
const BASE_CLAUDE_SETTINGS = {
	hooks: {
		Stop: [{ hooks: claudeHook("to_review") }],
		SubagentStop: [{ hooks: claudeHook("activity") }],
		PreToolUse: [{ matcher: "*", hooks: claudeHook("activity") }],
		PermissionRequest: [{ matcher: "*", hooks: claudeHook("to_review") }],
		PostToolUse: [{ matcher: "*", hooks: claudeHook("to_in_progress") }],
		PostToolUseFailure: [{ matcher: "*", hooks: claudeHook("to_in_progress") }],
		Notification: [
			{ matcher: "permission_prompt", hooks: claudeHook("to_review") },
			{ matcher: "*", hooks: claudeHook("activity") },
		],
		UserPromptSubmit: [{ hooks: claudeHook("to_in_progress") }],
	},
};
const codexHook = (event: string) =>
	`command=${JSON.stringify(`${KANBAN_NODE} 'hooks' 'codex-hook' '--event' '${event}' '--source' 'codex'`)},timeout=5`;
const BASE_CODEX = {
	binary: "codex",
	args: [
		"-c",
		"check_for_update_on_startup=false",
		"--sandbox",
		"workspace-write",
		"--ask-for-approval",
		"on-request",
		"-c",
		"features.hooks=true",
		"-c",
		'hooks.state={"/<session-flags>/config.toml:user_prompt_submit:0:0"={trusted_hash="sha256:294984807e1feb49192c1ababa129596bed58e273627c790e76a03d54d2c7088"},"/<session-flags>/config.toml:stop:0:0"={trusted_hash="sha256:a0ec432d147f6ad1a8b4c9c2b52fa04c2ffe74b9e24dc35e4eed15d57e6896ee"},"/<session-flags>/config.toml:permission_request:0:0"={trusted_hash="sha256:debdfdf9d7158d1cb4a1cf339c282dd3dc1ff1368600857288876bc6c89736ce"},"/<session-flags>/config.toml:pre_tool_use:0:0"={trusted_hash="sha256:85b1455200aeeb0f72a65740e632e1e24f56dbcd03a0369358f33e78b53e214d"},"/<session-flags>/config.toml:post_tool_use:0:0"={trusted_hash="sha256:c1d447e6689a1bac6fac4d1a9fe88d4b2ef22f5d0216611273e21ab946c6306b"}}',
		"-c",
		`hooks.UserPromptSubmit=[{hooks=[{type="command",${codexHook("to_in_progress")}}]}]`,
		"-c",
		`hooks.Stop=[{hooks=[{type="command",${codexHook("to_review")}}]}]`,
		"-c",
		`hooks.PermissionRequest=[{matcher="*",hooks=[{type="command",${codexHook("to_review")}}]}]`,
		"-c",
		`hooks.PreToolUse=[{matcher="*",hooks=[{type="command",${codexHook("activity")}}]}]`,
		"-c",
		`hooks.PostToolUse=[{matcher="*",hooks=[{type="command",${codexHook("activity")}}]}]`,
		PROMPT,
	],
	env: { KANBAN_HOOK_TASK_ID: "task-t3", KANBAN_HOOK_WORKSPACE_ID: "workspace-t3" },
};

// The stub binding command: records argv + stdin, prints the prepared result for its harness.
const STUB = String.raw`
const fs = require("node:fs");
const [log, results] = process.argv.slice(2, 4);
let body = "";
process.stdin.on("data", (d) => (body += d));
process.stdin.on("end", () => {
	fs.appendFileSync(log, JSON.stringify({ argv: process.argv.slice(4), body }) + "\n");
	let harness = null;
	try { harness = JSON.parse(body).harness; } catch {}
	process.stdout.write(fs.readFileSync(results + "/" + harness + ".json", "utf8"));
});
`;

const ORIGINAL = {
	argv: [...process.argv],
	execArgv: [...process.execArgv],
	execPath: process.execPath,
	env: { ...process.env },
};
let root: string;
let workspace: string;
let log: string;
let receipt: string;
let mcpConfig: string;
let captureSettings: string;
let codexCaptureSettings: string;
let results: string;

const CAPTURE = (event: string) => `t3-capture-hook --event ${event}`;
const CAPTURE_SETTINGS = {
	hooks: {
		Stop: [{ hooks: [{ type: "command", command: CAPTURE("Stop"), timeout: 60 }] }],
		PreCompact: [{ hooks: [{ type: "command", command: CAPTURE("PreCompact"), timeout: 60 }] }],
	},
};
const CODEX_CAPTURE_SETTINGS = {
	hooks: {
		UserPromptSubmit: [{ hooks: [{ type: "command", command: CAPTURE("UserPromptSubmit"), timeout: 60 }] }],
		Stop: [{ hooks: [{ type: "command", command: CAPTURE("Stop"), timeout: 60 }] }],
	},
};
const MCP_CONFIG = { mcpServers: { "t3-desk-memory": { command: "/opt/t3/bin/t3-memory", args: ["serve"] } } };
const CODEX_MCP = 'mcp_servers.t3-desk-memory={command="/opt/t3/bin/t3-memory",args=["serve"]}';
const codexCapture = (event: string) =>
	`hooks.${event}=[{hooks=[{type="command",command=${JSON.stringify(CAPTURE(event))},timeout=60}]}]`;

beforeEach(() => {
	root = realpathSync(mkdtempSync(join(tmpdir(), "t3-launch-")));
	workspace = join(root, "workspace");
	mkdirSync(workspace);
	mkdirSync(join(root, "home"));
	const launch = join(root, "launch");
	mkdirSync(launch, { mode: 0o700 });
	receipt = join(launch, "receipt.json");
	mcpConfig = join(launch, "mcp.json");
	captureSettings = join(launch, "hooks.json");
	codexCaptureSettings = join(launch, "codex-hooks.json");
	for (const [path, value] of [
		[receipt, { stub: true }],
		[mcpConfig, MCP_CONFIG],
		[captureSettings, CAPTURE_SETTINGS],
		[codexCaptureSettings, CODEX_CAPTURE_SETTINGS],
	] as const) {
		writeFileSync(path, JSON.stringify(value));
		chmodSync(path, 0o600);
	}
	results = join(root, "results");
	mkdirSync(results);
	writeFileSync(
		join(results, "claude.json"),
		JSON.stringify({
			receipt_path: receipt,
			native_session_id: NATIVE,
			argv_additions: ["--session-id", NATIVE, "--mcp-config", mcpConfig, "--settings", captureSettings],
			env_additions: { [RECEIPT_ENV]: receipt },
			files: { receipt, mcp_config: mcpConfig, hook_settings: captureSettings },
		}),
	);
	codexResult(false);
	log = join(root, "calls.jsonl");
	writeFileSync(log, "");
	const stub = join(root, "binding-stub.cjs");
	writeFileSync(stub, STUB);

	process.env.HOME = join(root, "home");
	delete process.env.KANBAN_STORAGE_ROOT;
	delete process.env.KANBAN_ASSISTANT_MEMORY_WORKSPACE;
	delete process.env.KANBAN_ASSISTANT_MEMORY_COMMAND;
	process.env.KANBAN_LAUNCH_BINDING_COMMAND = JSON.stringify([REAL_NODE, stub, log, results]);
	process.argv = ["node", "/Users/example/repo/dist/cli.js"];
	process.execArgv = [];
	Object.defineProperty(process, "execPath", { configurable: true, value: "/usr/local/bin/node" });
});

afterEach(() => {
	process.argv = [...ORIGINAL.argv];
	process.execArgv = [...ORIGINAL.execArgv];
	Object.defineProperty(process, "execPath", { configurable: true, value: ORIGINAL.execPath });
	for (const key of Object.keys(process.env)) {
		if (!(key in ORIGINAL.env)) delete process.env[key];
	}
	Object.assign(process.env, ORIGINAL.env);
	rmSync(root, { recursive: true, force: true });
});

/** The stub's Codex prepare result (A1): capture hooks in `files.hook_settings`, optionally repeated as overrides. */
function codexResult(withArgvHooks: boolean) {
	writeFileSync(
		join(results, "codex.json"),
		JSON.stringify({
			receipt_path: receipt,
			native_session_id: null,
			argv_additions: [
				"-c",
				CODEX_MCP,
				...(withArgvHooks ? ["-c", codexCapture("UserPromptSubmit"), "-c", codexCapture("Stop")] : []),
			],
			env_additions: { [RECEIPT_ENV]: receipt },
			files: { receipt, hook_settings: codexCaptureSettings },
		}),
	);
}

function input(agentId: "claude" | "codex", desk: string | null) {
	return {
		taskId: "task-t3",
		agentId,
		binary: agentId,
		args: [],
		cwd: workspace,
		prompt: PROMPT,
		workspaceId: "workspace-t3",
		...(desk ? { deskId: desk, desk_id: desk } : {}),
	} as unknown as Parameters<typeof prepareAgentLaunch>[0];
}

function calls(): Array<{ argv: string[]; request: Record<string, unknown> }> {
	return readFileSync(log, "utf8")
		.split("\n")
		.filter(Boolean)
		.map((line) => {
			const entry = JSON.parse(line) as { argv: string[]; body: string };
			return { argv: entry.argv, request: JSON.parse(entry.body) as Record<string, unknown> };
		});
}

function normalized(launch: { binary?: string; args: string[]; env: Record<string, string | undefined> }) {
	return JSON.parse(
		JSON.stringify({ binary: launch.binary ?? null, args: launch.args, env: launch.env }).replaceAll(root, "<ROOT>"),
	);
}

function readSettings(value: string): { hooks: Record<string, Array<{ matcher?: string; hooks: unknown[] }>> } {
	return JSON.parse(value.trimStart().startsWith("{") ? value : readFileSync(value, "utf8"));
}

function commandsOf(value: unknown): string[] {
	if (Array.isArray(value)) return value.flatMap(commandsOf);
	if (value && typeof value === "object") {
		const record = value as Record<string, unknown>;
		const own = typeof record.command === "string" ? [record.command] : [];
		return [...own, ...Object.entries(record).flatMap(([k, v]) => (k === "command" ? [] : commandsOf(v)))];
	}
	return [];
}

function expectRequest(request: Record<string, unknown>, harness: string) {
	expect(Object.keys(request).sort()).toEqual(PREPARE_FIELDS);
	expect(request).toMatchObject({
		harness,
		desk_id: DESK,
		task_id: "task-t3",
		source: "board",
		parent_session_id: null,
	});
	expect(realpathSync(String(request.workspace))).toBe(workspace);
	for (const key of ["provider", "model"]) {
		expect(typeof request[key] === "string" && (request[key] as string).trim().length > 0, key).toBe(true);
	}
}

// --- a minimal TOML inline-value reader for Codex `-c key=value` overrides -------------------
function parseToml(text: string): unknown {
	let i = 0;
	const skip = () => {
		while (i < text.length && /\s/.test(text[i] as string)) i += 1;
	};
	const basic = (): string => {
		let j = i + 1;
		while (j < text.length && text[j] !== '"') j += text[j] === "\\" ? 2 : 1;
		const raw = text.slice(i, j + 1);
		i = j + 1;
		return JSON.parse(raw) as string;
	};
	const literal = (): string => {
		const end = text.indexOf("'", i + 1);
		const out = text.slice(i + 1, end);
		i = end + 1;
		return out;
	};
	const key = (): string => {
		skip();
		if (text[i] === '"') return basic();
		if (text[i] === "'") return literal();
		const match = /^[A-Za-z0-9_-]+/.exec(text.slice(i));
		if (!match) throw new Error(`bad key at ${i}: ${text.slice(i, i + 20)}`);
		i += match[0].length;
		return match[0];
	};
	const path = (): string[] => {
		const out = [key()];
		skip();
		while (text[i] === ".") {
			i += 1;
			out.push(key());
			skip();
		}
		return out;
	};
	const value = (): unknown => {
		skip();
		const c = text[i];
		if (c === '"') return basic();
		if (c === "'") return literal();
		if (c === "[") {
			i += 1;
			const out: unknown[] = [];
			for (;;) {
				skip();
				if (text[i] === "]") {
					i += 1;
					return out;
				}
				out.push(value());
				skip();
				if (text[i] === ",") i += 1;
			}
		}
		if (c === "{") {
			i += 1;
			const out: Record<string, unknown> = {};
			for (;;) {
				skip();
				if (text[i] === "}") {
					i += 1;
					return out;
				}
				const keys = path();
				skip();
				if (text[i] !== "=") throw new Error(`expected = at ${i}`);
				i += 1;
				setPath(out, keys, value());
				skip();
				if (text[i] === ",") i += 1;
			}
		}
		const match = /^(true|false|[+-]?\d+(?:\.\d+)?)/.exec(text.slice(i));
		if (!match) throw new Error(`unsupported TOML at ${i}: ${text.slice(i, i + 20)}`);
		i += match[0].length;
		return match[0] === "true" ? true : match[0] === "false" ? false : Number(match[0]);
	};
	const out = value();
	skip();
	if (i !== text.length) throw new Error(`trailing TOML at ${i}`);
	return out;
	function setPath(target: Record<string, unknown>, keys: string[], v: unknown) {
		let node = target;
		for (const k of keys.slice(0, -1)) {
			if (!node[k] || typeof node[k] !== "object") node[k] = {};
			node = node[k] as Record<string, unknown>;
		}
		node[keys[keys.length - 1] as string] = v;
	}
}

function keyPath(key: string): string[] {
	const probe = parseToml(`{${key}=0}`) as Record<string, unknown>;
	const out: string[] = [];
	let node: unknown = probe;
	while (node && typeof node === "object") {
		const [k, v] = Object.entries(node as Record<string, unknown>)[0] as [string, unknown];
		out.push(k);
		node = v;
	}
	return out;
}

/** Codex: apply -c overrides in order; a later override replaces the value at its key path. */
function codexConfig(args: string[]): { config: Record<string, unknown>; rest: string[] } {
	const config: Record<string, unknown> = {};
	const rest: string[] = [];
	for (let index = 0; index < args.length; index += 1) {
		const arg = args[index] as string;
		let override: string | null = null;
		if ((arg === "-c" || arg === "--config") && index + 1 < args.length) {
			override = args[index + 1] as string;
			index += 1;
		} else if (arg.startsWith("--config=")) {
			override = arg.slice("--config=".length);
		} else if (arg.startsWith("-c") && arg.length > 2 && !arg.startsWith("--")) {
			override = arg.slice(2);
		}
		if (override === null) {
			rest.push(arg);
			continue;
		}
		const at = override.indexOf("=");
		const keys = keyPath(override.slice(0, at));
		let parsed: unknown;
		try {
			parsed = parseToml(override.slice(at + 1));
		} catch {
			parsed = override.slice(at + 1);
		}
		let node = config;
		for (const k of keys.slice(0, -1)) {
			if (!node[k] || typeof node[k] !== "object") node[k] = {};
			node = node[k] as Record<string, unknown>;
		}
		node[keys[keys.length - 1] as string] = parsed;
	}
	return { config, rest };
}

function withoutPairs(args: string[], flags: string[]): string[] {
	const out: string[] = [];
	for (let index = 0; index < args.length; index += 1) {
		const arg = args[index] as string;
		if (flags.includes(arg)) {
			index += 1;
			continue;
		}
		if (flags.some((flag) => arg.startsWith(`${flag}=`))) continue;
		out.push(arg);
	}
	return out;
}

describe("T3 board task launch binding", () => {
	it("P2: a Claude desk task gets the minted session, its MCP config and one merged settings file", async () => {
		const base = await prepareAgentLaunch(input("claude", null));
		const baseSettings = readSettings(base.args[base.args.indexOf("--settings") + 1] as string);
		const launch = await prepareAgentLaunch(input("claude", DESK));

		const made = calls();
		expect(made).toHaveLength(1);
		expectRequest(made[0]?.request ?? {}, "claude");

		const args = launch.args;
		expect(args.filter((a) => a === "--session-id" || a.startsWith("--session-id="))).toHaveLength(1);
		expect(args[args.indexOf("--session-id") + 1]).toBe(NATIVE);
		expect(args).not.toContain("--strict-mcp-config");
		const mcpAt = args.indexOf("--mcp-config");
		expect(mcpAt).toBeGreaterThanOrEqual(0);
		const mcpValue = args[mcpAt + 1] as string;
		const servers = (
			JSON.parse(mcpValue.trimStart().startsWith("{") ? mcpValue : readFileSync(mcpValue, "utf8")) as {
				mcpServers: Record<string, { command?: string }>;
			}
		).mcpServers;
		expect(servers["t3-desk-memory"]?.command).toBe("/opt/t3/bin/t3-memory");
		const afterConfig = args[mcpAt + 2];
		expect(
			afterConfig === undefined || afterConfig.startsWith("-"),
			`--mcp-config would swallow ${afterConfig}`,
		).toBe(true);

		const settingsFlags = args.filter((a) => a === "--settings" || a.startsWith("--settings="));
		expect(settingsFlags).toHaveLength(1);
		const merged = readSettings(args[args.indexOf("--settings") + 1] as string);
		for (const [event, groups] of Object.entries(baseSettings.hooks)) {
			const present = commandsOf(merged.hooks[event] ?? []);
			for (const command of commandsOf(groups)) expect(present, `${event} lost ${command}`).toContain(command);
		}
		expect(commandsOf(merged.hooks.Stop)).toContain(CAPTURE("Stop"));
		expect(commandsOf(merged.hooks.PreCompact)).toContain(CAPTURE("PreCompact"));

		expect(launch.env[RECEIPT_ENV]).toBe(receipt);
		for (const [key, value] of Object.entries(base.env)) expect(launch.env[key], key).toBe(value);
		// Apart from the injected flags, the spawn is the base spawn.
		expect(withoutPairs(args, ["--settings", "--session-id", "--mcp-config"])).toEqual(
			withoutPairs(base.args, ["--settings"]),
		);

		// A later unassigned launch neither picks up nor strips this task's capture hooks.
		const later = await prepareAgentLaunch(input("claude", null));
		const laterSettings = readSettings(later.args[later.args.indexOf("--settings") + 1] as string);
		expect(commandsOf(laterSettings.hooks)).not.toContain(CAPTURE("Stop"));
		const again = readSettings(args[args.indexOf("--settings") + 1] as string);
		expect(commandsOf(again.hooks.Stop)).toContain(CAPTURE("Stop"));
		expect(commandsOf(again.hooks.Stop)).toEqual(expect.arrayContaining(commandsOf(baseSettings.hooks.Stop)));
	});

	for (const [label, withArgvHooks] of [
		["files.hook_settings only", false],
		["files.hook_settings and argv -c hooks.* overrides", true],
	] as const) {
		it(`P3: a Codex desk task merges its capture hooks with Kanban's own hooks (${label})`, async () => {
			codexResult(withArgvHooks);
			const base = await prepareAgentLaunch(input("codex", null));
			const launch = await prepareAgentLaunch(input("codex", DESK));

			const made = calls();
			expect(made).toHaveLength(1);
			expectRequest(made[0]?.request ?? {}, "codex");

			const before = codexConfig(base.args);
			const after = codexConfig(launch.args);
			const hooks = (after.config.hooks ?? {}) as Record<string, unknown>;
			const baseHooks = (before.config.hooks ?? {}) as Record<string, unknown>;
			for (const [event, value] of Object.entries(baseHooks)) {
				if (event === "state") continue;
				const present = commandsOf(hooks[event]);
				for (const command of commandsOf(value)) expect(present, `${event} lost ${command}`).toContain(command);
			}
			expect(commandsOf(hooks.Stop)).toContain(CAPTURE("Stop"));
			expect(commandsOf(hooks.UserPromptSubmit)).toContain(CAPTURE("UserPromptSubmit"));
			expect(hooks.state).toBeDefined();
			expect((after.config.features as Record<string, unknown> | undefined)?.hooks).toBe(true);
			const servers = (after.config.mcp_servers ?? {}) as Record<string, { command?: string }>;
			expect(servers["t3-desk-memory"]?.command).toBe("/opt/t3/bin/t3-memory");

			expect(launch.env[RECEIPT_ENV]).toBe(receipt);
			for (const [key, value] of Object.entries(base.env)) expect(launch.env[key], key).toBe(value);
			expect(after.rest).toEqual(before.rest);
			expect(launch.binary ?? null).toBe(base.binary ?? null);
		});
	}

	it("P5: an unassigned Claude task spawns the base argv, environment and Kanban hooks", async () => {
		const launch = await prepareAgentLaunch(input("claude", null));
		expect(calls()).toEqual([]);
		expect(normalized(launch)).toEqual(BASE_CLAUDE);
		expect(readSettings(launch.args[launch.args.indexOf("--settings") + 1] as string)).toEqual(BASE_CLAUDE_SETTINGS);
	});

	it("P5: an unassigned Codex task spawns the base argv and environment", async () => {
		const launch = await prepareAgentLaunch(input("codex", null));
		expect(calls()).toEqual([]);
		expect(normalized(launch)).toEqual(BASE_CODEX);
	});

	it("the task model keeps an optional deskId (A2)", () => {
		const card = {
			id: "task-t3",
			prompt: PROMPT,
			startInPlanMode: false,
			baseRef: "main",
			createdAt: 1,
			updatedAt: 1,
		};
		const assigned = runtimeBoardCardSchema.parse({ ...card, deskId: DESK }) as Record<string, unknown>;
		expect(assigned.deskId).toBe(DESK);
		expect("deskId" in (runtimeBoardCardSchema.parse(card) as Record<string, unknown>)).toBe(false);
	});
});
