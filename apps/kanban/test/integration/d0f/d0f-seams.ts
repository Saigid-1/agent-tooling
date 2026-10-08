// Order D0f (docs/work/orders/D0f-board-without-agent-sdk.md): every spelling the
// TEST arm assumed where the order leaves FEATURE to name it, reconciled at the meet
// with FEATURE's own (the FEATURE arm's seams). Change it here and nowhere else in the
// Kanban tests. The image tests carry the same seams in tests/install/d0f_harness.py.
//
// Fixed by the code, not seams: the Claude provider's id in @clinebot/llms is
// "claude-code"; the packages are @anthropic-ai/claude-agent-sdk and
// ai-sdk-provider-claude-code; "the version the lockfile pins" is read from
// apps/kanban/package-lock.json.
//
// Assumed (TEST, blind)                          -> FEATURE's spelling
// S1 notice file CLAUDE-AGENT-SDK-NOTICE.md      -> CLAUDE_AGENT_SDK_NOTICE_TEXT in
//                                                   src/optional-providers/claude-agent-sdk-notice.ts
// S2 runtime.getClaudeAgentSdkStatus             -> the same query; its fields map in parseClaudeSdkStatus
//    installed                                   -> state ("installed" true, "not_installed" false; any
//                                                   other state is kept as that string, so it equals neither)
//    version                                     -> installed.sdkVersion (the install record), which must
//                                                   equal provider.sdkVersion when the state is "installed"
//    defaultVersion                              -> pinned.sdkVersion
//    notice, noticeSha256                        -> notice.text, notice.sha256
// S3 runtime.installClaudeAgentSdk               -> the same mutation; its input maps in installInput
//    {acknowledged: true, noticeSha256}          -> {acknowledgedNoticeSha256: noticeSha256} ("" when none named)
//    {acknowledged: false}                       -> {acknowledgedNoticeSha256: null}
//    {version}                                   -> {sdkVersion}
//    success {ok: true}                          -> {ok: true, code: "installed"}
// S4 scripts/build.mjs into KANBAN_BUILD_OUTDIR  -> the same (build.mjs reads KANBAN_BUILD_OUTDIR)
// S5 npm, npm_config_registry                    -> the same (the action runs npm with the board's env)
// S6 anywhere under the state root               -> <runtime home>/optional-packages/claude-agent-sdk,
//                                                   under the state root; the tests still look under the
//                                                   whole state root, which is stricter for absence

import { CLAUDE_AGENT_SDK_NOTICE_TEXT } from "../../../src/optional-providers/claude-agent-sdk-notice";

export const D0F_SEAMS = {
	/** S1. Where the notice text is pinned in the repo. Relative to apps/kanban. */
	noticeModule: "src/optional-providers/claude-agent-sdk-notice.ts",
	/** S2. The Claude provider's install state and the action's offer: a tRPC query, no input. */
	statusQuery: "runtime.getClaudeAgentSdkStatus",
	/** S3. The install action: a tRPC mutation (see installInput). */
	installMutation: "runtime.installClaudeAgentSdk",
	/** S4. The board bundle is what scripts/build.mjs emits into KANBAN_BUILD_OUTDIR. */
	buildScript: "scripts/build.mjs",
	/**
	 * S5. The action fetches through npm, which takes its registry from the board's
	 * environment. The tests point it at a local registry that serves stand-in packages.
	 */
	registryEnv: "npm_config_registry",
	/**
	 * S6. Where the action installs: FEATURE names <runtime home>/optional-packages/claude-agent-sdk,
	 * under the state root (the board's HOME, /state in the image). The tests find an installed
	 * package anywhere under the state root by its package.json.
	 */
	installedPackageJson: "node_modules/@anthropic-ai/claude-agent-sdk/package.json",
} as const;

/** S1: the notice text as pinned in the repo (FEATURE pins it as a string constant, not a file). */
export function pinnedNoticeText(): string {
	if (typeof CLAUDE_AGENT_SDK_NOTICE_TEXT !== "string" || CLAUDE_AGENT_SDK_NOTICE_TEXT.length === 0) {
		throw new Error(`${D0F_SEAMS.noticeModule} pins no notice text (CLAUDE_AGENT_SDK_NOTICE_TEXT)`);
	}
	return CLAUDE_AGENT_SDK_NOTICE_TEXT;
}

