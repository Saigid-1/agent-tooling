// O1 (docs/work/orders/O1-opencode-board-launch.md): OpenCode launches from the board under a named
// approval policy. These are the adapter-level properties; the effective policy on the pinned binary
// (A1) is test/integration/o1/o1-a1-effective-policy.integration.test.ts.
import { existsSync, mkdirSync, mkdtempSync, readFileSync, realpathSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import {
	getRuntimeAgentCatalogEntry,
	getRuntimeLaunchSupportedAgentCatalog,
	isRuntimeAgentLaunchSupported,
} from "../../../src/core/agent-catalog";
import { prepareAgentLaunch } from "../../../src/terminal/agent-session-adapters";

const POLICY = { edit: "allow", bash: "ask", webfetch: "ask", external_directory: "deny" };
const SAVED_ENV = [
	"HOME",
	"KANBAN_STORAGE_ROOT",
	"KANBAN_HOOK_TASK_ID",
	"OPENCODE_CONFIG",
	"OPENCODE_CONFIG_DIR",
	"OPENCODE_CONFIG_CONTENT",
	"OPENCODE_PERMISSION",
	"OPENCODE_DISABLE_PROJECT_CONFIG",
	"XDG_CONFIG_HOME",
] as const;

let scratch: string;
let home: string;
let work: string;
let saved: Record<string, string | undefined>;

beforeEach(() => {
	saved = Object.fromEntries(SAVED_ENV.map((name) => [name, process.env[name]]));
	for (const name of SAVED_ENV) delete process.env[name];
	scratch = realpathSync(mkdtempSync(join(tmpdir(), "o1-opencode-launch-")));
	home = join(scratch, "home");
	work = join(scratch, "work");
	mkdirSync(home, { recursive: true });
	mkdirSync(work, { recursive: true });
	process.env.HOME = home;
});

afterEach(() => {
	for (const name of SAVED_ENV) {
		if (saved[name] === undefined) delete process.env[name];
		else process.env[name] = saved[name];
	}
	rmSync(scratch, { recursive: true, force: true });
});

function launch(overrides: Partial<Parameters<typeof prepareAgentLaunch>[0]> = {}) {
	return prepareAgentLaunch({
		taskId: "o1-task",
		agentId: "opencode",
		binary: "opencode",
		args: [],
		cwd: work,
		prompt: "",
		workspaceId: "o1-workspace",
		...overrides,
	});
}

function generatedConfig(env: Record<string, string | undefined>): Record<string, unknown> {
	const path = env.OPENCODE_CONFIG;
	expect(path).toBeTruthy();
	return JSON.parse(readFileSync(path as string, "utf8")) as Record<string, unknown>;
}

describe("O1 P3: the gate opens for OpenCode only", () => {
	it("lists opencode among the launch-supported harnesses with empty base arguments", () => {
		expect(isRuntimeAgentLaunchSupported("opencode")).toBe(true);
		expect(getRuntimeLaunchSupportedAgentCatalog().map((entry) => entry.id)).toContain("opencode");
		expect(getRuntimeAgentCatalogEntry("opencode")?.baseArgs).toEqual([]);
	});

	it("still refuses every harness without a verified policy", async () => {
		for (const agentId of ["gemini", "droid", "kiro", "cline"] as const) {
			expect(isRuntimeAgentLaunchSupported(agentId)).toBe(false);
			await expect(launch({ agentId, binary: getRuntimeAgentCatalogEntry(agentId)?.binary })).rejects.toThrow(
				/not ready/,
			);
		}
	});
});

describe("O1 P1: the generated opencode.json names the policy", () => {
	it("carries exactly the four permission keys, in order, with and without board hooks", async () => {
		for (const workspaceId of ["o1-workspace", undefined]) {
			const prepared = await launch({ workspaceId });
			const config = generatedConfig(prepared.env);
			expect(config.permission).toEqual(POLICY);
			expect(Object.keys(config.permission as object)).toEqual(Object.keys(POLICY));
		}
	});

	it("keeps the board plugin in the generated file", async () => {
		const prepared = await launch();
		const plugins = generatedConfig(prepared.env).plugin as string[];
		expect(plugins).toHaveLength(1);
		expect(existsSync(fileURLToPath(plugins[0] as string))).toBe(true);
	});
});

describe("O1 P2: argument hardening and the caller's OPENCODE_CONFIG", () => {
	it("refuses caller-supplied arguments and alternate executables before the adapter adds its own", async () => {
		for (const args of [["--auto"], ["run"], ["--agent", "build"], ["--model", "x/y"], ["--pure"]]) {
			await expect(launch({ args })).rejects.toThrow(/not reviewed/);
		}
		for (const binary of ["/usr/local/bin/opencode", "opencode-dev"]) {
			await expect(launch({ binary })).rejects.toThrow(/catalog binary/);
		}
	});

	it("always points the launched OPENCODE_CONFIG at the generated file, whatever the caller set", async () => {
		const callerConfig = join(scratch, "caller-opencode.json");
		writeFileSync(callerConfig, JSON.stringify({ model: "openrouter/caller-model", permission: { "*": "allow" } }));
		process.env.OPENCODE_CONFIG = callerConfig;
		const prepared = await launch({ env: { OPENCODE_CONFIG: callerConfig } });
		expect(prepared.env.OPENCODE_CONFIG).not.toBe(callerConfig);
		expect(generatedConfig(prepared.env).permission).toEqual(POLICY);
		// The caller's file stays the model-resolution base.
		expect(prepared.args[prepared.args.indexOf("--model") + 1]).toBe("openrouter/caller-model");
	});
});

describe("O1 A1: no other configuration source can loosen the launched policy", () => {
	it("disables project config, moves the global config to a board-owned directory and clears outranking sources", async () => {
		const callerEnv = {
			OPENCODE_CONFIG_DIR: join(scratch, "caller-dir"),
			OPENCODE_CONFIG_CONTENT: JSON.stringify({ permission: { "*": "allow" } }),
			OPENCODE_PERMISSION: JSON.stringify({ "*": "allow" }),
			OPENCODE_DISABLE_PROJECT_CONFIG: "0",
			XDG_CONFIG_HOME: join(home, ".config"),
		};
		Object.assign(process.env, callerEnv);
		const prepared = await launch({ env: callerEnv });
		expect(prepared.env.OPENCODE_DISABLE_PROJECT_CONFIG).toBe("1");
		expect(prepared.env.OPENCODE_CONFIG_DIR).toBe("");
		expect(prepared.env.OPENCODE_CONFIG_CONTENT).toBe("");
		expect(prepared.env.OPENCODE_PERMISSION).toBe("");
		expect(prepared.env.OPENCODE_TEST_MANAGED_CONFIG_DIR).toBe("");
		// An empty OPENCODE_TEST_HOME would make OpenCode read the worktree's .opencode as HOME's.
		expect(prepared.env.OPENCODE_TEST_HOME).toBe(home);
		// Board-owned: inside the board's runtime home (HOME/.cline/kanban when no storage root is set),
		// never the user's own config home.
		const configHome = prepared.env.XDG_CONFIG_HOME as string;
		expect(configHome).toBeTruthy();
		expect(configHome).not.toBe(join(home, ".config"));
		expect(configHome.startsWith(join(home, ".cline", "kanban"))).toBe(true);
	});

	it("refuses to launch while a HOME .opencode config is present, before writing the launch", async () => {
		const dotDirectory = join(home, ".opencode");
		const loosening = JSON.stringify({ permission: { bash: "allow", "*": "allow" } });
		const agent = "---\npermission:\n  bash: allow\n---\nbuild\n";
		for (const [name, content] of [
			["opencode.json", loosening],
			["opencode.jsonc", loosening],
			["agent/build.md", agent],
		]) {
			const path = join(dotDirectory, name as string);
			mkdirSync(join(path, ".."), { recursive: true });
			writeFileSync(path, content as string);
			await expect(launch()).rejects.toThrow(/\.opencode/);
			expect(existsSync(join(home, ".cline", "kanban", "hooks", "opencode", "opencode.json"))).toBe(false);
			rmSync(join(dotDirectory, (name as string).split("/")[0] as string), { recursive: true });
		}
		// What OpenCode's installer and dependency step leave there is not configuration.
		mkdirSync(join(dotDirectory, "bin"), { recursive: true });
		writeFileSync(join(dotDirectory, ".gitignore"), "node_modules\n");
		await expect(launch()).resolves.toBeTruthy();
	});

	it("refuses OpenCode plan mode, whose plan agent the policy's edit rule would open", async () => {
		await expect(launch({ startInPlanMode: true })).rejects.toThrow(/plan mode/);
	});
});

describe("O1 amendment-1 (c): where an approval is noticed", () => {
	it("launches the interactive form with --prompt, never `opencode run`", async () => {
		const prepared = await launch({ prompt: "Fix the bug" });
		expect(prepared.args).not.toContain("run");
		expect(prepared.args).toContain("--prompt");
		expect(prepared.args[prepared.args.indexOf("--prompt") + 1]).toBe("Fix the bug");
	});

	it("moves the card to review when the plugin's event hook receives permission.asked", async () => {
		const prepared = await launch();
		const pluginPath = fileURLToPath((generatedConfig(prepared.env).plugin as string[])[0] as string);
		const commands: string[] = [];
		const shell = (strings: TemplateStringsArray, ...values: unknown[]) => {
			commands.push(
				strings.reduce(
					(text, part, index) => text + part + (index < values.length ? String(values[index]) : ""),
					"",
				),
			);
			return Promise.resolve();
		};
		const client = { session: { list: async () => ({ data: [{ id: "ses_root" }] }) } };
		const source = readFileSync(pluginPath, "utf8").replace("export const KanbanPlugin =", "return");
		const globals = globalThis as { __kanbanOpencodePluginV3?: boolean };
		delete globals.__kanbanOpencodePluginV3;
		process.env.KANBAN_HOOK_TASK_ID = "o1-task";
		const factory = new Function(source)() as (input: { $: typeof shell; client: typeof client }) => Promise<{
			event: (input: { event: { type: string; properties: Record<string, unknown> } }) => Promise<void>;
		}>;
		const hooks = await factory({ $: shell, client });
		delete globals.__kanbanOpencodePluginV3;
		await hooks.event({
			event: { type: "session.status", properties: { sessionID: "ses_root", status: { type: "busy" } } },
		});
		expect(commands.some((command) => command.includes("to_in_progress"))).toBe(true);
		commands.length = 0;
		await hooks.event({
			event: {
				type: "permission.asked",
				properties: { id: "per_1", sessionID: "ses_root", permission: "bash", patterns: ["ls"] },
			},
		});
		expect(commands.some((command) => command.includes("to_review"))).toBe(true);
	});
});
