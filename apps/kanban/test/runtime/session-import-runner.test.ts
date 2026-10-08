import { chmod, mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { execFileSync } from "node:child_process";
import { afterEach, describe, expect, it } from "vitest";

import { getSessionImportSetup, runSessionImport } from "../../src/session-import/session-import-runner";

const EXECUTABLE_ENV = "KANBAN_SESSION_IMPORT_EXECUTABLE";
const CONFIG_ENV = "KANBAN_SESSION_IMPORT_CONFIG";
const previousExecutable = process.env[EXECUTABLE_ENV];
const previousConfig = process.env[CONFIG_ENV];
const previousPythonPath = process.env.PYTHONPATH;
const previousTestPython = process.env.TEST_IMPORT_PYTHON;
let temporaryRoot: string | null = null;

afterEach(async () => {
	if (previousExecutable === undefined) delete process.env[EXECUTABLE_ENV];
	else process.env[EXECUTABLE_ENV] = previousExecutable;
	if (previousConfig === undefined) delete process.env[CONFIG_ENV];
	else process.env[CONFIG_ENV] = previousConfig;
	if (previousPythonPath === undefined) delete process.env.PYTHONPATH;
	else process.env.PYTHONPATH = previousPythonPath;
	if (previousTestPython === undefined) delete process.env.TEST_IMPORT_PYTHON;
	else process.env.TEST_IMPORT_PYTHON = previousTestPython;
	if (temporaryRoot) await rm(temporaryRoot, { recursive: true, force: true });
	temporaryRoot = null;
});

describe("session import host bridge", () => {
	it("explains missing host setup without picking a command", async () => {
		const setup = await getSessionImportSetup({});
		expect(setup.configured).toBe(false);
		expect(setup.message).toContain(EXECUTABLE_ENV);
	});

	it("uses only the fixed host executable and JSON stdin for preview", async () => {
		temporaryRoot = await mkdtemp(join(tmpdir(), "kanban-session-import-"));
		const executable = join(temporaryRoot, "kp-agent-session-import");
		const config = join(temporaryRoot, "desk.json");
		await writeFile(config, "{}", { mode: 0o600 });
		await writeFile(executable, `#!/usr/bin/env node
let input = "";
process.stdin.on("data", chunk => input += chunk);
process.stdin.on("end", () => {
  const body = JSON.parse(input);
  process.stdout.write(JSON.stringify({schema_version:"ops.session-import.result.v1",status:"preview",plan_token:"plan",counts:{rows:3},attribution:{selected_desk_id:body.selected_desk_id},coverage:{start_offset:0,end_offset:2,source_size:2,mode:body.mode,complete:true}}) + "\\n");
});
`, { mode: 0o700 });
		await chmod(executable, 0o700);
		process.env[EXECUTABLE_ENV] = executable;
		process.env[CONFIG_ENV] = config;
		const result = await runSessionImport({
			action: "preview", sourceFile: "/private/native.jsonl", nativeSessionId: "native-id",
			selectedDeskId: "reviewed-binding", mode: "full",
		});
		expect(result.status).toBe("preview");
		expect(result.attribution?.selected_desk_id).toBe("reviewed-binding");
		expect(result.counts?.rows).toBe(3);
	});

	it.skipIf(!previousTestPython)("reads the real bounded CLI preview from a synthetic admitted desk and native file", async () => {
		const repositoryRoot = resolve(process.cwd(), "../..");
		const python = previousTestPython;
		if (!python) throw new Error("Set TEST_IMPORT_PYTHON to the installed tooling interpreter");
		temporaryRoot = await mkdtemp(join(tmpdir(), "kanban-session-real-cli-"));
		const stateRoot = join(temporaryRoot, "state");
		await mkdir(stateRoot, { mode: 0o700 });
		const catalog = join(temporaryRoot, "catalog.json");
		const doctrine = join(temporaryRoot, "doctrine.md");
		await writeFile(catalog, await readFile(join(repositoryRoot, "config/desk-context/catalog.example.json")), { mode: 0o600 });
		await writeFile(doctrine, await readFile(join(repositoryRoot, "config/desk-context/doctrine.md")));
		const config = join(temporaryRoot, "desk.json");
		await writeFile(config, JSON.stringify({ schema_version: "ops.desk-memory.local.v1", state_root: stateRoot,
			catalog_path: catalog, workspace_root: temporaryRoot, provider_instance: "test", provider_session_id: "session-1" }), { mode: 0o600 });
		process.env.PYTHONPATH = join(repositoryRoot, "packages/tooling/src");
		execFileSync(python, ["-c", "from pathlib import Path; from kp_agent_tooling._impl.service.desk_memory_runtime import initialize, admit; import sys; p=Path(sys.argv[1]); initialize(p); admit(p, desk_id='implementation-desk', provider_id='test', model_id='test')", config], { env: process.env });
		const nativeId = "a55869b4-6ab4-4e5c-b028-8d4b31d27b71";
		const sourceFile = join(temporaryRoot, `rollout-2026-09-24T12-00-00-${nativeId}.jsonl`);
		const rows = [
			{ type: "session_meta", timestamp: "2026-09-24T12:00:00Z", payload: { id: nativeId, cwd: "/synthetic/repo" } },
			{ type: "event_msg", timestamp: "2026-09-24T12:01:00Z", payload: { type: "user_message", message: "synthetic local request" } },
		];
		await writeFile(sourceFile, `${rows.map((row) => JSON.stringify(row)).join("\n")}\n`);
		const executable = join(temporaryRoot, "kp-agent-session-import");
		await writeFile(executable, `#!/usr/bin/env node
const fs = require("node:fs");
const { spawnSync } = require("node:child_process");
const result = spawnSync(process.env.TEST_IMPORT_PYTHON, ["-m", "kp_agent_tooling.session_import_cli", ...process.argv.slice(2)], { input: fs.readFileSync(0), env: process.env });
process.stdout.write(result.stdout);
process.exit(result.status ?? 1);
`, { mode: 0o700 });
		await chmod(executable, 0o700);
		process.env.TEST_IMPORT_PYTHON = python;
		process.env[EXECUTABLE_ENV] = executable;
		process.env[CONFIG_ENV] = config;
		const desks = await runSessionImport({ action: "desks" });
		const selectedDeskId = desks.desks?.[0]?.binding_key;
		expect(selectedDeskId).toBeTruthy();
		if (!selectedDeskId) throw new Error("No registered desk in real CLI fixture");
		const preview = await runSessionImport({ action: "preview", sourceFile, nativeSessionId: nativeId,
			selectedDeskId, mode: "full" });
		expect(preview.status).toBe("preview");
		expect(preview.coverage?.next_offset).toBe(0);
		expect(preview.next_batch_offset).toBeGreaterThan(0);
		expect(preview.coverage?.observed_size).toBeGreaterThan(0);
		expect(preview.counts?.visible_events).toBe(1);
		if (!preview.plan_token || !preview.job_id) throw new Error("Preview identity missing");
		const applied = await runSessionImport({ action: "apply", planToken: preview.plan_token, consent: true });
		expect(applied.phase).toBe("ready");
		expect(applied.follower_heartbeat_at).toBeNull();
		const captured = await runSessionImport({ action: "continue", jobId: preview.job_id });
		expect(captured.counts?.imported).toBe(1);
		await runSessionImport({ action: "continue", jobId: preview.job_id });
		const status = await runSessionImport({ action: "status", jobId: preview.job_id });
		expect(status.phase).toBe("complete");
		expect(status.worker_active).toBe(false);

	});
});
