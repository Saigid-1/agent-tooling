import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { access, chmod, constants as fsConstants, mkdir, readdir, readFile, writeFile } from "node:fs/promises";
import { homedir } from "node:os";
import { basename, dirname, isAbsolute, join } from "node:path";
import { pathToFileURL } from "node:url";
import { z } from "zod";

import { isRuntimeAgentLaunchSupported } from "../core/agent-catalog";
import type {
	RuntimeAgentId,
	RuntimeHookEvent,
	RuntimeTaskImage,
	RuntimeTaskSessionSummary,
} from "../core/api-contract";
import { runtimeTaskDeskIdSchema } from "../core/api-contract";
import { isHomeAgentSessionId } from "../core/home-agent-session";
import { buildKanbanCommandParts } from "../core/kanban-command";
import { quoteShellArg } from "../core/shell";
import { lockedFileSystem } from "../fs/locked-file-system";
import { resolveHomeAgentAppendSystemPrompt } from "../prompts/append-system-prompt";
import { getRuntimeHomePath } from "../state/workspace-state";
import { bindAssistantMemory } from "./assistant-memory-launch";
import {
	addCodexConfigOverrides,
	type CodexAdditionalHooks,
	configureCodexHooks,
	hasCodexConfigOverride,
} from "./codex-hook-config";
import { createHookRuntimeEnv } from "./hook-runtime-context";
import {
	getOpenCodeAuthPathCandidates,
	getOpenCodeConfigPathCandidates,
	getOpenCodeModelStatePathCandidates,
} from "./opencode-paths";
import { stripAnsi } from "./output-utils";
import type { SessionTransitionEvent } from "./session-state-machine";
import { prepareTaskPromptWithImages } from "./task-image-prompt";

export interface AgentAdapterLaunchInput {
	taskId: string;
	agentId: RuntimeAgentId;
	binary?: string;
	args: string[];
	autonomousModeEnabled?: boolean;
	cwd: string;
	prompt: string;
	images?: RuntimeTaskImage[];
	startInPlanMode?: boolean;
	resumeFromTrash?: boolean;
	env?: Record<string, string | undefined>;
	workspaceId?: string;
	/** Registry desk chosen on the task card; the launch binding admits the session to it. */
	deskId?: string;
}

export type AgentOutputTransitionDetector = (
	data: string,
	summary: RuntimeTaskSessionSummary,
) => SessionTransitionEvent | null;

export type AgentOutputTransitionInspectionPredicate = (summary: RuntimeTaskSessionSummary) => boolean;

export interface PreparedAgentLaunch {
	binary?: string;
	args: string[];
	env: Record<string, string | undefined>;
	cleanup?: () => Promise<void>;
	deferredStartupInput?: string;
	detectOutputTransition?: AgentOutputTransitionDetector;
	shouldInspectOutputForTransition?: AgentOutputTransitionInspectionPredicate;
}

interface HookContext {
	taskId: string;
	workspaceId: string;
}

interface HookCommandMetadata {
	source?: string;
	activityText?: string;
	hookEventName?: string;
	notificationType?: string;
}

/** What `kp-agent-launch prepare` returned for this launch (see docs/LAUNCH-BINDING.md). */
export interface LaunchBinding {
	receiptPath: string;
	nativeSessionId: string | null;
	argvAdditions: string[];
	envAdditions: Record<string, string>;
	hookSettingsPath: string | null;
}

interface AdapterPrepareInput extends AgentAdapterLaunchInput {
	launchBinding?: LaunchBinding | null;
}

interface AgentSessionAdapter {
	/** Refusals that must come before anything is bound or written for the launch. */
	preflight?(input: AgentAdapterLaunchInput): Promise<void>;
	prepare(input: AdapterPrepareInput): Promise<PreparedAgentLaunch>;
}

const LAUNCH_BINDING_NOT_CONFIGURED = "This task has a desk, but launch binding is not configured on this host.";
const LAUNCH_BINDING_EXECUTABLE = "kp-agent-launch";
const LAUNCH_BINDING_PROVIDER = "harness-default";
const LAUNCH_BINDING_MODEL = "harness-default";
const LAUNCH_BINDING_TIMEOUT_MS = 30_000;
const LAUNCH_BINDING_OUTPUT_BYTES = 1024 * 1024;
const launchBindingCommandSchema = z.array(z.string().min(1).max(4096)).min(1).max(20);
const launchBindingResultSchema = z.object({
	receipt_path: z.string().min(1).max(4096),
	native_session_id: z.string().min(1).max(256).nullable(),
	argv_additions: z.array(z.string().max(65536)).max(64),
	env_additions: z.record(z.string().regex(/^[A-Z_][A-Z0-9_]{0,127}$/), z.string().max(4096)),
	files: z.record(z.string(), z.string().max(4096).nullable()),
});
const launchBindingErrorSchema = z.object({ status: z.literal("error"), category: z.string(), message: z.string() });
const hookSettingsDocumentSchema = z.object({ hooks: z.record(z.string(), z.array(z.unknown())) });
const hookSettingsGroupSchema = z.object({
	hooks: z.array(
		z.object({ type: z.literal("command"), command: z.string().min(1), timeout: z.number().int().positive() }),
	),
});

/**
 * True when the command runs `kp-agent-launch` directly (not through e.g. `docker exec`) and the
 * registry configuration it names with `--config` is not written yet. The board in the container
 * names the runtime's fixed registry path before the operator writes it.
 */
function launchRegistryConfigUnwritten(command: string[]): boolean {
	if (basename(command[0] ?? "") !== LAUNCH_BINDING_EXECUTABLE) return false;
	const index = command.indexOf("--config");
	const inline = command.find((item) => item.startsWith("--config="));
	const config = index >= 0 ? command[index + 1] : inline?.slice("--config=".length);
	return config !== undefined && isAbsolute(config) && !existsSync(config);
}

function runLaunchBindingCommand(
	executable: string,
	args: string[],
	body: string,
	env: Record<string, string | undefined>,
): Promise<{ code: number | null; stdout: string }> {
	return new Promise((resolve, reject) => {
		const child = spawn(executable, args, { stdio: ["pipe", "pipe", "pipe"], env });
		const output: Buffer[] = [];
		let bytes = 0;
		let settled = false;
		const finish = (error: Error | null, code: number | null = null) => {
			if (settled) return;
			settled = true;
			clearTimeout(timer);
			if (error) reject(error);
			else resolve({ code, stdout: Buffer.concat(output).toString("utf8") });
		};
		const timer = setTimeout(() => {
			child.kill();
			finish(new Error("Launch binding timed out."));
		}, LAUNCH_BINDING_TIMEOUT_MS);
		child.on("error", () => finish(new Error("Launch binding command could not start.")));
		child.stdin.on("error", () => finish(new Error("Launch binding input unavailable.")));
		child.stdout.on("data", (chunk: Buffer) => {
			bytes += chunk.length;
			if (bytes > LAUNCH_BINDING_OUTPUT_BYTES) {
				child.kill();
				finish(new Error("Launch binding response exceeds its bound."));
			} else output.push(chunk);
		});
		child.stderr.on("data", () => {}); // Never relay paths or credentials from the child.
		child.on("close", (code) => finish(null, code));
		child.stdin.end(body);
	});
}

/**
 * Bind a desk task's launch through the operator command in KANBAN_LAUNCH_BINDING_COMMAND
 * (a JSON argv, e.g. kp-agent-launch --config <registry config>). Tasks without a desk, and
 * harnesses without an enabled profile (exit 3, harness_unavailable), launch unbound as before.
 */
export async function resolveLaunchBinding(input: AgentAdapterLaunchInput): Promise<LaunchBinding | null> {
	if (input.deskId === undefined) {
		return null;
	}
	const deskId = runtimeTaskDeskIdSchema.parse(input.deskId);
	if (isHomeAgentSessionId(input.taskId)) {
		throw new Error("Desks apply to board tasks; the sidebar assistant keeps its isolated memory.");
	}
	const configured = process.env.KANBAN_LAUNCH_BINDING_COMMAND;
	if (!configured) {
		throw new Error(LAUNCH_BINDING_NOT_CONFIGURED);
	}
	let command: string[];
	try {
		command = launchBindingCommandSchema.parse(JSON.parse(configured));
	} catch {
		throw new Error("Launch binding command must be a JSON argv array.");
	}
	const executable = command[0];
	if (!executable || !isAbsolute(executable)) {
		throw new Error("Launch binding requires an absolute operator executable.");
	}
	if (launchRegistryConfigUnwritten(command)) {
		throw new Error(
			`${LAUNCH_BINDING_NOT_CONFIGURED} The desk registry configuration it names is not written yet; the task was not started.`,
		);
	}
	const request = {
		harness: input.agentId,
		provider: LAUNCH_BINDING_PROVIDER,
		model: LAUNCH_BINDING_MODEL,
		desk_id: deskId,
		workspace: input.cwd,
		task_id: input.taskId,
		source: "board",
		parent_session_id: null,
	};
	// The launcher runs with the agent's own environment, so `~` capture roots name the agent's home.
	const env = Object.fromEntries(
		Object.entries({ ...process.env, ...input.env }).filter(([, value]) => value !== undefined),
	);
	const { code, stdout } = await runLaunchBindingCommand(
		executable,
		[...command.slice(1), "prepare"],
		JSON.stringify(request),
		env,
	);
	let parsed: unknown;
	try {
		parsed = JSON.parse(stdout);
	} catch {
		throw new Error("Launch binding returned an invalid response.");
	}
	if (code !== 0) {
		const refusal = launchBindingErrorSchema.safeParse(parsed);
		if (code === 3 && refusal.success && refusal.data.category === "harness_unavailable") {
			return null;
		}
		throw new Error(`Launch binding refused this task: ${refusal.success ? refusal.data.message : "unknown error"}`);
	}
	const result = launchBindingResultSchema.safeParse(parsed);
	if (!result.success) {
		throw new Error("Launch binding returned an invalid response.");
	}
	const hookSettingsPath = result.data.files.hook_settings ?? null;
	for (const path of [result.data.receipt_path, hookSettingsPath]) {
		if (path !== null && !isAbsolute(path)) {
			throw new Error("Launch binding paths must be absolute.");
		}
	}
	return {
		receiptPath: result.data.receipt_path,
		nativeSessionId: result.data.native_session_id,
		argvAdditions: result.data.argv_additions,
		envAdditions: result.data.env_additions,
		hookSettingsPath,
	};
}

