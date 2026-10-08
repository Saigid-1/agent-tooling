// Order O1 (docs/work/orders/O1-opencode-board-launch.md, A1 and amendment-1): the in-container half of
// tests/image/test_o1_opencode_launch_image.py. Not a vitest file. The image test bundles it with
// apps/kanban's esbuild and runs it with the node of O3's `opencode` image, beside the pinned binary.
//
//   node runner.mjs matrix <spec.json>   effective-policy scenarios through `opencode run` (no --auto)
//   node runner.mjs tui <spec.json>      the board's own PTY launch of the TUI; does an ask reach the board?
//   node runner.mjs hooks ...            the board hook command the Kanban plugin runs: recorded, exit 0
//
// The launch is the board's: TerminalSessionManager.startTaskSession prepares it through the real
// adapter (prepareAgentLaunch) and composes the process environment. In `matrix` the PTY spawn is
// captured (binary, argv, cwd, env) and the instrument re-runs that launch as `opencode run --format
// json`: the argv minus its `--prompt <text>` pair, plus a probe message. In `tui` the real PTY
// (node-pty) runs the launch unchanged. The board's hook command is built from process.execPath and
// process.argv[1], so inside the container it re-enters this file, which records it.
//
// The model is a stub OpenAI-compatible provider on loopback inside the container; the test supplies
// its provider config through OpenCode's managed config directory (/etc/opencode), which carries no
// `permission` key. Each probe message names one tool; the stub answers it with one call of that tool,
// and answers everything else with text.

