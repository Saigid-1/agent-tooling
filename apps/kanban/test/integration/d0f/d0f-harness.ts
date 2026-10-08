// Harness for the D0f Kanban tests (order docs/work/orders/D0f-board-without-agent-sdk.md).
//
// The board under test is the shipped bundle: scripts/build.mjs builds dist/cli.js into
// a scratch `app/dist`, beside `app/node_modules/node-pty` only, which is the layout the
// product image ships (/app/dist and /app/node_modules/node-pty). Nothing else from
// node_modules is reachable from the bundle, so the SDK is absent unless the bundle
// carries it or the install action puts it under the state root.
//
// Isolation. Every board runs with an environment built from scratch: HOME, TMPDIR and
// the storage root inside a scratch state root; PATH holding only node, npm and git;
// ANTHROPIC_BASE_URL at a closed port; no inherited variable, so no credential. Every
// network peer is local: a stand-in npm registry that records each request. HTTP(S)
// proxies point at a closed port, with loopback exempt, so npm (and Node's own fetch, where
// NODE_USE_ENV_PROXY is honoured) cannot reach any other host.
//
// What the registry serves (reconciled at the meet). The install action checks every
// package it fetches at a version the board's lockfile pins against the lockfile's
// integrity, so at those versions (the SDK's pin and the provider's pin) the registry
// serves the published tarball itself, byte for byte, read from npm's content-addressed
// cache by that integrity (npm ci fills it). Every other SDK version (the older one a user
// may choose, the newer `latest`) is a stand-in with no Anthropic code that makes no model
// call. The SDK's platform packages (its native binary) are not served: npm skips them as
// optional. Which process imported the installed SDK's entry module, and which file at
// which version, is recorded by a module load hook preloaded into the board (NODE_OPTIONS
// --import), the same for the published SDK and the stand-in.

import { type ChildProcess, spawn, spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import {
	copyFileSync,
	existsSync,
	mkdirSync,
	mkdtempSync,
	readdirSync,
	readFileSync,
	realpathSync,
	rmSync,
	statSync,
	symlinkSync,
	writeFileSync,
} from "node:fs";
import { createServer, type IncomingMessage, type Server, type ServerResponse } from "node:http";
import { createRequire } from "node:module";
import { tmpdir } from "node:os";
import { delimiter, dirname, join, relative, resolve, sep } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { gunzipSync, gzipSync } from "node:zlib";

import { type ClaudeSdkStatus, D0F_SEAMS, installInput, parseClaudeSdkStatus } from "./d0f-seams";

export const KANBAN_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "../../..");
export const REPO_ROOT = resolve(KANBAN_ROOT, "../..");
export const SDK_PACKAGE = "@anthropic-ai/claude-agent-sdk";
export const PROVIDER_PACKAGE = "ai-sdk-provider-claude-code";
export const CLAUDE_PROVIDER_ID = "claude-code";
/** The per-image scanner; its MARKERS are the one list both suites use. */
export const SCANNER_FILE = join(REPO_ROOT, "tests/image/agent_sdk_absence.py");
export const SDK_SOURCE_FRAGMENT = `${SDK_PACKAGE}/`;

// --------------------------------------------------------------------------------------
// Scratch

export interface Scratch {
	path: string;
	cleanup: () => void;
}

/** A physical scratch directory (the board refuses roots reached through a symlink). */
export function createScratch(prefix: string): Scratch {
	const path = realpathSync(mkdtempSync(join(tmpdir(), prefix)));
	return { path, cleanup: () => rmSync(path, { recursive: true, force: true, maxRetries: 15, retryDelay: 300 }) };
}

// --------------------------------------------------------------------------------------
// Pins and markers

/** apps/kanban/package-lock.json's entry for a top-level node_modules package, or null. */
function lockfileEntry(name: string): { version?: string; integrity?: string } | null {
	const lock = JSON.parse(readFileSync(join(KANBAN_ROOT, "package-lock.json"), "utf8")) as {
		packages?: Record<string, { version?: string; integrity?: string }>;
	};
	return lock.packages?.[`node_modules/${name}`] ?? null;
}

/** The version apps/kanban/package-lock.json pins for a top-level node_modules package, or null. */
export function lockfilePin(name: string): string | null {
	return lockfileEntry(name)?.version ?? null;
}

/** The MARKERS tuple of tests/image/agent_sdk_absence.py, read from its source. */
export function scannerMarkers(): string[] {
	const source = readFileSync(SCANNER_FILE, "utf8");
	const block = source.match(/^MARKERS[^=]*=\s*\(([\s\S]*?)^\)/m);
	if (!block?.[1]) {
		throw new Error(`no MARKERS tuple in ${SCANNER_FILE}`);
	}
	const markers = [...block[1].matchAll(/b"((?:[^"\\]|\\.)*)"/g)].map((match) => match[1] ?? "");
	if (markers.length === 0) {
		throw new Error(`MARKERS in ${SCANNER_FILE} lists no byte strings`);
	}
	return markers;
}

// --------------------------------------------------------------------------------------
// The board bundle

export interface BoardBundle {
	root: string;
	appDir: string;
	cliPath: string;
	buildLog: string;
}