async function readLaunchHookSettings(path: string): Promise<Record<string, unknown[]>> {
	return hookSettingsDocumentSchema.parse(JSON.parse(await readFile(path, "utf8"))).hooks;
}

/** Kanban's Claude hooks merged into the launch's own settings file (capture first, then Kanban). */
async function mergeClaudeLaunchSettings(
	binding: LaunchBinding,
	kanbanHooks: Record<string, unknown[]> | null,
): Promise<string[]> {
	const args = [...binding.argvAdditions];
	if (!kanbanHooks) {
		return args;
	}
	const settingsIndex = args.indexOf("--settings");
	const settingsPath = settingsIndex >= 0 ? args[settingsIndex + 1] : undefined;
	if (!settingsPath || settingsPath !== binding.hookSettingsPath) {
		throw new Error("Launch binding did not provide a Claude settings file to merge Kanban hooks into.");
	}
	const merged = await readLaunchHookSettings(settingsPath);
	for (const [event, groups] of Object.entries(kanbanHooks)) {
		merged[event] = [...(merged[event] ?? []), ...groups];
	}
	await writeFile(settingsPath, JSON.stringify({ hooks: merged }), { mode: 0o600 });
	return args;
}

const CODEX_HOOK_OVERRIDE_KEY = /^(features\.hooks|hooks\.[A-Za-z_]+)$/;

/** Split launch-binding argv into Codex `-c` overrides (hook-related or other) and remaining arguments. */
function splitCodexLaunchArgs(argvAdditions: string[]): {
	hookOverrides: Array<{ key: string; value: string }>;
	otherOverrides: Array<{ key: string; value: string }>;
	rest: string[];
} {
	const hookOverrides: Array<{ key: string; value: string }> = [];
	const otherOverrides: Array<{ key: string; value: string }> = [];
	const rest: string[] = [];
	for (let index = 0; index < argvAdditions.length; index += 1) {
		const arg = argvAdditions[index] ?? "";
		const next = argvAdditions[index + 1];
		if ((arg === "-c" || arg === "--config") && typeof next === "string") {
			const separator = next.indexOf("=");
			const key = separator > 0 ? next.slice(0, separator) : "";
			if (!key) {
				throw new Error("Launch binding returned an invalid Codex override.");
			}
			(CODEX_HOOK_OVERRIDE_KEY.test(key) ? hookOverrides : otherOverrides).push({
				key,
				value: next.slice(separator + 1),
			});
			index += 1;
			continue;
		}
		rest.push(arg);
	}
	return { hookOverrides, otherOverrides, rest };
}

/** Codex launch-binding argv merged with Kanban's own hook overrides (when Kanban hooks are on). */
async function applyCodexLaunchBinding(
	codexArgs: string[],
	binding: LaunchBinding,
	kanbanHooks: boolean,
): Promise<void> {
	const { hookOverrides, otherOverrides, rest } = splitCodexLaunchArgs(binding.argvAdditions);
	if (kanbanHooks) {
		configureCodexHooks(codexArgs, await readCodexAdditionalHooks(binding));
	} else {
		addCodexConfigOverrides(codexArgs, hookOverrides);
	}
	addCodexConfigOverrides(codexArgs, otherOverrides);
	if (rest.length > 0) {
		const subcommandIndex = codexArgs.findIndex((arg) => arg === "resume" || arg === "fork");
		codexArgs.splice(subcommandIndex === -1 ? codexArgs.length : subcommandIndex, 0, ...rest);
	}
}

async function readCodexAdditionalHooks(binding: LaunchBinding): Promise<CodexAdditionalHooks> {
	if (!binding.hookSettingsPath) {
		throw new Error("Launch binding did not provide hook settings to merge with Kanban's Codex hooks.");
	}
	const additional: CodexAdditionalHooks = {};
	for (const [event, groups] of Object.entries(await readLaunchHookSettings(binding.hookSettingsPath))) {
		for (const group of groups) {
			for (const handler of hookSettingsGroupSchema.parse(group).hooks) {
				additional[event] = [...(additional[event] ?? []), { command: handler.command, timeout: handler.timeout }];
			}
		}
	}
	return additional;
}

function escapeForTemplateLiteral(value: string): string {
	return value.replaceAll("\\", "\\\\").replaceAll("`", "\\`");
}

function powerShellQuote(value: string): string {
	return `"${value.replaceAll("`", "``").replaceAll('"', '`"')}"`;
}

function resolveHookContext(input: AgentAdapterLaunchInput): HookContext | null {
	const workspaceId = input.workspaceId?.trim();
	if (!workspaceId) {
		return null;
	}
	return {
		taskId: input.taskId,
		workspaceId,
	};
}

function buildHookCommand(event: RuntimeHookEvent, metadata?: HookCommandMetadata): string {
	const parts = buildHooksCommandParts(["ingest", "--event", event]);
	if (metadata?.source) {
		parts.push("--source", metadata.source);
	}
	if (metadata?.activityText) {
		parts.push("--activity-text", metadata.activityText);
	}
	if (metadata?.hookEventName) {
		parts.push("--hook-event-name", metadata.hookEventName);
	}
	if (metadata?.notificationType) {
		parts.push("--notification-type", metadata.notificationType);
	}
	return parts.map(quoteShellArg).join(" ");
}

function buildHooksCommandParts(args: string[]): string[] {
	return buildKanbanCommandParts(["hooks", ...args]);
}

function buildHooksCommand(args: string[]): string {
	return buildHooksCommandParts(args).map(quoteShellArg).join(" ");
}

function hasCliOption(args: string[], optionName: string): boolean {
	for (let i = 0; i < args.length; i += 1) {
		const arg = args[i];
		if (arg === optionName || arg.startsWith(`${optionName}=`)) {
			return true;
		}
	}
	return false;
}

function getClineHookScriptPath(
	hooksDir: string,
	hookName: "Notification" | "TaskComplete" | "UserPromptSubmit" | "PreToolUse" | "PostToolUse",
): string {
	if (process.platform === "win32") {
		return join(hooksDir, `${hookName}.ps1`);
	}
	return join(hooksDir, hookName);
}

function buildClineHookScriptContent(event: RuntimeHookEvent): string {
	const commandParts = buildHooksCommandParts(["notify", "--event", event, "--source", "cline"]);
	if (process.platform === "win32") {
		const command = commandParts.map(powerShellQuote).join(" ");
		return `$inputText = [Console]::In.ReadToEnd()
try {
  $inputText | & ${command} | Out-Null
} catch {
}
Write-Output '{"cancel":false}'
exit 0
`;
	}
	const command = commandParts.map(quoteShellArg).join(" ");
	return `#!/usr/bin/env bash
INPUT="$(cat || true)"
printf '%s' "$INPUT" | ${command} >/dev/null 2>&1 || true
echo '{"cancel":false}'
`;
}

function buildClineNotificationHookScriptContent(): string {
	const commandParts = buildHooksCommandParts(["notify", "--event", "to_review", "--source", "cline"]);
	if (process.platform === "win32") {
		const command = commandParts.map(powerShellQuote).join(" ");
		return `$inputText = [Console]::In.ReadToEnd()
if (
  $inputText -match '"event"\\s*:\\s*"user_attention"' -and
  $inputText -notmatch '"source"\\s*:\\s*"completion_result"'
) {
  try {
    $inputText | & ${command} | Out-Null
  } catch {
  }
}
Write-Output '{"cancel":false}'
exit 0
`;
	}
	const command = commandParts.map(quoteShellArg).join(" ");
	return `#!/usr/bin/env bash
INPUT="$(cat || true)"
if printf '%s' "$INPUT" | grep -Eq '"event"[[:space:]]*:[[:space:]]*"user_attention"' &&
  ! printf '%s' "$INPUT" | grep -Eq '"source"[[:space:]]*:[[:space:]]*"completion_result"'; then
  printf '%s' "$INPUT" | ${command} >/dev/null 2>&1 || true
fi
echo '{"cancel":false}'
`;
}

