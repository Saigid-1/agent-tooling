// Order O2 (docs/work/orders/O2-opencode-third-harness.md, F1 and F2): the in-container half of
// tests/image/test_o2_f1_strangers_run_image.py and test_o2_f2_per_launch_isolation_image.py. Not a vitest
// file. The image tests bundle it with apps/kanban's esbuild and run it with the node of O3's `opencode`
// image, beside the pinned binary. Seams: tests/o2_seams.py (handed in through the spec).
//
//   node runner.mjs run <spec.json>     the board's desk launches of OpenCode, each with one turn
//   node runner.mjs hooks ...           the board hook command the Kanban plugin runs: recorded, exit 0
//
// Every launch is the board's: TerminalSessionManager.startTaskSession with a `deskId`, the real adapter
// and KANBAN_LAUNCH_BINDING_COMMAND naming the image's kp-agent-launch (O2-S9), in the real PTY (node-pty
// from the image), the TUI with --prompt. The PTY spawn is recorded on its way through (binary, argv, cwd,
// environment) and the process runs unchanged. The model is a stub OpenAI-compatible provider on loopback
// in this process (O2-S10). The board's hook command is built from process.execPath and process.argv[1],
// so inside the container it re-enters this file, which records it.
//
// After each launch's turn the runner asks tests/image/o2_container.py (the image's Python) to wait for
// the binding, the episode and the search hit (`poll`). With `instrument: true` it runs `opencode debug
// config` with each launch's own environment, argv minus --prompt, cwd and PWD=cwd (O2-S13) and starts each
// local MCP server it lists (`serve-check`). Then it stops the sessions (the board terminal's exit), lets
// SessionEnd settle, collects `final`, and scans for install artifacts (O2-S12) and the provider-key
// sentinel (O2-S14). The network recorder's log is read by the test.

