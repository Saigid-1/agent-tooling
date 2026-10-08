// O1 A1 (docs/work/orders/O1-opencode-board-launch.md, amendment-1): the EFFECTIVE policy of a board
// OpenCode launch, on the real pinned binary in order O3's image. It runs only when
// AGENT_TOOLING_TEST_IMAGE_OPENCODE names a built `opencode` target (the variable O3's image suite reads).
//
// The board adapter (this source) prepares every launch on the host. Each fixture's directory is mounted
// into the image at the same path, and fixtures/run-in-image.mjs runs, per fixture, `opencode debug config`,
// `opencode debug agent build` and the launched interactive form under a pseudo-terminal against a loopback
// stub model, with no network.
//
// Loosening shapes (the order's and amendment-1 (a)'s), each in every placement OpenCode or
// src/terminal/opencode-paths.ts names: the project opencode.json; the XDG global opencode.json,
// opencode.jsonc and config.json; APPDATA/LOCALAPPDATA; the project and a global file together. HOME's
// .opencode is refused by the adapter (asserted here); its unguarded rows only record why.
//
// What the instruments add, none of it permission-bearing: the stub provider and its model, written into
// the board-owned global config directory the launch names; OPENCODE_DISABLE_MODELS_FETCH;
// npm_config_registry at the stub (OpenCode's background dependency step fails fast offline); OPENCODE_DB
// inside the container; O1_HOOK_LOG for the hook recorder that stands in for `kanban hooks ingest`.
import { spawnSync } from "node:child_process";
import { mkdirSync, mkdtempSync, readFileSync, realpathSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { afterAll, beforeAll, describe, expect, it } from "vitest";

import { prepareAgentLaunch } from "../../../src/terminal/agent-session-adapters";

const IMAGE = process.env.AGENT_TOOLING_TEST_IMAGE_OPENCODE?.trim() || "";
const FIXTURES = join(dirname(fileURLToPath(import.meta.url)), "fixtures");
const STUB_PORT = 18_080;
const POLICY = { edit: "allow", bash: "ask", webfetch: "ask", external_directory: "deny" };
const EVERY_KEY = [
	"read",
	"edit",
	"glob",
	"grep",
	"list",
	"bash",
	"task",
	"external_directory",
	"todowrite",
	"question",
	"webfetch",
	"websearch",
	"lsp",
	"doom_loop",
	"skill",
];
const SHAPES: Record<string, Record<string, unknown>> = {
	"every-key-allow": Object.fromEntries(EVERY_KEY.map((key) => [key, "allow"])),
	"star-allow": { "*": "allow" },
	"bash-then-star": { bash: "allow", "*": "allow" },
	"bash-pattern-map": { bash: { "*": "allow" } },
};
// Placement -> files (relative to the fixture root) and the board environment that makes OpenCode look there.
const PLACEMENTS: Record<string, { files: string[]; env?: Record<string, string> }> = {
	project: { files: ["work/opencode.json"] },
	"xdg-opencode.json": { files: ["home/.config/opencode/opencode.json"] },
	"xdg-opencode.jsonc": { files: ["home/.config/opencode/opencode.jsonc"] },
	"xdg-config.json": { files: ["home/.config/opencode/config.json"] },
	appdata: {
		files: ["home/appdata/opencode/opencode.json", "home/localappdata/opencode/opencode.json"],
		env: { APPDATA: "<home>/appdata", LOCALAPPDATA: "<home>/localappdata" },
	},
	"project+xdg": { files: ["work/opencode.json", "home/.config/opencode/opencode.json"] },
};
const DOT_PLACEMENTS: Record<string, string[]> = {
	"dot-opencode.json": ["home/.opencode/opencode.json"],
	"dot-opencode.jsonc": ["home/.opencode/opencode.jsonc"],
	"project+dot": ["work/opencode.json", "home/.opencode/opencode.json"],
};
const CASES = ["bash", "webfetch", "external"] as const;

interface Rule {
	permission: string;
	pattern: string;
	action: string;
}

interface Fixture {
	id: string;
	kind: "guarded" | "control" | "unguarded-dot";
	placement: string;
	shape: string;
	root: string;
	cwd: string;
	outside: string;
	hookLog: string;
	fixtureFiles: string[];
	env: Record<string, string>;
	runs: Array<{ case: string; argv: string[]; markers: string[] }>;
}

interface RunResult {
	case: string;
	reason: string;
	stub: Array<{ kind?: string; lastRole?: string | null }>;
	hooks: Array<{ event?: string; metadata?: { notification_type?: string } | null }>;
	markers: Record<string, boolean>;
}

interface FixtureResult {
	id: string;
	config: { status: number | null; stdout: string; stderr: string };
	agent: { status: number | null; stdout: string; stderr: string };
	runs: RunResult[];
}

function wildcard(pattern: string, value: string): boolean {
	const source = [...pattern].map((char) =>
		char === "*" ? ".*" : char === "?" ? "." : char.replace(/[\\^$.|+()[\]{}]/g, "\\$&"),
	);
	return new RegExp(`^${source.join("")}$`, "s").test(value);
}

/** OpenCode's evaluation: the last rule whose permission and pattern both match decides. */
function effective(rules: Rule[], permission: string, pattern: string): string {
	let action = "ask";
	for (const rule of rules) {
		if (wildcard(rule.permission, permission) && wildcard(rule.pattern, pattern)) action = rule.action;
	}
	return action;
}

/** The file of one `message=loading path=` log line; OpenCode quotes a value that holds a space. */
function loadedPath(line: string): string {
	const value = line.slice(line.indexOf("path=") + "path=".length);
	return value.startsWith('"')
		? (JSON.parse(value.slice(0, value.indexOf('"', 1) + 1)) as string)
		: (value.split(" ")[0] ?? "");
}

function writeJson(path: string, value: unknown): void {
	mkdirSync(dirname(path), { recursive: true });
	writeFileSync(path, JSON.stringify(value));
}

function imagePath(): string {
	const inspected = spawnSync("docker", ["image", "inspect", "--format", "{{json .Config.Env}}", IMAGE], {
		encoding: "utf8",
	});
	const env = JSON.parse(inspected.stdout || "[]") as string[];
	return (env.find((entry) => entry.startsWith("PATH=")) ?? "PATH=/usr/local/bin:/usr/bin:/bin").slice("PATH=".length);
}

describe.skipIf(!IMAGE)("O1 A1: the effective policy on the pinned binary", () => {
	let scratch = "";
	const saved = {
		home: process.env.HOME,
		root: process.env.KANBAN_STORAGE_ROOT,
		argv: process.argv,
		execPath: process.execPath,
		execArgv: process.execArgv,
	};
	const fixtures: Fixture[] = [];
	const refusals: Array<{ placement: string; shape: string; refused: boolean; message: string }> = [];
	let results: FixtureResult[] = [];
	let imagePathValue = "";

	async function prepareFixture(
		id: string,
		kind: Fixture["kind"],
		placement: string,
		shape: string,
		files: string[],
		boardEnv: Record<string, string>,
		mechanism: boolean,
	): Promise<Fixture> {
		const root = join(scratch, id);
		const home = join(root, "home");
		const work = join(root, "work");
		const outside = join(root, "outside");
		for (const directory of [home, work, outside]) mkdirSync(directory, { recursive: true });
		spawnSync("git", ["init", "-q", work]);
		spawnSync("git", [
			"-C",
			work,
			"-c",
			"user.email=o1@example.invalid",
			"-c",
			"user.name=o1",
			"commit",
			"-q",
			"--allow-empty",
			"-m",
			"fixture",
		]);
		const requestEnv = Object.fromEntries(
			Object.entries(boardEnv).map(([key, value]) => [key, value.replace("<home>", home)]),
		);
		const fixtureFiles = files.map((file) => join(root, file));
		if (kind !== "unguarded-dot") for (const file of fixtureFiles) writeJson(file, { permission: SHAPES[shape] });
		process.env.HOME = home;
		const runs: Fixture["runs"] = [];
		let launchEnv: Record<string, string | undefined> = {};
		for (const toolCase of CASES) {
			const prompt =
				toolCase === "external" ? `CASE:external PATH:${join(outside, "written.txt")}` : `CASE:${toolCase}`;
			const launch = await prepareAgentLaunch({
				taskId: `task-${id}`,
				agentId: "opencode",
				binary: "opencode",
				args: [],
				cwd: work,
				prompt,
				workspaceId: "o1-a1",
				env: requestEnv,
			});
			launchEnv = launch.env;
			runs.push({
				case: toolCase,
				argv: [launch.binary ?? "opencode", ...launch.args],
				markers: [join(work, "bash-marker.txt"), join(outside, "written.txt")],
			});
		}
		// The unguarded rows write HOME's .opencode file after the adapter's check, to record what it would do.
		if (kind === "unguarded-dot") for (const file of fixtureFiles) writeJson(file, { permission: SHAPES[shape] });
		const configHome = launchEnv.XDG_CONFIG_HOME as string;
		writeJson(join(configHome, "opencode", "opencode.json"), {
			provider: {
				stub: {
					npm: "@ai-sdk/openai-compatible",
					name: "Stub",
					options: { baseURL: `http://127.0.0.1:${STUB_PORT}/v1`, apiKey: "stub-not-a-credential" },
					models: { "stub-model": { name: "Stub model", tool_call: true } },
				},
			},
			model: "stub/stub-model",
		});
		const hookLog = join(root, "hooks.log");
		writeFileSync(hookLog, "");
		let board: Record<string, string | undefined> = launchEnv;
		if (!mechanism) {
			// The control: only the generated file, as the adapter set OPENCODE_CONFIG before O1's mechanism.
			board = {
				OPENCODE_CONFIG: launchEnv.OPENCODE_CONFIG,
				KANBAN_HOOK_TASK_ID: launchEnv.KANBAN_HOOK_TASK_ID,
				KANBAN_HOOK_WORKSPACE_ID: launchEnv.KANBAN_HOOK_WORKSPACE_ID,
				XDG_CONFIG_HOME: configHome,
			};
		}
		const env = Object.fromEntries(
			Object.entries({
				PATH: imagePathValue,
				HOME: home,
				TMPDIR: "/tmp",
				...requestEnv,
				...board,
				COLORTERM: "truecolor",
				TERM: "xterm-256color",
				TERM_PROGRAM: "kanban",
				OPENCODE_DISABLE_MODELS_FETCH: "1",
				npm_config_registry: `http://127.0.0.1:${STUB_PORT}/npm/`,
				OPENCODE_DB: `/tmp/o1-a1-${id}.db`,
				O1_HOOK_LOG: hookLog,
			}).filter((entry): entry is [string, string] => entry[1] !== undefined),
		);
		return { id, kind, placement, shape, root, cwd: work, outside, hookLog, fixtureFiles, env, runs };
	}

	beforeAll(async () => {
		scratch = realpathSync(mkdtempSync(join(tmpdir(), "o1-a1-")));
		imagePathValue = imagePath();
		delete process.env.KANBAN_STORAGE_ROOT;
		// The board plugin's hook command becomes `/usr/local/bin/node <hook-recorder.mjs> hooks ingest ...`.
		process.argv = ["node", join(FIXTURES, "hook-recorder.mjs")];
		process.execArgv = [];
		Object.defineProperty(process, "execPath", { configurable: true, value: "/usr/local/bin/node" });
		try {
			for (const [shape] of Object.entries(SHAPES)) {
				for (const [placement, spec] of Object.entries(PLACEMENTS)) {
					fixtures.push(
						await prepareFixture(
							`${placement}--${shape}`,
							"guarded",
							placement,
							shape,
							spec.files,
							spec.env ?? {},
							true,
						),
					);
				}
				for (const [placement, files] of Object.entries(DOT_PLACEMENTS)) {
					const probeRoot = join(scratch, `refusal--${placement}--${shape}`);
					mkdirSync(join(probeRoot, "work"), { recursive: true });
					for (const file of files) writeJson(join(probeRoot, file), { permission: SHAPES[shape] });
					process.env.HOME = join(probeRoot, "home");
					let message = "";
					try {
						await prepareAgentLaunch({
							taskId: "refusal",
							agentId: "opencode",
							binary: "opencode",
							args: [],
							cwd: join(probeRoot, "work"),
							prompt: "",
							workspaceId: "o1-a1",
						});
					} catch (error) {
						message = (error as Error).message;
					}
					refusals.push({ placement, shape, refused: message.includes(".opencode"), message });
				}
				fixtures.push(
					await prepareFixture(
						`unguarded-dot--${shape}`,
						"unguarded-dot",
						"dot-opencode.json (unguarded)",
						shape,
						DOT_PLACEMENTS["dot-opencode.json"] ?? [],
						{},
						true,
					),
				);
			}
			fixtures.push(
				await prepareFixture(
					"control--no-mechanism--star-allow",
					"control",
					"project (no mechanism)",
					"star-allow",
					["work/opencode.json"],
					{},
					false,
				),
			);
		} finally {
			process.env.HOME = saved.home;
			process.argv = saved.argv;
			process.execArgv = saved.execArgv;
			Object.defineProperty(process, "execPath", { configurable: true, value: saved.execPath });
		}
		const plan = {
			stubPort: STUB_PORT,
			stubLog: join(scratch, "stub.log"),
			fixtures: fixtures.map(({ id, cwd, env, hookLog, runs }) => ({ id, cwd, env, hookLog, runs })),
		};
		writeFileSync(join(scratch, "plan.json"), JSON.stringify(plan));
		// The image runs as its own user; the fixtures are test-owned.
		spawnSync("chmod", ["-R", "a+rwX", scratch]);
		const run = spawnSync(
			"docker",
			[
				"run",
				"--rm",
				"--name",
				`o1-a1-${process.pid}`,
				"--network",
				"none",
				"--entrypoint",
				"/usr/local/bin/node",
				"-v",
				`${scratch}:${scratch}`,
				"-v",
				`${FIXTURES}:${FIXTURES}:ro`,
				IMAGE,
				join(FIXTURES, "run-in-image.mjs"),
				join(scratch, "plan.json"),
				join(scratch, "results.json"),
			],
			{ encoding: "utf8", timeout: 55 * 60_000, maxBuffer: 64 * 1024 * 1024 },
		);
		if (run.status !== 0) throw new Error(`image run exited ${run.status}: ${(run.stderr ?? "").slice(-2000)}`);
		results = JSON.parse(readFileSync(join(scratch, "results.json"), "utf8")) as FixtureResult[];
		if (process.env.O1_A1_REPORT) {
			writeFileSync(process.env.O1_A1_REPORT, JSON.stringify(report(), null, 1));
		}
	}, 60 * 60_000);

	afterAll(() => {
		process.env.HOME = saved.home;
		if (saved.root === undefined) delete process.env.KANBAN_STORAGE_ROOT;
		else process.env.KANBAN_STORAGE_ROOT = saved.root;
		if (scratch && !process.env.O1_A1_KEEP) rmSync(scratch, { recursive: true, force: true });
	});

	function resultOf(fixture: Fixture): FixtureResult {
		const result = results.find((candidate) => candidate.id === fixture.id);
		if (!result) throw new Error(`no result for ${fixture.id}`);
		return result;
	}

	function rulesOf(result: FixtureResult): Rule[] {
		return (JSON.parse(result.agent.stdout) as { permission: Rule[] }).permission;
	}

	function summarize(fixture: Fixture) {
		const result = resultOf(fixture);
		const rules = rulesOf(result);
		const loaded = result.config.stderr
			.split("\n")
			.filter((line) => line.includes("message=loading path="))
			.map(loadedPath);
		const behaviour = Object.fromEntries(
			result.runs.map((run) => {
				const asked = run.hooks.some(
					(hook) => hook.event === "to_review" && hook.metadata?.notification_type === "permission.asked",
				);
				const ran =
					(run.case === "bash" && run.markers[join(fixture.cwd, "bash-marker.txt")] === true) ||
					(run.case === "webfetch" && run.stub.some((line) => line.kind === "page")) ||
					(run.case === "external" && run.markers[join(fixture.outside, "written.txt")] === true);
				const resultSent = run.stub.some((line) => line.kind === "chat" && line.lastRole === "tool");
				return [run.case, ran ? "allow" : asked ? "ask" : resultSent ? "deny" : `unobserved(${run.reason})`];
			}),
		);
		return {
			id: fixture.id,
			kind: fixture.kind,
			placement: fixture.placement,
			shape: fixture.shape,
			resolvedPermission: (JSON.parse(result.config.stdout) as { permission?: unknown }).permission,
			fixtureFilesLoaded: fixture.fixtureFiles
				.filter((file) => loaded.includes(file))
				.map((file) => file.slice(fixture.root.length + 1)),
			agentRules: {
				bash: effective(rules, "bash", "touch bash-marker.txt"),
				webfetch: effective(rules, "webfetch", `http://127.0.0.1:${STUB_PORT}/page`),
				external_directory: effective(rules, "external_directory", `${fixture.outside}/*`),
				edit: effective(rules, "edit", "inside.txt"),
			},
			behaviour,
		};
	}

	function report() {
		return { image: IMAGE, refusals, fixtures: fixtures.map(summarize) };
	}

	it('the instruments see an allow: without the mechanism a project {"*":"allow"} runs the command', () => {
		const control = fixtures.find((fixture) => fixture.kind === "control") as Fixture;
		const summary = summarize(control);
		expect(summary.fixtureFilesLoaded).toEqual(["work/opencode.json"]);
		expect(summary.agentRules.bash).toBe("allow");
		expect(summary.behaviour.bash).toBe("allow");
	});

	it("refuses every launch while HOME's .opencode carries a loosening file, alone or with a project file", () => {
		expect(refusals.length).toBe(Object.keys(SHAPES).length * Object.keys(DOT_PLACEMENTS).length);
		for (const refusal of refusals)
			expect(refusal, `${refusal.placement} ${refusal.shape}`).toMatchObject({ refused: true });
	});

	for (const shape of Object.keys(SHAPES)) {
		for (const placement of Object.keys(PLACEMENTS)) {
			it(`${placement} ${shape}: bash and webfetch ask, external directories are denied, edits stay allowed`, () => {
				const fixture = fixtures.find((candidate) => candidate.id === `${placement}--${shape}`) as Fixture;
				const summary = summarize(fixture);
				expect(resultOf(fixture).config.status).toBe(0);
				expect(summary.fixtureFilesLoaded).toEqual([]);
				expect(summary.resolvedPermission).toEqual(POLICY);
				expect(summary.agentRules).toEqual({
					bash: "ask",
					webfetch: "ask",
					external_directory: "deny",
					edit: "allow",
				});
				// Amendment-1 (c): the ask reached the board plugin as permission.asked and moved the card to review.
				expect(summary.behaviour).toEqual({ bash: "ask", webfetch: "ask", external: "deny" });
			});
		}
	}
});
