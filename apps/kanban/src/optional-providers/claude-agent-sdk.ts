// The Claude Agent SDK as an optional component the user installs: where it
// lives, whether the board has it, and loading it.
//
// The board never bundles @anthropic-ai/claude-agent-sdk or the provider adapter
// that imports it (ai-sdk-provider-claude-code); scripts/build.mjs keeps both out
// of dist/. Without them the board serves normally and the Claude Code provider
// reports "not installed". The user's install action
// (claude-agent-sdk-install.ts) writes them into the board's state directory:
//
//   <runtime home>/optional-packages/claude-agent-sdk/
//     install.json                       the current install (written last, atomically)
//     installs/<sdk>_<provider>_<time>/   package.json, package-lock.json,
//                                        node_modules/, kanban-loader.mjs
//
// In the container image the runtime home is /state/kanban/kanban, on the /state
// bind mount, so the install survives a board restart and never touches the
// read-only image. The board loads the provider from there when it starts and
// right after an install, without restarting. Loading never fetches anything.

import { existsSync } from "node:fs";
import { lstat, readdir, readFile, rm, writeFile } from "node:fs/promises";
import { join, relative, sep } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

import type { RuntimeClaudeAgentSdkState, RuntimeClaudeAgentSdkStatus } from "../core/api-contract";
import { getRuntimeHomePath } from "../state/workspace-state";
import {
	CLAUDE_AGENT_SDK_NOTICE_TEXT,
	CLAUDE_AGENT_SDK_NOTICE_TITLE,
	computeClaudeAgentSdkNoticeSha256,
} from "./claude-agent-sdk-notice";
import pinFile from "./claude-agent-sdk-pin.json" with { type: "json" };
import {
	CLAUDE_AGENT_SDK_INSTALL_ACTION_LABEL,
	CLAUDE_AGENT_SDK_INSTALL_PROCEDURE,
	CLAUDE_CODE_PROVIDER_ID,
	type ClaudeCodeProviderModule,
	getLoadedClaudeCodeProviderModule,
	setLoadedClaudeCodeProviderModule,
} from "./claude-code-provider-registry";
import { createClaudeCode } from "./claude-code-provider-shim";

export interface PinnedPackage {
	version: string;
	integrity: string;
}

/** One package the install action fetches. */
export interface InstallPackage {
	name: string;
	/** The one entry the user may choose a version for; every other entry installs at its pinned version. */
	userVersion?: boolean;
}

interface PinFile {
	schema: string;
	sdk: string;
	provider: string;
	install: InstallPackage[];
	packages: Record<string, PinnedPackage>;
}

export const CLAUDE_AGENT_SDK_PIN = pinFile as PinFile;
export const CLAUDE_AGENT_SDK_PACKAGE = CLAUDE_AGENT_SDK_PIN.sdk;
export const CLAUDE_CODE_PROVIDER_PACKAGE = CLAUDE_AGENT_SDK_PIN.provider;

/**
 * The package list the install action fetches: claude-agent-sdk-pin.json `install`.
 * Today the SDK (the user's version) and its provider adapter; a later item can add
 * packages there without changing the action, its notice flow or the install path.
 */
export const CLAUDE_AGENT_SDK_INSTALL_PACKAGES: readonly InstallPackage[] = CLAUDE_AGENT_SDK_PIN.install;

export function pinnedPackage(name: string): PinnedPackage {
	const entry = CLAUDE_AGENT_SDK_PIN.packages[name];
	if (!entry) {
		throw new Error(`claude-agent-sdk-pin.json does not pin ${name}.`);
	}
	return entry;
}

/** The versions apps/kanban/package-lock.json pins; the install's default. */
export const CLAUDE_AGENT_SDK_PINNED_VERSION = pinnedPackage(CLAUDE_AGENT_SDK_PACKAGE).version;
export const CLAUDE_CODE_PROVIDER_PINNED_VERSION = pinnedPackage(CLAUDE_CODE_PROVIDER_PACKAGE).version;

const INSTALL_RECORD_FILE = "install.json";
export const INSTALL_RECORD_SCHEMA = "kanban.claude-agent-sdk-install.v1";
const INSTALLS_DIR = "installs";
const LOADER_FILE = "kanban-loader.mjs";
const PROBE_MODEL_ID = "sonnet";
const INSTALL_DIRECTORY_NAME = /^[0-9A-Za-z][0-9A-Za-z._+-]*$/;

