import { execFile } from "node:child_process";
import { readFile, writeFile } from "node:fs/promises";
import { isAbsolute, resolve } from "node:path";
import { promisify } from "node:util";
import { z } from "zod";
import { isHomeAgentSessionId } from "../core/home-agent-session";
import type { AgentAdapterLaunchInput, PreparedAgentLaunch } from "./agent-session-adapters";

const run = promisify(execFile);
const resultSchema = z.object({
	native_session_id: z.string().uuid(), settings_path: z.string(), mcp_path: z.string(),
	launch_receipt: z.string(), binding_key: z.string(),
});
const hookSettings = z.object({ hooks: z.record(z.string(), z.array(z.unknown())) });

/** Explicit operator selection applies only to this workspace's sidebar. */
export async function bindAssistantMemory(input: AgentAdapterLaunchInput, launch: PreparedAgentLaunch): Promise<PreparedAgentLaunch> {
	const configured = process.env.KANBAN_ASSISTANT_MEMORY_WORKSPACE;
	if (!configured || resolve(input.cwd) !== resolve(configured) || !isHomeAgentSessionId(input.taskId)) return launch;
	if (input.agentId !== "claude" || input.resumeFromTrash) {
		throw new Error("Isolated assistant capture currently supports fresh Claude Code sidebar sessions only.");
	}
	const command = z.array(z.string().min(1)).min(1).parse(JSON.parse(process.env.KANBAN_ASSISTANT_MEMORY_COMMAND ?? "null"));
	if (!isAbsolute(command[0])) throw new Error("Assistant memory command must name an absolute executable.");
	const { stdout } = await run(command[0], [...command.slice(1), "--workspace", input.cwd, "--task", input.taskId, "prepare"], {
		timeout: 30000, maxBuffer: 65536, env: { ...process.env, ...input.env },
	});
	const bound = resultSchema.parse(JSON.parse(stdout));
	for (const path of [bound.settings_path, bound.mcp_path, bound.launch_receipt]) {
		if (!isAbsolute(path)) throw new Error("Assistant memory paths must be absolute.");
	}
	const args = [...launch.args];
	const settingsIndex = args.indexOf("--settings");
	const assistant = hookSettings.parse(JSON.parse(await readFile(bound.settings_path, "utf8")));
	if (settingsIndex >= 0) {
		const existing = hookSettings.parse(JSON.parse(await readFile(args[settingsIndex + 1], "utf8")));
		for (const [event, hooks] of Object.entries(existing.hooks)) {
			assistant.hooks[event] = [...(assistant.hooks[event] ?? []), ...hooks];
		}
		args.splice(settingsIndex, 2);
	}
	await writeFile(bound.settings_path, JSON.stringify(assistant), { mode: 0o600 });
	// Existing user/project hooks and MCP registrations may write to global memory.
	// This explicit isolated mode uses only scoped settings and its private MCP.
	args.push("--settings", bound.settings_path, "--setting-sources", "", "--session-id", bound.native_session_id,
		"--strict-mcp-config", "--mcp-config", bound.mcp_path);
	return { ...launch, args, env: { ...launch.env, ASSISTANT_MEMORY_LAUNCH_RECEIPT: bound.launch_receipt } };
}
