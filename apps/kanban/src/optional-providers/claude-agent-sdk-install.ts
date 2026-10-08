// The board action that installs the Claude Agent SDK (tRPC
// runtime.installClaudeAgentSdk). It runs only on the user's request, and only
// when that request carries the SHA-256 of the pinned notice the board showed:
// without it nothing is fetched and nothing is written. It installs the package
// list in claude-agent-sdk-pin.json `install` (today @anthropic-ai/claude-agent-sdk,
// at the lockfile's version unless the user chose another, and
// ai-sdk-provider-claude-code at the lockfile's version) with npm into
// a staging directory under the install root, checks the result against the
// lockfile's integrity, moves it into place, loads it into the running board,
// and only then records it as the current install and removes the superseded
// installs. A failed install removes only its own staging directory. The board
// does not restart.

import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { mkdir, mkdtemp, readFile, rename, rm, writeFile } from "node:fs/promises";
import { dirname, join } from "node:path";

import type { RuntimeClaudeAgentSdkInstallRequest, RuntimeClaudeAgentSdkInstallResponse } from "../core/api-contract";
import {
	activateClaudeAgentSdkInstall,
	CLAUDE_AGENT_SDK_INSTALL_PACKAGES,
	CLAUDE_AGENT_SDK_PIN,
	CLAUDE_AGENT_SDK_PINNED_VERSION,
	CLAUDE_CODE_PROVIDER_PACKAGE,
	errorMessage,
	getClaudeAgentSdkInstallRecordPath,
	getClaudeAgentSdkInstallRoot,
	getClaudeAgentSdkInstallsDirectory,
	getClaudeAgentSdkStatus,
	INSTALL_RECORD_SCHEMA,
	type InstallRecord,
	isClaudeAgentSdkInstallRunning,
	markClaudeAgentSdkInstallFinished,
	markClaudeAgentSdkInstallStarted,
	pinnedPackage,
	readInstalledPackageVersion,
	removeSupersededClaudeAgentSdkInstalls,
	writeClaudeAgentSdkLoader,
} from "./claude-agent-sdk";
import { CLAUDE_AGENT_SDK_NOTICE_SHA256, isClaudeAgentSdkNoticePinned } from "./claude-agent-sdk-notice";

/** Optional override: an npm CLI script the install runs with the board's own Node (tests, mirrors). */
export const CLAUDE_AGENT_SDK_NPM_CLI_ENV = "KANBAN_CLAUDE_AGENT_SDK_NPM_CLI";

const STAGING_PREFIX = ".staging-";
const NPM_TIMEOUT_MS = 15 * 60_000;
const OUTPUT_TAIL_CHARS = 4_000;
const EXACT_VERSION = /^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$/;

type InstallOutcome = Omit<RuntimeClaudeAgentSdkInstallResponse, "status">;

interface NpmInvocation {
	command: string;
	argsPrefix: string[];
	shell: boolean;
}

function resolveNpmInvocation(): NpmInvocation {
	const configured = process.env[CLAUDE_AGENT_SDK_NPM_CLI_ENV]?.trim();
	if (configured) {
		return { command: process.execPath, argsPrefix: [configured], shell: false };
	}
	const nodeDirectory = dirname(process.execPath);
	for (const candidate of [
		join(nodeDirectory, "..", "lib", "node_modules", "npm", "bin", "npm-cli.js"),
		join(nodeDirectory, "node_modules", "npm", "bin", "npm-cli.js"),
	]) {
		if (existsSync(candidate)) {
			return { command: process.execPath, argsPrefix: [candidate], shell: false };
		}
	}
	const windows = process.platform === "win32";
	return { command: windows ? "npm.cmd" : "npm", argsPrefix: [], shell: windows };
}

function appendTail(current: string, chunk: string): string {
	const next = current + chunk;
	return next.length > OUTPUT_TAIL_CHARS ? next.slice(next.length - OUTPUT_TAIL_CHARS) : next;
}