import { spawn, spawnSync } from "node:child_process";
import { appendFileSync, existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { createServer, type IncomingMessage, type ServerResponse } from "node:http";
import { dirname, join } from "node:path";

import { PtySession } from "../../../src/terminal/pty-session";
import { TerminalSessionManager } from "../../../src/terminal/session-manager";

const OUT_DIR = "/o1/out";
const HOOK_LOG = join(OUT_DIR, "hooks.jsonl");
const STUB_LOG = join(OUT_DIR, "stub.jsonl");
const STUB_PORT = 18431;
const PROBE = /o1-probe tool=(bash|webfetch|write) id=([\w.-]+)(?: target=([\w./-]+))?/;

interface Fixture {
	/** "project": relative to the task worktree; "home": relative to HOME. */
	base: "project" | "home";
	path: string;
	text: string;
}

interface Scenario {
	id: string;
	/** "board": the board's launch; "raw": plain `opencode` with the container env (instrument controls). */
	launch: "board" | "raw";
	fixtures: Fixture[];
}

interface Spec {
	out: string;
	scenarios: Scenario[];
	probes?: Array<"bash" | "webfetch" | "write">;
	timeoutMs?: number;
}

interface CapturedLaunch {
	binary: string;
	args: string[];
	cwd: string;
	env: Record<string, string>;
}

// --------------------------------------------------------------------------- board hook recorder

if (process.argv[2] === "hooks") {
	try {
		appendFileSync(
			HOOK_LOG,
			`${JSON.stringify({
				at: Date.now(),
				argv: process.argv.slice(2),
				taskId: process.env.KANBAN_HOOK_TASK_ID ?? null,
				workspaceId: process.env.KANBAN_HOOK_WORKSPACE_ID ?? null,
			})}\n`,
		);
	} catch {
		// A recorder failure must not change OpenCode's behaviour.
	}
	process.exit(0);
}

// --------------------------------------------------------------------------- stub provider

function log(path: string, entry: Record<string, unknown>): void {
	appendFileSync(path, `${JSON.stringify({ at: Date.now(), ...entry })}\n`);
}

function readJsonLines(path: string): Array<Record<string, unknown>> {
	if (!existsSync(path)) return [];
	return readFileSync(path, "utf8")
		.split("\n")
		.filter((line) => line.trim())
		.map((line) => {
			try {
				return JSON.parse(line) as Record<string, unknown>;
			} catch {
				return { unparsed: line };
			}
		});
}

function messageText(message: { content?: unknown }): string {
	if (typeof message.content === "string") return message.content;
	if (Array.isArray(message.content)) {
		return message.content.map((part) => (typeof part?.text === "string" ? part.text : "")).join("");
	}
	return "";
}

function toolArguments(tool: string, id: string, target: string | undefined): Record<string, unknown> {
	if (tool === "bash") return { command: "touch o1-bash-marker", description: "o1 probe" };
	if (tool === "webfetch") return { url: `http://127.0.0.1:${STUB_PORT}/o1-fetch/${id}`, format: "text" };
	return { filePath: target ?? "/nonexistent/o1-external.txt", content: "o1" };
}

function startStub(): Promise<void> {
	const server = createServer((req: IncomingMessage, res: ServerResponse) => {
		let body = "";
		req.on("data", (chunk) => {
			body += chunk;
		});
		req.on("end", () => {
			const url = req.url ?? "";
			if (req.method === "GET" && url.startsWith("/o1-fetch/")) {
				log(STUB_LOG, { kind: "fetch", id: url.slice("/o1-fetch/".length) });
				res.end("o1-fetched");
				return;
			}
			let parsed: {
				stream?: boolean;
				model?: string;
				tools?: Array<{ function?: { name?: string } }>;
				messages?: Array<{ role?: string; content?: unknown; tool_calls?: unknown }>;
			} = {};
			try {
				parsed = JSON.parse(body);
			} catch {
				// Logged below with no probe.
			}
			const tools = (parsed.tools ?? []).map((tool) => tool.function?.name).filter((name): name is string => !!name);
			const messages = parsed.messages ?? [];
			const probe = PROBE.exec(
				messages
					.filter((m) => m.role === "user")
					.map(messageText)
					.join("\n"),
			);
			const toolResults = messages.filter((m) => m.role === "tool").map(messageText);
			const answered = messages.some((m) => m.role === "assistant" && m.tool_calls);
			const call = probe && tools.includes(probe[1]) && toolResults.length === 0 && !answered ? probe[1] : null;
			log(STUB_LOG, {
				kind: "chat",
				id: probe?.[2] ?? null,
				tool: probe?.[1] ?? null,
				tools,
				call,
				toolResults,
			});
			const model = parsed.model ?? "stub-model";
			const chunk = (delta: Record<string, unknown>, finish: string | null) => ({
				id: "chatcmpl-o1",
				object: "chat.completion.chunk",
				created: 0,
				model,
				choices: [{ index: 0, delta, finish_reason: finish }],
				...(finish ? { usage: { prompt_tokens: 1, completion_tokens: 1, total_tokens: 2 } } : {}),
			});
			if (parsed.stream) {
				res.writeHead(200, { "content-type": "text/event-stream" });
				const send = (value: unknown) => res.write(`data: ${JSON.stringify(value)}\n\n`);
				if (call && probe) {
					const args = JSON.stringify(toolArguments(call, probe[2], probe[3]));
					send(
						chunk(
							{
								role: "assistant",
								tool_calls: [
									{ index: 0, id: "call_o1", type: "function", function: { name: call, arguments: args } },
								],
							},
							null,
						),
					);
					send(chunk({}, "tool_calls"));
				} else {
					send(chunk({ role: "assistant", content: "o1 done" }, null));
					send(chunk({}, "stop"));
				}
				res.end("data: [DONE]\n\n");
				return;
			}
			res.writeHead(200, { "content-type": "application/json" });
			res.end(
				JSON.stringify({
					id: "chatcmpl-o1",
					object: "chat.completion",
					created: 0,
					model,
					choices: [{ index: 0, message: { role: "assistant", content: "o1 title" }, finish_reason: "stop" }],
					usage: { prompt_tokens: 1, completion_tokens: 1, total_tokens: 2 },
				}),
			);
		});
	});
	return new Promise((resolve) => server.listen(STUB_PORT, "127.0.0.1", () => resolve()));
}

// --------------------------------------------------------------------------- scenario setup

interface ScenarioDirs {
	root: string;
	home: string;
	repo: string;
	outside: string;
}

function prepareDirs(scenario: Scenario): ScenarioDirs {
	const root = join("/state/o1-scenarios", scenario.id);
	const dirs = { root, home: join(root, "home"), repo: join(root, "repo"), outside: join(root, "outside") };
	for (const dir of [dirs.home, dirs.repo, dirs.outside]) mkdirSync(dir, { recursive: true });
	// The task worktree is a git worktree, as the board's are; OpenCode takes its project root from it.
	const init = spawnSync("git", ["init", "-q", dirs.repo], { encoding: "utf8" });
	if (init.status !== 0) throw new Error(`git init failed: ${init.stderr}`);
	for (const fixture of scenario.fixtures) {
		const path = join(fixture.base === "project" ? dirs.repo : dirs.home, fixture.path);
		mkdirSync(dirname(path), { recursive: true });
		writeFileSync(path, fixture.text);
	}
	return dirs;
}

function runProcess(
	binary: string,
	args: string[],
	options: { cwd: string; env: Record<string, string>; timeoutMs: number },
): Promise<{ code: number | null; stdout: string; stderr: string; timedOut: boolean }> {
	return new Promise((resolve) => {
		const child = spawn(binary, args, { cwd: options.cwd, env: options.env, stdio: ["ignore", "pipe", "pipe"] });
		let stdout = "";
		let stderr = "";
		let timedOut = false;
		const timer = setTimeout(() => {
			timedOut = true;
			child.kill("SIGKILL");
		}, options.timeoutMs);
		child.stdout.on("data", (data) => {
			stdout += data;
		});
		child.stderr.on("data", (data) => {
			stderr += data;
		});
		child.on("error", (error) => {
			stderr += String(error);
		});
		child.on("close", (code) => {
			clearTimeout(timer);
			resolve({ code, stdout, stderr, timedOut });
		});
	});
}

function definedEnv(env: Record<string, string | undefined> | undefined): Record<string, string> {
	return Object.fromEntries(
		Object.entries(env ?? {}).filter((entry): entry is [string, string] => entry[1] !== undefined),
	);
}

function fakePtySession() {
	return {
		pid: 0,
		write() {},
		resize() {},
		pause() {},
		resume() {},
		stop() {},
		wasInterrupted: () => false,
	};
}

/** The board's launch for this scenario: startTaskSession through the real adapter, PTY spawn captured. */
async function captureBoardLaunch(scenario: Scenario, dirs: ScenarioDirs): Promise<CapturedLaunch> {
	const original = PtySession.spawn;
	let captured: CapturedLaunch | null = null;
	PtySession.spawn = ((request: {
		binary: string;
		args?: string[] | string;
		cwd: string;
		env?: Record<string, string | undefined>;
	}) => {
		captured = {
			binary: request.binary,
			args: typeof request.args === "string" ? [request.args] : [...(request.args ?? [])],
			cwd: request.cwd,
			env: definedEnv(request.env),
		};
		return fakePtySession();
	}) as unknown as typeof PtySession.spawn;
	try {
		const manager = new TerminalSessionManager();
		await manager.startTaskSession({
			taskId: `o1-${scenario.id}`,
			agentId: "opencode",
			binary: "opencode",
			args: [],
			autonomousModeEnabled: true,
			cwd: dirs.repo,
			prompt: `o1-probe tool=bash id=${scenario.id}`,
			workspaceId: "o1-workspace",
		});
	} finally {
		PtySession.spawn = original;
	}
	if (!captured) throw new Error("the board started no process");
	return captured;
}

/**
 * The launch environment in the instrument's form. Measured at the pin: the TUI (the board's launch form)
 * takes its project directory from the process cwd, but `opencode run` takes it from $PWD when PWD is set,
 * and the board's environment carries the board's own PWD. So the instrument sets PWD to the launch's
 * cwd, the directory the TUI works in; nothing else changes.
 */
function instrumentEnv(launch: CapturedLaunch): Record<string, string> {
	return { ...launch.env, PWD: launch.cwd };
}

/** The launch argv in the instrument's form: `run --format json`, minus `--prompt <text>`, plus the probe. */
function instrumentArgs(launchArgs: string[], message: string): string[] {
	const rest: string[] = [];
	for (let index = 0; index < launchArgs.length; index += 1) {
		const arg = launchArgs[index] ?? "";
		if (arg === "--prompt") {
			index += 1;
			continue;
		}
		if (arg.startsWith("--prompt=")) continue;
		rest.push(arg);
	}
	return ["run", "--format", "json", ...rest, message];
}

function agentName(launchArgs: string[]): string {
	const index = launchArgs.indexOf("--agent");
	if (index >= 0 && launchArgs[index + 1]) return launchArgs[index + 1] as string;
	const inline = launchArgs.find((arg) => arg.startsWith("--agent="));
	return inline ? inline.slice("--agent=".length) : "build";
}

interface ProbeResult {
	tool: string;
	/** "withheld": the tool was never offered to the model (OpenCode withholds a tool it denies outright). */
	outcome: "allow" | "ask" | "deny" | "withheld" | "unknown";
	status: string | null;
	error: string | null;
	sideEffect: boolean;
	providerReached: boolean;
	offered: boolean;
	code: number | null;
	timedOut: boolean;
	stderrTail: string;
}

async function probe(
	tool: "bash" | "webfetch" | "write",
	scenario: Scenario,
	dirs: ScenarioDirs,
	launch: CapturedLaunch,
	timeoutMs: number,
): Promise<ProbeResult> {
	const id = `${scenario.id}.${tool}`;
	const target = join(dirs.outside, `o1-external-${tool}.txt`);
	const message = `o1-probe tool=${tool} id=${id}${tool === "write" ? ` target=${target}` : ""}`;
	const marker = join(launch.cwd, "o1-bash-marker");
	const result = await runProcess(launch.binary, instrumentArgs(launch.args, message), {
		cwd: launch.cwd,
		env: instrumentEnv(launch),
		timeoutMs,
	});
	let status: string | null = null;
	let error: string | null = null;
	for (const line of result.stdout.split("\n")) {
		try {
			const event = JSON.parse(line) as {
				type?: string;
				part?: { tool?: string; state?: { status?: string; error?: string } };
			};
			if (event.type === "tool_use" && event.part?.tool === tool) {
				status = event.part.state?.status ?? null;
				error = event.part.state?.error ?? null;
			}
		} catch {
			// Not a JSON event line.
		}
	}
	const stubEntries = readJsonLines(STUB_LOG);
	const providerReached = stubEntries.some((entry) => entry.kind === "chat" && entry.id === id && entry.call === tool);
	const offered = stubEntries.some(
		(entry) => entry.kind === "chat" && entry.id === id && Array.isArray(entry.tools) && entry.tools.includes(tool),
	);
	const sideEffect =
		tool === "bash"
			? existsSync(marker)
			: tool === "webfetch"
				? stubEntries.some((entry) => entry.kind === "fetch" && entry.id === id)
				: existsSync(target);
	let outcome: ProbeResult["outcome"] = "unknown";
	if (sideEffect || status === "completed") outcome = "allow";
	else if (status === "error" && error?.startsWith("The user rejected permission")) outcome = "ask";
	else if (status === "error" && error?.startsWith("The user has specified a rule which prevents")) outcome = "deny";
	else if (!offered && stubEntries.some((entry) => entry.kind === "chat" && entry.id === id)) outcome = "withheld";
	return {
		tool,
		outcome,
		status,
		error: error ? error.slice(0, 600) : null,
		sideEffect,
		providerReached,
		offered,
		code: result.code,
		timedOut: result.timedOut,
		stderrTail: result.stderr.slice(-800),
	};
}

async function runMatrix(spec: Spec): Promise<void> {
	await startStub();
	const results: Record<string, unknown> = {};
	const timeoutMs = spec.timeoutMs ?? 60_000;
	for (const scenario of spec.scenarios) {
		const started = Date.now();
		const dirs = prepareDirs(scenario);
		process.env.HOME = dirs.home;
		let launch: CapturedLaunch;
		try {
			if (scenario.launch === "board") {
				launch = await captureBoardLaunch(scenario, dirs);
			} else {
				launch = { binary: "opencode", args: [], cwd: dirs.repo, env: definedEnv(process.env) };
			}
		} catch (error) {
			results[scenario.id] = { launched: false, refusal: error instanceof Error ? error.message : String(error) };
			writeFileSync(spec.out, JSON.stringify(results, null, 1));
			continue;
		}
		const probes: ProbeResult[] = [];
		for (const tool of spec.probes ?? ["bash", "webfetch", "write"]) {
			// After a probe that timed out, the rest of this scenario is not measured (bounded run time).
			if (probes.some((done) => done.timedOut)) break;
			probes.push(await probe(tool, scenario, dirs, launch, timeoutMs));
		}
		const ruleset = await runProcess(launch.binary, ["debug", "agent", agentName(launch.args)], {
			cwd: launch.cwd,
			env: instrumentEnv(launch),
			timeoutMs,
		});
		let permission: unknown = null;
		try {
			permission = (JSON.parse(ruleset.stdout) as { permission?: unknown }).permission ?? null;
		} catch {
			permission = { unparsed: ruleset.stdout.slice(0, 2000), stderr: ruleset.stderr.slice(-800) };
		}
		results[scenario.id] = {
			launched: true,
			argv: launch.args,
			opencodeEnv: Object.fromEntries(
				Object.entries(launch.env).filter(([key]) => key.startsWith("OPENCODE_") || key.startsWith("XDG_")),
			),
			probes,
			ruleset: permission,
			seconds: Math.round((Date.now() - started) / 1000),
		};
		writeFileSync(spec.out, JSON.stringify(results, null, 1));
	}
}

// --------------------------------------------------------------------------- the TUI launch

async function runTui(spec: Spec): Promise<void> {
	await startStub();
	const scenario = spec.scenarios[0];
	if (!scenario) throw new Error("tui needs one scenario");
	const dirs = prepareDirs(scenario);
	process.env.HOME = dirs.home;
	const taskId = `o1-${scenario.id}`;
	const id = `${scenario.id}.bash`;
	const timeoutMs = spec.timeoutMs ?? 120_000;
	const result: Record<string, unknown> = { launched: false };
	const manager = new TerminalSessionManager();
	try {
		await manager.startTaskSession({
			taskId,
			agentId: "opencode",
			binary: "opencode",
			args: [],
			autonomousModeEnabled: true,
			cwd: dirs.repo,
			prompt: `o1-probe tool=bash id=${id}`,
			workspaceId: "o1-workspace",
			cols: 120,
			rows: 40,
		});
		result.launched = true;
	} catch (error) {
		result.refusal = error instanceof Error ? error.message : String(error);
		writeFileSync(spec.out, JSON.stringify(result, null, 1));
		return;
	}
	const started = Date.now();
	const reviews = () =>
		readJsonLines(HOOK_LOG).filter(
			(entry) =>
				entry.taskId === taskId &&
				Array.isArray(entry.argv) &&
				(entry.argv as string[]).join(" ").includes("--event to_review"),
		);
	const chats = () => readJsonLines(STUB_LOG).filter((entry) => entry.kind === "chat" && entry.id === id);
	let reviewAt: number | null = null;
	let toolResultsAtReview: number | null = null;
	while (Date.now() - started < timeoutMs) {
		await new Promise((resolve) => setTimeout(resolve, 500));
		if (reviews().length > 0 && reviewAt === null) {
			reviewAt = Date.now() - started;
			toolResultsAtReview = chats().filter(
				(entry) => Array.isArray(entry.toolResults) && (entry.toolResults as unknown[]).length > 0,
			).length;
			// Hold the session a little longer: the ask must still be pending (no tool result reached the model).
			await new Promise((resolve) => setTimeout(resolve, 5_000));
			break;
		}
	}
	const snapshot = await manager.getRestoreSnapshot(taskId).catch(() => null);
	Object.assign(result, {
		reviewAtMs: reviewAt,
		toolResultsAtReview,
		toolResultsAtEnd: chats().filter(
			(entry) => Array.isArray(entry.toolResults) && (entry.toolResults as unknown[]).length > 0,
		).length,
		toolCallsIssued: chats().filter((entry) => entry.call === "bash").length,
		bashOffered: chats().some((entry) => Array.isArray(entry.tools) && entry.tools.includes("bash")),
		markerExists: existsSync(join(dirs.repo, "o1-bash-marker")),
		reviews: reviews(),
		hooks: readJsonLines(HOOK_LOG)
			.filter((entry) => entry.taskId === taskId)
			.map((entry) => entry.argv),
		summary: manager.getSummary(taskId),
		screenTail: JSON.stringify(snapshot ?? null).slice(-3000),
	});
	writeFileSync(spec.out, JSON.stringify(result, null, 1));
	manager.stopTaskSession(taskId);
}

// --------------------------------------------------------------------------- main

async function main(): Promise<void> {
	const [mode, specPath] = process.argv.slice(2);
	if (!specPath) throw new Error("usage: runner.mjs matrix|tui <spec.json>");
	mkdirSync(OUT_DIR, { recursive: true });
	mkdirSync("/state/kanban", { recursive: true });
	const spec = JSON.parse(readFileSync(specPath, "utf8")) as Spec;
	if (mode === "matrix") await runMatrix(spec);
	else if (mode === "tui") await runTui(spec);
	else throw new Error(`unknown mode ${mode}`);
}

main().then(
	() => process.exit(0),
	(error) => {
		process.stderr.write(`o1 runner failed: ${error instanceof Error ? error.stack : String(error)}\n`);
		process.exit(1);
	},
);