function buildClinePreToolUseHookScriptContent(): string {
	const activityCommand = buildHooksCommandParts(["notify", "--event", "activity", "--source", "cline"]);
	const reviewCommand = buildHooksCommandParts(["notify", "--event", "to_review", "--source", "cline"]);
	const inProgressCommand = buildHooksCommandParts(["notify", "--event", "to_in_progress", "--source", "cline"]);
	if (process.platform === "win32") {
		const activity = activityCommand.map(powerShellQuote).join(" ");
		const review = reviewCommand.map(powerShellQuote).join(" ");
		const inProgress = inProgressCommand.map(powerShellQuote).join(" ");
		return `$inputText = [Console]::In.ReadToEnd()
$isUserQuestionTool = $inputText -match '"(toolName|tool)"\\s*:\\s*"(ask_followup_question|plan_mode_respond)"'
try {
  $inputText | & ${activity} | Out-Null
} catch {
}
if ($isUserQuestionTool) {
  try {
    $inputText | & ${review} | Out-Null
  } catch {
  }
} else {
  try {
    $inputText | & ${inProgress} | Out-Null
  } catch {
  }
}
Write-Output '{"cancel":false}'
exit 0
`;
	}
	const activity = activityCommand.map(quoteShellArg).join(" ");
	const review = reviewCommand.map(quoteShellArg).join(" ");
	const inProgress = inProgressCommand.map(quoteShellArg).join(" ");
	return `#!/usr/bin/env bash
INPUT="$(cat || true)"
printf '%s' "$INPUT" | ${activity} >/dev/null 2>&1 || true
if printf '%s' "$INPUT" | grep -Eq '"(toolName|tool)"[[:space:]]*:[[:space:]]*"(ask_followup_question|plan_mode_respond)"'; then
  printf '%s' "$INPUT" | ${review} >/dev/null 2>&1 || true
else
  printf '%s' "$INPUT" | ${inProgress} >/dev/null 2>&1 || true
fi
echo '{"cancel":false}'
`;
}

function buildClinePostToolUseHookScriptContent(): string {
	const activityCommand = buildHooksCommandParts(["notify", "--event", "activity", "--source", "cline"]);
	const inProgressCommand = buildHooksCommandParts(["notify", "--event", "to_in_progress", "--source", "cline"]);
	if (process.platform === "win32") {
		const activity = activityCommand.map(powerShellQuote).join(" ");
		const inProgress = inProgressCommand.map(powerShellQuote).join(" ");
		return `$inputText = [Console]::In.ReadToEnd()
$isUserQuestionTool = $inputText -match '"(toolName|tool)"\\s*:\\s*"(ask_followup_question|plan_mode_respond)"'
try {
  $inputText | & ${activity} | Out-Null
} catch {
}
if ($isUserQuestionTool) {
  try {
    $inputText | & ${inProgress} | Out-Null
  } catch {
  }
}
Write-Output '{"cancel":false}'
exit 0
`;
	}
	const activity = activityCommand.map(quoteShellArg).join(" ");
	const inProgress = inProgressCommand.map(quoteShellArg).join(" ");
	return `#!/usr/bin/env bash
INPUT="$(cat || true)"
printf '%s' "$INPUT" | ${activity} >/dev/null 2>&1 || true
if printf '%s' "$INPUT" | grep -Eq '"(toolName|tool)"[[:space:]]*:[[:space:]]*"(ask_followup_question|plan_mode_respond)"'; then
  printf '%s' "$INPUT" | ${inProgress} >/dev/null 2>&1 || true
fi
echo '{"cancel":false}'
`;
}

function buildOpenCodePluginContent(
	reviewCommand: string,
	toInProgressCommand: string,
	activityCommand: string,
): string {
	const reviewCmd = escapeForTemplateLiteral(reviewCommand);
	const toInProgressCmd = escapeForTemplateLiteral(toInProgressCommand);
	const activityCmd = escapeForTemplateLiteral(activityCommand);
	return `export const KanbanPlugin = async ({ $, client }) => {
  if (globalThis.__kanbanOpencodePluginV3) return {};
  globalThis.__kanbanOpencodePluginV3 = true;

  // Order O2: a launch bound to a desk carries its capture hook command (a JSON argv) in its own
  // environment. The plugin maps a root session's idle status to Stop and compaction to PreCompact;
  // the board runs SessionEnd when the terminal exits.
  const launchHook = (() => {
    try {
      const argv = JSON.parse(process?.env?.${OPENCODE_LAUNCH_HOOK_ENV} ?? "");
      return Array.isArray(argv) && argv.length > 0 && argv.every((part) => typeof part === "string" && part) ? argv : null;
    } catch {
      return null;
    }
  })();
  const boardHooks = !!process?.env?.KANBAN_HOOK_TASK_ID;
  if (!boardHooks && !launchHook) return {};
  let spawnLaunchHook = null;

  // Detached and never awaited: OpenCode's hooks have no timeout, so the capture never holds the
  // session. A spawn that fails is recovered by the next Stop or by SessionEnd.
  const runLaunchHook = async (hookEventName, sessionID) => {
    if (!launchHook || typeof sessionID !== "string" || !sessionID) return;
    try {
      spawnLaunchHook ??= (await import("node:child_process")).spawn;
      const child = spawnLaunchHook(launchHook[0], launchHook.slice(1), {
        cwd: process.cwd(),
        env: process.env,
        detached: true,
        stdio: ["pipe", "ignore", "ignore"],
      });
      child.on("error", () => {});
      child.stdin.on("error", () => {});
      child.stdin.end(JSON.stringify({ hook_event_name: hookEventName, session_id: sessionID, cwd: process.cwd() }));
      child.unref();
    } catch {
      // Recovered by the next Stop or by SessionEnd.
    }
  };

  let currentState = "idle";
  let rootSessionID = null;
  const childSessionCache = new Map();
  const messageRoleByID = new Map();
  const assistantTextByMessageID = new Map();
  const latestAssistantBySessionID = new Map();
  const toolInputByCallID = new Map();

  const asRecord = (value) => {
    if (!value || typeof value !== "object" || Array.isArray(value)) {
      return null;
    }
    return value;
  };

  const getMessageKey = (sessionID, messageID) => String(sessionID) + ":" + String(messageID);
  const getToolCallKey = (sessionID, callID) => String(sessionID) + ":" + String(callID);

  const encodePayload = (payload) => {
    if (!payload || typeof payload !== "object") {
      return "";
    }
    try {
      return Buffer.from(JSON.stringify(payload), "utf8").toString("base64");
    } catch {
      return "";
    }
  };

	const notify = async (kind, payload) => {
		if (!boardHooks) return;
		try {
			const encoded = encodePayload(payload);
			if (kind === "review") {
				if (encoded) {
					await $\`${reviewCmd} --metadata-base64 \${encoded}\`;
				} else {
					await $\`${reviewCmd}\`;
				}
				return;
			}
			if (kind === "in_progress") {
				if (encoded) {
					await $\`${toInProgressCmd} --metadata-base64 \${encoded}\`;
				} else {
					await $\`${toInProgressCmd}\`;
				}
				return;
			}
			if (encoded) {
				await $\`${activityCmd} --metadata-base64 \${encoded}\`;
			} else {
				await $\`${activityCmd}\`;
			}
		} catch {
			// Best effort: hook errors should never break OpenCode event handling.
		}
	};

  const notifyReview = async (sessionID, payload = {}) => {
    const mergedPayload = {
      ...payload,
      last_assistant_message:
        typeof payload.last_assistant_message === "string"
          ? payload.last_assistant_message
          : (latestAssistantBySessionID.get(sessionID) ?? undefined),
    };
		await notify("review", mergedPayload);
  };

  const notifyInProgress = async (payload = {}) => {
		await notify("in_progress", payload);
  };

  const notifyActivity = async (payload = {}) => {
		await notify("activity", payload);
  };

  const isChildSession = async (sessionID) => {
    if (!sessionID) return true;
    if (!client?.session?.list) return true;
    if (childSessionCache.has(sessionID)) {
      return childSessionCache.get(sessionID);
    }
    try {
      const sessions = await client.session.list();
      const session = sessions.data?.find((candidate) => candidate.id === sessionID);
      const isChild = !!session?.parentID;
      childSessionCache.set(sessionID, isChild);
      return isChild;
    } catch {
      return true;
    }
  };

  const handleBusy = async (sessionID) => {
    if (!sessionID) {
      return;
    }
    if (!rootSessionID) {
      rootSessionID = sessionID;
    }
    if (sessionID !== rootSessionID) {
      return;
    }
    if (currentState === "idle") {
      currentState = "busy";
      await notifyInProgress({
        hook_event_name: "session.status",
      });
    }
  };

  const handleReview = async (sessionID, payload = {}, force = false) => {
    if (!sessionID) {
      return;
    }
    if (!rootSessionID) {
      rootSessionID = sessionID;
    }
    if (rootSessionID && sessionID !== rootSessionID) {
      return;
    }

    const shouldNotify = force || currentState === "busy";
    if (shouldNotify) {
      currentState = "idle";
      await notifyReview(sessionID, payload);
      rootSessionID = null;
    }
  };

  return {
    event: async ({ event }) => {
      if (event.type === "message.updated") {
        const info = asRecord(event.properties?.info);
        const sessionID = typeof info?.sessionID === "string" ? info.sessionID : null;
        if (await isChildSession(sessionID)) {
          return;
        }

        const messageID = typeof info?.id === "string" ? info.id : null;
        const role = typeof info?.role === "string" ? info.role : null;
        if (messageID && role) {
          messageRoleByID.set(getMessageKey(sessionID, messageID), role);
          if (role === "assistant" && !assistantTextByMessageID.has(getMessageKey(sessionID, messageID))) {
            assistantTextByMessageID.set(getMessageKey(sessionID, messageID), "");
          }
        }
        return;
      }

      if (event.type === "message.part.updated") {
        const part = asRecord(event.properties?.part);
        if (!part) {
          return;
        }

        const sessionID = typeof part.sessionID === "string" ? part.sessionID : null;
        if (await isChildSession(sessionID)) {
          return;
        }

        if (part.type !== "text") {
          return;
        }

        const messageID = typeof part.messageID === "string" ? part.messageID : null;
        if (!messageID) {
          return;
        }

        const messageKey = getMessageKey(sessionID, messageID);
        if (messageRoleByID.get(messageKey) !== "assistant") {
          return;
        }

        const delta = typeof event.properties?.delta === "string" ? event.properties.delta : "";
        const fullText = typeof part.text === "string" ? part.text : "";
        const previousText = assistantTextByMessageID.get(messageKey) ?? "";
        const nextText = delta ? previousText + delta : (fullText || previousText);
        const normalized = nextText.trim();
        if (!normalized) {
          return;
        }

        assistantTextByMessageID.set(messageKey, normalized);
        latestAssistantBySessionID.set(sessionID, normalized);
        return;
      }

      // A runtime ask arrives as this event; the pinned OpenCode never calls the "permission.ask"
      // hook below. An ask anywhere in the task (a subagent's too) waits on the user in OpenCode's
      // own terminal, so the card moves to review.
      if (event.type === "permission.asked") {
        const askSessionID = typeof event.properties?.sessionID === "string" ? event.properties.sessionID : null;
        if (!askSessionID) {
          return;
        }
        const reviewSessionID = (await isChildSession(askSessionID)) ? (rootSessionID ?? askSessionID) : askSessionID;
        await handleReview(
          reviewSessionID,
          {
            hook_event_name: "PermissionRequest",
            notification_type: "permission.asked",
          },
          true,
        );
        return;
      }

      const sessionID = event.properties?.sessionID;
      if (await isChildSession(sessionID)) {
        return;
      }

      if (event.type === "session.status") {
        const status = event.properties?.status;
        if (status?.type === "busy") {
          await handleBusy(sessionID);
        } else if (status?.type === "idle") {
          void runLaunchHook("Stop", sessionID);
          await handleReview(sessionID, {
            hook_event_name: "session.status",
          });
        }
      }

      if (event.type === "session.busy") {
        await handleBusy(sessionID);
      }
      if (event.type === "session.idle") {
        await handleReview(sessionID, {
          hook_event_name: "session.idle",
        });
      }
      if (event.type === "session.error") {
        await handleReview(
          sessionID,
          {
            hook_event_name: "session.error",
          },
          true,
        );
      }
    },
    // Awaited by OpenCode with no timeout: return at once, checking the session in the background.
    "experimental.session.compacting": async (input) => {
      const sessionID = typeof input?.sessionID === "string" ? input.sessionID : null;
      if (!launchHook || !sessionID) return;
      void isChildSession(sessionID).then((child) => (child ? undefined : runLaunchHook("PreCompact", sessionID)));
    },
    "tool.execute.before": async (input, output) => {
      const sessionID = typeof input?.sessionID === "string" ? input.sessionID : null;
      if (await isChildSession(sessionID)) {
        return;
      }

      await handleBusy(sessionID);

      const toolName = typeof input?.tool === "string" ? input.tool : undefined;
      const callID = typeof input?.callID === "string" ? input.callID : "";
      const toolInput = asRecord(output?.args);
      if (callID) {
        toolInputByCallID.set(getToolCallKey(sessionID, callID), toolInput);
      }

      await notifyActivity({
        hook_event_name: "BeforeTool",
        tool_name: toolName,
        tool_input: toolInput ?? undefined,
      });
    },
    "tool.execute.after": async (input) => {
      const sessionID = typeof input?.sessionID === "string" ? input.sessionID : null;
      if (await isChildSession(sessionID)) {
        return;
      }

      const toolName = typeof input?.tool === "string" ? input.tool : undefined;
      const callID = typeof input?.callID === "string" ? input.callID : "";
      const toolInput = callID ? toolInputByCallID.get(getToolCallKey(sessionID, callID)) : null;
      if (callID) {
        toolInputByCallID.delete(getToolCallKey(sessionID, callID));
      }

      await notifyActivity({
        hook_event_name: "AfterTool",
        tool_name: toolName,
        tool_input: toolInput ?? undefined,
      });
    },
    "permission.ask": async (_permission, output) => {
      if (output?.status === "ask") {
        const sessionID = typeof _permission?.sessionID === "string" ? _permission.sessionID : null;
        if (await isChildSession(sessionID)) {
          return;
        }
        await handleReview(
          sessionID,
          {
            hook_event_name: "PermissionRequest",
            notification_type: "permission.asked",
          },
          true,
        );
      }
    },
  };
};
`;
}