/** npm install in the staging directory: no lifecycle scripts, its cache inside the staging directory. */
async function runNpmInstall(stagingDirectory: string): Promise<void> {
	const npm = resolveNpmInvocation();
	const args = [
		...npm.argsPrefix,
		"install",
		"--no-audit",
		"--no-fund",
		"--no-update-notifier",
		"--ignore-scripts",
		"--omit=dev",
		"--cache",
		join(stagingDirectory, ".npm-cache"),
		"--prefix",
		stagingDirectory,
	];
	const env: NodeJS.ProcessEnv = { ...process.env, npm_config_update_notifier: "false" };
	delete env.KANBAN_INTERNAL_AUTH_TOKEN;
	await new Promise<void>((resolvePromise, rejectPromise) => {
		const child = spawn(npm.command, args, {
			cwd: stagingDirectory,
			env,
			shell: npm.shell,
			stdio: ["ignore", "pipe", "pipe"],
			windowsHide: true,
		});
		let output = "";
		const collect = (chunk: Buffer) => {
			output = appendTail(output, chunk.toString("utf8"));
		};
		child.stdout?.on("data", collect);
		child.stderr?.on("data", collect);
		const timer = setTimeout(() => {
			child.kill("SIGTERM");
			setTimeout(() => child.kill("SIGKILL"), 5_000).unref();
		}, NPM_TIMEOUT_MS);
		timer.unref();
		child.on("error", (error) => {
			clearTimeout(timer);
			rejectPromise(new Error(`Could not run npm: ${error.message}`));
		});
		child.on("close", (code, signal) => {
			clearTimeout(timer);
			if (code === 0) {
				resolvePromise();
				return;
			}
			const reason = signal ? `signal ${signal}` : `code ${code}`;
			rejectPromise(new Error(`npm install exited with ${reason}.${output.trim() ? `\n${output.trim()}` : ""}`));
		});
	});
}

/**
 * The install's package list (claude-agent-sdk-pin.json `install`) at the versions this
 * request installs: the user's version for the userVersion entry, the pinned version
 * for every other entry.
 */
function requestedPackages(userVersion: string): Array<[name: string, version: string]> {
	return CLAUDE_AGENT_SDK_INSTALL_PACKAGES.map((entry) => [
		entry.name,
		entry.userVersion ? userVersion : pinnedPackage(entry.name).version,
	]);
}

function packageNameFromLockPath(lockPath: string): string | null {
	const marker = "node_modules/";
	const index = lockPath.lastIndexOf(marker);
	return index >= 0 ? lockPath.slice(index + marker.length) : null;
}

/**
 * Checks npm's result: every requested package is installed at its requested
 * version, and every package at a version the board's lockfile pins carries the
 * lockfile's integrity.
 */
async function verifyStagedInstall(
	stagingDirectory: string,
	requested: Array<[name: string, version: string]>,
): Promise<InstallRecord["packages"]> {
	const lock = JSON.parse(await readFile(join(stagingDirectory, "package-lock.json"), "utf8")) as {
		packages?: Record<string, { version?: string; integrity?: string }>;
	};
	const recorded: InstallRecord["packages"] = {};
	for (const [lockPath, entry] of Object.entries(lock.packages ?? {})) {
		const name = packageNameFromLockPath(lockPath);
		const pinned = name ? CLAUDE_AGENT_SDK_PIN.packages[name] : undefined;
		if (!pinned || typeof entry.version !== "string") {
			continue;
		}
		recorded[lockPath] = { version: entry.version, integrity: entry.integrity ?? null };
		if (entry.version === pinned.version && entry.integrity !== pinned.integrity) {
			throw new Error(
				`${name}@${entry.version} arrived with integrity ${entry.integrity ?? "(none)"}, ` +
					`but the board's lockfile pins ${pinned.integrity}.`,
			);
		}
	}
	for (const [name, version] of requested) {
		const locked = recorded[`node_modules/${name}`];
		const onDisk = await readInstalledPackageVersion(stagingDirectory, name);
		if (locked?.version !== version || onDisk !== version) {
			throw new Error(`npm did not install ${name}@${version} (found ${onDisk ?? "nothing"}).`);
		}
	}
	return recorded;
}

async function writeInstallRecord(root: string, record: InstallRecord): Promise<void> {
	const target = getClaudeAgentSdkInstallRecordPath(root);
	const temporary = `${target}.${process.pid}.tmp`;
	await writeFile(temporary, `${JSON.stringify(record, null, 2)}\n`, { encoding: "utf8", mode: 0o600 });
	await rename(temporary, target);
}