import { spawn } from "node:child_process";
import { appendFileSync, existsSync, mkdirSync, readdirSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { createServer, type IncomingMessage, type ServerResponse } from "node:http";
import { join } from "node:path";

import { PtySession } from "../../../src/terminal/pty-session";
import { TerminalSessionManager } from "../../../src/terminal/session-manager";

const OUT_DIR = "/o2/out";
const HOOK_LOG = join(OUT_DIR, "hooks.jsonl");
const STUB_LOG = join(OUT_DIR, "stub.jsonl");
const TOOLS = "/o2/tools/o2_container.py";

interface LaunchSpec {
	id: string;
	desk: number;
	prompt: string;
	answer: string;
}

interface Spec {
	world_out: string;
	result: string;
	launches: LaunchSpec[];
	instrument: boolean;
	key_sentinel: string;
	scan_roots: string[];
	settle_seconds: number;
	seams: {
		harness: string;
		stub_port: number;
		launch_binding_variable: string;
		image_launch_executable: string;
		board_launch: { agentId: "opencode"; binary: string; args: string[]; autonomousModeEnabled: boolean; workspaceId: string };
		install_artifacts: string[];
		debug_config_argv: string[];
		capture_deadline_seconds: number;
		key_file_in_image: string;
	};
	extra_logs: string[];
}

interface CapturedLaunch {
	binary: string;
	args: string[];
	cwd: string;
	env: Record<string, string>;
	pid: number | null;
}

// --------------------------------------------------------------------------- board hook recorder

if (process.argv[2] === "hooks") {
	try {
		appendFileSync(
			HOOK_LOG,
			`${JSON.stringify({ at: Date.now(), argv: process.argv.slice(2), taskId: process.env.KANBAN_HOOK_TASK_ID ?? null })}\n`,
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

/** Answers a streamed chat with the launch's answer text (found by its prompt), anything else with a title. */
function startStub(spec: Spec): Promise<void> {
	const server = createServer((req: IncomingMessage, res: ServerResponse) => {
		let body = "";
		req.on("data", (chunk) => {
			body += chunk;
		});
		req.on("end", () => {
			let parsed: { stream?: boolean; model?: string; messages?: Array<{ role?: string; content?: unknown }> } = {};
			try {
				parsed = JSON.parse(body);
			} catch {
				// Logged with no launch.
			}
			const asked = (parsed.messages ?? [])
				.filter((m) => m.role === "user")
				.map(messageText)
				.join("\n");
			const launch = spec.launches.find((item) => asked.includes(item.prompt)) ?? null;
			const session = req.headers["x-opencode-session-id"] ?? null;
			log(STUB_LOG, { kind: "chat", stream: !!parsed.stream, launch: launch?.id ?? null, session });
			const model = parsed.model ?? "stub-model";
			const chunk = (delta: Record<string, unknown>, finish: string | null) => ({
				id: "chatcmpl-o2",
				object: "chat.completion.chunk",
				created: 0,
				model,
				choices: [{ index: 0, delta, finish_reason: finish }],
				...(finish ? { usage: { prompt_tokens: 1, completion_tokens: 1, total_tokens: 2 } } : {}),
			});
			if (parsed.stream) {
				res.writeHead(200, { "content-type": "text/event-stream" });
				const send = (value: unknown) => res.write(`data: ${JSON.stringify(value)}\n\n`);
				send(chunk({ role: "assistant", content: launch ? launch.answer : "o2 title" }, null));
				send(chunk({}, "stop"));
				res.end("data: [DONE]\n\n");
				if (launch) log(STUB_LOG, { kind: "answered", launch: launch.id, session });
				return;
			}
			res.writeHead(200, { "content-type": "application/json" });
			res.end(
				JSON.stringify({
					id: "chatcmpl-o2",
					object: "chat.completion",
					created: 0,
					model,
					choices: [{ index: 0, message: { role: "assistant", content: "o2 title" }, finish_reason: "stop" }],
					usage: { prompt_tokens: 1, completion_tokens: 1, total_tokens: 2 },
				}),
			);
		});
	});
	return new Promise((resolve) => server.listen(spec.seams.stub_port, "127.0.0.1", () => resolve()));
}

// --------------------------------------------------------------------------- helpers

function runProcess(
	binary: string,
	args: string[],
	options: { cwd?: string; env?: Record<string, string>; timeoutMs: number },
): Promise<{ code: number | null; stdout: string; stderr: string; timedOut: boolean }> {
	return new Promise((resolve) => {
		const child = spawn(binary, args, {
			cwd: options.cwd,
			env: options.env ?? (process.env as Record<string, string>),
			stdio: ["ignore", "pipe", "pipe"],
		});
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

async function tool(args: string[], timeoutMs: number): Promise<unknown> {
	const result = await runProcess("python3", [TOOLS, ...args], { timeoutMs });
	try {
		return JSON.parse(result.stdout.trim().split("\n").pop() ?? "");
	} catch {
		return { error: `o2_container ${args[0]} exit ${result.code}`, stderr: result.stderr.slice(-2000) };
	}
}

function definedEnv(env: Record<string, string | undefined> | undefined): Record<string, string> {
	return Object.fromEntries(
		Object.entries(env ?? {}).filter((entry): entry is [string, string] => entry[1] !== undefined),
	);
}

function walk(root: string, visit: (path: string, isDirectory: boolean) => boolean): void {
	let entries: import("node:fs").Dirent[];
	try {
		entries = readdirSync(root, { withFileTypes: true });
	} catch {
		return;
	}
	for (const entry of entries) {
		const path = join(root, entry.name);
		const descend = visit(path, entry.isDirectory());
		if (entry.isDirectory() && descend) walk(path, visit);
	}
}

/** Paths named like an installer's output (O2-S12) under the scan roots; node_modules is not entered. */
function installArtifacts(spec: Spec): string[] {
	const names = new Set(spec.seams.install_artifacts);
	const found: string[] = [];
	for (const root of spec.scan_roots) {
		walk(root, (path, isDirectory) => {
			const name = path.slice(path.lastIndexOf("/") + 1);
			if (names.has(name)) found.push(path);
			return isDirectory && name !== "node_modules" && name !== ".git";
		});
	}
	return found.sort();
}

/** Files under the scan roots and the extra paths whose bytes contain the sentinel. */
function sentinelFiles(spec: Spec, extra: string[]): string[] {
	const needle = Buffer.from(spec.key_sentinel);
	const found: string[] = [];
	const check = (path: string) => {
		try {
			const info = statSync(path);
			if (info.isFile() && info.size <= 16 * 1024 * 1024 && readFileSync(path).includes(needle)) found.push(path);
		} catch {
			// Unreadable or gone.
		}
	};
	for (const root of spec.scan_roots) {
		walk(root, (path, isDirectory) => {
			if (!isDirectory) check(path);
			return isDirectory && !path.endsWith("/node_modules");
		});
	}
	for (const path of extra) check(path);
	return found.sort();
}

function procEnviron(pid: number | null): Record<string, string> | null {
	if (!pid) return null;
	try {
		return Object.fromEntries(
			readFileSync(`/proc/${pid}/environ`, "utf8")
				.split("\0")
				.filter(Boolean)
				.map((entry) => {
					const index = entry.indexOf("=");
					return [entry.slice(0, index), entry.slice(index + 1)];
				}),
		);
	} catch {
		return null;
	}
}

/** The launch argv in the instrument's form: `debug config` (O2-S13), minus `--prompt <text>`. */
function instrumentArgs(spec: Spec, launchArgs: string[]): string[] {
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
	return [...spec.seams.debug_config_argv, ...rest];
}

function parseConfig(text: string): Record<string, unknown> | null {
	const start = text.indexOf("{");
	if (start < 0) return null;
	try {
		return JSON.parse(text.slice(start)) as Record<string, unknown>;
	} catch {
		return null;
	}
}

async function waitFor(predicate: () => boolean, timeoutMs: number): Promise<boolean> {
	const started = Date.now();
	while (Date.now() - started < timeoutMs) {
		if (predicate()) return true;
		await new Promise((resolve) => setTimeout(resolve, 250));
	}
	return predicate();
}

// --------------------------------------------------------------------------- the run

async function run(spec: Spec): Promise<void> {
	mkdirSync("/state/kanban", { recursive: true });
	await startStub(spec);
	const world = JSON.parse(readFileSync(spec.world_out, "utf8")) as { operator: string; desks: string[] };
	const home = "/state/o2/home";
	mkdirSync(home, { recursive: true });
	process.env.HOME = home;
	process.env[spec.seams.launch_binding_variable] = JSON.stringify([
		spec.seams.image_launch_executable,
		"--config",
		world.operator,
	]);
	const artifactsBefore = installArtifacts(spec);
	const captured = new Map<string, CapturedLaunch>();
	const original = PtySession.spawn;
	let current: string | null = null;
	PtySession.spawn = ((request: Parameters<typeof PtySession.spawn>[0]) => {
		const session = original.call(PtySession, request);
		if (current) {
			captured.set(current, {
				binary: request.binary,
				args: typeof request.args === "string" ? [request.args] : [...(request.args ?? [])],
				cwd: request.cwd,
				env: definedEnv(request.env),
				pid: session.pid ?? null,
			});
		}
		return session;
	}) as typeof PtySession.spawn;

	const manager = new TerminalSessionManager();
	const result: Record<string, unknown> = { launches: {} };
	const launches = result.launches as Record<string, Record<string, unknown>>;
	const started = Date.now();
	for (const launch of spec.launches) {
		const repo = `/state/o2/work/task-${launch.id}`;
		mkdirSync(repo, { recursive: true });
		const init = await runProcess("git", ["init", "-q", repo], { timeoutMs: 30_000 });
		const entry: Record<string, unknown> = { desk: world.desks[launch.desk], repo, gitInit: init.code };
		launches[launch.id] = entry;
		current = launch.id;
		try {
			await manager.startTaskSession({
				taskId: `o2-${launch.id}`,
				cwd: repo,
				prompt: launch.prompt,
				deskId: world.desks[launch.desk],
				cols: 120,
				rows: 40,
				...spec.seams.board_launch,
			});
			entry.launched = true;
		} catch (error) {
			entry.launched = false;
			entry.refusal = error instanceof Error ? error.message : String(error);
		}
		current = null;
		const launchCapture = captured.get(launch.id);
		if (launchCapture) {
			entry.argv = launchCapture.args;
			entry.cwd = launchCapture.cwd;
			entry.env = launchCapture.env;
			entry.pid = launchCapture.pid;
		}
	}

	// Each launch's turn: the stub served its answer; then the binding, the episode and the search hit.
	for (const launch of spec.launches) {
		const entry = launches[launch.id] as Record<string, unknown>;
		if (!entry.launched) continue;
		entry.answered = await waitFor(
			() => readJsonLines(STUB_LOG).some((item) => item.kind === "answered" && item.launch === launch.id),
			spec.seams.capture_deadline_seconds * 1000,
		);
		entry.answeredAtMs = Date.now() - started;
		const pid = entry.pid as number | null;
		entry.procEnv = procEnviron(pid);
	}
	for (const launch of spec.launches) {
		const entry = launches[launch.id] as Record<string, unknown>;
		if (!entry.launched) continue;
		entry.poll = await tool(["poll", "/o2/out/spec.json", launch.id], (spec.seams.capture_deadline_seconds + 120) * 1000);
	}

	// F2's instrument: what each live launch's OpenCode would load, and who each local MCP server serves.
	if (spec.instrument) {
		for (const launch of spec.launches) {
			const entry = launches[launch.id] as Record<string, unknown>;
			const capture = captured.get(launch.id);
			if (!capture) continue;
			const env = { ...capture.env, PWD: capture.cwd };
			const debug = await runProcess(capture.binary, instrumentArgs(spec, capture.args), {
				cwd: capture.cwd,
				env,
				timeoutMs: 120_000,
			});
			const config = parseConfig(debug.stdout);
			entry.debugConfig = { code: debug.code, timedOut: debug.timedOut, stderr: debug.stderr.slice(-1500) };
			const mcp = (config?.mcp ?? {}) as Record<string, Record<string, unknown>>;
			entry.mcp = mcp;
			const checks: unknown[] = [];
			for (const [name, server] of Object.entries(mcp)) {
				if (server?.type !== "local" || server?.enabled === false) {
					checks.push({ name, skipped: `type ${String(server?.type)} enabled ${String(server?.enabled)}` });
					continue;
				}
				const requestPath = join(OUT_DIR, `serve-check-${launch.id}-${name}.json`);
				writeFileSync(requestPath, JSON.stringify({ name, server, env, cwd: capture.cwd }));
				checks.push(await tool(["serve-check", requestPath], 150_000));
			}
			entry.serverChecks = checks;
		}
	}

	// The board terminal's exit (SessionEnd), then what was captured and queued, then the scans.
	for (const launch of spec.launches) {
		manager.stopTaskSession(`o2-${launch.id}`);
	}
	await new Promise((resolve) => setTimeout(resolve, spec.settle_seconds * 1000));
	for (const launch of spec.launches) {
		const entry = launches[launch.id] as Record<string, unknown>;
		entry.final = await tool(["final", "/o2/out/spec.json", launch.id], 300_000);
		entry.summary = manager.getSummary(`o2-${launch.id}`);
	}
	PtySession.spawn = original;
	const artifactsAfter = installArtifacts(spec);
	result.installArtifacts = artifactsAfter.filter((path) => !artifactsBefore.includes(path));
	result.sentinelFiles = sentinelFiles(spec, [STUB_LOG, HOOK_LOG, ...spec.extra_logs]);
	result.hooks = readJsonLines(HOOK_LOG);
	result.stub = readJsonLines(STUB_LOG);
	result.keyFileReadable = (() => {
		try {
			return readFileSync(spec.seams.key_file_in_image, "utf8").includes(spec.key_sentinel);
		} catch {
			return false;
		}
	})();
	writeFileSync(spec.result, JSON.stringify(result, null, 1));
}

async function main(): Promise<void> {
	const [mode, specPath] = process.argv.slice(2);
	if (mode !== "run" || !specPath) throw new Error("usage: runner.mjs run <spec.json>");
	mkdirSync(OUT_DIR, { recursive: true });
	await run(JSON.parse(readFileSync(specPath, "utf8")) as Spec);
}

main().then(
	() => process.exit(0),
	(error) => {
		process.stderr.write(`o2 runner failed: ${error instanceof Error ? error.stack : String(error)}\n`);
		process.exit(1);
	},
);
