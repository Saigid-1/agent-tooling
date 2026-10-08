import { createHash } from "node:crypto";
import { lstat, open } from "node:fs/promises";
import { isAbsolute } from "node:path";
import type { RuntimeNativeSessionBinding } from "../../core/api-contract";

const MAX_HEADER_BYTES = 64 * 1024;

function record(value: unknown): Record<string, unknown> | null {
	return value !== null && typeof value === "object" && !Array.isArray(value)
		? (value as Record<string, unknown>)
		: null;
}

/** Bind only when explicit hook fields agree with the exact native transcript header. */
export async function resolveCodexNativeBindingFromHookPayload(
	payload: Record<string, unknown> | null,
	expectedCwd: string,
): Promise<RuntimeNativeSessionBinding | null> {
	const nativeId = payload?.session_id;
	const sourcePath = payload?.transcript_path;
	if (
		typeof nativeId !== "string" ||
		!nativeId ||
		nativeId.length > 256 ||
		typeof sourcePath !== "string" ||
		!isAbsolute(sourcePath) ||
		sourcePath.length > 4096
	)
		return null;

	try {
		const pathInfo = await lstat(sourcePath);
		if (!pathInfo.isFile()) return null;
		const file = await open(sourcePath, "r");
		try {
			const info = await file.stat();
			if (!info.isFile() || info.size === 0 || info.dev !== pathInfo.dev || info.ino !== pathInfo.ino) return null;
			const buffer = Buffer.alloc(Math.min(info.size, MAX_HEADER_BYTES));
			let length = 0;
			let newline = -1;
			while (length < buffer.length && newline < 0) {
				const next = await file.read(buffer, length, buffer.length - length, length);
				if (next.bytesRead === 0) break;
				newline = buffer.subarray(length, length + next.bytesRead).indexOf(10);
				if (newline >= 0) newline += length;
				length += next.bytesRead;
			}
			if (newline < 0 && info.size > MAX_HEADER_BYTES) return null;
			const headerBytes = buffer.subarray(0, newline >= 0 ? newline + 1 : length);
			const header = record(JSON.parse(headerBytes.toString("utf8")) as unknown);
			const metadata = record(header?.payload);
			if (
				header?.type !== "session_meta" ||
				metadata?.id !== nativeId ||
				metadata.cwd !== expectedCwd ||
				record(metadata.source)?.subagent !== undefined
			)
				return null;
			return {
				providerId: "codex",
				nativeId,
				observedAt: new Date().toISOString(),
				source: "codex_hook_transcript_meta",
				sourcePath,
				sourceSha256: createHash("sha256").update(headerBytes).digest("hex"),
			};
		} finally {
			await file.close();
		}
	} catch {
		return null;
	}
}