const LOADER_SOURCE = [
	"// Written by the Kanban board's Claude Agent SDK install action. It loads the",
	"// packages the user installed here through Node's own module resolution.",
	`export * as providerModule from ${JSON.stringify(CLAUDE_CODE_PROVIDER_PACKAGE)};`,
	`export * as sdkModule from ${JSON.stringify(CLAUDE_AGENT_SDK_PACKAGE)};`,
	`export const sdkEntry = import.meta.resolve(${JSON.stringify(CLAUDE_AGENT_SDK_PACKAGE)});`,
	"",
].join("\n");

export interface InstallRecord {
	schema: typeof INSTALL_RECORD_SCHEMA;
	directory: string;
	sdkVersion: string;
	providerVersion: string;
	pinnedVersion: boolean;
	packages: Record<string, { version: string; integrity: string | null }>;
	notice: { sha256: string; acknowledgedAt: string };
	installedAt: string;
}

export interface ProviderLoad {
	registered: boolean;
	sdkEntryReached: boolean;
	sdkEntry: string | null;
	sdkVersion: string | null;
	providerVersion: string | null;
	modelConstructed: boolean;
	modelSpecificationVersion: string | null;
	error: string | null;
}

const NOT_LOADED: ProviderLoad = {
	registered: false,
	sdkEntryReached: false,
	sdkEntry: null,
	sdkVersion: null,
	providerVersion: null,
	modelConstructed: false,
	modelSpecificationVersion: null,
	error: null,
};

let loadAttempt: Promise<ProviderLoad> | null = null;
let currentLoad: ProviderLoad = NOT_LOADED;
let installing = false;
let lastInstallError: string | null = null;

// ── Paths and the install record ────────────────────────────────────────

/** Where the install action writes: <runtime home>/optional-packages/claude-agent-sdk. */
export function getClaudeAgentSdkInstallRoot(): string {
	return join(getRuntimeHomePath(), "optional-packages", "claude-agent-sdk");
}

export function getClaudeAgentSdkInstallsDirectory(root: string = getClaudeAgentSdkInstallRoot()): string {
	return join(root, INSTALLS_DIR);
}

export function getClaudeAgentSdkInstallRecordPath(root: string = getClaudeAgentSdkInstallRoot()): string {
	return join(root, INSTALL_RECORD_FILE);
}

function isInside(parent: string, child: string): boolean {
	const relation = relative(parent, child);
	return relation.length > 0 && !relation.startsWith("..") && !relation.startsWith(sep);
}

export function errorMessage(error: unknown): string {
	return error instanceof Error ? error.message : String(error);
}

export async function readInstallRecord(root: string = getClaudeAgentSdkInstallRoot()): Promise<InstallRecord | null> {
	let raw: string;
	try {
		raw = await readFile(getClaudeAgentSdkInstallRecordPath(root), "utf8");
	} catch {
		return null;
	}
	const parsed = JSON.parse(raw) as Partial<InstallRecord>;
	if (
		parsed.schema !== INSTALL_RECORD_SCHEMA ||
		typeof parsed.directory !== "string" ||
		!INSTALL_DIRECTORY_NAME.test(parsed.directory) ||
		typeof parsed.sdkVersion !== "string" ||
		typeof parsed.providerVersion !== "string"
	) {
		throw new Error(`${getClaudeAgentSdkInstallRecordPath(root)} is not a Claude Agent SDK install record.`);
	}
	return parsed as InstallRecord;
}

// ── Loading ─────────────────────────────────────────────────────────────

async function readPackageVersion(packageDirectory: string): Promise<string | null> {
	try {
		const manifest = JSON.parse(await readFile(join(packageDirectory, "package.json"), "utf8")) as {
			version?: unknown;
		};
		return typeof manifest.version === "string" ? manifest.version : null;
	} catch {
		return null;
	}
}

export function packageDirectory(installDirectory: string, name: string): string {
	return join(installDirectory, "node_modules", ...name.split("/"));
}

export async function readInstalledPackageVersion(installDirectory: string, name: string): Promise<string | null> {
	return await readPackageVersion(packageDirectory(installDirectory, name));
}

/** The install action writes this next to node_modules; the board imports the packages through it. */
export async function writeClaudeAgentSdkLoader(installDirectory: string): Promise<void> {
	await writeFile(join(installDirectory, LOADER_FILE), LOADER_SOURCE, "utf8");
}

interface ImportedInstall {
	providerModule: ClaudeCodeProviderModule;
	load: ProviderLoad;
}

