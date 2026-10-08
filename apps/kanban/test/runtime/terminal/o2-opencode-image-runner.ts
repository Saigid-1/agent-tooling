// Order O2 (docs/work/orders/O2-opencode-third-harness.md): the in-container half of
// tests/image/test_o2_opencode_harness_image.py. Not a vitest file. The image test bundles it with apps/kanban's
// esbuild and runs it with the node of O3's `opencode` image, beside the pinned binary, in a container with no
// network.
//
//   node runner.mjs f1 <spec.json>   one desk task launched from the board, one turn, its capture and search
//   node runner.mjs f2 <spec.json>   two concurrent desk tasks on two desks; each asks its own memory server
//   node runner.mjs hooks ...        the board hook command the Kanban plugin runs: recorded, exit 0
//
// Every launch is the board's: TerminalSessionManager.startTaskSession with a desk, through the real adapter and
// `kp-agent-launch prepare`, in a real PTY (node-pty) running the TUI. The desk world (registry, desks) is built
// with the public CLIs; the runtime runs as Compose runs it (AGENT_MEMORY_VOLUME set, stores under /state/memory),
// so a seal is indexed only by the indexer role, here `kp-agent-desk indexer --watch` in the same container.
//
// Instruments, none of them reachable by the launch as a service:
// - egress: /etc/resolv.conf names a DNS recorder on 127.0.0.1:53 that answers every A query with 127.0.0.1, and
//   recorders on 127.0.0.1:80 and :443 log the HTTP Host line or the TLS SNI, then close. Any name the process
//   looks up (registry.npmjs.org, models.opencode.ai, ...) and any connection it then opens is recorded; nothing
//   answers it.
// - the model: a stub OpenAI-compatible provider on 127.0.0.1 (an IP literal, so it needs no lookup), given to
//   OpenCode through the managed config directory (/etc/opencode, mounted read-only; no permission key).
// - the store: tests/image/o2_store_probe.py, run with the image's python, reads the test's own store.

import { spawn, spawnSync } from "node:child_process";
import dgram from "node:dgram";
import { appendFileSync, existsSync, mkdirSync, readdirSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { createServer as createHttpServer, type IncomingMessage, type ServerResponse } from "node:http";
import { createServer as createTcpServer } from "node:net";
import { join } from "node:path";

import { PtySession } from "../../../src/terminal/pty-session";
import { TerminalSessionManager } from "../../../src/terminal/session-manager";

const OUT = "/o2/out";
const PROBE = "/o2/in/o2_store_probe.py";
const HOOK_LOG = join(OUT, "hooks.jsonl");
const STUB_LOG = join(OUT, "stub.jsonl");
const EGRESS_LOG = join(OUT, "egress.jsonl");
const STUB_PORT = 18450;
const WORLD = "/state/o2";
const STATE = "/state/memory/registry";

interface Spec {
	out: string;
	interval: number;
	timeoutMs: number;
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
			`${JSON.stringify({ at: Date.now(), argv: process.argv.slice(2), taskId: process.env.KANBAN_HOOK_TASK_ID ?? null })}\n`,
		);
	} catch {
		// A recorder failure must not change OpenCode's behaviour.
	}
	process.exit(0);
}

// --------------------------------------------------------------------------- instruments

function log(path: string, entry: Record<string, unknown>): void {
	appendFileSync(path, `${JSON.stringify({ at: Date.now(), ...entry })}\n`);
}

function readJsonLines(path: string): Array<Record<string, unknown>> {
	if (!existsSync(path)) return [];
	return readFileSync(path, "utf8")
		.split("\n")
		.filter((line) => line.trim())
		.map((line) => JSON.parse(line) as Record<string, unknown>);
}

