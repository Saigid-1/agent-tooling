import { readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import * as esbuild from "esbuild";

const appDirectory = resolve(dirname(fileURLToPath(import.meta.url)), "..");

/**
 * Runtime externals. `node-pty` is a native addon with a compiled binding
 * and a spawn-helper binary that must live on disk, so it can't be bundled.
 * The Claude Agent SDK packages below are external too (claudeAgentSdkExternalPlugin).
 * Everything else esbuild can inline.
 */
const external = ["node-pty"];
const outputDirectory = process.env.KANBAN_BUILD_OUTDIR || "dist";

/**
 * Never bundled: @anthropic-ai/claude-agent-sdk (Anthropic's terms, not
 * redistributable with the board) and ai-sdk-provider-claude-code, the provider
 * adapter that imports it. The user installs both through the board's install
 * action (src/optional-providers/claude-agent-sdk.ts). The bundled Cline SDK's
 * import of the adapter binds to the board's loader shim, which hands it the
 * user's install at run time; the SDK and its platform packages are external.
 */
const CLAUDE_CODE_PROVIDER_SHIM = join(appDirectory, "src/optional-providers/claude-code-provider-shim.ts");
const CLAUDE_AGENT_SDK_PIN = join(appDirectory, "src/optional-providers/claude-agent-sdk-pin.json");
const claudeAgentSdkPin = JSON.parse(readFileSync(CLAUDE_AGENT_SDK_PIN, "utf8"));
/** The install action's package list (the pin's `install`): none of them may be bundled. */
const CLAUDE_AGENT_SDK_EXTERNALS = claudeAgentSdkPin.install.map((entry) => entry.name);

const claudeAgentSdkExternalPlugin = {
	name: "claude-agent-sdk-external",
	setup(build) {
		build.onResolve({ filter: /^ai-sdk-provider-claude-code(?:\/.*)?$/ }, (args) => {
			if (args.path !== "ai-sdk-provider-claude-code") {
				return { errors: [{ text: `${args.path}: only the package root binds to the board's loader shim` }] };
			}
			return { path: CLAUDE_CODE_PROVIDER_SHIM };
		});
		build.onResolve({ filter: /^@anthropic-ai\/claude-agent-sdk(?:-[a-z0-9-]+)?(?:\/.*)?$/ }, (args) => ({
			path: args.path,
			external: true,
		}));
	},
};

/**
 * The pinned versions must equal the lockfile's, entry for entry: every listed
 * package and its platform packages (`<name>-<platform>`).
 */
function assertClaudeAgentSdkPinMatchesLockfile() {
	const pin = claudeAgentSdkPin;
	const lock = JSON.parse(readFileSync(join(appDirectory, "package-lock.json"), "utf8"));
	const listed = (name) =>
		CLAUDE_AGENT_SDK_EXTERNALS.some(
			(external) => name === external || (name.startsWith(`${external}-`) && /^[a-z0-9-]+$/.test(name.slice(external.length + 1))),
		);
	const locked = Object.entries(lock.packages ?? {})
		.filter(([path]) => path.startsWith("node_modules/") && listed(path.slice("node_modules/".length)))
		.map(([path, entry]) => [path.slice("node_modules/".length), { version: entry.version, integrity: entry.integrity }]);
	const expected = JSON.stringify(Object.fromEntries(locked.sort(([a], [b]) => a.localeCompare(b))));
	const pinned = JSON.stringify(
		Object.fromEntries(Object.entries(pin.packages).sort(([a], [b]) => a.localeCompare(b))),
	);
	if (expected !== pinned) {
		throw new Error(`${CLAUDE_AGENT_SDK_PIN} differs from package-lock.json:\n lock ${expected}\n pin  ${pinned}`);
	}
}

/** Fails the build if any Claude Agent SDK code was bundled or is imported statically. */
function assertClaudeAgentSdkNotBundled(metafile, label) {
	const problems = [];
	for (const input of Object.keys(metafile.inputs)) {
		if (CLAUDE_AGENT_SDK_EXTERNALS.some((name) => input.includes(`node_modules/${name}`))) {
			problems.push(`bundled input ${input}`);
		}
	}
	for (const [output, meta] of Object.entries(metafile.outputs)) {
		for (const imported of meta.imports ?? []) {
			const isSdk = CLAUDE_AGENT_SDK_EXTERNALS.some(
				(name) => imported.path === name || imported.path.startsWith(`${name}/`) || imported.path.startsWith(`${name}-`),
			);
			if (isSdk && imported.kind !== "dynamic-import") {
				problems.push(`${output} imports ${imported.path} statically (${imported.kind}); the board would not start without it`);
			}
		}
	}
	if (problems.length > 0) {
		throw new Error(`${label}: the Claude Agent SDK must never be bundled:\n  ${problems.join("\n  ")}`);
	}
}

/** Local trial build: never bake inherited OTEL exporter settings into artifacts. */
const define = {
	"process.env.NODE_ENV": '"production"',
	"process.env.OTEL_TELEMETRY_ENABLED": '"false"',
	"process.env.OTEL_EXPORTER_OTLP_ENDPOINT": '""',
	"process.env.OTEL_METRICS_EXPORTER": '""',
	"process.env.OTEL_LOGS_EXPORTER": '""',
	"process.env.OTEL_EXPORTER_OTLP_PROTOCOL": '""',
	"process.env.OTEL_METRIC_EXPORT_INTERVAL": '""',
	"process.env.OTEL_EXPORTER_OTLP_HEADERS": '""',
};

/**
 * Bundled CJS dependencies call require() on Node built-ins (process, fs, etc.).
 * ESM output needs a real require() function for those calls to work.
 */
const cjsShimBanner = [
	'import { createRequire as __kanban_createRequire } from "node:module";',
	"const require = __kanban_createRequire(import.meta.url);",
].join("\n");

/** Shared esbuild options for both entry points. */
const shared = {
	bundle: true,
	format: "esm",
	platform: "node",
	target: "node20",
	external,
	define,
	sourcemap: true,
	packages: "bundle",
	banner: { js: cjsShimBanner },
	plugins: [claudeAgentSdkExternalPlugin],
	metafile: true,
};

assertClaudeAgentSdkPinMatchesLockfile();

const [cliResult, indexResult] = await Promise.all([
	// CLI binary
	esbuild.build({
		...shared,
		entryPoints: ["src/cli.ts"],
		outfile: `${outputDirectory}/cli.js`,
		banner: { js: `#!/usr/bin/env node\n${cjsShimBanner}` },
	}),
	// Library export
	esbuild.build({
		...shared,
		entryPoints: ["src/index.ts"],
		outfile: `${outputDirectory}/index.js`,
	}),
]);

assertClaudeAgentSdkNotBundled(cliResult.metafile, "dist/cli.js");
assertClaudeAgentSdkNotBundled(indexResult.metafile, "dist/index.js");

console.log("esbuild: bundled dist/cli.js and dist/index.js (Claude Agent SDK external, not bundled)");
