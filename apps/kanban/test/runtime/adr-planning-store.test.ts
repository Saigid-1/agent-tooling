import { createHash } from "node:crypto";
import { mkdir, mkdtemp, rm, symlink, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const context = vi.hoisted(() => ({ repoPath: "", statePath: "", workspaceId: "workspace-a" }));
vi.mock("../../src/state/workspace-state", () => ({ loadWorkspaceContext: vi.fn(async () => context) }));
vi.mock("node:child_process", () => ({ execFileSync: vi.fn(() => `${"a".repeat(40)}\n`) }));

import { listAdrDocuments, registerAdrDocument } from "../../src/state/adr-planning-store";

let root: string;
beforeEach(async () => {
	root = await mkdtemp(join(tmpdir(), "adr-planning-"));
	context.repoPath = join(root, "repo");
	context.statePath = join(root, "state");
	await mkdir(context.repoPath);
	await mkdir(context.statePath);
});
afterEach(async () => {
	await rm(root, { recursive: true, force: true });
});
const input = { initiativeId: "platform", adrId: "ADR-8", path: "decision.md" };
describe("ADR planning before execution slices", () => {
	it("retains exact content separately from HEAD, replays and preserves changed revisions", async () => {
		const content = "# Decide interfaces\nStatus: Proposed\n\nUnresolved: ownership\n";
		await writeFile(join(context.repoPath, input.path), content);
		const first = await registerAdrDocument(context.repoPath, input);
		expect(first.sha256).toBe(createHash("sha256").update(content).digest("hex"));
		expect(first.sourceRevision).toBe("a".repeat(40));
		expect(first.content).toBe(content);
		expect(first.declaredStatus).toBe("Proposed");
		expect(await registerAdrDocument(context.repoPath, input)).toEqual(first);
		await writeFile(join(context.repoPath, input.path), `${content}Next question\n`);
		await registerAdrDocument(context.repoPath, input);
		expect(await listAdrDocuments(context.repoPath)).toHaveLength(2);
	});
	it("rejects traversal, absolute paths and symlink sources", async () => {
		await writeFile(join(root, "outside.md"), "Private");
		await expect(registerAdrDocument(context.repoPath, { ...input, path: "../outside.md" })).rejects.toThrow(
			"inside this repository",
		);
		await expect(registerAdrDocument(context.repoPath, { ...input, path: join(root, "outside.md") })).rejects.toThrow(
			"repository-relative",
		);
		await symlink(join(root, "outside.md"), join(context.repoPath, "link.md"));
		await expect(registerAdrDocument(context.repoPath, { ...input, path: "link.md" })).rejects.toThrow(
			"physical file",
		);
	});
	it("rejects oversized source instead of silently truncating the planning snapshot", async () => {
		await writeFile(join(context.repoPath, input.path), "x".repeat(200001));
		await expect(registerAdrDocument(context.repoPath, input)).rejects.toThrow("under 200 KB");
	});
});

it("rejects tampered snapshots and oversized or symlinked indexes", async () => {
	await writeFile(join(context.repoPath, input.path), "# Decision");
	const snapshot = await registerAdrDocument(context.repoPath, input);
	const index = join(context.statePath, "adr-planning.json");
	await writeFile(index, JSON.stringify([{ ...snapshot, content: "tampered" }]));
	await expect(listAdrDocuments(context.repoPath)).rejects.toThrow("digest mismatch");
	await writeFile(index, " ".repeat(8 * 1024 * 1024 + 1));
	await expect(listAdrDocuments(context.repoPath)).rejects.toThrow("8 MiB");
	await rm(index);
	await writeFile(join(root, "elsewhere.json"), "[]");
	await symlink(join(root, "elsewhere.json"), index);
	await expect(listAdrDocuments(context.repoPath)).rejects.toThrow("physical");
});