function startDnsRecorder(): Promise<void> {
	const socket = dgram.createSocket("udp4");
	socket.on("message", (message, peer) => {
		const labels: string[] = [];
		let index = 12;
		while (index < message.length && message[index] !== 0) {
			const length = message[index] as number;
			labels.push(message.subarray(index + 1, index + 1 + length).toString("latin1"));
			index += length + 1;
		}
		const type = message.readUInt16BE(index + 1);
		log(EGRESS_LOG, { kind: "dns", name: labels.join("."), type });
		const header = Buffer.from(message.subarray(0, 12));
		header.writeUInt16BE(0x8180, 2);
		header.writeUInt16BE(type === 1 ? 1 : 0, 6);
		header.writeUInt32BE(0, 8);
		const answer =
			type === 1 ? Buffer.from([0xc0, 0x0c, 0, 1, 0, 1, 0, 0, 0, 30, 0, 4, 127, 0, 0, 1]) : Buffer.alloc(0);
		socket.send(Buffer.concat([header, message.subarray(12, index + 5), answer]), peer.port, peer.address);
	});
	return new Promise((resolve) => socket.bind(53, "127.0.0.1", () => resolve()));
}

function tlsServerName(data: Buffer): string | null {
	try {
		let index = 5 + 4 + 2 + 32;
		index += 1 + (data[index] as number);
		index += 2 + data.readUInt16BE(index);
		index += 1 + (data[index] as number);
		const end = index + 2 + data.readUInt16BE(index);
		index += 2;
		while (index < end) {
			const type = data.readUInt16BE(index);
			const length = data.readUInt16BE(index + 2);
			if (type === 0) return data.subarray(index + 9, index + 9 + data.readUInt16BE(index + 7)).toString("latin1");
			index += 4 + length;
		}
	} catch {
		// Not a ClientHello.
	}
	return null;
}

function startConnectionRecorder(port: number): Promise<void> {
	const server = createTcpServer((socket) => {
		socket.once("data", (data: Buffer) => {
			log(EGRESS_LOG, {
				kind: port === 443 ? "tls" : "http",
				port,
				detail: port === 443 ? tlsServerName(data) : data.toString("latin1").split("\r\n").slice(0, 2).join(" | "),
			});
			socket.destroy();
		});
		socket.on("error", () => {});
	});
	return new Promise((resolve) => server.listen(port, "127.0.0.1", () => resolve()));
}

function text(message: { content?: unknown }): string {
	if (typeof message.content === "string") return message.content;
	if (Array.isArray(message.content)) {
		return message.content.map((part) => (typeof part?.text === "string" ? part.text : "")).join("");
	}
	return "";
}

/**
 * The stub model. A user message `o2-turn token=<t>` is answered `o2 answer <t>answer`. A user message
 * `o2-probe mcp=<name> token=<t>` is answered, while no tool result has come back, with one call of the offered
 * tool whose name contains `kp_desk_memory` and ends with `<name>`, then with `o2 probe done <t>`.
 */
