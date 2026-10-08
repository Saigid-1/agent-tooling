// Order O1 (docs/work/orders/O1-opencode-board-launch.md, with amendment-1): the spellings the TEST
// arm assumed where the order leaves FEATURE to name them, written once. The meet reconciles each
// with FEATURE's own by editing this file (and tests/image/o1_seams.py) only.
//
// Fixed by the order, not seams:
// - the policy (P1): permission {"edit": "allow", "bash": "ask", "webfetch": "ask",
//   "external_directory": "deny"}, exactly; `external_directory` exists at the pinned
//   opencode-ai@1.18.34, so the order's fallback (`edit` becomes "ask") does not apply;
// - the launch list is RUNTIME_LAUNCH_SUPPORTED_AGENT_IDS (src/core/agent-catalog.ts), read through
//   getRuntimeLaunchSupportedAgentCatalog(); the catalog's `baseArgs` stay empty (P2);
// - every board launch goes through prepareAgentLaunch (src/terminal/agent-session-adapters.ts)
//   from TerminalSessionManager.startTaskSession, which always passes the board's workspaceId.
//
// Assumed (TEST, blind)
// O1-S1 "the generated opencode.json" is the file the launch's own environment names in
//       OPENCODE_CONFIG (P2: "OPENCODE_CONFIG is always the generated file").
// O1-S2 "a board launch" is prepareAgentLaunch with a workspaceId (the hook context), as
//       runtime-api.ts starts every terminal task session.
// O1-S3 "the launched process" is the PtySession.spawn request TerminalSessionManager.startTaskSession
//       builds (process env, then the request env, then the launch env), executed: the tests run
//       a stub `opencode` on PATH with exactly that binary, argv, cwd and environment.
// O1-S4 a refusal is a rejected prepareAgentLaunch (any message): the order names no wording. Each
//       refusal test carries a positive control (the plain OpenCode launch proceeds), so a gate that
//       refuses OpenCode outright cannot pass it.

import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

/** P1, verbatim. */
export const O1_POLICY = {
	edit: "allow",
	bash: "ask",
	webfetch: "ask",
	external_directory: "deny",
} as const;

/** P3: the harnesses that hold a verified approval policy once O1 lands. */
export const O1_LAUNCH_SUPPORTED = ["claude", "codex", "opencode"] as const;

/** P3: harnesses without a verified policy; each must still be refused. */
export const O1_POLICYLESS = ["gemini", "droid", "kiro", "cline"] as const;

/** A board launch of OpenCode (O1-S2). */
export const O1_BOARD_LAUNCH = {
	agentId: "opencode" as const,
	binary: "opencode",
	args: [] as string[],
	autonomousModeEnabled: true,
	prompt: "Fix the failing test",
	workspaceId: "o1-workspace",
};

export interface O1TempHome {
	root: string;
	home: string;
	cwd: string;
	restore(): void;
}

const ISOLATED_ENV = ["HOME", "KANBAN_STORAGE_ROOT", "OPENCODE_CONFIG", "XDG_CONFIG_HOME", "APPDATA", "LOCALAPPDATA"];

/**
 * A fresh HOME (the runtime home, the user's OpenCode config paths) and task worktree, with every
 * variable the adapter reads for OpenCode cleared; restore() puts the process back.
 */
export function o1TempHome(): O1TempHome {
	const root = mkdtempSync(join(tmpdir(), "o1-opencode-"));
	const home = join(root, "home");
	const cwd = join(root, "worktree");
	mkdirSync(home, { recursive: true });
	mkdirSync(cwd, { recursive: true });
	const saved = new Map(ISOLATED_ENV.map((key) => [key, process.env[key]]));
	for (const key of ISOLATED_ENV) delete process.env[key];
	process.env.HOME = home;
	return {
		root,
		home,
		cwd,
		restore() {
			for (const [key, value] of saved) {
				if (value === undefined) delete process.env[key];
				else process.env[key] = value;
			}
			rmSync(root, { recursive: true, force: true });
		},
	};
}

/** A caller's own OpenCode config that loosens everything (P2's "caller's OPENCODE_CONFIG"). */
export function writeCallerConfig(dir: string): { path: string; text: string } {
	const path = join(dir, "caller-opencode.json");
	const text = `${JSON.stringify({
		model: "openrouter/o1/caller-model",
		permission: { "*": "allow", edit: "allow", bash: "allow", webfetch: "allow", external_directory: "allow" },
	})}\n`;
	writeFileSync(path, text);
	return { path, text };
}
