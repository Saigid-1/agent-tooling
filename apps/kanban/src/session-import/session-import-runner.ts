// Host-configured bridge to the bounded, local session import CLI.

import { spawn } from "node:child_process";
import { access, stat } from "node:fs/promises";
import { constants } from "node:fs";
import { basename, isAbsolute } from "node:path";
import { sessionImportResultSchema, type SessionImportAction, type SessionImportResult, type SessionImportSetup } from "./session-import-contract";

const EXECUTABLE_ENV = "KANBAN_SESSION_IMPORT_EXECUTABLE";
const CONFIG_ENV = "KANBAN_SESSION_IMPORT_CONFIG";
const MAX_OUTPUT_BYTES = 1_000_000;
const COMMAND_TIMEOUT_MS = 120_000;

interface HostConfig {
	executablePath: string;
	configPath: string;
}

export async function getSessionImportSetup(env: NodeJS.ProcessEnv = process.env): Promise<SessionImportSetup> {
	const executablePath = env[EXECUTABLE_ENV]?.trim() || null;
	const configPath = env[CONFIG_ENV]?.trim() || null;
	if (!executablePath || !configPath) {
		return {
			configured: false,
			message: `Set ${EXECUTABLE_ENV} and ${CONFIG_ENV} in the Kanban host environment. The config must identify an initialized, registered desk for the exact host session.`,
			executablePath,
			configPath,
		};
	}
	if (!isAbsolute(executablePath) || basename(executablePath) !== "kp-agent-session-import" || !isAbsolute(configPath)) {
		return {
			configured: false,
			message: "The host must select an absolute kp-agent-session-import executable and an absolute private desk config path.",
			executablePath,
			configPath,
		};
	}
	try {
		const [executable, config] = await Promise.all([stat(executablePath), stat(configPath)]);
		await access(executablePath, constants.X_OK);
		if (!executable.isFile() || !config.isFile()) {
			throw new Error("Host paths must name regular files");
		}
	} catch {
		return {
			configured: false,
			message: "The import executable or desk config is unavailable. Check the selected host paths and desk registration.",
			executablePath,
			configPath,
		};
	}
	return { configured: true, message: "Host import service is configured. Preview will verify the selected session and desk.", executablePath, configPath };
}

async function requireHostConfig(): Promise<HostConfig> {
	const setup = await getSessionImportSetup();
	if (!setup.configured || !setup.executablePath || !setup.configPath) {
		throw new Error(setup.message);
	}
	return { executablePath: setup.executablePath, configPath: setup.configPath };
}

function buildInput(action: SessionImportAction): object {
	if (action.action === "preview") {
		return {
			schema_version: "ops.session-import.request.v1",
			runtime: "codex",
			source_file: action.sourceFile,
			native_session_id: action.nativeSessionId,
			selected_desk_id: action.selectedDeskId,
			mode: action.mode,
			follow: action.mode === "current-turn-and-forward",
			import_actor: "kanban-user",
		};
	}
	if (action.action === "apply") {
		return { schema_version: "ops.session-import.apply.v1", plan_token: action.planToken, consent: true };
	}
	if (action.action === "assert-owner") {
		return {
			schema_version: "ops.session-import.owner-assertion.v1",
			job_id: action.jobId,
			selected_desk_id: action.selectedDeskId,
			asserted_by: action.assertedBy,
			recorded_at: new Date().toISOString(),
			evidence: ["operator_confirmed_exact_session_preview"],
		};
	}
	if (action.action === "desks" || action.action === "list-following") return {};
	return { schema_version: "ops.session-import.job-ref.v1", job_id: action.jobId };
}

function parseResult(stdout: string): SessionImportResult {
	const lines = stdout.trim().split(/\r?\n/);
	const last = lines.at(-1);
	if (!last) {
		throw new Error("The import service returned no result.");
	}
	try {
		return sessionImportResultSchema.parse(JSON.parse(last));
	} catch {
		throw new Error("The import service returned an invalid result.");
	}
}

async function runBoundedCommand(config: HostConfig, action: SessionImportAction): Promise<SessionImportResult> {
	return await new Promise((resolve, reject) => {
		const args = ["--config", config.configPath, action.action];
		if (action.action !== "desks" && action.action !== "list-following") args.push("--input", "-");
		const child = spawn(config.executablePath, args, {
			stdio: ["pipe", "pipe", "ignore"],
			shell: false,
		});
		let stdout = "";
		let settled = false;
		const fail = (message: string) => {
			if (settled) return;
			settled = true;
			child.kill();
			reject(new Error(message));
		};
		const timer = setTimeout(() => fail("The import service timed out."), COMMAND_TIMEOUT_MS);
		child.stdout.on("data", (chunk: Buffer) => {
			stdout += chunk.toString("utf8");
			if (Buffer.byteLength(stdout, "utf8") > MAX_OUTPUT_BYTES) fail("The import service returned too much data.");
		});
		child.on("error", () => fail("The configured import executable could not start."));
		child.on("close", (exitCode) => {
			clearTimeout(timer);
			if (settled) return;
			settled = true;
			try {
				const result = parseResult(stdout);
				if (exitCode !== 0 && result.status !== "error") {
					reject(new Error("The import service failed before completing this action."));
				} else {
					resolve(result);
				}
			} catch (error) {
				reject(error);
			}
		});
		child.stdin.end(action.action === "desks" || action.action === "list-following" ? undefined : JSON.stringify(buildInput(action)));
	});
}

async function startFollowing(config: HostConfig, jobId: string): Promise<SessionImportResult> {
	return await new Promise((resolve, reject) => {
		const child = spawn(config.executablePath, ["--config", config.configPath, "follow", "--input", "-"], {
			stdio: ["pipe", "ignore", "ignore"],
			shell: false,
			detached: true,
		});
		child.once("error", () => reject(new Error("The configured follow worker could not start.")));
		child.once("spawn", () => {
			child.stdin.end(JSON.stringify({ schema_version: "ops.session-import.job-ref.v1", job_id: jobId }));
			child.unref();
			resolve({ schema_version: "ops.session-import.result.v1", status: "ok", phase: "start_requested", job_id: jobId });
		});
	});
}

export async function runSessionImport(action: SessionImportAction): Promise<SessionImportResult> {
	const config = await requireHostConfig();
	if (action.action === "follow") return await startFollowing(config, action.jobId);
	return await runBoundedCommand(config, action);
}

export async function recoverFollowingSessionImports(warn: (message: string) => void): Promise<void> {
	const setup = await getSessionImportSetup();
	if (!setup.configured) return;
	const listing = await runSessionImport({ action: "list-following" });
	for (const job of listing.jobs ?? []) {
		if (!["following", "ready", "waiting_for_complete_row", "index_pending", "processing"].includes(job.phase ?? "") || job.worker_active === true) continue;
		try {
			await runSessionImport({ action: "follow", jobId: job.job_id });
		} catch {
			warn(`Could not restart session import follower for job ${job.job_id}.`);
		}
	}
}

// A killed container can leave a valid lease behind. Recheck after its expiry;
// never steal a lease merely because this board process has just started.
export function startSessionImportRecovery(warn: (message: string) => void): () => Promise<void> {
	let pending: Promise<void> | null = null;
	const recover = () => {
		if (pending) return;
		pending = recoverFollowingSessionImports(warn)
			.catch(() => warn("Could not inspect persisted session import followers; recovery will retry."))
			.finally(() => { pending = null; });
	};
	recover();
	const timer = setInterval(recover, 30_000);
	timer.unref();
	return async () => {
		clearInterval(timer);
		await pending;
	};
}