function startStubProvider(): Promise<void> {
	const server = createHttpServer((request: IncomingMessage, response: ServerResponse) => {
		let body = "";
		request.on("data", (chunk) => {
			body += chunk;
		});
		request.on("end", () => {
			let parsed: {
				stream?: boolean;
				model?: string;
				tools?: Array<{ function?: { name?: string } }>;
				messages?: Array<{ role?: string; content?: unknown; tool_calls?: unknown }>;
			} = {};
			try {
				parsed = JSON.parse(body);
			} catch {
				// Logged with no token.
			}
			const tools = (parsed.tools ?? []).map((tool) => tool.function?.name).filter((name): name is string => !!name);
			const messages = parsed.messages ?? [];
			const lastUser = [...messages].reverse().find((message) => message.role === "user");
			const userText = lastUser ? text(lastUser) : "";
			const token = /token=([A-Za-z0-9]+)/.exec(userText)?.[1] ?? null;
			const probe = /o2-probe mcp=([a-z_]+)/.exec(userText)?.[1] ?? null;
			const sinceUser = messages.slice(messages.lastIndexOf(lastUser as never) + 1);
			const toolResults = sinceUser.filter((message) => message.role === "tool").map(text);
			const target = probe
				? tools.find((name) => name.includes("kp_desk_memory") && name.endsWith(probe))
				: undefined;
			const call = target && toolResults.length === 0 ? target : null;
			log(STUB_LOG, {
				stream: !!parsed.stream,
				token,
				probe,
				tools,
				call,
				toolResults,
				session: request.headers["x-opencode-session-id"] ?? null,
			});
			const model = parsed.model ?? "stub-model";
			const chunk = (delta: Record<string, unknown>, finish: string | null) => ({
				id: "chatcmpl-o2",
				object: "chat.completion.chunk",
				created: 0,
				model,
				choices: [{ index: 0, delta, finish_reason: finish }],
				...(finish ? { usage: { prompt_tokens: 1, completion_tokens: 1, total_tokens: 2 } } : {}),
			});
			if (!parsed.stream) {
				response.writeHead(200, { "content-type": "application/json" });
				response.end(
					JSON.stringify({
						id: "chatcmpl-o2",
						object: "chat.completion",
						created: 0,
						model,
						choices: [{ index: 0, message: { role: "assistant", content: "o2 title" }, finish_reason: "stop" }],
						usage: { prompt_tokens: 1, completion_tokens: 1, total_tokens: 2 },
					}),
				);
				return;
			}
			response.writeHead(200, { "content-type": "text/event-stream" });
			const send = (value: unknown) => response.write(`data: ${JSON.stringify(value)}\n\n`);
			if (call) {
				send(
					chunk(
						{
							role: "assistant",
							tool_calls: [
								{ index: 0, id: "call_o2", type: "function", function: { name: call, arguments: "{}" } },
							],
						},
						null,
					),
				);
				send(chunk({}, "tool_calls"));
			} else {
				const answer = probe ? `o2 probe done ${token}` : `o2 answer ${token}answer`;
				send(chunk({ role: "assistant", content: answer }, null));
				send(chunk({}, "stop"));
			}
			response.end("data: [DONE]\n\n");
		});
	});
	return new Promise((resolve) => server.listen(STUB_PORT, "127.0.0.1", () => resolve()));
}

// --------------------------------------------------------------------------- the desk world (public CLIs)

function cli(
	executable: string,
	args: string[],
	input?: string,
): { status: number | null; stdout: string; stderr: string } {
	const result = spawnSync(executable, args, { input, encoding: "utf8", timeout: 120_000 });
	return { status: result.status, stdout: result.stdout ?? "", stderr: result.stderr ?? "" };
}

function ok(result: { status: number | null; stdout: string; stderr: string }, what: string): string {
	if (result.status !== 0)
		throw new Error(`${what} failed (${result.status}): ${result.stdout.slice(-800)} ${result.stderr.slice(-800)}`);
	return result.stdout;
}

interface World {
	operator: string;
	desks: Array<{ deskId: string; bindingKey: string }>;
}

function buildWorld(deskCount: number): World {
	mkdirSync(WORLD, { recursive: true });
	mkdirSync(STATE, { recursive: true, mode: 0o700 });
	const roster = join("/state/memory", "roster.json");
	writeFileSync(
		roster,
		JSON.stringify({
			schema_version: "agent-tooling.role-roster.v1",
			roles: [{ role_id: "Scribe", label: "Scribe", purpose: "Record decisions as they are made." }],
		}),
		{ mode: 0o600 },
	);
	const descriptor = join(WORLD, "registry.json");
	writeFileSync(
		descriptor,
		JSON.stringify({ schema_version: "agent-tooling.desk-registry.v1", tenant_id: "tenant-o2", roster_path: roster }),
		{ mode: 0o600 },
	);
	const operator = join(WORLD, "operator.json");
	writeFileSync(
		operator,
		JSON.stringify({
			schema_version: "ops.desk-memory.local.v1",
			state_root: STATE,
			catalog_path: descriptor,
			workspace_root: WORLD,
			provider_instance: "board-launcher",
			provider_session_id: "operator-console",
		}),
		{ mode: 0o600 },
	);
	ok(cli("kp-agent-desk", ["--config", operator, "initialize"]), "desk initialize");
	ok(cli("kp-agent-desk-registry", ["--config", operator, "initialize"]), "registry initialize");
	const desks: World["desks"] = [];
	for (let index = 0; index < deskCount; index += 1) {
		const deskId = `desk:${crypto.randomUUID()}`;
		ok(
			cli(
				"kp-agent-desk-registry",
				["--config", operator, "save"],
				JSON.stringify({
					desk_id: deskId,
					name: `O2 desk ${index + 1}`,
					description: "A desk for the O2 image run.",
					role: "Scribe",
					repos: ["workspace-repo"],
					capture: true,
					memory_write: true,
					expected_version: 0,
				}),
			),
			"desk save",
		);
		desks.push({ deskId, bindingKey: "" });
	}
	const listed = JSON.parse(ok(cli("kp-agent-desk-registry", ["--config", operator, "list"]), "registry list")) as {
		desks: Array<{ desk_id: string; binding_key?: string; binding?: { binding_key: string } }>;
	};
	for (const desk of desks) {
		const row = listed.desks.find((candidate) => candidate.desk_id === desk.deskId);
		desk.bindingKey = row?.binding_key ?? row?.binding?.binding_key ?? "";
	}
	return { operator, desks };
}

