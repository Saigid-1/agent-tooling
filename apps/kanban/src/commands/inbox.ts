import { constants } from "node:fs";
import { open } from "node:fs/promises";
import { createTRPCProxyClient, httpLink } from "@trpc/client";
import type { Command } from "commander";
import { durableInboxMessageSchema } from "../core/durable-inbox";
import type { RuntimeAppRouter } from "../trpc/app-router";

/** An explicit file is retry material: no credentials, implicit routing, or transcript capture. */
export async function sendInboxMessage(endpoint: string, filePath: string): Promise<unknown> {
	const url = new URL(endpoint);
	if (
		url.protocol !== "http:" ||
		!["127.0.0.1", "[::1]"].includes(url.hostname) ||
		url.username ||
		url.password ||
		url.pathname !== "/" ||
		url.search ||
		url.hash
	)
		throw new Error(
			"Inbox publisher requires an explicit loopback HTTP origin (use a local tunnel for remote hosts)",
		);
	const file = await open(filePath, constants.O_RDONLY | constants.O_NOFOLLOW);
	let input: unknown;
	try {
		const info = await file.stat();
		if (!info.isFile() || info.size > 32768)
			throw new Error("Message input must be a regular file of at most 32 KiB");
		const bytes = Buffer.alloc(32769);
		let length = 0;
		while (length < bytes.length) {
			const { bytesRead } = await file.read(bytes, length, bytes.length - length, null);
			if (!bytesRead) break;
			length += bytesRead;
		}
		if (length > 32768) throw new Error("Message input exceeds 32 KiB");
		input = JSON.parse(bytes.subarray(0, length).toString("utf8")) as unknown;
	} finally {
		await file.close();
	}
	const message = durableInboxMessageSchema.parse(input);
	const client = createTRPCProxyClient<RuntimeAppRouter>({
		links: [
			httpLink({
				url: `${url.origin}/api/trpc`,
				headers: { "x-kanban-workspace-id": message.workspace_id },
				fetch: (url, options) => fetch(url, { ...options, redirect: "error", signal: AbortSignal.timeout(10000) }),
			}),
		],
	});
	return await client.inbox.send.mutate(message);
}

export function registerInboxCommand(program: Command): void {
	program
		.command("inbox-send")
		.description("Persist a bounded message and notify exact recipients; repeat the same input safely.")
		.requiredOption("--endpoint <origin>", "Local Kanban runtime origin")
		.requiredOption("--file <path>", "JSON message file retained for retry")
		.action(async (options: { endpoint: string; file: string }) => {
			try {
				process.stdout.write(`${JSON.stringify(await sendInboxMessage(options.endpoint, options.file))}\n`);
			} catch (error) {
				process.stderr.write(
					`Inbox publication not confirmed; retain and retry the same input: ${error instanceof Error ? error.message : String(error)}\n`,
				);
				process.exitCode = 1;
			}
		});
}
