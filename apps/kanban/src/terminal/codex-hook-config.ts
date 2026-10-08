import { createHash } from "node:crypto";

import type { RuntimeHookEvent } from "../core/api-contract";
import { buildKanbanCommandParts } from "../core/kanban-command";
import { quoteShellArg } from "../core/shell";

const CODEX_HOOK_TIMEOUT_SECONDS = 5;

type CodexHookConfigEvent = "PermissionRequest" | "PostToolUse" | "PreToolUse" | "Stop" | "UserPromptSubmit";

type JsonValue = JsonPrimitive | JsonArray | JsonObject;
type JsonPrimitive = boolean | null | number | string;
type JsonArray = JsonValue[];
interface JsonObject {
	[key: string]: JsonValue;
}

interface CodexHookConfig {
	eventName: CodexHookConfigEvent;
	matcher?: string;
	command: string;
}

interface CodexHookTrustEntry {
	key: string;
	trustedHash: string;
}

export function hasCodexConfigOverride(args: string[], key: string): boolean {
	for (let i = 0; i < args.length; i += 1) {
		const arg = args[i];
		if (arg === "-c" || arg === "--config") {
			const next = args[i + 1];
			if (typeof next === "string" && next.startsWith(`${key}=`)) {
				return true;
			}
			i += 1;
			continue;
		}
		if (arg.startsWith(`-c${key}=`) || arg.startsWith(`--config=${key}=`)) {
			return true;
		}
	}
	return false;
}

function findCodexConfigOverrideInsertIndex(args: string[]): number {
	const subcommandIndex = args.findIndex((arg) => arg === "resume" || arg === "fork");
	return subcommandIndex === -1 ? args.length : subcommandIndex;
}

function addCodexConfigOverrideBeforeSubcommand(args: string[], key: string, value: string): void {
	if (hasCodexConfigOverride(args, key)) {
		return;
	}
	args.splice(findCodexConfigOverrideInsertIndex(args), 0, "-c", `${key}=${value}`);
}

function buildCodexHookCommand(event: RuntimeHookEvent): string {
	return buildKanbanCommandParts(["hooks", "codex-hook", "--event", event, "--source", "codex"])
		.map(quoteShellArg)
		.join(" ");
}

function buildCodexHookConfigValue(command: string, matcher?: string): string {
	const matcherConfig = matcher ? `matcher=${JSON.stringify(matcher)},` : "";
	const commandConfig = JSON.stringify(command);
	return `[{${matcherConfig}hooks=[{type="command",command=${commandConfig},timeout=${CODEX_HOOK_TIMEOUT_SECONDS}}]}]`;
}

function codexSessionFlagsConfigSource(): string {
	return process.platform === "win32" ? String.raw`C:\<session-flags>\config.toml` : "/<session-flags>/config.toml";
}

/** Hook commands another launcher (the launch binding) installs beside Kanban's own, by event. */
export interface CodexAdditionalHookCommand {
	command: string;
	timeout: number;
}
export type CodexAdditionalHooks = Record<string, CodexAdditionalHookCommand[]>;

interface CodexHookGroupConfig {
	eventName: string;
	matcher?: string;
	command: string;
	timeout: number;
}

const CODEX_HOOK_EVENT_NAME = /^[A-Z][A-Za-z]{0,63}$/;

function codexAnyHookEventKeyLabel(eventName: string): string {
	return eventName.replace(/(?<!^)(?=[A-Z])/g, "_").toLowerCase();
}

function codexHookEventKeyLabel(eventName: CodexHookConfigEvent): string {
	switch (eventName) {
		case "PermissionRequest":
			return "permission_request";
		case "PostToolUse":
			return "post_tool_use";
		case "PreToolUse":
			return "pre_tool_use";
		case "Stop":
			return "stop";
		case "UserPromptSubmit":
			return "user_prompt_submit";
	}
}

