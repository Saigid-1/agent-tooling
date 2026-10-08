import { createHash } from "node:crypto";
import { mkdir, mkdtemp, readFile, realpath, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { readWorkspaceSessionRegistry } from "../../src/state/workspace-state";

let storageRoot: string | undefined;
let sessionsPath: string;
let previousStorageRoot: string | undefined;

beforeEach(async () => {
	previousStorageRoot = process.env.KANBAN_STORAGE_ROOT;
	storageRoot = undefined;
	const root = await mkdtemp(join(await realpath(tmpdir()), "harness-registry-"));
	storageRoot = root;
	process.env.KANBAN_STORAGE_ROOT = root;
	const workspacesPath = join(root, "kanban", "workspaces");
	await mkdir(join(workspacesPath, "repo-a"), { recursive: true });
	await writeFile(
		join(workspacesPath, "index.json"),
		JSON.stringify({
			version: 1,
			entries: { "repo-a": { workspaceId: "repo-a", repoPath: "/example/repo-a" } },
			repoPathToId: { "/example/repo-a": "repo-a" },
		}),
	);
	sessionsPath = join(workspacesPath, "repo-a", "sessions.json");
});

afterEach(async () => {
	if (previousStorageRoot === undefined) delete process.env.KANBAN_STORAGE_ROOT;
	else process.env.KANBAN_STORAGE_ROOT = previousStorageRoot;
	if (storageRoot) await rm(storageRoot, { recursive: true, force: true });
});

describe("saved workspace session registry reader", () => {
	it("reports an absent file explicitly", async () => {
		const snapshot = await readWorkspaceSessionRegistry("repo-a");
		expect(snapshot.sessions).toEqual({});
		expect(snapshot.evidence).toMatchObject({ path: sessionsPath, present: false, sha256: null });
	});
	it("rejects malformed and null JSON", async () => {
		await writeFile(sessionsPath, "{");
		await expect(readWorkspaceSessionRegistry("repo-a")).rejects.toThrow("Malformed JSON");
		await writeFile(sessionsPath, "null");
		await expect(readWorkspaceSessionRegistry("repo-a")).rejects.toThrow("received null");
	});
	it("rejects a file above the read bound", async () => {
		await writeFile(sessionsPath, Buffer.alloc(4 * 1024 * 1024 + 1));
		await expect(readWorkspaceSessionRegistry("repo-a")).rejects.toThrow("exceeds");
	});
	it("rejects a non-regular registry path", async () => {
		await mkdir(sessionsPath);
		await expect(readWorkspaceSessionRegistry("repo-a")).rejects.toThrow("not a regular file");
	});
	it("hashes the saved bytes without changing them", async () => {
		const bytes = Buffer.from("{}\n");
		await writeFile(sessionsPath, bytes);
		const before = await readFile(sessionsPath);
		const snapshot = await readWorkspaceSessionRegistry("repo-a");
		expect(snapshot.sessions).toEqual({});
		expect(snapshot.evidence.sha256).toBe(createHash("sha256").update(bytes).digest("hex"));
		expect(snapshot.evidence.present).toBe(true);
		expect(await readFile(sessionsPath)).toEqual(before);
	});
});