function getHookAgentDirectory(agentId: RuntimeAgentId): string {
	return join(getRuntimeHomePath(), "hooks", agentId);
}

const KIRO_KANBAN_AGENT_NAME = "kanban";

function getKiroAgentConfigPath(): string {
	return join(homedir(), ".kiro", "agents", `${KIRO_KANBAN_AGENT_NAME}.json`);
}

async function ensureTextFile(filePath: string, content: string, executable = false): Promise<void> {
	await lockedFileSystem.writeTextFileAtomic(filePath, content, {
		executable,
	});
}

function withPrompt(args: string[], prompt: string, mode: "append" | "flag", flag?: string): PreparedAgentLaunch {
	const trimmed = prompt.trim();
	if (!trimmed) {
		return {
			args,
			env: {},
		};
	}
	if (mode === "flag" && flag) {
		args.push(flag, trimmed);
	} else {
		args.push(trimmed);
	}
	return {
		args,
		env: {},
	};
}

function toBracketedPasteSubmission(command: string): string {
	return `\u001b[200~${command}\u001b[201~\r`;
}

const claudeAdapter: AgentSessionAdapter = {
	async prepare(input) {
		const binding = input.launchBinding ?? null;
		if (binding && input.resumeFromTrash && binding.nativeSessionId !== null) {
			// A launch that mints its session id cannot also continue a previous session.
			throw new Error("A task bound to a desk starts a new Claude session; it cannot resume the previous one.");
		}
		const args = [...input.args];
		if (hasCliOption(args, "--dangerously-skip-permissions")) {
			throw new Error("Claude permission bypass is not available in this build.");
		}
		const env: Record<string, string | undefined> = {
			FORCE_HYPERLINK: "1",
		};
		const appendedSystemPrompt = resolveHomeAgentAppendSystemPrompt(input.taskId);
		if (input.autonomousModeEnabled) {
			// Auto mode is gated behind this env var on Bedrock/Vertex/Foundry; the Anthropic API ignores it.
			env.CLAUDE_CODE_ENABLE_AUTO_MODE = "1";
		}
		if (
			input.autonomousModeEnabled &&
			!input.startInPlanMode &&
			!hasCliOption(args, "--permission-mode") &&
			!hasCliOption(args, "--dangerously-skip-permissions")
		) {
			args.push("--permission-mode", "auto");
		}
		if (input.resumeFromTrash && !hasCliOption(args, "--continue")) {
			args.push("--continue");
		}
		if (input.startInPlanMode) {
			const withoutImmediateBypass = args.filter((arg) => arg !== "--dangerously-skip-permissions");
			args.length = 0;
			args.push(...withoutImmediateBypass);
			args.push("--permission-mode", "plan");
		}

		const hooks = resolveHookContext(input);
		if (hooks) {
			const settingsPath = join(getHookAgentDirectory("claude"), "settings.json");
			const hooksSettings = {
				hooks: {
					Stop: [{ hooks: [{ type: "command", command: buildHookCommand("to_review", { source: "claude" }) }] }],
					SubagentStop: [
						{ hooks: [{ type: "command", command: buildHookCommand("activity", { source: "claude" }) }] },
					],
					PreToolUse: [
						{
							matcher: "*",
							hooks: [{ type: "command", command: buildHookCommand("activity", { source: "claude" }) }],
						},
					],
					PermissionRequest: [
						{
							matcher: "*",
							hooks: [{ type: "command", command: buildHookCommand("to_review", { source: "claude" }) }],
						},
					],
					PostToolUse: [
						{
							matcher: "*",
							hooks: [{ type: "command", command: buildHookCommand("to_in_progress", { source: "claude" }) }],
						},
					],
					PostToolUseFailure: [
						{
							matcher: "*",
							hooks: [{ type: "command", command: buildHookCommand("to_in_progress", { source: "claude" }) }],
						},
					],
					Notification: [
						{
							matcher: "permission_prompt",
							hooks: [{ type: "command", command: buildHookCommand("to_review", { source: "claude" }) }],
						},
						{
							matcher: "*",
							hooks: [{ type: "command", command: buildHookCommand("activity", { source: "claude" }) }],
						},
					],
					UserPromptSubmit: [
						{
							hooks: [{ type: "command", command: buildHookCommand("to_in_progress", { source: "claude" }) }],
						},
					],
				},
			};
			if (binding) {
				// One settings file carries both the launch's capture hooks and Kanban's hooks.
				args.push(...(await mergeClaudeLaunchSettings(binding, hooksSettings.hooks)));
			} else {
				await ensureTextFile(settingsPath, JSON.stringify(hooksSettings, null, 2));
				args.push("--settings", settingsPath);
			}
			Object.assign(
				env,
				createHookRuntimeEnv({
					taskId: hooks.taskId,
					workspaceId: hooks.workspaceId,
				}),
			);
		} else if (binding) {
			args.push(...(await mergeClaudeLaunchSettings(binding, null)));
		}
		if (binding) {
			Object.assign(env, binding.envAdditions);
		}

		if (
			appendedSystemPrompt &&
			!hasCliOption(args, "--append-system-prompt") &&
			!hasCliOption(args, "--system-prompt")
		) {
			args.push("--append-system-prompt", appendedSystemPrompt);
		}

		const withPromptLaunch = withPrompt(args, input.prompt, "append");
		return {
			...withPromptLaunch,
			env: {
				...withPromptLaunch.env,
				...env,
			},
		};
	},
};

