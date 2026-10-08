import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { constants } from "node:fs";
import { open, realpath } from "node:fs/promises";
import { isAbsolute, join, relative, resolve } from "node:path";
import { z } from "zod";
import { lockedFileSystem } from "../fs/locked-file-system";
import { loadWorkspaceContext } from "./workspace-state";

export const registerAdrDocumentSchema = z.object({
	initiativeId: z.string().trim().min(1).max(200),
	adrId: z.string().trim().min(1).max(200),
	path: z.string().trim().min(1).max(1000),
});
const MAX_INDEX_BYTES = 8 * 1024 * 1024;
const MAX_SNAPSHOTS = 100;
const documentSchema = registerAdrDocumentSchema.extend({
	workspaceId: z.string().min(1).max(512),
	title: z.string().max(2000),
	declaredStatus: z.string().max(2000).nullable(),
	sourceRevision: z.string().regex(/^[a-f0-9]{40}$/),
	sha256: z.string().regex(/^[a-f0-9]{64}$/),
	content: z.string().max(200_000),
	registeredAt: z.string().datetime(),
});
export type AdrPlanningDocument = z.infer<typeof documentSchema>;
const documentsSchema = z.array(documentSchema).max(MAX_SNAPSHOTS);

async function readDocuments(path: string): Promise<AdrPlanningDocument[]> {
	try {
		if ((await realpath(path)) !== path) throw new Error("Planning index must be physical");
		const handle = await open(path, constants.O_RDONLY | constants.O_NOFOLLOW);
		let bytes: Buffer;
		try {
			const stat = await handle.stat();
			if (!stat.isFile() || stat.size > MAX_INDEX_BYTES)
				throw new Error("Planning index exceeds its 8 MiB file boundary");
			bytes = await handle.readFile();
			if (bytes.length > MAX_INDEX_BYTES) throw new Error("Planning index exceeds 8 MiB");
		} finally {
			await handle.close();
		}
		const documents = documentsSchema.parse(JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes)));
		for (const document of documents) {
			if (createHash("sha256").update(document.content).digest("hex") !== document.sha256)
				throw new Error("Planning snapshot content digest mismatch");
		}
		return documents;
	} catch (error) {
		if (error instanceof Error && "code" in error && error.code === "ENOENT") return [];
		throw error;
	}
}
export async function listAdrDocuments(cwd: string): Promise<AdrPlanningDocument[]> {
	const context = await loadWorkspaceContext(cwd, { autoCreateIfMissing: false });
	return readDocuments(join(context.statePath, "adr-planning.json"));
}
/** Retain the exact reviewed working-tree bytes. HEAD is provenance, not a claim those bytes are committed. */
export async function registerAdrDocument(cwd: string, value: z.infer<typeof registerAdrDocumentSchema>) {
	const input = registerAdrDocumentSchema.parse(value);
	const context = await loadWorkspaceContext(cwd, { autoCreateIfMissing: false });
	if (isAbsolute(input.path) || !/\.md$/i.test(input.path))
		throw new Error("Choose a repository-relative Markdown path");
	const root = await realpath(context.repoPath);
	const source = resolve(root, input.path);
	const sourceRelative = relative(root, source);
	if (sourceRelative.startsWith("..") || isAbsolute(sourceRelative) || (await realpath(source)) !== source)
		throw new Error("ADR source must be a physical file inside this repository");
	const handle = await open(source, constants.O_RDONLY | constants.O_NOFOLLOW);
	let bytes: Buffer;
	try {
		const stat = await handle.stat();
		if (!stat.isFile() || stat.size > 200_000) throw new Error("ADR source must be a Markdown file under 200 KB");
		bytes = await handle.readFile();
		if (bytes.length > 200_000) throw new Error("ADR source exceeds 200 KB");
	} finally {
		await handle.close();
	}
	const content = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
	const document: AdrPlanningDocument = {
		...input,
		path: sourceRelative,
		workspaceId: context.workspaceId,
		title: (/^#\s+(.+)$/m.exec(content)?.[1] ?? input.adrId).slice(0, 2000),
		declaredStatus: /^\s*(?:\*\*)?Status(?:\*\*)?\s*:\s*(.+)$/im.exec(content)?.[1] ?? null,
		sourceRevision: execFileSync("git", ["rev-parse", "HEAD"], { cwd: root, encoding: "utf8" }).trim(),
		sha256: createHash("sha256").update(bytes).digest("hex"),
		content,
		registeredAt: new Date().toISOString(),
	};
	const path = join(context.statePath, "adr-planning.json");
	return lockedFileSystem.withLock({ path }, async () => {
		const prior = await readDocuments(path);
		const same = prior.find(
			(item) =>
				item.initiativeId === input.initiativeId && item.adrId === input.adrId && item.sha256 === document.sha256,
		);
		if (same) return same;
		const next = documentsSchema.parse([...prior, document]);
		if (Buffer.byteLength(JSON.stringify(next)) > MAX_INDEX_BYTES)
			throw new Error(
				"Planning snapshot history exceeds 8 MiB; export and archive reviewed history before registering more",
			);
		await lockedFileSystem.writeJsonFileAtomic(path, next, { lock: null });
		return document;
	});
}