/** Builds the board bundle with the repository's own build script into `<root>/app/dist`. */
export function buildBoardBundle(root: string): BoardBundle {
	const appDir = join(root, "app");
	const distDir = join(appDir, "dist");
	mkdirSync(distDir, { recursive: true });
	const result = spawnSync(process.execPath, [D0F_SEAMS.buildScript], {
		cwd: KANBAN_ROOT,
		env: { ...process.env, KANBAN_BUILD_OUTDIR: distDir },
		encoding: "utf8",
		timeout: 240_000,
	});
	const buildLog = `${result.stdout ?? ""}${result.stderr ?? ""}`;
	if (result.status !== 0) {
		throw new Error(`${D0F_SEAMS.buildScript} exited ${String(result.status)}:\n${buildLog.slice(-4000)}`);
	}
	copyFileSync(join(KANBAN_ROOT, "package.json"), join(appDir, "package.json"));
	mkdirSync(join(appDir, "node_modules"), { recursive: true });
	symlinkSync(join(KANBAN_ROOT, "node_modules/node-pty"), join(appDir, "node_modules/node-pty"), "dir");
	// The web UI is not under test; one page lets GET / answer as the shipped board does.
	mkdirSync(join(distDir, "web-ui/assets"), { recursive: true });
	writeFileSync(join(distDir, "web-ui/index.html"), "<!doctype html><title>Kanban</title><div id=root></div>\n");
	writeFileSync(join(distDir, "web-ui/assets/app.js"), "export {};\n");
	return { root, appDir, cliPath: join(distDir, "cli.js"), buildLog };
}

export interface SourceMapSummary {
	sources: string[];
	sdkSources: string[];
}

export function readSourceMap(path: string): SourceMapSummary {
	const map = JSON.parse(readFileSync(path, "utf8")) as { sources?: unknown };
	const sources = Array.isArray(map.sources) ? map.sources.filter((s): s is string => typeof s === "string") : [];
	return { sources, sdkSources: sources.filter((source) => source.includes(SDK_SOURCE_FRAGMENT)) };
}

/** Each emitted JavaScript file and the markers it contains. */
export function markersInBundle(distDir: string, markers: string[]): Record<string, string[]> {
	const found: Record<string, string[]> = {};
	for (const name of readdirSync(distDir)) {
		const path = join(distDir, name);
		if (!statSync(path).isFile()) continue;
		const content = readFileSync(path, "utf8");
		const hits = markers.filter((marker) => content.includes(marker));
		if (hits.length > 0) found[name] = hits;
	}
	return found;
}

/**
 * Why the SDK is not absent for this bundle, or [] when it is: the bundle carries no SDK
 * source or marker, and neither package resolves from the bundle's directory.
 */
export function sdkAbsenceProblems(bundle: BoardBundle): string[] {
	const problems: string[] = [];
	const distDir = dirname(bundle.cliPath);
	for (const name of readdirSync(distDir).filter((entry) => entry.endsWith(".map"))) {
		const { sdkSources } = readSourceMap(join(distDir, name));
		if (sdkSources.length > 0) problems.push(`${name} lists ${sdkSources.join(", ")}`);
	}
	for (const [name, hits] of Object.entries(markersInBundle(distDir, scannerMarkers()))) {
		if (name.endsWith(".js")) problems.push(`${name} contains SDK code (${hits.join(", ")})`);
	}
	const requireFromBundle = createRequire(bundle.cliPath);
	for (const name of [SDK_PACKAGE, PROVIDER_PACKAGE]) {
		try {
			problems.push(`${name} resolves from the bundle: ${requireFromBundle.resolve(name)}`);
		} catch {
			// Not resolvable: absent, as in the image.
		}
	}
	return problems;
}

// --------------------------------------------------------------------------------------
// A minimal tar writer, for the stand-in registry's tarballs

function tarHeader(name: string, size: number, type: string): Buffer {
	const header = Buffer.alloc(512, 0);
	const put = (value: string, offset: number, length: number) => {
		header.write(value.slice(0, length), offset, length, "utf8");
	};
	const octal = (value: number, length: number) => `${value.toString(8).padStart(length - 1, "0")}\0`;
	put(name, 0, 100);
	put(octal(type === "5" ? 0o755 : 0o644, 8), 100, 8);
	put(octal(0, 8), 108, 8);
	put(octal(0, 8), 116, 8);
	put(octal(size, 12), 124, 12);
	put(octal(0, 12), 136, 12);
	put("        ", 148, 8);
	put(type, 156, 1);
	put("ustar\0", 257, 6);
	put("00", 263, 2);
	let sum = 0;
	for (const byte of header) sum += byte;
	put(`${sum.toString(8).padStart(6, "0")}\0 `, 148, 8);
	return header;
}

function padded(data: Buffer): Buffer {
	const rest = data.length % 512;
	return rest === 0 ? data : Buffer.concat([data, Buffer.alloc(512 - rest, 0)]);
}

/** A gzipped tar of `files` (path -> content), every path under `package/`, as npm packs. */
export function npmTarball(files: Map<string, Buffer>): Buffer {
	const blocks: Buffer[] = [];
	for (const [path, content] of files) {
		const name = `package/${path}`;
		if (Buffer.byteLength(name) > 100) {
			const record = (length: number) => `${length} path=${name}\n`;
			let length = Buffer.byteLength(record(0));
			while (Buffer.byteLength(record(length)) !== length) length = Buffer.byteLength(record(length));
			const pax = Buffer.from(record(length));
			blocks.push(tarHeader("PaxHeader", pax.length, "x"), padded(pax));
		}
		blocks.push(tarHeader(name, content.length, "0"), padded(content));
	}
	blocks.push(Buffer.alloc(1024, 0));
	return gzipSync(Buffer.concat(blocks));
}

