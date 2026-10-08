import { createHash } from "node:crypto";
import { mkdtemp, rm, symlink, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { resolveCodexNativeBindingFromHookPayload } from "../../src/commands/hook-events/codex-native-binding";

const cwd = "/example/task";

async function withTranscript(run: (path: string) => Promise<void>): Promise<void> {
	const directory = await mkdtemp(join(tmpdir(), "kanban-native-hook-"));
	try {
		await run(join(directory, "transcript.jsonl"));
	} finally {
		await rm(directory, { recursive: true, force: true });
	}
}

describe("Codex native hook binding", () => {
	it("binds only an explicit hook ID matching the exact transcript header and cwd", async () => {
		await withTranscript(async (path) => {
			const header = `${JSON.stringify({ type: "session_meta", payload: { id: "native-1", cwd, source: "cli" } })}\n`;
			await writeFile(
				path,
				`${header}${JSON.stringify({ type: "event_msg", payload: { type: "task_started" } })}\n`,
			);
			const binding = await resolveCodexNativeBindingFromHookPayload(
				{ session_id: "native-1", transcript_path: path },
				cwd,
			);
			expect(binding).toMatchObject({
				providerId: "codex",
				nativeId: "native-1",
				source: "codex_hook_transcript_meta",
				sourcePath: path,
				sourceSha256: createHash("sha256").update(header).digest("hex"),
			});
		});
	});
	it("refuses absent fields, ID mismatch, cwd mismatch and descendant metadata", async () => {
		await withTranscript(async (path) => {
			await writeFile(path, `${JSON.stringify({ type: "session_meta", payload: { id: "native-1", cwd } })}\n`);
			expect(await resolveCodexNativeBindingFromHookPayload({ transcript_path: path }, cwd)).toBeNull();
			expect(await resolveCodexNativeBindingFromHookPayload({ session_id: "native-1" }, cwd)).toBeNull();
			expect(
				await resolveCodexNativeBindingFromHookPayload({ session_id: "native-2", transcript_path: path }, cwd),
			).toBeNull();
			expect(
				await resolveCodexNativeBindingFromHookPayload({ session_id: "native-1", transcript_path: path }, "/other"),
			).toBeNull();
			await writeFile(
				path,
				`${JSON.stringify({ type: "session_meta", payload: { id: "native-1", cwd, source: { subagent: {} } } })}\n`,
			);
			expect(
				await resolveCodexNativeBindingFromHookPayload({ session_id: "native-1", transcript_path: path }, cwd),
			).toBeNull();
		});
	});
	it("refuses symlinks and headers beyond the read bound", async () => {
		await withTranscript(async (path) => {
			await writeFile(path, `${JSON.stringify({ type: "session_meta", payload: { id: "native-1", cwd } })}\n`);
			if (process.platform !== "win32") {
				const link = join(path, "..", "link.jsonl");
				await symlink(path, link);
				expect(
					await resolveCodexNativeBindingFromHookPayload({ session_id: "native-1", transcript_path: link }, cwd),
				).toBeNull();
			}
			await writeFile(path, " ".repeat(64 * 1024 + 1));
			expect(
				await resolveCodexNativeBindingFromHookPayload({ session_id: "native-1", transcript_path: path }, cwd),
			).toBeNull();
		});
	});
});