function codexPromptDetector(data: string, summary: RuntimeTaskSessionSummary): SessionTransitionEvent | null {
	if (summary.state !== "awaiting_review") {
		return null;
	}
	if (summary.reviewReason !== "attention" && summary.reviewReason !== "hook") {
		return null;
	}
	const stripped = stripAnsi(data);
	if (/(?:^|\n)\s*›/.test(stripped)) {
		return { type: "agent.prompt-ready" };
	}
	return null;
}

function shouldInspectCodexOutputForTransition(summary: RuntimeTaskSessionSummary): boolean {
	return (
		summary.state === "awaiting_review" &&
		(summary.reviewReason === "attention" || summary.reviewReason === "hook" || summary.reviewReason === "error")
	);
}

const codexAdapter: AgentSessionAdapter = {
	async prepare(input) {
		const codexArgs = [...input.args];
		const env: Record<string, string | undefined> = {};
		const binary = input.binary;
		let deferredStartupInput: string | undefined;
		const appendedSystemPrompt = resolveHomeAgentAppendSystemPrompt(input.taskId);

		if (!hasCodexConfigOverride(codexArgs, "check_for_update_on_startup")) {
			codexArgs.push("-c", "check_for_update_on_startup=false");
		}

		if (!hasCliOption(codexArgs, "--sandbox")) codexArgs.push("--sandbox", "workspace-write");
		if (!hasCliOption(codexArgs, "--ask-for-approval")) codexArgs.push("--ask-for-approval", "on-request");

		if (input.resumeFromTrash) {
			if (!codexArgs.includes("resume")) {
				codexArgs.push("resume");
			}
			if (!hasCliOption(codexArgs, "--last")) {
				codexArgs.push("--last");
			}
		}

		if (appendedSystemPrompt && !hasCodexConfigOverride(codexArgs, "developer_instructions")) {
			codexArgs.push("-c", `developer_instructions=${JSON.stringify(appendedSystemPrompt)}`);
		}

		const hooks = resolveHookContext(input);
		const binding = input.launchBinding ?? null;
		if (hooks) {
			if (binding) {
				await applyCodexLaunchBinding(codexArgs, binding, true);
			} else {
				configureCodexHooks(codexArgs);
			}
			Object.assign(
				env,
				createHookRuntimeEnv({
					taskId: hooks.taskId,
					workspaceId: hooks.workspaceId,
				}),
			);
		} else if (binding) {
			await applyCodexLaunchBinding(codexArgs, binding, false);
		}
		if (binding) {
			Object.assign(env, binding.envAdditions);
		}

		const trimmed = input.prompt.trim();
		if (input.startInPlanMode) {
			const planCommand = trimmed ? `/plan ${trimmed}` : "/plan";
			deferredStartupInput = toBracketedPasteSubmission(planCommand);
		} else if (trimmed) {
			codexArgs.push(trimmed);
		}

		if (hooks) {
			return {
				binary,
				args: codexArgs,
				env,
				deferredStartupInput,
				detectOutputTransition: codexPromptDetector,
				shouldInspectOutputForTransition: shouldInspectCodexOutputForTransition,
			};
		}

		return {
			binary,
			args: codexArgs,
			env,
			deferredStartupInput,
			detectOutputTransition: codexPromptDetector,
			shouldInspectOutputForTransition: shouldInspectCodexOutputForTransition,
		};
	},
};

const geminiAdapter: AgentSessionAdapter = {
	async prepare(input) {
		const args = [...input.args];
		const env: Record<string, string | undefined> = {};

		if (input.autonomousModeEnabled && !hasCliOption(args, "--yolo")) {
			args.push("--yolo");
		}

		if (input.resumeFromTrash && !hasCliOption(args, "--resume")) {
			args.push("--resume", "latest");
		}

		if (input.startInPlanMode) {
			args.push("--approval-mode=plan");
		}

		const hooks = resolveHookContext(input);
		if (hooks) {
			const configPath = join(getHookAgentDirectory("gemini"), "settings.json");
			const geminiHookCommand = buildHooksCommand(["gemini-hook"]);

			const config = {
				hooks: {
					BeforeTool: [
						{
							hooks: [{ type: "command", command: geminiHookCommand }],
						},
					],
					AfterTool: [
						{
							hooks: [{ type: "command", command: geminiHookCommand }],
						},
					],
					AfterAgent: [
						{
							hooks: [{ type: "command", command: geminiHookCommand }],
						},
					],
					BeforeAgent: [
						{
							hooks: [{ type: "command", command: geminiHookCommand }],
						},
					],
					Notification: [
						{
							hooks: [{ type: "command", command: geminiHookCommand }],
						},
					],
				},
			};
			await ensureTextFile(configPath, JSON.stringify(config, null, 2));
			Object.assign(
				env,
				createHookRuntimeEnv({
					taskId: hooks.taskId,
					workspaceId: hooks.workspaceId,
				}),
			);
			env.GEMINI_CLI_SYSTEM_SETTINGS_PATH = configPath;
		}

		const trimmed = input.prompt.trim();
		if (trimmed) {
			args.push("-i", trimmed);
			return {
				args,
				env,
			};
		}

		return {
			args,
			env,
		};
	},
};

async function resolveOpenCodeBaseConfigPath(explicitPath: string | undefined): Promise<string | null> {
	const candidates = getOpenCodeConfigPathCandidates({ explicitPath });
	for (const candidate of candidates) {
		try {
			await access(candidate);
			return candidate;
		} catch {
			// Keep searching.
		}
	}
	return null;
}

function hasOpenCodeModelArg(args: string[]): boolean {
	for (const arg of args) {
		if (arg === "--model" || arg === "-m") {
			return true;
		}
		if (arg.startsWith("--model=") || arg.startsWith("-m=")) {
			return true;
		}
	}
	return false;
}

function normalizeOpenCodeModel(providerId: string, modelId: string): string {
	if (modelId.startsWith(`${providerId}/`)) {
		return modelId;
	}
	return `${providerId}/${modelId}`;
}

function stripJsonComments(input: string): string {
	let output = "";
	let inString = false;
	let escaped = false;
	let inLineComment = false;
	let inBlockComment = false;

	for (let i = 0; i < input.length; i += 1) {
		const current = input[i];
		const next = i + 1 < input.length ? input[i + 1] : "";

		if (inLineComment) {
			if (current === "\n") {
				inLineComment = false;
				output += current;
			}
			continue;
		}
		if (inBlockComment) {
			if (current === "*" && next === "/") {
				inBlockComment = false;
				i += 1;
			}
			continue;
		}
		if (!inString && current === "/" && next === "/") {
			inLineComment = true;
			i += 1;
			continue;
		}
		if (!inString && current === "/" && next === "*") {
			inBlockComment = true;
			i += 1;
			continue;
		}

		output += current;
		if (inString) {
			if (escaped) {
				escaped = false;
			} else if (current === "\\") {
				escaped = true;
			} else if (current === '"') {
				inString = false;
			}
			continue;
		}
		if (current === '"') {
			inString = true;
		}
	}
	return output;
}

function tryExtractOpenCodeModelFromConfig(rawConfig: string): string | null {
	let parsed: unknown;
	try {
		parsed = JSON.parse(rawConfig);
	} catch {
		try {
			parsed = JSON.parse(stripJsonComments(rawConfig));
		} catch {
			return null;
		}
	}
	if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
		return null;
	}
	const root = parsed as Record<string, unknown>;

	const directModel = root.model;
	if (typeof directModel === "string" && directModel.trim()) {
		return directModel.trim();
	}

	const mode = root.mode;
	if (mode && typeof mode === "object" && !Array.isArray(mode)) {
		const build = (mode as Record<string, unknown>).build;
		if (build && typeof build === "object" && !Array.isArray(build)) {
			const model = (build as Record<string, unknown>).model;
			if (typeof model === "string" && model.trim()) {
				return model.trim();
			}
		}
	}

	const agent = root.agent;
	if (agent && typeof agent === "object" && !Array.isArray(agent)) {
		const build = (agent as Record<string, unknown>).build;
		if (build && typeof build === "object" && !Array.isArray(build)) {
			const model = (build as Record<string, unknown>).model;
			if (typeof model === "string" && model.trim()) {
				return model.trim();
			}
		}
	}

	return null;
}