function filesOf(directory: string, base = directory, files = new Map<string, Buffer>()): Map<string, Buffer> {
	for (const entry of readdirSync(directory, { withFileTypes: true })) {
		const path = join(directory, entry.name);
		if (entry.isDirectory()) {
			if (entry.name !== "node_modules") filesOf(path, base, files);
		} else if (entry.isFile()) {
			files.set(relative(base, path).split(sep).join("/"), readFileSync(path));
		}
	}
	return files;
}

/** One file of a gzipped npm tarball (`package/<path>`), or null. */
export function tarballFile(tarball: Buffer, path: string): Buffer | null {
	const data = gunzipSync(tarball);
	let offset = 0;
	let longName: string | null = null;
	while (offset + 512 <= data.length) {
		const header = data.subarray(offset, offset + 512);
		if (header.every((byte) => byte === 0)) break;
		const field = (start: number, length: number) =>
			header
				.subarray(start, start + length)
				.toString("utf8")
				.replace(/\0.*$/s, "");
		const size = Number.parseInt(field(124, 12).trim() || "0", 8);
		const type = field(156, 1);
		const prefix = field(345, 155);
		const body = data.subarray(offset + 512, offset + 512 + size);
		const name = longName ?? (prefix ? `${prefix}/${field(0, 100)}` : field(0, 100));
		longName = null;
		if (type === "x") {
			longName = body.toString("utf8").match(/\d+ path=([^\n]*)\n/)?.[1] ?? null;
		} else if ((type === "0" || type === "") && name.replace(/^[^/]+\//, "") === path) {
			return Buffer.from(body);
		}
		offset += 512 + Math.ceil(size / 512) * 512;
	}
	return null;
}

let npmCache: string | null = null;

/** npm's cache directory: `npm config get cache` (it honours npm_config_cache). */
function npmCacheDirectory(): string {
	if (npmCache) return npmCache;
	const npm = which("npm");
	const result = npm ? spawnSync(npm, ["config", "get", "cache"], { encoding: "utf8", timeout: 60_000 }) : null;
	const directory = result?.status === 0 ? result.stdout.trim() : "";
	if (!directory)
		throw new Error(`cannot read npm's cache directory (npm config get cache): ${result?.stderr ?? "no npm"}`);
	npmCache = directory;
	return directory;
}

export interface PublishedPackage {
	version: string;
	integrity: string;
	manifest: Record<string, unknown>;
	tarball: Buffer;
}

const published = new Map<string, PublishedPackage>();

/**
 * The published tarball of a package at the version the lockfile pins, byte for byte: read
 * from npm's content-addressed cache (`_cacache/content-v2`) by the lockfile's integrity,
 * and checked against it. npm ci fills the cache; nothing is fetched here.
 */
export function publishedPackage(name: string): PublishedPackage {
	const cached = published.get(name);
	if (cached) return cached;
	const entry = lockfileEntry(name);
	const integrity = entry?.integrity ?? "";
	const [algorithm, digest] = integrity.split("-", 2);
	if (!entry?.version || algorithm !== "sha512" || !digest) {
		throw new Error(`apps/kanban/package-lock.json pins no sha512 integrity for ${name}`);
	}
	const hex = Buffer.from(digest, "base64").toString("hex");
	const path = join(
		npmCacheDirectory(),
		"_cacache",
		"content-v2",
		"sha512",
		hex.slice(0, 2),
		hex.slice(2, 4),
		hex.slice(4),
	);
	if (!existsSync(path)) {
		throw new Error(
			`the published tarball of ${name}@${entry.version} (${integrity}) is not in npm's cache at ${path}; ` +
				`run npm ci in apps/kanban, or npm cache add ${name}@${entry.version}`,
		);
	}
	const tarball = readFileSync(path);
	if (createHash("sha512").update(tarball).digest("hex") !== hex) {
		throw new Error(`npm's cache holds other bytes for ${name}@${entry.version} at ${path}`);
	}
	const manifestBytes = tarballFile(tarball, "package.json");
	if (!manifestBytes) throw new Error(`the published tarball of ${name}@${entry.version} has no package.json`);
	const manifest = JSON.parse(manifestBytes.toString("utf8")) as Record<string, unknown>;
	if (manifest.version !== entry.version) {
		throw new Error(`the published tarball of ${name}@${entry.version} declares version ${String(manifest.version)}`);
	}
	const found = { version: entry.version, integrity, manifest, tarball };
	published.set(name, found);
	return found;
}

// --------------------------------------------------------------------------------------
// The stand-in SDK

/**
 * A stand-in @anthropic-ai/claude-agent-sdk: the entry points ai-sdk-provider-claude-code
 * imports (query, tool, createSdkMcpServer). It appends one JSON line per event to
 * `$D0F_STUB_SDK_MARKER_DIR/events.jsonl`: `evaluated` when the module runs, `query` when
 * the provider calls its entry point. query() makes no model call; iterating it throws.
 * The `import` event is the load hook's (sdkImportRecorderSource), for every SDK alike.
 */
export function standInSdkFiles(version: string): Map<string, Buffer> {
	const manifest = {
		name: SDK_PACKAGE,
		version,
		description: "D0f test stand-in for the Claude Agent SDK; no Anthropic code.",
		type: "module",
		main: "sdk.mjs",
		exports: { ".": "./sdk.mjs", "./package.json": "./package.json" },
		license: "UNLICENSED",
	};
	const sdk = `import { appendFileSync } from "node:fs";
import { join } from "node:path";
const VERSION = ${JSON.stringify(version)};
function record(event, detail = {}) {
	const dir = process.env.D0F_STUB_SDK_MARKER_DIR;
	if (!dir) return;
	appendFileSync(join(dir, "events.jsonl"), JSON.stringify({ event, version: VERSION, url: import.meta.url, pid: process.pid, ...detail }) + "\\n");
}
record("evaluated");
export const D0F_STAND_IN = true;
export function query(input) {
	record("query", { prompt: typeof input?.prompt, options: Object.keys(input?.options ?? {}).sort() });
	const iterator = (async function* () {
		throw new Error("d0f stand-in claude-agent-sdk: entry point reached; no model call is made");
	})();
	return Object.assign(iterator, {
		interrupt: async () => {},
		setPermissionMode: async () => {},
		setModel: async () => {},
		supportedCommands: async () => [],
		supportedModels: async () => [],
		mcpServerStatus: async () => [],
		close: () => {},
	});
}
export function tool(name, description, inputSchema, handler) {
	return { name, description, inputSchema, handler };
}
export function createSdkMcpServer(options) {
	record("createSdkMcpServer", { name: options?.name });
	return { type: "sdk", name: options?.name, instance: {} };
}
`;
	return new Map([
		["package.json", Buffer.from(`${JSON.stringify(manifest, null, 2)}\n`)],
		["sdk.mjs", Buffer.from(sdk)],
		["README.md", Buffer.from("D0f test stand-in. Not the Claude Agent SDK.\n")],
	]);
}

export interface SdkEvent {
	event: string;
	version: string;
	url: string;
	pid: number;
}

export function readSdkEvents(markerDir: string): SdkEvent[] {
	const path = join(markerDir, "events.jsonl");
	if (!existsSync(path)) return [];
	return readFileSync(path, "utf8")
		.split("\n")
		.filter((line) => line.trim().length > 0)
		.map((line) => JSON.parse(line) as SdkEvent);
}

// --------------------------------------------------------------------------------------
// The stand-in npm registry

export interface RecordedRequest {
	method: string;
	url: string;
	at: number;
}

interface RegistryVersion {
	manifest: Record<string, unknown>;
	tarball: Buffer;
}

export interface StandInRegistry {
	url: string;
	requests: RecordedRequest[];
	/** Requests for a package's metadata or tarball, by package name. */
	requestedPackages: () => string[];
	/** Tarball downloads as `name@version`. */
	downloads: () => string[];
	close: () => Promise<void>;
}

/** Every directory under node_modules that holds `name` (top level and nested). */
function localPackageDirs(name: string): string[] {
	const found: string[] = [];
	const visit = (modules: string, depth: number) => {
		const candidate = join(modules, name);
		if (existsSync(join(candidate, "package.json"))) found.push(candidate);
		if (depth > 3) return;
		for (const entry of readdirSync(modules, { withFileTypes: true })) {
			if (!entry.isDirectory()) continue;
			const scoped = entry.name.startsWith("@")
				? readdirSync(join(modules, entry.name), { withFileTypes: true })
						.filter((inner) => inner.isDirectory())
						.map((inner) => join(modules, entry.name, inner.name))
				: [join(modules, entry.name)];
			for (const packageDir of scoped) {
				const nested = join(packageDir, "node_modules");
				if (existsSync(nested)) visit(nested, depth + 1);
			}
		}
	};
	visit(join(KANBAN_ROOT, "node_modules"), 0);
	return found;
}

function localVersions(name: string): Map<string, RegistryVersion> {
	const versions = new Map<string, RegistryVersion>();
	for (const directory of localPackageDirs(name)) {
		const files = filesOf(directory);
		const manifest = JSON.parse((files.get("package.json") ?? Buffer.from("{}")).toString("utf8")) as Record<
			string,
			unknown
		>;
		const version = String(manifest.version);
		if (versions.has(version)) continue;
		// Served without lifecycle scripts or dev dependencies: an install runs nothing of ours.
		delete manifest.scripts;
		delete manifest.devDependencies;
		files.set("package.json", Buffer.from(`${JSON.stringify(manifest, null, 2)}\n`));
		versions.set(version, { manifest, tarball: npmTarball(files) });
	}
	return versions;
}

export interface StandInRegistryOptions {
	/** SDK versions to serve: the lockfile's pin as published, every other version as the stand-in. */
	sdkVersions: string[];
	/** The `latest` dist-tag for the SDK: newer than the pin, so "latest" is not "the pin". */
	sdkLatest: string;
}

function publishedVersion(name: string): RegistryVersion {
	const found = publishedPackage(name);
	return { manifest: found.manifest, tarball: found.tarball };
}

export async function startStandInRegistry(options: StandInRegistryOptions): Promise<StandInRegistry> {
	const requests: RecordedRequest[] = [];
	const packages = new Map<string, { versions: Map<string, RegistryVersion>; latest: string }>();
	const sdkPin = lockfilePin(SDK_PACKAGE);
	const sdkVersions = new Map<string, RegistryVersion>();
	for (const version of options.sdkVersions) {
		if (version === sdkPin) {
			sdkVersions.set(version, publishedVersion(SDK_PACKAGE));
			continue;
		}
		const files = standInSdkFiles(version);
		sdkVersions.set(version, {
			manifest: JSON.parse((files.get("package.json") as Buffer).toString("utf8")) as Record<string, unknown>,
			tarball: npmTarball(files),
		});
	}
	packages.set(SDK_PACKAGE, { versions: sdkVersions, latest: options.sdkLatest });
	// The provider adapter at its lockfile pin, as published (the action verifies its integrity).
	const provider = publishedPackage(PROVIDER_PACKAGE);
	packages.set(PROVIDER_PACKAGE, {
		versions: new Map([[provider.version, publishedVersion(PROVIDER_PACKAGE)]]),
		latest: provider.version,
	});

	const lookup = (name: string) => {
		let entry = packages.get(name);
		// Never serve any other Anthropic Claude package (the SDK's platform packages carry its
		// native binary): only the SDK and provider entries above.
		if (!entry && name.startsWith("@anthropic-ai/claude-")) return null;
		if (!entry) {
			const versions = localVersions(name);
			if (versions.size === 0) return null;
			entry = { versions, latest: [...versions.keys()][0] as string };
			packages.set(name, entry);
		}
		return entry;
	};

	let baseUrl = "";
	const server: Server = createServer((request: IncomingMessage, response: ServerResponse) => {
		const url = request.url ?? "/";
		requests.push({ method: request.method ?? "GET", url, at: Date.now() });
		const send = (status: number, body: Buffer | string, type: string) => {
			response.writeHead(status, { "Content-Type": type, "Cache-Control": "no-store" });
			response.end(body);
		};
		const tarballMatch = url.match(/^\/-\/d0f-tarball\/([^/]+)\/([^/]+)\.tgz$/);
		if (tarballMatch) {
			const name = decodeURIComponent(tarballMatch[1] ?? "");
			const version = decodeURIComponent(tarballMatch[2] ?? "");
			const tarball = lookup(name)?.versions.get(version)?.tarball;
			if (tarball) send(200, tarball, "application/octet-stream");
			else send(404, '{"error":"not found"}', "application/json");
			return;
		}
		const name = decodeURIComponent(url.split("?")[0]?.slice(1) ?? "");
		const entry = name.startsWith("-/") ? null : lookup(name);
		if (!entry) {
			send(404, '{"error":"not found"}', "application/json");
			return;
		}
		const versions: Record<string, unknown> = {};
		for (const [version, item] of entry.versions) {
			versions[version] = {
				...item.manifest,
				_id: `${name}@${version}`,
				dist: {
					tarball: `${baseUrl}/-/d0f-tarball/${encodeURIComponent(name)}/${encodeURIComponent(version)}.tgz`,
					shasum: createHash("sha1").update(item.tarball).digest("hex"),
					integrity: `sha512-${createHash("sha512").update(item.tarball).digest("base64")}`,
				},
			};
		}
		send(200, JSON.stringify({ name, "dist-tags": { latest: entry.latest }, versions }), "application/json");
	});
	await new Promise<void>((resolveListen) => server.listen(0, "127.0.0.1", () => resolveListen()));
	const address = server.address();
	baseUrl = `http://127.0.0.1:${typeof address === "object" && address ? address.port : 0}`;
	const packageOf = (url: string): string | null => {
		const tarball = url.match(/^\/-\/d0f-tarball\/([^/]+)\//);
		if (tarball) return decodeURIComponent(tarball[1] ?? "");
		const path = url.split("?")[0]?.slice(1) ?? "";
		return path && !path.startsWith("-/") ? decodeURIComponent(path) : null;
	};
	return {
		url: `${baseUrl}/`,
		requests,
		requestedPackages: () => [...new Set(requests.map((r) => packageOf(r.url)).filter((n): n is string => !!n))],
		downloads: () =>
			requests
				.map((r) => r.url.match(/^\/-\/d0f-tarball\/([^/]+)\/([^/]+)\.tgz$/))
				.filter((m): m is RegExpMatchArray => m !== null)
				.map((m) => `${decodeURIComponent(m[1] ?? "")}@${decodeURIComponent(m[2] ?? "")}`),
		close: () => new Promise<void>((resolveClose) => server.close(() => resolveClose())),
	};
}

// --------------------------------------------------------------------------------------
// The board process

function which(name: string): string | null {
	const besideNode = join(dirname(process.execPath), name);
	if (existsSync(besideNode)) return besideNode;
	for (const directory of (process.env.PATH ?? "").split(delimiter)) {
		const candidate = join(directory, name);
		if (directory && existsSync(candidate)) return candidate;
	}
	return null;
}

export interface StateRoot {
	root: string;
	stateDir: string;
	markerDir: string;
	binDir: string;
	/** The import recorder the board preloads (outside the state root). */
	recorder: string;
}

/**
 * A module load hook the board preloads (NODE_OPTIONS --import): when a process loads the
 * entry module of any installed @anthropic-ai/claude-agent-sdk (its package.json's
 * exports "." or main), it appends {event: "import", version, url, pid} to
 * `$D0F_STUB_SDK_MARKER_DIR/events.jsonl`. It reads; it changes nothing it loads.
 */
export function sdkImportRecorderSource(): string {
	return `import { appendFileSync, readFileSync } from "node:fs";
import { registerHooks } from "node:module";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
const PACKAGE = /^(.*[\\/]node_modules[\\/]@anthropic-ai[\\/]claude-agent-sdk)[\\/]/;
function entryOf(directory) {
	const manifest = JSON.parse(readFileSync(join(directory, "package.json"), "utf8"));
	let target = manifest.exports && typeof manifest.exports === "object" && "." in manifest.exports ? manifest.exports["."] : manifest.exports;
	while (target && typeof target === "object") target = target.import ?? target.node ?? target.default;
	return { file: join(directory, typeof target === "string" ? target : manifest.main ?? "index.js"), version: String(manifest.version) };
}
registerHooks({
	load(url, context, nextLoad) {
		const result = nextLoad(url, context);
		const markers = process.env.D0F_STUB_SDK_MARKER_DIR;
		if (markers && url.startsWith("file:")) {
			try {
				const path = fileURLToPath(url);
				const match = PACKAGE.exec(path);
				const entry = match ? entryOf(match[1]) : null;
				if (entry && entry.file === path) {
					appendFileSync(join(markers, "events.jsonl"), JSON.stringify({ event: "import", version: entry.version, url, pid: process.pid }) + "\\n");
				}
			} catch {}
		}
		return result;
	},
});
`;
}

/** A state root laid out as the image's /state: kanban and tmp exist, the rest is the board's. */
export function createStateRoot(root: string): StateRoot {
	const stateDir = join(root, "state");
	const markerDir = join(root, "sdk-events");
	const binDir = join(root, "bin");
	for (const directory of [stateDir, join(stateDir, "kanban"), join(stateDir, "tmp"), markerDir, binDir]) {
		mkdirSync(directory, { recursive: true });
	}
	for (const name of ["node", "npm", "git"]) {
		const target = name === "node" ? process.execPath : which(name);
		if (target) symlinkSync(target, join(binDir, name));
	}
	const recorder = join(root, "d0f-sdk-import-recorder.mjs");
	writeFileSync(recorder, sdkImportRecorderSource());
	return { root, stateDir, markerDir, binDir, recorder };
}

export interface BoardOptions {
	registry?: StandInRegistry | null;
	env?: Record<string, string>;
}

export interface RunningBoard {
	url: string;
	child: ChildProcess;
	output: () => string;
	stop: () => Promise<void>;
}

async function freePort(): Promise<number> {
	const server = createServer();
	await new Promise<void>((resolveListen) => server.listen(0, "127.0.0.1", () => resolveListen()));
	const address = server.address();
	const port = typeof address === "object" && address ? address.port : 0;
	await new Promise<void>((resolveClose) => server.close(() => resolveClose()));
	return port;
}

export function boardEnvironment(state: StateRoot, options: BoardOptions = {}): Record<string, string> {
	return {
		PATH: [state.binDir, "/usr/bin", "/bin"].join(delimiter),
		HOME: state.stateDir,
		TMPDIR: join(state.stateDir, "tmp"),
		LANG: "C.UTF-8",
		NODE_ENV: "production",
		KANBAN_STORAGE_ROOT: join(state.stateDir, "kanban"),
		KANBAN_NO_AUTO_UPDATE: "1",
		CLINE_SESSION_BACKEND_MODE: "local",
		// Belt and braces: nothing in these tests may reach a model service or the internet.
		ANTHROPIC_BASE_URL: "http://127.0.0.1:9",
		NODE_USE_ENV_PROXY: "1",
		HTTP_PROXY: "http://127.0.0.1:9",
		HTTPS_PROXY: "http://127.0.0.1:9",
		http_proxy: "http://127.0.0.1:9",
		https_proxy: "http://127.0.0.1:9",
		NO_PROXY: "127.0.0.1,localhost",
		no_proxy: "127.0.0.1,localhost",
		GIT_AUTHOR_NAME: "Test",
		GIT_AUTHOR_EMAIL: "test@example.invalid",
		GIT_COMMITTER_NAME: "Test",
		GIT_COMMITTER_EMAIL: "test@example.invalid",
		npm_config_update_notifier: "false",
		npm_config_audit: "false",
		npm_config_fund: "false",
		D0F_STUB_SDK_MARKER_DIR: state.markerDir,
		// The import recorder (a file URL: no space for NODE_OPTIONS to split on).
		NODE_OPTIONS: `--import=${pathToFileURL(state.recorder).href}`,
		...(options.registry ? { [D0F_SEAMS.registryEnv]: options.registry.url } : {}),
		...(options.env ?? {}),
	};
}

/** Starts `node <bundle>/cli.js` on loopback (no passcode) and waits until it says it runs. */
export async function startBoard(
	bundle: BoardBundle,
	state: StateRoot,
	options: BoardOptions = {},
): Promise<RunningBoard> {
	const port = await freePort();
	const child = spawn(process.execPath, [bundle.cliPath, "--host", "127.0.0.1", "--port", String(port), "--no-open"], {
		cwd: state.stateDir,
		env: boardEnvironment(state, options),
		stdio: ["ignore", "pipe", "pipe"],
	});
	let output = "";
	child.stdout?.on("data", (chunk: Buffer) => {
		output += chunk.toString();
	});
	child.stderr?.on("data", (chunk: Buffer) => {
		output += chunk.toString();
	});
	const url = await new Promise<string>((resolveStart, rejectStart) => {
		const timer = setTimeout(() => {
			rejectStart(new Error(`the board did not report it was running within 60s; output:\n${output.slice(-4000)}`));
		}, 60_000);
		const poll = setInterval(() => {
			const match = output.match(/Cline Kanban running at (http:\/\/127\.0\.0\.1:\d+)/);
			if (match?.[1]) {
				clearTimeout(timer);
				clearInterval(poll);
				resolveStart(match[1]);
			}
		}, 100);
		child.once("exit", (code, signal) => {
			clearTimeout(timer);
			clearInterval(poll);
			rejectStart(
				new Error(
					`the board exited before serving (code=${String(code)} signal=${String(signal)}):\n${output.slice(-4000)}`,
				),
			);
		});
	});
	const stop = async () => {
		if (child.exitCode !== null || child.signalCode !== null) return;
		const exited = new Promise<void>((resolveExit) => child.once("exit", () => resolveExit()));
		child.kill("SIGINT");
		const timeout = new Promise<"timeout">((resolveTimeout) => setTimeout(() => resolveTimeout("timeout"), 10_000));
		if ((await Promise.race([exited.then(() => "exited" as const), timeout])) === "timeout") {
			child.kill("SIGKILL");
			await exited;
		}
	};
	return { url, child, output: () => output, stop };
}

export function isRunning(board: RunningBoard): boolean {
	return board.child.exitCode === null && board.child.signalCode === null;
}

// --------------------------------------------------------------------------------------
// tRPC over HTTP

export interface TrpcResult {
	status: number;
	data?: unknown;
	error?: { message?: string; code?: string | number; data?: unknown };
	raw: string;
}

async function trpcCall(
	board: RunningBoard,
	method: "GET" | "POST",
	path: string,
	input: unknown,
	workspaceId?: string,
): Promise<TrpcResult> {
	const headers: Record<string, string> = {};
	if (workspaceId) headers["x-kanban-workspace-id"] = workspaceId;
	let target = `${board.url}/api/trpc/${path}`;
	let body: string | undefined;
	if (method === "GET") {
		if (input !== undefined) target += `?input=${encodeURIComponent(JSON.stringify(input))}`;
	} else {
		headers["Content-Type"] = "application/json";
		body = JSON.stringify(input ?? {});
	}
	const response = await fetch(target, { method, headers, body, signal: AbortSignal.timeout(180_000) });
	const raw = await response.text();
	let parsed: { result?: { data?: unknown }; error?: TrpcResult["error"] & { json?: unknown } } = {};
	try {
		parsed = JSON.parse(raw);
	} catch {
		// Not JSON: kept in raw.
	}
	return { status: response.status, data: parsed.result?.data, error: parsed.error, raw };
}

export function trpcQuery(board: RunningBoard, path: string, input?: unknown, workspaceId?: string) {
	return trpcCall(board, "GET", path, input, workspaceId);
}

export function trpcMutation(board: RunningBoard, path: string, input?: unknown, workspaceId?: string) {
	return trpcCall(board, "POST", path, input, workspaceId);
}

export function describeResult(result: TrpcResult): string {
	return `HTTP ${result.status} ${result.raw.slice(0, 600)}`;
}

// --------------------------------------------------------------------------------------
// Board functions used by the tests

export async function addWorkspace(board: RunningBoard, root: string): Promise<string> {
	const repo = join(root, "workspace");
	mkdirSync(repo, { recursive: true });
	const git = (...args: string[]) => {
		const result = spawnSync("git", args, {
			cwd: repo,
			encoding: "utf8",
			env: { PATH: process.env.PATH ?? "", HOME: root, GIT_CONFIG_NOSYSTEM: "1" },
		});
		if (result.status !== 0) throw new Error(`git ${args.join(" ")}: ${result.stderr}`);
	};
	git("init", "-q", "-b", "main");
	writeFileSync(join(repo, "README.md"), "d0f workspace\n");
	git("add", ".");
	git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "init");
	const added = await trpcMutation(board, "projects.add", { path: repo });
	const project = (added.data as { ok?: boolean; project?: { id?: string } } | undefined)?.project;
	if (!project?.id) throw new Error(`projects.add did not add the workspace: ${describeResult(added)}`);
	return project.id;
}

export async function saveProvider(board: RunningBoard, settings: Record<string, unknown>): Promise<TrpcResult> {
	return await trpcMutation(board, "runtime.saveClineProviderSettings", settings);
}

export async function waitFor<T>(probe: () => T | null | undefined | false, timeoutMs: number): Promise<T | null> {
	const deadline = Date.now() + timeoutMs;
	while (Date.now() < deadline) {
		const value = probe();
		if (value) return value;
		await new Promise((resolveSleep) => setTimeout(resolveSleep, 200));
	}
	return null;
}

export function sleep(ms: number): Promise<void> {
	return new Promise((resolveSleep) => setTimeout(resolveSleep, ms));
}

// --------------------------------------------------------------------------------------
// The installed SDK, wherever under the state root the action put it

export interface InstalledSdk {
	packageJson: string;
	version: string;
}

/** Every `node_modules/@anthropic-ai/claude-agent-sdk/package.json` under a directory. */
export function findInstalledSdk(directory: string): InstalledSdk[] {
	const found: InstalledSdk[] = [];
	const visit = (current: string, depth: number) => {
		if (depth > 12) return;
		let entries: import("node:fs").Dirent[];
		try {
			entries = readdirSync(current, { withFileTypes: true });
		} catch {
			return;
		}
		for (const entry of entries) {
			if (!entry.isDirectory() || entry.isSymbolicLink()) continue;
			const path = join(current, entry.name);
			const candidate = join(path, "package.json");
			if (path.split(sep).join("/").endsWith(dirname(D0F_SEAMS.installedPackageJson)) && existsSync(candidate)) {
				const manifest = JSON.parse(readFileSync(candidate, "utf8")) as { version?: string };
				found.push({ packageJson: candidate, version: String(manifest.version) });
			}
			visit(path, depth + 1);
		}
	};
	visit(directory, 0);
	return found;
}

/** Every path (relative) under a directory: a before/after listing for writes. */
export function listTree(directory: string): Set<string> {
	const paths = new Set<string>();
	const visit = (current: string) => {
		let entries: import("node:fs").Dirent[];
		try {
			entries = readdirSync(current, { withFileTypes: true });
		} catch {
			return;
		}
		for (const entry of entries) {
			const path = join(current, entry.name);
			paths.add(relative(directory, path));
			if (entry.isDirectory() && !entry.isSymbolicLink()) visit(path);
		}
	};
	visit(directory);
	return paths;
}

export function sha256(text: string | Buffer): string {
	return createHash("sha256").update(text).digest("hex");
}

// --------------------------------------------------------------------------------------
// One board world: a state root, the stand-in registry and a running board

/** Stand-in SDK versions: the pin, an older version a user may choose, and a newer `latest`. */
export function standInVersions(pin: string): { pin: string; older: string; latest: string } {
	const [major, minor, patch] = pin.split(".").map((part) => Number.parseInt(part, 10));
	if ([major, minor, patch].some((part) => part === undefined || Number.isNaN(part))) {
		throw new Error(`the lockfile pin ${pin} is not x.y.z`);
	}
	return {
		pin,
		// A user's choice that is not the pin: one patch below it (one above when the pin's patch is 0).
		older: `${major}.${minor}.${patch === 0 ? 1 : (patch as number) - 1}`,
		latest: `${major}.${minor}.${(patch as number) + 871}`,
	};
}

export interface World {
	root: string;
	state: StateRoot;
	registry: StandInRegistry;
	board: RunningBoard;
	versions: { pin: string; older: string; latest: string };
	/** Stops the board and starts it again on the same state root and registry. */
	restart: () => Promise<RunningBoard>;
	close: () => Promise<void>;
}

export async function openWorld(bundle: BoardBundle, scratchRoot: string, name: string): Promise<World> {
	const root = join(scratchRoot, name);
	mkdirSync(root, { recursive: true });
	const pin = lockfilePin(SDK_PACKAGE);
	if (!pin)
		throw new Error(
			`apps/kanban/package-lock.json pins no ${SDK_PACKAGE}: "the version the lockfile pins" is unreadable`,
		);
	const versions = standInVersions(pin);
	const registry = await startStandInRegistry({
		sdkVersions: [versions.older, versions.pin, versions.latest],
		sdkLatest: versions.latest,
	});
	const state = createStateRoot(root);
	let board: RunningBoard;
	try {
		board = await startBoard(bundle, state, { registry });
	} catch (error) {
		await registry.close();
		throw error;
	}
	const world: World = {
		root,
		state,
		registry,
		board,
		versions,
		restart: async () => {
			await world.board.stop();
			world.board = await startBoard(bundle, state, { registry });
			return world.board;
		},
		close: async () => {
			await world.board.stop();
			await registry.close();
		},
	};
	return world;
}

export async function readStatus(board: RunningBoard): Promise<{ result: TrpcResult; status: ClaudeSdkStatus }> {
	const result = await trpcQuery(board, D0F_SEAMS.statusQuery);
	if (result.status !== 200) {
		throw new Error(`${D0F_SEAMS.statusQuery} did not answer: ${describeResult(result)}`);
	}
	return { result, status: parseClaudeSdkStatus(result.data) };
}

/** The action as the user takes it after reading the notice: acknowledged, naming the notice shown. */
export async function installAcknowledged(board: RunningBoard, version?: string): Promise<TrpcResult> {
	const { status } = await readStatus(board);
	return await trpcMutation(
		board,
		D0F_SEAMS.installMutation,
		installInput({ acknowledged: true, noticeSha256: status.noticeSha256, ...(version ? { version } : {}) }),
	);
}

export async function catalogIds(board: RunningBoard): Promise<string[]> {
	const result = await trpcQuery(board, "runtime.getClineProviderCatalog");
	const providers = (result.data as { providers?: { id?: string }[] } | undefined)?.providers;
	if (result.status !== 200 || !Array.isArray(providers)) {
		throw new Error(`runtime.getClineProviderCatalog did not answer: ${describeResult(result)}`);
	}
	return providers.map((provider) => String(provider.id));
}