function repository(name: string): string {
	const path = join(WORLD, name);
	mkdirSync(path, { recursive: true });
	const init = spawnSync("git", ["init", "-q", path], { encoding: "utf8" });
	if (init.status !== 0) throw new Error(`git init failed: ${init.stderr}`);
	return path;
}

interface ChildResult {
	status: number | null;
	ms: number;
	stdout: string;
	stderr: string;
	timedOut: boolean;
}

/**
 * Every child that runs while OpenCode talks to the stub is asynchronous: the stub and the egress recorders
 * live in this process, so a synchronous child would stall them until it exits.
 */
function runChild(
	command: string,
	args: string[],
	options: { cwd?: string; env?: Record<string, string | undefined>; timeoutMs: number },
): Promise<ChildResult> {
	return new Promise((resolve) => {
		const started = Date.now();
		const child = spawn(command, args, {
			cwd: options.cwd,
			env: options.env ?? process.env,
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
		child.on("close", (status) => {
			clearTimeout(timer);
			resolve({ status, ms: Date.now() - started, stdout, stderr, timedOut });
		});
	});
}

async function probe(args: string[]): Promise<Record<string, unknown>> {
	const result = await runChild("python3", [PROBE, ...args], { timeoutMs: 300_000 });
	try {
		return JSON.parse(result.stdout) as Record<string, unknown>;
	} catch {
		return {
			error: `probe exit ${result.status}`,
			stdout: result.stdout.slice(-2000),
			stderr: result.stderr.slice(-2000),
		};
	}
}

function startIndexer(interval: number) {
	const indexerLog = join(OUT, "indexer.jsonl");
	const child = spawn(
		"kp-agent-desk",
		["indexer", "--root", "/state/memory", "--watch", "--interval", String(interval)],
		{
			stdio: ["ignore", "pipe", "pipe"],
		},
	);
	child.stdout.on("data", (data) => appendFileSync(indexerLog, data));
	child.stderr.on("data", (data) => appendFileSync(indexerLog, data));
	return child;
}

function treeOf(root: string, depth = 3): string[] {
	const found: string[] = [];
	const walk = (directory: string, left: number) => {
		let entries: string[];
		try {
			entries = readdirSync(directory);
		} catch {
			return;
		}
		for (const entry of entries) {
			const path = join(directory, entry);
			found.push(path);
			try {
				if (left > 0 && statSync(path).isDirectory()) walk(path, left - 1);
			} catch {
				// Gone.
			}
		}
	};
	walk(root, depth);
	return found;
}

// --------------------------------------------------------------------------- the board launch

const captured = new Map<string, CapturedLaunch>();

function captureSpawns(): void {
	const original = PtySession.spawn.bind(PtySession);
	PtySession.spawn = ((request: Parameters<typeof PtySession.spawn>[0]) => {
		const args = request.args;
		captured.set(request.cwd, {
			binary: request.binary,
			args: typeof args === "string" ? [args] : [...(args ?? [])],
			cwd: request.cwd,
			env: Object.fromEntries(
				Object.entries(request.env ?? {}).filter((entry): entry is [string, string] => entry[1] !== undefined),
			),
		});
		return original(request);
	}) as typeof PtySession.spawn;
}

async function startDeskTask(
	manager: TerminalSessionManager,
	taskId: string,
	cwd: string,
	deskId: string,
	prompt: string,
) {
	await manager.startTaskSession({
		taskId,
		agentId: "opencode",
		binary: "opencode",
		args: [],
		autonomousModeEnabled: true,
		cwd,
		prompt,
		workspaceId: "o2-workspace",
		deskId,
		cols: 120,
		rows: 40,
	});
}

function receiptOf(launch: CapturedLaunch | undefined): string | null {
	return launch?.env.KP_AGENT_LAUNCH_RECEIPT ?? null;
}

/** `opencode run --format json` with a captured launch's own environment and cwd (O1's instrument form). */
async function runInstrument(
	launch: CapturedLaunch,
	args: string[],
	extraEnv: Record<string, string>,
	timeoutMs: number,
) {
	const result = await runChild("opencode", ["run", "--format", "json", ...args], {
		cwd: launch.cwd,
		env: { ...launch.env, PWD: launch.cwd, ...extraEnv },
		timeoutMs,
	});
	return {
		status: result.status,
		ms: result.ms,
		timedOut: result.timedOut,
		session: /"sessionID":"(ses_[A-Za-z0-9]+)"/.exec(result.stdout)?.[1] ?? null,
		stdoutTail: result.stdout.slice(-1500),
		stderrTail: result.stderr.slice(-1500),
	};
}

/**
 * The launch-hook and export children seen in /proc while `watch` runs (sampled every 100 ms): the hook runs
 * detached from OpenCode, so its process is the observable sign that a turn end or the terminal's exit ran it.
 */
const everSeen = new Set<string>();

function watchProcesses(): { stop: () => Array<{ at: number; kind: string; pid: string }> } {
	const seen = new Map<string, { at: number; kind: string; pid: string }>();
	const sample = () => {
		let pids: string[] = [];
		try {
			pids = readdirSync("/proc").filter((entry) => /^\d+$/.test(entry));
		} catch {
			return;
		}
		for (const pid of pids) {
			let args: string[];
			try {
				args = readFileSync(`/proc/${pid}/cmdline`, "utf8").split("\0");
			} catch {
				continue;
			}
			const kind =
				args.includes("kp_agent_tooling.launch_cli") && args.includes("hook")
					? "launch-hook"
					: args.includes("--pure") && args.includes("export")
						? "export-child"
						: null;
			// A process an earlier watch saw (say, a Stop hook still exiting) is not this watch's.
			if (kind && !everSeen.has(pid)) {
				everSeen.add(pid);
				seen.set(pid, { at: Date.now(), kind, pid });
			}
		}
	};
	const timer = setInterval(sample, 100);
	return {
		stop: () => {
			clearInterval(timer);
			return [...seen.values()];
		},
	};
}

function egressSince(at: number) {
	return readJsonLines(EGRESS_LOG).filter((entry) => (entry.at as number) >= at);
}

// --------------------------------------------------------------------------- F1

async function runF1(spec: Spec): Promise<void> {
	const result: Record<string, unknown> = {};
	const save = () => writeFileSync(spec.out, JSON.stringify(result, null, 1));
	const world = buildWorld(1);
	const desk = world.desks[0] as World["desks"][number];
	result.desk = desk;
	process.env.KANBAN_LAUNCH_BINDING_COMMAND = JSON.stringify([
		"/usr/local/bin/kp-agent-launch",
		"--config",
		world.operator,
	]);
	const indexer = startIndexer(spec.interval);
	captureSpawns();
	const repo = repository("repo-f1");
	const token = `zq${Date.now().toString(36)}`;
	const manager = new TerminalSessionManager();
	const egressStart = Date.now();
	const launchedAt = Date.now();
	const turnProcesses = watchProcesses();
	try {
		await startDeskTask(manager, "o2-f1", repo, desk.deskId, `o2-turn token=${token}`);
		result.launched = true;
	} catch (error) {
		result.launched = false;
		result.refusal = error instanceof Error ? error.message : String(error);
		save();
		indexer.kill();
		return;
	}
	const launch = captured.get(repo);
	const receipt = receiptOf(launch);
	result.launch = {
		argv: launch?.args,
		opencodeEnv: Object.fromEntries(
			Object.entries(launch?.env ?? {}).filter(
				([key]) => key.startsWith("OPENCODE_") || key.startsWith("XDG_") || key.startsWith("KP_"),
			),
		),
		providerKeys: Object.entries(launch?.env ?? {})
			.filter(([key, value]) => /_API_KEY$/.test(key) && value)
			.map(([key]) => key),
	};
	save();
	// The turn is captured: bound by the plugin's Stop, sealed, enqueued, then found by a desk search.
	const observed = await probe([
		"observe",
		receipt ?? "",
		`${token}answer`,
		String(Math.round(spec.timeoutMs / 1000)),
		String(launchedAt / 1000),
	]);
	result.observe = observed;
	result.hooksWhileRunning = readJsonLines(HOOK_LOG).filter((entry) => entry.taskId === "o2-f1").length;
	result.stubWhileRunning = readJsonLines(STUB_LOG);
	result.egressDuringLaunch = egressSince(egressStart);
	result.configDirs = {
		boundConfigHome: launch ? treeOf(join(launch.env.XDG_CONFIG_HOME ?? "/nonexistent", "opencode"), 2) : null,
		boundConfigHomeMode: launch
			? (statSync(join(launch.env.XDG_CONFIG_HOME ?? "/", "opencode")).mode & 0o777).toString(8)
			: null,
		homeOpencode: existsSync("/state/.opencode") ? treeOf("/state/.opencode", 2) : null,
	};
	save();
	// The board terminal exits: SessionEnd runs for the bound session; the snapshot is already captured.
	result.processesDuringTurn = turnProcesses.stop();
	const exitProcesses = watchProcesses();
	manager.stopTaskSession("o2-f1");
	await new Promise((resolve) => setTimeout(resolve, 15_000));
	result.processesAfterExit = exitProcesses.stop();
	result.afterSessionEnd = await probe(["counts", receipt ?? ""]);
	result.egressAfterSessionEnd = egressSince(egressStart);
	save();
	// The A1 instrument controls, on the same launch environment: a writable config directory without
	// node_modules as the carrier (the order's mutant), and O1's unbound global config directory.
	if (launch) {
		const writable = join(WORLD, "mutant-config-dir");
		mkdirSync(writable, { recursive: true });
		const controlStart = Date.now();
		const carrier = await runInstrument(
			launch,
			[`o2-turn token=${token}ctl`],
			{ OPENCODE_CONFIG_DIR: writable },
			120_000,
		);
		result.controlWritableCarrier = { ...carrier, egress: egressSince(controlStart), tree: treeOf(writable, 1) };
		const before = Date.now();
		const quiet = await runInstrument(launch, [`o2-turn token=${token}qt`], {}, 120_000);
		result.controlBoundEnvironmentRun = { ...quiet, egress: egressSince(before) };
	}
	save();
	indexer.kill();
	result.indexerTail = existsSync(join(OUT, "indexer.jsonl"))
		? readFileSync(join(OUT, "indexer.jsonl"), "utf8").split("\n").slice(-5)
		: [];
	save();
}

// --------------------------------------------------------------------------- F2

async function runF2(spec: Spec): Promise<void> {
	const result: Record<string, unknown> = {};
	const save = () => writeFileSync(spec.out, JSON.stringify(result, null, 1));
	const world = buildWorld(2);
	result.desks = world.desks;
	process.env.KANBAN_LAUNCH_BINDING_COMMAND = JSON.stringify([
		"/usr/local/bin/kp-agent-launch",
		"--config",
		world.operator,
	]);
	const indexer = startIndexer(spec.interval);
	captureSpawns();
	const manager = new TerminalSessionManager();
	const launches = world.desks.map((desk, index) => ({
		desk,
		task: `o2-f2-${index + 1}`,
		repo: repository(`repo-f2-${index + 1}`),
		token: `zq${index + 1}${Date.now().toString(36)}`,
	}));
	// Side by side, started one after the other: the second task starts once the first is bound, and both stay
	// running through the probes. Measured at the pin, two OpenCode TUIs starting within a second of each other on
	// one data directory can stall one of them after its `init` log line, with or without a launch binding
	// (unbound board launches stall the same way), so a simultaneous cold start is not what F2 measures.
	const bindings: Array<Record<string, unknown>> = [];
	for (const launch of launches) {
		await startDeskTask(manager, launch.task, launch.repo, launch.desk.deskId, `o2-turn token=${launch.token}`);
		const receipt = receiptOf(captured.get(launch.repo));
		bindings.push(await probe(["wait-bound", receipt ?? "", String(Math.round(spec.timeoutMs / 1000))]));
	}
	result.bothRunning = launches.map((launch) => manager.getSummary(launch.task)?.state ?? null);
	const rows: Array<Record<string, unknown>> = [];
	for (const [index, launch] of launches.entries()) {
		const captured1 = captured.get(launch.repo);
		const receipt = receiptOf(captured1);
		const bound = bindings[index] as Record<string, unknown>;
		const row: Record<string, unknown> = {
			task: launch.task,
			deskId: launch.desk.deskId,
			bindingKey: launch.desk.bindingKey,
			receipt,
			bound,
		};
		if (typeof bound.session !== "string") {
			// Never bound: what the board's terminal shows for the task tells a launch that never ran a turn
			// from a turn whose hook failed.
			const snapshot = await manager.getRestoreSnapshot(launch.task).catch(() => null);
			row.summary = manager.getSummary(launch.task);
			row.screenTail = JSON.stringify(snapshot ?? null).slice(-4000);
			const logs = "/state/.local/share/opencode/log";
			row.opencodeLogs = existsSync(logs)
				? readdirSync(logs).map((name) => ({ name, tail: readFileSync(join(logs, name), "utf8").slice(-20_000) }))
				: null;
		}
		if (captured1 && typeof bound.session === "string") {
			const config = await runChild("opencode", ["debug", "config"], {
				cwd: captured1.cwd,
				env: { ...captured1.env, PWD: captured1.cwd },
				timeoutMs: 60_000,
			});
			try {
				row.mcp = (JSON.parse(config.stdout) as { mcp?: unknown }).mcp ?? null;
			} catch {
				row.mcp = { unparsed: config.stdout.slice(0, 1500), stderr: config.stderr.slice(-800) };
			}
			const before = readJsonLines(STUB_LOG).length;
			row.run = await runInstrument(
				captured1,
				["--session", bound.session, `o2-probe mcp=bindings token=${launch.token}p`],
				{},
				120_000,
			);
			row.stub = readJsonLines(STUB_LOG)
				.slice(before)
				.filter((entry) => entry.token === `${launch.token}p`);
		}
		rows.push(row);
		result.launches = rows;
		save();
	}
	for (const launch of launches) manager.stopTaskSession(launch.task);
	await new Promise((resolve) => setTimeout(resolve, 5_000));
	indexer.kill();
	save();
}

// --------------------------------------------------------------------------- main

async function main(): Promise<void> {
	const [mode, specPath] = process.argv.slice(2);
	if (!specPath) throw new Error("usage: runner.mjs f1|f2 <spec.json>");
	mkdirSync(OUT, { recursive: true });
	mkdirSync("/state/kanban", { recursive: true }); // KANBAN_STORAGE_ROOT, as the board role's
	const spec = JSON.parse(readFileSync(specPath, "utf8")) as Spec;
	await startDnsRecorder();
	await startConnectionRecorder(80);
	await startConnectionRecorder(443);
	await startStubProvider();
	if (mode === "f1") await runF1(spec);
	else if (mode === "f2") await runF2(spec);
	else throw new Error(`unknown mode ${mode}`);
}

main().then(
	() => process.exit(0),
	(error) => {
		process.stderr.write(`o2 runner failed: ${error instanceof Error ? error.stack : String(error)}\n`);
		process.exit(1);
	},
);