async function resolveOpenCodePreferredModelArg(configPath: string | null): Promise<string | null> {
	if (configPath) {
		try {
			const rawConfig = await readFile(configPath, "utf8");
			const modelFromConfig = tryExtractOpenCodeModelFromConfig(rawConfig);
			if (modelFromConfig) {
				return modelFromConfig;
			}
		} catch {
			// Fall through to state-based fallback.
		}
	}

	const modelStateCandidates = getOpenCodeModelStatePathCandidates();
	let recentModels: Array<{ providerID?: unknown; modelID?: unknown }> = [];
	for (const modelStatePath of modelStateCandidates) {
		try {
			const raw = await readFile(modelStatePath, "utf8");
			const parsed = JSON.parse(raw) as { recent?: Array<{ providerID?: unknown; modelID?: unknown }> };
			if (Array.isArray(parsed.recent)) {
				recentModels = parsed.recent;
				break;
			}
		} catch {
			// Keep searching through candidate state paths.
		}
	}
	if (recentModels.length === 0) {
		return null;
	}

	const configuredProviders = new Set<string>();
	for (const authPath of getOpenCodeAuthPathCandidates()) {
		try {
			const raw = await readFile(authPath, "utf8");
			const parsed = JSON.parse(raw) as Record<string, unknown>;
			for (const [provider, value] of Object.entries(parsed)) {
				if (!value || typeof value !== "object" || Array.isArray(value)) {
					continue;
				}
				const key = (value as Record<string, unknown>).key;
				if (typeof key === "string" && key.trim()) {
					configuredProviders.add(provider);
				}
			}
			break;
		} catch {
			// Keep searching through candidate auth paths.
		}
	}

	const candidates: Array<{ providerId: string; model: string }> = [];
	for (const entry of recentModels) {
		const providerId = typeof entry.providerID === "string" ? entry.providerID.trim() : "";
		const modelId = typeof entry.modelID === "string" ? entry.modelID.trim() : "";
		if (!providerId || !modelId) {
			continue;
		}
		candidates.push({ providerId, model: normalizeOpenCodeModel(providerId, modelId) });
	}
	if (candidates.length === 0) {
		return null;
	}

	const preferredProviderOrder = ["openrouter", "anthropic", "openai", "opencode", "google", "amazon-bedrock"];
	for (const providerId of preferredProviderOrder) {
		const match = candidates.find((candidate) => candidate.providerId === providerId);
		if (!match) {
			continue;
		}
		if (configuredProviders.size === 0 || configuredProviders.has(providerId)) {
			return match.model;
		}
	}

	const configuredMatch = candidates.find((candidate) => configuredProviders.has(candidate.providerId));
	if (configuredMatch) {
		return configuredMatch.model;
	}

	return candidates[0].model;
}

/**
 * The approval policy of a board OpenCode launch (order O1, P1): edits stay inside the task worktree, as
 * Codex's workspace-write sandbox keeps them, and every command and web fetch waits for the user, the
 * stricter reading of Codex's on-request approvals.
 */
const OPENCODE_BOARD_PERMISSION = {
	edit: "allow",
	bash: "ask",
	webfetch: "ask",
	external_directory: "deny",
} as const;

/**
 * Order O2, R2: a desk task's launch binding reaches the OpenCode process, per launch. The binding carries
 * its desk memory server in OPENCODE_CONFIG_CONTENT, a per-process source that OpenCode merges ABOVE the
 * generated file (docs read section 4; measured on the pinned binary: a document holding only `mcp`
 * leaves `opencode debug agent build`'s ruleset exactly O1's), and its hook command, a JSON argv, in
 * KP_AGENT_LAUNCH_HOOK_COMMAND. The adapter accepts exactly that shape: the content may hold only the
 * one `kp_desk_memory` local server, so nothing in it can name a permission, agent, tool or plugin, and
 * the binding may not set any variable the policy environment owns.
 */
const OPENCODE_LAUNCH_CONTENT_ENV = "OPENCODE_CONFIG_CONTENT";
const OPENCODE_LAUNCH_HOOK_ENV = "KP_AGENT_LAUNCH_HOOK_COMMAND";
const OPENCODE_LAUNCH_SESSION_SCHEMA = "agent-tooling.launch-session.v1";
const OPENCODE_SESSION_END_TIMEOUT_MS = 180_000;
const openCodeLaunchContentSchema = z
	.object({
		mcp: z
			.object({
				kp_desk_memory: z
					.object({
						type: z.literal("local"),
						command: z.array(z.string().min(1).max(4096)).min(1).max(32),
						environment: z
							.record(z.string().regex(/^[A-Za-z_][A-Za-z0-9_]{0,127}$/), z.string().max(4096))
							.optional(),
						enabled: z.literal(true).optional(),
					})
					.strict(),
			})
			.strict(),
	})
	.strict();
const openCodeLaunchHookSchema = z.array(z.string().min(1).max(4096)).min(1).max(20);
const openCodeLaunchSessionSchema = z.object({
	schema_version: z.literal(OPENCODE_LAUNCH_SESSION_SCHEMA),
	native_session_id: z.string().regex(/^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/),
	transcript: z.string().nullable(),
});
/**
 * Provider credentials the launched process never inherits (O2, R2): OpenRouter's key reaches OpenCode
 * only through the generated file's `{file:...}` reference. Every variable named *_API_KEY in the board's
 * environment is cleared as well.
 */
const OPENCODE_PROVIDER_CREDENTIALS = [
	"OPENCODE_AUTH_CONTENT",
	"ANTHROPIC_AUTH_TOKEN",
	"AWS_ACCESS_KEY_ID",
	"AWS_SECRET_ACCESS_KEY",
	"AWS_SESSION_TOKEN",
	"AWS_BEARER_TOKEN_BEDROCK",
];
/** The operator's OpenRouter key file (the model gateway's), named to the board by the Compose manifest. */
const OPENCODE_OPENROUTER_KEY_FILE_ENV = "KANBAN_OPENCODE_OPENROUTER_KEY_FILE";

/** Entries of HOME/.opencode that OpenCode's installer or dependency step writes; none is configuration. */
const OPENCODE_HOME_DIRECTORY_INERT_ENTRIES = new Set([
	".DS_Store",
	".gitignore",
	"bin",
	"bun.lock",
	"bun.lockb",
	"node_modules",
	"package-lock.json",
	"package.json",
]);

/**
 * The launched OpenCode reads its configuration from the generated file and from directories the board
 * owns. Measured on the pinned binary (O1 A1): a permission map merges key by key and keeps each key at
 * the position of its first source, so a project or global file that places "*" (or a pattern map)
 * after a policy key loosens the policy from below, whatever source the board uses above it. The
 * environment therefore turns off every source a repository or user file can feed:
 * - OPENCODE_DISABLE_PROJECT_CONFIG: the worktree's opencode.json[c] and its .opencode directories
 *   (agents, commands, plugins);
 * - XDG_CONFIG_HOME: the global config directory (~/.config/opencode) moves to a board-owned one;
 * - OPENCODE_CONFIG_DIR, OPENCODE_CONFIG_CONTENT, OPENCODE_PERMISSION, OPENCODE_TEST_MANAGED_CONFIG_DIR:
 *   inherited values outrank the generated file, and an empty value reads as unset;
 * - OPENCODE_TEST_HOME: an empty value makes OpenCode read the worktree's .opencode as HOME's, so it is
 *   pinned to the home the launch checked.
 * HOME/.opencode cannot be moved; the launch refuses while it carries configuration.
 */
function buildOpenCodePolicyEnvironment(configPath: string, configHome: string, home: string): Record<string, string> {
	return {
		OPENCODE_CONFIG: configPath,
		OPENCODE_DISABLE_PROJECT_CONFIG: "1",
		XDG_CONFIG_HOME: configHome,
		OPENCODE_CONFIG_DIR: "",
		OPENCODE_CONFIG_CONTENT: "",
		OPENCODE_PERMISSION: "",
		OPENCODE_TEST_MANAGED_CONFIG_DIR: "",
		OPENCODE_TEST_HOME: home,
		// The policy was measured at the pinned version; a launch never upgrades the binary under it.
		OPENCODE_DISABLE_AUTOUPDATE: "1",
		// O2, R2: no session is shared and the model catalogue is never fetched (the binary's own snapshot
		// serves); Claude Code's CLAUDE.md and skills under HOME are another harness's instructions, and
		// measured at the pin they reach the model's request unless this is set.
		OPENCODE_DISABLE_SHARE: "1",
		OPENCODE_DISABLE_MODELS_FETCH: "1",
		OPENCODE_DISABLE_CLAUDE_CODE: "1",
	};
}

/**
 * O2, R2 (Verification A1): a bound launch installs no package. Measured at the pin, OpenCode runs a
 * background npm install of @opencode-ai/plugin into every WRITABLE config directory at every launch
 * (an existing node_modules or package.json does not stop it), so the bound launch's global config
 * directory is board-owned, empty and read-only (0555). It must stay empty: a file there would be a
 * global config source beneath the policy.
 */
async function ensureReadOnlyOpenCodeConfigHome(configHome: string): Promise<void> {
	const directory = join(configHome, "opencode");
	await mkdir(directory, { recursive: true });
	const entries = await readdir(directory);
	if (entries.length > 0) {
		throw new Error(`${directory} must stay empty for a desk launch of OpenCode; the task was not started.`);
	}
	await chmod(directory, 0o555);
}

function parseLaunchJson(value: string): unknown {
	try {
		return JSON.parse(value);
	} catch {
		return null;
	}
}