function canonicalizeJson(value: JsonValue): JsonValue {
	if (Array.isArray(value)) {
		return value.map(canonicalizeJson);
	}
	if (value !== null && typeof value === "object") {
		const sorted: JsonObject = {};
		for (const key of Object.keys(value).sort()) {
			sorted[key] = canonicalizeJson(value[key]);
		}
		return sorted;
	}
	return value;
}

function versionForCodexHookIdentity(value: JsonObject): string {
	const canonical = canonicalizeJson(value);
	const serialized = JSON.stringify(canonical);
	const hash = createHash("sha256").update(serialized).digest("hex");
	return `sha256:${hash}`;
}

function buildCodexHookTrustEntry(config: CodexHookConfig): CodexHookTrustEntry {
	const handler: JsonObject = {
		async: false,
		command: config.command,
		timeout: CODEX_HOOK_TIMEOUT_SECONDS,
		type: "command",
	};
	const group: JsonObject = {
		hooks: [handler],
	};
	if (config.matcher !== undefined) {
		group.matcher = config.matcher;
	}
	const eventKeyLabel = codexHookEventKeyLabel(config.eventName);
	const identity: JsonObject = {
		event_name: eventKeyLabel,
		...group,
	};
	return {
		key: `${codexSessionFlagsConfigSource()}:${eventKeyLabel}:0:0`,
		trustedHash: versionForCodexHookIdentity(identity),
	};
}

function buildCodexHookGroupTrustEntry(group: CodexHookGroupConfig, groupIndex: number): CodexHookTrustEntry {
	const handler: JsonObject = {
		async: false,
		command: group.command,
		timeout: group.timeout,
		type: "command",
	};
	const identityGroup: JsonObject = { hooks: [handler] };
	if (group.matcher !== undefined) {
		identityGroup.matcher = group.matcher;
	}
	const eventKeyLabel = codexAnyHookEventKeyLabel(group.eventName);
	return {
		key: `${codexSessionFlagsConfigSource()}:${eventKeyLabel}:${groupIndex}:0`,
		trustedHash: versionForCodexHookIdentity({ event_name: eventKeyLabel, ...identityGroup }),
	};
}

function buildCodexHookGroupsConfigValue(groups: CodexHookGroupConfig[]): string {
	return `[${groups
		.map((group) => {
			const matcherConfig = group.matcher ? `matcher=${JSON.stringify(group.matcher)},` : "";
			return `{${matcherConfig}hooks=[{type="command",command=${JSON.stringify(group.command)},timeout=${group.timeout}}]}`;
		})
		.join(",")}]`;
}

function buildCodexHookTrustStateConfigValue(entries: CodexHookTrustEntry[]): string {
	const states = entries.map(
		(entry) => `${JSON.stringify(entry.key)}={trusted_hash=${JSON.stringify(entry.trustedHash)}}`,
	);
	return `{${states.join(",")}}`;
}

