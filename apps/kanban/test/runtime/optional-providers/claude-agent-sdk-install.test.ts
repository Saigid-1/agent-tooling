// Unit tests for the Claude Agent SDK install path (src/optional-providers). They
// run a stand-in npm CLI that writes small fake packages, so nothing is fetched and
// no Anthropic code runs; the image probes exercise the real packages.
import {
	existsSync,
	mkdirSync,
	mkdtempSync,
	readdirSync,
	readFileSync,
	realpathSync,
	rmSync,
	symlinkSync,
	writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import {
	CLAUDE_AGENT_SDK_PINNED_VERSION,
	getClaudeAgentSdkInstallRoot,
	getClaudeAgentSdkInstallsDirectory,
	getClaudeAgentSdkStatus,
	initializeClaudeAgentSdkAtStartup,
	resetClaudeAgentSdkStateForTests,
} from "../../../src/optional-providers/claude-agent-sdk";
import {
	CLAUDE_AGENT_SDK_NPM_CLI_ENV,
	installClaudeAgentSdk,
} from "../../../src/optional-providers/claude-agent-sdk-install";
import {
	CLAUDE_AGENT_SDK_NOTICE_SHA256,
	computeClaudeAgentSdkNoticeSha256,
} from "../../../src/optional-providers/claude-agent-sdk-notice";
import pin from "../../../src/optional-providers/claude-agent-sdk-pin.json" with { type: "json" };
import {
	ClaudeAgentSdkNotInstalledError,
	getLoadedClaudeCodeProviderModule,
} from "../../../src/optional-providers/claude-code-provider-registry";
import { createClaudeCode } from "../../../src/optional-providers/claude-code-provider-shim";

const APP_ROOT = fileURLToPath(new URL("../../../", import.meta.url));
const SDK = pin.sdk;
const PROVIDER = pin.provider;

const FAKE_NPM = `
import { appendFileSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
const args = process.argv.slice(2);
appendFileSync(process.env.FAKE_NPM_LOG, JSON.stringify(args) + "\\n");
const prefix = args[args.indexOf("--prefix") + 1];
const deps = JSON.parse(readFileSync(join(prefix, "package.json"), "utf8")).dependencies;
const integrity = JSON.parse(process.env.FAKE_NPM_INTEGRITY);
const write = (path, content) => { mkdirSync(join(path, ".."), { recursive: true }); writeFileSync(path, content); };
const sdkDir = join(prefix, "node_modules", ...${JSON.stringify(SDK)}.split("/"));
write(join(sdkDir, "package.json"), JSON.stringify({ name: ${JSON.stringify(SDK)}, version: deps[${JSON.stringify(SDK)}], type: "module", exports: { ".": { types: "./entry.d.ts", default: "./entry.mjs" } } }));
write(join(sdkDir, "entry.mjs"), "export function query() { throw new Error('no model call in tests'); }\\n");
const providerDir = join(prefix, "node_modules", ${JSON.stringify(PROVIDER)});
write(join(providerDir, "package.json"), JSON.stringify({ name: ${JSON.stringify(PROVIDER)}, version: deps[${JSON.stringify(PROVIDER)}], type: "module", exports: { ".": { import: "./dist/index.js", require: "./dist/index.cjs" } } }));
write(join(providerDir, "dist", "index.js"), [
  "import { query } from " + JSON.stringify(${JSON.stringify(SDK)}) + ";",
  "export function createClaudeCode(options = {}) {",
  "  const create = (modelId) => ({ specificationVersion: 'v3', provider: 'claude-code', modelId, options, query });",
  "  const provider = (modelId) => create(modelId);",
  "  provider.languageModel = create;",
  "  return provider;",
  "}",
].join("\\n"));
const packages = { "": { dependencies: deps } };
for (const name of [${JSON.stringify(SDK)}, ${JSON.stringify(PROVIDER)}]) {
  packages["node_modules/" + name] = { version: deps[name], integrity: integrity[name] ?? "sha512-unpinned" };
}
writeFileSync(join(prefix, "package-lock.json"), JSON.stringify({ lockfileVersion: 3, packages }));
`;

const savedEnv = {
	root: process.env.KANBAN_STORAGE_ROOT,
	npm: process.env[CLAUDE_AGENT_SDK_NPM_CLI_ENV],
	log: process.env.FAKE_NPM_LOG,
	integrity: process.env.FAKE_NPM_INTEGRITY,
};

let workDir: string;
let npmLog: string;

function restore(name: string, value: string | undefined): void {
	if (value === undefined) {
		delete process.env[name];
	} else {
		process.env[name] = value;
	}
}

function pinnedIntegrity(): Record<string, string> {
	return {
		[SDK]: pin.packages[SDK as keyof typeof pin.packages].integrity,
		[PROVIDER]: pin.packages[PROVIDER as keyof typeof pin.packages].integrity,
	};
}

function npmRuns(): string[][] {
	if (!existsSync(npmLog)) {
		return [];
	}
	return readFileSync(npmLog, "utf8")
		.trim()
		.split("\n")
		.map((line) => JSON.parse(line) as string[]);
}

beforeEach(() => {
	workDir = realpathSync(mkdtempSync(join(tmpdir(), "kanban-claude-sdk-")));
	const storageRoot = join(workDir, "storage");
	writeFileSync(join(workDir, "fake-npm.mjs"), FAKE_NPM);
	mkdirSync(storageRoot);
	npmLog = join(workDir, "npm-runs.log");
	process.env.KANBAN_STORAGE_ROOT = storageRoot;
	process.env[CLAUDE_AGENT_SDK_NPM_CLI_ENV] = join(workDir, "fake-npm.mjs");
	process.env.FAKE_NPM_LOG = npmLog;
	process.env.FAKE_NPM_INTEGRITY = JSON.stringify(pinnedIntegrity());
	resetClaudeAgentSdkStateForTests();
});

afterEach(() => {
	resetClaudeAgentSdkStateForTests();
	restore("KANBAN_STORAGE_ROOT", savedEnv.root);
	restore(CLAUDE_AGENT_SDK_NPM_CLI_ENV, savedEnv.npm);
	restore("FAKE_NPM_LOG", savedEnv.log);
	restore("FAKE_NPM_INTEGRITY", savedEnv.integrity);
	rmSync(workDir, { recursive: true, force: true });
});

describe("Claude Agent SDK pins", () => {
	it("pins the notice text by its SHA-256", () => {
		expect(computeClaudeAgentSdkNoticeSha256()).toBe(CLAUDE_AGENT_SDK_NOTICE_SHA256);
	});

	it("pins the same versions and integrity as package-lock.json", () => {
		const lock = JSON.parse(readFileSync(join(APP_ROOT, "package-lock.json"), "utf8")) as {
			packages: Record<string, { version: string; integrity: string }>;
		};
		for (const [name, entry] of Object.entries(pin.packages)) {
			const locked = lock.packages[`node_modules/${name}`];
			expect({ name, version: locked?.version, integrity: locked?.integrity }).toEqual({ name, ...entry });
		}
		expect(pin.packages[SDK as keyof typeof pin.packages].version).toBe(CLAUDE_AGENT_SDK_PINNED_VERSION);
	});

	it("lists the packages the install action fetches: the SDK at the user's version, then its adapter", () => {
		expect(pin.install).toEqual([{ name: SDK, userVersion: true }, { name: PROVIDER }]);
		for (const entry of pin.install) {
			expect(pin.packages[entry.name as keyof typeof pin.packages]).toBeDefined();
		}
	});
});

describe("Claude Agent SDK install action", () => {
	it("reports not installed, and refuses only the Claude Code provider, without the SDK", async () => {
		const status = await getClaudeAgentSdkStatus();
		expect(status.state).toBe("not_installed");
		expect(status.provider.registered).toBe(false);
		expect(status.notice.sha256).toBe(CLAUDE_AGENT_SDK_NOTICE_SHA256);
		expect(status.installAction.procedure).toBe("runtime.installClaudeAgentSdk");
		expect(() => createClaudeCode()).toThrow(ClaudeAgentSdkNotInstalledError);
	});

	it("fetches and writes nothing without the acknowledged notice digest", async () => {
		for (const request of [{}, { acknowledgedNoticeSha256: null }, { acknowledgedNoticeSha256: "0".repeat(64) }]) {
			const response = await installClaudeAgentSdk(request);
			expect(response).toMatchObject({ ok: false, code: "notice_not_acknowledged" });
			expect(response.status.state).toBe("not_installed");
		}
		expect(npmRuns()).toEqual([]);
		expect(existsSync(getClaudeAgentSdkInstallRoot())).toBe(false);
	});

	it("refuses a version that is not exact, before running npm", async () => {
		const response = await installClaudeAgentSdk({
			acknowledgedNoticeSha256: CLAUDE_AGENT_SDK_NOTICE_SHA256,
			sdkVersion: "latest",
		});
		expect(response).toMatchObject({ ok: false, code: "invalid_version" });
		expect(npmRuns()).toEqual([]);
	});

	it("installs the pinned versions under the runtime home and registers the provider", async () => {
		const response = await installClaudeAgentSdk({ acknowledgedNoticeSha256: CLAUDE_AGENT_SDK_NOTICE_SHA256 });
		expect(response.error).toBeNull();
		expect(response).toMatchObject({ ok: true, code: "installed" });
		const root = getClaudeAgentSdkInstallRoot();
		expect(root).toBe(join(workDir, "storage", "kanban", "optional-packages", "claude-agent-sdk"));
		expect(response.status).toMatchObject({
			state: "installed",
			installRoot: root,
			installed: { sdkVersion: CLAUDE_AGENT_SDK_PINNED_VERSION, pinnedVersion: true },
			provider: { registered: true, sdkEntryReached: true },
		});
		expect(response.status.provider.sdkEntry?.startsWith(root)).toBe(true);

		const runs = npmRuns();
		expect(runs).toHaveLength(1);
		expect(runs[0]).toEqual(expect.arrayContaining(["install", "--ignore-scripts", "--prefix"]));
		expect(runs[0]?.[runs[0].indexOf("--prefix") + 1]?.startsWith(root)).toBe(true);
		expect(createClaudeCode()("sonnet")).toMatchObject({ provider: "claude-code", modelId: "sonnet" });
	});

	it("installs the user's chosen SDK version", async () => {
		const response = await installClaudeAgentSdk({
			acknowledgedNoticeSha256: CLAUDE_AGENT_SDK_NOTICE_SHA256,
			sdkVersion: "0.2.999",
		});
		expect(response).toMatchObject({ ok: true, code: "installed" });
		expect(response.status.installed).toMatchObject({ sdkVersion: "0.2.999", pinnedVersion: false });
		expect(response.status.provider.sdkVersion).toBe("0.2.999");
	});

	it("installs nothing when a pinned package arrives with another integrity", async () => {
		process.env.FAKE_NPM_INTEGRITY = JSON.stringify({ ...pinnedIntegrity(), [SDK]: "sha512-tampered" });
		const response = await installClaudeAgentSdk({ acknowledgedNoticeSha256: CLAUDE_AGENT_SDK_NOTICE_SHA256 });
		expect(response).toMatchObject({ ok: false, code: "install_failed" });
		expect(response.error).toMatch(/integrity/);
		expect(response.status.state).toBe("not_installed");
		expect(existsSync(join(getClaudeAgentSdkInstallRoot(), "install.json"))).toBe(false);
		expect(() => createClaudeCode()).toThrow(ClaudeAgentSdkNotInstalledError);
	});

	it("survives a board restart, and a restart clears what an interrupted install left", async () => {
		const response = await installClaudeAgentSdk({ acknowledgedNoticeSha256: CLAUDE_AGENT_SDK_NOTICE_SHA256 });
		expect(response).toMatchObject({ ok: true, code: "installed" });
		const leftover = join(getClaudeAgentSdkInstallRoot(), "installs", ".staging-interrupted");
		mkdirSync(leftover);

		resetClaudeAgentSdkStateForTests();
		expect(() => createClaudeCode()).toThrow(ClaudeAgentSdkNotInstalledError);
		const afterRestart = await initializeClaudeAgentSdkAtStartup();
		expect(afterRestart).toMatchObject({ registered: true, sdkEntryReached: true, error: null });
		expect((await getClaudeAgentSdkStatus()).state).toBe("installed");
		expect(existsSync(leftover)).toBe(false);
		expect(npmRuns()).toHaveLength(1);
	});

	it("a second install at another version leaves one install directory: the current one, which install.json names", async () => {
		const first = await installClaudeAgentSdk({ acknowledgedNoticeSha256: CLAUDE_AGENT_SDK_NOTICE_SHA256 });
		expect(first).toMatchObject({ ok: true, code: "installed" });
		const installs = getClaudeAgentSdkInstallsDirectory();
		const [firstDirectory] = readdirSync(installs);
		const firstModule = getLoadedClaudeCodeProviderModule();
		mkdirSync(join(installs, ".staging-leftover"));

		const second = await installClaudeAgentSdk({
			acknowledgedNoticeSha256: CLAUDE_AGENT_SDK_NOTICE_SHA256,
			sdkVersion: "0.2.999",
		});
		expect(second).toMatchObject({ ok: true, code: "installed" });
		const record = JSON.parse(readFileSync(join(getClaudeAgentSdkInstallRoot(), "install.json"), "utf8")) as {
			directory: string;
			sdkVersion: string;
		};
		expect(record.sdkVersion).toBe("0.2.999");
		expect(record.directory).not.toBe(firstDirectory);
		expect(readdirSync(installs)).toEqual([record.directory]);

		// The provider reaches the new install's entry point, and the board did not restart:
		// the module loaded from the removed directory stays loaded in this process.
		const current = join(installs, record.directory);
		expect(second.status.installed?.directory).toBe(current);
		expect(second.status.provider).toMatchObject({ registered: true, sdkEntryReached: true, sdkVersion: "0.2.999" });
		expect(second.status.provider.sdkEntry?.startsWith(`${current}/`)).toBe(true);
		expect(createClaudeCode()("sonnet")).toMatchObject({ provider: "claude-code", modelId: "sonnet" });
		expect(firstModule?.createClaudeCode()("sonnet")).toMatchObject({ provider: "claude-code", modelId: "sonnet" });
		const status = await getClaudeAgentSdkStatus();
		expect(status).toMatchObject({
			state: "installed",
			installed: { sdkVersion: "0.2.999", directory: current, pinnedVersion: false },
			provider: {
				registered: true,
				sdkEntryReached: true,
				sdkVersion: "0.2.999",
				modelConstructed: true,
				error: null,
			},
			lastInstallError: null,
		});
		expect(npmRuns()).toHaveLength(2);
	});

	/**
	 * A failed install removes nothing: the previous install stays installed and usable.
	 * Its directory, install.json and the status are unchanged, the provider stays
	 * registered, and only the failed attempt's own staging directory is removed (an
	 * earlier staging leftover stays until the next start). Two failures: npm exiting
	 * non-zero, and a pinned package arriving with another integrity.
	 */
	it.each([
		["npm exits non-zero", "exit"],
		["a pinned package arrives with another integrity", "integrity"],
	])("a failed install (%s) removes nothing but its own staging directory", async (_label, failure) => {
		const first = await installClaudeAgentSdk({ acknowledgedNoticeSha256: CLAUDE_AGENT_SDK_NOTICE_SHA256 });
		expect(first).toMatchObject({ ok: true, code: "installed" });
		const installs = getClaudeAgentSdkInstallsDirectory();
		const recordPath = join(getClaudeAgentSdkInstallRoot(), "install.json");
		mkdirSync(join(installs, ".staging-earlier"));
		const entriesBefore = readdirSync(installs).sort();
		const recordBefore = readFileSync(recordPath, "utf8");
		const { lastInstallError: _before, ...statusBefore } = await getClaudeAgentSdkStatus();
		expect(statusBefore).toMatchObject({ state: "installed", provider: { registered: true } });

		if (failure === "exit") {
			const failingNpm = join(workDir, "failing-npm.mjs");
			writeFileSync(
				failingNpm,
				[
					'import { appendFileSync, writeFileSync } from "node:fs";',
					'import { join } from "node:path";',
					"const args = process.argv.slice(2);",
					'appendFileSync(process.env.FAKE_NPM_LOG, JSON.stringify(args) + "\\n");',
					'writeFileSync(join(args[args.indexOf("--prefix") + 1], "partial"), "partial\\n");',
					'process.stderr.write("npm ERR! simulated failure\\n");',
					"process.exit(1);",
				].join("\n"),
			);
			process.env[CLAUDE_AGENT_SDK_NPM_CLI_ENV] = failingNpm;
		} else {
			process.env.FAKE_NPM_INTEGRITY = JSON.stringify({ ...pinnedIntegrity(), [SDK]: "sha512-tampered" });
		}
		const failed = await installClaudeAgentSdk({
			acknowledgedNoticeSha256: CLAUDE_AGENT_SDK_NOTICE_SHA256,
			sdkVersion: failure === "exit" ? "0.2.999" : undefined,
		});
		expect(failed).toMatchObject({ ok: false, code: "install_failed" });
		const runs = npmRuns();
		expect(runs).toHaveLength(2);
		const failedStaging = runs[1]?.[runs[1].indexOf("--prefix") + 1];
		expect(failedStaging?.startsWith(`${installs}/.staging-`)).toBe(true);
		expect(existsSync(failedStaging as string)).toBe(false);
		expect(readdirSync(installs).sort()).toEqual(entriesBefore);
		expect(readFileSync(recordPath, "utf8")).toBe(recordBefore);
		const { lastInstallError, ...statusAfter } = await getClaudeAgentSdkStatus();
		expect(statusAfter).toEqual(statusBefore);
		expect(lastInstallError).toBe(failed.error);
		expect(createClaudeCode()("sonnet")).toMatchObject({ provider: "claude-code", modelId: "sonnet" });
	});

	it("removes a symlinked entry in installs/ as a link, never its target", async () => {
		const outside = join(workDir, "outside");
		mkdirSync(join(outside, "kept"), { recursive: true });
		writeFileSync(join(outside, "kept", "file"), "outside the install root\n");
		const first = await installClaudeAgentSdk({ acknowledgedNoticeSha256: CLAUDE_AGENT_SDK_NOTICE_SHA256 });
		expect(first).toMatchObject({ ok: true, code: "installed" });
		const installs = getClaudeAgentSdkInstallsDirectory();
		symlinkSync(join(outside, "kept"), join(installs, "linked-entry"));

		const second = await installClaudeAgentSdk({
			acknowledgedNoticeSha256: CLAUDE_AGENT_SDK_NOTICE_SHA256,
			sdkVersion: "0.2.999",
		});
		expect(second).toMatchObject({ ok: true, code: "installed" });
		const record = JSON.parse(readFileSync(join(getClaudeAgentSdkInstallRoot(), "install.json"), "utf8")) as {
			directory: string;
		};
		expect(readdirSync(installs)).toEqual([record.directory]);
		expect(readFileSync(join(outside, "kept", "file"), "utf8")).toBe("outside the install root\n");
	});

	it("start removes nothing through an installs/ directory that is a symlink", async () => {
		const first = await installClaudeAgentSdk({ acknowledgedNoticeSha256: CLAUDE_AGENT_SDK_NOTICE_SHA256 });
		expect(first).toMatchObject({ ok: true, code: "installed" });
		const installs = getClaudeAgentSdkInstallsDirectory();
		const elsewhere = join(workDir, "elsewhere");
		mkdirSync(join(elsewhere, "not-an-install"), { recursive: true });
		writeFileSync(join(elsewhere, "not-an-install", "file"), "kept\n");
		rmSync(installs, { recursive: true, force: true });
		symlinkSync(elsewhere, installs);

		resetClaudeAgentSdkStateForTests();
		await initializeClaudeAgentSdkAtStartup();
		expect(readFileSync(join(elsewhere, "not-an-install", "file"), "utf8")).toBe("kept\n");
	});

	it("reaches the SDK entry point and constructs the provider's model without a credential", async () => {
		const saved = { anthropic: process.env.ANTHROPIC_API_KEY, oauth: process.env.CLAUDE_CODE_OAUTH_TOKEN };
		delete process.env.ANTHROPIC_API_KEY;
		delete process.env.CLAUDE_CODE_OAUTH_TOKEN;
		try {
			const response = await installClaudeAgentSdk({ acknowledgedNoticeSha256: CLAUDE_AGENT_SDK_NOTICE_SHA256 });
			const root = getClaudeAgentSdkInstallRoot();
			expect(response.status.provider).toMatchObject({
				registered: true,
				sdkEntryReached: true,
				modelConstructed: true,
				modelSpecificationVersion: "v3",
				error: null,
			});
			expect(response.status.provider.sdkEntry).toMatch(/entry\.mjs$/);
			expect(response.status.provider.sdkEntry?.startsWith(root)).toBe(true);
			expect(createClaudeCode()("sonnet")).toMatchObject({
				provider: "claude-code",
				modelId: "sonnet",
				specificationVersion: "v3",
			});
		} finally {
			restore("ANTHROPIC_API_KEY", saved.anthropic);
			restore("CLAUDE_CODE_OAUTH_TOKEN", saved.oauth);
		}
	});
});