/** The binding's environment additions for OpenCode, checked against the one shape O2 reviewed. */
function openCodeLaunchBindingEnvironment(
	binding: LaunchBinding,
	policy: Record<string, string>,
): Record<string, string> {
	if (binding.argvAdditions.length > 0) {
		throw new Error(
			"An OpenCode launch binding carries its configuration in its environment; argv additions are not reviewed.",
		);
	}
	for (const [name, value] of Object.entries(binding.envAdditions)) {
		if (name === OPENCODE_LAUNCH_CONTENT_ENV) {
			if (!openCodeLaunchContentSchema.safeParse(parseLaunchJson(value)).success) {
				throw new Error(
					"The OpenCode launch configuration may hold only its desk memory server; the task was not started.",
				);
			}
		} else if (name === OPENCODE_LAUNCH_HOOK_ENV) {
			if (!openCodeLaunchHookSchema.safeParse(parseLaunchJson(value)).success) {
				throw new Error("The OpenCode launch hook command is not a command argv; the task was not started.");
			}
		} else if (
			name in policy ||
			name.startsWith("OPENCODE_") ||
			name.startsWith("XDG_") ||
			name === "HOME" ||
			name === "PATH" ||
			isOpenCodeProviderCredential(name)
		) {
			throw new Error(`An OpenCode launch binding may not set ${name}; the task was not started.`);
		}
	}
	if (!(OPENCODE_LAUNCH_CONTENT_ENV in binding.envAdditions) || !(OPENCODE_LAUNCH_HOOK_ENV in binding.envAdditions)) {
		throw new Error(
			"The OpenCode launch binding names no desk memory server or hook command; the task was not started.",
		);
	}
	return { ...binding.envAdditions };
}

function isOpenCodeProviderCredential(name: string): boolean {
	return /_API_KEY$/.test(name) || OPENCODE_PROVIDER_CREDENTIALS.includes(name);
}

/** Provider credentials in the board's environment, cleared for the launched process. */
function clearedOpenCodeCredentials(input: AgentAdapterLaunchInput): Record<string, string> {
	const inherited = { ...process.env, ...input.env };
	const names = Object.keys(inherited).filter((name) => inherited[name] && isOpenCodeProviderCredential(name));
	return Object.fromEntries(names.map((name) => [name, ""]));
}

/** `{file:...}` for the operator's OpenRouter key file when the board names a readable one; never the key. */
async function resolveOpenRouterKeyReference(): Promise<string | null> {
	const path = process.env[OPENCODE_OPENROUTER_KEY_FILE_ENV]?.trim();
	if (!path || !isAbsolute(path) || /[{}\r\n]/.test(path)) {
		return null;
	}
	try {
		await access(path, fsConstants.R_OK);
	} catch {
		return null;
	}
	return `{file:${path}}`;
}

/**
 * The board terminal's exit is the session's end (O2, R1): the launch's hook runs with SessionEnd for the
 * session its first hook bound (the launch directory's session.json), so a Stop that failed or was missed
 * is recovered. A launch whose session was never bound has nothing to capture.
 */
async function runOpenCodeSessionEnd(
	binding: LaunchBinding,
	hookCommand: string[],
	cwd: string,
	env: Record<string, string | undefined>,
): Promise<void> {
	let session: string;
	try {
		const record = openCodeLaunchSessionSchema.parse(
			JSON.parse(await readFile(join(dirname(binding.receiptPath), "session.json"), "utf8")),
		);
		session = record.native_session_id;
	} catch {
		return;
	}
	const childEnv = Object.fromEntries(
		Object.entries(env).filter((entry): entry is [string, string] => entry[1] !== undefined),
	);
	await new Promise<void>((resolve) => {
		const child = spawn(hookCommand[0] as string, hookCommand.slice(1), {
			cwd,
			env: childEnv,
			stdio: ["pipe", "ignore", "ignore"],
		});
		const timer = setTimeout(() => child.kill(), OPENCODE_SESSION_END_TIMEOUT_MS);
		const done = () => {
			clearTimeout(timer);
			resolve();
		};
		child.on("error", done);
		child.on("close", done);
		child.stdin.on("error", () => {});
		child.stdin.end(JSON.stringify({ hook_event_name: "SessionEnd", session_id: session, cwd }));
	});
}

/** The home the launched OpenCode reads HOME/.opencode from (its environment, then the account's). */
function resolveOpenCodeLaunchHome(input: AgentAdapterLaunchInput): string {
	const home = input.env?.HOME || process.env.HOME || homedir();
	if (!isAbsolute(home)) {
		throw new Error("OpenCode launches need an absolute HOME.");
	}
	return home;
}

/** Refuse while HOME/.opencode holds anything OpenCode would read above the generated policy. */
async function assertNoOpenCodeHomeConfiguration(home: string): Promise<void> {
	const directory = join(home, ".opencode");
	let entries: string[];
	try {
		entries = await readdir(directory);
	} catch (error) {
		if ((error as NodeJS.ErrnoException).code === "ENOENT") {
			return;
		}
		throw new Error(`OpenCode reads ${directory}, which the board cannot inspect; the task was not started.`);
	}
	const configured = entries.filter((entry) => !OPENCODE_HOME_DIRECTORY_INERT_ENTRIES.has(entry)).sort();
	if (configured.length > 0) {
		throw new Error(
			`OpenCode reads ${directory} (${configured.join(", ")}) above the board's approval policy. ` +
				"Move that configuration out of it to launch OpenCode from the board; the task was not started.",
		);
	}
}

const opencodeAdapter: AgentSessionAdapter = {
	async preflight(input) {
		if (input.startInPlanMode) {
			// The policy's edit "allow" follows the plan agent's own edit rules, so plan mode would edit.
			throw new Error("OpenCode plan mode is not reviewed under the board's approval policy.");
		}
		await assertNoOpenCodeHomeConfiguration(resolveOpenCodeLaunchHome(input));
	},
	async prepare(input) {
		const home = resolveOpenCodeLaunchHome(input);
		await assertNoOpenCodeHomeConfiguration(home);
		const args = [...input.args];
		const env: Record<string, string | undefined> = {};
		// A caller's OPENCODE_CONFIG stays the model-resolution base; it is never the launched config.
		const baseConfigPath = await resolveOpenCodeBaseConfigPath(input.env?.OPENCODE_CONFIG);
		if (input.resumeFromTrash && !hasCliOption(args, "--continue")) {
			args.push("--continue");
		}

		const opencodeDirectory = getHookAgentDirectory("opencode");
		const pluginPath = join(opencodeDirectory, "kanban.js");
		const configPath = join(opencodeDirectory, "opencode.json");
		const pluginContent = buildOpenCodePluginContent(
			buildHookCommand("to_review", { source: "opencode" }),
			buildHookCommand("to_in_progress", { source: "opencode" }),
			buildHookCommand("activity", { source: "opencode" }),
		);
		await ensureTextFile(pluginPath, pluginContent);
		const openRouterKey = await resolveOpenRouterKeyReference();
		const config = {
			permission: OPENCODE_BOARD_PERMISSION,
			plugin: [pathToFileURL(pluginPath).href],
			...(openRouterKey ? { provider: { openrouter: { options: { apiKey: openRouterKey } } } } : {}),
		};
		await ensureTextFile(configPath, JSON.stringify(config));
		// The desk memory server and the capture hook travel per launch (O2), never in this board-wide file.
		const binding = input.launchBinding ?? null;
		const configHome = join(opencodeDirectory, binding ? "bound-config-home" : "config-home");
		const policy = buildOpenCodePolicyEnvironment(configPath, configHome, home);
		const bindingEnv = binding ? openCodeLaunchBindingEnvironment(binding, policy) : null;
		if (bindingEnv) {
			await ensureReadOnlyOpenCodeConfigHome(configHome);
		}
		Object.assign(env, policy, clearedOpenCodeCredentials(input), bindingEnv ?? {});

		// The plugin moves the card only for a launch with board hooks (it reads KANBAN_HOOK_TASK_ID).
		const hooks = resolveHookContext(input);
		if (hooks) {
			Object.assign(
				env,
				createHookRuntimeEnv({
					taskId: hooks.taskId,
					workspaceId: hooks.workspaceId,
				}),
			);
		}

		// Workaround: with --prompt, OpenCode can pick an unexpected provider/model.
		// Explicitly pass the user's preferred model so prompt runs stay on their usual provider.
		if (!hasOpenCodeModelArg(args)) {
			const preferredModel = await resolveOpenCodePreferredModelArg(baseConfigPath);
			if (preferredModel) {
				args.push("--model", preferredModel);
			}
		}

		const trimmed = input.prompt.trim();
		if (trimmed) {
			args.push("--prompt", trimmed);
		}
		if (!binding || !bindingEnv) {
			return { args, env };
		}
		const hookCommand = openCodeLaunchHookSchema.parse(JSON.parse(bindingEnv[OPENCODE_LAUNCH_HOOK_ENV] as string));
		const launchEnv = { ...process.env, ...input.env, ...env };
		return {
			args,
			env,
			cleanup: () => runOpenCodeSessionEnd(binding, hookCommand, input.cwd, launchEnv),
		};
	},
};