async function performInstall(sdkVersion: string, noticeSha256: string): Promise<InstallOutcome> {
	let staging: string | null = null;
	try {
		const root = getClaudeAgentSdkInstallRoot();
		const installs = getClaudeAgentSdkInstallsDirectory(root);
		await mkdir(installs, { recursive: true, mode: 0o700 });
		staging = await mkdtemp(join(installs, STAGING_PREFIX));
		const requested = requestedPackages(sdkVersion);
		const manifest = {
			name: "kanban-claude-agent-sdk",
			private: true,
			description:
				"Installed by the Kanban board at the user's acknowledged request. Not part of the board and not distributed with it.",
			dependencies: Object.fromEntries(requested),
		};
		await writeFile(join(staging, "package.json"), `${JSON.stringify(manifest, null, 2)}\n`, "utf8");
		await runNpmInstall(staging);
		const packages = await verifyStagedInstall(staging, requested);
		const providerVersion = Object.fromEntries(requested)[CLAUDE_CODE_PROVIDER_PACKAGE] ?? "unknown";
		await rm(join(staging, ".npm-cache"), { recursive: true, force: true });
		await writeClaudeAgentSdkLoader(staging);

		const installedAt = new Date().toISOString();
		const directory = `${requested.map(([, version]) => version).join("_")}_${installedAt.replace(/[^0-9]/g, "")}`;
		const installDirectory = join(installs, directory);
		await rename(staging, installDirectory);
		staging = null;

		let load: Awaited<ReturnType<typeof activateClaudeAgentSdkInstall>>;
		try {
			load = await activateClaudeAgentSdkInstall(installDirectory);
		} catch (error) {
			await rm(installDirectory, { recursive: true, force: true });
			throw error;
		}
		await writeInstallRecord(root, {
			schema: INSTALL_RECORD_SCHEMA,
			directory,
			sdkVersion,
			providerVersion,
			pinnedVersion: requested.every(([name, version]) => pinnedPackage(name).version === version),
			packages,
			notice: { sha256: noticeSha256, acknowledgedAt: installedAt },
			installedAt,
		});
		// The new install is in place, loaded and recorded: remove the superseded ones
		// and any staging leftovers. This runs before the install is marked finished, so
		// no other install's staging directory can exist yet.
		await removeSupersededClaudeAgentSdkInstalls(root, directory);
		markClaudeAgentSdkInstallFinished({ load });
		return { ok: true, code: "installed", error: null };
	} catch (error) {
		if (staging) {
			await rm(staging, { recursive: true, force: true }).catch(() => undefined);
		}
		const message = errorMessage(error);
		markClaudeAgentSdkInstallFinished({ error: message });
		return { ok: false, code: "install_failed", error: message };
	}
}

async function respond(outcome: InstallOutcome): Promise<RuntimeClaudeAgentSdkInstallResponse> {
	return { ...outcome, status: await getClaudeAgentSdkStatus() };
}

/**
 * The board action. Fetches nothing and writes nothing unless the request carries
 * the SHA-256 of the pinned notice the board shows: the user's acknowledgement.
 */
export async function installClaudeAgentSdk(
	request: RuntimeClaudeAgentSdkInstallRequest,
): Promise<RuntimeClaudeAgentSdkInstallResponse> {
	if (!isClaudeAgentSdkNoticePinned()) {
		return await respond({
			ok: false,
			code: "notice_not_pinned",
			error: "The Claude Agent SDK notice does not match its pinned digest; the board refuses to install.",
		});
	}
	const acknowledged = request.acknowledgedNoticeSha256?.trim().toLowerCase() ?? "";
	if (acknowledged !== CLAUDE_AGENT_SDK_NOTICE_SHA256) {
		return await respond({
			ok: false,
			code: "notice_not_acknowledged",
			error:
				"Nothing was installed: acknowledge the Claude Agent SDK notice first " +
				"(acknowledgedNoticeSha256 must be the SHA-256 of the notice the board shows).",
		});
	}
	const sdkVersion = request.sdkVersion?.trim() || CLAUDE_AGENT_SDK_PINNED_VERSION;
	if (!EXACT_VERSION.test(sdkVersion)) {
		return await respond({
			ok: false,
			code: "invalid_version",
			error: `"${sdkVersion}" is not an exact version (for example ${CLAUDE_AGENT_SDK_PINNED_VERSION}).`,
		});
	}
	if (isClaudeAgentSdkInstallRunning()) {
		return await respond({
			ok: false,
			code: "install_in_progress",
			error: "An install of the Claude Agent SDK is already running.",
		});
	}
	markClaudeAgentSdkInstallStarted();
	return await respond(await performInstall(sdkVersion, acknowledged));
}
