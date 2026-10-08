// Runs inside the OpenCode image (order O3's target) for the O1 A1 check:
// node run-in-image.mjs <plan.json> <results.json>
// The plan holds, per fixture, the environment, working directory and argv the board adapter produced.
// For each fixture this runs three instruments against the pinned binary:
//   1. `opencode debug config --print-logs`: the resolved configuration and every file OpenCode loaded;
//   2. `opencode debug agent build`: the agent's merged permission rules, in evaluation order;
//   3. the launched interactive form itself (the TUI with --prompt) under a pseudo-terminal (the board's
//      own node-pty), one run per tool case, until the stub model receives the tool's result or the
//      board plugin records a review move; then the run is stopped. An ask never resolves in the TUI, so
//      a command that ran was allowed, and a tool result without an ask was a deny.
import { spawn, spawnSync } from "node:child_process";
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const pty = createRequire("/app/")("node-pty");
const here = dirname(fileURLToPath(import.meta.url));
const [planFile, resultsFile] = process.argv.slice(2);
const plan = JSON.parse(readFileSync(planFile, "utf8"));

const read = (path) => (existsSync(path) ? readFileSync(path, "utf8") : "");
const lines = (text) =>
	text
		.split("\n")
		.filter(Boolean)
		.map((line) => {
			try {
				return JSON.parse(line);
			} catch {
				return { unparsed: line };
			}
		});
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

writeFileSync(plan.stubLog, "");
const stub = spawn(process.execPath, [join(here, "stub-provider.mjs"), String(plan.stubPort), plan.stubLog], { stdio: "ignore" });
for (let attempt = 0; attempt < 50 && !read(plan.stubLog).includes("listening"); attempt += 1) await sleep(100);

function debug(fixture, args) {
	const result = spawnSync("opencode", args, {
		cwd: fixture.cwd,
		env: fixture.env,
		encoding: "utf8",
		timeout: 120_000,
		killSignal: "SIGKILL",
		maxBuffer: 32 * 1024 * 1024,
	});
	return { status: result.status, signal: result.signal, stdout: result.stdout ?? "", stderr: result.stderr ?? "" };
}

async function interactive(fixture, run) {
	const stubStart = read(plan.stubLog).length;
	const hookStart = read(fixture.hookLog).length;
	const started = Date.now();
	const child = pty.spawn(run.argv[0], run.argv.slice(1), {
		name: "xterm-256color",
		cols: 140,
		rows: 45,
		cwd: fixture.cwd,
		env: fixture.env,
	});
	let screen = "";
	child.onData((data) => {
		screen = (screen + data).slice(-200_000);
	});
	let exited = null;
	child.onExit((event) => {
		exited = event;
	});
	let reason = "timeout";
	let stopAt = null;
	while (!exited && Date.now() - started < 100_000) {
		const stubLines = lines(read(plan.stubLog).slice(stubStart));
		const hookLines = lines(read(fixture.hookLog).slice(hookStart));
		if (stopAt === null) {
			if (stubLines.some((line) => line.kind === "chat" && line.lastRole === "tool")) {
				reason = "tool-result-sent";
				stopAt = Date.now() + 3_000;
			} else if (hookLines.some((line) => line.event === "to_review")) {
				reason = "review-recorded";
				stopAt = Date.now() + 3_000;
			}
		}
		if (stopAt !== null && Date.now() >= stopAt) break;
		await sleep(250);
	}
	if (!exited) {
		try {
			process.kill(-child.pid, "SIGKILL");
		} catch {
			child.kill("SIGKILL");
		}
	}
	await sleep(500);
	const plain = screen.replace(/\x1b\[[0-9;?]*[ -/]*[@-~]/g, "").replace(/\x1b\][^\x07]*\x07/g, "");
	return {
		case: run.case,
		reason,
		ms: Date.now() - started,
		exited,
		stub: lines(read(plan.stubLog).slice(stubStart)),
		hooks: lines(read(fixture.hookLog).slice(hookStart)),
		markers: Object.fromEntries((run.markers ?? []).map((path) => [path, existsSync(path)])),
		screenTail: plain.slice(-1200),
	};
}

const results = [];
for (const fixture of plan.fixtures) {
	const result = { id: fixture.id, config: null, agent: null, runs: [] };
	result.config = debug(fixture, ["debug", "config", "--print-logs", "--log-level", "INFO"]);
	result.agent = debug(fixture, ["debug", "agent", "build"]);
	for (const run of fixture.runs) result.runs.push(await interactive(fixture, run));
	results.push(result);
	writeFileSync(resultsFile, JSON.stringify(results, null, 1));
}
stub.kill();
console.log(`fixtures=${results.length}`);