const droidAdapter: AgentSessionAdapter = {
	async prepare(input) {
		const args = [...input.args];
		const env: Record<string, string | undefined> = {};

		if (input.resumeFromTrash && !hasCliOption(args, "--resume") && !hasCliOption(args, "-r")) {
			args.push("--resume");
		}

		const hooks = resolveHookContext(input);
		const shouldWriteSettings = Boolean(hooks) || input.startInPlanMode || input.autonomousModeEnabled !== undefined;
		if (shouldWriteSettings) {
			const settingsPath = join(getHookAgentDirectory("droid"), "settings.json");
			const settings: Record<string, unknown> = {
				autonomyMode: input.startInPlanMode ? "spec" : input.autonomousModeEnabled ? "auto-high" : "normal",
			};

			if (hooks) {
				const droidActiveToolMatcher = "Read|Grep|Glob|FetchUrl|WebSearch|Execute|Task|Edit|Create";
				const reviewNotifyCommand = buildHooksCommand(["notify", "--event", "to_review", "--source", "droid"]);
				const inProgressNotifyCommand = buildHooksCommand([
					"notify",
					"--event",
					"to_in_progress",
					"--source",
					"droid",
				]);
				const activityNotifyCommand = buildHooksCommand(["notify", "--event", "activity", "--source", "droid"]);
				settings.hooks = {
					Stop: [{ hooks: [{ type: "command", command: reviewNotifyCommand }] }],
					Notification: [
						{ hooks: [{ type: "command", command: activityNotifyCommand }] },
						{ hooks: [{ type: "command", command: reviewNotifyCommand }] },
					],
					PreToolUse: [
						{ matcher: "*", hooks: [{ type: "command", command: activityNotifyCommand }] },
						{ matcher: droidActiveToolMatcher, hooks: [{ type: "command", command: inProgressNotifyCommand }] },
						{ matcher: "AskUser", hooks: [{ type: "command", command: reviewNotifyCommand }] },
					],
					PostToolUse: [
						{ matcher: "*", hooks: [{ type: "command", command: activityNotifyCommand }] },
						{ matcher: "AskUser", hooks: [{ type: "command", command: inProgressNotifyCommand }] },
					],
					PostToolUseFailure: [{ matcher: "*", hooks: [{ type: "command", command: activityNotifyCommand }] }],
					UserPromptSubmit: [{ hooks: [{ type: "command", command: inProgressNotifyCommand }] }],
				};

				Object.assign(
					env,
					createHookRuntimeEnv({
						taskId: hooks.taskId,
						workspaceId: hooks.workspaceId,
					}),
				);
			}

			await ensureTextFile(settingsPath, JSON.stringify(settings, null, 2));
			if (!hasCliOption(args, "--settings")) {
				args.push("--settings", settingsPath);
			}
		}

		const appendedSystemPrompt = resolveHomeAgentAppendSystemPrompt(input.taskId);
		if (
			appendedSystemPrompt &&
			!hasCliOption(args, "--append-system-prompt") &&
			!hasCliOption(args, "--system-prompt")
		) {
			args.push("--append-system-prompt", appendedSystemPrompt);
		}

		const withPromptLaunch = withPrompt(args, input.prompt, "append");
		return {
			...withPromptLaunch,
			env: {
				...withPromptLaunch.env,
				...env,
			},
		};
	},
};

const kiroAdapter: AgentSessionAdapter = {
	async prepare(input) {
		const args = [...input.args];
		const env: Record<string, string | undefined> = {};

		if (input.autonomousModeEnabled && !hasCliOption(args, "--trust-all-tools")) {
			args.push("--trust-all-tools");
		}

		if (input.resumeFromTrash && !hasCliOption(args, "--resume") && !hasCliOption(args, "-r")) {
			args.push("--resume");
		}

		const hooks = resolveHookContext(input);
		const appendedSystemPrompt = resolveHomeAgentAppendSystemPrompt(input.taskId);
		if (hooks || appendedSystemPrompt) {
			const configPath = getKiroAgentConfigPath();
			const config: Record<string, unknown> = {
				name: KIRO_KANBAN_AGENT_NAME,
				description: "Kanban-managed Kiro agent with hook forwarding.",
				tools: ["*"],
			};

			if (hooks) {
				config.hooks = {
					agentSpawn: [
						{
							command: buildHookCommand("to_in_progress", {
								source: "kiro",
								hookEventName: "agentSpawn",
							}),
						},
					],
					userPromptSubmit: [
						{
							command: buildHookCommand("to_in_progress", {
								source: "kiro",
								hookEventName: "userPromptSubmit",
							}),
						},
					],
					preToolUse: [
						{
							command: buildHookCommand("activity", {
								source: "kiro",
								hookEventName: "preToolUse",
							}),
						},
						{
							command: buildHookCommand("to_in_progress", {
								source: "kiro",
								hookEventName: "preToolUse",
							}),
						},
					],
					postToolUse: [
						{
							command: buildHookCommand("activity", {
								source: "kiro",
								hookEventName: "postToolUse",
							}),
						},
					],
					stop: [
						{
							command: buildHookCommand("to_review", {
								source: "kiro",
								hookEventName: "stop",
								activityText: "Waiting for review",
							}),
						},
					],
				};
				Object.assign(
					env,
					createHookRuntimeEnv({
						taskId: hooks.taskId,
						workspaceId: hooks.workspaceId,
					}),
				);
			}

			if (appendedSystemPrompt) {
				config.prompt = appendedSystemPrompt;
			}

			await ensureTextFile(configPath, JSON.stringify(config, null, 2));
			if (!hasCliOption(args, "--agent")) {
				args.push("--agent", KIRO_KANBAN_AGENT_NAME);
			}
		}

		const trimmedPrompt = input.prompt.trim();
		const planPrompt = input.startInPlanMode
			? [
					"First, inspect the codebase and produce a clear implementation plan only.",
					"Do not modify files, do not use write tools, and do not implement anything yet.",
					"After you present the plan, ask for approval before making changes.",
					trimmedPrompt
						? `\n\nTask:\n${trimmedPrompt}`
						: " Ask the user what they want planned if the task is unclear.",
				].join(" ")
			: input.prompt;
		const withPromptLaunch = withPrompt(args, planPrompt, "append");
		return {
			...withPromptLaunch,
			env: {
				...withPromptLaunch.env,
				...env,
			},
		};
	},
};

const clineAdapter: AgentSessionAdapter = {
	async prepare(input) {
		const args = [...input.args];
		const env: Record<string, string | undefined> = {};

		if (input.autonomousModeEnabled && !hasCliOption(args, "--auto-approve-all")) {
			args.push("--auto-approve-all");
		}

		if (input.resumeFromTrash && !hasCliOption(args, "--continue")) {
			args.push("--continue");
		}

		if (input.startInPlanMode) {
			args.push("--plan");
		}

		const hooks = resolveHookContext(input);
		if (hooks) {
			const hooksDir = getHookAgentDirectory("cline");
			const notificationHookPath = getClineHookScriptPath(hooksDir, "Notification");
			const taskCompleteHookPath = getClineHookScriptPath(hooksDir, "TaskComplete");
			const userPromptSubmitHookPath = getClineHookScriptPath(hooksDir, "UserPromptSubmit");
			const preToolUseHookPath = getClineHookScriptPath(hooksDir, "PreToolUse");
			const postToolUseHookPath = getClineHookScriptPath(hooksDir, "PostToolUse");
			const executable = process.platform !== "win32";

			await ensureTextFile(notificationHookPath, buildClineNotificationHookScriptContent(), executable);
			await ensureTextFile(taskCompleteHookPath, buildClineHookScriptContent("to_review"), executable);
			await ensureTextFile(userPromptSubmitHookPath, buildClineHookScriptContent("to_in_progress"), executable);
			await ensureTextFile(preToolUseHookPath, buildClinePreToolUseHookScriptContent(), executable);
			await ensureTextFile(postToolUseHookPath, buildClinePostToolUseHookScriptContent(), executable);

			if (!hasCliOption(args, "--hooks-dir")) {
				args.push("--hooks-dir", hooksDir);
			}

			Object.assign(
				env,
				createHookRuntimeEnv({
					taskId: hooks.taskId,
					workspaceId: hooks.workspaceId,
				}),
			);
		}

		const withPromptLaunch = withPrompt(args, input.prompt, "append");
		return {
			...withPromptLaunch,
			env: {
				...withPromptLaunch.env,
				...env,
			},
		};
	},
};

const ADAPTERS: Record<RuntimeAgentId, AgentSessionAdapter> = {
	claude: claudeAdapter,
	codex: codexAdapter,
	gemini: geminiAdapter,
	opencode: opencodeAdapter,
	droid: droidAdapter,
	kiro: kiroAdapter,
	cline: clineAdapter,
};

export async function prepareAgentLaunch(input: AgentAdapterLaunchInput): Promise<PreparedAgentLaunch> {
	if (!isRuntimeAgentLaunchSupported(input.agentId)) {
		throw new Error(`Agent ${input.agentId} is not ready for launch under a verified approval policy.`);
	}
	if (input.binary !== undefined && input.binary !== input.agentId) {
		throw new Error(`Agent ${input.agentId} must launch through its catalog binary in this trial.`);
	}
	if (input.args.length !== 0) {
		throw new Error(`Custom ${input.agentId} launch arguments are not reviewed for this trial.`);
	}
	await ADAPTERS[input.agentId].preflight?.(input);
	const preparedPrompt = await prepareTaskPromptWithImages({
		prompt: input.prompt,
		images: input.images,
	});
	// A task without a desk gets no launch binding and launches exactly as before.
	const launchBinding = await resolveLaunchBinding(input);
	const launch = await ADAPTERS[input.agentId].prepare({
		...input,
		prompt: preparedPrompt,
		launchBinding,
	});
	return bindAssistantMemory(input, launch);
}