/** Imports an install and checks that the provider adapter reaches the SDK's entry point. */
async function importInstall(installDirectory: string): Promise<ImportedInstall> {
	const providerDirectory = packageDirectory(installDirectory, CLAUDE_CODE_PROVIDER_PACKAGE);
	const nestedSdk = packageDirectory(providerDirectory, CLAUDE_AGENT_SDK_PACKAGE);
	if (existsSync(nestedSdk)) {
		const nestedVersion = await readPackageVersion(nestedSdk);
		throw new Error(
			`${CLAUDE_CODE_PROVIDER_PACKAGE} resolves its own copy of ${CLAUDE_AGENT_SDK_PACKAGE} ` +
				`(${nestedVersion ?? "unknown version"}), not the installed one; ` +
				"choose an SDK version within the provider's supported range.",
		);
	}
	const loader = (await import(pathToFileURL(join(installDirectory, LOADER_FILE)).href)) as {
		providerModule: Partial<ClaudeCodeProviderModule>;
		sdkModule: Record<string, unknown>;
		sdkEntry: string;
	};
	const sdkEntry = fileURLToPath(loader.sdkEntry);
	if (!isInside(installDirectory, sdkEntry)) {
		throw new Error(`${CLAUDE_AGENT_SDK_PACKAGE} resolved outside the install directory: ${sdkEntry}`);
	}
	if (typeof loader.sdkModule.query !== "function") {
		throw new Error(`${CLAUDE_AGENT_SDK_PACKAGE} at ${sdkEntry} does not export query().`);
	}
	if (typeof loader.providerModule.createClaudeCode !== "function") {
		throw new Error(`${CLAUDE_CODE_PROVIDER_PACKAGE} does not export createClaudeCode().`);
	}
	return {
		providerModule: loader.providerModule as ClaudeCodeProviderModule,
		load: {
			...NOT_LOADED,
			sdkEntryReached: true,
			sdkEntry,
			sdkVersion: await readPackageVersion(packageDirectory(installDirectory, CLAUDE_AGENT_SDK_PACKAGE)),
			providerVersion: await readPackageVersion(providerDirectory),
		},
	};
}

/**
 * Registers the provider module, then constructs a model through the same
 * createClaudeCode() the bundled Cline SDK calls (the build's loader shim).
 * Construction needs no credential and sends no request. On failure the
 * previously registered provider stays.
 */
function registerAndProbe(imported: ImportedInstall): ProviderLoad {
	const previous = getLoadedClaudeCodeProviderModule();
	setLoadedClaudeCodeProviderModule(imported.providerModule);
	try {
		const model = createClaudeCode()(PROBE_MODEL_ID);
		const specificationVersion = typeof model.specificationVersion === "string" ? model.specificationVersion : null;
		if (model.provider !== CLAUDE_CODE_PROVIDER_ID || model.modelId !== PROBE_MODEL_ID || !specificationVersion) {
			throw new Error(`${CLAUDE_CODE_PROVIDER_PACKAGE} constructed an unexpected model for "${PROBE_MODEL_ID}".`);
		}
		return {
			...imported.load,
			registered: true,
			modelConstructed: true,
			modelSpecificationVersion: specificationVersion,
		};
	} catch (error) {
		setLoadedClaudeCodeProviderModule(previous);
		throw error;
	}
}

/** Imports, registers and probes an install directory. Throws, keeping the previous provider, on failure. */
export async function activateClaudeAgentSdkInstall(installDirectory: string): Promise<ProviderLoad> {
	return registerAndProbe(await importInstall(installDirectory));
}

/**
 * Loads the current install, if any. Never throws and never fetches: without an
 * install it reports not installed; a broken install reports its error.
 */
export async function loadInstalledClaudeAgentSdk(): Promise<ProviderLoad> {
	const attempt = (async (): Promise<ProviderLoad> => {
		try {
			const root = getClaudeAgentSdkInstallRoot();
			const record = await readInstallRecord(root);
			if (!record) {
				setLoadedClaudeCodeProviderModule(null);
				return NOT_LOADED;
			}
			return await activateClaudeAgentSdkInstall(join(getClaudeAgentSdkInstallsDirectory(root), record.directory));
		} catch (error) {
			setLoadedClaudeCodeProviderModule(null);
			return { ...NOT_LOADED, error: errorMessage(error) };
		}
	})();
	loadAttempt = attempt;
	currentLoad = await attempt;
	return currentLoad;
}

async function ensureLoadAttempted(): Promise<void> {
	if (loadAttempt) {
		await loadAttempt;
		return;
	}
	await loadInstalledClaudeAgentSdk();
}

/**
 * Removes every entry of <root>/installs except `current`: installs that are no
 * longer current and what an interrupted install left (.staging-*). Board start
 * calls it before loading, and the install action right after it records and
 * loads a new install. It touches only the entries of installs/: a symlinked
 * installs/ is not entered, and a symlinked entry is removed as a link, never
 * followed. A module already imported from a removed directory stays loaded in
 * the running process. Best effort: never throws.
 */