/** S2's response, as the tests read it. */
export interface ClaudeSdkStatus {
	/**
	 * True when the provider is installed and registered, false when it reads "not installed".
	 * Any other state ("installing", "unavailable") is kept as that string, so a test expecting
	 * either true or false fails on it.
	 */
	installed: boolean | "installing" | "unavailable";
	/** The installed SDK version, or null. */
	version: string | null;
	/** The version the action installs when the user picks none: the lockfile's pin. */
	defaultVersion: string;
	/** The notice the board shows before the action, exactly the pinned text. */
	notice: string;
	/** sha256 (hex) of that text: what an acknowledgement names. */
	noticeSha256: string;
	/** FEATURE's state, as answered. */
	state: string;
}

const STATES = ["not_installed", "installing", "installed", "unavailable"] as const;

function asRecord(value: unknown): Record<string, unknown> | null {
	return value !== null && typeof value === "object" && !Array.isArray(value)
		? (value as Record<string, unknown>)
		: null;
}

/** Reads S2's response, or names what it lacks. */
export function parseClaudeSdkStatus(data: unknown): ClaudeSdkStatus {
	const value = asRecord(data) ?? {};
	const problems: string[] = [];
	const state = value.state;
	if (typeof state !== "string" || !(STATES as readonly string[]).includes(state)) {
		problems.push(`state: one of ${STATES.join(" | ")}`);
	}
	const pinned = asRecord(value.pinned);
	if (typeof pinned?.sdkVersion !== "string") problems.push("pinned.sdkVersion: string");
	const notice = asRecord(value.notice);
	if (typeof notice?.text !== "string") problems.push("notice.text: string");
	if (typeof notice?.sha256 !== "string") problems.push("notice.sha256: string");
	const installedRecord = value.installed === null ? null : asRecord(value.installed);
	if (value.installed !== null && typeof installedRecord?.sdkVersion !== "string") {
		problems.push("installed: null | {sdkVersion: string}");
	}
	const provider = asRecord(value.provider);
	if (typeof provider?.registered !== "boolean") problems.push("provider.registered: boolean");
	if (!(provider?.sdkVersion === null || typeof provider?.sdkVersion === "string")) {
		problems.push("provider.sdkVersion: string | null");
	}
	const version = installedRecord ? (installedRecord.sdkVersion as string) : null;
	if (state === "installed" && (provider?.registered !== true || provider?.sdkVersion !== version)) {
		problems.push(
			`state installed: the provider registered with the recorded SDK version (registered ${String(provider?.registered)}, ` +
				`loaded ${String(provider?.sdkVersion)}, recorded ${String(version)})`,
		);
	}
	if (state === "not_installed" && version !== null) {
		problems.push(`state not_installed with an install record at ${version}`);
	}
	if (problems.length > 0) {
		throw new Error(`${D0F_SEAMS.statusQuery} answered ${JSON.stringify(data)}; it lacks ${problems.join(", ")}`);
	}
	return {
		installed:
			state === "installed" ? true : state === "not_installed" ? false : (state as "installing" | "unavailable"),
		version,
		defaultVersion: pinned?.sdkVersion as string,
		notice: notice?.text as string,
		noticeSha256: notice?.sha256 as string,
		state: state as string,
	};
}

/**
 * S3's input. The conservative reading of "the notice shown AND acknowledged": the
 * acknowledgement names the notice it acknowledges (its sha256), so an
 * acknowledgement of any other text, or none, installs nothing. FEATURE's request has
 * no separate flag: the digest is the acknowledgement. `acknowledged: false` sends an
 * explicit null; `acknowledged: true` naming no notice sends an empty digest.
 */
export function installInput(input: {
	acknowledged?: boolean;
	noticeSha256?: string;
	version?: string;
}): Record<string, unknown> {
	const body: Record<string, unknown> = {};
	if (input.acknowledged === true) body.acknowledgedNoticeSha256 = input.noticeSha256 ?? "";
	if (input.acknowledged === false) body.acknowledgedNoticeSha256 = null;
	if (input.version !== undefined) body.sdkVersion = input.version;
	return body;
}

/** S3's response: success is `ok: true` with code "installed" (a refusal is ok: false or a tRPC error). */
export function installSucceeded(data: unknown): boolean {
	const value = asRecord(data);
	return value?.ok === true && value?.code === "installed";
}