export function configureCodexHooks(args: string[], additionalHooks?: CodexAdditionalHooks): void {
	if (additionalHooks && Object.keys(additionalHooks).length > 0) {
		configureCodexHooksWithAdditional(args, additionalHooks);
		return;
	}
	const inProgressHook: CodexHookConfig = {
		eventName: "UserPromptSubmit",
		command: buildCodexHookCommand("to_in_progress"),
	};
	const reviewHook: CodexHookConfig = {
		eventName: "Stop",
		command: buildCodexHookCommand("to_review"),
	};
	const permissionRequestHook: CodexHookConfig = {
		eventName: "PermissionRequest",
		command: buildCodexHookCommand("to_review"),
		matcher: "*",
	};
	const preToolUseActivityHook: CodexHookConfig = {
		eventName: "PreToolUse",
		command: buildCodexHookCommand("activity"),
		matcher: "*",
	};
	const postToolUseActivityHook: CodexHookConfig = {
		eventName: "PostToolUse",
		command: buildCodexHookCommand("activity"),
		matcher: "*",
	};
	const trustStateConfigValue = buildCodexHookTrustStateConfigValue(
		[inProgressHook, reviewHook, permissionRequestHook, preToolUseActivityHook, postToolUseActivityHook].map(
			buildCodexHookTrustEntry,
		),
	);

	addCodexConfigOverrideBeforeSubcommand(args, "features.hooks", "true");
	addCodexConfigOverrideBeforeSubcommand(args, "hooks.state", trustStateConfigValue);
	addCodexConfigOverrideBeforeSubcommand(
		args,
		"hooks.UserPromptSubmit",
		buildCodexHookConfigValue(inProgressHook.command),
	);
	addCodexConfigOverrideBeforeSubcommand(args, "hooks.Stop", buildCodexHookConfigValue(reviewHook.command));
	addCodexConfigOverrideBeforeSubcommand(
		args,
		"hooks.PermissionRequest",
		buildCodexHookConfigValue(permissionRequestHook.command, permissionRequestHook.matcher),
	);
	addCodexConfigOverrideBeforeSubcommand(
		args,
		"hooks.PreToolUse",
		buildCodexHookConfigValue(preToolUseActivityHook.command, preToolUseActivityHook.matcher),
	);
	addCodexConfigOverrideBeforeSubcommand(
		args,
		"hooks.PostToolUse",
		buildCodexHookConfigValue(postToolUseActivityHook.command, postToolUseActivityHook.matcher),
	);
}

function kanbanCodexHookGroups(): CodexHookGroupConfig[] {
	const timeout = CODEX_HOOK_TIMEOUT_SECONDS;
	return [
		{ eventName: "UserPromptSubmit", command: buildCodexHookCommand("to_in_progress"), timeout },
		{ eventName: "Stop", command: buildCodexHookCommand("to_review"), timeout },
		{ eventName: "PermissionRequest", command: buildCodexHookCommand("to_review"), matcher: "*", timeout },
		{ eventName: "PreToolUse", command: buildCodexHookCommand("activity"), matcher: "*", timeout },
		{ eventName: "PostToolUse", command: buildCodexHookCommand("activity"), matcher: "*", timeout },
	];
}

/**
 * Kanban's hooks plus another launcher's hooks in one value per event. Each event
 * keeps Kanban's group first; the additional groups follow, and every group is
 * trusted for this session at its own index.
 */
function configureCodexHooksWithAdditional(args: string[], additionalHooks: CodexAdditionalHooks): void {
	const groupsByEvent = new Map<string, CodexHookGroupConfig[]>();
	for (const group of kanbanCodexHookGroups()) {
		groupsByEvent.set(group.eventName, [...(groupsByEvent.get(group.eventName) ?? []), group]);
	}
	for (const [eventName, commands] of Object.entries(additionalHooks)) {
		if (!CODEX_HOOK_EVENT_NAME.test(eventName)) {
			throw new Error("Launch binding names an invalid Codex hook event.");
		}
		for (const { command, timeout } of commands) {
			groupsByEvent.set(eventName, [...(groupsByEvent.get(eventName) ?? []), { eventName, command, timeout }]);
		}
	}
	const trustEntries = [...groupsByEvent.values()].flatMap((groups) =>
		groups.map((group, index) => buildCodexHookGroupTrustEntry(group, index)),
	);
	addCodexConfigOverrideBeforeSubcommand(args, "features.hooks", "true");
	addCodexConfigOverrideBeforeSubcommand(args, "hooks.state", buildCodexHookTrustStateConfigValue(trustEntries));
	for (const [eventName, groups] of groupsByEvent) {
		addCodexConfigOverrideBeforeSubcommand(args, `hooks.${eventName}`, buildCodexHookGroupsConfigValue(groups));
	}
}

/** Insert `-c key=value` pairs before a `resume`/`fork` subcommand; keys already set are kept. */
export function addCodexConfigOverrides(args: string[], overrides: Array<{ key: string; value: string }>): void {
	for (const { key, value } of overrides) {
		addCodexConfigOverrideBeforeSubcommand(args, key, value);
	}
}