export async function removeSupersededClaudeAgentSdkInstalls(root: string, current: string | null): Promise<void> {
	const installs = getClaudeAgentSdkInstallsDirectory(root);
	try {
		if (!(await lstat(installs)).isDirectory()) {
			return;
		}
		for (const name of await readdir(installs)) {
			const entry = join(installs, name);
			if (name === current || !isInside(installs, entry)) {
				continue;
			}
			await rm(entry, { recursive: true, force: true }).catch(() => undefined);
		}
	} catch {
		// Cleanup is best effort; loading reports any real problem.
	}
}

/**
 * Board start: removes what an interrupted install left and installs that are no
 * longer current, then loads the current install. Never throws, never fetches.
 */
export async function initializeClaudeAgentSdkAtStartup(): Promise<ProviderLoad> {
	try {
		const root = getClaudeAgentSdkInstallRoot();
		const record = await readInstallRecord(root).catch(() => null);
		await removeSupersededClaudeAgentSdkInstalls(root, record?.directory ?? null);
	} catch {
		// Cleanup is best effort; loading reports any real problem.
	}
	return await loadInstalledClaudeAgentSdk();
}

// ── Install state (set by the install action) ───────────────────────────

export function markClaudeAgentSdkInstallStarted(): void {
	installing = true;
}

export function markClaudeAgentSdkInstallFinished(result: { load: ProviderLoad } | { error: string }): void {
	installing = false;
	if ("load" in result) {
		currentLoad = result.load;
		loadAttempt = Promise.resolve(result.load);
		lastInstallError = null;
	} else {
		lastInstallError = result.error;
	}
}

export function isClaudeAgentSdkInstallRunning(): boolean {
	return installing;
}

// ── Status ──────────────────────────────────────────────────────────────

function isRegistered(): boolean {
	return currentLoad.registered && getLoadedClaudeCodeProviderModule() !== null;
}

function resolveState(hasRecord: boolean): RuntimeClaudeAgentSdkState {
	if (installing) {
		return "installing";
	}
	if (isRegistered()) {
		return "installed";
	}
	return hasRecord || currentLoad.error ? "unavailable" : "not_installed";
}

/** The provider's install state, for the provider catalog. */
export async function getClaudeCodeProviderInstallState(): Promise<RuntimeClaudeAgentSdkState> {
	await ensureLoadAttempted();
	return resolveState(false);
}

export async function getClaudeAgentSdkStatus(): Promise<RuntimeClaudeAgentSdkStatus> {
	await ensureLoadAttempted();
	const root = getClaudeAgentSdkInstallRoot();
	let record: InstallRecord | null = null;
	let recordError: string | null = null;
	try {
		record = await readInstallRecord(root);
	} catch (error) {
		recordError = errorMessage(error);
	}
	return {
		providerId: CLAUDE_CODE_PROVIDER_ID,
		sdkPackage: CLAUDE_AGENT_SDK_PACKAGE,
		providerPackage: CLAUDE_CODE_PROVIDER_PACKAGE,
		state: resolveState(record !== null || recordError !== null),
		installRoot: root,
		pinned: {
			sdkVersion: CLAUDE_AGENT_SDK_PINNED_VERSION,
			providerVersion: CLAUDE_CODE_PROVIDER_PINNED_VERSION,
		},
		installed: record
			? {
					sdkVersion: record.sdkVersion,
					providerVersion: record.providerVersion,
					directory: join(getClaudeAgentSdkInstallsDirectory(root), record.directory),
					installedAt: record.installedAt,
					pinnedVersion: record.pinnedVersion,
				}
			: null,
		provider: {
			registered: isRegistered(),
			sdkEntryReached: currentLoad.sdkEntryReached,
			sdkEntry: currentLoad.sdkEntry,
			sdkVersion: currentLoad.sdkVersion,
			modelConstructed: currentLoad.modelConstructed,
			modelSpecificationVersion: currentLoad.modelSpecificationVersion,
			error: currentLoad.error ?? recordError,
		},
		notice: {
			title: CLAUDE_AGENT_SDK_NOTICE_TITLE,
			text: CLAUDE_AGENT_SDK_NOTICE_TEXT,
			sha256: computeClaudeAgentSdkNoticeSha256(),
		},
		installAction: {
			procedure: CLAUDE_AGENT_SDK_INSTALL_PROCEDURE,
			label: CLAUDE_AGENT_SDK_INSTALL_ACTION_LABEL,
			requiresAcknowledgedNoticeSha256: true,
		},
		lastInstallError,
	};
}

/** Test seam: forget in-memory state (the install on disk is untouched). */
export function resetClaudeAgentSdkStateForTests(): void {
	loadAttempt = null;
	currentLoad = NOT_LOADED;
	installing = false;
	lastInstallError = null;
	setLoadedClaudeCodeProviderModule(null);
}
